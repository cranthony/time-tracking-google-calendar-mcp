import contextlib
import dataclasses
import logging
import threading
import typing
from datetime import date, datetime, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import TypeAdapter
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import server
from server import EventCancel, EventShift, EventUpdate
from utilities.note_compaction import CompactionCreate, CompactionUpdate
from tests.test_event_changes import FakeCalendar
from utilities.event_changes import EventChanges
from calendar_clients import google_calendar, google_sheets
from calendar_clients.google_calendar import (
    CLEARABLE_EVENT_FIELDS,
    Event,
    TimeZoneNotSetError,
)
from calendar_clients.write_lock import WRITE_LOCK
from server import PublicEvent
from utilities.action_calendar import ActionCalendar
from utilities.action_groups import GroupTree
from utilities.actions import ActionTree
from utilities.compaction_additions import NewAction, NewLocation, NewPerson
from utilities.facts import Facts
from utilities.judgments import Judgment, JudgmentsDue
from utilities.note_compaction import CompactionError, EventDecision, Problem
from utilities.noted_time_sheet import NotedTime, NoteWithId, SheetNote
from utilities.recurrences import Repeat
from utilities.traits import SEED_TRAITS, Trait
from utilities.action_groups import ActionGroup
from utilities.actions import Action
from utilities.locations import Location
from utilities.people import CancelledEvent, Circle, ListedPerson, Person

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _no_op_memory_tracking(monkeypatch):
    """Every tool body is wrapped in `track(...)` (see
    utilities/memory_diagnostics.py, which has its own dedicated tests) --
    replaced here with a no-op so these tests exercise each tool's own
    logic without the overhead of real RSS/tracemalloc/objgraph work on
    every call."""
    monkeypatch.setattr(server, "track", lambda label: contextlib.nullcontext())


def _fake_client(monkeypatch) -> MagicMock:
    client = MagicMock()
    monkeypatch.setattr(server, "get_calendar_client", lambda: client)
    return client



@pytest.fixture(autouse=True)
def _no_actions(monkeypatch):
    """Every event tool fills in action_names and effective_priority from
    the Actions tab -- faked here as having no actions at all, so tests
    that don't care about actions never touch a real sheet. Tests that do
    care use _fake_actions. Seeds the cache rather than replacing
    get_action_store, so its own caching tests still exercise the real
    one."""
    actions = MagicMock()
    actions.tree.return_value = ActionTree([], GroupTree([]))
    monkeypatch.setattr(server, "_actions", actions)


@pytest.fixture(autouse=True)
def _no_people_or_locations(monkeypatch):
    """Event tools read people and locations too (for facts): faked here as
    there being none but the user, the same way as `_no_actions`."""
    people, locations = MagicMock(), MagicMock()
    people.all.return_value = [Person(id="self", name="Me")]
    locations.all.return_value = []
    monkeypatch.setattr(server, "_people", people)
    monkeypatch.setattr(server, "_locations", locations)


def _fake_actions(monkeypatch, *actions: Action) -> MagicMock:
    store = MagicMock()
    store.tree.return_value = ActionTree(list(actions), GroupTree([]))
    monkeypatch.setattr(server, "get_action_store", lambda: store)
    return store


def _action(action_id: str = "g1", **overrides) -> Action:
    fields = {"id": action_id, "name": "Focus", "status": "active", "label_id": f"label-{action_id}"}
    fields.update(overrides)
    return Action(**fields)


def _fake_noted_time_sheet(monkeypatch) -> MagicMock:
    noted_time_sheet = MagicMock()
    monkeypatch.setattr(server, "get_noted_time_sheet", lambda: noted_time_sheet)
    return noted_time_sheet


def _tracked_labels(monkeypatch) -> list[str]:
    """Replaces the `_no_op_memory_tracking` fixture's no-op with one that
    also records each label `track` was called with, for tests that need
    to assert which one a tool used."""
    labels: list[str] = []
    monkeypatch.setattr(
        server, "track", lambda label: labels.append(label) or contextlib.nullcontext()
    )
    return labels


def _event(**overrides) -> Event:
    fields = {
        "summary": "Focus block",
        "start": datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    }
    fields.update(overrides)
    return Event(**fields)


def _public_event(**overrides) -> PublicEvent:
    fields = {
        "summary": "Focus block",
        "start": datetime(2026, 1, 1, 9, 0, tzinfo=UTC),
        "end": datetime(2026, 1, 1, 10, 0, tzinfo=UTC),
    }
    fields.update(overrides)
    return PublicEvent(**fields)


class TestPublicEvent:
    def test_actions_inferred_from_a_label_are_marked_and_not_written_back(self):
        public = PublicEvent.from_event(Event(id="e1", action_ids=["g1"], actions_from_label=True))

        assert public.actions_from_label
        assert public.to_event().action_ids is None
        public.actions_from_label = False  # Confirmed: stored.
        assert public.to_event().action_ids == ["g1"]

    def test_hides_internal_fields_from_its_fields(self):
        field_names = {f.name for f in dataclasses.fields(PublicEvent)}

        assert field_names.isdisjoint(server.INTERNAL_EVENT_FIELDS)
        # is_cancelled has no Event equivalent -- it's derived from the
        # hidden status field, not a field PublicEvent passes through.
        # Likewise effective_priority and action_names, derived from the
        # event's actions. And compacted, from compacted_until.
        event_derived_fields = field_names - {
            "is_cancelled",
            "effective_priority",
            "action_names",
            "compacted",
        }
        assert event_derived_fields == {
            f.name for f in dataclasses.fields(Event)
        } - server.INTERNAL_EVENT_FIELDS

    def test_from_event_exposes_is_end_of_day_sleep_and_recurring_event_id(self):
        event = _event(id="abc123", is_end_of_day_sleep=True, recurring_event_id="series-1")

        public_event = PublicEvent.from_event(event)

        assert public_event.is_end_of_day_sleep is True
        assert public_event.recurring_event_id == "series-1"

    def test_to_event_ignores_is_end_of_day_sleep_and_recurring_event_id(self):
        public_event = _public_event(
            id="abc123", priority=1, is_end_of_day_sleep=True, recurring_event_id="series-1"
        )

        event = public_event.to_event()

        assert event.id == "abc123"
        assert event.priority == 1
        assert event.is_end_of_day_sleep is None
        assert event.recurring_event_id is None

    def test_from_event_carries_event_label_id(self):
        event = _event(id="abc123", event_label_id="label-1")

        public_event = PublicEvent.from_event(event)

        assert public_event.event_label_id == "label-1"

    def test_from_event_effective_priority_defaults_to_the_events_own(self):
        event = _event(id="abc123", priority=1)

        public_event = PublicEvent.from_event(event)

        assert public_event.effective_priority == 1

    def test_from_event_takes_effective_priority_from_the_events_actions(self):
        event = _event(id="abc123", action_ids=["g1"], action_priority=0)

        public_event = PublicEvent.from_event(event)

        assert public_event.priority is None
        assert public_event.effective_priority == 0

    def test_to_event_ignores_effective_priority(self):
        public_event = _public_event(id="abc123", effective_priority=0)

        event = public_event.to_event()

        assert event.priority is None

    def test_to_event_ignores_event_label_id_since_actions_decide_it(self):
        public_event = _public_event(id="abc123", event_label_id="label-1")

        event = public_event.to_event()

        assert event.event_label_id is None

    def test_to_event_carries_action_ids_but_not_action_names(self):
        public_event = _public_event(id="abc123", action_ids=["g1", "g2"], action_names=["A", "B"])

        event = public_event.to_event()

        assert event.action_ids == ["g1", "g2"]

    def test_from_event_names_the_events_actions(self):
        tree = ActionTree([_action("g1", name="Cooking"), _action("g2", name="Hosting")], GroupTree([]))
        event = _event(id="abc123", action_ids=["g2", "g1", "gone"])

        public_event = PublicEvent.from_event(event, tree)

        assert public_event.action_ids == ["g2", "g1", "gone"]
        assert public_event.action_names == ["Hosting", "Cooking", "(unknown action gone)"]

    def test_from_event_exposes_cancellation_alongside_its_other_fields(self):
        event = _event(id="abc123", status="cancelled", priority=1, location="Room")

        public_event = PublicEvent.from_event(event)

        assert public_event.id == "abc123"
        assert public_event.is_cancelled is True
        assert public_event.summary == event.summary
        assert public_event.start == event.start
        assert public_event.end == event.end
        assert public_event.priority == 1
        assert public_event.location == "Room"

    def test_from_event_is_cancelled_false_for_a_confirmed_event(self):
        event = _event(id="abc123", status="confirmed")

        public_event = PublicEvent.from_event(event)

        assert public_event.is_cancelled is False
        assert public_event.summary == event.summary

    def test_to_event_maps_is_cancelled_true_to_cancelled_status(self):
        public_event = _public_event(id="abc123", is_cancelled=True)

        event = public_event.to_event()

        assert event.status == "cancelled"

    def test_to_event_setting_is_cancelled_false_has_no_effect(self):
        public_event = _public_event(id="abc123", is_cancelled=False)

        event = public_event.to_event()

        assert event.status is None


class TestListEvents:
    def test_delegates_to_calendar_client(self, monkeypatch):
        client = _fake_client(monkeypatch)
        events = [_event(id="abc123", is_end_of_day_sleep=True)]
        client.list_events.return_value = events
        min_time = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
        max_time = datetime(2026, 1, 2, 0, 0, tzinfo=UTC)

        result = server.list_events(min_time, max_time)

        assert result == [PublicEvent.from_event(events[0])]
        client.list_events.assert_called_once_with(min_time, max_time)

    def test_result_carries_is_end_of_day_sleep_and_recurring_event_id(self, monkeypatch):
        client = _fake_client(monkeypatch)
        client.list_events.return_value = [
            _event(id="abc123", is_end_of_day_sleep=True, recurring_event_id="series-1")
        ]

        result = server.list_events(
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC), datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        )

        assert result[0].is_end_of_day_sleep is True
        assert result[0].recurring_event_id == "series-1"

    def test_omits_cancelled_events(self, monkeypatch):
        client = _fake_client(monkeypatch)
        client.list_events.return_value = [
            _event(id="abc123", status="confirmed"),
            _event(id="def456", status="cancelled"),
        ]

        result = server.list_events(
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC), datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        )

        assert [event.id for event in result] == ["abc123"]

    def test_fills_in_actions_and_effective_priority_from_the_event_label(self, monkeypatch):
        # An event never given actions has only a label: it's read as
        # doing the action that holds it.
        client = _fake_client(monkeypatch)
        _fake_actions(monkeypatch, _action("g1", label_id="label-1", priority=0))
        client.list_events.return_value = [_event(id="abc123", event_label_id="label-1")]

        result = server.list_events(
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC), datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        )

        assert result[0].action_ids == ["g1"]
        assert result[0].action_names == ["Focus"]
        assert result[0].effective_priority == 0
        # The event's own priority stays unset, so sending it back to
        # update_event doesn't copy the action's onto it.
        assert result[0].priority is None

    def test_events_own_priority_wins_over_its_actions(self, monkeypatch):
        client = _fake_client(monkeypatch)
        _fake_actions(monkeypatch, _action("g1", priority=0))
        client.list_events.return_value = [_event(id="abc123", action_ids=["g1"], priority=3)]

        result = server.list_events(
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC), datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        )

        assert result[0].effective_priority == 3


class TestGetEvent:
    def test_delegates_to_calendar_client(self, monkeypatch):
        client = _fake_client(monkeypatch)
        event = _event(id="abc123", is_end_of_day_sleep=True)
        client.get_event.return_value = event

        result = server.get_event("abc123")

        assert result == PublicEvent.from_event(event)
        assert result.is_end_of_day_sleep is True
        client.get_event.assert_called_once_with("abc123")

    def test_raises_tool_error_for_cancelled_event(self, monkeypatch):
        client = _fake_client(monkeypatch)
        client.get_event.return_value = _event(id="abc123", status="cancelled")

        with pytest.raises(ToolError):
            server.get_event("abc123")

    def test_fills_in_effective_priority_from_the_events_action(self, monkeypatch):
        client = _fake_client(monkeypatch)
        _fake_actions(monkeypatch, _action("g1", priority=0))
        client.get_event.return_value = _event(id="abc123", action_ids=["g1"])

        result = server.get_event("abc123")

        assert result.effective_priority == 0
        assert result.priority is None


def _fake_changes(monkeypatch, events=None):
    """The event tools' batches checked and written against a fake calendar
    (tests/test_event_changes.py's), with a fake Cancellations."""
    calendar = FakeCalendar(events if events is not None else [_event(id="abc123"), _event(
        id="def456", start=datetime(2026, 1, 1, 10, 0, tzinfo=UTC), end=datetime(2026, 1, 1, 11, 0, tzinfo=UTC),
    )])
    cancellations = MagicMock()
    monkeypatch.setattr(server, "get_cancellation_store", lambda: cancellations)
    monkeypatch.setattr(server, "get_event_changes", lambda: EventChanges(calendar, cancellations))
    return calendar, cancellations


def _at(hour, minute=0):
    return datetime(2026, 1, 1, hour, minute, tzinfo=UTC)


class TestUpdateEvent:
    def test_updates_several_events_at_once(self, monkeypatch):
        calendar, _ = _fake_changes(monkeypatch)

        result = server.update_event(updates=[
            EventUpdate(event=PublicEvent(id="abc123", start=_at(8), end=_at(9))),
            EventUpdate(event=PublicEvent(id="def456", summary="Renamed")),
        ])

        assert [e.id for e in result.events] == ["abc123", "def456"]
        assert calendar.events["abc123"].start == _at(8)
        assert calendar.events["def456"].summary == "Renamed"
        assert result.dry_run is False

    def test_an_overlap_is_refused_and_logged_changing_nothing(self, monkeypatch, caplog):
        calendar, _ = _fake_changes(monkeypatch)

        with caplog.at_level(logging.WARNING, logger="server"), pytest.raises(ToolError, match="Nothing was changed"):
            server.update_event(updates=[EventUpdate(event=PublicEvent(id="abc123", start=_at(9), end=_at(10, 30)))])

        assert calendar.written == []
        assert "tool=update_event categories=overlap" in caplog.text

    def test_moves_cancels_creates_and_shifts_together(self, monkeypatch):
        calendar, cancellations = _fake_changes(monkeypatch)

        result = server.update_event(
            updates=[EventUpdate(event=PublicEvent(id="abc123", start=_at(9), end=_at(10, 30)))],
            cancels=[EventCancel(event_id="def456", counts_against_follow_through=True)],
            creates=[PublicEvent(summary="Walk", start=_at(10, 30), end=_at(11))],
        )

        assert calendar.events["def456"].status == "cancelled"
        cancellations.record.assert_called_once()
        assert any(e.summary == "Walk" for e in calendar.events.values())
        assert "Focus block · ⇢30m" not in (result.timeline or "")
        assert "✕" in result.timeline

    def test_a_shift_moves_its_events(self, monkeypatch):
        calendar, _ = _fake_changes(monkeypatch)

        server.update_event(shifts=[EventShift(event_ids=["abc123", "def456"], minutes=60)])

        assert [calendar.events[i].start for i in ("abc123", "def456")] == [_at(10), _at(11)]

    def test_a_dry_run_changes_nothing(self, monkeypatch):
        calendar, _ = _fake_changes(monkeypatch)

        result = server.update_event(
            updates=[EventUpdate(event=PublicEvent(id="abc123", start=_at(8), end=_at(9)))], dry_run=True
        )

        assert result.dry_run is True
        assert calendar.written == []
        assert "Focus block · ⇠1h00m" in result.timeline

    def test_an_update_cant_cancel(self, monkeypatch):
        calendar, _ = _fake_changes(monkeypatch)

        with pytest.raises(ToolError, match="put it in cancels"):
            server.update_event(updates=[EventUpdate(event=PublicEvent(id="abc123", is_cancelled=True))])

        assert calendar.written == []

    def test_history_is_changed_only_when_allowed(self, monkeypatch):
        calendar, _ = _fake_changes(monkeypatch, [_event(id="abc123", compacted_until=_at(10))])

        with pytest.raises(ToolError, match="is history"):
            server.update_event(updates=[EventUpdate(event=PublicEvent(id="abc123", summary="Renamed"))])
        server.update_event(
            updates=[EventUpdate(event=PublicEvent(id="abc123", summary="Renamed"))], allow_compacted_changes=True
        )

        assert calendar.events["abc123"].summary == "Renamed"

    def test_clears_the_fields_it_names(self, monkeypatch):
        calendar, _ = _fake_changes(monkeypatch)
        calendar.events["abc123"].priority = 2

        server.update_event(updates=[EventUpdate(event=PublicEvent(id="abc123", summary="Renamed"), clear_fields=["priority"])])

        (_, patch) = calendar.written[0]
        assert patch.summary == "Renamed" and patch.cleared == {"priority"}

    def test_refuses_to_both_set_and_clear_a_field(self, monkeypatch):
        _fake_changes(monkeypatch)

        with pytest.raises(ToolError, match=r"Can't both set and clear \['priority'\]"):
            server.update_event(updates=[EventUpdate(event=PublicEvent(id="abc123", priority=1), clear_fields=["priority"])])

    def test_clearable_fields_match_the_events(self):
        assert set(typing.get_args(server.EventField)) == CLEARABLE_EVENT_FIELDS

    def test_refuses_action_ids_that_arent_actions(self, monkeypatch):
        calendar, _ = _fake_changes(monkeypatch)
        _fake_actions(monkeypatch, _action("g7k2qp", name="Cooking"))

        with pytest.raises(ToolError, match=r"no action with the id or name 'g7k2qq'; did you mean g7k2qp \(Cooking\)"):
            server.update_event(updates=[EventUpdate(event=PublicEvent(id="abc123", action_ids=["g7k2qq"]))])

        assert calendar.written == []

    def _fake_people_and_places(self, monkeypatch):
        people, locations = MagicMock(), MagicMock()
        people.all.return_value = [Person(id="self", name="Me"), Person(id="sam", name="Sam")]
        locations.all.return_value = [Location(id="home", name="Home")]
        monkeypatch.setattr(server, "get_people_store", lambda: people)
        monkeypatch.setattr(server, "get_location_store", lambda: locations)

    def test_sets_facts_normalized(self, monkeypatch):
        calendar, _ = _fake_changes(monkeypatch)
        self._fake_people_and_places(monkeypatch)

        server.update_event(updates=[EventUpdate(
            event=PublicEvent(id="abc123", facts=Facts(location_id="home", with_ids=[" sam"], notes={"sam": " glad  "}))
        )])

        assert calendar.events["abc123"].facts == Facts(location_id="home", with_ids=["sam"], notes={"sam": "glad"})

    @pytest.mark.parametrize(
        "facts, message",
        [
            (Facts(with_ids=["self"]), r'Its facts "with_ids" never names "self"'),
            (Facts(for_ids=["p9"]), r"Its facts name \['p9'\], who aren't people"),
            (Facts(location_id="nowhere"), r"Its facts' location 'nowhere' isn't a location"),
        ],
    )
    def test_refuses_facts_that_arent_well_formed(self, monkeypatch, facts, message):
        calendar, _ = _fake_changes(monkeypatch)
        self._fake_people_and_places(monkeypatch)

        with pytest.raises(ToolError, match=message):
            server.update_event(updates=[EventUpdate(event=PublicEvent(id="abc123", facts=facts))])

        assert calendar.written == []


class TestCreateEvent:
    def test_creates_several_at_once(self, monkeypatch):
        calendar, _ = _fake_changes(monkeypatch)

        result = server.create_event([
            PublicEvent(summary="Walk", start=_at(11), end=_at(11, 30)),
            PublicEvent(summary="Read", start=_at(11, 30), end=_at(12)),
        ])

        assert [e.summary for e in result.events] == ["Walk", "Read"]
        assert len(calendar.events) == 4

    def test_one_that_overlaps_is_refused(self, monkeypatch):
        calendar, _ = _fake_changes(monkeypatch)

        with pytest.raises(ToolError, match="overlaps 'Walk'"):
            server.create_event([PublicEvent(summary="Walk", start=_at(9, 30), end=_at(10, 15))])

        assert calendar.written == []


class TestDeleteEvent:
    def test_cancels_several_saying_which_count(self, monkeypatch):
        calendar, cancellations = _fake_changes(monkeypatch)

        result = server.delete_event([
            EventCancel(event_id="abc123", counts_against_follow_through=False),
            EventCancel(event_id="def456", counts_against_follow_through=True),
        ])

        assert {e.id: e.is_cancelled for e in result.events} == {"abc123": True, "def456": True}
        (event, source), _ = cancellations.record.call_args
        assert (event.id, source) == ("def456", "delete_event")


class TestNote:
    def test_appends_to_the_noted_time_sheet_and_returns_it(self, monkeypatch):
        noted_time_sheet = _fake_noted_time_sheet(monkeypatch)
        noted_time_sheet.append.side_effect = lambda n: SheetNote(row=7, note=n)
        noted_time = NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, tzinfo=UTC), description="Started work")

        result = server.note(noted_time)

        assert result == NoteWithId(
            id="2026-01-01T09:00:00+00:00#7",
            timestamp=noted_time.timestamp,
            description="Started work",
        )
        noted_time_sheet.append.assert_called_once_with(noted_time)

    def test_never_lets_a_caller_set_the_compaction_id(self, monkeypatch):
        noted_time_sheet = _fake_noted_time_sheet(monkeypatch)
        noted_time_sheet.append.side_effect = lambda n: SheetNote(row=7, note=n)
        noted_time = NotedTime(
            timestamp=datetime(2026, 1, 1, 9, 0, tzinfo=UTC), compaction_id="sneaky"
        )

        result = server.note(noted_time)

        assert result.compaction_id is None
        assert noted_time_sheet.append.call_args.args[0].compaction_id is None


class TestGetNotes:
    def test_returns_the_uncompacted_notes_with_ids_sorted_by_timestamp(self, monkeypatch):
        noted_time_sheet = _fake_noted_time_sheet(monkeypatch)
        noted_time_sheet.read_with_rows.return_value = [
            SheetNote(row=2, note=NotedTime(timestamp=datetime(2026, 1, 1, 10, 0, tzinfo=UTC))),
            SheetNote(
                row=3,
                note=NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, tzinfo=UTC), description="Started work"),
            ),
        ]

        result = server.get_notes()

        assert [n.id for n in result] == [
            "2026-01-01T09:00:00+00:00#3",
            "2026-01-01T10:00:00+00:00#2",
        ]
        assert result[0].description == "Started work"
        noted_time_sheet.read_with_rows.assert_called_once_with(include_compacted=False)

    def test_can_include_compacted_notes(self, monkeypatch):
        noted_time_sheet = _fake_noted_time_sheet(monkeypatch)
        noted_time_sheet.read_with_rows.return_value = []

        server.get_notes(include_compacted=True)

        noted_time_sheet.read_with_rows.assert_called_once_with(include_compacted=True)


class TestEditNote:
    def test_delegates_to_the_compactor_and_returns_the_new_id(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)
        new_time = datetime(2026, 1, 1, 9, 30, tzinfo=UTC)
        compactor.edit_note.return_value = SheetNote(
            row=5, note=NotedTime(timestamp=new_time, description="Standup")
        )

        result = server.edit_note("2026-01-01T09:00:00+00:00#5", timestamp=new_time)

        assert result.id == "2026-01-01T09:30:00+00:00#5"
        compactor.edit_note.assert_called_once_with(
            "2026-01-01T09:00:00+00:00#5", timestamp=new_time, description=None
        )

    def test_reports_a_refusal_as_a_tool_error(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)
        compactor.edit_note.side_effect = CompactionError("already compacted")

        with pytest.raises(ToolError, match="already compacted"):
            server.edit_note("2026-01-01T09:00:00+00:00#5", description="x")


class TestDeleteNote:
    def test_delegates_to_the_compactor(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)

        result = server.delete_note("2026-01-01T09:00:00+00:00#5")

        assert result is compactor.delete_note.return_value
        compactor.delete_note.assert_called_once_with("2026-01-01T09:00:00+00:00#5")

    def test_reports_a_refusal_as_a_tool_error(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)
        compactor.delete_note.side_effect = CompactionError("no longer holds")

        with pytest.raises(ToolError, match="no longer holds"):
            server.delete_note("2026-01-01T09:00:00+00:00#5")


def _fake_compactor(monkeypatch) -> MagicMock:
    compactor = MagicMock()
    monkeypatch.setattr(server, "get_note_compactor", lambda: compactor)
    return compactor


class TestPrepareCompaction:
    def test_delegates_to_the_compactor(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)

        result = server.prepare_compaction()

        assert result is compactor.prepare.return_value

    def test_runs_with_sheet_reads_cached_for_the_call_only(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)
        caches = []
        compactor.prepare.side_effect = lambda: caches.append(google_sheets._read_cache.get())

        server.prepare_compaction()

        assert caches == [{}]
        assert google_sheets._read_cache.get() is None

    def test_runs_with_calendar_listings_cached_for_the_call_only(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)
        caches = []
        compactor.prepare.side_effect = lambda: caches.append(google_calendar._listings.get())

        server.prepare_compaction()

        assert caches == [[]]
        assert google_calendar._listings.get() is None


class TestCompactNotes:
    def test_a_dry_run_plans_its_updates_creates_and_cancels(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)
        end = datetime(2026, 1, 1, 12, 15, tzinfo=UTC)

        result = server.compact_notes(
            updates=[CompactionUpdate(event_id="e1", end=end)],
            creates=[CompactionCreate(summary="Walk", start_note="n1", end_note="n2")],
            cancels=[EventCancel(event_id="e2", counts_against_follow_through=False)],
        )

        assert result is compactor.dry_run.return_value
        compactor.dry_run.assert_called_once_with(
            [
                EventDecision(action="keep", event_id="e1", end=end),
                EventDecision(action="create", summary="Walk", start_note="n1", end_note="n2"),
                EventDecision(action="cancel", event_id="e2", counts_against_follow_through=False),
            ],
            None, None, None, None,
        )

    def test_a_dry_run_passes_ignored_notes_through(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)

        server.compact_notes(updates=[CompactionUpdate(event_id="e1")], ignore_notes=["n2"])

        compactor.dry_run.assert_called_once_with([EventDecision(action="keep", event_id="e1")], ["n2"], None, None, None)

    def test_a_dry_run_with_no_decisions_records_everything_as_on_schedule(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)

        server.compact_notes()

        compactor.dry_run.assert_called_once_with([], None, None, None, None)

    def test_a_dry_run_passes_what_it_adds_through(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)
        new_actions = [NewAction(ref="new:juggle", name="Juggle")]
        new_people = [NewPerson(ref="new:alex", name="Alex")]
        new_locations = [NewLocation(ref="new:park", name="Park")]

        server.compact_notes(new_actions=new_actions, new_people=new_people, new_locations=new_locations)

        compactor.dry_run.assert_called_once_with([], None, new_actions, new_people, new_locations)

    def test_a_dry_run_with_a_compaction_id_describes_the_stored_plan(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)

        result = server.compact_notes(compaction_id="abc")

        assert result is compactor.describe.return_value
        compactor.describe.assert_called_once_with("abc")

    def test_committing_applies_the_stored_plan(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)

        result = server.compact_notes(compaction_id="abc", dry_run=False)

        assert result is compactor.commit.return_value
        compactor.commit.assert_called_once_with("abc")

    def test_committing_without_a_dry_run_first_is_refused(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)

        with pytest.raises(ToolError, match="dry run first"):
            server.compact_notes(updates=[], dry_run=False)

        compactor.commit.assert_not_called()

    def test_compaction_errors_become_tool_errors(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)
        compactor.commit.side_effect = CompactionError("the notes changed")

        with pytest.raises(ToolError, match="the notes changed"):
            server.compact_notes(compaction_id="abc", dry_run=False)

    def test_a_rejection_is_logged_with_its_categories(self, monkeypatch, caplog):
        compactor = _fake_compactor(monkeypatch)
        compactor.dry_run.side_effect = CompactionError.of(
            [Problem("overlap", "'A' overlaps 'B'"), Problem("unknown_note", "'n9' isn't a note")]
        )

        with caplog.at_level(logging.WARNING, logger="server"), pytest.raises(ToolError):
            server.compact_notes(updates=[])

        assert caplog.messages == [
            "compaction rejected: tool=compact_notes categories=overlap,unknown_note: "
            "'A' overlaps 'B' | 'n9' isn't a note"
        ]

    def test_committing_without_a_dry_run_is_logged_as_such(self, monkeypatch, caplog):
        _fake_compactor(monkeypatch)

        with caplog.at_level(logging.WARNING, logger="server"), pytest.raises(ToolError):
            server.compact_notes(dry_run=False)

        assert "categories=no_dry_run" in caplog.text


class TestAbandonCompaction:
    def test_delegates_to_the_compactor(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)

        result = server.abandon_compaction("abc")

        assert result is compactor.abandon.return_value
        compactor.abandon.assert_called_once_with("abc")

    def test_compaction_errors_become_tool_errors(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)
        compactor.abandon.side_effect = CompactionError("already complete")

        with pytest.raises(ToolError, match="already complete"):
            server.abandon_compaction("abc")


class TestGetNoteCompactor:
    def test_caches_across_calls(self, monkeypatch):
        monkeypatch.setattr(server, "_note_compactor", None)
        monkeypatch.setattr(server, "_compaction_journal", None)
        monkeypatch.setattr(server, "get_calendar_client", lambda: MagicMock())
        monkeypatch.setattr(server, "get_noted_time_sheet", lambda: MagicMock())
        monkeypatch.setattr(server, "get_people_store", lambda: MagicMock())
        monkeypatch.setattr(server, "get_location_store", lambda: MagicMock())
        monkeypatch.setattr(server, "get_trait_store", lambda: MagicMock())
        monkeypatch.setattr(server, "get_trait_rollup", lambda: MagicMock())
        monkeypatch.setattr(server, "get_cancellation_store", lambda: MagicMock())
        built = []
        monkeypatch.setattr(server, "build_compaction_journal", lambda: built.append(1) or MagicMock())

        first = server.get_note_compactor()
        second = server.get_note_compactor()

        assert first is second
        assert len(built) == 1


class TestGetCalendarClient:
    def test_caches_client_across_calls(self, monkeypatch):
        built = []

        def fake_build():
            client = MagicMock()
            built.append(client)
            return client

        monkeypatch.setattr(server, "_calendar_client", None)
        monkeypatch.setattr(server, "build_calendar_client", fake_build)

        first = server.get_calendar_client()
        second = server.get_calendar_client()

        assert first is second
        assert len(built) == 1


def _fake_traits(monkeypatch) -> MagicMock:
    traits = MagicMock()
    traits.all.return_value = list(SEED_TRAITS)
    monkeypatch.setattr(server, "get_trait_store", lambda: traits)
    return traits


class TestTraitTools:
    def test_get_traits_delegates(self, monkeypatch):
        traits = _fake_traits(monkeypatch)
        traits.get_traits.return_value = []

        assert server.get_traits(["archived"]) == []
        traits.get_traits.assert_called_once_with(["archived"])

    def test_create_and_update_wrap_errors_as_tool_errors(self, monkeypatch):
        traits = _fake_traits(monkeypatch)
        traits.create_trait.side_effect = ValueError("The trait 'X': part 1 (prep) needs a target")
        traits.update_trait.side_effect = ValueError("'x' isn't a trait")

        with pytest.raises(ToolError, match="needs a target"):
            server.create_trait(Trait(name="X"))
        with pytest.raises(ToolError, match="isn't a trait"):
            server.update_trait(Trait(id="x"), clear_fields=["definition"])
        traits.update_trait.assert_called_once_with(Trait(id="x"), ["definition"])


class TestActionTools:
    def test_delegate_to_the_store(self, monkeypatch):
        actions = MagicMock()
        monkeypatch.setattr(server, "get_action_store", lambda: actions)

        server.get_actions(["archived"])
        server.get_action("Walk")
        server.create_action(Action(name="Walk"))
        server.update_action(Action(id="a1"), clear_fields=["note"])
        server.get_action_groups()
        server.get_action_group("Creative")
        server.create_action_group(ActionGroup(name="Creative"))
        server.update_action_group(ActionGroup(id="g1"), clear_fields=["group_id"])
        server.delete_action_group("g1")
        server.get_priority_colors()
        server.update_priority_color(1, "#123456")

        actions.priority_colors.assert_called_once_with()
        actions.set_priority_color.assert_called_once_with(1, "#123456")
        actions.get_action_groups.assert_called_once_with()
        actions.get_action_group.assert_called_once_with("Creative")
        actions.create_action_group.assert_called_once_with(ActionGroup(name="Creative"))
        actions.update_action_group.assert_called_once_with(ActionGroup(id="g1"), ["group_id"])
        actions.delete_action_group.assert_called_once_with("g1")
        actions.get_actions.assert_called_once_with(["archived"])
        actions.get_action.assert_called_once_with("Walk")
        actions.create_action.assert_called_once_with(Action(name="Walk"))
        actions.update_action.assert_called_once_with(Action(id="a1"), ["note"])

    def test_wrap_errors(self, monkeypatch):
        actions = MagicMock()
        actions.get_action.side_effect = ValueError("There's no action with the id or name 'x'")
        actions.create_action.side_effect = ValueError("there's already an action named 'Walk'")
        monkeypatch.setattr(server, "get_action_store", lambda: actions)

        with pytest.raises(ToolError, match="no action"):
            server.get_action("x")
        with pytest.raises(ToolError, match="already an action"):
            server.create_action(Action(name="walk"))

    def test_the_store_is_built_once(self, monkeypatch):
        built = []
        monkeypatch.setattr(server, "_actions", None)
        monkeypatch.setattr(server, "build_actions", lambda: built.append(1) or MagicMock())

        assert server.get_action_store() is server.get_action_store()
        assert built == [1]


class TestPeopleTools:
    def _cancellations(self, monkeypatch, by_person=None):
        store = MagicMock()
        store.by_person.return_value = by_person or {}
        monkeypatch.setattr(server, "get_cancellation_store", lambda: store)
        return store

    def test_delegate_to_the_store(self, monkeypatch):
        people = MagicMock()
        people.get_people.return_value = [ListedPerson(id="self", name="Me")]
        people.get_person.return_value = ListedPerson(id="self", name="Me")
        monkeypatch.setattr(server, "get_people_store", lambda: people)
        self._cancellations(monkeypatch)

        server.get_people(["archived"])
        server.get_person("self")
        server.create_person(Person(name="Sam"))
        server.update_person(Person(id="p1"), clear_fields=["context"])
        server.get_circles()
        server.get_circle("Family")
        server.create_circle(Circle(name="Family"))
        server.update_circle(Circle(id="c1"), clear_fields=["note"])
        server.delete_circle("c1")

        people.get_people.assert_called_once_with(["archived"])
        people.get_person.assert_called_once_with("self")
        people.create_person.assert_called_once_with(Person(name="Sam"))
        people.update_person.assert_called_once_with(Person(id="p1"), ["context"])
        people.get_circles.assert_called_once_with()
        people.get_circle.assert_called_once_with("Family")
        people.create_circle.assert_called_once_with(Circle(name="Family"))
        people.update_circle.assert_called_once_with(Circle(id="c1"), ["note"])
        people.delete_circle.assert_called_once_with("c1")

    def test_each_person_comes_with_the_events_the_user_cancelled_that_count_against_them(self, monkeypatch):
        dropped = CancelledEvent(event_id="c1", summary="Lunch", engagement="with", source="delete_event")
        people = MagicMock()
        people.get_people.return_value = [ListedPerson(id="self", name="Me"), ListedPerson(id="sam", name="Sam")]
        people.get_person.return_value = ListedPerson(id="sam", name="Sam")
        monkeypatch.setattr(server, "get_people_store", lambda: people)
        self._cancellations(monkeypatch, {"sam": [dropped]})

        listed = server.get_people()
        one = server.get_person("Sam")

        assert [(p.id, p.cancelled_events) for p in listed] == [("self", []), ("sam", [dropped])]
        assert one.cancelled_events == [dropped]

    def test_wrap_errors(self, monkeypatch):
        people = MagicMock()
        people.create_person.side_effect = ValueError("there's already a person named 'Sam'")
        monkeypatch.setattr(server, "get_people_store", lambda: people)

        with pytest.raises(ToolError, match="already a person"):
            server.create_person(Person(name="Sam"))


class TestLocationTools:
    def test_delegate_to_the_store(self, monkeypatch):
        locations = MagicMock()
        monkeypatch.setattr(server, "get_location_store", lambda: locations)

        server.get_locations()
        server.get_location("Home")
        server.create_location(Location(name="Home"))
        server.update_location(Location(id="l1"), clear_fields=["hint"])
        server.delete_location("l1")

        locations.all.assert_called_once_with()
        locations.get_location.assert_called_once_with("Home")
        locations.create_location.assert_called_once_with(Location(name="Home"))
        locations.update_location.assert_called_once_with(Location(id="l1"), ["hint"])
        locations.delete_location.assert_called_once_with("l1")

    def test_wrap_errors(self, monkeypatch):
        locations = MagicMock()
        locations.get_location.side_effect = ValueError("There's no location with the id or name 'x'")
        monkeypatch.setattr(server, "get_location_store", lambda: locations)

        with pytest.raises(ToolError, match="no location"):
            server.get_location("x")


class TestGetCompactionStatus:
    def test_reports_the_last_compaction_and_latest_compacted_note(self, monkeypatch):
        journal = MagicMock()
        journal.last_stamped_now.return_value = datetime(2026, 10, 2, 21, tzinfo=timezone.utc)
        notes = MagicMock()
        latest = NotedTime(timestamp=datetime(2026, 10, 2, 20, tzinfo=timezone.utc), description="Done", compaction_id="c1")
        notes.read_with_latest_compacted.return_value = ([], latest)
        monkeypatch.setattr(server, "get_compaction_journal", lambda: journal)
        monkeypatch.setattr(server, "get_noted_time_sheet", lambda: notes)
        compactor = _fake_compactor(monkeypatch)
        compactor.judgments_due.return_value = JudgmentsDue(compaction_id="c1", events=[MagicMock()], parts=[], history={})

        status = server.get_compaction_status()

        assert status == server.CompactionStatus(
            last_compaction=datetime(2026, 10, 2, 21, tzinfo=timezone.utc),
            latest_compacted_note=latest,
            judgments_pending="c1",
        )

    def test_is_empty_before_any_compaction(self, monkeypatch):
        journal = MagicMock()
        journal.last_stamped_now.return_value = None
        notes = MagicMock()
        notes.read_with_latest_compacted.return_value = ([], None)
        monkeypatch.setattr(server, "get_compaction_journal", lambda: journal)
        monkeypatch.setattr(server, "get_noted_time_sheet", lambda: notes)
        _fake_compactor(monkeypatch).judgments_due.return_value = None

        assert server.get_compaction_status() == server.CompactionStatus()


class TestTraitScoreTools:
    def test_get_trait_scores_delegates(self, monkeypatch):
        rollup = MagicMock()
        monkeypatch.setattr(server, "get_trait_rollup", lambda: rollup)

        result = server.get_trait_scores("p1", date(2026, 10, 1), date(2026, 10, 5))

        assert result is rollup.get.return_value
        rollup.get.assert_called_once_with("p1", date(2026, 10, 1), date(2026, 10, 5))

    def test_rebuild_rolls_up_every_day_in_the_span(self, monkeypatch):
        rollup = MagicMock()
        monkeypatch.setattr(server, "get_trait_rollup", lambda: rollup)

        server.rebuild_trait_scores(date(2026, 9, 30), date(2026, 10, 2))

        rollup.roll_up.assert_called_once_with([date(2026, 9, 30), date(2026, 10, 1), date(2026, 10, 2)])
        with pytest.raises(ToolError, match="end is before start"):
            server.rebuild_trait_scores(date(2026, 10, 2), date(2026, 10, 1))


class TestJudgmentTools:
    def test_prepare_judgments_delegates(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)

        result = server.prepare_judgments("c1", redo=True)

        assert result is compactor.judgments_due.return_value
        compactor.judgments_due.assert_called_once_with("c1", redo=True)

    def test_prepare_judgments_without_a_compaction_says_so(self, monkeypatch):
        _fake_compactor(monkeypatch).judgments_due.return_value = None

        with pytest.raises(ToolError, match="no applied compaction to judge"):
            server.prepare_judgments()

    def test_record_judgments_delegates_and_wraps_errors(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)
        judgments = [Judgment(request_id="e1/self/adventurous/judgment", rating=2, reasoning="A new place.")]

        assert server.record_judgments("c1", judgments) is compactor.record_judgments.return_value
        compactor.record_judgments.assert_called_once_with("c1", judgments)
        compactor.record_judgments.side_effect = CompactionError("the rating 9 isn't one of its ratings")
        with pytest.raises(ToolError, match="isn't one of its ratings"):
            server.record_judgments("c1", judgments)


class TestGetNotedTimeSheet:
    def test_caches_across_calls(self, monkeypatch):
        built = []

        def fake_build():
            noted_time_sheet = MagicMock()
            built.append(noted_time_sheet)
            return noted_time_sheet

        monkeypatch.setattr(server, "_noted_time_sheet", None)
        monkeypatch.setattr(server, "build_noted_time_sheet", fake_build)

        first = server.get_noted_time_sheet()
        second = server.get_noted_time_sheet()

        assert first is second
        assert len(built) == 1


class TestMemoryTracking:
    """Each tool wraps its body in `track(...)` -- see
    utilities/memory_diagnostics.py, tested on its own merits in
    tests/test_memory_diagnostics.py. These just confirm the wiring: the
    right label, actually wrapping the call."""

    def test_list_events(self, monkeypatch):
        _fake_client(monkeypatch)
        labels = _tracked_labels(monkeypatch)

        server.list_events(
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC), datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        )

        assert labels == ["list_events"]

    def test_get_event(self, monkeypatch):
        client = _fake_client(monkeypatch)
        client.get_event.return_value = _event(id="abc123")
        labels = _tracked_labels(monkeypatch)

        server.get_event("abc123")

        assert labels == ["get_event"]

    def test_update_event(self, monkeypatch):
        _fake_changes(monkeypatch)
        labels = _tracked_labels(monkeypatch)

        server.update_event(updates=[EventUpdate(event=PublicEvent(id="abc123", summary="x"))])

        assert labels == ["update_event"]

    def test_create_event(self, monkeypatch):
        _fake_changes(monkeypatch)
        labels = _tracked_labels(monkeypatch)

        server.create_event([_public_event(start=_at(14), end=_at(15))])

        assert labels == ["create_event"]

    def test_delete_event(self, monkeypatch):
        _fake_changes(monkeypatch)
        labels = _tracked_labels(monkeypatch)

        server.delete_event([EventCancel(event_id="abc123", counts_against_follow_through=False)])

        assert labels == ["delete_event"]

    def test_action_tools(self, monkeypatch):
        monkeypatch.setattr(server, "get_action_store", lambda: MagicMock())
        labels = _tracked_labels(monkeypatch)

        server.get_actions()
        server.create_action(Action(name="Walk"))

        assert labels == ["get_actions", "create_action"]

    def test_note(self, monkeypatch):
        _fake_noted_time_sheet(monkeypatch)
        labels = _tracked_labels(monkeypatch)

        server.note(NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, tzinfo=UTC)))

        assert labels == ["note"]

    def test_get_notes(self, monkeypatch):
        noted_time_sheet = _fake_noted_time_sheet(monkeypatch)
        noted_time_sheet.read.return_value = []
        labels = _tracked_labels(monkeypatch)

        server.get_notes()

        assert labels == ["get_notes"]


class TestWithCors:
    @pytest.fixture
    def client(self, monkeypatch):
        monkeypatch.setenv("MCP_CORS_ALLOWED_ORIGINS", "https://tracker.example")

        async def mcp_endpoint(request):
            # Stands in for the real endpoint, auth included: anything
            # without a bearer token gets a 401.
            if "authorization" not in request.headers:
                return PlainTextResponse("unauthorized", status_code=401)
            return PlainTextResponse("ok", headers={"Mcp-Session-Id": "session-1"})

        app = Starlette(routes=[Route("/mcp", mcp_endpoint, methods=["POST"])])
        return TestClient(server.with_cors(app))

    @staticmethod
    def preflight(client, origin):
        return client.options(
            "/mcp",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type,mcp-session-id,mcp-protocol-version",
            },
        )

    @pytest.mark.parametrize(
        "origin", ["http://localhost:8765", "http://127.0.0.1:3000", "https://tracker.example"]
    )
    def test_answers_preflight_before_auth(self, client, origin):
        response = self.preflight(client, origin)

        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == origin

    @pytest.mark.parametrize(
        "origin", ["https://evil.example", "http://localhost.evil.example", "https://localhost:8765"]
    )
    def test_rejects_other_origins(self, client, origin):
        response = self.preflight(client, origin)

        assert response.status_code == 400
        assert "access-control-allow-origin" not in response.headers

    def test_exposes_session_id(self, client):
        response = client.post(
            "/mcp", headers={"Origin": "http://localhost:8765", "Authorization": "Bearer t"}
        )

        assert response.headers["mcp-session-id"] == "session-1"
        assert "mcp-session-id" in response.headers["access-control-expose-headers"].lower()


class TestPublicRecurrence:
    def test_shows_a_series_in_its_own_time_zone_with_its_schedule_in_words(self):
        series = Event(
            id="s1",
            summary="Standup",
            start=datetime(2026, 10, 5, 13, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 14, tzinfo=timezone.utc),
            time_zone="America/New_York",
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"],
            action_ids=["g1"],
        )
        tree = ActionTree([Action(id="g1", name="Work", status="active", label_id="l1")], GroupTree([]))

        public = server.PublicRecurrence.from_event(series, tree)

        assert public.start.isoformat() == "2026-10-05T09:00:00-04:00"
        assert public.schedule == "Every week on Mon"
        assert public.action_names == ["Work"]
        assert public.repeat == Repeat(every="week", weekdays=["mon"])

    def test_a_series_repeating_in_ways_repeat_cant_say_shows_its_rules_in_schedule(self):
        rules = ["RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1"]
        series = Event(
            id="s1",
            summary="Payroll",
            start=datetime(2026, 10, 30, 13, tzinfo=timezone.utc),
            end=datetime(2026, 10, 30, 14, tzinfo=timezone.utc),
            time_zone="America/New_York",
            recurrence=rules,
        )

        public = server.PublicRecurrence.from_event(series, ActionTree([], GroupTree([])))

        assert public.repeat is None
        assert public.schedule == rules[0]

    def test_update_recurrence_passes_the_repeat_along(self, monkeypatch):
        recurrences = MagicMock()
        recurrences.update.return_value = []
        monkeypatch.setattr(server, "get_recurrences", lambda: recurrences)
        monkeypatch.setattr(server, "_check_action_ids", lambda *args, **kwargs: None)
        repeat = Repeat(every="day", count=5)

        server.update_recurrence(server.PublicRecurrence(id="s1", repeat=repeat), "s1_x")

        changes, starting_at, passed = recurrences.update.call_args.args
        assert (changes.id, changes.recurrence, starting_at, passed) == ("s1", None, "s1_x", repeat)

    def test_update_recurrence_reports_a_bad_repeat_as_a_tool_error(self, monkeypatch):
        recurrences = MagicMock()
        recurrences.update.side_effect = ValueError("Give count or until, not both")
        monkeypatch.setattr(server, "get_recurrences", lambda: recurrences)
        monkeypatch.setattr(server, "_check_action_ids", lambda *args, **kwargs: None)

        with pytest.raises(ToolError, match="count or until"):
            server.update_recurrence(
                server.PublicRecurrence(id="s1", repeat=Repeat(every="day", count=2, until=date(2026, 12, 31)))
            )

    def test_update_recurrence_clears_the_fields_it_names(self, monkeypatch):
        recurrences = MagicMock()
        recurrences.update.return_value = []
        monkeypatch.setattr(server, "get_recurrences", lambda: recurrences)
        monkeypatch.setattr(server, "_check_action_ids", lambda *args, **kwargs: None)

        server.update_recurrence(server.PublicRecurrence(id="s1"), "s1_x", clear_fields=["priority"])

        changes, starting_at, _ = recurrences.update.call_args.args
        assert (changes.cleared, starting_at) == ({"priority"}, "s1_x")

    def test_update_recurrence_refuses_a_bad_clear_before_splitting(self, monkeypatch):
        recurrences = MagicMock()
        monkeypatch.setattr(server, "get_recurrences", lambda: recurrences)
        monkeypatch.setattr(server, "_check_action_ids", lambda *args, **kwargs: None)

        with pytest.raises(ToolError, match="Can't both set and clear"):
            server.update_recurrence(server.PublicRecurrence(id="s1", priority=1), "s1_x", clear_fields=["priority"])

        recurrences.update.assert_not_called()

    def test_reads_an_until_date_as_a_date_and_a_datetime_as_a_datetime(self):
        model = TypeAdapter(server.PublicRecurrence)

        dated = model.validate_python({"id": "s1", "repeat": {"every": "week", "until": "2026-12-31"}})
        timed = model.validate_python({"id": "s1", "repeat": {"every": "week", "until": "2026-12-31T00:00:00-05:00"}})

        assert dated.repeat.until == date(2026, 12, 31)
        assert timed.repeat.until == datetime(2026, 12, 31, 5, tzinfo=timezone.utc)


class TestDeleteRecurrence:
    def test_returns_nothing_for_a_series_deleted_whole(self, monkeypatch):
        recurrences = MagicMock()
        recurrences.delete.return_value = None
        monkeypatch.setattr(server, "get_recurrences", lambda: recurrences)

        assert server.delete_recurrence("s1") == []
        recurrences.delete.assert_called_once_with("s1", None)

    def test_returns_what_is_left_of_the_series_after_this_and_following(self, monkeypatch):
        recurrences = MagicMock()
        recurrences.delete.return_value = Event(
            id="s1",
            start=datetime(2026, 10, 5, 13, tzinfo=timezone.utc),
            end=datetime(2026, 10, 5, 14, tzinfo=timezone.utc),
            time_zone="America/New_York",
            recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z"],
        )
        monkeypatch.setattr(server, "get_recurrences", lambda: recurrences)


        (left,) = server.delete_recurrence("s1", "s1_x")

        recurrences.delete.assert_called_once_with("s1", "s1_x")
        assert left.schedule == "Every week on Mon, until Oct 19, 2026"

    def test_reports_an_event_from_another_series_as_a_tool_error(self, monkeypatch):
        recurrences = MagicMock()
        recurrences.delete.side_effect = ValueError("Event x isn't one of series s1's events")
        monkeypatch.setattr(server, "get_recurrences", lambda: recurrences)

        with pytest.raises(ToolError, match="isn't one of series s1's events"):
            server.delete_recurrence("s1", "x")


class TestSetTimeZone:
    def _fake_marker(self, monkeypatch) -> MagicMock:
        marker = MagicMock()
        monkeypatch.setattr(server, "CompactionMarker", lambda client: marker)
        return marker

    def test_sets_both_calendars_time_zones(self, monkeypatch):
        client = _fake_client(monkeypatch)
        client.set_time_zone.return_value = ZoneInfo("America/New_York")
        marker = self._fake_marker(monkeypatch)

        assert server.set_time_zone("America/New_York") == "America/New_York"

        client.set_time_zone.assert_called_once_with("America/New_York")
        marker.calendar.assert_called_once_with(create=False)
        marker.calendar.return_value.set_time_zone.assert_called_once_with("America/New_York")

    def test_without_a_compactions_calendar_sets_only_the_main_one(self, monkeypatch):
        client = _fake_client(monkeypatch)
        client.set_time_zone.return_value = ZoneInfo("America/New_York")
        self._fake_marker(monkeypatch).calendar.return_value = None

        assert server.set_time_zone("America/New_York") == "America/New_York"

    def test_reports_a_bad_name_as_a_tool_error(self, monkeypatch):
        _fake_client(monkeypatch).set_time_zone.side_effect = ValueError("'Nowhere' isn't a time zone")
        marker = self._fake_marker(monkeypatch)

        with pytest.raises(ToolError, match="isn't a time zone"):
            server.set_time_zone("Nowhere")

        marker.calendar.assert_not_called()

    def test_any_tool_without_one_asks_for_it_to_be_set_then_retried(self, monkeypatch):
        compactor = MagicMock()
        compactor.prepare.side_effect = TimeZoneNotSetError("no time zone")
        monkeypatch.setattr(server, "get_note_compactor", lambda: compactor)

        with pytest.raises(ToolError, match="Call set_time_zone .* then call prepare_compaction again"):
            server.prepare_compaction()


_READ_ONLY_TOOLS = {
    "list_events",
    "get_event",
    "get_recurrence",
    "get_compaction_status",
    "get_notes",
    "get_traits",
    "get_actions",
    "get_action",
    "get_action_groups",
    "get_action_group",
    "get_priority_colors",
    "get_people",
    "get_person",
    "get_circles",
    "get_circle",
    "get_locations",
    "get_location",
    "prepare_judgments",
    "get_trait_scores",
}


@pytest.mark.without_write_lock
class TestWriteLock:
    def test_every_tool_but_the_read_only_ones_holds_it(self):
        # A new tool has to be put on one side or the other: a read-only
        # one that reaches a write fails (see calendar_clients/
        # write_lock.py), so only list one here that never writes.
        tools = {tool.name: tool.fn for tool in server.mcp._tool_manager.list_tools()}

        assert _READ_ONLY_TOOLS <= tools.keys()
        assert {name for name, fn in tools.items() if not getattr(fn, "writes", False)} == _READ_ONLY_TOOLS

    def test_a_writing_tool_holds_it_for_its_whole_call(self, monkeypatch):
        held = []
        sheet = MagicMock()
        sheet.append.side_effect = lambda noted_time: held.append(WRITE_LOCK.held()) or SheetNote(
            row=2, note=noted_time
        )
        monkeypatch.setattr(server, "get_noted_time_sheet", lambda: sheet)

        server.note(NotedTime(timestamp=datetime(2026, 10, 3, 9, tzinfo=UTC)))

        assert held == [True]
        assert not WRITE_LOCK.held()

    def test_a_read_only_tool_runs_while_another_thread_holds_it(self, monkeypatch):
        sheet = MagicMock()
        sheet.read_with_rows.return_value = []
        monkeypatch.setattr(server, "get_noted_time_sheet", lambda: sheet)
        done = threading.Event()

        with WRITE_LOCK:
            thread = threading.Thread(target=lambda: (server.get_notes(), done.set()))
            thread.start()
            assert done.wait(5)
        thread.join()

    def test_a_built_helper_returns_without_waiting_for_it(self, monkeypatch):
        sheet = MagicMock()
        monkeypatch.setattr(server, "_noted_time_sheet", sheet)
        got = []

        with WRITE_LOCK:
            thread = threading.Thread(target=lambda: got.append(server.get_noted_time_sheet()))
            thread.start()
            thread.join(5)

        assert got == [sheet]

    def test_a_helper_builds_under_it(self, monkeypatch):
        held = []
        monkeypatch.setattr(server, "_noted_time_sheet", None)
        monkeypatch.setattr(server, "build_noted_time_sheet", lambda: held.append(WRITE_LOCK.held()) or MagicMock())

        server.get_noted_time_sheet()

        assert held == [True]
