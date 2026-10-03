import base64
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from calendar_clients.google_calendar import Event
from tests.fake_sheets import FakeSheets
from tests.test_goals import FakeLabelCalendar, _UNNAMED
from utilities.goal_health import Assessment, GoalHealth, assessment_event_id, band
from utilities.goal_periods import last_ended, parse_period, period_containing
from utilities.goal_sheet import Goal
from utilities.goals import Goals

TZ = ZoneInfo("America/New_York")
TODAY = date(2026, 10, 2)  # A Friday.
NOW = datetime.combine(TODAY, time(21), TZ)


class FakeCalendar(FakeLabelCalendar):
    """The main calendar (its events, labels and metadata), and the Goal
    Health calendar it creates, with all-day events filtered by date range
    and private property the way the API does."""

    def __init__(self, events=()):
        super().__init__([_UNNAMED])
        self.events = list(events)
        # A night's end-of-day sleep, 23:00-07:00, on every date with none
        # in `events` -- so periods have the sleeps that bound them (see
        # utilities/sleep_days.py). False for none.
        self.nightly_sleep = True
        self.health_events: dict[str, dict] = {}
        self.created: list[tuple] = []
        self.hidden: list[str] = []
        self.calendar_id = "main"

    def get_time_zone(self):
        return TZ

    def create_calendar(self, summary, description=None, time_zone=None):
        self.created.append((summary, time_zone))
        return "health-calendar"

    def hide_calendar(self, calendar_id):
        self.hidden.append(calendar_id)
        return True

    def for_calendar(self, calendar_id):
        assert calendar_id == "health-calendar"
        return self

    def list_events(self, time_min, time_max):
        events = list(self.events)
        if self.nightly_sleep:
            slept = {e.end.astimezone(TZ).date() for e in events if e.is_end_of_day_sleep}
            day = time_min.astimezone(TZ).date()
            while day <= time_max.astimezone(TZ).date():
                wake = datetime.combine(day, time(7), TZ)
                if day not in slept and wake <= NOW:  # Only nights already slept.
                    events.append(
                        Event(id=f"night-{day}", summary="Sleep", start=wake - timedelta(hours=8), end=wake,
                              is_end_of_day_sleep=True)
                    )
                day += timedelta(days=1)
        return [e for e in events if e.end > time_min and e.start < time_max]

    def list_event_resources(self, time_min, time_max, *, private_property=None):
        key, _, value = (private_property or "=").partition("=")
        return [
            item
            for item in self.health_events.values()
            if date.fromisoformat(item["end"]["date"]) > time_min.date()
            and date.fromisoformat(item["start"]["date"]) < time_max.date()
            and (not key or item["extendedProperties"]["private"].get(key) == value)
        ]

    def upsert_event_resource(self, event_id, body):
        self.health_events[event_id] = {**self.health_events.get(event_id, {}), **body, "id": event_id}
        return self.health_events[event_id]


def _setup(goals=(), events=()):
    calendar = FakeCalendar(events)
    store = Goals(calendar, FakeSheets(), today=lambda: TODAY)
    for goal in goals:
        store.create_goal(goal)
    by_name = {g.name: g for g in store.tree().goals}
    return GoalHealth(calendar, store, now=lambda: NOW), store, calendar, by_name


def _event(start: str, end: str, goal_ids=None, **fields) -> Event:
    return Event(
        id=f"e-{start}",
        summary="x",
        start=datetime.fromisoformat(start).replace(tzinfo=TZ),
        end=datetime.fromisoformat(end).replace(tzinfo=TZ),
        goal_ids=goal_ids,
        **fields,
    )


def _assessment(goal: Goal, period: str, rating=80, **fields) -> Assessment:
    return Assessment(goal_id=goal.id, cadence=goal.cadence, period=period, rating=rating, method="subjective", **fields)


class TestEventIds:
    def test_encode_goal_cadence_and_period_reversibly(self):
        event_id = assessment_event_id("g7k2qp", "daily", "2026-09-30")

        assert event_id == "csrmmcjhe1u68ob9dhsnochg68r2qc1p5kpj0"
        assert set(event_id) <= set("0123456789abcdefghijklmnopqrstuv")
        padded = event_id.upper() + "=" * (-len(event_id) % 8)
        assert base64.b32hexdecode(padded).decode() == "g7k2qp|daily|2026-09-30"

    def test_differ_by_cadence_even_for_the_same_period_name(self):
        assert assessment_event_id("g", "daily", "x") != assessment_event_id("g", "weekly", "x")


class TestBand:
    @pytest.mark.parametrize("rating, emoji", [(0, "🔴"), (39, "🔴"), (40, "🟡"), (69, "🟡"), (70, "🟢"), ("skip", "⚪")])
    def test_bands(self, rating, emoji):
        assert band(rating) == emoji


class TestRecordAssessments:
    def test_records_as_proposed_on_a_hidden_calendar_in_the_main_ones_time_zone(self):
        health, _, calendar, goals = _setup([Goal(name="Cooking", cadence="daily")])
        cooking = goals["Cooking"]

        (recorded,) = health.record_assessments([_assessment(cooking, "2026-10-01", status="confirmed")])

        assert recorded.status == "proposed"
        assert recorded.assessed == NOW
        assert calendar.created == [("Goal Health", "America/New_York")]
        assert calendar.hidden == ["health-calendar"]
        assert calendar.metadata["goal-health-calendar"] == "health-calendar"
        (item,) = calendar.health_events.values()
        assert item["summary"] == "🟢 Cooking · 2026-10-01 · 80 (proposed)"
        assert (item["start"], item["end"]) == ({"date": "2026-10-01"}, {"date": "2026-10-02"})
        assert item["transparency"] == "transparent"

    def test_recording_a_period_again_replaces_it(self):
        health, _, calendar, goals = _setup([Goal(name="Cooking", cadence="weekly")])
        cooking = goals["Cooking"]

        health.record_assessments([_assessment(cooking, "week-2026-09-20", rating=50)])
        health.record_assessments([_assessment(cooking, "week-2026-09-20", rating="skip", rationale="sick")])

        (item,) = calendar.health_events.values()
        assert item["summary"] == "⚪ Cooking · week-2026-09-20 · skipped (proposed)"
        assert item["description"] == "sick"
        assert calendar.created == [("Goal Health", "America/New_York")]  # created once

    @pytest.mark.parametrize(
        "change, message",
        [
            ({"goal_id": "nope"}, "isn't a goal"),
            ({"cadence": "weekly"}, "is assessed daily, not weekly"),
            ({"period": "2026-10"}, "isn't a period for the daily cadence"),
            ({"period": "2026-10-03"}, "hasn't started yet"),
            ({"rating": 101}, "0 to 100"),
            ({"rationale": "x" * 8001}, "longer than 8000 bytes"),
            ({"explanation": "x" * 1025}, "longer than 1024"),
        ],
    )
    def test_refuses_invalid_assessments_and_writes_none_of_the_batch(self, change, message):
        health, _, calendar, goals = _setup([Goal(name="Cooking", cadence="daily")])
        good = _assessment(goals["Cooking"], "2026-10-01")
        bad = Assessment(**{**_assessment(goals["Cooking"], "2026-09-30").__dict__, **change})

        with pytest.raises(ValueError, match=message):
            health.record_assessments([good, bad])

        assert calendar.health_events == {}

    def test_a_goal_without_a_cadence_isnt_assessed(self):
        health, _, _, goals = _setup([Goal(name="Cooking")])

        with pytest.raises(ValueError, match="has no cadence"):
            health.record_assessments(
                [Assessment(goal_id=goals["Cooking"].id, cadence="daily", period="2026-10-01", rating=5, method="llm")]
            )


class TestHistory:
    def test_lists_a_goals_assessments_by_period_defaulting_to_its_last_12_periods(self):
        health, _, _, goals = _setup([Goal(name="Cooking", cadence="weekly"), Goal(name="Reading", cadence="weekly")])
        cooking, reading = goals["Cooking"], goals["Reading"]
        week = last_ended("weekly", TODAY)
        old = week
        for _ in range(12):
            old = old.previous()  # 13 weeks back: outside the default range
        health.record_assessments(
            [
                _assessment(cooking, week.id, 90),
                _assessment(cooking, week.previous().id, 60),
                _assessment(cooking, old.id, 10),
                _assessment(reading, week.id, 30),
            ]
        )

        history = health.history([cooking.id])

        assert [(a.period, a.rating) for a in history] == [(week.previous().id, 60), (week.id, 90)]
        everything = health.history([cooking.id], start=old.start)
        assert [a.rating for a in everything] == [10, 60, 90]

    def test_is_empty_before_anything_is_recorded_without_creating_the_calendar(self):
        health, _, calendar, goals = _setup([Goal(name="Cooking", cadence="daily")])

        assert health.history([goals["Cooking"].id]) == []
        assert calendar.created == []


class TestConfirmAndCache:
    def test_confirming_updates_the_goals_health_and_a_skip_doesnt_replace_it(self):
        health, store, _, goals = _setup([Goal(name="Cooking", cadence="daily")])
        cooking = goals["Cooking"]

        health.confirm_assessments(
            [
                _assessment(cooking, "2026-09-29", 40),
                _assessment(cooking, "2026-09-30", 85),
                _assessment(cooking, "2026-10-01", "skip"),
            ]
        )

        listed = next(g for g in store.get_goals().goals if g.id == cooking.id)
        assert (listed.health, listed.health_period) == (85, "2026-10-01")
        assert listed.health_trend == "-,-,-,-,-,40,85,-"
        assert listed.stale_periods == 0

    def test_proposed_ratings_dont_count(self):
        health, store, _, goals = _setup([Goal(name="Cooking", cadence="daily")])
        cooking = goals["Cooking"]

        health.record_assessments([_assessment(cooking, "2026-10-01", 85)])
        health.rebuild_cache()

        listed = next(g for g in store.get_goals().goals if g.id == cooking.id)
        assert listed.health is None
        assert listed.stale_periods == 0  # created today: none of its days has ended yet

    def test_stale_periods_count_ended_periods_since_the_last_assessed_one(self):
        health, store, _, goals = _setup([Goal(name="Cooking", cadence="daily")])
        cooking = goals["Cooking"]

        health.confirm_assessments([_assessment(cooking, "2026-09-28", 70)])

        listed = next(g for g in store.get_goals().goals if g.id == cooking.id)
        assert listed.stale_periods == 3  # 29th, 30th and 1st have ended unassessed

    def test_a_tab_without_cache_columns_gains_them(self):
        health, store, _, goals = _setup([Goal(name="Cooking", cadence="daily")])
        sheet = store._sheet
        header = sheet._read_header()
        trimmed = [c for c in header if not c.startswith("health")]
        sheet._sheets_client.write_rows_in_sheet(
            sheet.spreadsheet_id, sheet._sheet_id, "A1:Z1", [trimmed + [""] * (len(header) - len(trimmed))]
        )

        health.confirm_assessments([_assessment(goals["Cooking"], "2026-10-01", 77)])

        assert "health" in sheet._read_header()
        assert store.tree().by_id[goals["Cooking"].id].health == 77


class TestMeasure:
    def _goals(self):
        return [
            Goal(name="Cooking", cadence="weekly", measure={"kind": "duration", "target_min": 300}),
            Goal(name="Hosting", cadence="weekly", measure={"kind": "count", "target": 1, "noun": "dinners"}),
            Goal(name="Wake 7am", cadence="daily", measure={"kind": "wake_time", "target": "07:00", "grace_min": 10, "zero_at_min": 60}),
            Goal(name="Feel", cadence="weekly", measure={"kind": "subjective", "prompt": "How was it?"}),
        ]

    def test_duration_counts_minutes_of_the_goal_and_its_sub_goals_within_the_period(self):
        health, store, calendar, goals = _setup(self._goals())
        store.create_goal(Goal(name="Tofu", parent_id=goals["Cooking"].id))
        tofu = next(g for g in store.tree().goals if g.name == "Tofu")
        cooking = goals["Cooking"]
        calendar.events = [
            _event("2026-09-21T18:00", "2026-09-21T20:00", [cooking.id]),  # 120
            _event("2026-09-23T18:00", "2026-09-23T19:10", [tofu.id]),  # 70, via its sub-goal
            _event("2026-09-27T06:30", "2026-09-27T07:30", [cooking.id]),  # 30 inside the week, which ends at 7am
            _event("2026-09-24T18:00", "2026-09-24T19:00", ["other"]),  # not this goal
            _event("2026-09-25T18:00", "2026-09-25T19:00", [cooking.id], status="cancelled"),
        ]

        proposals = {a.goal_id: a for a in health.measure("weekly")}

        cooked = proposals[cooking.id]
        assert cooked.period == "week-2026-09-20"
        assert (cooked.rating, cooked.method, cooked.status) == (73, "metric", "proposed")
        assert cooked.explanation == "3h 40m of 5h target → 73"
        assert cooked.metrics == {"minutes": 220, "target_min": 300}
        assert "Feel" not in {store.tree().by_id[i].name for i in proposals}  # subjective: not measured

    def test_a_measure_can_look_at_other_goals_events_instead(self):
        health, store, calendar, goals = _setup(self._goals())
        cooking, hosting = goals["Cooking"], goals["Hosting"]
        store.create_goal(Goal(name="Brunch", parent_id=hosting.id))
        brunch = next(g for g in store.tree().goals if g.name == "Brunch")
        store.update_goal(
            Goal(id=cooking.id, measure={"kind": "duration", "target_min": 300, "goal_ids": [hosting.id]})
        )
        calendar.events = [
            _event("2026-09-21T18:00", "2026-09-21T20:00", [cooking.id]),  # its own: not counted
            _event("2026-09-23T18:00", "2026-09-23T19:00", [hosting.id]),  # 60
            _event("2026-09-24T10:00", "2026-09-24T11:30", [brunch.id]),  # 90, Hosting's sub-goal
        ]

        (cooked,) = health.measure("weekly", goal_ids=[cooking.id])

        assert cooked.metrics == {"minutes": 150, "target_min": 300}

    def test_a_measure_can_leave_out_sub_goals_events(self):
        health, store, calendar, goals = _setup(self._goals())
        cooking = goals["Cooking"]
        store.create_goal(Goal(name="Tofu", parent_id=cooking.id))
        tofu = next(g for g in store.tree().goals if g.name == "Tofu")
        store.update_goal(
            Goal(id=cooking.id, measure={"kind": "duration", "target_min": 300, "include_sub_goals": False})
        )
        calendar.events = [
            _event("2026-09-21T18:00", "2026-09-21T20:00", [cooking.id]),  # 120
            _event("2026-09-23T18:00", "2026-09-23T19:10", [tofu.id]),  # its sub-goal: left out
        ]

        (cooked,) = health.measure("weekly", goal_ids=[cooking.id])

        assert cooked.metrics == {"minutes": 120, "target_min": 300}

    def test_a_sub_goal_can_look_at_its_parents_events(self):
        health, store, calendar, goals = _setup(self._goals())
        cooking = goals["Cooking"]
        store.create_goal(
            Goal(
                name="Tofu",
                parent_id=cooking.id,
                cadence="weekly",
                measure={"kind": "count", "target": 4, "goal_ids": [cooking.id]},
            )
        )
        tofu = next(g for g in store.tree().goals if g.name == "Tofu")
        calendar.events = [
            _event("2026-09-21T18:00", "2026-09-21T20:00", [cooking.id]),
            _event("2026-09-23T18:00", "2026-09-23T19:00", [tofu.id]),
        ]

        (counted,) = health.measure("weekly", goal_ids=[tofu.id])

        assert counted.metrics == {"count": 2, "target": 4}

    def test_count_and_an_events_label_standing_in_for_its_goal(self):
        health, store, calendar, goals = _setup(self._goals())
        hosting = goals["Hosting"]
        # Written before goals: only a label.
        calendar.events = [_event("2026-09-26T18:00", "2026-09-26T21:00", event_label_id=hosting.label_id)]

        (hosted,) = health.measure("weekly", goal_ids=[hosting.id])

        assert (hosted.rating, hosted.explanation) == (100, "1 of 1 dinners → 100")

    def test_wake_time_scores_minutes_late_past_the_grace(self):
        health, _, calendar, goals = _setup(self._goals())
        calendar.events = [
            _event("2026-09-30T23:00", "2026-10-01T07:35", is_end_of_day_sleep=True),
        ]

        (woke,) = health.measure("daily")

        assert woke.period == "2026-10-01"
        assert woke.rating == 50  # 35 late, grace 10, zero at 60
        assert woke.explanation == "Woke 07:35; target 07:00 with 10 min grace → 50"

    def test_up_past_midnight_the_last_day_to_end_is_still_the_day_before(self):
        _, store, calendar, goals = _setup(self._goals())
        calendar.events = [_event("2026-09-30T23:00", "2026-10-01T07:35", is_end_of_day_sleep=True)]
        # 1am on the 3rd, before the night's sleep: it's still the 2nd.
        health = GoalHealth(calendar, store, now=lambda: datetime(2026, 10, 3, 1, tzinfo=TZ))

        (woke,) = health.measure("daily")

        assert health.today() == TODAY
        assert woke.period == "2026-10-01"

    def test_wake_time_averages_a_longer_period(self):
        health, store, calendar, goals = _setup(self._goals())
        wake = goals["Wake 7am"]
        store.update_goal(Goal(id=wake.id, cadence="weekly"))
        calendar.nightly_sleep = False
        calendar.events = [
            _event("2026-09-19T23:00", "2026-09-20T06:50", is_end_of_day_sleep=True),  # 100, starts the week
            _event("2026-09-20T23:00", "2026-09-21T07:05", is_end_of_day_sleep=True),  # 100
            _event("2026-09-21T23:00", "2026-09-22T08:00", is_end_of_day_sleep=True),  # 0
            _event("2026-09-26T23:00", "2026-09-27T07:00", is_end_of_day_sleep=True),  # ends the week
        ]

        proposals = {a.goal_id: a for a in health.measure("weekly")}

        assert proposals[wake.id].rating == 67
        assert proposals[wake.id].explanation.startswith("Mean of 3 wake-ups")

    def test_rollup_takes_its_sub_goals_confirmed_ratings(self):
        health, store, _, goals = _setup([Goal(name="Home", cadence="weekly", measure={"kind": "rollup", "agg": "min"})])
        home = goals["Home"]
        store.create_goal(Goal(name="Cook", parent_id=home.id, cadence="daily"))
        store.create_goal(Goal(name="Clean", parent_id=home.id, cadence="weekly"))
        tree = store.tree()
        cook = next(g for g in tree.goals if g.name == "Cook")
        clean = next(g for g in tree.goals if g.name == "Clean")
        health.confirm_assessments(
            [
                _assessment(cook, "2026-09-21", 80),
                _assessment(cook, "2026-09-22", 60),  # mean 70
                _assessment(clean, "week-2026-09-20", 90),
            ]
        )
        health.record_assessments([_assessment(clean, "week-2026-09-27", 10)])  # proposed: ignored

        (rolled,) = health.measure("weekly", "week-2026-09-20")

        assert (rolled.rating, rolled.method) == (70, "rollup")
        assert rolled.explanation == "Min of 2 sub-goals (70, 90) → 70"

    def test_only_active_goals_with_that_cadence(self):
        health, store, _, goals = _setup(self._goals())
        store.update_goal(Goal(id=goals["Cooking"].id, status="inactive"))

        measured = {a.goal_id for a in health.measure("weekly")}

        assert goals["Cooking"].id not in measured
        assert goals["Wake 7am"].id not in measured

    def test_measures_a_given_period(self):
        health, _, _, goals = _setup(self._goals())

        proposals = health.measure("weekly", "week-2026-09-06", goal_ids=[goals["Cooking"].id])

        assert [(a.period, a.rating) for a in proposals] == [("week-2026-09-06", 0)]
