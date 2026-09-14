"""The logical event label: a calendar's raw label (calendar_clients/
google_calendar.py's CalendarClient/EventLabel) plus a priority sourced from
a synced Google Sheet (utilities/event_label_sheet.py's EventLabelSheet).

Google Calendar's own label resource has no field for priority at all (see
that module's EventLabel), so it lives only in the sheet. EventLabels here is
the glue between the two: it's application-level policy (how a raw label and
a sheet row combine into one EventLabel, and how creating/updating/syncing
should resolve a color from a priority), not API integration, which is why it
doesn't live on either CalendarClient or EventLabelSheet -- the same
relationship utilities/reallocating_calendar.py's ReallocatingCalendar has
with CalendarClient.

This module's EventLabel is a different class from calendar_clients/
google_calendar.py's EventLabel (same name, deliberately -- one is the "raw"
label, this is the richer one everything except raw_label CLI commands
should use), with `background_color` optional (derived from `priority` via
`color_for_priority` when left unset) and `priority` itself, neither of which
the raw one has.
"""

from __future__ import annotations

from dataclasses import asdict, astuple

from calendar_clients.google_calendar import CalendarClient
from calendar_clients.google_sheets import SheetsClient
from utilities.event_label_sheet import EventLabelSheet, EventLabel

_SHEET_ID_METADATA_KEY = "event-label-sheet-id"
"""The CalendarClient.get_calendar_metadata/set_calendar_metadata key this
app stores the event label sheet's spreadsheet id under -- on the
calendar itself, rather than something found by searching Drive, so
find_sheet is a single deterministic lookup instead of a "most recently
modified" guess among however many sheets this app has created."""



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

        event_label_sheet_id = self._calendar_client.get_calendar_metadata(_SHEET_ID_METADATA_KEY)
        if event_label_sheet_id is None:
            # Create the initial event label sheet, and prepopulate it with the
            # current set of event labels.
            event_label_sheet_id = EventLabelSheet.create(
                sheets_client,
                initial_labels=self._get_calendar_labels(),
            )
            self._calendar_client.set_calendar_metadata(_SHEET_ID_METADATA_KEY, event_label_sheet_id)

        self._event_label_sheet = EventLabelSheet(sheets_client, event_label_sheet_id)

    @property
    def sheet_id(self) -> str:
        return self._event_label_sheet.spreadsheet_id

    def _get_calendar_labels(self) -> list[EventLabel]:
        raw_labels, _etag = self._calendar_client.list_event_labels()
        return [EventLabel.from_raw(label) for label in raw_labels]

    def _get_sheet_labels(self) -> list[EventLabel]:
        return self._event_label_sheet.read()

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
            # want to sync back to the Sheet.
            assert len(new_raw_calendar_labels) == len(sheet_labels)
            update_sheet = False
            for i, raw_calendar_label in enumerate(new_raw_calendar_labels):
                if sheet_labels[i].id is None:
                    sheet_labels[i].id = raw_calendar_label.id
                    update_sheet = True
            if update_sheet:
                self._event_label_sheet.write(sheet_labels)

        assert all(label.id is not None for label in sheet_labels)
        return sheet_labels

    def list_labels(self) -> list[EventLabel]:
        """Because of the way this class works, it somewhat counterintuitively updates the
        labels when you list them."""
        return self.sync_labels()

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
