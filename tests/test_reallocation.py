from datetime import datetime, timedelta, timezone

import pytest

from calendar_clients.google_calendar import Event
from utilities.reallocation import (
    ReallocationError,
    ReallocationOptions,
    _duration,
    _effective_min_duration,
    _effective_priority,
    _reclaimable_minutes,
    reallocate_for_new_event,
)

UTC = timezone.utc


def _event(**overrides) -> Event:
    fields = {
        "summary": "Event",
        "start": datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    }
    fields.update(overrides)
    return Event(**fields)


class TestDuration:
    def test_computes_end_minus_start(self):
        event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
        )

        assert _duration(event) == timedelta(hours=1, minutes=30)


class TestEffectivePriority:
    def test_returns_priority_when_set(self):
        assert _effective_priority(_event(priority=3)) == 3

    def test_returns_one_when_unset(self):
        assert _effective_priority(_event(priority=None)) == 1


class TestEffectiveMinDuration:
    def test_returns_own_min_duration_when_no_override(self):
        event = _event(id="abc123", min_duration=timedelta(minutes=20))

        assert _effective_min_duration(event, {}) == timedelta(minutes=20)

    def test_returns_zero_when_unset_and_no_override(self):
        event = _event(id="abc123", min_duration=None)

        assert _effective_min_duration(event, {}) == timedelta(0)

    def test_override_takes_precedence(self):
        event = _event(id="abc123", min_duration=timedelta(minutes=20))

        assert _effective_min_duration(event, {"abc123": 5}) == timedelta(minutes=5)

    def test_override_ignored_for_a_different_id(self):
        event = _event(id="abc123", min_duration=timedelta(minutes=20))

        assert _effective_min_duration(event, {"other": 5}) == timedelta(minutes=20)


class TestReclaimableMinutes:
    def test_full_duration_when_min_duration_unset(self):
        event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )

        assert _reclaimable_minutes(event) == 60.0

    def test_duration_minus_min_duration(self):
        event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            min_duration=timedelta(minutes=20),
        )

        assert _reclaimable_minutes(event) == 40.0

    def test_floored_at_zero(self):
        event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            min_duration=timedelta(hours=1),
        )

        assert _reclaimable_minutes(event) == 0.0


class TestReallocationOptions:
    def test_defaults_are_unset(self):
        options = ReallocationOptions()

        assert options.split_threshold_minutes is None
        assert options.min_duration_overrides is None

    def test_resolved_fills_in_defaults(self):
        resolved = ReallocationOptions().resolved()

        assert resolved.split_threshold_minutes == 15
        assert resolved.min_duration_overrides == {}

    def test_resolved_keeps_explicit_values(self):
        options = ReallocationOptions(
            split_threshold_minutes=30, min_duration_overrides={"abc123": 10}
        )

        resolved = options.resolved()

        assert resolved.split_threshold_minutes == 30
        assert resolved.min_duration_overrides == {"abc123": 10}


class TestReallocationError:
    def test_defaults(self):
        error = ReallocationError("no room")

        assert str(error) == "no room"
        assert error.remaining is None
        assert error.higher_priority_events == []
        assert error.events_at_floor == []

    def test_carries_structured_fields(self):
        event = _event(id="abc123")

        error = ReallocationError(
            "no room",
            remaining=timedelta(minutes=5),
            higher_priority_events=[event],
            events_at_floor=[],
        )

        assert error.remaining == timedelta(minutes=5)
        assert error.higher_priority_events == [event]


class TestReallocateForNewEvent:
    def test_validates_day_events_is_not_empty(self):
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )

        with pytest.raises(ValueError):
            reallocate_for_new_event([], new_event, ReallocationOptions())

    def test_validates_last_event_ends_after_new_event(self):
        existing = _event(
            id="e1",
            start=datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 8, 30, tzinfo=UTC),
            priority=1,
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )

        with pytest.raises(ValueError):
            reallocate_for_new_event([existing], new_event, ReallocationOptions())

    def test_validates_new_event_id_not_already_present(self):
        old_version = _event(
            id="m1",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
        )
        new_event = _event(
            id="m1",
            start=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
        )

        with pytest.raises(ValueError):
            reallocate_for_new_event([old_version], new_event, ReallocationOptions())

    def test_validates_day_events_sorted(self):
        first = _event(
            id="f1",
            start=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
        )
        second = _event(
            id="s1",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 12, 30, tzinfo=UTC),
        )

        with pytest.raises(ValueError):
            reallocate_for_new_event([first, second], new_event, ReallocationOptions())

    def test_validates_day_events_non_overlapping(self):
        first = _event(
            id="f1",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )
        second = _event(
            id="s1",
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 12, 30, tzinfo=UTC),
        )

        with pytest.raises(ValueError):
            reallocate_for_new_event([first, second], new_event, ReallocationOptions())

    def test_validates_preceding_overlap_must_be_the_first_event(self):
        first = _event(
            id="f1",
            start=datetime(2026, 1, 1, 8, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 8, 30, tzinfo=UTC),
        )
        second = _event(
            id="s1",
            start=datetime(2026, 1, 1, 8, 45, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
        )

        with pytest.raises(ValueError):
            reallocate_for_new_event([first, second], new_event, ReallocationOptions())

    def test_raises_on_shortfall_when_nothing_is_reclaimable(self):
        anchor = _event(
            id="a1",
            start=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
            priority=1,
            min_duration=timedelta(hours=1),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=1,
        )

        with pytest.raises(ReallocationError) as exc_info:
            reallocate_for_new_event([anchor], new_event, ReallocationOptions())

        assert exc_info.value.remaining == timedelta(hours=1)
        assert exc_info.value.higher_priority_events == []
        assert exc_info.value.events_at_floor == [anchor]

    def test_reclaims_from_a_gap_without_moving_the_untouched_event(self):
        existing = _event(
            id="e1",
            start=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
            priority=1,
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            priority=1,
        )

        result = reallocate_for_new_event([existing], new_event, ReallocationOptions())

        assert result == [new_event]
        assert new_event.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert new_event.end == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        # Only the gap was reclaimed from -- existing itself is untouched.
        assert existing.start == datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
        assert existing.end == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def test_shrinks_preceding_event_without_splitting_below_threshold(self):
        preceding = _event(
            id="p1",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 40, tzinfo=UTC),
            priority=1,
            min_duration=timedelta(minutes=30),
        )
        later = _event(
            id="l1",
            start=datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 11, 30, tzinfo=UTC),
            priority=1,
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=1,
        )

        result = reallocate_for_new_event([preceding, later], new_event, ReallocationOptions())

        # Only a 10-minute overlap (below the 15-minute default split
        # threshold), so preceding just shrinks -- no continuation event.
        assert preceding.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert preceding.end == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        assert new_event.start == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        assert new_event.end == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        # The 30 minutes new_event needed came from the gap after it, not
        # from later itself -- later doesn't move.
        assert later.start == datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
        assert later.end == datetime(2026, 1, 1, 11, 30, tzinfo=UTC)
        assert later not in result
        assert {e.id for e in result} == {"p1", None}

    def test_splits_preceding_event_into_a_continuation(self):
        preceding = _event(
            id="p1",
            summary="Long meeting",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
            priority=1,
            min_duration=timedelta(minutes=30),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=1,
        )

        result = reallocate_for_new_event([preceding], new_event, ReallocationOptions())

        assert len(result) == 3
        by_start = sorted(result, key=lambda e: e.start)

        assert by_start[0] is preceding
        assert preceding.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert preceding.end == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)

        assert by_start[1] is new_event
        assert new_event.start == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        assert new_event.end == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)

        continuation = by_start[2]
        assert continuation is not preceding
        assert continuation.id is None
        assert continuation.summary == "Long meeting (continued)"
        # Starts right where new_event ends (not where new_event starts --
        # that would overlap it), and gave up the 30 minutes new_event
        # needed off its own (originally 1-hour) remainder.
        assert continuation.start == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        assert continuation.end == datetime(2026, 1, 1, 10, 30, tzinfo=UTC)

    def test_raises_when_preceding_event_cannot_shrink_enough(self):
        preceding = _event(
            id="p1",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=1,
            min_duration=timedelta(minutes=45),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 20, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 50, tzinfo=UTC),
            priority=1,
        )

        with pytest.raises(ReallocationError):
            reallocate_for_new_event([preceding], new_event, ReallocationOptions())

        # Nothing should have been mutated before the exception.
        assert preceding.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert preceding.end == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)

    def test_raises_on_shortfall_with_structured_details(self):
        existing = _event(
            id="e1",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            priority=1,
            min_duration=timedelta(minutes=30),
        )
        anchor = _event(
            id="a1",
            start=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
            priority=1,
            min_duration=timedelta(hours=1),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=1,
        )

        with pytest.raises(ReallocationError) as exc_info:
            reallocate_for_new_event([existing, anchor], new_event, ReallocationOptions())

        assert exc_info.value.remaining == timedelta(minutes=30)
        assert exc_info.value.higher_priority_events == []
        assert exc_info.value.events_at_floor == [anchor, existing]
        # Nothing should have been mutated.
        assert existing.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert existing.end == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)

    def test_marks_fully_reclaimed_event_as_cancelled(self):
        # A strictly lower priority (higher number) than new_event's, so
        # the cancellation isn't riding on a tie-break.
        existing = _event(
            id="e1",
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=5,
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            priority=1,
        )

        result = reallocate_for_new_event([existing], new_event, ReallocationOptions())

        assert existing.status == "cancelled"
        assert existing in result
        # Cancelling doesn't reposition it.
        assert existing.start == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        assert existing.end == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)

    def test_shortfall_without_min_duration_override(self):
        later = _event(
            id="e1",
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
            priority=1,
            min_duration=timedelta(minutes=45),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            priority=1,
        )

        with pytest.raises(ReallocationError) as exc_info:
            reallocate_for_new_event([later], new_event, ReallocationOptions())

        assert exc_info.value.remaining == timedelta(minutes=15)

    def test_min_duration_override_enables_reclaim_that_would_otherwise_shortfall(self):
        later = _event(
            id="e1",
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
            priority=1,
            min_duration=timedelta(minutes=45),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            priority=1,
        )

        result = reallocate_for_new_event(
            [later], new_event, ReallocationOptions(min_duration_overrides={"e1": 15})
        )

        assert new_event.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert new_event.end == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        # later doesn't move -- it shrinks from its own end, keeping its
        # own start.
        assert later.start == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        assert later.end == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        assert later in result
