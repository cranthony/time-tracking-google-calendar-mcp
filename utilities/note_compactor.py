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
settled isn't offered again. The one event that ended within `_LOOKBACK`
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
from utilities.facets import Facets, facet_problems
from utilities.goal_details import WHAT_MATTERS, GoalDetails, WhatMatters, add_to_section, checked_what_matters, section
from utilities.history_digest import history_digest
from utilities.traits import activity_label
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
from utilities.compaction_timeline import Timeline, join_days
from utilities.note_compaction import (
    CompactionChange,
    CompactionError,
    CompactionPlan,
    EventDecision,
    EventState,
    PlanNote,
    facets_from_dict,
    plan_compaction,
    planned_timeline,
)
from utilities.noted_time_sheet import NotedTime, NotedTimeSheet, SheetNote
from utilities.goals import OVERALL_ID, Goals, GoalTree
from utilities.reallocating_calendar import ReallocatingCalendar

logger = logging.getLogger(__name__)

_CANDIDATE_WINDOW = timedelta(hours=1)
"""How far either side of a note's time a planned event may start or end
and still be offered to it as a candidate."""

_HINT_HISTORY = timedelta(days=28)
"""How far back goals are looked for to suggest for an event."""

_MAX_DAYS = 7
"""The most days one compaction takes on, oldest first: a longer backlog
takes more than one, so each plan stays small enough to review."""

_LOOKBACK = timedelta(minutes=15)
"""How long before the compaction window starts an event may have ended
and still be offered (only the latest one) -- see the module docstring."""

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
    "'create' {summary, a start and an end (a time or note each), goal_ids?}: something "
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
    "GOALS: each event can serve goals (`goals` lists them; `goal_ids` on an event, primary "
    "first). Tagging events with goals should cost the user almost nothing, so: for every past "
    "event with `suggested_goal_ids`, add a 'keep' {event_id, goal_ids} applying them -- without "
    "asking, unless the notes clearly say otherwise; give a 'create' goal_ids when the notes "
    "clearly imply them; otherwise leave goals alone ('keep' without goal_ids keeps them, and [] "
    "clears them). Ask about a goal only when genuinely torn between two. In the timeline, ◆ marks "
    "a goal an event already serves and ◇ one it's being given (or, before deciding, one "
    "suggested); a correction from the user is just a new dry run. "
    "FACETS: every past event with `traits_goal_ids` carries a goal rated by traits (often a "
    "person), so record what happened at it: add `facets` to its 'keep' (or 'create'), judged "
    "from its notes, title and description -- with_goal_ids (goals of the people present; "
    "usually its traits goals), for_goal_ids (people it was done for who weren't there: "
    "preparing a gift or a plan -- then they're not in with_goal_ids), activity and place (short "
    "labels: REUSE the labels in that goal's `traits_goals` digest when they fit, so they group, and use "
    "its `cadence_activities` labels EXACTLY for those activities -- its cadences count only events "
    "labelled so), "
    "creative (0-3: made something together), new (none, activity, place or both: new to "
    "them, judged against the digest -- an activity or place the digest lists isn't new), effort "
    "(0-3: effort beyond showing up -- prepared, cooked, hosted, traveled), attention (0-3: the "
    "quality of attention, from the notes; leave it out if they don't say) and why (one line of "
    "evidence). Leave out what the notes don't support, rather than guess. Facets replace an "
    "event's facets whole, so an event that has some (`facets`) keeps them unless you send new "
    "ones; don't redo them without a reason. In the timeline, ▸ marks facets an event has and "
    "▹ ones it's being given. "
    "WHAT MATTERS TO THEM: `traits_goals` also shows each goal's \"What matters to them\" "
    "section -- facts, upcoming moments and preferences. When the notes reveal something new "
    "worth remembering about a person (a birthday, a new job, a worry, a favorite), pass it in "
    "compact_notes' `what_matters` {goal_id, items: [one line each]}; it's added, dated, when the "
    "plan is applied. Don't repeat what the section already says. "
    "After every dry run, show the user the result's `timeline.text` verbatim in a code block "
    "(it's laid out narrow enough for a phone, so don't reformat or widen it), and list the "
    "warnings, asking whether to apply it or what to change. " + _APPROVAL_RULE
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
    goal_ids: list[str] | None = None
    """The goals it serves now, primary first."""

    goal_names: list[str] | None = None
    suggested_goal_ids: list[str] | None = None
    """For a past event serving no goals: those the latest event with the
    same title, in the last few weeks, served -- to apply unless the notes
    say otherwise (see `DECISION_GUIDE`)."""

    priority: int | None = None
    is_fixed_time: bool | None = None
    facets: Facets | None = None
    """What happened at it, if it's been recorded (see utilities/facets.py)."""

    traits_goal_ids: list[str] | None = None
    """For a past event: the goals rated by traits that it counts toward
    (its goals, or their ancestors, with a traits measure) -- give it
    facets (see `DECISION_GUIDE`). `None` if none."""


@dataclass(kw_only=True)
class TraitsGoalContext:
    """A goal rated by traits that one of the events counts toward."""

    id: str
    path: str
    digest: str
    """Its history digest: the activities and places of its past events,
    with how often and when (see utilities/history_digest.py) -- the
    labels to reuse in facets, and what's not new."""

    what_matters: str | None = None
    """Its description's "What matters to them" section, if it has one."""

    cadence_activities: list[str] | None = None
    """The activities its own trait parts count (a visit every 21 days,
    say): label events of those activities with exactly these, or they
    won't count."""


@dataclass(kw_only=True)
class ContextGoal:
    id: str
    path: str


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

    compaction_window_start: datetime | None = None
    """Where the events offered start: the later of the (first) day's
    start and the last compaction -- see the module docstring."""

    timeline: Timeline | None = None
    """`notes` beside `events` as planned -- see
    utilities/compaction_timeline.py."""

    goals: list[ContextGoal] | None = None
    """The goals an event can be given: every active one."""

    previous_note: PreviousNote | None = None
    """The latest already-compacted note, however long ago it was
    written -- see the module docstring."""

    traits_goals: list[TraitsGoalContext] | None = None
    """The goals rated by traits that the events count toward, with their
    digests and what matters to them -- see `DECISION_GUIDE`."""

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


@dataclass(kw_only=True)
class _Walked:
    """One day of a batch, as `NoteCompactor._walk` reached it."""

    day: _Day
    plan: CompactionPlan | None = None
    decisions: list[EventDecision] = field(default_factory=list)
    ignore_notes: list[str] = field(default_factory=list)


_STATE_FIELDS = (
    "summary", "start", "end", "description", "location", "status",
    "is_fixed_time", "priority", "event_label_id", "goal_ids", "min_duration", "facets",
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
        goals: Goals | None = None,
        marker: CompactionMarker | None = None,
        details: GoalDetails | None = None,
    ) -> None:
        """`calendar` reads the day's events (through the same goal-aware
        view reallocation uses); `client` is what the planned changes are
        written through (a GoalCalendar, so each event's label follows
        its goals). `goals` names, suggests and checks events' goals.
        `marker`, if given, is moved to each compaction once it's stamped
        (see utilities/compaction_marker.py). `details` holds goals'
        descriptions, whose "What matters to them" sections compaction
        shows and adds to."""
        self._marker = marker
        self._details = details
        self._calendar = calendar
        self._client = client
        self._goals = goals
        self._notes = notes
        self._journal = journal
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def prepare(self) -> CompactionContext:
        self._prefetch()
        self._journal.garbage_collect()
        open_ids = self._journal.open_compactions()
        open_id = self._journal.load(open_ids[0][0]).batch_id if open_ids else None
        walked, remaining = self._walk(self._clock())
        if not walked:
            return CompactionContext(notes=[], events=[], open_compaction=open_id)
        days = [w.day for w in walked]
        tree = self._goals.tree() if self._goals else None
        names = _goal_names(tree)
        suggested = self._suggestions(days, tree) if tree is not None else {}
        traits_of = _traits_goals(days, tree) if tree is not None else {}
        notes: list[ContextNote] = []
        events: dict[str, ContextEvent] = {}
        timelines: list[tuple[datetime, Timeline]] = []
        for day in days:
            first = min(n.note.timestamp for n in day.notes)
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
                        goal_ids=e.goal_ids,
                        goal_names=[names.get(g, g) for g in e.goal_ids] if e.goal_ids is not None else None,
                        suggested_goal_ids=suggested.get(e.id),
                        priority=e.effective_priority,
                        is_fixed_time=e.is_fixed_time,
                        facets=e.facets,
                        traits_goal_ids=traits_of.get(e.id),
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
            goals=[
                ContextGoal(id=g.id, path=tree.path(g.id)) for g in tree.ordered() if g.active and g.id != OVERALL_ID
            ] if tree is not None else None,
            previous_note=PreviousNote(
                timestamp=previous_note.timestamp, description=previous_note.description
            ) if previous_note is not None else None,
            traits_goals=self._traits_context(days, tree, traits_of) if traits_of else None,
        )

    def _traits_context(
        self, days: list[_Day], tree: GoalTree, traits_of: dict[str, list[str]]
    ) -> list[TraitsGoalContext]:
        """Each goal rated by traits that the days' events count toward,
        with its digest of the events before the first day, and what
        matters to it."""
        goal_ids = list(dict.fromkeys(g for ids in traits_of.values() for g in ids))
        start = days[0].compaction_window_start
        history = [e for e in self._calendar.list_events(start - timedelta(days=180), start) if e.status != "cancelled"]
        tz = start.tzinfo
        context = []
        for goal_id in goal_ids:
            goal = tree.by_id[goal_id]
            description = self._details.get(goal_id) if self._details is not None else None
            context.append(
                TraitsGoalContext(
                    id=goal_id,
                    path=tree.path(goal_id),
                    digest=history_digest(goal, tree, history, start, tz).text,
                    what_matters=section(description, WHAT_MATTERS),
                    cadence_activities=_cadence_activities(goal.measure) or None,
                )
            )
        return context

    def _prefetch(self, *, goals: bool = True) -> None:
        """Read the notes tab, the journal and (if `goals`) the goals tab
        -- all a step reads of the spreadsheet -- in one request, so every
        later read of them in the same tool call is served from the cache
        (see `SheetsClient.prefetch`): Google Sheets caps read requests at
        60 a minute."""
        tabs = [self._notes.whole_tab, self._journal.whole_tab]
        if goals and self._goals is not None:
            tabs.append(self._goals.whole_tab)
        if goals and self._details is not None:
            tabs.append(self._details.whole_tab)
        self._notes.prefetch(tabs)

    def _suggestions(self, days: list[_Day], tree: GoalTree) -> dict[str, list[str]]:
        """Goals to suggest for the days' past events that serve none: those
        the latest earlier event with the same title served, within
        _HINT_HISTORY (ignoring goals since deleted). Recurring events need
        nothing extra: an instance's goals come with its series."""
        bare = [e for day in days for e in day.events if e.id and not e.goal_ids and e.end <= day.now and e.summary]
        if not bare:
            return {}
        start = days[0].compaction_window_start
        history = self._calendar.list_events(start - _HINT_HISTORY, start)
        hints: dict[str, list[str]] = {}
        for event in sorted(history, key=lambda e: e.start):
            usable = [
                g for g in event.goal_ids or ()
                if g in tree.by_id and tree.by_id[g].status != "deleted"
            ]
            if event.summary and usable and event.status != "cancelled":
                hints[_title_key(event.summary)] = usable
        return {e.id: hints[_title_key(e.summary)] for e in bare if _title_key(e.summary) in hints}

    def dry_run(
        self,
        decisions: list[EventDecision],
        ignore_notes: list[str] | None = None,
        what_matters: list[WhatMatters] | None = None,
    ) -> CompactionResult:
        """`what_matters`: lines to add to goals' "What matters to them"
        sections when the plan is applied."""
        self._prefetch()
        self._require_no_open_compaction()
        decisions = self._checked_facets(decisions)
        additions = self._checked_what_matters(what_matters or [])
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
                plan = self._plan(day, mine, ignored)
            except CompactionError as exc:
                raise CompactionError(f"{label}: {exc}" if label else str(exc)) from exc
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
                    what_matters=additions if number == len(walked) else {},
                )
                for number, w in enumerate(walked, start=1)
            ]
        )
        changes = [c for w in walked for c in w.plan.changes]
        note_count = sum(len(w.day.notes) for w in walked)
        days = f" over {len(walked)} days" if len(walked) > 1 else ""
        tree = self._goals.tree() if self._goals else None
        adding = [
            f"add to {tree.by_id[g].name if tree else g}'s \"{WHAT_MATTERS}\": " + "; ".join(items)
            for g, items in additions.items()
        ]
        return CompactionResult(
            status="planned",
            compaction_id=compaction_id,
            changes=changes,
            warnings=[warning for w in walked for warning in w.plan.warnings] + adding,
            timeline=join_days([(w.day.day_start, w.plan.timeline) for w in walked]),
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
        then its notes stamped, before the next day's."""
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
            raise CompactionError(f"compaction {compaction_id} was abandoned; run a new dry run")
        pending = [d for d in days if d.status != STAMPED]
        if pending[0].status == PLANNED:
            self._require_no_open_compaction()
            self._verify_unchanged(pending)
        for journal in pending:
            if journal.status == PLANNED:
                self._journal.set_status(journal, APPLYING)
            for step in journal.steps:
                if step.status != "done":
                    self._apply_step(journal, step)
                    self._journal.mark_step_done(step)
            self._add_what_matters(journal)
            self._journal.set_status(journal, APPLIED)
            self._notes.mark_compacted(journal.note_ids, journal.id)
            self._journal.set_status(journal, STAMPED)
        steps = sum(len(d.steps) for d in days)
        notes = sum(len(d.note_ids) for d in days)
        return CompactionResult(
            status="applied",
            compaction_id=compaction_id,
            changes=changes,
            warnings=[w for d in days for w in d.warnings] + self._move_marker(days[-1].now),
            message=f"applied {steps} change(s) and marked {notes} note(s) compacted",
        )

    def _add_what_matters(self, journal: JournalCompaction) -> None:
        """Add `journal`'s lines to goals' "What matters to them" sections,
        dated its day. A line already there is skipped, so doing it again
        (resuming a commit) is harmless."""
        if not journal.what_matters or self._details is None:
            return
        on = journal.now.date()
        for goal_id, items in journal.what_matters.items():
            current = self._details.get(goal_id)
            updated = add_to_section(current, items, on)
            if updated != (current or ""):
                self._details.set(goal_id, updated)

    def _checked_facets(self, decisions: list[EventDecision]) -> list[EventDecision]:
        """`decisions` with their facets normalized; CompactionError if any
        aren't well formed or name goals that aren't."""
        tree = self._goals.tree() if self._goals else None
        checked, problems = [], []
        for number, decision in enumerate(decisions, start=1):
            if decision.facets is not None:
                facets = decision.facets.normalized()
                label = f"decision {number} ({decision.action}{' ' + decision.event_id if decision.event_id else ''})"
                problems += [f"{label}: its facets {p}" for p in facet_problems(facets)]
                named = [*(facets.with_goal_ids or ()), *(facets.for_goal_ids or ())]
                if tree is not None and named:
                    try:
                        tree.check_goal_ids(named, for_events=True)
                    except ValueError as exc:
                        problems.append(f"{label}: its facets: {exc}")
                decision = replace(decision, facets=facets)
            checked.append(decision)
        if problems:
            raise CompactionError("\n".join(problems))
        return checked

    def _checked_what_matters(self, additions: list[WhatMatters]) -> dict[str, list[str]]:
        """`additions` by goal id; CompactionError for an unknown goal or
        a line that isn't one."""
        if not additions:
            return {}
        if self._details is None:
            raise CompactionError("this calendar has no goal descriptions to add what matters to")
        tree = self._goals.tree() if self._goals else None
        known = set(tree.by_id) if tree is not None else {a.goal_id for a in additions}
        try:
            return checked_what_matters(additions, known)
        except ValueError as exc:
            raise CompactionError(str(exc)) from exc

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
        self._prefetch(goals=False)
        return edit_note(self._notes, self._journal, note_id, timestamp=timestamp, description=description)

    def delete_note(self, note_id: str) -> NotedTime:
        """See the module-level `delete_note`."""
        self._prefetch(goals=False)
        return delete_note(self._notes, self._journal, note_id)

    def abandon(self, compaction_id: str) -> CompactionResult:
        days = self._journal.load_batch(compaction_id)
        if all(d.status == STAMPED for d in days):
            raise CompactionError(f"compaction {compaction_id} is already complete; there's nothing to abandon")
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
    ) -> _Day:
        """The day the oldest of `notes` falls in -- or, with none, the one
        `window_start` does -- with its notes. Its
        compaction window starts at `window_start`, if given, or else no
        earlier than `last_stamped`. `first`: whether it's the batch's
        first day, the only one shown the latest compacted note and the
        last compaction, as context. `sleepless`: nights that don't end
        it, and `border`: where it ends instead of where its night does
        -- see `_cut`."""
        oldest = min(n.note.timestamp for n in notes) if notes else window_start
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
                "abandon_compaction, before starting another"
            )

    def _verify_unchanged(self, days: list[JournalCompaction]) -> None:
        """Plan `days` -- a batch's days not yet applied -- again, each with
        its own decisions, and check they come out as they did when they
        were previewed."""
        stale = CompactionError(
            f"the notes or calendar changed since compaction {days[0].batch_id} was previewed; "
            "run a new dry run"
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
            plan = self._plan(day, journal.decisions, journal.ignore_notes)
            if comparable(plan.changes) != comparable(journal.changes()):
                raise stale
            return _Walked(day=day, plan=plan)

        walked, _remaining = self._walk(days[-1].now, plan_day, night, max_days=len(days))
        if len(walked) != len(days):
            raise stale

    def _plan(
        self, day: _Day, decisions: list[EventDecision], ignore_notes: list[str] | None
    ) -> CompactionPlan:
        """`plan_compaction` for `day`, with the decisions' goals checked,
        and created events' labels checked against the calendar's: Calendar
        rejects inserting an event with a label it doesn't have (HTTP 400),
        which would stop the commit partway -- and every retry with it. A
        created event that has one anyway (a split continuation, cloned
        label and all, from reflowing the day) just drops it; the label it
        gets follows its goals when it's written. Only reads the labels when
        a created event has one."""
        tree = self._goals.tree() if self._goals else None
        if tree is not None:
            current = {e.id: e.goal_ids or [] for e in day.events if e.id}
            problems = []
            for d in decisions:
                if d.goal_ids:
                    try:
                        tree.check_goal_ids(d.goal_ids, for_events=True, already=current.get(d.event_id, []))
                    except ValueError as exc:
                        problems.append(f"{d.action} {d.event_id or d.summary!r}: {exc}")
            if problems:
                raise CompactionError("\n".join(problems))
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
            goal_names=_goal_names(tree),
            previous_note=_latest_compacted(day),
            last_compaction=day.last_compaction,
            next_day_follows=day.has_next,
        )
        labelled = [c for c in plan.changes if c.action == "create" and c.after.event_label_id is not None]
        if not labelled:
            return plan
        labels, _etag = self._client.list_event_labels()
        label_ids = {label.id for label in labels}
        for change in labelled:
            if change.after.event_label_id not in label_ids:
                change.after.event_label_id = None
        return plan

    def _apply_step(self, journal: JournalCompaction, step: JournalStep) -> None:
        if step.action == "create":
            event = step.after.to_event(_new_event_id(journal.id, step.step))
            try:
                self._client.create_event(event)
            except HttpError as exc:
                # A retried create: the deterministic id already exists.
                if exc.resp.status != 409:
                    raise
        else:
            self._client.update_event(_patch_for(step))


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
        raise CompactionError(str(exc)) from exc


def delete_note(notes: NotedTimeSheet, journal: CompactionJournal, note_id: str) -> NotedTime:
    """`NotedTimeSheet.delete`, refused like `edit_note`."""
    _require_not_being_applied(journal, note_id)
    try:
        return notes.delete(note_id)
    except ValueError as exc:
        raise CompactionError(str(exc)) from exc


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
                "abandon it with abandon_compaction, first"
            )


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


def _cadence_activities(measure: dict | None) -> list[str]:
    """The activities a traits measure's own parts count (see
    utilities/traits.py), in order."""
    overrides = (measure or {}).get("parts")
    found: list[str] = []
    for parts in overrides.values() if isinstance(overrides, dict) else ():
        for part in parts if isinstance(parts, list) else ():
            activity = part.get("activity") if isinstance(part, dict) else None
            if isinstance(activity, str) and activity_label(activity) not in found:
                found.append(activity_label(activity))
    return found


def _traits_goals(days: list[_Day], tree: GoalTree) -> dict[str, list[str]]:
    """For each of the days' past events that counts toward a goal rated
    by traits -- one of its goals, or their ancestors, has a traits measure
    -- those goals, by event id."""
    rated = [g.id for g in tree.goals if g.active and isinstance(g.measure, dict) and g.measure.get("kind") == "traits"]
    if not rated:
        return {}
    found: dict[str, list[str]] = {}
    for day in days:
        for event in day.events:
            if not event.id or event.start >= day.now or event.status == "cancelled":
                continue
            ids = [*(event.goal_ids or ()), *((event.facets.with_goal_ids or []) if event.facets else [])]
            goals = [g for g in rated if any(tree.under(i, g) for i in ids if i in tree.by_id)]
            if goals:
                found[event.id] = goals
    return found


def _goal_names(tree: GoalTree | None) -> dict[str, str]:
    return {g.id: g.name or g.id for g in tree.goals if g.id} if tree is not None else {}


def _title_key(summary: str) -> str:
    return " ".join(summary.casefold().split())


def _new_event_id(compaction_id: str, step: int) -> str:
    """A deterministic id for a created event (Calendar accepts a-v and
    0-9, 5-1024 characters), so retrying a create can't duplicate it."""
    return f"cmp{compaction_id}s{step:03d}"


def _patch_for(step: JournalStep) -> Event:
    if step.action == "cancel":
        return Event(id=step.event_id, status="cancelled")
    before, after = step.before, step.after
    patch = Event(id=step.event_id)
    for name in ("summary", "start", "end", "description", "location", "priority", "event_label_id", "goal_ids"):
        value = getattr(after, name)
        if value is not None and value != getattr(before, name):
            setattr(patch, name, value)
    if after.facets != before.facets:
        # Empty facets remove them: see Event.facets.
        patch.facets = facets_from_dict(after.facets)
    if after.is_fixed_time:
        # An actual event is pinned explicitly, not left to inherit it
        # from its label.
        patch.is_fixed_time = True
    if after.min_duration_minutes is not None and (
        after.is_fixed_time or after.min_duration_minutes != before.min_duration_minutes
    ):
        patch.min_duration = timedelta(minutes=after.min_duration_minutes)
    return patch

