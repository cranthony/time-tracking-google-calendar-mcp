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
- `create` (a `summary`, both edges, optionally `action_ids`): something
  that happened that wasn't planned.

`keep` and `create` take `action_ids` (the first setting its color) to
set what was done at an event; for `keep`, leaving it out keeps them,
and `[]` clears them. They take `facts` too (see utilities/facts.py):
where it was, who it was with and for, and a note on each person there,
replacing its facts whole; left out, they're kept, and empty facts
remove them.
- `merge` (an event id, `into` another): fold one event into another, for
  when the user doesn't remember where one ended and the next began. The
  target grows to cover both and is titled after both (unless renamed);
  the merged one is cancelled. Never implied -- a decision has to ask for
  it.

## What happens to the rest of the calendar

- The past is treated as certain. Every resulting past event -- decided,
  or untouched and so on schedule -- becomes a *fact*: once the
  compaction is stamped, everything before its `now` is history. Nothing
  is written on an event to say so; it's what it would leave unchanged.
- Only what's happened is settled. An event still going on at `now` is
  settled up to `now` -- its start, and its lasting until now, are
  fact -- and may run on past where it was planned to end: a later
  compaction says how far.
- A later compaction keeps to what an earlier one settled
  (`history_until`, its `now`): an event that started before then can't
  have its start moved, end before then, or be cancelled or merged
  away. Its end can run later.
- A `keep` that moves an event in the future is a direct reschedule.
- NOTHING IS MOVED TO MAKE ROOM. Every event a decision keeps or creates
  must end after it starts and not overlap any other of the day's
  events -- past or still to come -- or the plan is refused, naming every
  problem, with the day as it would leave it (the same refusal as any
  batch of event changes: see utilities/event_changes.py). The decisions
  say what gives way: an overrun moves or shortens the next event, an
  earlier bedtime ends or cancels what ran past it.
- Every note that doesn't set an edge, and isn't listed in
  `ignore_notes`, has its text added to the description of the event it
  falls within, so it survives compaction -- or, if `note_targets` names
  one for it, to that event instead. A note that sets an edge is only
  added to an event if `note_targets` has it: to the one it names, or,
  if it names none, to the one whose edge it sets.

The day's own end-of-day sleep event ends the day, and its end starts
the next one, which is never adjusted -- only warned about, unless the
next day is being compacted with it (`next_day_follows`). Cancelling it
-- a night without sleep -- makes the day run on to the next one's end.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Literal

from calendar_clients.google_calendar import MAX_DESCRIPTION_BYTES, Event
from utilities.event_changes import ChangeError, Placed, Problem, overlap_problems, rejection
from utilities.facts import Facts
from utilities.compaction_timeline import Timeline, TimelineEvent, TimelineNote, build_timeline

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

    description: str | None = None
    """`keep`/`create`: the event's whole description, as given -- the
    final word: no notes or `annotate` text are added to it ("" clears
    it). For the user's edits (see utilities/compaction_proposals.py)."""

    location: str | None = None
    """`keep`/`create`: the event's free-text location ("" clears it)."""

    priority: int | None = None
    """`keep`/`create`: the event's own priority."""

    action_ids: list[str] | None = None
    """`keep`/`create`: what was done at it, the first action setting its
    color. For `keep`, `None` keeps its actions and `[]` clears them."""

    facts: Facts | None = None
    """`keep`/`create`: where, who with, who for, and notes on each person
    there (see utilities/facts.py), replacing its facts whole. For `keep`,
    `None` keeps them, and empty facts remove them."""

    counts_against_follow_through: bool | None = None
    """`cancel`: whether the user dropped it (it counts against the
    follow-through of whoever it was planned with) or the plan changed.
    `None`, from before cancels said, counts."""

    key: str | None = None
    """`create`: what a proposal calls the event before it exists, so an
    edit can name it (see utilities/compaction_proposals.py)."""

    def to_json_dict(self) -> dict:
        return {
            key: (
                value.isoformat() if isinstance(value, datetime)
                else facts_dict(value) if isinstance(value, Facts)
                else value
            )
            for key, value in self.__dict__.items()
            if value is not None
        }

    @classmethod
    def from_json_dict(cls, data: dict) -> "EventDecision":
        parsed = dict(data)
        # From before goals, and from goals, which actions replaced.
        for retired in ("event_label_id", "goal_ids", "facets"):
            parsed.pop(retired, None)
        for key in _DATETIME_FIELDS:
            if key in parsed:
                parsed[key] = datetime.fromisoformat(parsed[key])
        if "facts" in parsed:
            parsed["facts"] = facts_from_dict(parsed["facts"])
        return cls(**parsed)


@dataclass(kw_only=True)
class CompactionUpdate:
    """A planned event that happened: an `update` of compact_notes -- see
    `NoteCompactor`'s instructions. Edges stay as planned unless moved, to
    a time or to a note's."""

    event_id: str
    summary: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    start_note: str | None = None
    end_note: str | None = None
    annotate: str | None = None
    action_ids: list[str] | None = None
    facts: Facts | None = None

    def decision(self) -> EventDecision:
        return EventDecision(action="keep", **vars(self))


@dataclass(kw_only=True)
class CompactionCreate:
    """Something unplanned that happened: a `create` of compact_notes."""

    summary: str
    start: datetime | None = None
    end: datetime | None = None
    start_note: str | None = None
    end_note: str | None = None
    annotate: str | None = None
    action_ids: list[str] | None = None
    facts: Facts | None = None
    key: str | None = None
    """The key a proposal already knows this event by, to keep it (see
    utilities/compaction_proposals.py); a new one is given otherwise."""

    def decision(self) -> EventDecision:
        return EventDecision(action="create", **vars(self))


def facts_dict(facts: Facts | None) -> dict[str, Any] | None:
    """`facts` as the JSON object they're stored as (see
    utilities/facts.py) -- `None` for none, or empty ones."""
    if facts is None or facts.is_empty():
        return None
    return json.loads(facts.to_json())


def facts_from_dict(data: dict[str, Any] | None) -> Facts | None:
    """The inverse of `facts_dict`: empty facts for `None`."""
    return Facts.from_json(json.dumps(data)) if data is not None else Facts()


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
    priority: int | None = None
    event_label_id: str | None = None
    action_ids: list[str] | None = None
    facts: dict[str, Any] | None = None
    """As stored on the event (see utilities/facts.py); `None` for none."""

    @classmethod
    def from_event(cls, event: Event) -> "EventState":
        return cls(
            summary=event.summary,
            start=event.start,
            end=event.end,
            description=event.description,
            location=event.location,
            status=event.status,
            priority=event.priority,
            event_label_id=event.event_label_id,
            action_ids=list(event.action_ids) if event.action_ids is not None else None,
            facts=facts_dict(event.facts),
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
            priority=self.priority,
            event_label_id=self.event_label_id,
            action_ids=list(self.action_ids) if self.action_ids is not None else None,
            facts=facts_from_dict(self.facts) if self.facts is not None else None,
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
        # From goals, which actions replaced; from reallocation, which
        # batches of changes did (see utilities/event_changes.py); and from
        # marking each event compacted, which the last compaction's time did.
        for retired in ("goal_ids", "facets", "min_duration_minutes", "is_fixed_time", "compacted_until"):
            parsed.pop(retired, None)
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

    key: str | None = None
    """For a `create`: its decision's key, if it has one."""

    before: EventState | None = None
    after: EventState | None = None


NoteUse = Literal["edge", "annotates", "ignored", "unused"]


@dataclass(kw_only=True)
class CompactionPlan:
    changes: list[CompactionChange]
    warnings: list[str] = field(default_factory=list)
    timeline: Timeline | None = None
    note_uses: dict[str, tuple[NoteUse, str | None]] = field(default_factory=dict)
    """Each note's id -> what the plan does with it, and the event (id,
    or a created one's key) it's used with: the one it `annotates`, or
    an `edge` it sets (and isn't added to anything); `ignored`, or
    `unused` (no text, or no event to add it to)."""

    note_edges: dict[str, str] = field(default_factory=dict)
    """Each note that sets an edge -> that event (id, or key), whether or
    not it's also added to one."""


@dataclass(kw_only=True)
class NoteAnnotation:
    """A note to add to a particular event, rather than the one it falls
    within: an `annotate_notes` entry of compact_notes."""

    note_id: str
    event_id: str
    """An event's id, or a created event's key."""


class CompactionError(ChangeError):
    """The decisions (or the calendar they were checked against) can't be
    turned into a plan. The message says what to fix, and lists the valid
    choices where there are any -- it's written to be read by the model
    that produced the decisions. Its `categories` are logged as any
    refused change's are (see utilities/event_changes.py)."""


@dataclass
class _Fact:
    """An event whose resulting time is certain: it's placed exactly
    there."""

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

    fixed_description: bool = False
    """Its decision gives its whole description: nothing's added to it."""

    created_key: str | None = None
    """For a create: its decision's `key`, if it has one."""


def plan_compaction(
    notes: list[PlanNote],
    decisions: list[EventDecision],
    day_events: list[Event],
    now: datetime,
    *,
    ignore_notes: list[str] | None = None,
    day_start: datetime | None = None,
    names: dict[str, str] | None = None,
    previous_note: PlanNote | None = None,
    last_compaction: datetime | None = None,
    next_day_follows: bool = False,
    history_until: datetime | None = None,
    note_targets: dict[str, str | None] | None = None,
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
    `names` (an action's, person's or location's id -> its name) labels
    events' actions and facts in the timeline. `previous_note` (an already-compacted
    note) and `last_compaction` (when that compaction ran) are only shown
    in the timeline, as context. `history_until` is where what an earlier
    compaction settled ends -- its `now` (see the module docstring).
    `note_targets` (a note's id -> an event's id, or a created one's key)
    adds those notes to those events, wherever they fall; one naming none
    (`None`) adds its note where it falls -- or, if it sets an edge, to
    that event.

    Raises `CompactionError` (listing everything wrong at once) if the
    decisions are invalid or leave past events overlapping. Never mutates
    its arguments."""
    ignored = set(ignore_notes or [])
    note_targets = dict(note_targets or {})
    problems: list[str] = []
    warnings: list[str] = []

    ordered = [n for _, n in sorted(enumerate(notes), key=lambda p: (p[1].timestamp, p[0]))]
    notes_by_id = {n.id: n for n in notes}
    for note in ordered:
        if note.timestamp > now:
            problems.append(Problem("note_after_now", f"note {note.id} is timestamped after now ({now.isoformat()})"))
    for note_id in sorted(ignored - set(notes_by_id)):
        problems.append(
            Problem("unknown_note", f"ignore_notes: {note_id!r} isn't one of this round's notes; {_valid_notes(notes)}")
        )
    for note_id in sorted(set(note_targets) - set(notes_by_id)):
        problems.append(
            Problem("unknown_note", f"annotate_notes: {note_id!r} isn't one of this round's notes; {_valid_notes(notes)}")
        )
    for note_id in sorted(set(note_targets) & ignored):
        problems.append(f"note {note_id} can't be both ignored and added to an event")

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

    resolved = _resolve(decisions, notes, notes_by_id, events_by_id, closing, now, problems, history_until)
    if problems:
        raise CompactionError.of(problems, "malformed_decision")
    facts, cancels, merged_into, touched = resolved

    decided_ids = {f.base.id for f in facts if f.base} | set(cancels) | set(touched)
    for event in live:
        if event.id in decided_ids or event.end > now:
            continue
        copy = copies[event.id]
        facts.append(
            _Fact(
                key=event.id,
                start=copy.start,
                end=copy.end,
                event=copy,
                base=event,
                reason="happened as planned -- no note says otherwise",
                default=True,
            )
        )
    facts.sort(key=lambda f: (f.start, f.end))

    for event_id, decision in touched.items():
        if decision.summary:
            copies[event_id].summary = decision.summary
        if decision.action_ids is not None:
            copies[event_id].action_ids = list(decision.action_ids)
        if decision.facts is not None:
            copies[event_id].facts = decision.facts
        _set_fields(copies[event_id], decision)
    annotated, annotated_keys, kept_out = _annotate(
        ordered, ignored, facts, touched, copies, cancels, warnings, problems, note_targets
    )
    if problems:
        raise CompactionError.of(problems, "description_too_long")

    simulated = _place(facts, copies, cancels, warnings, next_day_follows, history_until)
    changes = _changes(facts, events_by_id, copies, cancels, simulated)
    if not changes:
        warnings.append("nothing on the calendar needs to change")
    timeline = _timeline(
        ordered, ignored, facts, live, copies, cancels, merged_into, annotated, simulated, now, names or {}
    )
    timeline = _with_context(timeline, previous_note, last_compaction)
    edges = {n.id: f.key for f in facts for n in (f.start_note, f.end_note) if n is not None}
    note_uses: dict[str, tuple[NoteUse, str | None]] = {}
    for note in ordered:
        if note.id in annotated_keys:
            note_uses[note.id] = ("annotates", annotated_keys[note.id])
        elif note.id in edges:
            note_uses[note.id] = ("edge", edges[note.id])
        elif note.id in ignored or note.id in kept_out:
            note_uses[note.id] = ("ignored", None)
        else:
            note_uses[note.id] = ("unused", None)
    return CompactionPlan(
        changes=changes, warnings=warnings, timeline=timeline, note_uses=note_uses, note_edges=edges
    )


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
    history_until: datetime | None = None,
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
                problems.append(
                    Problem("unknown_note", f"{label}: {note_id!r} isn't one of this round's notes; {_valid_notes(notes)}")
                )
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
            problems.append(
                Problem("unknown_event", f"{label}: {decision.event_id!r} isn't one of this day's events; {valid_events}")
            )
            continue
        if decision.event_id in by_event:
            problems.append(Problem("duplicate_decision", f"{label}: event {decision.event_id} has more than one decision"))
            continue
        if decision.action != "keep" and (
            moves or decision.summary or decision.annotate or decision.facts
            or decision.description is not None or decision.location is not None or decision.priority is not None
        ):
            problems.append(
                f"{label}: times, notes, summary, annotate, description, location, priority and facts only go "
                "with 'keep' or 'create'"
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
    def settled(event: Event) -> datetime | None:
        """How long an earlier compaction recorded `event` going on, if it
        started before `history_until`."""
        if history_until is None or event.start >= history_until:
            return None
        return min(event.end, history_until)

    for event_id, decision in by_event.items():
        compacted = settled(events_by_id[event_id])
        if decision.action in ("cancel", "merge") and compacted is not None:
            problems.append(
                Problem(
                    "compacted",
                    f"{events_by_id[event_id].summary!r} ({event_id}) can't be "
                    f"{'cancelled' if decision.action == 'cancel' else 'merged away'}: an earlier compaction "
                    f"recorded it happening, until {compacted.isoformat()}. If it ended sooner than "
                    "planned, end it no earlier than that with 'keep'.",
                )
            )
            continue
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
                Problem("nonpositive_length", f"{label}: {base.summary!r} would run from {start.isoformat()} to {end.isoformat()}, "
                "which isn't a positive length")
            )
            continue
        compacted = settled(base)
        if compacted is not None and start != base.start:
            problems.append(
                Problem(
                    "compacted",
                    f"{label}: {base.summary!r} started at {base.start.isoformat()}, as an earlier compaction "
                    "recorded -- its start can't move. If something else happened first, give that its own "
                    "event.",
                )
            )
            continue
        if compacted is not None and end < compacted:
            problems.append(
                Problem(
                    "compacted",
                    f"{label}: {base.summary!r} can't end at {end.isoformat()}: an earlier compaction recorded "
                    f"it going on until {compacted.isoformat()}, so it ends then or later.",
                )
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
        if decision.action_ids is not None:
            event.action_ids = list(decision.action_ids)
        if decision.facts is not None:
            event.facts = decision.facts
        _set_fields(event, decision)
        event.start, event.end = start, end
        if event_id in merges:
            reason = (
                f"merged with {', '.join(repr(m.summary) for m in members[1:])} -- where one ended "
                "and the next began isn't remembered"
            )
        elif not changed:
            reason = "happened as planned"
        elif start >= now:
            reason = "moved as requested"
        else:
            reason = "realigned to match the notes"
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
                fixed_description=decision.description is not None,
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
                Problem(
                    "nonpositive_length",
                    f"{label}: {decision.summary!r} would run from {start.isoformat()} to "
                    f"{end.isoformat()}, which isn't a positive length",
                )
            )
            continue
        facts.append(
            _Fact(
                key=decision.key or f"new:{number}",
                created_key=decision.key,
                start=start,
                end=end,
                event=Event(
                    summary=decision.summary.strip(),
                    start=start,
                    end=end,
                    action_ids=list(decision.action_ids) if decision.action_ids is not None else None,
                    facts=decision.facts,
                    description=decision.description or None,
                    location=decision.location or None,
                    priority=decision.priority,
                ),
                base=None,
                reason="something the notes show happened that wasn't planned",
                start_note=start_note,
                end_note=end_note,
                annotations=[decision.annotate.strip()] if (decision.annotate or "").strip() else [],
                fixed_description=decision.description is not None,
            )
        )
    return facts, cancels, merged_into, touched


def _set_fields(event: Event, decision: EventDecision) -> None:
    """Give `event` the description, location and priority `decision`
    sets, if any ("" clears a description or location)."""
    if decision.description is not None:
        event.description = decision.description
    if decision.location is not None:
        event.location = decision.location
    if decision.priority is not None:
        event.priority = decision.priority


def _annotate(
    ordered: list[PlanNote],
    ignored: set[str],
    facts: list[_Fact],
    touched: dict[str, EventDecision],
    copies: dict[str, Event],
    cancels: dict[str, str],
    warnings: list[str],
    problems: list[str],
    note_targets: dict[str, str | None] | None = None,
) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Add every note that doesn't set an edge (and isn't ignored), and
    every `annotate`, to the description of the event it belongs to: the
    one `note_targets` names for it (edge or not), or else the one it
    falls within -- except that an edge note is added only if
    `note_targets` has it, to the event whose edge it sets if it names
    none. Returns note id -> the title of the event it was added to,
    and note id -> that event's id (or key) -- and the notes that went to
    an event whose description its decision gives whole, which nothing's
    added to (a note whose text it has counts as added).

    A description that would grow past `MAX_DESCRIPTION_BYTES` is a
    problem, not a warning: Calendar would silently cut it short."""
    anchors = {n.id for f in facts for n in (f.start_note, f.end_note) if n is not None}
    fact_ids = {f.base.id for f in facts if f.base}
    # Where each candidate event ends up: facts exactly, everything else
    # where it is.
    targets: list[tuple[datetime, datetime, str, Event]] = [
        (f.start, f.end, f.key, f.event) for f in facts
    ] + [
        (e.start, e.end, event_id, e)
        for event_id, e in copies.items()
        if event_id not in fact_ids and event_id not in cancels
    ]
    by_key = {t[2]: t for t in targets}
    edge_of = {n.id: f.key for f in facts for n in (f.start_note, f.end_note) if n is not None}
    note_targets = note_targets or {}
    lines: dict[str, list[tuple[datetime | None, str]]] = {}
    events: dict[str, Event] = {}
    note_ids: dict[str, list[str]] = {}
    annotated: dict[str, str] = {}
    annotated_keys: dict[str, str] = {}
    fixed = {f.key for f in facts if f.fixed_description} | {
        event_id for event_id, decision in touched.items() if decision.description is not None
    }
    kept_out: set[str] = set()
    for note in ordered:
        text = (note.description or "").strip()
        target = note_targets.get(note.id)
        if (note.id in anchors and note.id not in note_targets) or note.id in ignored or not text:
            continue
        if target is None and note.id in anchors:
            target = edge_of[note.id]
        hit = None
        if target is not None:
            hit = by_key.get(target)
            if hit is None and target in cancels:
                warnings.append(
                    f"note {note.id} ({text!r}) was to go with {target}, which is cancelled, so it was added "
                    "where it falls instead"
                )
            elif hit is None:
                problems.append(
                    Problem("unknown_event", f"note {note.id} ({text!r}) can't go with {target!r}: it isn't one of this day's events")
                )
                continue
        hit = hit or next((t for t in targets if t[0] <= note.timestamp < t[1]), None) or next(
            (t for t in targets if t[0] < note.timestamp <= t[1]), None
        )
        if hit is None:
            warnings.append(
                f"note {note.id} ({text!r}) doesn't fall within any event, so its text wasn't "
                "added anywhere"
            )
            continue
        if hit[2] in fixed:
            if text not in (hit[3].description or ""):
                kept_out.add(note.id)
                continue
            annotated[note.id] = hit[3].summary or hit[2]
            annotated_keys[note.id] = hit[2]
            continue
        lines.setdefault(hit[2], []).append((note.timestamp, text))
        events[hit[2]] = hit[3]
        note_ids.setdefault(hit[2], []).append(note.id)
        annotated[note.id] = hit[3].summary or hit[2]
        annotated_keys[note.id] = hit[2]
    for fact in facts:
        for text in fact.annotations if fact.key not in fixed else ():
            lines.setdefault(fact.key, []).append((None, text))
            events[fact.key] = fact.event
    for event_id, decision in touched.items():
        if (decision.annotate or "").strip() and event_id not in fixed:
            lines.setdefault(event_id, []).append((None, decision.annotate.strip()))
            events[event_id] = copies[event_id]

    for key, entries in lines.items():
        event = events[key]
        timed = sorted((e for e in entries if e[0] is not None), key=lambda e: e[0])
        formatted = [
            f"- {moment.astimezone(event.start.tzinfo).strftime('%H:%M')} {text}" for moment, text in timed
        ] + [f"- {text}" for moment, text in entries if moment is None]
        # A line the description already has isn't added again: replaying a
        # proposal whose notes were partly written (see utilities/
        # compaction_proposals.py) mustn't repeat them.
        existing = (event.description or "").splitlines()
        formatted = [line for line in formatted if line not in existing]
        if not formatted:
            continue
        if "Notes:" in existing:
            prefix = f"{event.description}\n"
        else:
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
    for key in fixed:
        event = by_key[key][3] if key in by_key else copies.get(key)
        size = len((event.description or "").encode("utf-8")) if event is not None else 0
        if size > MAX_DESCRIPTION_BYTES:
            problems.append(
                f"the description of {event.summary or key!r} is {size} bytes, over the {MAX_DESCRIPTION_BYTES} "
                "Calendar keeps (it silently cuts the rest): shorten it"
            )
    return annotated, annotated_keys, kept_out


@dataclass
class _Simulated:
    working: list[Event]
    """The day as it ends up, including new (unsaved, id-less) events."""



def _place(
    facts: list[_Fact],
    copies: dict[str, Event],
    cancels: dict[str, str],
    warnings: list[str],
    next_day_follows: bool = False,
    history_until: datetime | None = None,
) -> _Simulated:
    """Every event where the decisions leave it: a decided one at its
    decided times, a new one where it's created, the rest where they
    were. Nothing is moved to make room: if an event a decision moves or
    creates would overlap another, or take no time, the plan is refused,
    naming every problem, with the day as it would leave it -- the same
    refusal as any batch of event changes (see utilities/
    event_changes.py), so the decisions can be put right at once."""
    decided = {f.base.id: f for f in facts if f.base and not f.default}
    # Every decided event answers for what it overlaps, moved or not: one
    # an earlier day of the batch moved (a night that ran late) is already
    # where it was put by the time this day reads it.
    placed = [Placed(event=f.event, key=f.key, moved=True, asked=True) for f in facts if not f.default]
    placed += [
        Placed(event=copy, key=event_id)
        for event_id, copy in copies.items()
        if event_id not in decided and event_id not in cancels
    ]
    problems = overlap_problems(placed)
    if problems:
        raise CompactionError.wrapping(
            str(
                rejection(
                    problems, placed,
                    since=min(p.event.start for p in placed), until=max(p.event.end for p in placed),
                    history_until=history_until,
                )
            ),
            CompactionError.of(problems, "overlap"),
        )
    for fact in facts:
        if fact.moves_day_end and fact.end != fact.base.end and not next_day_follows:
            warnings.append(
                f"{fact.event.summary!r} now ends at {fact.end.isoformat()} instead of "
                f"{fact.base.end.isoformat()}. Compaction doesn't adjust the next day for that -- "
                "check the next day's first events, and move them with update_event if they now overlap."
            )
    return _Simulated(working=[p.event for p in placed])


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
        else:
            after = EventState.from_event(copies[event_id])
            only_notes = replace(after, description=before.description) == before
            only_actions = replace(after, description=before.description, action_ids=before.action_ids) == before
            only_facts = (
                replace(after, description=before.description, action_ids=before.action_ids, facts=before.facts)
                == before
            )
            if event_id in default_ids:
                reason = next(f.reason for f in facts if f.default and f.base.id == event_id)
            elif only_notes:
                reason = "added the notes that fall during it"
            elif only_actions:
                reason = "set what was done at it (its actions)"
            elif only_facts:
                reason = "recorded what happened at it (its facts)"
            else:
                reason = "updated"
        if after != before:
            changes.append(
                CompactionChange(action="update", event_id=event_id, reason=reason, before=before, after=after)
            )
    for fact in facts:
        if fact.base is None:
            changes.append(
                CompactionChange(
                    action="create", reason=fact.reason, key=fact.created_key, after=EventState.from_event(fact.event)
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
    names: dict[str, str],
) -> Timeline:
    def named(action_ids) -> list[str]:
        return [names.get(a, a) for a in action_ids or ()]

    def fact_fields(final: Event, original: Event | None = None) -> dict:
        """The timeline's facts fields for `final`: its facts' lines,
        and whether this compaction sets them."""
        said = facts_lines(final.facts, names)
        changed = bool(said) and (original is None or facts_dict(final.facts) != facts_dict(original.facts))
        return dict(facts=said, new_facts=changed)

    def missing(final: Event, start: datetime) -> list[str]:
        """What `final` lacks that its judgments need, if it's in the
        past -- what compaction records."""
        if start >= now:
            return []
        gaps = [] if final.action_ids else ["action"]
        if not (final.facts and final.facts.location_id):
            gaps.append("location")
        return gaps

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
            status = "planned"
        events.append(
            TimelineEvent(
                summary=final.summary or original.id,
                status=status,
                start=final.start,
                end=final.end,
                start_note=fact.start_note.id if fact and fact.start_note else None,
                end_note=fact.end_note.id if fact and fact.end_note else None,
                actions=named(final.action_ids),
                new_actions=[n for n in named(final.action_ids) if n not in named(original.action_ids)],
                **fact_fields(final, original),
                missing=missing(final, final.start),
                **planned,
            )
        )
    for fact in facts:
        if fact.base is None:
            events.append(
                TimelineEvent(
                    summary=fact.event.summary,
                    status="new",
                    event_id=fact.created_key,
                    start=fact.start,
                    end=fact.end,
                    start_note=fact.start_note.id if fact.start_note else None,
                    end_note=fact.end_note.id if fact.end_note else None,
                    actions=named(fact.event.action_ids),
                    new_actions=named(fact.event.action_ids),
                    **fact_fields(fact.event),
                    missing=missing(fact.event, fact.start),
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


def facts_lines(facts: Facts | None, names: dict[str, str]) -> list[str]:
    """`facts` in lines for the timeline: where, who with and who for in
    one ("@ home · with Sam, Alex · for Mom"), then each person's note
    ("Sam: tired, but glad we went"); none for none."""
    if facts is None or facts.is_empty():
        return []

    def named(ids) -> str:
        return ", ".join(names.get(i, i) for i in ids)

    parts = []
    if facts.location_id:
        parts.append(f"@ {names.get(facts.location_id, facts.location_id)}")
    if facts.with_ids:
        parts.append(f"with {named(facts.with_ids)}")
    if facts.for_ids:
        parts.append(f"for {named(facts.for_ids)}")
    lines = [" · ".join(parts)] if parts else []
    lines += [f"{names.get(person, person)}: {note}" for person, note in (facts.notes or {}).items()]
    return lines


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
    names: dict[str, str] | None = None,
    suggested: dict[str, list[str]] | None = None,
    *,
    previous_note: PlanNote | None = None,
    last_compaction: datetime | None = None,
) -> Timeline:
    """The timeline before anything is decided: the notes beside the
    day's events as planned -- what a model compares to make its
    decisions. `suggested` (event id -> action ids) marks actions suggested
    for events that have none. `previous_note`, an already-compacted note,
    and `last_compaction`, when it was compacted, are shown as context."""
    names = names or {}
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
                actions=[names.get(a, a) for a in e.action_ids or ()],
                new_actions=[names.get(a, a) for a in suggested.get(e.id, ())],
                facts=facts_lines(e.facts, names),
            )
            for e in day_events
            if e.id and e.status != "cancelled"
        ],
        now,
        decided=False,
    )
    return _with_context(timeline, previous_note, last_compaction)
