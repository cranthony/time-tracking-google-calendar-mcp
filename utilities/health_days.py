"""How a day's goal health is kept on the Goal Health calendar: every
assessment of the day, and its reflection, in one all-day "day" event --
or, if they don't all fit in one, a few, titled (1/X), (2/X) and so on.

**Fields.** Everything is in the event's private extended properties: the
day (`period`), which part it is of how many, the reflection's journal,
intentions, whether it's complete and when it was last recorded, and one
property per assessment, `a.<goal id>`, holding it as JSON. Calendar
silently truncates a value over 1024 characters, so a longer one (a long
rationale, say) continues in `a.<goal id>.1`, `.2` and so on, every 1000
bytes; the journal the same. The title and description are only for looking at in Google
Calendar: the overall goal's rating in the title, and a line per goal in
the description.

**Parts.** Calendar allows an event up to 300 properties of 32 kB (keys
and values) in all. A day's assessments are packed into its first event,
after the reflection, in the order given (the goal tree's), and those that
don't fit go on in a second, and so on; each part's id encodes the day and
its number. Every write replaces the day's events in full, and deletes any
parts it no longer needs. One assessment takes at most about 10 kB (see
utilities/goal_health.py's limits), so it always fits in a part.

**Checking.** Since Calendar drops or truncates what's too big without
saying so, each write compares the properties Calendar returns with those
sent, and raises if they differ.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from calendar_clients.google_calendar import MAX_DESCRIPTION_BYTES, CalendarClient

PREFIX = "cascading-time-tracker-"
"""The same prefix calendar_clients/google_calendar.py gives this app's
own extended properties on events."""

KIND = "health-day"
SCHEMA = "1"

PART_BUDGET_BYTES = 30_000
"""How much of Calendar's 32 kB of properties (keys and values, as UTF-8)
one part may use, leaving room for error."""

PART_MAX_PROPERTIES = 280
"""Under Calendar's 300 properties per event."""

_CHUNK_BYTES = 1000
"""Under Calendar's 1024 characters per property value."""

Rating = int | Literal["skip"]
Method = Literal["metric", "subjective", "llm", "rollup"]
Status = Literal["proposed", "confirmed"]


@dataclass(kw_only=True)
class Assessment:
    """One health rating of one goal for one day."""

    goal_id: str
    day: date
    """The day rated -- from waking on it to waking the next."""

    rating: int | Literal["skip"]
    """0-100, or "skip" for a day that was deliberately not rated."""

    method: Method
    status: Status = "proposed"
    """Read-only on input: recording always proposes; only a reflection
    confirms."""

    explanation: str | None = None
    """One line saying how a measured (or carried-over) rating was
    reached."""

    metrics: dict[str, Any] | None = None
    """The measured values behind it. A subjective rating carried over
    from an earlier day, rather than asked for, has "carried_from": that
    day."""

    rationale: str | None = None
    """Anything said about it, by the user or the model."""

    assessed: datetime | None = None
    """Read-only: when it was last written."""

    @property
    def carried(self) -> bool:
        """A subjective rating carried over, rather than given."""
        return bool(self.metrics and self.metrics.get("carried_from"))

    @property
    def unmet(self) -> bool:
        """A skip of a day without an event its measure's `only_if`
        needs."""
        return self.rating == "skip" and bool(self.metrics and self.metrics.get("only_if"))


@dataclass(kw_only=True)
class DayReflection:
    """A day's reflection: see utilities/reflection.py."""

    journal: str | None = None
    intentions: list[str] = field(default_factory=list)
    complete: bool = False
    """Whether every rated goal had been rated when it was last recorded."""

    reflected: datetime | None = None
    """When it was last recorded."""


@dataclass(kw_only=True)
class HealthDay:
    """Everything kept about one day's goal health."""

    day: date
    assessments: dict[str, Assessment] = field(default_factory=dict)
    """By goal id."""

    reflection: DayReflection | None = None
    parts: int = 0
    """How many events it's kept in now: 0 for a day with none yet."""


def band(rating: Rating | None) -> str:
    """The rating's color band, as an emoji: 0-39 red, 40-69 yellow,
    70-100 green."""
    if not isinstance(rating, int):
        return "⚪"
    return "🔴" if rating < 40 else "🟡" if rating < 70 else "🟢"


def day_event_id(day: date, part: int) -> str:
    """The deterministic event id of the `part`th (from 1) event of `day`:
    `health-day|day|part` in lowercase base32hex, Calendar's own event-id
    alphabet."""
    encoded = base64.b32hexencode(f"{KIND}|{day.isoformat()}|{part}".encode()).decode()
    return encoded.rstrip("=").lower()


class HealthDays:
    """The day events on a Goal Health calendar. `calendar` gives the
    calendar, creating it if asked to and it doesn't exist yet (`None` if
    not)."""

    def __init__(self, calendar: Callable[[bool], CalendarClient | None]) -> None:
        self._calendar = calendar

    def read(self, first: date, end: date, tz) -> dict[date, HealthDay]:
        """The days from `first` up to `end` (exclusive) with anything
        kept, by day."""
        calendar = self._calendar(False)
        if calendar is None:
            return {}
        items = calendar.list_event_resources(
            datetime.combine(first, time(), tz),
            datetime.combine(end, time(), tz),
            private_property=f"{PREFIX}kind={KIND}",
        )
        return {d: day for d, day in decode(items).items() if first <= d < end}

    def write(self, day: HealthDay, names: dict[str, str], overall_id: str) -> HealthDay:
        """Replace `day`'s events with what it holds now, deleting any
        parts it no longer needs (`day.parts` says how many it had), and
        return it with its new count. `names` gives the goals' names
        (paths) for the description. Raises ValueError if Calendar didn't
        keep every property as sent."""
        calendar = self._calendar(True)
        bodies = encode(day, names, overall_id)
        for part, body in enumerate(bodies, 1):
            response = calendar.replace_event_resource(day_event_id(day.day, part), body)
            kept = response.get("extendedProperties", {}).get("private", {})
            if kept != body["extendedProperties"]["private"]:
                raise ValueError(
                    f"Calendar didn't keep {day.day}'s goal health as written (part {part} of {len(bodies)})"
                )
        for part in range(len(bodies) + 1, day.parts + 1):
            calendar.delete_event_resource(day_event_id(day.day, part))
        return HealthDay(day=day.day, assessments=day.assessments, reflection=day.reflection, parts=len(bodies))


# -- encoding --------------------------------------------------------------------


def encode(day: HealthDay, names: dict[str, str], overall_id: str) -> list[dict]:
    """`day` as the bodies of its events, in order -- see the module
    docstring."""
    reflection = _reflection_properties(day.reflection) if day.reflection else []
    entries = [(goal_id, _chunks(f"a.{goal_id}", _json(_to_json(a)))) for goal_id, a in day.assessments.items()]
    # Each part's own fields, with room for any part number and count.
    overhead = _size(_base_properties(day.day, 99, 99))
    packed: list[tuple[list[tuple[str, str]], list[str]]] = []
    properties, goal_ids = list(reflection), []
    for goal_id, chunks in entries:
        fits = (
            overhead + _size(properties + chunks) <= PART_BUDGET_BYTES
            and len(properties) + len(chunks) + 5 <= PART_MAX_PROPERTIES
        )
        if not fits and (properties or goal_ids):
            packed.append((properties, goal_ids))
            properties, goal_ids = [], []
        properties += chunks
        goal_ids.append(goal_id)
    packed.append((properties, goal_ids))
    count = len(packed)
    overall = day.assessments.get(overall_id)
    bodies = []
    for part, (properties, goal_ids) in enumerate(packed, 1):
        lines = [_line(day.assessments[g], names.get(g, g)) for g in goal_ids]
        journal = day.reflection.journal if day.reflection and part == 1 else None
        bodies.append(
            {
                "summary": _summary(day, overall, part, count),
                "description": _description(journal, lines),
                "start": {"date": day.day.isoformat()},
                "end": {"date": (day.day + timedelta(days=1)).isoformat()},
                "transparency": "transparent",
                "status": "confirmed",
                "extendedProperties": {
                    "private": {f"{PREFIX}{k}": v for k, v in _base_properties(day.day, part, count) + properties}
                },
            }
        )
    return bodies


def decode(items: list[dict]) -> dict[date, HealthDay]:
    """The days kept in `items` (a day's events, from any range), by day."""
    by_day: dict[date, dict[str, str]] = {}
    parts: dict[date, int] = {}
    for item in items:
        if item.get("status") == "cancelled":
            continue
        properties = {
            key.removeprefix(PREFIX): value
            for key, value in item.get("extendedProperties", {}).get("private", {}).items()
            if key.startswith(PREFIX)
        }
        if properties.get("kind") != KIND:
            continue
        try:
            day = date.fromisoformat(properties.get("period", ""))
        except ValueError:
            continue
        by_day.setdefault(day, {}).update(properties)
        parts[day] = max(parts.get(day, 0), int(properties.get("parts") or 1))
    days = {}
    for day, properties in sorted(by_day.items()):
        assessments = {}
        for key in properties:
            name, _, goal_id = key.partition(".")
            if name == "a" and goal_id and "." not in goal_id:
                assessments[goal_id] = _from_json(goal_id, day, json.loads(_joined(properties, key)))
        reflection = None
        if "complete" in properties:
            reflected = properties.get("reflected")
            reflection = DayReflection(
                journal=_joined(properties, "journal") or None,
                intentions=[str(i) for i in json.loads(properties.get("intentions") or "[]")],
                complete=properties["complete"] == "true",
                reflected=datetime.fromisoformat(reflected) if reflected else None,
            )
        days[day] = HealthDay(day=day, assessments=assessments, reflection=reflection, parts=parts[day])
    return days


def _base_properties(day: date, part: int, count: int) -> list[tuple[str, str]]:
    return [
        ("kind", KIND),
        ("period", day.isoformat()),
        ("part", str(part)),
        ("parts", str(count)),
        ("schema", SCHEMA),
    ]


def _reflection_properties(reflection: DayReflection) -> list[tuple[str, str]]:
    properties = [
        ("intentions", _json(reflection.intentions)),
        ("complete", "true" if reflection.complete else "false"),
    ]
    if reflection.reflected:
        properties.append(("reflected", reflection.reflected.isoformat()))
    if reflection.journal:
        properties += _chunks("journal", reflection.journal)
    return properties


def _to_json(assessment: Assessment) -> dict[str, Any]:
    fields = {
        "rating": assessment.rating,
        "method": assessment.method,
        "status": assessment.status,
        "explanation": assessment.explanation,
        "metrics": assessment.metrics,
        "rationale": assessment.rationale,
        "assessed": assessment.assessed.isoformat() if assessment.assessed else None,
    }
    return {k: v for k, v in fields.items() if v is not None}


def _from_json(goal_id: str, day: date, fields: dict[str, Any]) -> Assessment:
    assessed = fields.get("assessed")
    return Assessment(
        goal_id=goal_id,
        day=day,
        rating=fields["rating"],
        method=fields["method"],
        status=fields.get("status", "proposed"),
        explanation=fields.get("explanation"),
        metrics=fields.get("metrics"),
        rationale=fields.get("rationale"),
        assessed=datetime.fromisoformat(assessed) if assessed else None,
    )


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _chunks(key: str, text: str) -> list[tuple[str, str]]:
    """`text` under `key`, continuing in `key.1`, `key.2`, ... every
    _CHUNK_BYTES -- of UTF-8, which is never fewer than the characters
    Calendar counts, however it counts them -- without splitting a
    character."""
    pieces, piece, size = [], [], 0
    for char in text:
        length = len(char.encode())
        if size + length > _CHUNK_BYTES:
            pieces.append("".join(piece))
            piece, size = [], 0
        piece.append(char)
        size += length
    pieces.append("".join(piece))
    return [(key if i == 0 else f"{key}.{i}", p) for i, p in enumerate(pieces)]


def _joined(properties: dict[str, str], key: str) -> str:
    """What `_chunks` split up under `key`, back together."""
    pieces = []
    i = 0
    while (name := key if i == 0 else f"{key}.{i}") in properties:
        pieces.append(properties[name])
        i += 1
    return "".join(pieces)


def _size(properties: list[tuple[str, str]]) -> int:
    return sum(len(f"{PREFIX}{k}".encode()) + len(v.encode()) for k, v in properties)


def _shown(assessment: Assessment) -> str:
    rating = assessment.rating
    shown = "skipped" if rating == "skip" else str(rating)
    return shown + (" (proposed)" if assessment.status == "proposed" else "")


def _summary(day: HealthDay, overall: Assessment | None, part: int, count: int) -> str:
    title = f"📝 Reflection · {day.day.isoformat()}" if day.reflection else f"📊 Goal health · {day.day.isoformat()}"
    if overall is not None:
        title += f" · {band(overall.rating)} {_shown(overall)}"
    if day.reflection and not day.reflection.complete:
        title += " (in progress)"
    return title + (f" ({part}/{count})" if count > 1 else "")


def _line(assessment: Assessment, name: str) -> str:
    said = assessment.explanation or assessment.rationale
    return f"{band(assessment.rating)} {_shown(assessment)} {name}" + (f" — {said}" if said else "")


def _description(journal: str | None, lines: list[str]) -> str:
    """For looking at only, so cut short to fit Calendar's limit."""
    text = "\n\n".join(p for p in (journal, "\n".join(lines)) if p)
    encoded = text.encode()
    if len(encoded) <= MAX_DESCRIPTION_BYTES:
        return text
    return encoded[: MAX_DESCRIPTION_BYTES - 4].decode(errors="ignore") + "…"
