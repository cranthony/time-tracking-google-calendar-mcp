"""Shared test helper for building `Event`s (and standalone datetimes) from
short time-of-day strings instead of verbose
`datetime(2026, 1, 1, 9, 30, tzinfo=UTC)` calls, so a test's events can be
read at a glance.

All times are on one hard-coded day (`DAY`, in UTC); "+1" after a time
means it falls on the day after `DAY` instead (e.g. an overnight sleep
block spanning "20:00-07:00+1").
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from calendar_clients.google_calendar import Event

UTC = timezone.utc
DAY = date(2026, 1, 1)

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(\+1)?$")


def time_at(time: str) -> datetime:
    """A `datetime` for `time` ("HH:MM", or "HH:MM+1" for the day after
    `DAY`), in UTC on `DAY`."""
    match = _TIME_RE.match(time)
    if match is None:
        raise ValueError(f'{time!r} doesn\'t look like "HH:MM" or "HH:MM+1"')
    hour, minute, next_day = match.groups()
    day = DAY + timedelta(days=1) if next_day else DAY
    return datetime(day.year, day.month, day.day, int(hour), int(minute), tzinfo=UTC)


def event_at(time_range: str, **overrides) -> Event:
    """An `Event` spanning `time_range` -- "HH:MM-HH:MM", or
    "HH:MM-HH:MM+1" if the end time falls on the day after `DAY` -- so a
    test's events can be read at a glance instead of full `datetime(...)`
    calls. Any other `Event` field (`id`, `summary`, `priority`,
    `min_duration`, `status`, ...) can be set via keyword argument, the
    same as constructing `Event` directly; `summary` defaults to `"Event"`
    if not given."""
    start_str, _, end_str = time_range.partition("-")
    fields = {"summary": "Event", "start": time_at(start_str), "end": time_at(end_str)}
    fields.update(overrides)
    return Event(**fields)
