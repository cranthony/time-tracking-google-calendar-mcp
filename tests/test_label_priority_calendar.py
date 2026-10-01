from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from calendar_clients.google_calendar import Event
from utilities.label_priority_calendar import LabelPriorityCalendar, fill_in_from_labels

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


def _calendar(
    event_labels_priorities: dict, event_labels_fixed_times: dict | None = None
) -> tuple[LabelPriorityCalendar, MagicMock, MagicMock]:
    client = MagicMock()
    event_labels = MagicMock()
    event_labels.label_priorities.return_value = event_labels_priorities
    event_labels.label_fixed_times.return_value = event_labels_fixed_times or {}
    return LabelPriorityCalendar(client, event_labels), client, event_labels


_DAY = (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC))


class TestListEvents:
    def test_fills_in_the_labels_priority_and_fixed_time(self):
        calendar, client, _ = _calendar({"label-1": 3}, {"label-1": True})
        client.list_events.return_value = [_event(event_label_id="label-1")]

        events = calendar.list_events(*_DAY)

        assert events[0].label_priority == 3
        assert events[0].label_is_fixed_time is True
        assert events[0].effective_priority == 3
        assert events[0].effective_is_fixed_time is True

    def test_never_touches_the_events_own_fields(self):
        calendar, client, _ = _calendar({"label-1": 3}, {"label-1": True})
        client.list_events.return_value = [
            _event(event_label_id="label-1", min_duration=timedelta(minutes=5))
        ]

        events = calendar.list_events(*_DAY)

        assert events[0].priority is None
        assert events[0].is_fixed_time is None
        assert events[0].min_duration == timedelta(minutes=5)

    def test_the_events_own_values_win(self):
        calendar, client, _ = _calendar({"label-1": 3}, {"label-1": True})
        client.list_events.return_value = [
            _event(event_label_id="label-1", priority=1, is_fixed_time=False)
        ]

        events = calendar.list_events(*_DAY)

        assert events[0].effective_priority == 1
        assert events[0].effective_is_fixed_time is False

    def test_leaves_label_fields_unset_when_there_is_no_event_label(self):
        calendar, client, _ = _calendar({"label-1": 3}, {"label-1": True})
        client.list_events.return_value = [_event(event_label_id=None)]

        events = calendar.list_events(*_DAY)

        assert events[0].label_priority is None
        assert events[0].label_is_fixed_time is None

    def test_leaves_label_fields_unset_when_the_label_has_none_of_its_own(self):
        calendar, client, _ = _calendar({"label-1": None}, {"label-1": None})
        client.list_events.return_value = [_event(event_label_id="label-1")]

        events = calendar.list_events(*_DAY)

        assert events[0].effective_priority is None
        assert events[0].effective_is_fixed_time is None

    def test_leaves_label_fields_unset_when_the_label_is_unknown(self):
        calendar, client, _ = _calendar({})
        client.list_events.return_value = [_event(event_label_id="label-1")]

        events = calendar.list_events(*_DAY)

        assert events[0].label_priority is None
        assert events[0].label_is_fixed_time is None

    def test_does_not_mutate_the_original_event(self):
        calendar, client, _ = _calendar({"label-1": 3})
        original = _event(event_label_id="label-1")
        client.list_events.return_value = [original]

        events = calendar.list_events(*_DAY)

        assert original.label_priority is None
        assert events[0] is not original

    def test_passes_through_time_min_and_time_max(self):
        calendar, client, _ = _calendar({})
        client.list_events.return_value = []

        calendar.list_events(*_DAY)

        client.list_events.assert_called_once_with(*_DAY)


class TestGetEvent:
    def test_fills_in_the_labels_priority_and_fixed_time(self):
        calendar, client, _ = _calendar({"label-1": 2}, {"label-1": True})
        client.get_event.return_value = _event(event_label_id="label-1")

        event = calendar.get_event("abc123")

        assert event.effective_priority == 2
        assert event.effective_is_fixed_time is True
        assert event.priority is None
        assert event.is_fixed_time is None
        client.get_event.assert_called_once_with("abc123")


class TestFillInFromLabels:
    def test_fills_in_already_read_events(self):
        _, _, event_labels = _calendar({"label-1": 3}, {"label-1": True})
        original = _event(event_label_id="label-1")

        events = fill_in_from_labels([original, _event(id="def456")], event_labels)

        assert events[0].label_priority == 3
        assert events[0].label_is_fixed_time is True
        assert events[1].label_priority is None
        assert original.label_priority is None


class TestCreateAndUpdateEvent:
    def test_create_event_passes_straight_through(self):
        calendar, client, event_labels = _calendar({})
        new_event = _event(id=None)
        client.create_event.return_value = new_event

        result = calendar.create_event(new_event)

        assert result is new_event
        client.create_event.assert_called_once_with(new_event)
        event_labels.label_priorities.assert_not_called()
        event_labels.label_fixed_times.assert_not_called()

    def test_update_event_passes_straight_through(self):
        calendar, client, event_labels = _calendar({})
        updated_event = _event()
        client.update_event.return_value = updated_event

        result = calendar.update_event(updated_event)

        assert result is updated_event
        client.update_event.assert_called_once_with(updated_event)
        event_labels.label_priorities.assert_not_called()
        event_labels.label_fixed_times.assert_not_called()
