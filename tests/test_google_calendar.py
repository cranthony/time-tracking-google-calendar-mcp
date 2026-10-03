from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from googleapiclient.errors import HttpError

from calendar_clients.google_calendar import (
    Calendar,
    CalendarClient,
    Event,
    EventLabel,
    EventLabelConflictError,
)
from calendar_clients.write_lock import WriteLockNotHeldError

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
    @pytest.mark.parametrize(
        "goal_ids, stored",
        [(["g1", "g2"], "g1 g2"), ([], "")],  # [] is stored, so clearing goals sticks
    )
    def test_goal_ids_round_trip_through_a_private_extended_property(self, goal_ids, stored):
        body = Event(goal_ids=goal_ids).to_api_body()

        assert body["extendedProperties"]["private"] == {"cascading-time-tracker-goal_ids": stored}
        data = api_event("1", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        data["extendedProperties"] = body["extendedProperties"]
        assert Event.from_api(data).goal_ids == goal_ids

    def test_goal_ids_are_none_when_never_set(self):
        assert Event.from_api(api_event("1", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")).goal_ids is None
        assert "extendedProperties" not in Event().to_api_body()

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
        assert event.is_fixed_time is None
        assert event.priority is None
        assert event.is_end_of_day_sleep is None
        assert event.event_label_id is None

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

    def test_from_api_extracts_a_series_rules_and_time_zone(self):
        data = api_event("abc123", "2026-01-05T09:00:00-05:00", "2026-01-05T10:00:00-05:00")
        data["start"]["timeZone"] = "America/New_York"
        data["recurrence"] = ["RRULE:FREQ=WEEKLY;BYDAY=MO"]

        event = Event.from_api(data)

        assert event.recurrence == ["RRULE:FREQ=WEEKLY;BYDAY=MO"]
        assert event.time_zone == "America/New_York"

    def test_from_api_extracts_an_instances_original_start(self):
        data = api_event("abc123_x", "2026-01-06T15:00:00+00:00", "2026-01-06T16:00:00+00:00")
        data["originalStartTime"] = {"dateTime": "2026-01-05T14:00:00+00:00"}

        assert Event.from_api(data).original_start == datetime(2026, 1, 5, 14, tzinfo=UTC)

    def test_to_api_body_sends_rules_and_time_zone_but_never_original_start(self):
        start = datetime(2026, 1, 5, 9, tzinfo=EST)
        event = Event(
            start=start,
            end=start + timedelta(hours=1),
            time_zone="America/New_York",
            recurrence=["RRULE:FREQ=DAILY"],
            original_start=start,
        )

        body = event.to_api_body()

        assert body["recurrence"] == ["RRULE:FREQ=DAILY"]
        assert body["start"]["timeZone"] == body["end"]["timeZone"] == "America/New_York"
        assert "originalStartTime" not in body

    def test_from_api_extracts_event_label_id(self):
        data = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        data["eventLabelId"] = "label-1"

        event = Event.from_api(data)

        assert event.event_label_id == "label-1"

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

    def test_from_api_forces_min_duration_to_full_duration_when_fixed_time(self):
        data = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        data["extendedProperties"] = {
            "private": {
                "cascading-time-tracker-min_duration": "30",
                "cascading-time-tracker-is_fixed_time": "true",
            }
        }

        event = Event.from_api(data)

        assert event.min_duration == timedelta(hours=1)
        assert event.is_fixed_time is True

    def test_from_api_parses_is_fixed_duration_false(self):
        data = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        data["extendedProperties"] = {
            "private": {"cascading-time-tracker-is_fixed_duration": "false"}
        }

        event = Event.from_api(data)

        assert event.is_fixed_duration is False

    def test_from_api_parses_is_fixed_time_false(self):
        data = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        data["extendedProperties"] = {
            "private": {"cascading-time-tracker-is_fixed_time": "false"}
        }

        event = Event.from_api(data)

        assert event.is_fixed_time is False

    def test_from_api_ignores_other_private_keys(self):
        data = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        data["extendedProperties"] = {"private": {"someOtherApp-key": "value"}}

        event = Event.from_api(data)

        assert event.min_duration is None
        assert event.is_fixed_duration is None
        assert event.is_fixed_time is None
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

    def test_to_api_body_includes_event_label_id_when_present(self):
        event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            event_label_id="label-1",
        )

        assert event.to_api_body()["eventLabelId"] == "label-1"

    def test_to_api_body_omits_event_label_id_when_absent(self):
        event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )

        assert "eventLabelId" not in event.to_api_body()

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

    def test_to_api_body_includes_is_fixed_time_true(self):
        event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            is_fixed_time=True,
        )

        assert event.to_api_body()["extendedProperties"] == {
            "private": {"cascading-time-tracker-is_fixed_time": "true"}
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

    def test_has_no_priority_field(self):
        # Google Calendar has no field for a label's priority -- it's
        # sourced only from the goal that owns the label (see
        # utilities/goals.py), never this class -- even if the name happens
        # to look like it might encode one.
        label = EventLabel.from_api(
            {"id": "label-1", "backgroundColor": "#123456", "name": "P1 Design Work"}
        )

        assert not hasattr(label, "priority")
        assert label.name == "P1 Design Work"

    def test_background_color_is_required(self):
        with pytest.raises(TypeError):
            EventLabel(name="No color")


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
            maxResults=2500,
        )

    def test_list_events_follows_next_page_token_until_the_last_page(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.side_effect = [
            {
                "items": [api_event("1", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")],
                "nextPageToken": "page-2",
            },
            {
                "items": [api_event("2", "2026-01-01T11:00:00+00:00", "2026-01-01T12:00:00+00:00")],
                "nextPageToken": "page-3",
            },
            {"items": [api_event("3", "2026-01-01T13:00:00+00:00", "2026-01-01T14:00:00+00:00")]},
        ]
        client = make_client(service)

        events = client.list_events(
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC), datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        )

        assert [e.id for e in events] == ["1", "2", "3"]
        page_tokens = [
            call.kwargs.get("pageToken") for call in service.events.return_value.list.call_args_list
        ]
        assert page_tokens == [None, "page-2", "page-3"]

    def test_list_instances_includes_cancelled_ones_and_follows_pages(self):
        service = MagicMock()
        instances = service.events.return_value.instances
        instances.return_value.execute.side_effect = [
            {
                "items": [api_event("s_1", "2026-01-05T09:00:00+00:00", "2026-01-05T10:00:00+00:00")],
                "nextPageToken": "page-2",
            },
            {"items": [api_event("s_2", "2026-01-12T09:00:00+00:00", "2026-01-12T10:00:00+00:00")]},
        ]
        client = make_client(service)

        events = client.list_instances("s", datetime(2026, 1, 19, tzinfo=UTC))

        assert [e.id for e in events] == ["s_1", "s_2"]
        first = instances.call_args_list[0].kwargs
        assert (first["eventId"], first["showDeleted"], first["timeMax"]) == ("s", True, "2026-01-19T00:00:00+00:00")
        assert instances.call_args_list[1].kwargs["pageToken"] == "page-2"

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

    def test_create_event_sends_a_caller_chosen_id_so_a_retry_cannot_duplicate(self):
        service = MagicMock()
        insert = service.events.return_value.insert
        insert.return_value.execute.return_value = api_event(
            "cmpabc123s001", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        client = make_client(service)

        client.create_event(
            Event(
                id="cmpabc123s001",
                summary="Busy",
                start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
                end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            )
        )

        assert insert.call_args.kwargs["body"]["id"] == "cmpabc123s001"

    def test_create_event_without_an_id_lets_calendar_assign_one(self):
        service = MagicMock()
        insert = service.events.return_value.insert
        insert.return_value.execute.return_value = api_event(
            "assigned", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        client = make_client(service)

        client.create_event(
            Event(
                summary="Busy",
                start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
                end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            )
        )

        assert "id" not in insert.call_args.kwargs["body"]

    def test_create_event_passes_event_label_version_when_setting_event_label_id(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = api_event(
            "new-id", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00", summary="New"
        )
        client = make_client(service)
        event = Event(
            summary="New",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            event_label_id="label-1",
        )

        client.create_event(event)

        service.events.return_value.insert.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID, body=event.to_api_body(), eventLabelVersion=1
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

    def test_update_event_passes_event_label_version_when_setting_event_label_id(self):
        service = MagicMock()
        service.events.return_value.patch.return_value.execute.return_value = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T11:00:00+00:00"
        )
        client = make_client(service)
        event = Event(id="abc123", event_label_id="label-1")

        client.update_event(event)

        service.events.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID,
            eventId="abc123",
            body=event.to_api_body(),
            eventLabelVersion=1,
        )

    def test_update_event_omits_event_label_version_when_not_touching_event_label_id(self):
        service = MagicMock()
        service.events.return_value.patch.return_value.execute.return_value = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T11:00:00+00:00"
        )
        client = make_client(service)
        event = Event(id="abc123", summary="Busy")

        client.update_event(event)

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
    def test_returns_parsed_labels_and_etag(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "etag": '"abc123"',
            "labelProperties": {
                "eventLabels": [
                    {"id": "l1", "backgroundColor": "#8e24aa", "name": "Design Work"},
                    {"id": "l2", "backgroundColor": "#d50000"},
                ]
            }
        }
        client = make_client(service)

        labels, etag = client.list_event_labels()

        assert [label.id for label in labels] == ["l1", "l2"]
        assert labels[0].name == "Design Work"
        assert labels[1].name is None
        assert etag == '"abc123"'
        service.calendars.return_value.get.assert_called_once_with(calendarId=TEST_CALENDAR_ID)

    def test_returns_empty_list_when_no_labels_defined(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {}
        client = make_client(service)

        labels, etag = client.list_event_labels()

        assert labels == []
        assert etag is None


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

    def test_renaming_a_label_keeps_its_background_color(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "labelProperties": {
                "eventLabels": [{"id": "l1", "backgroundColor": "#fbd75b", "name": "Old"}]
            }
        }
        service.calendars.return_value.patch.return_value.execute.return_value = {
            "labelProperties": {
                "eventLabels": [{"id": "l1", "backgroundColor": "#fbd75b", "name": "New"}]
            }
        }
        client = make_client(service)

        updated = client.update_event_label("l1", name="New")

        assert updated.name == "New"
        assert updated.background_color == "#fbd75b"
        service.calendars.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID,
            body={
                "labelProperties": {
                    "eventLabels": [{"id": "l1", "backgroundColor": "#fbd75b", "name": "New"}]
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


class TestCalendarClientReplaceEventLabels:
    def test_creates_updates_and_deletes_in_one_call(self):
        # replace_event_labels no longer fetches its own etag (unlike
        # create/update/delete_event_label) -- it's the caller's job to
        # supply one (e.g. from a prior list_event_labels() call), since
        # utilities/goals.py's Goals does other work (validating, writing
        # the sheet) between reading the etag and writing.
        service = MagicMock()
        service.calendars.return_value.patch.return_value.execute.return_value = {
            "labelProperties": {
                "eventLabels": [
                    {"id": "l1", "backgroundColor": "#d50000", "name": "Renamed"},
                    {"id": "l3", "backgroundColor": "#8e24aa", "name": "New"},
                ]
            }
        }
        client = make_client(service)

        updated = client.replace_event_labels(
            [
                EventLabel(id="l1", background_color="#d50000", name="Renamed"),
                EventLabel(background_color="#8e24aa", name="New"),
            ],
            '"etag-1"',
        )

        assert [label.id for label in updated] == ["l1", "l3"]
        service.calendars.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID,
            body={
                "labelProperties": {
                    "eventLabels": [
                        {"id": "l1", "backgroundColor": "#d50000", "name": "Renamed"},
                        {"backgroundColor": "#8e24aa", "name": "New"},
                    ]
                }
            },
        )
        request = service.calendars.return_value.patch.return_value
        request.headers.__setitem__.assert_called_once_with("If-Match", '"etag-1"')

    def test_replacing_with_an_empty_list_deletes_every_label(self):
        service = MagicMock()
        service.calendars.return_value.patch.return_value.execute.return_value = {}
        client = make_client(service)

        updated = client.replace_event_labels([])

        assert updated == []
        service.calendars.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID,
            body={"labelProperties": {"eventLabels": []}},
        )

    def test_skips_if_match_when_no_etag_given(self):
        service = MagicMock()
        service.calendars.return_value.patch.return_value.execute.return_value = {}
        client = make_client(service)

        client.replace_event_labels([])

        request = service.calendars.return_value.patch.return_value
        request.headers.__setitem__.assert_not_called()

    def test_raises_conflict_error_on_412(self):
        service = MagicMock()
        service.calendars.return_value.patch.return_value.execute.side_effect = HttpError(
            MagicMock(status=412), b"Precondition check failed."
        )
        client = make_client(service)

        with pytest.raises(EventLabelConflictError):
            client.replace_event_labels([], etag='"stale"')


class TestCalendarClientGetCalendarMetadata:
    def test_returns_none_when_description_is_absent(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {}
        client = make_client(service)

        assert client.get_calendar_metadata("event-label-sheet-id") is None

    def test_returns_none_when_key_has_no_marker(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "description": "A calendar for time tracking."
        }
        client = make_client(service)

        assert client.get_calendar_metadata("event-label-sheet-id") is None

    def test_returns_value_from_marker_alongside_human_text(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "description": (
                "A calendar for time tracking.\n"
                "[cascading-time-tracker:event-label-sheet-id=sheet-1]"
            )
        }
        client = make_client(service)

        assert client.get_calendar_metadata("event-label-sheet-id") == "sheet-1"

    def test_ignores_markers_for_other_keys(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "description": "[cascading-time-tracker:some-other-key=other-value]"
        }
        client = make_client(service)

        assert client.get_calendar_metadata("event-label-sheet-id") is None


class TestCalendarClientSetCalendarMetadata:
    def test_appends_marker_to_existing_human_description(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "etag": '"abc"',
            "description": "A calendar for time tracking.",
        }
        client = make_client(service)

        client.set_calendar_metadata("event-label-sheet-id", "sheet-1")

        service.calendars.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID,
            body={
                "description": (
                    "A calendar for time tracking.\n"
                    "[cascading-time-tracker:event-label-sheet-id=sheet-1]"
                )
            },
        )

    def test_replaces_an_existing_marker_for_the_same_key(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "description": "[cascading-time-tracker:event-label-sheet-id=old-sheet]"
        }
        client = make_client(service)

        client.set_calendar_metadata("event-label-sheet-id", "new-sheet")

        service.calendars.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID,
            body={"description": "[cascading-time-tracker:event-label-sheet-id=new-sheet]"},
        )

    def test_leaves_markers_for_other_keys_untouched(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "description": "[cascading-time-tracker:some-other-key=other-value]"
        }
        client = make_client(service)

        client.set_calendar_metadata("event-label-sheet-id", "sheet-1")

        service.calendars.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID,
            body={
                "description": (
                    "[cascading-time-tracker:some-other-key=other-value]\n"
                    "[cascading-time-tracker:event-label-sheet-id=sheet-1]"
                )
            },
        )

    def test_none_value_removes_the_marker(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "description": "A calendar for time tracking.\n"
            "[cascading-time-tracker:event-label-sheet-id=sheet-1]"
        }
        client = make_client(service)

        client.set_calendar_metadata("event-label-sheet-id", None)

        service.calendars.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID,
            body={"description": "A calendar for time tracking."},
        )

    def test_sets_if_match_header_from_get_etag(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "etag": '"abc123"',
            "description": None,
        }
        client = make_client(service)

        client.set_calendar_metadata("event-label-sheet-id", "sheet-1")

        request = service.calendars.return_value.patch.return_value
        request.headers.__setitem__.assert_called_once_with("If-Match", '"abc123"')

    def test_raises_conflict_error_on_412(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {
            "etag": '"stale"',
            "description": None,
        }
        service.calendars.return_value.patch.return_value.execute.side_effect = HttpError(
            MagicMock(status=412), b"Precondition check failed."
        )
        client = make_client(service)

        with pytest.raises(EventLabelConflictError):
            client.set_calendar_metadata("event-label-sheet-id", "sheet-1")


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


class TestCalendarClientGoalHealthCalls:
    """The raw calls utilities/goal_health.py makes for its own calendar."""

    def test_upsert_inserts_with_the_given_id(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = {"id": "abc"}

        result = make_client(service).upsert_event_resource("abc", {"summary": "x"})

        assert result == {"id": "abc"}
        service.events.return_value.insert.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID, body={"summary": "x", "id": "abc"}
        )
        service.events.return_value.patch.assert_not_called()

    def test_upsert_overwrites_an_event_that_already_exists(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.side_effect = HttpError(
            MagicMock(status=409), b"duplicate"
        )
        service.events.return_value.patch.return_value.execute.return_value = {"id": "abc", "summary": "y"}

        result = make_client(service).upsert_event_resource("abc", {"summary": "y"})

        assert result["summary"] == "y"
        service.events.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID, eventId="abc", body={"summary": "y"}
        )

    def test_upsert_raises_other_errors(self):
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.side_effect = HttpError(
            MagicMock(status=500), b"boom"
        )

        with pytest.raises(HttpError):
            make_client(service).upsert_event_resource("abc", {})

    def test_lists_raw_events_by_private_property_across_pages(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.side_effect = [
            {"items": [{"id": "1"}], "nextPageToken": "p2"},
            {"items": [{"id": "2"}]},
        ]
        start = datetime(2026, 9, 1, tzinfo=UTC)
        end = datetime(2026, 10, 1, tzinfo=UTC)

        items = make_client(service).list_event_resources(start, end, private_property="k=v")

        assert [i["id"] for i in items] == ["1", "2"]
        calls = service.events.return_value.list.call_args_list
        assert all(c.kwargs["privateExtendedProperty"] == "k=v" for c in calls)
        assert calls[1].kwargs["pageToken"] == "p2"

    def test_time_zone_is_fetched_once(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {"timeZone": "Europe/Paris"}
        client = make_client(service)

        assert client.get_time_zone() == ZoneInfo("Europe/Paris")
        assert client.get_time_zone() == ZoneInfo("Europe/Paris")
        service.calendars.return_value.get.assert_called_once()

    def test_sets_the_time_zone_and_remembers_it(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.return_value = {"timeZone": "UTC"}
        client = make_client(service)
        assert client.get_time_zone() == ZoneInfo("UTC")

        assert client.set_time_zone("America/New_York") == ZoneInfo("America/New_York")

        service.calendars.return_value.patch.assert_called_once_with(
            calendarId=TEST_CALENDAR_ID, body={"timeZone": "America/New_York"}
        )
        assert client.get_time_zone() == ZoneInfo("America/New_York")
        service.calendars.return_value.get.assert_called_once()

    @pytest.mark.parametrize("name", ["Mars/Olympus_Mons", "america/new_york", "", "../etc/passwd"])
    def test_refuses_a_name_that_isnt_a_time_zone(self, name):
        service = MagicMock()

        with pytest.raises(ValueError, match="isn't a time zone"):
            make_client(service).set_time_zone(name)

        service.calendars.return_value.patch.assert_not_called()

    def test_creates_a_calendar_in_a_time_zone(self):
        service = MagicMock()
        service.calendars.return_value.insert.return_value.execute.return_value = {"id": "new-cal"}

        assert make_client(service).create_calendar("Goal Health", "d", time_zone="Europe/Paris") == "new-cal"
        service.calendars.return_value.insert.assert_called_once_with(
            body={"summary": "Goal Health", "description": "d", "timeZone": "Europe/Paris"}
        )

    def test_hiding_a_calendar_is_best_effort(self):
        service = MagicMock()
        service.calendarList.return_value.patch.return_value.execute.side_effect = HttpError(
            MagicMock(status=403), b"insufficient scope"
        )

        assert make_client(service).hide_calendar("cal") is False

    def test_colors_a_calendar_in_the_users_list(self):
        service = MagicMock()

        assert make_client(service).color_calendar("cal", "#d50000") is True
        service.calendarList.return_value.patch.assert_called_once_with(
            calendarId="cal",
            colorRgbFormat=True,
            body={"backgroundColor": "#d50000", "foregroundColor": "#ffffff"},
        )

    def test_coloring_a_calendar_is_best_effort(self):
        service = MagicMock()
        service.calendarList.return_value.patch.return_value.execute.side_effect = HttpError(
            MagicMock(status=403), b"insufficient scope"
        )

        assert make_client(service).color_calendar("cal", "#d50000") is False

    def test_lists_every_event_on_a_calendar_across_pages(self):
        service = MagicMock()
        service.events.return_value.list.return_value.execute.side_effect = [
            {"items": [{"id": "1"}], "nextPageToken": "p2"},
            {"items": [{"id": "2"}]},
        ]

        items = make_client(service).list_all_event_resources()

        assert [i["id"] for i in items] == ["1", "2"]
        calls = service.events.return_value.list.call_args_list
        assert "timeMin" not in calls[0].kwargs
        assert calls[1].kwargs["pageToken"] == "p2"

    def test_for_calendar_shares_the_service(self):
        service = MagicMock()

        other = make_client(service).for_calendar("other-cal")

        assert other.calendar_id == "other-cal"
        assert other._service is service


_CALENDAR_WRITES = [
    "create_calendar",
    "hide_calendar",
    "color_calendar",
    "set_time_zone",
    "upsert_event_resource",
    "create_event",
    "update_event",
    "delete_event",
    "create_event_label",
    "update_event_label",
    "delete_event_label",
    "replace_event_labels",
    "set_calendar_metadata",
    "_patch_calendar",
]


@pytest.mark.without_write_lock
class TestWritesRequireTheWriteLock:
    @pytest.mark.parametrize("method", _CALENDAR_WRITES)
    def test_a_write_refuses_without_it_and_sends_nothing(self, method):
        service = MagicMock()

        with pytest.raises(WriteLockNotHeldError):
            getattr(make_client(service), method)()

        assert not service.mock_calls

    def test_a_read_needs_no_lock(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )

        assert make_client(service).get_event("abc123").id == "abc123"
