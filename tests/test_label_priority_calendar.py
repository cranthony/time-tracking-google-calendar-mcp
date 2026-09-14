from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from calendar_clients.google_calendar import Event
from utilities.label_priority_calendar import LabelPriorityCalendar

UTC = timezone.utc


def _event(**overrides) -> Event:
    fields = {
        "id": "abc123",
        "summary": "Event",
        "start": datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    }
    fields.update(overrides)
    return Event(**fields)


def _calendar(event_labels_priorities: dict) -> tuple[LabelPriorityCalendar, MagicMock, MagicMock]:
    client = MagicMock()
    event_labels = MagicMock()
    event_labels.label_priorities.return_value = event_labels_priorities
    return LabelPriorityCalendar(client, event_labels), client, event_labels


class TestListEvents:
    def test_fills_in_priority_from_the_event_label_when_unset(self):
        calendar, client, _ = _calendar({"label-1": 3})
        client.list_events.return_value = [_event(event_label_id="label-1", priority=None)]

        events = calendar.list_events(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
        )

        assert events[0].priority == 3

    def test_leaves_an_explicit_priority_alone(self):
        calendar, client, event_labels = _calendar({"label-1": 3})
        client.list_events.return_value = [_event(event_label_id="label-1", priority=1)]

        events = calendar.list_events(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
        )

        assert events[0].priority == 1

    def test_leaves_priority_unset_when_there_is_no_event_label(self):
        calendar, client, _ = _calendar({})
        client.list_events.return_value = [_event(event_label_id=None, priority=None)]

        events = calendar.list_events(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
        )

        assert events[0].priority is None

    def test_leaves_priority_unset_when_the_label_has_none_of_its_own(self):
        calendar, client, _ = _calendar({"label-1": None})
        client.list_events.return_value = [_event(event_label_id="label-1", priority=None)]

        events = calendar.list_events(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
        )

        assert events[0].priority is None

    def test_leaves_priority_unset_when_the_label_is_unknown(self):
        calendar, client, _ = _calendar({})
        client.list_events.return_value = [_event(event_label_id="label-1", priority=None)]

        events = calendar.list_events(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
        )

        assert events[0].priority is None

    def test_does_not_mutate_the_original_event(self):
        calendar, client, _ = _calendar({"label-1": 3})
        original = _event(event_label_id="label-1", priority=None)
        client.list_events.return_value = [original]

        events = calendar.list_events(
            datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)
        )

        assert original.priority is None
        assert events[0] is not original

    def test_passes_through_time_min_and_time_max(self):
        calendar, client, _ = _calendar({})
        client.list_events.return_value = []
        time_min = datetime(2026, 1, 1, tzinfo=UTC)
        time_max = datetime(2026, 1, 2, tzinfo=UTC)

        calendar.list_events(time_min, time_max)

        client.list_events.assert_called_once_with(time_min, time_max)


class TestGetEvent:
    def test_fills_in_priority_from_the_event_label_when_unset(self):
        calendar, client, _ = _calendar({"label-1": 2})
        client.get_event.return_value = _event(event_label_id="label-1", priority=None)

        event = calendar.get_event("abc123")

        assert event.priority == 2
        client.get_event.assert_called_once_with("abc123")

    def test_leaves_an_explicit_priority_alone(self):
        calendar, client, _ = _calendar({"label-1": 2})
        client.get_event.return_value = _event(event_label_id="label-1", priority=1)

        event = calendar.get_event("abc123")

        assert event.priority == 1


class TestCreateAndUpdateEvent:
    def test_create_event_passes_straight_through(self):
        calendar, client, event_labels = _calendar({})
        new_event = _event(id=None)
        client.create_event.return_value = new_event

        result = calendar.create_event(new_event)

        assert result is new_event
        client.create_event.assert_called_once_with(new_event)
        event_labels.label_priorities.assert_not_called()

    def test_update_event_passes_straight_through(self):
        calendar, client, event_labels = _calendar({})
        updated_event = _event()
        client.update_event.return_value = updated_event

        result = calendar.update_event(updated_event)

        assert result is updated_event
        client.update_event.assert_called_once_with(updated_event)
        event_labels.label_priorities.assert_not_called()
