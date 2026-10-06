"""A tab of the calendar's metadata spreadsheet (see
utilities/calendar_metadata_sheet.py) holding one dataclass per row: the
actions, action groups, people, circles and locations tabs.

Columns are read by header
name, so a column a user adds by hand never shifts the others, and its
cells are kept as they were on every write. A field the tab has no column
for yet gets one the first time a row has a value for it. Each field's
cell is its text: a number as digits, a list or object as JSON, and
nothing for `None`.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Collection
from dataclasses import fields, replace
from typing import Any, Generic, TypeVar

from calendar_clients.google_sheets import SheetsClient, TabRange
from utilities import calendar_metadata_sheet

_HEADER_RANGE = "A1:Z1"
_DATA_RANGE = "A2:Z"
_WHOLE_RANGE = "A1:Z"

Row = TypeVar("Row")


_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_ID_LENGTH = 6


def new_id(taken: Collection[str | None]) -> str:
    """A short random id (e.g. "a7k2qp") not in `taken`."""
    while True:
        candidate = "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))
        if candidate not in taken:
            return candidate


def check_clear(item: object, clear_fields: Collection[str], clearable: Collection[str]) -> None:
    """Refuse an update's `clear_fields` that can't be cleared, or that
    `item` also sets."""
    unknown = set(clear_fields) - set(clearable)
    if unknown:
        raise ValueError(f"Can't clear {sorted(unknown)}; clearable fields are {sorted(clearable)}")
    both = sorted(name for name in clear_fields if getattr(item, name) is not None)
    if both:
        raise ValueError(f"Can't both set and clear {both}")


def updated(current: Row, given: Row, clear_fields: Collection[str], read_only: Collection[str] = ("id",)) -> Row:
    """`current` with whichever of `given`'s fields are set (but the
    read-only ones), and `clear_fields` blanked."""
    return replace(
        current,
        **{
            f.name: getattr(given, f.name)
            for f in fields(given)
            if f.name not in read_only and getattr(given, f.name) is not None
        },
        **{name: None for name in clear_fields},
    )


def _kind(annotation: Any) -> str:
    """How a field with this (string) annotation is kept in a cell:
    "int", "json" or "str"."""
    text = str(annotation)
    if text.startswith(("list", "dict")):
        return "json"
    if text.startswith("int"):
        return "int"
    return "str"


class RowSheet(Generic[Row]):
    """Reads and writes one tab of `row_type` rows -- a keyword-only
    dataclass whose fields are the tab's columns, in order."""

    def __init__(
        self,
        sheets_client: SheetsClient,
        spreadsheet_id: str,
        sheet_id: int,
        row_type: type[Row],
        required: tuple[str, ...] = ("id", "name"),
    ) -> None:
        """`required`: the columns the tab's header must have."""
        self._required = required
        self._sheets_client = sheets_client
        self._spreadsheet_id = spreadsheet_id
        self._sheet_id = sheet_id
        self._row_type = row_type
        self._kinds = {f.name: _kind(f.type) for f in fields(row_type)}
        self.header_row = [f.name for f in fields(row_type)]

    @classmethod
    def ensure(
        cls,
        sheets_client: SheetsClient,
        spreadsheet_id: str,
        *,
        role: str,
        title: str,
        row_type: type[Row],
        required: tuple[str, ...] = ("id", "name"),
    ) -> "RowSheet[Row]":
        """The tab tagged `role`, adding it (with just its header row) the
        first time."""
        header_row = [f.name for f in fields(row_type)]
        sheet_id = calendar_metadata_sheet.find_tab(sheets_client, spreadsheet_id, role)
        if sheet_id is None:
            sheet_id = calendar_metadata_sheet.create_tab(
                sheets_client,
                spreadsheet_id,
                role=role,
                title=title,
                populate=lambda new_id: sheets_client.write_rows_in_sheet(
                    spreadsheet_id, new_id, _HEADER_RANGE, [header_row]
                ),
            )
        return cls(sheets_client, spreadsheet_id, sheet_id, row_type, required)

    @property
    def spreadsheet_id(self) -> str:
        return self._spreadsheet_id

    @property
    def whole_tab(self) -> TabRange:
        """This whole tab, for `SheetsClient.prefetch`."""
        return TabRange(self._spreadsheet_id, self._sheet_id, _WHOLE_RANGE)

    def prefetch(self, ranges: list[TabRange]) -> None:
        self._sheets_client.prefetch(ranges)

    def read(self) -> list[Row]:
        """Every row, in sheet order; blank rows are skipped. A cell that
        can't be read as its field's kind (a hand edit) is kept as text,
        for the caller's checks to report."""
        header_row, rows = self._read()
        return [self._from_row(header_row, row) for row in rows if any(cell.strip() for cell in row)]

    def write(self, items: list[Row]) -> None:
        """Overwrite the data rows with `items`, keeping unknown columns'
        cells, and blanking rows left over from a longer list."""
        original_header, previous = self._read()
        header_row = original_header + [
            name for name in self.header_row
            if name not in original_header and any(getattr(item, name) is not None for item in items)
        ]
        by_id = {self._from_row(header_row, row).id: row for row in previous if any(c.strip() for c in row)}
        rows = [self._to_row(header_row, item, by_id.get(item.id)) for item in items]
        rows += [[""] * len(header_row)] * max(0, len(previous) - len(items))
        if header_row == original_header:
            self._sheets_client.write_rows_in_sheet(self._spreadsheet_id, self._sheet_id, _DATA_RANGE, rows)
        else:
            self._sheets_client.write_rows_in_sheet(
                self._spreadsheet_id, self._sheet_id, _WHOLE_RANGE, [header_row, *rows]
            )

    def _read(self) -> tuple[list[str], list[list[str]]]:
        header, rows = self._sheets_client.read_ranges_in_sheet(
            self._spreadsheet_id, self._sheet_id, [_HEADER_RANGE, _DATA_RANGE]
        )
        header_row = [cell.strip() for cell in header[0]] if header else []
        missing = [name for name in self._required if name not in header_row]
        if missing:
            raise ValueError(f"The {self._row_type.__name__} tab's header row is missing columns: {missing}")
        return header_row, rows

    def _from_row(self, header_row: list[str], data: list[str]) -> Row:
        values: dict[str, Any] = {}
        for i, header in enumerate(header_row):
            if header not in self._kinds:
                continue
            text = (data[i] if i < len(data) else "").strip()
            if not text:
                continue
            kind = self._kinds[header]
            try:
                values[header] = int(text) if kind == "int" else json.loads(text) if kind == "json" else text
            except ValueError:
                values[header] = text
        return self._row_type(**values)

    def _to_row(self, header_row: list[str], item: Row, original: list[str] | None) -> list[str]:
        row = []
        for i, header in enumerate(header_row):
            if header in self._kinds:
                value = getattr(item, header)
                row.append(
                    "" if value is None
                    else value if isinstance(value, str)
                    else json.dumps(value, separators=(",", ":")) if isinstance(value, (list, dict))
                    else str(value)
                )
            else:
                row.append(original[i] if original is not None and i < len(original) else "")
        return row
