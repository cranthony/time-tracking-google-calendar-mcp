from datetime import datetime, timezone

import pytest

from calendar_clients.google_calendar import Event
from utilities.reallocation import (
    ReallocationOptions,
    _reclaimable_seconds,
    reallocate_for_new_event,
)

UTC = timezone.utc


class TestReallocateForNewEvent:
    def test_raises_not_implemented(self):
        new_event = Event(
            summary="Focus block",
            start=datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
            end=datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
            priority=1,
        )

        with pytest.raises(NotImplementedError):
            reallocate_for_new_event([], new_event)

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
                options=ReallocationOptions(ignore_min_duration_for_event_ids=frozenset({"abc123"})),
            )


class TestReallocationOptions:
    def test_defaults(self):
        options = ReallocationOptions()

        assert options.split_threshold_minutes == 15
        assert options.ignore_min_duration_for_event_ids == frozenset()


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
