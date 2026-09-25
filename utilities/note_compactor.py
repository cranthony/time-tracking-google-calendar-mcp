"""Compacting notes: the application-level policy that ties the notes tab
(utilities/noted_time_sheet.py), the calendar, the planner
(utilities/note_compaction.py) and the write-ahead journal
(utilities/compaction_journal.py) together.

The flow, as the MCP tools expose it:

1. `prepare` -- read-only. Hands the client one *reallocation day* of
   uncompacted notes (each with a stable id and a shortlist of nearby
   planned events) plus that day's planned events. The client reads the
   free-form notes and decides what each one means.
2. `dry_run` -- validates the client's dispositions and writes the plan
   to the journal as `planned`, returning it (and a compaction id) for
   review. Nothing on the calendar changes.
3. `commit` -- applies a `planned` compaction after checking the notes and
   calendar still match what was previewed, journaling each step as it
   goes, and only then stamps the notes as compacted. If it dies partway
   it can simply be called again: the journal remembers exactly what was
   approved and how far it got. A compaction that can't be finished can be
   `abandon`ed.

A day at a time: notes are processed one reallocation day (see
`ReallocatingCalendar.list_day_events`) per compaction, oldest first, so a
backlog spanning several days takes one compaction per day. Each day's
window starts where the last *stamped* compaction's `now` left off (the
first note's timestamp, only if nothing has ever been stamped), so a
calendar event that already ended before the next note is written --
this morning's getting-ready block, an earlier work block -- is still in
range instead of silently falling outside the fetch window.

`dry_run` also takes `reschedules` -- direct "move this planned event"
instructions alongside the note dispositions, for redirecting the plan
itself ("move lunch later and adjust the afternoon accordingly") rather
than interpreting what happened. See `utilities/note_compaction.py`'s
`Reschedule`; it's journaled and applied in the same plan as the notes.
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
from utilities.note_compaction import (
    CompactionChange,
    CompactionError,
    NeedsClarification,
    NoteDisposition,
    PlanNote,
    Reschedule,
    plan_compaction,
)
from utilities.noted_time_sheet import NotedTimeSheet, SheetNote
from utilities.reallocating_calendar import ReallocatingCalendar

_CANDIDATE_WINDOW = timedelta(hours=1)
"""How far either side of a note's time a planned event may start or end
and still be offered to it as a candidate."""

DISPOSITION_GUIDE = (
    "Give every note in `notes` above (by its id) one disposition, and only those notes -- "
    "not ones from get_notes or an earlier prepare_compaction call, even if they're still "
    "uncompacted. Notes past this round belong to a later day and aren't offered yet (see "
    "`remaining_note_count`); compact_notes rejects a disposition for any other note id. "
    "Each disposition is made of effects. "
    "'starts' {event_id}: a planned event began at that time. "
    "'starts_unplanned' {summary}: something that wasn't planned began. "
    "'ends' {event_id} (or {started_by_note} for an unplanned activity): it ended at that time. "
    "'marker': just an annotation of what was happening. 'ignore': no meaning. "
    "'ambiguous' {question}: you can't tell -- ask the user. "
    "A note often ends one activity and starts the next: give it both effects. "
    "Only use event ids from `events`; each note's `candidates` are the likeliest. "
    "'marker', 'ignore' and 'ambiguous' can't be combined with other effects. "
    "'starts'/'starts_unplanned'/'ends' may also carry {rename} (override the resulting event's "
    "title) and/or {annotate} (extra text for its description) -- typically set these after showing "
    "the user a dry run and hearing what they want changed, then call compact_notes again. "
    "Separately, `reschedules` (a compact_notes argument, not a disposition) directly moves a "
    "planned event to a new {start} and/or {end} (either may be left out -- not both -- and is "
    "filled in from the other plus the event's current duration, so giving just {start} moves it "
    "without changing its length) -- for 'move lunch later and adjust the afternoon accordingly' "
    "style requests that aren't about what a note means. It reflows the rest of the day around it "
    "exactly like a note-derived activity, in the same plan; an event already accounted for by a "
    "note can't also be rescheduled."
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
class ContextEvent:
    id: str
    summary: str | None
    start: datetime
    end: datetime
    description: str | None = None
    event_label_id: str | None = None
    priority: int | None = None
    is_fixed_time: bool | None = None


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

    instructions: str = DISPOSITION_GUIDE


@dataclass(kw_only=True)
class CompactionResult:
    status: Literal[
        "needs_clarification",
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
    questions: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.changes = self.changes or []
        self.warnings = self.warnings or []
        self.questions = self.questions or []


@dataclass
class _Day:
    notes: list[SheetNote]
    events: list[Event]
    day_end: datetime
    now: datetime
    remaining: int


class NoteCompactor:
    def __init__(
        self,
        *,
        calendar: ReallocatingCalendar,
        client,
        notes: NotedTimeSheet,
        journal: CompactionJournal,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """`calendar` reads a day's events (it's the same label-priority-
        aware view reallocation uses); `client` is what the planned
        changes are written through."""
        self._calendar = calendar
        self._client = client
        self._notes = notes
        self._journal = journal
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def prepare(self) -> CompactionContext:
        now = self._clock()
        open_ids = self._journal.open_compactions()
        open_id = open_ids[0][0] if open_ids else None
        day = self._day(now)
        if day is None:
            return CompactionContext(notes=[], events=[], open_compaction=open_id)
        return CompactionContext(
            notes=[
                ContextNote(
                    id=n.id,
                    timestamp=n.note.timestamp,
                    description=n.note.description,
                    candidates=_candidates(n.note.timestamp, day.events),
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
                    event_label_id=e.event_label_id,
                    priority=e.priority,
                    is_fixed_time=e.is_fixed_time,
                )
                for e in day.events
                if e.id
            ],
            now=day.now,
            day_end=day.day_end,
            remaining_note_count=day.remaining,
            open_compaction=open_id,
        )

    def dry_run(
        self, dispositions: list[NoteDisposition], reschedules: list[Reschedule] | None = None
    ) -> CompactionResult:
        self._require_no_open_compaction()
        day = self._day(self._clock())
        if day is None:
            return CompactionResult(status="nothing_to_compact", message="there are no uncompacted notes")
        try:
            plan = plan_compaction(
                _plan_notes(day), dispositions, day.events, day.now, reschedules=reschedules
            )
        except NeedsClarification as exc:
            return CompactionResult(
                status="needs_clarification",
                message="ask the user these, then call again with the corrected dispositions",
                questions=exc.questions,
            )
        superseded = self._supersede_planned()
        compaction_id = uuid.uuid4().hex[:12]
        self._journal.start(
            compaction_id,
            now=day.now,
            note_ids=[n.id for n in day.notes],
            dispositions=dispositions,
            plan=plan,
            reschedules=reschedules,
        )
        return CompactionResult(
            status="planned",
            compaction_id=compaction_id,
            changes=plan.changes,
            warnings=plan.warnings,
            message=(
                f"{len(plan.changes)} calendar change(s) planned for {len(day.notes)} note(s); "
                "nothing has been changed yet. Show this to the user, then call compact_notes "
                f"with compaction_id={compaction_id!r} and dry_run=False to apply it. If they want "
                "something different, correct the dispositions and call compact_notes again "
                "(this plan is then replaced)."
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
        sheet_notes = self._notes.read_with_rows()
        if not sheet_notes:
            return None
        anchor = self._journal.last_stamped_now() or min(n.note.timestamp for n in sheet_notes)
        events = [e for e in self._calendar.list_day_events(anchor) if e.status != "cancelled"]
        sleep = next((e for e in events if e.is_end_of_day_sleep), None)
        day_end = sleep.end if sleep is not None else anchor + timedelta(hours=24)
        in_day = [n for n in sheet_notes if n.note.timestamp <= day_end]
        return _Day(
            notes=in_day,
            events=events,
            day_end=day_end,
            now=min(now, day_end),
            remaining=len(sheet_notes) - len(in_day),
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
        plan = plan_compaction(
            _plan_notes(day), journal.dispositions, day.events, day.now, reschedules=journal.reschedules
        )

        def comparable(changes: list[CompactionChange]) -> list[tuple]:
            return [(c.action, c.event_id, c.before, c.after) for c in changes]

        if comparable(plan.changes) != comparable(journal.changes()):
            raise stale

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


def _plan_notes(day: _Day) -> list[PlanNote]:
    return [
        PlanNote(id=n.id, timestamp=n.note.timestamp, description=n.note.description)
        for n in day.notes
    ]


def _candidates(timestamp: datetime, events: list[Event]) -> list[str]:
    near = [
        e
        for e in events
        if e.id and e.end > timestamp - _CANDIDATE_WINDOW and e.start < timestamp + _CANDIDATE_WINDOW
    ]
    near.sort(key=lambda e: abs(e.start - timestamp))
    return [e.id for e in near]


def _new_event_id(compaction_id: str, step: int) -> str:
    """A deterministic id for a created event (Calendar accepts a-v and
    0-9, 5-1024 characters), so retrying a create can't duplicate it."""
    return f"cmp{compaction_id}s{step:03d}"


def _patch_for(step: JournalStep) -> Event:
    if step.action == "cancel":
        return Event(id=step.event_id, status="cancelled")
    before, after = step.before, step.after
    patch = Event(id=step.event_id)
    for name in ("summary", "start", "end", "description", "location", "priority", "event_label_id"):
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
