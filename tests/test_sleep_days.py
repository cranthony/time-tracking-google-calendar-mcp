from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from calendar_clients.google_calendar import Event
from utilities.goal_periods import parse_period
from utilities.sleep_days import MissingSleep, NotOver, current_day, day_start, period_window

TZ = ZoneInfo("America/New_York")


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def _sleep(start: datetime, end: datetime, **fields) -> Event:
    return Event(id=f"s{start:%d%H}", summary="Sleep", start=start, end=end, is_end_of_day_sleep=True, **fields)


def test_a_day_starts_when_its_end_of_day_sleep_ends():
    events = [_sleep(_at(1, 23), _at(2, 7, 15)), Event(id="nap", start=_at(2, 13), end=_at(2, 14))]

    assert day_start(date(2026, 10, 2), events, TZ) == _at(2, 7, 15)


def test_without_a_logged_sleep_a_day_has_no_start():
    cancelled = [_sleep(_at(1, 23), _at(2, 7), status="cancelled")]

    assert day_start(date(2026, 10, 2), cancelled, TZ) is None
    assert day_start(date(2026, 10, 2), [], TZ) is None


def test_the_current_day_goes_on_past_midnight_until_waking():
    events = [_sleep(_at(1, 23, 30), _at(2, 7))]

    assert current_day(events, TZ, _at(3, 1)) == date(2026, 10, 2)  # Up late on the 2nd.
    assert current_day(events, TZ, _at(2, 6)) == date(2026, 10, 1)  # Still asleep.
    assert current_day(events, TZ, _at(2, 8)) == date(2026, 10, 2)
    assert current_day([], TZ, _at(2, 8)) == date(2026, 10, 1)  # No waking logged yet.


def test_a_periods_window_runs_waking_to_waking():
    events = [_sleep(_at(3, 23), _at(4, 7)), _sleep(_at(10, 23), _at(11, 6, 30))]
    week = parse_period("weekly", "week-2026-10-04")  # Sun Oct 4 - Sat Oct 10.

    assert period_window(week, events, TZ, _at(13, 9)) == (_at(4, 7), _at(11, 6, 30))


def test_a_period_missing_a_sleep_is_refused_naming_the_days():
    events = [_sleep(_at(10, 23), _at(11, 6, 30))]
    week = parse_period("weekly", "week-2026-10-04")

    with pytest.raises(MissingSleep, match="2026-10-04") as raised:
        period_window(week, events, TZ, _at(13, 9))

    assert raised.value.days == [date(2026, 10, 4)]


def test_a_period_still_going_on_is_refused_as_not_over():
    events = [_sleep(_at(11, 23), _at(12, 6, 30))]
    today = parse_period("daily", "2026-10-12")

    with pytest.raises(NotOver, match="isn't over yet: it ends when you wake on 2026-10-13"):
        period_window(today, events, TZ, _at(12, 20))
    # Its last day was yesterday, but waking today isn't in the calendar: not over either.
    with pytest.raises(NotOver):
        period_window(today, events, TZ, _at(13, 9))


def test_a_planned_sleep_still_to_come_doesnt_end_a_period():
    tonight = _sleep(_at(12, 23), _at(13, 7))  # In the calendar ahead of time.
    events = [_sleep(_at(11, 23), _at(12, 6, 30)), tonight]
    today = parse_period("daily", "2026-10-12")

    with pytest.raises(NotOver):
        period_window(today, events, TZ, _at(12, 21))  # 9pm: not asleep yet.
    with pytest.raises(NotOver):
        period_window(today, events, TZ, _at(13, 6))  # Asleep, until 7am.
    assert period_window(today, events, TZ, _at(13, 8)) == (_at(12, 6, 30), _at(13, 7))
