"""Fills in an event's priority from its event label when reading it.

calendar_clients/google_calendar.py's CalendarClient is a thin, pure
wrapper around the Google Calendar API -- Event.priority and
Event.event_label_id are just two unrelated fields to it. utilities/
event_labels.py's EventLabels is the layer that actually knows an event
label's priority (sourced from a synced Google Sheet -- see that module).
LabelPriorityCalendar here is the glue between them: whenever it reads an
event (list_events/get_event) that has an event_label_id but no explicit
priority of its own, it fills one in from that label's priority -- so
reallocation (utilities/reallocation.py, via utilities/
reallocating_calendar.py's ReallocatingCalendar) naturally treats an event
label as the source of an event's priority when the event itself doesn't
set one.

Application-level policy, not API integration, which is why it doesn't
live on CalendarClient itself -- the same relationship
ReallocatingCalendar has with CalendarClient.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from calendar_clients.google_calendar import CalendarClient, Event
from utilities.event_labels import EventLabels


class LabelPriorityCalendar:
    """Wraps a CalendarClient with an EventLabels, filling in an event's
    priority from its event label (Event.event_label_id) whenever it's
    read (list_events/get_event) with no explicit priority of its own --
    the event's own priority always wins when it has one. create_event/
    update_event are passed straight through unchanged; it's the caller's
    job to decide what priority (if any) to set when writing an event."""

    def __init__(self, client: CalendarClient, event_labels: EventLabels) -> None:
        self._client = client
        self._event_labels = event_labels

    def list_events(self, time_min: datetime, time_max: datetime) -> list[Event]:
        events = self._client.list_events(time_min, time_max)
        priorities = self._event_labels.label_priorities()
        return [self._fill_in_priority(event, priorities) for event in events]

    def get_event(self, event_id: str) -> Event:
        event = self._client.get_event(event_id)
        return self._fill_in_priority(event, self._event_labels.label_priorities())

    def create_event(self, event: Event) -> Event:
        return self._client.create_event(event)

    def update_event(self, event: Event) -> Event:
        return self._client.update_event(event)

    @staticmethod
    def _fill_in_priority(event: Event, priorities: dict[str, int | None]) -> Event:
        if event.priority is not None or event.event_label_id is None:
            return event
        priority = priorities.get(event.event_label_id)
        if priority is None:
            return event
        return replace(event, priority=priority)
