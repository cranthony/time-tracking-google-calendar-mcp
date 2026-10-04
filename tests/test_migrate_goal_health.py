from datetime import date, datetime, timedelta

import pytest

from migrate_goal_health import migrate
from tests.test_health_days import TZ, FakeHealthCalendar
from utilities.goal_sheet import Goal
from utilities.goals import GoalTree
from utilities.health_days import PREFIX, Assessment, DayReflection, HealthDay, HealthDays, day_event_id

DAY = date(2026, 9, 30)


class FakeOldCalendar(FakeHealthCalendar):
    def list_all_event_resources(self):
        return list(self.events.values())


def _tree():
    return GoalTree(
        [
            Goal(id="overall", name="Overall", status="active"),
            Goal(id="cook01", name="Cooking", status="active", label_id="l1"),
            Goal(id="read01", name="Reading", status="active", label_id="l2"),
        ]
    )


def _old(event_id, day: str, description=None, **properties):
    """An old event, all day on `day`."""
    return {
        "id": event_id,
        "summary": f"old {event_id}",
        "description": description or "",
        "start": {"date": day},
        "end": {"date": (date.fromisoformat(day) + timedelta(days=1)).isoformat()},
        "extendedProperties": {"private": {f"{PREFIX}{k}": v for k, v in properties.items()}},
    }


def _old_assessment(goal_id, day=DAY, rating="80", description=None, **properties):
    return _old(
        f"a-{goal_id}-{day}", day.isoformat(), description,
        kind="assessment", goal=goal_id, period=day.isoformat(), rating=rating, method="subjective",
        status="confirmed", schema="1", **properties,
    )


def _calendar(*items):
    calendar = FakeOldCalendar()
    for item in items:
        calendar.events[item["id"]] = item
    return calendar


def _read(calendar, day=DAY):
    return HealthDays(lambda create: calendar).read(day, day + timedelta(days=1), TZ).get(day)


class TestMigrate:
    def test_folds_a_days_old_events_into_one_then_deletes_them(self):
        calendar = _calendar(
            _old_assessment("cook01", rating="55", explanation="1h of 2h", metrics='{"minutes":60}'),
            _old_assessment("read01", rating="skip", description="sick"),
            _old(
                "r-1", DAY.isoformat(), "busy day",
                kind="reflection", cadence="daily", period=DAY.isoformat(), intentions='["rest"]',
                complete="true", reflected="2026-10-01T09:00:00-04:00",
            ),
        )

        plan = migrate(calendar, _tree(), TZ, apply=True, say=lambda _: None)

        assert set(calendar.events) == {day_event_id(DAY, 1)}
        assert (plan.written, plan.deleted) == (1, 3)
        day = _read(calendar)
        assert list(day.assessments) == ["cook01", "read01"]
        cooking = day.assessments["cook01"]
        assert (cooking.rating, cooking.explanation, cooking.metrics, cooking.status) == (
            55, "1h of 2h", {"minutes": 60}, "confirmed"
        )
        assert (day.assessments["read01"].rating, day.assessments["read01"].rationale) == ("skip", "sick")
        assert day.reflection == DayReflection(
            journal="busy day", intentions=["rest"], complete=True,
            reflected=datetime.fromisoformat("2026-10-01T09:00:00-04:00"),
        )
        assert calendar.events[day_event_id(DAY, 1)]["summary"] == "📝 Reflection · 2026-09-30"

    def test_a_dry_run_changes_nothing(self):
        calendar = _calendar(_old_assessment("cook01"))
        before = dict(calendar.events)
        said = []

        plan = migrate(calendar, _tree(), TZ, apply=False, say=said.append)

        assert calendar.events == before
        assert list(plan.days) == [DAY]
        assert said[0] == "1 old event(s) on 1 day(s) to fold into one event a day."

    def test_whats_already_in_the_new_form_wins(self):
        calendar = _calendar(_old_assessment("cook01", rating="10"), _old_assessment("read01", rating="20"))
        new = HealthDay(
            day=DAY,
            assessments={"cook01": Assessment(goal_id="cook01", day=DAY, rating=90, method="metric")},
        )
        HealthDays(lambda create: calendar).write(new, {}, "overall")

        migrate(calendar, _tree(), TZ, apply=True, say=lambda _: None)

        day = _read(calendar)
        assert {g: a.rating for g, a in day.assessments.items()} == {"cook01": 90, "read01": 20}
        assert set(calendar.events) == {day_event_id(DAY, 1)}

    def test_leaves_events_from_before_daily_ratings_alone(self):
        weekly = _old(
            "weekly", "2026-09-28", None, kind="assessment", goal="cook01", period="2026-W40", rating="50"
        )
        calendar = _calendar(weekly)

        plan = migrate(calendar, _tree(), TZ, apply=True, say=lambda _: None)

        assert calendar.events == {"weekly": weekly}
        assert plan.unreadable == ["weekly old weekly"]

    def test_keeps_a_days_old_events_if_it_doesnt_read_back(self):
        calendar = _calendar(_old_assessment("cook01"))
        calendar.list_event_resources = lambda *args, **kwargs: []

        with pytest.raises(RuntimeError, match="didn't read back as written"):
            migrate(calendar, _tree(), TZ, apply=True, say=lambda _: None)

        assert f"a-cook01-{DAY}" in calendar.events
