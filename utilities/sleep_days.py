"""Days as you live them: bounded by your end-of-day sleep, not by midnight.

Day D starts when you wake on D -- the end of the `is_end_of_day_sleep`
event that ends on that date -- and lasts until you wake the next day, so
it includes the night's sleep, as reallocation's "day" does (see
utilities/reallocation.py). Without a sleep event ending on a date, that
date's day starts at midnight -- but not until midday, if that's still to
come: before then, a missing sleep is more likely not logged yet than
skipped, so the day before carries on.

A period of days (a week, a month) runs from its first day's start to the
start of the day after its last; utilities/reflection.py and utilities/
goal_health.py read events and notes in these windows.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo

from calendar_clients.google_calendar import Event
from utilities.goal_periods import Period

LISTING_MARGIN = timedelta(days=1)
"""How far past a period's midnights to list events, to find the sleeps
that bound it."""


def day_start(day: date, events: list[Event], tz: tzinfo, now: datetime) -> datetime | None:
    """When `day` started: the end of the first end-of-day sleep ending on
    it, or else its midnight -- or `None` if it hasn't started yet (see the
    module docstring). `events` must cover `day`."""
    wakes = [
        event.end
        for event in events
        if event.is_end_of_day_sleep and event.status != "cancelled" and event.end.astimezone(tz).date() == day
    ]
    if wakes:
        return min(wakes)
    midnight = datetime.combine(day, time(), tz)
    if day < now.astimezone(tz).date() or now >= midnight + timedelta(hours=12):
        return midnight
    return None


def period_window(span: Period, events: list[Event], tz: tzinfo, now: datetime) -> tuple[datetime, datetime]:
    """Where `span` starts and ends -- its end being `now` while its last day
    is still going on. `events` must cover it with LISTING_MARGIN to spare."""
    start = day_start(span.start, events, tz, now) or datetime.combine(span.start, time(), tz)
    end = day_start(span.end, events, tz, now) or max(now, start)
    return start, max(start, end)


def current_day(events: list[Event], tz: tzinfo, now: datetime) -> date:
    """The day `now` falls in: today's date once that day has started, else
    yesterday's. `events` must cover the last two days."""
    today = now.astimezone(tz).date()
    started = day_start(today, events, tz, now)
    return today if started is not None and started <= now else today - timedelta(days=1)


def listing_range(span: Period, tz: tzinfo) -> tuple[datetime, datetime]:
    """The times to list events between, to read `span` and the sleeps
    that bound it."""
    return (
        datetime.combine(span.start, time(), tz) - LISTING_MARGIN,
        datetime.combine(span.end, time(), tz) + LISTING_MARGIN,
    )
