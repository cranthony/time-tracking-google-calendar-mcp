"""Reflections: a conversation, run by the MCP client, that confirms the
health assessments due for one period of one cadence -- a day, a
Sunday-Saturday week, a month or two months -- and may record a short
journal and a few intentions. See docs/goals-design.md section 11.

`prepare_reflection` gathers what the conversation needs (read-only);
`record_reflection` previews, then commits. Committing is the only way an
assessment becomes `confirmed` (utilities/goal_health.py's
`confirm_assessments`), and it writes a reflection event beside the
assessments on the Goal Health calendar: an all-day event spanning the
period, its description the journal, its private extended properties the
cadence, period and intentions. Its id encodes the cadence and period, so
recording a period's reflection again replaces it.

A reflection at a cadence *rates* only the active goals with that
cadence; a longer one also *reviews* the shorter-cadence goals' confirmed
ratings within its period, without rating them again (each goal is
assessed at exactly one cadence).
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from calendar_clients.google_calendar import Event
from utilities.goal_calendar import fill_in_from_goals
from utilities.goal_health import (
    MAX_RATIONALE_BYTES,
    Assessment,
    GoalHealth,
    band,
)
from utilities.goal_periods import Period, last_ended, parse_period, period_containing
from utilities.goal_sheet import CADENCES, Cadence, Goal
from utilities.goals import Goals, GoalTree
from utilities.noted_time_sheet import NotedTimeSheet

_PREFIX = "cascading-time-tracker-"

MAX_INTENTIONS = 3

_LOOKBACK_PERIODS = 12
"""How far back `prepare_reflection` looks for a period still to reflect on."""

_RECENT_RATINGS = 6

_DIGESTED_CADENCES = frozenset({"daily", "weekly"})
"""Cadences short enough for the context to list the period's events and
notes; longer ones rely on the minutes per goal."""


@dataclass(kw_only=True)
class PastRating:
    period: str
    rating: int | Literal["skip"]


@dataclass(kw_only=True)
class DueGoal:
    """A goal the reflection rates."""

    goal_id: str
    path: str
    measure: dict[str, Any] | None = None
    target: str | None = None
    deadline: date | None = None
    note: str | None = None
    recent: list[PastRating] = field(default_factory=list)
    """Its last confirmed ratings at this cadence before this period."""

    proposed: Assessment | None = None
    """What to start from: a rating recorded earlier for this period, or
    one measured from the calendar (with its explanation); `None` for a
    goal to ask about (subjective) or judge (llm)."""


@dataclass(kw_only=True)
class ReviewedGoal:
    """A shorter-cadence goal, for a look at its trend; not rated again."""

    goal_id: str
    path: str
    cadence: Cadence
    ratings: list[PastRating] = field(default_factory=list)
    """Its confirmed ratings within the period."""

    mean: int | None = None


@dataclass(kw_only=True)
class GoalTime:
    goal_id: str
    path: str
    minutes: int
    """Minutes of events serving the goal or any of its sub-goals."""


@dataclass(kw_only=True)
class ReflectionContext:
    cadence: Cadence
    period: str
    first_day: date
    last_day: date
    already_reflected: bool
    """A reflection has already been recorded for this period; recording
    again replaces it."""

    journal: str | None = None
    """The journal recorded with it, if so."""

    due: list[DueGoal] = field(default_factory=list)
    reviewed: list[ReviewedGoal] = field(default_factory=list)
    goal_time: list[GoalTime] = field(default_factory=list)
    events_digest: str | None = None
    """The period's events, day by day, with their goals (daily and weekly
    reflections only)."""

    notes_digest: str | None = None
    """The period's compacted notes (daily and weekly reflections only)."""

    previous_intentions: list[str] = field(default_factory=list)
    """From the previous reflection at this cadence."""

    uncompacted_notes: int = 0
    """Notes up to the period's end that haven't been compacted: until
    they are, the calendar (and so the measured ratings) may be off."""

    instructions: str = ""


@dataclass(kw_only=True)
class ReflectionResult:
    status: Literal["preview", "recorded"]
    cadence: Cadence
    period: str
    preview: str
    """One line per rating, and the journal and intentions."""

    assessments: list[Assessment] = field(default_factory=list)
    not_rated: list[str] = field(default_factory=list)
    """Paths of goals due but not rated (they stay unassessed)."""

    message: str = ""


def reflection_event_id(cadence: str, period: str) -> str:
    encoded = base64.b32hexencode(f"reflection|{cadence}|{period}".encode()).decode()
    return encoded.rstrip("=").lower()


class Reflections:
    def __init__(self, health: GoalHealth, goals: Goals, notes: NotedTimeSheet | None = None) -> None:
        self._health = health
        self._goals = goals
        self._notes = notes

    # -- preparing --------------------------------------------------------------

    def prepare(self, cadence: Cadence, period: str | None = None) -> ReflectionContext:
        _check_cadence(cadence)
        tree = self._goals.tree()
        today = self._health.now().date()
        span = parse_period(cadence, period) if period else self._next_unreflected(cadence, tree, today)
        if span.start > today:
            raise ValueError(f"{span.id} hasn't started yet")
        reflected = self._reflection(cadence, span)
        previous = self._reflection(cadence, span.previous())

        rated = [g for g in tree.ordered() if g.active and g.cadence == cadence]
        recorded = {
            a.goal_id: a
            for a in self._health.history([g.id for g in rated], cadence, span.start, span.end - timedelta(days=1))
            if a.period == span.id
        } if rated else {}
        measured = {a.goal_id: a for a in self._health.measure(cadence, span.id)} if rated else {}
        due = []
        for goal in rated:
            before = [
                a for a in self._health.history(
                    [goal.id], cadence, _periods_back(span, _RECENT_RATINGS).start, span.start - timedelta(days=1)
                )
                if a.status == "confirmed"
            ]
            due.append(
                DueGoal(
                    goal_id=goal.id,
                    path=tree.path(goal.id),
                    measure=goal.measure,
                    target=goal.target,
                    deadline=goal.deadline,
                    note=goal.note,
                    recent=[PastRating(period=a.period, rating=a.rating) for a in before[-_RECENT_RATINGS:]],
                    proposed=recorded.get(goal.id) or measured.get(goal.id),
                )
            )

        shorter = CADENCES[: CADENCES.index(cadence)]
        reviewed = []
        for goal in tree.ordered():
            if not (goal.active and goal.cadence in shorter):
                continue
            ratings = [
                a for a in self._health.history([goal.id], goal.cadence, span.start, span.end - timedelta(days=1))
                if a.status == "confirmed" and span.contains(parse_period(a.cadence, a.period).start)
            ]
            numbers = [a.rating for a in ratings if isinstance(a.rating, int)]
            reviewed.append(
                ReviewedGoal(
                    goal_id=goal.id,
                    path=tree.path(goal.id),
                    cadence=goal.cadence,
                    ratings=[PastRating(period=a.period, rating=a.rating) for a in ratings],
                    mean=round(sum(numbers) / len(numbers)) if numbers else None,
                )
            )

        events = self._events(span, tree)
        notes = self._notes.read(include_compacted=True) if self._notes else []
        tz = self._health.now().tzinfo
        end = datetime.combine(span.end, time(), tz)
        digested = cadence in _DIGESTED_CADENCES
        return ReflectionContext(
            cadence=cadence,
            period=span.id,
            first_day=span.start,
            last_day=span.end - timedelta(days=1),
            already_reflected=reflected is not None,
            journal=(reflected or {}).get("description") or None,
            due=due,
            reviewed=reviewed,
            goal_time=_goal_time(events, tree, span, tz),
            events_digest=_events_digest(events, tree, span, tz) if digested else None,
            notes_digest=_notes_digest(notes, span, tz) if digested and self._notes else None,
            previous_intentions=_intentions(previous),
            uncompacted_notes=sum(1 for n in notes if n.compaction_id is None and n.timestamp < end),
            instructions=_instructions(cadence, span),
        )

    def _next_unreflected(self, cadence: str, tree: GoalTree, today: date) -> Period:
        """The oldest period, of the last few that have ended, with no
        reflection yet -- or the last one to end, if they all have one."""
        last = last_ended(cadence, today)
        created = [g.created for g in tree.goals if g.cadence == cadence and g.created and g.status != "deleted"]
        first = _periods_back(last, _LOOKBACK_PERIODS - 1)
        if created:
            first = max(first, period_containing(cadence, min(created)), key=lambda p: p.start)
        else:
            first = last
        reflected = {r["period"] for r in self._reflections(cadence, first.start, last.end)}
        period = first
        while period.start <= last.start:
            if period.id not in reflected:
                return period
            period = period.next()
        return last

    # -- recording --------------------------------------------------------------

    def record(
        self,
        cadence: Cadence,
        period: str,
        assessments: list[Assessment],
        journal: str | None = None,
        intentions: list[str] | None = None,
        *,
        dry_run: bool = True,
    ) -> ReflectionResult:
        _check_cadence(cadence)
        span = parse_period(cadence, period)
        if span.start > self._health.now().date():
            raise ValueError(f"{span.id} hasn't started yet")
        problems = []
        seen = set()
        for a in assessments:
            if (a.cadence, a.period) != (cadence, span.id):
                problems.append(f"{a.goal_id}: this reflection rates {cadence} {span.id}, not {a.cadence} {a.period}")
            if a.goal_id in seen:
                problems.append(f"{a.goal_id} is rated more than once")
            seen.add(a.goal_id)
        if journal and len(journal.encode()) > MAX_RATIONALE_BYTES:
            problems.append(f"the journal is longer than {MAX_RATIONALE_BYTES} bytes (Calendar would cut it short)")
        intentions = [i.strip() for i in intentions or [] if i.strip()]
        if len(intentions) > MAX_INTENTIONS:
            problems.append(f"at most {MAX_INTENTIONS} intentions")
        elif len(json.dumps(intentions)) > 1024:
            problems.append("the intentions are too long together (1024 characters as JSON)")
        if problems:
            raise ValueError("; ".join(problems))
        self._health.check(assessments)

        tree = self._goals.tree()
        due = [g for g in tree.ordered() if g.active and g.cadence == cadence]
        not_rated = [tree.path(g.id) for g in due if g.id not in seen]
        preview = _preview(assessments, tree, journal, intentions, not_rated)
        if dry_run:
            return ReflectionResult(
                status="preview",
                cadence=cadence,
                period=span.id,
                preview=preview,
                assessments=assessments,
                not_rated=not_rated,
                message=(
                    "Nothing has been recorded yet. Show the user the preview; once they agree, call "
                    "record_reflection again with the same arguments and dry_run=False. Goals not rated "
                    "stay unassessed for this period (rate them \"skip\" to say so on purpose)."
                ),
            )
        confirmed = self._health.confirm_assessments(assessments)
        self._write_reflection(span, journal, intentions, len(confirmed))
        return ReflectionResult(
            status="recorded",
            cadence=cadence,
            period=span.id,
            preview=preview,
            assessments=confirmed,
            not_rated=not_rated,
            message=f"Recorded the {cadence} reflection for {span.id}: {len(confirmed)} rating(s) confirmed.",
        )

    def _write_reflection(self, span: Period, journal: str | None, intentions: list[str], rated: int) -> None:
        calendar = self._health.health_calendar()
        properties = {
            "kind": "reflection",
            "cadence": span.cadence,
            "period": span.id,
            "intentions": json.dumps(intentions),
            "rated": str(rated),
            "reflected": self._health.now().isoformat(),
            "schema": "1",
        }
        body = {
            "summary": f"📝 {span.cadence.replace('_', ' ')} reflection · {span.id}",
            "description": journal or "",
            "start": {"date": span.start.isoformat()},
            "end": {"date": span.end.isoformat()},
            "transparency": "transparent",
            "extendedProperties": {"private": {f"{_PREFIX}{k}": v for k, v in properties.items()}},
        }
        response = calendar.upsert_event_resource(reflection_event_id(span.cadence, span.id), body)
        if len(response.get("description", "")) < len(body["description"]):
            raise ValueError(f"Calendar shortened the journal; keep it under {MAX_RATIONALE_BYTES} bytes")

    # -- reading ------------------------------------------------------------------

    def _reflections(self, cadence: str, first: date, end: date) -> list[dict]:
        """The reflection events at `cadence` overlapping `first`..`end`
        (exclusive), as dicts with their properties unprefixed plus the
        description."""
        calendar = self._health.health_calendar(create=False)
        if calendar is None:
            return []
        tz = self._health.now().tzinfo
        items = calendar.list_event_resources(
            datetime.combine(first, time(), tz),
            datetime.combine(end, time(), tz),
            private_property=f"{_PREFIX}kind=reflection",
        )
        found = []
        for item in items:
            if item.get("status") == "cancelled":
                continue
            properties = {
                key.removeprefix(_PREFIX): value
                for key, value in item.get("extendedProperties", {}).get("private", {}).items()
            }
            if properties.get("cadence") == cadence:
                found.append({**properties, "description": item.get("description")})
        return found

    def _reflection(self, cadence: str, span: Period) -> dict | None:
        return next((r for r in self._reflections(cadence, span.start, span.end) if r.get("period") == span.id), None)

    def _events(self, span: Period, tree: GoalTree) -> list[Event]:
        tz = self._health.now().tzinfo
        listed = self._health.calendar_client.list_events(datetime.combine(span.start, time(), tz), datetime.combine(span.end, time(), tz))
        return [e for e in fill_in_from_goals(listed, tree) if e.status != "cancelled"]


# -- helpers --------------------------------------------------------------------


def _check_cadence(cadence: str) -> None:
    if cadence not in CADENCES:
        raise ValueError(f"Unknown cadence {cadence!r}; cadences are {', '.join(CADENCES)}")


def _periods_back(period: Period, count: int) -> Period:
    for _ in range(count):
        period = period.previous()
    return period


def _intentions(reflection: dict | None) -> list[str]:
    if not reflection or not reflection.get("intentions"):
        return []
    try:
        return [str(i) for i in json.loads(reflection["intentions"])]
    except ValueError:
        return []


def _goal_time(events: list[Event], tree: GoalTree, span: Period, tz) -> list[GoalTime]:
    start = datetime.combine(span.start, time(), tz)
    end = datetime.combine(span.end, time(), tz)
    minutes: dict[str, float] = {}
    for event in events:
        overlap = (min(event.end, end) - max(event.start, start)).total_seconds() / 60
        if overlap <= 0:
            continue
        served = set()
        for goal_id in event.goal_ids or ():
            served.update(g.id for g in tree.chain(goal_id))
        for goal_id in served:
            minutes[goal_id] = minutes.get(goal_id, 0) + overlap
    times = [
        GoalTime(goal_id=goal_id, path=tree.path(goal_id), minutes=round(total))
        for goal_id, total in minutes.items()
        if goal_id in tree.by_id and round(total) > 0
    ]
    return sorted(times, key=lambda t: (-t.minutes, t.path))


def _events_digest(events: list[Event], tree: GoalTree, span: Period, tz) -> str:
    start = datetime.combine(span.start, time(), tz)
    end = datetime.combine(span.end, time(), tz)
    lines = []
    day = None
    for event in sorted(events, key=lambda e: e.start):
        if event.end <= start or event.start >= end:
            continue
        local = event.start.astimezone(tz)
        if local.date() != day:
            day = local.date()
            lines.append(f"{local:%a %m-%d}")
        goals = ", ".join(tree.by_id[g].name for g in event.goal_ids or () if g in tree.by_id)
        lines.append(
            f"  {local:%H:%M}-{event.end.astimezone(tz):%H:%M} {event.summary or '(no title)'}"
            + (f" [{goals}]" if goals else "")
        )
    return "\n".join(lines) or "(no events)"


def _notes_digest(notes, span: Period, tz) -> str:
    start = datetime.combine(span.start, time(), tz)
    end = datetime.combine(span.end, time(), tz)
    lines = [
        f"{n.timestamp.astimezone(tz):%a %m-%d %H:%M} {n.description}"
        for n in sorted(notes, key=lambda n: n.timestamp)
        if start <= n.timestamp < end and n.description
    ]
    return "\n".join(lines) or "(no notes)"


def _preview(
    assessments: list[Assessment], tree: GoalTree, journal: str | None, intentions: list[str], not_rated: list[str]
) -> str:
    lines = []
    for a in assessments:
        name = tree.path(a.goal_id) if a.goal_id in tree.by_id else a.goal_id
        rating = "skipped" if a.rating == "skip" else str(a.rating)
        why = " — ".join(part for part in (a.explanation, a.rationale) if part)
        lines.append(f"{band(a.rating)} {name}: {rating}" + (f" — {why}" if why else ""))
    for path in not_rated:
        lines.append(f"· {path}: not rated")
    if journal:
        lines.append(f"Journal: {journal}")
    for intention in intentions:
        lines.append(f"Intention: {intention}")
    return "\n".join(lines) or "(nothing to record)"


def _instructions(cadence: str, span: Period) -> str:
    longer = cadence != "daily"
    steps = [
        f"This is the {cadence.replace('_', ' ')} reflection for {span.id}. Ratings are 0-100 "
        "(0-39 red, 40-69 yellow, 70-100 green), or \"skip\".",
        "If uncompacted_notes > 0, say the calendar may not reflect them yet, and offer to compact "
        "notes first.",
        "1. Open with the goals in `due` that have a `proposed` rating: one line each with its band, "
        "rating and explanation. Ask for agreement in bulk; change only what the user objects to, "
        "recording their reason as the rationale.",
        "2. For each due goal without a proposal whose measure is subjective, ask its prompt, one goal "
        "at a time, for a 0-100 number. Accept \"skip\"; turn words like \"pretty good\" into a number "
        "and confirm it.",
        "3. For each due goal with an llm measure, propose a rating with a one-sentence rationale "
        "grounded in events_digest, notes_digest and goal_time, and ask for confirmation.",
    ]
    if longer:
        steps.append(
            "4. Summarize `reviewed` (shorter-cadence goals' ratings in this period) in a line or two, "
            "without rating them."
        )
    steps += [
        "5. If there are previous_intentions, ask whether they happened.",
        f"6. Ask, optionally, for a short journal entry and up to {MAX_INTENTIONS} intentions for the "
        "next period.",
        "7. Call record_reflection with the ratings (method: metric, rollup, subjective or llm; keep a "
        "measured rating's explanation), journal and intentions. It previews first; show the preview, "
        "and only once the user agrees call it again with dry_run=False. Only then are the ratings "
        "confirmed. Keep it brief: a daily reflection should take a couple of minutes.",
    ]
    return "\n".join(steps)
