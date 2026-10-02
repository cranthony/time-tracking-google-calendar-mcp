"""Manages the tab a calendar's event labels were synced with, within its
shared calendar metadata spreadsheet (see
utilities/calendar_metadata_sheet.py).

Superseded by goals (utilities/goal_sheet.py, utilities/goals.py): the
only thing that still reads this tab is `Goals`' one-time migration of a
calendar's labels into goals. Kept so that migration can read a tab
written by an earlier version.

calendar_clients/google_calendar.py's CalendarClient and calendar_clients/
google_sheets.py's SheetsClient are thin, pure API wrappers -- neither has any
idea what an "event label sheet" is. EventLabelSheet here is the glue between
them: it knows the tab's shape (header row, ID column narrowed, which range
holds data) and, via calendar_metadata_sheet.ensure_tab, how to find/create
*the* tab for a calendar, but nothing about what a row means as an event
label, or how syncing it should reconcile with the calendar's actual labels
-- that was the job of the (since removed) EventLabels, one layer up.
"""

from __future__ import annotations
from dataclasses import asdict, dataclass, fields

from calendar_clients.google_calendar import EventLabel as RawEventLabel, color_for_priority
from calendar_clients.google_sheets import SheetsClient
from utilities import calendar_metadata_sheet

DEFAULT_SHEET_TITLE = "Event Labels"

_SHEET_ROLE = "event-labels"

_ID_COLUMN_INDEX = 0
_ID_COLUMN_PIXEL_WIDTH = 60
"""Narrow -- users aren't expected to care about the ID column's
contents, just that it's there and round-trips."""

_HEADER_RANGE = "A1:F1"
_DATA_RANGE = "A2:F"

_OPTIONAL_COLUMNS = frozenset({"note"})
"""Header columns a tab may lack and still be read: `note` was added
after tabs already existed in the wild, so a tab without one keeps
working -- it just can't hold a note until its header gains one (see
`_check_note_column`)."""


@dataclass(kw_only=True)
class EventLabel:
    """One of a calendar's custom event labels, plus its priority (see
    the module docstring). `id`/`name`/`background_color` mirror
    `calendar_clients.google_calendar.EventLabel`; `priority` was sourced
    only from the event label sheet."""

    id: str | None = None
    """Uniquely identifies the label within its calendar. `None` until
    the label has been created."""

    name: str | None = None
    """Optional display name, up to 50 characters."""

    background_color: str | None = None
    """Hex color (e.g. "#8e24aa") events with this label are shown in.
    May be left `None` to derive one from `priority` instead."""

    priority: int | None = None
    """This label's priority, if known -- see the module docstring."""

    fixed_time: bool | None = None
    """Whether events with this label should keep their start/end time
    fixed -- see `calendar_clients.google_calendar.Event.is_fixed_time`
    and `utilities/reallocation.py`'s "Fixed time" section. Sourced
    purely from the sheet, the same way `priority` is -- Google
    Calendar's raw label resource has no room for it either."""

    note: str | None = None
    """Free-text note about this label, e.g. what it's for. Sheet-only,
    like `priority`/`fixed_time`, and purely informational: nothing
    reads it but people and the agent."""

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
            fixed_time=(
                decoded["fixed_time"].strip().lower() == "true" if decoded.get("fixed_time") else None
            ),
            note=decoded.get("note") or None,
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
            if header == "fixed_time" and value is not None:
                result.append("TRUE" if value else "FALSE")
            else:
                result.append("" if value is None else str(value))
        return result


class EventLabelSheet:
    """Reads and writes to the tab that represents the event label
    metadata for a calendar, within its shared calendar metadata
    spreadsheet.
    """

    def __init__(self,
                 sheets_client: SheetsClient,
                 spreadsheet_id: str,
                 sheet_id: int) -> None:
        self._sheets_client = sheets_client
        self._spreadsheet_id = spreadsheet_id
        self._sheet_id = sheet_id

    @property
    def spreadsheet_id(self) -> str:
        return self._spreadsheet_id

    @staticmethod
    def ensure(
        sheets_client: SheetsClient,
        spreadsheet_id: str,
        *,
        is_new_spreadsheet: bool,
        initial_labels: list[EventLabel] | None = None,
    ) -> "EventLabelSheet":
        """The calendar's event-labels tab within `spreadsheet_id`,
        creating (or adopting, per calendar_metadata_sheet.ensure_tab's
        `reuse_sheet_id`) and populating it -- narrowed ID column,
        header row, `initial_labels` -- the first time only.

        `is_new_spreadsheet` (see calendar_metadata_sheet.
        ensure_spreadsheet) must be `False` when adopting a spreadsheet
        that already existed before per-tab tagging did: its sheetId 0
        already holds real, previously-written event label data (header
        row included) that must not be overwritten, even though this is
        still the first time its tab gets tagged.
        """
        sheet_id, tab_created = calendar_metadata_sheet.ensure_tab(
            sheets_client,
            spreadsheet_id,
            role=_SHEET_ROLE,
            title=DEFAULT_SHEET_TITLE,
            reuse_sheet_id=0,
        )
        sheet = EventLabelSheet(sheets_client, spreadsheet_id, sheet_id)
        if tab_created and is_new_spreadsheet:
            sheets_client.set_column_width(
                spreadsheet_id,
                sheet_id=sheet_id,
                column_index=_ID_COLUMN_INDEX,
                pixel_width=_ID_COLUMN_PIXEL_WIDTH,
            )
            header_row = [f.name for f in fields(EventLabel)]
            sheets_client.write_rows_in_sheet(spreadsheet_id, sheet_id, _HEADER_RANGE, [header_row])
            if initial_labels:
                sheets_client.write_rows_in_sheet(spreadsheet_id, sheet_id, _DATA_RANGE, [
                    label.to_row(header_row, None)
                    for label in initial_labels
                ])
        return sheet

    def read(self) -> list[EventLabel]:
        """The data rows (everything after HEADER_ROW) of this tab."""
        header_row = self._read_header()
        rows = self._sheets_client.read_rows_in_sheet(self._spreadsheet_id, self._sheet_id, _DATA_RANGE)
        return [EventLabel.from_row(header_row, row) for row in rows]

    def write(self, event_labels: list[EventLabel]) -> None:
        """Overwrite this tab's data rows with `event_labels`."""
        header_row = self._read_header()
        _check_note_column(header_row, event_labels)
        previous_rows = self._sheets_client.read_rows_in_sheet(
            self._spreadsheet_id, self._sheet_id, _DATA_RANGE
        )
        self._sheets_client.write_rows_in_sheet(self._spreadsheet_id, self._sheet_id, _DATA_RANGE, [
            event_label.to_row(header_row, previous_rows[i] if i < len(previous_rows) else None)
            for i, event_label in enumerate(event_labels)
        ])

    def append(self, label: EventLabel) -> None:
        """Add `label` as a new row at the end of this tab."""
        header_row = self._read_header()
        _check_note_column(header_row, [label])
        previous_rows = self._sheets_client.read_rows_in_sheet(
            self._spreadsheet_id, self._sheet_id, _DATA_RANGE
        )
        new_row = label.to_row(header_row, None)
        self._sheets_client.write_rows_in_sheet(
            self._spreadsheet_id, self._sheet_id, _DATA_RANGE, previous_rows + [new_row]
        )

    def _read_header(self) -> list[str]:
        rows = self._sheets_client.read_rows_in_sheet(self._spreadsheet_id, self._sheet_id, _HEADER_RANGE)
        header_row = rows[0] if rows else []
        expected = {f.name for f in fields(EventLabel)} - _OPTIONAL_COLUMNS
        if not expected <= set(header_row):
            raise ValueError(
                f"Missing expected header columns at {_HEADER_RANGE}: {expected - set(header_row)}"
            )
        return header_row


def _check_note_column(header_row: list[str], event_labels: list[EventLabel]) -> None:
    """Refuse to write a note to a tab with no `note` column -- `to_row`
    would otherwise drop it without a word (see `_OPTIONAL_COLUMNS`)."""
    if "note" not in header_row and any(label.note is not None for label in event_labels):
        raise ValueError(
            f"This event labels tab has no \"note\" column; add that header in {_HEADER_RANGE} "
            "to store label notes"
        )
