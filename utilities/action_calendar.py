"""Ties events to actions: fills in what an event inherits from its
actions when it's read, and derives its event label from them when it's
written.

calendar_clients/google_calendar.py's CalendarClient is a thin wrapper
around the Calendar API -- Event.action_ids, Event.event_label_id and
Event.action_priority are unrelated fields to it. utilities/actions.py's
`Actions` knows the actions and their groups. `ActionCalendar` here is
the glue between them:

- **On read** (`list_events`/`get_event`, or `fill_in_from_actions` for
  events read some other way): an event never given actions, only a
  label -- one picked by hand in Calendar, say -- is read as doing the
  action that holds that label, marked `actions_from_label` so the
  inference can be told apart from actions the event was given, and is
  never written back as if it were. Its `action_priority` is filled in
  from all its actions alike: the highest (lowest-numbered) priority
  among them, each action's own or its nearest group's, so
  `Event.effective_priority` -- what compaction and the time summaries read --
  falls back to it. The event's own priority is never touched.
- **On write** (`create_event`/`update_event`), whenever `action_ids` is
  being written: `event_label_id` is the first action's label, if the
  calendar has it (an active action always does; a proposed one does
  while there's room -- see utilities/actions.py), or none. Calendar
  rejects *inserting* an event with a label it doesn't have, so an
  insert checks; an update keeps the event's label if it's already the
  first action's own, since Calendar accepts re-sending an event's
  existing label even after the label is removed, so an archived
  action's events go back to its color if it's made active again.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from calendar_clients.google_calendar import CalendarClient, Event, EventLabel
from utilities.actions import Actions, ActionTree


class ActionCalendar:
    """Wraps a CalendarClient with an Actions -- see the module docstring."""

    def __init__(self, client: CalendarClient, actions: Actions) -> None:
        self._client = client
        self._actions = actions

    def list_events(self, time_min: datetime, time_max: datetime) -> list[Event]:
        return fill_in_from_actions(self._client.list_events(time_min, time_max), self._actions.tree())

    def get_event(self, event_id: str) -> Event:
        return fill_in_from_actions([self._client.get_event(event_id)], self._actions.tree())[0]

    def create_event(self, event: Event) -> Event:
        return self._client.create_event(self._with_label(event, inserting=True))

    def update_event(self, event: Event) -> Event:
        return self._client.update_event(self._with_label(event, inserting=False))

    def import_event(self, event: Event, ical_uid: str) -> Event:
        return self._client.import_event(self._with_label(event, inserting=True), ical_uid)

    def list_event_labels(self) -> tuple[list[EventLabel], str]:
        return self._client.list_event_labels()

    def _with_label(self, event: Event, *, inserting: bool) -> Event:
        tree = self._actions.tree()
        primary = tree.by_id.get(event.action_ids[0]) if event.action_ids and not event.actions_from_label else None
        held: set[str] | None = None
        if primary is not None and primary.status == "proposed" and primary.label_id:
            # Whether a proposed action holds its label depends on the room
            # left, so ask the calendar.
            held = {label.id for label in self._client.list_event_labels()[0]}
        return with_action_label(event, tree, inserting=inserting, held=held)


def fill_in_from_actions(events: list[Event], tree: ActionTree) -> list[Event]:
    """`events`, each with its action_ids (from its label, for an event
    never given actions) and action_priority filled in. Never mutates
    `events` themselves."""
    return [_fill_in(event, tree) for event in events]


def _fill_in(event: Event, tree: ActionTree) -> Event:
    action_ids = event.action_ids
    if action_ids is None:
        owner = tree.action_for_label(event.event_label_id)
        if owner is None:
            return event
        event = replace(event, actions_from_label=True)
        action_ids = [owner.id]
    if not action_ids:
        return replace(event, action_ids=action_ids)
    priorities = [p for p in (tree.priority(i) for i in action_ids) if p is not None]
    return replace(event, action_ids=action_ids, action_priority=min(priorities, default=None))


def with_action_label(
    event: Event, tree: ActionTree, *, inserting: bool, held: set[str] | None = None
) -> Event:
    """`event` with `event_label_id` derived from its first action (see the
    module docstring), or unchanged if it isn't writing `action_ids`. On
    an update, "no label" is sent as "" (which removes one); on an insert,
    as None. `held`: the calendar's label ids, if they've been read --
    without them, only an active action's label counts as held."""
    if event.action_ids is None or event.actions_from_label:
        return event
    none = None if inserting else ""
    primary = tree.by_id.get(event.action_ids[0]) if event.action_ids else None
    if primary is None or not primary.label_id:
        return replace(event, event_label_id=none)
    if not inserting and event.event_label_id == primary.label_id:
        return event
    holds = primary.label_id in held if held is not None else primary.status == "active"
    return replace(event, event_label_id=primary.label_id if holds else none)
