from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import server
from calendar_clients.google_calendar import Event

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


class TestListEvents:
    def test_delegates_to_calendar_client(self, monkeypatch):
        client = _fake_client(monkeypatch)
        events = [_event(id="abc123")]
        client.list_events.return_value = events
        min_time = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        max_time = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

        result = server.list_events(min_time, max_time)

        assert result == events
        client.list_events.assert_called_once_with(min_time, max_time)


class TestGetEvent:
    def test_delegates_to_calendar_client(self, monkeypatch):
        client = _fake_client(monkeypatch)
        event = _event(id="abc123")
        client.get_event.return_value = event

        result = server.get_event("abc123")

        assert result == event
        client.get_event.assert_called_once_with("abc123")


class TestUpdateEvent:
    def test_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            server.update_event(_event())


class TestCreateEvent:
    def test_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            server.create_event(_event())


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
