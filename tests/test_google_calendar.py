import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from googleapiclient.errors import HttpError

from calendar_clients.google_calendar import (
    CLEARABLE_EVENT_FIELDS,
    Calendar,
    CalendarClient,
    Event,
    EventLabel,
    EventLabelConflictError,
    TimeZoneNotSetError,
    cached_calendar_listings,
)
from calendar_clients.write_lock import WriteLockNotHeldError
from utilities.facts import Facts

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
        "action_ids, stored",
        [(["a1", "a2"], "a1 a2"), ([], "")],  # [] is stored, so clearing actions sticks
    )
    def test_action_ids_round_trip_through_a_private_extended_property(self, action_ids, stored):
        body = Event(action_ids=action_ids).to_api_body()

        assert body["extendedProperties"]["private"] == {"cascading-time-tracker-action_ids": stored}
        data = api_event("1", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
        data["extendedProperties"] = body["extendedProperties"]
        assert Event.from_api(data).action_ids == action_ids

    def test_action_ids_are_none_when_never_set(self):
        assert Event.from_api(api_event("1", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")).action_ids is None
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
                "cascading-time-tracker-priority": "2",
                "cascading-time-tracker-is_end_of_day_sleep": "true",
            }
        }

        event = Event.from_api(data)

        assert event.priority == 2
        assert event.is_end_of_day_sleep is True

    def test_from_api_ignores_other_private_keys(self):
        data = api_event(
            "abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"
        )
        data["extendedProperties"] = {"private": {"someOtherApp-key": "value"}}

        event = Event.from_api(data)

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
            priority=2,
            is_end_of_day_sleep=True,
        )

        assert event.to_api_body()["extendedProperties"] == {
            "private": {
                "cascading-time-tracker-priority": "2",
                "cascading-time-tracker-is_end_of_day_sleep": "true",
            }
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

    @pytest.mark.parametrize("priority", [-1, 0, 1, 2, 3, 5])
    def test_to_api_body_never_writes_a_colorId(self, priority):
        # An event's own color overrides its label's, and writing one --
        # even clearing it -- can drop the label: see to_api_body.
        assert "colorId" not in Event(id="abc123", priority=priority).to_api_body()
        assert "colorId" not in Event(id="abc123", cleared=frozenset({"priority"})).to_api_body()

    def test_to_api_body_sends_cleared_fields_as_null(self):
        event = Event(id="abc123", summary="Focus", cleared=CLEARABLE_EVENT_FIELDS)

        assert event.to_api_body() == {
            "summary": "Focus",
            "description": None,
            "location": None,
            "extendedProperties": {
                "private": {
                    "cascading-time-tracker-priority": None,
                    "cascading-time-tracker-facts": None,
                    "cascading-time-tracker-facts-2": None,
                    "cascading-time-tracker-facts-3": None,
                    "cascading-time-tracker-facts-4": None,
                    "cascading-time-tracker-facts-5": None,
                    "cascading-time-tracker-facts-6": None,
                    "cascading-time-tracker-facts-7": None,
                    "cascading-time-tracker-facts-8": None,
                    "cascading-time-tracker-judgments": None,
                    **{f"cascading-time-tracker-judgments-{i}": None for i in range(2, 17)},
                }
            },
        }

    def test_facts_round_trip_as_compact_json_under_short_keys(self):
        facts = Facts(location_id="l1", with_ids=["p1"], for_ids=["p2"], notes={"self": "tired", "p1": "glad"})
        body = Event(id="abc123", facts=facts).to_api_body()

        private = body["extendedProperties"]["private"]
        assert json.loads(private["cascading-time-tracker-facts"]) == {
            "location": "l1", "with": ["p1"], "for": ["p2"], "notes": {"self": "tired", "p1": "glad"}
        }
        # Unused chunks are removed, so a shorter value leaves no tail.
        assert all(private[f"cascading-time-tracker-facts-{i}"] is None for i in range(2, 9))
        read = Event.from_api(
            {**body, "id": "abc123", "start": {"dateTime": "2026-10-01T18:00:00-04:00"},
             "end": {"dateTime": "2026-10-01T20:00:00-04:00"}}
        )
        assert read.facts == facts

    def test_long_facts_are_split_across_properties_and_joined_on_read(self):
        facts = Facts(notes={"self": "x" * 900, "p1": "y" * 900}, with_ids=["p1"])
        body = Event(id="abc123", facts=facts).to_api_body()

        private = {k: v for k, v in body["extendedProperties"]["private"].items() if v is not None}
        assert sorted(private) == ["cascading-time-tracker-facts", "cascading-time-tracker-facts-2"]
        assert all(len(v) <= 1024 for v in private.values())
        read = Event.from_api(
            {"id": "abc123", "start": {"dateTime": "2026-10-01T18:00:00-04:00"},
             "end": {"dateTime": "2026-10-01T20:00:00-04:00"}, "extendedProperties": {"private": private}}
        )
        assert read.facts == facts

    def test_creating_an_event_with_facts_sends_no_null_properties(self):
        # An insert refuses a null private property, which a patch would
        # take to mean "remove it".
        service = MagicMock()
        service.events.return_value.insert.return_value.execute.return_value = api_event(
            "abc123", "2026-10-01T18:00:00-04:00", "2026-10-01T20:00:00-04:00"
        )
        event = Event(
            summary="Dinner",
            start=datetime(2026, 10, 1, 18, tzinfo=timezone.utc),
            end=datetime(2026, 10, 1, 20, tzinfo=timezone.utc),
            facts=Facts(with_ids=["p1"]),
        )

        make_client(service).create_event(event)

        body = service.events.return_value.insert.call_args.kwargs["body"]
        assert body["extendedProperties"]["private"] == {"cascading-time-tracker-facts": '{"with":["p1"]}'}

    def test_updating_an_event_nulls_only_the_properties_it_has(self):
        # A recurring instance refuses a null for a property it doesn't
        # have, and shorter facts null every chunk they don't use.
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = {
            "extendedProperties": {"private": {
                "cascading-time-tracker-facts": "{}", "cascading-time-tracker-facts-2": "x",
                "cascading-time-tracker-priority": "1",
            }}
        }
        service.events.return_value.patch.return_value.execute.return_value = api_event(
            "abc123", "2026-10-01T18:00:00-04:00", "2026-10-01T20:00:00-04:00"
        )

        make_client(service).update_event(
            Event(id="abc123", facts=Facts(with_ids=["p1"]), cleared=frozenset({"priority", "location"}))
        )

        body = service.events.return_value.patch.call_args.kwargs["body"]
        assert body["extendedProperties"]["private"] == {
            "cascading-time-tracker-facts": '{"with":["p1"]}',
            "cascading-time-tracker-facts-2": None,
            "cascading-time-tracker-priority": None,
        }
        assert body["location"] is None

    def test_updating_with_nothing_to_remove_sends_no_properties(self):
        service = MagicMock()
        service.events.return_value.get.return_value.execute.return_value = {}
        service.events.return_value.patch.return_value.execute.return_value = api_event(
            "abc123", "2026-10-01T18:00:00-04:00", "2026-10-01T20:00:00-04:00"
        )

        make_client(service).update_event(Event(id="abc123", summary="Dinner", cleared=frozenset({"facts"})))

        assert "extendedProperties" not in service.events.return_value.patch.call_args.kwargs["body"]

    def test_facts_too_long_to_store_are_refused(self):
        facts = Facts(notes={f"p{i}": "z" * 1000 for i in range(9)})

        with pytest.raises(ValueError, match="longer than 8192 characters"):
            Event(id="abc123", facts=facts).to_api_body()

    def test_empty_facts_remove_them(self):
        body = Event(id="abc123", facts=Facts()).to_api_body()

        assert body["extendedProperties"]["private"] == {
            "cascading-time-tracker-facts": None, **{f"cascading-time-tracker-facts-{i}": None for i in range(2, 9)}
        }

    def test_facts_that_arent_json_read_as_none(self):
        read = Event.from_api(
            {"id": "abc123", "start": {"dateTime": "2026-10-01T18:00:00-04:00"},
             "end": {"dateTime": "2026-10-01T20:00:00-04:00"},
             "extendedProperties": {"private": {"cascading-time-tracker-facts": "not json"}}}
        )
        assert read.facts is None

    def test_to_api_body_clears_some_app_properties_while_setting_others(self):
        event = Event(id="abc123", is_end_of_day_sleep=True, cleared=frozenset({"location"}))

        body = event.to_api_body()

        assert "colorId" not in body
        assert body["location"] is None
        assert body["extendedProperties"] == {
            "private": {"cascading-time-tracker-is_end_of_day_sleep": "true"}
        }

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({"cleared": frozenset({"summary"})}, r"Can't clear \['summary'\]; clearable fields are"),
            ({"cleared": frozenset({"action_ids"})}, r"Can't clear \['action_ids'\]"),
            ({"priority": 1, "cleared": frozenset({"priority"})}, r"Can't both set and clear \['priority'\]"),
        ],
    )
    def test_refuses_clearing_what_cant_be_cleared_or_is_also_set(self, kwargs, message):
        with pytest.raises(ValueError, match=message):
            Event(id="abc123", **kwargs)

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
        # sourced only from the action that holds the label (see
        # utilities/actions.py), never this class -- even if the name happens
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
        # utilities/actions.py's Actions does other work (validating, writing
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


class TestCalendarClientSideCalendarCalls:
    """The raw calls utilities/compaction_marker.py makes for its own
    calendar."""

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

    def test_a_calendar_without_a_time_zone_is_refused_until_one_is_set(self):
        service = MagicMock()
        service.calendars.return_value.get.return_value.execute.side_effect = [{}, {"timeZone": "Europe/Paris"}]
        client = make_client(service)

        with pytest.raises(TimeZoneNotSetError):
            client.get_time_zone()
        # Not remembered: one set in Google Calendar since is picked up.
        assert client.get_time_zone() == ZoneInfo("Europe/Paris")

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

        assert make_client(service).create_calendar("Compactions", "d", time_zone="Europe/Paris") == "new-cal"
        service.calendars.return_value.insert.assert_called_once_with(
            body={"summary": "Compactions", "description": "d", "timeZone": "Europe/Paris"}
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


def _day_listing_service() -> MagicMock:
    """A service whose every listing answers the same three events, at 09,
    11 and 13 o'clock on 1 January."""
    service = MagicMock()
    service.events.return_value.list.return_value.execute.return_value = {
        "items": [
            api_event("1", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"),
            api_event("2", "2026-01-01T11:00:00+00:00", "2026-01-01T12:00:00+00:00"),
            api_event("3", "2026-01-01T13:00:00+00:00", "2026-01-01T14:00:00+00:00"),
        ]
    }
    return service


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 1, 1, hour, minute, tzinfo=UTC)


class TestCachedCalendarListings:
    def test_a_listing_within_an_earlier_one_is_answered_from_it(self):
        service = _day_listing_service()
        client = make_client(service)

        with cached_calendar_listings():
            client.list_events(_at(0), _at(23))
            events = client.list_events(_at(10, 30), _at(13))

        # Calendar's own rule: ends after the start, starts before the end.
        assert [e.id for e in events] == ["2"]
        service.events.return_value.list.assert_called_once()

    def test_an_edge_touching_the_range_is_left_out_as_calendar_does(self):
        client = make_client(_day_listing_service())

        with cached_calendar_listings():
            client.list_events(_at(0), _at(23))
            events = client.list_events(_at(10), _at(11))

        assert events == []

    def test_a_listing_past_the_earlier_one_lists_again(self):
        service = _day_listing_service()
        client = make_client(service)

        with cached_calendar_listings():
            client.list_events(_at(8), _at(12))
            client.list_events(_at(8), _at(14))

        assert service.events.return_value.list.call_count == 2

    def test_another_calendar_lists_its_own(self):
        service = _day_listing_service()
        client = make_client(service)

        with cached_calendar_listings():
            client.list_events(_at(0), _at(23))
            client.for_calendar("other-cal").list_events(_at(0), _at(23))

        assert service.events.return_value.list.call_count == 2

    def test_what_it_answers_is_a_copy(self):
        client = make_client(_day_listing_service())

        with cached_calendar_listings():
            client.list_events(_at(0), _at(23))[0].summary = "changed"
            client.list_events(_at(0), _at(23))[1].summary = "changed too"
            events = client.list_events(_at(0), _at(23))

        assert [e.summary for e in events] == ["Busy", "Busy", "Busy"]

    @pytest.mark.parametrize(
        "write",
        [
            lambda client: client.delete_event_resource("1"),
            lambda client: client.delete_event("1"),
            lambda client: client.upsert_event_resource("abcde", {"summary": "New"}),
        ],
    )
    def test_any_write_forgets_every_listing(self, write):
        service = _day_listing_service()
        client = make_client(service)

        with cached_calendar_listings():
            client.list_events(_at(0), _at(23))
            write(client)
            client.list_events(_at(0), _at(23))

        assert service.events.return_value.list.call_count == 2

    def test_nothing_is_kept_outside_the_block_or_after_it(self):
        service = _day_listing_service()
        client = make_client(service)

        client.list_events(_at(0), _at(23))
        with cached_calendar_listings():
            client.list_events(_at(0), _at(23))
        client.list_events(_at(0), _at(23))

        assert service.events.return_value.list.call_count == 3

    def test_a_nested_block_shares_the_outer_ones(self):
        service = _day_listing_service()
        client = make_client(service)

        with cached_calendar_listings():
            client.list_events(_at(0), _at(23))
            with cached_calendar_listings():
                client.list_events(_at(9), _at(10))

        service.events.return_value.list.assert_called_once()


_CALENDAR_WRITES = [
    "create_calendar",
    "hide_calendar",
    "color_calendar",
    "set_time_zone",
    "upsert_event_resource",
    "replace_event_resource",
    "delete_event_resource",
    "create_event",
    "import_event",
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
