from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from calendar_clients.google_calendar import Event
from utilities.goal_periods import parse_period
from utilities.sleep_days import current_day, day_start, period_window

TZ = ZoneInfo("America/New_York")


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def _sleep(start: datetime, end: datetime, **fields) -> Event:
    return Event(id=f"s{start:%d%H}", summary="Sleep", start=start, end=end, is_end_of_day_sleep=True, **fields)


def test_a_day_starts_when_its_end_of_day_sleep_ends():
    events = [_sleep(_at(1, 23), _at(2, 7, 15)), Event(id="nap", start=_at(2, 13), end=_at(2, 14))]

    assert day_start(date(2026, 10, 2), events, TZ, _at(3, 12)) == _at(2, 7, 15)


def test_a_cancelled_sleep_doesnt_count():
    events = [_sleep(_at(1, 23), _at(2, 7), status="cancelled")]

    assert day_start(date(2026, 10, 2), events, TZ, _at(3, 12)) == _at(2, 7)


def test_without_a_sleep_a_day_starts_at_7am():
    assert day_start(date(2026, 10, 2), [], TZ, _at(2, 13)) == _at(2, 7)
    assert day_start(date(2026, 10, 2), [], TZ, _at(3, 1)) == _at(2, 7)
    # Before 7am with no sleep logged: still the day before.
    assert day_start(date(2026, 10, 2), [], TZ, _at(2, 6, 59)) is None
    assert current_day([], TZ, _at(2, 1)) == date(2026, 10, 1)
    assert current_day([], TZ, _at(2, 7)) == date(2026, 10, 2)


def test_the_current_day_goes_on_past_midnight_until_waking():
    events = [_sleep(_at(1, 23, 30), _at(2, 7))]

    assert current_day(events, TZ, _at(3, 1)) == date(2026, 10, 2)  # Up late on the 2nd.
    assert current_day(events, TZ, _at(2, 6)) == date(2026, 10, 1)  # Still asleep.
    assert current_day(events, TZ, _at(2, 8)) == date(2026, 10, 2)


def test_a_periods_window_runs_waking_to_waking_or_to_now():
    events = [_sleep(_at(4, 23), _at(5, 7)), _sleep(_at(11, 23), _at(12, 6, 30))]
    week = parse_period("weekly", "week-2026-10-04")  # Sun Oct 4 - Sat Oct 10.

    # Neither the 4th nor the 11th had a sleep ending on it: 7am. The 12th did.
    assert period_window(week, events, TZ, _at(13, 9)) == (_at(4, 7), _at(11, 7))
    day = parse_period("daily", "2026-10-11")
    assert period_window(day, events, TZ, _at(13, 9)) == (_at(11, 7), _at(12, 6, 30))
    today = parse_period("daily", "2026-10-12")
    assert period_window(today, events, TZ, _at(12, 20)) == (_at(12, 6, 30), _at(12, 20))
    assert period_window(today, events, TZ, _at(12, 20))[1] - _at(12, 6, 30) == timedelta(hours=13, minutes=30)
