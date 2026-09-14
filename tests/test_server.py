import dataclasses
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from mcp.server.mcpserver.exceptions import ToolError

import server
from calendar_clients.google_calendar import Event, EventLabelConflictError
from server import PublicEvent
from utilities.event_labels import EventLabel
from utilities.reallocating_calendar import ReallocatingCalendar
from utilities.reallocation import ReallocationOptions

UTC = timezone.utc


def _fake_client(monkeypatch) -> MagicMock:
    client = MagicMock()
    monkeypatch.setattr(server, "get_calendar_client", lambda: client)
    return client


def _fake_reallocating_calendar(monkeypatch) -> MagicMock:
    reallocating_calendar = MagicMock()
    monkeypatch.setattr(server, "get_reallocating_calendar", lambda: reallocating_calendar)
    return reallocating_calendar


def _fake_event_labels(monkeypatch) -> MagicMock:
    event_labels = MagicMock()
    monkeypatch.setattr(server, "get_calendar_with_event_labels", lambda: event_labels)
    return event_labels


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

        assert field_names.isdisjoint(server.INTERNAL_EVENT_FIELDS)
        # is_cancelled has no Event equivalent -- it's derived from the
        # hidden status field, not a field PublicEvent passes through.
        event_derived_fields = field_names - {"is_cancelled"}
        assert event_derived_fields == {
            f.name for f in dataclasses.fields(Event)
        } - server.INTERNAL_EVENT_FIELDS

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

    def test_from_event_carries_event_label_id(self):
        event = _event(id="abc123", event_label_id="label-1")

        public_event = PublicEvent.from_event(event)

        assert public_event.event_label_id == "label-1"

    def test_to_event_carries_event_label_id(self):
        public_event = _public_event(id="abc123", event_label_id="label-1")

        event = public_event.to_event()

        assert event.event_label_id == "label-1"

    def test_from_event_exposes_cancellation_alongside_its_other_fields(self):
        event = _event(id="abc123", status="cancelled", priority=1, location="Room")

        public_event = PublicEvent.from_event(event)

        assert public_event.id == "abc123"
        assert public_event.is_cancelled is True
        assert public_event.summary == event.summary
        assert public_event.start == event.start
        assert public_event.end == event.end
        assert public_event.priority == 1
        assert public_event.location == "Room"

    def test_from_event_is_cancelled_false_for_a_confirmed_event(self):
        event = _event(id="abc123", status="confirmed")

        public_event = PublicEvent.from_event(event)

        assert public_event.is_cancelled is False
        assert public_event.summary == event.summary

    def test_to_event_maps_is_cancelled_true_to_cancelled_status(self):
        public_event = _public_event(id="abc123", is_cancelled=True)

        event = public_event.to_event()

        assert event.status == "cancelled"

    def test_to_event_setting_is_cancelled_false_has_no_effect(self):
        public_event = _public_event(id="abc123", is_cancelled=False)

        event = public_event.to_event()

        assert event.status is None


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

    def test_omits_cancelled_events(self, monkeypatch):
        client = _fake_client(monkeypatch)
        client.list_events.return_value = [
            _event(id="abc123", status="confirmed"),
            _event(id="def456", status="cancelled"),
        ]

        result = server.list_events(
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC), datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        )

        assert [event.id for event in result] == ["abc123"]


class TestGetEvent:
    def test_delegates_to_calendar_client(self, monkeypatch):
        client = _fake_client(monkeypatch)
        event = _event(id="abc123", is_end_of_day_sleep=True)
        client.get_event.return_value = event

        result = server.get_event("abc123")

        assert result == PublicEvent.from_event(event)
        assert not hasattr(result, "is_end_of_day_sleep")
        client.get_event.assert_called_once_with("abc123")

    def test_raises_tool_error_for_cancelled_event(self, monkeypatch):
        client = _fake_client(monkeypatch)
        client.get_event.return_value = _event(id="abc123", status="cancelled")

        with pytest.raises(ToolError):
            server.get_event("abc123")


class TestUpdateEvent:
    def test_delegates_to_calendar_client(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        updated = _event(id="abc123", summary="Renamed")
        reallocating_calendar.update_event.return_value = [updated]
        public_event = _public_event(id="abc123", summary="Renamed")

        result = server.update_event(public_event)

        assert result == [PublicEvent.from_event(updated)]
        (call_updated_event, call_options), _ = reallocating_calendar.update_event.call_args
        assert call_updated_event == public_event.to_event()
        assert call_options == ReallocationOptions()

    def test_result_includes_every_affected_event(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        updated = _event(id="abc123")
        shrunk = _event(id="def456", summary="Shrunk")
        reallocating_calendar.update_event.return_value = [updated, shrunk]

        result = server.update_event(_public_event(id="abc123"))

        assert {e.id for e in result} == {"abc123", "def456"}

    def test_wraps_value_error_as_tool_error(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        reallocating_calendar.update_event.side_effect = ValueError(
            "updated_event.id is required to update an event with reallocation"
        )

        with pytest.raises(ToolError):
            server.update_event(_public_event(id="abc123"))


class TestCreateEvent:
    def test_delegates_to_calendar_client(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        created = _event(id="abc123")
        reallocating_calendar.create_event.return_value = [created]
        new_public_event = _public_event()

        result = server.create_event(new_public_event)

        assert result == [PublicEvent.from_event(created)]
        (call_new_event, call_options), _ = reallocating_calendar.create_event.call_args
        assert call_new_event == new_public_event.to_event()
        assert call_options == ReallocationOptions()

    def test_result_includes_every_affected_event(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        created = _event(id="abc123")
        shrunk = _event(id="def456", summary="Shrunk")
        reallocating_calendar.create_event.return_value = [created, shrunk]

        result = server.create_event(_public_event())

        assert {e.id for e in result} == {"abc123", "def456"}

    def test_includes_cancelled_events_marked_is_cancelled(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        created = _event(id="abc123")
        cancelled = _event(id="def456", status="cancelled", summary="Old meeting")
        reallocating_calendar.create_event.return_value = [created, cancelled]

        result = server.create_event(_public_event())

        assert [e.id for e in result] == ["abc123", "def456"]
        cancelled_public_event = result[1]
        assert cancelled_public_event.is_cancelled is True
        assert cancelled_public_event.summary == "Old meeting"

    def test_wraps_value_error_as_tool_error(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        reallocating_calendar.create_event.side_effect = ValueError("bad input")

        with pytest.raises(ToolError):
            server.create_event(_public_event())


class TestDeleteEvent:
    def test_delegates_to_calendar_client(self, monkeypatch):
        client = _fake_client(monkeypatch)
        client.update_event.return_value = _event(id="abc123", status="cancelled")

        result = server.delete_event("abc123")

        sent_event = client.update_event.call_args[0][0]
        assert sent_event.id == "abc123"
        assert sent_event.status == "cancelled"
        client.delete_event.assert_not_called()
        assert len(result) == 1
        assert result[0].id == "abc123"
        assert result[0].is_cancelled is True


class TestCreateEventLabel:
    def test_delegates_to_event_labels(self, monkeypatch):
        event_labels = _fake_event_labels(monkeypatch)
        new_label = EventLabel(background_color="#8e24aa", name="Design Work")
        resulting_labels = [EventLabel(id="l1", background_color="#8e24aa", name="Design Work")]
        event_labels.create_label.return_value = resulting_labels

        result = server.create_event_label(new_label)

        assert result == resulting_labels
        event_labels.create_label.assert_called_once_with(new_label)

    def test_wraps_conflict_error_as_tool_error(self, monkeypatch):
        event_labels = _fake_event_labels(monkeypatch)
        event_labels.create_label.side_effect = EventLabelConflictError("stale etag")

        with pytest.raises(ToolError):
            server.create_event_label(EventLabel(background_color="#8e24aa"))

    def test_wraps_value_error_as_tool_error(self, monkeypatch):
        event_labels = _fake_event_labels(monkeypatch)
        event_labels.create_label.side_effect = ValueError(
            "background_color is required when priority is not set"
        )

        with pytest.raises(ToolError):
            server.create_event_label(EventLabel())


class TestUpdateEventLabel:
    def test_delegates_to_event_labels(self, monkeypatch):
        event_labels = _fake_event_labels(monkeypatch)
        updated_label = EventLabel(id="l1", background_color="#000000")
        resulting_labels = [EventLabel(id="l1", background_color="#000000", name="Design Work")]
        event_labels.update_label.return_value = resulting_labels

        result = server.update_event_label(updated_label)

        assert result == resulting_labels
        event_labels.update_label.assert_called_once_with(updated_label)

    def test_wraps_value_error_as_tool_error(self, monkeypatch):
        event_labels = _fake_event_labels(monkeypatch)
        event_labels.update_label.side_effect = ValueError("event label 'missing' not found")

        with pytest.raises(ToolError):
            server.update_event_label(EventLabel(id="missing", background_color="#000000"))

    def test_wraps_conflict_error_as_tool_error(self, monkeypatch):
        event_labels = _fake_event_labels(monkeypatch)
        event_labels.update_label.side_effect = EventLabelConflictError("stale etag")

        with pytest.raises(ToolError):
            server.update_event_label(EventLabel(id="l1", background_color="#000000"))


class TestSyncEventLabelsFromSheet:
    def test_delegates_to_event_labels(self, monkeypatch):
        event_labels = _fake_event_labels(monkeypatch)
        labels = [EventLabel(id="l1", background_color="#8e24aa", name="Design Work", priority=1)]
        event_labels.sync_labels.return_value = labels

        result = server.sync_event_labels_from_sheet()

        assert result == labels
        event_labels.sync_labels.assert_called_once_with()

    def test_wraps_value_error_as_tool_error(self, monkeypatch):
        event_labels = _fake_event_labels(monkeypatch)
        event_labels.sync_labels.side_effect = ValueError("No event label sheet tracked")

        with pytest.raises(ToolError):
            server.sync_event_labels_from_sheet()

    def test_wraps_conflict_error_as_tool_error(self, monkeypatch):
        event_labels = _fake_event_labels(monkeypatch)
        event_labels.sync_labels.side_effect = EventLabelConflictError("stale etag")

        with pytest.raises(ToolError):
            server.sync_event_labels_from_sheet()


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


class TestGetReallocatingCalendar:
    def test_caches_across_calls(self, monkeypatch):
        client = _fake_client(monkeypatch)
        monkeypatch.setattr(server, "_reallocating_calendar", None)

        first = server.get_reallocating_calendar()
        second = server.get_reallocating_calendar()

        assert first is second
        assert isinstance(first, ReallocatingCalendar)
        assert first._client is client


class TestGetEventLabels:
    def test_caches_across_calls(self, monkeypatch):
        built = []

        def fake_build():
            event_labels = MagicMock()
            built.append(event_labels)
            return event_labels

        monkeypatch.setattr(server, "_event_labels", None)
        monkeypatch.setattr(server, "build_event_labels", fake_build)

        first = server.get_calendar_with_event_labels()
        second = server.get_calendar_with_event_labels()

        assert first is second
        assert len(built) == 1
