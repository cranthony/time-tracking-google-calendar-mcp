"""Ties events to goals: fills in what an event inherits from its goals
when it's read, and derives its event label from them when it's written.

calendar_clients/google_calendar.py's CalendarClient is a thin wrapper
around the Calendar API -- Event.goal_ids, Event.event_label_id and
Event.goal_priority are unrelated fields to it. utilities/goals.py's
`Goals` knows the goal tree. `GoalCalendar` here is the glue between
them, replacing utilities/label_priority_calendar.py's
LabelPriorityCalendar:

- **On read** (`list_events`/`get_event`, or `fill_in_from_goals` for
  events read some other way): an event written before goals existed has
  no goal_ids, only a label -- it's read as serving the goal that owns
  that label, marked `goals_from_label` so the inference can be told
  apart from goals the event was given, and is never written back as
  if it were. Its `goal_priority`/`goal_is_fixed_time` are filled in from
  its primary goal (or that goal's nearest ancestor that sets one), so
  `Event.effective_priority`/`effective_is_fixed_time` -- all reallocation
  and compaction read -- fall back to them. The event's own
  priority/is_fixed_time are never touched.
- **On write** (`create_event`/`update_event`), whenever `goal_ids` is
  being written: `event_label_id` is derived from the primary goal.
  Calendar rejects *inserting* an event with a label it doesn't have
  (docs/goals-design.md section 5), so an insert uses the label of the
  nearest *active* goal in the primary goal's chain, or none. An update
  keeps the event's label if it's already the primary goal's own --
  Calendar accepts re-sending an event's existing label even after the
  label is removed, so an inactive goal's events go back to its color
  when it's reactivated -- and otherwise derives it the same way.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from calendar_clients.google_calendar import CalendarClient, Event, EventLabel
from utilities.goals import Goals, GoalTree


class GoalCalendar:
    """Wraps a CalendarClient with a Goals -- see the module docstring."""

    def __init__(self, client: CalendarClient, goals: Goals) -> None:
        self._client = client
        self._goals = goals

    def list_events(self, time_min: datetime, time_max: datetime) -> list[Event]:
        return fill_in_from_goals(self._client.list_events(time_min, time_max), self._goals.tree())

    def get_event(self, event_id: str) -> Event:
        return fill_in_from_goals([self._client.get_event(event_id)], self._goals.tree())[0]

    def create_event(self, event: Event) -> Event:
        return self._client.create_event(with_goal_label(event, self._goals.tree(), inserting=True))

    def update_event(self, event: Event) -> Event:
        return self._client.update_event(with_goal_label(event, self._goals.tree(), inserting=False))

    def list_event_labels(self) -> tuple[list[EventLabel], str]:
        return self._client.list_event_labels()


def fill_in_from_goals(events: list[Event], tree: GoalTree) -> list[Event]:
    """`events`, each with its goal_ids (from its label, for an event
    written before goals) and goal_priority/goal_is_fixed_time filled in.
    Never mutates `events` themselves."""
    return [_fill_in(event, tree) for event in events]


def _fill_in(event: Event, tree: GoalTree) -> Event:
    goal_ids = event.goal_ids
    if goal_ids is None:
        owner = tree.goal_for_label(event.event_label_id)
        if owner is None:
            return event
        event = replace(event, goals_from_label=True)
        goal_ids = [owner.id]
    if not goal_ids:
        return replace(event, goal_ids=goal_ids)
    return replace(
        event,
        goal_ids=goal_ids,
        goal_priority=tree.priority(goal_ids[0]),
        goal_is_fixed_time=tree.fixed_time(goal_ids[0]),
    )


def with_goal_label(event: Event, tree: GoalTree, *, inserting: bool) -> Event:
    """`event` with `event_label_id` derived from its goals (see the module
    docstring), or unchanged if it isn't writing `goal_ids`. On an update,
    "no label" is sent as "" (which removes one); on an insert, as None."""
    if event.goal_ids is None or event.goals_from_label:
        return event
    none = None if inserting else ""
    if not event.goal_ids:
        return replace(event, event_label_id=none)
    primary = tree.by_id.get(event.goal_ids[0])
    if primary is None:
        return replace(event, event_label_id=none)
    if not inserting and primary.label_id and event.event_label_id == primary.label_id:
        return event
    return replace(event, event_label_id=tree.active_label_id(primary.id) or none)
