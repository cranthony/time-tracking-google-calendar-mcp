from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]

logger = logging.getLogger(__name__)


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

    summary: str
    """See https://developers.google.com/workspace/calendar/api/v3/reference/events#summary
    for more information."""

    start: datetime
    """See https://developers.google.com/workspace/calendar/api/v3/reference/events#start
    for more information."""

    end: datetime
    """See https://developers.google.com/workspace/calendar/api/v3/reference/events#end
    for more information."""

    description: str | None = None
    """Optional free-text description of the event. Can include HTML.
    See https://developers.google.com/workspace/calendar/api/v3/reference/events#description
    for more information."""

    location: str | None = None
    """Optional free-text location of the event.
    See https://developers.google.com/workspace/calendar/api/v3/reference/events#location
    for more information."""

    extended_properties: dict[str, dict[str, str]] | None = None
    """Google Calendar's free-form key/value tags, shaped like the API's
    `extendedProperties`: `{"private": {...}, "shared": {...}}`. "private"
    and "shared" are the only valid top-level keys: properties under
    "private" aren't shared with other copies of the event on other
    calendars, while properties under "shared" are visible to other
    attendees.
    See https://developers.google.com/workspace/calendar/api/v3/reference/events#extendedProperties,
    https://developers.google.com/workspace/calendar/api/v3/reference/events#extendedProperties.private,
    and https://developers.google.com/workspace/calendar/api/v3/reference/events#extendedProperties.shared
    for more information."""

    @classmethod
    def from_api(cls, data: dict) -> "Event":
        return cls(
            id=data.get("id"),
            summary=data.get("summary", ""),
            start=_parse_datetime(data["start"]),
            end=_parse_datetime(data["end"]),
            description=data.get("description"),
            location=data.get("location"),
            extended_properties=data.get("extendedProperties"),
        )

    def to_api_body(self) -> dict:
        body: dict = {
            "summary": self.summary,
            "start": _format_datetime(self.start),
            "end": _format_datetime(self.end),
        }
        if self.description is not None:
            body["description"] = self.description
        if self.location is not None:
            body["location"] = self.location
        if self.extended_properties is not None:
            body["extendedProperties"] = self.extended_properties
        return body

    def overlaps(self, other_start: datetime, other_end: datetime) -> bool:
        assert self.start < self.end
        assert other_start < other_end
        return self.start < other_end and other_start < self.end


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

    calendar_id: the Google Calendar to operate on. Supply "primary" to
        represent the main calendar associated with the authenticated
        user's Google account, or a specific calendar's ID otherwise.
        There is deliberately no default — callers must decide explicitly.
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

    def has_overlap(self, start: datetime, end: datetime) -> bool:
        return any(event.overlaps(start, end) for event in self.list_events(start, end))

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
