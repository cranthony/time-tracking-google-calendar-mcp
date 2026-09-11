from __future__ import annotations

from datetime import datetime

from mcp.server.mcpserver import MCPServer

from calendar_clients.google_calendar import CalendarClient, Event
from config import build_calendar_client

mcp = MCPServer("time-tracking-google-calendar-mcp")

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
def list_events(min_time: datetime, max_time: datetime) -> list[Event]:
    """List events between min_time and max_time."""
    return get_calendar_client().list_events(min_time, max_time)


@mcp.tool()
def get_event(id: str) -> Event:
    """Get a single event by its ID."""
    return get_calendar_client().get_event(id)


@mcp.tool()
def update_event(event: Event) -> list[Event]:
    """Update an existing event. Returns the events affected by the update."""
    raise NotImplementedError


@mcp.tool()
def create_event(event: Event) -> list[Event]:
    """Create a new event. Returns the events affected by the creation."""
    raise NotImplementedError


@mcp.tool()
def delete_event(id: str) -> list[Event]:
    """Delete an event by its ID. Returns the events affected by the deletion."""
    raise NotImplementedError


if __name__ == "__main__":
    mcp.run()
