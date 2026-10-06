from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from calendar_clients.google_calendar import Event
from tests.event_time_helpers import event_at, time_at
from utilities import event_changes
from utilities.event_changes import RESEND_RULE, Cancel, ChangeError, EventChanges, Shift


class FakeCalendar:
    """Events by id, read and written as an ActionCalendar would."""

    def __init__(self, events):
        self.events = {e.id: e for e in events}
        self.written = []

    def get_event(self, event_id):
        return replace(self.events[event_id])

    def list_events(self, time_min, time_max):
        return sorted(
            (replace(e) for e in self.events.values() if e.status != "cancelled" and e.end > time_min and e.start < time_max),
            key=lambda e: e.start,
        )

    def update_event(self, patch):
        self.written.append(("update", patch))
        event = self.events[patch.id]
        changes = {k: v for k, v in vars(patch).items() if k not in ("id", "cleared") and v is not None and v != getattr(Event(), k)}
        self.events[patch.id] = replace(event, **changes)
        return replace(self.events[patch.id])

    def create_event(self, event):
        created = replace(event, id=f"new{len(self.events)}")
        self.written.append(("create", created))
        self.events[created.id] = created
        return replace(created)


def _day():
    return [
        event_at("09:00-10:00", id="email", summary="Email"),
        event_at("10:00-11:00", id="report", summary="Report"),
        event_at("11:00-11:30", id="coffee", summary="Coffee with Sam"),
        event_at("11:30-12:30", id="gym", summary="Gym"),
        event_at("12:30-13:15", id="lunch", summary="Lunch"),
        event_at("13:15-13:30", id="call", summary="Call"),
        event_at("23:00-07:00+1", id="s1", summary="Sleep", is_end_of_day_sleep=True),
    ]


def _changes(events=None, cancellations=None):
    calendar = FakeCalendar(events if events is not None else _day())
    return EventChanges(calendar, cancellations), calendar


def _move(event_id, start, end):
    return Event(id=event_id, start=time_at(start), end=time_at(end))


class TestOneEvent:
    def test_a_move_into_free_time_is_written(self):
        changes, calendar = _changes()

        written = changes.apply(changes.check(updates=[_move("call", "14:00", "14:15")]), "update_event")

        assert [(e.id, e.start) for e in written] == [("call", time_at("14:00"))]

    def test_an_update_that_doesnt_move_it_is_never_checked_for_overlaps(self):
        events = _day()
        events.append(event_at("09:30-09:45", id="clash", summary="Clash"))  # Already overlapping Email.
        changes, calendar = _changes(events)

        changes.apply(changes.check(updates=[Event(id="email", summary="Inbox")]), "update_event")

        assert calendar.events["email"].summary == "Inbox"

    def test_overlaps_the_batch_doesnt_cause_are_left_alone(self):
        events = _day()
        events.append(event_at("09:30-09:45", id="clash", summary="Clash"))
        changes, _ = _changes(events)

        changes.check(updates=[_move("report", "10:00", "10:45")])


class TestOverlaps:
    """Example 1 of the design: move Lunch earlier, shifting Gym."""

    def test_a_move_into_another_event_is_refused_with_everything_to_fix_it_at_once(self):
        changes, calendar = _changes()

        with pytest.raises(ChangeError) as excinfo:
            changes.check(updates=[_move("lunch", "11:30", "12:15")])

        message = str(excinfo.value)
        assert message.startswith(RESEND_RULE)
        assert "- 'Lunch' (11:30–12:15, as asked) overlaps 'Gym' (11:30–12:30)." in message
        # The stretch it touches, through the night, as it would leave it.
        assert "  11:30–12:15  Lunch *  lunch" in message
        assert "  13:15–13:30  Call  call" in message
        assert "  23:00–07:00  Sleep  s1  (end of day)" in message
        assert excinfo.value.categories == ["overlap"]
        assert calendar.written == []

    def test_every_problem_is_named_at_once(self):
        changes, _ = _changes()

        with pytest.raises(ChangeError) as excinfo:
            changes.check(updates=[_move("lunch", "11:30", "12:15"), _move("call", "09:30", "09:30")])

        message = str(excinfo.value)
        assert "'Lunch'" in message and "'Gym'" in message
        assert "'Call' (09:30–09:30, as asked) would end no later than it starts" in message
        assert excinfo.value.categories == ["nonpositive_length", "overlap"]

    def test_moving_whats_in_the_way_in_the_same_batch_passes(self):
        changes, calendar = _changes()
        batch = changes.check(
            updates=[_move("lunch", "11:30", "12:15"), _move("gym", "12:15", "13:15")]
        )

        changes.apply(batch, "update_event")

        assert (calendar.events["gym"].start, calendar.events["gym"].end) == (time_at("12:15"), time_at("13:15"))
        text = event_changes.timeline(batch).text
        assert "Lunch · ⇠1h00m (was 12:30–13:15)" in text
        assert "Gym · ⇢45m (was 11:30–12:30)" in text

    def test_a_shift_moves_each_of_its_events_as_an_update(self):
        changes, calendar = _changes()

        changes.apply(
            changes.check(
                updates=[_move("coffee", "11:00", "11:45")],
                shifts=[Shift(event_ids=["gym", "lunch", "call"], minutes=15)],
            ),
            "update_event",
        )

        assert [calendar.events[i].start for i in ("gym", "lunch", "call")] == [
            time_at("11:45"), time_at("12:45"), time_at("13:30"),
        ]

    def test_an_event_can_have_only_one_change(self):
        changes, _ = _changes()

        with pytest.raises(ChangeError, match="event gym is in the batch twice") as excinfo:
            changes.check(updates=[_move("gym", "12:00", "13:00")], shifts=[Shift(event_ids=["gym"], minutes=5)])

        assert excinfo.value.categories == ["duplicate_change"]

    def test_a_new_event_is_checked_too(self):
        changes, _ = _changes()

        with pytest.raises(ChangeError, match="'Report' \\(10:00–11:00\\) overlaps 'Nap' \\(10:30–11:15, as asked\\)"):
            changes.check(creates=[Event(summary="Nap", start=time_at("10:30"), end=time_at("11:15"))])


class TestCancels:
    """Example 2 of the design: move Gym earlier, cancelling Coffee."""

    def test_cancelling_whats_in_the_way_makes_room(self):
        cancellations = MagicMock()
        changes, calendar = _changes(cancellations=cancellations)
        batch = changes.check(
            updates=[_move("gym", "11:00", "12:00")],
            cancels=[Cancel(event_id="coffee", counts_against_follow_through=False)],
        )

        changes.apply(batch, "update_event")

        assert calendar.events["coffee"].status == "cancelled"
        cancellations.record.assert_not_called()
        assert "✕ Coffee with Sam · cancelled" in event_changes.timeline(batch).text

    def test_a_cancel_that_counts_is_recorded_against_follow_through(self):
        cancellations = MagicMock()
        changes, _ = _changes(cancellations=cancellations)

        changes.apply(changes.check(cancels=[Cancel(event_id="coffee", counts_against_follow_through=True)]), "delete_event")

        (event, source), _ = cancellations.record.call_args
        assert (event.id, source) == ("coffee", "delete_event")

    def test_an_unknown_or_cancelled_event_is_refused(self):
        events = _day()
        events[0] = replace(events[0], status="cancelled")
        changes, _ = _changes(events)

        with pytest.raises(ChangeError) as excinfo:
            changes.check(cancels=[Cancel(event_id="nope", counts_against_follow_through=False),
                                   Cancel(event_id="email", counts_against_follow_through=False)])

        message = str(excinfo.value)
        assert "there's no event 'nope'" in message and "is cancelled already" in message
        assert excinfo.value.categories == ["unknown_event"]


class TestHistory:
    def _compacted(self):
        events = _day()
        events[0] = replace(events[0], compacted_until=time_at("10:00"))  # Email, over.
        events[1] = replace(events[1], compacted_until=time_at("10:30"))  # Report, compacted while going on.
        return events

    @pytest.mark.parametrize(
        "batch",
        [
            {"updates": [Event(id="email", summary="Inbox")]},
            {"updates": [_move("report", "10:15", "11:00")]},
            {"cancels": [Cancel(event_id="email", counts_against_follow_through=False)]},
            {"shifts": [Shift(event_ids=["email"], minutes=-10)]},
        ],
    )
    def test_compacted_events_cant_be_changed(self, batch):
        changes, _ = _changes(self._compacted())

        with pytest.raises(ChangeError, match="is history") as excinfo:
            changes.check(**batch)

        assert excinfo.value.categories == ["compacted"]

    def test_unless_the_user_has_approved_changing_history(self):
        changes, calendar = _changes(self._compacted())

        changes.apply(changes.check(updates=[Event(id="email", summary="Inbox")], allow_compacted=True), "update_event")

        assert calendar.events["email"].summary == "Inbox"

    def test_an_event_still_going_on_may_run_on(self):
        changes, _ = _changes(self._compacted())

        changes.check(updates=[Event(id="report", end=time_at("10:45"))])

        with pytest.raises(ChangeError, match="is history"):
            changes.check(updates=[Event(id="report", end=time_at("10:20"))])


def test_a_new_event_needs_a_start_and_an_end():
    changes, _ = _changes()

    with pytest.raises(ChangeError, match="new event 1 \\('Nap'\\) needs a start and an end"):
        changes.check(creates=[Event(summary="Nap", start=time_at("14:00"))])
