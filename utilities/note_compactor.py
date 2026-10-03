"""Compacting notes: the application-level policy that ties the notes tab
(utilities/noted_time_sheet.py), the calendar, the planner
(utilities/note_compaction.py) and the write-ahead journal
(utilities/compaction_journal.py) together.

The flow, as the MCP tools expose it:

1. `prepare` -- read-only. Hands the client one day of uncompacted notes
   (each with a stable id and a shortlist of nearby planned events), that
   day's planned events, and the two side by side as a `Timeline` (see
   utilities/compaction_timeline.py). The client compares them and
   decides, event by event, what the notes show happened differently.
2. `dry_run` -- validates the client's decisions and writes the plan to
   the journal as `planned`, returning it (and a compaction id, and the
   resulting timeline) for review. Nothing on the calendar changes.
3. `commit` -- applies a `planned` compaction after checking the notes and
   calendar still match what was previewed, journaling each step as it
   goes, and only then stamps the notes as compacted. If it dies partway
   it can simply be called again: the journal remembers exactly what was
   approved and how far it got. A compaction that can't be finished can be
   `abandon`ed.

A day at a time: notes are processed one day per compaction, oldest
first, so a backlog spanning several days takes one compaction per day.
The day is the one the oldest uncompacted note falls in: it starts when
the last end-of-day sleep event that began before that note ends (or at
the note, if it's earlier -- a note written before the planned wake-up
time), and runs to the end of the next end-of-day sleep event after that
(or 24 hours, if there isn't one).

A day can take several compactions, so the events offered -- the
*compaction window* -- start at the later of the day's start and the last
*stamped* compaction's `now`: whatever an earlier compaction already
settled isn't offered again. The one event that ended within `_LOOKBACK`
before the compaction window starts is offered too, so an event the last
compaction closed off at "now" (or the night's sleep) can still be
stretched. Likewise the latest compacted note, if it's within `_LOOKBACK`
before the compaction window starts, is offered as `previous_note`: what
the user said just before the window often says what was going on as it
began. Every past event offered is recorded as on schedule unless
the client's decisions say otherwise (see utilities/note_compaction.py).

`prepare` also garbage-collects the journal (`CompactionJournal.
garbage_collect`) before doing anything else -- see there, and
`NotedTimeSheet.garbage_collect` (called from `note`/`append` instead,
since that's where the notes tab grows), for what that means for row
numbers and ids.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from googleapiclient.errors import HttpError

from calendar_clients.google_calendar import Event
from utilities.compaction_journal import (
    ABANDONED,
    APPLIED,
    APPLYING,
    PLANNED,
    STAMPED,
    CompactionJournal,
    JournalCompaction,
    JournalStep,
)
from utilities.compaction_timeline import Timeline
from utilities.note_compaction import (
    CompactionChange,
    CompactionError,
    CompactionPlan,
    EventDecision,
    PlanNote,
    plan_compaction,
    planned_timeline,
)
from utilities.noted_time_sheet import NotedTime, NotedTimeSheet, SheetNote
from utilities.goals import Goals, GoalTree
from utilities.reallocating_calendar import ReallocatingCalendar

_CANDIDATE_WINDOW = timedelta(hours=1)
"""How far either side of a note's time a planned event may start or end
and still be offered to it as a candidate."""

_HINT_HISTORY = timedelta(days=28)
"""How far back goals are looked for to suggest for an event."""

_LOOKBACK = timedelta(minutes=15)
"""How long before the compaction window starts an event may have ended,
or the latest compacted note been written, and still be offered (only the
latest one) -- see the module docstring."""

_APPROVAL_RULE = (
    "Then STOP and wait for the user's reply. Only call compact_notes with dry_run=False once the "
    "user has explicitly approved this plan after seeing it -- never in the same turn as the dry "
    "run. A request to compact made before they saw the plan (\"compact my notes\") isn't approval "
    "of it."
)
"""Repeated wherever a model is told what to do after a dry run: the
server can't tell whether the user replied, so this rests on the model."""

DECISION_GUIDE = (
    "`timeline` shows this day's notes beside its planned events. Compare them and decide, event "
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
    "`notes` (only this round's -- notes for later days aren't offered yet, see "
    "`remaining_note_count`) and event ids from `events`. "
    "`events` may start with one that ended just before `compaction_window_start` (usually last "
    "night's sleep); if a note shows it actually ran later -- the user slept in -- move its end "
    "with 'keep', and say when whatever it now overlaps happened. "
    "`previous_note`, if there is one, is the last note an earlier compaction already used, "
    "written within 15 minutes before `compaction_window_start` -- context only (it can't be "
    "used as a start_note/end_note or ignored): e.g. if it said 'starting the report', the report "
    "was already under way as this window began, and the first note may well be its end. "
    "A 'keep' that moves a future event reschedules it: it's pinned there and the rest of the day "
    "reflows around it, in the same plan -- for 'move lunch later and adjust the afternoon' "
    "requests. The day's own end-of-day sleep event works differently: moving its start moves "
    "bedtime (an earlier one shortens or cancels what runs past it), and its end -- the wake-up "
    "time -- starts the next day, which compaction never adjusts, so move only its start to "
    "change only bedtime. "
    "GOALS: each event can serve goals (`goals` lists them; `goal_ids` on an event, primary "
    "first). Tagging events with goals should cost the user almost nothing, so: for every past "
    "event with `suggested_goal_ids`, add a 'keep' {event_id, goal_ids} applying them -- without "
    "asking, unless the notes clearly say otherwise; give a 'create' goal_ids when the notes "
    "clearly imply them; otherwise leave goals alone ('keep' without goal_ids keeps them, and [] "
    "clears them). Ask about a goal only when genuinely torn between two. In the timeline, ◆ marks "
    "a goal an event already serves and ◇ one it's being given (or, before deciding, one "
    "suggested); a correction from the user is just a new dry run. "
    "After every dry run, show the user the result's `timeline` as two parallel lanes -- notes on "
    "the left, events on the right, aligned by time, with each anchoring note joined to the event "
    "edge it sets -- drawing it as a visual if you can render one, otherwise showing "
    "`timeline.text` verbatim in a code block, and list the warnings, asking whether to apply it "
    "or what to change. " + _APPROVAL_RULE
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


@dataclass(kw_only=True)
class ContextGoal:
    id: str
    path: str


@dataclass(kw_only=True)
class CompactionContext:
    """What a client needs to interpret one day of notes -- see
    `NoteCompactor.prepare`."""

    notes: list[ContextNote]
    events: list[ContextEvent]
    now: datetime | None = None
    """The effective 'now' for this day: the actual now, or the end of
    the day if that's earlier."""

    day_end: datetime | None = None
    remaining_note_count: int = 0
    """Uncompacted notes belonging to later days -- compact those in
    later rounds."""

    open_compaction: str | None = None
    """The id of a compaction that's begun but not finished, if any. It
    must be resumed or abandoned before a new one can start."""

    compaction_window_start: datetime | None = None
    """Where the events offered start: the later of the day's start and
    the last compaction -- see the module docstring."""

    timeline: Timeline | None = None
    """`notes` beside `events` as planned -- see
    utilities/compaction_timeline.py."""

    goals: list[ContextGoal] | None = None
    """The goals an event can be given: every active one."""

    previous_note: PreviousNote | None = None
    """The latest already-compacted note, if it was written within
    `_LOOKBACK` before `compaction_window_start` -- see the module
    docstring."""

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
    remaining: int
    previous: Event | None = None
    """The event that ended just before the compaction window, if one
    was offered."""

    latest_compacted: NotedTime | None = None
    """The compacted note with the latest timestamp, read with `notes`."""


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
    ) -> None:
        """`calendar` reads the day's events (through the same goal-aware
        view reallocation uses); `client` is what the planned changes are
        written through (a GoalCalendar, so each event's label follows
        its goals). `goals` names, suggests and checks events' goals."""
        self._calendar = calendar
        self._client = client
        self._goals = goals
        self._notes = notes
        self._journal = journal
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def prepare(self) -> CompactionContext:
        self._journal.garbage_collect()
        now = self._clock()
        open_ids = self._journal.open_compactions()
        open_id = open_ids[0][0] if open_ids else None
        day = self._day(now)
        if day is None:
            return CompactionContext(notes=[], events=[], open_compaction=open_id)
        first = min(n.note.timestamp for n in day.notes) if day.notes else None
        tree = self._goals.tree() if self._goals else None
        names = _goal_names(tree)
        suggested = self._suggestions(day, tree) if tree is not None else {}
        previous_note = self._previous_note(day)
        return CompactionContext(
            notes=[
                ContextNote(
                    id=n.id,
                    timestamp=n.note.timestamp,
                    description=n.note.description,
                    candidates=_candidates(
                        n.note.timestamp, day.events, day.previous if n.note.timestamp == first else None
                    ),
                )
                for n in day.notes
            ],
            events=[
                ContextEvent(
                    id=e.id,
                    summary=e.summary,
                    start=e.start,
                    end=e.end,
                    description=e.description,
                    goal_ids=e.goal_ids,
                    goal_names=[names.get(g, g) for g in e.goal_ids] if e.goal_ids is not None else None,
                    suggested_goal_ids=suggested.get(e.id),
                    priority=e.effective_priority,
                    is_fixed_time=e.effective_is_fixed_time,
                )
                for e in day.events
                if e.id
            ],
            now=day.now,
            day_end=day.day_end,
            remaining_note_count=day.remaining,
            open_compaction=open_id,
            compaction_window_start=day.compaction_window_start,
            timeline=planned_timeline(
                _plan_notes(day),
                day.events,
                day.now,
                names,
                suggested,
                previous_note=PlanNote(
                    id="previous_note", timestamp=previous_note.timestamp, description=previous_note.description
                ) if previous_note is not None else None,
            ),
            goals=[
                ContextGoal(id=g.id, path=tree.path(g.id)) for g in tree.ordered() if g.active
            ] if tree is not None else None,
            previous_note=PreviousNote(
                timestamp=previous_note.timestamp, description=previous_note.description
            ) if previous_note is not None else None,
        )

    def _previous_note(self, day: _Day) -> NotedTime | None:
        """The latest compacted note, if it was written within _LOOKBACK
        before the compaction window -- see the module docstring."""
        latest = day.latest_compacted
        start = day.compaction_window_start
        if latest is not None and start - _LOOKBACK <= latest.timestamp <= start:
            return latest
        return None

    def _suggestions(self, day: _Day, tree: GoalTree) -> dict[str, list[str]]:
        """Goals to suggest for the day's past events that serve none: those
        the latest earlier event with the same title served, within
        _HINT_HISTORY (ignoring goals since deleted). Recurring events need
        nothing extra: an instance's goals come with its series."""
        bare = [e for e in day.events if e.id and not e.goal_ids and e.end <= day.now and e.summary]
        if not bare:
            return {}
        start = day.compaction_window_start
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
        self, decisions: list[EventDecision], ignore_notes: list[str] | None = None
    ) -> CompactionResult:
        self._require_no_open_compaction()
        day = self._day(self._clock())
        if day is None:
            return CompactionResult(status="nothing_to_compact", message="there are no uncompacted notes")
        plan = self._plan(day, decisions, ignore_notes)
        superseded = self._supersede_planned()
        compaction_id = uuid.uuid4().hex[:12]
        self._journal.start(
            compaction_id,
            now=day.now,
            note_ids=[n.id for n in day.notes],
            decisions=decisions,
            ignore_notes=ignore_notes or [],
            plan=plan,
        )
        return CompactionResult(
            status="planned",
            compaction_id=compaction_id,
            changes=plan.changes,
            warnings=plan.warnings,
            timeline=plan.timeline,
            message=(
                f"{len(plan.changes)} calendar change(s) planned for {len(day.notes)} note(s); "
                "nothing has been changed yet. Show the user `timeline` as two lanes (see the "
                "instructions from prepare_compaction) and the warnings. "
                + _APPROVAL_RULE
                + f" Once they approve, apply it with compaction_id={compaction_id!r} and "
                "dry_run=False. If they want something different, correct the decisions and call "
                "compact_notes again (this plan is then replaced)."
                + (f" (Replaced {superseded} earlier unapplied plan(s).)" if superseded else "")
            ),
        )

    def describe(self, compaction_id: str) -> CompactionResult:
        """The stored plan for `compaction_id`, without doing anything."""
        journal = self._journal.load(compaction_id)
        return CompactionResult(
            status=_STATUS_FOR_JOURNAL.get(journal.status, "planned"),
            compaction_id=compaction_id,
            changes=journal.changes(),
            warnings=journal.warnings,
            message=f"compaction {compaction_id} is {journal.status}",
        )

    def commit(self, compaction_id: str) -> CompactionResult:
        journal = self._journal.load(compaction_id)
        if journal.status == STAMPED:
            return CompactionResult(
                status="already_compacted",
                compaction_id=compaction_id,
                changes=journal.changes(),
                message=f"compaction {compaction_id} was already applied and its notes stamped",
            )
        if journal.status == ABANDONED:
            raise CompactionError(f"compaction {compaction_id} was abandoned; run a new dry run")
        if journal.status == PLANNED:
            self._require_no_open_compaction()
            self._verify_unchanged(journal)
            self._journal.set_status(journal, APPLYING)
        for step in journal.steps:
            if step.status != "done":
                self._apply_step(journal, step)
                self._journal.mark_step_done(step)
        self._journal.set_status(journal, APPLIED)
        self._notes.mark_compacted(journal.note_ids, journal.id)
        self._journal.set_status(journal, STAMPED)
        return CompactionResult(
            status="applied",
            compaction_id=compaction_id,
            changes=journal.changes(),
            warnings=journal.warnings,
            message=f"applied {len(journal.steps)} change(s) and marked {len(journal.note_ids)} note(s) compacted",
        )

    def edit_note(
        self, note_id: str, *, timestamp: datetime | None = None, description: str | None = None
    ) -> SheetNote:
        """See the module-level `edit_note`."""
        return edit_note(self._notes, self._journal, note_id, timestamp=timestamp, description=description)

    def delete_note(self, note_id: str) -> NotedTime:
        """See the module-level `delete_note`."""
        return delete_note(self._notes, self._journal, note_id)

    def abandon(self, compaction_id: str) -> CompactionResult:
        journal = self._journal.load(compaction_id)
        if journal.status == STAMPED:
            raise CompactionError(f"compaction {compaction_id} is already complete; there's nothing to abandon")
        self._journal.set_status(journal, ABANDONED)
        return CompactionResult(
            status="abandoned",
            compaction_id=compaction_id,
            message=(
                f"compaction {compaction_id} abandoned. Any steps it had already applied stay applied "
                "(the journal has each one's before-state); its notes are still uncompacted."
            ),
        )

    def _day(self, now: datetime) -> _Day | None:
        sheet_notes, latest_compacted = self._notes.read_with_latest_compacted()
        if not sheet_notes:
            return None
        first = min(n.note.timestamp for n in sheet_notes)
        # The day the oldest note falls in starts when the night before it
        # ends -- or at the note, if it was written before the wake-up time.
        recent = self._calendar.list_events(first - timedelta(hours=24), first + timedelta(seconds=1))
        opening = max(
            (e for e in recent if e.is_end_of_day_sleep and e.status != "cancelled" and e.start <= first),
            key=lambda e: e.start,
            default=None,
        )
        day_start = min(opening.end, first) if opening is not None else first
        last_stamped = self._journal.last_stamped_now()
        compaction_window_start = (
            max(day_start, last_stamped) if last_stamped is not None else day_start
        )

        fetched = sorted(
            (
                e
                for e in self._calendar.list_events(
                    compaction_window_start - _LOOKBACK, day_start + timedelta(hours=24)
                )
                if e.status != "cancelled"
            ),
            key=lambda e: e.start,
        )
        earlier = [e for e in fetched if e.end <= compaction_window_start]
        events = [e for e in fetched if e.end > compaction_window_start]
        closing = next(
            (i for i, e in enumerate(events) if e.is_end_of_day_sleep and e.start > day_start), None
        )
        if closing is not None:
            events = events[: closing + 1]
        previous = max(earlier, key=lambda e: e.end) if earlier else None
        if previous is not None:
            events.insert(0, previous)
        day_end = events[-1].end if closing is not None else day_start + timedelta(hours=24)
        in_day = [n for n in sheet_notes if n.note.timestamp <= day_end]
        return _Day(
            notes=in_day,
            events=events,
            day_start=day_start,
            compaction_window_start=compaction_window_start,
            day_end=day_end,
            now=min(now, day_end),
            remaining=len(sheet_notes) - len(in_day),
            previous=previous,
            latest_compacted=latest_compacted,
        )

    def _supersede_planned(self) -> int:
        """Abandon every earlier plan that was never applied. A new dry run
        replaces them -- it's how a rejected or corrected plan is redone --
        and leaving them `planned` would let a stale one be committed by
        mistake. Returns how many were replaced."""
        replaced = self._journal.compactions_with_status(PLANNED)
        for compaction_id, _status in replaced:
            self._journal.set_status(self._journal.load(compaction_id), ABANDONED)
        return len(replaced)

    def _require_no_open_compaction(self) -> None:
        open_compactions = self._journal.open_compactions()
        if open_compactions:
            compaction_id, status = open_compactions[0]
            raise CompactionError(
                f"compaction {compaction_id} is {status} -- finish it with compact_notes("
                f"compaction_id={compaction_id!r}, dry_run=False), or abandon it with "
                "abandon_compaction, before starting another"
            )

    def _verify_unchanged(self, journal: JournalCompaction) -> None:
        day = self._day(journal.now)
        stale = CompactionError(
            f"the notes or calendar changed since compaction {journal.id} was previewed; "
            "run a new dry run"
        )
        if day is None or {n.id for n in day.notes} != set(journal.note_ids):
            raise stale
        plan = self._plan(day, journal.decisions, journal.ignore_notes)

        def comparable(changes: list[CompactionChange]) -> list[tuple]:
            return [(c.action, c.event_id, c.before, c.after) for c in changes]

        if comparable(plan.changes) != comparable(journal.changes()):
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
        plan = plan_compaction(
            _plan_notes(day),
            decisions,
            day.events,
            day.now,
            ignore_notes=ignore_notes,
            day_start=day.day_start,
            goal_names=_goal_names(tree),
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
        if note_id in journal.load(compaction_id).note_ids:
            raise CompactionError(
                f"note {note_id!r} is part of compaction {compaction_id}, which is {status} -- "
                f"finish it with compact_notes(compaction_id={compaction_id!r}, dry_run=False), or "
                "abandon it with abandon_compaction, first"
            )


def _plan_notes(day: _Day) -> list[PlanNote]:
    return [
        PlanNote(id=n.id, timestamp=n.note.timestamp, description=n.note.description)
        for n in day.notes
    ]


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
    if after.is_fixed_time:
        # An actual event is pinned explicitly, not left to inherit it
        # from its label.
        patch.is_fixed_time = True
    if after.min_duration_minutes is not None and (
        after.is_fixed_time or after.min_duration_minutes != before.min_duration_minutes
    ):
        patch.min_duration = timedelta(minutes=after.min_duration_minutes)
    return patch


_STATUS_FOR_JOURNAL = {STAMPED: "already_compacted", ABANDONED: "abandoned"}
"""How a stored compaction's journal status reads as a result status;
anything else (planned, or begun but unfinished) is shown as `planned` --
the `message` carries its exact state."""
