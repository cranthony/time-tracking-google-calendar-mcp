"""Compaction planning: turning a day's free-form time notes, plus a
model's interpretation of them, into calendar changes.

The notes are treated as the authority on what actually happened. Reading
free-form text is *not* this module's job -- the MCP client (a model) does
that, and hands back one `NoteDisposition` per note saying what each note
means. This module is the deterministic half: a pure function,
`plan_compaction`, with no API access, that validates those dispositions
and works out exactly which events to update, create, and cancel. That
keeps everything risky testable, and the plan previewable before anything
is written.

## What a note can mean (`NoteEffect`)

A note has one or more *effects*:

- `starts` (a planned event's id) or `starts_unplanned` (a summary, for
  something that wasn't in the plan): an activity began at the note's time.
- `ends` (a planned event's id, or -- for an unplanned activity -- the id
  of the note that started it): an activity ended at the note's time.
- `marker`: just an annotation of what was happening. It changes nothing
  on its own, but its text is appended to the description of the event it
  falls inside, so it survives compaction.
- `ignore`: no effect at all.
- `ambiguous` (a question): the model can't tell; the plan refuses to
  proceed and hands the question back so the user can be asked.

Notes commonly do two things at once (ending one activity while starting
the next), so a note may combine `starts`/`starts_unplanned`/`ends`
effects. `marker`, `ignore` and `ambiguous` must stand alone.

## Turning effects into time

Only notes with a `starts`/`ends` effect are *boundaries*.

- An activity with a start note and an end note runs exactly between them.
- An activity with only a start note runs until the next boundary -- the
  gap is filled by extending it, not left empty. The last such activity
  is still in progress: it runs to `now`, or to its planned end if that's
  later, but never into the end-of-day sleep block (with a warning if
  that cap applies).
- An activity with only an end note honors that end, and its start is
  pulled back to the *previous* boundary's time, again so nothing is left
  empty. (With no previous boundary it keeps its planned start.)
- Time after an explicit end and before the next boundary is genuinely
  unaccounted for, and stays free.

Two activities that come out overlapping -- e.g. "9:00 started email",
"9:30 done with report" gives the email [9:00, 9:30] and the report
[9:00, 9:30] -- are **merged into one event** ("email and report") rather
than guessing which one it was. The first planned one keeps its id; any
other planned one is cancelled.

## What happens to the rest of the calendar

- The past is treated as certain. Each resulting activity becomes a
  *fact*: its event is set to exactly that interval, and pinned
  (`is_fixed_time`) so no later reallocation moves it.
- A planned event that no note accounts for, and that falls entirely
  inside the noted span in the past, is cancelled. One in the past but
  outside the span isn't evidenced either way, so it's left alone (with a
  warning).
- Everything else -- the future -- reflows around the facts using
  `utilities/reallocation.py`, simulated in memory here, so a plan can be
  previewed without touching the calendar.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal

from calendar_clients.google_calendar import Event
from utilities.reallocation import ReallocationOptions, reallocate_for_new_event

EffectKind = Literal["starts", "starts_unplanned", "ends", "marker", "ignore", "ambiguous"]

_BOUNDARY_KINDS = ("starts", "starts_unplanned", "ends")
_STANDALONE_KINDS = ("marker", "ignore", "ambiguous")


@dataclass(kw_only=True)
class NoteEffect:
    """One thing a note means -- see the module docstring. Which of the
    optional fields are meaningful depends on `kind`."""

    kind: EffectKind
    event_id: str | None = None
    """`starts`/`ends`: the id of a planned event (one of the ids this
    day's context offered)."""

    started_by_note: str | None = None
    """`ends`, for an unplanned activity instead of `event_id`: the id of
    the note whose `starts_unplanned` effect began it."""

    summary: str | None = None
    """`starts_unplanned`: the new activity's title."""

    event_label_id: str | None = None
    """`starts_unplanned`: optionally, an event label to assign it."""

    question: str | None = None
    """`ambiguous`: what to ask the user."""


@dataclass(kw_only=True)
class NoteDisposition:
    """The model's interpretation of one note."""

    note_id: str
    effects: list[NoteEffect]


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


class CompactionError(ValueError):
    """The dispositions (or the calendar they were checked against) can't
    be turned into a plan. The message says what to fix, and lists the
    valid choices where there are any -- it's written to be read by the
    model that produced the dispositions."""


class NeedsClarification(CompactionError):
    """One or more notes were marked `ambiguous`; `questions` are for the
    user."""

    def __init__(self, questions: list[str]):
        super().__init__("needs clarification: " + " | ".join(questions))
        self.questions = questions


@dataclass
class _Activity:
    key: str
    event: Event | None
    summary: str
    label_id: str | None = None
    start_note: PlanNote | None = None
    end_note: PlanNote | None = None
    start: datetime | None = None
    end: datetime | None = None


@dataclass
class _Fact:
    start: datetime
    end: datetime
    members: list[_Activity]
    event: Event = None  # type: ignore[assignment]
    base: Event | None = None


def plan_compaction(
    notes: list[PlanNote],
    dispositions: list[NoteDisposition],
    day_events: list[Event],
    now: datetime,
    options: ReallocationOptions | None = None,
) -> CompactionPlan:
    """Plan the calendar changes that `dispositions` (the interpretation
    of `notes`) imply for `day_events`, as of `now`. See the module
    docstring. Raises `CompactionError` (listing everything wrong at once)
    if the dispositions are invalid, or `NeedsClarification` if any are
    `ambiguous`. Never mutates its arguments."""
    options = options or ReallocationOptions()
    warnings: list[str] = []
    problems: list[str] = []
    questions: list[str] = []

    ordered = [n for _, n in sorted(enumerate(notes), key=lambda p: (p[1].timestamp, p[0]))]
    events_by_id = {e.id: e for e in day_events if e.id and e.status != "cancelled"}

    by_note = _index_dispositions(notes, dispositions, problems)
    for note in ordered:
        if note.timestamp > now:
            problems.append(f"note {note.id} is timestamped after now ({now.isoformat()})")

    activities = _build_activities(ordered, by_note, events_by_id, problems, questions)
    if problems:
        raise CompactionError("\n".join(problems))
    if questions:
        raise NeedsClarification(questions)
    if not activities:
        return CompactionPlan(changes=[], warnings=["no note starts or ends anything; nothing to do"])

    sleep = next((e for e in day_events if e.is_end_of_day_sleep and e.status != "cancelled"), None)
    _compute_intervals(
        ordered, by_note, activities, now, sleep.start if sleep else None, problems, warnings
    )
    if problems:
        raise CompactionError("\n".join(problems))

    facts = _merge_into_facts(activities, warnings)
    _build_fact_events(facts, ordered, by_note, warnings)
    return _simulate(facts, day_events, events_by_id, now, options, warnings)


def _index_dispositions(
    notes: list[PlanNote], dispositions: list[NoteDisposition], problems: list[str]
) -> dict[str, NoteDisposition]:
    known = [n.id for n in notes]
    by_note: dict[str, NoteDisposition] = {}
    for disposition in dispositions:
        if disposition.note_id not in known:
            problems.append(
                f"disposition for unknown note {disposition.note_id!r}; valid note ids: {', '.join(known)}"
            )
        elif disposition.note_id in by_note:
            problems.append(f"note {disposition.note_id} has more than one disposition")
        else:
            by_note[disposition.note_id] = disposition
    missing = [note_id for note_id in known if note_id not in by_note]
    if missing:
        problems.append(
            f"no disposition for note(s) {', '.join(missing)} -- every note needs one "
            "(use kind 'ignore' for a note with no effect)"
        )
    return by_note


def _build_activities(
    ordered: list[PlanNote],
    by_note: dict[str, NoteDisposition],
    events_by_id: dict[str, Event],
    problems: list[str],
    questions: list[str],
) -> dict[str, _Activity]:
    valid_ids = ", ".join(sorted(events_by_id)) or "(none)"
    activities: dict[str, _Activity] = {}

    def planned(event_id: str | None, note: PlanNote) -> _Activity | None:
        if not event_id or event_id not in events_by_id:
            problems.append(
                f"note {note.id}: event {event_id!r} isn't one of this day's planned events; "
                f"valid event ids: {valid_ids}"
            )
            return None
        event = events_by_id[event_id]
        return activities.setdefault(
            event_id, _Activity(key=event_id, event=event, summary=event.summary or "")
        )

    for note in ordered:
        disposition = by_note.get(note.id)
        if disposition is None:
            continue
        effects = disposition.effects
        if not effects:
            problems.append(f"note {note.id} has no effects (use kind 'ignore' if it means nothing)")
            continue
        if len(effects) > 1 and any(e.kind in _STANDALONE_KINDS for e in effects):
            problems.append(
                f"note {note.id}: 'marker', 'ignore' and 'ambiguous' can't be combined with other effects"
            )
            continue
        for effect in effects:
            if effect.kind == "ambiguous":
                questions.append(
                    f"note {note.id} ({note.timestamp.isoformat()}, {note.description!r}): "
                    f"{effect.question or 'what does this note mean?'}"
                )
            elif effect.kind in ("marker", "ignore"):
                continue
            elif effect.kind == "starts":
                activity = planned(effect.event_id, note)
                if activity is None:
                    continue
                if activity.start_note is not None:
                    problems.append(
                        f"note {note.id}: event {effect.event_id} was already started by "
                        f"note {activity.start_note.id}"
                    )
                else:
                    activity.start_note = note
            elif effect.kind == "starts_unplanned":
                key = f"new:{note.id}"
                if not (effect.summary or "").strip():
                    problems.append(f"note {note.id}: 'starts_unplanned' needs a summary")
                elif key in activities:
                    problems.append(f"note {note.id} starts more than one unplanned activity")
                else:
                    activities[key] = _Activity(
                        key=key,
                        event=None,
                        summary=effect.summary.strip(),
                        label_id=effect.event_label_id,
                        start_note=note,
                    )
            elif effect.kind == "ends":
                if bool(effect.event_id) == bool(effect.started_by_note):
                    problems.append(
                        f"note {note.id}: 'ends' needs exactly one of event_id (a planned event) "
                        "or started_by_note (the note that started an unplanned activity)"
                    )
                    continue
                if effect.event_id:
                    activity = planned(effect.event_id, note)
                else:
                    activity = activities.get(f"new:{effect.started_by_note}")
                    if activity is None:
                        problems.append(
                            f"note {note.id}: started_by_note {effect.started_by_note!r} isn't an "
                            "earlier note that started an unplanned activity"
                        )
                if activity is None:
                    continue
                if activity.end_note is not None:
                    problems.append(f"note {note.id}: {activity.summary!r} was already ended by note {activity.end_note.id}")
                else:
                    activity.end_note = note
            else:
                problems.append(f"note {note.id}: unknown effect kind {effect.kind!r}")
    return activities


def _compute_intervals(
    ordered: list[PlanNote],
    by_note: dict[str, NoteDisposition],
    activities: dict[str, _Activity],
    now: datetime,
    bedtime: datetime | None,
    problems: list[str],
    warnings: list[str],
) -> None:
    boundary = [
        n for n in ordered if any(e.kind in _BOUNDARY_KINDS for e in by_note[n.id].effects)
    ]
    position = {n.id: i for i, n in enumerate(boundary)}

    for activity in activities.values():
        if activity.start_note is not None:
            start = activity.start_note.timestamp
            if activity.end_note is not None:
                end = activity.end_note.timestamp
            else:
                i = position[activity.start_note.id]
                if i + 1 < len(boundary):
                    end = boundary[i + 1].timestamp
                else:
                    # Still in progress: to now, or its planned end if that's
                    # later -- but never into the end-of-day sleep block.
                    end = max(now, activity.event.end) if activity.event else now
                    if bedtime is not None and start < bedtime < end:
                        end = bedtime
                        warnings.append(
                            f"{activity.summary!r} has no end note, so it runs until bedtime "
                            f"({bedtime.isoformat()})"
                        )
        else:
            end = activity.end_note.timestamp
            i = position[activity.end_note.id]
            if i > 0:
                start = boundary[i - 1].timestamp
            elif activity.event is not None and activity.event.start < end:
                start = activity.event.start
                warnings.append(
                    f"{activity.summary!r} only has an end note and no earlier note, so it keeps "
                    "its planned start"
                )
            else:
                problems.append(
                    f"{activity.summary!r} is ended by note {activity.end_note.id} but nothing says "
                    "when it began; add a start note, or mark that note ambiguous"
                )
                continue
        if end <= start:
            problems.append(
                f"{activity.summary!r} would run from {start.isoformat()} to {end.isoformat()}, "
                "which isn't a positive length -- two notes probably share a timestamp"
            )
            continue
        activity.start, activity.end = start, end


def _merge_into_facts(activities: dict[str, _Activity], warnings: list[str]) -> list[_Fact]:
    ordered = sorted(
        activities.values(), key=lambda a: (a.start, a.event is None, a.end)
    )
    facts: list[_Fact] = []
    for activity in ordered:
        if facts and activity.start < facts[-1].end:
            facts[-1].members.append(activity)
            facts[-1].end = max(facts[-1].end, activity.end)
        else:
            facts.append(_Fact(start=activity.start, end=activity.end, members=[activity]))
    for fact in facts:
        if len(fact.members) > 1:
            names = _joined_summary(fact.members)
            warnings.append(
                f"the notes put {len(fact.members)} activities at the same time, so they were "
                f"merged into one event, {names!r} ({fact.start.isoformat()} to "
                f"{fact.end.isoformat()})"
            )
    return facts


def _joined_summary(members: list[_Activity]) -> str:
    names: list[str] = []
    for member in members:
        if member.summary and member.summary not in names:
            names.append(member.summary)
    return " and ".join(names)


def _build_fact_events(
    facts: list[_Fact],
    ordered: list[PlanNote],
    by_note: dict[str, NoteDisposition],
    warnings: list[str],
) -> None:
    for fact in facts:
        planned = [m for m in fact.members if m.event is not None]
        summary = _joined_summary(fact.members)
        if planned:
            fact.base = planned[0].event
            event = replace(fact.base)
        else:
            event = Event(event_label_id=next((m.label_id for m in fact.members if m.label_id), None))
        event.summary = summary
        event.start = fact.start
        event.end = fact.end
        event.is_fixed_time = True
        event.min_duration = fact.end - fact.start
        fact.event = event

    for note in ordered:
        effects = by_note[note.id].effects
        if not effects or effects[0].kind != "marker" or not (note.description or "").strip():
            continue
        target = next((f for f in facts if f.start <= note.timestamp < f.end), None)
        if target is None:
            target = next((f for f in reversed(facts) if f.end <= note.timestamp), None)
        if target is None:
            warnings.append(
                f"marker note {note.id} ({note.description!r}) comes before every activity, so its "
                "text wasn't attached to any event"
            )
            continue
        local = note.timestamp.astimezone(target.start.tzinfo)
        line = f"- {local.strftime('%H:%M')} {note.description.strip()}"
        current = target.event.description
        if current and "\nNotes:\n" in current:
            target.event.description = f"{current}\n{line}"
        elif current:
            target.event.description = f"{current}\n\nNotes:\n{line}"
        else:
            target.event.description = f"Notes:\n{line}"


def _simulate(
    facts: list[_Fact],
    day_events: list[Event],
    events_by_id: dict[str, Event],
    now: datetime,
    options: ReallocationOptions,
    warnings: list[str],
) -> CompactionPlan:
    originals = {e.id: replace(e) for e in day_events if e.id}
    copies = {e.id: replace(e) for e in day_events if e.id and e.status != "cancelled"}

    mapped_ids = {m.event.id for f in facts for m in f.members if m.event}
    base_ids = {f.base.id for f in facts if f.base}
    span_start = facts[0].start
    span_end = facts[-1].end

    explicit_cancel: dict[str, str] = {}
    for fact in facts:
        for member in fact.members:
            if member.event is not None and member.event.id != (fact.base.id if fact.base else None):
                explicit_cancel[member.event.id] = (
                    f"merged into {fact.event.summary!r}, which the notes show as one activity"
                )
    left_alone: list[str] = []
    for event in events_by_id.values():
        if event.id in mapped_ids or event.is_end_of_day_sleep:
            continue
        if event.end > now:
            continue
        if event.start >= span_start and event.end <= span_end:
            explicit_cancel[event.id] = "no note accounts for it, and it's in the past"
        else:
            left_alone.append(event.summary or event.id)
    if left_alone:
        warnings.append(
            "planned events in the past but outside the noted span were left alone: "
            + ", ".join(repr(name) for name in left_alone)
        )

    working = sorted(
        (
            copies[event_id]
            for event_id in copies
            if event_id not in base_ids and event_id not in explicit_cancel
        ),
        key=lambda e: e.start,
    )
    for fact in facts:
        # reallocate_for_new_event wants only the day *from* the new event's
        # start onward (at most the first event may overlap that start), so
        # whatever already ended before this fact -- including every
        # earlier fact -- is set aside and can't be disturbed.
        head = [e for e in working if e.end <= fact.start]
        tail = [e for e in working if e.end > fact.start]
        try:
            changed = reallocate_for_new_event(tail, fact.event, options)
        except ValueError as exc:
            raise CompactionError(
                f"can't fit {fact.event.summary!r} ({fact.start.isoformat()} to "
                f"{fact.end.isoformat()}) into the day: {exc}. A day needs an event after the "
                "last noted time (normally the end-of-day sleep event) for the rest to reflow into."
            ) from exc
        known = {id(e) for e in tail}
        alive = [e for e in tail if e.status != "cancelled"]
        extra = [e for e in changed if id(e) not in known and e.status != "cancelled"]
        working = head + sorted(alive + extra, key=lambda e: e.start)

    for fact in facts:
        if fact.event.status == "cancelled" or (fact.event.start, fact.event.end) != (fact.start, fact.end):
            raise CompactionError(
                f"couldn't keep {fact.event.summary!r} at {fact.start.isoformat()} to "
                f"{fact.end.isoformat()} while reflowing the rest of the day"
            )

    changes: list[CompactionChange] = []
    fact_by_base = {f.base.id: f for f in facts if f.base}
    for event_id, before in originals.items():
        if before.status == "cancelled":
            continue
        if event_id in fact_by_base:
            fact = fact_by_base[event_id]
            changes.append(
                CompactionChange(
                    action="update",
                    event_id=event_id,
                    reason="recorded as what actually happened, and pinned in place",
                    before=EventState.from_event(before),
                    after=EventState.from_event(fact.event),
                )
            )
        elif event_id in explicit_cancel:
            changes.append(
                CompactionChange(
                    action="cancel",
                    event_id=event_id,
                    reason=explicit_cancel[event_id],
                    before=EventState.from_event(before),
                )
            )
        else:
            after = copies[event_id]
            if after.status == "cancelled":
                changes.append(
                    CompactionChange(
                        action="cancel",
                        event_id=event_id,
                        reason="no room was left for it once the actual events were placed",
                        before=EventState.from_event(before),
                    )
                )
            elif (after.start, after.end) != (before.start, before.end):
                changes.append(
                    CompactionChange(
                        action="update",
                        event_id=event_id,
                        reason="moved to make room for the actual events",
                        before=EventState.from_event(before),
                        after=EventState.from_event(after),
                    )
                )
    fact_events = {id(f.event) for f in facts}
    for fact in facts:
        if fact.base is None:
            changes.append(
                CompactionChange(
                    action="create",
                    reason="something the notes show happened that wasn't planned",
                    after=EventState.from_event(fact.event),
                )
            )
    for event in working:
        if event.id is None and id(event) not in fact_events:
            changes.append(
                CompactionChange(
                    action="create",
                    reason="the remainder of an event that an actual event split in two",
                    after=EventState.from_event(event),
                )
            )

    order = {"cancel": 0, "update": 1, "create": 2}
    changes.sort(
        key=lambda c: (
            order[c.action],
            (c.after or c.before).start,
            c.event_id or "",
        )
    )
    return CompactionPlan(changes=changes, warnings=warnings)
