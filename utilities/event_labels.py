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

from dataclasses import dataclass, replace

from calendar_clients.google_calendar import CalendarClient
from calendar_clients.google_calendar import EventLabel as RawEventLabel
from calendar_clients.google_calendar import color_for_priority
from utilities.event_label_sheet import DEFAULT_SHEET_TITLE, EventLabelSheet


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


def _from_raw(label: RawEventLabel) -> EventLabel:
    return EventLabel(id=label.id, name=label.name, background_color=label.background_color)


def _to_raw(label: EventLabel) -> RawEventLabel:
    background_color = label.background_color
    if background_color is None:
        background_color = color_for_priority(label.priority)[1]
    return RawEventLabel(id=label.id, name=label.name, background_color=background_color)


class EventLabels:
    """Manages a calendar's event labels as `EventLabel` objects --
    `CalendarClient`'s raw labels, enriched with a priority sourced from
    an `EventLabelSheet` -- see the module docstring.
    """

    def __init__(self, calendar_client: CalendarClient, event_label_sheet: EventLabelSheet) -> None:
        self._calendar_client = calendar_client
        self._event_label_sheet = event_label_sheet

    def list_labels(self) -> list[EventLabel]:
        """Every custom event label on this calendar, with `priority`
        filled in from the tracked event label sheet's matching row (see
        `EventLabelSheet.find_sheet`) when there is one. A label keeps
        `priority=None` if it has no matching row, or if no sheet has
        been created at all."""
        labels = [_from_raw(label) for label in self._calendar_client.list_event_labels()]
        spreadsheet_id = self._event_label_sheet.find_sheet()
        if spreadsheet_id is None:
            return labels
        rows = self._event_label_sheet.read_rows(spreadsheet_id)
        priority_by_id = {
            row_label.id: row_label.priority
            for row_label in (_label_from_row(row) for row in rows)
            if row_label.id is not None and row_label.priority is not None
        }
        return [
            replace(label, priority=priority_by_id[label.id]) if label.id in priority_by_id else label
            for label in labels
        ]

    def create_label(
        self, background_color: str | None = None, name: str | None = None, priority: int | None = None
    ) -> EventLabel:
        """Create a new event label. `background_color` is a hex string
        (e.g. "#8e24aa"); if omitted, it's derived from `priority`
        instead. `priority` is not itself persisted (Calendar has no
        field for it) -- it only affects the color this call resolves
        to; sync an event label sheet if you want it remembered."""
        resolved_color = background_color if background_color is not None else color_for_priority(priority)[1]
        raw = self._calendar_client.create_event_label(resolved_color, name)
        return replace(_from_raw(raw), priority=priority)

    def update_label(
        self,
        label_id: str,
        *,
        background_color: str | None = None,
        name: str | None = None,
        priority: int | None = None,
    ) -> EventLabel:
        """Update an existing event label's `background_color` and/or
        `name` -- whichever is left `None` keeps its current value.
        `priority` (if given, and `background_color` is not) recolors
        the label using the color derived from that priority, the same
        as a freshly-created label would get. `priority` isn't itself
        persisted: it only affects the color this specific call
        resolves to. Raises `ValueError` if no label with `label_id`
        exists."""
        resolved_color = background_color
        if resolved_color is None and priority is not None:
            resolved_color = color_for_priority(priority)[1]
        raw = self._calendar_client.update_event_label(label_id, background_color=resolved_color, name=name)
        return replace(_from_raw(raw), priority=priority)

    def delete_label(self, label_id: str) -> EventLabel:
        """Delete an event label by its id. Returns the label as it was
        just before deletion. Raises `ValueError` if no label with
        `label_id` exists."""
        return _from_raw(self._calendar_client.delete_event_label(label_id))

    def create_sheet(self, title: str = DEFAULT_SHEET_TITLE) -> str:
        """Create a new event label sheet, pre-populated with this
        calendar's *current* labels (no Priority column values --
        Calendar doesn't know any) so that syncing it back immediately
        afterward, with no edits, is a no-op rather than deleting every
        label that already exists (see `sync_from_sheet`). Returns the
        new spreadsheet's id. Raises `ValueError` if this calendar
        already has an event label sheet tracked -- see
        `EventLabelSheet.create_sheet`."""
        labels = [_from_raw(label) for label in self._calendar_client.list_event_labels()]
        initial_rows = [_row_from_label(label) for label in labels] or None
        return self._event_label_sheet.create_sheet(title, initial_rows)

    def sync_from_sheet(self) -> list[EventLabel]:
        """Make this calendar's event labels match the event label sheet
        tracked on it (see `EventLabelSheet.find_sheet`) exactly:

        - A row with a blank ID becomes a newly-created label; that row's
          ID cell is then filled in with the real, Google-assigned ID, so
          syncing again doesn't create a duplicate.
        - A row whose ID matches an existing label fully overwrites that
          label's name, background color, and priority (not merged with
          whatever it had before -- see `CalendarClient.replace_event_
          labels`).
        - Any existing label whose ID isn't present in the sheet at all
          is deleted.

        Raises `ValueError` if no event label sheet is tracked on this
        calendar (see `EventLabelSheet.resolve_sheet_id`) -- call
        `create_sheet` first. Returns the resulting labels, each with
        `priority` populated from its row."""
        spreadsheet_id = self._event_label_sheet.resolve_sheet_id()
        rows = self._event_label_sheet.read_rows(spreadsheet_id)
        desired_labels = [_label_from_row(row) for row in rows]
        updated_raw = self._calendar_client.replace_event_labels([_to_raw(label) for label in desired_labels])
        # updated_raw comes back from Calendar with no priority (Calendar
        # has no field for it) -- so take each row's real, possibly
        # newly-assigned id from there, but keep name/background_color/
        # priority as this sheet already had them (replace_event_labels
        # wrote exactly what we asked for).
        merged_labels = [
            replace(desired, id=updated.id) for desired, updated in zip(desired_labels, updated_raw)
        ]
        self._event_label_sheet.write_rows(
            spreadsheet_id, [_row_from_label(label) for label in merged_labels]
        )
        return merged_labels
