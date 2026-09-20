import argparse
import dataclasses
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import calendar_cli
from calendar_clients.google_calendar import Event
from calendar_clients.google_calendar import EventLabel as RawEventLabel
from utilities.event_labels import EventLabel
from utilities.label_priority_calendar import LabelPriorityCalendar
from utilities.noted_time_sheet import NotedTime

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


def _raw_event_label(**overrides) -> RawEventLabel:
    fields = {"id": "label-1", "background_color": "#8e24aa", "name": "Design Work"}
    fields.update(overrides)
    return RawEventLabel(**fields)


def _event_label(**overrides) -> EventLabel:
    fields = {"id": "label-1", "background_color": "#8e24aa", "name": "Design Work"}
    fields.update(overrides)
    return EventLabel(**fields)


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
        assert calendar_cli._parse_event_key_value("summary=New title") == (
            "summary",
            "New title",
        )

    def test_parses_int_attribute(self):
        assert calendar_cli._parse_event_key_value("priority=1") == ("priority", 1)

    def test_parses_bool_attribute(self):
        assert calendar_cli._parse_event_key_value("is_fixed_duration=true") == (
            "is_fixed_duration",
            True,
        )

    def test_parses_duration_attribute(self):
        key, value = calendar_cli._parse_event_key_value("min_duration=30m")
        assert key == "min_duration"
        assert value == timedelta(minutes=30)

    def test_parses_datetime_attribute(self):
        key, value = calendar_cli._parse_event_key_value("start=2026-01-01T09:00:00Z")
        assert key == "start"
        assert value == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)

    def test_raises_when_missing_equals_sign(self):
        with pytest.raises(argparse.ArgumentTypeError):
            calendar_cli._parse_event_key_value("priority")

    def test_raises_on_unknown_attribute(self):
        with pytest.raises(argparse.ArgumentTypeError):
            calendar_cli._parse_event_key_value("id=new-id")

    def test_raises_on_invalid_value_for_known_attribute(self):
        with pytest.raises(argparse.ArgumentTypeError):
            calendar_cli._parse_event_key_value("priority=not-a-number")


class TestParseLabelKeyValue:
    def test_parses_background_color(self):
        assert calendar_cli._parse_label_key_value("background_color=#8e24aa") == (
            "background_color",
            "#8e24aa",
        )

    def test_parses_name(self):
        assert calendar_cli._parse_label_key_value("name=Design Work") == (
            "name",
            "Design Work",
        )

    def test_parses_priority(self):
        assert calendar_cli._parse_label_key_value("priority=1") == ("priority", 1)

    def test_raises_when_missing_equals_sign(self):
        with pytest.raises(argparse.ArgumentTypeError):
            calendar_cli._parse_label_key_value("background_color")

    def test_raises_on_unknown_attribute(self):
        with pytest.raises(argparse.ArgumentTypeError):
            calendar_cli._parse_label_key_value("id=new-id")


class TestParseRawLabelKeyValue:
    def test_parses_background_color(self):
        assert calendar_cli._parse_raw_label_key_value("background_color=#8e24aa") == (
            "background_color",
            "#8e24aa",
        )

    def test_parses_name(self):
        assert calendar_cli._parse_raw_label_key_value("name=Design Work") == (
            "name",
            "Design Work",
        )

    def test_raises_when_missing_equals_sign(self):
        with pytest.raises(argparse.ArgumentTypeError):
            calendar_cli._parse_raw_label_key_value("background_color")

    def test_raises_on_unknown_attribute(self):
        with pytest.raises(argparse.ArgumentTypeError):
            calendar_cli._parse_raw_label_key_value("id=new-id")

    def test_raises_on_priority_since_raw_labels_have_none(self):
        with pytest.raises(argparse.ArgumentTypeError):
            calendar_cli._parse_raw_label_key_value("priority=1")


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


class TestLabelAttributeParsers:
    def test_covers_every_label_attribute_except_id(self):
        # id is assigned by Google when a label is created, and would
        # repoint update_label at a different label if it could be set.
        label_attributes = {f.name for f in dataclasses.fields(EventLabel)} - {"id"}

        assert set(calendar_cli._LABEL_ATTRIBUTE_PARSERS) == label_attributes


class TestRawLabelAttributeParsers:
    def test_covers_every_raw_label_attribute_except_id(self):
        raw_label_attributes = {f.name for f in dataclasses.fields(RawEventLabel)} - {"id"}

        assert set(calendar_cli._RAW_LABEL_ATTRIBUTE_PARSERS) == raw_label_attributes


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
        assert "is_fixed_time" not in details
        assert "priority" not in details

    def test_includes_optional_fields_when_set(self):
        event = _event(
            description="Details",
            location="Room",
            min_duration=timedelta(minutes=30),
            is_fixed_duration=True,
            is_fixed_time=True,
            priority=1,
        )

        details = calendar_cli._format_event_details(event)

        assert "description: Details" in details
        assert "location: Room" in details
        assert "min_duration: 0:30:00" in details
        assert "is_fixed_duration: True" in details
        assert "is_fixed_time: True" in details
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


def _fake_reallocating_calendar(monkeypatch) -> MagicMock:
    reallocating_calendar = MagicMock()
    monkeypatch.setattr(
        calendar_cli, "_build_reallocating_calendar", lambda client: reallocating_calendar
    )
    return reallocating_calendar


class TestBuildReallocatingCalendar:
    def test_wraps_client_in_a_label_priority_calendar(self, monkeypatch):
        client = MagicMock()
        event_labels = MagicMock()
        monkeypatch.setattr(calendar_cli, "build_event_labels", lambda: event_labels)

        reallocating_calendar = calendar_cli._build_reallocating_calendar(client)

        assert isinstance(reallocating_calendar._client, LabelPriorityCalendar)
        assert reallocating_calendar._client._client is client
        assert reallocating_calendar._client._event_labels is event_labels


class TestMainUpdate:
    def test_builds_event_and_delegates_to_reallocation(self, capsys, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        reallocating_calendar.update_event.return_value = [_event(summary="Moved")]
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "calendar_cli.py",
                "update",
                "abc123",
                "start=2026-01-01T09:00:00Z",
                "end=2026-01-01T09:30:00Z",
                "priority=1",
            ],
        )

        calendar_cli.main()

        (sent_event, options), _ = reallocating_calendar.update_event.call_args
        assert sent_event.id == "abc123"
        assert sent_event.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert sent_event.end == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        assert sent_event.priority == 1
        assert options.split_threshold_minutes is None
        out = capsys.readouterr().out
        assert "summary: Moved" in out

    def test_prints_every_affected_event(self, monkeypatch, capsys):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        reallocating_calendar.update_event.return_value = [
            _event(id="abc123", summary="Moved"),
            _event(id="def456", summary="Shrunk"),
        ]
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "calendar_cli.py",
                "update",
                "abc123",
                "start=2026-01-01T09:00:00Z",
                "end=2026-01-01T09:30:00Z",
            ],
        )

        calendar_cli.main()

        out = capsys.readouterr().out
        assert "abc123" in out
        assert "def456" in out

    def test_allows_start_without_end(self, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        reallocating_calendar.update_event.return_value = [_event()]
        monkeypatch.setattr(
            sys, "argv", ["calendar_cli.py", "update", "abc123", "start=2026-01-01T09:00:00Z"]
        )

        calendar_cli.main()

        sent_event = reallocating_calendar.update_event.call_args[0][0]
        assert sent_event.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert sent_event.end is None

    def test_allows_end_without_start(self, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        reallocating_calendar.update_event.return_value = [_event()]
        monkeypatch.setattr(
            sys, "argv", ["calendar_cli.py", "update", "abc123", "end=2026-01-01T09:30:00Z"]
        )

        calendar_cli.main()

        sent_event = reallocating_calendar.update_event.call_args[0][0]
        assert sent_event.end == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        assert sent_event.start is None

    def test_requires_start_or_end(self, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "update", "abc123", "priority=1"])

        with pytest.raises(SystemExit):
            calendar_cli.main()

        reallocating_calendar.update_event.assert_not_called()


class TestMainCreate:
    def test_builds_event_and_delegates_to_reallocation(self, capsys, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        reallocating_calendar.create_event.return_value = [_event(summary="New")]
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

        (sent_event, options), _ = reallocating_calendar.create_event.call_args
        assert sent_event.id is None
        assert sent_event.summary == "New"
        assert sent_event.start == datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        assert sent_event.end == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        assert sent_event.priority == 1
        assert options.split_threshold_minutes is None
        out = capsys.readouterr().out
        assert "summary: New" in out

    def test_prints_every_affected_event(self, monkeypatch, capsys):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        reallocating_calendar.create_event.return_value = [
            _event(id="abc123", summary="New"),
            _event(id="def456", summary="Shrunk"),
        ]
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
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "create", *properties])

        with pytest.raises(SystemExit):
            calendar_cli.main()

        reallocating_calendar.create_event.assert_not_called()


class TestMainDelete:
    def test_deletes_event(self, capsys, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "delete", "abc123"])

        calendar_cli.main()

        client.delete_event.assert_called_once_with("abc123")
        assert "abc123" in capsys.readouterr().out


def _fake_event_labels(monkeypatch) -> MagicMock:
    event_labels = MagicMock()
    monkeypatch.setattr(calendar_cli, "build_event_labels", lambda: event_labels)
    return event_labels


def _fake_noted_time_sheet(monkeypatch) -> MagicMock:
    noted_time_sheet = MagicMock()
    monkeypatch.setattr(calendar_cli, "build_noted_time_sheet", lambda: noted_time_sheet)
    return noted_time_sheet


class TestMainListRawLabels:
    def test_lists_labels(self, capsys, monkeypatch):
        client = MagicMock()
        client.list_event_labels.return_value = ([_raw_event_label()], '"etag-1"')
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "list_raw_labels"])

        calendar_cli.main()

        client.list_event_labels.assert_called_once()
        out = capsys.readouterr().out
        assert "label-1" in out
        assert "#8e24aa" in out
        assert "Design Work" in out

    def test_prints_message_when_no_labels(self, capsys, monkeypatch):
        client = MagicMock()
        client.list_event_labels.return_value = ([], None)
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "list_raw_labels"])

        calendar_cli.main()

        assert "No event labels found." in capsys.readouterr().out


class TestMainCreateRawLabel:
    def test_creates_label_with_name(self, capsys, monkeypatch):
        client = MagicMock()
        client.create_event_label.return_value = _raw_event_label()
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "calendar_cli.py",
                "create_raw_label",
                "background_color=#8e24aa",
                "name=Design Work",
            ],
        )

        calendar_cli.main()

        client.create_event_label.assert_called_once_with("#8e24aa", "Design Work")
        out = capsys.readouterr().out
        assert "label-1" in out
        assert "Design Work" in out

    def test_creates_label_without_name(self, monkeypatch):
        client = MagicMock()
        client.create_event_label.return_value = _raw_event_label(name=None)
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(
            sys, "argv", ["calendar_cli.py", "create_raw_label", "background_color=#8e24aa"]
        )

        calendar_cli.main()

        client.create_event_label.assert_called_once_with("#8e24aa", None)

    def test_requires_background_color(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(
            sys, "argv", ["calendar_cli.py", "create_raw_label", "name=Design Work"]
        )

        with pytest.raises(SystemExit):
            calendar_cli.main()

        client.create_event_label.assert_not_called()


class TestMainUpdateRawLabel:
    def test_updates_background_color(self, capsys, monkeypatch):
        client = MagicMock()
        client.update_event_label.return_value = _raw_event_label(background_color="#d50000")
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(
            sys,
            "argv",
            ["calendar_cli.py", "update_raw_label", "label-1", "background_color=#d50000"],
        )

        calendar_cli.main()

        client.update_event_label.assert_called_once_with(
            "label-1", background_color="#d50000", name=None
        )
        assert "#d50000" in capsys.readouterr().out

    def test_updates_name(self, monkeypatch):
        client = MagicMock()
        client.update_event_label.return_value = _raw_event_label(name="New name")
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(
            sys, "argv", ["calendar_cli.py", "update_raw_label", "label-1", "name=New name"]
        )

        calendar_cli.main()

        client.update_event_label.assert_called_once_with(
            "label-1", background_color=None, name="New name"
        )

    def test_requires_at_least_one_property(self, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "update_raw_label", "label-1"])

        with pytest.raises(SystemExit):
            calendar_cli.main()

        client.update_event_label.assert_not_called()


class TestMainDeleteRawLabel:
    def test_deletes_label(self, capsys, monkeypatch):
        client = MagicMock()
        client.delete_event_label.return_value = _raw_event_label()
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "delete_raw_label", "label-1"])

        calendar_cli.main()

        client.delete_event_label.assert_called_once_with("label-1")
        assert "label-1" in capsys.readouterr().out


class TestMainSyncLabels:
    def test_syncs_from_tracked_sheet(self, capsys, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        event_labels = _fake_event_labels(monkeypatch)
        event_labels.sync_labels.return_value = [_event_label(priority=1)]
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "sync_labels"])

        calendar_cli.main()

        event_labels.sync_labels.assert_called_once_with()
        out = capsys.readouterr().out
        assert "label-1" in out

    def test_prints_message_when_no_labels(self, capsys, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        event_labels = _fake_event_labels(monkeypatch)
        event_labels.sync_labels.return_value = []
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "sync_labels"])

        calendar_cli.main()

        assert "No event labels found." in capsys.readouterr().out


class TestMainCreateLabel:
    def test_creates_label_with_name(self, capsys, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        event_labels = _fake_event_labels(monkeypatch)
        event_labels.create_label.return_value = [_event_label()]
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "calendar_cli.py",
                "create_label",
                "background_color=#8e24aa",
                "name=Design Work",
            ],
        )

        calendar_cli.main()

        event_labels.create_label.assert_called_once_with(
            EventLabel(background_color="#8e24aa", name="Design Work", priority=None)
        )
        out = capsys.readouterr().out
        assert "label-1" in out
        assert "Design Work" in out

    def test_creates_label_without_name(self, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        event_labels = _fake_event_labels(monkeypatch)
        event_labels.create_label.return_value = [_event_label(name=None)]
        monkeypatch.setattr(
            sys, "argv", ["calendar_cli.py", "create_label", "background_color=#8e24aa"]
        )

        calendar_cli.main()

        event_labels.create_label.assert_called_once_with(
            EventLabel(background_color="#8e24aa", name=None, priority=None)
        )

    def test_creates_label_from_priority_alone(self, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        event_labels = _fake_event_labels(monkeypatch)
        event_labels.create_label.return_value = [
            _event_label(background_color="#fbd75b", priority=1)
        ]
        monkeypatch.setattr(
            sys,
            "argv",
            ["calendar_cli.py", "create_label", "name=Design Work", "priority=1"],
        )

        calendar_cli.main()

        event_labels.create_label.assert_called_once_with(
            EventLabel(background_color=None, name="Design Work", priority=1)
        )

    def test_no_properties_required_upfront(self, monkeypatch):
        # create_label doesn't enforce "background_color or priority" itself
        # -- EventLabels.create_label always resolves a color, defaulting
        # if neither is given.
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        event_labels = _fake_event_labels(monkeypatch)
        event_labels.create_label.return_value = [_event_label(background_color="#a4bdfc")]
        monkeypatch.setattr(
            sys, "argv", ["calendar_cli.py", "create_label", "name=Design Work"]
        )

        calendar_cli.main()

        event_labels.create_label.assert_called_once_with(
            EventLabel(background_color=None, name="Design Work", priority=None)
        )


class TestMainUpdateLabel:
    def test_updates_background_color(self, capsys, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        event_labels = _fake_event_labels(monkeypatch)
        event_labels.update_label.return_value = [_event_label(background_color="#d50000")]
        monkeypatch.setattr(
            sys,
            "argv",
            ["calendar_cli.py", "update_label", "label-1", "background_color=#d50000"],
        )

        calendar_cli.main()

        event_labels.update_label.assert_called_once_with(
            EventLabel(id="label-1", background_color="#d50000", name=None, priority=None)
        )
        assert "#d50000" in capsys.readouterr().out

    def test_updates_name(self, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        event_labels = _fake_event_labels(monkeypatch)
        event_labels.update_label.return_value = [_event_label(name="New name")]
        monkeypatch.setattr(
            sys, "argv", ["calendar_cli.py", "update_label", "label-1", "name=New name"]
        )

        calendar_cli.main()

        event_labels.update_label.assert_called_once_with(
            EventLabel(id="label-1", background_color=None, name="New name", priority=None)
        )

    def test_updates_priority(self, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        event_labels = _fake_event_labels(monkeypatch)
        event_labels.update_label.return_value = [
            _event_label(background_color="#7ae7bf", priority=3)
        ]
        monkeypatch.setattr(
            sys, "argv", ["calendar_cli.py", "update_label", "label-1", "priority=3"]
        )

        calendar_cli.main()

        event_labels.update_label.assert_called_once_with(
            EventLabel(id="label-1", background_color=None, name=None, priority=3)
        )

    def test_requires_at_least_one_property(self, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        event_labels = _fake_event_labels(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "update_label", "label-1"])

        with pytest.raises(SystemExit):
            calendar_cli.main()

        event_labels.update_label.assert_not_called()


class TestResolveNoteTimestamp:
    def test_resolves_around_given_now(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

        timestamp = calendar_cli.resolve_note_timestamp(1800, now=now)

        assert timestamp == datetime(2026, 1, 1, 11, 30, tzinfo=UTC)

    def test_zero_seconds_ago_is_now(self):
        now = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

        assert calendar_cli.resolve_note_timestamp(0, now=now) == now

    def test_defaults_to_current_time_when_now_not_given(self):
        before = datetime.now(UTC)

        timestamp = calendar_cli.resolve_note_timestamp(0)

        after = datetime.now(UTC)
        assert before <= timestamp <= after


class TestMainNote:
    def test_records_a_note_with_description(self, capsys, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        noted_time_sheet = _fake_noted_time_sheet(monkeypatch)
        fixed_timestamp = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        resolve = MagicMock(return_value=fixed_timestamp)
        monkeypatch.setattr(calendar_cli, "resolve_note_timestamp", resolve)
        monkeypatch.setattr(
            sys, "argv", ["calendar_cli.py", "note", "30m", "Started work"]
        )

        calendar_cli.main()

        resolve.assert_called_once_with(1800.0)
        noted_time_sheet.append.assert_called_once_with(
            NotedTime(timestamp=fixed_timestamp, description="Started work")
        )
        out = capsys.readouterr().out
        assert "2026-01-01 09:00:00+00:00" in out
        assert "Started work" in out

    def test_records_a_note_without_description(self, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        noted_time_sheet = _fake_noted_time_sheet(monkeypatch)
        fixed_timestamp = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        monkeypatch.setattr(
            calendar_cli, "resolve_note_timestamp", lambda seconds_ago: fixed_timestamp
        )
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "note", "0s"])

        calendar_cli.main()

        noted_time_sheet.append.assert_called_once_with(
            NotedTime(timestamp=fixed_timestamp, description=None)
        )

    def test_resolves_ago_relative_to_now_end_to_end(self, monkeypatch):
        # No mocking of resolve_note_timestamp here -- confirms the real
        # duration arithmetic, the same way TestMainList.
        # test_uses_default_window_when_omitted checks list's window
        # without pinning "now".
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        noted_time_sheet = _fake_noted_time_sheet(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "note", "30m"])

        before = datetime.now(UTC)
        calendar_cli.main()
        after = datetime.now(UTC)

        noted_time = noted_time_sheet.append.call_args.args[0]
        assert before - timedelta(minutes=30) <= noted_time.timestamp <= after - timedelta(minutes=30)

    def test_raises_on_an_unparseable_duration(self, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        noted_time_sheet = _fake_noted_time_sheet(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "note", "not-a-duration"])

        with pytest.raises(SystemExit):
            calendar_cli.main()

        noted_time_sheet.append.assert_not_called()


class TestMainGetNotes:
    def test_lists_notes(self, capsys, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        noted_time_sheet = _fake_noted_time_sheet(monkeypatch)
        noted_time_sheet.read.return_value = [
            NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, tzinfo=UTC), description="Started work")
        ]
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "get_notes"])

        calendar_cli.main()

        noted_time_sheet.read.assert_called_once_with()
        out = capsys.readouterr().out
        assert "2026-01-01T09:00:00+00:00" in out
        assert "Started work" in out

    def test_prints_message_when_no_notes(self, capsys, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        noted_time_sheet = _fake_noted_time_sheet(monkeypatch)
        noted_time_sheet.read.return_value = []
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "get_notes"])

        calendar_cli.main()

        assert "No notes found." in capsys.readouterr().out


class TestMainClearNotes:
    def test_clears_notes_and_prints_the_cleared_ones(self, capsys, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        noted_time_sheet = _fake_noted_time_sheet(monkeypatch)
        noted_time_sheet.clear.return_value = [
            NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, tzinfo=UTC), description="Started work")
        ]
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "clear_notes"])

        calendar_cli.main()

        noted_time_sheet.clear.assert_called_once_with()
        out = capsys.readouterr().out
        assert "2026-01-01T09:00:00+00:00" in out
        assert "Started work" in out

    def test_prints_message_when_nothing_to_clear(self, capsys, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        noted_time_sheet = _fake_noted_time_sheet(monkeypatch)
        noted_time_sheet.clear.return_value = []
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "clear_notes"])

        calendar_cli.main()

        assert "No notes to clear." in capsys.readouterr().out
