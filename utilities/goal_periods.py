"""The periods a goal is assessed over, one per cadence (see
docs/goals-design.md sections 6 and 11.1): a day, a Sunday-Saturday
week, a calendar month, or an aligned pair of months (Jan-Feb, Mar-Apr,
..., Nov-Dec). Pure date arithmetic, no API access.

Each period has an id, distinct in format per cadence:

| cadence          | id                | covers                          |
| ---------------- | ----------------- | ------------------------------- |
| `daily`          | `2026-09-30`      | that day                        |
| `weekly`         | `week-2026-09-27` | the week starting that Sunday   |
| `monthly`        | `2026-09`         | that month                      |
| `every_2_months` | `2026-09..10`     | those two months                |
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

_WEEK = re.compile(r"^week-(\d{4}-\d{2}-\d{2})$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_TWO_MONTHS = re.compile(r"^(\d{4})-(\d{2})\.\.(\d{2})$")


@dataclass(frozen=True)
class Period:
    cadence: str
    id: str
    start: date
    end: date
    """Exclusive: the first day after the period."""

    def contains(self, day: date) -> bool:
        return self.start <= day < self.end

    def previous(self) -> "Period":
        return period_containing(self.cadence, self.start - timedelta(days=1))

    def next(self) -> "Period":
        return period_containing(self.cadence, self.end)


def period_containing(cadence: str, day: date) -> Period:
    """The `cadence` period `day` falls in."""
    if cadence == "daily":
        return Period(cadence, day.isoformat(), day, day + timedelta(days=1))
    if cadence == "weekly":
        sunday = day - timedelta(days=(day.weekday() + 1) % 7)
        return Period(cadence, f"week-{sunday.isoformat()}", sunday, sunday + timedelta(days=7))
    if cadence == "monthly":
        start = day.replace(day=1)
        return Period(cadence, f"{start.year:04d}-{start.month:02d}", start, _add_months(start, 1))
    if cadence == "every_2_months":
        first_month = day.month - (day.month - 1) % 2  # Jan, Mar, May, ...
        start = date(day.year, first_month, 1)
        return Period(
            cadence, f"{start.year:04d}-{first_month:02d}..{first_month + 1:02d}", start, _add_months(start, 2)
        )
    raise ValueError(f"Unknown cadence {cadence!r}")


def parse_period(cadence: str, period_id: str) -> Period:
    """The `cadence` period with id `period_id`; ValueError if it isn't
    one, saying what one looks like."""
    try:
        if cadence == "daily":
            start = date.fromisoformat(period_id)
        elif cadence == "weekly":
            match = _WEEK.match(period_id)
            start = date.fromisoformat(match.group(1)) if match else None
            if start is not None and start.weekday() != 6:
                raise ValueError(f"{period_id!r} doesn't start on a Sunday")
        elif cadence == "monthly":
            match = _MONTH.match(period_id)
            start = date(int(match.group(1)), int(match.group(2)), 1) if match else None
        elif cadence == "every_2_months":
            match = _TWO_MONTHS.match(period_id)
            start = None
            if match:
                year, first, second = (int(g) for g in match.groups())
                if first % 2 == 1 and second == first + 1:
                    start = date(year, first, 1)
        else:
            raise ValueError(f"Unknown cadence {cadence!r}")
    except ValueError as exc:
        if "cadence" in str(exc) or "Sunday" in str(exc):
            raise
        start = None
    if start is None:
        example = period_containing(cadence, date(2026, 9, 30)).id
        raise ValueError(f"{period_id!r} isn't a period for the {cadence} cadence; those look like {example!r}")
    period = period_containing(cadence, start)
    assert period.id == period_id, (period, period_id)
    return period


def last_ended(cadence: str, today: date) -> Period:
    """The most recent `cadence` period that has fully ended by `today`."""
    return period_containing(cadence, today).previous()


def _add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    return date(start.year + month_index // 12, month_index % 12 + 1, 1)
