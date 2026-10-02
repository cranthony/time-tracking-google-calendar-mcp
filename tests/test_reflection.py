from datetime import datetime

import pytest

from tests.fake_sheets import FakeSheets
from tests.test_goal_health import NOW, TODAY, TZ, FakeCalendar, _event
from utilities.goal_health import Assessment, GoalHealth
from utilities.goal_sheet import Goal
from utilities.goals import Goals
from utilities.noted_time_sheet import NotedTime
from utilities.reflection import Reflections, reflection_event_id

# TODAY is Friday 2026-10-02; the last week to end is week-2026-09-20.


class FakeNotes:
    def __init__(self, notes=()):
        self.notes = list(notes)

    def read(self, *, include_compacted=False):
        return [n for n in self.notes if include_compacted or n.compaction_id is None]


def _note(at: str, description: str, compaction_id=None) -> NotedTime:
    return NotedTime(
        timestamp=datetime.fromisoformat(at).replace(tzinfo=TZ), description=description, compaction_id=compaction_id
    )


def _setup(goals=(), events=(), notes=()):
    calendar = FakeCalendar(events)
    store = Goals(calendar, FakeSheets(), today=lambda: TODAY)
    for goal in goals:
        store.create_goal(goal)
    # Created long enough ago to have periods to reflect on.
    tree = store.tree()
    for goal in tree.goals:
        goal.created = TODAY.replace(month=9, day=1)
    store._sheet.write(tree.goals)
    health = GoalHealth(calendar, store, now=lambda: NOW)
    by_name = {g.name: g for g in store.tree().goals}
    return Reflections(health, store, FakeNotes(notes)), health, store, calendar, by_name


def _rating(goal: Goal, period: str, rating=80, **fields) -> Assessment:
    return Assessment(goal_id=goal.id, cadence=goal.cadence, period=period, rating=rating, method="subjective", **fields)


_COOKING = Goal(name="Cooking", cadence="weekly", measure={"kind": "duration", "target_min": 300})
_FEEL = Goal(name="Feel", cadence="weekly", measure={"kind": "subjective", "prompt": "How was it?"})
_WAKE = Goal(name="Wake", cadence="daily", measure={"kind": "subjective"})


class TestPrepare:
    def test_defaults_to_the_oldest_ended_period_with_no_reflection(self):
        reflections, *_ = _setup([_COOKING])

        first = reflections.prepare("weekly")

        assert first.period == "week-2026-08-30"  # the goals were created 2026-09-01
        assert not first.already_reflected
        reflections.record("weekly", first.period, [], dry_run=False)
        assert reflections.prepare("weekly").period == "week-2026-09-06"

    def test_when_every_recent_period_is_reflected_it_offers_the_last_one_again(self):
        reflections, *_ = _setup([_COOKING])
        for week in ["08-30", "09-06", "09-13"]:
            reflections.record("weekly", f"week-2026-{week}", [], dry_run=False)
        reflections.record("weekly", "week-2026-09-20", [], journal="good week", dry_run=False)

        context = reflections.prepare("weekly")

        assert context.period == "week-2026-09-20"
        assert context.already_reflected
        assert context.journal == "good week"

    def test_a_daily_reflection_with_no_day_named_offers_days_to_ask_about(self):
        reflections, *_ = _setup([_WAKE])
        unreflected = {"09-21", "09-24", "09-25", "09-28"}
        for day in range(1, 31):
            if f"09-{day:02d}" not in unreflected:
                reflections.record("daily", f"2026-09-{day:02d}", [], dry_run=False)
        reflections.record("daily", "2026-10-01", [], dry_run=False)

        context = reflections.prepare("daily")

        assert context.period is None
        # It's 21:00 on Oct 2: that day, still going on, then the newest
        # three without a reflection; one older one isn't offered.
        assert [(c.period, c.current, c.ends) for c in context.choices] == [
            ("2026-10-02", True, None),
            ("2026-09-28", False, datetime(2026, 9, 29, tzinfo=TZ)),
            ("2026-09-25", False, datetime(2026, 9, 26, tzinfo=TZ)),
            ("2026-09-24", False, datetime(2026, 9, 25, tzinfo=TZ)),
        ]
        assert context.older_unreflected == 1
        assert "Ask which day" in context.instructions

    def test_a_days_events_run_from_waking_to_waking(self):
        sleep_into_2nd = _event("2026-10-01T23:30", "2026-10-02T07:15", is_end_of_day_sleep=True)
        sleep_into_3rd = _event("2026-10-03T00:45", "2026-10-03T08:00", is_end_of_day_sleep=True)
        late = _event("2026-10-03T00:10", "2026-10-03T00:40")  # After midnight, before sleep.
        early = _event("2026-10-02T06:00", "2026-10-02T07:00")  # Before waking: the day before.
        reflections, *_ = _setup([_WAKE], events=[sleep_into_2nd, sleep_into_3rd, late, early])

        context = reflections.prepare("daily", "2026-10-02")

        assert (context.starts, context.ends) == (sleep_into_2nd.end, sleep_into_3rd.end)
        assert "00:10-00:40" in context.events_digest  # The night's late event...
        assert "00:45-08:00" in context.events_digest  # ...and its sleep.
        assert "06:00-07:00" not in context.events_digest

    def test_rates_active_goals_with_the_cadence_proposing_measured_or_recorded_ratings(self):
        reflections, health, store, calendar, goals = _setup([_COOKING, _FEEL, _WAKE])
        cooking, feel = goals["Cooking"], goals["Feel"]
        store.create_goal(Goal(name="Paused", cadence="weekly", status="inactive"))
        calendar.events = [_event("2026-09-22T18:00", "2026-09-22T20:30", [cooking.id])]
        health.confirm_assessments([_rating(feel, "week-2026-09-13", 60)])
        health.record_assessments([_rating(feel, "week-2026-09-20", 75, rationale="said in passing")])

        context = reflections.prepare("weekly", "week-2026-09-20")

        due = {d.path: d for d in context.due}
        assert set(due) == {"Cooking", "Feel"}  # not the paused one, nor the daily one
        assert due["Cooking"].proposed.explanation == "2h 30m of 5h target → 50"
        assert due["Feel"].proposed.rating == 75 and due["Feel"].proposed.status == "proposed"
        assert [(r.period, r.rating) for r in due["Feel"].recent] == [("week-2026-09-13", 60)]

    def test_a_longer_reflection_reviews_shorter_cadence_goals(self):
        reflections, health, _, _, goals = _setup([_COOKING, _WAKE])
        wake = goals["Wake"]
        health.confirm_assessments([_rating(wake, "2026-09-21", 90), _rating(wake, "2026-09-22", 70)])

        weekly = reflections.prepare("weekly", "week-2026-09-20")
        daily = reflections.prepare("daily", "2026-09-21")

        (reviewed,) = weekly.reviewed
        assert (reviewed.path, reviewed.mean) == ("Wake", 80)
        assert [r.period for r in reviewed.ratings] == ["2026-09-21", "2026-09-22"]
        assert daily.reviewed == []

    def test_counts_time_per_goal_including_sub_goals(self):
        reflections, _, store, calendar, goals = _setup([_COOKING])
        cooking = goals["Cooking"]
        store.create_goal(Goal(name="Tofu", parent_id=cooking.id))
        tofu = next(g for g in store.tree().goals if g.name == "Tofu")
        calendar.events = [
            _event("2026-09-22T18:00", "2026-09-22T19:00", [tofu.id]),
            _event("2026-09-23T18:00", "2026-09-23T18:30", [cooking.id]),
        ]

        context = reflections.prepare("weekly", "week-2026-09-20")

        assert [(t.path, t.minutes) for t in context.goal_time] == [("Cooking", 90), ("Cooking › Tofu", 60)]

    def test_digests_the_periods_events_and_notes_for_short_cadences_only(self):
        reflections, _, _, calendar, goals = _setup(
            [_COOKING, Goal(name="Home", cadence="monthly", measure={"kind": "subjective"})],
            notes=[
                _note("2026-09-22T18:15", "started the curry", compaction_id="c1"),
                _note("2026-10-02T08:00", "not compacted yet"),
            ],
        )
        calendar.events = [_event("2026-09-22T18:00", "2026-09-22T20:30", [goals["Cooking"].id])]
        calendar.events[0].summary = "Dinner"

        weekly = reflections.prepare("weekly", "week-2026-09-20")
        monthly = reflections.prepare("monthly", "2026-09")

        assert weekly.events_digest == "Tue 09-22\n  18:00-20:30 Dinner [Cooking]"
        assert weekly.notes_digest == "Tue 09-22 18:15 started the curry"
        assert weekly.uncompacted_notes == 0  # the uncompacted one is after the week
        assert monthly.events_digest is None and monthly.notes_digest is None
        assert reflections.prepare("daily", "2026-10-02").uncompacted_notes == 1

    def test_brings_back_the_previous_reflections_intentions(self):
        reflections, *_ = _setup([_FEEL])
        reflections.record("weekly", "week-2026-09-13", [], intentions=["cook twice", " "], dry_run=False)

        context = reflections.prepare("weekly", "week-2026-09-20")

        assert context.previous_intentions == ["cook twice"]
        assert "record_reflection" in context.instructions

    def test_refuses_a_period_that_hasnt_started(self):
        reflections, *_ = _setup([_FEEL])

        with pytest.raises(ValueError, match="hasn't started yet"):
            reflections.prepare("weekly", "week-2026-10-04")


class TestRecord:
    def test_a_dry_run_previews_without_writing(self):
        reflections, _, _, calendar, goals = _setup([_COOKING, _FEEL])
        cooking = goals["Cooking"]
        rating = _rating(cooking, "week-2026-09-20", 50, explanation="2h 30m of 5h target → 50")

        result = reflections.record("weekly", "week-2026-09-20", [rating], journal="busy week", intentions=["rest"])

        assert result.status == "preview"
        assert result.preview.splitlines() == [
            "🟡 Cooking: 50 — 2h 30m of 5h target → 50",
            "· Feel: not rated",
            "Journal: busy week",
            "Intention: rest",
        ]
        assert result.not_rated == ["Feel"]
        assert calendar.health_events == {}

    def test_committing_confirms_the_ratings_and_records_the_reflection(self):
        reflections, _, store, calendar, goals = _setup([_COOKING, _FEEL])
        cooking, feel = goals["Cooking"], goals["Feel"]

        result = reflections.record(
            "weekly",
            "week-2026-09-20",
            [_rating(cooking, "week-2026-09-20", 50), _rating(feel, "week-2026-09-20", "skip")],
            journal="busy week",
            intentions=["rest"],
            dry_run=False,
        )

        assert result.status == "recorded"
        assert {a.status for a in result.assessments} == {"confirmed"}
        reflection = calendar.health_events[reflection_event_id("weekly", "week-2026-09-20")]
        assert reflection["description"] == "busy week"
        assert reflection["summary"] == "📝 weekly reflection · week-2026-09-20"
        listed = {g.name: g for g in store.get_goals().goals}
        assert listed["Cooking"].health == 50  # the cache follows confirmed ratings

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({"assessments": "wrong period"}, "rates weekly week-2026-09-20, not weekly week-2026-09-13"),
            ({"assessments": "twice"}, "rated more than once"),
            ({"intentions": ["a", "b", "c", "d"]}, "at most 3 intentions"),
            ({"journal": "x" * 8001}, "journal is longer than 8000 bytes"),
        ],
    )
    def test_refuses_what_it_cant_record_and_writes_nothing(self, kwargs, message):
        reflections, _, _, calendar, goals = _setup([_COOKING])
        cooking = goals["Cooking"]
        assessments = {
            "wrong period": [_rating(cooking, "week-2026-09-13")],
            "twice": [_rating(cooking, "week-2026-09-20"), _rating(cooking, "week-2026-09-20")],
        }.get(kwargs.pop("assessments", None), [])

        with pytest.raises(ValueError, match=message):
            reflections.record("weekly", "week-2026-09-20", assessments, dry_run=False, **kwargs)

        assert calendar.health_events == {}
