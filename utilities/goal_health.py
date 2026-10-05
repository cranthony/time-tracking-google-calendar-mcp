"""Goal health: daily ratings of how each goal went, stored on a dedicated
Goal Health calendar, and the measurements that propose them. See
docs/goals-design.md sections 6-8.

Every active goal with a measure (or with rated sub-goals -- see
utilities/goals.py's GoalTree.rated) is rated once a day, in the daily
reflection (utilities/reflection.py). Days run from waking to waking --
see utilities/sleep_days.py.

**Storage.** Each day's assessments, with its reflection, are kept in one
all-day event on an app-created calendar -- not the main one, whose
events must never overlap -- or a few, if they don't fit in one: see
utilities/health_days.py. Writing an assessment reads its day, changes
it, and writes the day back, replacing any earlier assessment of the same
goal. The calendar's id is kept on the main calendar
(`set_calendar_metadata`), and it's created the first time it's needed,
in the main calendar's time zone so the two agree on what a day is.

**Confirmation.** `record_assessments` only ever writes `proposed`
assessments; only a reflection confirms one (`confirm_assessments`), and
only confirmed ratings feed a goal's at-a-glance health: the `health`/
`health_period`/`health_trend` cache columns of the goals tab.

**Changed ratings.** Every explanation `measure` gives (a rollup's
included) ends in "→ <rating>". A rating written with an
explanation ending in a different one was changed from what was proposed
-- in a reflection, say -- so the explanation no longer explains it: it's
dropped, leaving the rationale (the reason for the change) to say why,
or replaced by "Changed from <rating>" if there's no rationale. The
metrics are kept: they're still what was measured.

**Measuring.** `measure` proposes a rating for each goal whose measure
the calendar can answer -- `duration` (minutes of its events over its
interval), `count` (how many), `time_constraint` (when the day's events
start or end), `time_window` (whether one of them falls in a window of
the day), `follow_through` (a running score its cancelled events lower
and its kept ones restore), `traits` (its traits' scores, from parts
computed over its events and their facets -- see
utilities/trait_scores.py) and `rollup` (its immediate sub-goals'
confirmed ratings that day),
or "skip" for any goal, whatever its kind, whose measure's `only_if`
names a goal with no events that day -- each with a one-line
`explanation` of how the number was reached. It
never writes anything. See utilities/goal_measures.py for the specs.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Any

from calendar_clients.google_calendar import CalendarClient, Event
from utilities.goal_calendar import fill_in_from_goals
from utilities.goal_measures import weight_on
from utilities.goal_periods import Period, period_containing
from utilities.goal_sheet import Goal
from utilities.goals import OVERALL_ID, GoalChanges, Goals, GoalTree
from utilities.health_days import (
    Assessment,
    DayReflection,
    HealthDay,
    HealthDays,
    Method,
    Rating,
    Status,
    band,
    explanation_of,
)
from utilities.health_summary import day_summary
from utilities.sleep_days import current_day_from, listing_range, period_window
from utilities.traits import Trait

__all__ = ["Assessment", "DayReflection", "GoalHealth", "HealthDay", "band", "day_period"]

HEALTH_CALENDAR_METADATA_KEY = "goal-health-calendar"
HEALTH_CALENDAR_SUMMARY = "Goal Health"

CADENCE = "daily"
"""The one cadence goals are rated at."""

MAX_RATIONALE_BYTES = 8000
"""How long a rationale, or a journal, may be."""

_MAX_PROPERTY_CHARS = 1024
"""How long an explanation may be."""

_MAX_METRICS_CHARS = 4000
"""How long the metrics may be, as JSON: an assessment is kept across as
many properties as it needs (utilities/health_days.py), and a traits
rating's run longer than most."""

TREND_LENGTH = 8

HISTORY_DAYS = 12
"""How many days `history` reads back by default."""

_CACHE_HISTORY = timedelta(days=3 * 366)
"""How far back the health cache looks for a goal's latest rating."""

MEASURED_KINDS = frozenset(
    {"duration", "count", "time_constraint", "time_window", "follow_through", "traits", "rollup"}
)

_EVENT_KINDS = frozenset({"duration", "count", "time_constraint", "time_window", "follow_through", "traits"})
"""The measure kinds read from the calendar's events."""


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
        traits: Callable[[], list[Trait]] | None = None,
    ) -> None:
        """`traits` reads the Traits tab (see utilities/traits.py), for
        goals with a traits measure; without it, they aren't measured."""
        self._client = calendar_client
        self._goals = goals
        self._traits = traits
        self._now = now or (lambda: datetime.now(calendar_client.get_time_zone()))
        self._today = today or (
            lambda: current_day_from(calendar_client.list_events, calendar_client.get_time_zone(), self._now())
        )
        self._health_client: CalendarClient | None = None
        self._days = HealthDays(lambda create: self._health_calendar(create=create))

    # -- writing ------------------------------------------------------------

    def record_assessments(self, assessments: list[Assessment]) -> list[Assessment]:
        """Write `assessments` as proposed (whatever their `status`).
        Validates all of them before writing any."""
        return self._write(assessments, status="proposed")

    def confirm_assessments(self, assessments: list[Assessment]) -> list[Assessment]:
        """Write `assessments` as confirmed, then refresh those goals'
        health cache. For reflections only."""
        return self._write(assessments, status="confirmed")

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
        """The Goal Health calendar; created if need be, unless `create`
        is false (then `None` if there isn't one yet)."""
        return self._health_calendar(create=create)

    def write_day(
        self,
        day: date,
        assessments: list[Assessment],
        *,
        status: Status,
        reflect: Callable[[DayReflection | None], DayReflection] | None = None,
    ) -> list[Assessment]:
        """Write `assessments`, all of `day`, as `status`, and -- given
        `reflect`, which takes the day's reflection so far -- its
        reflection, in one write; then, if they're confirmed, refresh
        those goals' health cache. Validates the assessments first. For
        reflections (utilities/reflection.py)."""
        if any(a.day != day for a in assessments):
            raise ValueError(f"every assessment written with the reflection must be of {day}")
        return self._write(assessments, status=status, reflect={day: reflect} if reflect else {})

    def _write(
        self,
        assessments: list[Assessment],
        *,
        status: Status,
        reflect: dict[date, Callable[[DayReflection | None], DayReflection]] | None = None,
    ) -> list[Assessment]:
        tree = self._goals.tree()
        today = self._today()
        for assessment in assessments:
            self._check(assessment, tree, today)
        reflect = reflect or {}
        days = sorted({a.day for a in assessments} | set(reflect))
        if not days:
            return []
        # From the day before, for the summary's changes since.
        existing = self.read_days(days[0] - timedelta(days=1), days[-1] + timedelta(days=1))
        order = {g.id: i for i, g in enumerate(tree.ordered())}
        written = []
        for day in days:
            current = existing.get(day) or HealthDay(day=day)
            merged = dict(current.assessments)
            for assessment in assessments:
                if assessment.day == day:
                    merged[assessment.goal_id] = Assessment(
                        **{
                            **assessment.__dict__,
                            "explanation": explanation_of(assessment),
                            "status": status,
                            "assessed": self._now(),
                        }
                    )
                    written.append(merged[assessment.goal_id])
            ordered = dict(sorted(merged.items(), key=lambda item: (order.get(item[0], len(order)), item[0])))
            reflection = reflect[day](current.reflection) if day in reflect else current.reflection
            before = existing.get(day - timedelta(days=1))
            existing[day] = self._days.write(
                HealthDay(day=day, assessments=ordered, reflection=reflection, parts=current.parts),
                day_summary(tree, ordered, before.assessments if before else {}),
                OVERALL_ID,
            )
        if status == "confirmed":
            self._refresh_cache({a.goal_id for a in written})
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
        if assessment.metrics is not None and len(_json(assessment.metrics)) > _MAX_METRICS_CHARS:
            raise ValueError(f"{label}: the metrics are longer than {_MAX_METRICS_CHARS} characters as JSON")
        if len((assessment.rationale or "").encode()) > MAX_RATIONALE_BYTES:
            raise ValueError(
                f"{label}: the rationale is longer than {MAX_RATIONALE_BYTES} bytes"
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
        order = {goal_id: i for i, goal_id in enumerate(dict.fromkeys(goal_ids))}
        found = [
            a for a in self.read(first, last + timedelta(days=1))
            if a.goal_id in order and (not confirmed_only or a.status == "confirmed")
        ]
        return sorted(found, key=lambda a: (order[a.goal_id], a.day))

    def read(self, first: date, end: date, *, goal_id: str | None = None) -> list[Assessment]:
        """Every goal's (or just `goal_id`'s) assessments of the days from
        `first` up to `end` (exclusive), by goal then day."""
        assessments = [
            a for day in self.read_days(first, end).values() for a in day.assessments.values()
            if goal_id is None or a.goal_id == goal_id
        ]
        return sorted(assessments, key=lambda a: (a.goal_id, a.day))

    def read_days(self, first: date, end: date) -> dict[date, HealthDay]:
        """Everything kept about the days from `first` up to `end`
        (exclusive) -- their assessments and reflections -- by day."""
        return self._days.read(first, end, self._client.get_time_zone())

    # -- measuring ----------------------------------------------------------

    def measure(self, day: date | None = None, goal_ids: list[str] | None = None) -> list[Assessment]:
        """Proposed assessments of `day` (default: the last one that's
        over) for the rated goals whose measure the calendar can answer:
        duration, count, time_constraint, time_window, follow_through,
        rollups whose rated sub-goals all have a confirmed rating that day, and any whose
        `only_if` isn't met (a skip). Writes nothing. Raises
        utilities/sleep_days.py's NotOver or MissingSleep (both ValueErrors)
        if the day isn't over, or the sleeps that bound it aren't in the
        calendar."""
        tree = self._goals.tree()
        if goal_ids is not None:
            tree.check_goal_ids(goal_ids)
        day = day or self._today() - timedelta(days=1)
        goals = [
            g for g in tree.ordered()
            if _measurable(tree.measure(g.id) or {}) and (goal_ids is None or g.id in goal_ids)
        ]
        confirmed = {
            a.goal_id: a
            for a in self.read(day, day + timedelta(days=1))
            if a.status == "confirmed"
        } if any(tree.measure(g.id)["kind"] == "rollup" for g in goals) else {}
        return [p for p in self.propose(day, goals, tree, confirmed) if p is not None]

    def propose(
        self,
        day: date,
        goals: list[Goal],
        tree: GoalTree,
        confirmed: dict[str, Assessment],
        *,
        judgments: dict[str, dict[str, dict[str, int]]] | None = None,
        breakdowns: dict[str, Any] | None = None,
    ) -> list[Assessment | None]:
        """A proposed rating of `day` for each of `goals` that the calendar
        can answer (see `measure`), `None` for the rest, in order.
        `confirmed` holds that day's confirmed ratings by goal, for
        rollups. A traits measure's judgment parts take their scores from
        `judgments` (by goal id, then trait id, then part key), and each
        traits rating, part by part (utilities/trait_scores.py's
        TraitsRating), is put in `breakdowns`, by goal id, if it's given."""
        measures = [tree.measure(g.id) or {} for g in goals]
        traits = self._traits() if self._traits and any(m.get("kind") == "traits" for m in measures) else []
        window, events, cancelled = self._events_for(day, measures, tree, traits)
        proposals: list[Assessment | None] = []
        for goal, measure in zip(goals, measures):
            kind = measure.get("kind")
            condition = _only_if(measure)
            if condition is not None and window is not None and not _met(goal, condition, window, events, tree):
                proposals.append(_unmet(goal, condition, day, kind, tree))
                continue
            if kind == "rollup":
                measured = _rollup(goal, measure, tree, confirmed, day)
            elif kind == "follow_through" and window is not None:
                measured = _follow_through(goal, measure, window, events, cancelled, tree)
            elif kind == "traits" and window is not None and self._traits is not None:
                from utilities.trait_scores import score_traits

                scored = score_traits(
                    goal, measure, traits, window, events, cancelled, tree, (judgments or {}).get(goal.id)
                )
                if breakdowns is not None:
                    breakdowns[goal.id] = replace(scored, goal_id=goal.id, day=day)
                measured = scored.rating, scored.explanation, scored.metrics()
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

    def traits_rating(self, goal_id: str, day: date | None = None, judgments=None):
        """How `goal_id`'s traits measure rates `day` (default: the last
        one that's over), part by part, with the events behind each part:
        utilities/trait_scores.py's TraitsRating. `judgments` gives judgment
        parts' scores, by trait id then part key. Writes nothing."""
        from utilities.trait_scores import score_traits

        tree = self._goals.tree()
        tree.check_goal_ids([goal_id])
        goal = tree.by_id[goal_id]
        measure = goal.measure or {}
        if measure.get("kind") != "traits":
            raise ValueError(f"{goal.name!r} ({goal_id}) isn't measured by traits")
        day = day or self._today() - timedelta(days=1)
        traits = self._traits() if self._traits else []
        window, events, cancelled = self._events_for(day, [measure], tree, traits)
        rated = score_traits(goal, measure, traits, window, events, cancelled, tree, judgments)
        return replace(rated, goal_id=goal_id, day=day)

    def _events_for(
        self, day: date, measures: list[dict[str, Any]], tree: GoalTree, traits: list[Trait]
    ) -> tuple[tuple[datetime, datetime] | None, list[Event], list[Event]]:
        """The day's window, and the events (kept, then cancelled) that
        `measures` read to rate it -- none, and no window, if they read no
        events."""
        if not any(m.get("kind") in _EVENT_KINDS or isinstance(m.get("only_if"), dict) for m in measures):
            return None, [], []
        span = day_period(day)
        tz = self._client.get_time_zone()
        # With the sleeps that bound the day, which runs from waking to
        # waking -- see utilities/sleep_days.py -- and as far back as the
        # longest look back (and, for a traits measure's continuity, ahead).
        first, last = listing_range(span, tz)
        first = min([first] + [first - _look_back(m) for m in measures if m.get("kind") in _LOOKS_BACK])
        show_deleted = any(m.get("kind") == "follow_through" for m in measures)
        if traits:
            from utilities.trait_scores import reach

            starts, ends = first, last
            for measure in measures:
                if measure.get("kind") == "traits":
                    back, ahead, needs_cancelled = reach(measure, traits)
                    first, last = min(first, starts - back), max(last, ends + ahead)
                    show_deleted = show_deleted or needs_cancelled
        if show_deleted:
            listed = self._with_series_goals(self._client.list_events(first, last, show_deleted=True))
        else:
            listed = self._client.list_events(first, last)
        filled = fill_in_from_goals(listed, tree)
        events = [e for e in filled if e.status != "cancelled"]
        cancelled = [e for e in filled if e.status == "cancelled"]
        return period_window(span, events, tz, self._now()), events, cancelled

    def _with_series_goals(self, events: list[Event]) -> list[Event]:
        """`events`, but with each cancelled instance of a recurring series
        that's kept no goals given its series' (and its label, if it's kept
        none) -- and, if it's kept no end either, its series' length."""
        series: dict[str, Event | None] = {}
        filled = []
        for event in events:
            if event.status == "cancelled" and event.recurring_event_id and not event.goal_ids:
                if event.recurring_event_id not in series:
                    try:
                        series[event.recurring_event_id] = self._client.get_event(event.recurring_event_id)
                    except Exception:  # Gone, say: then it can't be told whose it was.
                        series[event.recurring_event_id] = None
                master = series[event.recurring_event_id]
                if master is not None:
                    event = replace(
                        event,
                        goal_ids=master.goal_ids,
                        event_label_id=event.event_label_id or master.event_label_id,
                        end=event.end if event.end > event.start else event.start + (master.end - master.start),
                    )
            filled.append(event)
        return filled

    # -- the health cache ----------------------------------------------------

    def rebuild_cache(self) -> GoalChanges:
        """Recompute every goal's health cache from its confirmed history.
        Returns the goals whose cache changed."""
        tree = self._goals.tree()
        changed = self._refresh_cache({g.id for g in tree.goals if tree.rated(g.id) or g.health_period})
        return self._goals.changes(changed)

    def _refresh_cache(self, goal_ids: set[str]) -> list[str]:
        """Recompute these goals' health caches; the ids of those that
        changed."""
        tree = self._goals.tree()
        today = self._today()
        # Not just since it was created: history can be filled in for
        # earlier days (e.g. a goal migrated from a label).
        confirmed: dict[str, list[Assessment]] = {}
        for a in self.read(today - _CACHE_HISTORY, today + timedelta(days=1)):
            if a.status == "confirmed":
                confirmed.setdefault(a.goal_id, []).append(a)
        updates = {
            goal_id: _health_of(confirmed.get(goal_id, []), today) for goal_id in goal_ids if goal_id in tree.by_id
        }
        return self._goals.set_health(updates)

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


def _measurable(measure: dict[str, Any]) -> bool:
    """Whether `measure` can propose anything from the calendar: on some
    days, at least, for one with an `only_if`."""
    return measure.get("kind") in MEASURED_KINDS or isinstance(measure.get("only_if"), dict)


def _only_if(measure: dict[str, Any]) -> dict[str, Any] | None:
    """The measure's `only_if`, if it has one; without its own
    `events_of`, it looks at the measure's events -- its `events_of` and
    `include_sub_goals` -- see utilities/goal_measures.py."""
    condition = measure.get("only_if")
    if not isinstance(condition, dict):
        return None
    if "events_of" in condition:
        return condition
    return {**{k: measure[k] for k in ("events_of", "include_sub_goals") if k in measure}, **condition}


def _met(
    goal: Goal, condition: dict[str, Any], window: tuple[datetime, datetime], events: list[Event], tree: GoalTree
) -> bool:
    """Whether the day (`window`) has an event of the goal a measure's
    `only_if` names -- see utilities/goal_measures.py."""
    start, end = window
    return any(e.start < end and e.end > start for e in _served(events, goal, condition, tree))


def _unmet(goal: Goal, condition: dict[str, Any], day: date, kind: str | None, tree: GoalTree) -> Assessment:
    """The skip proposed for a day without an event its `only_if` needs."""
    source = condition["events_of"] if condition.get("events_of") in tree.by_id else goal.id
    return Assessment(
        goal_id=goal.id,
        day=day,
        rating="skip",
        method=_METHODS.get(kind, "metric"),
        explanation=f"No events of {tree.by_id[source].name} that day → skip",
        metrics={"only_if": source},
    )


_METHODS: dict[str | None, Method] = {"rollup": "rollup", "subjective": "subjective", "llm": "llm"}
"""Each kind's assessment method, but for the metric ones."""


_LOOKS_BACK = frozenset({"duration", "count", "follow_through"})
"""The measure kinds that look at days before the one being rated."""


def _look_back(measure: dict[str, Any]) -> timedelta:
    """How far before a day's end a duration, count or follow-through
    measure looks."""
    if measure.get("kind") == "follow_through":
        return timedelta(days=measure.get("look_back_days", FOLLOW_THROUGH_LOOK_BACK_DAYS))
    return timedelta(days=max(measure.get("interval_days", 1), measure.get("zero_at_days", 0)))


def _served(events: list[Event], goal: Goal, measure: dict[str, Any], tree: GoalTree) -> list[Event]:
    """`events` given `goal` or any of its descendants -- or, if its
    measure names another goal in events_of, that one or its descendants
    (as though it were that goal). With its measure's include_sub_goals
    false, not the descendants."""
    chosen = {measure["events_of"] if isinstance(measure.get("events_of"), str) else goal.id}
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
    *, kind: str, served: list[Event] | None = None,
) -> Measured:
    """A duration or count measure's rating: see utilities/goal_measures.py.
    `served`, if given, are the events it counts, instead of the goal's."""
    end = window[1]
    interval_days = measure.get("interval_days", 1)
    interval = timedelta(days=interval_days)
    served = served if served is not None else _served(events, goal, measure, tree)
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
        return 0, f"No events of {goal_name} that day → 0", metrics
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


def _measure_time_window(goal, measure, day, window, events, tree) -> Measured | None:
    """Whether one of the day's events of the goal (see `_served`) fell in
    a window of the day, each as far outside it as its closest edge -- see
    utilities/goal_measures.py."""
    try:
        opens, closes = time.fromisoformat(measure.get("from", "")), time.fromisoformat(measure.get("to", ""))
    except (TypeError, ValueError):
        return None
    if opens >= closes:
        return None
    grace = measure.get("grace_min", 0)
    zero_at = max(measure.get("zero_at_min", 60), grace + 1)
    start, end = window
    tz = start.tzinfo
    days = [e for e in _served(events, goal, measure, tree) if e.start < end and e.end > start]
    goal_name = tree.by_id[measure["events_of"]].name if measure.get("events_of") in tree.by_id else goal.name
    span = f"{opens:%H:%M}–{closes:%H:%M}"
    metrics: dict[str, Any] = {"from": f"{opens:%H:%M}", "to": f"{closes:%H:%M}"}
    if not days:
        return 0, f"No events of {goal_name} that day → 0", metrics
    window_start, window_end = datetime.combine(day, opens, tz), datetime.combine(day, closes, tz)

    def out(event: Event) -> float:
        """Minutes from the window to the event's closest edge; 0 if they
        overlap."""
        return max(0.0, (event.start - window_end).total_seconds(), (window_start - event.end).total_seconds()) / 60

    closest = min(days, key=lambda e: (out(e), e.start))
    off = out(closest)
    rating = 100 if off <= grace else 0 if off >= zero_at else round(100 * (zero_at - off) / (zero_at - grace))
    at = f"{closest.start.astimezone(tz):%H:%M}–{closest.end.astimezone(tz):%H:%M}"
    metrics.update(at=at, off_min=round(off))
    if off == 0:
        where = f"in {span}"
    else:
        side = "after" if closest.start >= window_end else "before"
        where = f"{round(off)} min {side} {span} with {grace:g} min grace"
    return rating, f"{at}; {where} → {rating}", metrics


FOLLOW_THROUGH_PENALTY = 25
FOLLOW_THROUGH_RECOVERY = 25
FOLLOW_THROUGH_LOOK_BACK_DAYS = 30


def _follow_through(
    goal: Goal, measure: dict[str, Any], window: tuple[datetime, datetime], events: list[Event],
    cancelled: list[Event], tree: GoalTree,
) -> Measured:
    """A running score that the goal's cancelled events (see `_served`)
    lower and its kept ones restore, day by day over the look back -- see
    utilities/goal_measures.py."""
    penalty = measure.get("penalty", FOLLOW_THROUGH_PENALTY)
    recovery = measure.get("recovery", FOLLOW_THROUGH_RECOVERY)
    look_back_days = measure.get("look_back_days", FOLLOW_THROUGH_LOOK_BACK_DAYS)
    kept = _served(events, goal, measure, tree)
    dropped = [e for e in _served(cancelled, goal, measure, tree) if not any(_overlap(k, e) for k in kept)]
    start, end = window
    # The day being rated, and the 24-hour days before it, oldest first.
    days = [(start - timedelta(days=back), start - timedelta(days=back - 1)) for back in range(look_back_days - 1, 0, -1)]
    days.append((start, end))
    score = before = 100.0
    lost = gained = 0
    for day_start, day_end in days:
        before = score
        lost = sum(1 for e in dropped if day_start <= e.start < day_end)
        gained = sum(1 for e in kept if day_start <= e.start < day_end)
        score = max(0.0, min(100.0, score - penalty * lost + (recovery if gained else 0)))
    rating, was = round(score), round(before)
    said = ", ".join(
        part for part in (
            f"{lost} cancelled (−{penalty * lost:g})" if lost else "",
            f"{gained} kept (+{recovery:g})" if gained else "",
        ) if part
    ) or "Nothing cancelled or kept"
    metrics = {
        "cancelled": lost, "kept": gained, "before": was, "penalty": penalty, "recovery": recovery,
        "look_back_days": look_back_days,
    }
    return rating, f"{said} that day, from {was} → {rating}", metrics


def _overlap(kept: Event, dropped: Event) -> bool:
    """Whether `kept` covers any of `dropped`'s time -- its start, if it
    has no length."""
    if dropped.end > dropped.start:
        return kept.start < dropped.end and kept.end > dropped.start
    return kept.start <= dropped.start < kept.end


_MEASURES: dict[str, Callable[..., Measured | None]] = {
    "duration": _measure_duration,
    "count": _measure_count,
    "time_constraint": _measure_time_constraint,
    "time_window": _measure_time_window,
}


def roll_up(goal: Goal, tree: GoalTree, ratings: dict[str, Assessment], day: date) -> Assessment | None:
    """The goal's rollup of `day` from those of its rated sub-goals that
    have a rating in `ratings` -- leaving out any without one, for a
    provisional rollup (see utilities/reflection.py) -- or `None` if none
    of them has."""
    measure = tree.measure(goal.id) or {}
    measured = _rollup(goal, measure, tree, ratings, day, partial=True)
    if measured is None:
        return None
    rating, explanation, metrics = measured
    return Assessment(
        goal_id=goal.id, day=day, rating=rating, method="rollup", explanation=explanation, metrics=metrics
    )


def _rollup(
    goal: Goal,
    measure: dict[str, Any],
    tree: GoalTree,
    confirmed: dict[str, Assessment],
    day: date,
    *,
    partial: bool = False,
) -> Measured | None:
    """The goal's rating from its rated sub-goals' confirmed ratings that
    `day`, or `None` while any of them has none yet (or, if `partial`,
    while all of them have none, leaving out the rest). "skip" if they were
    all skipped (or weigh nothing). A temporary weight weighs what it does
    on `day` (see `weight_on`)."""
    children = tree.rated_children(goal.id)
    if partial:
        children = [c for c in children if c.id in confirmed]
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
        on_day = {goal_id: weight_on(w, day) for goal_id, w in (measure.get("weights") or {}).items()}
        weights = {goal_id: w for goal_id, w in on_day.items() if goal_id in ratings and w > 0}
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
