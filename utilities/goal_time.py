"""How much time went toward each goal: the minutes of events serving it
or any of its sub-goals, within a window of wall-clock time. An event
serving two goals counts in full toward both, and toward each of their
ancestors once."""

from __future__ import annotations

from datetime import datetime, timedelta

from calendar_clients.google_calendar import Event
from utilities.goals import OVERALL_ID, GoalTree

RECENT_WINDOWS: dict[str, timedelta] = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}
"""The windows every goal's summary counts its time over, ending at the
last compaction (see utilities/goals.py's GoalList.as_of)."""


def goal_minutes(events: list[Event], tree: GoalTree, start: datetime, end: datetime) -> dict[str, int]:
    """Minutes of `events` (with their goal_ids filled in -- see
    utilities/goal_calendar.py's fill_in_from_goals) between `start` and
    `end`, by goal, each including its sub-goals' events -- the overall
    goal's being every goal's, each event counted once. Cancelled events
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
        if served and OVERALL_ID in tree.by_id:
            served.add(OVERALL_ID)
        for goal_id in served:
            minutes[goal_id] = minutes.get(goal_id, 0) + overlap
    return {goal_id: round(total) for goal_id, total in minutes.items() if round(total) > 0}


def goal_status_minutes(
    events: list[Event], tree: GoalTree, start: datetime, end: datetime
) -> dict[str, dict[frozenset[str], int]]:
    """`goal_minutes`, split by status: for each goal, the minutes of
    `events` between `start` and `end` serving it or its sub-goals, by the
    statuses of the goals each event is given among them -- not those
    goals' ancestors. So an event given only an inactive sub-goal of an
    active goal counts toward the active goal as inactive time. Adding up
    a goal's entries whose statuses include any of a set gives the time
    spent on it through goals with those statuses, each event once."""
    minutes: dict[str, dict[frozenset[str], float]] = {}
    for event in events:
        if event.status == "cancelled":
            continue
        overlap = (min(event.end, end) - max(event.start, start)).total_seconds() / 60
        if overlap <= 0:
            continue
        given = [g for g in event.goal_ids or () if g in tree.by_id and g != OVERALL_ID]
        if not given:
            continue
        holders = {a.id for g in given for a in tree.chain(g)}
        if OVERALL_ID in tree.by_id:
            holders.add(OVERALL_ID)
        for holder in holders:
            statuses = frozenset(tree.by_id[g].status for g in given if tree.under(g, holder))
            split = minutes.setdefault(holder, {})
            split[statuses] = split.get(statuses, 0) + overlap
    return {
        goal_id: {statuses: round(total) for statuses, total in split.items() if round(total) > 0}
        for goal_id, split in minutes.items()
    }
