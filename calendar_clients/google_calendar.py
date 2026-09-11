from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar"]


@dataclass
class Event:
    """A calendar event, decoupled from the Google API's raw resource shape."""

    summary: str
    start: datetime
    end: datetime
    id: str | None = None
    description: str | None = None

    @classmethod
    def from_api(cls, data: dict) -> "Event":
        return cls(
            id=data.get("id"),
            summary=data.get("summary", ""),
            start=_parse_datetime(data["start"]),
            end=_parse_datetime(data["end"]),
            description=data.get("description"),
        )

    def to_api_body(self) -> dict:
        body: dict = {
            "summary": self.summary,
            "start": _format_datetime(self.start),
            "end": _format_datetime(self.end),
        }
        if self.description is not None:
            body["description"] = self.description
        return body

    def overlaps(self, other_start: datetime, other_end: datetime) -> bool:
        return self.start < other_end and other_start < self.end


def _parse_datetime(value: dict) -> datetime:
    raw = value.get("dateTime") or value.get("date")
    return datetime.fromisoformat(raw)


def _format_datetime(value: datetime) -> dict:
    return {"dateTime": value.isoformat()}


def load_credentials(token_path: Path, credentials_path: Path) -> Credentials:
    """Load cached OAuth credentials, refreshing or running the consent flow as needed."""
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return creds


class CalendarClient:
    """Wraps the Google Calendar API behind a small, mockable interface."""

    def __init__(self, service, calendar_id: str = "primary"):
        self._service = service
        self._calendar_id = calendar_id

    @classmethod
    def from_credentials(
        cls,
        token_path: Path,
        credentials_path: Path,
        calendar_id: str = "primary",
    ) -> "CalendarClient":
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
