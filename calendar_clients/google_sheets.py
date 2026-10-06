from __future__ import annotations

import contextlib
import random
import re
import time
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from googleapiclient.errors import HttpError

from calendar_clients.google_auth import build_service, load_credentials
from calendar_clients.write_lock import requires_write_lock


class SheetsClient:
    """Wraps the Google Sheets API behind a small, mockable interface --
    the thin layer `utilities/calendar_metadata_sheet.py` and each tab's
    own module (e.g. `utilities/goal_sheet.py`) build their higher-level
    logic on top of, the same way `CalendarClient` is a thin layer under
    `utilities/event_changes.py`. Knows nothing about event
    labels, time notes, or any other meaning attached to a tab; just
    spreadsheets, tabs, rows, and columns.

    This only talks to the Sheets API, never the Drive API directly --
    there's no need for a spreadsheet's id to be looked up by searching
    Drive; `utilities/calendar_metadata_sheet.py` gets it from
    `CalendarClient.get_calendar_metadata`/`set_calendar_metadata`
    instead. `drive.file` is still the OAuth scope this relies on,
    though (see `calendar_clients/google_auth.py`): the Sheets API's own
    `spreadsheets.create` requires it (or a broader Drive/Sheets scope)
    even when the Drive API surface itself is never called.
    """

    def __init__(self, sheets_service):
        self._sheets_service = sheets_service

    @classmethod
    def from_credentials(cls, token_path: Path, credentials_path: Path) -> "SheetsClient":
        """See `calendar_clients.google_auth.load_credentials` for
        `token_path`/`credentials_path` -- this shares the same OAuth
        token (and its combined `SCOPES`) as `CalendarClient`, so both
        must be loaded from the same underlying credentials to avoid
        refreshing/writing `token_path` twice; prefer
        `config._build_calendar_and_sheets_clients` over calling this
        directly when both clients are needed together."""
        creds = load_credentials(token_path, credentials_path)
        return cls(build_service("sheets", "v4", credentials=creds))

    @requires_write_lock
    def create_spreadsheet(self, title: str) -> str:
        """Create a new, empty spreadsheet titled `title`. Returns the
        new spreadsheet's id. Its one default tab is left exactly as
        the API creates it (sheetId 0, titled "Sheet1") -- callers that
        want it renamed/tagged do so afterwards, e.g. via
        `update_sheet_properties`/`create_sheet_metadata`."""
        spreadsheet = _execute(
            self._sheets_service.spreadsheets()
            .create(body={"properties": {"title": title}}, fields="spreadsheetId")
        )
        return spreadsheet["spreadsheetId"]

    @requires_write_lock
    def add_sheet(
        self, spreadsheet_id: str, title: str, *, tab_color: dict[str, float] | None = None
    ) -> int:
        """Add a new tab titled `title` to `spreadsheet_id`, optionally
        with a `tab_color` (an RGB dict, e.g. {"red": 0.26, "green":
        0.52, "blue": 0.96}) -- purely a visible cue for a human looking
        at the spreadsheet, see `utilities/calendar_metadata_sheet.py`.
        Returns the new tab's sheetId."""
        properties: dict = {"title": title}
        if tab_color is not None:
            properties["tabColor"] = tab_color
        response = _execute(
            self._sheets_service.spreadsheets()
            .batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={"requests": [{"addSheet": {"properties": properties}}]},
            )
        )
        return response["replies"][0]["addSheet"]["properties"]["sheetId"]

    @requires_write_lock
    def update_sheet_properties(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        *,
        title: str | None = None,
        tab_color: dict[str, float] | None = None,
    ) -> None:
        """Update one or more of an existing tab's display properties
        (only `title`/`tab_color` are supported -- the only ones any
        caller has needed so far) in a single call. Passing neither is a
        no-op -- no request is sent."""
        properties: dict = {"sheetId": sheet_id}
        fields = []
        if title is not None:
            properties["title"] = title
            fields.append("title")
        if tab_color is not None:
            properties["tabColor"] = tab_color
            fields.append("tabColor")
        if not fields:
            return
        _execute(
            self._sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "updateSheetProperties": {
                                "properties": properties,
                                "fields": ",".join(fields),
                            }
                        }
                    ]
                },
            )
        )

    def get_sheet_title(self, spreadsheet_id: str, sheet_id: int) -> str:
        """The current display title of the tab identified by `sheet_id`
        within `spreadsheet_id` -- looked up fresh every time (never
        cached) so a rename since the tab was created/tagged is picked
        up immediately. Raises `ValueError` if no tab with that id
        exists (e.g. it was deleted from under this app)."""
        response = _execute(
            self._sheets_service.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields="sheets.properties")
        )
        for sheet in response.get("sheets", []):
            properties = sheet["properties"]
            if properties["sheetId"] == sheet_id:
                return properties["title"]
        raise ValueError(f"No sheet with sheetId {sheet_id} in spreadsheet {spreadsheet_id}")

    @requires_write_lock
    def create_sheet_metadata(self, spreadsheet_id: str, sheet_id: int, key: str, value: str) -> None:
        """Tag the tab identified by `sheet_id` with developer metadata
        `key`/`value`, PROJECT-scoped (queryable only by this app's own
        OAuth client -- never shown in the Sheets UI, and invisible to
        any other app). See `find_sheet_id` for the matching lookup, and
        `utilities/calendar_metadata_sheet.py` for why tabs are located
        this way instead of by title or position."""
        _execute(
            self._sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "createDeveloperMetadata": {
                                "developerMetadata": {
                                    "metadataKey": key,
                                    "metadataValue": value,
                                    "location": {"sheetId": sheet_id},
                                    "visibility": "PROJECT",
                                }
                            }
                        }
                    ]
                },
            )
        )

    def find_sheet_id(self, spreadsheet_id: str, key: str, value: str) -> int | None:
        """The sheetId of `spreadsheet_id`'s tab tagged `key`/`value` via
        `create_sheet_metadata`, or `None` if no tab carries that tag."""
        response = _execute(
            self._sheets_service.spreadsheets()
            .developerMetadata()
            .search(
                spreadsheetId=spreadsheet_id,
                body={
                    "dataFilters": [
                        {"developerMetadataLookup": {"metadataKey": key, "metadataValue": value}}
                    ]
                },
            )
        )
        matches = response.get("matchedDeveloperMetadata", [])
        if not matches:
            return None
        return matches[0]["developerMetadata"]["location"]["sheetId"]

    @requires_write_lock
    def set_column_width(
        self, spreadsheet_id: str, *, sheet_id: int, column_index: int, pixel_width: int
    ) -> None:
        """Narrow (or widen) a single column, by its 0-based index, on the
        sheet identified by `sheet_id` (0 for the first/default sheet of a
        newly-created spreadsheet)."""
        _execute(
            self._sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "updateDimensionProperties": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "dimension": "COLUMNS",
                                    "startIndex": column_index,
                                    "endIndex": column_index + 1,
                                },
                                "properties": {"pixelSize": pixel_width},
                                "fields": "pixelSize",
                            }
                        }
                    ]
                },
            )
        )

    @requires_write_lock
    def delete_rows(
        self,
        spreadsheet_id: str,
        sheet_id: int,
        *,
        start_row: int,
        end_row: int,
        keep_at_least: int = 0,
    ) -> None:
        """Permanently delete rows `start_row` through `end_row` (both
        1-based, inclusive) from the tab identified by `sheet_id`,
        shifting every row below up to fill the gap. Used to
        garbage-collect old rows from an only-ever-growing, append-only
        tab (see each such tab's own `garbage_collect`).

        Deleting rows shrinks the tab's grid, and a write past the end of
        the grid fails -- so if that leaves the tab with fewer than
        `keep_at_least` rows in all, empty rows are added at the bottom to
        make up the difference. The delete reports the tab's remaining
        row count itself, so this costs a second request only when rows
        actually need adding."""
        _forget_cached_reads(spreadsheet_id, sheet_id)
        response = _execute(
            self._sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "deleteDimension": {
                                "range": {
                                    "sheetId": sheet_id,
                                    "dimension": "ROWS",
                                    "startIndex": start_row - 1,
                                    "endIndex": end_row,
                                }
                            }
                        }
                    ],
                    "includeSpreadsheetInResponse": True,
                    "responseIncludeGridData": False,
                },
                fields="updatedSpreadsheet(sheets(properties(sheetId,gridProperties(rowCount))))",
            )
        )
        row_count = next(
            (
                sheet["properties"]["gridProperties"]["rowCount"]
                for sheet in response.get("updatedSpreadsheet", {}).get("sheets", [])
                if sheet["properties"]["sheetId"] == sheet_id
            ),
            None,
        )
        if row_count is None or row_count >= keep_at_least:
            return
        _execute(
            self._sheets_service.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id,
                body={
                    "requests": [
                        {
                            "appendDimension": {
                                "sheetId": sheet_id,
                                "dimension": "ROWS",
                                "length": keep_at_least - row_count,
                            }
                        }
                    ]
                },
            )
        )

    def read_rows(self, spreadsheet_id: str, sheet_range: str) -> list[list[str]]:
        """The cell values in `sheet_range` (e.g. "Sheet1!A2:D"), one list
        per row -- a row with trailing blank cells may come back shorter
        than the range's column count (the API omits them), and there are
        no rows at all (`[]`) if the range is entirely empty."""
        response = _execute(
            self._sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=sheet_range)
        )
        return response.get("values", [])

    @requires_write_lock
    def write_rows(self, spreadsheet_id: str, sheet_range: str, rows: list[list[str]]) -> None:
        """Overwrite the cells starting at `sheet_range`'s top-left corner
        with `rows`. Only writes exactly `len(rows)` rows -- any existing
        rows beyond that within `sheet_range` are left untouched, so a
        caller replacing a previously-longer set of rows must clear the
        old range first."""
        _forget_cached_reads(spreadsheet_id)
        _execute(
            self._sheets_service.spreadsheets().values().update(
                spreadsheetId=spreadsheet_id,
                range=sheet_range,
                valueInputOption="RAW",
                body={"values": rows},
            )
        )

    def read_rows_in_sheet(
        self, spreadsheet_id: str, sheet_id: int, range_within_sheet: str
    ) -> list[list[str]]:
        """Like `read_rows`, but for `range_within_sheet` (e.g. "A2:D",
        without a sheet name) within the tab identified by `sheet_id` --
        instead of a caller assuming an unqualified range means "the
        first tab" (true, but only as long as a spreadsheet has exactly
        one tab, and fragile against reordering once it has more) or
        remembering a title that may since have been renamed.

        The range is sent as a sheetId-based GridRange data filter
        (`values.batchGetByDataFilter`), so there's no need to look up
        the tab's current title first: one read request, not two. Google
        Sheets caps read requests per minute per user (60 by default),
        and a compaction reads its tabs dozens of times, so that second
        request is what used to push it over.

        Inside `cached_sheet_reads`, a repeat of the same read is served
        from memory instead -- see there. To read more than one range of a
        tab, `read_ranges_in_sheet` does it in one request."""
        return self.read_ranges_in_sheet(spreadsheet_id, sheet_id, [range_within_sheet])[0]

    def read_ranges_in_sheet(
        self, spreadsheet_id: str, sheet_id: int, ranges_within_sheet: list[str]
    ) -> list[list[list[str]]]:
        """`read_rows_in_sheet` for each of `ranges_within_sheet` (a tab's
        header row and some of its data, say), in that order -- in one
        read request, `values.batchGetByDataFilter` taking any number of
        data filters. Inside `cached_sheet_reads`, each range is cached on
        its own, and any range that falls within one already cached is
        served from memory (see `_cached_rows`), so only the rest are
        requested."""
        ranges = [TabRange(spreadsheet_id, sheet_id, rng) for rng in ranges_within_sheet]
        found = {tab_range: _cached_rows(tab_range) for tab_range in ranges}
        missing = list(dict.fromkeys(r for r, rows in found.items() if rows is None))
        if missing:
            found.update(self._fetch(spreadsheet_id, missing))
        return [[list(row) for row in found[tab_range]] for tab_range in ranges]

    def prefetch(self, ranges: list["TabRange"]) -> None:
        """Inside `cached_sheet_reads`, read every one of `ranges` not
        already cached -- across tabs, in one read request per
        spreadsheet -- so later reads that fall within them are served
        from memory. For a step that will read several tabs: Google
        Sheets caps read requests at 60 a minute, so reading each tab
        whole up front costs one request instead of one per tab. Does
        nothing outside `cached_sheet_reads`, where nothing would keep
        what it read."""
        if _read_cache.get() is None:
            return
        missing = list(dict.fromkeys(r for r in ranges if _cached_rows(r) is None))
        for spreadsheet_id in dict.fromkeys(r.spreadsheet_id for r in missing):
            self._fetch(spreadsheet_id, [r for r in missing if r.spreadsheet_id == spreadsheet_id])

    def _fetch(self, spreadsheet_id: str, ranges: list["TabRange"]) -> dict["TabRange", list[list[str]]]:
        """`ranges` (all in `spreadsheet_id`), in one request, cached
        inside `cached_sheet_reads`."""
        grid_ranges = [_grid_range(r.sheet_id, r.range) for r in ranges]
        response = _execute(
            self._sheets_service.spreadsheets()
            .values()
            .batchGetByDataFilter(
                spreadsheetId=spreadsheet_id,
                body={
                    "dataFilters": [{"gridRange": grid_range} for grid_range in grid_ranges],
                    "majorDimension": "ROWS",
                },
            )
        )
        fetched = dict(zip(ranges, _values_by_filter(response, grid_ranges)))
        cache = _read_cache.get()
        if cache is not None:
            for tab_range, rows in fetched.items():
                cache[tab_range] = [list(row) for row in rows]
        return fetched

    @requires_write_lock
    def write_rows_in_sheet(
        self, spreadsheet_id: str, sheet_id: int, range_within_sheet: str, rows: list[list[str]]
    ) -> None:
        """`write_rows`'s counterpart to `read_rows_in_sheet` -- see
        there for why the tab is addressed by `sheet_id` instead of by
        title.

        Written through `values.batchUpdateByDataFilter` with a GridRange
        bounded to exactly `rows` (a write request, no read at all). That
        endpoint refuses values that don't fit in the range it matched --
        which is what happens when `rows` would land past the tab's
        current last row, since unlike `values.update` it doesn't grow the
        grid to fit. In that one case (whether it's refused outright or
        comes back having written fewer cells than asked), this falls back to an A1 range
        qualified with the tab's current title (`values.update` does grow
        the grid), paying for the title lookup only then."""
        if not rows:
            return
        _forget_cached_reads(spreadsheet_id, sheet_id)
        grid_range = _grid_range(sheet_id, range_within_sheet)
        grid_range["endRowIndex"] = grid_range["startRowIndex"] + len(rows)
        try:
            response = _execute(
                self._sheets_service.spreadsheets()
                .values()
                .batchUpdateByDataFilter(
                    spreadsheetId=spreadsheet_id,
                    body={
                        "valueInputOption": "RAW",
                        "data": [
                            {
                                "dataFilter": {"gridRange": grid_range},
                                "majorDimension": "ROWS",
                                "values": rows,
                            }
                        ],
                    },
                )
            )
        except HttpError as error:
            if error.resp.status != 400:
                raise
        else:
            if response.get("totalUpdatedCells", 0) >= sum(len(row) for row in rows):
                return
        self.write_rows(spreadsheet_id, self._qualify(spreadsheet_id, sheet_id, range_within_sheet), rows)

    def _qualify(self, spreadsheet_id: str, sheet_id: int, range_within_sheet: str) -> str:
        title = self.get_sheet_title(spreadsheet_id, sheet_id)
        escaped_title = title.replace("'", "''")
        return f"'{escaped_title}'!{range_within_sheet}"


def _values_by_filter(response: dict, grid_ranges: list[dict]) -> list[list[list[str]]]:
    """Each of `grid_ranges`' rows from a `batchGetByDataFilter` response.
    Each value range comes back with the data filters it matched, so
    they're matched up by those, falling back to the order they came back
    in."""
    value_ranges = response.get("valueRanges", [])
    by_range: list[list[list[str]] | None] = [None] * len(grid_ranges)
    unmatched = []
    for value_range in value_ranges:
        values = value_range.get("valueRange", {}).get("values", [])
        echoed = [f.get("gridRange") for f in value_range.get("dataFilters", [])]
        index = next(
            (i for i, grid_range in enumerate(grid_ranges) if by_range[i] is None and grid_range in echoed), None
        )
        if index is None:
            unmatched.append(values)
        else:
            by_range[index] = values
    leftovers = iter(unmatched)
    return [values if values is not None else next(leftovers, []) for values in by_range]


@dataclass(frozen=True)
class TabRange:
    """A range of one tab: `range` (e.g. "A1:C", without a sheet name)
    within the tab `sheet_id` of `spreadsheet_id`."""

    spreadsheet_id: str
    sheet_id: int
    range: str


_read_cache: ContextVar[dict[TabRange, list[list[str]]] | None] = ContextVar("_read_cache", default=None)


def _cached_rows(wanted: TabRange) -> list[list[str]] | None:
    """`wanted`'s rows from the cache, or `None` if it isn't cached: read
    exactly, or cut out of a cached range of the same tab that wholly
    contains it -- exactly as the API would return it, trailing empty
    cells and rows dropped, and blank rows in the middle as `[]`."""
    cache = _read_cache.get()
    if cache is None:
        return None
    if wanted in cache:
        return cache[wanted]
    inner = _grid_range(wanted.sheet_id, wanted.range)
    for cached, rows in cache.items():
        if (cached.spreadsheet_id, cached.sheet_id) != (wanted.spreadsheet_id, wanted.sheet_id):
            continue
        outer = _grid_range(cached.sheet_id, cached.range)
        if _contains(outer, inner):
            return _within(rows, outer, inner)
    return None


def _contains(outer: dict, inner: dict) -> bool:
    if not (
        outer["startColumnIndex"] <= inner["startColumnIndex"]
        and inner["endColumnIndex"] <= outer["endColumnIndex"]
        and outer["startRowIndex"] <= inner["startRowIndex"]
    ):
        return False
    if "endRowIndex" not in outer:
        return True
    return "endRowIndex" in inner and inner["endRowIndex"] <= outer["endRowIndex"]


def _within(rows: list[list[str]], outer: dict, inner: dict) -> list[list[str]]:
    """The part of `rows` (read for `outer`) that `inner` covers."""
    first = inner["startRowIndex"] - outer["startRowIndex"]
    last = inner["endRowIndex"] - outer["startRowIndex"] if "endRowIndex" in inner else None
    left = inner["startColumnIndex"] - outer["startColumnIndex"]
    right = inner["endColumnIndex"] - outer["startColumnIndex"]
    result = []
    for row in rows[first:last]:
        cells = list(row[left:right])
        while cells and cells[-1] == "":
            cells.pop()
        result.append(cells)
    while result and not result[-1]:
        result.pop()
    return result


@contextlib.contextmanager
def cached_sheet_reads() -> Iterator[None]:
    """Within this block, a `read_rows_in_sheet` repeating an earlier one
    (same spreadsheet and tab, and a range within the one read before) is
    answered from memory instead of spending another read request -- on
    any `SheetsClient`, since several
    can be open on the same spreadsheet (see `config.py`) and share tabs.
    Any write or row deletion through any of them forgets what was cached
    for that tab first, so a read always sees this process's own writes.

    Meant to wrap one MCP tool call (see `server.py`): the notes tab and
    the compaction journal are read many times over in one compaction
    step, but the only thing that can change them between those reads,
    short of the user hand-editing the spreadsheet mid-call, is this
    process. Never kept across calls, so a hand edit between calls is
    always seen. Nested blocks share the outer one's cache."""
    if _read_cache.get() is not None:
        yield
        return
    token = _read_cache.set({})
    try:
        yield
    finally:
        _read_cache.reset(token)


def _forget_cached_reads(spreadsheet_id: str, sheet_id: int | None = None) -> None:
    """Drop cached reads of `sheet_id` in `spreadsheet_id` -- or of every
    tab in it, when a write's tab isn't known by id (`write_rows`)."""
    cache = _read_cache.get()
    if cache is None:
        return
    for key in [k for k in cache if k.spreadsheet_id == spreadsheet_id and sheet_id in (None, k.sheet_id)]:
        del cache[key]


_A1_RANGE = re.compile(r"^([A-Z]+)(\d+):([A-Z]+)(\d*)$")


def _grid_range(sheet_id: int, range_within_sheet: str) -> dict:
    """`range_within_sheet` -- the only A1 shapes this app uses: "A2:D"
    (open-ended, from a row down) or "B5:C7" (a bounded block) -- as a
    GridRange on `sheet_id`: 0-based, end-exclusive, with no
    `endRowIndex` for an open-ended range (the API reads a missing index
    as unbounded on that side)."""
    match = _A1_RANGE.match(range_within_sheet)
    if match is None:
        raise ValueError(f"Unsupported range {range_within_sheet!r}")
    first_col, first_row, last_col, last_row = match.groups()
    grid_range = {
        "sheetId": sheet_id,
        "startRowIndex": int(first_row) - 1,
        "startColumnIndex": _column_number(first_col) - 1,
        "endColumnIndex": _column_number(last_col),
    }
    if last_row:
        grid_range["endRowIndex"] = int(last_row)
    return grid_range


def _column_number(letters: str) -> int:
    number = 0
    for letter in letters:
        number = number * 26 + (ord(letter) - ord("A") + 1)
    return number


_MAX_RATE_LIMIT_RETRIES = 6


def _execute(request, *, sleep=time.sleep, rand=random.random):
    """`request.execute()`, retrying with randomized exponential backoff
    (up to ~1 minute in all, long enough for a per-minute quota to roll
    over) when the API says it's rate-limited (HTTP 429) -- and only then.

    Not `execute(num_retries=...)`: that also retries 5xx responses,
    which can mean "failed after being applied", and some of this
    client's requests aren't safe to repeat (`delete_rows` would delete
    a second, different set of rows). A 429 is always refused up front,
    so retrying it is always safe."""
    for attempt in range(_MAX_RATE_LIMIT_RETRIES + 1):
        try:
            return request.execute()
        except HttpError as error:
            if error.resp.status != 429 or attempt == _MAX_RATE_LIMIT_RETRIES:
                raise
            sleep(rand() * 2**attempt)
