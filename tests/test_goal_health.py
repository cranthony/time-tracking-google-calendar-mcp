import base64
from datetime import date, datetime, time, timedelta
from dataclasses import replace
from zoneinfo import ZoneInfo

import pytest

from calendar_clients.google_calendar import Event
from tests.fake_sheets import FakeSheets
from tests.test_goals import FakeLabelCalendar, _UNNAMED
from utilities.facets import Facets
from utilities.goal_health import Assessment, GoalHealth, band
from utilities.health_days import day_event_id
from utilities.goal_sheet import Goal
from utilities.goals import OVERALL_ID, Goals
from utilities.traits import SEED_TRAITS

TZ = ZoneInfo("America/New_York")
TODAY = date(2026, 10, 2)  # A Friday.
NOW = datetime.combine(TODAY, time(21), TZ)
YESTERDAY = TODAY - timedelta(days=1)
"""The last day that's over: from waking on it at 7am to waking today."""


class FakeCalendar(FakeLabelCalendar):
    """The main calendar (its events, labels and metadata), and the Goal
    Health calendar it creates, with all-day events filtered by date range
    and private property the way the API does."""

    def __init__(self, events=()):
        super().__init__([_UNNAMED])
        self.events = list(events)
        # A night's end-of-day sleep, 23:00-07:00, on every date with none
        # in `events` -- so days have the sleeps that bound them (see
        # utilities/sleep_days.py). False for none.
        self.nightly_sleep = True
        self.health_events: dict[str, dict] = {}
        self.created: list[tuple] = []
        self.hidden: list[str] = []
        self.calendar_id = "main"
        self.listed: list[tuple[datetime, datetime]] = []
        # Recurring series' master events, by id, for get_event.
        self.series: dict[str, Event] = {}

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

    def list_events(self, time_min, time_max, *, show_deleted=False):
        self.listed.append((time_min, time_max))
        # Like the API: cancelled events only if asked for.
        events = [e for e in self.events if show_deleted or e.status != "cancelled"]
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

    def get_event(self, event_id):
        return self.series[event_id]

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

    def replace_event_resource(self, event_id, body):
        self.health_events[event_id] = {**body, "id": event_id}
        return self.health_events[event_id]

    def delete_event_resource(self, event_id):
        self.health_events.pop(event_id, None)


def _setup(goals=(), events=()):
    calendar = FakeCalendar(events)
    store = Goals(calendar, FakeSheets(), today=lambda: TODAY)
    for goal in goals:
        store.create_goal(goal)
    by_name = {g.name: g for g in store.tree().goals}
    return GoalHealth(calendar, store, now=lambda: NOW), store, calendar, by_name


def _child(store: Goals, name: str, parent: Goal, **fields) -> Goal:
    store.create_goal(Goal(name=name, parent_id=parent.id, **fields))
    return next(g for g in store.tree().goals if g.name == name)


def _event(start: str, end: str, goal_ids=None, **fields) -> Event:
    return Event(
        id=fields.pop("id", f"e-{start}"),
        summary=fields.pop("summary", "x"),
        start=datetime.fromisoformat(start).replace(tzinfo=TZ),
        end=datetime.fromisoformat(end).replace(tzinfo=TZ),
        goal_ids=goal_ids,
        **fields,
    )


def _assessment(goal: Goal, day: date | str, rating=80, **fields) -> Assessment:
    day = date.fromisoformat(day) if isinstance(day, str) else day
    return Assessment(goal_id=goal.id, day=day, rating=rating, method=fields.pop("method", "subjective"), **fields)


_FEEL = {"kind": "subjective", "prompt": "How was it?"}


class TestBand:
    @pytest.mark.parametrize("rating, emoji", [(0, "🔴"), (39, "🔴"), (40, "🟡"), (69, "🟡"), (70, "🟢"), ("skip", "⚪")])
    def test_bands(self, rating, emoji):
        assert band(rating) == emoji


class TestRecordAssessments:
    def test_records_as_proposed_on_a_hidden_calendar_in_the_main_ones_time_zone(self):
        health, _, calendar, goals = _setup([Goal(name="Cooking", measure=_FEEL, priority=0)])
        cooking = goals["Cooking"]

        (recorded,) = health.record_assessments([_assessment(cooking, "2026-10-01", status="confirmed")])

        assert recorded.status == "proposed"
        assert recorded.day == date(2026, 10, 1)
        assert recorded.assessed == NOW
        assert calendar.created == [("Goal Health", "America/New_York")]
        assert calendar.hidden == ["health-calendar"]
        assert calendar.metadata["goal-health-calendar"] == "health-calendar"
        (item,) = calendar.health_events.values()
        assert item["id"] == day_event_id(date(2026, 10, 1), 1)
        assert item["summary"] == "📊 Goal health · 2026-10-01"
        assert item["description"] == "Priority 0\n🟢 Cooking ~80"
        assert (item["start"], item["end"]) == ({"date": "2026-10-01"}, {"date": "2026-10-02"})
        assert item["transparency"] == "transparent"
        properties = item["extendedProperties"]["private"]
        assert properties["cascading-time-tracker-period"] == "2026-10-01"
        assert f"cascading-time-tracker-a.{cooking.id}" in properties

    def test_a_days_assessments_share_one_event_recording_one_again_replacing_it(self):
        health, _, calendar, goals = _setup(
            [Goal(name="Cooking", measure=_FEEL, priority=1), Goal(name="Reading", measure=_FEEL)]
        )
        cooking, reading = goals["Cooking"], goals["Reading"]

        health.record_assessments([_assessment(cooking, "2026-09-30", rating=50)])
        health.record_assessments([_assessment(reading, "2026-09-30", rating=70)])
        health.record_assessments([_assessment(cooking, "2026-09-30", rating="skip", rationale="sick")])

        (item,) = calendar.health_events.values()
        # Only the goals given their own priority are in the description.
        assert item["description"] == "Priority 1\n⚪ Cooking skipped: sick"
        assert {(a.goal_id, a.rating, a.rationale) for a in health.read(date(2026, 9, 30), date(2026, 10, 1))} == {
            (cooking.id, "skip", "sick"), (reading.id, 70, None)
        }
        assert calendar.created == [("Goal Health", "America/New_York")]  # created once

    def test_assessments_of_different_days_go_on_their_own_days(self):
        health, _, calendar, goals = _setup([Goal(name="Cooking", measure=_FEEL)])
        cooking = goals["Cooking"]

        health.record_assessments([_assessment(cooking, "2026-09-30"), _assessment(cooking, "2026-10-01", 40)])

        assert set(calendar.health_events) == {
            day_event_id(date(2026, 9, 30), 1), day_event_id(date(2026, 10, 1), 1)
        }

    def test_the_overall_rating_is_in_the_title(self):
        health, _, calendar, goals = _setup([Goal(name="Cooking", measure=_FEEL)])

        health.record_assessments([_assessment(goals["Overall"], "2026-10-01", 35, method="rollup")])

        (item,) = calendar.health_events.values()
        assert item["summary"] == "📊 Goal health · 2026-10-01 · 🔴 35 (proposed)"

    @pytest.mark.parametrize(
        "change, message",
        [
            ({"goal_id": "nope"}, "isn't a goal"),
            ({"day": date(2026, 10, 3)}, "hasn't started yet"),
            ({"rating": 101}, "0 to 100"),
            ({"rationale": "x" * 8001}, "longer than 8000 bytes"),
            ({"explanation": "x" * 1025}, "longer than 1024"),
        ],
    )
    def test_refuses_invalid_assessments_and_writes_none_of_the_batch(self, change, message):
        health, _, calendar, goals = _setup([Goal(name="Cooking", measure=_FEEL)])
        good = _assessment(goals["Cooking"], "2026-10-01")
        bad = Assessment(**{**_assessment(goals["Cooking"], "2026-09-30").__dict__, **change})

        with pytest.raises(ValueError, match=message):
            health.record_assessments([good, bad])

        assert calendar.health_events == {}

    def test_a_goal_with_nothing_to_rate_it_by_isnt_rated(self):
        health, _, _, goals = _setup([Goal(name="Cooking")])

        with pytest.raises(ValueError, match="isn't rated"):
            health.record_assessments([_assessment(goals["Cooking"], "2026-10-01", method="llm")])

    def test_a_goal_without_a_measure_is_rated_by_its_sub_goals(self):
        health, store, _, goals = _setup([Goal(name="Home")])
        _child(store, "Cook", goals["Home"], measure=_FEEL)

        (recorded,) = health.record_assessments([_assessment(goals["Home"], "2026-10-01", method="rollup")])

        assert recorded.rating == 80


class TestChangedRatings:
    """A rating changed from the one proposed loses the explanation of the
    one proposed: its rationale says why instead."""

    @pytest.mark.parametrize(
        "rating, explanation, rationale, kept",
        [
            (33, "Mean of 3 sub-goals (0, 0, 100) → 33", None, "Mean of 3 sub-goals (0, 0, 100) → 33"),
            (90, "Mean of 3 sub-goals (0, 0, 100) → 33", "Lots of app work", None),
            (90, "Mean of 3 sub-goals (0, 0, 100) → 33", None, "Changed from 33"),
            ("skip", "1h of 2h in the day → 50", "Sick", None),
            (50, "1h of 2h in the day → 50", "Felt longer", "1h of 2h in the day → 50"),
            (50, "No events of Piano that day → skip", None, "Changed from skip"),
            (60, "Carried over from 2026-09-29", None, "Carried over from 2026-09-29"),
            (60, None, "Felt fine", None),
        ],
    )
    def test_its_explanation_gives_way_to_the_reason(self, rating, explanation, rationale, kept):
        health, _, calendar, goals = _setup([Goal(name="Cooking", measure=_FEEL)])
        cooking = goals["Cooking"]
        metrics = {"minutes": 60}

        (recorded,) = health.record_assessments(
            [_assessment(cooking, "2026-10-01", rating, explanation=explanation, rationale=rationale, metrics=metrics)]
        )

        assert recorded.explanation == kept
        assert recorded.metrics == metrics
        (stored,) = health.read(date(2026, 10, 1), date(2026, 10, 2))
        assert stored.explanation == kept
        assert stored.rationale == rationale


class TestHistory:
    def test_lists_a_goals_assessments_by_day_defaulting_to_the_last_12_days(self):
        health, _, _, goals = _setup([Goal(name="Cooking", measure=_FEEL), Goal(name="Reading", measure=_FEEL)])
        cooking, reading = goals["Cooking"], goals["Reading"]
        old = TODAY - timedelta(days=13)  # outside the default range
        health.record_assessments(
            [
                _assessment(cooking, YESTERDAY, 90),
                _assessment(cooking, YESTERDAY - timedelta(days=1), 60),
                _assessment(cooking, old, 10),
                _assessment(reading, YESTERDAY, 30),
            ]
        )

        history = health.history([cooking.id])

        assert [(a.day, a.rating) for a in history] == [(YESTERDAY - timedelta(days=1), 60), (YESTERDAY, 90)]
        everything = health.history([cooking.id], start=old)
        assert [a.rating for a in everything] == [10, 60, 90]

    def test_is_empty_before_anything_is_recorded_without_creating_the_calendar(self):
        health, _, calendar, goals = _setup([Goal(name="Cooking", measure=_FEEL)])

        assert health.history([goals["Cooking"].id]) == []
        assert calendar.created == []


class TestConfirmAndCache:
    def test_confirming_updates_the_goals_health_and_a_skip_doesnt_replace_it(self):
        health, store, _, goals = _setup([Goal(name="Cooking", measure=_FEEL)])
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
        assert listed.health_trend == "-,-,-,-,-,40,85,-"  # the 8 days up to yesterday
        assert listed.stale_days == 0

    def test_rebuilding_restores_a_lost_cache_and_reports_only_the_goals_it_changed(self):
        health, store, _, goals = _setup([Goal(name="Cooking", measure=_FEEL), Goal(name="Reading", measure=_FEEL)])
        cooking = goals["Cooking"]
        health.confirm_assessments([_assessment(cooking, "2026-10-01", 70)])
        store._sheet.write([replace(g, health=None, health_period=None, health_trend=None) for g in store.tree().goals])

        result = health.rebuild_cache()

        assert [(g.id, g.health, g.health_period) for g in result.changed] == [(cooking.id, 70, "2026-10-01")]
        assert result.affected == []
        assert health.rebuild_cache().changed == []

    def test_the_trend_runs_to_today_once_today_is_rated(self):
        health, store, _, goals = _setup([Goal(name="Cooking", measure=_FEEL)])
        cooking = goals["Cooking"]

        health.confirm_assessments([_assessment(cooking, "2026-10-01", 50), _assessment(cooking, TODAY, 60)])

        assert store.tree().by_id[cooking.id].health_trend == "-,-,-,-,-,-,50,60"

    def test_proposed_ratings_dont_count(self):
        health, store, _, goals = _setup([Goal(name="Cooking", measure=_FEEL)])
        cooking = goals["Cooking"]

        health.record_assessments([_assessment(cooking, "2026-10-01", 85)])
        health.rebuild_cache()

        listed = next(g for g in store.get_goals().goals if g.id == cooking.id)
        assert listed.health is None
        assert listed.stale_days is None  # never confirmed

    def test_stale_days_count_ended_days_since_the_last_rated_one(self):
        health, store, _, goals = _setup([Goal(name="Cooking", measure=_FEEL), Goal(name="Folder")])
        cooking = goals["Cooking"]

        health.confirm_assessments([_assessment(cooking, "2026-09-28", 70)])

        listed = {g.name: g for g in store.get_goals().goals}
        assert listed["Cooking"].stale_days == 3  # 29th, 30th and 1st have ended unrated
        assert listed["Folder"].stale_days is None  # nothing to rate it by

    def test_a_tab_without_cache_columns_gains_them(self):
        health, store, _, goals = _setup([Goal(name="Cooking", measure=_FEEL)])
        sheet = store._sheet
        header = sheet._read_header_and_data()[0]
        trimmed = [c for c in header if not c.startswith("health")]
        sheet._sheets_client.write_rows_in_sheet(
            sheet.spreadsheet_id, sheet._sheet_id, "A1:Z1", [trimmed + [""] * (len(header) - len(trimmed))]
        )

        health.confirm_assessments([_assessment(goals["Cooking"], "2026-10-01", 77)])

        assert "health" in sheet._read_header_and_data()[0]
        assert store.tree().by_id[goals["Cooking"].id].health == 77


class TestMeasureDurationAndCount:
    def test_duration_counts_minutes_of_the_goal_and_its_sub_goals_within_the_day(self):
        health, store, calendar, goals = _setup([Goal(name="Cooking", measure={"kind": "duration", "target_min": 300})])
        cooking = goals["Cooking"]
        tofu = _child(store, "Tofu", cooking)
        calendar.events = [
            _event("2026-10-01T12:00", "2026-10-01T14:00", [cooking.id]),  # 120
            _event("2026-10-01T18:00", "2026-10-01T19:10", [tofu.id]),  # 70, via its sub-goal
            _event("2026-10-02T06:30", "2026-10-02T07:30", [cooking.id]),  # 30 inside the day, which ends at 7am
            _event("2026-10-01T06:00", "2026-10-01T07:00", [cooking.id]),  # before it started
            _event("2026-10-01T15:00", "2026-10-01T16:00", ["other"]),  # not this goal
            _event("2026-10-01T16:00", "2026-10-01T17:00", [cooking.id], status="cancelled"),
        ]

        (cooked,) = health.measure()

        assert cooked.day == YESTERDAY
        assert (cooked.rating, cooked.method, cooked.status) == (73, "metric", "proposed")
        assert cooked.explanation == "3h 40m of 5h in the day → 73"
        assert cooked.metrics == {"minutes": 220, "target_min": 300, "interval_days": 1}

    def test_duration_over_an_interval_of_days(self):
        health, _, calendar, goals = _setup(
            [Goal(name="Cooking", measure={"kind": "duration", "target_min": 300, "interval_days": 7})]
        )
        cooking = goals["Cooking"]
        calendar.events = [
            _event("2026-09-26T12:00", "2026-09-26T14:00", [cooking.id]),  # 120: within 7 days of 7am on the 2nd
            _event("2026-09-24T12:00", "2026-09-24T14:00", [cooking.id]),  # too long ago
        ]

        (cooked,) = health.measure()

        assert (cooked.rating, cooked.explanation) == (40, "2h of 5h in the last 7 days → 40")

    def test_count_and_an_events_label_standing_in_for_its_goal(self):
        health, _, calendar, goals = _setup([Goal(name="Hosting", measure={"kind": "count", "target": 1, "noun": "dinners"})])
        hosting = goals["Hosting"]
        # Written before goals: only a label.
        calendar.events = [_event("2026-10-01T18:00", "2026-10-01T21:00", event_label_id=hosting.label_id)]

        (hosted,) = health.measure()

        assert (hosted.rating, hosted.explanation) == (100, "1 of 1 dinners in the day → 100")

    def _visits(self, *visited: str):
        """"Visit parents every 2 months": healthy within 60 days of a
        visit, falling to 0 by 90."""
        health, _, calendar, goals = _setup(
            [Goal(name="Parents", measure={
                "kind": "count", "target": 1, "noun": "visits", "interval_days": 60, "zero_at_days": 90,
            })]
        )
        parents = goals["Parents"]
        calendar.events = [_event(f"{day}T10:00", f"{day}T16:00", [parents.id]) for day in visited]
        (rated,) = health.measure()
        return rated

    def test_a_count_over_an_interval_is_healthy_while_its_met(self):
        rated = self._visits("2026-08-10")

        assert (rated.rating, rated.explanation) == (100, "1 of 1 visits in the last 60 days → 100")

    def test_a_count_declines_once_its_interval_lapses_reaching_0_at_zero_at_days(self):
        # The day ends at 7am on Oct 2; a visit ending at 4pm 75 days and
        # 15 hours before that lapsed 15 days 15 hours ago, of 30.
        rated = self._visits("2026-07-18")

        assert rated.rating == 48
        assert rated.explanation == "0 of 1 visits in the last 60 days; met until 15.6 days ago, 0 once 30 days pass → 48"
        assert rated.metrics["lapsed_days"] == 15.6

    def test_a_count_not_met_since_zero_at_days_is_0(self):
        assert self._visits("2026-06-01").rating == 0
        assert self._visits().explanation == "0 of 1 visits in the last 60 days; not met in the last 90 days → 0"

    def test_a_count_reads_back_as_far_as_zero_at_days(self):
        health, _, calendar, goals = _setup(
            [Goal(name="Parents", measure={"kind": "count", "target": 1, "interval_days": 60, "zero_at_days": 90})]
        )

        health.measure()

        assert min(start for start, _ in calendar.listed) <= datetime(2026, 7, 4, 7, tzinfo=TZ)

    def test_a_duration_declines_from_when_its_window_last_held_enough(self):
        # 2h a day: the 24 hours before a moment held the whole run until
        # 2pm on Oct 1, 17 hours before the day ended at 7am, of the 2 days
        # it has to fall to 0.
        health, _, calendar, goals = _setup(
            [Goal(name="Run", measure={"kind": "duration", "target_min": 120, "zero_at_days": 3})]
        )
        run = goals["Run"]
        calendar.events = [_event("2026-09-30T14:00", "2026-09-30T16:00", [run.id])]

        (rated,) = health.measure()

        assert rated.metrics["minutes"] == 0
        assert rated.rating == round(100 * (1 - 17 / 48))
        assert rated.metrics["lapsed_days"] == 0.7

    def test_a_measure_can_look_at_another_goals_events_as_though_it_were_that_goal(self):
        # "Work 40 hours a week", under "Fulfil my work commitment",
        # measuring its parent's events: none is given the sub-goal.
        health, store, calendar, goals = _setup([Goal(name="Work")])
        work = goals["Work"]
        meetings = _child(store, "Meetings", work)
        hours = _child(
            store,
            "40 hours a week",
            work,
            measure={"kind": "duration", "target_min": 2400, "interval_days": 7, "events_of": work.id},
        )
        calendar.events = [
            _event("2026-10-01T09:00", "2026-10-01T12:00", [work.id]),  # 180
            _event("2026-10-01T13:00", "2026-10-01T14:00", [meetings.id]),  # 60, Work's sub-goal
            _event("2026-09-30T09:00", "2026-09-30T17:00", [work.id]),  # 480
        ]

        (rated,) = health.measure(goal_ids=[hours.id])

        assert rated.metrics["minutes"] == 720
        assert rated.explanation == "12h of 40h in the last 7 days → 30"

    def test_events_of_can_leave_out_that_goals_sub_goals(self):
        health, store, calendar, goals = _setup([Goal(name="Work")])
        work = goals["Work"]
        meetings = _child(store, "Meetings", work)
        hours = _child(
            store,
            "Desk time",
            work,
            measure={"kind": "duration", "target_min": 300, "events_of": work.id, "include_sub_goals": False},
        )
        calendar.events = [
            _event("2026-10-01T09:00", "2026-10-01T12:00", [work.id]),  # 180
            _event("2026-10-01T13:00", "2026-10-01T14:00", [meetings.id]),  # left out
        ]

        (rated,) = health.measure(goal_ids=[hours.id])

        assert rated.metrics["minutes"] == 180

    def test_events_of_must_be_a_goal(self):
        _, store, _, goals = _setup([Goal(name="Work")])

        with pytest.raises(ValueError, match="\"events_of\" names 'nope', which isn't a goal"):
            store.update_goal(
                Goal(id=goals["Work"].id, measure={"kind": "duration", "target_min": 60, "events_of": "nope"})
            )

    def test_a_measure_can_leave_out_sub_goals_events(self):
        health, store, calendar, goals = _setup([Goal(name="Cooking")])
        cooking = goals["Cooking"]
        tofu = _child(store, "Tofu", cooking)
        store.update_goal(
            Goal(id=cooking.id, measure={"kind": "duration", "target_min": 300, "include_sub_goals": False})
        )
        calendar.events = [
            _event("2026-10-01T12:00", "2026-10-01T14:00", [cooking.id]),  # 120
            _event("2026-10-01T18:00", "2026-10-01T19:10", [tofu.id]),  # its sub-goal: left out
        ]

        (cooked,) = health.measure(goal_ids=[cooking.id])

        assert cooked.metrics["minutes"] == 120


class TestMeasureTimeConstraint:
    def _setup(self, **measure):
        health, store, calendar, goals = _setup([Goal(name="Work")])
        work = goals["Work"]
        constraint = _child(
            store,
            "Constraint",
            work,
            measure={"kind": "time_constraint", "events_of": work.id, "grace_min": 10, "zero_at_min": 60, **measure},
        )
        return health, calendar, work, constraint

    def test_rates_the_first_events_start_by_a_time_falling_off_past_the_grace(self):
        health, calendar, work, constraint = self._setup(edge="start", target="09:30")
        calendar.events = [
            _event("2026-10-01T10:05", "2026-10-01T12:00", [work.id]),  # 35 late
            _event("2026-10-01T13:00", "2026-10-01T17:00", [work.id]),
            _event("2026-10-01T06:00", "2026-10-01T06:30", [work.id]),  # before the day started, at 7
        ]

        (rated,) = health.measure(goal_ids=[constraint.id])

        assert rated.day == YESTERDAY
        assert rated.rating == 50  # 35 late, grace 10, zero at 60
        assert rated.explanation == "Started 10:05; by 09:30 with 10 min grace → 50"
        assert rated.metrics == {"edge": "start", "target": "09:30", "when": "by", "at": "10:05"}

    def test_rates_the_last_events_end(self):
        health, calendar, work, constraint = self._setup(edge="end", target="17:30")
        calendar.events = [
            _event("2026-10-01T09:30", "2026-10-01T12:00", [work.id]),
            _event("2026-10-01T13:00", "2026-10-01T17:35", [work.id]),  # within the grace
        ]

        (rated,) = health.measure(goal_ids=[constraint.id])

        assert (rated.rating, rated.explanation) == (100, "Ended 17:35; by 17:30 with 10 min grace → 100")

    def test_after_rates_being_too_early(self):
        health, calendar, work, constraint = self._setup(edge="start", target="08:00", when="after")
        calendar.events = [_event("2026-10-01T07:20", "2026-10-01T09:00", [work.id])]  # 40 early

        (rated,) = health.measure(goal_ids=[constraint.id])

        assert (rated.rating, rated.explanation) == (40, "Started 07:20; not before 08:00 with 10 min grace → 40")

    def test_a_day_without_such_events_is_zero(self):
        health, _, _, constraint = self._setup(edge="start", target="09:30")

        (rated,) = health.measure(goal_ids=[constraint.id])

        assert (rated.rating, rated.explanation) == (0, "No events of Work that day → 0")

    def test_without_events_of_it_reads_its_own_goals_events(self):
        # "Up by 07:00": the "get up" event, given the goal itself.
        health, store, calendar, goals = _setup(
            [Goal(name="Get up", measure={"kind": "time_constraint", "edge": "start", "target": "07:00"})]
        )
        calendar.events = [_event("2026-10-01T07:20", "2026-10-01T07:40", [goals["Get up"].id])]

        (rated,) = health.measure()

        assert rated.explanation == "Started 07:20; by 07:00 with 0 min grace → 67"

    def test_up_past_midnight_the_last_day_to_end_is_still_the_day_before(self):
        _, store, calendar, goals = _setup(
            [Goal(name="Get up", measure={"kind": "time_constraint", "edge": "start", "target": "07:00"})]
        )
        # 1am on the 3rd, before the night's sleep: it's still the 2nd.
        health = GoalHealth(calendar, store, now=lambda: datetime(2026, 10, 3, 1, tzinfo=TZ))

        (rated,) = health.measure()

        assert health.today() == TODAY
        assert rated.day == YESTERDAY

    def test_a_day_that_isnt_over_cant_be_measured(self):
        health, *_ = self._setup(edge="start", target="09:30")

        with pytest.raises(ValueError, match="isn't over yet"):
            health.measure(TODAY)


class TestMeasureTimeWindow:
    def _setup(self, **measure):
        # "Lunch between 11:30 and 13:30", measuring meals tagged "Eat well".
        health, store, calendar, goals = _setup([Goal(name="Eat well")])
        eat_well = goals["Eat well"]
        _child(
            store,
            "Lunch on time",
            eat_well,
            measure={"kind": "time_window", "from": "11:30", "to": "13:30", "events_of": eat_well.id, **measure},
        )
        return health, calendar, eat_well

    def test_an_event_in_the_window_is_full_marks(self):
        health, calendar, lunch = self._setup()
        calendar.events = [_event("2026-10-01T12:00", "2026-10-01T12:30", [lunch.id])]

        (rated,) = health.measure()

        assert rated.day == YESTERDAY
        assert (rated.rating, rated.explanation) == (100, "12:00–12:30; in 11:30–13:30 → 100")
        assert rated.metrics == {"from": "11:30", "to": "13:30", "at": "12:00–12:30", "off_min": 0}

    def test_an_event_overlapping_the_window_is_in_it(self):
        health, calendar, lunch = self._setup()
        calendar.events = [_event("2026-10-01T13:15", "2026-10-01T14:00", [lunch.id])]

        (rated,) = health.measure()

        assert rated.rating == 100

    def test_falls_off_by_the_closest_edge_after_the_window(self):
        health, calendar, lunch = self._setup(zero_at_min=60)
        calendar.events = [_event("2026-10-01T14:00", "2026-10-01T14:30", [lunch.id])]  # starts 30 after

        (rated,) = health.measure()

        assert (rated.rating, rated.explanation) == (50, "14:00–14:30; 30 min after 11:30–13:30 with 0 min grace → 50")
        assert rated.metrics["off_min"] == 30

    def test_falls_off_by_the_closest_edge_before_the_window_past_the_grace(self):
        health, calendar, lunch = self._setup(grace_min=10, zero_at_min=60)
        calendar.events = [_event("2026-10-01T10:30", "2026-10-01T11:05", [lunch.id])]  # ends 25 before

        (rated,) = health.measure()

        assert (rated.rating, rated.explanation) == (70, "10:30–11:05; 25 min before 11:30–13:30 with 10 min grace → 70")

    def test_far_outside_the_window_is_zero(self):
        health, calendar, lunch = self._setup()
        calendar.events = [_event("2026-10-01T16:00", "2026-10-01T16:30", [lunch.id])]

        (rated,) = health.measure()

        assert rated.rating == 0

    def test_the_closest_of_the_days_events_rates_it(self):
        # Any meal can count as lunch: whichever is closest to the window.
        health, calendar, lunch = self._setup()
        calendar.events = [
            _event("2026-10-01T08:00", "2026-10-01T08:20", [lunch.id]),
            _event("2026-10-01T13:00", "2026-10-01T13:20", [lunch.id]),
            _event("2026-10-01T18:30", "2026-10-01T19:15", [lunch.id]),
        ]

        (rated,) = health.measure()

        assert (rated.rating, rated.metrics["at"]) == (100, "13:00–13:20")

    def test_a_day_without_such_events_is_zero(self):
        health, _, _ = self._setup()

        (rated,) = health.measure()

        assert (rated.rating, rated.explanation) == (0, "No events of Eat well that day → 0")


_FOLLOW = {"kind": "follow_through"}


class TestMeasureFollowThrough:
    """Yesterday runs from 7am on 2026-10-01 to 7am on the 2nd; the days
    before it are the 24 hours before that, and so on."""

    def test_a_cancelled_event_lowers_the_rating(self):
        health, _, calendar, goals = _setup([Goal(name="Word", measure=_FOLLOW)])
        word = goals["Word"].id
        calendar.events = [_event("2026-10-01T10:00", "2026-10-01T11:00", [word], status="cancelled")]

        (rated,) = health.measure()

        assert (rated.rating, rated.method) == (75, "metric")
        assert rated.explanation == "1 cancelled (−25) that day, from 100 → 75"
        assert rated.metrics == {
            "cancelled": 1, "kept": 0, "before": 100, "penalty": 25, "recovery": 25, "look_back_days": 30,
        }

    def test_a_lowered_rating_carries_over_days_without_events(self):
        health, _, calendar, goals = _setup([Goal(name="Word", measure={**_FOLLOW, "penalty": 40})])
        word = goals["Word"].id
        calendar.events = [_event("2026-09-29T10:00", "2026-09-29T11:00", [word], status="cancelled")]

        (rated,) = health.measure()

        assert rated.rating == 60
        assert rated.explanation == "Nothing cancelled or kept that day, from 60 → 60"

    def test_days_with_kept_events_recover_it_once_a_day(self):
        health, _, calendar, goals = _setup([Goal(name="Word", measure=_FOLLOW)])
        word = goals["Word"].id
        calendar.events = [
            _event("2026-09-28T10:00", "2026-09-28T11:00", [word], status="cancelled"),
            _event("2026-09-28T12:00", "2026-09-28T13:00", [word], status="cancelled"),
            _event("2026-09-28T14:00", "2026-09-28T15:00", [word], status="cancelled"),  # 25
            _event("2026-09-30T10:00", "2026-09-30T11:00", [word]),  # 50
            _event("2026-10-01T10:00", "2026-10-01T11:00", [word]),
            _event("2026-10-01T12:00", "2026-10-01T13:00", [word]),  # 75, not 100: once for the day
        ]

        (rated,) = health.measure()

        assert rated.rating == 75
        assert rated.explanation == "2 kept (+25) that day, from 50 → 75"

    def test_cancelled_and_kept_on_the_same_day(self):
        health, _, calendar, goals = _setup([Goal(name="Word", measure={**_FOLLOW, "recovery": 10})])
        word = goals["Word"].id
        calendar.events = [
            _event("2026-10-01T10:00", "2026-10-01T11:00", [word], status="cancelled"),
            _event("2026-10-01T12:00", "2026-10-01T13:00", [word]),
        ]

        (rated,) = health.measure()

        assert rated.rating == 85
        assert rated.explanation == "1 cancelled (−25), 1 kept (+10) that day, from 100 → 85"

    def test_only_the_goals_events_count_and_a_replaced_one_is_excused(self):
        health, store, calendar, goals = _setup([Goal(name="Word", measure=_FOLLOW), Goal(name="Other")])
        word = goals["Word"]
        promise = _child(store, "Promise", word)
        calendar.events = [
            _event("2026-10-01T09:00", "2026-10-01T10:00", [promise.id], status="cancelled"),  # a sub-goal's
            _event("2026-10-01T11:00", "2026-10-01T12:00", [goals["Other"].id], status="cancelled"),
            # Merged into the event after it, which covers its time.
            _event("2026-10-01T14:00", "2026-10-01T15:00", [word.id], status="cancelled", id="merged"),
            _event("2026-10-01T14:30", "2026-10-01T16:00", [word.id]),
        ]

        (rated,) = health.measure()

        assert (rated.metrics["cancelled"], rated.metrics["kept"], rated.rating) == (1, 1, 100)

    def test_a_cancellation_before_the_look_back_is_forgotten(self):
        health, _, calendar, goals = _setup([Goal(name="Word", measure={**_FOLLOW, "look_back_days": 3})])
        word = goals["Word"].id
        calendar.events = [_event("2026-09-28T10:00", "2026-09-28T11:00", [word], status="cancelled")]

        (rated,) = health.measure()

        assert rated.rating == 100

    def test_a_cancelled_instance_without_goals_is_its_series(self):
        health, _, calendar, goals = _setup([Goal(name="Word", measure=_FOLLOW)])
        word = goals["Word"].id
        calendar.series["series"] = _event("2026-08-01T10:00", "2026-08-01T11:00", [word], id="series")
        calendar.events = [
            _event("2026-10-01T10:00", "2026-10-01T10:00", status="cancelled", recurring_event_id="series"),
        ]

        (rated,) = health.measure()

        assert (rated.metrics["cancelled"], rated.rating) == (1, 75)

    def test_a_cancelled_instance_with_a_label_but_no_goals_takes_its_series_goals(self):
        health, store, calendar, goals = _setup([Goal(name="Word", measure=_FOLLOW), Goal(name="Other")])
        word = goals["Word"].id
        calendar.series["series"] = _event("2026-08-01T10:00", "2026-08-01T11:00", [word], id="series")
        calendar.events = [
            _event(
                "2026-10-01T10:00", "2026-10-01T11:00", status="cancelled", recurring_event_id="series",
                event_label_id=goals["Other"].label_id,
            ),
        ]

        (rated,) = health.measure()

        assert (rated.metrics["cancelled"], rated.rating) == (1, 75)

    def test_a_placeholder_for_an_edited_series_isnt_counted(self):
        """What Calendar lists for an instance a series edit took away:
        "CANCELLED", with no goals, label or series."""
        health, _, calendar, goals = _setup([Goal(name="Word", measure=_FOLLOW)])
        calendar.events = [
            _event("2026-10-01T10:00", "2026-10-01T11:00", status="cancelled", summary="CANCELLED"),
        ]

        (rated,) = health.measure()

        assert rated.rating == 100


class TestMeasureOnlyIf:
    def _setup(self, measure, **only_if):
        health, store, calendar, goals = _setup([Goal(name="Piano")])
        piano = goals["Piano"]
        gated = _child(store, "Gated", piano, measure={**measure, "only_if": only_if})
        return health, calendar, piano, gated

    def test_a_day_without_its_goals_events_is_skipped_whatever_the_kind(self):
        health, _, _, gated = self._setup({"kind": "duration", "target_min": 30})

        (rated,) = health.measure(goal_ids=[gated.id])

        assert (rated.rating, rated.method) == ("skip", "metric")
        assert rated.explanation == "No events of Gated that day → skip"
        assert rated.metrics == {"only_if": gated.id}
        assert rated.unmet

    def test_on_a_day_with_them_its_measured_as_usual(self):
        health, calendar, _, gated = self._setup({"kind": "duration", "target_min": 30})
        calendar.events = [_event("2026-10-01T18:00", "2026-10-01T18:15", [gated.id])]

        (rated,) = health.measure(goal_ids=[gated.id])

        assert rated.rating == 50

    def test_events_of_names_another_goal(self):
        health, store, calendar, goals = _setup([Goal(name="Piano")])
        piano = goals["Piano"]
        practice = _child(
            store, "Practice", piano,
            measure={"kind": "subjective", "prompt": "How did it go?", "only_if": {"events_of": piano.id}},
        )

        (skipped,) = health.measure(goal_ids=[practice.id])
        calendar.events = [_event("2026-10-01T18:00", "2026-10-01T18:15", [piano.id])]

        assert (skipped.rating, skipped.method, skipped.explanation) == (
            "skip", "subjective", "No events of Piano that day → skip"
        )
        assert health.measure(goal_ids=[practice.id]) == []  # to be asked


    def test_without_events_of_it_looks_at_the_measures_events(self):
        health, store, calendar, goals = _setup([Goal(name="Eat well")])
        meals = goals["Eat well"]
        breakfast = _child(
            store, "Breakfast", meals,
            measure={
                "kind": "time_constraint", "edge": "start", "target": "08:00", "events_of": meals.id, "only_if": {}
            },
        )

        (skipped,) = health.measure(goal_ids=[breakfast.id])
        calendar.events = [_event("2026-10-01T08:00", "2026-10-01T08:30", [meals.id])]
        (rated,) = health.measure(goal_ids=[breakfast.id])

        assert (skipped.rating, skipped.explanation) == ("skip", "No events of Eat well that day → skip")
        assert skipped.metrics == {"only_if": meals.id}
        assert rated.rating == 100

    def test_its_own_events_of_doesnt_take_the_measures_include_sub_goals(self):
        health, store, calendar, goals = _setup([Goal(name="Piano")])
        piano = goals["Piano"]
        scales = _child(store, "Scales", piano)
        gated = _child(
            store, "Gated", piano,
            measure={
                "kind": "count", "target": 1, "include_sub_goals": False, "only_if": {"events_of": piano.id}
            },
        )
        calendar.events = [_event("2026-10-01T18:00", "2026-10-01T18:15", [scales.id])]

        (rated,) = health.measure(goal_ids=[gated.id])

        assert rated.rating == 0  # met by Scales, a sub-goal of Piano; Gated itself has no events


class TestMeasureRollup:
    def _tree(self, measure=None):
        health, store, _, goals = _setup([Goal(name="Home", measure=measure)])
        home = goals["Home"]
        children = [_child(store, name, home, measure=_FEEL) for name in ("Cook", "Clean", "Shop")]
        return health, store, home, children

    def test_waits_until_every_sub_goal_is_confirmed(self):
        health, _, home, (cook, clean, shop) = self._tree()
        health.confirm_assessments([_assessment(cook, YESTERDAY, 80), _assessment(clean, YESTERDAY, 60)])
        health.record_assessments([_assessment(shop, YESTERDAY, 10)])  # proposed: not yet

        assert health.measure(goal_ids=[home.id]) == []

    def test_a_goal_without_a_measure_takes_the_mean_of_its_sub_goals_that_day(self):
        health, _, home, (cook, clean, shop) = self._tree()
        health.confirm_assessments(
            [
                _assessment(cook, YESTERDAY, 80),
                _assessment(clean, YESTERDAY, 60),
                _assessment(shop, YESTERDAY, "skip"),  # left out
                _assessment(cook, YESTERDAY - timedelta(days=1), 0),  # another day
            ]
        )

        (rolled,) = health.measure(goal_ids=[home.id])

        assert (rolled.rating, rolled.method) == (70, "rollup")
        assert rolled.explanation == "Mean of 2 sub-goals (80, 60) → 70"

    def test_weighted_weighs_sub_goals_not_listed_at_0(self):
        health, store, home, (cook, clean, shop) = self._tree()
        store.update_goal(Goal(id=home.id, measure={"kind": "rollup", "agg": "weighted", "weights": {cook.id: 3, clean.id: 1}}))
        health.confirm_assessments(
            [_assessment(cook, YESTERDAY, 80), _assessment(clean, YESTERDAY, 40), _assessment(shop, YESTERDAY, 0)]
        )

        (rolled,) = health.measure(goal_ids=[home.id])

        assert rolled.rating == 70
        assert rolled.explanation == "Weighted mean of 2 sub-goals (80×3, 40×1) → 70"

    def test_a_temporary_weight_weighs_then_from_its_date_on(self):
        health, store, home, (cook, clean, shop) = self._tree()
        set_aside = {"weight": 0, "until": (YESTERDAY + timedelta(days=1)).isoformat(), "then": 1}
        store.update_goal(Goal(id=home.id, measure={"kind": "rollup", "agg": "weighted", "weights": {cook.id: 1, clean.id: set_aside}}))
        health.confirm_assessments(
            [_assessment(cook, YESTERDAY, 80), _assessment(clean, YESTERDAY, 40), _assessment(shop, YESTERDAY, 0)]
        )

        (before,) = health.measure(goal_ids=[home.id])
        store.update_goal(
            Goal(id=home.id, measure={"kind": "rollup", "agg": "weighted", "weights": {cook.id: 1, clean.id: {**set_aside, "until": YESTERDAY.isoformat()}}})
        )
        (after,) = health.measure(goal_ids=[home.id])

        assert (before.rating, before.metrics["weights"]) == (80, {cook.id: 1})
        assert (after.rating, after.metrics["weights"]) == (60, {cook.id: 1, clean.id: 1})

    def test_weighted_with_nothing_weighed_is_skipped(self):
        health, store, home, (cook, clean, shop) = self._tree()
        store.update_goal(Goal(id=home.id, measure={"kind": "rollup", "agg": "weighted", "weights": {cook.id: 1}}))
        health.confirm_assessments(
            [_assessment(cook, YESTERDAY, "skip"), _assessment(clean, YESTERDAY, 40), _assessment(shop, YESTERDAY, 0)]
        )

        (rolled,) = health.measure(goal_ids=[home.id])

        assert rolled.rating == "skip"

    def test_weights_must_name_its_sub_goals(self):
        _, store, home, (cook, *_) = self._tree()

        with pytest.raises(ValueError, match="isn't one of its sub-goals"):
            store.update_goal(Goal(id=home.id, measure={"kind": "rollup", "agg": "weighted", "weights": {home.id: 1}}))

    @pytest.mark.parametrize("percentile, rating, word", [(0, 20, "Lowest"), (100, 90, "Highest"), (50, 60, "50th percentile"), (75, 75, "75th percentile")])
    def test_percentile(self, percentile, rating, word):
        health, store, home, (cook, clean, shop) = self._tree()
        store.update_goal(Goal(id=home.id, measure={"kind": "rollup", "agg": "percentile", "percentile": percentile}))
        health.confirm_assessments(
            [_assessment(cook, YESTERDAY, 90), _assessment(clean, YESTERDAY, 20), _assessment(shop, YESTERDAY, 60)]
        )

        (rolled,) = health.measure(goal_ids=[home.id])

        assert rolled.rating == rating
        assert rolled.explanation == f"{word} of 3 sub-goals (90, 20, 60) → {rating}"

    def test_a_min_rollup_from_before_percentiles_is_the_lowest(self):
        health, store, home, (cook, clean, shop) = self._tree()
        sheet_goals = store.tree().goals
        next(g for g in sheet_goals if g.id == home.id).measure = {"kind": "rollup", "agg": "min"}
        store._sheet.write(sheet_goals)
        health.confirm_assessments(
            [_assessment(cook, YESTERDAY, 90), _assessment(clean, YESTERDAY, 20), _assessment(shop, YESTERDAY, 60)]
        )

        (rolled,) = health.measure(goal_ids=[home.id])

        assert rolled.rating == 20

    def test_only_active_sub_goals_count(self):
        health, store, home, (cook, clean, shop) = self._tree()
        store.update_goal(Goal(id=shop.id, status="inactive"))
        health.confirm_assessments([_assessment(cook, YESTERDAY, 80), _assessment(clean, YESTERDAY, 60)])

        (rolled,) = health.measure(goal_ids=[home.id])

        assert rolled.rating == 70


class TestMeasure:
    def test_only_active_goals_the_calendar_can_answer(self):
        health, store, _, goals = _setup(
            [
                Goal(name="Cooking", measure={"kind": "duration", "target_min": 300}),
                Goal(name="Reading", measure={"kind": "duration", "target_min": 30}),
                Goal(name="Feel", measure=_FEEL),
                Goal(name="Judged", measure={"kind": "llm", "rubric": "?"}),
            ]
        )
        store.update_goal(Goal(id=goals["Cooking"].id, status="inactive"))

        measured = {a.goal_id for a in health.measure()}

        assert measured == {goals["Reading"].id}

    def test_measures_a_given_day(self):
        health, _, _, goals = _setup([Goal(name="Cooking", measure={"kind": "duration", "target_min": 300})])

        proposals = health.measure(date(2026, 9, 6))

        assert [(a.day, a.rating) for a in proposals] == [(date(2026, 9, 6), 0)]


class TestOverall:
    def test_a_duration_measure_counts_every_goals_events_once(self):
        health, store, calendar, goals = _setup([Goal(name="Cooking"), Goal(name="Reading")])
        cooking, reading = goals["Cooking"], goals["Reading"]
        tofu = _child(store, "Tofu", cooking)
        store.update_goal(Goal(id=OVERALL_ID, measure={"kind": "duration", "target_min": 240}))
        calendar.events = [
            _event("2026-10-01T12:00", "2026-10-01T13:00", [tofu.id]),  # 60, a sub-goal's
            _event("2026-10-01T18:00", "2026-10-01T19:00", [cooking.id, reading.id]),  # 60, once
            _event("2026-10-01T20:00", "2026-10-01T21:00"),  # no goal
        ]

        (rated,) = health.measure(goal_ids=[OVERALL_ID])

        assert (rated.rating, rated.metrics["minutes"]) == (50, 120)

    def test_rolls_up_the_top_level_goals(self):
        health, store, _, goals = _setup([Goal(name="Cooking", measure=_FEEL), Goal(name="Reading", measure=_FEEL)])
        health.confirm_assessments(
            [_assessment(goals["Cooking"], YESTERDAY, 80), _assessment(goals["Reading"], YESTERDAY, 40)]
        )

        (rated,) = health.measure(goal_ids=[OVERALL_ID])

        assert rated.explanation == "Mean of 2 sub-goals (80, 40) → 60"


class TestMeasureTraits:
    """Yesterday runs from 7am on 2026-10-01 to 7am on the 2nd."""

    def _setup(self, measure=None):
        calendar = FakeCalendar()
        store = Goals(calendar, FakeSheets(), today=lambda: TODAY, trait_ids=lambda: [t.id for t in SEED_TRAITS])
        store.create_goal(Goal(name="Person", measure=measure or {"kind": "traits", "traits": ["creative", "generous"]}))
        person = next(g for g in store.tree().goals if g.name == "Person")
        health = GoalHealth(calendar, store, now=lambda: NOW, traits=lambda: SEED_TRAITS)
        return health, calendar, person

    def test_rates_a_goal_by_its_traits_keeping_their_scores(self):
        health, calendar, person = self._setup({"kind": "traits", "traits": ["reliable", "creative"]})
        calendar.events = [_event("2026-10-01T18:00", "2026-10-01T20:00", [person.id])]

        (rated,) = health.measure()

        # Creative's one part is a judgment, not made yet, so it's left out.
        assert rated.method == "metric"
        assert rated.explanation == "Traits (Reliable 83, Creative –) → 83"
        assert rated.metrics == {
            "traits": {"reliable": 83, "creative": None},
            "parts": {"reliable": {"continuity": 50, "follow_through": 100, "count": 100}, "creative": {"judgment": None}},
            "window_days": 30,
        }

    def test_reads_back_over_the_window_and_ahead_for_continuity(self):
        health, calendar, person = self._setup({"kind": "traits", "traits": ["reliable"]})
        calendar.events = [_event("2026-10-10T18:00", "2026-10-10T20:00", [person.id])]

        (rated,) = health.measure()

        first, last = calendar.listed[-1]
        assert first <= datetime(2026, 9, 1, 7, tzinfo=TZ) and last >= datetime(2026, 10, 16, tzinfo=TZ)
        assert rated.metrics["parts"]["reliable"]["continuity"] == 50

    def test_explains_each_part_with_the_events_behind_it(self):
        health, calendar, person = self._setup({"kind": "traits", "traits": "all"})
        calendar.events = [_event("2026-10-01T18:00", "2026-10-01T20:00", [person.id])]

        explained = health.traits_rating(person.id)

        assert (explained.goal_id, explained.day) == (person.id, YESTERDAY)
        count = next(t for t in explained.traits if t.trait_id == "reliable").parts[2]
        assert (count.key, count.score, count.event_ids) == ("count", 100, ["e-2026-10-01T18:00"])
        assert ("adventurous", "judgment") in [(t, p.key) for t, p in explained.judgments_due]

    def test_only_a_traits_measure_is_explained(self):
        health, _, _ = self._setup()
        other = Goal(name="Other", measure={"kind": "count", "target": 1})
        health._goals.create_goal(other)
        other_id = next(g.id for g in health._goals.tree().goals if g.name == "Other")

        with pytest.raises(ValueError, match="isn't measured by traits"):
            health.traits_rating(other_id)

    def test_a_measure_naming_an_unknown_trait_is_refused(self):
        health, _, person = self._setup()

        with pytest.raises(ValueError, match="names 'kind', which isn't a trait"):
            health._goals.update_goal(Goal(id=person.id, measure={"kind": "traits", "traits": ["kind"]}))
