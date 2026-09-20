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
"""

from __future__ import annotations
from dataclasses import dataclass, fields
from datetime import datetime

from calendar_clients.google_sheets import SheetsClient
from utilities import calendar_metadata_sheet

DEFAULT_SHEET_TITLE = calendar_metadata_sheet.TIME_NOTES_SHEET_TITLE

_SHEET_ROLE = calendar_metadata_sheet.TIME_NOTES_SHEET_ROLE

_HEADER_RANGE = "A1:B1"
_DATA_RANGE = "A2:B"


@dataclass(kw_only=True)
class NotedTime:
    """A single uncompacted time note: some moment worth recording, with
    an optional free-text description of what it marks."""

    timestamp: datetime
    """When this note refers to. Required -- a note with no timestamp
    isn't a time note."""

    description: str | None = None
    """Optional free-text description of what this timestamp marks."""

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

    def read(self) -> list[NotedTime]:
        """The data rows (everything after the header row) of this tab."""
        header_row = self._read_header()
        rows = self._sheets_client.read_rows_in_sheet(self._spreadsheet_id, self._sheet_id, _DATA_RANGE)
        return [NotedTime.from_row(header_row, row) for row in rows]

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
        """Add `noted_time` as a new row at the end of this tab."""
        header_row = self._read_header()
        previous_rows = self._sheets_client.read_rows_in_sheet(
            self._spreadsheet_id, self._sheet_id, _DATA_RANGE
        )
        new_row = noted_time.to_row(header_row, None)
        self._sheets_client.write_rows_in_sheet(
            self._spreadsheet_id, self._sheet_id, _DATA_RANGE, previous_rows + [new_row]
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
