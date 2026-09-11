from datetime import datetime, timedelta, timezone

import pytest

from calendar_clients.google_calendar import Event
from utilities.reallocation import (
    ReallocationOptions,
    _duration,
    _reclaimable_seconds,
    reallocate_for_new_event,
)

UTC = timezone.utc


class TestDuration:
    def test_computes_end_minus_start(self):
        event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 30, tzinfo=UTC),
        )

        assert _duration(event) == timedelta(hours=1, minutes=30)


class TestReallocateForNewEvent:
    def test_raises_not_implemented(self):
        new_event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=1,
        )

        with pytest.raises(NotImplementedError):
            reallocate_for_new_event([], new_event, ReallocationOptions())

    def test_raises_not_implemented_with_options(self):
        new_event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=1,
        )

        with pytest.raises(NotImplementedError):
            reallocate_for_new_event(
                [],
                new_event,
                ReallocationOptions(min_duration_overrides={"abc123": 30}),
            )


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


class TestReclaimableSeconds:
    def test_raises_not_implemented(self):
        span = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=3,
        )

        with pytest.raises(NotImplementedError):
            _reclaimable_seconds(span)
