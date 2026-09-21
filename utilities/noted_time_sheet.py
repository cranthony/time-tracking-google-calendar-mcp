"""Manages the tab a calendar's noted times are recorded in, within its
shared calendar metadata spreadsheet (see
utilities/calendar_metadata_sheet.py).

calendar_clients/google_sheets.py's SheetsClient is a thin, pure API
wrapper -- it has no idea what a "noted time" is. NotedTimeSheet here
knows the tab's shape (header row, which range holds data) and, via
calendar_metadata_sheet.ensure_tab, how to find/create *the* tab for a
calendar, but nothing about how a note should be presented -- that's
server.py/calendar_cli.py's job, one layer up, the same split
utilities/event_label_sheet.py's EventLabelSheet (which this mirrors)
has with utilities/event_labels.py.

Unlike an event label, a noted time has no counterpart on the Calendar
API to reconcile with -- it's purely a Sheet-native record -- so there's
no id column here, and no EventLabels-equivalent reconciliation layer
above this one: server.py/calendar_cli.py talk to NotedTimeSheet
directly.

Notes are never deleted. Compacting them (utilities/note_compactor.py)
stamps each consumed row's `compaction_id` instead, and `read`/
`read_with_rows` return only unstamped ("uncompacted") notes unless asked
otherwise. Since rows are never removed, a note's row number is
permanent, and with its timestamp forms its id (see `SheetNote`).
"""

from __future__ import annotations
from dataclasses import dataclass, fields
from datetime import datetime

from calendar_clients.google_sheets import SheetsClient
from utilities import calendar_metadata_sheet

DEFAULT_SHEET_TITLE = calendar_metadata_sheet.TIME_NOTES_SHEET_TITLE

_SHEET_ROLE = calendar_metadata_sheet.TIME_NOTES_SHEET_ROLE

_FIRST_DATA_ROW = 2
"""Row 1 is the header."""


@dataclass(kw_only=True)
class NotedTime:
    """A single time note: some moment worth recording, with an optional
    free-text description of what it marks."""

    timestamp: datetime
    """When this note refers to. Required -- a note with no timestamp
    isn't a time note."""

    description: str | None = None
    """Optional free-text description of what this timestamp marks."""

    compaction_id: str | None = None
    """The id of the compaction (see utilities/note_compactor.py) that
    consumed this note, or `None` while it's still uncompacted. Set only
    by `NotedTimeSheet.mark_compacted`, never by a caller recording a
    note."""

    @classmethod
    def from_row(cls, header_row: list[str], data: list[str]) -> "NotedTime":
        field_names = {f.name for f in fields(cls)}
        decoded: dict[str, str] = {}
        for i, header in enumerate(header_row):
            if header in field_names:
                assert header not in decoded, f"{header} specified multiple times"
                decoded[header] = data[i] if i < len(data) else ""
        timestamp_value = decoded.get("timestamp")
        if not timestamp_value:
            raise ValueError(f"Row is missing a timestamp: {data!r}")
        return cls(
            timestamp=datetime.fromisoformat(timestamp_value),
            description=decoded.get("description") or None,
            compaction_id=decoded.get("compaction_id") or None,
        )

    def to_row(self, header_row: list[str], original_row: list[str] | None = None) -> list[str]:
        field_names = {f.name for f in fields(self)}
        result = []
        for i, header in enumerate(header_row):
            if header not in field_names:
                result.append(
                    original_row[i] if original_row is not None and i < len(original_row) else ""
                )
                continue
            value = getattr(self, header)
            if header == "timestamp":
                result.append(value.isoformat())
            else:
                result.append("" if value is None else str(value))
        return result


_LAST_COLUMN = chr(ord("A") + len(fields(NotedTime)) - 1)
_HEADER_RANGE = f"A1:{_LAST_COLUMN}1"
_DATA_RANGE = f"A{_FIRST_DATA_ROW}:{_LAST_COLUMN}"


@dataclass(kw_only=True)
class SheetNote:
    """A `NotedTime` plus the sheet row it lives in. Rows are never
    removed, so `row` is permanent. `id` combines that row with the
    note's timestamp -- e.g. `2026-01-01T09:05:00+00:00#5` -- so a note
    can be told apart by *when* it was, and so a stale id (the row was
    edited, or moved) is caught instead of silently pointing at the
    wrong note: see `NotedTimeSheet.mark_compacted`."""

    row: int
    note: NotedTime

    @property
    def id(self) -> str:
        return f"{self.note.timestamp.isoformat()}#{self.row}"


def parse_note_id(note_id: str) -> tuple[datetime, int]:
    """The (timestamp, row) a `SheetNote.id` encodes. Raises `ValueError`
    for anything that isn't one."""
    timestamp, separator, row = note_id.rpartition("#")
    if separator and row.isdigit():
        try:
            return datetime.fromisoformat(timestamp), int(row)
        except ValueError:
            pass
    raise ValueError(f"{note_id!r} isn't a note id (expected e.g. '2026-01-01T09:05:00+00:00#5')")


class NotedTimeSheet:
    """Reads and writes to the tab that records a calendar's noted
    times, within its shared calendar metadata spreadsheet."""

    def __init__(self, sheets_client: SheetsClient, spreadsheet_id: str, sheet_id: int) -> None:
        self._sheets_client = sheets_client
        self._spreadsheet_id = spreadsheet_id
        self._sheet_id = sheet_id

    @property
    def spreadsheet_id(self) -> str:
        return self._spreadsheet_id

    @staticmethod
    def ensure(sheets_client: SheetsClient, spreadsheet_id: str) -> "NotedTimeSheet":
        """The calendar's noted-times tab within `spreadsheet_id`,
        creating and tagging it (with a header row) the first time only
        -- see calendar_metadata_sheet.ensure_tab. Unlike
        EventLabelSheet.ensure, there's no pre-existing data to worry
        about preserving: this tab never existed before per-tab tagging
        did, so it's always either already tagged or brand new."""
        sheet_id, tab_created = calendar_metadata_sheet.ensure_tab(
            sheets_client,
            spreadsheet_id,
            role=_SHEET_ROLE,
            title=DEFAULT_SHEET_TITLE,
        )
        sheet = NotedTimeSheet(sheets_client, spreadsheet_id, sheet_id)
        if tab_created:
            header_row = [f.name for f in fields(NotedTime)]
            sheets_client.write_rows_in_sheet(spreadsheet_id, sheet_id, _HEADER_RANGE, [header_row])
        return sheet

    def read_with_rows(self, *, include_compacted: bool = False) -> list[SheetNote]:
        """The data rows (everything after the header row) of this tab,
        in sheet order, each with its row number. Only uncompacted notes
        unless `include_compacted`. Rows with nothing in them are
        skipped (their row numbers still count)."""
        header_row = self._read_header()
        rows = self._sheets_client.read_rows_in_sheet(self._spreadsheet_id, self._sheet_id, _DATA_RANGE)
        result = []
        for offset, row in enumerate(rows):
            if not any(cell.strip() for cell in row):
                continue
            note = NotedTime.from_row(header_row, row)
            if include_compacted or note.compaction_id is None:
                result.append(SheetNote(row=_FIRST_DATA_ROW + offset, note=note))
        return result

    def read(self, *, include_compacted: bool = False) -> list[NotedTime]:
        """Every note, sorted by timestamp -- notes are appended in
        whatever order they're recorded in, not necessarily chronological
        (e.g. backfilling an earlier note after a later one). Only
        uncompacted notes unless `include_compacted`."""
        noted_times = [
            sheet_note.note for sheet_note in self.read_with_rows(include_compacted=include_compacted)
        ]
        noted_times.sort(key=lambda noted_time: noted_time.timestamp)
        return noted_times

    def write(self, noted_times: list[NotedTime]) -> None:
        """Overwrite this tab's data rows with `noted_times`."""
        header_row = self._read_header()
        previous_rows = self._sheets_client.read_rows_in_sheet(
            self._spreadsheet_id, self._sheet_id, _DATA_RANGE
        )
        self._sheets_client.write_rows_in_sheet(self._spreadsheet_id, self._sheet_id, _DATA_RANGE, [
            noted_time.to_row(header_row, previous_rows[i] if i < len(previous_rows) else None)
            for i, noted_time in enumerate(noted_times)
        ])

    def append(self, noted_time: NotedTime) -> None:
        """Add `noted_time` as a new row at the end of this tab. Writes
        only that new row -- never rewriting the existing ones -- so it
        can't clobber a concurrent `mark_compacted` stamp on one of
        them."""
        header_row = self._read_header()
        previous_rows = self._sheets_client.read_rows_in_sheet(
            self._spreadsheet_id, self._sheet_id, _DATA_RANGE
        )
        next_row = _FIRST_DATA_ROW + len(previous_rows)
        self._sheets_client.write_rows_in_sheet(
            self._spreadsheet_id,
            self._sheet_id,
            f"A{next_row}:{_LAST_COLUMN}",
            [noted_time.to_row(header_row, None)],
        )

    def mark_compacted(self, note_ids: list[str], compaction_id: str) -> None:
        """Stamp `compaction_id` onto the notes `note_ids` (see
        `SheetNote.id`), marking them compacted. Touches only that one
        column of those rows -- and refuses, writing nothing, if any
        row no longer holds the note its id names (its timestamp changed,
        or it's gone), or was already compacted by a *different*
        compaction."""
        if not note_ids:
            return
        wanted = [parse_note_id(note_id) for note_id in note_ids]
        current = {n.row: n.note for n in self.read_with_rows(include_compacted=True)}
        for (timestamp, row), note_id in zip(wanted, note_ids):
            note = current.get(row)
            if note is None or note.timestamp != timestamp:
                raise ValueError(
                    f"row {row} no longer holds the note {note_id!r} -- it was edited or "
                    "removed since it was read"
                )
            if note.compaction_id not in (None, compaction_id):
                raise ValueError(
                    f"note {note_id!r} was already compacted by {note.compaction_id!r}"
                )
        header_row = self._read_header()
        column = chr(ord("A") + header_row.index("compaction_id"))
        for first, last in _contiguous_runs(sorted({row for _, row in wanted})):
            self._sheets_client.write_rows_in_sheet(
                self._spreadsheet_id,
                self._sheet_id,
                f"{column}{first}:{column}{last}",
                [[compaction_id]] * (last - first + 1),
            )

    def _read_header(self) -> list[str]:
        rows = self._sheets_client.read_rows_in_sheet(self._spreadsheet_id, self._sheet_id, _HEADER_RANGE)
        header_row = rows[0] if rows else []
        expected = {f.name for f in fields(NotedTime)}
        if not expected <= set(header_row):
            raise ValueError(
                f"Missing expected header columns at {_HEADER_RANGE}: {expected - set(header_row)}"
            )
        return header_row


def _contiguous_runs(sorted_rows: list[int]) -> list[tuple[int, int]]:
    """[(first, last), ...] for each run of consecutive numbers."""
    runs: list[tuple[int, int]] = []
    for row in sorted_rows:
        if runs and row == runs[-1][1] + 1:
            runs[-1] = (runs[-1][0], row)
        else:
            runs.append((row, row))
    return runs
