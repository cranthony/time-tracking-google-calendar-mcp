from dataclasses import fields, replace
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from googleapiclient.errors import HttpError

from calendar_clients.google_calendar import Event
from utilities.recurrences import NthWeekday, Recurrences, Repeat, describe_rules, split_series_id

NY = ZoneInfo("America/New_York")


def _at(day: int, hour: int = 9, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, tzinfo=NY)


class FakeCalendar:
    """Events by id; updates patch whichever fields are set, as Calendar's
    patch does."""

    def __init__(self, *events: Event) -> None:
        self.events = {event.id: replace(event) for event in events}
        self.writes: list[tuple[str, Event]] = []

    def get_event(self, event_id: str) -> Event:
        return replace(self.events[event_id])

    def create_event(self, event: Event) -> Event:
        if event.id in self.events:
            raise HttpError(MagicMock(status=409), b"The requested identifier already exists.")
        self.writes.append(("create", event))
        self.events[event.id] = replace(event)
        return replace(event)

    def update_event(self, event: Event) -> Event:
        self.writes.append(("update", event))
        stored = self.events[event.id]
        for field in fields(Event):
            if field.name != "id" and getattr(event, field.name) is not None:
                setattr(stored, field.name, getattr(event, field.name))
        return replace(stored)


def _weekly(rule: str = "RRULE:FREQ=WEEKLY;BYDAY=MO") -> Event:
    """Every Monday 09:00-10:00 from Mon Oct 5, 2026."""
    return Event(
        id="series1",
        summary="Standup",
        start=_at(5),
        end=_at(5, 10),
        time_zone="America/New_York",
        recurrence=[rule],
        goal_ids=["work"],
    )


def _instance(day: int, *, moved_to: datetime | None = None) -> Event:
    start = moved_to or _at(day)
    return Event(
        id=f"series1_202610{day:02d}",
        summary="Standup",
        start=start,
        end=start + timedelta(hours=1),
        recurring_event_id="series1",
        original_start=_at(day),
    )


def _recurrences(calendar: FakeCalendar, instances: list[Event] | None = None) -> Recurrences:
    return Recurrences(calendar, lambda series_id, before: instances or [], lambda: NY)


class TestSeries:
    def test_finds_the_series_from_any_of_its_events(self):
        calendar = FakeCalendar(_weekly(), _instance(12))
        recurrences = _recurrences(calendar)

        assert recurrences.series("series1_20261012").id == "series1"
        assert recurrences.series("series1").id == "series1"

    def test_refuses_a_single_event(self):
        calendar = FakeCalendar(Event(id="lone", summary="Lunch", start=_at(5, 12), end=_at(5, 13)))

        with pytest.raises(ValueError, match="isn't part of a recurring series"):
            _recurrences(calendar).series("lone")


class TestUpdate:
    def test_edits_the_whole_series_through_one_of_its_events(self):
        calendar = FakeCalendar(_weekly(), _instance(12))

        (updated,) = _recurrences(calendar).update(Event(id="series1_20261012", summary="Team standup"))

        assert updated.id == "series1"
        assert calendar.events["series1"].summary == "Team standup"
        assert calendar.events["series1"].recurrence == ["RRULE:FREQ=WEEKLY;BYDAY=MO"]

    def test_new_times_carry_the_series_time_zone(self):
        calendar = FakeCalendar(_weekly())

        _recurrences(calendar).update(Event(id="series1", start=_at(5, 10), end=_at(5, 11)))

        (_, patch), = calendar.writes
        assert patch.time_zone == "America/New_York"

    def test_this_and_following_splits_then_edits_only_the_later_part(self):
        calendar = FakeCalendar(_weekly(), _instance(19))

        later, earlier = _recurrences(calendar).update(
            Event(id="series1", summary="Team standup", start=_at(5, 10), end=_at(5, 11)),
            starting_at="series1_20261019",
        )

        assert earlier.id == "series1"
        assert earlier.summary == "Standup"
        assert earlier.recurrence == ["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z"]
        assert later.summary == "Team standup"
        # Moved an hour later, from the event it was split at.
        assert (later.start, later.end) == (_at(19, 10), _at(19, 11))
        assert later.goal_ids == ["work"]

    def test_this_and_following_refuses_an_event_from_another_series(self):
        other = Event(id="other_x", start=_at(6), end=_at(6, 10), recurring_event_id="other")
        other_series = replace(_weekly(), id="other")
        calendar = FakeCalendar(_weekly(), other_series, other)

        with pytest.raises(ValueError, match="isn't one of series series1's events"):
            _recurrences(calendar).update(Event(id="series1", summary="x"), starting_at="other_x")

        assert calendar.writes == []

    def test_writes_a_repeat_as_rules_in_the_series_time_zone(self):
        calendar = FakeCalendar(_weekly())

        _recurrences(calendar).update(
            Event(id="series1"), repeat=Repeat(every="week", weekdays=["mon", "wed"], until=date(2026, 12, 31))
        )

        # Through the end of Dec 31 in New York, which is Jan 1 in UTC.
        assert calendar.events["series1"].recurrence == ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20270101T045959Z"]

    def test_this_and_following_writes_the_repeat_to_the_later_part(self):
        calendar = FakeCalendar(_weekly(), _instance(19))

        later, earlier = _recurrences(calendar).update(
            Event(id="series1"), starting_at="series1_20261019", repeat=Repeat(every="week", weekdays=["tue"])
        )

        assert later.recurrence == ["RRULE:FREQ=WEEKLY;BYDAY=TU"]
        assert earlier.recurrence == ["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z"]

    def test_refuses_a_bad_repeat_without_writing(self):
        calendar = FakeCalendar(_weekly(), _instance(19))

        with pytest.raises(ValueError, match="count or until"):
            _recurrences(calendar).update(
                Event(id="series1"),
                starting_at="series1_20261019",
                repeat=Repeat(every="week", count=3, until=date(2026, 12, 31)),
            )

        assert calendar.writes == []


class TestDelete:
    def test_cancels_the_whole_series_through_one_of_its_events(self):
        calendar = FakeCalendar(_weekly(), _instance(12))

        assert _recurrences(calendar).delete("series1_20261012") is None

        assert calendar.writes == [("update", Event(id="series1", status="cancelled"))]

    def test_this_and_following_ends_the_series_before_the_event(self):
        calendar = FakeCalendar(_weekly(), _instance(19))

        left = _recurrences(calendar).delete("series1", starting_at="series1_20261019")

        assert left.recurrence == ["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z"]
        assert left.status is None
        assert [kind for kind, _ in calendar.writes] == ["update"]  # No copy is made.

    def test_this_and_following_replaces_a_count_and_keeps_other_rules(self):
        series = replace(
            _weekly(),
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=10", "EXDATE;TZID=America/New_York:20261012T090000"],
        )
        calendar = FakeCalendar(series, _instance(19))

        left = _recurrences(calendar).delete("series1", starting_at="series1_20261019")

        assert left.recurrence == [
            "RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z",
            "EXDATE;TZID=America/New_York:20261012T090000",
        ]

    def test_this_and_following_ends_a_moved_event_where_the_series_put_it(self):
        calendar = FakeCalendar(_weekly(), _instance(19, moved_to=_at(18, 15)))

        left = _recurrences(calendar).delete("series1", starting_at="series1_20261019")

        assert left.recurrence == ["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z"]

    def test_this_and_following_from_the_first_event_cancels_the_series(self):
        calendar = FakeCalendar(_weekly(), _instance(5))

        assert _recurrences(calendar).delete("series1", starting_at="series1_20261005") is None
        assert calendar.events["series1"].status == "cancelled"

    def test_this_and_following_refuses_an_event_from_another_series(self):
        other = Event(id="other_x", start=_at(6), end=_at(6, 10), recurring_event_id="other")
        calendar = FakeCalendar(_weekly(), replace(_weekly(), id="other"), other)

        with pytest.raises(ValueError, match="isn't one of series series1's events"):
            _recurrences(calendar).delete("series1", starting_at="other_x")
        assert calendar.writes == []


class TestSplit:
    def test_ends_the_series_before_the_event_and_starts_a_copy_at_it(self):
        calendar = FakeCalendar(_weekly(), _instance(19))

        earlier, later = _recurrences(calendar).split("series1_20261019")

        assert earlier.recurrence == ["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z"]
        assert later.id == split_series_id("series1", _at(19))
        assert (later.start, later.end, later.time_zone) == (_at(19), _at(19, 10), "America/New_York")
        assert later.recurrence == ["RRULE:FREQ=WEEKLY;BYDAY=MO"]
        assert later.summary == "Standup"

    def test_a_moved_event_splits_where_the_series_put_it(self):
        calendar = FakeCalendar(_weekly(), _instance(19, moved_to=_at(20, 15)))

        _, later = _recurrences(calendar).split("series1_20261019")

        assert later.start == _at(19)

    def test_shares_a_count_between_the_two(self):
        calendar = FakeCalendar(_weekly("RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=10"), _instance(19))
        before = [_instance(5), _instance(12)]

        earlier, later = _recurrences(calendar, before).split("series1_20261019")

        assert earlier.recurrence == ["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z"]
        assert later.recurrence == ["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=8"]

    def test_keeps_an_until_and_copies_exceptions(self):
        series = replace(
            _weekly("RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261231T235959Z"),
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261231T235959Z", "EXDATE;TZID=America/New_York:20261109T090000"],
        )
        calendar = FakeCalendar(series, _instance(19))

        earlier, later = _recurrences(calendar).split("series1_20261019")

        assert later.recurrence == series.recurrence
        assert earlier.recurrence[1] == "EXDATE;TZID=America/New_York:20261109T090000"

    def test_the_first_event_leaves_nothing_to_split(self):
        calendar = FakeCalendar(_weekly(), _instance(5))

        assert _recurrences(calendar).split("series1_20261005") == (None, calendar.events["series1"])
        assert calendar.writes == []

    def test_a_retried_split_finds_the_series_it_already_made(self):
        calendar = FakeCalendar(_weekly(), _instance(19))
        recurrences = _recurrences(calendar)
        _, first = recurrences.split("series1_20261019")
        # As if the trim had failed: the old series is whole again.
        calendar.events["series1"].recurrence = ["RRULE:FREQ=WEEKLY;BYDAY=MO"]

        earlier, again = recurrences.split("series1_20261019")

        assert again.id == first.id
        assert [kind for kind, _ in calendar.writes].count("create") == 1
        assert earlier.recurrence == ["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z"]


def test_split_series_ids_are_valid_calendar_ids():
    series_id = split_series_id("series1", _at(19))

    assert series_id == split_series_id("series1", _at(19))
    assert series_id != split_series_id("series1", _at(26))
    assert 5 <= len(series_id) <= 1024 and set(series_id) <= set("0123456789abcdefghijklmnopqrstuv")


@pytest.mark.parametrize(
    "rules, phrase",
    [
        (["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"], "Every week on Mon, Wed"),
        (["RRULE:FREQ=DAILY;INTERVAL=2;COUNT=10"], "Every 2 days, 10 times"),
        (["RRULE:FREQ=MONTHLY;BYDAY=-1FR"], "Every month on the last Fri"),
        (["RRULE:FREQ=MONTHLY;BYMONTHDAY=15"], "Every month on day 15"),
        (["RRULE:FREQ=WEEKLY;UNTIL=20261231T235959Z"], "Every week, until Dec 31, 2026"),
        (["RRULE:FREQ=WEEKLY", "EXDATE:20261012T130000Z"], "Every week, with exceptions"),
        (["RRULE:FREQ=MONTHLY;BYMONTHDAY=1,-1"], "Every month on day 1 and the last day"),
        (["RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=4TH"], "Every year on the fourth Thu in Nov"),
        (["RRULE:FREQ=WEEKLY", "RDATE:20261021T180000Z"], "Every week, plus 1 added date"),
        (["RRULE:FREQ=HOURLY"], "RRULE:FREQ=HOURLY"),
    ],
)
def test_describes_rules_in_words(rules, phrase):
    assert describe_rules(rules, NY) == phrase


class TestRepeat:
    @pytest.mark.parametrize(
        "repeat, rules",
        [
            (Repeat(every="day"), ["RRULE:FREQ=DAILY"]),
            (
                Repeat(every="week", interval=2, weekdays=["tue", "thu"], week_starts_on="sun"),
                ["RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH;WKST=SU"],
            ),
            (Repeat(every="month", month_days=[1, -1], count=12), ["RRULE:FREQ=MONTHLY;BYMONTHDAY=1,-1;COUNT=12"]),
            (
                Repeat(every="month", nth_weekdays=[NthWeekday(nth=-1, weekday="fri")]),
                ["RRULE:FREQ=MONTHLY;BYDAY=-1FR"],
            ),
            (
                Repeat(every="year", months=[11], nth_weekdays=[NthWeekday(nth=4, weekday="thu")]),
                ["RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=4TH"],
            ),
            (
                # As a split leaves it: a second before the split event.
                Repeat(every="week", until=_at(19) - timedelta(seconds=1)),
                ["RRULE:FREQ=WEEKLY;UNTIL=20261019T125959Z"],
            ),
            (
                Repeat(every="week", skipped=[_at(12), _at(19)], added=[_at(21, 14)]),
                [
                    "RRULE:FREQ=WEEKLY",
                    "EXDATE;TZID=America/New_York:20261012T090000,20261019T090000",
                    "RDATE;TZID=America/New_York:20261021T140000",
                ],
            ),
        ],
    )
    def test_round_trips_through_rules(self, repeat, rules):
        assert repeat.to_rules(NY) == rules
        assert Repeat.from_rules(rules, NY) == repeat

    def test_an_until_date_is_the_end_of_that_day_in_the_series_zone(self):
        (rule,) = Repeat(every="day", until=date(2026, 12, 31)).to_rules(NY)

        assert rule == "RRULE:FREQ=DAILY;UNTIL=20270101T045959Z"

    def test_reads_times_given_in_utc_or_another_zone_in_the_series_zone(self):
        repeat = Repeat.from_rules(
            ["RRULE:FREQ=WEEKLY;UNTIL=20261231", "EXDATE:20261012T130000Z", "RDATE;TZID=Europe/London:20261021T190000"],
            NY,
        )

        assert repeat.until == date(2026, 12, 31)
        assert repeat.skipped == [_at(12)]
        assert repeat.added == [_at(21, 14)]
        assert repeat.skipped[0].tzinfo == NY

    @pytest.mark.parametrize(
        "rules",
        [
            ["RRULE:FREQ=HOURLY"],
            ["RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1"],
            ["RRULE:FREQ=WEEKLY", "EXRULE:FREQ=MONTHLY"],
            ["RRULE:FREQ=WEEKLY", "RRULE:FREQ=DAILY"],
            ["RRULE:FREQ=YEARLY;BYDAY=20MO"],
            ["RRULE:FREQ=WEEKLY", "EXDATE;VALUE=DATE:20261012"],
            ["RRULE:BYDAY=MO"],
        ],
    )
    def test_reads_rules_it_cant_say_as_none(self, rules):
        assert Repeat.from_rules(rules, NY) is None

    @pytest.mark.parametrize(
        "repeat, message",
        [
            (Repeat(every="fortnight"), "every must be one of"),
            (Repeat(every="week", interval=0), "interval must be at least 1"),
            (Repeat(every="week", count=3, until=date(2026, 12, 31)), "count or until"),
            (Repeat(every="week", count=0), "count must be at least 1"),
            (Repeat(every="week", weekdays=["monday"]), "not 'monday'"),
            (Repeat(every="month", nth_weekdays=[NthWeekday(nth=6, weekday="mon")]), "nth must be 1 to 5"),
            (Repeat(every="week", nth_weekdays=[NthWeekday(nth=1, weekday="mon")]), 'not "week"'),
            (Repeat(every="year", nth_weekdays=[NthWeekday(nth=4, weekday="thu")]), "need months"),
            (Repeat(every="month", month_days=[0]), "not 0"),
            (Repeat(every="week", month_days=[1]), 'not for every "week"'),
            (Repeat(every="year", months=[13]), "not 13"),
        ],
    )
    def test_refuses_what_it_cant_write(self, repeat, message):
        with pytest.raises(ValueError, match=message):
            repeat.to_rules(NY)
