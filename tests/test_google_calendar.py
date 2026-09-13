from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from googleapiclient.errors import HttpError

from calendar_clients import google_calendar
from calendar_clients.google_calendar import (
    Calendar,
    CalendarClient,
    Event,
    EventLabel,
    EventLabelConflictError,
    load_credentials,
)

UTC = timezone.utc
EST = timezone(timedelta(hours=-5))
TEST_CALENDAR_ID = "my-calendar-id"


def make_client(service: MagicMock) -> CalendarClient:
    return CalendarClient(service, calendar_id=TEST_CALENDAR_ID)


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
            api_event(
                "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
            )
        )

        assert event.id == "abc123"
        assert event.summary == "Busy"
        assert event.start == datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)
        assert event.end == datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
        assert event.description is None
        assert event.location is None
        assert event.status is None
        assert event.recurring_event_id is None
        assert event.min_duration is None
        assert event.is_fixed_duration is None
        assert event.priority is None
        assert event.is_end_of_day_sleep is None

    def test_from_api_parses_non_utc_offset(self):
        event = Event.from_api(
            api_event(
                "abc123", "2026-01-01T09:00:00-05:00", "2026-01-01T10:00:00-05:00"
            )
        )

        assert event.start == datetime(2026, 1, 1, 9, 0, 0, tzinfo=EST)
        assert event.end == datetime(2026, 1, 1, 10, 0, 0, tzinfo=EST)

    def test_from_api_parses_z_suffix_as_utc(self):
        event = Event.from_api(
            api_event("abc123", "2026-01-01T09:00:00Z", "2026-01-01T10:00:00Z")
        )

        assert event.start == datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)
        assert event.end == datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)

    def test_from_api_extracts_location(self):
        data = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        data["location"] = "Conference Room A"

        event = Event.from_api(data)

        assert event.location == "Conference Room A"

    def test_from_api_extracts_status(self):
        data = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        data["status"] = "cancelled"

        event = Event.from_api(data)

        assert event.status == "cancelled"

    def test_from_api_extracts_recurring_event_id(self):
        data = api_event(
            "abc123_20260101T090000Z",
            "2026-01-01T09:00:00+00:00",
            "2026-01-01T10:00:00+00:00",
        )
        data["recurringEventId"] = "abc123"

        event = Event.from_api(data)

        assert event.recurring_event_id == "abc123"

    def test_from_api_uses_timeZone_when_dateTime_is_naive(self):
        data = {
            "id": "abc123",
            "summary": "Busy",
            "start": {"dateTime": "2026-01-01T09:00:00", "timeZone": "America/New_York"},
            "end": {"dateTime": "2026-01-01T10:00:00", "timeZone": "America/New_York"},
        }

        event = Event.from_api(data)

        assert event.start == datetime(
            2026, 1, 1, 9, 0, tzinfo=ZoneInfo("America/New_York")
        )
        assert event.end == datetime(
            2026, 1, 1, 10, 0, tzinfo=ZoneInfo("America/New_York")
        )

    def test_from_api_extracts_app_properties(self):
        data = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        data["extendedProperties"] = {
            "private": {
                "cascading-time-tracker-min_duration": "30",
                "cascading-time-tracker-is_fixed_duration": "false",
                "cascading-time-tracker-priority": "2",
                "cascading-time-tracker-is_end_of_day_sleep": "true",
            }
        }

        event = Event.from_api(data)

        assert event.min_duration == timedelta(minutes=30)
        assert event.is_fixed_duration is False
        assert event.priority == 2
        assert event.is_end_of_day_sleep is True

    def test_from_api_forces_min_duration_to_full_duration_when_fixed(self):
        data = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        data["extendedProperties"] = {
            "private": {
                "cascading-time-tracker-min_duration": "30",
                "cascading-time-tracker-is_fixed_duration": "true",
            }
        }

        event = Event.from_api(data)

        assert event.min_duration == timedelta(hours=1)

    def test_from_api_parses_is_fixed_duration_false(self):
        data = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        data["extendedProperties"] = {
            "private": {"cascading-time-tracker-is_fixed_duration": "false"}
        }

        event = Event.from_api(data)

        assert event.is_fixed_duration is False

    def test_from_api_ignores_other_private_keys(self):
        data = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        data["extendedProperties"] = {"private": {"someOtherApp-key": "value"}}

        event = Event.from_api(data)

        assert event.min_duration is None
        assert event.is_fixed_duration is None
        assert event.priority is None
        assert event.is_end_of_day_sleep is None

    def test_from_api_raises_when_dateTime_missing_timezone(self):
        with pytest.raises(ValueError):
            Event.from_api(
                api_event("abc123", "2026-01-01T09:00:00", "2026-01-01T10:00:00")
            )

    def test_from_api_raises_when_only_all_day_date_given(self):
        data = {
            "id": "abc123",
            "summary": "Busy",
            "start": {"date": "2026-01-01"},
            "end": {"date": "2026-01-02"},
        }

        with pytest.raises(ValueError):
            Event.from_api(data)

    def test_from_api_defaults_summary_to_none_when_absent(self):
        data = {
            "id": "abc123",
            "start": {"dateTime": "2026-01-01T09:00:00+00:00"},
            "end": {"dateTime": "2026-01-01T10:00:00+00:00"},
        }

        event = Event.from_api(data)

        assert event.summary is None

    def test_to_api_body_omits_optional_fields_when_absent(self):
        event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )

        body = event.to_api_body()

        assert "description" not in body
        assert "location" not in body
        assert "status" not in body
        assert "recurringEventId" not in body
        assert "extendedProperties" not in body
        assert body["summary"] == "Focus block"
        assert body["start"] == {"dateTime": "2026-01-01T09:00:00+00:00"}
        assert body["end"] == {"dateTime": "2026-01-01T10:00:00+00:00"}

    def test_to_api_body_for_a_partial_update_payload_without_summary_start_end(self):
        event = Event(id="abc123", priority=1)

        body = event.to_api_body()

        assert "summary" not in body
        assert "start" not in body
        assert "end" not in body
        assert body["extendedProperties"] == {
            "private": {"cascading-time-tracker-priority": "1"}
        }

    def test_to_api_body_includes_description_when_present(self):
        event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            description="Deep work",
        )

        assert event.to_api_body()["description"] == "Deep work"

    def test_to_api_body_includes_location_when_present(self):
        event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            location="Conference Room A",
        )

        assert event.to_api_body()["location"] == "Conference Room A"

    def test_to_api_body_includes_status_when_present(self):
        event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            status="tentative",
        )

        assert event.to_api_body()["status"] == "tentative"

    def test_to_api_body_never_includes_recurring_event_id(self):
        event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            recurring_event_id="abc123",
        )

        assert "recurringEventId" not in event.to_api_body()

    def test_to_api_body_includes_app_properties_when_present(self):
        event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            min_duration=timedelta(minutes=30),
            is_fixed_duration=True,
            priority=2,
            is_end_of_day_sleep=True,
        )

        assert event.to_api_body()["extendedProperties"] == {
            "private": {
                "cascading-time-tracker-min_duration": "30",
                "cascading-time-tracker-is_fixed_duration": "true",
                "cascading-time-tracker-priority": "2",
                "cascading-time-tracker-is_end_of_day_sleep": "true",
            }
        }

    def test_to_api_body_includes_is_fixed_duration_false(self):
        event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            is_fixed_duration=False,
        )

        assert event.to_api_body()["extendedProperties"] == {
            "private": {"cascading-time-tracker-is_fixed_duration": "false"}
        }

    def test_to_api_body_includes_only_the_app_properties_that_are_set(self):
        event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=1,
        )

        assert event.to_api_body()["extendedProperties"] == {
            "private": {"cascading-time-tracker-priority": "1"}
        }

    def test_to_api_body_raises_when_start_or_end_is_naive(self):
        event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )

        with pytest.raises(ValueError):
            event.to_api_body()

    def test_to_api_body_omits_colorId_when_priority_is_unset(self):
        # priority=None here means "this payload doesn't touch priority"
        # (a partial-update payload, or a fresh Event nobody's given a
        # priority yet) -- there's no way to tell those apart, and either
        # way to_api_body must not guess a color.
        event = Event(id="abc123")

        assert "colorId" not in event.to_api_body()

    @pytest.mark.parametrize(
        "priority,expected_color_id",
        [(-1, "8"), (0, "8"), (1, "5"), (2, None), (3, "2"), (4, "2"), (5, "2")],
    )
    def test_to_api_body_sets_colorId_from_priority(self, priority, expected_color_id):
        event = Event(id="abc123", priority=priority)

        assert event.to_api_body()["colorId"] == expected_color_id

    def test_clone_copies_fields_independently(self):
        event = Event(
            id="abc123",
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=2,
        )

        clone = event.clone()
        clone.id = None
        clone.priority = 5

        assert clone.summary == "Focus block"
        assert clone.start == event.start
        assert event.id == "abc123"
        assert event.priority == 2


class TestCalendar:
    def test_from_api_parses_fields(self):
        calendar = Calendar.from_api(
            {"id": "cal123", "summary": "Time tracking", "description": "Work blocks"}
        )

        assert calendar.id == "cal123"
        assert calendar.summary == "Time tracking"
        assert calendar.description == "Work blocks"

    def test_from_api_defaults_optional_fields_to_none(self):
        calendar = Calendar.from_api({"summary": "Time tracking"})

        assert calendar.id is None
        assert calendar.description is None

    def test_to_api_body_omits_description_when_absent(self):
        calendar = Calendar(summary="Time tracking")

        body = calendar.to_api_body()

        assert body == {"summary": "Time tracking"}

    def test_to_api_body_includes_description_when_present(self):
        calendar = Calendar(summary="Time tracking", description="Work blocks")

        body = calendar.to_api_body()

        assert body == {"summary": "Time tracking", "description": "Work blocks"}


class TestEventLabel:
    def test_from_api_parses_fields(self):
        label = EventLabel.from_api(
            {"id": "label-1", "backgroundColor": "#8e24aa", "name": "Design Work"}
        )

        assert label.id == "label-1"
        assert label.background_color == "#8e24aa"
        assert label.name == "Design Work"

    def test_from_api_defaults_name_to_none(self):
        label = EventLabel.from_api({"id": "label-1", "backgroundColor": "#8e24aa"})

        assert label.name is None

    def test_to_api_body_omits_id_and_name_when_absent(self):
        label = EventLabel(background_color="#8e24aa")

        assert label.to_api_body() == {"backgroundColor": "#8e24aa"}

    def test_to_api_body_includes_id_and_name_when_present(self):
        label = EventLabel(id="label-1", background_color="#8e24aa", name="Design Work")

        assert label.to_api_body() == {
            "id": "label-1",
            "backgroundColor": "#8e24aa",
            "name": "Design Work",
        }

    def test_from_api_parses_priority_prefix_and_strips_it_from_name(self):
        label = EventLabel.from_api(
            {"id": "label-1", "backgroundColor": "#123456", "name": "P1 Design Work"}
        )

        assert label.priority == 1
        assert label.name == "Design Work"

    def test_from_api_parses_priority_only_prefix_with_no_name(self):
        label = EventLabel.from_api({"id": "label-1", "backgroundColor": "#123456", "name": "P2"})

        assert label.priority == 2
        assert label.name is None

    def test_from_api_leaves_priority_none_when_name_has_no_prefix(self):
        label = EventLabel.from_api(
            {"id": "label-1", "backgroundColor": "#123456", "name": "Design Work"}
        )

        assert label.priority is None
        assert label.name == "Design Work"
        assert label.background_color == "#123456"

    def test_from_api_nulls_background_color_when_it_matches_the_priority_color(self):
        label = EventLabel.from_api(
            {"id": "label-1", "backgroundColor": "#fbd75b", "name": "P1 Design Work"}
        )

        assert label.priority == 1
        assert label.background_color is None

    def test_from_api_keeps_background_color_when_it_does_not_match_the_priority_color(self):
        label = EventLabel.from_api(
            {"id": "label-1", "backgroundColor": "#123456", "name": "P1 Design Work"}
        )

        assert label.priority == 1
        assert label.background_color == "#123456"

    def test_to_api_body_adds_priority_prefix_to_name(self):
        label = EventLabel(background_color="#123456", name="Design Work", priority=1)

        assert label.to_api_body()["name"] == "P1 Design Work"

    def test_to_api_body_uses_bare_priority_prefix_when_no_name(self):
        label = EventLabel(background_color="#123456", priority=1)

        assert label.to_api_body()["name"] == "P1"

    def test_to_api_body_derives_background_color_from_priority(self):
        assert EventLabel(priority=0).to_api_body()["backgroundColor"] == "#e1e1e1"
        assert EventLabel(priority=1).to_api_body()["backgroundColor"] == "#fbd75b"
        assert EventLabel(priority=3).to_api_body()["backgroundColor"] == "#7ae7bf"

    def test_to_api_body_prefers_explicit_background_color_over_priority(self):
        label = EventLabel(background_color="#123456", priority=1)

        assert label.to_api_body()["backgroundColor"] == "#123456"

    def test_to_api_body_raises_when_no_background_color_and_no_priority(self):
        with pytest.raises(ValueError):
            EventLabel(name="No color, no priority").to_api_body()

    def test_to_api_body_raises_when_priority_has_no_default_color(self):
        with pytest.raises(ValueError):
            EventLabel(priority=2).to_api_body()


class TestCalendarClientListEvents:
    def test_list_events_maps_response_items(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {
            "items": [
                api_event(
                    "1", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
                ),
                api_event(
                    "2", "2026-01-01T11:00:00+00:00", "2026-01-01T12:00:00+00:00"
                ),
            ]
        }
        client = make_client(service)

        events = client.list_events(
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC), datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        )

        assert [e.id for e in events] == ["1", "2"]
        service.events.return_value.list.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID,
            timeMin="2026-01-01T00:00:00+00:00",
            timeMax="2026-01-02T00:00:00+00:00",
            singleEvents=True,
            orderBy="startTime",
        )

    def test_list_events_returns_empty_list_when_no_items(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {}
        client = make_client(service)

        events = client.list_events(
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC), datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        )

        assert events == []


class TestCalendarClientGetEvent:
    def test_get_event_returns_parsed_event(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        client = make_client(service)

        event = client.get_event("abc123")

        assert event.id == "abc123"
        service.events.return_value.get.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID, eventId="abc123"
        )


class TestCalendarClientCreateEvent:
    def test_create_event_sends_body_and_returns_parsed_event(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = api_event(
            "new-id", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00", summary="New"
        )
        client = make_client(service)
        event = Event(
            summary="New",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )

        result = client.create_event(event)

        assert result.id == "new-id"
        service.events.return_value.insert.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID, body=event.to_api_body()
        )


class TestCalendarClientUpdateEvent:
    def test_update_event_requires_id(self):
        client = make_client(MagicMock())
        event = Event(
            summary="No id",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )

        with pytest.raises(ValueError):
            client.update_event(event)

    def test_update_event_patches_and_returns_parsed_event(self):
        service = MagicMock()
        service.events.return_value.patch.return_value.execute.return_value = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T11:00:00+00:00"
        )
        client = make_client(service)
        event = Event(
            id="abc123",
            summary="Busy",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
        )

        result = client.update_event(event)

        assert result.end == datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
        service.events.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID, eventId="abc123", body=event.to_api_body()
        )


class TestCalendarClientDeleteEvent:
    def test_delete_event_calls_delete_with_event_id(self):
        service = MagicMock()
        client = make_client(service)

        client.delete_event("abc123")

        service.events.return_value.delete.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID, eventId="abc123"
        )
        service.events.return_value.delete.return_value.execute.assert_called_once()


class TestCalendarClientListEventLabels:
    def test_returns_parsed_labels(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "labelProperties": {
                "eventLabels": [
                    {"id": "l1", "backgroundColor": "#8e24aa", "name": "Design Work"},
                    {"id": "l2", "backgroundColor": "#d50000"},
                ]
            }
        }
        client = make_client(service)

        labels = client.list_event_labels()

        assert [label.id for label in labels] == ["l1", "l2"]
        assert labels[0].name == "Design Work"
        assert labels[1].name is None
        service.calendars.return_value.get.assert_called_once_with(calendarId=TEST_CALENDAR_ID)

    def test_returns_empty_list_when_no_labels_defined(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {}
        client = make_client(service)

        assert client.list_event_labels() == []


class TestCalendarClientCreateEventLabel:
    def test_appends_new_label_and_returns_it(self):
        service = MagicMock()
        existing = {"id": "l1", "backgroundColor": "#d50000"}
        service.calendars.return_value.get.return_value.execute.return_value = {
            "labelProperties": {"eventLabels": [existing]}
        }
        service.calendars.return_value.patch.return_value.execute.return_value = {
            "labelProperties": {
                "eventLabels": [
                    existing,
                    {"id": "l2", "backgroundColor": "#8e24aa", "name": "Design Work"},
                ]
            }
        }
        client = make_client(service)

        created = client.create_event_label("#8e24aa", "Design Work")

        assert created.id == "l2"
        assert created.background_color == "#8e24aa"
        assert created.name == "Design Work"
        service.calendars.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID,
            body={
                "labelProperties": {
                    "eventLabels": [
                        existing,
                        {"backgroundColor": "#8e24aa", "name": "Design Work"},
                    ]
                }
            },
        )

    def test_creates_first_label_when_none_exist(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {}
        service.calendars.return_value.patch.return_value.execute.return_value = {
            "labelProperties": {"eventLabels": [{"id": "l1", "backgroundColor": "#8e24aa"}]}
        }
        client = make_client(service)

        created = client.create_event_label("#8e24aa")

        assert created.id == "l1"
        assert created.name is None

    def test_creates_label_from_priority_alone(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {}
        service.calendars.return_value.patch.return_value.execute.return_value = {
            "labelProperties": {
                "eventLabels": [{"id": "l1", "backgroundColor": "#fbd75b", "name": "P1 Design Work"}]
            }
        }
        client = make_client(service)

        client.create_event_label(name="Design Work", priority=1)

        service.calendars.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID,
            body={
                "labelProperties": {
                    "eventLabels": [{"backgroundColor": "#fbd75b", "name": "P1 Design Work"}]
                }
            },
        )


class TestCalendarClientUpdateEventLabel:
    def test_raises_when_label_not_found(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "labelProperties": {"eventLabels": []}
        }
        client = make_client(service)

        with pytest.raises(ValueError):
            client.update_event_label("missing", background_color="#000000")

    def test_updates_background_color_only(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "labelProperties": {
                "eventLabels": [{"id": "l1", "backgroundColor": "#d50000", "name": "Old"}]
            }
        }
        service.calendars.return_value.patch.return_value.execute.return_value = {
            "labelProperties": {
                "eventLabels": [{"id": "l1", "backgroundColor": "#8e24aa", "name": "Old"}]
            }
        }
        client = make_client(service)

        updated = client.update_event_label("l1", background_color="#8e24aa")

        assert updated.background_color == "#8e24aa"
        assert updated.name == "Old"
        service.calendars.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID,
            body={
                "labelProperties": {
                    "eventLabels": [{"id": "l1", "backgroundColor": "#8e24aa", "name": "Old"}]
                }
            },
        )

    def test_updates_name_only_keeping_existing_background_color(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "labelProperties": {
                "eventLabels": [{"id": "l1", "backgroundColor": "#d50000", "name": "Old"}]
            }
        }
        service.calendars.return_value.patch.return_value.execute.return_value = {
            "labelProperties": {
                "eventLabels": [{"id": "l1", "backgroundColor": "#d50000", "name": "New"}]
            }
        }
        client = make_client(service)

        updated = client.update_event_label("l1", name="New")

        assert updated.background_color == "#d50000"
        assert updated.name == "New"
        service.calendars.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID,
            body={
                "labelProperties": {
                    "eventLabels": [{"id": "l1", "backgroundColor": "#d50000", "name": "New"}]
                }
            },
        )

    def test_renaming_a_prioritized_label_keeps_its_priority_prefix(self):
        # The label's stored name already has priority baked into it as a
        # "P1 " prefix; renaming it must not lose that prefix just
        # because only `name` was given here.
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "labelProperties": {
                "eventLabels": [{"id": "l1", "backgroundColor": "#fbd75b", "name": "P1 Old"}]
            }
        }
        service.calendars.return_value.patch.return_value.execute.return_value = {
            "labelProperties": {
                "eventLabels": [{"id": "l1", "backgroundColor": "#fbd75b", "name": "P1 New"}]
            }
        }
        client = make_client(service)

        updated = client.update_event_label("l1", name="New")

        assert updated.priority == 1
        assert updated.name == "New"
        service.calendars.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID,
            body={
                "labelProperties": {
                    "eventLabels": [{"id": "l1", "backgroundColor": "#fbd75b", "name": "P1 New"}]
                }
            },
        )

    def test_updating_priority_recolors_a_label_using_the_derived_color(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "labelProperties": {
                "eventLabels": [{"id": "l1", "backgroundColor": "#fbd75b", "name": "P1 Design Work"}]
            }
        }
        service.calendars.return_value.patch.return_value.execute.return_value = {
            "labelProperties": {
                "eventLabels": [{"id": "l1", "backgroundColor": "#7ae7bf", "name": "P3 Design Work"}]
            }
        }
        client = make_client(service)

        updated = client.update_event_label("l1", priority=3)

        assert updated.priority == 3
        service.calendars.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID,
            body={
                "labelProperties": {
                    "eventLabels": [{"id": "l1", "backgroundColor": "#7ae7bf", "name": "P3 Design Work"}]
                }
            },
        )


class TestCalendarClientDeleteEventLabel:
    def test_raises_when_label_not_found(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "labelProperties": {"eventLabels": []}
        }
        client = make_client(service)

        with pytest.raises(ValueError):
            client.delete_event_label("missing")

    def test_removes_label_and_returns_it(self):
        service = MagicMock()
        target = {"id": "l1", "backgroundColor": "#8e24aa", "name": "Design Work"}
        other = {"id": "l2", "backgroundColor": "#d50000"}
        service.calendars.return_value.get.return_value.execute.return_value = {
            "labelProperties": {"eventLabels": [target, other]}
        }
        service.calendars.return_value.patch.return_value.execute.return_value = {
            "labelProperties": {"eventLabels": [other]}
        }
        client = make_client(service)

        removed = client.delete_event_label("l1")

        assert removed.id == "l1"
        assert removed.background_color == "#8e24aa"
        assert removed.name == "Design Work"
        service.calendars.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID,
            body={"labelProperties": {"eventLabels": [other]}},
        )


class TestCalendarClientEventLabelEtagGuard:
    def test_patch_sets_if_match_header_from_get_etag(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "etag": '"abc123"',
            "labelProperties": {"eventLabels": []},
        }
        service.calendars.return_value.patch.return_value.execute.return_value = {
            "labelProperties": {"eventLabels": [{"id": "l1", "backgroundColor": "#8e24aa"}]}
        }
        client = make_client(service)

        client.create_event_label("#8e24aa")

        request = service.calendars.return_value.patch.return_value
        request.headers.__setitem__.assert_called_once_with("If-Match", '"abc123"')

    def test_skips_if_match_when_no_etag(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "labelProperties": {"eventLabels": []}
        }
        service.calendars.return_value.patch.return_value.execute.return_value = {
            "labelProperties": {"eventLabels": [{"id": "l1", "backgroundColor": "#8e24aa"}]}
        }
        client = make_client(service)

        client.create_event_label("#8e24aa")

        request = service.calendars.return_value.patch.return_value
        request.headers.__setitem__.assert_not_called()

    def test_raises_conflict_error_on_412(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "etag": '"stale"',
            "labelProperties": {"eventLabels": []},
        }
        service.calendars.return_value.patch.return_value.execute.side_effect = HttpError(
            MagicMock(status=412), b"Precondition check failed."
        )
        client = make_client(service)

        with pytest.raises(EventLabelConflictError):
            client.create_event_label("#8e24aa")

    def test_other_http_errors_are_not_treated_as_conflicts(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "etag": '"abc"',
            "labelProperties": {"eventLabels": []},
        }
        service.calendars.return_value.patch.return_value.execute.side_effect = HttpError(
            MagicMock(status=500), b"Internal error."
        )
        client = make_client(service)

        with pytest.raises(HttpError):
            client.create_event_label("#8e24aa")


class TestLoadCredentials:
    def _mock_expired_creds(self) -> MagicMock:
        creds = MagicMock()
        creds.valid = False
        creds.expired = True
        creds.refresh_token = "refresh-token"
        creds.to_json.return_value = "{}"
        return creds

    def test_refreshes_and_rewrites_token_path(self, monkeypatch):
        creds = self._mock_expired_creds()
        monkeypatch.setattr(
            google_calendar.Credentials,
            "from_authorized_user_file",
            MagicMock(return_value=creds),
        )
        token_path = MagicMock(spec=Path)
        token_path.exists.return_value = True

        result = load_credentials(token_path, Path("credentials.json"))

        assert result is creds
        creds.refresh.assert_called_once()
        token_path.write_text.assert_called_once_with("{}")

    def test_swallows_oserror_when_token_path_is_not_writable(self, monkeypatch, caplog):
        creds = self._mock_expired_creds()
        monkeypatch.setattr(
            google_calendar.Credentials,
            "from_authorized_user_file",
            MagicMock(return_value=creds),
        )
        token_path = MagicMock(spec=Path)
        token_path.exists.return_value = True
        token_path.write_text.side_effect = OSError("Read-only file system")

        with caplog.at_level("WARNING", logger="calendar_clients.google_calendar"):
            result = load_credentials(token_path, Path("credentials.json"))

        assert result is creds
        creds.refresh.assert_called_once()
        assert any(
            "Could not write refreshed credentials" in record.message
            for record in caplog.records
        )
