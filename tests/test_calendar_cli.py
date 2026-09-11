import argparse
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import calendar_cli
from calendar_clients.google_calendar import Event

UTC = timezone.utc


def _event(**overrides) -> Event:
    fields = {
        "id": "abc123",
        "summary": "Focus block",
        "start": datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    }
    fields.update(overrides)
    return Event(**fields)


class TestParseDuration:
    def test_parses_valid_duration(self):
        assert calendar_cli._parse_duration("1h") == 3600

    def test_raises_on_unparseable_duration(self):
        with pytest.raises(argparse.ArgumentTypeError):
            calendar_cli._parse_duration("not a duration")


class TestResolveWindow:
    def test_resolves_around_given_now(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

        time_min, time_max = calendar_cli.resolve_window(3600, 7200, now=now)

        assert time_min == datetime(2026, 1, 1, 11, 0, tzinfo=UTC)
        assert time_max == datetime(2026, 1, 1, 14, 0, tzinfo=UTC)

    def test_defaults_to_current_time_when_now_not_given(self):
        before = datetime.now(UTC)

        time_min, time_max = calendar_cli.resolve_window(0, 0)

        after = datetime.now(UTC)
        assert before <= time_min <= after
        assert before <= time_max <= after


class TestFormatEventLine:
    def test_includes_id_times_and_summary(self):
        line = calendar_cli._format_event_line(_event())

        assert "abc123" in line
        assert "Focus block" in line
        assert "2026-01-01T09:00:00+00:00" in line


class TestFormatEventDetails:
    def test_includes_required_fields_only_when_optional_fields_absent(self):
        details = calendar_cli._format_event_details(_event())

        assert "id: abc123" in details
        assert "summary: Focus block" in details
        assert "start: 2026-01-01T09:00:00+00:00" in details
        assert "end: 2026-01-01T10:00:00+00:00" in details
        assert "description" not in details
        assert "location" not in details
        assert "min_duration" not in details
        assert "is_fixed_duration" not in details
        assert "priority" not in details

    def test_includes_optional_fields_when_set(self):
        event = _event(
            description="Details",
            location="Room",
            min_duration=timedelta(minutes=30),
            is_fixed_duration=True,
            priority=1,
        )

        details = calendar_cli._format_event_details(event)

        assert "description: Details" in details
        assert "location: Room" in details
        assert "min_duration: 0:30:00" in details
        assert "is_fixed_duration: True" in details
        assert "priority: 1" in details


class TestMainList:
    def test_lists_events(self, capsys, monkeypatch):
        client = MagicMock()
        client.list_events.return_value = [_event()]
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "list", "1h", "2h"])

        calendar_cli.main()

        client.list_events.assert_called_once()
        out = capsys.readouterr().out
        assert "abc123" in out
        assert "Focus block" in out

    def test_prints_message_when_no_events(self, capsys, monkeypatch):
        client = MagicMock()
        client.list_events.return_value = []
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "list"])

        calendar_cli.main()

        assert "No events found." in capsys.readouterr().out

    def test_uses_default_window_when_omitted(self, monkeypatch):
        client = MagicMock()
        client.list_events.return_value = []
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "list"])

        calendar_cli.main()

        time_min, time_max = client.list_events.call_args[0]
        assert (time_max - time_min) == timedelta(hours=2)


class TestMainGet:
    def test_gets_event(self, capsys, monkeypatch):
        client = MagicMock()
        client.get_event.return_value = _event()
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "get", "abc123"])

        calendar_cli.main()

        client.get_event.assert_called_once_with("abc123")
        assert "id: abc123" in capsys.readouterr().out
