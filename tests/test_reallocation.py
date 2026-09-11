from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from calendar_clients.google_calendar import Event
from reallocation import _reclaimable_seconds, reallocate_for_new_event

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
            reallocate_for_new_event(MagicMock(), new_event)


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
