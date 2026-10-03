"""Manages the tab a calendar's goals live in, within its shared calendar
metadata spreadsheet (see utilities/calendar_metadata_sheet.py).

Like utilities/event_label_sheet.py, which it replaces: this module knows
the tab's shape (header row, which columns hold what, how each value is
written as a cell) and how to read and write whole rows of it, but
nothing about what a goal means for the calendar -- its label, the
hierarchy's inherited values, syncing -- which is utilities/goals.py's
job, one layer up. See docs/goals-design.md section 3.

Columns are read by header name, so a column added later (or one a user
adds by hand) never shifts the others, and unknown columns' cells are
kept as they were on every write.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, fields
from datetime import date
from typing import Any, Literal

from calendar_clients.google_sheets import SheetsClient
from utilities import calendar_metadata_sheet

GoalStatus = Literal["proposed", "active", "inactive", "completed", "archived", "deleted"]
GOAL_STATUSES: tuple[str, ...] = ("proposed", "active", "inactive", "completed", "archived", "deleted")
"""Where a goal stands:

- `proposed`: suggested, but not taken on yet.
- `active`: being worked on. The only status that holds a calendar label
  and is assessed.
- `inactive`: paused, perhaps to pick up again.
- `completed`: achieved.
- `archived`: no longer relevant, and kept out of the way.
- `deleted`: shouldn't have existed. Kept only so that history and events
  pointing at it still make sense; no event can be given it again.

Every status but `active` frees the goal's label and keeps its history."""

_HEADER_RANGE = "A1:Z1"
_DATA_RANGE = "A2:Z"
"""Wide enough for every column below plus some a user (or a later
version) adds; columns are matched by header, not position."""

_NARROW_COLUMNS = ("id", "label_id")
_NARROW_PIXEL_WIDTH = 60
"""Ids nobody's expected to read -- just to leave alone."""


@dataclass(kw_only=True)
class Goal:
    """One goal: something the user is working toward. Goals form a tree
    through `parent_id`. See docs/goals-design.md section 3.1."""

    id: str | None = None
    """Immutable short id (e.g. "g7k2qp"), assigned on creation."""

    parent_id: str | None = None
    """The goal this one is a sub-goal of; `None` for a top-level goal."""

    name: str | None = None
    """Short name, at most 50 characters -- also its calendar label's name."""

    status: GoalStatus | None = None
    """Where it stands -- see GOAL_STATUSES. Only `active` goals take up
    one of the calendar's event label slots."""

    background_color: str | None = None
    """Hex color for its label; derived from its priority when unset."""

    priority: int | None = None
    """Inherited by events (and sub-goals) that don't set their own."""

    fixed_time: bool | None = None
    """Inherited by events (and sub-goals) that don't set their own."""

    measure: dict[str, Any] | None = None
    """How its health is rated in each day's reflection: {"kind": ...,
    ...}, with the fields its kind takes -- see utilities/goal_measures.py.
    Every active goal is reflected on daily; one without a measure is
    rated by the mean of its sub-goals' ratings, if it has any."""

    target: str | None = None
    """Free-text target, e.g. "300 min/week"."""

    deadline: date | None = None

    note: str | None = None
    """Short free text, e.g. what it's for."""

    label_id: str | None = None
    """Read-only: the calendar label id reserved for this goal, kept even
    while it's inactive so reactivating restores the same label."""

    created: date | None = None
    """Read-only: when it was created."""

    health: int | None = None
    """Read-only cache: its latest confirmed daily rating (0-100) -- see
    utilities/goal_health.py, which keeps these three up to date."""

    health_period: str | None = None
    """Read-only cache: the day `health` (or a skip) covers, e.g.
    "2026-09-30"."""

    health_trend: str | None = None
    """Read-only cache: its last 8 days' confirmed ratings, oldest first
    and comma-separated, "-" for a day with none."""

    @property
    def active(self) -> bool:
        return self.status == "active"

    @classmethod
    def from_row(cls, header_row: list[str], data: list[str]) -> "Goal":
        cells = {header: (data[i] if i < len(data) else "") for i, header in enumerate(header_row)}

        def text(name: str) -> str | None:
            return cells.get(name, "").strip() or None

        def boolean(name: str) -> bool | None:
            value = text(name)
            return value.lower() == "true" if value is not None else None

        def day(name: str) -> date | None:
            value = text(name)
            return date.fromisoformat(value) if value is not None else None

        priority, measure, health = text("priority"), text("measure"), text("health")
        status = (text("status") or "").lower() or None
        if status is None and boolean("active") is not None:
            # A tab from before statuses: just TRUE/FALSE.
            status = "active" if boolean("active") else "inactive"
        return cls(
            id=text("id"),
            parent_id=text("parent_id"),
            name=text("name"),
            status=status,
            background_color=text("background_color"),
            priority=int(priority) if priority is not None else None,
            fixed_time=boolean("fixed_time"),
            measure=json.loads(measure) if measure is not None else None,
            target=text("target"),
            deadline=day("deadline"),
            note=text("note"),
            label_id=text("label_id"),
            created=day("created"),
            health=int(health) if health is not None else None,
            health_period=text("health_period"),
            health_trend=text("health_trend"),
        )

    def to_row(self, header_row: list[str], original_row: list[str] | None = None) -> list[str]:
        field_names = {f.name for f in fields(self)}
        row = []
        for i, header in enumerate(header_row):
            if header in field_names:
                row.append(_cell(getattr(self, header)))
            elif header == "active":
                # Kept in step on a tab from before statuses.
                row.append(_cell(self.active) if self.status is not None else "")
            else:
                row.append(original_row[i] if original_row is not None and i < len(original_row) else "")
        return row


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"))
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


HEADER_ROW = [
    "id",
    "parent_id",
    "name",
    "status",
    "label_id",
    "background_color",
    "priority",
    "fixed_time",
    "measure",
    "target",
    "deadline",
    "created",
    "note",
    "health",
    "health_period",
    "health_trend",
]
"""A new goals tab's columns, in order -- every `Goal` field."""

assert set(HEADER_ROW) == {f.name for f in fields(Goal)}


class GoalSheet:
    """Reads and writes the goals tab of a calendar's metadata spreadsheet."""

    def __init__(self, sheets_client: SheetsClient, spreadsheet_id: str, sheet_id: int) -> None:
        self._sheets_client = sheets_client
        self._spreadsheet_id = spreadsheet_id
        self._sheet_id = sheet_id

    @property
    def spreadsheet_id(self) -> str:
        return self._spreadsheet_id

    @staticmethod
    def find(sheets_client: SheetsClient, spreadsheet_id: str) -> "GoalSheet | None":
        """The calendar's goals tab, or `None` if it has none yet."""
        sheet_id = calendar_metadata_sheet.find_tab(
            sheets_client, spreadsheet_id, calendar_metadata_sheet.GOALS_SHEET_ROLE
        )
        return GoalSheet(sheets_client, spreadsheet_id, sheet_id) if sheet_id is not None else None

    @staticmethod
    def create(
        sheets_client: SheetsClient,
        spreadsheet_id: str,
        goals: Iterable[Goal],
        *,
        reuse_sheet_id: int | None = None,
    ) -> "GoalSheet":
        """A new goals tab holding `goals`, tagged only once they're
        written (see `calendar_metadata_sheet.create_tab`)."""
        goals = list(goals)

        def populate(sheet_id: int) -> None:
            for column in _NARROW_COLUMNS:
                sheets_client.set_column_width(
                    spreadsheet_id,
                    sheet_id=sheet_id,
                    column_index=HEADER_ROW.index(column),
                    pixel_width=_NARROW_PIXEL_WIDTH,
                )
            sheets_client.write_rows_in_sheet(spreadsheet_id, sheet_id, _HEADER_RANGE, [HEADER_ROW])
            if goals:
                sheets_client.write_rows_in_sheet(
                    spreadsheet_id, sheet_id, _DATA_RANGE, [goal.to_row(HEADER_ROW) for goal in goals]
                )

        sheet_id = calendar_metadata_sheet.create_tab(
            sheets_client,
            spreadsheet_id,
            role=calendar_metadata_sheet.GOALS_SHEET_ROLE,
            title=calendar_metadata_sheet.GOALS_SHEET_TITLE,
            populate=populate,
            reuse_sheet_id=reuse_sheet_id,
        )
        return GoalSheet(sheets_client, spreadsheet_id, sheet_id)

    def read(self) -> list[Goal]:
        """Every goal row, in sheet order; blank rows are skipped."""
        header_row, rows = self._read_header_and_data()
        return [Goal.from_row(header_row, row) for row in rows if any(cell.strip() for cell in row)]

    def write(self, goals: list[Goal]) -> None:
        """Overwrite the data rows with `goals`, keeping any unknown
        columns' cells, and blanking rows left over from a longer list."""
        header_row, previous = self._read_header_and_data()
        header_row = self._with_columns_for(goals, header_row)
        by_id = {
            Goal.from_row(header_row, row).id: row for row in previous if any(cell.strip() for cell in row)
        }
        rows = [goal.to_row(header_row, by_id.get(goal.id)) for goal in goals]
        # Blank out whatever's left below (including rows a user blanked).
        rows += [[""] * len(header_row)] * max(0, len(previous) - len(goals))
        self._sheets_client.write_rows_in_sheet(self._spreadsheet_id, self._sheet_id, _DATA_RANGE, rows)

    def _with_columns_for(self, goals: list[Goal], header_row: list[str]) -> list[str]:
        """`header_row`, plus a column for any field one of `goals` has a
        value for but the tab has no column for yet (e.g. the health cache
        on a tab made before it existed), added to the tab itself."""
        missing = [
            name for name in HEADER_ROW
            if name not in header_row and any(getattr(goal, name) is not None for goal in goals)
        ]
        if not missing:
            return header_row
        header_row = header_row + missing
        self._sheets_client.write_rows_in_sheet(self._spreadsheet_id, self._sheet_id, _HEADER_RANGE, [header_row])
        return header_row

    def _read_header_and_data(self) -> tuple[list[str], list[list[str]]]:
        """The header row and the data rows, in one read request."""
        header, rows = self._sheets_client.read_ranges_in_sheet(
            self._spreadsheet_id, self._sheet_id, [_HEADER_RANGE, _DATA_RANGE]
        )
        return self._checked_header(header), rows

    @staticmethod
    def _checked_header(rows: list[list[str]]) -> list[str]:
        header_row = [cell.strip() for cell in rows[0]] if rows else []
        missing = [column for column in ("id", "name", "label_id") if column not in header_row]
        if "status" not in header_row and "active" not in header_row:
            missing.append("status")
        if missing:
            raise ValueError(f"The goals tab's header row ({_HEADER_RANGE}) is missing columns: {missing}")
        return header_row
