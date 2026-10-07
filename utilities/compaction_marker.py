"""A marker in Google Calendar of when notes were last compacted: one
bright red, 5-minute event ending then, so a glance at the calendar shows
how far it's settled fact.

It lives on a small calendar of its own, "Compactions", which the app
creates the first time it's needed (in the main calendar's time zone,
shown in the same red) and records on the main calendar with
`set_calendar_metadata`. Nothing that reads events -- listing them, compacting,
changing events, counting actions' time, reflecting -- reads that calendar, so
the marker is never among them, and the main calendar's events never
overlap it.

There's only ever one marker. It has a fixed id, so each compaction moves
it (inserting it the first time, overwriting it after, and restoring it if
it was deleted by hand), and every write also deletes anything else on the
calendar -- a copy made in Google Calendar, say -- since the calendar
holds nothing but the marker.

`NoteCompactor` moves it once a compaction is stamped. Moving it is best
effort: a failure is reported, but never fails the compaction, and the
next one puts it right.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from calendar_clients.google_calendar import CalendarClient

MARKER_CALENDAR_METADATA_KEY = "compaction-marker-calendar"
MARKER_CALENDAR_SUMMARY = "Compactions"

MARKER_EVENT_ID = "compactionmarker0"
"""The marker's fixed id: 5-1024 characters of a-v and 0-9."""

MARKER_DURATION = timedelta(minutes=5)

MARKER_COLOR_ID = "11"
"""Google Calendar's own "Tomato", its brightest red."""

MARKER_CALENDAR_COLOR = "#d50000"
"""Tomato, for the calendar itself in the user's list."""


class CompactionMarker:
    """The marker of the last compaction -- see the module docstring."""

    def __init__(self, client: CalendarClient) -> None:
        """`client` is the main calendar's."""
        self._client = client
        self._marker_client: CalendarClient | None = None

    def mark(self, at: datetime) -> None:
        """Make the marker end at `at`, the last compaction's time, and the
        only event on its calendar. Creates the calendar if need be."""
        calendar = self.calendar()
        tz = self._client.get_time_zone()
        local = at.astimezone(tz)
        calendar.upsert_event_resource(
            MARKER_EVENT_ID,
            {
                "summary": f"✓ Compacted · {local:%H:%M}",
                "description": "Notes were compacted into the calendar up to here.",
                "start": {"dateTime": (local - MARKER_DURATION).isoformat()},
                "end": {"dateTime": local.isoformat()},
                "colorId": MARKER_COLOR_ID,
                "transparency": "transparent",
                # Restores it, if it was deleted by hand.
                "status": "confirmed",
            },
        )
        for item in calendar.list_all_event_resources():
            if item.get("id") != MARKER_EVENT_ID and item.get("status") != "cancelled":
                calendar.delete_event(item["id"])

    def calendar(self, *, create: bool = True) -> CalendarClient | None:
        """The Compactions calendar; created if need be, unless `create` is
        false (then `None` if there isn't one yet)."""
        if self._marker_client is None:
            calendar_id = self._client.get_calendar_metadata(MARKER_CALENDAR_METADATA_KEY)
            if calendar_id is None:
                if not create:
                    return None
                calendar_id = self._client.create_calendar(
                    MARKER_CALENDAR_SUMMARY,
                    "When notes were last compacted into the Time Tracking calendar, kept by Cascading "
                    "Time Tracker: one event, moved each time.",
                    time_zone=self._client.get_time_zone().key,
                )
                self._client.color_calendar(calendar_id, MARKER_CALENDAR_COLOR)
                self._client.set_calendar_metadata(MARKER_CALENDAR_METADATA_KEY, calendar_id)
            self._marker_client = self._client.for_calendar(calendar_id)
        return self._marker_client
