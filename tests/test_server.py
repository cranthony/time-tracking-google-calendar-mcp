import dataclasses
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import server
from calendar_clients.google_calendar import Event
from server import PublicEvent

UTC = timezone.utc


def _fake_client(monkeypatch) -> MagicMock:
    client = MagicMock()
    monkeypatch.setattr(server, "get_calendar_client", lambda: client)
    return client


def _event(**overrides) -> Event:
    fields = {
        "summary": "Focus block",
        "start": datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    }
    fields.update(overrides)
    return Event(**fields)


def _public_event(**overrides) -> PublicEvent:
    fields = {
        "summary": "Focus block",
        "start": datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    }
    fields.update(overrides)
    return PublicEvent(**fields)


class TestPublicEvent:
    def test_hides_is_end_of_day_sleep_from_its_fields(self):
        field_names = {f.name for f in dataclasses.fields(PublicEvent)}

        assert "is_end_of_day_sleep" not in field_names
        assert field_names == {f.name for f in dataclasses.fields(Event)} - server.HIDDEN_FROM_MCP

    def test_from_event_drops_is_end_of_day_sleep(self):
        event = _event(id="abc123", priority=1, is_end_of_day_sleep=True)

        public_event = PublicEvent.from_event(event)

        assert public_event.id == "abc123"
        assert public_event.priority == 1
        assert not hasattr(public_event, "is_end_of_day_sleep")

    def test_to_event_never_sets_is_end_of_day_sleep(self):
        public_event = _public_event(id="abc123", priority=1)

        event = public_event.to_event()

        assert event.id == "abc123"
        assert event.priority == 1
        assert event.is_end_of_day_sleep is None


class TestListEvents:
    def test_delegates_to_calendar_client(self, monkeypatch):
        client = _fake_client(monkeypatch)
        events = [_event(id="abc123", is_end_of_day_sleep=True)]
        client.list_events.return_value = events
        min_time = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        max_time = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

        result = server.list_events(min_time, max_time)

        assert result == [PublicEvent.from_event(events[0])]
        client.list_events.assert_called_once_with(min_time, max_time)

    def test_result_carries_no_is_end_of_day_sleep_value(self, monkeypatch):
        client = _fake_client(monkeypatch)
        client.list_events.return_value = [_event(id="abc123", is_end_of_day_sleep=True)]

        result = server.list_events(
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC), datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        )

        assert not hasattr(result[0], "is_end_of_day_sleep")


class TestGetEvent:
    def test_delegates_to_calendar_client(self, monkeypatch):
        client = _fake_client(monkeypatch)
        event = _event(id="abc123", is_end_of_day_sleep=True)
        client.get_event.return_value = event

        result = server.get_event("abc123")

        assert result == PublicEvent.from_event(event)
        assert not hasattr(result, "is_end_of_day_sleep")
        client.get_event.assert_called_once_with("abc123")


class TestUpdateEvent:
    def test_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            server.update_event(_public_event())


class TestCreateEvent:
    def test_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            server.create_event(_public_event())


class TestDeleteEvent:
    def test_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            server.delete_event("abc123")


class TestGetCalendarClient:
    def test_caches_client_across_calls(self, monkeypatch):
        built = []

        def fake_build():
            client = MagicMock()
            built.append(client)
            return client

        monkeypatch.setattr(server, "_calendar_client", None)
        monkeypatch.setattr(server, "build_calendar_client", fake_build)

        first = server.get_calendar_client()
        second = server.get_calendar_client()

        assert first is second
        assert len(built) == 1
