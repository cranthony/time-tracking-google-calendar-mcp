"""Keeps a calendar's custom event labels in sync with a Google Sheet.

calendar_clients/google_calendar.py's CalendarClient and calendar_clients/
google_sheets.py's SheetsClient are thin, pure API wrappers -- neither has any
idea what an "event label sheet" is. EventLabelSheet here is the glue between
them: it's application-level policy (what columns the sheet has, how a row
maps to an EventLabel, how syncing reconciles the two), not API integration,
which is why it doesn't live on either client -- the same relationship
utilities/reallocating_calendar.py's ReallocatingCalendar has with
CalendarClient.

Priority lives *only* here (and in the sheet itself): Google Calendar's own
label resource has no such field (see EventLabel), so a label read straight
from CalendarClient always has priority=None. sync_from_sheet and
list_labels_with_priority are the only two places priority gets attached to
an EventLabel, by cross-referencing the synced sheet -- if no sheet has been
created yet, priority stays None, exactly as if it were never asked about.
"""

from __future__ import annotations

from dataclasses import replace

from calendar_clients.google_calendar import CalendarClient, EventLabel
from calendar_clients.google_sheets import SheetsClient

DEFAULT_SHEET_TITLE = "Event Labels"

_SHEET_TAG_PROPERTIES = {"cascading-time-tracker-kind": "event-label-sheet"}
"""Drive appProperties this app tags every event label sheet it creates
with, so find_sheet (SheetsClient.find_spreadsheet) can find one again
later without the caller needing to remember its spreadsheet id."""

_HEADER_ROW = ["ID", "Name", "Background Color", "Priority"]
_ID_COLUMN_INDEX = 0
_ID_COLUMN_PIXEL_WIDTH = 60
"""Narrow -- see the class docstring: users aren't expected to care about
the ID column's contents, just that it's there and round-trips."""

_HEADER_RANGE = "Sheet1!A1:D1"
_DATA_RANGE = "Sheet1!A2:D"


def _row_from_label(label: EventLabel) -> list[str]:
    return [
        label.id or "",
        label.name or "",
        label.background_color or "",
        str(label.priority) if label.priority is not None else "",
    ]


def _label_from_row(row: list[str]) -> EventLabel:
    # Sheets omits trailing blank cells from a row entirely, so pad back
    # out to all four columns before unpacking.
    id_, name, background_color, priority = (row + ["", "", "", ""])[:4]
    return EventLabel(
        id=id_ or None,
        name=name or None,
        background_color=background_color or None,
        priority=int(priority) if priority else None,
    )


class EventLabelSheet:
    """Syncs a calendar's custom event labels (`CalendarClient`) with a
    Google Sheet (`SheetsClient`) that has one row per label: ID, Name,
    Background Color, Priority -- see the module docstring.
    """

    def __init__(self, calendar_client: CalendarClient, sheets_client: SheetsClient) -> None:
        self._calendar_client = calendar_client
        self._sheets_client = sheets_client

    def create_sheet(self, title: str = DEFAULT_SHEET_TITLE) -> str:
        """Create a new event label sheet, tagged so `find_sheet` can find
        it again later, pre-populated with this calendar's *current*
        labels (no Priority column values -- Calendar doesn't know any)
        so that syncing it back immediately afterward, with no edits, is
        a no-op rather than deleting every label that already exists (see
        `sync_from_sheet`). Returns the new spreadsheet's id."""
        spreadsheet_id = self._sheets_client.create_spreadsheet(title, _SHEET_TAG_PROPERTIES)
        self._sheets_client.set_column_width(
            spreadsheet_id,
            sheet_id=0,
            column_index=_ID_COLUMN_INDEX,
            pixel_width=_ID_COLUMN_PIXEL_WIDTH,
        )
        self._sheets_client.write_rows(spreadsheet_id, _HEADER_RANGE, [_HEADER_ROW])
        labels = self._calendar_client.list_event_labels()
        if labels:
            self._sheets_client.write_rows(
                spreadsheet_id, _DATA_RANGE, [_row_from_label(label) for label in labels]
            )
        return spreadsheet_id

    def find_sheet(self) -> str | None:
        """The id of the most-recently-modified event label sheet this
        app has created, or `None` if it hasn't created one (yet)."""
        return self._sheets_client.find_spreadsheet(_SHEET_TAG_PROPERTIES)

    def sync_from_sheet(self, spreadsheet_id: str | None = None) -> list[EventLabel]:
        """Make this calendar's event labels match `spreadsheet_id` (or
        `find_sheet()`'s result, if not given) exactly:

        - A row with a blank ID becomes a newly-created label; that row's
          ID cell is then filled in with the real, Google-assigned ID, so
          syncing again doesn't create a duplicate.
        - A row whose ID matches an existing label fully overwrites that
          label's name, background color, and priority (not merged with
          whatever it had before -- see `CalendarClient.replace_event_
          labels`).
        - Any existing label whose ID isn't present in the sheet at all
          is deleted.

        Raises `ValueError` if no sheet is found and none was given.
        Returns the resulting labels, each with `priority` populated from
        its row (this is the one place, besides `list_labels_with_
        priority`, where `EventLabel.priority` gets set from something
        other than a direct call's own argument)."""
        spreadsheet_id = spreadsheet_id or self.find_sheet()
        if spreadsheet_id is None:
            raise ValueError(
                "No event label sheet found -- call create_sheet() first, or pass an "
                "explicit spreadsheet_id."
            )
        rows = self._sheets_client.read_rows(spreadsheet_id, _DATA_RANGE)
        desired_labels = [_label_from_row(row) for row in rows]
        updated_labels = self._calendar_client.replace_event_labels(desired_labels)
        # updated_labels come back from Calendar via EventLabel.from_api,
        # which never sets priority (Calendar has no such field) -- so
        # take each row's real, possibly-newly-assigned id from there, but
        # keep name/background_color/priority as this sheet already had
        # them (replace_event_labels wrote exactly what we asked for).
        merged_labels = [
            replace(desired, id=updated.id)
            for desired, updated in zip(desired_labels, updated_labels)
        ]
        self._sheets_client.write_rows(
            spreadsheet_id, _DATA_RANGE, [_row_from_label(label) for label in merged_labels]
        )
        return merged_labels

    def list_labels_with_priority(self, spreadsheet_id: str | None = None) -> list[EventLabel]:
        """This calendar's current event labels (`CalendarClient.list_
        event_labels`), with each one's `priority` filled in from
        `spreadsheet_id` (or `find_sheet()`'s result) when a row with a
        matching ID exists there -- read-only, nothing is written back.
        A label keeps `priority=None` if it has no matching row, or if no
        sheet was found at all."""
        labels = self._calendar_client.list_event_labels()
        spreadsheet_id = spreadsheet_id or self.find_sheet()
        if spreadsheet_id is None:
            return labels
        rows = self._sheets_client.read_rows(spreadsheet_id, _DATA_RANGE)
        priority_by_id = {
            row_label.id: row_label.priority
            for row_label in (_label_from_row(row) for row in rows)
            if row_label.id is not None and row_label.priority is not None
        }
        return [
            replace(label, priority=priority_by_id[label.id]) if label.id in priority_by_id else label
            for label in labels
        ]
