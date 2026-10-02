"""Days as you live them: bounded by your end-of-day sleep, not by midnight.

Day D starts when you wake on D -- the end of the `is_end_of_day_sleep`
event that ends on that date -- and lasts until you wake the next day, so
it includes the night's sleep, as reallocation's "day" does (see
utilities/reallocation.py).

The calendar's end-of-day sleep events are trusted as they are -- marked
with calendar_cli.py, their times kept up to date by note compaction --
and nothing is guessed: a period of days (a day, a week, a month) runs
from the sleep ending on its first day to the sleep ending on the day
after its last, and if either isn't in the calendar, `period_window`
refuses rather than pick a time. A period whose ending sleep hasn't ended
yet -- not in the calendar, or planned but still to come -- isn't over
(NotOver); one missing an earlier sleep can't be bounded (MissingSleep).
utilities/reflection.py and utilities/goal_health.py read events and notes
in these windows.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo

from calendar_clients.google_calendar import Event
from utilities.goal_periods import Period

LISTING_MARGIN = timedelta(days=1)
"""How far past a period's midnights to list events, to find the sleeps
that bound it."""


class MissingSleep(ValueError):
    """A period's bounds need an end-of-day sleep that isn't logged."""

    def __init__(self, days: list[date]) -> None:
        self.days = days
        listed = ", ".join(day.isoformat() for day in days)
        super().__init__(
            f"No end-of-day sleep ends on {listed}, so it can't be told when "
            f"{'that day' if len(days) == 1 else 'those days'} started. Add the sleep you woke from to "
            "the calendar, marked as an end-of-day sleep, then try again."
        )


def day_start(day: date, events: list[Event], tz: tzinfo) -> datetime | None:
    """When `day` started -- the end of the first end-of-day sleep ending on
    it -- or `None` if the calendar has none. A sleep still to come counts:
    see `period_window`. `events` must cover `day`."""
    wakes = [
        event.end
        for event in events
        if event.is_end_of_day_sleep and event.status != "cancelled" and event.end.astimezone(tz).date() == day
    ]
    return min(wakes) if wakes else None


class NotOver(ValueError):
    """A period that hasn't ended: you haven't woken from its last night."""

    def __init__(self, span: Period) -> None:
        self.span = span
        super().__init__(
            f"{span.id} isn't over yet: it ends when you wake on {span.end.isoformat()}. If you have, "
            "check the calendar has the sleep you woke from, marked as an end-of-day sleep and ending "
            "when you woke, then try again."
        )


def period_window(span: Period, events: list[Event], tz: tzinfo, now: datetime) -> tuple[datetime, datetime]:
    """Where `span` starts and ends: when you woke on its first day, and
    when you woke the day after its last. Raises NotOver if it hasn't ended
    -- its ending sleep isn't in the calendar yet, or is but ends after
    `now` -- else MissingSleep if a sleep it needs isn't in the calendar.
    `events` must cover it with LISTING_MARGIN to spare."""
    start = day_start(span.start, events, tz)
    end = day_start(span.end, events, tz)
    if (end is None or end > now) and span.end >= now.astimezone(tz).date():
        raise NotOver(span)
    missing = [day for day, woke in [(span.start, start), (span.end, end)] if woke is None]
    if missing:
        raise MissingSleep(missing)
    return start, end


def current_day(events: list[Event], tz: tzinfo, now: datetime) -> date:
    """The day `now` falls in: today's date once you've woken today (an
    end-of-day sleep in the calendar ended today, before `now`), else
    yesterday's. `events` must cover the last two days."""
    today = now.astimezone(tz).date()
    started = day_start(today, events, tz)
    return today if started is not None and started <= now else today - timedelta(days=1)


def listing_range(span: Period, tz: tzinfo) -> tuple[datetime, datetime]:
    """The times to list events between, to read `span` and the sleeps
    that bound it."""
    return (
        datetime.combine(span.start, time(), tz) - LISTING_MARGIN,
        datetime.combine(span.end, time(), tz) + LISTING_MARGIN,
    )
