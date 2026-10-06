"""Compacting notes: the application-level policy that ties the notes tab
(utilities/noted_time_sheet.py), the calendar, the planner
(utilities/note_compaction.py) and the write-ahead journal
(utilities/compaction_journal.py) together.

The flow, as the MCP tools expose it:

1. `prepare` -- read-only. Hands the client every day of uncompacted
   notes up to now (each note with a stable id and a shortlist of nearby
   planned events), those days' planned events, and the two side by side
   as a `Timeline` (see utilities/compaction_timeline.py). The client
   compares them and decides, event by event, what the notes show
   happened differently.
2. `dry_run` -- validates the client's decisions and writes the plan to
   the journal as `planned`, returning it (and a compaction id, and the
   resulting timeline) for review. Nothing on the calendar changes.
3. `commit` -- applies a `planned` compaction after checking the notes and
   calendar still match what was previewed, journaling each step as it
   goes, and stamping each day's notes as compacted once that day's steps
   are done. If it dies partway it can simply be called again: the
   journal remembers exactly what was approved and how far it got. A
   compaction that can't be finished can be `abandon`ed.
4. `record_judgments` -- the facts written, the client judges the traits
   of each event's people (see utilities/judgments.py). Nothing about
   judging is sent before the user approves the plan: the commit hands
   back the plan's final timeline and, beside it, the judgments due, in
   one compact list (each event's people and their parts, then each part
   and each person's history once). A compaction isn't complete until
   they're all judged. `judgments_due` hands them over again -- to
   finish, or redo. Once it's complete, the days it settled are rolled up
   into each person's trait scores (see utilities/trait_rollup.py).

A day at a time, all at once: one compaction takes on every day of
uncompacted notes up to now (at most `_MAX_DAYS`, oldest first), but
plans each day on its own, and the user reviews them together. A day is
the one the oldest of the notes left falls in: it starts when the last
end-of-day sleep event that began before that note ends (or at the note,
if it's earlier -- a note written before the planned wake-up time), and
runs to the end of the next end-of-day sleep event after that (or 24
hours, if there isn't one). Each day after the first is planned as if the
one before it had already been compacted: it starts where that one ended
(its `now`), against the calendar as that one's plan would leave it
(`_PlannedCalendar`). A day before the last is wholly past, so its plan
records it and reflows nothing; only the last day, the one with now in
it, has a future to reflow. The night between two days is decided once,
whole, by the earlier day, and its end is the border between them (see
`_cut`): a note the decisions use to end it -- woke early, or slept in
-- moves the border to it, taking every note up to it into the earlier
day, and the later day starts there. After a late wake-up, the later day
still starts at the planned one, and is compacted even without notes of
its own, so the morning the night now runs over is settled there: past
events under it are overlaps to resolve, and later ones reflow after it.
A cancelled night -- no sleep -- makes the two days one long one. In the journal, each day is a compaction of
its own, applied and stamped in order, and together they're a *batch*
under the first day's id (see utilities/compaction_journal.py) -- the
compaction id the MCP tools use. Notes written after now wait for a
later compaction.

A day can take several compactions, so the events offered -- the
*compaction window* -- start at the later of the day's start and the last
*stamped* compaction's `now` (or, for a later day of a batch, the day
before it's): whatever an earlier compaction already
settled isn't offered again. Nor is anything after it skipped: when the
last compaction ran before the night that ends its day (in the evening,
before bed), that day isn't over, so the batch starts with it, at the
last compaction -- even if its oldest note was written during that night
(the user stayed up), or the morning after it (that day's evening and
night are settled first, notes or not). See `_anchor`. The one event that ended within `_LOOKBACK`
before the compaction window starts is offered too, so an event the last
compaction closed off at "now" (or the night's sleep) can still be
stretched. The latest compacted note, however long ago it was written,
is offered as `previous_note`: what the user last said before the window
often says what was going on as it began. The timeline shows it too, and
when the last compaction ran, as context -- for the first day; a later
day has the day before it right above it. Every past event
offered is recorded as on schedule unless
the client's decisions say otherwise (see utilities/note_compaction.py).

The events offered run through each day's end-of-day sleep, because an
event that ran long pushes what follows it later, and the reflow needs
somewhere for that to go. But compaction is about recording the past,
so the timeline shown to the user stops at `now`: a later event appears
in it only if it's near enough to a note to be one of its candidates
(lunch at noon, for an 11:45 "starting lunch") or -- after a dry run --
if the plan changes it.

`prepare` also garbage-collects the journal (`CompactionJournal.
garbage_collect`) before doing anything else -- see there, and
`NotedTimeSheet.garbage_collect` (called from `note`/`append` instead,
since that's where the notes tab grows), for what that means for row
numbers and ids.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Literal

from googleapiclient.errors import HttpError

from calendar_clients.google_calendar import Event
from utilities.actions import Actions, ActionTree
from utilities.compaction_additions import (
    REF_PREFIX,
    Additions,
    NewAction,
    NewLocation,
    NewPerson,
    check_additions,
    create_additions,
    resolve,
)
from utilities.facts import SELF_ID, Facts, fact_problems
from utilities.judgments import Judging, Judgment, JudgmentRequest, JudgmentsDue, JudgmentsResult
from utilities.trait_rollup import TraitRollup
from utilities.locations import Locations
from utilities.people import People
from utilities.compaction_journal import (
    ABANDONED,
    APPLIED,
    APPLYING,
    PLANNED,
    STAMPED,
    CompactionJournal,
    JournalCompaction,
    JournalStep,
    PlannedDay,
)
from utilities.compaction_marker import CompactionMarker
from utilities.cancellations import Cancellations
from utilities.compaction_timeline import Timeline, join_days, render
from utilities.note_compaction import (
    CompactionChange,
    CompactionError,
    CompactionPlan,
    EventDecision,
    PlanNote,
    Problem,
    facts_from_dict,
    plan_compaction,
    planned_timeline,
)
from utilities.noted_time_sheet import NotedTime, NotedTimeSheet, SheetNote
from utilities.reallocating_calendar import ReallocatingCalendar

logger = logging.getLogger(__name__)

_CANDIDATE_WINDOW = timedelta(hours=1)
"""How far either side of a note's time a planned event may start or end
and still be offered to it as a candidate."""

_HINT_HISTORY = timedelta(days=28)
"""How far back actions are looked for to suggest for an event."""

_MAX_DAYS = 7
"""The most days one compaction takes on, oldest first: a longer backlog
takes more than one, so each plan stays small enough to review."""

_LOOKBACK = timedelta(minutes=15)
"""How long before the compaction window starts an event may have ended
and still be offered (only the latest one) -- see the module docstring."""

_ADJACENT = timedelta(hours=48)
"""The longest the oldest note may come after the last compaction for
the batch to start at the last compaction, not the note: no more than
the day after the night after it -- see `NoteCompactor._anchor`."""

_PREFETCH_BEFORE = timedelta(hours=24) + _LOOKBACK
_PREFETCH_AFTER = timedelta(hours=48)
"""What `NoteCompactor._walk` lists up front, around its notes: from the
night before the oldest (a day starts no earlier than 24 hours before
its oldest note, and offers what ended `_LOOKBACK` before that) to two
days past now (where the last day ends, unless more than one night in a
row is cancelled -- a listing past it just isn't answered from this)."""

_APPROVAL_RULE = (
    "Then STOP and wait for the user's reply. Only call compact_notes with dry_run=False once the "
    "user has explicitly approved this plan after seeing it -- never in the same turn as the dry "
    "run. A request to compact made before they saw the plan (\"compact my notes\") isn't approval "
    "of it."
)
"""Repeated wherever a model is told what to do after a dry run: the
server can't tell whether the user replied, so this rests on the model."""

DECISION_GUIDE = (
    "`timeline` shows the notes beside the planned events -- for every day of notes up to now, each "
    "under a heading with its date (`days` lists them when there's more than one). Each day is "
    "compacted on its own, but you decide them all in one list. Compare them and decide, event "
    "by event, what the notes show happened differently -- then call compact_notes with those "
    "`decisions`. SILENCE MEANS ON SCHEDULE: any past event you don't mention is recorded exactly "
    "as planned, so only mention the events the notes contradict. Notes are sparse -- the user "
    "doesn't note every event, so a missing start or end note never means an event didn't happen "
    "or ran into its neighbor. "
    "READ EACH NOTE'S TENSE to tell which edge it marks: most notes are written as something "
    "finishes, so a note in the past tense ('finished the report', 'had lunch', 'done with email', "
    "'went for a walk') marks the END of that event -- it ran up to the note's time, not from it. "
    "Only a note in the future tense ('going to the gym', 'will start the report', 'about to "
    "eat') or one that explicitly says it's starting something ('starting email', 'heading out "
    "to', 'now working on') marks a START. Don't assume a note starts the event it names; when the "
    "tense is ambiguous (a bare 'email'), use the timing and the neighboring notes, and ask if "
    "it's still unclear. "
    "Each decision has an `action`: "
    "'keep' {event_id}: it happened; each edge stays as planned unless you move it -- "
    "{start_note}/{end_note} (a note id) sets that edge to the note's time and links the note to "
    "it, {start}/{end} sets an explicit time (add the note too when it gives a time relative to "
    "itself, e.g. 'leaving 15 minutes early'). 'keep' also takes {summary} to rename an event and "
    "{annotate} to add text to its description. "
    "'cancel' {event_id}: it didn't happen. "
    "'create' {summary, a start and an end (a time or note each), action_ids?, facts?}: something "
    "unplanned happened. "
    "'merge' {event_id, into}: fold one event into another, titled after both -- ONLY when the user "
    "has told you they don't remember where one ended and the other began; never just because the "
    "notes are sparse. "
    "Past events can't overlap: if a moved edge runs into another past event, compact_notes rejects "
    "the plan and names the overlap. Decide which edge gives way (an overrun usually delays or "
    "shortens the next event), and ask the user if the notes don't tell you. "
    "Every note you don't use as a start_note/end_note has its text added to the description of the "
    "event it falls within; list any that shouldn't be in `ignore_notes`. Use only note ids from "
    "`notes` (only this round's -- a longer backlog than this takes another round, see "
    "`remaining_note_count`) and event ids from `events`. "
    "`events` may start with one that ended just before `compaction_window_start` (usually last "
    "night's sleep); if a note shows it actually ran later -- the user slept in -- move its end "
    "with 'keep', and say when whatever it now overlaps happened. "
    "An event with `compacted_until` was settled by an earlier compaction up to then -- usually one "
    "still going on when it ran: its start, and its lasting until then, are fact, so keep its start, "
    "end it no earlier (it may well have run later), and don't cancel or merge it; compact_notes "
    "refuses otherwise. Likewise, what's still going on now is recorded up to now, and the next "
    "compaction says where it ended. When the last compaction ran in the evening, before the "
    "night, this round starts there, at `compaction_window_start`: notes written during the planned "
    "night mean the user stayed up -- move the night's start (bedtime) to when they went to bed -- "
    "and a first day with no notes of its own settles that evening and night before the morning. "
    "`previous_note`, if there is one, is the last note an earlier compaction already used, "
    "however long ago -- context only (it can't be used as a start_note/end_note or ignored): "
    "e.g. if it said 'starting the report' shortly before `compaction_window_start`, the report "
    "was already under way as this window began, and the first note may well be its end; the "
    "longer before the window it was written, the less it says about how the window began. The "
    "timeline shows it too (✓), and when the last compaction ran, both as context. "
    "COMPACTION RECORDS THE PAST: `events` runs to the end of the day, but `timeline` stops at "
    "`now`, except for later events near a note (one may be what a note starts early) and, after "
    "a dry run, later events the plan changes -- e.g. pushes later after an overrun. "
    "A 'keep' that moves a future event reschedules it: it's pinned there and the rest of the day "
    "reflows around it, in the same plan -- for 'move lunch later and adjust the afternoon' "
    "requests. A day's end-of-day sleep event works differently: moving its start moves "
    "bedtime (an earlier one shortens or cancels what runs past it), and its end -- the wake-up "
    "time -- starts the next day. THE NIGHT IS THE BORDER BETWEEN DAYS: give each night at most "
    "one decision, whichever day's heading its notes are under. A note that marks waking up "
    "(early or late) is that night's end_note -- it moves the border to that note, so the notes "
    "up to it belong to the day before; a note in the night that doesn't end it ('can't sleep') "
    "is just added to it. If the user slept in, the morning events the night now runs over are "
    "settled with the next day: a past one it overlaps has to be moved or cancelled (the next day's "
    "decisions may use the wake-up note as an edge too), and later ones reflow after it. Cancel a "
    "night only if the user didn't sleep: its two days then become one long day. When the next "
    "day isn't in this round, compaction never adjusts it, so move only the night's start to "
    "change only bedtime. "
    "COMPACTION ESTABLISHES THE FACTS of every past event, which traits are judged from later -- "
    "so for each past event in the timeline, settle: what the user was doing (its ACTIONS), where "
    "it was, who was there with them, who it was for, and a note on each person who was there. "
    "Settle them from the notes, the title and the description; don't ask the user what the "
    "notes already say, and leave out what doesn't apply (a solo event has no `with_ids`). "
    "ACTIONS: each event's `action_ids` are what the user was doing, each a verb from `actions` "
    "(\"play guitar\", \"eat a meal\"); an event can have several, the first setting its color. "
    "For every past event with `suggested_action_ids`, add a 'keep' {event_id, action_ids} applying "
    "them unless the notes say otherwise; give a 'create' its action_ids too. When no action fits, "
    "add one (see NEW below) rather than force a poor match. 'keep' without action_ids keeps them, "
    "and [] clears them. In the timeline, ◆ marks an action an event already has and ◇ one it's "
    "being given (or, before deciding, one suggested). "
    "FACTS: add `facts` to the event's 'keep' (or 'create'): location_id (from `locations`, matched "
    "by their hints), with_ids (people from `people` who were there; never \"self\" -- the user is at "
    "every event), for_ids (people it was done for who weren't there: preparing a gift or a plan -- "
    "then they're not in with_ids), and notes: {person id: a subjective line on how it was for "
    "them} for each person who was there, \"self\" for the user -- whatever the notes say that "
    "might help judge it later (they were tired, it was their idea, we laughed a lot). The notes "
    "aren't judgments: you don't know the traits, so just record what happened. Facts replace an "
    "event's facts whole, so an event that has some (`facts`) keeps them unless you send new ones. "
    "In the timeline, ▸ marks facts an event has and ▹ ones it's being given. "
    "After a dry run, ⚠ marks a past event still missing its action or location (listed again "
    "under `Missing:` at the end of the day): settle it from the notes and run again, or, if they "
    "don't say, ask the user. "
    "NEW actions, people and locations: when an event's action, a person or a place isn't in the "
    "lists, add it with compact_notes' new_actions, new_people or new_locations -- each with a "
    "`ref` starting \"new:\" (\"new:ukulele\") that your decisions use wherever its id would go -- "
    "rather than by separate tools: they're created when the plan is applied, so the user "
    "confirms them with it. A new action is a verb phrase, its status active (the user approves "
    "it with the plan); a new person needs a context when their name is taken; a new location "
    "needs a hint. Check the lists first: don't add one that's already there under another name. "
    "Before the dry run, confirm with the user anything you couldn't settle from the notes -- who "
    "was there, where it was -- in a short list; after it, they confirm the whole plan. "
    "After every dry run, show the user the result's `timeline.text` verbatim in a code block "
    "(it's laid out narrow enough for a phone, so don't reformat or widen it -- it shows each "
    "event's actions and facts compactly, under it), then the new actions, people and locations "
    "it adds, and the warnings, asking whether to apply it or what to change. " + _APPROVAL_RULE
)


@dataclass(kw_only=True)
class ContextNote:
    id: str
    timestamp: datetime
    description: str | None
    candidates: list[str]
    """Ids of planned events near this note's time, nearest first -- the
    likeliest events for it to start or end."""


@dataclass(kw_only=True)
class PreviousNote:
    timestamp: datetime
    description: str | None


@dataclass(kw_only=True)
class ContextEvent:
    id: str
    summary: str | None
    start: datetime
    end: datetime
    description: str | None = None
    action_ids: list[str] | None = None
    """What was done at it now, the first setting its color."""

    action_names: list[str] | None = None
    suggested_action_ids: list[str] | None = None
    """For a past event with no actions: those the latest event with the
    same title, in the last few weeks, had -- to apply unless the notes
    say otherwise (see `DECISION_GUIDE`)."""

    priority: int | None = None
    is_fixed_time: bool | None = None
    facts: Facts | None = None
    """Where, who with, who for, and notes on each person there, if
    they've been recorded (see utilities/facts.py)."""

    compacted_until: datetime | None = None
    """How much of it an earlier compaction settled: its start, and its
    lasting until this, can't change (see `DECISION_GUIDE`)."""


@dataclass(kw_only=True)
class ContextAction:
    """An action an event can be given (its groups left out)."""

    id: str
    name: str
    note: str | None = None


@dataclass(kw_only=True)
class ContextPerson:
    id: str
    name: str
    context: str | None = None
    what_matters: str | None = None
    """What's important to them, for the notes to say what touched it."""


@dataclass(kw_only=True)
class ContextLocation:
    id: str
    name: str
    hint: str | None = None
    """How to tell that an event or note refers to it."""


@dataclass(kw_only=True)
class ContextDay:
    """One of several days compacted together."""

    label: str
    """Its date, as the timeline heads it, e.g. "Sat 03 Oct"."""

    compaction_window_start: datetime
    day_end: datetime
    note_ids: list[str]


@dataclass(kw_only=True)
class CompactionContext:
    """What a client needs to interpret the notes of one or more days --
    see `NoteCompactor.prepare`."""

    notes: list[ContextNote]
    events: list[ContextEvent]
    now: datetime | None = None
    """The effective 'now' for the last day: the actual now, or the end
    of that day if that's earlier."""

    day_end: datetime | None = None
    """Where the last day ends."""

    remaining_note_count: int = 0
    """Uncompacted notes left for a later compaction: those past the
    most days one takes on (`_MAX_DAYS`), or written after now."""

    days: list[ContextDay] | None = None
    """When the notes span several days: each day, oldest first. `None`
    for one."""

    open_compaction: str | None = None
    """The id of a compaction that's begun but not finished, if any. It
    must be resumed or abandoned before a new one can start."""

    judgments_pending: str | None = None
    """The id of the last compaction, if its judgments aren't all made:
    make them (judgments_due, record_judgments) before compacting more."""

    compaction_window_start: datetime | None = None
    """Where the events offered start: the later of the (first) day's
    start and the last compaction -- see the module docstring."""

    timeline: Timeline | None = None
    """`notes` beside `events` as planned -- see
    utilities/compaction_timeline.py."""

    actions: list[ContextAction] | None = None
    """The actions an event can be given: every active and proposed one."""

    people: list[ContextPerson] | None = None
    """Everyone active, "self" (the user) first."""

    locations: list[ContextLocation] | None = None

    previous_note: PreviousNote | None = None
    """The latest already-compacted note, however long ago it was
    written -- see the module docstring."""

    instructions: str = DECISION_GUIDE


@dataclass(kw_only=True)
class CompactionResult:
    status: Literal[
        "planned",
        "applied",
        "already_compacted",
        "abandoned",
        "nothing_to_compact",
    ]
    message: str
    compaction_id: str | None = None
    changes: list[CompactionChange] = None  # type: ignore[assignment]
    warnings: list[str] = None  # type: ignore[assignment]
    timeline: Timeline | None = None
    """For a dry run: the notes beside the events as they'd end up -- show
    this to the user (see `DECISION_GUIDE`)."""

    additions: dict[str, list[dict]] | None = None
    """For a dry run: the actions, people and locations it adds when it's
    applied -- show these to the user too."""

    judgments: JudgmentsDue | None = None
    """Once applied: the judgments its events call for, to make now, of
    the events as `timeline` shows them (see utilities/judgments.py). The
    compaction isn't complete until they're recorded."""

    scored_days: list[str] | None = None
    """Once complete: the days whose trait scores it rolled up."""

    def __post_init__(self) -> None:
        self.changes = self.changes or []
        self.warnings = self.warnings or []


@dataclass
class _Day:
    notes: list[SheetNote]
    events: list[Event]
    day_start: datetime
    compaction_window_start: datetime
    day_end: datetime
    now: datetime
    previous: Event | None = None
    """The event that ended just before the compaction window, if one
    was offered."""

    latest_compacted: NotedTime | None = None
    """The compacted note with the latest timestamp, read with `notes`."""

    last_compaction: datetime | None = None
    """The last stamped compaction's `now`, if there's been one -- for
    the batch's first day only, as context."""

    closing: Event | None = None
    """The day's own end-of-day sleep event, the night it ends with, if
    it has one."""

    has_next: bool = False
    """Whether another day of the batch follows it."""

    night_before: Event | None = None
    """The night that ended the batch's day before this one, if that day
    decided it ran later than planned -- into this one's morning: see
    `NoteCompactor._plan`."""

    earlier_notes: dict[str, datetime] = field(default_factory=dict)
    """The batch's earlier days' notes (id -> time): this day's decisions
    may use one as an edge, at its time, without taking the note."""

    calendar: "_PlannedCalendar | None" = field(default=None, repr=False)
    """The calendar it was read from, as the days before it in the batch
    leave it -- for what's before its compaction window."""


@dataclass(kw_only=True)
class _Walked:
    """One day of a batch, as `NoteCompactor._walk` reached it."""

    day: _Day
    plan: CompactionPlan | None = None
    decisions: list[EventDecision] = field(default_factory=list)
    ignore_notes: list[str] = field(default_factory=list)


_STATE_FIELDS = (
    "summary", "start", "end", "description", "location", "status",
    "is_fixed_time", "priority", "event_label_id", "action_ids", "min_duration", "facts", "compacted_until",
)


class _PlannedCalendar:
    """A calendar's events as the planned changes `apply`d to it would
    leave them, without writing anything: what each day of a batch is
    planned against, so it starts from the day before it as planned."""

    def __init__(self, calendar: ReallocatingCalendar) -> None:
        self._calendar = calendar
        self._changed: dict[str, Event] = {}
        self._created: list[Event] = []

    def apply(self, changes: list[CompactionChange]) -> None:
        for change in changes:
            if change.action == "create":
                self._created.append(change.after.to_event(f"planned{len(self._created) + 1}"))
            elif change.action == "cancel":
                self._changed[change.event_id] = Event(status="cancelled")
            else:
                self._changed[change.event_id] = change.after.to_event()

    def list_events(self, time_min: datetime, time_max: datetime) -> list[Event]:
        events = []
        for event in self._calendar.list_events(time_min, time_max):
            changed = self._changed.get(event.id)
            if changed is not None:
                event = replace(
                    event,
                    **{f: getattr(changed, f) for f in _STATE_FIELDS if getattr(changed, f) is not None},
                )
            events.append(event)
        events += [replace(e) for e in self._created]
        return [e for e in events if e.end > time_min and e.start < time_max]


class NoteCompactor:
    def __init__(
        self,
        *,
        calendar: ReallocatingCalendar,
        client,
        notes: NotedTimeSheet,
        journal: CompactionJournal,
        clock: Callable[[], datetime] | None = None,
        actions: Actions | None = None,
        people: People | None = None,
        locations: Locations | None = None,
        marker: CompactionMarker | None = None,
        judging: Judging | None = None,
        rollup: TraitRollup | None = None,
        cancellations: Cancellations | None = None,
    ) -> None:
        """`calendar` reads the day's events (through the same
        action-aware view reallocation uses); `client` is what the planned
        changes are written through (an ActionCalendar, so each event's
        label follows its actions). `actions`, `people` and `locations`
        are what events' actions and facts name, and what a plan can add
        to. `marker`, if given, is moved to each compaction once it's
        stamped (see utilities/compaction_marker.py). `judging`, if given,
        makes and records the judgments that complete a compaction (see
        utilities/judgments.py), and `rollup`, if given, rolls the days a
        complete compaction settled up into trait scores (see
        utilities/trait_rollup.py). `cancellations`, if given, records each
        event a compaction cancels for the people it counts against in
        follow-through, and the timeline lists them (see utilities/
        cancellations.py)."""
        self._cancellations = cancellations
        self._marker = marker
        self._judging = judging
        self._rollup = rollup
        self._calendar = calendar
        self._client = client
        self._actions = actions
        self._people = people
        self._locations = locations
        self._notes = notes
        self._journal = journal
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def prepare(self) -> CompactionContext:
        self._prefetch()
        # Before garbage collection, which may trim the last batch's days.
        pending = self._judgments_pending()
        self._journal.garbage_collect()
        open_ids = self._journal.open_compactions()
        open_id = self._journal.load(open_ids[0][0]).batch_id if open_ids else None
        walked, remaining = self._walk(self._clock())
        if not walked:
            return CompactionContext(notes=[], events=[], open_compaction=open_id, judgments_pending=pending)
        days = [w.day for w in walked]
        tree = self._actions.tree() if self._actions else None
        names = self._names()
        suggested = self._suggestions(days, tree) if tree is not None else {}
        notes: list[ContextNote] = []
        events: dict[str, ContextEvent] = {}
        timelines: list[tuple[datetime, Timeline]] = []
        for day in days:
            first = min((n.note.timestamp for n in day.notes), default=None)
            candidates = {
                n.id: _candidates(n.note.timestamp, day.events, day.previous if n.note.timestamp == first else None)
                for n in day.notes
            }
            notes += [
                ContextNote(
                    id=n.id,
                    timestamp=n.note.timestamp,
                    description=n.note.description,
                    candidates=candidates[n.id],
                )
                for n in day.notes
            ]
            for e in day.events:
                if e.id and e.id not in events:
                    events[e.id] = ContextEvent(
                        id=e.id,
                        summary=e.summary,
                        start=e.start,
                        end=e.end,
                        description=e.description,
                        action_ids=e.action_ids,
                        action_names=[names.get(a, a) for a in e.action_ids] if e.action_ids is not None else None,
                        suggested_action_ids=suggested.get(e.id),
                        priority=e.effective_priority,
                        is_fixed_time=e.is_fixed_time,
                        facts=e.facts,
                        compacted_until=e.compacted_until,
                    )
            timelines.append(
                (
                    day.day_start,
                    planned_timeline(
                        _plan_notes(day),
                        _shown(day, candidates),
                        day.now,
                        names,
                        suggested,
                        previous_note=_latest_compacted(day),
                        last_compaction=day.last_compaction,
                    ),
                )
            )
        previous_note = days[0].latest_compacted
        return CompactionContext(
            notes=notes,
            events=list(events.values()),
            now=days[-1].now,
            day_end=days[-1].day_end,
            remaining_note_count=remaining,
            open_compaction=open_id,
            compaction_window_start=days[0].compaction_window_start,
            days=[
                ContextDay(
                    label=_day_label(day),
                    compaction_window_start=day.compaction_window_start,
                    day_end=day.day_end,
                    note_ids=[n.id for n in day.notes],
                )
                for day in days
            ] if len(days) > 1 else None,
            timeline=join_days(timelines),
            actions=[
                ContextAction(id=a.id, name=a.name, note=a.note)
                for a in tree.actions
                if a.status in ("active", "proposed")
            ] if tree is not None else None,
            people=[
                ContextPerson(id=p.id, name=p.name, context=p.context, what_matters=p.what_matters)
                for p in self._people.get_people()
            ] if self._people is not None else None,
            locations=[
                ContextLocation(id=loc.id, name=loc.name, hint=loc.hint) for loc in self._locations.all()
            ] if self._locations is not None else None,
            previous_note=PreviousNote(
                timestamp=previous_note.timestamp, description=previous_note.description
            ) if previous_note is not None else None,
            judgments_pending=pending,
        )

    def prefetch(self) -> None:
        """Read every tab a step reads, in one request -- see `_prefetch`."""
        self._prefetch()

    def _prefetch(self, *, facts: bool = True) -> None:
        """Read the notes tab, the journal and (if `facts`) the actions,
        people and locations tabs -- all a step reads of the spreadsheet
        -- in one request, so every later read of them in the same tool
        call is served from the cache (see `SheetsClient.prefetch`):
        Google Sheets caps read requests at 60 a minute."""
        tabs = [self._notes.whole_tab, self._journal.whole_tab]
        if facts:
            if self._actions is not None:
                tabs += self._actions.whole_tabs
            if self._people is not None:
                tabs += self._people.whole_tabs
            if self._locations is not None:
                tabs.append(self._locations.whole_tab)
            if self._judging is not None:
                tabs += self._judging.whole_tabs
            if self._rollup is not None:
                tabs += self._rollup.whole_tabs
            if self._cancellations is not None:
                tabs += self._cancellations.whole_tabs
        self._notes.prefetch(tabs)

    def _names(self, additions: Additions | None = None) -> dict[str, str]:
        """Every action's, person's and location's id (and every ref in
        `additions`), with its name -- for the timeline."""
        names: dict[str, str] = {}
        if self._actions is not None:
            names.update((a.id, a.name or a.id) for a in self._actions.all() if a.id)
        if self._people is not None:
            names.update((p.id, p.name or p.id) for p in self._people.all() if p.id)
        if self._locations is not None:
            names.update((loc.id, loc.name or loc.id) for loc in self._locations.all() if loc.id)
        if additions is not None:
            names.update((ref, f"{name} (new)") for ref, name in additions.refs.items())
        return names

    def _suggestions(self, days: list[_Day], tree: ActionTree) -> dict[str, list[str]]:
        """Actions to suggest for the days' past events that have none: those
        the latest earlier event with the same title had, within
        _HINT_HISTORY (ignoring actions since deleted). Recurring events
        need nothing extra: an instance's actions come with its series."""
        bare = [e for day in days for e in day.events if e.id and not e.action_ids and e.end <= day.now and e.summary]
        if not bare:
            return {}
        start = days[0].compaction_window_start
        history = self._calendar.list_events(start - _HINT_HISTORY, start)
        hints: dict[str, list[str]] = {}
        for event in sorted(history, key=lambda e: e.start):
            usable = [a for a in event.action_ids or () if a in tree.by_id and tree.by_id[a].status != "deleted"]
            if event.summary and usable and event.status != "cancelled":
                hints[_title_key(event.summary)] = usable
        return {e.id: hints[_title_key(e.summary)] for e in bare if _title_key(e.summary) in hints}

    def dry_run(
        self,
        decisions: list[EventDecision],
        ignore_notes: list[str] | None = None,
        new_actions: list[NewAction] | None = None,
        new_people: list[NewPerson] | None = None,
        new_locations: list[NewLocation] | None = None,
    ) -> CompactionResult:
        """`new_actions`, `new_people` and `new_locations`: what the plan
        adds when it's applied, which its decisions name by their refs
        (see utilities/compaction_additions.py)."""
        self._prefetch()
        self._require_no_open_compaction()
        additions = Additions(actions=new_actions or [], people=new_people or [], locations=new_locations or [])
        problems = check_additions(additions, self._actions, self._people, self._locations)
        if problems:
            raise CompactionError.of(problems, "additions")
        decisions = self._checked_facts(decisions, additions)
        pending = list(decisions)
        ignoring = list(ignore_notes or [])

        def night(event_id: str, index: int) -> EventDecision | None:
            return next((d for d in pending if d.event_id == event_id), None)

        def plan_day(day: _Day, index: int) -> _Walked:
            label = _day_label(day) if day.has_next or index else None
            try:
                mine, pending[:] = _decisions_for(day, pending)
                note_ids = {n.id for n in day.notes}
                ignored = [i for i in ignoring if not day.has_next or i in note_ids]
                ignoring[:] = [i for i in ignoring if i not in ignored]
                plan = self._plan(day, mine, ignored, additions)
            except CompactionError as exc:
                raise CompactionError.wrapping(f"{label}: {exc}" if label else str(exc), exc) from exc
            if label:
                plan.warnings = [f"{label}: {w}" for w in plan.warnings]
            return _Walked(day=day, plan=plan, decisions=mine, ignore_notes=ignored)

        walked, remaining = self._walk(self._clock(), plan_day, night)
        if not walked:
            return CompactionResult(status="nothing_to_compact", message="there are no uncompacted notes")
        superseded = self._supersede_planned()
        compaction_id = uuid.uuid4().hex[:12]
        self._journal.start_batch(
            [
                PlannedDay(
                    compaction_id=_day_id(compaction_id, number),
                    now=w.day.now,
                    note_ids=[n.id for n in w.day.notes],
                    decisions=w.decisions,
                    ignore_notes=w.ignore_notes,
                    plan=w.plan,
                    additions=additions.to_json_dict() if number == 1 else {},
                )
                for number, w in enumerate(walked, start=1)
            ]
        )
        changes = [c for w in walked for c in w.plan.changes]
        note_count = sum(len(w.day.notes) for w in walked)
        days = f" over {len(walked)} days" if len(walked) > 1 else ""
        return CompactionResult(
            status="planned",
            compaction_id=compaction_id,
            changes=changes,
            warnings=[warning for w in walked for warning in w.plan.warnings],
            timeline=join_days([(w.day.day_start, w.plan.timeline) for w in walked]),
            additions=additions.to_json_dict() or None,
            message=(
                f"{len(changes)} calendar change(s) planned for {note_count} note(s){days}; "
                "nothing has been changed yet. Show the user `timeline.text` in a code block (see the "
                "instructions from prepare_compaction) and the warnings. "
                + _APPROVAL_RULE
                + f" Once they approve, apply it with compaction_id={compaction_id!r} and "
                "dry_run=False. If they want something different, correct the decisions and call "
                "compact_notes again (this plan is then replaced)."
                + (f" (Replaced {superseded} earlier unapplied plan(s).)" if superseded else "")
                + (
                    f" {remaining} later note(s) aren't part of it: compact them next."
                    if remaining
                    else ""
                )
            ),
        )

    def describe(self, compaction_id: str) -> CompactionResult:
        """The stored plan for `compaction_id`, without doing anything."""
        days = self._journal.load_batch(compaction_id)
        statuses = {d.status for d in days}
        if statuses == {STAMPED}:
            status = "already_compacted"
        elif ABANDONED in statuses:
            status = "abandoned"
        else:
            status = "planned"
        if len(days) == 1:
            message = f"compaction {compaction_id} is {days[0].status}"
        else:
            message = f"compaction {compaction_id} covers {len(days)} days: " + ", ".join(
                f"day {d.day} {d.status}" for d in days
            )
        return CompactionResult(
            status=status,
            compaction_id=compaction_id,
            changes=[c for d in days for c in d.changes()],
            warnings=[w for d in days for w in d.warnings],
            message=message,
        )

    def commit(self, compaction_id: str) -> CompactionResult:
        """Apply the batch `compaction_id` a day at a time: each day's steps,
        then its notes stamped, before the next day's. Hands back its final
        timeline (unless it's resumed partway) and the judgments it calls
        for, to make of the events as that timeline shows them."""
        self._prefetch()
        days = self._journal.load_batch(compaction_id)
        changes = [c for d in days for c in d.changes()]
        if all(d.status == STAMPED for d in days):
            # Again, in case moving it failed the first time.
            last = self._journal.last_stamped_now()
            return CompactionResult(
                status="already_compacted",
                compaction_id=compaction_id,
                changes=changes,
                warnings=self._move_marker(last) if last is not None else [],
                message=f"compaction {compaction_id} was already applied and its notes stamped",
            )
        if any(d.status == ABANDONED for d in days):
            raise CompactionError(f"compaction {compaction_id} was abandoned; run a new dry run", category="abandoned")
        pending = [d for d in days if d.status != STAMPED]
        timeline = None
        if pending[0].status == PLANNED:
            self._require_no_open_compaction()
            timeline = self._verify_unchanged(pending)
        refs: dict[str, str] | None = None
        for journal in pending:
            if journal.status == PLANNED:
                self._journal.set_status(journal, APPLYING)
            if refs is None:
                # Created (or found, on a resumed commit) before any step
                # names them.
                additions = Additions.from_json_dict(days[0].additions)
                refs = create_additions(additions, self._actions, self._people, self._locations)
            for step in journal.steps:
                if step.status != "done":
                    self._apply_step(journal, step, refs)
                    self._journal.mark_step_done(step)
            self._record_cancellations(journal)
            self._journal.set_status(journal, APPLIED)
            self._notes.mark_compacted(journal.note_ids, journal.id)
            self._journal.set_status(journal, STAMPED)
        steps = sum(len(d.steps) for d in days)
        notes = sum(len(d.note_ids) for d in days)
        warnings = [w for d in days for w in d.warnings] + self._move_marker(days[-1].now)
        # The days as loaded, not read again after all that writing.
        due = self._due(compaction_id, days, redo=False) if self._judging is not None else None
        message = f"applied {steps} change(s) and marked {notes} note(s) compacted"
        scored = None
        if due is not None and due.events:
            message += (
                f". The compaction isn't complete yet: make the {due.count} judgment(s) in `judgments` now, "
                "yourself, of the events as `timeline` shows them, and record them with record_judgments "
                "(see `judgments.instructions`)"
            )
        else:
            scored = self._roll_up(days)
        return CompactionResult(
            status="applied",
            compaction_id=compaction_id,
            changes=changes,
            warnings=warnings,
            message=message,
            judgments=due if due is not None and due.events else None,
            scored_days=scored,
            timeline=timeline,
        )

    def _roll_up(self, days: list[JournalCompaction]) -> list[str] | None:
        """Roll the days a complete compaction settled up into trait scores
        (none without a rollup); the days, as dates."""
        if self._rollup is None:
            return None
        starts = [
            state.start for day in days for step in day.steps for state in (step.before, step.after)
            if state is not None and state.start is not None
        ]
        start = min(starts, default=days[0].now)
        rows = self._rollup.roll_up(self._rollup.days_between(start, days[-1].now))
        return sorted({r.day for r in rows})

    # -- judgments -------------------------------------------------------------

    def judgments_due(self, compaction_id: str | None = None, *, redo: bool = False) -> JudgmentsDue | None:
        """The judgments the stamped compaction `compaction_id` (by default
        the last one) calls for: those not made yet, or with `redo`, all of
        them, each with the one made already. `None` without judging, or
        with no stamped compaction."""
        if self._judging is None:
            return None
        compaction_id = compaction_id or self._journal.last_stamped_batch()
        if compaction_id is None:
            return None
        return self._due(compaction_id, self._journal.load_batch(compaction_id), redo=redo)

    def _due(self, compaction_id: str, days: list[JournalCompaction], *, redo: bool) -> JudgmentsDue:
        return self._judging.due(compaction_id, self._requests(compaction_id, days, redo=redo))

    def _requests(self, compaction_id: str, days: list[JournalCompaction], *, redo: bool) -> list[JudgmentRequest]:
        """The judgment requests the stamped batch `days` calls for -- see
        `judgments_due`."""
        if not all(d.status == STAMPED for d in days):
            raise CompactionError(
                f"compaction {compaction_id} hasn't been applied, so its events have nothing to judge",
                category="not_applied",
            )
        return self._judging.requests(*_judged_events(days), include_judged=redo)

    def record_judgments(self, compaction_id: str, judgments: list[Judgment]) -> JudgmentsResult:
        """Record `judgments` on the compaction's events; whether that
        completes it."""
        if self._judging is None:
            raise CompactionError("this calendar has no traits to judge", category="no_traits")
        days = self._journal.load_batch(compaction_id)
        try:
            recorded = self._judging.record(self._requests(compaction_id, days, redo=True), judgments)
        except ValueError as exc:
            raise CompactionError(str(exc), category="judgment") from exc
        remaining = [r.id for r in self._requests(compaction_id, days, redo=False)]
        scored = self._roll_up(days) if not remaining else None
        return JudgmentsResult(
            compaction_id=compaction_id,
            recorded=recorded,
            remaining=remaining,
            complete=not remaining,
            scored_days=scored,
            message=(
                f"recorded {recorded} judgment(s); compaction {compaction_id} is complete" if not remaining
                else f"recorded {recorded} judgment(s); {len(remaining)} still to make before compaction "
                f"{compaction_id} is complete (see `remaining`)"
            ),
        )

    def _judgments_pending(self) -> str | None:
        """The last compaction's id, if it has judgments still to make."""
        if self._judging is None:
            return None
        compaction_id = self._journal.last_stamped_batch()
        if compaction_id is None:
            return None
        try:
            due = self._requests(compaction_id, self._journal.load_batch(compaction_id), redo=False)
        except CompactionError:
            return None
        return compaction_id if due else None

    def _checked_facts(self, decisions: list[EventDecision], additions: Additions) -> list[EventDecision]:
        """`decisions` with their facts normalized; CompactionError if any
        aren't well formed, or name people or locations that aren't there
        (or being added)."""
        people = {p.id for p in self._people.all()} if self._people is not None else None
        locations = {loc.id for loc in self._locations.all()} if self._locations is not None else None
        new_people = {p.ref for p in additions.people}
        new_locations = {loc.ref for loc in additions.locations}
        checked, problems = [], []
        for number, decision in enumerate(decisions, start=1):
            if decision.facts is not None:
                facts = decision.facts.normalized()
                label = f"decision {number} ({decision.action}{' ' + decision.event_id if decision.event_id else ''})"
                problems += [f"{label}: its facts {p}" for p in fact_problems(facts)]
                if people is not None:
                    unknown = [i for i in facts.people() if i not in people and i not in new_people and i != SELF_ID]
                    if unknown:
                        problems.append(
                            f"{label}: its facts name {unknown}, who aren't people (get_people lists them; add "
                            "someone new with new_people)"
                        )
                if locations is not None and facts.location_id and facts.location_id not in locations | new_locations:
                    problems.append(
                        f"{label}: its facts' location {facts.location_id!r} isn't a location (add a new one with "
                        "new_locations)"
                    )
                decision = replace(decision, facts=facts)
            checked.append(decision)
        if problems:
            raise CompactionError.of(problems, "facts")
        return checked

    def _move_marker(self, at: datetime) -> list[str]:
        """Move the last-compaction marker to `at`, if there's a marker;
        a warning to report if that failed. Best effort: the compaction
        itself is done either way, and the next one moves it again."""
        if self._marker is None:
            return []
        try:
            self._marker.mark(at)
        except Exception as exc:  # Any failure: the compaction mustn't fail with it.
            logger.warning("Couldn't move the compaction marker", exc_info=True)
            return [f"couldn't move the last-compaction marker in Google Calendar ({exc}); the next compaction will"]
        return []

    def edit_note(
        self, note_id: str, *, timestamp: datetime | None = None, description: str | None = None
    ) -> SheetNote:
        """See the module-level `edit_note`."""
        self._prefetch(facts=False)
        return edit_note(self._notes, self._journal, note_id, timestamp=timestamp, description=description)

    def delete_note(self, note_id: str) -> NotedTime:
        """See the module-level `delete_note`."""
        self._prefetch(facts=False)
        return delete_note(self._notes, self._journal, note_id)

    def abandon(self, compaction_id: str) -> CompactionResult:
        days = self._journal.load_batch(compaction_id)
        if all(d.status == STAMPED for d in days):
            raise CompactionError(
                f"compaction {compaction_id} is already complete; there's nothing to abandon", category="already_complete"
            )
        finished = sum(1 for d in days if d.status == STAMPED)
        for day in days:
            if day.status not in (STAMPED, ABANDONED):
                self._journal.set_status(day, ABANDONED)
        return CompactionResult(
            status="abandoned",
            compaction_id=compaction_id,
            message=(
                f"compaction {compaction_id} abandoned. Any steps it had already applied stay applied "
                "(the journal has each one's before-state); its notes are still uncompacted."
                + (
                    f" ({finished} of its {len(days)} days had already been applied in full, so those "
                    "days' notes stay compacted.)"
                    if finished
                    else ""
                )
            ),
        )

    def _walk(
        self,
        now: datetime,
        plan_day: Callable[[_Day, int], _Walked] | None = None,
        night: Callable[[str, int], EventDecision | None] | None = None,
        *,
        max_days: int = _MAX_DAYS,
    ) -> tuple[list[_Walked], int]:
        """The days of uncompacted notes up to `now`, oldest first -- at
        most `max_days` of them -- and how many uncompacted notes are left
        after them. Each day after the first starts where the one before
        it ends, as if that one had been compacted (see the module
        docstring). `plan_day` (the day, and which it is, from 0) plans
        each day as it's reached; the days after it are then read as that
        plan would leave the calendar. `night` (a sleep event's id, and
        which day it is) is the decision on that day's night, if there's
        one -- where the day ends depends on it (see `_cut`)."""
        sheet_notes, latest = self._notes.read_with_latest_compacted()
        notes = [n for n in sheet_notes if n.note.timestamp <= now]
        times = {n.id: n.note.timestamp for n in notes}
        last_stamped = self._journal.last_stamped_now()
        calendar = _PlannedCalendar(self._calendar)
        walked: list[_Walked] = []
        window_start: datetime | None = None
        night_before: Event | None = None
        earlier: dict[str, datetime] = {}
        if not notes:
            return walked, len(sheet_notes)
        # Every day below lists a stretch of this, several times over as
        # its night is decided (see `_cut`): one listing of it all first,
        # so inside a tool call they're each answered from it (see
        # `cached_calendar_listings`).
        self._calendar.list_events(min(times.values()) - _PREFETCH_BEFORE, now + _PREFETCH_AFTER)
        anchor = self._anchor(min(times.values()), last_stamped, calendar)
        while True:
            day = self._cut(
                lambda sleepless, border: self._day(
                    now,
                    notes,
                    calendar,
                    last_stamped=last_stamped,
                    latest_compacted=latest,
                    first=not walked,
                    window_start=window_start,
                    sleepless=sleepless,
                    border=border,
                    anchor=None if walked else anchor,
                ),
                (lambda event_id: night(event_id, len(walked))) if night else None,
                times,
            )
            in_day = {n.id for n in day.notes}
            notes = [n for n in notes if n.id not in in_day]
            # After a late wake-up, the next day's morning is under the
            # night now: it's compacted too, notes or not, to settle that.
            planned_end = day.closing.end if day.closing is not None else day.day_end
            overslept = day.day_end > planned_end and planned_end < now
            day.has_next = (bool(notes) or overslept) and day.now < now and len(walked) + 1 < max_days
            day.night_before = night_before
            day.earlier_notes = dict(earlier)
            walked.append(plan_day(day, len(walked)) if plan_day else _Walked(day=day))
            if not day.has_next:
                break
            if walked[-1].plan is not None:
                calendar.apply(walked[-1].plan.changes)
            last_stamped = day.now
            # After a late wake-up the next day starts at the planned one,
            # so the events the night now runs over are its to settle.
            window_start = min(day.day_end, planned_end)
            night_before = day.closing if overslept else None
            earlier.update((n.id, n.note.timestamp) for n in day.notes)
        return walked, len(sheet_notes) - sum(len(w.day.notes) for w in walked)

    @staticmethod
    def _anchor(oldest: datetime, last_stamped: datetime | None, calendar: _PlannedCalendar) -> datetime | None:
        """Where to find a batch's first day, if not at its `oldest` note: at
        the last compaction (`last_stamped`), when that ran before the night
        that ends its day -- that day isn't over, so nothing after the last
        compaction is skipped -- and the oldest note is no later than the
        day after that night: written during it (the user stayed up), or
        the next morning (the evening and night are settled first). `None`
        to find it at the oldest note, as when the note's on the same day,
        or the last compaction ran during the night, or long before."""
        if last_stamped is None or oldest - last_stamped > _ADJACENT:
            return None
        nights = sorted(
            (
                e for e in calendar.list_events(last_stamped, oldest + timedelta(seconds=1))
                if e.is_end_of_day_sleep and e.status != "cancelled"
            ),
            key=lambda e: e.start,
        )
        night = next((e for e in nights if e.end > last_stamped), None)
        if night is None or night.start <= last_stamped or oldest < night.start:
            return None
        if any(e.start > night.start for e in nights):
            return None  # The note's later than the day after the night.
        return last_stamped

    @staticmethod
    def _cut(
        day_for: Callable[[frozenset[str], datetime | None], _Day],
        night: Callable[[str], EventDecision | None] | None,
        times: dict[str, datetime],
    ) -> _Day:
        """The day `day_for` reads, ended where the decision on its night
        (`night`) puts it. A day ends with its night's sleep, and the
        night's end is the border with the next day: a decision that moves
        it -- the user woke early, or slept in -- moves the border, and
        with it which notes are this day's. One that cancels it -- a night
        without sleep -- runs the day on to the next night, as one long
        day. `times`: the notes' times, for a note that ends the night."""
        sleepless: frozenset[str] = frozenset()
        border: datetime | None = None
        day = day_for(sleepless, border)
        while night is not None and day.closing is not None:
            decision = night(day.closing.id)
            if decision is None:
                break
            if decision.action == "cancel" and day.closing.id not in sleepless:
                sleepless = sleepless | {day.closing.id}
            elif decision.action == "keep" and border is None and (decision.end or decision.end_note):
                border = decision.end or times.get(decision.end_note)
                if border is None:
                    break  # An unknown note: the plan reports it.
            else:
                break
            day = day_for(sleepless, border)
        return day

    def _day(
        self,
        now: datetime,
        notes: list[SheetNote],
        calendar: _PlannedCalendar,
        *,
        last_stamped: datetime | None,
        latest_compacted: NotedTime | None,
        first: bool,
        window_start: datetime | None = None,
        sleepless: frozenset[str] = frozenset(),
        border: datetime | None = None,
        anchor: datetime | None = None,
    ) -> _Day:
        """The day the oldest of `notes` falls in -- or, with none, the one
        `window_start` does, or with an `anchor` (see `_anchor`), the one
        that does -- with its notes. Its
        compaction window starts at `window_start`, if given, or else no
        earlier than `last_stamped`. `first`: whether it's the batch's
        first day, the only one shown the latest compacted note and the
        last compaction, as context. `sleepless`: nights that don't end
        it, and `border`: where it ends instead of where its night does
        -- see `_cut`."""
        oldest = anchor or (min(n.note.timestamp for n in notes) if notes else window_start)
        # The day the oldest note falls in starts when the night before it
        # ends -- or at the note, if it was written before the wake-up time.
        recent = calendar.list_events(oldest - timedelta(hours=24), oldest + timedelta(seconds=1))
        opening = max(
            (e for e in recent if e.is_end_of_day_sleep and e.status != "cancelled" and e.start <= oldest),
            key=lambda e: e.start,
            default=None,
        )
        day_start = min(opening.end, oldest) if opening is not None else oldest
        if window_start is not None:
            compaction_window_start = window_start
        else:
            compaction_window_start = (
                max(day_start, last_stamped) if last_stamped is not None else day_start
            )
        reach = timedelta(hours=24) * (1 + len(sleepless))

        fetched = sorted(
            (
                e
                for e in calendar.list_events(compaction_window_start - _LOOKBACK, day_start + reach)
                if e.status != "cancelled"
            ),
            key=lambda e: e.start,
        )
        earlier = [e for e in fetched if e.end <= compaction_window_start]
        events = [e for e in fetched if e.end > compaction_window_start]
        closing = next(
            (
                i
                for i, e in enumerate(events)
                if e.is_end_of_day_sleep and e.start > day_start and e.id not in sleepless
            ),
            None,
        )
        if closing is not None:
            events = events[: closing + 1]
        previous = max(earlier, key=lambda e: e.end) if earlier else None
        if previous is not None:
            events.insert(0, previous)
        if border is not None:
            day_end = border
        elif closing is not None:
            day_end = events[-1].end
        else:
            day_end = day_start + reach
        return _Day(
            notes=[n for n in notes if n.note.timestamp <= day_end],
            events=events,
            day_start=day_start,
            compaction_window_start=compaction_window_start,
            day_end=day_end,
            now=min(now, day_end),
            previous=previous,
            latest_compacted=latest_compacted if first else None,
            last_compaction=last_stamped if first else None,
            closing=events[-1] if closing is not None else None,
            calendar=calendar,
        )

    def _supersede_planned(self) -> int:
        """Abandon every earlier plan that was never applied. A new dry run
        replaces them -- it's how a rejected or corrected plan is redone --
        and leaving them `planned` would let a stale one be committed by
        mistake. Returns how many were replaced (a batch counting once)."""
        replaced = [self._journal.load(i) for i, _status in self._journal.compactions_with_status(PLANNED)]
        for compaction in replaced:
            self._journal.set_status(compaction, ABANDONED)
        return len({c.batch_id for c in replaced})

    def _require_no_open_compaction(self) -> None:
        open_compactions = self._journal.open_compactions()
        if open_compactions:
            compaction_id, status = open_compactions[0]
            batch_id = self._journal.load(compaction_id).batch_id
            raise CompactionError(
                f"compaction {batch_id} is {status} -- finish it with compact_notes("
                f"compaction_id={batch_id!r}, dry_run=False), or abandon it with "
                "abandon_compaction, before starting another",
                category="open_compaction",
            )

    def _verify_unchanged(self, days: list[JournalCompaction]) -> Timeline:
        """Plan `days` -- a batch's days not yet applied -- again, each with
        its own decisions, and check they come out as they did when they
        were previewed. Returns their timeline, as planned."""
        stale = CompactionError(
            f"the notes or calendar changed since compaction {days[0].batch_id} was previewed; "
            "run a new dry run",
            category="stale",
        )

        def comparable(changes: list[CompactionChange]) -> list[tuple]:
            return [(c.action, c.event_id, c.before, c.after) for c in changes]

        def night(event_id: str, index: int) -> EventDecision | None:
            if index >= len(days):
                return None
            return next((d for d in days[index].decisions if d.event_id == event_id), None)

        def plan_day(day: _Day, index: int) -> _Walked:
            journal = days[index]
            if {n.id for n in day.notes} != set(journal.note_ids):
                raise stale
            plan = self._plan(day, journal.decisions, journal.ignore_notes, Additions.from_json_dict(days[0].additions))
            if comparable(plan.changes) != comparable(journal.changes()):
                raise stale
            return _Walked(day=day, plan=plan)

        walked, _remaining = self._walk(days[-1].now, plan_day, night, max_days=len(days))
        if len(walked) != len(days):
            raise stale
        return join_days([(w.day.day_start, w.plan.timeline) for w in walked])

    def _plan(
        self, day: _Day, decisions: list[EventDecision], ignore_notes: list[str] | None, additions: Additions
    ) -> CompactionPlan:
        """`plan_compaction` for `day`, with the decisions' actions checked
        (a ref to a new action in `additions` included), and created
        events' labels checked against the calendar's: Calendar rejects
        inserting an event with a label it doesn't have (HTTP 400), which
        would stop the commit partway -- and every retry with it. A
        created event that has one anyway (a split continuation, cloned
        label and all, from reflowing the day) just drops it; the label it
        gets follows its actions when it's written. Only reads the labels
        when a created event has one."""
        tree = self._actions.tree() if self._actions else None
        if tree is not None:
            current = {e.id: e.action_ids or [] for e in day.events if e.id}
            new = {a.ref for a in additions.actions}
            problems = []
            for d in decisions:
                named = [a for a in d.action_ids or () if a not in new]
                if d.action_ids and any(a.startswith(REF_PREFIX) and a not in new for a in d.action_ids):
                    problems.append(f"{d.action} {d.event_id or d.summary!r}: a ref that isn't in new_actions")
                    continue
                if named:
                    try:
                        tree.check_action_ids(named, already=current.get(d.event_id, []))
                    except ValueError as exc:
                        problems.append(f"{d.action} {d.event_id or d.summary!r}: {exc}")
            if problems:
                raise CompactionError.of(problems, "actions")
        night = day.night_before
        if night is not None and any(e.id == night.id for e in day.events):
            # The night the day before decided ran late is placed first, as
            # decided, so what it now runs into -- this morning -- moves out
            # from under it. The day's own decisions never name it: it's the
            # day before's.
            decisions = [EventDecision(action="keep", event_id=night.id)] + list(decisions)
        plan = plan_compaction(
            _plan_notes(day),
            decisions,
            day.events,
            day.now,
            ignore_notes=ignore_notes,
            day_start=day.day_start,
            names=self._names(additions),
            previous_note=_latest_compacted(day),
            last_compaction=day.last_compaction,
            next_day_follows=day.has_next,
        )
        self._check_before_window(day, plan)
        self._show_follow_through(day, decisions, plan)
        if tree is not None:
            _keep_priorities(plan, tree)
        labelled = [c for c in plan.changes if c.action == "create" and c.after.event_label_id is not None]
        if not labelled:
            return plan
        labels, _etag = self._client.list_event_labels()
        label_ids = {label.id for label in labels}
        for change in labelled:
            if change.after.event_label_id not in label_ids:
                change.after.event_label_id = None
        return plan

    def _show_follow_through(self, day: _Day, decisions: list[EventDecision], plan: CompactionPlan) -> None:
        """Mark in `plan`'s timeline each event its decisions cancel ("it
        didn't happen") that counts against someone's follow-through, with
        who -- see utilities/cancellations.py."""
        if self._cancellations is None or plan.timeline is None:
            return
        cancelled = {d.event_id for d in decisions if d.action == "cancel"}
        events = {e.id: e for e in day.events if e.id in cancelled}
        marked = False
        for shown in plan.timeline.events:
            event = events.get(shown.event_id)
            if event is None:
                continue
            shown.follow_through = [
                f"{m.person_name} ({', '.join(m.trait_names)})" for m in self._cancellations.matches(event)
            ]
            marked = marked or bool(shown.follow_through)
        if marked:
            plan.timeline.text = render(plan.timeline)

    def _record_cancellations(self, journal: JournalCompaction) -> None:
        """Record each event `journal`'s day cancelled with a 'cancel'
        decision -- not a merge, nor one the reflow had no room for -- for
        the people it counts against in follow-through, as it was planned.
        Again, harmlessly, on a resumed commit."""
        if self._cancellations is None:
            return
        cancelled = {d.event_id for d in journal.decisions if d.action == "cancel"}
        for step in journal.steps:
            if step.action == "cancel" and step.event_id in cancelled and step.before is not None:
                self._cancellations.record(
                    step.before.to_event(step.event_id), f"compaction {journal.batch_id}", at=self._clock()
                )

    @staticmethod
    def _check_before_window(day: _Day, plan: CompactionPlan) -> None:
        """CompactionError (an overlap) if `plan` puts an event over one
        before `day`'s compaction window that isn't among its events --
        one an earlier compaction already settled, which the plan's own
        overlap checks never see."""
        placed = [c for c in plan.changes if c.action != "cancel" and c.after is not None]
        reach = min((c.after.start for c in placed), default=None)
        if reach is None or reach >= day.compaction_window_start or day.calendar is None:
            return
        offered = {e.id for e in day.events}
        before = [
            e for e in day.calendar.list_events(reach, day.compaction_window_start)
            if e.status != "cancelled" and e.id not in offered
        ]
        problems = [
            Problem(
                "overlap",
                f"{c.after.summary!r} ({c.after.start.isoformat()} to {c.after.end.isoformat()}) runs back over "
                f"{e.summary!r} ({e.start.isoformat()} to {e.end.isoformat()}), from before this round's events "
                f"begin ({day.compaction_window_start.isoformat()}) -- an earlier compaction settled it. Start it "
                f"no earlier than {e.end.isoformat()}, or change {e.summary!r} with update_event first.",
            )
            for c in placed
            for e in before
            if e.id != c.event_id and e.start < c.after.end and e.end > c.after.start
        ]
        if problems:
            raise CompactionError.of(problems, "overlap")

    def _apply_step(self, journal: JournalCompaction, step: JournalStep, refs: dict[str, str]) -> None:
        """Make `step`'s change, with the refs to what the plan added
        replaced by their ids (`refs`)."""
        if step.action == "create":
            event = resolve(step.after.to_event(_new_event_id(journal.id, step.step)), refs)
            try:
                self._client.create_event(event)
            except HttpError as exc:
                # A retried create: the deterministic id already exists.
                if exc.resp.status != 409:
                    raise
        else:
            self._client.update_event(resolve(_patch_for(step), refs))


def edit_note(
    notes: NotedTimeSheet,
    journal: CompactionJournal,
    note_id: str,
    *,
    timestamp: datetime | None = None,
    description: str | None = None,
) -> SheetNote:
    """`NotedTimeSheet.edit`, refused (`CompactionError`) for a note a
    compaction is partway through applying -- see `_require_not_being_applied`."""
    _require_not_being_applied(journal, note_id)
    try:
        return notes.edit(note_id, timestamp=timestamp, description=description)
    except ValueError as exc:
        raise CompactionError(str(exc), category="note_edit") from exc


def delete_note(notes: NotedTimeSheet, journal: CompactionJournal, note_id: str) -> NotedTime:
    """`NotedTimeSheet.delete`, refused like `edit_note`."""
    _require_not_being_applied(journal, note_id)
    try:
        return notes.delete(note_id)
    except ValueError as exc:
        raise CompactionError(str(exc), category="note_edit") from exc


def _require_not_being_applied(journal: CompactionJournal, note_id: str) -> None:
    """A compaction being applied stamps its notes last, and only checks
    their timestamps, not what they say -- so changing one of them
    midway would get the changed note stamped as if it were the one
    planned for. (A compaction that's only `planned` needs no guard: its
    commit re-checks the notes and refuses if they changed.)"""
    for compaction_id, status in journal.open_compactions():
        compaction = journal.load(compaction_id)
        if note_id in compaction.note_ids:
            batch_id = compaction.batch_id
            raise CompactionError(
                f"note {note_id!r} is part of compaction {batch_id}, which is {status} -- "
                f"finish it with compact_notes(compaction_id={batch_id!r}, dry_run=False), or "
                "abandon it with abandon_compaction, first",
                category="note_in_compaction",
            )


def _judged_events(days: list[JournalCompaction]) -> tuple[list[str], tuple[datetime, datetime]]:
    """The ids of a batch's events whose facts it set -- updated or created
    -- and the span of time they're in."""
    ids: list[str] = []
    spans: list[tuple[datetime, datetime]] = []
    for day in days:
        for step in day.steps:
            if step.action == "cancel" or step.after is None or not step.after.facts:
                continue
            ids.append(step.event_id if step.action == "update" else _new_event_id(day.id, step.step))
            spans.append((step.after.start, step.after.end))
    if not spans:
        return [], (days[0].now, days[0].now)
    return ids, (min(s for s, _ in spans), max(e for _, e in spans))


def _decisions_for(
    day: _Day, decisions: list[EventDecision]
) -> tuple[list[EventDecision], list[EventDecision]]:
    """Which of `decisions` are `day`'s, and which are left for the days
    after it. An event's decision is the day's whose events it's among --
    the night it ends with included, all of it (see `NoteCompactor._cut`):
    a night is decided once, by the day before it. A 'create' is the
    day's it starts in. The last day takes whatever's left, unknown ids and all, to report
    them as any day would.

    A decision may use a note of an earlier day of the batch as an edge
    -- "finally up" ends the night, and starts the morning -- at that
    note's time: the note stays the earlier day's."""
    decisions = [_at_earlier_notes(d, day.earlier_notes) for d in decisions]
    if not day.has_next:
        return decisions, []
    event_ids = {e.id for e in day.events if e.id}
    times = {n.id: n.note.timestamp for n in day.notes}
    mine: list[EventDecision] = []
    rest: list[EventDecision] = []
    for decision in decisions:
        if decision.action == "create":
            start = decision.start or times.get(decision.start_note)
            end = decision.end or times.get(decision.end_note)
            ours = start < day.day_end if start is not None else end is not None and end <= day.day_end
            (mine if ours else rest).append(decision)
        elif decision.event_id in event_ids:
            mine.append(decision)
        else:
            rest.append(decision)
    return mine, rest


def _at_earlier_notes(decision: EventDecision, earlier: dict[str, datetime]) -> EventDecision:
    """`decision`, with any edge set by a note in `earlier` set to that
    note's time instead -- see `_decisions_for`."""
    changes = {}
    if decision.start_note in earlier:
        changes.update(start=decision.start or earlier[decision.start_note], start_note=None)
    if decision.end_note in earlier:
        changes.update(end=decision.end or earlier[decision.end_note], end_note=None)
    return replace(decision, **changes) if changes else decision


def _day_label(day: _Day) -> str:
    """The day's date, in the notes' own time zone, e.g. "Sat 03 Oct" --
    as the timeline heads it (see utilities/compaction_timeline.py)."""
    tz = day.notes[0].note.timestamp.tzinfo if day.notes else day.day_start.tzinfo
    return day.day_start.astimezone(tz).strftime("%a %d %b")


def _day_id(batch_id: str, number: int) -> str:
    """The compaction id of a batch's `number`th day (from 1): the
    batch's own id for the first -- see utilities/compaction_journal.py."""
    return batch_id if number == 1 else f"{batch_id}d{number}"


def _plan_notes(day: _Day) -> list[PlanNote]:
    return [
        PlanNote(id=n.id, timestamp=n.note.timestamp, description=n.note.description)
        for n in day.notes
    ]


def _latest_compacted(day: _Day) -> PlanNote | None:
    """The latest compacted note, for the timeline to show as context."""
    latest = day.latest_compacted
    if latest is None:
        return None
    return PlanNote(id="previous_note", timestamp=latest.timestamp, description=latest.description)


def _shown(day: _Day, candidates: dict[str, list[str]]) -> list[Event]:
    """The day's events that `prepare`'s timeline shows: those that
    started before `now`, plus any later one that's a note's candidate --
    e.g. lunch, planned for noon, that an 11:45 "starting lunch" note may
    start. The rest of the future is still offered in `events` (see the
    module docstring); it's just not the notes' business to show."""
    near = {i for ids in candidates.values() for i in ids}
    return [e for e in day.events if e.id and (e.start < day.now or e.id in near)]


def _candidates(timestamp: datetime, events: list[Event], previous: Event | None = None) -> list[str]:
    """`previous` (the event just before the compaction window, offered
    to the day's first note) is listed last: however long after its
    planned end the note is, it may be what the note ends -- e.g.
    "finally up"."""
    near = [
        e
        for e in events
        if e.id and e.end > timestamp - _CANDIDATE_WINDOW and e.start < timestamp + _CANDIDATE_WINDOW
    ]
    near.sort(key=lambda e: abs(e.start - timestamp))
    ids = [e.id for e in near]
    if previous is not None and previous.id and previous.id not in ids:
        ids.append(previous.id)
    return ids


def _title_key(summary: str) -> str:
    return " ".join(summary.casefold().split())


def _keep_priorities(plan: CompactionPlan, tree: ActionTree) -> None:
    """Give each event `plan` compacts the priority its actions give it now
    -- the highest (lowest-numbered) of theirs -- as its own, unless it has
    one: actions' priorities change often, and what happened keeps the
    priority it had (see Event.action_priority). An event whose actions
    give none is left without."""
    for change in plan.changes:
        after = change.after
        if change.action == "cancel" or after is None or after.compacted_until is None or after.priority is not None:
            continue
        priorities = [p for p in (tree.priority(a) for a in after.action_ids or ()) if p is not None]
        if priorities:
            after.priority = min(priorities)


def _new_event_id(compaction_id: str, step: int) -> str:
    """A deterministic id for a created event (Calendar accepts a-v and
    0-9, 5-1024 characters), so retrying a create can't duplicate it."""
    return f"cmp{compaction_id}s{step:03d}"


def _patch_for(step: JournalStep) -> Event:
    if step.action == "cancel":
        return Event(id=step.event_id, status="cancelled")
    before, after = step.before, step.after
    patch = Event(id=step.event_id)
    for name in (
        "summary", "start", "end", "description", "location", "priority", "event_label_id", "action_ids", "compacted_until",
    ):
        value = getattr(after, name)
        if value is not None and value != getattr(before, name):
            setattr(patch, name, value)
    if after.facts != before.facts:
        # Empty facts remove them: see Event.facts.
        patch.facts = facts_from_dict(after.facts)
    if after.is_fixed_time:
        # An actual event is pinned explicitly, not left to inherit it
        # from its label.
        patch.is_fixed_time = True
    if after.min_duration_minutes is not None and (
        after.is_fixed_time or after.min_duration_minutes != before.min_duration_minutes
    ):
        patch.min_duration = timedelta(minutes=after.min_duration_minutes)
    return patch

