"""Goal health: assessments of how a goal went over one period of its
cadence, stored on a dedicated Goal Health calendar, and the measurements
that propose them. See docs/goals-design.md sections 6-8.

**Storage.** One all-day event per (goal, cadence, period) on an
app-created calendar -- not the main one, whose events must never
overlap -- spanning the period, with everything structured in private
extended properties (which Calendar can filter on) and the rationale in
its description. Its id encodes the goal, cadence and period, so writing
an assessment again overwrites it instead of adding another. The
calendar's id is kept on the main calendar (`set_calendar_metadata`), and
it's created the first time it's needed, in the main calendar's time zone
so the two agree on what a day is.

**Confirmation.** `record_assessments` only ever writes `proposed`
assessments; only a reflection confirms one (`confirm_assessments`, used
by phase 3's reflection tools), and only confirmed ratings feed a goal's
at-a-glance health: the `health`/`health_period`/`health_trend` cache
columns of the goals tab.

**Measuring.** `measure` proposes a rating for each goal whose `measure`
can be computed from the calendar -- `duration` (minutes of its events),
`count` (how many), `wake_time` (when the day's sleep ended) and `rollup`
(its sub-goals' confirmed ratings) -- each with a one-line `explanation`
of how the number was reached. It never writes anything.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from calendar_clients.google_calendar import CalendarClient, Event
from utilities.goal_calendar import fill_in_from_goals
from utilities.sleep_days import listing_range, period_window
from utilities.goal_periods import Period, last_ended, parse_period, period_containing
from utilities.goal_sheet import GOAL_STATUSES, Cadence, Goal
from utilities.goals import GoalList, Goals, GoalTree

HEALTH_CALENDAR_METADATA_KEY = "goal-health-calendar"
HEALTH_CALENDAR_SUMMARY = "Goal Health"

_PREFIX = "cascading-time-tracker-"
"""The same prefix calendar_clients/google_calendar.py gives this app's
own extended properties on events."""

MAX_RATIONALE_BYTES = 8000
"""Under Calendar's silent description cut-off (MAX_DESCRIPTION_BYTES)."""

_MAX_PROPERTY_CHARS = 1024
"""Calendar's limit on one extended property's value."""

TREND_LENGTH = 8

_CACHE_HISTORY = timedelta(days=3 * 366)
"""How far back the health cache looks for a goal's latest rating."""

Rating = int | Literal["skip"]
Method = Literal["metric", "subjective", "llm", "rollup"]
Status = Literal["proposed", "confirmed"]

_MEASURED_KINDS = frozenset({"duration", "count", "wake_time", "rollup"})


@dataclass(kw_only=True)
class Assessment:
    """One health rating of one goal for one period of its cadence."""

    goal_id: str
    cadence: Cadence
    """The goal's cadence when it was assessed."""

    period: str
    """e.g. "2026-09-30", "week-2026-09-27", "2026-09", "2026-09..10"."""

    rating: int | Literal["skip"]
    """0-100, or "skip" for a period that was deliberately not rated."""

    method: Method
    status: Status = "proposed"
    """Read-only on input: recording always proposes; only a reflection
    confirms."""

    explanation: str | None = None
    """One line saying how a measured rating was reached."""

    metrics: dict[str, Any] | None = None
    """The measured values behind it."""

    rationale: str | None = None
    """Anything said about it, by the user or the model."""

    assessed: datetime | None = None
    """Read-only: when it was last written."""


def band(rating: Rating | None) -> str:
    """The rating's color band, as an emoji: 0-39 red, 40-69 yellow,
    70-100 green."""
    if not isinstance(rating, int):
        return "⚪"
    return "🔴" if rating < 40 else "🟡" if rating < 70 else "🟢"


def assessment_event_id(goal_id: str, cadence: str, period: str) -> str:
    """The deterministic, collision-free event id for one assessment:
    `goal|cadence|period` in lowercase base32hex (Calendar's own event-id
    alphabet), so it decodes back to all three."""
    encoded = base64.b32hexencode(f"{goal_id}|{cadence}|{period}".encode()).decode()
    return encoded.rstrip("=").lower()


class GoalHealth:
    """A calendar's goal assessments -- see the module docstring."""

    def __init__(
        self,
        calendar_client: CalendarClient,
        goals: Goals,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = calendar_client
        self._goals = goals
        self._now = now or (lambda: datetime.now(calendar_client.get_time_zone()))
        self._health_client: CalendarClient | None = None

    # -- writing ------------------------------------------------------------

    def record_assessments(self, assessments: list[Assessment]) -> list[Assessment]:
        """Write `assessments` as proposed (whatever their `status`).
        Validates all of them before writing any."""
        return self._write(assessments, status="proposed")

    def confirm_assessments(self, assessments: list[Assessment]) -> list[Assessment]:
        """Write `assessments` as confirmed, then refresh those goals'
        health cache. For reflections only."""
        written = self._write(assessments, status="confirmed")
        self._refresh_cache({a.goal_id for a in written})
        return written

    def check(self, assessments: list[Assessment]) -> None:
        """Raise ValueError if any of `assessments` couldn't be recorded --
        everything `record_assessments` checks, without writing."""
        tree = self._goals.tree()
        today = self._now().date()
        for assessment in assessments:
            self._check(assessment, tree, today)

    def now(self) -> datetime:
        """Now, in the main calendar's time zone."""
        return self._now()

    @property
    def calendar_client(self) -> CalendarClient:
        """The main calendar."""
        return self._client

    def health_calendar(self, *, create: bool = True) -> CalendarClient | None:
        """The Goal Health calendar, for the reflections kept beside the
        assessments (see utilities/reflection.py); created if need be,
        unless `create` is false (then `None` if there isn't one yet)."""
        return self._health_calendar(create=create)

    def _write(self, assessments: list[Assessment], *, status: Status) -> list[Assessment]:
        tree = self._goals.tree()
        today = self._now().date()
        checked = [(a, self._check(a, tree, today)) for a in assessments]
        health = self._health_calendar()
        written = []
        for assessment, (goal, period) in checked:
            assessment = Assessment(**{**assessment.__dict__, "status": status, "assessed": self._now()})
            body = _event_body(assessment, goal, period)
            response = health.upsert_event_resource(
                assessment_event_id(assessment.goal_id, assessment.cadence, assessment.period), body
            )
            if len(response.get("description", "")) < len(body["description"]):
                raise ValueError(
                    f"Calendar shortened the rationale for {goal.name!r} ({assessment.period}); "
                    f"keep it under {MAX_RATIONALE_BYTES} bytes"
                )
            written.append(_from_event(response))
        return written

    def _check(self, assessment: Assessment, tree: GoalTree, today: date) -> tuple[Goal, Period]:
        tree.check_goal_ids([assessment.goal_id])
        goal = tree.by_id[assessment.goal_id]
        label = f"{goal.name!r} ({assessment.goal_id})"
        if goal.cadence is None:
            raise ValueError(f"{label} has no cadence, so it isn't assessed; give it one first")
        if assessment.cadence != goal.cadence:
            raise ValueError(f"{label} is assessed {goal.cadence}, not {assessment.cadence}")
        period = parse_period(assessment.cadence, assessment.period)
        if period.start > today:
            raise ValueError(f"{assessment.period} hasn't started yet")
        rating = assessment.rating
        if rating != "skip" and not (isinstance(rating, int) and 0 <= rating <= 100):
            raise ValueError(f"{label}: a rating is a whole number from 0 to 100, or \"skip\"")
        if assessment.explanation and len(assessment.explanation) > _MAX_PROPERTY_CHARS:
            raise ValueError(f"{label}: the explanation is longer than {_MAX_PROPERTY_CHARS} characters")
        if assessment.metrics is not None and len(_json(assessment.metrics)) > _MAX_PROPERTY_CHARS:
            raise ValueError(f"{label}: the metrics are longer than {_MAX_PROPERTY_CHARS} characters as JSON")
        if len((assessment.rationale or "").encode()) > MAX_RATIONALE_BYTES:
            raise ValueError(
                f"{label}: the rationale is longer than {MAX_RATIONALE_BYTES} bytes (Calendar would cut it short)"
            )
        return goal, period

    # -- reading ------------------------------------------------------------

    def history(
        self,
        goal_ids: list[str],
        cadence: Cadence | None = None,
        start: date | None = None,
        end: date | None = None,
        *,
        confirmed_only: bool = False,
    ) -> list[Assessment]:
        """`goal_ids`' assessments overlapping `start`..`end` (inclusive),
        by goal then period. The default range is the last 12 periods of
        each goal's own cadence."""
        tree = self._goals.tree()
        tree.check_goal_ids(goal_ids)
        today = self._now().date()
        found: list[Assessment] = []
        for goal_id in dict.fromkeys(goal_ids):
            goal = tree.by_id[goal_id]
            first, last = start, end
            if first is None:
                if goal.cadence is None:
                    continue
                period = period_containing(goal.cadence, today)
                for _ in range(12):
                    period = period.previous()
                first = period.start
            items = self._read(first, (last or today) + timedelta(days=1), goal_id=goal_id)
            found += [
                a for a in items
                if (cadence is None or a.cadence == cadence) and (not confirmed_only or a.status == "confirmed")
            ]
        return found

    def _read(self, first: date, end: date, *, goal_id: str | None = None) -> list[Assessment]:
        """Assessments overlapping `first`..`end` (exclusive), by period."""
        health = self._health_calendar(create=False)
        if health is None:
            return []
        tz = self._client.get_time_zone()
        items = health.list_event_resources(
            datetime.combine(first, time(), tz),
            datetime.combine(end, time(), tz),
            private_property=f"{_PREFIX}goal={goal_id}" if goal_id else None,
        )
        assessments = [a for a in (_from_event(item) for item in items if item.get("status") != "cancelled") if a]
        return sorted(assessments, key=lambda a: (a.goal_id, _period_start(a)))

    # -- measuring ----------------------------------------------------------

    def measure(
        self, cadence: Cadence, period: str | None = None, goal_ids: list[str] | None = None
    ) -> list[Assessment]:
        """Proposed assessments for the active goals with `cadence` whose
        measure can be computed (see the module docstring), for `period`
        (default: the most recent one that's fully ended). Writes nothing."""
        tree = self._goals.tree()
        if goal_ids is not None:
            tree.check_goal_ids(goal_ids)
        today = self._now().date()
        span = parse_period(cadence, period) if period else last_ended(cadence, today)
        goals = [
            g for g in tree.ordered()
            if g.active and g.cadence == cadence and (g.measure or {}).get("kind") in _MEASURED_KINDS
            and (goal_ids is None or g.id in goal_ids)
        ]
        if not goals:
            return []
        tz = self._client.get_time_zone()
        start = datetime.combine(span.start, time(), tz)
        end = datetime.combine(span.end, time(), tz)
        events: list[Event] = []
        if any(g.measure["kind"] != "rollup" for g in goals):
            # With the sleeps that bound the period, whose days run from
            # waking to waking -- see utilities/sleep_days.py.
            listed = self._client.list_events(*listing_range(span, tz))
            events = [e for e in fill_in_from_goals(listed, tree) if e.status != "cancelled"]
            start, end = period_window(span, events, tz, self._now())
        proposals = []
        for goal in goals:
            measured = _MEASURES[goal.measure["kind"]](self, goal, span, start, end, events, tree)
            if measured is not None:
                rating, explanation, metrics = measured
                proposals.append(
                    Assessment(
                        goal_id=goal.id,
                        cadence=cadence,
                        period=span.id,
                        rating=rating,
                        method="rollup" if goal.measure["kind"] == "rollup" else "metric",
                        explanation=explanation,
                        metrics=metrics,
                    )
                )
        return proposals

    # -- the health cache ----------------------------------------------------

    def rebuild_cache(self) -> GoalList:
        """Recompute every goal's health cache from its confirmed history."""
        self._refresh_cache({g.id for g in self._goals.tree().goals if g.cadence})
        return self._goals.get_goals(GOAL_STATUSES)

    def _refresh_cache(self, goal_ids: set[str]) -> None:
        tree = self._goals.tree()
        today = self._now().date()
        updates: dict[str, tuple[int | None, str | None, str | None]] = {}
        for goal_id in goal_ids:
            goal = tree.by_id.get(goal_id)
            if goal is None or goal.cadence is None:
                continue
            # Not just since it was created: history can be filled in for
            # earlier periods (e.g. a goal migrated from a label).
            confirmed = [
                a for a in self._read(today - _CACHE_HISTORY, today + timedelta(days=1), goal_id=goal_id)
                if a.status == "confirmed" and a.cadence == goal.cadence
            ]
            updates[goal_id] = _health_of(confirmed, goal.cadence, today)
        self._goals.set_health(updates)

    # -- the Goal Health calendar ---------------------------------------------

    def _health_calendar(self, *, create: bool = True) -> CalendarClient | None:
        if self._health_client is None:
            calendar_id = self._client.get_calendar_metadata(HEALTH_CALENDAR_METADATA_KEY)
            if calendar_id is None:
                if not create:
                    return None
                calendar_id = self._client.create_calendar(
                    HEALTH_CALENDAR_SUMMARY,
                    "Goal health assessments, kept by Cascading Time Tracker. Best viewed in the Time "
                    "Tracker app.",
                    time_zone=self._client.get_time_zone().key,
                )
                self._client.hide_calendar(calendar_id)
                self._client.set_calendar_metadata(HEALTH_CALENDAR_METADATA_KEY, calendar_id)
            self._health_client = self._client.for_calendar(calendar_id)
        return self._health_client


# -- event encoding ----------------------------------------------------------


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _event_body(assessment: Assessment, goal: Goal, period: Period) -> dict:
    rating = assessment.rating
    shown = "skipped" if rating == "skip" else str(rating)
    proposed = " (proposed)" if assessment.status == "proposed" else ""
    properties = {
        "kind": "assessment",
        "goal": assessment.goal_id,
        "cadence": assessment.cadence,
        "period": assessment.period,
        "rating": str(rating),
        "method": assessment.method,
        "status": assessment.status,
        "explanation": assessment.explanation,
        "metrics": _json(assessment.metrics) if assessment.metrics is not None else None,
        "assessed": assessment.assessed.isoformat() if assessment.assessed else None,
        "schema": "1",
    }
    return {
        "summary": f"{band(rating)} {goal.name} · {period.id} · {shown}{proposed}",
        "description": assessment.rationale or "",
        "start": {"date": period.start.isoformat()},
        "end": {"date": period.end.isoformat()},
        "transparency": "transparent",
        "extendedProperties": {
            "private": {f"{_PREFIX}{key}": value for key, value in properties.items() if value is not None}
        },
    }


def _from_event(item: dict) -> Assessment | None:
    properties = item.get("extendedProperties", {}).get("private", {})

    def prop(key: str) -> str | None:
        return properties.get(f"{_PREFIX}{key}")

    if prop("kind") != "assessment":
        return None
    rating = prop("rating")
    metrics = prop("metrics")
    assessed = prop("assessed")
    return Assessment(
        goal_id=prop("goal"),
        cadence=prop("cadence"),
        period=prop("period"),
        rating="skip" if rating == "skip" else int(rating),
        method=prop("method"),
        status=prop("status") or "proposed",
        explanation=prop("explanation"),
        metrics=json.loads(metrics) if metrics else None,
        rationale=item.get("description") or None,
        assessed=datetime.fromisoformat(assessed) if assessed else None,
    )


def _period_start(assessment: Assessment) -> date:
    try:
        return parse_period(assessment.cadence, assessment.period).start
    except ValueError:
        return date.min


def _health_of(
    confirmed: list[Assessment], cadence: str, today: date
) -> tuple[int | None, str | None, str | None]:
    """(health, health_period, health_trend) from a goal's confirmed
    assessments at `cadence`: its latest rating (a skip doesn't replace
    it), the latest period assessed at all, and the last TREND_LENGTH
    periods' ratings up to the last one that's fully ended."""
    by_period = {a.period: a for a in confirmed}
    if not by_period:
        return None, None, None
    ordered = sorted(by_period.values(), key=_period_start)
    rated = [a for a in ordered if a.rating != "skip"]
    period = last_ended(cadence, today)
    if ordered[-1].period == period_containing(cadence, today).id:
        period = period.next()  # The current period has been assessed already.
    trend = []
    for _ in range(TREND_LENGTH):
        rating = by_period[period.id].rating if period.id in by_period else None
        trend.append("-" if rating in (None, "skip") else str(rating))
        period = period.previous()
    return (
        rated[-1].rating if rated else None,
        ordered[-1].period,
        ",".join(reversed(trend)),
    )


# -- measures ----------------------------------------------------------------

Measured = tuple[int, str, dict[str, Any]]


def _served(events: list[Event], goal: Goal, tree: GoalTree) -> list[Event]:
    """`events` serving `goal` or any of its descendants -- or, if its
    measure names goal_ids, any of those or their descendants. With its
    measure's include_sub_goals false, not the descendants."""
    chosen = set(goal.measure.get("goal_ids") or [goal.id])
    if goal.measure.get("include_sub_goals", True):
        wanted = {g.id for g in tree.goals if chosen & {a.id for a in tree.chain(g.id)}}
    else:
        wanted = chosen
    return [e for e in events if set(e.goal_ids or ()) & wanted]


def _capped(value: float, target: float) -> int:
    return min(100, round(100 * value / target)) if target > 0 else 100


def _measure_duration(
    health: GoalHealth, goal: Goal, period: Period, start: datetime, end: datetime, events: list[Event], tree: GoalTree
) -> Measured | None:
    target = goal.measure.get("target_min")
    if not isinstance(target, (int, float)):
        return None
    minutes = round(
        sum(
            max(0.0, (min(e.end, end) - max(e.start, start)).total_seconds() / 60)
            for e in _served(events, goal, tree)
        )
    )
    return (
        _capped(minutes, target),
        f"{_hours(minutes)} of {_hours(round(target))} target → {_capped(minutes, target)}",
        {"minutes": minutes, "target_min": target},
    )


def _measure_count(
    health: GoalHealth, goal: Goal, period: Period, start: datetime, end: datetime, events: list[Event], tree: GoalTree
) -> Measured | None:
    target = goal.measure.get("target")
    if not isinstance(target, (int, float)):
        return None
    count = sum(1 for e in _served(events, goal, tree) if e.start < end and e.end > start)
    noun = goal.measure.get("noun", "events")
    return (
        _capped(count, target),
        f"{count} of {target:g} {noun} → {_capped(count, target)}",
        {"count": count, "target": target},
    )


def _measure_wake_time(
    health: GoalHealth, goal: Goal, period: Period, start: datetime, end: datetime, events: list[Event], tree: GoalTree
) -> Measured | None:
    try:
        target = time.fromisoformat(goal.measure.get("target", ""))
    except (TypeError, ValueError):
        return None
    grace = goal.measure.get("grace_min", 0)
    zero_at = max(goal.measure.get("zero_at_min", 60), grace + 1)
    tz = start.tzinfo
    wakes = sorted(
        e.end.astimezone(tz) for e in events if e.is_end_of_day_sleep and start <= e.end < end
    )
    by_day = {}
    for wake in wakes:
        by_day.setdefault(wake.date(), wake)  # The day's first wake-up.
    if not by_day:
        return None
    scores = []
    for day, wake in by_day.items():
        late = (wake - datetime.combine(day, target, tz)).total_seconds() / 60
        scores.append(
            100 if late <= grace else 0 if late >= zero_at else round(100 * (zero_at - late) / (zero_at - grace))
        )
    rating = round(sum(scores) / len(scores))
    if len(by_day) == 1:
        (wake,) = by_day.values()
        explanation = f"Woke {wake:%H:%M}; target {target:%H:%M} with {grace} min grace → {rating}"
    else:
        explanation = f"Mean of {len(by_day)} wake-ups against {target:%H:%M} with {grace} min grace → {rating}"
    return (
        rating,
        explanation,
        {"wakes": {day.isoformat(): f"{wake:%H:%M}" for day, wake in by_day.items()}, "target": f"{target:%H:%M}"},
    )


def _measure_rollup(
    health: GoalHealth, goal: Goal, period: Period, start: datetime, end: datetime, events: list[Event], tree: GoalTree
) -> Measured | None:
    children = [g for g in tree.goals if g.parent_id == goal.id and g.active and g.cadence]
    if not children:
        return None
    ratings: dict[str, int] = {}
    for child in children:
        found = [
            a for a in health.history([child.id], child.cadence, period.start, period.end - timedelta(days=1))
            if a.status == "confirmed" and isinstance(a.rating, int)
            and period.start <= _period_start(a) and parse_period(a.cadence, a.period).end <= period.end
        ]
        if found:
            ratings[child.id] = round(sum(a.rating for a in found) / len(found))
    if not ratings:
        return None
    agg = goal.measure.get("agg", "min")
    rating = min(ratings.values()) if agg == "min" else round(sum(ratings.values()) / len(ratings))
    word = "Min" if agg == "min" else "Mean"
    return (
        rating,
        f"{word} of {len(ratings)} sub-goal{'s' if len(ratings) != 1 else ''} "
        f"({', '.join(str(r) for r in ratings.values())}) → {rating}",
        {"children": ratings, "agg": agg},
    )


def _hours(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    return f"{hours}h {rest}m" if hours and rest else f"{hours}h" if hours else f"{rest}m"


_MEASURES: dict[str, Callable[..., Measured | None]] = {
    "duration": _measure_duration,
    "count": _measure_count,
    "wake_time": _measure_wake_time,
    "rollup": _measure_rollup,
}
