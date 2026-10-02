from datetime import date, datetime

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
    def test_with_no_period_named_it_offers_completed_unreflected_ones(self):
        reflections, *_ = _setup([_COOKING])

        context = reflections.prepare("weekly")

        assert context.period is None
        assert not context.due
        # It's Friday Oct 2: this week isn't over. The goals were created
        # Sep 1, so the week of Aug 30 is the oldest; it isn't offered.
        assert [(c.period, c.first_day, c.last_day) for c in context.choices] == [
            ("week-2026-09-20", date(2026, 9, 20), date(2026, 9, 26)),
            ("week-2026-09-13", date(2026, 9, 13), date(2026, 9, 19)),
            ("week-2026-09-06", date(2026, 9, 6), date(2026, 9, 12)),
        ]
        assert context.older_unreflected == 1
        assert "Ask which week" in context.instructions

    def test_when_every_recent_period_is_reflected_it_offers_the_last_one_again(self):
        reflections, *_ = _setup([_COOKING])
        for week in ["08-30", "09-06", "09-13", "09-20"]:
            reflections.record("weekly", f"week-2026-{week}", [], dry_run=False)

        context = reflections.prepare("weekly")

        assert [(c.period, c.already_reflected) for c in context.choices] == [("week-2026-09-20", True)]
        assert context.older_unreflected == 0

    def test_a_daily_reflection_offers_completed_days_bounded_by_sleep(self):
        reflections, *_ = _setup([_WAKE])
        unreflected = {"09-21", "09-24", "09-25", "09-28"}
        for day in range(1, 31):
            if f"09-{day:02d}" not in unreflected:
                reflections.record("daily", f"2026-09-{day:02d}", [], dry_run=False)
        reflections.record("daily", "2026-10-01", [], dry_run=False)

        context = reflections.prepare("daily")

        # It's 21:00 on Oct 2, which isn't over: the newest three completed
        # days without a reflection, each ending at 7am with no sleep logged.
        assert [(c.period, c.ends) for c in context.choices] == [
            ("2026-09-28", datetime(2026, 9, 29, 7, tzinfo=TZ)),
            ("2026-09-25", datetime(2026, 9, 26, 7, tzinfo=TZ)),
            ("2026-09-24", datetime(2026, 9, 25, 7, tzinfo=TZ)),
        ]
        assert context.older_unreflected == 1
        assert "Ask which day" in context.instructions

    def test_a_named_period_can_be_reflected_on_again(self):
        reflections, *_ = _setup([_COOKING])
        reflections.record("weekly", "week-2026-09-20", [], journal="good week", dry_run=False)

        context = reflections.prepare("weekly", "week-2026-09-20")

        assert context.already_reflected
        assert context.journal == "good week"

    def test_a_days_events_run_from_waking_to_waking(self):
        sleep_into_1st = _event("2026-09-30T23:30", "2026-10-01T07:15", is_end_of_day_sleep=True)
        sleep_into_2nd = _event("2026-10-02T00:45", "2026-10-02T08:00", is_end_of_day_sleep=True)
        late = _event("2026-10-02T00:10", "2026-10-02T00:40")  # After midnight, before sleep.
        early = _event("2026-10-01T06:00", "2026-10-01T07:00")  # Before waking: the day before.
        reflections, *_ = _setup([_WAKE], events=[sleep_into_1st, sleep_into_2nd, late, early])

        context = reflections.prepare("daily", "2026-10-01")

        assert (context.starts, context.ends) == (sleep_into_1st.end, sleep_into_2nd.end)
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
                _note("2026-10-02T06:30", "not compacted yet"),  # Oct 1's day: before waking
            ],
        )
        calendar.events = [_event("2026-09-22T18:00", "2026-09-22T20:30", [goals["Cooking"].id])]
        calendar.events[0].summary = "Dinner"

        weekly = reflections.prepare("weekly", "week-2026-09-20")
        monthly = reflections.prepare("monthly", "2026-09")

        assert "Tue 09-22\n  18:00-20:30 Dinner [Cooking]" in weekly.events_digest
        assert weekly.notes_digest == "Tue 09-22 18:15 started the curry"
        assert weekly.uncompacted_notes == 0  # the uncompacted one is after the week
        assert monthly.events_digest is None and monthly.notes_digest is None
        assert reflections.prepare("daily", "2026-10-01").uncompacted_notes == 1

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


class TestSleepBoundaries:
    def test_a_period_whose_sleep_isnt_logged_is_refused_and_flagged(self):
        reflections, _, _, calendar, _ = _setup([_WAKE])
        calendar.nightly_sleep = False
        calendar.events = [
            _event("2026-09-29T23:00", "2026-09-30T07:00", is_end_of_day_sleep=True),
            _event("2026-09-30T23:00", "2026-10-01T07:00", is_end_of_day_sleep=True),
        ]

        with pytest.raises(ValueError, match="No end-of-day sleep ends on 2026-09-29"):
            reflections.prepare("daily", "2026-09-29")
        # Waking on the 2nd isn't logged, so the 1st isn't over.
        with pytest.raises(ValueError, match="2026-10-01 isn't over yet: it ends when you wake on 2026-10-02"):
            reflections.prepare("daily", "2026-10-01")
        choices = {c.period: c for c in reflections.prepare("daily").choices}

        assert choices["2026-09-30"].missing_sleep == []
        assert choices["2026-09-30"].starts == datetime(2026, 9, 30, 7, tzinfo=TZ)
        assert choices["2026-09-29"].missing_sleep == [date(2026, 9, 29)]
        assert choices["2026-09-29"].starts is None

    def test_a_period_still_going_on_cant_be_prepared_or_recorded(self):
        reflections, *_ = _setup([_WAKE])

        with pytest.raises(ValueError, match="isn't over yet"):
            reflections.prepare("daily", "2026-10-02")
        with pytest.raises(ValueError, match="isn't over yet"):
            reflections.record("daily", "2026-10-02", [], dry_run=False)
