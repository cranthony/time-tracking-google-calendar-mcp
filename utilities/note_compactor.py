"""Compacting notes: the application-level policy that ties the notes tab
(utilities/noted_time_sheet.py), the calendar, the planner
(utilities/note_compaction.py) and the write-ahead journal
(utilities/compaction_journal.py) together.

A compaction is a *proposal* the user confirms (see utilities/
compaction_proposals.py, and docs/compaction-proposals.md for the
contract). The flow, as the MCP tools expose it:

1. `prepare` -- read-only. Hands the client every day from the last
   compaction up to now, notes or not (each uncompacted note with a
   stable id and a shortlist of nearby planned events), those days'
   planned events, the two side by side as a `Timeline` (see utilities/
   compaction_timeline.py), and the open proposal, if there is one. The
   client compares them and decides, event by event, what the notes show
   happened differently.
2. `dry_run` (compact_notes) -- validates the client's decisions, lays
   the user's edits over them, and writes the plan to the journal as a
   proposal's revision, returning it (and the resulting timeline).
   Nothing on the calendar changes. The user reviews it in the app:
   `amend` (their edits), `add_note`/`withdraw_note` (feedback for the
   client, answered by its next revision), `get_proposal`.
3. `confirm` -- the user's alone: plans the revision again, and if
   nothing changed, applies it (`commit`), journaling each step as it
   goes, and stamping each day's notes as compacted once that day's steps
   are done. If it dies partway, `finish` resumes it: the journal
   remembers exactly what was confirmed and how far it got. A write that
   can never succeed proposes what's left of it again (`_rebuild`). A
   proposal can be `abandon`ed.
4. `record_judgments` -- the facts written, the client judges the traits
   of each event's people (see utilities/judgments.py). Nothing about
   judging is sent before the user approves the plan: the commit hands
   back the plan's final timeline and, beside it, the judgments due, in
   one compact list (each event's people and their parts, then each part
   and each person's history once). A compaction isn't complete until
   they're all judged. `judgments_due` hands them over again -- to
   finish, or redo.

A day at a time, all at once: one compaction takes on every day from
the last compaction up to now (at most `_MAX_DAYS`, oldest first), notes
or not, but plans each day on its own, and the user reviews them
together. The first day is the one the last compaction ran in (before
there's been one, the one the oldest note falls in, or today): it starts
when the last end-of-day sleep event that began before then ends (or at
the note, if it's earlier -- a note written before the planned wake-up
time), and runs to the end of the next end-of-day sleep event after that
(or 24 hours, if there isn't one). Each day after the first is planned as if the
one before it had already been compacted: it starts where that one ended
(its `now`), against the calendar as that one's plan would leave it
(`_PlannedCalendar`). A day before the last is wholly past, so its plan
only records it; only the last day, the one with now in it, has a
future to reschedule. The night between two days is decided once,
whole, by the earlier day, and its end is the border between them (see
`_cut`): a note the decisions use to end it -- woke early, or slept in
-- moves the border to it, taking every note up to it into the earlier
day, and the later day starts there. After a late wake-up, the later day
still starts at the planned one, so the morning the night now runs over
is settled there: past events under it, and later ones it reaches, are
overlaps to resolve.
A cancelled night -- no sleep -- makes the two days one long one. In the journal, each day is a compaction of
its own, applied and stamped in order, and together they're a *batch*
under the first day's id (see utilities/compaction_journal.py) -- the
compaction id the MCP tools use. Notes written after now wait for a
later compaction.

A day can take several compactions, so the events offered -- the
*compaction window* -- start at the later of the day's start and the last
*stamped* compaction's `now` (or, for a later day of a batch, the day
before it's): whatever an earlier compaction already
settled isn't offered again. Nor is anything after it skipped: a day
with no notes is confirmed as planned. The one event that ended within `_LOOKBACK`
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
event that ran long runs into what follows it, which the decisions then
have to move too -- and nothing may overlap any of them (see utilities/
note_compaction.py). But compaction is about recording the past,
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

import hashlib
import json
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
from utilities.habits import SUBJECT_PREFIX, Habit, subject_id
from utilities.judgments import (
    HabitJudgmentsDue,
    Judging,
    Judgment,
    JudgmentRequest,
    JudgmentsDue,
    JudgmentsResult,
)
from utilities.locations import Locations
from utilities.people import People
from utilities.compaction_journal import (
    ABANDONED,
    APPLIED,
    APPLYING,
    FAILED,
    OPEN_STATUSES,
    PLANNED,
    PROPOSED,
    STAMPED,
    SUPERSEDED,
    CompactionJournal,
    JournalCompaction,
    JournalStep,
    PlannedDay,
    RevisionMeta,
)
from utilities.compaction_proposals import (
    AdditionChoice,
    Feedback,
    FeedbackReply,
    NoteEdit,
    Proposal,
    ProposalContext,
    ProposalEvent,
    ProposalNote,
    ProposalResult,
    ProposalState,
    ProposalSummary,
    UserEdit,
    claude_key,
    claude_key_number,
    decision_shape,
    edit_id,
    edit_json,
    feedback_id,
    merge,
    new_proposal_id,
    revision_id,
    settle_refs,
    split_feedback_id,
)
from utilities.cancellations import Cancellations
from utilities.compaction_timeline import Timeline, join_days, render
from utilities.note_compaction import (
    CompactionChange,
    CompactionError,
    CompactionPlan,
    EventDecision,
    EventState,
    NoteAnnotation,
    PlanNote,
    Problem,
    facts_from_dict,
    plan_compaction,
    planned_timeline,
)
from utilities.noted_time_sheet import NotedTime, NotedTimeSheet, SheetNote

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

_PREFETCH_BEFORE = timedelta(hours=24) + _LOOKBACK
_PREFETCH_AFTER = timedelta(hours=48)
"""What `NoteCompactor._walk` lists up front, around its notes: from the
night before the oldest (a day starts no earlier than 24 hours before
its oldest note, and offers what ended `_LOOKBACK` before that) to two
days past now (where the last day ends, unless more than one night in a
row is cancelled -- a listing past it just isn't answered from this)."""

_APPROVAL_RULE = (
    "Nothing is applied until the user confirms it, in the app (confirm_proposal isn't yours to "
    "call). If they ask you for changes, make them with compact_notes again, passing proposal_id "
    "and the revision you started from."
)
"""Repeated wherever a model is told what to do after proposing."""

DECISION_GUIDE = (
    "`timeline` shows the notes beside the planned events -- for every day of notes up to now, each "
    "under a heading with its date (`days` lists them when there's more than one). Each day is "
    "compacted on its own, but you decide them all in one list. Compare them and decide, event "
    "by event, what the notes show happened differently -- then call compact_notes with them, as "
    "`updates`, `creates` and `cancels`. SILENCE MEANS ON SCHEDULE: any past event you don't mention is recorded exactly "
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
    "`updates` {event_id}: it happened; each edge stays as planned unless you move it -- "
    "{start_note}/{end_note} (a note id) sets that edge to the note's time and links the note to "
    "it, {start}/{end} sets an explicit time (add the note too when it gives a time relative to "
    "itself, e.g. 'leaving 15 minutes early'). An update also takes {summary} to rename an event and "
    "{annotate} to add text to its description. "
    "`creates` {summary, a start and an end (a time or note each), action_ids?, facts?}: something "
    "unplanned happened. "
    "`cancels` {event_id, counts_against_follow_through}: it didn't happen -- true if the user "
    "dropped it (skipped it, didn't get to it: it counts against the follow-through of whoever it "
    "was planned with), false if the plan changed for another reason (someone else called it off, "
    "it moved elsewhere). An event goes in only one of the lists. When the user tells you they "
    "don't remember where one event ended and the next began, grow one with an update (renamed "
    "after both) and cancel the other, not counting it -- only then, never just because the notes "
    "are sparse. "
    "NOTHING IS MOVED TO MAKE ROOM: every event you update or create must end after it starts and "
    "must not overlap any other event in `events` -- past or still to come, the others you change "
    "included. When a moved edge runs into a neighbor, move, shorten or cancel the neighbor in the "
    "same call (an overrun usually delays or shortens the next event; ask the user if the notes "
    "don't say which gives way). A call that breaks this changes nothing, and lists every problem "
    "and the day as it would leave it, so fix them all at once. "
    "Every note you don't use as a start_note/end_note has its text added to the description of the "
    "event it falls within; list any that shouldn't be in `ignore_notes`, and any that belong with "
    "another event in `annotate_notes` ({note_id, event_id} -- an event's id, or a create's `key` "
    "from the open proposal; for a new create, put the text in its `annotate`). Use only note ids from "
    "`notes` (only this round's -- a longer backlog than this takes another round, see "
    "`remaining_note_count`) and event ids from `events`. "
    "`events` may start with one that ended just before `compaction_window_start` (usually last "
    "night's sleep); if a note shows it actually ran later -- the user slept in -- move its end "
    "with an update, and say when whatever it now overlaps happened. "
    "An event with `history_until` was settled by an earlier compaction up to then -- usually one "
    "still going on when it ran: its start, and its lasting until then, are fact, so keep its start, "
    "end it no earlier (it may well have run later), and don't cancel it; compact_notes "
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
    "`now`, except for later events near a note (one may be what a note starts early) and, once "
    "proposed, later events the plan changes. "
    "An update that moves a future event reschedules it -- for 'move lunch later and adjust the "
    "afternoon': move each event it now runs into too, in the same call. A day's end-of-day sleep "
    "event works differently: moving its start moves bedtime (an earlier one needs whatever runs "
    "past it shortened or cancelled in the same call), and its end -- the wake-up "
    "time -- starts the next day. THE NIGHT IS THE BORDER BETWEEN DAYS: give each night at most "
    "one decision, whichever day's heading its notes are under. A note that marks waking up "
    "(early or late) is that night's end_note -- it moves the border to that note, so the notes "
    "up to it belong to the day before; a note in the night that doesn't end it ('can't sleep') "
    "is just added to it. If the user slept in, the morning events the night now runs over are "
    "settled with the next day: a past one it overlaps has to be moved or cancelled (the next day's "
    "decisions may use the wake-up note as an edge too), and later ones it reaches have to move "
    "too. Cancel a "
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
    "For every past event with `suggested_action_ids`, add an update {event_id, action_ids} applying "
    "them unless the notes say otherwise; give a create its action_ids too. When no action fits, "
    "add one (see NEW below) rather than force a poor match. An update without action_ids keeps them, "
    "and [] clears them. In the timeline, ◆ marks an action an event already has and ◇ one it's "
    "being given (or, before deciding, one suggested). "
    "FACTS: add `facts` to the event's update (or create): location_id (from `locations`, matched "
    "by their hints), with_ids (people from `people` who were there; never \"self\" -- the user is at "
    "every event), for_ids (people it was done for who weren't there: preparing a gift or a plan -- "
    "then they're not in with_ids), and notes: {person id: a subjective line on how it was for "
    "them} for each person who was there, \"self\" for the user -- whatever the notes say that "
    "might help judge it later (they were tired, it was their idea, we laughed a lot). The notes "
    "aren't judgments: you don't know the traits, so just record what happened. Facts replace an "
    "event's facts whole, so an event that has some (`facts`) keeps them unless you send new ones. "
    "In the timeline, ▸ marks facts an event has and ▹ ones it's being given. "
    "Once proposed, ⚠ marks a past event still missing its action or location (listed again "
    "under `Missing:` at the end of the day): settle it from the notes and propose again, or, if "
    "they don't say, ask the user -- or, with no one to ask (a scheduled run), leave it for the "
    "user to settle in review. "
    "NEW actions, people and locations: when an event's action, a person or a place isn't in the "
    "lists, add it with compact_notes' new_actions, new_people or new_locations -- each with a "
    "`ref` starting \"new:\" (\"new:ukulele\") that your updates and creates use wherever its id would go -- "
    "rather than by separate tools: they're created when the plan is applied, so the user "
    "confirms them with it. A new action is a verb phrase, its status active (the user approves "
    "it with the plan); a new person needs a context when their name is taken; a new location "
    "needs a hint. Check the lists first: don't add one that's already there under another name. "
    "In a conversation, confirm with the user anything you couldn't settle from the notes -- who "
    "was there, where it was -- in a short list before proposing; with no one to ask, propose what "
    "the notes do say. The user confirms the whole proposal, in the app. "
    "In a conversation, after every compact_notes show the user the result's `timeline.text` "
    "verbatim in a code block (it's laid out narrow enough for a phone, so don't reformat or widen "
    "it -- it shows each event's actions and facts compactly, under it), then the new actions, "
    "people and locations it adds, and the warnings. "
    "PROPOSALS: compact_notes doesn't apply anything -- it writes a proposal (or a new revision of "
    "the open one) for the user to review in the app, where they confirm it, edit it, or leave "
    "you feedback. With a proposal open (`proposal`), every compact_notes call revises it: pass "
    "its proposal_id and the revision you started from, and send ALL your decisions again -- "
    "`proposal.updates`, `creates` and `cancels` are your current ones; keep each create's `key` "
    "so the user's edits of it follow it -- changed as the notes and the feedback say, planned to "
    "now. The user's own edits (`proposal.user_edits`) are laid over yours by the server: never "
    "send them, and don't fight them -- that includes what they said a note is for (an edit with "
    "action `note`), and the additions they settled (`proposal.settled_additions`: created, found "
    "among those there, or dropped -- name each by its `id` from then on, or leave it out; send "
    "`proposal.new_actions`, `new_people` and `new_locations` again for the rest). Answer every open feedback item (`proposal.feedback`, "
    "status open) in `replies`, saying what you changed, or asking what they meant if you can't "
    "tell; to change an event (or a note) the user edited, the feedback you answer has to be about "
    "that event (`event_id`) or note (`note_id`). " + _APPROVAL_RULE
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
    facts: Facts | None = None
    """Where, who with, who for, and notes on each person there, if
    they've been recorded (see utilities/facts.py)."""

    history_until: datetime | None = None
    """How much of it an earlier compaction settled, if it started before
    that one's `now`: its start, and its lasting until this, can't change
    (see `DECISION_GUIDE`)."""


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

    proposal: ProposalContext | None = None
    """The open proposal, to revise or extend: your decisions in it, the
    user's edits and their feedback (see `DECISION_GUIDE`)."""

    instructions: str = DECISION_GUIDE


@dataclass(kw_only=True)
class CompactionResult:
    status: Literal[
        "planned",
        "proposed",
        "applied",
        "already_compacted",
        "abandoned",
        "nothing_to_compact",
    ]
    message: str
    compaction_id: str | None = None
    proposal_id: str | None = None
    revision: int | None = None
    changes: list[CompactionChange] = None  # type: ignore[assignment]
    warnings: list[str] = None  # type: ignore[assignment]
    timeline: Timeline | None = None
    """Once proposed: the notes beside the events as they'd end up -- show
    this to the user (see `DECISION_GUIDE`)."""

    additions: dict[str, list[dict]] | None = None
    """For a dry run: the actions, people and locations it adds when it's
    applied -- show these to the user too."""

    judgments: JudgmentsDue | None = None
    """Once applied: the judgments its events call for, to make now, of
    the events as `timeline` shows them (see utilities/judgments.py). The
    compaction isn't complete until they're recorded."""

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

    history_until: datetime | None = None
    """Where what's settled already ends: the last stamped compaction's
    `now` for the batch's first day, and where the day before it ends
    for each after it (see utilities/note_compaction.py)."""

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
    note_targets: dict[str, str] = field(default_factory=dict)


_STATE_FIELDS = (
    "summary", "start", "end", "description", "location", "status",
    "priority", "event_label_id", "action_ids", "facts",
)


class _PlannedCalendar:
    """A calendar's events as the planned changes `apply`d to it would
    leave them, without writing anything: what each day of a batch is
    planned against, so it starts from the day before it as planned."""

    def __init__(self, calendar) -> None:
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
        calendar,
        client,
        notes: NotedTimeSheet,
        journal: CompactionJournal,
        clock: Callable[[], datetime] | None = None,
        actions: Actions | None = None,
        people: People | None = None,
        locations: Locations | None = None,
        judging: Judging | None = None,
        cancellations: Cancellations | None = None,
    ) -> None:
        """`calendar` reads the day's events (through the same
        action-aware view, an ActionCalendar); `client` is what the planned
        changes are written through (an ActionCalendar, so each event's
        label follows its actions). `actions`, `people` and `locations`
        are what events' actions and facts name, and what a plan can add
        to. `judging`, if given, makes and records the judgments that
        complete a compaction (see utilities/judgments.py).
        `cancellations`, if given, records each event a compaction cancels
        for the people it counts against in follow-through, and the
        timeline lists them (see utilities/cancellations.py)."""
        self._cancellations = cancellations
        self._judging = judging
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
        proposal = self._proposal_context()
        walked, remaining = self._walk(self._clock())
        if not walked:
            return CompactionContext(
                notes=[], events=[], open_compaction=open_id, judgments_pending=pending, proposal=proposal
            )
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
                        facts=e.facts,
                        history_until=(
                            min(e.end, day.history_until)
                            if day.history_until is not None and e.start < day.history_until
                            else None
                        ),
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
            proposal=proposal,
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
        *,
        proposal_id: str | None = None,
        revision: int | None = None,
        replies: list[FeedbackReply] | None = None,
        annotate_notes: list[NoteAnnotation] | None = None,
    ) -> CompactionResult:
        """compact_notes: propose what happened, from the last compaction to
        now -- or, with the open proposal's `proposal_id` and the
        `revision` Claude started from, revise or extend it (see utilities/
        compaction_proposals.py). Nothing on the calendar changes.
        `decisions` are all of Claude's; `replies` answer the user's
        feedback, and `annotate_notes` add notes to particular events.
        `new_actions`, `new_people` and `new_locations`: what it adds when
        it's applied, which its decisions name by their refs (see
        utilities/compaction_additions.py)."""
        self._prefetch()
        # First: anything read after it deletes is read again.
        self._journal.garbage_collect()
        self._require_no_open_compaction()
        open_id = self._journal.open_proposal()
        if open_id is not None and proposal_id is None:
            raise CompactionError(
                f"proposal {open_id} is open: revise or extend it -- compact_notes with "
                f"proposal_id={open_id!r} and the revision you started from (prepare_compaction's "
                "`proposal` shows it)",
                category="open_proposal",
            )
        if proposal_id is not None and proposal_id != open_id:
            raise CompactionError(
                f"there's no open proposal {proposal_id!r}" + (f"; the open one is {open_id}" if open_id else ""),
                category="unknown_proposal",
            )
        claude_additions = Additions(
            actions=new_actions or [], people=new_people or [], locations=new_locations or []
        )
        claude_targets = _note_targets(annotate_notes or [])
        now = self._clock()
        current: list[JournalCompaction] | None = None
        edit_rows: list[tuple[int, UserEdit]] = []
        feedback_rows: list[tuple[int, Feedback]] = []
        base_meta: RevisionMeta | None = None
        if open_id is None:
            proposal = new_proposal_id(uuid.uuid4().hex)
            meta = None
        else:
            proposal = open_id
            current = self._current(proposal)
            meta = current[0].meta
            base_meta = self._base(proposal, meta, revision)
            edit_rows = self._journal.user_edits(proposal)
            feedback_rows = self._journal.feedback(proposal)
        answered, replaced = self._answers(proposal, replies or [], feedback_rows, edit_rows, base_meta)
        edits = [e for row, e in edit_rows if row not in replaced]
        # What the user settled of what's added is neither checked nor
        # created again; the decisions name it by its id from here.
        settled_additions = merge(proposal, [], edits).settled
        additions = _unsettled(claude_additions, settled_additions)
        problems = check_additions(additions, self._actions, self._people, self._locations)
        if problems:
            raise CompactionError.of(problems, "additions")
        decisions = self._checked_facts(decisions, claude_additions, settled=set(settled_additions))
        decisions, key_seq = _keyed(proposal, decisions, meta)
        aliases = meta.aliases if meta else {}
        settled = set(meta.settled) if meta else set()
        merged = merge(
            proposal, decisions, edits, known_ids=self._known_ids(now), aliases=aliases, settled=settled,
            claude_ignore=list(ignore_notes or []), claude_targets=claude_targets,
            known_notes=self._uncompacted_notes(now),
        )
        walked, remaining = self._plan_walk(
            _settled(merged), merged.ignore_notes, additions, now, note_targets=merged.note_targets
        )
        if not walked or (meta is None and not _has_anything(walked)):
            return CompactionResult(
                status="nothing_to_compact",
                message="nothing has happened to compact since the last compaction",
            )
        superseded = self._supersede_planned()
        revision_number = meta.revision + 1 if meta else 1
        outcomes = _outcomes(walked)
        new_meta = RevisionMeta(
            proposal=proposal,
            revision=revision_number,
            window_start=walked[0].day.compaction_window_start,
            created=now,
            base=revision,
            user_seq=max((e.seq for _, e in edit_rows), default=0),
            by="claude",
            reason="proposed" if meta is None else "revised for notes" if answered else "extended",
            changed=_changed(meta.outcomes if meta else None, outcomes),
            outcomes=outcomes,
            key_seq=key_seq,
            claude_decisions=decisions,
            claude_ignore_notes=sorted(set(ignore_notes or [])),
            claude_note_targets=claude_targets,
            claude_additions=claude_additions.to_json_dict(),
            aliases=aliases,
            settled=sorted(settled),
            judge_also=meta.judge_also if meta else [],
        )
        self._write_revision(walked, new_meta, additions, current)
        newer = {e.event_id: e.id for _, e in edit_rows if base_meta is not None and e.seq > base_meta.user_seq}
        for row, item in answered:
            if item.event_id in newer:
                item.superseded_by = [newer[item.event_id]]
            item.answered_in = revision_number
            self._journal.update_feedback(row, item)
        for row in replaced:
            self._journal.set_user_edit_status(row, "replaced")
        unknown = {e.id for e in merged.unknown}
        for row, edit in edit_rows:
            if edit.id in unknown and row not in replaced:
                self._journal.set_user_edit_status(row, "inapplicable")
        changes = [c for w in walked for c in w.plan.changes]
        note_count = sum(len(w.day.notes) for w in walked)
        days = f" over {len(walked)} days" if len(walked) > 1 else ""
        still_open = [f for _, f in feedback_rows if f.status == "open" and f.id not in {i.id for _, i in answered}]
        return CompactionResult(
            status="proposed",
            compaction_id=revision_id(proposal, revision_number),
            proposal_id=proposal,
            revision=revision_number,
            changes=changes,
            warnings=[warning for w in walked for warning in w.plan.warnings],
            timeline=join_days([(w.day.day_start, w.plan.timeline) for w in walked]),
            additions=additions.to_json_dict() or None,
            message=(
                f"proposal {proposal} revision {revision_number}: {len(changes)} calendar change(s) for "
                f"{note_count} note(s){days}, through {walked[-1].day.now.isoformat()}; nothing has been "
                "changed yet. In a conversation, show the user `timeline.text` in a code block (see the "
                "instructions from prepare_compaction) and the warnings. "
                + _APPROVAL_RULE
                + (f" (Replaced {superseded} unapplied plan(s) from before proposals.)" if superseded else "")
                + (
                    f" {len(still_open)} feedback item(s) came in after your revision began and are still "
                    "open: prepare_compaction again and answer them."
                    if still_open
                    else ""
                )
                + (
                    f" {remaining} later note(s) aren't part of it: they'll be in the next proposal."
                    if remaining
                    else ""
                )
            ),
        )

    def _plan_walk(
        self,
        decisions: list[EventDecision],
        ignore_notes: list[str] | None,
        additions: Additions,
        now: datetime,
        *,
        note_targets: dict[str, str] | None = None,
        max_days: int = _MAX_DAYS,
    ) -> tuple[list[_Walked], int]:
        """Walk the days up to `now`, planning each with its share of
        `decisions` (see `_decisions_for`), of the notes to ignore and of
        those to add to a particular event (`note_targets`)."""
        pending = list(decisions)
        ignoring = list(ignore_notes or [])
        targeting: dict[str, str | None] = dict(note_targets or {})

        def night(event_id: str, index: int) -> EventDecision | None:
            return next((d for d in pending if d.event_id == event_id), None)

        def plan_day(day: _Day, index: int) -> _Walked:
            label = _day_label(day) if day.has_next or index else None
            try:
                mine, pending[:] = _decisions_for(day, pending)
                note_ids = {n.id for n in day.notes}
                ignored = [i for i in ignoring if not day.has_next or i in note_ids]
                ignoring[:] = [i for i in ignoring if i not in ignored]
                targets = {n: e for n, e in targeting.items() if not day.has_next or n in note_ids}
                for note in targets:
                    del targeting[note]
                plan = self._plan(day, mine, ignored, additions, targets)
            except CompactionError as exc:
                raise CompactionError.wrapping(f"{label}: {exc}" if label else str(exc), exc) from exc
            if label:
                plan.warnings = [f"{label}: {w}" for w in plan.warnings]
            return _Walked(day=day, plan=plan, decisions=mine, ignore_notes=ignored, note_targets=targets)

        return self._walk(now, plan_day, night, max_days=max_days)

    def _write_revision(
        self,
        walked: list[_Walked],
        meta: RevisionMeta,
        additions: Additions,
        previous: list[JournalCompaction] | None,
        edits: list[UserEdit] = (),
    ) -> None:
        """Journal `walked` as `meta`'s revision -- with the user `edits`
        it's the first to include -- and supersede the one it replaces."""
        batch = revision_id(meta.proposal, meta.revision)
        self._journal.start_batch(
            [
                PlannedDay(
                    compaction_id=_day_id(batch, number),
                    now=w.day.now,
                    note_ids=[n.id for n in w.day.notes],
                    decisions=w.decisions,
                    ignore_notes=w.ignore_notes,
                    plan=w.plan,
                    additions=additions.to_json_dict() if number == 1 else {},
                    note_targets=w.note_targets,
                )
                for number, w in enumerate(walked, start=1)
            ],
            meta,
            edits=list(edits),
        )
        for day in previous or ():
            if day.status in (PROPOSED, PLANNED):
                self._journal.set_status(day, SUPERSEDED)

    def _current(self, proposal: str) -> list[JournalCompaction]:
        """Every day of `proposal`'s newest revision."""
        revisions = self._journal.revisions(proposal)
        if not revisions:
            raise CompactionError(f"there's no proposal {proposal!r}", category="unknown_proposal")
        return self._journal.load_batch(revisions[-1][1])

    def _open(self, proposal_id: str) -> list[JournalCompaction]:
        """The open proposal `proposal_id`'s newest revision, refused if it
        isn't the open one."""
        open_id = self._journal.open_proposal()
        if proposal_id != open_id:
            raise CompactionError(
                f"proposal {proposal_id!r} isn't open"
                + (f"; the open one is {open_id}" if open_id else "; there's no open proposal"),
                category="unknown_proposal",
            )
        return self._current(proposal_id)

    def _base(self, proposal: str, meta: RevisionMeta, revision: int | None) -> RevisionMeta:
        """The revision a change to `proposal` started from, refused unless
        it's one of its revisions."""
        if revision is None or not 1 <= revision <= meta.revision:
            raise CompactionError(
                f"say which revision of proposal {proposal} you started from (revision=, 1 to "
                f"{meta.revision}; the current one is {meta.revision})",
                category="stale_revision",
            )
        base = meta if revision == meta.revision else self._journal.revision_meta(proposal, revision)
        if base is None:
            raise CompactionError(
                f"revision {revision} of proposal {proposal} is gone: prepare again", category="stale_revision"
            )
        return base

    def _answers(
        self,
        proposal: str,
        replies: list[FeedbackReply],
        feedback_rows: list[tuple[int, Feedback]],
        edit_rows: list[tuple[int, UserEdit]],
        base: RevisionMeta | None,
    ) -> tuple[list[tuple[int, Feedback]], set[int]]:
        """The feedback `replies` answer (each with its row, answered), and
        the rows of the user edits they replace: those of an event an
        answered item is about, up to the starting revision's `user_seq`.
        Refused if an item open since before the starting revision was
        written goes unanswered, or a reply names one that isn't open."""
        by_id = {f.id: (row, f) for row, f in feedback_rows}
        problems = []
        answered: list[tuple[int, Feedback]] = []
        for reply in replies:
            found = by_id.get(reply.feedback_id)
            if found is None:
                problems.append(f"replies: {reply.feedback_id!r} isn't feedback on proposal {proposal}")
                continue
            row, item = found
            if item.status == "withdrawn":
                continue  # The user took it back meanwhile.
            if item.status != "open":
                problems.append(f"replies: {item.id} is already {item.status}")
                continue
            if not reply.reply.strip():
                problems.append(f"replies: the reply to {item.id} is empty")
                continue
            answered.append((row, replace(item, status="answered", reply=reply.reply.strip())))
        if base is not None:
            done = {item.id for _, item in answered}
            missed = [
                f.id for _, f in feedback_rows
                if f.status == "open" and f.created < base.created and f.id not in done
            ]
            if missed:
                problems.append(
                    f"answer every open feedback item in `replies` -- these aren't: {', '.join(missed)}"
                )
        if problems:
            raise CompactionError.of(problems, "feedback")
        about = {name for _, item in answered for name in (item.event_id, item.note_id) if name}
        replaced = {
            row for row, edit in edit_rows
            if edit.status == "active" and edit.event_id in about and base is not None and edit.seq <= base.user_seq
        }
        return answered, replaced

    def _known_ids(self, now: datetime) -> set[str]:
        """The ids of the events a proposal up to `now` may name: those in
        the stretch `_walk` lists (so this listing answers its own)."""
        sheet_notes, _latest = self._notes.read_with_latest_compacted()
        times = [n.note.timestamp for n in sheet_notes if n.note.timestamp <= now]
        last_stamped = self._journal.last_stamped_now()
        anchor = last_stamped if last_stamped is not None else (None if times else now)
        oldest = min([anchor, *times] if anchor is not None else times)
        return {
            e.id
            for e in self._calendar.list_events(oldest - _PREFETCH_BEFORE, now + _PREFETCH_AFTER)
            if e.id and e.status != "cancelled"
        }

    def _proposal_context(self) -> ProposalContext | None:
        """The open proposal, for `prepare` -- see `ProposalContext`."""
        proposal = self._journal.open_proposal()
        if proposal is None:
            return None
        current = self._current(proposal)
        meta = current[0].meta
        feedback = [f for _, f in self._journal.feedback(proposal)]
        edits = [e for _, e in self._journal.user_edits(proposal)]
        claude_additions = _claude_additions(current)
        shapes: dict[str, list[dict]] = {"updates": [], "creates": [], "cancels": []}
        for decision in meta.claude_decisions:
            kind, shape = decision_shape(decision)
            shapes[kind].append(shape)
        return ProposalContext(
            id=proposal,
            revision=meta.revision,
            state=_state(current, feedback),
            user_seq=meta.user_seq,
            **shapes,
            ignore_notes=_claude_ignore(current),
            annotate_notes=[{"note_id": n, "event_id": e} for n, e in meta.claude_note_targets.items()],
            new_actions=claude_additions.get("actions", []),
            new_people=claude_additions.get("people", []),
            new_locations=claude_additions.get("locations", []),
            settled_additions=list(merge(proposal, [], edits).settled.values()),
            user_edits=edits,
            feedback=feedback,
        )

    def proposal_summary(self) -> ProposalSummary | None:
        """The open proposal, for get_compaction_status."""
        proposal = self._journal.open_proposal()
        if proposal is None:
            return None
        current = self._current(proposal)
        meta = current[0].meta
        feedback = [f for _, f in self._journal.feedback(proposal)]
        return ProposalSummary(
            id=proposal,
            revision=meta.revision,
            state=_state(current, feedback),
            window_start=meta.window_start,
            through=current[-1].now,
            open_feedback=sum(1 for f in feedback if f.status == "open"),
        )

    def get_proposal(self, proposal_id: str | None = None, since_revision: int | None = None) -> Proposal:
        """The current revision of `proposal_id` (by default the open
        proposal), planned again on the calendar as it is now -- see
        `Proposal`."""
        self._prefetch()
        proposal = proposal_id or self._journal.open_proposal()
        if proposal is None:
            raise CompactionError("there's no open proposal", category="unknown_proposal")
        current = self._current(proposal)
        return self._view(current, self._replanned(current), since_revision=since_revision)

    def _replanned(self, current: list[JournalCompaction]) -> list[_Walked] | CompactionError:
        """`current` planned again, for showing -- or why it can't be. An
        applied or abandoned revision isn't: the calendar's moved on."""
        if any(d.status in (STAMPED, ABANDONED, *OPEN_STATUSES) for d in current):
            return CompactionError("it's been applied or abandoned", category="finished")
        try:
            return self._replan(current, strict=False)
        except CompactionError as exc:
            return exc

    def _view(
        self,
        current: list[JournalCompaction],
        walked: list[_Walked] | CompactionError,
        *,
        since_revision: int | None = None,
        replaced: list[str] | None = None,
        edits: list[UserEdit] | None = None,
        feedback: list[Feedback] | None = None,
    ) -> Proposal:
        meta = current[0].meta
        proposal = meta.proposal
        if edits is None:
            edits = [e for _, e in self._journal.user_edits(proposal)]
        if feedback is None:
            feedback = [f for _, f in self._journal.feedback(proposal)]
        state = _state(current, feedback)
        view = Proposal(
            id=proposal,
            revision=meta.revision,
            state=state,
            window_start=meta.window_start,
            through=current[-1].now,
            by=meta.by,
            reason=meta.reason,
            created=meta.created,
            warnings=[w for d in current for w in d.warnings],
            additions=current[0].additions or None,
            user_edits=edits,
            feedback=feedback,
            replaced=replaced,
        )
        if isinstance(walked, CompactionError):
            view.changes = [c for d in current for c in d.changes()]
            if state in ("awaiting_review", "awaiting_claude"):
                view.problem = f"it no longer plans against the calendar: {walked}"
        else:
            included = [e for e in edits if e.seq <= meta.user_seq]
            merged = merge(
                proposal, meta.claude_decisions, included, aliases=meta.aliases,
                claude_ignore=_claude_ignore(current), claude_targets=meta.claude_note_targets,
            )
            view.events = _proposal_events(walked, merged.decided_by)
            view.notes = _proposal_notes(walked, merged)
            view.settled_additions = list(merged.settled.values())
            view.changes = [c for w in walked for c in w.plan.changes]
            view.timeline = join_days([(w.day.day_start, w.plan.timeline) for w in walked])
        if since_revision is not None:
            changed: set[str] = set()
            for number in range(since_revision + 1, meta.revision + 1):
                later = meta if number == meta.revision else self._journal.revision_meta(proposal, number)
                if later is not None:
                    changed |= set(later.changed)
            view.changed_since = sorted(changed)
        return view

    def amend(
        self,
        proposal_id: str,
        revision: int,
        decisions: list[EventDecision],
        as_planned: list[str] | None = None,
        notes: list[NoteEdit] | None = None,
        additions: list[AdditionChoice] | None = None,
    ) -> Proposal:
        """amend_proposal: the user's `decisions`, `notes` (what notes are
        for), `additions` (what's added, settled now) and `as_planned`
        events, notes and refs (whose decisions they clear), laid over the
        open proposal as a new revision -- see utilities/
        compaction_proposals.py. An addition the user creates is created
        right away, once the revision is known to plan."""
        self._prefetch()
        self._journal.garbage_collect()
        current = self._open(proposal_id)
        meta = current[0].meta
        if any(d.status in OPEN_STATUSES for d in current):
            raise CompactionError(
                f"proposal {proposal_id} is being applied; it can't be changed now", category="applying"
            )
        self._base(proposal_id, meta, revision)
        claude_additions = Additions.from_json_dict(_claude_additions(current))
        edit_rows = self._journal.user_edits(proposal_id)
        known_settled = merge(proposal_id, [], [e for _, e in edit_rows]).settled
        decisions = self._checked_facts(decisions, claude_additions, settled=set(known_settled))
        seq = max((e.seq for _, e in edit_rows), default=0)
        now = self._clock()
        new_edits: list[UserEdit] = []
        for decision in decisions:
            seq += 1
            new_edits.append(
                UserEdit(
                    id=edit_id(proposal_id, seq),
                    seq=seq,
                    event_id=decision.event_id if decision.action != "create" else None,
                    edit=edit_json(decision),
                    created=now,
                    base_revision=revision,
                )
            )
        for note in notes or ():
            seq += 1
            new_edits.append(
                UserEdit(
                    id=edit_id(proposal_id, seq), seq=seq, event_id=note.note_id, edit=note.edit(),
                    created=now, base_revision=revision,
                )
            )
        choices = self._addition_choices(claude_additions, additions or [])
        for choice, _item in choices:
            seq += 1
            new_edits.append(
                UserEdit(
                    id=edit_id(proposal_id, seq), seq=seq, event_id=choice.ref,
                    edit={"action": "addition", "use": choice.use, **({"id": choice.id} if choice.id else {})},
                    created=now, base_revision=revision,
                )
            )
        for name in as_planned or ():
            seq += 1
            new_edits.append(
                UserEdit(
                    id=edit_id(proposal_id, seq), seq=seq, event_id=name, edit={"action": "as_planned"},
                    created=now, base_revision=revision,
                )
            )
        if not new_edits:
            raise CompactionError("there's nothing to amend", category="empty")
        through = current[-1].now

        def planned(edits: list[UserEdit]):
            merged = merge(
                proposal_id,
                meta.claude_decisions,
                [e for _, e in edit_rows] + edits,
                known_ids=self._known_ids(through),
                aliases=meta.aliases,
                settled=set(meta.settled),
                claude_ignore=_claude_ignore(current),
                claude_targets=meta.claude_note_targets,
                known_notes={n for d in current for n in d.note_ids},
            )
            unknown_new = [e for e in merged.unknown if e in edits]
            if unknown_new:
                raise CompactionError(
                    "these edits name events or notes that aren't in the proposal: "
                    + ", ".join(repr(e.event_id or e.edit.get("event_id")) for e in unknown_new),
                    category="unknown_event",
                )
            walked, _remaining = self._plan_walk(
                _settled(merged), merged.ignore_notes, _unsettled(claude_additions, merged.settled), through,
                note_targets=merged.note_targets, max_days=len(current),
            )
            return merged, walked

        creating = {e.event_id: e for e in new_edits if e.edit.get("action") == "addition" and e.edit["use"] == "create"}
        if creating:
            # Planned first without them, so nothing's created for a
            # revision that's then refused.
            planned([e for e in new_edits if e.event_id not in creating])
            for choice, item in choices:
                if choice.use == "create":
                    creating[choice.ref].edit["id"] = self._create_addition(item, choice)
        merged, walked = planned(new_edits)
        additions = _unsettled(claude_additions, merged.settled)
        # What a description just written leaves out, it leaves out for
        # good; notes added later go below it.
        kept_out = {note: key for w in walked for note, key in w.plan.kept_out.items()}
        left_out: dict[str, list[str]] = {}
        for edit in new_edits:
            if edit.edit.get("description") is not None and edit.edit.get("action") in ("keep", "create"):
                described = edit.id if edit.edit["action"] == "create" else edit.event_id
                left_out[described] = sorted(n for n, key in kept_out.items() if key == described)
                edit.edit["dropped_notes"] = left_out[described]
        for w in walked:
            # As the revision's decisions are journaled too.
            w.decisions = [
                replace(d, dropped_notes=left_out[d.key or d.event_id])
                if d.description is not None and d.dropped_notes is None and (d.key or d.event_id) in left_out
                else d
                for d in w.decisions
            ]
        outcomes = _outcomes(walked)
        new_meta = replace(
            meta,
            revision=meta.revision + 1,
            window_start=walked[0].day.compaction_window_start,
            created=now,
            base=revision,
            user_seq=seq,
            by="user",
            reason="user edit",
            changed=_changed(meta.outcomes, outcomes),
            outcomes=outcomes,
            claude_additions=claude_additions.to_json_dict(),
        )
        replaced = self._replaced(proposal_id, meta, revision, new_edits)
        self._write_revision(walked, new_meta, additions, current, edits=new_edits)
        unknown = {e.id for e in merged.unknown}
        for row, edit in edit_rows:
            if edit.id in unknown:
                self._journal.set_user_edit_status(row, "inapplicable")
        return self._view(self._current(proposal_id), walked, replaced=replaced)

    def _addition_choices(
        self, additions: Additions, choices: list[AdditionChoice]
    ) -> list[tuple[AdditionChoice, NewAction | NewPerson | NewLocation]]:
        """Each of the user's `choices` with the addition it settles.
        CompactionError if one names a ref the proposal doesn't add, or an
        existing one that isn't there."""
        added: dict[str, tuple[str, NewAction | NewPerson | NewLocation]] = {
            **{a.ref: ("action", a) for a in additions.actions},
            **{p.ref: ("person", p) for p in additions.people},
            **{loc.ref: ("location", loc) for loc in additions.locations},
        }
        there = {
            "action": {a.id for a in self._actions.all()} if self._actions is not None else set(),
            "person": {p.id for p in self._people.all()} if self._people is not None else set(),
            "location": {loc.id for loc in self._locations.all()} if self._locations is not None else set(),
        }
        problems, found = [], []
        for choice in choices:
            if choice.ref not in added:
                problems.append(f"additions: {choice.ref!r} isn't something the proposal adds")
                continue
            kind, item = added[choice.ref]
            if choice.use == "existing" and choice.id not in there[kind]:
                problems.append(f"additions: {choice.ref} -- there's no {kind} {choice.id!r}")
                continue
            if choice.use != "existing" and choice.id is not None:
                problems.append(f"additions: {choice.ref} -- only 'existing' takes an id")
                continue
            found.append((choice, item))
        if problems:
            raise CompactionError.of(problems, "additions")
        return found

    def _create_addition(self, item: NewAction | NewPerson | NewLocation, choice: AdditionChoice) -> str:
        """Create `item` now, as the user corrected it -- or find the one
        by its name already there -- and return its id. A new action is
        active: the user approved it."""
        corrected = {
            name: value
            for name, value in (("name", choice.name), ("context", choice.context), ("hint", choice.hint))
            if value is not None and hasattr(item, name)
        }
        item = replace(item, **corrected)
        if isinstance(item, NewAction):
            item = replace(item, status="active")
        single = Additions(
            actions=[item] if isinstance(item, NewAction) else [],
            people=[item] if isinstance(item, NewPerson) else [],
            locations=[item] if isinstance(item, NewLocation) else [],
        )
        return create_additions(single, self._actions, self._people, self._locations)[item.ref]

    def _replaced(
        self, proposal: str, meta: RevisionMeta, base: int, edits: list[UserEdit]
    ) -> list[str]:
        """The events `edits` name whose decision by Claude changed after
        revision `base`, the one the user was looking at -- as far as the
        journal still says."""
        if base == meta.revision:
            return []
        try:
            then = self._journal.load(revision_id(proposal, base))
        except CompactionError:
            return []
        if not (then.decisions or then.steps or then.meta.claude_decisions):
            return []  # Garbage collection kept only its first row.
        then = then.meta.claude_decisions

        def by_event(decisions: list[EventDecision]) -> dict[str, dict]:
            return {(d.key or d.event_id): d.to_json_dict() for d in decisions}

        before, now = by_event(then), by_event(meta.claude_decisions)
        return sorted(
            {e.event_id for e in edits if e.event_id and before.get(e.event_id) != now.get(e.event_id)}
        )

    def add_note(
        self,
        proposal_id: str,
        text: str,
        event_id: str | None = None,
        at: datetime | None = None,
        note_id: str | None = None,
    ) -> Feedback:
        """add_proposal_note: feedback for Claude on the open proposal --
        about an event, a time or a note, if about one."""
        self._prefetch(facts=False)
        if any(d.status in OPEN_STATUSES for d in self._open(proposal_id)):
            raise CompactionError(f"proposal {proposal_id} is being applied", category="applying")
        if not text.strip():
            raise CompactionError("the note is empty", category="empty")
        seq = max((f.seq for _, f in self._journal.feedback(proposal_id)), default=0) + 1
        item = Feedback(
            id=feedback_id(proposal_id, seq),
            seq=seq,
            text=text.strip(),
            event_id=event_id,
            note_id=note_id,
            at=at,
            by="user",
            created=self._clock(),
        )
        self._journal.add_feedback(item, proposal_id)
        return item

    def _uncompacted_notes(self, now: datetime) -> set[str]:
        """The ids of the notes a proposal up to `now` may name."""
        sheet_notes, _latest = self._notes.read_with_latest_compacted()
        return {n.id for n in sheet_notes if n.note.timestamp <= now}

    def withdraw_note(self, feedback: str) -> Feedback:
        """withdraw_proposal_note: take back an open feedback item."""
        self._prefetch(facts=False)
        parts = split_feedback_id(feedback)
        if parts is None:
            raise CompactionError(f"{feedback!r} isn't a feedback id", category="unknown_feedback")
        proposal, _seq = parts
        found = next(((row, f) for row, f in self._journal.feedback(proposal) if f.id == feedback), None)
        if found is None:
            raise CompactionError(f"there's no feedback {feedback!r}", category="unknown_feedback")
        row, item = found
        if item.status == "withdrawn":
            return item
        if item.status == "answered":
            raise CompactionError(
                f"{feedback} was already answered, in revision {item.answered_in}: see the reply",
                category="answered",
            )
        self._open(proposal)
        item = replace(item, status="withdrawn")
        self._journal.update_feedback(row, item)
        return item

    def confirm(self, proposal_id: str, revision: int) -> ProposalResult:
        """confirm_proposal: apply the open proposal's current revision,
        after checking it still plans the same on the calendar as it is
        now -- see docs/compaction-proposals.md."""
        self._prefetch()
        current = self._open(proposal_id)
        meta = current[0].meta
        if any(d.status in OPEN_STATUSES for d in current):
            raise CompactionError(
                f"proposal {proposal_id} is already being applied: finish it with finish_proposal",
                category="applying",
            )
        if revision != meta.revision:
            raise CompactionError(
                f"revision {revision} isn't proposal {proposal_id}'s current one ({meta.revision}): review "
                "that, and confirm it",
                category="stale_revision",
            )
        feedback = [f for _, f in self._journal.feedback(proposal_id)]
        waiting = [f.id for f in feedback if f.status == "open"]
        if waiting:
            raise CompactionError(
                f"feedback is waiting for Claude ({', '.join(waiting)}): wait for the revision that "
                "answers it, or withdraw it",
                category="feedback_open",
            )
        if current[0].status != PROPOSED:
            raise CompactionError(f"proposal {proposal_id} needs Claude first", category="needs_claude")
        self._require_no_open_compaction()
        self._journal.garbage_collect()
        current = self._current(proposal_id)
        try:
            self._replan(current, strict=True)
        except CompactionError as exc:
            if "stale" not in exc.categories:
                return self._needs_claude(proposal_id, current, f"it no longer plans: {exc}")
            try:
                walked = self._replan(current, strict=False)
            except CompactionError as replanned:
                return self._needs_claude(proposal_id, current, f"it no longer plans: {replanned}")
            outcomes = _outcomes(walked)
            new_meta = replace(
                meta,
                revision=meta.revision + 1,
                window_start=walked[0].day.compaction_window_start,
                created=self._clock(),
                base=meta.revision,
                by="server",
                reason="recheck",
                changed=_changed(meta.outcomes, outcomes),
                outcomes=outcomes,
            )
            self._write_revision(walked, new_meta, Additions.from_json_dict(current[0].additions), current)
            return ProposalResult(
                status="rechecked",
                message=(
                    f"the notes or calendar changed since revision {revision}, so it was planned again as "
                    f"revision {new_meta.revision}: review what changed, and confirm that"
                ),
                proposal=self._view(self._current(proposal_id), walked, since_revision=revision),
            )
        return self._apply(proposal_id, current, feedback)

    def finish(self, proposal_id: str) -> ProposalResult:
        """finish_proposal: resume applying a confirmed proposal that
        stopped partway. It was confirmed, so this needs no new approval."""
        self._prefetch()
        current = self._current(proposal_id)
        statuses = {d.status for d in current}
        if statuses == {STAMPED}:
            return ProposalResult(
                status="applied",
                message=f"proposal {proposal_id} was already applied",
                proposal=self._view(current, CompactionError("applied", category="finished")),
            )
        if not statuses & set(OPEN_STATUSES):
            raise CompactionError(
                f"proposal {proposal_id} hasn't been confirmed, so there's nothing to finish",
                category="not_confirmed",
            )
        return self._apply(proposal_id, current)

    def _apply(
        self, proposal: str, current: list[JournalCompaction], feedback: list[Feedback] | None = None
    ) -> ProposalResult:
        """Apply (or resume applying) `current`, the confirmed revision: a
        write that can never succeed rebuilds what's left as a new
        revision (`_rebuild`); any other failure leaves it to resume. What
        the result shows is read first: after the writes, reading again
        would be another request."""
        edits = [e for _, e in self._journal.user_edits(proposal)]
        if feedback is None:
            feedback = [f for _, f in self._journal.feedback(proposal)]
        try:
            result = self.commit(current[0].batch_id, verify=False)
        except HttpError as exc:
            if exc.resp.status in (404, 410):
                return self._rebuild(proposal, current[0].batch_id, exc)
            raise CompactionError(
                f"applying proposal {proposal} stopped partway ({exc}); what's done stays done -- finish it "
                "with finish_proposal",
                category="apply_stopped",
            ) from exc
        judging = " Claude makes its judgments next (prepare_judgments)." if result.judgments else ""
        for day in current:
            day.status = STAMPED
        return ProposalResult(
            status="applied",
            message=result.message.split(". The compaction isn't complete yet")[0] + "." + judging,
            proposal=self._view(
                current, CompactionError("applied", category="finished"), edits=edits, feedback=feedback
            ),
        )

    def _rebuild(self, proposal: str, batch: str, error: HttpError) -> ProposalResult:
        """After a write that can never succeed: mark the revision's
        unfinished days failed, and propose what's left of it as a new
        revision for the user to confirm -- its decisions replayed on the
        calendar as it is now (see docs/compaction-proposals.md)."""
        days = self._journal.load_batch(batch)
        meta = days[0].meta
        unfinished = [d for d in days if d.status != STAMPED]
        failed = unfinished[0]
        self._record_cancellations(failed, only_done=True)
        aliases = dict(meta.aliases)
        settled = set(meta.settled)
        judge_also = list(meta.judge_also)
        for day in days:
            if day.status == STAMPED:
                ids, _span = _judged_events([day])
                judge_also += [[i, None, None] for i in ids]
        for step in failed.steps:
            if step.status != "done":
                continue
            if step.action == "create" and step.key:
                aliases[step.key] = _new_event_id(failed.id, step.step)
            elif step.action == "cancel" and step.event_id:
                settled.add(step.event_id)
            if step.action != "cancel" and step.after is not None and step.after.facts:
                judge_also.append(
                    [step.event_id if step.action == "update" else _new_event_id(failed.id, step.step), None, None]
                )
        for day in unfinished:
            self._journal.set_status(day, FAILED)
        history = {
            name for day in days if day.status == STAMPED for d in day.decisions
            for name in (d.event_id, d.key) if name
        }
        through = days[-1].now
        known = self._known_ids(through)
        carried: list[EventDecision] = []
        dropped: list[str] = []
        for decision in meta.claude_decisions:
            if decision.action == "create":
                if decision.key not in aliases:
                    carried.append(decision)
            elif decision.event_id in settled or decision.event_id in history:
                continue
            elif decision.event_id not in known:
                dropped.append(decision.event_id)
            else:
                carried.append(decision)
        edit_rows = self._journal.user_edits(proposal)
        merged = merge(
            proposal, carried, [e for _, e in edit_rows], known_ids=known, aliases=aliases,
            settled=settled | history, claude_ignore=_claude_ignore(days),
            claude_targets=meta.claude_note_targets, known_notes=self._uncompacted_notes(through),
        )
        inapplicable = {e.id for e in merged.unknown}
        for row, edit in edit_rows:
            if edit.id in inapplicable:
                self._journal.set_user_edit_status(row, "inapplicable")
        stopped = f"applying revision {meta.revision} stopped: {error}"
        additions = _unsettled(Additions.from_json_dict(_claude_additions(days)), merged.settled)
        try:
            walked, _remaining = self._plan_walk(
                _settled(merged), merged.ignore_notes, additions, through,
                note_targets=merged.note_targets, max_days=len(unfinished),
            )
        except CompactionError as exc:
            return self._needs_claude(proposal, days, f"{stopped}. What's left of it couldn't be planned again: {exc}")
        left_out = sorted(set(dropped) | {e.event_id for e in merged.unknown if e.event_id})
        walked[0].plan.warnings.insert(
            0,
            f"{stopped}. What it wrote before then stays; this is what's left of it"
            + (f" -- leaving out what was about {', '.join(left_out)}, which is gone" if left_out else "")
            + ".",
        )
        outcomes = _outcomes(walked)
        new_meta = replace(
            meta,
            revision=meta.revision + 1,
            window_start=walked[0].day.compaction_window_start,
            created=self._clock(),
            base=meta.revision,
            by="server",
            reason="apply failed",
            changed=_changed(meta.outcomes, outcomes),
            outcomes=outcomes,
            claude_decisions=carried,
            aliases=aliases,
            settled=sorted(settled | history),
            judge_also=judge_also,
        )
        self._write_revision(walked, new_meta, additions, None)
        return ProposalResult(
            status="rebuilt",
            message=f"{stopped}; what's left of it is revision {new_meta.revision}, to review and confirm",
            proposal=self._view(self._current(proposal), walked),
        )

    def _needs_claude(self, proposal: str, current: list[JournalCompaction], why: str) -> ProposalResult:
        """Hand the proposal to Claude: feedback from the server saying
        `why`, which blocks confirming until a revision answers it."""
        rows = self._journal.feedback(proposal)
        seq = max((f.seq for _, f in rows), default=0) + 1
        self._journal.add_feedback(
            Feedback(
                id=feedback_id(proposal, seq),
                seq=seq,
                text=f"Revision {current[0].meta.revision} {why}",
                by="server",
                created=self._clock(),
            ),
            proposal,
        )
        return ProposalResult(
            status="needs_claude",
            message=f"proposal {proposal} {why}. It's waiting for Claude to revise it.",
            proposal=self._view(self._current(proposal), CompactionError(why, category="needs_claude")),
        )

    def commit(self, compaction_id: str, *, verify: bool = True) -> CompactionResult:
        """Apply the batch `compaction_id` a day at a time: each day's steps,
        then its notes stamped, before the next day's. Hands back its final
        timeline (unless it's resumed partway, or not `verify`ing -- see
        `confirm`, which checks it first) and the judgments it calls for,
        to make of the events as that timeline shows them."""
        self._prefetch()
        days = self._journal.load_batch(compaction_id)
        changes = [c for d in days for c in d.changes()]
        if all(d.status == STAMPED for d in days):
            return CompactionResult(
                status="already_compacted",
                compaction_id=compaction_id,
                changes=changes,
                message=f"compaction {compaction_id} was already applied and its notes stamped",
            )
        if any(d.status == ABANDONED for d in days):
            raise CompactionError(f"compaction {compaction_id} was abandoned; run a new dry run", category="abandoned")
        if any(d.status in (SUPERSEDED, FAILED) for d in days):
            raise CompactionError(
                f"compaction {compaction_id} was {'superseded by a newer revision' if days[0].status == SUPERSEDED else 'stopped by a write that failed'}",
                category="superseded",
            )
        pending = [d for d in days if d.status != STAMPED]
        timeline = None
        if pending[0].status in (PLANNED, PROPOSED):
            self._require_no_open_compaction()
            if verify:
                walked = self._replan(pending, strict=True)
                timeline = join_days([(w.day.day_start, w.plan.timeline) for w in walked])
        refs: dict[str, str] | None = None
        for journal in pending:
            if journal.status in (PLANNED, PROPOSED):
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
        warnings = [w for d in days for w in d.warnings]
        # The days as loaded, not read again after all that writing.
        due = self._due(compaction_id, days, redo=False) if self._judging is not None else None
        message = f"applied {steps} change(s) and marked {notes} note(s) compacted"
        if due is not None and due.events:
            message += (
                f". The compaction isn't complete yet: make the {due.count} judgment(s) in `judgments` now, "
                "yourself, of the events as `timeline` shows them, and record them with record_judgments "
                "(see `judgments.instructions`)"
            )
        return CompactionResult(
            status="applied",
            compaction_id=compaction_id,
            changes=changes,
            warnings=warnings,
            message=message,
            judgments=due if due is not None and due.events else None,
            timeline=timeline,
        )

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

    def habit_judgments_due(
        self, habit_id: str, since: datetime | None = None, *, redo: bool = False
    ) -> HabitJudgmentsDue:
        """A backfill of the habit `habit_id`'s judgments (by id or name):
        those its settled events in scope since `since` call for -- by
        default as far back as its judgment parts average over, and the
        days of scores shown -- until where history ends; only those not
        made yet, or with `redo`, all of them, each with the one made
        already."""
        habit, since, until = self._backfill(habit_id, since)
        backfill_id = f"{subject_id(habit.id)}@{since.isoformat()}"
        requests = self._judging.habit_requests(habit, since, until, include_judged=redo)
        due = self._judging.due(backfill_id, requests)
        return HabitJudgmentsDue(
            backfill_id=backfill_id, habit_id=habit.id, habit_name=habit.name or habit.id, since=since,
            until=until, events=due.events, parts=due.parts, history=due.history,
        )

    def _backfill(self, habit_id: str, since: datetime | None) -> tuple[Habit, datetime, datetime]:
        """The habit a backfill is of, from when, and until when: where
        history ends now."""
        if self._judging is None:
            raise CompactionError("this calendar has no traits to judge", category="no_traits")
        try:
            habit = self._judging.habit(habit_id)
        except ValueError as exc:
            raise CompactionError(str(exc), category="unknown_habit") from exc
        until = self._journal.last_stamped_now()
        if until is None:
            raise CompactionError("nothing's been compacted yet, so there's nothing settled to judge", category="not_applied")
        since = since or until - timedelta(days=self._judging.backfill_days(habit))
        if since.tzinfo is None:
            raise CompactionError("give `since` with its UTC offset", category="judgment")
        return habit, since, until

    def _record_backfill(self, backfill_id: str, judgments: list[Judgment]) -> JudgmentsResult:
        """Record `judgments` from the backfill `backfill_id`
        ("habit:<id>@<since>"); whether any are still to make."""
        habit_part, _, since_part = backfill_id.partition("@")
        try:
            since = datetime.fromisoformat(since_part)
        except ValueError:
            raise CompactionError(
                f"{backfill_id!r} isn't a backfill_id (prepare_habit_judgments gives one)", category="judgment"
            ) from None
        habit, since, until = self._backfill(habit_part.removeprefix(SUBJECT_PREFIX), since)
        try:
            recorded = self._judging.record(
                self._judging.habit_requests(habit, since, until, include_judged=True), judgments
            )
        except ValueError as exc:
            raise CompactionError(str(exc), category="judgment") from exc
        remaining = [r.id for r in self._judging.habit_requests(habit, since, until)]
        return JudgmentsResult(
            compaction_id=backfill_id,
            recorded=recorded,
            remaining=remaining,
            complete=not remaining,
            message=(
                f"recorded {recorded} judgment(s); the backfill of {habit.name!r} is complete" if not remaining
                else f"recorded {recorded} judgment(s); {len(remaining)} still to make for {habit.name!r} "
                "(see `remaining`)"
            ),
        )

    def record_judgments(self, compaction_id: str, judgments: list[Judgment]) -> JudgmentsResult:
        """Record `judgments` on the compaction's events; whether that
        completes it. Given a habit backfill's id in place of a
        compaction's, on that habit's events (see
        `habit_judgments_due`)."""
        if compaction_id.startswith(SUBJECT_PREFIX):
            return self._record_backfill(compaction_id, judgments)
        if self._judging is None:
            raise CompactionError("this calendar has no traits to judge", category="no_traits")
        days = self._journal.load_batch(compaction_id)
        try:
            recorded = self._judging.record(self._requests(compaction_id, days, redo=True), judgments)
        except ValueError as exc:
            raise CompactionError(str(exc), category="judgment") from exc
        remaining = [r.id for r in self._requests(compaction_id, days, redo=False)]
        return JudgmentsResult(
            compaction_id=compaction_id,
            recorded=recorded,
            remaining=remaining,
            complete=not remaining,
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

    def _checked_facts(
        self, decisions: list[EventDecision], additions: Additions, *, settled: set[str] = frozenset()
    ) -> list[EventDecision]:
        """`decisions` with their facts normalized; CompactionError if any
        aren't well formed, or name people or locations that aren't there
        (or being added)."""
        people = {p.id for p in self._people.all()} if self._people is not None else None
        locations = {loc.id for loc in self._locations.all()} if self._locations is not None else None
        new_people = {p.ref for p in additions.people} | settled
        new_locations = {loc.ref for loc in additions.locations} | settled
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
        """Abandon a proposal (by its id) -- its current revision -- or a
        compaction from before proposals (by its batch id)."""
        if self._journal.revisions(compaction_id):
            days = self._current(compaction_id)
        else:
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
        """The days from the last compaction (or, before there's been one,
        the oldest uncompacted note's) up to `now`, oldest first, notes or
        not -- at most `max_days` of them -- and how many uncompacted notes
        are left after them. Each day after the first starts where the one
        before it ends, as if that one had been compacted (see the module
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
        # The first day is the one the last compaction ran in -- it's
        # settled up to then -- or, before there's been one, the oldest
        # note's, or today's.
        anchor = last_stamped if last_stamped is not None else (None if notes else now)
        if anchor is not None and anchor >= now:
            return walked, len(sheet_notes)
        oldest = min([anchor, *times.values()] if anchor is not None else times.values())
        # Every day below lists a stretch of this, several times over as
        # its night is decided (see `_cut`): one listing of it all first,
        # so inside a tool call they're each answered from it (see
        # `cached_calendar_listings`).
        self._calendar.list_events(oldest - _PREFETCH_BEFORE, now + _PREFETCH_AFTER)
        history_until = last_stamped
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
            # night now: it starts at the planned wake-up, to settle that.
            planned_end = day.closing.end if day.closing is not None else day.day_end
            overslept = day.day_end > planned_end and planned_end < now
            day.has_next = day.now < now and len(walked) + 1 < max_days
            day.night_before = night_before
            day.earlier_notes = dict(earlier)
            day.history_until = history_until
            walked.append(plan_day(day, len(walked)) if plan_day else _Walked(day=day))
            if not day.has_next:
                break
            if walked[-1].plan is not None:
                calendar.apply(walked[-1].plan.changes)
            last_stamped = day.now
            # After a late wake-up the next day starts at the planned one,
            # so the events the night now runs over are its to settle.
            window_start = min(day.day_end, planned_end)
            history_until = window_start
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
        anchor: datetime | None = None,
    ) -> _Day:
        """The day the oldest of `notes` falls in -- or, with none, the one
        `window_start` does, or with an `anchor` (see `_walk`), the one
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
                f"compaction {batch_id} is {status} -- finish it with finish_proposal, or abandon it "
                "with abandon_compaction, before starting another",
                category="open_compaction",
            )

    def _replan(self, days: list[JournalCompaction], *, strict: bool) -> list[_Walked]:
        """Plan `days` -- a batch's days not yet applied -- again, each with
        its own decisions, as of the batch's `now`. `strict`: check they
        come out as they did when they were previewed (CompactionError,
        "stale", if not)."""
        stale = CompactionError(
            f"the notes or calendar changed since compaction {days[0].batch_id} was previewed; "
            "run a new dry run",
            category="stale",
        )

        def comparable(changes: list[CompactionChange]) -> list[tuple]:
            return [(c.action, c.event_id, c.key, c.before, c.after) for c in changes]

        def night(event_id: str, index: int) -> EventDecision | None:
            if index >= len(days):
                return None
            return next((d for d in days[index].decisions if d.event_id == event_id), None)

        def plan_day(day: _Day, index: int) -> _Walked:
            journal = days[min(index, len(days) - 1)]
            if strict and {n.id for n in day.notes} != set(journal.note_ids):
                raise stale
            plan = self._plan(
                day, journal.decisions, journal.ignore_notes, Additions.from_json_dict(days[0].additions),
                journal.note_targets,
            )
            if strict and comparable(plan.changes) != comparable(journal.changes()):
                raise stale
            return _Walked(
                day=day, plan=plan, decisions=journal.decisions, ignore_notes=journal.ignore_notes,
                note_targets=journal.note_targets,
            )

        walked, _remaining = self._walk(days[-1].now, plan_day, night, max_days=len(days))
        if strict and len(walked) != len(days):
            raise stale
        return walked

    def _plan(
        self,
        day: _Day,
        decisions: list[EventDecision],
        ignore_notes: list[str] | None,
        additions: Additions,
        note_targets: dict[str, str] | None = None,
    ) -> CompactionPlan:
        """`plan_compaction` for `day`, with the decisions' actions checked
        (a ref to a new action in `additions` included), and created
        events' labels checked against the calendar's: Calendar rejects
        inserting an event with a label it doesn't have (HTTP 400), which
        would stop the commit partway -- and every retry with it. A
        created event that has one anyway just drops it; the label it gets
        follows its actions when it's written. Only reads the labels when a
        created event has one."""
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
            history_until=day.history_until,
            note_targets=note_targets,
        )
        self._check_before_window(day, plan)
        self._show_follow_through(day, decisions, plan)
        if tree is not None:
            _keep_priorities(plan, tree, day)
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
        cancelled = {d.event_id for d in decisions if d.action == "cancel" and d.counts_against_follow_through is not False}
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

    def _record_cancellations(self, journal: JournalCompaction, *, only_done: bool = False) -> None:
        """Record each event `journal`'s day cancelled with a 'cancel'
        decision that counts -- not one that doesn't, nor a merge -- for
        the people it counts against in follow-through, as it was planned.
        Again, harmlessly, on a resumed commit. `only_done`: just those
        already cancelled, for an apply that failed partway."""
        if self._cancellations is None:
            return
        # A cancel from before cancels said whether they counted, counted.
        cancelled = {
            d.event_id for d in journal.decisions
            if d.action == "cancel" and d.counts_against_follow_through is not False
        }
        for step in journal.steps:
            if only_done and step.status != "done":
                continue
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
                "finish it with finish_proposal, or abandon it with abandon_compaction, first",
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
    # What an apply of an earlier revision gave facts before it failed.
    meta = days[0].meta
    for event_id, _start, _end in meta.judge_also if meta is not None else ():
        if event_id not in ids:
            ids.append(event_id)
    if not spans:
        return ids, (days[0].now, days[0].now)
    return ids, (min(s for s, _ in spans), max(e for _, e in spans))


def _keyed(
    proposal: str, decisions: list[EventDecision], meta: RevisionMeta | None
) -> tuple[list[EventDecision], int]:
    """`decisions` with a key on every create -- one it kept, or a new
    one -- and the highest key number given. CompactionError for a key
    that isn't one of `proposal`'s (or is given twice)."""
    key_seq = meta.key_seq if meta is not None else 0
    problems, seen, keyed = [], set(), []
    for decision in decisions:
        if decision.action != "create":
            keyed.append(replace(decision, key=None))
            continue
        if decision.key is not None:
            number = claude_key_number(proposal, decision.key)
            if number is None or number > key_seq or decision.key in seen:
                problems.append(
                    f"create {decision.summary!r}: {decision.key!r} isn't one of proposal {proposal}'s keys "
                    "(keep only those prepare_compaction gave; leave it out for something new)"
                )
                continue
            seen.add(decision.key)
            keyed.append(decision)
            continue
        key_seq += 1
        keyed.append(replace(decision, key=claude_key(proposal, key_seq)))
    if problems:
        raise CompactionError.of(problems, "unknown_key")
    return keyed, key_seq


def _note_targets(annotations: list[NoteAnnotation]) -> dict[str, str]:
    """`annotate_notes` as note -> event; CompactionError for a note given
    twice."""
    targets: dict[str, str] = {}
    twice = []
    for annotation in annotations:
        if annotation.note_id in targets:
            twice.append(annotation.note_id)
        targets[annotation.note_id] = annotation.event_id
    if twice:
        raise CompactionError(
            f"annotate_notes: each note goes with one event -- {', '.join(twice)} are given more than once",
            category="malformed_decision",
        )
    return targets


def _claude_additions(days: list[JournalCompaction]) -> dict[str, list[dict]]:
    """What Claude's decisions in a revision add, as JSON -- for one from
    before they were kept apart from what's left to create, its first
    day's `additions`."""
    meta = days[0].meta
    if meta is not None and meta.claude_additions is not None:
        return meta.claude_additions
    return days[0].additions or {}


def _unsettled(additions: Additions, settled: dict) -> Additions:
    """`additions` but those the user settled."""
    return Additions(
        actions=[a for a in additions.actions if a.ref not in settled],
        people=[p for p in additions.people if p.ref not in settled],
        locations=[loc for loc in additions.locations if loc.ref not in settled],
    )


def _settled(merged) -> list[EventDecision]:
    """`merged`'s decisions naming what the user settled of what's added
    by its id -- or leaving it out."""
    return [settle_refs(d, merged.settled) for d in merged.decisions]


def _claude_ignore(days: list[JournalCompaction]) -> list[str]:
    """The notes Claude ignored in a revision -- for one from before they
    were kept apart from the user's, every day's."""
    meta = days[0].meta
    if meta is not None and meta.claude_ignore_notes is not None:
        return list(meta.claude_ignore_notes)
    return sorted({i for d in days for i in d.ignore_notes})


def _proposal_notes(walked: list[_Walked], merged) -> list[ProposalNote]:
    """Each of `walked`'s notes, with what its plan does with it and who
    said so -- see `ProposalNote`."""
    notes = []
    for w in walked:
        for note in w.day.notes:
            use, event = w.plan.note_uses.get(note.id, ("unused", None))
            edge = w.plan.note_edges.get(note.id)
            decided_by = merged.notes_decided_by.get(note.id)
            if decided_by is None and use == "edge":
                decided_by = merged.decided_by.get(event)
            notes.append(
                ProposalNote(
                    id=note.id,
                    timestamp=note.note.timestamp,
                    description=note.note.description,
                    use=use,
                    event_id=event if event is None or not event.startswith("new:") else None,
                    edge_of=edge if edge is None or not edge.startswith("new:") else None,
                    decided_by=decided_by,
                )
            )
    return notes


def _has_anything(walked: list[_Walked]) -> bool:
    """Whether there's anything in `walked`'s days to confirm: a note, or
    an event that happened after the last compaction."""
    return any(w.day.notes for w in walked) or any(
        e.start < w.day.now and e.end > w.day.compaction_window_start for w in walked for e in w.day.events
    )


def _outcomes(walked: list[_Walked]) -> dict[str, str]:
    """Each event a plan changes (id, or a create's key) -> a digest of
    how -- see `RevisionMeta.outcomes`."""
    outcomes: dict[str, str] = {}
    for w in walked:
        for change in w.plan.changes:
            name = change.event_id or change.key
            if name is None:
                continue
            digest = json.dumps([change.action, change.after.to_json_dict() if change.after else None], sort_keys=True)
            outcomes[name] = hashlib.sha1(digest.encode("utf-8")).hexdigest()[:10]
    return outcomes


def _changed(before: dict[str, str] | None, after: dict[str, str]) -> list[str]:
    if before is None:
        return []
    return sorted(name for name in set(before) | set(after) if before.get(name) != after.get(name))


def _state(days: list[JournalCompaction], feedback: list[Feedback]) -> ProposalState:
    statuses = {d.status for d in days}
    if statuses == {STAMPED}:
        return "applied"
    if statuses & set(OPEN_STATUSES):
        return "applying"
    if ABANDONED in statuses:
        return "abandoned"
    if FAILED in statuses or any(f.status == "open" for f in feedback):
        return "awaiting_claude"
    return "awaiting_review"


def _proposal_events(walked: list[_Walked], decided_by: dict[str, str]) -> list:
    """Every event of `walked`'s days as their plans leave them -- see
    `ProposalEvent`. An event shown by an earlier day (a night, say) keeps
    that day's view unless a later day changes it."""
    out: dict[str, ProposalEvent] = {}
    for w in walked:
        day = w.day
        changes = {c.event_id: c for c in w.plan.changes if c.event_id}
        merged = {d.event_id for d in w.decisions if d.action == "merge"}
        cancels = {d.event_id: d for d in w.decisions if d.action == "cancel"}
        # Who each cancel counts against: the plan's timeline has it.
        against = {t.event_id: t.follow_through for t in w.plan.timeline.events if t.event_id} if w.plan.timeline else {}
        for event in day.events:
            if not event.id or event.id.startswith("planned"):
                continue
            change = changes.get(event.id)
            if event.id in out and change is None:
                continue
            state = change.after if change is not None and change.action == "update" else EventState.from_event(event)
            if change is not None and change.action == "cancel":
                status = "merged" if event.id in merged else "cancelled"
            elif (state.start, state.end) != (event.start, event.end):
                status = "adjusted"
            elif event.start < day.now:
                status = "on_schedule"
            else:
                status = "planned"
            counts = None
            if status == "cancelled":
                # A cancel from before cancels said whether they counted, counts.
                counts = event.id not in cancels or cancels[event.id].counts_against_follow_through is not False
            out[event.id] = ProposalEvent(
                counts_against_follow_through=counts,
                follow_through=list(against.get(event.id, [])) if counts else [],
                id=event.id,
                summary=state.summary,
                start=state.start,
                end=state.end,
                status=status,
                planned_start=event.start,
                planned_end=event.end,
                description=state.description,
                location=state.location,
                priority=state.priority,
                action_ids=state.action_ids,
                facts=facts_from_dict(state.facts) if state.facts is not None else None,
                decided_by=decided_by.get(event.id),
                history_until=(
                    min(event.end, day.history_until)
                    if day.history_until is not None and event.start < day.history_until
                    else None
                ),
                is_end_of_day_sleep=bool(event.is_end_of_day_sleep),
            )
        for change in w.plan.changes:
            if change.action != "create" or change.after is None:
                continue
            name = change.key or f"new{len(out) + 1}"
            out[name] = ProposalEvent(
                id=name,
                summary=change.after.summary,
                start=change.after.start,
                end=change.after.end,
                status="new",
                description=change.after.description,
                location=change.after.location,
                priority=change.after.priority,
                action_ids=change.after.action_ids,
                facts=facts_from_dict(change.after.facts) if change.after.facts is not None else None,
                decided_by=decided_by.get(change.key) if change.key else None,
            )
    return sorted(out.values(), key=lambda e: (e.start or e.planned_start, e.id))


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


def _keep_priorities(plan: CompactionPlan, tree: ActionTree, day: _Day) -> None:
    """Give each event `plan` settles -- each that started before `day`'s
    `now`, changed or not -- the priority its actions give it now (the
    highest, lowest-numbered, of theirs) as its own, unless it has one:
    actions' priorities change often, and what happened keeps the priority
    it had (see Event.action_priority). An event whose actions give none
    is left without."""

    def kept(state: EventState) -> int | None:
        if state.start is None or state.start >= day.now or state.priority is not None:
            return None
        return min((p for p in (tree.priority(a) for a in state.action_ids or ()) if p is not None), default=None)

    changed = set()
    for change in plan.changes:
        changed.add(change.event_id)
        if change.action != "cancel" and change.after is not None and (priority := kept(change.after)) is not None:
            change.after.priority = priority
    for event in day.events:
        if not event.id or event.id in changed or event.status == "cancelled":
            continue
        before = EventState.from_event(event)
        if (priority := kept(before)) is not None:
            plan.changes.append(
                CompactionChange(
                    action="update",
                    event_id=event.id,
                    reason="kept the priority its actions give it, as its own",
                    before=before,
                    after=replace(before, priority=priority),
                )
            )


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
        "summary", "start", "end", "description", "location", "priority", "event_label_id", "action_ids",
    ):
        value = getattr(after, name)
        if value is not None and value != getattr(before, name):
            setattr(patch, name, value)
    if after.facts != before.facts:
        # Empty facts remove them: see Event.facts.
        patch.facts = facts_from_dict(after.facts)
    return patch

