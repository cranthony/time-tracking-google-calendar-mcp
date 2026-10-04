"""Move the Goal Health calendar to one event per day, once.

Assessments used to be kept one all-day event per (goal, day), beside one
reflection event per day. Now each day's assessments and reflection share
one event (see utilities/health_days.py), and the server reads nothing
else. This folds every old event into its day's event, reads each day
back to check it, and only then deletes that day's old events.

Anything already in the new form wins over an old event for the same
goal (or reflection) and day. Old events that can't be read as a day's --
from before goals were only rated daily -- are listed, but left alone:
nothing reads them.

Run it with no arguments first: it only says what it would do. Then run
it with --apply, while nothing else is writing to the calendar; it then
rebuilds the goals tab's health columns (`rebuild_goal_health_cache`).

Usage:
    python migrate_goal_health.py [--apply]
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from calendar_clients.google_calendar import CalendarClient
from calendar_clients.write_lock import WRITE_LOCK
from utilities.goal_health import GoalHealth
from utilities.goals import OVERALL_ID, GoalTree
from utilities.health_days import PREFIX, Assessment, DayReflection, HealthDay, HealthDays, decode


@dataclass(kw_only=True)
class Plan:
    """What `migrate` found, and did."""

    days: dict[date, HealthDay] = field(default_factory=dict)
    """Each day with old events, as it's to be written."""

    old_events: dict[date, list[str]] = field(default_factory=dict)
    """The ids of each day's old events."""

    unreadable: list[str] = field(default_factory=list)
    """Old events that aren't a day's, left alone: (id, summary) lines."""

    written: int = 0
    deleted: int = 0


def migrate(
    calendar: CalendarClient, tree: GoalTree, tz, *, apply: bool, say: Callable[[str], None] = print
) -> Plan:
    """Fold the old events on the Goal Health `calendar` into day events
    (see the module docstring) -- if `apply`, else only work out how."""
    items = [item for item in calendar.list_all_event_resources() if item.get("status") != "cancelled"]
    current = decode(items)
    plan = Plan()
    legacy_assessments: dict[date, dict[str, Assessment]] = {}
    legacy_reflections: dict[date, DayReflection] = {}
    for item in items:
        properties = _properties(item)
        kind = properties.get("kind")
        if kind not in ("assessment", "reflection"):
            continue
        day = _day(properties)
        if day is None:
            plan.unreadable.append(f"{item['id']} {item.get('summary', '')}")
            continue
        plan.old_events.setdefault(day, []).append(item["id"])
        if kind == "assessment":
            assessment = _legacy_assessment(properties, day, item.get("description"))
            legacy_assessments.setdefault(day, {})[assessment.goal_id] = assessment
        else:
            legacy_reflections[day] = _legacy_reflection(properties, item.get("description"))

    order = {g.id: i for i, g in enumerate(tree.ordered())}
    for day in sorted(plan.old_events):
        existing = current.get(day) or HealthDay(day=day)
        merged = {**legacy_assessments.get(day, {}), **existing.assessments}
        plan.days[day] = HealthDay(
            day=day,
            assessments=dict(sorted(merged.items(), key=lambda item: (order.get(item[0], len(order)), item[0]))),
            reflection=existing.reflection or legacy_reflections.get(day),
            parts=existing.parts,
        )

    old = sum(len(ids) for ids in plan.old_events.values())
    say(f"{old} old event(s) on {len(plan.days)} day(s) to fold into one event a day.")
    if plan.unreadable:
        say(f"{len(plan.unreadable)} old event(s) aren't a day's, and are left alone:")
        for line in plan.unreadable:
            say(f"  {line}")
    if not apply:
        say("Nothing written: run again with --apply to migrate.")
        return plan

    days = HealthDays(lambda create: calendar)
    names = {g.id: tree.path(g.id) for g in tree.goals}
    for day, health_day in plan.days.items():
        written = days.write(health_day, names, OVERALL_ID)
        plan.written += written.parts
        read = days.read(day, day + timedelta(days=1), tz).get(day)
        if read is None or read.assessments != health_day.assessments or read.reflection != health_day.reflection:
            raise RuntimeError(f"{day} didn't read back as written; its old events are kept")
        for event_id in plan.old_events[day]:
            calendar.delete_event_resource(event_id)
            plan.deleted += 1
        say(f"{day}: {len(health_day.assessments)} assessment(s) in {written.parts} event(s); "
            f"{len(plan.old_events[day])} old event(s) deleted")
    say(f"Done: {plan.written} day event(s) written, {plan.deleted} old event(s) deleted.")
    return plan


def _properties(item: dict) -> dict[str, str]:
    return {
        key.removeprefix(PREFIX): value
        for key, value in item.get("extendedProperties", {}).get("private", {}).items()
    }


def _day(properties: dict[str, str]) -> date | None:
    """The day an old event is of, or `None` if it's of a longer period:
    a week ("2026-W40", which `date.fromisoformat` would read as its
    Monday) or a month."""
    period = properties.get("period") or ""
    if properties.get("cadence", "daily") != "daily" or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", period):
        return None
    try:
        return date.fromisoformat(period)
    except ValueError:
        return None


def _legacy_assessment(properties: dict[str, str], day: date, description: str | None) -> Assessment:
    rating = properties.get("rating")
    metrics = properties.get("metrics")
    assessed = properties.get("assessed")
    return Assessment(
        goal_id=properties["goal"],
        day=day,
        rating="skip" if rating == "skip" else int(rating),
        method=properties.get("method"),
        status=properties.get("status") or "proposed",
        explanation=properties.get("explanation"),
        metrics=json.loads(metrics) if metrics else None,
        rationale=description or None,
        assessed=datetime.fromisoformat(assessed) if assessed else None,
    )


def _legacy_reflection(properties: dict[str, str], description: str | None) -> DayReflection:
    reflected = properties.get("reflected")
    try:
        intentions = [str(i) for i in json.loads(properties.get("intentions") or "[]")]
    except ValueError:
        intentions = []
    return DayReflection(
        journal=description or None,
        intentions=intentions,
        # From before reflections went a level at a time, when one always was.
        complete=properties.get("complete") != "false",
        reflected=datetime.fromisoformat(reflected) if reflected else None,
    )


def main() -> None:
    from config import build_calendar_client, build_goals

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="migrate (without it, only say what would happen)")
    args = parser.parse_args()
    with WRITE_LOCK:
        calendar_client = build_calendar_client()
        goals = build_goals()
        goal_health = GoalHealth(calendar_client, goals)
        health = goal_health.health_calendar(create=False)
        if health is None:
            print("This calendar has no Goal Health calendar yet: nothing to migrate.")
            return
        migrate(health, goals.tree(), calendar_client.get_time_zone(), apply=args.apply)
        if args.apply:
            # Anything confirmed since the server stopped reading the old
            # events left the cache missing them.
            goal_health.rebuild_cache()
            print("Rebuilt the goals tab's health columns.")


if __name__ == "__main__":
    main()
