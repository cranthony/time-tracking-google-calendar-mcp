"""Reallocation-aware create/update, layered on top of a plain CalendarClient.

`calendar_clients/google_calendar.py`'s `CalendarClient` is a thin, pure
wrapper around the Google Calendar API -- it has no idea what reallocation
is. `utilities/reallocation.py`'s `reallocate_for_new_event` is a pure
algorithm -- it has no idea what a `CalendarClient` is. `ReallocatingCalendar`
here is the glue between them: it's application-level policy (how creating
or moving an event makes room for itself on a real, live calendar), not
Calendar API integration, which is why it doesn't live on `CalendarClient`
itself.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from calendar_clients.google_calendar import Event
from utilities.reallocation import ReallocationOptions, reallocate_for_new_event


class _EventCalendar(Protocol):
    """The subset of `CalendarClient`'s interface `ReallocatingCalendar`
    actually needs -- so `utilities/label_priority_calendar.py`'s
    `LabelPriorityCalendar` (or any other `CalendarClient`-shaped wrapper)
    can stand in for a plain `CalendarClient` without `ReallocatingCalendar`
    needing to know the difference."""

    def list_events(self, time_min: datetime, time_max: datetime) -> list[Event]: ...
    def get_event(self, event_id: str) -> Event: ...
    def create_event(self, event: Event) -> Event: ...
    def update_event(self, event: Event) -> Event: ...


class ReallocatingCalendar:
    """Wraps a `CalendarClient` (or `LabelPriorityCalendar`) with
    reallocation-aware `create_event`/`update_event` -- see the module
    docstring."""

    def __init__(self, client: _EventCalendar) -> None:
        self._client = client

    def list_day_events(self, start: datetime, ignore_id: str | None = None) -> list[Event]:
        """The events reallocation should treat as `start`'s "day": everything
        from `start` through roughly 24 hours later, truncated after the
        first `is_end_of_day_sleep` event found (if any) -- see
        utilities/reallocation.py's "The day".

        `ignore_id`: an event id to skip when deciding where the day ends
        -- but still included in the returned list otherwise. For
        `update_event` below, updating an event that's itself the day's
        own `is_end_of_day_sleep` marker (e.g. stretching a morning sleep
        block later): truncating against its own not-yet-applied prior
        position would cut off the rest of the day before `update_event`
        gets a chance to exclude it itself.
        """
        events = self._client.list_events(start, start + timedelta(hours=24))
        sleep_index = next(
            (
                i
                for i, event in enumerate(events)
                if event.is_end_of_day_sleep and event.id != ignore_id
            ),
            None,
        )
        if sleep_index is not None:
            events = events[: sleep_index + 1]
        return events

    def create_event(self, new_event: Event, options: ReallocationOptions) -> list[Event]:
        """Create `new_event`, reallocating time from `list_day_events
        (new_event.start)` as needed to make room for it (see
        utilities/reallocation.py). Returns every `Event` created or
        updated as a result -- `new_event` itself, plus whatever else
        reallocation touched (shrunk, moved, split, or cancelled) to make
        room -- each as the API's own response to creating/patching it.
        """
        if new_event.start is None or new_event.end is None:
            raise ValueError("new_event.start and new_event.end are required to create an event")
        day_events = self.list_day_events(new_event.start)
        return self._apply_reallocation(day_events, new_event, options)

    def update_event(self, updated_event: Event, options: ReallocationOptions) -> list[Event]:
        """Update `updated_event` (must already have an `id`) at its new
        `start`/`end`, reallocating time from the rest of its day as
        needed to make room -- the same as `create_event`, but for
        moving/resizing an event that already exists instead of creating a
        new one. `updated_event`'s own prior position is excluded from
        `day_events` first, since `reallocate_for_new_event` requires that
        a moved event not already appear in `day_events` -- see its
        docstring.

        `updated_event.start`/`.end` may be given individually -- either
        may be left `None` to mean "keep this event's current value". At
        least one of the two must be given, since reallocation needs a
        real span to make room for. Whichever is missing is filled in from
        `list_day_events`'s own result below (the same call already made
        for reallocation -- no second fetch) if this event is in it,
        falling back to a direct `get_event` only if it isn't (e.g. the
        one given value put it on a different day than its prior
        position).
        """
        if updated_event.id is None:
            raise ValueError("updated_event.id is required to update an event with reallocation")
        if updated_event.start is None and updated_event.end is None:
            raise ValueError(
                "updated_event.start and/or updated_event.end are required to update an "
                "event with reallocation"
            )

        day_events = self.list_day_events(
            updated_event.start or updated_event.end, ignore_id=updated_event.id
        )

        if updated_event.start is None or updated_event.end is None:
            current = next(
                (event for event in day_events if event.id == updated_event.id),
                None,
            ) or self._client.get_event(updated_event.id)
            if updated_event.start is None:
                updated_event.start = current.start
            if updated_event.end is None:
                updated_event.end = current.end

        day_events = [event for event in day_events if event.id != updated_event.id]
        return self._apply_reallocation(day_events, updated_event, options)

    def _apply_reallocation(
        self, day_events: list[Event], event: Event, options: ReallocationOptions
    ) -> list[Event]:
        plan = reallocate_for_new_event(day_events, event, options)
        return [
            self._client.create_event(planned)
            if planned.id is None
            else self._client.update_event(planned)
            for planned in plan
        ]
