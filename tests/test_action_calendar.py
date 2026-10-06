from datetime import datetime, timezone
from unittest.mock import MagicMock

from calendar_clients.google_calendar import Event, EventLabel
from utilities import priority_labels
from utilities.action_calendar import ActionCalendar, fill_in_from_actions, with_action_label
from utilities.action_groups import ActionGroup, GroupTree
from utilities.actions import Action, ActionTree

_START = datetime(2026, 10, 5, 9, tzinfo=timezone.utc)
_END = datetime(2026, 10, 5, 10, tzinfo=timezone.utc)


def _tree() -> ActionTree:
    return ActionTree(
        [
            Action(id="cook", name="Cook", status="active", label_id="l-cook", group_id="food"),
            Action(id="idea", name="Juggle", status="proposed", label_id="l-idea", priority=3),
            Action(id="old", name="Old", status="archived", label_id="l-old"),
        ],
        GroupTree([ActionGroup(id="food", name="Food", priority=1)]),
    )


def _event(**fields) -> Event:
    return Event(id="e1", summary="x", start=_START, end=_END, **fields)


class TestFillIn:
    def test_takes_the_highest_priority_among_the_actions_and_their_groups(self):
        (event,) = fill_in_from_actions([_event(action_ids=["idea", "cook"])], _tree())

        assert event.action_priority == 1  # Cook's group's.
        assert event.effective_priority == 1

    def test_an_event_without_actions_is_read_as_doing_its_labels(self):
        (event,) = fill_in_from_actions([_event(event_label_id="l-cook")], _tree())

        assert (event.action_ids, event.actions_from_label) == (["cook"], True)

    def test_leaves_alone_an_event_with_none_or_an_unknown_label(self):
        bare = _event(event_label_id="someone-elses")

        assert fill_in_from_actions([bare], _tree()) == [bare]
        assert fill_in_from_actions([_event(action_ids=[])], _tree())[0].action_ids == []


class TestWithActionLabel:
    def test_an_active_first_action_sets_the_label(self):
        assert with_action_label(_event(action_ids=["cook", "idea"]), _tree(), inserting=True).event_label_id == "l-cook"

    def test_an_archived_action_gives_no_label(self):
        assert with_action_label(_event(action_ids=["old"]), _tree(), inserting=True).event_label_id is None
        assert with_action_label(_event(action_ids=["old"]), _tree(), inserting=False).event_label_id == ""

    def test_an_update_keeps_the_label_the_event_already_has_from_its_action(self):
        event = _event(action_ids=["old"], event_label_id="l-old")

        assert with_action_label(event, _tree(), inserting=False).event_label_id == "l-old"

    def test_a_proposed_action_labels_it_only_if_the_calendar_holds_the_label(self):
        event = _event(action_ids=["idea"])

        assert with_action_label(event, _tree(), inserting=True, held={"l-idea"}).event_label_id == "l-idea"
        assert with_action_label(event, _tree(), inserting=True, held=set()).event_label_id is None

    def test_untouched_when_not_writing_actions_or_inferring_them(self):
        assert with_action_label(_event(event_label_id="x"), _tree(), inserting=False).event_label_id == "x"
        inferred = _event(action_ids=["cook"], actions_from_label=True)
        assert with_action_label(inferred, _tree(), inserting=False) == inferred


class TestActionCalendar:
    def _calendar(self, labels=()):
        client = MagicMock()
        client.create_event.side_effect = lambda event: event
        client.update_event.side_effect = lambda event: event
        client.list_event_labels.return_value = (list(labels), "etag")
        actions = MagicMock()
        actions.tree.return_value = _tree()
        return ActionCalendar(client, actions), client

    def test_reads_fill_in_and_writes_derive_the_label(self):
        calendar, client = self._calendar()
        client.list_events.return_value = [_event(action_ids=["cook"])]

        assert calendar.list_events(_START, _END)[0].action_priority == 1
        assert calendar.create_event(_event(action_ids=["cook"])).event_label_id == "l-cook"
        client.list_event_labels.assert_not_called()  # An active action always holds its label.

    def test_a_proposed_actions_label_is_checked_against_the_calendar(self):
        calendar, client = self._calendar([EventLabel(id="l-idea", name="Juggle", background_color="#123456")])

        assert calendar.create_event(_event(action_ids=["idea"])).event_label_id == "l-idea"
        client.list_event_labels.assert_called_once()


class TestPriorityLabels:
    """An event with no action's label takes its priority's (see
    utilities/priority_labels.py)."""

    def _calendar(self, current=None):
        client = MagicMock()
        client.create_event.side_effect = lambda event: event
        client.update_event.side_effect = lambda event: event
        client.get_event.return_value = current or _event()
        actions = MagicMock()
        actions.tree.return_value = _tree()
        return ActionCalendar(client, actions), client, actions

    def test_an_event_without_actions_takes_its_priority_label(self):
        calendar, _, actions = self._calendar()

        assert calendar.create_event(_event(priority=1)).event_label_id == priority_labels.label_id(1)
        actions.ensure_priority_labels.assert_called_once()  # Made before any is used.

    def test_without_a_priority_it_takes_the_default_ones(self):
        calendar, _, _ = self._calendar()

        assert calendar.create_event(_event()).event_label_id == priority_labels.label_id(2)

    def test_an_action_that_doesnt_hold_a_label_gives_its_priority(self):
        calendar, client, _ = self._calendar()
        client.list_event_labels.return_value = ([], "etag")  # No room for Juggle's.

        assert calendar.create_event(_event(action_ids=["idea"])).event_label_id == priority_labels.label_id(3)

    def test_clearing_its_actions_gives_it_its_priority_label(self):
        calendar, _, _ = self._calendar()

        assert calendar.update_event(_event(action_ids=[], priority=0)).event_label_id == priority_labels.label_id(0)

    def test_a_new_priority_relabels_an_event_without_an_actions_label(self):
        calendar, client, _ = self._calendar(current=_event(event_label_id=priority_labels.label_id(2)))

        sent = calendar.update_event(Event(id="e1", priority=0))

        assert sent.event_label_id == priority_labels.label_id(0)
        client.get_event.assert_called_once_with("e1")

    def test_a_new_priority_keeps_an_actions_label(self):
        calendar, _, _ = self._calendar(current=_event(action_ids=["cook"], event_label_id="l-cook"))

        sent = calendar.update_event(Event(id="e1", priority=0))

        assert sent.event_label_id is None  # Not written: it keeps l-cook.

    def test_one_inferred_from_its_label_keeps_it_too(self):
        calendar, _, _ = self._calendar(current=_event(event_label_id="l-cook"))

        assert calendar.update_event(Event(id="e1", priority=0)).event_label_id is None

    def test_an_update_writing_neither_actions_nor_a_priority_is_untouched(self):
        calendar, client, actions = self._calendar()

        assert calendar.update_event(Event(id="e1", summary="Renamed")).event_label_id is None
        client.get_event.assert_not_called()
        actions.ensure_priority_labels.assert_not_called()
