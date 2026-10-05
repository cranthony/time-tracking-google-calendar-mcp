from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from calendar_clients.google_calendar import Event
from migrate_people_to_traits import plan_events, reliable_parts
from utilities.facets import Facets
from utilities.goal_sheet import Goal
from utilities.goal_measures import measure_problems
from utilities.goals import GoalTree

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 5, 12, tzinfo=TZ)


def _goal(goal_id, parent=None, measure=None):
    return Goal(id=goal_id, parent_id=parent, name=goal_id, status="active", label_id=f"l-{goal_id}", measure=measure)


TREE = GoalTree([
    _goal("p1"),
    _goal("visit", "p1", {"kind": "count", "target": 1, "noun": "visits", "interval_days": 21, "zero_at_days": 42}),
    _goal("drive", "p1", {"kind": "count", "target": 1, "interval_days": 30}),
    _goal("any", "p1", {"kind": "count", "target": 1, "interval_days": 7}),
    _goal("ask", "p1", {"kind": "subjective", "prompt": "How was it?"}),
    _goal("p2", measure={"kind": "count", "target": 1, "interval_days": 7, "zero_at_days": 21}),
    _goal("other"),
])
PEOPLE = {"p1": {"visit": "Visit", "drive": "drive", "any": None, "ask": False}, "p2": {}}


def _event(event_id, days, goal_ids, **fields):
    start = NOW + timedelta(days=days)
    return Event(id=event_id, summary="x", start=start, end=start + timedelta(hours=1), goal_ids=goal_ids, **fields)


def test_each_cadence_becomes_a_count_part_by_activity():
    parts = reliable_parts(TREE.by_id["p1"], PEOPLE["p1"], TREE)

    assert parts == [
        {"kind": "continuity", "last_within_days": 7, "next_within_days": 7},
        {"kind": "follow_through"},
        {"kind": "count", "target": 1, "interval_days": 21, "zero_at_days": 42, "activity": "visit"},
        {"kind": "count", "target": 1, "interval_days": 30, "activity": "drive"},
        {"kind": "count", "target": 1, "interval_days": 7},
    ]
    assert measure_problems({"kind": "traits", "traits": "all", "parts": {"reliable": parts}}) == []


def test_a_person_with_a_count_of_their_own_keeps_it_as_a_cadence():
    assert reliable_parts(TREE.by_id["p2"], {}, TREE)[2:] == [
        {"kind": "count", "target": 1, "interval_days": 7, "zero_at_days": 21}
    ]
    assert reliable_parts(Goal(id="p3", name="p3"), {}, TREE) == [
        {"kind": "continuity", "last_within_days": 14, "next_within_days": 14},
        {"kind": "follow_through"},
    ]


def test_past_events_are_given_the_person_and_facets_and_future_series_are_left():
    events = [
        _event("a", -3, ["visit", "other"]),
        _event("b", -2, ["any"]),
        _event("c", -1, ["drive"], facets=Facets(attention=3)),  # keeps its facets
        _event("d", 2, ["visit"]),  # future: retagged only
        _event("e", 3, ["drive"], recurring_event_id="series"),  # by hand
        _event("f", -1, ["other"]),  # not theirs
    ]

    patches, by_hand = plan_events(events, PEOPLE, NOW)

    assert [(p.id, p.goal_ids, p.facets) for _, p in patches] == [
        ("a", ["p1", "other"], Facets(with_goal_ids=["p1"], activity="visit")),
        ("b", ["p1"], Facets(with_goal_ids=["p1"])),
        ("c", ["p1"], None),
        ("d", ["p1"], None),
    ]
    assert [e.id for e in by_hand] == ["e"]


def test_adjust_sets_the_continuity_window_and_adds_cadences():
    parts = reliable_parts(
        TREE.by_id["p1"],
        {"visit": "visit", "drive": False, "any": False},
        TREE,
        {"continuity_days": 1, "cadences": [{"activity": "Dinner", "interval_days": 7}]},
    )

    assert parts == [
        {"kind": "continuity", "last_within_days": 1, "next_within_days": 1},
        {"kind": "follow_through"},
        {"kind": "count", "target": 1, "interval_days": 21, "zero_at_days": 42, "activity": "visit"},
        {"kind": "count", "target": 1, "interval_days": 7, "activity": "dinner"},
    ]
