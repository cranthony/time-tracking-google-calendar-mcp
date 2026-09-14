from datetime import datetime, timedelta, timezone

import pytest

from tests.event_time_helpers import DAY, event_at, time_at

UTC = timezone.utc


class TestTimeAt:
    def test_parses_hh_mm_on_day(self):
        assert time_at("09:30") == datetime(DAY.year, DAY.month, DAY.day, 9, 30, tzinfo=UTC)

    def test_plus_one_falls_on_the_day_after(self):
        next_day = DAY + timedelta(days=1)
        assert time_at("07:00+1") == datetime(
            next_day.year, next_day.month, next_day.day, 7, 0, tzinfo=UTC
        )

    def test_rejects_an_unrecognized_format(self):
        with pytest.raises(ValueError):
            time_at("9:30am")


class TestEventAt:
    def test_sets_start_and_end(self):
        event = event_at("09:00-10:30")

        assert event.start == time_at("09:00")
        assert event.end == time_at("10:30")

    def test_end_on_the_day_after_start(self):
        event = event_at("20:00-07:00+1")

        assert event.start == time_at("20:00")
        assert event.end == time_at("07:00+1")
        assert event.end > event.start

    def test_defaults_summary_to_event(self):
        event = event_at("09:00-10:00")

        assert event.summary == "Event"

    def test_overrides_apply_and_take_precedence(self):
        event = event_at("09:00-10:00", id="abc123", summary="Focus block", priority=1)

        assert event.id == "abc123"
        assert event.summary == "Focus block"
        assert event.priority == 1
