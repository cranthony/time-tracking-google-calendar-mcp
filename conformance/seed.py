"""Calendars as a user's would be before a scenario starts: a few days of
planned events, each day ending in a sleep marked as the day's end (which
only calendar_cli.py sets, so it's seeded here)."""

from __future__ import annotations

from datetime import date, timedelta

from conformance.fake_google import FakeGoogle

ZONE = "America/New_York"
SLEEP_KEY = "cascading-time-tracker-is_end_of_day_sleep"


def local(day: date, hhmm: str) -> str:
    """A New York wall-clock time, as Google stores one with a timeZone."""
    return f"{day.isoformat()}T{hhmm}:00"


def event(google: FakeGoogle, calendar_id: str, day: date, start: str, end: str, summary: str, *, end_day: date | None = None, sleep: bool = False, **fields) -> str:
    body = {
        "summary": summary,
        "start": {"dateTime": local(day, start), "timeZone": ZONE},
        "end": {"dateTime": local(end_day or day, end), "timeZone": ZONE},
        **fields,
    }
    if sleep:
        body.setdefault("extendedProperties", {}).setdefault("private", {})[SLEEP_KEY] = "true"
    return google.add_event(calendar_id, body)


def planned_days(google: FakeGoogle, calendar_id: str, first: date, days: int) -> dict[date, dict[str, str]]:
    """`days` ordinary days from `first`: breakfast, work, lunch, more work,
    dinner, then sleep to the next morning. Returns each day's event ids
    by summary."""
    ids: dict[date, dict[str, str]] = {}
    for offset in range(days):
        day = first + timedelta(days=offset)
        ids[day] = {
            "Breakfast": event(google, calendar_id, day, "07:00", "07:30", "Breakfast"),
            "Work": event(google, calendar_id, day, "08:00", "12:00", "Work"),
            "Lunch": event(google, calendar_id, day, "12:00", "13:00", "Lunch"),
            "Afternoon work": event(google, calendar_id, day, "13:00", "17:00", "Afternoon work"),
            "Dinner": event(google, calendar_id, day, "18:00", "19:00", "Dinner"),
            "Sleep": event(google, calendar_id, day, "22:30", "06:30", "Sleep", end_day=day + timedelta(days=1), sleep=True),
        }
    return ids
