from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from calendar_clients.google_calendar import CalendarClient, Event, EventLabelConflictError
from config import (
    build_calendar_client,
    build_event_labels,
    get_mcp_resource_url,
    get_workos_authkit_domain,
)
from utilities.event_labels import EventLabels, EventLabel
from utilities.reallocation import ReallocationOptions
from utilities.reallocating_calendar import ReallocatingCalendar
from workos_auth import WorkOSTokenVerifier

# "stdio" (the default) is for local use -- a client spawns this process
# directly (Claude Desktop's local config, `mcp dev`). "streamable-http" is
# for hosting this remotely (e.g. on Render); see the README's "Deploying"
# section. Read once, at import time, since it also decides how MCPServer
# itself gets constructed below.
_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")

if _TRANSPORT == "streamable-http":
    _resource_url = get_mcp_resource_url()
    mcp = MCPServer(
        "time-tracking-google-calendar-mcp",
        token_verifier=WorkOSTokenVerifier(
            authkit_domain=get_workos_authkit_domain(), resource=_resource_url
        ),
        auth=AuthSettings(
            issuer_url=get_workos_authkit_domain(),
            resource_server_url=_resource_url,
            required_scopes=[],
            # False: WorkOSTokenVerifier already checks the token's audience
            # itself (via jwt.decode's audience=), so the SDK doesn't need
            # to check AccessToken.resource against resource_server_url too.
            validate_token_resource=False,
        ),
    )
else:
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
    is_cancelled (which has no Event equivalent -- Event's status is one
    of INTERNAL_EVENT_FIELDS, hidden entirely). Every MCP tool
    returns/accepts this instead of Event directly, so those fields never
    appear in the tool schema the agent sees (via tools/list) or in any
    tool result -- the agent has no way to know they exist, not just that
    their value is hidden.

    is_cancelled only ever moves from False to True: setting it False has
    no effect (see to_event), since there's no way to un-cancel a
    cancelled event."""

    id: str | None = None
    summary: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    description: str | None = None
    location: str | None = None
    min_duration: timedelta | None = None
    is_fixed_duration: bool | None = None
    priority: int | None = None
    event_label_id: str | None = None
    is_cancelled: bool = False

    @classmethod
    def from_event(cls, event: Event) -> "PublicEvent":
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
            event_label_id=event.event_label_id,
            is_cancelled=event.status == "cancelled",
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
            event_label_id=self.event_label_id,
            priority=self.priority,
            status="cancelled" if self.is_cancelled else None,
        )


_calendar_client: CalendarClient | None = None
_reallocating_calendar: ReallocatingCalendar | None = None
_event_labels: EventLabels | None = None


def get_calendar_client() -> CalendarClient:
    """Lazily construct and cache the CalendarClient, so credential loading
    (and the OAuth consent flow, on first run) happens once per process
    rather than on every tool call."""
    global _calendar_client
    if _calendar_client is None:
        _calendar_client = build_calendar_client()
    return _calendar_client


def get_reallocating_calendar() -> ReallocatingCalendar:
    """Lazily construct and cache the ReallocatingCalendar wrapping
    `get_calendar_client()`, the same way get_calendar_client itself
    caches its CalendarClient."""
    global _reallocating_calendar
    if _reallocating_calendar is None:
        _reallocating_calendar = ReallocatingCalendar(get_calendar_client())
    return _reallocating_calendar


def get_calendar_with_event_labels() -> EventLabels:
    """Lazily construct and cache the EventLabels, the same way
    get_calendar_client/get_reallocating_calendar cache theirs."""
    global _event_labels
    if _event_labels is None:
        _event_labels = build_event_labels()
    return _event_labels


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
    """Update an existing event, reallocating time from the rest of its
    day as needed to make room for its new position. Returns the events
    affected by the update."""
    updated_event = event.to_event()
    try:
        applied = get_reallocating_calendar().update_event(updated_event, ReallocationOptions())
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return [PublicEvent.from_event(e) for e in applied]


@mcp.tool()
def create_event(event: PublicEvent) -> list[PublicEvent]:
    """Create a new event. Returns the events affected by the creation."""
    new_event = event.to_event()
    try:
        applied = get_reallocating_calendar().create_event(new_event, ReallocationOptions())
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return [PublicEvent.from_event(e) for e in applied]


@mcp.tool()
def delete_event(id: str) -> list[PublicEvent]:
    """Delete an event by its ID. Returns the events affected by the deletion."""
    cancelled = get_calendar_client().update_event(Event(id=id, status="cancelled"))
    return [PublicEvent.from_event(cancelled)]


@mcp.tool()
def create_event_label(
    label: EventLabel
) -> list[EventLabel]:
    """Create a new event label with the given optional name and
    priority. Returns the resulting list of every event label -- there's
    no separate way to list labels; use this, update_event_label, or
    sync_event_labels_from_sheet to see the current ones."""
    try:
        return get_calendar_with_event_labels().create_label(label)
    except (ValueError, EventLabelConflictError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def update_event_label(label: EventLabel) -> list[EventLabel]:
    """Update an existing event label's background color, name, and/or
    priority. Any omitted properties keep their current value. Returns
    the resulting list of every event label -- see create_event_label."""
    try:
        return get_calendar_with_event_labels().update_label(label)
    except (ValueError, EventLabelConflictError) as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def sync_event_labels_from_sheet() -> list[EventLabel]:
    """Make this calendar's event labels match its tracked event label
    sheet exactly: rows with a blank ID become new labels, rows with a
    matching ID overwrite that label's name/color/priority, and any
    label with no matching row is deleted. Returns the resulting labels
    -- call this with no sheet changes pending to just see the current
    ones; there's no separate list tool. Fails if no event label sheet
    is tracked on this calendar -- that's
    a one-time, human-run bootstrap step (see create_calendar.py), not
    something this server can do on its own."""
    try:
        return get_calendar_with_event_labels().sync_labels()
    except (ValueError, EventLabelConflictError) as exc:
        raise ToolError(str(exc)) from exc


if __name__ == "__main__":
    if _TRANSPORT == "streamable-http":
        # Every Render web service must bind 0.0.0.0 and the $PORT it
        # assigns (default 10000 locally, to match Render's own default).
        mcp.run(
            transport="streamable-http",
            host="0.0.0.0",
            port=int(os.environ.get("PORT", 10000)),
        )
    else:
        mcp.run()
