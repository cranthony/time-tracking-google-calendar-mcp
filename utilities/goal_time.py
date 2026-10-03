"""How much time went toward each goal: the minutes of events serving it
or any of its sub-goals, within a window of wall-clock time. An event
serving two goals counts in full toward both, and toward each of their
ancestors once."""

from __future__ import annotations

from datetime import datetime, timedelta

from calendar_clients.google_calendar import Event
from utilities.goals import GoalTree

RECENT_WINDOWS: dict[str, timedelta] = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}
"""The windows every goal's summary counts its time over, ending at the
last compaction (see utilities/goals.py's GoalList.as_of)."""


def goal_minutes(events: list[Event], tree: GoalTree, start: datetime, end: datetime) -> dict[str, int]:
    """Minutes of `events` (with their goal_ids filled in -- see
    utilities/goal_calendar.py's fill_in_from_goals) between `start` and
    `end`, by goal, each including its sub-goals' events. Cancelled events
    and goals with no time are left out."""
    minutes: dict[str, float] = {}
    for event in events:
        if event.status == "cancelled":
            continue
        overlap = (min(event.end, end) - max(event.start, start)).total_seconds() / 60
        if overlap <= 0:
            continue
        served: set[str] = set()
        for goal_id in event.goal_ids or ():
            served.update(g.id for g in tree.chain(goal_id))
        for goal_id in served:
            minutes[goal_id] = minutes.get(goal_id, 0) + overlap
    return {goal_id: round(total) for goal_id, total in minutes.items() if round(total) > 0}
