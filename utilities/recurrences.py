"""Recurring events as a whole: reading a series, editing every event in
it, and splitting it at one of its events so that event and the ones
after it can be edited apart from those before.

Google Calendar keeps a recurring series as one *master* event, whose
`recurrence` holds its RFC 5545 rules ("RRULE:FREQ=WEEKLY;BYDAY=MO"), and
whose instances each carry the master's id as their `recurring_event_id`.
Editing the master edits every instance -- including one edited on its
own (an exception), whose fields other than its times are all reset to
the master's, even those the edit doesn't set; an edit to the master's
start/end resets the exception's times too. Found with
probe_series_edits.py.

Splitting ("this and following events") follows Google's own recipe --
https://developers.google.com/workspace/calendar/api/guides/recurringevents#modifying_all_following_instances
-- the series is ended just before the event, and a new series, a copy of
the old one, starts at it:

- an UNTIL rule keeps the same UNTIL; a COUNT rule is split between the
  two, so together they still have COUNT events; any EXDATE/RDATE lines
  are copied to both (each ignores dates outside its own span);
- the new series' id is derived from the old one's and the split time, so
  a split that's retried after failing half way finds the series it
  already made rather than making another;
- instances after the split that were edited on their own (moved,
  retitled) are dropped with the rest of the old series' tail, as in
  Google Calendar itself: the new series makes them afresh, unedited.

Edits to a series aren't reallocated (see utilities/reallocating_calendar.py):
they change many days at once, and each day's reallocation happens when
its own events are next written.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError

from calendar_clients.google_calendar import Event

RULE_PREFIXES = ("RRULE:", "EXRULE:", "RDATE", "EXDATE")
"""What each of a series' rule lines must start with (RDATE and EXDATE
lines may carry parameters, e.g. "EXDATE;TZID=...:")."""

_EDITABLE = (
    "summary",
    "start",
    "end",
    "description",
    "location",
    "min_duration",
    "is_fixed_duration",
    "is_fixed_time",
    "priority",
    "goal_ids",
    "recurrence",
)
"""The fields of a series that `Recurrences.update` writes."""


class _Calendar(Protocol):
    """What Recurrences reads and writes events through: a GoalCalendar,
    so events' goals are filled in and their labels derived."""

    def get_event(self, event_id: str) -> Event: ...
    def create_event(self, event: Event) -> Event: ...
    def update_event(self, event: Event) -> Event: ...


class Recurrences:
    """Recurring series on one calendar -- see the module docstring.
    `list_instances(series_id, before)` lists a series' instances
    (CalendarClient.list_instances), and `time_zone` gives the calendar's
    own zone, for a series that doesn't name one."""

    def __init__(
        self,
        calendar: _Calendar,
        list_instances: Callable[[str, datetime], list[Event]],
        time_zone: Callable[[], ZoneInfo],
    ) -> None:
        self._calendar = calendar
        self._list_instances = list_instances
        self._time_zone = time_zone

    def series(self, event_id: str) -> Event:
        """The master event of the series `event_id` is, or is one of.
        Raises ValueError if it isn't part of one."""
        event = self._calendar.get_event(event_id)
        if event.recurring_event_id:
            event = self._calendar.get_event(event.recurring_event_id)
        if not event.recurrence:
            raise ValueError(f"Event {event_id} isn't part of a recurring series")
        return event

    def update(self, changes: Event, starting_at: str | None = None) -> list[Event]:
        """Write whichever of `changes`' editable fields are set (see
        `_EDITABLE`), and clear those in `changes.cleared`, on the series
        `changes.id` is, or is one of -- or,
        given `starting_at` (one of its events), first split the series
        there, and edit only the part from that event on. Returns the
        edited series, then the earlier part if it was split.

        `changes.start`/`end` are the series' as the caller saw it (when
        its first event starts and ends), so when it's split they move
        the part from `starting_at` on by as much as they moved those: a
        weekly 09:00 series changed to 10:00 from some Monday on starts at
        10:00 that Monday, not on the series' first day."""
        if not changes.id:
            raise ValueError("Say which series to update: its id, or one of its events'")
        if changes.recurrence is not None:
            check_rules(changes.recurrence)
        series = self.series(changes.id)
        earlier = None
        target = series
        if starting_at is not None:
            if self.series(starting_at).id != series.id:
                raise ValueError(f"Event {starting_at} isn't one of series {series.id}'s events")
            earlier, target = self.split(starting_at)
        patch = Event(
            id=target.id, cleared=changes.cleared, **{name: getattr(changes, name) for name in _EDITABLE}
        )
        if target.id != series.id:
            if patch.start is not None:
                patch.start = target.start + (patch.start - series.start)
            if patch.end is not None:
                patch.end = target.end + (patch.end - series.end)
        if patch.start is not None or patch.end is not None:
            # A series' times need its zone, to keep its wall-clock time.
            patch.time_zone = target.time_zone or self._time_zone().key
        updated = self._calendar.update_event(patch)
        return [updated] + ([earlier] if earlier else [])

    def split(self, event_id: str) -> tuple[Event | None, Event]:
        """Split the series `event_id` is one of at that event: end it just
        before, and start a copy there. Returns the series before the
        event (`None` if it was the first, which leaves nothing to split)
        and the series from it on."""
        instance = self._calendar.get_event(event_id)
        if not instance.recurring_event_id:
            if instance.recurrence:
                return None, instance  # The series itself: its first event.
            raise ValueError(f"Event {event_id} isn't part of a recurring series")
        series = self._calendar.get_event(instance.recurring_event_id)
        at = instance.original_start or instance.start
        if at <= series.start:
            return None, series
        rules = list(series.recurrence or [])
        index = next((i for i, rule in enumerate(rules) if rule.startswith("RRULE:")), None)
        if index is None:
            raise ValueError(f"Series {series.id} has no RRULE, so it can't be split")
        parts = _rule_parts(rules[index])

        later_parts = dict(parts)
        if "COUNT" in parts:
            before = sum(
                1 for event in self._list_instances(series.id, at) if (event.original_start or event.start) < at
            )
            remaining = int(parts["COUNT"]) - before
            if remaining <= 0:
                raise ValueError(f"Event {event_id} is after the last of series {series.id}")
            later_parts["COUNT"] = str(remaining)
        earlier_parts = {k: v for k, v in parts.items() if k not in ("COUNT", "UNTIL")}
        earlier_parts["UNTIL"] = (at - timedelta(seconds=1)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        zone = series.time_zone or self._time_zone().key
        start = at.astimezone(ZoneInfo(zone))
        later = replace(
            series,
            id=split_series_id(series.id, at),
            start=start,
            end=start + (series.end - series.start),
            time_zone=zone,
            recurrence=_with_rule(rules, index, later_parts),
            status=None,
        )
        try:
            created = self._calendar.create_event(later)
        except HttpError as exc:
            if exc.resp.status != 409:
                raise
            created = self._calendar.get_event(later.id)  # Made by an earlier try.
        earlier = self._calendar.update_event(
            Event(id=series.id, recurrence=_with_rule(rules, index, earlier_parts))
        )
        return earlier, created


def split_series_id(series_id: str, at: datetime) -> str:
    """The id of the series split from `series_id` at `at`: the same every
    time, and a valid Calendar event id (a-v and 0-9)."""
    digest = hashlib.sha256(f"{series_id}|{at.astimezone(timezone.utc).isoformat()}".encode()).digest()
    return base64.b32hexencode(digest[:20]).decode().lower()


def check_rules(rules: list[str]) -> None:
    """Raise ValueError unless `rules` are a series' rule lines, with
    exactly one RRULE."""
    if not rules:
        raise ValueError("A series needs a rule, like RRULE:FREQ=WEEKLY")
    bad = [rule for rule in rules if not rule.startswith(RULE_PREFIXES)]
    if bad:
        raise ValueError(f"Rules must start with RRULE:, EXRULE:, RDATE or EXDATE, not {bad[0]!r}")
    if sum(rule.startswith("RRULE:") for rule in rules) != 1:
        raise ValueError("A series needs exactly one RRULE line")
    for rule in rules:
        if rule.startswith("RRULE:") and "FREQ" not in _rule_parts(rule):
            raise ValueError(f"{rule!r} needs a FREQ, like FREQ=WEEKLY")


def _rule_parts(rule: str) -> dict[str, str]:
    """"RRULE:FREQ=WEEKLY;BYDAY=MO" as {"FREQ": "WEEKLY", "BYDAY": "MO"}."""
    body = rule.split(":", 1)[1]
    return dict(part.split("=", 1) for part in body.split(";") if "=" in part)


def _with_rule(rules: list[str], index: int, parts: dict[str, str]) -> list[str]:
    return [
        "RRULE:" + ";".join(f"{k}={v}" for k, v in parts.items()) if i == index else rule
        for i, rule in enumerate(rules)
    ]


_UNITS = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}
_DAYS = {"MO": "Mon", "TU": "Tue", "WE": "Wed", "TH": "Thu", "FR": "Fri", "SA": "Sat", "SU": "Sun"}
_ORDINALS = {"1": "first", "2": "second", "3": "third", "4": "fourth", "-1": "last"}


def describe_rules(rules: list[str] | None, time_zone: ZoneInfo | None = None) -> str | None:
    """A series' rules in a phrase -- "Every week on Mon, Wed, until Dec 31,
    2026" -- or the rules themselves, joined, for what this doesn't know."""
    if not rules:
        return None
    rrules = [rule for rule in rules if rule.startswith("RRULE:")]
    if len(rrules) != 1:
        return "; ".join(rules)
    parts = _rule_parts(rrules[0])
    unit = _UNITS.get(parts.get("FREQ", ""))
    known = {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "COUNT", "UNTIL", "WKST"}
    if unit is None or set(parts) - known:
        return "; ".join(rules)
    interval = int(parts.get("INTERVAL", "1") or 1)
    phrase = f"Every {unit}" if interval == 1 else f"Every {interval} {unit}s"
    if byday := parts.get("BYDAY"):
        days = []
        for day in byday.split(","):
            ordinal, code = day[:-2], day[-2:]
            name = _DAYS.get(code, code)
            days.append(f"the {_ORDINALS.get(ordinal, ordinal)} {name}" if ordinal else name)
        phrase += " on " + ", ".join(days)
    if bymonthday := parts.get("BYMONTHDAY"):
        phrase += " on day " + ", ".join(bymonthday.split(","))
    if count := parts.get("COUNT"):
        phrase += f", {count} times"
    if until := parts.get("UNTIL"):
        try:
            when = (
                datetime.strptime(until, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(time_zone)
                if "T" in until
                else datetime.strptime(until, "%Y%m%d")
            )
            phrase += f", until {when:%b} {when.day}, {when.year}"
        except ValueError:
            phrase += f", until {until}"
    exceptions = sum(rule.startswith("EXDATE") for rule in rules)
    if exceptions:
        phrase += ", with exceptions"
    extra = [rule for rule in rules if rule.startswith(("RDATE", "EXRULE:"))]
    if extra:
        phrase += ", and more (" + "; ".join(extra) + ")"
    return phrase
