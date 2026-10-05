from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from calendar_clients.google_calendar import Event
from utilities.facets import Facets
from utilities.goal_sheet import Goal
from utilities.goals import GoalTree
from utilities.history_digest import history_digest

TZ = ZoneInfo("America/New_York")
END = datetime(2026, 10, 5, 7, tzinfo=TZ)
PERSON = Goal(id="p1", name="Person", status="active", label_id="l1")
OTHER = Goal(id="o1", name="Other", status="active", label_id="l2")
TREE = GoalTree([PERSON, OTHER])


def _event(day: date, goal_ids=("p1",), **facets) -> Event:
    start = datetime(day.year, day.month, day.day, 18, tzinfo=TZ)
    return Event(id=f"e{day}", summary="x", start=start, end=start + timedelta(hours=2), goal_ids=list(goal_ids),
                 facets=Facets(**facets) if facets else None)


def test_groups_activities_and_places_with_count_and_first_and_last_dates():
    events = [
        _event(date(2026, 4, 10), activity="salsa social", place="the hall"),
        _event(date(2026, 9, 28), activity="salsa social"),
        _event(date(2026, 8, 1), goal_ids=["o1"], for_goal_ids=["p1"], activity="baking"),
        _event(date(2026, 9, 1)),  # no facets
        _event(date(2026, 9, 2), goal_ids=["o1"], activity="chess"),  # not theirs
        _event(date(2026, 1, 1), activity="old"),  # before the window
    ]

    digest = history_digest(PERSON, TREE, events, END, TZ)

    assert (digest.events, digest.with_facets) == (4, 3)
    assert [(a.label, a.count, a.first, a.last) for a in digest.activities] == [
        ("salsa social", 2, date(2026, 4, 10), date(2026, 9, 28)),
        ("baking", 1, date(2026, 8, 1), date(2026, 8, 1)),
    ]
    assert [p.label for p in digest.places] == ["the hall"]
    assert digest.text == (
        "Person: 4 events in the last 180 days, 3 with facets\n"
        "Activities: salsa social ×2 (first 2026-04-10, last 2026-09-28); baking ×1 (first 2026-08-01, last 2026-08-01)\n"
        "Places: the hall ×1 (first 2026-04-10, last 2026-04-10)"
    )


def test_says_when_theres_nothing_recorded():
    digest = history_digest(PERSON, TREE, [], END, TZ, window_days=30)

    assert digest.text == (
        "Person: 0 events in the last 30 days, 0 with facets\nActivities: none recorded\nPlaces: none recorded"
    )
