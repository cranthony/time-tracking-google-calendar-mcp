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
from dataclasses import asdict, dataclass, fields

from calendar_clients.google_calendar import EventLabel as RawEventLabel, color_for_priority
from calendar_clients.google_sheets import SheetsClient

DEFAULT_SHEET_TITLE = "Event Labels"

_ID_COLUMN_INDEX = 0
_ID_COLUMN_PIXEL_WIDTH = 60
"""Narrow -- users aren't expected to care about the ID column's
contents, just that it's there and round-trips."""

# Since these are missing the sheet name, they fetch from the first visible
# sheet.
_HEADER_RANGE = "A1:D1"
_DATA_RANGE = "A2:D"


@dataclass(kw_only=True)
class EventLabel:
    """One of a calendar's custom event labels, plus its priority (see
    the module docstring). `id`/`name`/`background_color` mirror
    `calendar_clients.google_calendar.EventLabel`; `priority` is sourced
    from -- and, via `EventLabels.sync_from_sheet`, written back to -- a
    synced event label sheet, and is `None` if no sheet tracks this
    label (or no sheet has been created at all)."""

    id: str | None = None
    """Uniquely identifies the label within its calendar. `None` until
    the label has been created."""

    name: str | None = None
    """Optional display name, up to 50 characters."""

    background_color: str | None = None
    """Hex color (e.g. "#8e24aa") events with this label are shown in.
    May be left `None` to derive one from `priority` instead -- see
    `EventLabels.create_label`/`update_label`."""

    priority: int | None = None
    """This label's priority, if known -- see the module docstring."""

    @classmethod
    def from_raw(cls, raw: RawEventLabel) -> "EventLabel":
        return cls(**asdict(raw))

    def to_raw(self) -> RawEventLabel:
        background_color = self.background_color
        if background_color is None:
            background_color = color_for_priority(self.priority)[1]
        return RawEventLabel(id=self.id, name=self.name, background_color=background_color)

    @classmethod
    def from_row(cls, header_row: list[str], data: list[str]) -> "EventLabel":
        field_names = {f.name for f in fields(cls)}
        decoded: dict[str, str] = {}
        for i, header in enumerate(header_row):
            if header in field_names:
                assert header not in decoded, f"{header} specified multiple times"
                decoded[header] = data[i] if i < len(data) else ""
        return cls(
            id=decoded.get("id") or None,
            name=decoded.get("name") or None,
            background_color=decoded.get("background_color") or None,
            priority=int(decoded["priority"]) if decoded.get("priority") else None,
        )

    def to_row(self, header_row: list[str], original_row: list[str] | None = None) -> list[str]:
        field_names = {f.name for f in fields(self)}
        result = []
        for i, header in enumerate(header_row):
            if header in field_names:
                value = getattr(self, header)
                result.append("" if value is None else str(value))
            elif original_row is not None and i < len(original_row):
                result.append(original_row[i])
            else:
                result.append("")
        return result


class EventLabelSheet:
    """Reads and writes to the event label Sheet that represents the
    event label metadata for a calendar.
    """

    def __init__(self,
                 sheets_client: SheetsClient,
                 spreadsheet_id: str) -> None:
        self._sheets_client = sheets_client
        self._spreadsheet_id = spreadsheet_id

    @property
    def spreadsheet_id(self) -> str:
        return self._spreadsheet_id

    @staticmethod
    def create(sheets_client: SheetsClient,
               title: str = DEFAULT_SHEET_TITLE,
               initial_labels: list[EventLabel] | None = None) -> str:
        """Create a new event label sheet and returns its ID."""
        spreadsheet_id = sheets_client.create_spreadsheet(title)
        sheets_client.set_column_width(
            spreadsheet_id,
            sheet_id=0,
            column_index=_ID_COLUMN_INDEX,
            pixel_width=_ID_COLUMN_PIXEL_WIDTH,
        )
        header_row = [f.name for f in fields(EventLabel)]
        sheets_client.write_rows(spreadsheet_id, _HEADER_RANGE, [header_row])
        if initial_labels:
            sheets_client.write_rows(spreadsheet_id, _DATA_RANGE, [
                label.to_row(header_row, None)
                for label in initial_labels
            ])
        return spreadsheet_id

    def read(self) -> list[EventLabel]:
        """The data rows (everything after HEADER_ROW) of `spreadsheet_id`."""
        header_row = self._read_header()
        rows = self._sheets_client.read_rows(self._spreadsheet_id, _DATA_RANGE)
        return [EventLabel.from_row(header_row, row) for row in rows]

    def write(self, event_labels: list[EventLabel]) -> None:
        """Overwrite `spreadsheet_id`'s data rows with `rows`."""
        header_row = self._read_header()
        previous_rows = self._sheets_client.read_rows(self._spreadsheet_id, _DATA_RANGE)
        self._sheets_client.write_rows(self._spreadsheet_id, _DATA_RANGE, [
            event_label.to_row(header_row, previous_rows[i] if i < len(previous_rows) else None)
            for i, event_label in enumerate(event_labels)
        ])

    def append(self, label: EventLabel) -> None:
        """Add `label` as a new row at the end of the sheet."""
        header_row = self._read_header()
        previous_rows = self._sheets_client.read_rows(self._spreadsheet_id, _DATA_RANGE)
        new_row = label.to_row(header_row, None)
        self._sheets_client.write_rows(self._spreadsheet_id, _DATA_RANGE, previous_rows + [new_row])

    def _read_header(self) -> list[str]:
        rows = self._sheets_client.read_rows(self._spreadsheet_id, _HEADER_RANGE)
        header_row = rows[0] if rows else []
        expected = {f.name for f in fields(EventLabel)}
        if not expected <= set(header_row):
            raise ValueError(
                f"Missing expected header columns at {_HEADER_RANGE}: {expected - set(header_row)}"
            )
        return header_row
