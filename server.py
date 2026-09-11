from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from calendar_clients.google_calendar import CalendarClient, Event
from config import build_calendar_client
from utilities.reallocation import (
    ReallocationConflictError,
    ReallocationOptions,
    ReallocationShortfallError,
)

mcp = MCPServer("time-tracking-google-calendar-mcp")

INTERNAL_EVENT_FIELDS = frozenset({"is_end_of_day_sleep", "status", "recurring_event_id"})
"""Event fields the agent talking to this server should never see or set,
at all -- not just left null. Enforced by PublicEvent actually lacking
these fields (so they never appear in a tool's schema or result), not by
convention -- see PublicEvent below. tests/test_server.py's
TestPublicEvent asserts these are exactly the fields PublicEvent is
missing relative to Event, so this stays in sync with PublicEvent."""


@dataclass(kw_only=True)
class PublicEvent:
    """Event, minus the fields named in INTERNAL_EVENT_FIELDS, plus
    is_canceled (which has no Event equivalent -- Event's status is one of
    INTERNAL_EVENT_FIELDS, hidden entirely). Every MCP tool returns/accepts
    this instead of Event directly, so those fields never appear in the
    tool schema the agent sees (via tools/list) or in any tool result --
    the agent has no way to know they exist, not just that their value is
    hidden.

    is_canceled only ever moves from False to True: setting it False has
    no effect (see to_event), since there's no way to un-cancel a
    cancelled event. When an event is cancelled, every other field on its
    PublicEvent is None -- id and is_canceled are the only ones a caller
    can rely on."""

    id: str | None = None
    summary: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    description: str | None = None
    location: str | None = None
    min_duration: timedelta | None = None
    is_fixed_duration: bool | None = None
    priority: int | None = None
    is_canceled: bool = False

    @classmethod
    def from_event(cls, event: Event) -> "PublicEvent":
        if event.status == "cancelled":
            return cls(id=event.id, is_canceled=True)
        return cls(
            id=event.id,
            summary=event.summary,
            start=event.start,
            end=event.end,
            description=event.description,
            location=event.location,
            min_duration=event.min_duration,
            is_fixed_duration=event.is_fixed_duration,
            priority=event.priority,
        )

    def to_event(self) -> Event:
        return Event(
            id=self.id,
            summary=self.summary,
            start=self.start,
            end=self.end,
            description=self.description,
            location=self.location,
            min_duration=self.min_duration,
            is_fixed_duration=self.is_fixed_duration,
            priority=self.priority,
            status="cancelled" if self.is_canceled else None,
        )


_calendar_client: CalendarClient | None = None


def get_calendar_client() -> CalendarClient:
    """Lazily construct and cache the CalendarClient, so credential loading
    (and the OAuth consent flow, on first run) happens once per process
    rather than on every tool call."""
    global _calendar_client
    if _calendar_client is None:
        _calendar_client = build_calendar_client()
    return _calendar_client


@mcp.tool()
def list_events(min_time: datetime, max_time: datetime) -> list[PublicEvent]:
    """List events between min_time and max_time."""
    events = get_calendar_client().list_events(min_time, max_time)
    return [PublicEvent.from_event(event) for event in events if event.status != "cancelled"]


@mcp.tool()
def get_event(id: str) -> PublicEvent:
    """Get a single event by its ID."""
    event = get_calendar_client().get_event(id)
    if event.status == "cancelled":
        raise ToolError(f"Event {id} has been cancelled.")
    return PublicEvent.from_event(event)


@mcp.tool()
def update_event(event: PublicEvent) -> list[PublicEvent]:
    """Update an existing event. Returns the events affected by the update."""
    raise NotImplementedError


@mcp.tool()
def create_event(event: PublicEvent) -> list[PublicEvent]:
    """Create a new event. Returns the events affected by the creation."""
    new_event = event.to_event()
    try:
        applied = get_calendar_client().create_event_with_reallocation(
            new_event, ReallocationOptions()
        )
    except (ReallocationConflictError, ReallocationShortfallError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    return [PublicEvent.from_event(e) for e in applied]


@mcp.tool()
def delete_event(id: str) -> list[PublicEvent]:
    """Delete an event by its ID. Returns the events affected by the deletion."""
    raise NotImplementedError


if __name__ == "__main__":
    mcp.run()
