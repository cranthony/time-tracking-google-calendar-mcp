from datetime import datetime, timedelta, timezone

import pytest

from calendar_clients.google_calendar import Event
from utilities.reallocation import (
    ReallocationConflictError,
    ReallocationOptions,
    ReallocationShortfallError,
    _duration,
    _effective_min_duration,
    _effective_priority,
    _overlap,
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


class TestOverlap:
    def test_zero_when_disjoint(self):
        event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 11, 30, tzinfo=UTC),
        )

        assert _overlap(event, new_event) == timedelta(0)

    def test_zero_when_merely_adjacent(self):
        event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )

        assert _overlap(event, new_event) == timedelta(0)

    def test_partial_overlap(self):
        event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 40, tzinfo=UTC),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )

        assert _overlap(event, new_event) == timedelta(minutes=10)

    def test_new_event_entirely_within_event(self):
        event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
        )

        assert _overlap(event, new_event) == timedelta(minutes=30)


class TestEffectivePriority:
    def test_returns_priority_when_set(self):
        assert _effective_priority(_event(priority=3)) == 3

    def test_returns_two_when_unset(self):
        assert _effective_priority(_event(priority=None)) == 2


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
        error = ReallocationShortfallError("no room")

        assert str(error) == "no room"
        assert error.remaining is None
        assert error.events_at_floor == []

    def test_carries_structured_fields(self):
        event = _event(id="abc123")

        error = ReallocationShortfallError(
            "no room",
            remaining=timedelta(minutes=5),
            events_at_floor=[event],
        )

        assert error.remaining == timedelta(minutes=5)
        assert error.events_at_floor == [event]


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

    def test_cascades_through_untouched_events_until_a_gap_absorbs_it(self):
        # The exact case that motivated the cumulative walk: inserting a
        # new event where `first` already sits displaces `first`, which
        # displaces `second` in turn, until the natural hour of free time
        # before `third` absorbs the remaining 30 minutes -- third never
        # moves.
        first = _event(
            id="f1",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            priority=1,
        )
        second = _event(
            id="s1",
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=1,
        )
        third = _event(
            id="t1",
            start=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 11, 30, tzinfo=UTC),
            priority=1,
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            priority=1,
        )

        result = reallocate_for_new_event([first, second, third], new_event, ReallocationOptions())

        assert new_event.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert new_event.end == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        assert first.start == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        assert first.end == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        assert second.start == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        assert second.end == datetime(2026, 1, 1, 10, 30, tzinfo=UTC)
        assert third.start == datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
        assert third.end == datetime(2026, 1, 1, 11, 30, tzinfo=UTC)
        assert third not in result
        assert {e.id for e in result} == {"f1", "s1", None}

    def test_inserting_into_free_time_costs_nothing(self):
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

        # new_event's whole span was already free time (nothing overlaps
        # it), so nothing needs to be reclaimed -- existing is untouched.
        assert result == [new_event]
        assert new_event.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert new_event.end == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
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
        # The 10 minutes new_event actually displaced is exactly what
        # preceding's own shrink already resolves, so nothing further
        # needs reclaiming -- later is untouched.
        assert preceding.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert preceding.end == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        assert new_event.start == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        assert new_event.end == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
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
        # new_event landed entirely inside preceding's own original span,
        # with room on both sides -- preceding's split already accounts
        # for new_event's whole duration, so nothing extra is reclaimed
        # from continuation: it keeps its full remaining hour.
        assert continuation.start == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        assert continuation.end == datetime(2026, 1, 1, 11, 0, tzinfo=UTC)

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

        with pytest.raises(ReallocationConflictError):
            reallocate_for_new_event([preceding], new_event, ReallocationOptions())

        # Nothing should have been mutated before the exception.
        assert preceding.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert preceding.end == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)

    def test_raises_on_shortfall_when_nothing_is_reclaimable(self):
        existing = _event(
            id="e1",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            priority=1,
            min_duration=timedelta(minutes=30),
        )
        anchor = _event(
            id="a1",
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=1,
            min_duration=timedelta(minutes=30),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            priority=1,
        )

        with pytest.raises(ReallocationShortfallError) as exc_info:
            reallocate_for_new_event([existing, anchor], new_event, ReallocationOptions())

        assert exc_info.value.remaining == timedelta(minutes=30)
        assert exc_info.value.events_at_floor == [existing, anchor]

    def test_raises_on_shortfall_with_structured_details(self):
        # higher has no min_duration of its own, so even though it's
        # protected by priority, it still gets drained down to 0 as a last
        # resort once protected/anchor (both already at their own floor)
        # aren't enough on their own -- 30 of the 60 minutes needed.
        higher = _event(
            id="h1",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            priority=1,
        )
        protected = _event(
            id="p1",
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=2,
            min_duration=timedelta(minutes=30),
        )
        anchor = _event(
            id="a1",
            start=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
            priority=2,
            min_duration=timedelta(minutes=30),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=2,
        )

        with pytest.raises(ReallocationShortfallError) as exc_info:
            reallocate_for_new_event([higher, protected, anchor], new_event, ReallocationOptions())

        assert exc_info.value.remaining == timedelta(minutes=30)
        assert exc_info.value.events_at_floor == [protected, anchor, higher]
        # Nothing should have been mutated.
        assert higher.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert protected.start == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)

    def test_marks_fully_reclaimed_event_as_cancelled(self):
        # A strictly lower priority (higher number) than new_event's, so
        # the cancellation isn't riding on a tie-break.
        existing = _event(
            id="e1",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            priority=5,
        )
        anchor = _event(
            id="a1",
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=1,
            min_duration=timedelta(minutes=30),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            priority=1,
        )

        result = reallocate_for_new_event([existing, anchor], new_event, ReallocationOptions())
        assert result == [new_event, existing]

        assert existing.status == "cancelled"
        assert existing in result
        # Cancelling doesn't reposition it.
        assert existing.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert existing.end == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        # anchor was protected (at its min_duration floor already) and not
        # otherwise touched, so it doesn't move either.
        assert anchor.start == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        assert anchor.end == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        assert anchor not in result

    def test_shortfall_without_min_duration_override(self):
        later = _event(
            id="e1",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=1,
            min_duration=timedelta(minutes=45),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            priority=1,
        )

        with pytest.raises(ReallocationShortfallError) as exc_info:
            reallocate_for_new_event([later], new_event, ReallocationOptions())

        assert exc_info.value.remaining == timedelta(minutes=15)

    def test_min_duration_override_enables_reclaim_that_would_otherwise_shortfall(self):
        later = _event(
            id="e1",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
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
        assert result == [new_event, later]

        assert new_event.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert new_event.end == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        # later started tied with new_event, so new_event claims the
        # first 30 minutes and later is pushed to start right after it,
        # keeping its original end.
        assert later.start == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        assert later.end == datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
        assert later in result

    def test_does_not_touch_higher_priority_event_when_eligible_tier_is_enough(self):
        # Same shape as test_cascades_through_untouched_events_until_a_gap_
        # absorbs_it, except protected is a strictly more important priority
        # than new_event/first/second -- it must stay completely untouched,
        # since the eligible tier (first, second, and the natural gap
        # before protected) already covers the full reclaim on its own.
        first = _event(
            id="f1",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            priority=2,
        )
        second = _event(
            id="s1",
            start=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=2,
        )
        protected = _event(
            id="t1",
            start=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 11, 30, tzinfo=UTC),
            priority=1,
            min_duration=timedelta(minutes=15),
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 30, tzinfo=UTC),
            priority=2,
        )

        result = reallocate_for_new_event(
            [first, second, protected], new_event, ReallocationOptions()
        )

        assert protected.start == datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
        assert protected.end == datetime(2026, 1, 1, 11, 30, tzinfo=UTC)
        assert protected not in result

    def test_last_resort_reclaims_from_higher_priority_event_when_eligible_tier_falls_short(self):
        higher = _event(
            id="h1",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=1,
            min_duration=timedelta(minutes=30),
        )
        normal = _event(
            id="n1",
            start=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 15, tzinfo=UTC),
            priority=2,
        )
        new_event = _event(
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 9, 45, tzinfo=UTC),
            priority=2,
        )

        result = reallocate_for_new_event([higher, normal], new_event, ReallocationOptions())

        # normal (same priority as new_event) only has 15 of the 45 minutes
        # needed -- rather than fail, the remaining 30 minutes are reclaimed
        # from higher (a strictly more important priority), down to its own
        # min_duration, as a last resort.
        assert new_event.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert new_event.end == datetime(2026, 1, 1, 9, 45, tzinfo=UTC)
        assert higher.start == datetime(2026, 1, 1, 9, 45, tzinfo=UTC)
        assert higher.end == datetime(2026, 1, 1, 10, 15, tzinfo=UTC)
        assert normal.status == "cancelled"
        assert {e.id for e in result} == {None, "h1", "n1"}
