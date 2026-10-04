import base64
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from calendar_clients.google_calendar import MAX_DESCRIPTION_BYTES
from utilities.health_days import (
    PART_BUDGET_BYTES,
    PREFIX,
    Assessment,
    DayReflection,
    HealthDay,
    HealthDays,
    day_event_id,
    decode,
    encode,
)

TZ = ZoneInfo("America/New_York")
DAY = date(2026, 10, 1)


class FakeHealthCalendar:
    def __init__(self):
        self.events: dict[str, dict] = {}
        self.dropping = False

    def list_event_resources(self, time_min, time_max, *, private_property=None):
        key, _, value = private_property.partition("=")
        return [
            item for item in self.events.values()
            if date.fromisoformat(item["start"]["date"]) < time_max.date()
            and date.fromisoformat(item["end"]["date"]) > time_min.date()
            and item["extendedProperties"]["private"].get(key) == value
        ]

    def replace_event_resource(self, event_id, body):
        self.events[event_id] = {**body, "id": event_id}
        if self.dropping:  # As Calendar does with a value that's too long.
            private = dict(body["extendedProperties"]["private"])
            private.popitem()
            return {**body, "extendedProperties": {"private": private}}
        return self.events[event_id]

    def delete_event_resource(self, event_id):
        self.events.pop(event_id, None)


def _days():
    calendar = FakeHealthCalendar()
    return HealthDays(lambda create: calendar), calendar


def _assessment(goal_id, rating=80, **fields) -> Assessment:
    return Assessment(goal_id=goal_id, day=DAY, rating=rating, method="subjective", status="confirmed", **fields)


def _day(count, rationale="", reflection=None, parts=0) -> HealthDay:
    assessments = {f"g{i:05d}": _assessment(f"g{i:05d}", rationale=rationale or None) for i in range(count)}
    return HealthDay(day=DAY, assessments=assessments, reflection=reflection, parts=parts)


class TestEventIds:
    def test_encode_the_day_and_part_in_calendars_alphabet(self):
        event_id = day_event_id(DAY, 2)

        assert set(event_id) <= set("0123456789abcdefghijklmnopqrstuv")
        padded = event_id.upper() + "=" * (-len(event_id) % 8)
        assert base64.b32hexdecode(padded).decode() == "health-day|2026-10-01|2"


class TestRoundTrip:
    def test_reads_back_what_it_wrote(self):
        days, _ = _days()
        reflection = DayReflection(
            journal="A long day ✨ " * 200,  # Past one property's 1024 characters.
            intentions=["rest", "read"],
            complete=True,
            reflected=datetime(2026, 10, 2, 9, tzinfo=TZ),
        )
        day = HealthDay(
            day=DAY,
            assessments={
                "abc123": _assessment(
                    "abc123", 55, explanation="1h of 2h", metrics={"minutes": 60}, rationale="🎹" * 900,
                    assessed=datetime(2026, 10, 2, 9, tzinfo=TZ),
                ),
                "overall": _assessment("overall", "skip"),
            },
            reflection=reflection,
        )

        days.write(day, "A summary", "overall")

        (read,) = days.read(DAY, date(2026, 10, 2), TZ).values()
        assert read.assessments == day.assessments
        assert read.reflection == reflection
        assert read.parts == 1

    def test_no_value_is_over_calendars_limit_counting_any_way(self):
        encoded = encode(_day(1, rationale="🎹" * 3000), "", "overall")

        for value in encoded[0]["extendedProperties"]["private"].values():
            assert len(value.encode("utf-16-le")) // 2 <= 1024
            assert len(value.encode()) <= 1024

    def test_days_with_nothing_are_empty(self):
        days, _ = _days()

        assert days.read(DAY, date(2026, 10, 2), TZ) == {}


class TestParts:
    def test_a_day_that_doesnt_fit_in_one_event_is_split_and_titled_by_part(self):
        days, calendar = _days()
        # About 3 kB each: ten or so to an event.
        day = _day(25, rationale="x" * 3000, reflection=DayReflection(complete=True))
        day.assessments["overall"] = _assessment("overall", 75)

        written = days.write(day, "A summary", "overall")

        assert written.parts == len(calendar.events) >= 3
        for part in range(1, written.parts + 1):
            item = calendar.events[day_event_id(DAY, part)]
            assert item["summary"] == f"📝 Reflection · 2026-10-01 · 🟢 75 ({part}/{written.parts})"
            properties = item["extendedProperties"]["private"]
            assert sum(len(k.encode()) + len(v.encode()) for k, v in properties.items()) <= PART_BUDGET_BYTES
            assert len(properties) <= 300
        (read,) = days.read(DAY, date(2026, 10, 2), TZ).values()
        assert read.assessments == day.assessments
        assert read.parts == written.parts

    def test_parts_no_longer_needed_are_deleted(self):
        days, calendar = _days()
        big = days.write(_day(25, rationale="x" * 3000), "", "overall")

        small = days.write(_day(2, parts=big.parts), "", "overall")

        assert small.parts == 1
        assert set(calendar.events) == {day_event_id(DAY, 1)}
        assert calendar.events[day_event_id(DAY, 1)]["summary"] == "📊 Goal health · 2026-10-01"

    def test_the_reflection_journal_and_summary_stay_in_the_first_part(self):
        reflection = DayReflection(journal="tired", complete=False)
        bodies = encode(_day(25, rationale="x" * 3000, reflection=reflection), "A summary", "overall")

        assert bodies[0]["description"] == "tired\n\nA summary"
        assert all(b["description"] == "" for b in bodies[1:])
        assert f"{PREFIX}complete" in bodies[0]["extendedProperties"]["private"]
        assert all(f"{PREFIX}complete" not in b["extendedProperties"]["private"] for b in bodies[1:])
        assert bodies[0]["summary"] == f"📝 Reflection · 2026-10-01 (in progress) (1/{len(bodies)})"


class TestDescription:
    def test_is_cut_short_to_fit_without_losing_anything(self):
        days, calendar = _days()
        day = _day(200, reflection=DayReflection(journal="j" * 7000, complete=True))
        days.write(day, "A goal with a long name\n" * 200, "overall")

        description = calendar.events[day_event_id(DAY, 1)]["description"]
        assert len(description.encode()) <= MAX_DESCRIPTION_BYTES
        assert description.endswith("…")
        (read,) = days.read(DAY, date(2026, 10, 2), TZ).values()
        assert len(read.assessments) == 200
        assert read.reflection.journal == "j" * 7000


class TestChecking:
    def test_raises_if_calendar_didnt_keep_every_property(self):
        days, calendar = _days()
        calendar.dropping = True

        with pytest.raises(ValueError, match="didn't keep 2026-10-01's goal health as written"):
            days.write(_day(1), "", "overall")


class TestDecode:
    def test_ignores_other_events_and_cancelled_ones(self):
        (body,) = encode(_day(1), "", "overall")
        other = {"start": body["start"], "end": body["end"], "extendedProperties": {"private": {f"{PREFIX}kind": "x"}}}

        assert decode([{**body, "status": "cancelled"}, other]) == {}
        assert list(decode([body])) == [DAY]
