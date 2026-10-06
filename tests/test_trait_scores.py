from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from calendar_clients.google_calendar import Event
from utilities.action_groups import ActionGroup, GroupTree
from utilities.actions import Action, ActionTree
from utilities.facts import Facts
from utilities.people import Person
from utilities.trait_scores import reach, score_person, traits_for
from utilities.traits import Trait

DAY_START = datetime(2026, 10, 5, tzinfo=timezone.utc)
DAY = (DAY_START, DAY_START + timedelta(days=1))
SAM = Person(id="sam", name="Sam")
ME = Person(id="self", name="Me")
TREE = ActionTree(
    [Action(id="visit", name="Visit", group_id="social"), Action(id="call", name="Call", group_id="social"),
     Action(id="cook", name="Cook")],
    GroupTree([ActionGroup(id="social", name="Social")]),
)
JUDGED = {"kind": "judgment", "rubric": "R?", "ratings": {"0": "no", "1": "a bit", "2": "yes", "3": "very"}, "facts": []}


def _event(event_id, days_before_end, hours=2, with_ids=(), for_ids=(), actions=(), judgments=None, status=None):
    start = DAY[1] - timedelta(days=days_before_end)
    return Event(
        id=event_id, summary="x", start=start, end=start + timedelta(hours=hours), status=status,
        action_ids=list(actions), judgments=judgments,
        facts=Facts(with_ids=list(with_ids), for_ids=list(for_ids)) if with_ids or for_ids else None,
    )


def _score(part, events, cancelled=(), person=SAM):
    trait = Trait(id="t", name="T", status="active", parts=[part])
    (scored,) = score_person(person, [trait], DAY, list(events), list(cancelled), TREE)
    return scored.parts[0]


class TestJudgments:
    def test_the_mean_of_the_persons_judgments_in_the_window(self):
        events = [
            _event("e1", 1, with_ids=["sam"], judgments={"sam": {"t": {"judgment": {"rating": 3, "scale": 3}}}}),
            _event("e2", 5, with_ids=["sam"], judgments={"sam": {"t": {"judgment": {"rating": 1, "scale": 3}}}}),
            _event("e3", 40, with_ids=["sam"], judgments={"sam": {"t": {"judgment": {"rating": 0, "scale": 3}}}}),
            _event("e4", 2, with_ids=["sam"], judgments={"self": {"t": {"judgment": {"rating": 0, "scale": 3}}}}),
        ]

        part = _score(JUDGED, events)

        assert (part.score, part.said) == (67, "Mean of 2 judgment(s) in the last 30 days")

    def test_a_for_part_reads_events_done_for_them_and_its_window(self):
        events = [
            _event("e1", 1, for_ids=["sam"], judgments={"sam": {"t": {"judgment": {"rating": 1, "scale": 1}}}}),
            _event("e2", 10, for_ids=["sam"], judgments={"sam": {"t": {"judgment": {"rating": 0, "scale": 1}}}}),
        ]

        assert _score({**JUDGED, "engagement_type": "for", "window_days": 7}, events).score == 100

    def test_without_judgments_theres_nothing_to_score(self):
        assert _score(JUDGED, [_event("e1", 1, with_ids=["sam"])]).score is None


class TestComputedParts:
    @pytest.mark.parametrize("days, score", [((3, -5), 100), ((3,), 50), ((-5,), 50), ((20, -20), 0), ((), 0)])
    def test_continuity_wants_a_recent_and_an_upcoming_event(self, days, score):
        events = [_event(f"e{d}", d, with_ids=["sam"]) for d in days]

        assert _score({"kind": "continuity"}, events).score == score

    def test_the_user_was_at_every_event(self):
        part = _score({"kind": "continuity"}, [_event("e1", 3), _event("e2", -5)], person=ME)

        assert part.said == "Last 3 days ago; next in 5 days (within 14 and 14 days)"
        assert part.score == 100

    def test_count_against_its_target_narrowed_to_an_action_or_group(self):
        events = [
            _event("e1", 1, with_ids=["sam"], actions=["visit"]),
            _event("e2", 4, with_ids=["sam"], actions=["call"]),
            _event("e3", 6, with_ids=["sam"], actions=["cook"]),
            _event("e4", 2, actions=["visit"]),  # not with Sam
        ]

        assert _score({"kind": "count", "target": 2, "action": "visit"}, events).score == 50
        assert _score({"kind": "count", "target": 2, "action": "social"}, events).score == 100
        assert _score({"kind": "count", "target": 4}, events).said == "3 of 4 events in the last 30 days"

    def test_count_with_zero_at_days_falls_from_when_it_was_last_met(self):
        events = [_event("e1", 25, with_ids=["sam"])]

        part = _score({"kind": "count", "target": 1, "interval_days": 14, "zero_at_days": 28}, events)

        # Met until its end (2 hours in) left the window, nearly 11 days ago;
        # 0 once 14 more days pass.
        assert part.score == 22

    def test_duration_counts_minutes(self):
        events = [_event("e1", 1, hours=1, with_ids=["sam"]), _event("e2", 2, hours=2, with_ids=["sam"])]

        assert _score({"kind": "duration", "target_min": 360, "interval_days": 7}, events).score == 50

    def test_follow_through_loses_for_cancellations_and_regains_for_kept_ones(self):
        cancelled = [_event("c1", 0.5, with_ids=["sam"], status="cancelled")]
        kept = [_event("e1", 3, with_ids=["sam"])]

        part = _score({"kind": "follow_through"}, kept, cancelled)

        assert part.score == 75
        assert part.said == "1 cancelled (−25) that day, from 100"

    def test_a_cancellation_merged_into_a_kept_event_isnt_counted(self):
        cancelled = [_event("c1", 0.5, with_ids=["sam"], status="cancelled")]
        kept = [replace(cancelled[0], id="e1", status=None)]

        assert _score({"kind": "follow_through"}, kept, cancelled).score == 100

    def test_a_bad_part_isnt_scored(self):
        part = _score({"kind": "count", "target": 0}, [])

        assert (part.score, part.weight) == (None, 0)


class TestTraits:
    def test_a_trait_is_the_weighted_mean_of_its_scored_parts(self):
        trait = Trait(id="t", name="T", status="active", parts=[
            {"kind": "count", "target": 1}, {"kind": "continuity", "weight": 3}, JUDGED,
        ])
        events = [_event("e1", 1, with_ids=["sam"])]

        (scored,) = score_person(SAM, [trait], DAY, events, [], TREE)

        # count 100, continuity 50 (none ahead) x3, judgment unscored: 62.5
        assert scored.score == 62
        assert [p.score for p in scored.parts] == [100, 50, None]

    def test_a_persons_traits_select_and_replace(self):
        traits = [
            Trait(id="a", name="A", status="active", parts=[{"kind": "continuity"}]),
            Trait(id="b", name="B", status="active", parts=[{"kind": "continuity"}]),
            Trait(id="off", name="Off", status="off", parts=[{"kind": "continuity"}]),
        ]
        person = Person(id="sam", name="Sam", traits={"select": ["b"], "parts": {"b": [{"kind": "follow_through"}]}})

        assert [(t.id, [p["kind"] for p in parts]) for t, parts in traits_for(person, traits)] == [("b", ["follow_through"])]
        assert [t.id for t, _ in traits_for(SAM, traits)] == ["a", "b"]

    def test_reach_covers_every_parts_look_back_and_ahead(self):
        back, ahead = reach([
            {"kind": "continuity", "last_within_days": 10, "next_within_days": 21},
            {"kind": "count", "target": 1, "interval_days": 14, "zero_at_days": 60},
            JUDGED,
        ])

        assert (back, ahead) == (timedelta(days=60), timedelta(days=21))
