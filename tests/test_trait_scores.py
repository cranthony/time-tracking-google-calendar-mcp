from dataclasses import replace
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from calendar_clients.google_calendar import Event
from utilities.facets import Facets
from utilities.goal_sheet import Goal
from utilities.goals import GoalTree
from utilities.health_days import Assessment
from utilities.trait_scores import reach, score_traits, selected_traits, trait_history
from utilities.traits import SEED_TRAITS, Trait

TZ = ZoneInfo("America/New_York")
DAY_START = datetime(2026, 10, 1, 7, tzinfo=TZ)
DAY_END = datetime(2026, 10, 2, 7, tzinfo=TZ)
WINDOW = (DAY_START, DAY_END)

PERSON = Goal(id="p1", name="Person", status="active", label_id="l1", measure={"kind": "traits", "traits": "all"})
VISITS = Goal(id="p1v", parent_id="p1", name="Visits", status="active", label_id="l2")
OTHER = Goal(id="o1", name="Other", status="active", label_id="l3")
TREE = GoalTree([PERSON, VISITS, OTHER])


def _event(days_before_end: float, hours: float = 2, goal_ids=("p1",), **facets) -> Event:
    start = DAY_END - timedelta(days=days_before_end)
    return Event(
        id=f"e{days_before_end:g}",
        summary="x",
        start=start,
        end=start + timedelta(hours=hours),
        goal_ids=list(goal_ids),
        facets=Facets(**facets) if facets else None,
    )


def _trait(*parts, trait_id="t", status="active") -> Trait:
    return Trait(id=trait_id, name=trait_id.title(), status=status, parts=list(parts))


def _score(parts, events, measure=None, cancelled=(), judgments=None):
    traits = [_trait(*parts)]
    rated = score_traits(
        PERSON, measure or {"kind": "traits", "traits": "all"}, traits, WINDOW, list(events), list(cancelled), TREE,
        judgments,
    )
    return rated


def _part(part, events, **kwargs):
    return _score([part], events, **kwargs).traits[0].parts[0]


class TestParts:
    def test_prep_counts_for_events_in_the_window(self):
        events = [
            _event(3, goal_ids=["o1"], for_goal_ids=["p1"]),
            _event(10, goal_ids=["o1"], for_goal_ids=["p1v"]),  # a sub-goal's counts
            _event(40, goal_ids=["o1"], for_goal_ids=["p1"]),  # outside the window
            _event(2),  # with, not for
        ]

        part = _part({"kind": "prep", "target": 4}, events)

        assert (part.score, part.said) == (50, "2 of 4 prep events in the last 30 days")
        assert part.event_ids == ["e3", "e10"]

    def test_prep_regularity_is_the_share_of_weeks_with_prep(self):
        events = [_event(d, goal_ids=[], for_goal_ids=["p1"]) for d in (1, 2, 15, 30)]

        part = _part({"kind": "prep_regularity", "weeks": 4}, events)

        assert (part.score, part.said) == (50, "Prep in 2 of the last 4 weeks")

    @pytest.mark.parametrize(
        "days, score",
        [((3, -5), 100), ((3,), 50), ((-5,), 50), ((20, -20), 0), ((), 0)],
    )
    def test_continuity_wants_a_recent_and_an_upcoming_event(self, days, score):
        part = _part({"kind": "continuity", "last_within_days": 14, "next_within_days": 14}, [_event(d) for d in days])

        assert part.score == score

    def test_continuity_says_when(self):
        part = _part({"kind": "continuity"}, [_event(3, hours=24), _event(-5)])

        assert part.said == "Last 2 days ago; next in 5 days (within 14 and 14 days)"
        assert part.event_ids == ["e3", "e-5"]

    def test_together_creative_counts_with_events_making_something(self):
        events = [
            _event(1, creative=3),
            _event(2, creative=1),
            _event(3, goal_ids=["o1"], with_goal_ids=["p1"], creative=2),  # with, by its facets
            _event(4, for_goal_ids=["p1"], creative=3),  # for them, not with them
            _event(5),  # no facets
        ]

        part = _part({"kind": "together_creative", "target": 2}, events)

        assert (part.score, part.event_ids) == (100, ["e1", "e3"])

    def test_novelty_counts_whats_new(self):
        events = [_event(1, new="place"), _event(2, new="none"), _event(3, new="both"), _event(4)]

        part = _part({"kind": "novelty", "target": 4}, events)

        assert (part.score, part.event_ids) == (50, ["e1", "e3"])

    def test_effort_paid_weighs_minutes_by_effort_over_with_and_for_events(self):
        events = [
            _event(1, hours=2, effort=1),  # 120 × 2
            _event(2, hours=1, goal_ids=["o1"], for_goal_ids=["p1"], effort=3),  # 60 × 4
            _event(3, hours=1),  # 60 × 1
            _event(4, hours=1, goal_ids=["o1"]),  # not theirs
        ]

        part = _part({"kind": "effort_paid", "target": 1080}, events)

        assert (part.score, part.said) == (50, "540 of 1080 effort-minutes (minutes × (1 + effort)) in the last 30 days")

    def test_attention_is_the_mean_of_with_events_as_0_to_100(self):
        events = [_event(1, attention=3), _event(2, attention=1), _event(3)]

        assert _part({"kind": "attention"}, events).score == 67

    def test_attention_without_any_is_left_out(self):
        part = _part({"kind": "attention"}, [_event(1)])

        assert part.score is None
        assert _score([{"kind": "attention"}], [_event(1)]).rating == "skip"

    def test_a_judgment_waits_for_the_reflection(self):
        rated = _score([{"kind": "judgment", "rubric": "R"}, {"kind": "prep"}], [])

        assert rated.traits[0].parts[0].score is None
        assert rated.traits[0].score == 0
        assert [(t, p.key) for t, p in rated.judgments_due] == [("t", "judgment")]

    def test_a_judgment_given_counts(self):
        rated = _score([{"kind": "judgment", "rubric": "R"}, {"kind": "prep"}], [], judgments={"t": {"judgment": 80}})

        assert rated.traits[0].score == 40
        assert rated.judgments_due == []

    def test_count_defaults_its_interval_to_the_window(self):
        part = _part({"kind": "count", "target": 2}, [_event(1), _event(20, goal_ids=["p1v"]), _event(40)])

        assert part.score == 100
        assert part.said == "2 of 2 events in the last 30 days"
        assert part.event_ids == ["e1", "e20"]

    def test_follow_through_counts_the_goals_cancellations(self):
        cancelled = [replace(_event(0.5), status="cancelled")]

        part = _part({"kind": "follow_through"}, [], cancelled=cancelled)

        assert part.score == 75
        assert part.event_ids == ["e0.5"]

    def test_a_bad_part_isnt_rated(self):
        part = _part({"kind": "prep", "target": -1}, [_event(1, for_goal_ids=["p1"])])

        assert part.score is None
        assert part.said == 'Not rated: it "target" must be a number above 0'


class TestRating:
    def test_traits_and_parts_are_weighted_means(self):
        traits = [
            _trait({"kind": "novelty"}, {"kind": "attention", "weight": 3}, trait_id="a"),
            _trait({"kind": "prep"}, trait_id="b"),
        ]
        events = [_event(1, new="place", attention=1)]

        rated = score_traits(
            PERSON, {"kind": "traits", "traits": "all", "weights": {"b": 3}}, traits, WINDOW, events, [], TREE
        )

        # a: (100 + 33 × 3) / 4 = 50; b: 0; rating: (50 + 0 × 3) / 4.
        assert [t.score for t in rated.traits] == [50, 0]
        assert rated.rating == 12
        assert rated.explanation == "Traits (A 50, B 0×3) → 12"
        assert rated.metrics() == {
            "traits": {"a": 50, "b": 0},
            "parts": {"a": {"novelty": 100, "attention": 33}, "b": {"prep": 0}},
            "window_days": 30,
        }

    def test_off_archived_and_unknown_traits_are_left_out(self):
        traits = [_trait({"kind": "prep"}, trait_id="a"), _trait({"kind": "prep"}, trait_id="b", status="off")]

        chosen, left_out = selected_traits({"traits": ["a", "b", "c"]}, traits)

        assert [t.id for t in chosen] == ["a"]
        assert left_out == ["b", "c"]
        assert [t.id for t in selected_traits({"traits": "all"}, traits)[0]] == ["a"]

    def test_nothing_to_rate_is_a_skip(self):
        rated = _score([{"kind": "attention"}], [])

        assert rated.rating == "skip"
        assert rated.explanation == "No trait had anything to rate it by (T –) → skip"

    def test_the_seed_traits_rate_a_month_with_a_person(self):
        events = [
            _event(2, hours=3, activity="salsa social", new="place", creative=2, effort=1, attention=3),
            _event(9, goal_ids=["o1"], for_goal_ids=["p1"], effort=2),
            _event(-4),
        ]

        rated = score_traits(PERSON, PERSON.measure, SEED_TRAITS, WINDOW, events, [], TREE)

        assert {t.trait_id: t.score for t in rated.traits} == {
            "thoughtful": 38, "reliable": 100, "creative": 100, "adventurous": 100, "generous": 100,
        }
        assert rated.rating == 88


def test_reach_covers_every_parts_look_back_and_ahead():
    back, ahead, cancelled = reach({"traits": "all", "window_days": 20}, SEED_TRAITS)

    assert (back, ahead, cancelled) == (timedelta(days=30), timedelta(days=14), True)


def test_trait_history_is_the_daily_mean_across_goals():
    day = date(2026, 10, 1)
    assessments = [
        Assessment(goal_id="p1", day=day, rating=80, method="metric", metrics={"traits": {"generous": 80, "reliable": None}}),
        Assessment(goal_id="p2", day=day, rating=60, method="metric", metrics={"traits": {"generous": 61}}),
        Assessment(goal_id="p1", day=day + timedelta(days=1), rating=50, method="metric", metrics={"traits": {"generous": 50}}),
        Assessment(goal_id="o1", day=day, rating=90, method="metric", metrics={"minutes": 90}),
    ]

    history = trait_history(assessments, SEED_TRAITS)

    assert [(h.trait_id, h.day, h.score, [g.goal_id for g in h.goals]) for h in history] == [
        ("generous", day, 70, ["p1", "p2"]),
        ("generous", day + timedelta(days=1), 50, ["p1"]),
    ]
    assert history[0].name == "Generous"
    assert trait_history(assessments, SEED_TRAITS, ["reliable"]) == []
