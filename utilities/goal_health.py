"""Goal health: daily ratings of how each goal went, stored on a dedicated
Goal Health calendar, and the measurements that propose them. See
docs/goals-design.md sections 6-8.

Every active goal with a measure (or with rated sub-goals -- see
utilities/goals.py's GoalTree.rated) is rated once a day, in the daily
reflection (utilities/reflection.py). Days run from waking to waking --
see utilities/sleep_days.py.

**Storage.** One all-day event per (goal, day) on an app-created calendar
-- not the main one, whose events must never overlap -- with everything
structured in private extended properties (which Calendar can filter on)
and the rationale in its description. Its id encodes the goal and day, so
writing an assessment again overwrites it instead of adding another. The
calendar's id is kept on the main calendar (`set_calendar_metadata`), and
it's created the first time it's needed, in the main calendar's time zone
so the two agree on what a day is. Each event also records the cadence,
"daily", which every assessment has had since goals stopped having
cadences of their own; one recorded at another cadence before then is
ignored.

**Confirmation.** `record_assessments` only ever writes `proposed`
assessments; only a reflection confirms one (`confirm_assessments`), and
only confirmed ratings feed a goal's at-a-glance health: the `health`/
`health_period`/`health_trend` cache columns of the goals tab.

**Measuring.** `measure` proposes a rating for each goal whose measure
the calendar can answer -- `duration` (minutes of its events over its
interval), `count` (how many), `time_constraint` (when the day's events
start or end)
and `rollup` (its immediate sub-goals' confirmed ratings that day) --
each with a one-line `explanation` of how the number was reached. It
never writes anything. See utilities/goal_measures.py for the specs.
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
from utilities.goal_periods import Period, period_containing
from utilities.goal_sheet import GOAL_STATUSES, Goal
from utilities.goals import OVERALL_ID, GoalList, Goals, GoalTree
from utilities.sleep_days import current_day_from, listing_range, period_window

HEALTH_CALENDAR_METADATA_KEY = "goal-health-calendar"
HEALTH_CALENDAR_SUMMARY = "Goal Health"

CADENCE = "daily"
"""The one cadence goals are rated at, recorded with each assessment."""

_PREFIX = "cascading-time-tracker-"
"""The same prefix calendar_clients/google_calendar.py gives this app's
own extended properties on events."""

MAX_RATIONALE_BYTES = 8000
"""Under Calendar's silent description cut-off (MAX_DESCRIPTION_BYTES)."""

_MAX_PROPERTY_CHARS = 1024
"""Calendar's limit on one extended property's value."""

TREND_LENGTH = 8

HISTORY_DAYS = 12
"""How many days `history` reads back by default."""

_CACHE_HISTORY = timedelta(days=3 * 366)
"""How far back the health cache looks for a goal's latest rating."""

Rating = int | Literal["skip"]
Method = Literal["metric", "subjective", "llm", "rollup"]
Status = Literal["proposed", "confirmed"]

MEASURED_KINDS = frozenset({"duration", "count", "time_constraint", "rollup"})

_EVENT_KINDS = frozenset({"duration", "count", "time_constraint"})
"""The measure kinds read from the calendar's events."""
"""The measure kinds `measure` can rate from the calendar."""


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


def band(rating: Rating | None) -> str:
    """The rating's color band, as an emoji: 0-39 red, 40-69 yellow,
    70-100 green."""
    if not isinstance(rating, int):
        return "⚪"
    return "🔴" if rating < 40 else "🟡" if rating < 70 else "🟢"


def assessment_event_id(goal_id: str, day: date) -> str:
    """The deterministic, collision-free event id for one assessment:
    `goal|daily|day` in lowercase base32hex (Calendar's own event-id
    alphabet), so it decodes back to all three. ("daily" is kept from when
    goals had cadences, so assessments written then keep their ids.)"""
    encoded = base64.b32hexencode(f"{goal_id}|{CADENCE}|{day.isoformat()}".encode()).decode()
    return encoded.rstrip("=").lower()


def day_period(day: date) -> Period:
    return period_containing(CADENCE, day)


class GoalHealth:
    """A calendar's goal assessments -- see the module docstring."""

    def __init__(
        self,
        calendar_client: CalendarClient,
        goals: Goals,
        *,
        now: Callable[[], datetime] | None = None,
        today: Callable[[], date] | None = None,
    ) -> None:
        self._client = calendar_client
        self._goals = goals
        self._now = now or (lambda: datetime.now(calendar_client.get_time_zone()))
        self._today = today or (
            lambda: current_day_from(calendar_client.list_events, calendar_client.get_time_zone(), self._now())
        )
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
        today = self._today()
        for assessment in assessments:
            self._check(assessment, tree, today)

    def now(self) -> datetime:
        """Now, in the main calendar's time zone."""
        return self._now()

    def today(self) -> date:
        """The day you're in: the date you last woke on, so it's still
        yesterday until you wake, however late you're up -- see
        utilities/sleep_days.py's `current_day`."""
        return self._today()

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
        today = self._today()
        checked = [(a, self._check(a, tree, today)) for a in assessments]
        health = self._health_calendar()
        written = []
        for assessment, goal in checked:
            assessment = Assessment(**{**assessment.__dict__, "status": status, "assessed": self._now()})
            body = _event_body(assessment, goal)
            response = health.upsert_event_resource(assessment_event_id(assessment.goal_id, assessment.day), body)
            if len(response.get("description", "")) < len(body["description"]):
                raise ValueError(
                    f"Calendar shortened the rationale for {goal.name!r} ({assessment.day}); "
                    f"keep it under {MAX_RATIONALE_BYTES} bytes"
                )
            written.append(_from_event(response))
        return written

    def _check(self, assessment: Assessment, tree: GoalTree, today: date) -> Goal:
        tree.check_goal_ids([assessment.goal_id])
        goal = tree.by_id[assessment.goal_id]
        label = f"{goal.name!r} ({assessment.goal_id})"
        if not tree.rated(goal.id):
            raise ValueError(
                f"{label} isn't rated: only an active goal with a measure, or with sub-goals that are rated, is"
            )
        if assessment.day > today:
            raise ValueError(f"{assessment.day} hasn't started yet")
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
        return goal

    # -- reading ------------------------------------------------------------

    def history(
        self,
        goal_ids: list[str],
        start: date | None = None,
        end: date | None = None,
        *,
        confirmed_only: bool = False,
    ) -> list[Assessment]:
        """`goal_ids`' assessments from `start` to `end` (inclusive), by goal
        then day. By default, the last HISTORY_DAYS days before today, and
        today."""
        tree = self._goals.tree()
        tree.check_goal_ids(goal_ids)
        last = end or self._today()
        first = start or last - timedelta(days=HISTORY_DAYS)
        found: list[Assessment] = []
        for goal_id in dict.fromkeys(goal_ids):
            found += [
                a for a in self.read(first, last + timedelta(days=1), goal_id=goal_id)
                if not confirmed_only or a.status == "confirmed"
            ]
        return found

    def read(self, first: date, end: date, *, goal_id: str | None = None) -> list[Assessment]:
        """Every goal's (or just `goal_id`'s) assessments of the days from
        `first` up to `end` (exclusive), by goal then day."""
        health = self._health_calendar(create=False)
        if health is None:
            return []
        tz = self._client.get_time_zone()
        items = health.list_event_resources(
            datetime.combine(first, time(), tz),
            datetime.combine(end, time(), tz),
            private_property=f"{_PREFIX}goal={goal_id}" if goal_id else None,
        )
        assessments = [
            a for a in (_from_event(item) for item in items if item.get("status") != "cancelled")
            if a is not None and first <= a.day < end
        ]
        return sorted(assessments, key=lambda a: (a.goal_id, a.day))

    # -- measuring ----------------------------------------------------------

    def measure(self, day: date | None = None, goal_ids: list[str] | None = None) -> list[Assessment]:
        """Proposed assessments of `day` (default: the last one that's
        over) for the rated goals whose measure the calendar can answer:
        duration, count, time_constraint, and rollups whose rated sub-goals all
        have a confirmed rating that day. Writes nothing. Raises
        utilities/sleep_days.py's NotOver or MissingSleep (both ValueErrors)
        if the day isn't over, or the sleeps that bound it aren't in the
        calendar."""
        tree = self._goals.tree()
        if goal_ids is not None:
            tree.check_goal_ids(goal_ids)
        day = day or self._today() - timedelta(days=1)
        goals = [
            g for g in tree.ordered()
            if (tree.measure(g.id) or {}).get("kind") in MEASURED_KINDS and (goal_ids is None or g.id in goal_ids)
        ]
        confirmed = {
            a.goal_id: a
            for a in self.read(day, day + timedelta(days=1))
            if a.status == "confirmed"
        } if any(tree.measure(g.id)["kind"] == "rollup" for g in goals) else {}
        return [p for p in self.propose(day, goals, tree, confirmed) if p is not None]

    def propose(
        self, day: date, goals: list[Goal], tree: GoalTree, confirmed: dict[str, Assessment]
    ) -> list[Assessment | None]:
        """A proposed rating of `day` for each of `goals` that the calendar
        can answer (see `measure`), `None` for the rest, in order.
        `confirmed` holds that day's confirmed ratings by goal, for
        rollups."""
        measures = [tree.measure(g.id) or {} for g in goals]
        span = day_period(day)
        tz = self._client.get_time_zone()
        window: tuple[datetime, datetime] | None = None
        events: list[Event] = []
        if any(m.get("kind") in _EVENT_KINDS for m in measures):
            # With the sleeps that bound the day, which runs from waking to
            # waking -- see utilities/sleep_days.py -- and as far back as
            # the longest look back.
            first, last = listing_range(span, tz)
            first = min([first] + [first - _look_back(m) for m in measures if m.get("kind") in ("duration", "count")])
            listed = self._client.list_events(first, last)
            events = [e for e in fill_in_from_goals(listed, tree) if e.status != "cancelled"]
            window = period_window(span, events, tz, self._now())
        proposals: list[Assessment | None] = []
        for goal, measure in zip(goals, measures):
            kind = measure.get("kind")
            if kind == "rollup":
                measured = _rollup(goal, measure, tree, confirmed)
            elif kind in _MEASURES and window is not None:
                measured = _MEASURES[kind](goal, measure, day, window, events, tree)
            else:
                measured = None
            if measured is None:
                proposals.append(None)
                continue
            rating, explanation, metrics = measured
            proposals.append(
                Assessment(
                    goal_id=goal.id,
                    day=day,
                    rating=rating,
                    method="rollup" if kind == "rollup" else "metric",
                    explanation=explanation,
                    metrics=metrics,
                )
            )
        return proposals

    # -- the health cache ----------------------------------------------------

    def rebuild_cache(self) -> GoalList:
        """Recompute every goal's health cache from its confirmed history."""
        tree = self._goals.tree()
        self._refresh_cache({g.id for g in tree.goals if tree.rated(g.id) or g.health_period})
        return self._goals.get_goals(GOAL_STATUSES)

    def _refresh_cache(self, goal_ids: set[str]) -> None:
        tree = self._goals.tree()
        today = self._today()
        updates: dict[str, tuple[int | None, str | None, str | None]] = {}
        for goal_id in goal_ids:
            goal = tree.by_id.get(goal_id)
            if goal is None:
                continue
            # Not just since it was created: history can be filled in for
            # earlier days (e.g. a goal migrated from a label).
            confirmed = [
                a for a in self.read(today - _CACHE_HISTORY, today + timedelta(days=1), goal_id=goal_id)
                if a.status == "confirmed"
            ]
            updates[goal_id] = _health_of(confirmed, today)
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


def _event_body(assessment: Assessment, goal: Goal) -> dict:
    rating = assessment.rating
    shown = "skipped" if rating == "skip" else str(rating)
    proposed = " (proposed)" if assessment.status == "proposed" else ""
    day = assessment.day
    properties = {
        "kind": "assessment",
        "goal": assessment.goal_id,
        "cadence": CADENCE,
        "period": day.isoformat(),
        "rating": str(rating),
        "method": assessment.method,
        "status": assessment.status,
        "explanation": assessment.explanation,
        "metrics": _json(assessment.metrics) if assessment.metrics is not None else None,
        "assessed": assessment.assessed.isoformat() if assessment.assessed else None,
        "schema": "1",
    }
    return {
        "summary": f"{band(rating)} {goal.name} · {day.isoformat()} · {shown}{proposed}",
        "description": assessment.rationale or "",
        "start": {"date": day.isoformat()},
        "end": {"date": (day + timedelta(days=1)).isoformat()},
        "transparency": "transparent",
        "extendedProperties": {
            "private": {f"{_PREFIX}{key}": value for key, value in properties.items() if value is not None}
        },
    }


def _from_event(item: dict) -> Assessment | None:
    properties = item.get("extendedProperties", {}).get("private", {})

    def prop(key: str) -> str | None:
        return properties.get(f"{_PREFIX}{key}")

    if prop("kind") != "assessment" or prop("cadence") not in (None, CADENCE):
        return None
    try:
        day = date.fromisoformat(prop("period") or "")
    except ValueError:
        return None
    rating = prop("rating")
    metrics = prop("metrics")
    assessed = prop("assessed")
    return Assessment(
        goal_id=prop("goal"),
        day=day,
        rating="skip" if rating == "skip" else int(rating),
        method=prop("method"),
        status=prop("status") or "proposed",
        explanation=prop("explanation"),
        metrics=json.loads(metrics) if metrics else None,
        rationale=item.get("description") or None,
        assessed=datetime.fromisoformat(assessed) if assessed else None,
    )


def _health_of(confirmed: list[Assessment], today: date) -> tuple[int | None, str | None, str | None]:
    """(health, health_period, health_trend) from a goal's confirmed
    assessments: its latest rating (a skip doesn't replace it), the latest
    day rated at all, and the last TREND_LENGTH days' ratings up to the
    last one that's over (or today, if it's been rated already)."""
    by_day = {a.day: a for a in confirmed}
    if not by_day:
        return None, None, None
    ordered = sorted(by_day.values(), key=lambda a: a.day)
    rated = [a for a in ordered if a.rating != "skip"]
    last = today if ordered[-1].day >= today else today - timedelta(days=1)
    trend = []
    for back in range(TREND_LENGTH - 1, -1, -1):
        found = by_day.get(last - timedelta(days=back))
        trend.append("-" if found is None or found.rating == "skip" else str(found.rating))
    return rated[-1].rating if rated else None, ordered[-1].day.isoformat(), ",".join(trend)


# -- measures ----------------------------------------------------------------

Measured = tuple[Rating, str, dict[str, Any]]


def _look_back(measure: dict[str, Any]) -> timedelta:
    """How far before a day's end a duration or count measure looks."""
    return timedelta(days=max(measure.get("interval_days", 1), measure.get("zero_at_days", 0)))


def _served(events: list[Event], goal: Goal, measure: dict[str, Any], tree: GoalTree) -> list[Event]:
    """`events` given `goal` or any of its descendants -- or, if its
    measure names another goal in events_of, that one or its descendants
    (as though it were that goal). With its measure's include_sub_goals
    false, not the descendants. (A measure from before events_of may name
    several goals in goal_ids instead.)"""
    if isinstance(measure.get("events_of"), str):
        chosen = {measure["events_of"]}
    else:
        chosen = set(measure.get("goal_ids") or [goal.id])
    if measure.get("include_sub_goals", True):
        wanted = {g.id for g in tree.goals if any(tree.under(g.id, c) for c in chosen)}
    else:
        # The overall goal has no events of its own: its sub-goals' are.
        wanted = chosen | ({c.id for c in tree.children(OVERALL_ID)} if OVERALL_ID in chosen else set())
    return [e for e in events if set(e.goal_ids or ()) & wanted]


def _capped(value: float, target: float) -> int:
    return min(100, round(100 * value / target)) if target > 0 else 100


def _minutes_in(events: list[Event], start: datetime, end: datetime) -> float:
    return sum(max(0.0, (min(e.end, end) - max(e.start, start)).total_seconds() / 60) for e in events)


def _count_in(events: list[Event], start: datetime, end: datetime) -> int:
    return sum(1 for e in events if e.start < end and e.end > start)


def _last_met(
    events: list[Event], value: Callable[[datetime], float], end: datetime, interval: timedelta,
    horizon: timedelta, target: float, *, continuous: bool,
) -> datetime | None:
    """The latest time, from `end - horizon` to `end`, at which the window
    of `interval` before it met `target` -- `value(t)` being the window
    ending at `t`'s value -- or `None` if there's none. `value` changes
    only where an event's start or end enters or leaves the window, and
    between those changes it's linear if `continuous` (minutes), constant
    otherwise (a count)."""
    first = end - horizon
    points = {end, first}
    for event in events:
        for edge in (event.start, event.end, event.start + interval, event.end + interval):
            if first < edge < end:
                points.add(edge)
    ordered = sorted(points, reverse=True)
    if value(end) >= target:
        return end
    for high, low in zip(ordered, ordered[1:]):
        if continuous:
            at_low = value(low)
            if at_low >= target:
                at_high = value(high)
                return low + (high - low) * ((at_low - target) / (at_low - at_high))
        elif value(low + (high - low) / 2) >= target:
            return high
        elif value(low) >= target:
            return low
    return None


def _over_interval(
    goal: Goal, measure: dict[str, Any], window: tuple[datetime, datetime], events: list[Event], tree: GoalTree,
    *, kind: str,
) -> Measured:
    """A duration or count measure's rating: see utilities/goal_measures.py."""
    end = window[1]
    interval_days = measure.get("interval_days", 1)
    interval = timedelta(days=interval_days)
    served = _served(events, goal, measure, tree)
    if kind == "duration":
        target = measure["target_min"]

        def value(at: datetime) -> float:
            return _minutes_in(served, at - interval, at)

        amount = round(value(end))
        said = f"{_hours(amount)} of {_hours(round(target))}"
        metrics: dict[str, Any] = {"minutes": amount, "target_min": target}
    else:
        target = measure["target"]

        def value(at: datetime) -> float:
            return _count_in(served, at - interval, at)

        amount = round(value(end))
        said = f"{amount} of {target:g} {measure.get('noun', 'events')}"
        metrics = {"count": amount, "target": target}
    within = "the day" if interval_days == 1 else f"the last {_days(interval_days)}"
    metrics["interval_days"] = interval_days
    zero_at_days = measure.get("zero_at_days")
    if zero_at_days is None or amount >= target:
        rating = _capped(amount, target)
        return rating, f"{said} in {within} → {rating}", metrics
    met = _last_met(
        served, value, end, interval, timedelta(days=zero_at_days), target, continuous=kind == "duration"
    )
    grace = timedelta(days=zero_at_days - interval_days)
    lapsed = end - met if met is not None else grace
    rating = max(0, min(100, round(100 * (1 - lapsed / grace))))
    metrics["zero_at_days"] = zero_at_days
    if met is None:
        return 0, f"{said} in {within}; not met in the last {_days(zero_at_days)} → 0", metrics
    metrics["lapsed_days"] = round(lapsed / timedelta(days=1), 1)
    return (
        rating,
        f"{said} in {within}; met until {_days(lapsed / timedelta(days=1))} ago, 0 once "
        f"{_days(grace / timedelta(days=1))} pass → {rating}",
        metrics,
    )


def _measure_duration(goal, measure, day, window, events, tree) -> Measured | None:
    if not isinstance(measure.get("target_min"), (int, float)):
        return None
    return _over_interval(goal, measure, window, events, tree, kind="duration")


def _measure_count(goal, measure, day, window, events, tree) -> Measured | None:
    if not isinstance(measure.get("target"), (int, float)):
        return None
    return _over_interval(goal, measure, window, events, tree, kind="count")


def _measure_time_constraint(goal, measure, day, window, events, tree) -> Measured | None:
    """When the day's events of the goal (see `_served`) started, or
    ended, against the target -- see utilities/goal_measures.py."""
    try:
        target = time.fromisoformat(measure.get("target", ""))
    except (TypeError, ValueError):
        return None
    edge = measure.get("edge")
    if edge not in ("start", "end"):
        return None
    by = measure.get("when", "by") == "by"
    grace = measure.get("grace_min", 0)
    zero_at = max(measure.get("zero_at_min", 60), grace + 1)
    start, end = window
    tz = start.tzinfo
    days = [e for e in _served(events, goal, measure, tree) if e.start < end and e.end > start]
    noun = "Started" if edge == "start" else "Ended"
    goal_name = tree.by_id[measure["events_of"]].name if measure.get("events_of") in tree.by_id else goal.name
    metrics: dict[str, Any] = {"edge": edge, "target": f"{target:%H:%M}", "when": "by" if by else "after"}
    if not days:
        return "skip", f"No events of {goal_name} that day → skip", metrics
    at = (min(e.start for e in days) if edge == "start" else max(e.end for e in days)).astimezone(tz)
    goal_time = datetime.combine(day, target, tz)
    # Minutes on the wrong side of the target: late, if it's to be by it.
    off = ((at - goal_time) if by else (goal_time - at)).total_seconds() / 60
    rating = 100 if off <= grace else 0 if off >= zero_at else round(100 * (zero_at - off) / (zero_at - grace))
    metrics["at"] = f"{at:%H:%M}"
    return (
        rating,
        f"{noun} {at:%H:%M}; {'by' if by else 'not before'} {target:%H:%M} with {grace:g} min grace → {rating}",
        metrics,
    )


_MEASURES: dict[str, Callable[..., Measured | None]] = {
    "duration": _measure_duration,
    "count": _measure_count,
    "time_constraint": _measure_time_constraint,
}


def _rollup(goal: Goal, measure: dict[str, Any], tree: GoalTree, confirmed: dict[str, Assessment]) -> Measured | None:
    """The goal's rating from its rated sub-goals' confirmed ratings that
    day, or `None` while any of them has none yet. "skip" if they were all
    skipped (or weigh nothing)."""
    children = tree.rated_children(goal.id)
    if not children or any(c.id not in confirmed for c in children):
        return None
    ratings = {c.id: confirmed[c.id].rating for c in children if isinstance(confirmed[c.id].rating, int)}
    agg = measure.get("agg", "mean")
    if agg == "min":  # From before percentiles.
        agg, measure = "percentile", {**measure, "percentile": 0}
    metrics: dict[str, Any] = {"sub_goals": ratings, "agg": agg}
    listed = ", ".join(str(r) for r in ratings.values())
    count = f"{len(ratings)} sub-goal{'s' if len(ratings) != 1 else ''}"
    if agg == "weighted":
        weights = {goal_id: w for goal_id, w in (measure.get("weights") or {}).items() if goal_id in ratings and w > 0}
        metrics["weights"] = weights
        total = sum(weights.values())
        if not total:
            return "skip", "No weighted sub-goal was rated → skip", metrics
        rating = round(sum(ratings[g] * w for g, w in weights.items()) / total)
        terms = ", ".join(f"{ratings[g]}×{w:g}" for g, w in weights.items())
        return rating, f"Weighted mean of {len(weights)} sub-goal{'s' if len(weights) != 1 else ''} ({terms}) → {rating}", metrics
    if not ratings:
        return "skip", "Every sub-goal was skipped → skip", metrics
    if agg == "percentile":
        percentile = measure.get("percentile", 50)
        metrics["percentile"] = percentile
        values = sorted(ratings.values())
        position = (len(values) - 1) * percentile / 100
        below = int(position)
        above = min(below + 1, len(values) - 1)
        rating = round(values[below] + (values[above] - values[below]) * (position - below))
        word = "Lowest" if percentile == 0 else "Highest" if percentile == 100 else f"{percentile:g}th percentile"
        return rating, f"{word} of {count} ({listed}) → {rating}", metrics
    rating = round(sum(ratings.values()) / len(ratings))
    return rating, f"Mean of {count} ({listed}) → {rating}", metrics


def _hours(minutes: int) -> str:
    hours, rest = divmod(minutes, 60)
    return f"{hours}h {rest}m" if hours and rest else f"{hours}h" if hours else f"{rest}m"


def _days(days: float) -> str:
    if days < 1:
        return _hours(round(days * 24 * 60))
    days = round(days, 1)
    return f"{days:g} day{'s' if days != 1 else ''}"
