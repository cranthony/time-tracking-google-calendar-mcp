import argparse
import dataclasses
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


class TestParseBool:
    @pytest.mark.parametrize("value", ["true", "True", "1", "yes"])
    def test_parses_truthy_values(self, value):
        assert calendar_cli._parse_bool(value) is True

    @pytest.mark.parametrize("value", ["false", "False", "0", "no"])
    def test_parses_falsy_values(self, value):
        assert calendar_cli._parse_bool(value) is False

    def test_raises_on_unparseable_value(self):
        with pytest.raises(ValueError):
            calendar_cli._parse_bool("maybe")


class TestParseIsoDatetime:
    def test_parses_offset_datetime(self):
        assert calendar_cli._parse_iso_datetime("2026-01-01T09:00:00-05:00") == datetime(
            2026, 1, 1, 9, 0, tzinfo=timezone(timedelta(hours=-5))
        )

    def test_parses_z_suffix_as_utc(self):
        assert calendar_cli._parse_iso_datetime("2026-01-01T09:00:00Z") == datetime(
            2026, 1, 1, 9, 0, tzinfo=UTC
        )

    def test_raises_when_missing_timezone(self):
        with pytest.raises(ValueError):
            calendar_cli._parse_iso_datetime("2026-01-01T09:00:00")


class TestParseKeyValue:
    def test_parses_string_attribute(self):
        assert calendar_cli._parse_key_value("summary=New title") == (
            "summary",
            "New title",
        )

    def test_parses_int_attribute(self):
        assert calendar_cli._parse_key_value("priority=1") == ("priority", 1)

    def test_parses_bool_attribute(self):
        assert calendar_cli._parse_key_value("is_fixed_duration=true") == (
            "is_fixed_duration",
            True,
        )

    def test_parses_duration_attribute(self):
        key, value = calendar_cli._parse_key_value("min_duration=30m")
        assert key == "min_duration"
        assert value == timedelta(minutes=30)

    def test_parses_datetime_attribute(self):
        key, value = calendar_cli._parse_key_value("start=2026-01-01T09:00:00Z")
        assert key == "start"
        assert value == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

    def test_raises_when_missing_equals_sign(self):
        with pytest.raises(argparse.ArgumentTypeError):
            calendar_cli._parse_key_value("priority")

    def test_raises_on_unknown_attribute(self):
        with pytest.raises(argparse.ArgumentTypeError):
            calendar_cli._parse_key_value("id=new-id")

    def test_raises_on_invalid_value_for_known_attribute(self):
        with pytest.raises(argparse.ArgumentTypeError):
            calendar_cli._parse_key_value("priority=not-a-number")


class TestUpdatableAttributeParsers:
    def test_covers_every_event_attribute_except_id_and_recurring_event_id(self):
        # id would repoint the patch at a different event; recurring_event_id
        # is assigned by Google and never sent to the API, so setting it here
        # would silently have no effect.
        event_attributes = {f.name for f in dataclasses.fields(Event)} - {
            "id",
            "recurring_event_id",
        }

        assert set(calendar_cli._UPDATABLE_ATTRIBUTE_PARSERS) == event_attributes


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
        event = _event()

        details = calendar_cli._format_event_details(event)

        assert "id: abc123" in details
        assert "summary: Focus block" in details
        assert f"start: {event.start}" in details
        assert f"end: {event.end}" in details
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


class TestMainUpdateProperties:
    def test_sets_attributes_and_patches_without_fetching_first(self, capsys, monkeypatch):
        client = MagicMock()
        client.update_event.return_value = _event(priority=1, location="Room")
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(
            sys,
            "argv",
            ["calendar_cli.py", "update_properties", "abc123", "priority=1", "location=Room"],
        )

        calendar_cli.main()

        client.get_event.assert_not_called()
        sent_event = client.update_event.call_args[0][0]
        assert sent_event.id == "abc123"
        assert sent_event.priority == 1
        assert sent_event.location == "Room"
        out = capsys.readouterr().out
        assert "priority: 1" in out
        assert "location: Room" in out

    def test_only_sets_the_given_attributes(self, monkeypatch):
        client = MagicMock()
        client.update_event.side_effect = lambda event: event
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(
            sys, "argv", ["calendar_cli.py", "update_properties", "abc123", "priority=1"]
        )

        calendar_cli.main()

        sent_event = client.update_event.call_args[0][0]
        assert sent_event.priority == 1
        assert sent_event.summary is None
        assert sent_event.description is None
        assert sent_event.location is None


class TestMainCreate:
    def test_builds_event_and_delegates_to_reallocation(self, capsys, monkeypatch):
        client = MagicMock()
        client.create_event_with_reallocation.return_value = [_event(summary="New")]
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "calendar_cli.py",
                "create",
                "summary=New",
                "start=2026-01-01T09:00:00Z",
                "end=2026-01-01T09:30:00Z",
                "priority=1",
            ],
        )

        calendar_cli.main()

        (sent_event, options), _ = client.create_event_with_reallocation.call_args
        assert sent_event.id is None
        assert sent_event.summary == "New"
        assert sent_event.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert sent_event.end == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        assert sent_event.priority == 1
        assert options.split_threshold_minutes is None
        out = capsys.readouterr().out
        assert "summary: New" in out

    def test_prints_every_affected_event(self, monkeypatch, capsys):
        client = MagicMock()
        client.create_event_with_reallocation.return_value = [
            _event(id="abc123", summary="New"),
            _event(id="def456", summary="Shrunk"),
        ]
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "calendar_cli.py",
                "create",
                "summary=New",
                "start=2026-01-01T09:00:00Z",
                "end=2026-01-01T09:30:00Z",
            ],
        )

        calendar_cli.main()

        out = capsys.readouterr().out
        assert "abc123" in out
        assert "def456" in out

    @pytest.mark.parametrize(
        "properties",
        [
            ["start=2026-01-01T09:00:00Z", "end=2026-01-01T09:30:00Z"],
            ["summary=New", "end=2026-01-01T09:30:00Z"],
            ["summary=New", "start=2026-01-01T09:00:00Z"],
        ],
    )
    def test_requires_summary_start_and_end(self, monkeypatch, properties):
        client = MagicMock()
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "create", *properties])

        with pytest.raises(SystemExit):
            calendar_cli.main()

        client.create_event_with_reallocation.assert_not_called()


class TestMainDelete:
    def test_deletes_event(self, capsys, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "delete", "abc123"])

        calendar_cli.main()

        client.delete_event.assert_called_once_with("abc123")
        assert "abc123" in capsys.readouterr().out
