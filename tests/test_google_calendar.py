from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call
from zoneinfo import ZoneInfo

import pytest

from calendar_clients import google_calendar
from calendar_clients.google_calendar import Calendar, CalendarClient, Event, load_credentials
from utilities.reallocation import ReallocationOptions

UTC = timezone.utc
EST = timezone(timedelta(hours=-5))
CET = timezone(timedelta(hours=1))
IST = timezone(timedelta(hours=5, minutes=30))
JST = timezone(timedelta(hours=9))
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

    @pytest.mark.parametrize(
        ("start", "end", "expected"),
        [
            (
                datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
                datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
                True,
            ),
            (
                datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
                datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
                False,
            ),
            (
                datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
                datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
                False,
            ),
            (
                datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
                datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
                True,
            ),
        ],
    )
    def test_overlaps(self, start, end, expected):
        event = Event(
            summary="Existing",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )

        assert event.overlaps(start, end) is expected

    def test_overlaps_true_across_four_distinct_timezones(self):
        event = Event(
            summary="Existing",
            start=datetime(2026, 1, 1, 4, 0, tzinfo=EST),  # 09:00 UTC
            end=datetime(2026, 1, 1, 11, 0, tzinfo=CET),  # 10:00 UTC
        )

        assert event.overlaps(
            datetime(2026, 1, 1, 15, 0, tzinfo=IST),  # 09:30 UTC
            datetime(2026, 1, 1, 19, 30, tzinfo=JST),  # 10:30 UTC
        )

    def test_overlaps_false_across_four_distinct_timezones(self):
        event = Event(
            summary="Existing",
            start=datetime(2026, 1, 1, 4, 0, tzinfo=EST),  # 09:00 UTC
            end=datetime(2026, 1, 1, 11, 0, tzinfo=CET),  # 10:00 UTC
        )

        assert not event.overlaps(
            datetime(2026, 1, 1, 16, 0, tzinfo=IST),  # 10:30 UTC
            datetime(2026, 1, 1, 20, 30, tzinfo=JST),  # 11:30 UTC
        )

    def test_overlaps_asserts_self_start_before_self_end(self):
        event = Event(
            summary="Backwards",
            start=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        )

        with pytest.raises(AssertionError):
            event.overlaps(
                datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
                datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
            )

    def test_overlaps_asserts_other_start_before_other_end(self):
        event = Event(
            summary="Existing",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )

        with pytest.raises(AssertionError):
            event.overlaps(
                datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
                datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            )

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


class TestCalendarClientHasOverlap:
    def test_has_overlap_true_when_existing_event_overlaps(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {
            "items": [
                api_event(
                    "1", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
                )
            ]
        }
        client = make_client(service)

        assert client.has_overlap(
            datetime(2026, 1, 1, 9, 30, tzinfo=UTC), datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
        )

    def test_has_overlap_false_when_no_events(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.return_value = {"items": []}
        client = make_client(service)

        assert not client.has_overlap(
            datetime(2026, 1, 1, 9, 30, tzinfo=UTC), datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
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


class TestCalendarClientListDayEvents:
    def test_fetches_a_24_hour_window_from_start(self):
        client = make_client(MagicMock())
        client.list_events = MagicMock(return_value=[])
        start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

        client.list_day_events(start)

        client.list_events.assert_called_once_with(start, start + timedelta(hours=24))

    def test_truncates_after_the_end_of_day_sleep_event(self):
        client = make_client(MagicMock())
        start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        kept = Event(id="k", start=start, end=start + timedelta(hours=1))
        sleep = Event(
            id="s",
            start=start + timedelta(hours=1),
            end=start + timedelta(hours=2),
            is_end_of_day_sleep=True,
        )
        discarded = Event(id="d", start=start + timedelta(hours=3), end=start + timedelta(hours=4))
        client.list_events = MagicMock(return_value=[kept, sleep, discarded])

        events = client.list_day_events(start)

        assert [e.id for e in events] == ["k", "s"]

    def test_keeps_everything_when_no_sleep_event_found(self):
        client = make_client(MagicMock())
        start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        events_in = [Event(id="a", start=start, end=start + timedelta(hours=1))]
        client.list_events = MagicMock(return_value=events_in)

        events = client.list_day_events(start)

        assert events == events_in


class TestCalendarClientCreateEventWithReallocation:
    def test_requires_start_and_end(self):
        client = make_client(MagicMock())

        with pytest.raises(ValueError):
            client.create_event_with_reallocation(Event(summary="No times"), ReallocationOptions())

    def test_creates_new_event_when_day_is_otherwise_clear(self):
        client = make_client(MagicMock())
        start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        end = start + timedelta(minutes=30)
        anchor = Event(
            id="a1", start=start + timedelta(hours=2), end=start + timedelta(hours=3), priority=1
        )
        client.list_events = MagicMock(return_value=[anchor])
        created = Event(id="new-id", summary="New", start=start, end=end)
        client.create_event = MagicMock(return_value=created)
        client.update_event = MagicMock()

        new_event = Event(summary="New", start=start, end=end, priority=1)
        result = client.create_event_with_reallocation(new_event, ReallocationOptions())

        assert result == [created]
        client.create_event.assert_called_once_with(new_event)
        client.update_event.assert_not_called()

    def test_applies_update_for_events_reallocation_touches(self):
        preceding = Event(
            id="p1",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 40, tzinfo=UTC),
            priority=1,
            min_duration=timedelta(minutes=30),
        )
        later = Event(
            id="l1",
            start=datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 11, 30, tzinfo=UTC),
            priority=1,
        )
        client = make_client(MagicMock())
        client.list_events = MagicMock(return_value=[preceding, later])
        client.create_event = MagicMock(side_effect=lambda event: event)
        client.update_event = MagicMock(side_effect=lambda event: event)

        new_event = Event(
            summary="New",
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=1,
        )
        result = client.create_event_with_reallocation(new_event, ReallocationOptions())

        client.create_event.assert_called_once_with(new_event)
        client.update_event.assert_called_once_with(preceding)
        # later is never reclaimed from (a gap absorbs it), so it's left
        # out of the plan entirely -- not created, not updated.
        assert result == [preceding, new_event]


class TestCalendarClientUpdateEventAndReallocate:
    def test_requires_id(self):
        client = make_client(MagicMock())
        start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

        with pytest.raises(ValueError):
            client.update_event_and_reallocate(
                Event(summary="No id", start=start, end=start + timedelta(minutes=30)),
                ReallocationOptions(),
            )

    def test_requires_start_or_end(self):
        client = make_client(MagicMock())

        with pytest.raises(ValueError):
            client.update_event_and_reallocate(
                Event(id="abc123", summary="No times"), ReallocationOptions()
            )

    def test_fills_in_missing_end_from_list_day_events(self, monkeypatch):
        client = make_client(MagicMock())
        start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        current_end = start + timedelta(hours=1)
        current = Event(id="abc123", start=start, end=current_end, priority=1)
        client.list_events = MagicMock(return_value=[current])
        client.get_event = MagicMock()

        captured = {}

        def fake_reallocate(day_events, event, options):
            captured["event"] = event
            return [event]

        monkeypatch.setattr(google_calendar, "reallocate_for_new_event", fake_reallocate)
        client.update_event = MagicMock(side_effect=lambda event: event)

        new_start = start + timedelta(minutes=15)
        updated_event = Event(id="abc123", start=new_start, priority=1)

        client.update_event_and_reallocate(updated_event, ReallocationOptions())

        assert captured["event"].end == current_end
        client.get_event.assert_not_called()

    def test_fills_in_missing_start_from_list_day_events(self, monkeypatch):
        client = make_client(MagicMock())
        start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        end = start + timedelta(hours=1)
        current = Event(id="abc123", start=start, end=end, priority=1)
        client.list_events = MagicMock(return_value=[current])
        client.get_event = MagicMock()

        captured = {}

        def fake_reallocate(day_events, event, options):
            captured["event"] = event
            return [event]

        monkeypatch.setattr(google_calendar, "reallocate_for_new_event", fake_reallocate)
        client.update_event = MagicMock(side_effect=lambda event: event)

        new_end = end + timedelta(minutes=30)
        updated_event = Event(id="abc123", end=new_end, priority=1)

        client.update_event_and_reallocate(updated_event, ReallocationOptions())

        assert captured["event"].start == start
        client.get_event.assert_not_called()

    def test_falls_back_to_get_event_when_not_found_in_list_day_events(self, monkeypatch):
        # Only `end` is given, so the initial lookup anchors list_day_events
        # on that new end -- if the event's actual current position doesn't
        # fall within that window (e.g. it's actually much earlier in the
        # day), it won't be there, and an explicit get_event is required.
        client = make_client(MagicMock())
        end = datetime(2026, 1, 1, 17, 0, tzinfo=UTC)
        real_start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        real_end = real_start + timedelta(hours=1)
        client.list_events = MagicMock(return_value=[])
        client.get_event = MagicMock(
            return_value=Event(id="abc123", start=real_start, end=real_end, priority=1)
        )

        captured = {}

        def fake_reallocate(day_events, event, options):
            captured["event"] = event
            return [event]

        monkeypatch.setattr(google_calendar, "reallocate_for_new_event", fake_reallocate)
        client.update_event = MagicMock(side_effect=lambda event: event)

        updated_event = Event(id="abc123", end=end, priority=1)

        client.update_event_and_reallocate(updated_event, ReallocationOptions())

        client.get_event.assert_called_once_with("abc123")
        assert captured["event"].start == real_start

    def test_excludes_its_own_prior_position_from_day_events(self):
        client = make_client(MagicMock())
        start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        end = start + timedelta(minutes=30)
        # The event being updated is still present in what list_events
        # returns (its prior position, before this update is applied) --
        # update_event_and_reallocate must filter it out of day_events
        # itself, since reallocate_for_new_event refuses a day_events list
        # that already contains the moved event's id.
        prior_position = Event(id="moved", start=start, end=end, priority=1)
        anchor = Event(
            id="a1", start=start + timedelta(hours=2), end=start + timedelta(hours=3), priority=1
        )
        client.list_events = MagicMock(return_value=[prior_position, anchor])
        updated = Event(id="moved", summary="Moved", start=start, end=end)
        client.update_event = MagicMock(return_value=updated)
        client.create_event = MagicMock()

        moved_event = Event(id="moved", summary="Moved", start=start, end=end, priority=1)
        result = client.update_event_and_reallocate(moved_event, ReallocationOptions())

        assert result == [updated]
        client.update_event.assert_called_once_with(moved_event)
        client.create_event.assert_not_called()

    def test_applies_update_for_events_reallocation_touches(self):
        preceding = Event(
            id="p1",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 40, tzinfo=UTC),
            priority=1,
            min_duration=timedelta(minutes=30),
        )
        later = Event(
            id="l1",
            start=datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 11, 30, tzinfo=UTC),
            priority=1,
        )
        # moved's prior position -- must be excluded from day_events by id,
        # not treated as ordinary same-day competition for reclaimed time.
        moved_prior_position = Event(
            id="m1",
            start=datetime(2026, 1, 1, 11, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            priority=1,
        )
        client = make_client(MagicMock())
        client.list_events = MagicMock(return_value=[preceding, later, moved_prior_position])
        client.create_event = MagicMock(side_effect=lambda event: event)
        client.update_event = MagicMock(side_effect=lambda event: event)

        moved_event = Event(
            id="m1",
            summary="Moved earlier",
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=1,
        )
        result = client.update_event_and_reallocate(moved_event, ReallocationOptions())

        assert client.update_event.call_args_list == [call(preceding), call(moved_event)]
        client.create_event.assert_not_called()
        # later is never reclaimed from (a gap absorbs it), so it's left
        # out of the plan entirely -- not created, not updated.
        assert result == [preceding, moved_event]


class TestCalendarClientDeleteEvent:
    def test_delete_event_calls_delete_with_event_id(self):
        service = MagicMock()
        client = make_client(service)

        client.delete_event("abc123")

        service.events.return_value.delete.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID, eventId="abc123"
        )
        service.events.return_value.delete.return_value.execute.assert_called_once()


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
