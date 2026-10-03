from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from utilities.compaction_marker import (
    MARKER_CALENDAR_COLOR,
    MARKER_CALENDAR_METADATA_KEY,
    MARKER_EVENT_ID,
    CompactionMarker,
)

TZ = ZoneInfo("America/New_York")
AT = datetime(2026, 10, 3, 1, 5, tzinfo=timezone.utc)  # 21:05 on Oct 2, in New York


class FakeCalendar:
    """The main calendar and the Compactions calendar it creates, with
    events kept the way the API would: deleted ones stay, cancelled, under
    their id."""

    def __init__(self):
        self.metadata: dict[str, str] = {}
        self.created: list[tuple] = []
        self.colored: list[tuple] = []
        self.events: dict[str, dict] = {}

    def get_time_zone(self):
        return TZ

    def get_calendar_metadata(self, key):
        return self.metadata.get(key)

    def set_calendar_metadata(self, key, value):
        self.metadata[key] = value

    def create_calendar(self, summary, description=None, time_zone=None):
        self.created.append((summary, time_zone))
        return "compactions"

    def color_calendar(self, calendar_id, background_color):
        self.colored.append((calendar_id, background_color))
        return True

    def for_calendar(self, calendar_id):
        assert calendar_id == "compactions"
        return self

    def upsert_event_resource(self, event_id, body):
        self.events[event_id] = {**self.events.get(event_id, {}), **body, "id": event_id}
        return self.events[event_id]

    def list_all_event_resources(self):
        return list(self.events.values())

    def delete_event(self, event_id):
        self.events[event_id]["status"] = "cancelled"

    def live(self):
        return [e for e in self.events.values() if e.get("status") != "cancelled"]


class TestCompactionMarker:
    def test_marks_the_last_compaction_with_a_red_5_minute_event_ending_then(self):
        calendar = FakeCalendar()

        CompactionMarker(calendar).mark(AT)

        (marker,) = calendar.live()
        assert marker["id"] == MARKER_EVENT_ID
        assert marker["summary"] == "✓ Compacted · 21:05"
        assert marker["start"] == {"dateTime": "2026-10-02T21:00:00-04:00"}
        assert marker["end"] == {"dateTime": "2026-10-02T21:05:00-04:00"}
        assert marker["colorId"] == "11"
        assert marker["transparency"] == "transparent"

    def test_creates_its_own_red_calendar_once_in_the_main_ones_time_zone(self):
        calendar = FakeCalendar()
        marker = CompactionMarker(calendar)

        marker.mark(AT)
        marker.mark(AT)
        CompactionMarker(calendar).mark(AT)  # Found again, from the main calendar.

        assert calendar.created == [("Compactions", "America/New_York")]
        assert calendar.colored == [("compactions", MARKER_CALENDAR_COLOR)]
        assert calendar.metadata[MARKER_CALENDAR_METADATA_KEY] == "compactions"

    def test_moves_the_one_marker_rather_than_adding_another(self):
        calendar = FakeCalendar()
        marker = CompactionMarker(calendar)
        marker.mark(AT)

        marker.mark(datetime(2026, 10, 3, 16, 30, tzinfo=timezone.utc))

        (moved,) = calendar.live()
        assert moved["end"] == {"dateTime": "2026-10-03T12:30:00-04:00"}

    def test_brings_back_a_marker_deleted_by_hand(self):
        calendar = FakeCalendar()
        marker = CompactionMarker(calendar)
        marker.mark(AT)
        calendar.delete_event(MARKER_EVENT_ID)

        marker.mark(AT)

        assert [e["id"] for e in calendar.live()] == [MARKER_EVENT_ID]

    def test_deletes_anything_else_on_its_calendar(self):
        calendar = FakeCalendar()
        marker = CompactionMarker(calendar)
        marker.mark(AT)
        calendar.events["copy"] = {**calendar.events[MARKER_EVENT_ID], "id": "copy"}  # Copied by hand.

        marker.mark(AT)

        assert [e["id"] for e in calendar.live()] == [MARKER_EVENT_ID]

    def test_finds_no_calendar_without_making_one_when_asked_not_to(self):
        calendar = FakeCalendar()

        assert CompactionMarker(calendar).calendar(create=False) is None
        assert calendar.created == []
