"""Manages the Google Sheet a calendar's event labels can be synced with.

calendar_clients/google_calendar.py's CalendarClient and calendar_clients/
google_sheets.py's SheetsClient are thin, pure API wrappers -- neither has any
idea what an "event label sheet" is. EventLabelSheet here is the glue between
them: it knows the sheet's shape (header row, ID column narrowed, which range
holds data) and how to find/record *the* sheet for a calendar, but nothing
about what a row means as an event label, or how syncing it should reconcile
with the calendar's actual labels -- that's utilities/event_labels.py's job,
one layer up, which is why this module works in plain rows (list[str]), not
an EventLabel object.
"""

from __future__ import annotations

from calendar_clients.google_calendar import CalendarClient
from calendar_clients.google_sheets import SheetsClient

DEFAULT_SHEET_TITLE = "Event Labels"

HEADER_ROW = ["ID", "Name", "Background Color", "Priority"]
"""Every row after this one is one event label: ID (see _ID_COLUMN_INDEX),
Name, Background Color, Priority."""

_SHEET_ID_METADATA_KEY = "event-label-sheet-id"
"""The CalendarClient.get_calendar_metadata/set_calendar_metadata key this
app stores the event label sheet's spreadsheet id under -- on the
calendar itself, rather than something found by searching Drive, so
find_sheet is a single deterministic lookup instead of a "most recently
modified" guess among however many sheets this app has created."""

_ID_COLUMN_INDEX = 0
_ID_COLUMN_PIXEL_WIDTH = 60
"""Narrow -- users aren't expected to care about the ID column's
contents, just that it's there and round-trips."""

_HEADER_RANGE = "Sheet1!A1:D1"
_DATA_RANGE = "Sheet1!A2:D"


class EventLabelSheet:
    """Creates, locates, and reads/writes the rows of a calendar's event
    label sheet -- a spreadsheet with one row per label (see HEADER_ROW),
    whose id is tracked on the calendar itself (see `find_sheet`), not by
    searching Drive.
    """

    def __init__(self, calendar_client: CalendarClient, sheets_client: SheetsClient) -> None:
        self._calendar_client = calendar_client
        self._sheets_client = sheets_client

    def create_sheet(self, title: str = DEFAULT_SHEET_TITLE, initial_rows: list[list[str]] | None = None) -> str:
        """Create a new event label sheet, recorded on the calendar
        itself so `find_sheet` can find it again later, with the header
        row and (if given) `initial_rows` already written. Returns the
        new spreadsheet's id.

        Raises `ValueError` if this calendar already has an event label
        sheet tracked (see `find_sheet`) -- there's no `delete_sheet`, so
        untracking one first (`CalendarClient.set_calendar_metadata`
        with `value=None`) or using the existing sheet is the caller's
        job; this just refuses to silently orphan the previous one by
        overwriting what's tracked."""
        existing = self.find_sheet()
        if existing is not None:
            raise ValueError(
                f"This calendar already has an event label sheet tracked (spreadsheet id "
                f"{existing!r}) -- sync from it instead of creating a new one."
            )
        spreadsheet_id = self._sheets_client.create_spreadsheet(title)
        self._calendar_client.set_calendar_metadata(_SHEET_ID_METADATA_KEY, spreadsheet_id)
        self._sheets_client.set_column_width(
            spreadsheet_id,
            sheet_id=0,
            column_index=_ID_COLUMN_INDEX,
            pixel_width=_ID_COLUMN_PIXEL_WIDTH,
        )
        self._sheets_client.write_rows(spreadsheet_id, _HEADER_RANGE, [HEADER_ROW])
        if initial_rows:
            self._sheets_client.write_rows(spreadsheet_id, _DATA_RANGE, initial_rows)
        return spreadsheet_id

    def find_sheet(self) -> str | None:
        """The id of the event label sheet this app created for this
        calendar (recorded on the calendar itself by `create_sheet`), or
        `None` if it hasn't created one (yet)."""
        return self._calendar_client.get_calendar_metadata(_SHEET_ID_METADATA_KEY)

    def resolve_sheet_id(self) -> str:
        """The event label sheet tracked on this calendar (see
        `find_sheet`). Raises `ValueError` if none is tracked."""
        spreadsheet_id = self.find_sheet()
        if spreadsheet_id is None:
            raise ValueError(
                "No event label sheet is tracked on this calendar -- call create_sheet() first."
            )
        return spreadsheet_id

    def read_rows(self, spreadsheet_id: str) -> list[list[str]]:
        """The data rows (everything after HEADER_ROW) of `spreadsheet_id`."""
        return self._sheets_client.read_rows(spreadsheet_id, _DATA_RANGE)

    def write_rows(self, spreadsheet_id: str, rows: list[list[str]]) -> None:
        """Overwrite `spreadsheet_id`'s data rows with `rows`."""
        self._sheets_client.write_rows(spreadsheet_id, _DATA_RANGE, rows)
