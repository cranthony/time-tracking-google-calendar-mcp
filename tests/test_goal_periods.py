from datetime import date

import pytest

from utilities.goal_periods import last_ended, parse_period, period_containing


class TestPeriodContaining:
    @pytest.mark.parametrize(
        "cadence, day, period_id, start, end",
        [
            ("daily", date(2026, 9, 30), "2026-09-30", date(2026, 9, 30), date(2026, 10, 1)),
            # Weeks run Sunday to Saturday.
            ("weekly", date(2026, 10, 3), "week-2026-09-27", date(2026, 9, 27), date(2026, 10, 4)),
            ("weekly", date(2026, 9, 27), "week-2026-09-27", date(2026, 9, 27), date(2026, 10, 4)),
            ("weekly", date(2026, 12, 31), "week-2026-12-27", date(2026, 12, 27), date(2027, 1, 3)),
            ("monthly", date(2026, 12, 31), "2026-12", date(2026, 12, 1), date(2027, 1, 1)),
            # Pairs of months, aligned to the year: Jan-Feb, ..., Nov-Dec.
            ("every_2_months", date(2026, 10, 2), "2026-09..10", date(2026, 9, 1), date(2026, 11, 1)),
            ("every_2_months", date(2026, 12, 1), "2026-11..12", date(2026, 11, 1), date(2027, 1, 1)),
            ("every_2_months", date(2027, 2, 28), "2027-01..02", date(2027, 1, 1), date(2027, 3, 1)),
        ],
    )
    def test_finds_the_period_and_parses_its_id_back(self, cadence, day, period_id, start, end):
        period = period_containing(cadence, day)

        assert (period.id, period.start, period.end) == (period_id, start, end)
        assert parse_period(cadence, period_id) == period
        assert period.contains(day) and not period.contains(end)

    def test_steps_between_periods(self):
        week = period_containing("weekly", date(2026, 10, 2))

        assert week.previous().id == "week-2026-09-20"
        assert week.next().id == "week-2026-10-04"

    def test_last_ended_is_the_period_before_todays(self):
        assert last_ended("daily", date(2026, 10, 2)).id == "2026-10-01"
        assert last_ended("monthly", date(2026, 1, 15)).id == "2025-12"


class TestParsePeriod:
    @pytest.mark.parametrize(
        "cadence, period_id, message",
        [
            ("daily", "2026-9-30", "those look like '2026-09-30'"),
            ("weekly", "2026-09-27", "those look like 'week-2026-09-27'"),
            ("weekly", "week-2026-09-28", "doesn't start on a Sunday"),
            ("monthly", "2026-13", "isn't a period for the monthly cadence"),
            ("every_2_months", "2026-10..11", "those look like '2026-09..10'"),
            ("hourly", "x", "Unknown cadence"),
        ],
    )
    def test_refuses_what_isnt_a_period_of_that_cadence(self, cadence, period_id, message):
        with pytest.raises(ValueError, match=message):
            parse_period(cadence, period_id)
