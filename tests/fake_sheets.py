"""A tiny in-memory stand-in for `SheetsClient`, for tests that need real
read-after-write behavior across several tabs (a notes tab and a journal
tab, say) instead of a MagicMock with canned return values.

Supports the subset of A1 notation this app uses: "A2:H" (open-ended,
from a row down), "C3:C3"/"C2:C4" (a bounded block), and "A1:C1" (a row).
Like the real API, a read trims trailing empty cells from each row and
returns blank rows in the middle as `[]`; each tab has a grid of
`row_counts[sheet_id]` rows (1000, like a new tab, unless set), which
deleting rows shrinks and `ensure_row_count` grows; and a write past the end of the grid fails.
"""

from __future__ import annotations

import re

_RANGE = re.compile(r"^([A-Z]+)(\d+):([A-Z]+)(\d*)$")


def _column_number(letters: str) -> int:
    number = 0
    for letter in letters:
        number = number * 26 + (ord(letter) - ord("A") + 1)
    return number


class FakeSheets:
    DEFAULT_ROW_COUNT = 1000

    def __init__(self) -> None:
        self.cells: dict[int, dict[tuple[int, int], str]] = {}
        self.writes: list[tuple[int, str]] = []
        self.row_counts: dict[int, int] = {}

    def _tab(self, sheet_id: int) -> dict[tuple[int, int], str]:
        return self.cells.setdefault(sheet_id, {})

    def row_count(self, sheet_id: int) -> int:
        return self.row_counts.setdefault(sheet_id, self.DEFAULT_ROW_COUNT)

    @staticmethod
    def _parse(rng: str) -> tuple[int, int, int, int | None]:
        match = _RANGE.match(rng)
        if match is None:
            raise ValueError(f"FakeSheets doesn't understand range {rng!r}")
        first_col, first_row, last_col, last_row = match.groups()
        return (
            _column_number(first_col),
            int(first_row),
            _column_number(last_col),
            int(last_row) if last_row else None,
        )

    def read_rows_in_sheet(self, spreadsheet_id: str, sheet_id: int, rng: str) -> list[list[str]]:
        first_col, first_row, last_col, last_row = self._parse(rng)
        tab = self._tab(sheet_id)
        populated = [r for (r, c) in tab if r >= first_row and first_col <= c <= last_col]
        if not populated:
            return []
        end_row = min(max(populated), last_row) if last_row else max(populated)
        rows = []
        for row in range(first_row, end_row + 1):
            cells = [tab.get((row, col), "") for col in range(first_col, last_col + 1)]
            while cells and cells[-1] == "":
                cells.pop()
            rows.append(cells)
        return rows

    def write_rows_in_sheet(
        self, spreadsheet_id: str, sheet_id: int, rng: str, rows: list[list[str]]
    ) -> None:
        first_col, first_row, _last_col, _last_row = self._parse(rng)
        last_written = first_row + len(rows) - 1
        if last_written > self.row_count(sheet_id):
            raise ValueError(
                f"Range ({rng}) exceeds grid limits: row {last_written} of a "
                f"{self.row_count(sheet_id)}-row tab"
            )
        self.writes.append((sheet_id, rng))
        tab = self._tab(sheet_id)
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                tab[(first_row + i, first_col + j)] = value

    def delete_rows(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        *,
        start_row: int,
        end_row: int,
        keep_at_least: int = 0,
    ) -> None:
        count = end_row - start_row + 1
        self.row_counts[sheet_id] = max(self.row_count(sheet_id) - count, keep_at_least)
        tab = self._tab(sheet_id)
        shifted = {}
        for (r, c), v in tab.items():
            if r < start_row:
                shifted[(r, c)] = v
            elif r > end_row:
                shifted[(r - count, c)] = v
            # rows within [start_row, end_row] are dropped
        self.cells[sheet_id] = shifted

    def ensure_row_count(self, spreadsheet_id: str, sheet_id: int, at_least: int) -> None:
        self.row_counts[sheet_id] = max(self.row_count(sheet_id), at_least)

    def cell(self, sheet_id: int, address: str) -> str:
        match = re.match(r"^([A-Z]+)(\d+)$", address)
        return self._tab(sheet_id).get((int(match.group(2)), _column_number(match.group(1))), "")
