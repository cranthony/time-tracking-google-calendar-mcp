from datetime import datetime
from unittest.mock import MagicMock

import pytest

from calendar_clients.google_calendar import CalendarClient, Event


def make_client(service: MagicMock) -> CalendarClient:
    return CalendarClient(service, calendar_id="primary")


def api_event(event_id: str, start: str, end: str, summary: str = "Busy") -> dict:
    return {
        "id": event_id,
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }


class TestEvent:
    def test_from_api_parses_fields(self):
        event = Event.from_api(
            api_event("abc123", "2026-01-01T09:00:00", "2026-01-01T10:00:00")
        )

        assert event.id == "abc123"
        assert event.summary == "Busy"
        assert event.start == datetime(2026, 1, 1, 9, 0, 0)
        assert event.end == datetime(2026, 1, 1, 10, 0, 0)
        assert event.description is None

    def test_to_api_body_omits_description_when_absent(self):
        event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0),
            end=datetime(2026, 1, 1, 10, 0),
        )

        body = event.to_api_body()

        assert "description" not in body
        assert body["summary"] == "Focus block"
        assert body["start"] == {"dateTime": "2026-01-01T09:00:00"}
        assert body["end"] == {"dateTime": "2026-01-01T10:00:00"}

    def test_to_api_body_includes_description_when_present(self):
        event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0),
            end=datetime(2026, 1, 1, 10, 0),
            description="Deep work",
        )

        assert event.to_api_body()["description"] == "Deep work"

    @pytest.mark.parametrize(
        ("start", "end", "expected"),
        [
            (datetime(2026, 1, 1, 9, 30), datetime(2026, 1, 1, 10, 30), True),
            (datetime(2026, 1, 1, 8, 0), datetime(2026, 1, 1, 9, 0), False),
            (datetime(2026, 1, 1, 10, 0), datetime(2026, 1, 1, 11, 0), False),
            (datetime(2026, 1, 1, 9, 0), datetime(2026, 1, 1, 10, 0), True),
        ],
    )
    def test_overlaps(self, start, end, expected):
        event = Event(
            summary="Existing",
            start=datetime(2026, 1, 1, 9, 0),
            end=datetime(2026, 1, 1, 10, 0),
        )

        assert event.overlaps(start, end) is expected


class TestCalendarClientListEvents:
    def test_list_events_maps_response_items(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {
            "items": [
                api_event("1", "2026-01-01T09:00:00", "2026-01-01T10:00:00"),
                api_event("2", "2026-01-01T11:00:00", "2026-01-01T12:00:00"),
            ]
        }
        client = make_client(service)

        events = client.list_events(
            datetime(2026, 1, 1, 0, 0), datetime(2026, 1, 2, 0, 0)
        )

        assert [e.id for e in events] == ["1", "2"]
        service.events.return_value.list.assert_called_once_with(
            calendarId="primary",
            timeMin="2026-01-01T00:00:00",
            timeMax="2026-01-02T00:00:00",
            singleEvents=True,
            orderBy="startTime",
        )

    def test_list_events_returns_empty_list_when_no_items(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {}
        client = make_client(service)

        events = client.list_events(
            datetime(2026, 1, 1, 0, 0), datetime(2026, 1, 2, 0, 0)
        )

        assert events == []


class TestCalendarClientHasOverlap:
    def test_has_overlap_true_when_existing_event_overlaps(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {
            "items": [api_event("1", "2026-01-01T09:00:00", "2026-01-01T10:00:00")]
        }
        client = make_client(service)

        assert client.has_overlap(
            datetime(2026, 1, 1, 9, 30), datetime(2026, 1, 1, 10, 30)
        )

    def test_has_overlap_false_when_no_events(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {"items": []}
        client = make_client(service)

        assert not client.has_overlap(
            datetime(2026, 1, 1, 9, 30), datetime(2026, 1, 1, 10, 30)
        )


class TestCalendarClientCreateEvent:
    def test_create_event_sends_body_and_returns_parsed_event(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = api_event(
            "new-id", "2026-01-01T09:00:00", "2026-01-01T10:00:00", summary="New"
        )
        client = make_client(service)
        event = Event(
            summary="New",
            start=datetime(2026, 1, 1, 9, 0),
            end=datetime(2026, 1, 1, 10, 0),
        )

        result = client.create_event(event)

        assert result.id == "new-id"
        service.events.return_value.insert.assert_called_once_with(
            calendarId="primary", body=event.to_api_body()
        )


class TestCalendarClientUpdateEvent:
    def test_update_event_requires_id(self):
        client = make_client(MagicMock())
        event = Event(
            summary="No id",
            start=datetime(2026, 1, 1, 9, 0),
            end=datetime(2026, 1, 1, 10, 0),
        )

        with pytest.raises(ValueError):
            client.update_event(event)

    def test_update_event_patches_and_returns_parsed_event(self):
        service = MagicMock()
        service.events.return_value.patch.return_value.execute.return_value = api_event(
            "abc123", "2026-01-01T09:00:00", "2026-01-01T11:00:00"
        )
        client = make_client(service)
        event = Event(
            id="abc123",
            summary="Busy",
            start=datetime(2026, 1, 1, 9, 0),
            end=datetime(2026, 1, 1, 11, 0),
        )

        result = client.update_event(event)

        assert result.end == datetime(2026, 1, 1, 11, 0)
        service.events.return_value.patch.assert_called_once_with(
            calendarId="primary", eventId="abc123", body=event.to_api_body()
        )


class TestCalendarClientDeleteEvent:
    def test_delete_event_calls_delete_with_event_id(self):
        service = MagicMock()
        client = make_client(service)

        client.delete_event("abc123")

        service.events.return_value.delete.assert_called_once_with(
            calendarId="primary", eventId="abc123"
        )
        service.events.return_value.delete.return_value.execute.assert_called_once()
