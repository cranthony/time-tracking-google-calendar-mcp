"""A tiny in-memory stand-in for `SheetsClient`, for tests that need real
read-after-write behavior across several tabs (a notes tab and a journal
tab, say) instead of a MagicMock with canned return values.

Supports the subset of A1 notation this app uses: "A2:H" (open-ended,
from a row down), "C3:C3"/"C2:C4" (a bounded block), and "A1:C1" (a row).
Like the real API, a read trims trailing empty cells from each row and
returns blank rows in the middle as `[]`.
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
    def __init__(self) -> None:
        self.cells: dict[int, dict[tuple[int, int], str]] = {}
        self.writes: list[tuple[int, str]] = []

    def _tab(self, sheet_id: int) -> dict[tuple[int, int], str]:
        return self.cells.setdefault(sheet_id, {})

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
        self.writes.append((sheet_id, rng))
        tab = self._tab(sheet_id)
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                tab[(first_row + i, first_col + j)] = value

    def cell(self, sheet_id: int, address: str) -> str:
        match = re.match(r"^([A-Z]+)(\d+)$", address)
        return self._tab(sheet_id).get((int(match.group(2)), _column_number(match.group(1))), "")
