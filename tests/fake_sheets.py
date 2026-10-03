"""A tiny in-memory stand-in for `SheetsClient`, for tests that need real
read-after-write behavior across several tabs (a notes tab and a journal
tab, say) instead of a MagicMock with canned return values.

Supports the subset of A1 notation this app uses: "A2:H" (open-ended,
from a row down), "C3:C3"/"C2:C4" (a bounded block), and "A1:C1" (a row).
Like the real API, a read trims trailing empty cells from each row and
returns blank rows in the middle as `[]`; each tab has a grid of
`row_counts[sheet_id]` rows (1000, like a new tab, unless set), which
deleting rows shrinks; and a write past the end of the grid fails.
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
        self.tags: dict[tuple[str, str], int] = {}
        self.titles: dict[int, str] = {}

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

    def read_ranges_in_sheet(self, spreadsheet_id: str, sheet_id: int, ranges: list[str]) -> list[list[list[str]]]:
        return [self.read_rows_in_sheet(spreadsheet_id, sheet_id, rng) for rng in ranges]

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

    def cell(self, sheet_id: int, address: str) -> str:
        match = re.match(r"^([A-Z]+)(\d+)$", address)
        return self._tab(sheet_id).get((int(match.group(2)), _column_number(match.group(1))), "")

    # Tab management, for code that finds/creates tabs itself (e.g.
    # utilities/goals.py's migration). Tabs are tagged with developer
    # metadata the way calendar_metadata_sheet does it.

    def find_sheet_id(self, spreadsheet_id: str, key: str, value: str) -> int | None:
        return next((sheet_id for (k, v), sheet_id in self.tags.items() if (k, v) == (key, value)), None)

    def create_sheet_metadata(self, spreadsheet_id: str, sheet_id: int, key: str, value: str) -> None:
        self.tags[(key, value)] = sheet_id

    def add_sheet(self, spreadsheet_id: str, title: str, *, tab_color=None) -> int:
        sheet_id = max([0, *self.titles, *self.cells]) + 1
        self.titles[sheet_id] = title
        return sheet_id

    def update_sheet_properties(self, spreadsheet_id: str, sheet_id: int, *, title=None, tab_color=None) -> None:
        if title is not None:
            self.titles[sheet_id] = title

    def set_column_width(self, spreadsheet_id: str, *, sheet_id: int, column_index: int, pixel_width: int) -> None:
        pass

    def create_spreadsheet(self, title: str) -> str:
        return "new-spreadsheet"


def _column_letters(number: int) -> str:
    letters = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


class FakeSheetsService:
    """A stand-in for the Google Sheets API *service* that a real
    `SheetsClient` wraps, backed by a `FakeSheets` -- for tests that run
    the production client end to end (its per-tool-call read cache, see
    `cached_sheet_reads`, included) and count the requests that would
    actually reach Google. Supports the requests `SheetsClient` sends for
    reading and writing rows by sheetId (`values.batchGetByDataFilter`/
    `batchUpdateByDataFilter`), finding and tagging tabs
    (`developerMetadata.search`, and `batchUpdate`'s `addSheet`,
    `createDeveloperMetadata`, `updateSheetProperties`), sizing columns
    and deleting rows.

    `read_requests` lists each read request, in order: a row read as its
    tab and A1 range, a tab lookup as `(None, "developerMetadata.search")`."""

    def __init__(self, sheets: FakeSheets) -> None:
        self.sheets = sheets
        self.read_requests: list[tuple[int | None, str]] = []

    def spreadsheets(self):
        return self

    def values(self):
        return self

    def developerMetadata(self):
        return self

    def search(self, *, spreadsheetId, body):
        (data_filter,) = body["dataFilters"]
        lookup = data_filter["developerMetadataLookup"]

        def execute():
            self.read_requests.append((None, "developerMetadata.search"))
            sheet_id = self.sheets.find_sheet_id(spreadsheetId, lookup["metadataKey"], lookup["metadataValue"])
            if sheet_id is None:
                return {}
            return {"matchedDeveloperMetadata": [{"developerMetadata": {"location": {"sheetId": sheet_id}}}]}

        return _Request(execute)

    def batchUpdate(self, *, spreadsheetId, body, **_kwargs):
        def execute():
            replies = []
            for request in body["requests"]:
                (kind, detail), = request.items()
                if kind == "addSheet":
                    sheet_id = self.sheets.add_sheet(spreadsheetId, detail["properties"]["title"])
                    replies.append({"addSheet": {"properties": {"sheetId": sheet_id}}})
                    continue
                if kind == "createDeveloperMetadata":
                    metadata = detail["developerMetadata"]
                    self.sheets.create_sheet_metadata(
                        spreadsheetId, metadata["location"]["sheetId"], metadata["metadataKey"], metadata["metadataValue"]
                    )
                elif kind == "updateSheetProperties":
                    properties = detail["properties"]
                    self.sheets.update_sheet_properties(spreadsheetId, properties["sheetId"], title=properties.get("title"))
                elif kind == "deleteDimension":
                    grid = detail["range"]
                    self.sheets.delete_rows(
                        spreadsheetId, grid["sheetId"], start_row=grid["startIndex"] + 1, end_row=grid["endIndex"]
                    )
                elif kind != "updateDimensionProperties":
                    raise NotImplementedError(f"FakeSheetsService doesn't support {kind!r}")
                replies.append({})
            sheets = [
                {"properties": {"sheetId": sheet_id, "gridProperties": {"rowCount": self.sheets.row_count(sheet_id)}}}
                for sheet_id in {*self.sheets.cells, *self.sheets.titles}
            ]
            return {"replies": replies, "updatedSpreadsheet": {"sheets": sheets}}

        return _Request(execute)

    def batchGetByDataFilter(self, *, spreadsheetId, body):
        filters = body["dataFilters"]

        def execute():
            value_ranges = []
            for data_filter in filters:
                sheet_id, rng = self._a1(data_filter["gridRange"])
                value_ranges.append(
                    {
                        "dataFilters": [data_filter],
                        "valueRange": {"values": self.sheets.read_rows_in_sheet(spreadsheetId, sheet_id, rng)},
                    }
                )
            # One request, however many ranges: listed as its ranges.
            self.read_requests.append(
                (self._a1(filters[0]["gridRange"])[0], " + ".join(self._a1(f["gridRange"])[1] for f in filters))
            )
            return {"valueRanges": value_ranges}

        return _Request(execute)

    def batchUpdateByDataFilter(self, *, spreadsheetId, body):
        (data,) = body["data"]
        sheet_id, rng = self._a1(data["dataFilter"]["gridRange"])

        def execute():
            self.sheets.write_rows_in_sheet(spreadsheetId, sheet_id, rng, data["values"])
            return {"totalUpdatedCells": sum(len(row) for row in data["values"])}

        return _Request(execute)

    @staticmethod
    def _a1(grid_range: dict) -> tuple[int, str]:
        first = f"{_column_letters(grid_range['startColumnIndex'] + 1)}{grid_range['startRowIndex'] + 1}"
        last = _column_letters(grid_range["endColumnIndex"]) + (
            str(grid_range["endRowIndex"]) if "endRowIndex" in grid_range else ""
        )
        return grid_range["sheetId"], f"{first}:{last}"


class _Request:
    def __init__(self, execute) -> None:
        self.execute = execute
