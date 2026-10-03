import contextlib
import dataclasses
import threading
from datetime import date, datetime, timezone
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import server
from calendar_clients import google_sheets
from calendar_clients.google_calendar import Event, EventLabelConflictError
from calendar_clients.write_lock import WRITE_LOCK
from server import PublicEvent
from utilities.goal_calendar import GoalCalendar
from utilities.goal_health import Assessment
from utilities.goal_sheet import Goal
from utilities.goals import GoalList, GoalTree
from utilities.note_compaction import CompactionError, EventDecision
from utilities.noted_time_sheet import NotedTime, NoteWithId, SheetNote
from utilities.reallocating_calendar import ReallocatingCalendar
from utilities.reallocation import ReallocationOptions

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


def _fake_reallocating_calendar(monkeypatch) -> MagicMock:
    reallocating_calendar = MagicMock()
    monkeypatch.setattr(server, "get_reallocating_calendar", lambda: reallocating_calendar)
    return reallocating_calendar


@pytest.fixture(autouse=True)
def _no_goals(monkeypatch):
    """Every event tool fills in goal_names and effective_priority/
    effective_is_fixed_time from the goals tab -- faked here as having no
    goals at all, so tests that don't care about goals never touch a real
    sheet. Tests that do care use _fake_goals. Seeds the cache rather than
    replacing get_goal_store, so its own caching tests still exercise the
    real one."""
    goals = MagicMock()
    goals.tree.return_value = GoalTree([])
    monkeypatch.setattr(server, "_goals", goals)


def _fake_goals(monkeypatch, *goals: Goal) -> MagicMock:
    store = MagicMock()
    store.tree.return_value = GoalTree(list(goals))
    monkeypatch.setattr(server, "get_goal_store", lambda: store)
    return store


def _goal(goal_id: str = "g1", **overrides) -> Goal:
    fields = {"id": goal_id, "name": "Focus", "status": "active", "label_id": f"label-{goal_id}"}
    fields.update(overrides)
    return Goal(**fields)


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
    def test_goals_inferred_from_a_label_are_marked_and_not_written_back(self):
        public = PublicEvent.from_event(Event(id="e1", goal_ids=["g1"], goals_from_label=True))

        assert public.goals_from_label
        assert public.to_event().goal_ids is None
        public.goals_from_label = False  # Confirmed: stored.
        assert public.to_event().goal_ids == ["g1"]

    def test_hides_internal_fields_from_its_fields(self):
        field_names = {f.name for f in dataclasses.fields(PublicEvent)}

        assert field_names.isdisjoint(server.INTERNAL_EVENT_FIELDS)
        # is_cancelled has no Event equivalent -- it's derived from the
        # hidden status field, not a field PublicEvent passes through.
        # Likewise the effective_* fields and goal_names, derived from the
        # event's goals.
        event_derived_fields = field_names - {
            "is_cancelled",
            "effective_priority",
            "effective_is_fixed_time",
            "goal_names",
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

    def test_from_event_effective_fields_default_to_the_events_own(self):
        event = _event(id="abc123", priority=1, is_fixed_time=True)

        public_event = PublicEvent.from_event(event)

        assert public_event.effective_priority == 1
        assert public_event.effective_is_fixed_time is True

    def test_from_event_takes_effective_fields_from_the_events_goal(self):
        event = _event(id="abc123", goal_ids=["g1"], goal_priority=0, goal_is_fixed_time=True)

        public_event = PublicEvent.from_event(event)

        assert public_event.priority is None
        assert public_event.is_fixed_time is None
        assert public_event.effective_priority == 0
        assert public_event.effective_is_fixed_time is True

    def test_to_event_ignores_effective_fields(self):
        public_event = _public_event(
            id="abc123", effective_priority=0, effective_is_fixed_time=True
        )

        event = public_event.to_event()

        assert event.priority is None
        assert event.is_fixed_time is None
        assert event.min_duration is None

    def test_to_event_ignores_event_label_id_since_goals_decide_it(self):
        public_event = _public_event(id="abc123", event_label_id="label-1")

        event = public_event.to_event()

        assert event.event_label_id is None

    def test_to_event_carries_goal_ids_but_not_goal_names(self):
        public_event = _public_event(id="abc123", goal_ids=["g1", "g2"], goal_names=["A", "B"])

        event = public_event.to_event()

        assert event.goal_ids == ["g1", "g2"]

    def test_from_event_names_the_events_goals(self):
        tree = GoalTree([_goal("g1", name="Cooking"), _goal("g2", name="Hosting")])
        event = _event(id="abc123", goal_ids=["g2", "g1", "gone"])

        public_event = PublicEvent.from_event(event, tree)

        assert public_event.goal_ids == ["g2", "g1", "gone"]
        assert public_event.goal_names == ["Hosting", "Cooking", "(unknown goal gone)"]

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

    def test_fills_in_goals_and_effective_fields_from_the_event_label(self, monkeypatch):
        # An event written before goals has only a label: it's read as
        # serving the goal that owns it.
        client = _fake_client(monkeypatch)
        _fake_goals(monkeypatch, _goal("g1", label_id="label-1", priority=0, fixed_time=True))
        client.list_events.return_value = [_event(id="abc123", event_label_id="label-1")]

        result = server.list_events(
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC), datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        )

        assert result[0].goal_ids == ["g1"]
        assert result[0].goal_names == ["Focus"]
        assert result[0].effective_priority == 0
        assert result[0].effective_is_fixed_time is True
        # The event's own fields stay unset, so sending it back to
        # update_event doesn't copy the goal's values onto it.
        assert result[0].priority is None
        assert result[0].is_fixed_time is None
        assert result[0].min_duration is None

    def test_events_own_values_win_over_its_goals(self, monkeypatch):
        client = _fake_client(monkeypatch)
        _fake_goals(monkeypatch, _goal("g1", priority=0, fixed_time=True))
        client.list_events.return_value = [
            _event(id="abc123", goal_ids=["g1"], priority=3, is_fixed_time=False)
        ]

        result = server.list_events(
            datetime(2026, 1, 1, 0, 0, tzinfo=UTC), datetime(2026, 1, 2, 0, 0, tzinfo=UTC)
        )

        assert result[0].effective_priority == 3
        assert result[0].effective_is_fixed_time is False


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

    def test_fills_in_effective_fields_from_the_events_goal(self, monkeypatch):
        client = _fake_client(monkeypatch)
        _fake_goals(monkeypatch, _goal("g1", priority=0, fixed_time=True))
        client.get_event.return_value = _event(id="abc123", goal_ids=["g1"])

        result = server.get_event("abc123")

        assert result.effective_priority == 0
        assert result.effective_is_fixed_time is True
        assert result.priority is None
        assert result.is_fixed_time is None


class TestUpdateEvent:
    def test_delegates_to_calendar_client(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        updated = _event(id="abc123", summary="Renamed")
        reallocating_calendar.update_event.return_value = [updated]
        public_event = _public_event(id="abc123", summary="Renamed")

        result = server.update_event(public_event)

        assert result == [PublicEvent.from_event(updated)]
        (call_updated_event, call_options), _ = reallocating_calendar.update_event.call_args
        assert call_updated_event == public_event.to_event()
        assert call_options == ReallocationOptions()

    def test_result_includes_every_affected_event(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        updated = _event(id="abc123")
        shrunk = _event(id="def456", summary="Shrunk")
        reallocating_calendar.update_event.return_value = [updated, shrunk]

        result = server.update_event(_public_event(id="abc123"))

        assert {e.id for e in result} == {"abc123", "def456"}

    def test_fills_in_effective_fields_from_the_events_goal(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        _fake_goals(monkeypatch, _goal("g1", priority=0))
        reallocating_calendar.update_event.return_value = [_event(id="abc123", goal_ids=["g1"])]

        result = server.update_event(_public_event(id="abc123"))

        assert result[0].effective_priority == 0
        assert result[0].priority is None

    def test_refuses_goal_ids_that_arent_goals(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        _fake_goals(monkeypatch, _goal("g7k2qp", name="Cooking"))

        with pytest.raises(ToolError, match=r"'g7k2qq' isn't a goal; did you mean g7k2qp \(Cooking\)"):
            server.update_event(_public_event(id="abc123", goal_ids=["g7k2qq"]))

        reallocating_calendar.update_event.assert_not_called()

    def test_an_event_can_keep_a_deleted_goal_it_already_has(self, monkeypatch):
        client = _fake_client(monkeypatch)
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        _fake_goals(monkeypatch, _goal("g1", name="Oops", status="deleted"), _goal("g2"))
        client.get_event.return_value = _event(id="abc123", goal_ids=["g1"])
        reallocating_calendar.update_event.return_value = [_event(id="abc123", goal_ids=["g1"])]

        server.update_event(_public_event(id="abc123", goal_ids=["g1"], summary="Renamed"))

        reallocating_calendar.update_event.assert_called_once()
        client.get_event.return_value = _event(id="abc123", goal_ids=["g2"])
        with pytest.raises(ToolError, match="Deleted goals"):
            server.update_event(_public_event(id="abc123", goal_ids=["g2", "g1"]))

    def test_wraps_value_error_as_tool_error(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        reallocating_calendar.update_event.side_effect = ValueError(
            "updated_event.id is required to update an event with reallocation"
        )

        with pytest.raises(ToolError):
            server.update_event(_public_event(id="abc123"))


class TestCreateEvent:
    def test_delegates_to_calendar_client(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        created = _event(id="abc123")
        reallocating_calendar.create_event.return_value = [created]
        new_public_event = _public_event()

        result = server.create_event(new_public_event)

        assert result == [PublicEvent.from_event(created)]
        (call_new_event, call_options), _ = reallocating_calendar.create_event.call_args
        assert call_new_event == new_public_event.to_event()
        assert call_options == ReallocationOptions()

    def test_result_includes_every_affected_event(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        created = _event(id="abc123")
        shrunk = _event(id="def456", summary="Shrunk")
        reallocating_calendar.create_event.return_value = [created, shrunk]

        result = server.create_event(_public_event())

        assert {e.id for e in result} == {"abc123", "def456"}

    def test_includes_cancelled_events_marked_is_cancelled(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        created = _event(id="abc123")
        cancelled = _event(id="def456", status="cancelled", summary="Old meeting")
        reallocating_calendar.create_event.return_value = [created, cancelled]

        result = server.create_event(_public_event())

        assert [e.id for e in result] == ["abc123", "def456"]
        cancelled_public_event = result[1]
        assert cancelled_public_event.is_cancelled is True
        assert cancelled_public_event.summary == "Old meeting"

    def test_wraps_value_error_as_tool_error(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        reallocating_calendar.create_event.side_effect = ValueError("bad input")

        with pytest.raises(ToolError):
            server.create_event(_public_event())

    def test_refuses_goal_ids_that_arent_goals(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        _fake_goals(monkeypatch, _goal("g1", name="Cooking"))

        with pytest.raises(ToolError, match="'cooking' isn't a goal; did you mean g1 \\(Cooking\\)"):
            server.create_event(_public_event(goal_ids=["cooking"]))

        reallocating_calendar.create_event.assert_not_called()

    def test_refuses_a_deleted_goal(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        _fake_goals(monkeypatch, _goal("g1", name="Oops", status="deleted"))

        with pytest.raises(ToolError, match="Deleted goals can't be given to an event"):
            server.create_event(_public_event(goal_ids=["g1"]))

        reallocating_calendar.create_event.assert_not_called()

    def test_passes_valid_goal_ids_through(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        _fake_goals(monkeypatch, _goal("g1"))
        reallocating_calendar.create_event.return_value = [_event(id="abc123", goal_ids=["g1"])]

        result = server.create_event(_public_event(goal_ids=["g1"]))

        (call_new_event, _options), _ = reallocating_calendar.create_event.call_args
        assert call_new_event.goal_ids == ["g1"]
        assert result[0].goal_names == ["Focus"]


class TestDeleteEvent:
    def test_delegates_to_calendar_client(self, monkeypatch):
        client = _fake_client(monkeypatch)
        client.update_event.return_value = _event(id="abc123", status="cancelled")

        result = server.delete_event("abc123")

        sent_event = client.update_event.call_args[0][0]
        assert sent_event.id == "abc123"
        assert sent_event.status == "cancelled"
        client.delete_event.assert_not_called()
        assert len(result) == 1
        assert result[0].id == "abc123"
        assert result[0].is_cancelled is True


class TestGetGoals:
    def test_delegates_to_goals(self, monkeypatch):
        store = _fake_goals(monkeypatch)
        goal_list = GoalList(goals=[], label_slots_used=0)
        store.get_goals.return_value = goal_list

        assert server.get_goals(["completed"]) is goal_list
        store.get_goals.assert_called_once_with(["completed"])

    def test_wraps_errors_as_tool_errors(self, monkeypatch):
        store = _fake_goals(monkeypatch)
        store.get_goals.side_effect = ValueError("broken tab")

        with pytest.raises(ToolError, match="broken tab"):
            server.get_goals()


class TestCreateGoal:
    def test_delegates_to_goals(self, monkeypatch):
        store = _fake_goals(monkeypatch)
        goal_list = GoalList(goals=[], label_slots_used=1)
        store.create_goal.return_value = goal_list
        new_goal = Goal(name="Cooking", parent_id="g1")

        assert server.create_goal(new_goal) is goal_list
        store.create_goal.assert_called_once_with(new_goal)

    @pytest.mark.parametrize("error", [ValueError("too many labels"), EventLabelConflictError("stale etag")])
    def test_wraps_errors_as_tool_errors(self, monkeypatch, error):
        store = _fake_goals(monkeypatch)
        store.create_goal.side_effect = error

        with pytest.raises(ToolError):
            server.create_goal(Goal(name="Cooking"))


class TestUpdateGoal:
    def test_delegates_to_goals(self, monkeypatch):
        store = _fake_goals(monkeypatch)
        goal_list = GoalList(goals=[], label_slots_used=0)
        store.update_goal.return_value = goal_list
        goal = Goal(id="g1", status="inactive")

        assert server.update_goal(goal) is goal_list
        store.update_goal.assert_called_once_with(goal, ())

    def test_passes_clear_fields(self, monkeypatch):
        store = _fake_goals(monkeypatch)
        goal = Goal(id="g1")

        server.update_goal(goal, clear_fields=["parent_id", "measure"])

        store.update_goal.assert_called_once_with(goal, ["parent_id", "measure"])

    @pytest.mark.parametrize("error", [ValueError("'g9' isn't a goal"), EventLabelConflictError("stale etag")])
    def test_wraps_errors_as_tool_errors(self, monkeypatch, error):
        store = _fake_goals(monkeypatch)
        store.update_goal.side_effect = error

        with pytest.raises(ToolError):
            server.update_goal(Goal(id="g9", name="X"))


class TestSyncGoalsFromSheet:
    def test_delegates_to_goals(self, monkeypatch):
        store = _fake_goals(monkeypatch)
        goal_list = GoalList(goals=[], label_slots_used=0)
        store.sync.return_value = goal_list

        assert server.sync_goals_from_sheet() is goal_list
        store.sync.assert_called_once_with()

    @pytest.mark.parametrize("error", [ValueError("goal 'g1' has no name"), EventLabelConflictError("stale")])
    def test_wraps_errors_as_tool_errors(self, monkeypatch, error):
        store = _fake_goals(monkeypatch)
        store.sync.side_effect = error

        with pytest.raises(ToolError):
            server.sync_goals_from_sheet()


def _fake_goal_health(monkeypatch) -> MagicMock:
    health = MagicMock()
    monkeypatch.setattr(server, "get_goal_health", lambda: health)
    return health


class TestGoalHealthTools:
    def test_measure_goals_delegates(self, monkeypatch):
        health = _fake_goal_health(monkeypatch)
        health.measure.return_value = []

        assert server.measure_goals(date(2026, 9, 20), ["g1"]) == []
        health.measure.assert_called_once_with(date(2026, 9, 20), ["g1"])

    def test_record_assessments_delegates(self, monkeypatch):
        health = _fake_goal_health(monkeypatch)
        assessment = Assessment(goal_id="g1", day=date(2026, 10, 1), rating=80, method="subjective")
        health.record_assessments.return_value = [assessment]

        assert server.record_assessments([assessment]) == [assessment]
        health.record_assessments.assert_called_once_with([assessment])

    def test_get_goal_history_delegates(self, monkeypatch):
        from datetime import date

        health = _fake_goal_health(monkeypatch)
        health.history.return_value = []

        server.get_goal_history(["g1"], date(2026, 9, 1), date(2026, 9, 30))

        health.history.assert_called_once_with(["g1"], date(2026, 9, 1), date(2026, 9, 30))

    def test_rebuild_goal_health_cache_delegates(self, monkeypatch):
        health = _fake_goal_health(monkeypatch)
        goal_list = GoalList(goals=[], label_slots_used=0)
        health.rebuild_cache.return_value = goal_list

        assert server.rebuild_goal_health_cache() is goal_list

    @pytest.mark.parametrize(
        "tool, method, args",
        [
            ("measure_goals", "measure", ()),
            ("record_assessments", "record_assessments", ([],)),
            ("get_goal_history", "history", (["g1"],)),
            ("rebuild_goal_health_cache", "rebuild_cache", ()),
        ],
    )
    def test_wrap_value_errors_as_tool_errors_and_are_tracked(self, monkeypatch, tool, method, args):
        health = _fake_goal_health(monkeypatch)
        getattr(health, method).side_effect = ValueError("'2026-13' isn't a period")
        labels = _tracked_labels(monkeypatch)

        with pytest.raises(ToolError, match="isn't a period"):
            getattr(server, tool)(*args)

        assert labels == [tool]


class TestReflectionTools:
    def _fake(self, monkeypatch) -> MagicMock:
        reflections = MagicMock()
        monkeypatch.setattr(server, "get_reflections", lambda: reflections)
        return reflections

    def test_prepare_reflection_delegates(self, monkeypatch):
        reflections = self._fake(monkeypatch)

        server.prepare_reflection(date(2026, 9, 20))

        reflections.prepare.assert_called_once_with(date(2026, 9, 20))

    def test_record_reflection_previews_by_default(self, monkeypatch):
        reflections = self._fake(monkeypatch)

        server.record_reflection(date(2026, 9, 20), [], journal="j", intentions=["i"])

        reflections.record.assert_called_once_with(
            date(2026, 9, 20), [], "j", ["i"], dry_run=True
        )

    @pytest.mark.parametrize(
        "tool, method, args",
        [
            ("prepare_reflection", "prepare", ()),
            ("record_reflection", "record", (date(2026, 9, 20), [])),
        ],
    )
    def test_wrap_value_errors_and_are_tracked(self, monkeypatch, tool, method, args):
        reflections = self._fake(monkeypatch)
        getattr(reflections, method).side_effect = ValueError("hasn't started yet")
        labels = _tracked_labels(monkeypatch)

        with pytest.raises(ToolError, match="hasn't started yet"):
            getattr(server, tool)(*args)

        assert labels == [tool]


class TestGetGoalHealth:
    def test_caches_across_calls_on_the_shared_client_and_goals(self, monkeypatch):
        client = _fake_client(monkeypatch)
        goals = MagicMock()
        monkeypatch.setattr(server, "get_goal_store", lambda: goals)
        monkeypatch.setattr(server, "_goal_health", None)

        first = server.get_goal_health()

        assert first is server.get_goal_health()
        assert first._client is client and first._goals is goals


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


class TestCompactNotes:
    def test_a_dry_run_with_decisions_plans(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)
        decisions = [EventDecision(action="cancel", event_id="e1")]

        result = server.compact_notes(decisions=decisions)

        assert result is compactor.dry_run.return_value
        compactor.dry_run.assert_called_once_with(decisions, None)

    def test_a_dry_run_passes_ignored_notes_through(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)
        decisions = [
            EventDecision(action="keep", event_id="e1", end=datetime(2026, 1, 1, 12, 15, tzinfo=UTC))
        ]

        server.compact_notes(decisions=decisions, ignore_notes=["n2"])

        compactor.dry_run.assert_called_once_with(decisions, ["n2"])

    def test_a_dry_run_with_no_decisions_records_everything_as_on_schedule(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)

        server.compact_notes()

        compactor.dry_run.assert_called_once_with([], None)

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
            server.compact_notes(decisions=[], dry_run=False)

        compactor.commit.assert_not_called()

    def test_compaction_errors_become_tool_errors(self, monkeypatch):
        compactor = _fake_compactor(monkeypatch)
        compactor.commit.side_effect = CompactionError("the notes changed")

        with pytest.raises(ToolError, match="the notes changed"):
            server.compact_notes(compaction_id="abc", dry_run=False)


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
        monkeypatch.setattr(server, "get_reallocating_calendar", lambda: MagicMock())
        monkeypatch.setattr(server, "get_calendar_client", lambda: MagicMock())
        monkeypatch.setattr(server, "get_noted_time_sheet", lambda: MagicMock())
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


class TestGetReallocatingCalendar:
    def test_caches_across_calls(self, monkeypatch):
        client = _fake_client(monkeypatch)
        goals = MagicMock()
        monkeypatch.setattr(server, "get_goal_store", lambda: goals)
        monkeypatch.setattr(server, "_reallocating_calendar", None)

        first = server.get_reallocating_calendar()
        second = server.get_reallocating_calendar()

        assert first is second
        assert isinstance(first, ReallocatingCalendar)
        assert isinstance(first._client, GoalCalendar)
        assert first._client._client is client
        assert first._client._goals is goals


class TestGetGoalStore:
    def test_caches_across_calls(self, monkeypatch):
        built = []

        def fake_build(**kwargs):
            goals = MagicMock()
            built.append(kwargs)
            return goals

        monkeypatch.setattr(server, "_goals", None)
        monkeypatch.setattr(server, "build_goals", fake_build)

        first = server.get_goal_store()
        second = server.get_goal_store()

        assert first is second
        assert len(built) == 1

    def test_counts_recent_time_up_to_the_last_compaction(self, monkeypatch):
        built = []
        journal = MagicMock()
        journal.last_stamped_now.return_value = datetime(2026, 10, 2, 21, tzinfo=timezone.utc)
        monkeypatch.setattr(server, "_goals", None)
        monkeypatch.setattr(server, "get_compaction_journal", lambda: journal)
        monkeypatch.setattr(server, "build_goals", lambda **kwargs: built.append(kwargs) or MagicMock())

        server.get_goal_store()

        assert built[0]["last_compaction"]() == datetime(2026, 10, 2, 21, tzinfo=timezone.utc)


class TestGetCompactionStatus:
    def test_reports_the_last_compaction_and_latest_compacted_note(self, monkeypatch):
        journal = MagicMock()
        journal.last_stamped_now.return_value = datetime(2026, 10, 2, 21, tzinfo=timezone.utc)
        notes = MagicMock()
        latest = NotedTime(timestamp=datetime(2026, 10, 2, 20, tzinfo=timezone.utc), description="Done", compaction_id="c1")
        notes.read_with_latest_compacted.return_value = ([], latest)
        monkeypatch.setattr(server, "get_compaction_journal", lambda: journal)
        monkeypatch.setattr(server, "get_noted_time_sheet", lambda: notes)

        status = server.get_compaction_status()

        assert status == server.CompactionStatus(
            last_compaction=datetime(2026, 10, 2, 21, tzinfo=timezone.utc), latest_compacted_note=latest
        )

    def test_is_empty_before_any_compaction(self, monkeypatch):
        journal = MagicMock()
        journal.last_stamped_now.return_value = None
        notes = MagicMock()
        notes.read_with_latest_compacted.return_value = ([], None)
        monkeypatch.setattr(server, "get_compaction_journal", lambda: journal)
        monkeypatch.setattr(server, "get_noted_time_sheet", lambda: notes)

        assert server.get_compaction_status() == server.CompactionStatus()


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
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        reallocating_calendar.update_event.return_value = [_event(id="abc123")]
        labels = _tracked_labels(monkeypatch)

        server.update_event(_public_event(id="abc123"))

        assert labels == ["update_event"]

    def test_create_event(self, monkeypatch):
        reallocating_calendar = _fake_reallocating_calendar(monkeypatch)
        reallocating_calendar.create_event.return_value = [_event(id="abc123")]
        labels = _tracked_labels(monkeypatch)

        server.create_event(_public_event())

        assert labels == ["create_event"]

    def test_delete_event(self, monkeypatch):
        client = _fake_client(monkeypatch)
        client.update_event.return_value = _event(id="abc123", status="cancelled")
        labels = _tracked_labels(monkeypatch)

        server.delete_event("abc123")

        assert labels == ["delete_event"]

    @pytest.mark.parametrize(
        "tool, args, method",
        [
            ("get_goals", (), "get_goals"),
            ("create_goal", (Goal(name="Cooking"),), "create_goal"),
            ("update_goal", (Goal(id="g1"),), "update_goal"),
            ("sync_goals_from_sheet", (), "sync"),
        ],
    )
    def test_goal_tools(self, monkeypatch, tool, args, method):
        store = _fake_goals(monkeypatch)
        getattr(store, method).return_value = GoalList(goals=[], label_slots_used=0)
        labels = _tracked_labels(monkeypatch)

        getattr(server, tool)(*args)

        assert labels == [tool]

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
            goal_ids=["g1"],
        )
        tree = GoalTree([Goal(id="g1", name="Work", status="active", label_id="l1")])

        public = server.PublicRecurrence.from_event(series, tree)

        assert public.start.isoformat() == "2026-10-05T09:00:00-04:00"
        assert public.schedule == "Every week on Mon"
        assert public.goal_names == ["Work"]
        assert public.to_event().recurrence == ["RRULE:FREQ=WEEKLY;BYDAY=MO"]

    def test_update_recurrence_reports_bad_rules_as_tool_errors(self, monkeypatch):
        recurrences = MagicMock()
        recurrences.update.side_effect = ValueError("A series needs exactly one RRULE line")
        monkeypatch.setattr(server, "get_recurrences", lambda: recurrences)
        monkeypatch.setattr(server, "_check_goal_ids", lambda *args, **kwargs: None)

        with pytest.raises(ToolError, match="exactly one RRULE"):
            server.update_recurrence(server.PublicRecurrence(id="s1", rules=["RRULE:FREQ=DAILY"] * 2))


class TestSetTimeZone:
    def test_sets_both_calendars_time_zones(self, monkeypatch):
        client = _fake_client(monkeypatch)
        client.set_time_zone.return_value = ZoneInfo("America/New_York")
        health = _fake_goal_health(monkeypatch)

        assert server.set_time_zone("America/New_York") == "America/New_York"

        client.set_time_zone.assert_called_once_with("America/New_York")
        health.health_calendar.assert_called_once_with(create=False)
        health.health_calendar.return_value.set_time_zone.assert_called_once_with("America/New_York")

    def test_without_a_goal_health_calendar_sets_only_the_main_one(self, monkeypatch):
        client = _fake_client(monkeypatch)
        client.set_time_zone.return_value = ZoneInfo("America/New_York")
        _fake_goal_health(monkeypatch).health_calendar.return_value = None

        assert server.set_time_zone("America/New_York") == "America/New_York"

    def test_reports_a_bad_name_as_a_tool_error(self, monkeypatch):
        _fake_client(monkeypatch).set_time_zone.side_effect = ValueError("'Nowhere' isn't a time zone")
        health = _fake_goal_health(monkeypatch)

        with pytest.raises(ToolError, match="isn't a time zone"):
            server.set_time_zone("Nowhere")

        health.health_calendar.assert_not_called()


_READ_ONLY_TOOLS = {
    "list_events",
    "get_event",
    "get_recurrence",
    "get_goals",
    "measure_goals",
    "get_goal_history",
    "prepare_reflection",
    "get_compaction_status",
    "get_notes",
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
