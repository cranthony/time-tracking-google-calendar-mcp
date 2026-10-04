"""Compaction planning: realigning a day's planned events to its free-form
time notes, and turning that into calendar changes.

Notes are sparse: you jot one down when something notable happens, not at
every event boundary. So the planner starts from the plan and treats a
note as evidence that changes it, never as the only evidence of what
happened. **Silence means on schedule**: a past event no decision mentions
is recorded exactly as planned. Nothing is merged, cancelled or stretched
to fill a gap unless a decision says so.

Reading free-form text is *not* this module's job -- the MCP client (a
model) does that, compares the notes to the plan (see
utilities/compaction_timeline.py for the timeline it's given), and
hands back one `EventDecision` per event the notes show happened
differently. This module is the deterministic half: a pure function,
`plan_compaction`, with no API access, that validates those decisions and
works out exactly which events to update, create, and cancel -- and the
resulting `Timeline` to show the user -- so everything risky is
testable and the plan is previewable before anything is written.

## Decisions (`EventDecision`)

- `keep` (a planned event's id): it happened. Each edge stays where it was
  planned unless the decision moves it: `start_note`/`end_note` (a note
  id) sets that edge to the note's time and links the note to it;
  `start`/`end` sets an explicit time (alongside a note, when the note
  gives a time relative to itself -- "leaving 15 minutes early"). Also
  how an event is renamed (`summary`) or given extra description text
  (`annotate`) without moving it.
- `cancel` (an event id): it didn't happen.
- `create` (a `summary`, both edges, optionally `goal_ids`): something
  that happened that wasn't planned.

`keep` and `create` take `goal_ids` (primary first) to set the goals an
event serves; for `keep`, leaving it out keeps them, and `[]` clears
them.
- `merge` (an event id, `into` another): fold one event into another, for
  when the user doesn't remember where one ended and the next began. The
  target grows to cover both and is titled after both (unless renamed);
  the merged one is cancelled. Never implied -- a decision has to ask for
  it.

## What happens to the rest of the calendar

- The past is treated as certain. Every resulting past event -- decided,
  or untouched and so on schedule -- becomes a *fact*, pinned
  (`is_fixed_time`) so no later reallocation moves it. Facts may not
  overlap: if moving one edge runs into another event, the plan is
  rejected naming the overlap, and the decisions have to say which edge
  gives way. Planned end-of-day sleep events are facts too (so a note
  can't silently eat into one), just never pinned.
- A `keep` that moves an event in the future is a direct reschedule: it's
  pinned where it's put, the same as a past fact.
- Everything else -- the future -- reflows around the facts using
  `utilities/reallocation.py`, simulated in memory here.
- Every note that doesn't set an edge, and isn't listed in
  `ignore_notes`, has its text added to the description of the event it
  falls within, so it survives compaction.

Moving the day's own end-of-day sleep event is the exception: it moves
where the day ends instead of being placed within it, since nothing in
the day comes after it to reflow into. Its end starts the next day, which
is never adjusted -- only warned about, unless the next day is being
compacted with it (`next_day_follows`). See `_end_day_at`. Cancelling it
-- a night without sleep -- makes the day run on to the next one's end.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal

from calendar_clients.google_calendar import MAX_DESCRIPTION_BYTES, Event
from utilities.compaction_timeline import Timeline, TimelineEvent, TimelineNote, build_timeline
from utilities.reallocation import FixedTimeConflict, ReallocationOptions, reallocate_for_new_event

DecisionAction = Literal["keep", "cancel", "create", "merge"]

_DATETIME_FIELDS = ("start", "end")


@dataclass(kw_only=True)
class EventDecision:
    """What actually happened to one event -- see the module docstring.
    Which of the optional fields are meaningful depends on `action`."""

    action: DecisionAction
    event_id: str | None = None
    """`keep`/`cancel`/`merge`: the id of one of the day's events."""

    summary: str | None = None
    """`create`: the new event's title. `keep`: a new title for it."""

    start: datetime | None = None
    end: datetime | None = None
    """`keep`/`create`: an explicit time for that edge."""

    start_note: str | None = None
    end_note: str | None = None
    """`keep`/`create`: the id of the note that marks that edge. Sets the
    edge to the note's time, unless `start`/`end` gives one explicitly."""

    into: str | None = None
    """`merge`: the id of the event to merge this one into."""

    annotate: str | None = None
    """`keep`/`create`: extra text for the event's description."""

    goal_ids: list[str] | None = None
    """`keep`/`create`: the goals it serves, primary first. For `keep`,
    `None` keeps its goals and `[]` clears them."""

    def to_json_dict(self) -> dict:
        return {
            key: (value.isoformat() if isinstance(value, datetime) else value)
            for key, value in self.__dict__.items()
            if value is not None
        }

    @classmethod
    def from_json_dict(cls, data: dict) -> "EventDecision":
        parsed = dict(data)
        parsed.pop("event_label_id", None)  # From before goals.
        for key in _DATETIME_FIELDS:
            if key in parsed:
                parsed[key] = datetime.fromisoformat(parsed[key])
        return cls(**parsed)


@dataclass(kw_only=True)
class PlanNote:
    """A note, as the planner sees it."""

    id: str
    timestamp: datetime
    description: str | None = None


@dataclass(kw_only=True)
class EventState:
    """An event's state as recorded in a plan (and in the journal):
    the fields compaction reads or changes."""

    summary: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    description: str | None = None
    location: str | None = None
    status: str | None = None
    min_duration_minutes: int | None = None
    is_fixed_time: bool | None = None
    priority: int | None = None
    event_label_id: str | None = None
    goal_ids: list[str] | None = None

    @classmethod
    def from_event(cls, event: Event) -> "EventState":
        return cls(
            summary=event.summary,
            start=event.start,
            end=event.end,
            description=event.description,
            location=event.location,
            status=event.status,
            min_duration_minutes=(
                int(event.min_duration.total_seconds() // 60) if event.min_duration else None
            ),
            is_fixed_time=event.is_fixed_time,
            priority=event.priority,
            event_label_id=event.event_label_id,
            goal_ids=list(event.goal_ids) if event.goal_ids is not None else None,
        )

    def to_event(self, event_id: str | None = None) -> Event:
        return Event(
            id=event_id,
            summary=self.summary,
            start=self.start,
            end=self.end,
            description=self.description,
            location=self.location,
            status=self.status,
            min_duration=(
                timedelta(minutes=self.min_duration_minutes)
                if self.min_duration_minutes is not None
                else None
            ),
            is_fixed_time=self.is_fixed_time,
            priority=self.priority,
            event_label_id=self.event_label_id,
            goal_ids=list(self.goal_ids) if self.goal_ids is not None else None,
        )

    def to_json_dict(self) -> dict:
        return {
            key: (value.isoformat() if isinstance(value, datetime) else value)
            for key, value in self.__dict__.items()
            if value is not None
        }

    @classmethod
    def from_json_dict(cls, data: dict) -> "EventState":
        parsed = dict(data)
        for key in ("start", "end"):
            if key in parsed:
                parsed[key] = datetime.fromisoformat(parsed[key])
        return cls(**parsed)


@dataclass(kw_only=True)
class CompactionChange:
    """One calendar change a plan makes."""

    action: Literal["update", "create", "cancel"]
    reason: str
    event_id: str | None = None
    """`None` for a `create` (the event doesn't exist yet)."""

    before: EventState | None = None
    after: EventState | None = None


@dataclass(kw_only=True)
class CompactionPlan:
    changes: list[CompactionChange]
    warnings: list[str] = field(default_factory=list)
    timeline: Timeline | None = None


class CompactionError(ValueError):
    """The decisions (or the calendar they were checked against) can't be
    turned into a plan. The message says what to fix, and lists the valid
    choices where there are any -- it's written to be read by the model
    that produced the decisions."""


@dataclass
class _Fact:
    """An event whose resulting time is certain: it's placed exactly
    there, and everything else reflows around it."""

    key: str
    start: datetime
    end: datetime
    event: Event
    base: Event | None
    reason: str
    start_note: PlanNote | None = None
    end_note: PlanNote | None = None
    default: bool = False
    """Not mentioned by any decision: a past event recorded as planned.
    Stays where it is in the working day (pinned) instead of being placed
    like a decided fact."""

    moves_day_end: bool = False
    """A move of the day's own end-of-day sleep event -- see `_end_day_at`."""

    annotations: list[str] = field(default_factory=list)


def plan_compaction(
    notes: list[PlanNote],
    decisions: list[EventDecision],
    day_events: list[Event],
    now: datetime,
    *,
    ignore_notes: list[str] | None = None,
    day_start: datetime | None = None,
    options: ReallocationOptions | None = None,
    goal_names: dict[str, str] | None = None,
    previous_note: PlanNote | None = None,
    last_compaction: datetime | None = None,
    next_day_follows: bool = False,
) -> CompactionPlan:
    """Plan the calendar changes that `decisions` (what the notes show
    happened, event by event) imply for `day_events`, as of `now`.

    `day_start` is where this day begins: the day's own end-of-day sleep
    event (the one moving which moves bedtime) is the first that starts
    after it, and any sleep event before it is just the end of the
    previous night, adjustable like anything else. Without it, the first
    sleep event is the day's own. One a decision cancels isn't: a night
    without sleep, the day runs on to the next one. `next_day_follows`:
    the next day is being compacted too, after this one, so moving this
    day's wake-up time needs no warning -- that day starts from it.
    `goal_names` (goal id -> name) labels
    events' goals in the timeline. `previous_note` (an already-compacted
    note) and `last_compaction` (when that compaction ran) are only shown
    in the timeline, as context.

    Raises `CompactionError` (listing everything wrong at once) if the
    decisions are invalid or leave past events overlapping. Never mutates
    its arguments."""
    options = options or ReallocationOptions()
    ignored = set(ignore_notes or [])
    problems: list[str] = []
    warnings: list[str] = []

    ordered = [n for _, n in sorted(enumerate(notes), key=lambda p: (p[1].timestamp, p[0]))]
    notes_by_id = {n.id: n for n in notes}
    for note in ordered:
        if note.timestamp > now:
            problems.append(f"note {note.id} is timestamped after now ({now.isoformat()})")
    for note_id in sorted(ignored - set(notes_by_id)):
        problems.append(f"ignore_notes: {note_id!r} isn't one of this round's notes; {_valid_notes(notes)}")

    live = sorted(
        (e for e in day_events if e.id and e.status != "cancelled"), key=lambda e: e.start
    )
    events_by_id = {e.id: e for e in live}
    copies = {e.id: replace(e) for e in live}
    sleeps = [e for e in live if e.is_end_of_day_sleep]
    sleepless = {d.event_id for d in decisions if d.action == "cancel"}
    closing = next(
        (e for e in sleeps if (day_start is None or e.start > day_start) and e.id not in sleepless), None
    )

    resolved = _resolve(decisions, notes, notes_by_id, events_by_id, closing, now, problems)
    if problems:
        raise CompactionError("\n".join(problems))
    facts, cancels, merged_into, touched = resolved

    decided_ids = {f.base.id for f in facts if f.base} | set(cancels) | set(touched)
    for event in live:
        if event.id in decided_ids or event.end > now:
            continue
        copy = copies[event.id]
        if not event.is_end_of_day_sleep:
            copy.is_fixed_time = True
            copy.min_duration = copy.end - copy.start
        facts.append(
            _Fact(
                key=event.id,
                start=copy.start,
                end=copy.end,
                event=copy,
                base=event,
                reason="happened as planned -- no note says otherwise -- so pinned in place",
                default=True,
            )
        )
    facts.sort(key=lambda f: (f.start, f.end))
    _check_overlaps(facts, problems)
    if problems:
        raise CompactionError("\n".join(problems))

    for event_id, decision in touched.items():
        if decision.summary:
            copies[event_id].summary = decision.summary
        if decision.goal_ids is not None:
            copies[event_id].goal_ids = list(decision.goal_ids)
    annotated = _annotate(ordered, ignored, facts, touched, copies, cancels, warnings, problems)
    if problems:
        raise CompactionError("\n".join(problems))

    simulated = _simulate(facts, copies, cancels, options, warnings, next_day_follows)
    changes = _changes(facts, events_by_id, copies, cancels, simulated)
    if not changes:
        warnings.append("nothing on the calendar needs to change")
    timeline = _timeline(
        ordered, ignored, facts, live, copies, cancels, merged_into, annotated, simulated, now, goal_names or {}
    )
    timeline = _with_context(timeline, previous_note, last_compaction)
    return CompactionPlan(changes=changes, warnings=warnings, timeline=timeline)


def _valid_notes(notes: list[PlanNote]) -> str:
    return "valid note ids: " + (", ".join(n.id for n in notes) or "(none)")


def _resolve(
    decisions: list[EventDecision],
    notes: list[PlanNote],
    notes_by_id: dict[str, PlanNote],
    events_by_id: dict[str, Event],
    closing: Event | None,
    now: datetime,
    problems: list[str],
) -> tuple[list[_Fact], dict[str, str], dict[str, str], dict[str, EventDecision]]:
    """Validate `decisions` and turn them into facts (events whose time is
    now certain), cancellations (id -> reason), merges (merged id -> the
    id it was merged into), and `touched` events -- kept, in the future
    and not moved, so just renamed or annotated in place."""
    valid_events = "valid event ids: " + (", ".join(sorted(events_by_id)) or "(none)")

    def edge(label: str, explicit: datetime | None, note_id: str | None, fallback: datetime | None):
        note = None
        if note_id is not None:
            note = notes_by_id.get(note_id)
            if note is None:
                problems.append(f"{label}: {note_id!r} isn't one of this round's notes; {_valid_notes(notes)}")
        if explicit is not None:
            return explicit, note
        return (note.timestamp if note is not None else fallback), note

    by_event: dict[str, EventDecision] = {}
    creates: list[tuple[str, EventDecision]] = []
    for number, decision in enumerate(decisions, start=1):
        label = f"decision {number} ({decision.action}{' ' + decision.event_id if decision.event_id else ''})"
        moves = any(
            v is not None for v in (decision.start, decision.end, decision.start_note, decision.end_note)
        )
        if decision.action == "create":
            if decision.event_id is not None or decision.into is not None:
                problems.append(f"{label}: 'create' takes a summary and times, not event_id or into")
            elif not (decision.summary or "").strip():
                problems.append(f"{label}: 'create' needs a summary")
            else:
                creates.append((label, decision))
            continue
        if decision.action not in ("keep", "cancel", "merge"):
            problems.append(f"{label}: unknown action {decision.action!r}; use keep, cancel, create or merge")
            continue
        if decision.event_id not in events_by_id:
            problems.append(f"{label}: {decision.event_id!r} isn't one of this day's events; {valid_events}")
            continue
        if decision.event_id in by_event:
            problems.append(f"{label}: event {decision.event_id} has more than one decision")
            continue
        if decision.action != "keep" and (moves or decision.summary or decision.annotate):
            problems.append(
                f"{label}: times, notes, summary and annotate only go with 'keep' or 'create'"
            )
            continue
        if decision.action == "merge":
            if decision.into not in events_by_id or decision.into == decision.event_id:
                problems.append(f"{label}: 'into' must be the id of another of this day's events; {valid_events}")
                continue
            if closing is not None and closing.id in (decision.event_id, decision.into):
                problems.append(f"{label}: the end-of-day sleep event can't be merged")
                continue
        elif decision.into is not None:
            problems.append(f"{label}: 'into' only goes with 'merge'")
            continue
        by_event[decision.event_id] = decision

    merges: dict[str, list[str]] = {}
    for event_id, decision in by_event.items():
        if decision.action != "merge":
            continue
        target = by_event.get(decision.into)
        if target is not None and target.action != "keep":
            problems.append(
                f"can't merge {event_id} into {decision.into}: that event is itself being "
                f"{'cancelled' if target.action == 'cancel' else 'merged'}"
            )
            continue
        merges.setdefault(decision.into, []).append(event_id)

    facts: list[_Fact] = []
    cancels: dict[str, str] = {}
    merged_into: dict[str, str] = {}
    touched: dict[str, EventDecision] = {}
    for event_id, decision in by_event.items():
        if decision.action == "cancel":
            cancels[event_id] = "cancelled -- it didn't happen"
        elif decision.action == "merge" and decision.into in merges:
            merged_into[event_id] = decision.into
            cancels[event_id] = f"merged into {events_by_id[decision.into].summary!r}"

    for event_id in sorted(set(merges) | {i for i, d in by_event.items() if d.action == "keep"}):
        base = events_by_id[event_id]
        decision = by_event.get(event_id) or EventDecision(action="keep", event_id=event_id)
        label = f"keep {event_id}"
        start, start_note = edge(label, decision.start, decision.start_note, base.start)
        end, end_note = edge(label, decision.end, decision.end_note, base.end)
        members = [base] + [events_by_id[i] for i in merges.get(event_id, [])]
        for member in members[1:]:
            start, end = min(start, member.start), max(end, member.end)
        if end <= start:
            problems.append(
                f"{label}: {base.summary!r} would run from {start.isoformat()} to {end.isoformat()}, "
                "which isn't a positive length"
            )
            continue
        changed = (start, end) != (base.start, base.end)
        if not changed and end > now and event_id not in merges:
            touched[event_id] = decision
            continue
        event = replace(base)
        names: list[str] = []
        for member in members:
            if member.summary and member.summary not in names:
                names.append(member.summary)
        event.summary = (decision.summary or "").strip() or " and ".join(names) or base.summary
        if decision.goal_ids is not None:
            event.goal_ids = list(decision.goal_ids)
        event.start, event.end = start, end
        event.is_fixed_time = True
        event.min_duration = end - start
        if event_id in merges:
            reason = (
                f"merged with {', '.join(repr(m.summary) for m in members[1:])} -- where one ended "
                "and the next began isn't remembered -- and pinned in place"
            )
        elif not changed:
            reason = "happened as planned, and pinned in place"
        elif start >= now:
            reason = "moved as requested, and pinned in place"
        else:
            reason = "realigned to match the notes, and pinned in place"
        facts.append(
            _Fact(
                key=event_id,
                start=start,
                end=end,
                event=event,
                base=base,
                reason=reason,
                start_note=start_note,
                end_note=end_note,
                moves_day_end=closing is not None and event_id == closing.id,
                annotations=[decision.annotate.strip()] if (decision.annotate or "").strip() else [],
            )
        )

    for number, (label, decision) in enumerate(creates, start=1):
        start, start_note = edge(label, decision.start, decision.start_note, None)
        end, end_note = edge(label, decision.end, decision.end_note, None)
        if start is None or end is None:
            problems.append(
                f"{label}: 'create' needs both a start and an end (a time or a note each; "
                "for something still going on, end it at now)"
            )
            continue
        if end <= start:
            problems.append(
                f"{label}: {decision.summary!r} would run from {start.isoformat()} to "
                f"{end.isoformat()}, which isn't a positive length"
            )
            continue
        facts.append(
            _Fact(
                key=f"new:{number}",
                start=start,
                end=end,
                event=Event(
                    summary=decision.summary.strip(),
                    start=start,
                    end=end,
                    goal_ids=list(decision.goal_ids) if decision.goal_ids is not None else None,
                    is_fixed_time=True,
                    min_duration=end - start,
                ),
                base=None,
                reason="something the notes show happened that wasn't planned",
                start_note=start_note,
                end_note=end_note,
                annotations=[decision.annotate.strip()] if (decision.annotate or "").strip() else [],
            )
        )
    return facts, cancels, merged_into, touched


def _check_overlaps(facts: list[_Fact], problems: list[str]) -> None:
    """Past events (and directly moved ones) are certain, so two of them
    can't share time -- and which one gives way is for the decisions to
    say, not for the planner to guess."""
    for i, earlier in enumerate(facts):
        for later in facts[i + 1 :]:
            if later.start >= earlier.end:
                continue
            problems.append(
                f"{_describe(earlier)} overlaps {_describe(later)}. Decide which gives way -- move "
                "one of their edges with 'keep', cancel one, or (only if the user confirms they "
                "don't remember where one ended and the other began) merge them -- and ask the "
                "user if the notes don't say."
            )


def _describe(fact: _Fact) -> str:
    how = "on schedule" if fact.default else "as decided"
    return (
        f"{fact.event.summary!r} ({fact.key}, {how}, {fact.start.isoformat()} to "
        f"{fact.end.isoformat()})"
    )


def _annotate(
    ordered: list[PlanNote],
    ignored: set[str],
    facts: list[_Fact],
    touched: dict[str, EventDecision],
    copies: dict[str, Event],
    cancels: dict[str, str],
    warnings: list[str],
    problems: list[str],
) -> dict[str, str]:
    """Add every note that doesn't set an edge (and isn't ignored), and
    every `annotate`, to the description of the event it belongs to.
    Returns note id -> the title of the event it was added to.

    A description that would grow past `MAX_DESCRIPTION_BYTES` is a
    problem, not a warning: Calendar would silently cut it short."""
    anchors = {n.id for f in facts for n in (f.start_note, f.end_note) if n is not None}
    fact_ids = {f.base.id for f in facts if f.base}
    # Where each candidate event ends up, as far as is known before the
    # reflow: facts exactly, everything else where it is now.
    targets: list[tuple[datetime, datetime, str, Event]] = [
        (f.start, f.end, f.key, f.event) for f in facts
    ] + [
        (e.start, e.end, event_id, e)
        for event_id, e in copies.items()
        if event_id not in fact_ids and event_id not in cancels
    ]
    lines: dict[str, list[tuple[datetime | None, str]]] = {}
    events: dict[str, Event] = {}
    note_ids: dict[str, list[str]] = {}
    annotated: dict[str, str] = {}
    for note in ordered:
        text = (note.description or "").strip()
        if note.id in anchors or note.id in ignored or not text:
            continue
        hit = next((t for t in targets if t[0] <= note.timestamp < t[1]), None) or next(
            (t for t in targets if t[0] < note.timestamp <= t[1]), None
        )
        if hit is None:
            warnings.append(
                f"note {note.id} ({text!r}) doesn't fall within any event, so its text wasn't "
                "added anywhere"
            )
            continue
        lines.setdefault(hit[2], []).append((note.timestamp, text))
        events[hit[2]] = hit[3]
        note_ids.setdefault(hit[2], []).append(note.id)
        annotated[note.id] = hit[3].summary or hit[2]
    for fact in facts:
        for text in fact.annotations:
            lines.setdefault(fact.key, []).append((None, text))
            events[fact.key] = fact.event
    for event_id, decision in touched.items():
        if (decision.annotate or "").strip():
            lines.setdefault(event_id, []).append((None, decision.annotate.strip()))
            events[event_id] = copies[event_id]

    for key, entries in lines.items():
        event = events[key]
        timed = sorted((e for e in entries if e[0] is not None), key=lambda e: e[0])
        formatted = [
            f"- {moment.astimezone(event.start.tzinfo).strftime('%H:%M')} {text}" for moment, text in timed
        ] + [f"- {text}" for moment, text in entries if moment is None]
        prefix = f"{event.description}\n\nNotes:\n" if event.description else "Notes:\n"
        event.description = prefix + "\n".join(formatted)
        size = len(event.description.encode("utf-8"))
        if size > MAX_DESCRIPTION_BYTES:
            # Untimed entries are `annotate` text; timed ones are notes.
            fixes = ["shorten its `annotate`"] if any(moment is None for moment, _ in entries) else []
            if note_ids.get(key):
                fixes.insert(0, f"leave notes out with ignore_notes ({', '.join(note_ids[key])})")
            problems.append(
                f"the description of {event.summary or key!r} would be {size} bytes with these notes "
                f"added, over the {MAX_DESCRIPTION_BYTES} Calendar keeps (it silently cuts the rest)"
                + (f"; {' or '.join(fixes)}" if fixes else "")
            )
    return annotated


def _end_day_at(
    fact: _Fact,
    working: list[Event],
    explicit_cancel: dict[str, str],
    day_end_reasons: dict[str, str],
    warnings: list[str],
    next_day_follows: bool = False,
) -> list[Event]:
    """Move the day's end to where `fact` puts its end-of-day sleep event,
    and return `working` with it there.

    This isn't placed like any other fact (`reallocate_for_new_event`),
    because that needs something after the placed event for the rest to
    reflow into, and nothing in the day comes after its own sleep event
    -- the day is cut off there (see `ReallocatingCalendar.
    list_day_events`). It would also, given the chance, carry the rest of
    an interrupted evening event over to after the sleep, into the next
    day. So instead:

    - a later bedtime just leaves the evening it frees up free;
    - an earlier one shortens whatever runs past the new bedtime to end
      there, and cancels what starts after it (or can't be shortened
      that far without going under its `min_duration`); a fixed-time
      event in the way is an error, the same as two facts overlapping;
    - the sleep's end -- the start of the next day -- is moved as asked,
      but nothing in the next day is adjusted for it: compacting one day
      never changes the next. That gets a warning instead, since the
      next day's first events may now overlap it -- unless the next day
      is being compacted too (`next_day_follows`), starting from it.
    """
    sleep = fact.event
    bedtime = fact.start
    problems: list[str] = []
    kept: list[Event] = []
    for event in working:
        if event.id == fact.base.id:
            continue
        if event.end <= bedtime:
            kept.append(event)
            continue
        if event.is_fixed_time:
            problems.append(
                f"{sleep.summary!r} doesn't fit starting at {bedtime.isoformat()}: fixed-time "
                f"{event.summary!r} ({event.start.isoformat()} to {event.end.isoformat()}) is in "
                "the way and can't move. Start it later."
            )
            continue
        if event.start < bedtime and bedtime - event.start >= (event.min_duration or timedelta(0)):
            event.end = bedtime
            if event.id is not None:
                day_end_reasons[event.id] = f"shortened to end at the new bedtime, {bedtime.isoformat()}"
            kept.append(event)
        elif event.id is not None:
            explicit_cancel[event.id] = f"doesn't fit before the new bedtime, {bedtime.isoformat()}"
        # else: the unsaved remainder of a split event -- just never created.
    if problems:
        raise CompactionError("\n".join(problems))
    if fact.end != fact.base.end and not next_day_follows:
        warnings.append(
            f"{sleep.summary!r} now ends at {fact.end.isoformat()} instead of "
            f"{fact.base.end.isoformat()}. Compaction doesn't adjust the next day for that -- "
            "check the next day's first events, and move them with update_event if they now overlap."
        )
    return sorted(kept + [sleep], key=lambda e: e.start)


_SHORTEN_OR_MOVE = "Give it an earlier end or a later start with 'keep', or check the notes that bound it."


@dataclass
class _Simulated:
    working: list[Event]
    """The day as it ends up, including new (unsaved, id-less) events."""

    reflow_cancelled: set[str]
    day_end_reasons: dict[str, str]


def _simulate(
    facts: list[_Fact],
    copies: dict[str, Event],
    cancels: dict[str, str],
    options: ReallocationOptions,
    warnings: list[str],
    next_day_follows: bool = False,
) -> _Simulated:
    """Place every decided fact into the day, reflowing the rest around
    it, in memory. `copies` is updated in place to where each existing
    event ends up; `cancels` gains anything a moved bedtime cancels."""
    decided = [f for f in facts if not f.default]
    base_ids = {f.base.id for f in decided if f.base}
    working = sorted(
        (copies[event_id] for event_id in copies if event_id not in base_ids and event_id not in cancels),
        key=lambda e: e.start,
    )
    # The day's end is settled first, so every other fact reflows into the
    # day as it will actually end -- e.g. an activity noted just before a
    # later-than-planned bedtime, which would otherwise collide with the
    # sleep event's old start.
    day_end_reasons: dict[str, str] = {}
    for fact in decided:
        if fact.moves_day_end:
            working = _end_day_at(fact, working, cancels, day_end_reasons, warnings, next_day_follows)
    for fact in decided:
        if fact.moves_day_end:
            continue
        # reallocate_for_new_event wants only the day *from* the new event's
        # start onward (at most the first event may overlap that start), so
        # whatever already ended before this fact -- including every
        # earlier fact -- is set aside and can't be disturbed.
        head = [e for e in working if e.end <= fact.start]
        tail = [e for e in working if e.end > fact.start]
        pinned = next((e for e in tail if e.is_fixed_time and e.start < fact.end), None)
        if pinned is not None:
            # Reflowing can never move a fixed-time event out of the way,
            # so say so directly rather than let reallocation fail on it.
            raise CompactionError(
                f"{fact.event.summary!r} ({fact.start.isoformat()} to {fact.end.isoformat()}) "
                f"doesn't fit: it overlaps fixed-time {pinned.summary!r} "
                f"({pinned.start.isoformat()} to {pinned.end.isoformat()}), which can't move. "
                f"{_SHORTEN_OR_MOVE} If a note shows when {pinned.summary!r} actually started or "
                "ended, move it with 'keep' too."
            )
        try:
            changed = reallocate_for_new_event(tail, fact.event, options)
        except FixedTimeConflict as exc:
            raise CompactionError(f"{exc} {_SHORTEN_OR_MOVE}") from exc
        except ValueError as exc:
            hint = (
                " A day needs an event after the last noted time (normally the end-of-day sleep "
                "event) for the rest to reflow into."
                if not any(e.end > fact.end for e in tail)
                else ""
            )
            raise CompactionError(
                f"can't fit {fact.event.summary!r} ({fact.start.isoformat()} to "
                f"{fact.end.isoformat()}) into the day: {exc}.{hint}"
            ) from exc
        known = {id(e) for e in tail}
        alive = [e for e in tail if e.status != "cancelled"]
        extra = [e for e in changed if id(e) not in known and e.status != "cancelled"]
        working = head + sorted(alive + extra, key=lambda e: e.start)

    for fact in facts:
        if fact.event.status == "cancelled" or (fact.event.start, fact.event.end) != (fact.start, fact.end):
            raise CompactionError(
                f"{fact.event.summary!r} ({fact.start.isoformat()} to {fact.end.isoformat()}) "
                "doesn't fit: the rest of the day can't be moved around it without moving it too. "
                f"{_SHORTEN_OR_MOVE}"
            )
    return _Simulated(
        working=working,
        reflow_cancelled={i for i, e in copies.items() if e.status == "cancelled" and i not in cancels},
        day_end_reasons=day_end_reasons,
    )


def _changes(
    facts: list[_Fact],
    events_by_id: dict[str, Event],
    copies: dict[str, Event],
    cancels: dict[str, str],
    simulated: _Simulated,
) -> list[CompactionChange]:
    changes: list[CompactionChange] = []
    decided_by_base = {f.base.id: f for f in facts if f.base and not f.default}
    default_ids = {f.base.id for f in facts if f.default}
    for event_id, original in events_by_id.items():
        before = EventState.from_event(original)
        if event_id in cancels:
            changes.append(
                CompactionChange(action="cancel", event_id=event_id, reason=cancels[event_id], before=before)
            )
            continue
        if event_id in decided_by_base:
            fact = decided_by_base[event_id]
            after, reason = EventState.from_event(fact.event), fact.reason
        elif event_id in simulated.reflow_cancelled:
            changes.append(
                CompactionChange(
                    action="cancel",
                    event_id=event_id,
                    reason="no room was left for it once the actual events were placed",
                    before=before,
                )
            )
            continue
        else:
            after = EventState.from_event(copies[event_id])
            only_notes = replace(after, description=before.description) == before
            only_goals = replace(after, description=before.description, goal_ids=before.goal_ids) == before
            if only_notes:
                reason = "added the notes that fall during it"
            elif only_goals:
                reason = "set the goals it serves"
            elif event_id in default_ids:
                reason = next(f.reason for f in facts if f.default and f.base.id == event_id)
            else:
                reason = simulated.day_end_reasons.get(event_id, "moved to make room for the actual events")
        if after != before:
            changes.append(
                CompactionChange(action="update", event_id=event_id, reason=reason, before=before, after=after)
            )
    fact_events = {id(f.event) for f in facts}
    for fact in facts:
        if fact.base is None:
            changes.append(
                CompactionChange(action="create", reason=fact.reason, after=EventState.from_event(fact.event))
            )
    for event in simulated.working:
        if event.id is None and id(event) not in fact_events:
            changes.append(
                CompactionChange(
                    action="create",
                    reason="the remainder of an event that an actual event split in two",
                    after=EventState.from_event(event),
                )
            )

    order = {"cancel": 0, "update": 1, "create": 2}
    changes.sort(key=lambda c: (order[c.action], (c.after or c.before).start, c.event_id or ""))
    return changes


def _timeline(
    ordered: list[PlanNote],
    ignored: set[str],
    facts: list[_Fact],
    live: list[Event],
    copies: dict[str, Event],
    cancels: dict[str, str],
    merged_into: dict[str, str],
    annotated: dict[str, str],
    simulated: _Simulated,
    now: datetime,
    goal_names: dict[str, str],
) -> Timeline:
    def names(goal_ids) -> list[str]:
        return [goal_names.get(g, g) for g in goal_ids or ()]

    decided_by_base = {f.base.id: f for f in facts if f.base and not f.default}
    default_ids = {f.base.id for f in facts if f.default}
    events: list[TimelineEvent] = []
    for original in live:
        planned = dict(event_id=original.id, planned_start=original.start, planned_end=original.end)
        if original.id in cancels:
            target = decided_by_base.get(merged_into.get(original.id))
            events.append(
                TimelineEvent(
                    summary=original.summary or original.id,
                    status="merged" if target else "cancelled",
                    merged_into=target.event.summary if target else None,
                    **planned,
                )
            )
            continue
        if original.id in simulated.reflow_cancelled:
            events.append(TimelineEvent(summary=original.summary or original.id, status="cancelled", **planned))
            continue
        fact = decided_by_base.get(original.id)
        final = fact.event if fact else copies[original.id]
        if fact is None and original.start >= now and EventState.from_event(final) == EventState.from_event(original):
            continue  # the future, untouched: not the notes' business
        same = (final.start, final.end) == (original.start, original.end)
        if fact is not None:
            status = ("on_schedule" if final.end <= now else "planned") if same else "adjusted"
        elif original.id in default_ids:
            status = "on_schedule"
        else:
            status = "planned" if same else "reflowed"
        events.append(
            TimelineEvent(
                summary=final.summary or original.id,
                status=status,
                start=final.start,
                end=final.end,
                start_note=fact.start_note.id if fact and fact.start_note else None,
                end_note=fact.end_note.id if fact and fact.end_note else None,
                goals=names(final.goal_ids),
                new_goals=[n for n in names(final.goal_ids) if n not in names(original.goal_ids)],
                **planned,
            )
        )
    fact_events = {id(f.event) for f in facts}
    for fact in facts:
        if fact.base is None:
            events.append(
                TimelineEvent(
                    summary=fact.event.summary,
                    status="new",
                    start=fact.start,
                    end=fact.end,
                    start_note=fact.start_note.id if fact.start_note else None,
                    end_note=fact.end_note.id if fact.end_note else None,
                    goals=names(fact.event.goal_ids),
                    new_goals=names(fact.event.goal_ids),
                )
            )
    for event in simulated.working:
        if event.id is None and id(event) not in fact_events:
            events.append(
                TimelineEvent(
                    summary=event.summary or "",
                    status="new",
                    start=event.start,
                    end=event.end,
                    goals=names(event.goal_ids),
                )
            )

    anchors: dict[str, list[str]] = {}
    for fact in facts:
        if fact.start_note is not None:
            anchors.setdefault(fact.start_note.id, []).append(f"start of {fact.event.summary}")
        if fact.end_note is not None:
            anchors.setdefault(fact.end_note.id, []).append(f"end of {fact.event.summary}")
    notes = [
        TimelineNote(
            id=note.id,
            time=note.timestamp,
            text=note.description,
            anchors=anchors.get(note.id, []),
            annotates=annotated.get(note.id),
            ignored=note.id in ignored,
        )
        for note in ordered
    ]
    return build_timeline(notes, events, now)


def _with_context(timeline: Timeline, previous_note: PlanNote | None, last_compaction: datetime | None) -> Timeline:
    """`timeline` with the latest compacted note (marked compacted) and
    the last compaction's time added."""
    notes = list(timeline.notes)
    if previous_note is not None:
        notes.append(
            TimelineNote(
                id=previous_note.id, time=previous_note.timestamp, text=previous_note.description, compacted=True
            )
        )
    return build_timeline(
        notes, timeline.events, timeline.now, decided=timeline.decided, last_compaction=last_compaction
    )


def planned_timeline(
    notes: list[PlanNote],
    day_events: list[Event],
    now: datetime,
    goal_names: dict[str, str] | None = None,
    suggested: dict[str, list[str]] | None = None,
    *,
    previous_note: PlanNote | None = None,
    last_compaction: datetime | None = None,
) -> Timeline:
    """The timeline before anything is decided: the notes beside the
    day's events as planned -- what a model compares to make its
    decisions. `suggested` (event id -> goal ids) marks goals suggested
    for events that have none. `previous_note`, an already-compacted note,
    and `last_compaction`, when it was compacted, are shown as context."""
    goal_names = goal_names or {}
    suggested = suggested or {}
    timeline_notes = [TimelineNote(id=n.id, time=n.timestamp, text=n.description) for n in notes]
    timeline = build_timeline(
        timeline_notes,
        [
            TimelineEvent(
                summary=e.summary or e.id,
                status="planned",
                event_id=e.id,
                start=e.start,
                end=e.end,
                planned_start=e.start,
                planned_end=e.end,
                goals=[goal_names.get(g, g) for g in e.goal_ids or ()],
                new_goals=[goal_names.get(g, g) for g in suggested.get(e.id, ())],
            )
            for e in day_events
            if e.id and e.status != "cancelled"
        ],
        now,
        decided=False,
    )
    return _with_context(timeline, previous_note, last_compaction)
