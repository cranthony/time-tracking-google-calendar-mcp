from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from calendar_clients.google_calendar import Event
from utilities.goal_calendar import GoalCalendar, fill_in_from_goals, with_goal_label
from utilities.goal_sheet import Goal
from utilities.goals import GoalTree

UTC = timezone.utc
_START = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)


def _event(**overrides) -> Event:
    fields = {"id": "e1", "summary": "Dinner", "start": _START, "end": _START + timedelta(hours=1)}
    fields.update(overrides)
    return Event(**fields)


def _tree() -> GoalTree:
    """Hosting (active) > Cooking (inactive) > Tofu (inactive); Reading
    (inactive, top-level)."""
    return GoalTree([
        Goal(id="host", name="Hosting", status="active", label_id="l-host", priority=1, fixed_time=True),
        Goal(id="cook", name="Cooking", status="inactive", label_id="l-cook", parent_id="host"),
        Goal(id="tofu", name="Tofu", status="inactive", label_id="l-tofu", parent_id="cook", priority=3),
        Goal(id="read", name="Reading", status="inactive", label_id="l-read"),
    ])


class TestFillInFromGoals:
    def test_an_event_with_only_a_label_serves_the_goal_that_owns_it(self):
        (event,) = fill_in_from_goals([_event(event_label_id="l-cook")], _tree())

        assert event.goal_ids == ["cook"]
        assert event.goals_from_label
        assert (event.goal_priority, event.goal_is_fixed_time) == (1, True)  # from Hosting

    def test_an_events_own_goals_arent_marked_inferred(self):
        (event,) = fill_in_from_goals([_event(goal_ids=["host"], event_label_id="l-cook")], _tree())

        assert event.goal_ids == ["host"]
        assert not event.goals_from_label

    def test_writing_back_an_inferred_event_never_stores_its_goals(self):
        (event,) = fill_in_from_goals([_event(event_label_id="l-cook")], _tree())

        written = with_goal_label(event, _tree(), inserting=False)

        assert written.event_label_id == "l-cook"  # its label, untouched
        private = written.to_api_body().get("extendedProperties", {}).get("private", {})
        assert not any(key.endswith("goal_ids") for key in private)

    def test_inherits_from_the_primary_goal_only(self):
        (event,) = fill_in_from_goals([_event(goal_ids=["tofu", "host"])], _tree())

        assert (event.goal_priority, event.goal_is_fixed_time) == (3, True)

    def test_the_events_own_values_still_win(self):
        (event,) = fill_in_from_goals([_event(goal_ids=["host"], priority=2, is_fixed_time=False)], _tree())

        assert (event.effective_priority, event.effective_is_fixed_time) == (2, False)

    @pytest.mark.parametrize(
        "event",
        [_event(), _event(event_label_id="not-a-goals-label"), _event(goal_ids=[], event_label_id="l-host")],
    )
    def test_leaves_events_without_goals_alone(self, event):
        (filled,) = fill_in_from_goals([event], _tree())

        assert (filled.goal_priority, filled.goal_is_fixed_time) == (None, None)
        assert filled.goal_ids == event.goal_ids

    def test_never_mutates_its_input(self):
        event = _event(event_label_id="l-host")

        fill_in_from_goals([event], _tree())

        assert event.goal_ids is None


class TestWithGoalLabel:
    def test_leaves_a_write_that_doesnt_touch_goals_alone(self):
        event = _event(event_label_id="anything")

        assert with_goal_label(event, _tree(), inserting=False) is event

    def test_an_active_primary_goal_gives_its_own_label(self):
        event = with_goal_label(_event(goal_ids=["host"]), _tree(), inserting=True)

        assert event.event_label_id == "l-host"

    @pytest.mark.parametrize("inserting", [True, False])
    def test_an_inactive_goal_uses_its_nearest_active_ancestors_label(self, inserting):
        event = with_goal_label(_event(goal_ids=["tofu"]), _tree(), inserting=inserting)

        assert event.event_label_id == "l-host"

    def test_an_update_keeps_an_inactive_goals_own_label_the_event_already_has(self):
        # Calendar accepts re-sending an event's existing (removed) label,
        # and reactivating the goal then brings its color back.
        event = with_goal_label(
            _event(goal_ids=["tofu"], event_label_id="l-tofu"), _tree(), inserting=False
        )

        assert event.event_label_id == "l-tofu"

    def test_an_insert_never_uses_an_inactive_goals_label(self):
        # Calendar rejects inserting an event with a label it doesn't have.
        event = with_goal_label(
            _event(goal_ids=["tofu"], event_label_id="l-tofu"), _tree(), inserting=True
        )

        assert event.event_label_id == "l-host"

    @pytest.mark.parametrize(
        "inserting, expected",
        [(True, None), (False, "")],  # "" removes a label on update
    )
    @pytest.mark.parametrize("goal_ids", [[], ["read"], ["unknown"]])
    def test_no_active_goal_means_no_label(self, inserting, expected, goal_ids):
        event = with_goal_label(
            _event(goal_ids=goal_ids, event_label_id="l-host"), _tree(), inserting=inserting
        )

        assert event.event_label_id == expected


class TestGoalCalendar:
    def _calendar(self):
        client = MagicMock()
        client.create_event.side_effect = lambda event: event
        client.update_event.side_effect = lambda event: event
        goals = MagicMock()
        goals.tree.return_value = _tree()
        return GoalCalendar(client, goals), client

    def test_reads_fill_in_goals(self):
        calendar, client = self._calendar()
        client.list_events.return_value = [_event(event_label_id="l-host")]
        client.get_event.return_value = _event(event_label_id="l-host")

        assert calendar.list_events(_START, _START)[0].goal_ids == ["host"]
        assert calendar.get_event("e1").goal_ids == ["host"]

    def test_writes_derive_the_label(self):
        calendar, client = self._calendar()

        calendar.create_event(_event(id=None, goal_ids=["cook"]))
        calendar.update_event(_event(goal_ids=[]))

        assert client.create_event.call_args.args[0].event_label_id == "l-host"
        assert client.update_event.call_args.args[0].event_label_id == ""
