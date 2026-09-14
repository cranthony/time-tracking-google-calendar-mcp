from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from calendar_clients.google_auth import load_credentials

_APP_EXTENDED_PROPERTY_KEY_PREFIX = "cascading-time-tracker-"
"""Prefix for the extendedProperties.private keys this app uses to store its
own per-event fields, distinguishing them from any other private key that
might exist on an event."""

_METADATA_MARKER_RE = re.compile(r"^\[cascading-time-tracker:([\w-]+)=([^\]]*)\]$", re.MULTILINE)
"""Matches one of this app's own key=value markers, each its own line in a
calendar's description -- see CalendarClient.get_calendar_metadata/
set_calendar_metadata. The Calendars resource has no extendedProperties
-style field the way Events does (confirmed against the API reference), so
this piggybacks small bits of structured data onto the one free-text field
Calendars do have, kept clearly delimited from -- and never overwriting --
whatever human-readable description surrounds it."""


def _parse_calendar_metadata(description: str | None) -> dict[str, str]:
    if not description:
        return {}
    return dict(_METADATA_MARKER_RE.findall(description))


def _with_calendar_metadata(description: str | None, key: str, value: str | None) -> str:
    """`description` with `key`'s marker line set to `value` (added, or
    replaced in place if `key` already had one) -- or removed entirely if
    `value` is None. Every other marker line, and the human-readable text
    around them, is left exactly as it was."""
    kept_lines = []
    for line in (description or "").splitlines():
        match = _METADATA_MARKER_RE.match(line)
        if match is not None and match.group(1) == key:
            continue
        kept_lines.append(line)
    if value is not None:
        kept_lines.append(f"[cascading-time-tracker:{key}={value}]")
    return "\n".join(kept_lines).strip("\n")


def color_for_priority(priority: int | None) -> tuple[str | None, str]:
    """Returns both the colorId to be used in the calendar event, and
    the hex code that can be used when assigning this priority to an
    event label. The default calendar color isn't queryable by the API,
    unfortunately, so we hack it and hard-code it here.

    Public (not prefixed with `_`) so that `utilities/event_labels.py`
    can use it to derive an event label's `background_color` from a
    priority -- something this module itself no longer has any reason
    to do, since `EventLabel` here has no `priority` field (see its
    docstring)."""
    if priority is None:
        priority = 2  # Default priority.
    _PRIORITY_COLORS: dict[int, tuple[str | None, str]] = {
        0: ("8", "#e1e1e1"),  # Graphite (gray)
        1: ("5", "#fbd75b"),  # Banana (yellow)
        2: (None, "#a4bdfc"), # The default calendar color
        3: ("2", "#7ae7bf"),   # Sage (soft green)
    }
    def _clamp(value: int | None, lower: int, upper: int) -> int | None:
        return min(upper, max(lower, value))
    return _PRIORITY_COLORS.get(_clamp(priority, 0, 3))

def _color_id_for_priority(priority: int | None) -> str | None:
    """Priorities are colored with the colorId field, to make them easily
    visible on the calendar.  The colorId field is restricted to a fixed set
    of 11 colors.

    Note that event labels unlock the ability to specify our own colors.  The
    priority field doesn't use this feature because we intend to use it for
    a different categorization feature.  An event label's color supersedes a
    color ID."""
    return color_for_priority(priority)[0]


def _event_label_version_kwargs(body: dict) -> dict:
    """The `eventLabelVersion=1` query parameter insert/patch must be
    called with whenever `body` sets `eventLabelId` -- confirmed against
    https://developers.google.com/workspace/calendar/api/v3/reference/events:
    without it, the API silently ignores that field instead of applying
    it. Omitted otherwise, since it's specific to writing this one field."""
    return {"eventLabelVersion": 1} if "eventLabelId" in body else {}


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

    event_label_id: str | None = None
    """The id of one of this calendar's custom event labels (see
    `EventLabel`/`CalendarClient.list_event_labels`) assigned to this
    event, if any -- a real top-level API field (`eventLabelId`), not an
    `extendedProperties.private` one like `priority`/`min_duration`/etc
    above. Its color supersedes `colorId` on the calendar.
    See https://developers.google.com/workspace/calendar/api/v3/reference/events#eventLabelId
    for more information."""

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
            event_label_id=data.get("eventLabelId"),
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
        if self.event_label_id is not None:
            body["eventLabelId"] = self.event_label_id
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
    in calendar_clients/google_auth.py), it only ever sees/creates
    calendars of this kind that it made itself — never the user's
    existing calendars.
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
    """Raised by any CalendarClient method that reads the calendar,
    mutates something about it in memory, and writes the whole thing
    back (see _patch_calendar) -- event-label writes (create_event_label/
    update_event_label/delete_event_label/replace_event_labels) and
    calendar-metadata writes (set_calendar_metadata) alike -- when
    another writer changed the calendar in between this call's read and
    its write. Guarded by the calendar's `etag` (an `If-Match`
    precondition) so a lost update fails loudly instead of silently
    overwriting the other writer's change."""


@dataclass(kw_only=True)
class EventLabel:
    """One of a calendar's custom event labels, exactly as Google
    Calendar's API represents it -- a *raw* label. See
    https://developers.google.com/workspace/calendar/api/guides/labels

    Google Calendar itself has no concept of a label's priority -- there's
    no such field on the API's label resource, so this class has no
    `priority` field either. `utilities/event_labels.py`'s `EventLabel`
    (a different class, despite the same name) is the higher-level
    object that combines one of these with a priority sourced from a
    synced Google Sheet (see that module and `EventLabelSheet` in
    `utilities/event_label_sheet.py`) -- that's the type the MCP server
    and most of the CLI work with; this one is for direct, raw access
    (the CLI's `raw_label`-prefixed commands).
    """

    id: str | None = None
    """Uniquely identifies the label within its calendar. `None` until
    the label has been created; `CalendarClient.create_event_label`
    assigns this (a UUID Google generates) from the API response."""

    background_color: str
    """Hex color (e.g. "#8e24aa") events with this label are shown in --
    required by the API, and by this class in turn (unlike `utilities/
    event_labels.py`'s `EventLabel`, which can derive one from a
    priority instead)."""

    name: str | None = None
    """Optional display name, up to 50 characters."""

    @classmethod
    def from_api(cls, data: dict) -> "EventLabel":
        return cls(id=data.get("id"), background_color=data["backgroundColor"], name=data.get("name"))

    def to_api_body(self) -> dict:
        body: dict = {"backgroundColor": self.background_color}
        if self.id is not None:
            body["id"] = self.id
        if self.name is not None:
            body["name"] = self.name
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
        body = event.to_api_body()
        response = (
            self._service.events()
            .insert(calendarId=self._calendar_id, body=body, **_event_label_version_kwargs(body))
            .execute()
        )
        return Event.from_api(response)

    def update_event(self, event: Event) -> Event:
        if not event.id:
            raise ValueError("event.id is required to update an event")
        body = event.to_api_body()
        response = (
            self._service.events()
            .patch(
                calendarId=self._calendar_id,
                eventId=event.id,
                body=body,
                **_event_label_version_kwargs(body),
            )
            .execute()
        )
        return Event.from_api(response)

    def delete_event(self, event_id: str) -> None:
        self._service.events().delete(
            calendarId=self._calendar_id, eventId=event_id
        ).execute()

    def list_event_labels(self) -> tuple[list[EventLabel], str]:
        """Every custom event label currently defined on this calendar --
        see `EventLabel`.  Also returns the etag, in case this list is used
        to write the labels later."""
        etag, labels = self._get_raw_event_labels()
        return [EventLabel.from_api(label) for label in labels], etag

    def create_event_label(self, background_color: str, name: str | None = None) -> EventLabel:
        """Define a new event label on this calendar. The API has no way
        to add a single label in place -- creating one means replacing
        the whole `labelProperties.eventLabels` list with the existing
        labels plus this new one (see `_patch_event_labels`)."""
        etag, labels = self._get_raw_event_labels()
        existing_ids = {label["id"] for label in labels}
        new_label = EventLabel(background_color=background_color, name=name)
        updated = self._patch_event_labels(etag, labels + [new_label.to_api_body()])
        return next(label for label in updated if label.id not in existing_ids)

    def update_event_label(
        self, label_id: str, *, background_color: str | None = None, name: str | None = None
    ) -> EventLabel:
        """Update an existing event label's `background_color` and/or
        `name` -- whichever is left `None` keeps its current value.
        Raises `ValueError` if no label with `label_id` exists."""
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

    def replace_event_labels(self, labels: list[EventLabel], etag: str | None = None) -> list[EventLabel]:
        """Atomically replace this calendar's entire set of custom event
        labels with `labels`: each given label is created (if `id` is
        `None`) or fully overwritten (if `id` matches an existing label --
        unlike `update_event_label`, every field is replaced, not merged
        with the label's current value), and any existing label whose
        `id` isn't present in `labels` is deleted. Guarded by the
        calendar's etag exactly like create/update/delete_event_label, so
        a concurrent change raises `EventLabelConflictError`.

        Used by `utilities/event_label_sheet.py` to sync labels from a
        Google Sheet, where the sheet is the source of truth for the
        whole set."""
        updated = self._patch_event_labels(etag, [label.to_api_body() for label in labels])
        return updated

    def get_calendar_metadata(self, key: str) -> str | None:
        """This app's own `key`, most recently set via
        `set_calendar_metadata` -- or `None` if it's never been set. See
        `_parse_calendar_metadata` for where this actually lives (there's
        no dedicated field for it on the Calendars resource)."""
        calendar = self._service.calendars().get(calendarId=self._calendar_id).execute()
        return _parse_calendar_metadata(calendar.get("description")).get(key)

    def set_calendar_metadata(self, key: str, value: str | None) -> None:
        """Set (or, if `value` is `None`, remove) this app's own `key` on
        the calendar -- see `get_calendar_metadata`. Guarded by the
        calendar's etag exactly like the event-label writes above, so a
        lost update (e.g. someone editing the calendar's description by
        hand, or another metadata write, at the same time) raises
        `EventLabelConflictError` instead of silently clobbering it."""
        calendar = self._service.calendars().get(calendarId=self._calendar_id).execute()
        new_description = _with_calendar_metadata(calendar.get("description"), key, value)
        self._patch_calendar(calendar.get("etag"), {"description": new_description})

    def _get_raw_event_labels(self) -> tuple[str | None, list[dict]]:
        """(etag, eventLabels) for this calendar -- the etag lets
        `_patch_calendar` guard the matching write against a concurrent
        change to the same list."""
        calendar = self._service.calendars().get(calendarId=self._calendar_id).execute()
        return calendar.get("etag"), calendar.get("labelProperties", {}).get("eventLabels", [])

    def _patch_event_labels(self, etag: str | None, labels: list[dict]) -> list[EventLabel]:
        response = self._patch_calendar(etag, {"labelProperties": {"eventLabels": labels}})
        return [
            EventLabel.from_api(label)
            for label in response.get("labelProperties", {}).get("eventLabels", [])
        ]

    def _patch_calendar(self, etag: str | None, body: dict) -> dict:
        """PATCH this calendar with `body`, guarded by `etag` -- shared by
        every read-modify-write against the Calendars resource
        (event-label writes and calendar-metadata writes alike)."""
        request = self._service.calendars().patch(calendarId=self._calendar_id, body=body)
        if etag is not None:
            # Precondition: only apply this write if the calendar hasn't
            # changed since the matching read (verified empirically --
            # the API rejects a stale etag here with 412 exactly as it
            # does for events, though only the latter is documented).
            # Without this, two callers reading-then-writing concurrently
            # could silently clobber each other's change.
            request.headers["If-Match"] = etag
        try:
            return request.execute()
        except HttpError as exc:
            if exc.resp.status == 412:
                raise EventLabelConflictError(
                    "This calendar changed while this update was being made; fetch its "
                    "current value and try again."
                ) from exc
            raise
