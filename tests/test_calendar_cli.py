import argparse
import dataclasses
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

import calendar_cli
from tests.test_event_changes import FakeCalendar
from utilities.event_changes import EventChanges
from calendar_clients.google_calendar import Event
from calendar_clients.google_calendar import EventLabel as RawEventLabel
from utilities.action_calendar import ActionCalendar
from utilities.action_groups import GroupTree
from utilities.actions import ACTION_STATUSES, Action, ActionChanges, ActionList, ActionTree, ListedAction
from utilities.note_compaction import CompactionError
from utilities.noted_time_sheet import NotedTime, SheetNote

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


def _action_list(*actions: ListedAction) -> ActionList:
    return ActionList(actions=list(actions), label_slots_used=len(actions))


def _action_changes(*changed: ListedAction) -> ActionChanges:
    return ActionChanges(changed=list(changed), label_slots_used=len(changed))


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


class TestParseActionKeyValue:
    def test_parses_name(self):
        assert calendar_cli._parse_action_key_value("name=Play guitar") == ("name", "Play guitar")

    def test_parses_a_status(self):
        assert calendar_cli._parse_action_key_value("status=archived") == ("status", "archived")
        with pytest.raises(argparse.ArgumentTypeError, match="expected one of proposed, active"):
            calendar_cli._parse_action_key_value("status=inactive")

    def test_raises_on_read_only_attributes(self):
        for key in ("id", "label_id"):
            with pytest.raises(argparse.ArgumentTypeError):
                calendar_cli._parse_action_key_value(f"{key}=x")


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
    def test_covers_every_event_attribute_the_api_accepts_except_id(self):
        # id would repoint the patch at a different event; recurring_event_id
        # is assigned by Google and never sent to the API, so setting it here
        # would silently have no effect. action_priority belongs to the
        # event's actions, not the event, and is never sent to the API
        # either. A series' recurrence/time_zone are edited
        # through the recurrence tools (utilities/recurrences.py), and
        # original_start, like recurring_event_id, is Google's. cleared
        # names fields to remove rather than a value to set; it's set
        # through the MCP tools' clear_fields.
        event_attributes = {f.name for f in dataclasses.fields(Event)} - {
            "id",
            "recurring_event_id",
            "action_priority",
            "actions_from_label",
            "recurrence",
            "time_zone",
            "original_start",
            "cleared",
        }

        assert set(calendar_cli._UPDATABLE_ATTRIBUTE_PARSERS) == event_attributes


class TestActionAttributeParsers:
    def test_covers_every_action_attribute_except_the_read_only_ones(self):
        action_attributes = {f.name for f in dataclasses.fields(Action)} - {"id", "label_id"}

        assert set(calendar_cli._ACTION_ATTRIBUTE_PARSERS) == action_attributes


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
        assert "priority" not in details

    def test_includes_optional_fields_when_set(self):
        event = _event(
            description="Details",
            location="Room",
            priority=1,
        )

        details = calendar_cli._format_event_details(event)

        assert "description: Details" in details
        assert "location: Room" in details
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

    def test_action_ids_also_set_the_label_they_imply(self, monkeypatch):
        client = MagicMock()
        client.update_event.side_effect = lambda event: event
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        actions = MagicMock()
        actions.tree.return_value = ActionTree(
            [Action(id="g1", name="Cook", status="active", label_id="label-1")], GroupTree([])
        )
        monkeypatch.setattr(calendar_cli, "build_actions", lambda: actions)
        monkeypatch.setattr(
            sys, "argv", ["calendar_cli.py", "update_properties", "abc123", "action_ids=g1, g2"]
        )

        calendar_cli.main()

        sent_event = client.update_event.call_args[0][0]
        assert sent_event.action_ids == ["g1", "g2"]
        assert sent_event.event_label_id == "label-1"


def _fake_changes(monkeypatch, events=()):
    """`update`/`create` checked and written against a fake calendar
    (tests/test_event_changes.py's)."""
    calendar = FakeCalendar(list(events))
    monkeypatch.setattr(calendar_cli, "_build_event_changes", lambda client: EventChanges(calendar))
    monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
    return calendar


class TestBuildEventChanges:
    def test_writes_through_an_action_calendar(self, monkeypatch):
        client = MagicMock()
        actions = MagicMock()
        monkeypatch.setattr(calendar_cli, "build_actions", lambda: actions)

        changes = calendar_cli._build_event_changes(client)

        assert isinstance(changes._client, ActionCalendar)
        assert changes._client._client is client
        assert changes._client._actions is actions


class TestMainUpdate:
    def test_moves_the_event(self, capsys, monkeypatch):
        calendar = _fake_changes(monkeypatch, [_event(id="abc123", summary="Focus")])
        monkeypatch.setattr(
            sys,
            "argv",
            ["calendar_cli.py", "update", "abc123", "start=2026-01-01T11:00:00Z", "end=2026-01-01T11:30:00Z", "priority=1"],
        )

        calendar_cli.main()

        event = calendar.events["abc123"]
        assert (event.start, event.end, event.priority) == (
            datetime(2026, 1, 1, 11, 0, tzinfo=UTC), datetime(2026, 1, 1, 11, 30, tzinfo=UTC), 1,
        )
        assert "summary: Focus" in capsys.readouterr().out

    def test_keeps_the_end_without_one(self, monkeypatch):
        calendar = _fake_changes(monkeypatch, [_event(id="abc123")])
        before = calendar.events["abc123"].end
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "update", "abc123", "start=2026-01-01T08:00:00Z"])

        calendar_cli.main()

        assert calendar.events["abc123"].end == before

    def test_an_overlap_is_refused(self, monkeypatch):
        calendar = _fake_changes(monkeypatch, [
            _event(id="abc123"),
            _event(id="def456", start=datetime(2026, 1, 1, 11, 0, tzinfo=UTC), end=datetime(2026, 1, 1, 12, 0, tzinfo=UTC)),
        ])
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "update", "abc123", "end=2026-01-01T11:30:00Z"])

        with pytest.raises(SystemExit, match="Nothing was changed"):
            calendar_cli.main()

        assert calendar.written == []

    def test_requires_start_or_end(self, monkeypatch):
        calendar = _fake_changes(monkeypatch, [_event(id="abc123")])
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "update", "abc123", "priority=1"])

        with pytest.raises(SystemExit):
            calendar_cli.main()

        assert calendar.written == []


class TestMainCreate:
    def test_creates_the_event(self, capsys, monkeypatch):
        calendar = _fake_changes(monkeypatch)
        monkeypatch.setattr(
            sys,
            "argv",
            ["calendar_cli.py", "create", "summary=New", "start=2026-01-01T09:00:00Z", "end=2026-01-01T09:30:00Z", "priority=1"],
        )

        calendar_cli.main()

        (created,) = calendar.events.values()
        assert (created.summary, created.priority) == ("New", 1)
        assert "summary: New" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "properties",
        [
            ["start=2026-01-01T09:00:00Z", "end=2026-01-01T09:30:00Z"],
            ["summary=New", "end=2026-01-01T09:30:00Z"],
            ["summary=New", "start=2026-01-01T09:00:00Z"],
        ],
    )
    def test_requires_summary_start_and_end(self, monkeypatch, properties):
        calendar = _fake_changes(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "create", *properties])

        with pytest.raises(SystemExit):
            calendar_cli.main()

        assert calendar.written == []


class TestMainDelete:
    def test_deletes_event(self, capsys, monkeypatch):
        client = MagicMock()
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: client)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "delete", "abc123"])

        calendar_cli.main()

        client.delete_event.assert_called_once_with("abc123")
        assert "abc123" in capsys.readouterr().out


def _fake_actions(monkeypatch) -> MagicMock:
    actions = MagicMock()
    monkeypatch.setattr(calendar_cli, "build_actions", lambda: actions)
    return actions


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


class TestMainActions:
    def _run(self, monkeypatch, *argv):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", MagicMock)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", *argv])
        calendar_cli.main()

    def test_list_actions_prints_each_action_and_the_label_count(self, capsys, monkeypatch):
        actions = _fake_actions(monkeypatch)
        actions.get_actions.return_value = _action_list(
            ListedAction(id="a1", name="Play guitar", status="active", path="Creative › Play guitar"),
            ListedAction(id="a2", name="Juggle", status="proposed", path="Juggle"),
        )

        self._run(monkeypatch, "list_actions", "--all")

        actions.get_actions.assert_called_once_with(ACTION_STATUSES)
        out = capsys.readouterr().out
        assert "a1\tactive\tCreative › Play guitar" in out
        assert "(2 of 200 event labels in use)" in out

    def test_list_actions_picks_statuses(self, monkeypatch):
        actions = _fake_actions(monkeypatch)
        actions.get_actions.return_value = _action_list()

        self._run(monkeypatch, "list_actions", "--status", "archived", "--status", "deleted")

        actions.get_actions.assert_called_once_with(["archived", "deleted"])

    def test_list_actions_says_when_there_are_none(self, capsys, monkeypatch):
        actions = _fake_actions(monkeypatch)
        actions.get_actions.return_value = _action_list()

        self._run(monkeypatch, "list_actions")

        actions.get_actions.assert_called_once_with(None)
        assert "No actions found." in capsys.readouterr().out

    def test_create_action(self, capsys, monkeypatch):
        actions = _fake_actions(monkeypatch)
        actions.create_action.return_value = _action_changes(
            ListedAction(id="a2", name="Cook", status="active", path="Food › Cook")
        )

        self._run(monkeypatch, "create_action", "name=Cook", "group_id=g1", "priority=2")

        actions.create_action.assert_called_once_with(Action(name="Cook", group_id="g1", priority=2))
        assert "a2\tactive\tFood › Cook" in capsys.readouterr().out

    def test_update_action_sets_and_clears(self, monkeypatch):
        actions = _fake_actions(monkeypatch)
        actions.update_action.return_value = _action_changes()

        self._run(monkeypatch, "update_action", "a1", "status=archived", "--clear", "note")

        actions.update_action.assert_called_once_with(Action(id="a1", status="archived"), ["note"])

    def test_update_action_needs_something_to_do(self, monkeypatch):
        _fake_actions(monkeypatch)

        with pytest.raises(SystemExit):
            self._run(monkeypatch, "update_action", "a1")

    def test_errors_exit_with_the_message(self, monkeypatch):
        actions = _fake_actions(monkeypatch)
        actions.create_action.side_effect = ValueError("action 'x' needs a name")

        with pytest.raises(SystemExit, match="error: action 'x' needs a name"):
            self._run(monkeypatch, "create_action", "priority=1")


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
        noted_time_sheet.read_with_rows.return_value = [
            SheetNote(row=3, note=NotedTime(timestamp=datetime(2026, 1, 1, 10, 0, tzinfo=UTC))),
            SheetNote(
                row=2,
                note=NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, tzinfo=UTC), description="Started work"),
            ),
        ]
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "get_notes"])

        calendar_cli.main()

        noted_time_sheet.read_with_rows.assert_called_once_with()
        lines = capsys.readouterr().out.splitlines()
        assert lines == [
            "2026-01-01T09:00:00+00:00#2\t2026-01-01T09:00:00+00:00\tStarted work",
            "2026-01-01T10:00:00+00:00#3\t2026-01-01T10:00:00+00:00\t",
        ]

    def test_prints_message_when_no_notes(self, capsys, monkeypatch):
        monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
        noted_time_sheet = _fake_noted_time_sheet(monkeypatch)
        noted_time_sheet.read_with_rows.return_value = []
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "get_notes"])

        calendar_cli.main()

        assert "No notes found." in capsys.readouterr().out


_NOTE_ID = "2026-01-01T09:00:00+00:00#5"


def _fake_note_edits(monkeypatch):
    """Stubs edit_note/delete_note as the CLI imports them, recording calls."""
    monkeypatch.setattr(calendar_cli, "build_calendar_client", lambda: MagicMock())
    monkeypatch.setattr(calendar_cli, "build_noted_time_sheet", lambda: "notes")
    monkeypatch.setattr(calendar_cli, "build_compaction_journal", lambda: "journal")
    edit, delete = MagicMock(), MagicMock()
    monkeypatch.setattr(calendar_cli, "edit_note", edit)
    monkeypatch.setattr(calendar_cli, "delete_note", delete)
    return edit, delete


class TestMainEditNote:
    def test_edits_the_description(self, capsys, monkeypatch):
        edit, _ = _fake_note_edits(monkeypatch)
        edit.return_value = SheetNote(
            row=5, note=NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, tzinfo=UTC), description="Standup")
        )
        monkeypatch.setattr(
            sys, "argv", ["calendar_cli.py", "edit_note", _NOTE_ID, "--description", "Standup"]
        )

        calendar_cli.main()

        edit.assert_called_once_with("notes", "journal", _NOTE_ID, timestamp=None, description="Standup")
        out = capsys.readouterr().out
        assert f"id: {_NOTE_ID}" in out
        assert "Standup" in out

    def test_takes_a_new_time_as_a_duration_before_now(self, monkeypatch):
        edit, _ = _fake_note_edits(monkeypatch)
        edit.return_value = SheetNote(row=5, note=NotedTime(timestamp=datetime(2026, 1, 1, tzinfo=UTC)))
        fixed = datetime(2026, 1, 1, 8, 30, tzinfo=UTC)
        resolve = MagicMock(return_value=fixed)
        monkeypatch.setattr(calendar_cli, "resolve_note_timestamp", resolve)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "edit_note", _NOTE_ID, "--ago", "30m"])

        calendar_cli.main()

        resolve.assert_called_once_with(1800.0)
        assert edit.call_args.kwargs == {"timestamp": fixed, "description": None}

    def test_takes_a_new_time_as_an_iso_time(self, monkeypatch):
        edit, _ = _fake_note_edits(monkeypatch)
        edit.return_value = SheetNote(row=5, note=NotedTime(timestamp=datetime(2026, 1, 1, tzinfo=UTC)))
        monkeypatch.setattr(
            sys, "argv", ["calendar_cli.py", "edit_note", _NOTE_ID, "--at", "2026-01-01T09:30:00+00:00"]
        )

        calendar_cli.main()

        assert edit.call_args.kwargs["timestamp"] == datetime(2026, 1, 1, 9, 30, tzinfo=UTC)

    def test_an_empty_description_is_passed_through_to_clear_it(self, monkeypatch):
        edit, _ = _fake_note_edits(monkeypatch)
        edit.return_value = SheetNote(row=5, note=NotedTime(timestamp=datetime(2026, 1, 1, tzinfo=UTC)))
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "edit_note", _NOTE_ID, "--description", ""])

        calendar_cli.main()

        assert edit.call_args.kwargs["description"] == ""

    @pytest.mark.parametrize(
        "extra",
        [[], ["--ago", "1h", "--at", "2026-01-01T09:30:00+00:00"], ["--at", "not-a-time"]],
        ids=["nothing to change", "both times", "bad time"],
    )
    def test_rejects_bad_arguments(self, monkeypatch, extra):
        edit, _ = _fake_note_edits(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "edit_note", _NOTE_ID, *extra])

        with pytest.raises(SystemExit):
            calendar_cli.main()

        edit.assert_not_called()

    def test_reports_a_refusal_and_exits_nonzero(self, monkeypatch):
        edit, _ = _fake_note_edits(monkeypatch)
        edit.side_effect = CompactionError("already compacted")
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "edit_note", _NOTE_ID, "--description", "x"])

        with pytest.raises(SystemExit, match="error: already compacted"):
            calendar_cli.main()


class TestMainDeleteNote:
    def test_deletes_and_prints_what_it_was(self, capsys, monkeypatch):
        _, delete = _fake_note_edits(monkeypatch)
        delete.return_value = NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, tzinfo=UTC), description="oops")
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "delete_note", _NOTE_ID])

        calendar_cli.main()

        delete.assert_called_once_with("notes", "journal", _NOTE_ID)
        out = capsys.readouterr().out
        assert "Deleted:" in out and "oops" in out

    def test_reports_a_refusal_and_exits_nonzero(self, monkeypatch):
        _, delete = _fake_note_edits(monkeypatch)
        delete.side_effect = CompactionError("no longer holds the note")
        monkeypatch.setattr(sys, "argv", ["calendar_cli.py", "delete_note", _NOTE_ID])

        with pytest.raises(SystemExit, match="no longer holds the note"):
            calendar_cli.main()
