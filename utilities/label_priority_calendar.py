"""Fills in an event's label_priority and label_is_fixed_time from its
event label when reading it.

calendar_clients/google_calendar.py's CalendarClient is a thin, pure
wrapper around the Google Calendar API -- Event.priority, Event.
is_fixed_time, and Event.event_label_id are just unrelated fields to it.
utilities/event_labels.py's EventLabels is the layer that actually knows
an event label's priority and fixed_time (both sourced from a synced
Google Sheet -- see that module). LabelPriorityCalendar here is the glue
between them: whenever it reads an event (list_events/get_event) that has
an event_label_id, it fills in that label's priority/fixed_time as the
event's label_priority/label_is_fixed_time. Reallocation (utilities/
reallocation.py, via utilities/reallocating_calendar.py's
ReallocatingCalendar) reads Event.effective_priority/
effective_is_fixed_time, so it naturally treats an event label as the
source of an event's priority/fixed-time-ness when the event itself
doesn't set one -- while the event's own priority/is_fixed_time stay
untouched, so writing it back never copies its label's values onto it.

Application-level policy, not API integration, which is why it doesn't
live on CalendarClient itself -- the same relationship
ReallocatingCalendar has with CalendarClient.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from calendar_clients.google_calendar import CalendarClient, Event, EventLabel
from utilities.event_labels import EventLabels


class LabelPriorityCalendar:
    """Wraps a CalendarClient with an EventLabels, filling in an event's
    label_priority and label_is_fixed_time from its event label
    (Event.event_label_id) whenever it's read (list_events/get_event).
    The event's own priority/is_fixed_time are never touched, and still
    win over the label's (see Event.effective_priority/
    effective_is_fixed_time). create_event/update_event/list_event_labels
    are passed straight through unchanged; Event.to_api_body never sends
    the label_* fields."""

    def __init__(self, client: CalendarClient, event_labels: EventLabels) -> None:
        self._client = client
        self._event_labels = event_labels

    def list_events(self, time_min: datetime, time_max: datetime) -> list[Event]:
        return fill_in_from_labels(self._client.list_events(time_min, time_max), self._event_labels)

    def get_event(self, event_id: str) -> Event:
        return fill_in_from_labels([self._client.get_event(event_id)], self._event_labels)[0]

    def create_event(self, event: Event) -> Event:
        return self._client.create_event(event)

    def update_event(self, event: Event) -> Event:
        return self._client.update_event(event)

    def list_event_labels(self) -> tuple[list[EventLabel], str]:
        return self._client.list_event_labels()


def fill_in_from_labels(events: list[Event], event_labels: EventLabels) -> list[Event]:
    """`events`, each with its label's priority/fixed_time filled in as
    label_priority/label_is_fixed_time, the way LabelPriorityCalendar fills
    them in on read -- for callers
    (server.py's event tools) that already have events read some other
    way. Never mutates `events` themselves."""
    priorities = event_labels.label_priorities()
    fixed_times = event_labels.label_fixed_times()
    return [_fill_in(event, priorities, fixed_times) for event in events]


def _fill_in(
    event: Event, priorities: dict[str, int | None], fixed_times: dict[str, bool | None]
) -> Event:
    if event.event_label_id is None:
        return event
    return replace(
        event,
        label_priority=priorities.get(event.event_label_id),
        label_is_fixed_time=fixed_times.get(event.event_label_id),
    )
