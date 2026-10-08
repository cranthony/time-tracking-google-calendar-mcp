"""Compaction schedule hints: when the user's scheduled Claude routines
run -- the ones that compact notes into a proposal, and answer the notes
the user leaves on it -- as times of day. They're hints, not a schedule:
nothing here runs anything. A routine sets them (`set_compaction_schedule_
hints`) to say when it runs; the user's app reads them (`get_compaction_
schedule_hints`) and fetches a while after each, so it has what the
routine made to show.

**Where they live.** The **Compaction Schedule** tab of the calendar's
metadata spreadsheet, one row per hint -- `id`, `time` ("HH:MM", 24-hour,
in the calendar's time zone) and an optional `label` ("Morning
compaction") -- read by header name (utilities/row_sheet.py), so it can be
edited by hand. Times are unique; they're listed earliest first.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from calendar_clients.google_sheets import SheetsClient, TabRange
from utilities import calendar_metadata_sheet
from utilities.row_sheet import RowSheet, new_id

MAX_LABEL_LENGTH = 100

_TIME = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


@dataclass(kw_only=True)
class ScheduleHint:
    """One time a routine runs -- see the module docstring."""

    id: str | None = None
    """Short id, assigned when it's first saved."""

    time: str | None = None
    """When it runs, each day: "HH:MM", 24-hour, in the calendar's time
    zone. "7:30" is taken as "07:30"."""

    label: str | None = None
    """What runs then, to say: "Morning compaction"."""


@dataclass(kw_only=True)
class CompactionScheduleHints:
    """The hints, earliest first, and the zone their times are in."""

    hints: list[ScheduleHint]

    time_zone: str | None = None
    """The calendar's time zone (an IANA name), which the hints' times are
    in; None if the calendar has none set (see set_time_zone)."""


def normalized_time(text: str | None) -> str | None:
    """`text` as "HH:MM", if it's a time of day ("7:30", "07:30",
    "23:59"); else None."""
    match = _TIME.match(text or "")
    if not match:
        return None
    hours, minutes = int(match[1]), int(match[2])
    if hours > 23 or minutes > 59:
        return None
    return f"{hours:02d}:{minutes:02d}"


class CompactionSchedule:
    """A calendar's compaction schedule hints -- see the module docstring."""

    def __init__(self, sheet: RowSheet[ScheduleHint]) -> None:
        self._sheet = sheet

    @staticmethod
    def ensure(sheets_client: SheetsClient, spreadsheet_id: str) -> "CompactionSchedule":
        """The calendar's hints, adding the Compaction Schedule tab the
        first time."""
        return CompactionSchedule(
            RowSheet.ensure(
                sheets_client,
                spreadsheet_id,
                role=calendar_metadata_sheet.COMPACTION_SCHEDULE_SHEET_ROLE,
                title=calendar_metadata_sheet.COMPACTION_SCHEDULE_SHEET_TITLE,
                row_type=ScheduleHint,
                required=("id", "time"),
            )
        )

    @property
    def whole_tab(self) -> TabRange:
        return self._sheet.whole_tab

    def prefetch(self, ranges: list[TabRange]) -> None:
        self._sheet.prefetch(ranges)

    def all(self) -> list[ScheduleHint]:
        """Every hint, earliest first; one whose time isn't a time of day
        (a hand edit) last, as it's written."""
        return _in_order(self._sheet.read())

    def set_hints(self, hints: list[ScheduleHint]) -> list[ScheduleHint]:
        """Replace the hints whole with `hints`: each time written as
        "HH:MM", each without an id given one (one given is kept, if it's
        not another's). ValueError, writing nothing, for a time that isn't
        one, the same time twice, or a label too long. Returns them as
        saved."""
        problems = hint_problems(hints)
        if problems:
            raise ValueError("; ".join(problems))
        saved: list[ScheduleHint] = []
        taken: set[str | None] = set()
        for hint in hints:
            hint_id = hint.id if hint.id and hint.id not in taken else new_id(taken | {h.id for h in hints})
            taken.add(hint_id)
            label = hint.label.strip() if hint.label and hint.label.strip() else None
            saved.append(replace(hint, id=hint_id, time=normalized_time(hint.time), label=label))
        saved = _in_order(saved)
        self._sheet.write(saved)
        return saved


def _in_order(hints: list[ScheduleHint]) -> list[ScheduleHint]:
    return sorted(hints, key=lambda h: (normalized_time(h.time) is None, normalized_time(h.time) or h.time or ""))


def hint_problems(hints: list[ScheduleHint]) -> list[str]:
    """What's wrong with `hints`, each in a line; none if they're fine."""
    problems = []
    seen: set[str] = set()
    for hint in hints:
        time = normalized_time(hint.time)
        if time is None:
            problems.append(f"{hint.time!r} isn't a time of day: give it as \"HH:MM\", 24-hour (e.g. \"07:30\")")
            continue
        if time in seen:
            problems.append(f"{time} is given twice: each time is a hint once")
        seen.add(time)
        if hint.label and len(hint.label) > MAX_LABEL_LENGTH:
            problems.append(f"the label at {time} is longer than {MAX_LABEL_LENGTH} characters")
    return problems
