"""Row-seek hints for this app's growing, append-only Sheet-backed logs
(the noted-times tab and the compaction journal): a small, fixed-size
tab, one row per named hint, that lets a caller skip straight to roughly
the right place instead of reading a whole tab's worth of rows just to
find where new data starts or ends.

Google Sheets throttles how many rows can be read per minute, and both
of those tabs only ever grow (rows are never deleted -- see
utilities/noted_time_sheet.py and utilities/compaction_journal.py), so a
read that scales with their whole history gets more expensive forever.
A hint turns that into a read that scales with how much changed since
the hint was last set instead.

A hint is never trusted blindly, though: the user can edit either tab
directly (insert a row, clear a cell), which would make a stale hint
point at the wrong place. Each caller re-confirms its own hint -- by
reading a small, fixed number of rows around it and checking they look
the way a correct hint implies -- before relying on it, and falls back to
a full read (refreshing the hint from that) if the confirmation fails.
That check is domain-specific (what "looks right" means differs for an
append-position hint vs. a read-start hint), so it lives with each
caller, not here -- this module only stores and retrieves the row
numbers themselves.
"""

from __future__ import annotations

from calendar_clients.google_sheets import SheetsClient
from utilities import calendar_metadata_sheet

SHEET_ROLE = "row-hints"
SHEET_TITLE = "Row Hints"

_HEADER = ["hint", "row"]
_HEADER_RANGE = "A1:B1"
_DATA_RANGE = "A2:B"
_FIRST_DATA_ROW = 2


class RowHints:
    """Reads and writes named row-number hints, one per row, in their own
    tab of a calendar's metadata spreadsheet (see
    utilities/calendar_metadata_sheet.py). This tab never grows past one
    row per distinct hint name ever used, so it's read in full -- it's
    the much larger tabs elsewhere that this exists to spare from that.

    It's read once, though, not on every `get`/`set`: the rows are kept
    in memory for this object's lifetime and written through on `set`. A
    compaction consults hints a dozen times or more, and every one was a
    Sheets read request against a per-minute quota. A stale cached hint
    (the user edited this tab by hand) is no worse than a stale hint in
    the sheet itself -- callers re-confirm every hint before relying on
    it either way (see the module docstring).

    The one thing that is re-read is where a *new* hint name goes: more
    than one `RowHints` can be open on the same tab (the notes tab and
    the journal each `ensure` their own), so this object's rows may be
    missing a name the other just appended -- appending after them
    blindly would overwrite it."""

    def __init__(self, sheets_client: SheetsClient, spreadsheet_id: str, sheet_id: int) -> None:
        self._sheets_client = sheets_client
        self._spreadsheet_id = spreadsheet_id
        self._sheet_id = sheet_id
        self._cached_rows: list[list[str]] | None = None

    @staticmethod
    def ensure(sheets_client: SheetsClient, spreadsheet_id: str) -> "RowHints":
        """The calendar's row-hints tab within `spreadsheet_id`, creating
        and tagging it (with a header row) the first time only -- see
        calendar_metadata_sheet.ensure_tab."""
        sheet_id, created = calendar_metadata_sheet.ensure_tab(
            sheets_client, spreadsheet_id, role=SHEET_ROLE, title=SHEET_TITLE
        )
        if created:
            sheets_client.write_rows_in_sheet(spreadsheet_id, sheet_id, _HEADER_RANGE, [_HEADER])
        return RowHints(sheets_client, spreadsheet_id, sheet_id)

    def get(self, name: str) -> int | None:
        """The row last stored under `name`, or `None` if it's never been
        set. Never trust this alone -- see the module docstring."""
        rows = self._rows()
        offset = _offset_of(name, rows)
        if offset is None:
            return None
        row = rows[offset]
        return int(row[1]) if len(row) > 1 and row[1].strip() else None

    def set(self, name: str, row_number: int) -> None:
        """Store `row_number` under `name`, overwriting whatever was
        there -- adding a new row for `name` the first time it's used."""
        already_cached = self._cached_rows is not None
        rows = self._rows()
        offset = _offset_of(name, rows)
        if offset is None and already_cached:
            rows = self._rows(refresh=True)
            offset = _offset_of(name, rows)
        if offset is not None:
            self._sheets_client.write_rows_in_sheet(
                self._spreadsheet_id,
                self._sheet_id,
                f"B{_FIRST_DATA_ROW + offset}:B{_FIRST_DATA_ROW + offset}",
                [[str(row_number)]],
            )
            rows[offset] = [name, str(row_number)]
            return
        next_row = _FIRST_DATA_ROW + len(rows)
        self._sheets_client.write_rows_in_sheet(
            self._spreadsheet_id,
            self._sheet_id,
            f"A{next_row}:B{next_row}",
            [[name, str(row_number)]],
        )
        rows.append([name, str(row_number)])

    def _rows(self, *, refresh: bool = False) -> list[list[str]]:
        if self._cached_rows is None or refresh:
            self._cached_rows = self._sheets_client.read_rows_in_sheet(
                self._spreadsheet_id, self._sheet_id, _DATA_RANGE
            )
        return self._cached_rows


def _offset_of(name: str, rows: list[list[str]]) -> int | None:
    for offset, row in enumerate(rows):
        if row and row[0] == name:
            return offset
    return None
