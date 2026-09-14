from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, call

import pytest

from calendar_clients.google_calendar import CalendarClient, Event
from tests.event_time_helpers import event_at, time_at
from utilities import reallocating_calendar
from utilities.reallocating_calendar import ReallocatingCalendar
from utilities.reallocation import ReallocationOptions

UTC = timezone.utc
TEST_CALENDAR_ID = "my-calendar-id"


def make_client(service: MagicMock) -> CalendarClient:
    return CalendarClient(service, calendar_id=TEST_CALENDAR_ID)


class TestReallocatingCalendarListDayEvents:
    def test_fetches_a_24_hour_window_from_start(self):
        client = make_client(MagicMock())
        client.list_events = MagicMock(return_value=[])
        start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

        ReallocatingCalendar(client).list_day_events(start)

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

        events = ReallocatingCalendar(client).list_day_events(start)

        assert [e.id for e in events] == ["k", "s"]

    def test_keeps_everything_when_no_sleep_event_found(self):
        client = make_client(MagicMock())
        start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        events_in = [Event(id="a", start=start, end=start + timedelta(hours=1))]
        client.list_events = MagicMock(return_value=events_in)

        events = ReallocatingCalendar(client).list_day_events(start)

        assert events == events_in

    def test_ignore_id_skips_that_event_when_finding_where_the_day_ends(self):
        # The event matching ignore_id is itself marked
        # is_end_of_day_sleep, at the very front (e.g. update_event
        # extending a morning sleep block later) -- it must not be the
        # one truncation keys off of, but it's still returned like any
        # other event, since it's still part of the day as it stands now.
        start = datetime(2026, 1, 1, 1, 0, tzinfo=UTC)
        ignored_sleep = Event(
            id="s1",
            start=start,
            end=start + timedelta(hours=6),
            is_end_of_day_sleep=True,
        )
        kept = Event(
            id="k", start=start + timedelta(hours=6), end=start + timedelta(hours=7)
        )
        real_sleep = Event(
            id="s2",
            start=start + timedelta(hours=19),
            end=start + timedelta(hours=25),
            is_end_of_day_sleep=True,
        )
        discarded = Event(
            id="d", start=start + timedelta(hours=26), end=start + timedelta(hours=27)
        )
        client = make_client(MagicMock())
        client.list_events = MagicMock(
            return_value=[ignored_sleep, kept, real_sleep, discarded]
        )

        events = ReallocatingCalendar(client).list_day_events(start, ignore_id="s1")

        assert [e.id for e in events] == ["s1", "k", "s2"]

    def test_ignore_id_none_still_truncates_at_the_first_sleep_event(self):
        client = make_client(MagicMock())
        start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        sleep = Event(id="s", start=start, end=start + timedelta(hours=1), is_end_of_day_sleep=True)
        discarded = Event(id="d", start=start + timedelta(hours=2), end=start + timedelta(hours=3))
        client.list_events = MagicMock(return_value=[sleep, discarded])

        events = ReallocatingCalendar(client).list_day_events(start, ignore_id="some-other-id")

        assert [e.id for e in events] == ["s"]


class TestReallocatingCalendarCreateEvent:
    def test_requires_start_and_end(self):
        client = make_client(MagicMock())

        with pytest.raises(ValueError):
            ReallocatingCalendar(client).create_event(
                Event(summary="No times"), ReallocationOptions()
            )

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
        result = ReallocatingCalendar(client).create_event(new_event, ReallocationOptions())

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
        result = ReallocatingCalendar(client).create_event(new_event, ReallocationOptions())

        client.create_event.assert_called_once_with(new_event)
        client.update_event.assert_called_once_with(preceding)
        # later is never reclaimed from (a gap absorbs it), so it's left
        # out of the plan entirely -- not created, not updated.
        assert result == [preceding, new_event]


class TestReallocatingCalendarUpdateEvent:
    def test_requires_id(self):
        client = make_client(MagicMock())
        start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

        with pytest.raises(ValueError):
            ReallocatingCalendar(client).update_event(
                Event(summary="No id", start=start, end=start + timedelta(minutes=30)),
                ReallocationOptions(),
            )

    def test_requires_start_or_end(self):
        client = make_client(MagicMock())

        with pytest.raises(ValueError):
            ReallocatingCalendar(client).update_event(
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

        monkeypatch.setattr(reallocating_calendar, "reallocate_for_new_event", fake_reallocate)
        client.update_event = MagicMock(side_effect=lambda event: event)

        new_start = start + timedelta(minutes=15)
        updated_event = Event(id="abc123", start=new_start, priority=1)

        ReallocatingCalendar(client).update_event(updated_event, ReallocationOptions())

        assert captured["event"].end == current_end
        client.get_event.assert_not_called()
        client.list_events.assert_called_once()

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

        monkeypatch.setattr(reallocating_calendar, "reallocate_for_new_event", fake_reallocate)
        client.update_event = MagicMock(side_effect=lambda event: event)

        new_end = end + timedelta(minutes=30)
        updated_event = Event(id="abc123", end=new_end, priority=1)

        ReallocatingCalendar(client).update_event(updated_event, ReallocationOptions())

        assert captured["event"].start == start
        client.get_event.assert_not_called()
        client.list_events.assert_called_once()

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

        monkeypatch.setattr(reallocating_calendar, "reallocate_for_new_event", fake_reallocate)
        client.update_event = MagicMock(side_effect=lambda event: event)

        updated_event = Event(id="abc123", end=end, priority=1)

        ReallocatingCalendar(client).update_event(updated_event, ReallocationOptions())

        client.get_event.assert_called_once_with("abc123")
        client.list_events.assert_called_once()
        assert captured["event"].start == real_start

    def test_excludes_its_own_prior_position_from_day_events(self):
        client = make_client(MagicMock())
        start = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        end = start + timedelta(minutes=30)
        # The event being updated is still present in what list_events
        # returns (its prior position, before this update is applied) --
        # update_event must filter it out of day_events itself, since
        # reallocate_for_new_event refuses a day_events list that already
        # contains the moved event's id.
        prior_position = Event(id="moved", start=start, end=end, priority=1)
        anchor = Event(
            id="a1", start=start + timedelta(hours=2), end=start + timedelta(hours=3), priority=1
        )
        client.list_events = MagicMock(return_value=[prior_position, anchor])
        updated = Event(id="moved", summary="Moved", start=start, end=end)
        client.update_event = MagicMock(return_value=updated)
        client.create_event = MagicMock()

        moved_event = Event(id="moved", summary="Moved", start=start, end=end, priority=1)
        result = ReallocatingCalendar(client).update_event(moved_event, ReallocationOptions())

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
        result = ReallocatingCalendar(client).update_event(moved_event, ReallocationOptions())

        assert client.update_event.call_args_list == [call(preceding), call(moved_event)]
        client.create_event.assert_not_called()
        # later is never reclaimed from (a gap absorbs it), so it's left
        # out of the plan entirely -- not created, not updated.
        assert result == [preceding, moved_event]

    def test_updating_the_days_own_sleep_event_reallocates_the_rest_of_the_day(self):
        # Regression test for a real bug: the event being updated here is
        # itself the day's first is_end_of_day_sleep event (the morning
        # Sleep block). list_day_events must not truncate everything after
        # it away based on its own soon-to-be-replaced prior position --
        # see list_day_events's ignore_id. Once day_events is right,
        # reallocate_for_new_event already shifts the rest of the day
        # correctly (see test_reallocation.py's
        # test_extending_the_first_sleep_event_shifts_the_rest_of_a_full_day
        # for the same scenario exercised directly against it).
        get_ready = event_at("07:00-07:30", id="get_ready", summary="Get ready")
        journal = event_at("07:30-07:45", id="journal", summary="Journal")
        breakfast = event_at("07:45-08:15", id="breakfast", summary="Cook and eat breakfast")
        walk_to_gym = event_at("08:15-08:35", id="walk", summary="Walk to gym")
        work_out = event_at("08:35-09:35", id="workout", summary="Work out")
        commute = event_at("09:35-09:55", id="commute", summary="Commute to work")
        work = event_at("09:55-12:55", id="work", summary="Work")
        morning_sleep = event_at(
            "01:00-07:00", id="sleep1", summary="Sleep", is_end_of_day_sleep=True
        )
        evening_sleep = event_at(
            "20:00-07:00+1", id="sleep2", summary="Sleep", is_end_of_day_sleep=True
        )
        client = make_client(MagicMock())
        client.list_events = MagicMock(
            return_value=[
                morning_sleep,
                get_ready,
                journal,
                breakfast,
                walk_to_gym,
                work_out,
                commute,
                work,
                evening_sleep,
            ]
        )
        client.update_event = MagicMock(side_effect=lambda event: event)
        client.create_event = MagicMock(side_effect=lambda event: event)

        extended_sleep = event_at("01:00-10:00", id="sleep1", summary="Sleep")

        result = ReallocatingCalendar(client).update_event(extended_sleep, ReallocationOptions())

        assert extended_sleep.start == time_at("01:00")
        assert extended_sleep.end == time_at("10:00")
        assert get_ready.start == time_at("10:00")
        assert get_ready.end == time_at("10:30")
        assert work.start == time_at("12:55")
        assert work.end == time_at("15:55")
        # evening_sleep is untouched -- the ripple converges back onto its
        # original start before reaching it, and it's never even fetched
        # a second time.
        assert evening_sleep.start == time_at("20:00")
        assert evening_sleep.end == time_at("07:00+1")
        assert evening_sleep not in result
        client.list_events.assert_called_once()
