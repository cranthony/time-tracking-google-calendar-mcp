"""The logical event label: a calendar's raw label (calendar_clients/
google_calendar.py's CalendarClient/EventLabel) plus a priority and a
fixed_time flag, both sourced from a synced Google Sheet (utilities/
event_label_sheet.py's EventLabelSheet).

Google Calendar's own label resource has no field for priority (or
fixed_time) at all (see that module's EventLabel), so both live only in
the sheet. EventLabels here is the glue between the two: it's
application-level policy (how a raw label and a sheet row combine into
one EventLabel, and how creating/updating/syncing should resolve a color
from a priority), not API integration, which is why it doesn't live on
either CalendarClient or EventLabelSheet -- the same relationship
utilities/reallocating_calendar.py's ReallocatingCalendar has with
CalendarClient.

This module's EventLabel is a different class from calendar_clients/
google_calendar.py's EventLabel (same name, deliberately -- one is the "raw"
label, this is the richer one everything except raw_label CLI commands
should use), with `background_color` optional (derived from `priority` via
`color_for_priority` when left unset), `priority`, and `fixed_time`, none of
which the raw one has.
"""

from __future__ import annotations

from dataclasses import asdict, astuple, replace

from calendar_clients.google_calendar import CalendarClient, EventLabel as RawEventLabel
from calendar_clients.google_sheets import SheetsClient
from utilities import calendar_metadata_sheet
from utilities.event_label_sheet import EventLabelSheet, EventLabel


class EventLabels:
    """Manages a calendar's event labels as `EventLabel` objects --
    `CalendarClient`'s raw labels, enriched with metadata source from
    `EventLabelSheet`.

    Note that the calendar and the sheet are two separate sources of
    authority on which are valid labels.  For simplicity, this class
    assumes that the sheet is the real authority.  If it ever sees a
    mismatch between the sheet and the calendar, it pushes the labels
    in the sheet to the calendar.
    """

    def __init__(self, calendar_client: CalendarClient, sheets_client: SheetsClient) -> None:
        self._calendar_client = calendar_client

        spreadsheet_id, is_new_spreadsheet = calendar_metadata_sheet.ensure_spreadsheet(
            calendar_client, sheets_client
        )
        self._event_label_sheet = EventLabelSheet.ensure(
            sheets_client,
            spreadsheet_id,
            is_new_spreadsheet=is_new_spreadsheet,
            # Prepopulate a brand-new sheet with the calendar's current
            # labels; an adopted pre-existing one already has its own.
            initial_labels=self._get_calendar_labels() if is_new_spreadsheet else None,
        )

    @property
    def sheet_id(self) -> str:
        return self._event_label_sheet.spreadsheet_id

    def _get_calendar_labels(self) -> list[EventLabel]:
        raw_labels, _etag = self._calendar_client.list_event_labels()
        return [EventLabel.from_raw(label) for label in raw_labels]

    def _get_sheet_labels(self) -> list[EventLabel]:
        return self._event_label_sheet.read()

    def label_priorities(self) -> dict[str, int | None]:
        """label id -> priority, straight from the tracked sheet -- a
        read-only lookup (unlike `sync_labels`, never writes to the sheet
        or the calendar) for callers that just need to look a label's
        priority up, e.g. to fill it in on an event that doesn't have one
        of its own (see `utilities/label_priority_calendar.py`)."""
        return {label.id: label.priority for label in self._get_sheet_labels() if label.id is not None}

    def label_fixed_times(self) -> dict[str, bool | None]:
        """label id -> fixed_time, straight from the tracked sheet -- the
        `fixed_time` counterpart to `label_priorities`, read the same way
        and used the same way (see `utilities/label_priority_calendar.py`)."""
        return {
            label.id: label.fixed_time for label in self._get_sheet_labels() if label.id is not None
        }

    def sync_labels(self) -> list[EventLabel]:
        """Assume that the Sheet is the authority and sync its labels with the calendar."""
        raw_calendar_labels, etag = self._calendar_client.list_event_labels()
        sheet_labels = self._get_sheet_labels()
        raw_sheet_labels = [label.to_raw() for label in sheet_labels]

        # EventLabel (raw) isn't hashable (a plain, non-frozen dataclass),
        # so compare via astuple's plain tuples instead of set(EventLabel...).
        if {astuple(l) for l in raw_calendar_labels} != {astuple(l) for l in raw_sheet_labels}:
            new_raw_calendar_labels = self._calendar_client.replace_event_labels(raw_sheet_labels, etag)

            # Inserting into the calendar may have generated IDs that we'll
            # want to sync back to the Sheet.  The new list of IDs may not be
            # sorted the same way as the original, so do a search for event
            # labels by the other fields.
            assert len(new_raw_calendar_labels) == len(sheet_labels)
            update_sheet = False
            # Ids already claimed by one of this sheet's other rows can't
            # also be the match for a blank-id row, even if their
            # name/background_color happen to coincide -- excluding them
            # avoids treating that coincidence as ambiguity.
            known_ids = {l.id for l in sheet_labels if l.id is not None}
            for sheet_label in [l for l in sheet_labels if l.id is None]:
                raw_label = sheet_label.to_raw()
                def _remove_id(l: RawEventLabel) -> RawEventLabel:
                    return replace(l, id=None)
                matching_raw_calendar_labels = [
                    l
                    for l in new_raw_calendar_labels
                    if l.id not in known_ids and _remove_id(l) == raw_label
                ]
                if len(matching_raw_calendar_labels) == 0:
                    raise ValueError(f"Couldn't find matching event label for {sheet_label}")
                if len(matching_raw_calendar_labels) > 1:
                    # Can't tell which is which -- assigning either id
                    # could silently attach the wrong priority (or any
                    # other sheet-only field) to the wrong label.
                    raise ValueError(
                        f"Found multiple matching event labels for {sheet_label}; can't tell "
                        "which is which -- give each a unique name/background_color combination"
                    )
                sheet_label.id = matching_raw_calendar_labels[0].id
                update_sheet = True
            if update_sheet:
                self._event_label_sheet.write(sheet_labels)

        assert all(label.id is not None for label in sheet_labels)
        return sheet_labels

    def create_label(self, label: EventLabel) -> list[EventLabel]:
        self._event_label_sheet.append(label)
        return self.sync_labels()

    def update_label(self, label: EventLabel) -> list[EventLabel]:
        """Requires that label.id is filled in. The other fields may not be filled in,
        to keep the current value."""
        if not label.id:
            raise ValueError(f"Update requires label ID, but none supplied for {label}")
        sheet_labels = self._get_sheet_labels()
        if label.id not in {l.id for l in sheet_labels}:
            raise ValueError(f"Update requires valid label ID, but {label.id} is unknown")
        label_to_edit_index = next((i for i, l in enumerate(sheet_labels) if l.id == label.id), None)
        assert label_to_edit_index is not None
        for field, value in asdict(label).items():
            if field != "id" and value is not None:
                setattr(sheet_labels[label_to_edit_index], field, value)
        self._event_label_sheet.write(sheet_labels)
        return self.sync_labels()
