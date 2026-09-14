from datetime import timedelta

import pytest

from tests.event_time_helpers import event_at, time_at
from utilities.reallocation import (
    ReallocationOptions,
    _duration,
    _effective_min_duration,
    _effective_priority,
    _overlap,
    reallocate_for_new_event,
)


class TestDuration:
    def test_computes_end_minus_start(self):
        event = event_at("09:00-10:30")

        assert _duration(event) == timedelta(hours=1, minutes=30)


class TestOverlap:
    def test_zero_when_disjoint(self):
        event = event_at("09:00-09:30")
        new_event = event_at("11:00-11:30")

        assert _overlap(event, new_event) == timedelta(0)

    def test_zero_when_merely_adjacent(self):
        event = event_at("09:00-09:30")
        new_event = event_at("09:30-10:00")

        assert _overlap(event, new_event) == timedelta(0)

    def test_partial_overlap(self):
        event = event_at("09:00-09:40")
        new_event = event_at("09:30-10:00")

        assert _overlap(event, new_event) == timedelta(minutes=10)

    def test_new_event_entirely_within_event(self):
        event = event_at("09:00-11:00")
        new_event = event_at("09:30-10:00")

        assert _overlap(event, new_event) == timedelta(minutes=30)


class TestEffectivePriority:
    def test_returns_priority_when_set(self):
        assert _effective_priority(event_at("09:00-10:00", priority=3)) == 3

    def test_returns_two_when_unset(self):
        assert _effective_priority(event_at("09:00-10:00", priority=None)) == 2


class TestEffectiveMinDuration:
    def test_returns_own_min_duration_when_no_override(self):
        event = event_at("09:00-10:00", id="abc123", min_duration=timedelta(minutes=20))

        assert _effective_min_duration(event, {}) == timedelta(minutes=20)

    def test_returns_zero_when_unset_and_no_override(self):
        event = event_at("09:00-10:00", id="abc123", min_duration=None)

        assert _effective_min_duration(event, {}) == timedelta(0)

    def test_override_takes_precedence(self):
        event = event_at("09:00-10:00", id="abc123", min_duration=timedelta(minutes=20))

        assert _effective_min_duration(event, {"abc123": 5}) == timedelta(minutes=5)

    def test_override_ignored_for_a_different_id(self):
        event = event_at("09:00-10:00", id="abc123", min_duration=timedelta(minutes=20))

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


class TestReallocateForNewEvent:
    def test_validates_day_events_is_not_empty(self):
        new_event = event_at("09:00-10:00")

        with pytest.raises(ValueError):
            reallocate_for_new_event([], new_event, ReallocationOptions())

    def test_validates_last_event_ends_after_new_event(self):
        existing = event_at("08:00-08:30", id="e1", priority=1)
        new_event = event_at("09:00-10:00")

        with pytest.raises(ValueError):
            reallocate_for_new_event([existing], new_event, ReallocationOptions())

    def test_validates_new_event_id_not_already_present(self):
        old_version = event_at("09:00-09:30", id="m1")
        new_event = event_at("10:00-10:30", id="m1")

        with pytest.raises(ValueError):
            reallocate_for_new_event([old_version], new_event, ReallocationOptions())

    def test_validates_day_events_sorted(self):
        first = event_at("10:00-11:00", id="f1")
        second = event_at("09:00-09:30", id="s1")
        new_event = event_at("12:00-12:30")

        with pytest.raises(ValueError):
            reallocate_for_new_event([first, second], new_event, ReallocationOptions())

    def test_validates_day_events_non_overlapping(self):
        first = event_at("09:00-10:00", id="f1")
        second = event_at("09:30-10:30", id="s1")
        new_event = event_at("12:00-12:30")

        with pytest.raises(ValueError):
            reallocate_for_new_event([first, second], new_event, ReallocationOptions())

    def test_validates_preceding_overlap_must_be_the_first_event(self):
        first = event_at("08:00-08:30", id="f1")
        second = event_at("08:45-10:00", id="s1")
        new_event = event_at("09:00-09:30")

        with pytest.raises(ValueError):
            reallocate_for_new_event([first, second], new_event, ReallocationOptions())

    def test_cascades_through_untouched_events_until_a_gap_absorbs_it(self):
        # The exact case that motivated the cumulative walk: inserting a
        # new event where `first` already sits displaces `first`, which
        # displaces `second` in turn, until the natural hour of free time
        # before `third` absorbs the remaining 30 minutes -- third never
        # moves.
        first = event_at("09:00-09:30", id="f1", priority=1)
        second = event_at("09:30-10:00", id="s1", priority=1)
        third = event_at("11:00-11:30", id="t1", priority=1)
        new_event = event_at("09:00-09:30", priority=1)

        result = reallocate_for_new_event([first, second, third], new_event, ReallocationOptions())

        assert new_event.start == time_at("09:00")
        assert new_event.end == time_at("09:30")
        assert first.start == time_at("09:30")
        assert first.end == time_at("10:00")
        assert second.start == time_at("10:00")
        assert second.end == time_at("10:30")
        assert third.start == time_at("11:00")
        assert third.end == time_at("11:30")
        assert third not in result
        assert {e.id for e in result} == {"f1", "s1", None}

    def test_inserting_into_free_time_costs_nothing(self):
        existing = event_at("11:00-12:00", id="e1", priority=1)
        new_event = event_at("09:00-09:30", priority=1)

        result = reallocate_for_new_event([existing], new_event, ReallocationOptions())

        # new_event's whole span was already free time (nothing overlaps
        # it), so nothing needs to be reclaimed -- existing is untouched.
        assert result == [new_event]
        assert new_event.start == time_at("09:00")
        assert new_event.end == time_at("09:30")
        assert existing.start == time_at("11:00")
        assert existing.end == time_at("12:00")

    def test_shrinks_preceding_event_without_splitting_below_threshold(self):
        preceding = event_at(
            "09:00-09:40", id="p1", priority=1, min_duration=timedelta(minutes=30)
        )
        later = event_at("10:30-11:30", id="l1", priority=1)
        new_event = event_at("09:30-10:00", priority=1)

        result = reallocate_for_new_event([preceding, later], new_event, ReallocationOptions())

        # Only a 10-minute overlap (below the 15-minute default split
        # threshold), so preceding just shrinks -- no continuation event.
        # The 10 minutes new_event actually displaced is exactly what
        # preceding's own shrink already resolves, so nothing further
        # needs reclaiming -- later is untouched.
        assert preceding.start == time_at("09:00")
        assert preceding.end == time_at("09:30")
        assert new_event.start == time_at("09:30")
        assert new_event.end == time_at("10:00")
        assert later.start == time_at("10:30")
        assert later.end == time_at("11:30")
        assert later not in result
        assert {e.id for e in result} == {"p1", None}

    def test_splits_preceding_event_into_a_continuation(self):
        preceding = event_at(
            "09:00-11:00",
            id="p1",
            summary="Long meeting",
            priority=1,
            min_duration=timedelta(minutes=30),
        )
        new_event = event_at("09:30-10:00", priority=1)

        result = reallocate_for_new_event([preceding], new_event, ReallocationOptions())

        assert len(result) == 3
        by_start = sorted(result, key=lambda e: e.start)

        assert by_start[0] is preceding
        assert preceding.start == time_at("09:00")
        assert preceding.end == time_at("09:30")

        assert by_start[1] is new_event
        assert new_event.start == time_at("09:30")
        assert new_event.end == time_at("10:00")

        continuation = by_start[2]
        assert continuation is not preceding
        assert continuation.id is None
        assert continuation.summary == "Long meeting (continued)"
        # new_event landed entirely inside preceding's own original span,
        # with room on both sides -- preceding's split already accounts
        # for new_event's whole duration, so nothing extra is reclaimed
        # from continuation: it keeps its full remaining hour.
        assert continuation.start == time_at("10:00")
        assert continuation.end == time_at("11:00")

    def test_moves_preceding_event_whole_when_it_cannot_retain_its_min_duration(self):
        # preceding can only give 10 of its 15-minute floor before
        # new_event's start -- rather than truncate it to that
        # uncomfortable 10-minute sliver, it moves whole (still its
        # original 20 minutes) to right after new_event, ahead of anchor,
        # where it's just an ordinary span like any other: reclaimed from
        # down to its own floor (5 of its 20 minutes) since anchor alone
        # doesn't cover the rest of what's needed.
        preceding = event_at(
            "09:00-09:20", id="p1", priority=1, min_duration=timedelta(minutes=15)
        )
        anchor = event_at("09:20-10:00", id="a1", priority=1)
        new_event = event_at("09:10-09:40", priority=1)

        result = reallocate_for_new_event([preceding, anchor], new_event, ReallocationOptions())

        assert new_event.start == time_at("09:10")
        assert new_event.end == time_at("09:40")
        # preceding lands immediately after new_event -- ahead of anchor,
        # which was already in day_events -- shrunk to its own 15-minute
        # floor (not below, since it's just an ordinary span here).
        assert preceding.start == time_at("09:40")
        assert preceding.end == time_at("09:55")
        assert preceding in result
        assert anchor.start == time_at("09:55")
        assert anchor.end == time_at("10:10")
        assert anchor in result

    def test_moves_preceding_event_whole_with_no_other_day_events(self):
        # Same as above, but preceding is the only event in day_events --
        # regression guard for the case where there's nothing after it to
        # reinsert relative to.
        preceding = event_at(
            "09:00-10:00", id="p1", priority=1, min_duration=timedelta(minutes=50)
        )
        new_event = event_at("09:40-09:55", priority=1)

        result = reallocate_for_new_event([preceding], new_event, ReallocationOptions())

        assert new_event.start == time_at("09:40")
        assert new_event.end == time_at("09:55")
        assert preceding.start == time_at("09:55")
        assert preceding in result

    def test_shrinks_preceding_event_in_place_at_exactly_its_min_duration(self):
        # new_preceding_duration (15 min) exactly equals preceding's own
        # min_duration -- right at the boundary, so it still shrinks in
        # place rather than moving whole. leftover (13 min) is below the
        # default split threshold, so no continuation either. anchor sits
        # far enough out that the gap before it absorbs new_event's own
        # 10 minutes on its own, leaving preceding untouched beyond its
        # in-place shrink to floor -- isolating the boundary check from
        # the ordinary reclaim pass.
        preceding = event_at(
            "09:00-09:28", id="p1", priority=1, min_duration=timedelta(minutes=15)
        )
        anchor = event_at("11:00-12:00", id="a1", priority=1)
        new_event = event_at("09:15-09:25", priority=1)

        result = reallocate_for_new_event([preceding, anchor], new_event, ReallocationOptions())

        assert preceding.start == time_at("09:00")
        assert preceding.end == time_at("09:15")
        assert preceding in result
        assert new_event.start == time_at("09:15")
        assert new_event.end == time_at("09:25")
        assert anchor.start == time_at("11:00")
        assert anchor.end == time_at("12:00")
        assert anchor not in result

    def test_cancels_min_duration_protected_events_instead_of_shortfalling(self):
        # existing/anchor are both already at their min_duration floor, so
        # shrinking alone can't provide the 30 minutes needed -- but since
        # neither is more important than new_event, they're cancelled
        # outright (past their floor) rather than raising a shortfall.
        existing = event_at(
            "09:00-09:30", id="e1", priority=1, min_duration=timedelta(minutes=30)
        )
        anchor = event_at(
            "09:30-10:00", id="a1", priority=1, min_duration=timedelta(minutes=30)
        )
        new_event = event_at("09:00-09:30", priority=1)

        result = reallocate_for_new_event([existing, anchor], new_event, ReallocationOptions())

        assert existing.status == "cancelled"
        assert existing in result
        # existing alone covers the 30 minutes needed, so anchor -- next
        # in line -- is never even reached.
        assert anchor.start == time_at("09:30")
        assert anchor.end == time_at("10:00")
        assert anchor not in result

    def test_shrinks_a_higher_priority_events_zero_min_duration_before_cancelling_a_lower_one(self):
        # priority only decides the order spans are drawn from -- it's not
        # an eligibility gate. higher has no min_duration of its own (like
        # plenty of real events don't), so its normal shrink-to-floor pass
        # already takes all 20 minutes needed before lower -- a strictly
        # less important priority that would need cancelling outright to
        # help at all -- is ever touched.
        higher = event_at("09:00-09:30", id="h1", priority=1)
        lower = event_at("09:30-10:00", id="l1", priority=3, min_duration=timedelta(minutes=30))
        new_event = event_at("09:00-09:20", priority=2)

        result = reallocate_for_new_event([higher, lower], new_event, ReallocationOptions())

        assert higher.status != "cancelled"
        assert higher.start == time_at("09:20")
        assert higher.end == time_at("09:30")
        assert higher in result
        # lower is already at its own floor and has nothing to give in the
        # shrink pass, and higher alone was enough, so it's never even
        # considered for cancellation -- its position doesn't shift either,
        # since higher's total occupied time (new_event + its own 10
        # remaining minutes) exactly matches what higher alone used to.
        assert lower.start == time_at("09:30")
        assert lower.end == time_at("10:00")
        assert lower not in result

    def test_cancels_a_higher_priority_events_min_duration_as_a_last_resort(self):
        # important is a strictly more important priority than new_event,
        # with only 15 of its 60 minutes above its own 45-minute
        # min_duration floor -- but since no priority is exempt from
        # cancellation, the other 35 of the 50 minutes needed comes from
        # cancelling below that floor, rather than failing outright.
        important = event_at(
            "09:00-10:00", id="i1", priority=1, min_duration=timedelta(minutes=45)
        )
        new_event = event_at("09:00-09:50", priority=2)

        result = reallocate_for_new_event([important], new_event, ReallocationOptions())

        assert important.status != "cancelled"
        assert important.start == time_at("09:50")
        assert important.end == time_at("10:00")
        assert important in result

    def test_marks_fully_reclaimed_event_as_cancelled(self):
        # A strictly lower priority (higher number) than new_event's, so
        # the cancellation isn't riding on a tie-break.
        existing = event_at("09:00-09:30", id="e1", priority=5)
        anchor = event_at(
            "09:30-10:00", id="a1", priority=1, min_duration=timedelta(minutes=30)
        )
        new_event = event_at("09:00-09:30", priority=1)

        result = reallocate_for_new_event([existing, anchor], new_event, ReallocationOptions())
        assert result == [new_event, existing]

        assert existing.status == "cancelled"
        assert existing in result
        # Cancelling doesn't reposition it.
        assert existing.start == time_at("09:00")
        assert existing.end == time_at("09:30")
        # anchor was protected (at its min_duration floor already) and not
        # otherwise touched, so it doesn't move either.
        assert anchor.start == time_at("09:30")
        assert anchor.end == time_at("10:00")
        assert anchor not in result

    def test_cancels_below_min_duration_when_shrinking_alone_falls_short(self):
        # Shrinking later down to its 45-minute floor only recovers 15 of
        # the 30 minutes needed -- but since later isn't a higher priority
        # than new_event, the remaining 15 is cancelled outright below
        # that floor (down to 30 minutes total) rather than failing.
        later = event_at("09:00-10:00", id="e1", priority=1, min_duration=timedelta(minutes=45))
        new_event = event_at("09:00-09:30", priority=1)

        result = reallocate_for_new_event([later], new_event, ReallocationOptions())

        assert later.status != "cancelled"
        assert later.start == time_at("09:30")
        assert later.end == time_at("10:00")
        assert later in result

    def test_min_duration_override_lets_shrinking_alone_cover_the_reclaim(self):
        # With the override, shrinking later down to its (overridden) 15-
        # minute floor covers the full 30 minutes needed on its own --
        # reaching the exact same final position as the unoverridden case
        # above, but without ever cancelling past a min_duration floor.
        later = event_at("09:00-10:00", id="e1", priority=1, min_duration=timedelta(minutes=45))
        new_event = event_at("09:00-09:30", priority=1)

        result = reallocate_for_new_event(
            [later], new_event, ReallocationOptions(min_duration_overrides={"e1": 15})
        )
        assert result == [new_event, later]

        assert new_event.start == time_at("09:00")
        assert new_event.end == time_at("09:30")
        # later started tied with new_event, so new_event claims the
        # first 30 minutes and later is pushed to start right after it,
        # keeping its original end.
        assert later.start == time_at("09:30")
        assert later.end == time_at("10:00")
        assert later in result

    def test_does_not_touch_higher_priority_event_when_eligible_tier_is_enough(self):
        # Same shape as test_cascades_through_untouched_events_until_a_gap_
        # absorbs_it, except protected is a strictly more important priority
        # than new_event/first/second -- it must stay completely untouched,
        # since the eligible tier (first, second, and the natural gap
        # before protected) already covers the full reclaim on its own.
        first = event_at("09:00-09:30", id="f1", priority=2)
        second = event_at("09:30-10:00", id="s1", priority=2)
        protected = event_at(
            "11:00-11:30", id="t1", priority=1, min_duration=timedelta(minutes=15)
        )
        new_event = event_at("09:00-09:30", priority=2)

        result = reallocate_for_new_event(
            [first, second, protected], new_event, ReallocationOptions()
        )

        assert protected.start == time_at("11:00")
        assert protected.end == time_at("11:30")
        assert protected not in result

    def test_last_resort_reclaims_from_higher_priority_event_when_eligible_tier_falls_short(self):
        higher = event_at(
            "09:00-10:00", id="h1", priority=1, min_duration=timedelta(minutes=30)
        )
        normal = event_at("10:00-10:15", id="n1", priority=2)
        new_event = event_at("09:00-09:45", priority=2)

        result = reallocate_for_new_event([higher, normal], new_event, ReallocationOptions())

        # normal (same priority as new_event) only has 15 of the 45 minutes
        # needed -- rather than fail, the remaining 30 minutes are reclaimed
        # from higher (a strictly more important priority), down to its own
        # min_duration, as a last resort.
        assert new_event.start == time_at("09:00")
        assert new_event.end == time_at("09:45")
        assert higher.start == time_at("09:45")
        assert higher.end == time_at("10:15")
        assert normal.status == "cancelled"
        assert {e.id for e in result} == {None, "h1", "n1"}
