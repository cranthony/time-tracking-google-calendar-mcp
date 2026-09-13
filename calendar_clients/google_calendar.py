from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

SCOPES = ["https://www.googleapis.com/auth/calendar.app.created"]
"""This app requests calendar.app.created, not the broader
calendar.events/calendar.events.owned scopes. This means that the app can only
read and write events on calendars that it has created; it has no access to the
user's existing calendars, including their "primary" calendar.

This is deliberate: a compromised or misbehaving instance of this app cannot
read or touch anything outside the dedicated calendar(s) it made for itself.

Run create_calendar.py (at the project root) to create that dedicated calendar
and get the ID for the GOOGLE_CALENDAR_ID environment variable. See the
README's "Calendar access model" section.

See https://developers.google.com/workspace/calendar/api/auth for the scope
reference."""

_APP_EXTENDED_PROPERTY_KEY_PREFIX = "cascading-time-tracker-"
"""Prefix for the extendedProperties.private keys this app uses to store its
own per-event fields, distinguishing them from any other private key that
might exist on an event."""

logger = logging.getLogger(__name__)


def _clamp(value: int, lower: int, upper: int) -> int:
    return min(upper, max(lower, value))


_PRIORITY_COLOR_IDS: dict[int, str | None] = {
    0: "8",   # Graphite (gray)
    1: "5",   # Banana (yellow)
    2: None,  # The default calendar color
    3: "2",   # Sage (soft green)
}


def _color_id_for_priority(priority: int) -> str | None:
    """Priorities are colored with the colorId field, to make them easily
    visible on the calendar.  The colorId field is restricted to a fixed set
    of 11 colors.

    Note that event labels unlock the ability to specify our own colors.  The
    priority field doesn't use this feature because we intend to use it for
    a different categorization feature.  An event label's color supersedes a
    color ID."""
    return _PRIORITY_COLOR_IDS.get(_clamp(priority, 0, 3))


_PRIORITY_LABEL_COLORS: dict[int, str] = {
    0: "#e1e1e1",  # Graphite (gray) -- same swatch as colorId "8"
    1: "#fbd75b",  # Banana (yellow) -- same swatch as colorId "5"
    3: "#7ae7bf",  # Sage (soft green) -- same swatch as colorId "2"
}
"""Hex `background_color` `EventLabel.to_api_body` derives from
`priority` when none is given explicitly -- the same priority-color
scheme `_color_id_for_priority` uses for `Event.colorId`, expressed as
hex since a label can't reference the fixed colorId palette. No entry
for priority 2: `_color_id_for_priority` leaves that priority uncolored
(the calendar's own default), but a label has no equivalent "leave it
unset" option, so `EventLabel.to_api_body` raises `ValueError` if
`background_color` is still missing after this lookup."""


def _label_color_for_priority(priority: int) -> str | None:
    return _PRIORITY_LABEL_COLORS.get(_clamp(priority, 0, 3))


@dataclass(kw_only=True)
class Event:
    """A calendar event, decoupled from the Google API's raw resource shape.

    See the Google Calendar API Events resource reference:
    https://developers.google.com/calendar/api/v3/reference/events
    """

    id: str | None = None
    """Uniquely identifies the event. `None` until the event has been
    created; `CalendarClient.create_event` assigns this ID (from the API
    response) when the event is created.
    See https://developers.google.com/workspace/calendar/api/v3/reference/events#id
    for more information."""

    summary: str | None = None
    """Required to create an event. `None` is only valid for a partial
    update payload that doesn't touch summary — see `CalendarClient.update_event`.
    See https://developers.google.com/workspace/calendar/api/v3/reference/events#summary
    for more information."""

    start: datetime | None = None
    """Required to create an event. `None` is only valid for a partial
    update payload that doesn't touch start — see `CalendarClient.update_event`.
    See https://developers.google.com/workspace/calendar/api/v3/reference/events#start
    for more information."""

    end: datetime | None = None
    """Required to create an event. `None` is only valid for a partial
    update payload that doesn't touch end — see `CalendarClient.update_event`.
    See https://developers.google.com/workspace/calendar/api/v3/reference/events#end
    for more information."""

    description: str | None = None
    """Optional free-text description of the event. Can include HTML.
    See https://developers.google.com/workspace/calendar/api/v3/reference/events#description
    for more information."""

    location: str | None = None
    """Optional free-text location of the event.
    See https://developers.google.com/workspace/calendar/api/v3/reference/events#location
    for more information."""

    status: str | None = None
    """One of "confirmed", "tentative", or "cancelled". A cancelled event
    isn't removed from a calendar's results — it's returned with this
    status. An instance of a recurring event should never be deleted --
    set its status to "cancelled" instead.
    See https://developers.google.com/workspace/calendar/api/v3/reference/events#status
    for more information."""

    recurring_event_id: str | None = None
    """For an instance of a recurring event, the id of that series' master
    event. Read-only: assigned by Google when the event is created as part
    of a series, never sent to the API — see `to_api_body`.
    See https://developers.google.com/workspace/calendar/api/v3/reference/events#recurringEventId
    for more information."""

    min_duration: timedelta | None = None
    """The minimum duration this event may be shrunk to (e.g. by whatever
    resolves overlaps between events)."""

    is_fixed_duration: bool | None = None
    """If true, then we shouldn't change the duration of this event."""

    priority: int | None = None
    """This event's priority; lower values are higher priority. Also
    determines the event's `colorId` -- see `to_api_body` and
    `_PRIORITY_COLOR_IDS`."""

    is_end_of_day_sleep: bool | None = None
    """If true, this event is the user's end-of-day sleep block. A marker
    for identifying that event specifically (e.g. among reallocation
    candidates), independent of whatever `priority` it's also given."""

    @classmethod
    def from_api(cls, data: dict) -> "Event":
        private_properties = data.get("extendedProperties", {}).get("private", {})
        app_properties = _parse_properties(
            private_properties,
            {
                "min_duration": lambda s: timedelta(minutes=int(s)),
                "is_fixed_duration": lambda s: s.lower() == "true",
                "priority": int,
                "is_end_of_day_sleep": lambda s: s.lower() == "true",
            },
        )
        start = _parse_datetime(data["start"])
        end = _parse_datetime(data["end"])
        if app_properties.get("is_fixed_duration"):
            # A fixed-duration event may never be shrunk, so its
            # min_duration is its own full duration -- not whatever was
            # separately stored (or not) in extendedProperties.
            app_properties["min_duration"] = end - start
        return cls(
            id=data.get("id"),
            summary=data.get("summary"),
            start=start,
            end=end,
            description=data.get("description"),
            location=data.get("location"),
            status=data.get("status"),
            recurring_event_id=data.get("recurringEventId"),
            **app_properties,
        )

    def clone(self) -> "Event":
        """A copy of this event, safe to mutate independently -- e.g. to
        represent a split-off "(continued)" event during reallocation."""
        return replace(self)

    def to_api_body(self) -> dict:
        body: dict = {}
        if self.summary is not None:
            body["summary"] = self.summary
        if self.start is not None:
            body["start"] = _format_datetime(self.start)
        if self.end is not None:
            body["end"] = _format_datetime(self.end)
        if self.description is not None:
            body["description"] = self.description
        if self.location is not None:
            body["location"] = self.location
        if self.status is not None:
            body["status"] = self.status
        if self.priority is not None:
            # Color each event according to its priority.  Note that this
            # might be a partial update, in which case a missing priority
            # should mean "leave the color the same".
            body["colorId"] = _color_id_for_priority(self.priority)
        # recurring_event_id is deliberately never sent: it's assigned by
        # Google, not something a client sets.

        private_properties = _format_properties(
            self,
            {
                "min_duration": lambda d: str(int(d.total_seconds() / 60)),
                "is_fixed_duration": lambda b: "true" if b else "false",
                "priority": str,
                "is_end_of_day_sleep": lambda b: "true" if b else "false",
            },
        )
        if private_properties:
            body["extendedProperties"] = {"private": private_properties}

        return body


@dataclass(kw_only=True)
class Calendar:
    """A Google Calendar, decoupled from the API's raw resource shape.

    Because this app requests the calendar.app.created scope (see SCOPES
    above), it only ever sees/creates calendars of this kind that it made
    itself — never the user's existing calendars.
    See https://developers.google.com/workspace/calendar/api/v3/reference/calendars
    and https://developers.google.com/workspace/calendar/api/v3/reference/calendarList
    """

    id: str | None = None
    """Uniquely identifies the calendar. `None` until the calendar has been
    created; create_calendar.py assigns this ID (from the API response)
    when the calendar is created. This is the value to use as
    GOOGLE_CALENDAR_ID once you have one."""

    summary: str
    """The calendar's display name/title."""

    description: str | None = None
    """Optional free-text description of the calendar."""

    @classmethod
    def from_api(cls, data: dict) -> "Calendar":
        return cls(
            id=data.get("id"),
            summary=data.get("summary", ""),
            description=data.get("description"),
        )

    def to_api_body(self) -> dict:
        body: dict = {"summary": self.summary}
        if self.description is not None:
            body["description"] = self.description
        return body


class EventLabelConflictError(Exception):
    """Raised by CalendarClient.create_event_label/update_event_label/
    delete_event_label when another writer changed the calendar's event
    labels in between this call's read and its write. Each of those
    methods reads the full label list, mutates it in memory, and writes
    the whole thing back (see _patch_event_labels) -- guarded by the
    calendar's `etag` (an `If-Match` precondition) so a lost update fails
    loudly instead of silently overwriting the other writer's change."""


_PRIORITY_NAME_PREFIX_RE = re.compile(r"^P(-?\d+)(?: (.*))?$")
"""Matches the f"P{priority} " prefix `EventLabel.to_api_body` adds to a
label's name when `priority` is set -- group 1 is the priority, group 2
(absent if the label has no name of its own) is the rest of the name."""


@dataclass(kw_only=True)
class EventLabel:
    """One of a calendar's custom event labels. See
    https://developers.google.com/workspace/calendar/api/guides/labels
    """

    id: str | None = None
    """Uniquely identifies the label within its calendar. `None` until
    the label has been created; `CalendarClient.create_event_label`
    assigns this (a UUID Google generates) from the API response."""

    background_color: str | None = None
    """Hex color (e.g. "#8e24aa") events with this label are shown in.
    Required by the API, but may be left `None` here if `priority` is
    set: `to_api_body` then derives it from `priority` (see
    `_label_color_for_priority`) -- `from_api` also reverses this, so a
    label whose color already matches what its priority would derive
    round-trips back to `background_color=None` rather than a value
    that looks explicitly chosen."""

    name: str | None = None
    """Optional display name, up to 50 characters -- not counting the
    f"P{priority} " prefix `to_api_body` adds when `priority` is set,
    which `from_api` strips back off."""

    priority: int | None = None
    """This label's priority, if it has one. Encoded into the API's
    `name` field (there's no dedicated field for it) as a f"P{priority} "
    prefix, and used to derive `background_color` when that's left
    unset -- see `to_api_body`."""

    @classmethod
    def from_api(cls, data: dict) -> "EventLabel":
        background_color = data["backgroundColor"]
        name = data.get("name")
        priority = None
        if name is not None:
            match = _PRIORITY_NAME_PREFIX_RE.match(name)
            if match is not None:
                priority = int(match.group(1))
                name = match.group(2)
                if background_color == _label_color_for_priority(priority):
                    # This color is exactly what to_api_body would derive
                    # from this priority -- treat it as derived, not an
                    # independently chosen color, so a round trip through
                    # this class doesn't "freeze" a color that should
                    # keep following priority if priority changes later.
                    background_color = None
        return cls(id=data.get("id"), background_color=background_color, name=name, priority=priority)

    def to_api_body(self) -> dict:
        background_color = self.background_color
        if background_color is None:
            if self.priority is None:
                raise ValueError("background_color is required when priority is not set")
            background_color = _label_color_for_priority(self.priority)
            if background_color is None:
                raise ValueError(
                    f"background_color is required for priority {self.priority} -- "
                    "it has no default label color (see _PRIORITY_LABEL_COLORS)"
                )
        body: dict = {"backgroundColor": background_color}
        if self.id is not None:
            body["id"] = self.id
        name = self.name
        if self.priority is not None:
            name = f"P{self.priority} {name}" if name else f"P{self.priority}"
        if name is not None:
            body["name"] = name
        return body


def _parse_datetime(value: dict) -> datetime:
    raw = value.get("dateTime")
    if raw is None:
        raise ValueError(
            "Event is missing a dateTime with a UTC offset; all-day (date-only) "
            "events are not supported"
        )
    if raw.endswith("Z"):
        raw = f"{raw[:-1]}+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        time_zone = value.get("timeZone")
        if time_zone is None:
            raise ValueError(f"datetime {raw!r} must include a UTC offset/timezone")
        parsed = parsed.replace(tzinfo=ZoneInfo(time_zone))
    return parsed


def _format_datetime(value: datetime) -> dict:
    if value.tzinfo is None:
        raise ValueError(f"datetime {value!r} must be timezone-aware")
    return {"dateTime": value.isoformat()}


def _parse_properties(
    private_properties: dict[str, str], parsers: dict[str, Callable[[str], Any]]
) -> dict[str, Any]:
    """Parse whichever of this app's prefixed keys are present in
    private_properties, using the given per-attribute parser functions.
    Returns a dict keyed by attribute name (not the prefixed key) — suitable
    for passing to Event(**parsed) — omitting any attribute whose key is
    absent from private_properties."""
    parsed = {}
    for attr, parse in parsers.items():
        raw = private_properties.get(f"{_APP_EXTENDED_PROPERTY_KEY_PREFIX}{attr}")
        if raw is not None:
            parsed[attr] = parse(raw)
    return parsed


def _format_properties(
    obj: Any, formatters: dict[str, Callable[[Any], str]]
) -> dict[str, str]:
    """Format whichever of obj's named attributes are not None, using the
    given per-attribute formatter functions, into a dict of this app's
    prefixed extendedProperties.private keys to their string values."""
    formatted = {}
    for attr, format_value in formatters.items():
        value = getattr(obj, attr)
        if value is not None:
            formatted[f"{_APP_EXTENDED_PROPERTY_KEY_PREFIX}{attr}"] = format_value(value)
    return formatted


def load_credentials(token_path: Path, credentials_path: Path) -> Credentials:
    """Load cached OAuth credentials, refreshing or running the consent flow as needed.

    token_path: where the user's OAuth token (access token + refresh token) is
        cached, conventionally as `token.json`. This file does not need to
        exist yet: on first run (or once the cached token can no longer be
        refreshed), this function runs an interactive consent flow that opens
        a browser for the user to log into Google and grant access, then
        writes the resulting credentials here. On every later run, the
        cached token is read back and — if the access token has expired — is
        refreshed and, on a best-effort basis, rewritten to this same path
        (see below). This file contains live user credentials and must
        never be committed.
    credentials_path: path to the OAuth *client* secret file (conventionally
        `credentials.json`), downloaded once from the Google Cloud Console
        for the project this server registers as. It identifies the
        application, not the end user, but must still never be committed.

    Rewriting token_path after a refresh is best-effort: if the path isn't
    writable (e.g. a read-only mount, such as a Render Secret File), the
    write is skipped with a warning rather than raising. This is safe
    because a refresh doesn't change the refresh token itself, only the
    short-lived access token — so an unwritable token_path just means the
    next process start refreshes again from the same cached refresh token,
    rather than reusing an unexpired access token.
    """
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        try:
            token_path.write_text(creds.to_json())
        except OSError:
            logger.warning(
                "Could not write refreshed credentials to %s; continuing "
                "with in-memory credentials for this run.",
                token_path,
            )

    return creds


class CalendarClient:
    """Wraps the Google Calendar API behind a small, mockable interface.

    calendar_id: the Google Calendar to operate on. Because this app
        requests the calendar.app.created scope, this must be the ID of a
        calendar the app has created itself (run create_calendar.py) —
        "primary" or any other pre-existing calendar will not work. There
        is deliberately no default — callers must decide explicitly.
    """

    def __init__(self, service, calendar_id: str):
        self._service = service
        self._calendar_id = calendar_id

    @classmethod
    def from_credentials(
        cls,
        token_path: Path,
        credentials_path: Path,
        calendar_id: str,
    ) -> "CalendarClient":
        """See `load_credentials` for `token_path`/`credentials_path`, and
        `CalendarClient` for `calendar_id`."""
        creds = load_credentials(token_path, credentials_path)
        service = build("calendar", "v3", credentials=creds)
        return cls(service, calendar_id=calendar_id)

    def list_events(self, time_min: datetime, time_max: datetime) -> list[Event]:
        response = (
            self._service.events()
            .list(
                calendarId=self._calendar_id,
                timeMin=time_min.isoformat(),
                timeMax=time_max.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
        return [Event.from_api(item) for item in response.get("items", [])]

    def get_event(self, event_id: str) -> Event:
        response = (
            self._service.events()
            .get(calendarId=self._calendar_id, eventId=event_id)
            .execute()
        )
        return Event.from_api(response)

    def create_event(self, event: Event) -> Event:
        response = (
            self._service.events()
            .insert(calendarId=self._calendar_id, body=event.to_api_body())
            .execute()
        )
        return Event.from_api(response)

    def update_event(self, event: Event) -> Event:
        if not event.id:
            raise ValueError("event.id is required to update an event")
        response = (
            self._service.events()
            .patch(calendarId=self._calendar_id, eventId=event.id, body=event.to_api_body())
            .execute()
        )
        return Event.from_api(response)

    def delete_event(self, event_id: str) -> None:
        self._service.events().delete(
            calendarId=self._calendar_id, eventId=event_id
        ).execute()

    def list_event_labels(self) -> list[EventLabel]:
        """Every custom event label currently defined on this calendar --
        see `EventLabel`."""
        _, labels = self._get_raw_event_labels()
        return [EventLabel.from_api(label) for label in labels]

    def create_event_label(
        self,
        background_color: str | None = None,
        name: str | None = None,
        priority: int | None = None,
    ) -> EventLabel:
        """Define a new event label on this calendar. The API has no way
        to add a single label in place -- creating one means replacing
        the whole `labelProperties.eventLabels` list with the existing
        labels plus this new one (see `_patch_event_labels`). See
        `EventLabel` for how `background_color`/`priority` interact."""
        etag, labels = self._get_raw_event_labels()
        existing_ids = {label["id"] for label in labels}
        new_label = EventLabel(background_color=background_color, name=name, priority=priority)
        updated = self._patch_event_labels(etag, labels + [new_label.to_api_body()])
        return next(label for label in updated if label.id not in existing_ids)

    def update_event_label(
        self,
        label_id: str,
        *,
        background_color: str | None = None,
        name: str | None = None,
        priority: int | None = None,
    ) -> EventLabel:
        """Update an existing event label's `background_color`, `name`,
        and/or `priority` -- whichever is left `None` keeps its current
        value. Raises `ValueError` if no label with `label_id` exists."""
        etag, labels = self._get_raw_event_labels()
        for index, label in enumerate(labels):
            if label.get("id") == label_id:
                current = EventLabel.from_api(label)
                merged = EventLabel(
                    id=label_id,
                    background_color=(
                        background_color if background_color is not None else current.background_color
                    ),
                    name=name if name is not None else current.name,
                    priority=priority if priority is not None else current.priority,
                )
                labels[index] = merged.to_api_body()
                break
        else:
            raise ValueError(f"event label {label_id!r} not found")
        updated = self._patch_event_labels(etag, labels)
        return next(label for label in updated if label.id == label_id)

    def delete_event_label(self, label_id: str) -> EventLabel:
        """Remove an event label from this calendar. Returns the label as
        it was just before removal. Raises `ValueError` if no label with
        `label_id` exists."""
        etag, labels = self._get_raw_event_labels()
        remaining = [label for label in labels if label.get("id") != label_id]
        if len(remaining) == len(labels):
            raise ValueError(f"event label {label_id!r} not found")
        removed = next(EventLabel.from_api(label) for label in labels if label.get("id") == label_id)
        self._patch_event_labels(etag, remaining)
        return removed

    def _get_raw_event_labels(self) -> tuple[str | None, list[dict]]:
        """(etag, eventLabels) for this calendar -- the etag lets
        `_patch_event_labels` guard the matching write against a
        concurrent change to the same list."""
        calendar = self._service.calendars().get(calendarId=self._calendar_id).execute()
        return calendar.get("etag"), calendar.get("labelProperties", {}).get("eventLabels", [])

    def _patch_event_labels(self, etag: str | None, labels: list[dict]) -> list[EventLabel]:
        request = self._service.calendars().patch(
            calendarId=self._calendar_id, body={"labelProperties": {"eventLabels": labels}}
        )
        if etag is not None:
            # Precondition: only apply this write if the calendar's
            # labels haven't changed since _get_raw_event_labels read
            # them (verified empirically -- the API rejects a stale
            # etag here with 412 exactly as it does for events, though
            # only the latter is documented). Without this, two callers
            # reading-then-writing concurrently could silently clobber
            # each other's change.
            request.headers["If-Match"] = etag
        try:
            response = request.execute()
        except HttpError as exc:
            if exc.resp.status == 412:
                raise EventLabelConflictError(
                    "This calendar's event labels changed while this update was being "
                    "made; fetch the current labels and try again."
                ) from exc
            raise
        return [
            EventLabel.from_api(label)
            for label in response.get("labelProperties", {}).get("eventLabels", [])
        ]
