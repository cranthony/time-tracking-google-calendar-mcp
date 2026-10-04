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
probes/series_edits.py.

Deleting a series cancels its master, which cancels every instance;
deleting from one of its events on ("this and following events") ends
the series just before that event, as splitting does below, but makes
no copy -- so it too works on any rules, and its later instances,
exceptions included, are dropped.

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

A series' rules reach this server's clients as a `Repeat` -- "every week
on Mon and Wed, until Dec 31" -- rather than Google's strings, and only
rules a Repeat can say can be written. Splitting works on the strings
themselves, so even a series made elsewhere, with rules a Repeat can't
say, can be split.

Edits to a series aren't reallocated (see utilities/reallocating_calendar.py):
they change many days at once, and each day's reallocation happens when
its own events are next written.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Annotated, Literal, Protocol
from zoneinfo import ZoneInfo

from googleapiclient.errors import HttpError
from pydantic import BeforeValidator, ConfigDict

from calendar_clients.google_calendar import Event

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
)
"""The fields of a series that `Recurrences.update` writes (its rules
are written from the `Repeat` passed alongside)."""


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

    def update(
        self, changes: Event, starting_at: str | None = None, repeat: Repeat | None = None
    ) -> list[Event]:
        """Write whichever of `changes`' editable fields are set (see
        `_EDITABLE`), and `repeat` if given, to the series `changes.id`
        is, or is one of -- or, given `starting_at` (one of its events),
        first split the series there, and edit only the part from that
        event on. Returns the edited series, then the earlier part if it
        was split. Raises ValueError, before writing anything, if
        `repeat` can't be written (see `Repeat.check`).

        `changes.start`/`end` are the series' as the caller saw it (when
        its first event starts and ends), so when it's split they move
        the part from `starting_at` on by as much as they moved those: a
        weekly 09:00 series changed to 10:00 from some Monday on starts at
        10:00 that Monday, not on the series' first day. `repeat` replaces
        the series' rules whole, in the split-off part if it's split."""
        if not changes.id:
            raise ValueError("Say which series to update: its id, or one of its events'")
        if repeat is not None:
            repeat.check()
        series = self.series(changes.id)
        earlier = None
        target = series
        if starting_at is not None:
            if self.series(starting_at).id != series.id:
                raise ValueError(f"Event {starting_at} isn't one of series {series.id}'s events")
            earlier, target = self.split(starting_at)
        patch = Event(id=target.id, **{name: getattr(changes, name) for name in _EDITABLE})
        if target.id != series.id:
            if patch.start is not None:
                patch.start = target.start + (patch.start - series.start)
            if patch.end is not None:
                patch.end = target.end + (patch.end - series.end)
        zone = target.time_zone or self._time_zone().key
        if patch.start is not None or patch.end is not None:
            # A series' times need its zone, to keep its wall-clock time.
            patch.time_zone = zone
        if repeat is not None:
            patch.recurrence = repeat.to_rules(ZoneInfo(zone))
        updated = self._calendar.update_event(patch)
        return [updated] + ([earlier] if earlier else [])

    def delete(self, event_id: str, starting_at: str | None = None) -> Event | None:
        """Delete the series `event_id` is, or is one of -- or, given
        `starting_at` (one of its events), only that event and the ones
        after it, by ending the series just before it. Returns what's
        left of the series, `None` if nothing is (`starting_at` was its
        first event, or wasn't given)."""
        series = self.series(event_id)
        at = None
        if starting_at is not None:
            instance = self._calendar.get_event(starting_at)
            if (instance.recurring_event_id or instance.id) != series.id:
                raise ValueError(f"Event {starting_at} isn't one of series {series.id}'s events")
            at = instance.original_start or instance.start
        if at is None or at <= series.start:
            self._calendar.update_event(Event(id=series.id, status="cancelled"))
            return None
        rules, index, parts = _rrule(series)
        return self._calendar.update_event(
            Event(id=series.id, recurrence=_with_rule(rules, index, _ending_before(parts, at)))
        )

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
        rules, index, parts = _rrule(series)

        later_parts = dict(parts)
        if "COUNT" in parts:
            before = sum(
                1 for event in self._list_instances(series.id, at) if (event.original_start or event.start) < at
            )
            remaining = int(parts["COUNT"]) - before
            if remaining <= 0:
                raise ValueError(f"Event {event_id} is after the last of series {series.id}")
            later_parts["COUNT"] = str(remaining)

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
            Event(id=series.id, recurrence=_with_rule(rules, index, _ending_before(parts, at)))
        )
        return earlier, created


def split_series_id(series_id: str, at: datetime) -> str:
    """The id of the series split from `series_id` at `at`: the same every
    time, and a valid Calendar event id (a-v and 0-9)."""
    digest = hashlib.sha256(f"{series_id}|{at.astimezone(timezone.utc).isoformat()}".encode()).digest()
    return base64.b32hexencode(digest[:20]).decode().lower()


def _rrule(series: Event) -> tuple[list[str], int, dict[str, str]]:
    """`series`' rule lines, the index of its RRULE among them, and that
    RRULE's parts. Raises ValueError if it has no RRULE."""
    rules = list(series.recurrence or [])
    index = next((i for i, rule in enumerate(rules) if rule.startswith("RRULE:")), None)
    if index is None:
        raise ValueError(f"Series {series.id} has no RRULE, so it can't be ended early")
    return rules, index, _rule_parts(rules[index])


def _ending_before(parts: dict[str, str], at: datetime) -> dict[str, str]:
    """An RRULE's `parts`, ending just before `at` (any COUNT or UNTIL
    replaced)."""
    ended = {k: v for k, v in parts.items() if k not in ("COUNT", "UNTIL")}
    ended["UNTIL"] = (at - timedelta(seconds=1)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return ended


def _rule_parts(rule: str) -> dict[str, str]:
    """"RRULE:FREQ=WEEKLY;BYDAY=MO" as {"FREQ": "WEEKLY", "BYDAY": "MO"}."""
    body = rule.split(":", 1)[1]
    return dict(part.split("=", 1) for part in body.split(";") if "=" in part)


def _with_rule(rules: list[str], index: int, parts: dict[str, str]) -> list[str]:
    return [
        "RRULE:" + ";".join(f"{k}={v}" for k, v in parts.items()) if i == index else rule
        for i, rule in enumerate(rules)
    ]


Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

_FREQS = {"day": "DAILY", "week": "WEEKLY", "month": "MONTHLY", "year": "YEARLY"}
_WEEKDAYS = {"mon": "MO", "tue": "TU", "wed": "WE", "thu": "TH", "fri": "FR", "sat": "SA", "sun": "SU"}
_NTHS = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", -1: "last"}
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_PARTS = {"FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "COUNT", "UNTIL", "WKST"}
"""The RRULE parts a Repeat can say."""


def _date_or_datetime(value: object) -> object:
    """"2026-12-31" as a date and "2026-12-31T00:00:00-05:00" as a
    datetime -- which pydantic, left to itself, can confuse either way."""
    if isinstance(value, str):
        return date.fromisoformat(value) if len(value) == 10 else datetime.fromisoformat(value)
    return value


@dataclass(kw_only=True)
class NthWeekday:
    """One weekday of a month: the first Monday, the last Friday."""

    __pydantic_config__ = ConfigDict(use_attribute_docstrings=True)

    nth: Literal[1, 2, 3, 4, 5, -1]
    """Which of the month's `weekday`s: 1 for the first, 2 for the second,
    ... 5 for the fifth (months without one are skipped), or -1 for the
    last."""

    weekday: Weekday
    """The day of the week, e.g. "mon"."""


@dataclass(kw_only=True)
class Repeat:
    """How a recurring series repeats, starting from its first event's
    `start`: its events are at that event's time of day (in the series'
    time zone), on the days this says.

    Examples:
    - every weekday: {"every": "week", "weekdays": ["mon", "tue", "wed", "thu", "fri"]}
    - every other Tuesday: {"every": "week", "interval": 2, "weekdays": ["tue"]}
    - the 1st and 15th of each month: {"every": "month", "month_days": [1, 15]}
    - the last Friday of each month: {"every": "month", "nth_weekdays": [{"nth": -1, "weekday": "fri"}]}
    - the 4th Thursday of November: {"every": "year", "months": [11], "nth_weekdays": [{"nth": 4, "weekday": "thu"}]}
    - daily, 10 times: {"every": "day", "count": 10}
    - weekly through 2026: {"every": "week", "until": "2026-12-31"}

    Leaving out weekdays, nth_weekdays, month_days and months repeats on
    the first event's own day: weekly on its weekday, monthly on its day
    of the month, yearly on its date. Only what's shown here can be
    written; a Repeat that breaks a rule given below (e.g. count with
    until) is refused with the reason, and nothing is changed."""

    __pydantic_config__ = ConfigDict(use_attribute_docstrings=True)

    every: Literal["day", "week", "month", "year"]
    """How often the series repeats: its events fall in every `interval`th
    day, week, month or year."""

    interval: int = 1
    """1 for every day/week/month/year, 2 for every other one, and so on.
    At least 1."""

    weekdays: list[Weekday] | None = None
    """The days of the week events fall on, e.g. ["mon", "wed"]. Mostly
    for `every` "week". With "day" it keeps only those days (every
    weekday: "week" with all five is clearer); with "month" or "year"
    it's every one of those weekdays in the month or year -- use
    `nth_weekdays` for "the first Monday"."""

    nth_weekdays: list[NthWeekday] | None = None
    """Particular weekdays of the month, like the first Monday or the last
    Friday. Only for `every` "month", or "year" with `months` set (each
    then counts within those months). Can be given with `weekdays`."""

    month_days: list[int] | None = None
    """The days of the month events fall on: 1 to 31, or -1 for the last
    day (months without a given day are skipped, so use -1 for "the end
    of the month", not 31). Not for `every` "week"."""

    months: list[int] | None = None
    """Only these months, 1 (January) to 12 (December). Mostly for `every`
    "year", e.g. [11] with `nth_weekdays` for the 4th Thursday of
    November."""

    count: int | None = None
    """End the series after this many events, counting from its first.
    Can't be given with `until`; give neither for a series that never
    ends."""

    until: Annotated[datetime | date | None, BeforeValidator(_date_or_datetime)] = None
    """End the series here: no event starts after it. A date ("2026-12-31")
    means through the end of that day, in the series' time zone; a
    datetime is exact. Can't be given with `count`."""

    week_starts_on: Weekday | None = None
    """The day weeks start on, which only matters for `every` "week" with
    an `interval` above 1 and several `weekdays`: it decides which days
    count as the same week. Monday when left out."""

    skipped: list[datetime] | None = None
    """Starts of events the series leaves out, e.g.
    ["2026-11-09T09:00:00-05:00"]. Each must be exactly the start the
    rules give that event (a time without an offset is taken to be in the
    series' time zone). (To cancel one event, cancelling it through
    update_event is usually simpler.)"""

    added: list[datetime] | None = None
    """Starts of extra events, beyond those the rules make, e.g.
    ["2026-12-01T14:00:00-05:00"] (a time without an offset is taken to
    be in the series' time zone). Each lasts as long as the series'
    events do."""

    def check(self) -> None:
        """Raise ValueError unless this can be written as a series' rules."""
        if self.every not in _FREQS:
            raise ValueError(f"every must be one of {', '.join(_FREQS)}, not {self.every!r}")
        if self.interval < 1:
            raise ValueError("interval must be at least 1")
        if self.count is not None and self.until is not None:
            raise ValueError("Give count or until, not both")
        if self.count is not None and self.count < 1:
            raise ValueError("count must be at least 1")
        days = list(self.weekdays or []) + [nth.weekday for nth in self.nth_weekdays or []]
        days += [self.week_starts_on] if self.week_starts_on else []
        if (bad_day := next((day for day in days if day not in _WEEKDAYS), None)) is not None:
            raise ValueError(f"Weekdays are {', '.join(_WEEKDAYS)}, not {bad_day!r}")
        if self.nth_weekdays:
            if (bad_nth := next((nth.nth for nth in self.nth_weekdays if nth.nth not in _NTHS), None)) is not None:
                raise ValueError(f"nth must be 1 to 5, or -1 for the last, not {bad_nth}")
            if self.every in ("day", "week"):
                raise ValueError(f'nth_weekdays are for every "month" or "year", not "{self.every}"')
            if self.every == "year" and not self.months:
                raise ValueError('nth_weekdays with every "year" need months, to say which month they\'re in')
        if self.month_days:
            if (bad := next((day for day in self.month_days if not (1 <= day <= 31 or day == -1)), None)) is not None:
                raise ValueError(f"month_days must be 1 to 31, or -1 for the last, not {bad}")
            if self.every == "week":
                raise ValueError('month_days are not for every "week"')
        if (bad := next((month for month in self.months or [] if not 1 <= month <= 12), None)) is not None:
            raise ValueError(f"months must be 1 to 12, not {bad}")

    def to_rules(self, zone: ZoneInfo) -> list[str]:
        """These as a series' rule lines, for a series in `zone`: an RRULE,
        then EXDATE and RDATE lines for `skipped` and `added`. Raises
        ValueError if they can't be written (see `check`)."""
        self.check()
        parts = {"FREQ": _FREQS[self.every]}
        if self.interval != 1:
            parts["INTERVAL"] = str(self.interval)
        if self.months:
            parts["BYMONTH"] = ",".join(str(month) for month in self.months)
        if self.month_days:
            parts["BYMONTHDAY"] = ",".join(str(day) for day in self.month_days)
        byday = [_WEEKDAYS[day] for day in self.weekdays or []]
        byday += [f"{nth.nth}{_WEEKDAYS[nth.weekday]}" for nth in self.nth_weekdays or []]
        if byday:
            parts["BYDAY"] = ",".join(byday)
        if self.count is not None:
            parts["COUNT"] = str(self.count)
        if self.until is not None:
            until = (
                _in(zone, self.until)
                if isinstance(self.until, datetime)
                else datetime.combine(self.until, time(23, 59, 59), zone)
            )
            parts["UNTIL"] = until.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if self.week_starts_on:
            parts["WKST"] = _WEEKDAYS[self.week_starts_on]
        rules = ["RRULE:" + ";".join(f"{k}={v}" for k, v in parts.items())]
        for name, times in (("EXDATE", self.skipped), ("RDATE", self.added)):
            if times:
                values = ",".join(_in(zone, moment).strftime("%Y%m%dT%H%M%S") for moment in times)
                rules.append(f"{name};TZID={zone.key}:{values}")
        return rules

    @classmethod
    def from_rules(cls, rules: list[str] | None, zone: tzinfo) -> Repeat | None:
        """A series' rule lines as a Repeat, with its times in `zone` (the
        series'), or `None` if a Repeat can't say them: rules made
        elsewhere, with parts like BYSETPOS, or EXRULE lines."""
        if not rules:
            return None
        try:
            repeat = cls._parse(rules, zone)
            repeat.check()
        except (ValueError, KeyError):  # KeyError: an unknown code or TZID.
            return None
        return repeat

    @classmethod
    def _parse(cls, rules: list[str], zone: tzinfo) -> Repeat:
        rrules = [rule for rule in rules if rule.startswith("RRULE:")]
        if len(rrules) != 1:
            raise ValueError("Not exactly one RRULE")
        times: dict[str, list[datetime]] = {"EXDATE": [], "RDATE": []}
        for rule in rules:
            if rule.startswith("RRULE:"):
                continue
            head, _, values = rule.partition(":")
            name, *params = head.split(";")
            times[name].extend(_parse_times(params, values, zone))
        parts = _rule_parts(rrules[0])
        if set(parts) - _PARTS:
            raise ValueError(f"Unsupported parts: {set(parts) - _PARTS}")
        days = {code: day for day, code in _WEEKDAYS.items()}
        weekdays: list[Weekday] = []
        nth_weekdays: list[NthWeekday] = []
        for token in filter(None, parts.get("BYDAY", "").split(",")):
            match = re.fullmatch(r"([+-]?\d+)?([A-Z]{2})", token)
            if not match:
                raise ValueError(f"Bad BYDAY {token!r}")
            nth, code = match.groups()
            if nth:
                nth_weekdays.append(NthWeekday(nth=int(nth), weekday=days[code]))
            else:
                weekdays.append(days[code])
        return cls(
            every={code: every for every, code in _FREQS.items()}[parts["FREQ"]],
            interval=int(parts.get("INTERVAL") or 1),
            weekdays=weekdays or None,
            nth_weekdays=nth_weekdays or None,
            month_days=_ints(parts.get("BYMONTHDAY")),
            months=_ints(parts.get("BYMONTH")),
            count=int(parts["COUNT"]) if "COUNT" in parts else None,
            until=_parse_until(parts["UNTIL"], zone) if "UNTIL" in parts else None,
            week_starts_on=days[parts["WKST"]] if "WKST" in parts else None,
            skipped=times["EXDATE"] or None,
            added=times["RDATE"] or None,
        )

    def describe(self, zone: tzinfo | None = None) -> str:
        """These in a phrase: "Every week on Mon, Wed, until Dec 31, 2026"."""
        phrase = f"Every {self.every}" if self.interval == 1 else f"Every {self.interval} {self.every}s"
        days = [day.capitalize() for day in self.weekdays or []]
        days += [f"the {_NTHS[nth.nth]} {nth.weekday.capitalize()}" for nth in self.nth_weekdays or []]
        if self.month_days:
            numbered = [str(day) for day in self.month_days if day > 0]
            text = "day " + ", ".join(numbered) if numbered else ""
            if -1 in self.month_days:
                text += (" and " if text else "") + "the last day"
            days.append(text)
        if days:
            phrase += " on " + ", ".join(days)
        if self.months:
            phrase += " in " + ", ".join(_MONTHS[month - 1] for month in self.months)
        if self.count is not None:
            phrase += f", {self.count} times"
        if self.until is not None:
            when = self.until.astimezone(zone) if isinstance(self.until, datetime) else self.until
            phrase += f", until {when:%b} {when.day}, {when.year}"
        if self.skipped:
            phrase += ", with exceptions"
        if self.added:
            phrase += f", plus {len(self.added)} added date" + ("s" if len(self.added) > 1 else "")
        return phrase


def describe_rules(rules: list[str] | None, time_zone: tzinfo | None = None) -> str | None:
    """A series' rules in a phrase -- "Every week on Mon, Wed, until Dec 31,
    2026" -- or the rules themselves, joined, for what a Repeat can't say."""
    if not rules:
        return None
    repeat = Repeat.from_rules(rules, time_zone or timezone.utc)
    return repeat.describe(time_zone) if repeat else "; ".join(rules)


def _in(zone: tzinfo, moment: datetime) -> datetime:
    """`moment` in `zone`; a naive one is taken to be in it already."""
    return moment.replace(tzinfo=zone) if moment.tzinfo is None else moment.astimezone(zone)


def _ints(value: str | None) -> list[int] | None:
    return [int(part) for part in value.split(",")] if value else None


def _parse_until(value: str, zone: tzinfo) -> datetime | date:
    """An RRULE's UNTIL -- UTC ("...Z"), floating (taken to be in the
    series' zone), or a date -- in `zone`."""
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(zone)
    if "T" in value:
        return datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=zone)
    return datetime.strptime(value, "%Y%m%d").date()


def _parse_times(params: list[str], values: str, zone: tzinfo) -> list[datetime]:
    """An EXDATE or RDATE line's times, in `zone`, from its parameters
    (["TZID=America/New_York"]) and values ("20261109T090000,20261116T090000",
    or "20261109T140000Z"). Raises ValueError for dates or periods, which
    a Repeat can't say."""
    given = dict(param.split("=", 1) for param in params)
    if set(given) - {"TZID", "VALUE"} or given.get("VALUE", "DATE-TIME") != "DATE-TIME":
        raise ValueError(f"Unsupported parameters: {params}")
    named = ZoneInfo(given["TZID"]) if "TZID" in given else zone
    return [
        (
            datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            if value.endswith("Z")
            else datetime.strptime(value, "%Y%m%dT%H%M%S").replace(tzinfo=named)
        ).astimezone(zone)
        for value in values.split(",")
    ]

