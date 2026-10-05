"""Goal descriptions: the **Goal Details** tab of the calendar's metadata
spreadsheet (see utilities/calendar_metadata_sheet.py), one row per goal
that has one -- `goal_id | description` -- kept out of the goals tab,
which every event tool call reads, since descriptions can be long and are
only needed on demand. See docs/goals-design.md section 3.2.

A description is Markdown. A Sheets cell holds at most 50,000
characters, so a longer description continues in the next cells of its
row (`C`, `D`, ...) and is joined on read.

**What matters to them.** A person goal's description has a section,
under a `## What matters to them` heading, of facts, upcoming moments
and preferences, each a dated bullet (`- 2026-10-05: starts a new job in
November`). Compaction and reflection add to it when notes reveal
something (`add_to_section`, which skips a line that's already there, so
adding again is harmless), and it can be edited by hand. Compaction is
shown it, to judge thoughtfulness against.
"""

from __future__ import annotations

import re
from datetime import date

from calendar_clients.google_sheets import SheetsClient, TabRange
from utilities import calendar_metadata_sheet

WHAT_MATTERS = "What matters to them"
"""The heading of a person goal's section of what matters to them."""

MAX_CELL_CHARS = 50_000
"""Sheets' limit on one cell."""

MAX_DESCRIPTION_CHARS = 20 * MAX_CELL_CHARS
"""How long a description may be: 20 cells' worth (the tab is read
through column V)."""

_HEADER = ["goal_id", "description"]
_HEADER_RANGE = "A1:B1"
_DATA_RANGE = "A2:V"
_WHOLE_RANGE = "A1:V"


class GoalDetails:
    """The Goal Details tab -- see the module docstring."""

    def __init__(self, sheets_client: SheetsClient, spreadsheet_id: str, sheet_id: int) -> None:
        self._sheets_client = sheets_client
        self._spreadsheet_id = spreadsheet_id
        self._sheet_id = sheet_id

    @staticmethod
    def ensure(sheets_client: SheetsClient, spreadsheet_id: str) -> "GoalDetails":
        """The calendar's Goal Details tab, created (with its header row)
        the first time only."""
        sheet_id, created = calendar_metadata_sheet.ensure_tab(
            sheets_client,
            spreadsheet_id,
            role=calendar_metadata_sheet.GOAL_DETAILS_SHEET_ROLE,
            title=calendar_metadata_sheet.GOAL_DETAILS_SHEET_TITLE,
        )
        if created:
            sheets_client.write_rows_in_sheet(spreadsheet_id, sheet_id, _HEADER_RANGE, [_HEADER])
        return GoalDetails(sheets_client, spreadsheet_id, sheet_id)

    @property
    def whole_tab(self) -> TabRange:
        return TabRange(self._spreadsheet_id, self._sheet_id, _WHOLE_RANGE)

    def get(self, goal_id: str) -> str | None:
        """`goal_id`'s description, or `None` if it has none."""
        for row in self._rows():
            if row and row[0].strip() == goal_id:
                return "".join(row[1:]) or None
        return None

    def set(self, goal_id: str, description: str | None) -> None:
        """Replace `goal_id`'s description whole (`None` or "" removes
        it)."""
        description = description or ""
        if len(description) > MAX_DESCRIPTION_CHARS:
            raise ValueError(f"A description can be at most {MAX_DESCRIPTION_CHARS} characters")
        rows = self._rows()
        cells = [description[i : i + MAX_CELL_CHARS] for i in range(0, len(description), MAX_CELL_CHARS)]
        index = next((i for i, row in enumerate(rows) if row and row[0].strip() == goal_id), None)
        if index is None:
            if not cells:
                return
            # The first blank row, or a new one below the rest.
            index = next((i for i, row in enumerate(rows) if not any(c.strip() for c in row)), len(rows))
        row = [goal_id, *cells] if cells else []
        # Blank whatever cells a longer description used before.
        width = max(len(rows[index]) if index < len(rows) else 0, len(row), 2)
        row += [""] * (width - len(row))
        address = f"A{index + 2}:{_column(width)}{index + 2}"
        self._sheets_client.write_rows_in_sheet(self._spreadsheet_id, self._sheet_id, address, [row])

    def _rows(self) -> list[list[str]]:
        return self._sheets_client.read_rows_in_sheet(self._spreadsheet_id, self._sheet_id, _DATA_RANGE)


def _column(number: int) -> str:
    letters = ""
    while number:
        number, rest = divmod(number - 1, 26)
        letters = chr(ord("A") + rest) + letters
    return letters


_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def section(description: str | None, heading: str = WHAT_MATTERS) -> str | None:
    """The text under `heading` (any level, matched ignoring case) in a
    Markdown `description`, up to the next heading of the same level or
    higher; `None` if there's no such heading."""
    lines = (description or "").splitlines()
    bounds = _bounds(lines, heading)
    if bounds is None:
        return None
    start, end = bounds
    return "\n".join(lines[start + 1 : end]).strip()


def add_to_section(description: str | None, items: list[str], on: date, heading: str = WHAT_MATTERS) -> str:
    """`description` with each of `items` added to the end of its
    `heading` section as a bullet dated `on` -- the section added at the
    end, as a level-2 heading, if it has none. An item whose text is in
    the section already is skipped."""
    lines = (description or "").rstrip().splitlines()
    bounds = _bounds(lines, heading)
    if bounds is None:
        lines += ([""] if lines else []) + [f"## {heading}"]
        bounds = (len(lines) - 1, len(lines))
    start, end = bounds
    existing = "\n".join(lines[start + 1 : end])
    added = [f"- {on.isoformat()}: {item.strip()}" for item in items if item.strip() and item.strip() not in existing]
    if not added:
        return description or ""
    # After the section's last non-blank line.
    last = end
    while last > start + 1 and not lines[last - 1].strip():
        last -= 1
    # A blank line between a heading and the section's first bullet.
    lines[last:last] = ([""] if last == start + 1 else []) + added
    return "\n".join(lines).strip() + "\n"


def _bounds(lines: list[str], heading: str) -> tuple[int, int] | None:
    """(the heading's line, the line after its section) for `heading` in
    `lines`, or `None`."""
    for i, line in enumerate(lines):
        match = _HEADING.match(line)
        if match and match.group(2).casefold() == heading.casefold():
            level = len(match.group(1))
            for j in range(i + 1, len(lines)):
                other = _HEADING.match(lines[j])
                if other and len(other.group(1)) <= level:
                    return i, j
            return i, len(lines)
    return None
