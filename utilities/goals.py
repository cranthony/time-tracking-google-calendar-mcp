"""Goals: what the user is working toward, which replaced event labels.

utilities/goal_sheet.py's `GoalSheet` stores them (one row each, in the
calendar's metadata spreadsheet); calendar_clients/google_calendar.py's
`CalendarClient` holds the calendar's event labels. `Goals` here is the
application-level policy tying the two together -- the same role
utilities/event_labels.py's `EventLabels` had:

- Each goal reserves one label id. **A goal occupies its label exactly
  while it's active**: syncing makes the calendar's labels the active
  goals' (plus any unnamed labels, which are Calendar's own default
  colors and always left alone). Moving a goal to any other status
  (proposed, inactive, completed, archived or deleted -- see
  utilities/goal_sheet.py's GOAL_STATUSES) removes its label, freeing one
  of the calendar's 200 slots, but keeps its history; making it active
  again re-adds the label under the same id, which Calendar's events
  still point at.
- Goals form a tree. A goal without its own priority/fixed_time inherits
  its nearest ancestor's, and events inherit their primary goal's (see
  utilities/goal_calendar.py).
- The first time a calendar's `Goals` is built, its goals are migrated
  from its event labels tab (or, without one, its named labels): one
  active, top-level goal per label, keeping the label's id, so no event
  needs rewriting.

The sheet is authoritative: a hand edit to it reaches the calendar on the
next write or `sync`. See docs/goals-design.md sections 3-5.
"""

from __future__ import annotations

import difflib
import secrets
import uuid
from collections.abc import Callable, Collection
from dataclasses import astuple, dataclass, fields, replace
from datetime import date, datetime, timedelta
from typing import Any

from calendar_clients.google_calendar import CalendarClient, EventLabel as RawEventLabel, color_for_priority
from calendar_clients.google_sheets import SheetsClient, TabRange
from utilities import calendar_metadata_sheet
from utilities.goal_measures import DEFAULT_MEASURE, MEASURE_SHAPE_PROBLEM, measure_problems
from utilities.sleep_days import current_day_from
from utilities.goal_sheet import GOAL_STATUSES, Goal, GoalSheet

MAX_LABELS = 200
"""The most event labels a calendar can have (named or not)."""

MAX_NAME_LENGTH = 50
"""A goal's name is its label's name, which Calendar caps at 50."""

_LABEL_ID_NAMESPACE = uuid.UUID("5b0e5b1e-6d1c-4b5e-9c55-2f0a3d6c7e41")
"""For deriving a new goal's label id from its goal id (labels need a
UUID; goal ids are short)."""

_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_ID_LENGTH = 6

CLEARABLE_FIELDS = frozenset(
    {"parent_id", "background_color", "priority", "fixed_time", "measure", "target", "deadline", "note"}
)
"""Goal fields `update_goal` can blank. Not `name`/`status` (always
needed) nor the read-only `id`/`label_id`/`created`."""

OVERALL_ID = "overall"
"""The overall goal's id: one goal every calendar has, whose sub-goals are
implied to be all the top-level goals -- so it's rated, from them or by a
measure of its own, like any other goal, but rates everything together.
It holds no event label, can't be given to an event, and is always active
and top-level. It's in the goals tab once anything is written to it; until
then GoalTree supplies it. (Generated goal ids are 6 characters, so none
can be this.)"""

OVERALL_NAME = "Overall"

DEFAULT_STATUSES: tuple[str, ...] = ("proposed", "active", "inactive")
"""The goals listed unless others are asked for: those still in play.
Completed, archived and deleted ones are listed only on request."""

_READ_ONLY_FIELDS = frozenset({"id", "label_id", "created", "health", "health_period", "health_trend"})


@dataclass(kw_only=True)
class ListedGoal(Goal):
    """A goal as tools return it: plus its `path` from the top of the
    tree, e.g. "Cooking › Vegetarian › Tofu tikka", how many days have
    ended with no confirmed rating since its last one, and how much time
    went toward it recently."""

    path: str | None = None

    effective_color: str | None = None
    """Read-only: the color its label is shown in -- its own
    background_color, or the one it inherits (see GoalTree.color)."""

    stale_days: int | None = None
    """Fully ended days since `health_period` (or since it was created, if
    it's never been rated); `None` unless the daily reflection rates it
    (see GoalTree.rated)."""

    minutes_24h: int | None = None
    """Read-only: minutes of events serving it or any of its sub-goals in
    the 24 hours (of wall-clock time) up to GoalList.as_of; `None` if
    there's no as_of."""

    minutes_7d: int | None = None
    """Read-only: the same, over the 7 days up to GoalList.as_of."""

    minutes_by_statuses: list[StatusMinutes] | None = None
    """Read-only: minutes_24h and minutes_7d, split by the statuses of the
    goals each event is given among this one and its sub-goals -- see
    StatusMinutes. `None` if there's no as_of."""


@dataclass(kw_only=True)
class StatusMinutes:
    """The time toward a goal of the events whose goals, among it and its
    sub-goals, have exactly these statuses between them -- the statuses of
    the goals the events are given, not of their ancestors. So a goal's
    time through goals of any set of statuses is the sum over its
    StatusMinutes whose statuses include one of them, each event counted
    once however many goals it serves."""

    statuses: list[str]
    minutes_24h: int
    minutes_7d: int


@dataclass(kw_only=True)
class GoalList:
    goals: list[ListedGoal]
    """Parents before their children, siblings in sheet order."""

    label_slots_used: int
    """Labels on the calendar once synced: active goals plus unnamed ones."""

    label_slots_total: int = MAX_LABELS

    as_of: datetime | None = None
    """When notes were last compacted into the calendar (see utilities/
    note_compactor.py): the calendar is settled fact up to then, so each
    goal's minutes_24h/minutes_7d are counted up to it. `None` if notes
    have never been compacted."""

    minutes_by_statuses: list[StatusMinutes] | None = None
    """The time spent on any goal in the 24 hours and 7 days up to
    as_of, split by status: the overall goal's minutes_by_statuses.
    `None` if there's no as_of."""


def overall_goal() -> Goal:
    """The overall goal, as it is before anything's written to it."""
    return Goal(id=OVERALL_ID, name=OVERALL_NAME, status="active")


class GoalTree:
    """Read-only lookups over one snapshot of the goals, the overall goal
    among them (see OVERALL_ID), supplied if the sheet hasn't got it yet."""

    def __init__(self, goals: list[Goal]) -> None:
        if not any(goal.id == OVERALL_ID for goal in goals):
            goals = [overall_goal()] + list(goals)
        self.goals = goals
        self.by_id = {goal.id: goal for goal in goals if goal.id}
        self._by_label = {goal.label_id: goal for goal in goals if goal.label_id}

    def chain(self, goal_id: str) -> list[Goal]:
        """The goal and its ancestors, nearest first (stopping at a
        missing parent, or a cycle a hand edit made)."""
        chain: list[Goal] = []
        goal = self.by_id.get(goal_id)
        while goal is not None and goal not in chain:
            chain.append(goal)
            goal = self.by_id.get(goal.parent_id) if goal.parent_id else None
        return chain

    def path(self, goal_id: str) -> str:
        return " › ".join(goal.name or goal.id for goal in reversed(self.chain(goal_id)))

    def priority(self, goal_id: str) -> int | None:
        return next((g.priority for g in self.chain(goal_id) if g.priority is not None), None)

    def fixed_time(self, goal_id: str) -> bool | None:
        return next((g.fixed_time for g in self.chain(goal_id) if g.fixed_time is not None), None)

    def color(self, goal: Goal) -> str:
        """The goal's label color: its own background_color; else its
        nearest ancestor's; else, as a last resort, its priority's color
        (its own priority, or the one it inherits)."""
        for g in self.chain(goal.id) if goal.id else [goal]:
            if g.background_color:
                return g.background_color
        return color_for_priority(self.priority(goal.id) if goal.id else goal.priority)[1]

    def active_label_id(self, goal_id: str) -> str | None:
        """The label of the goal's nearest active goal in its chain
        (itself first), if any."""
        return next((g.label_id for g in self.chain(goal_id) if g.active), None)

    def goal_for_label(self, label_id: str | None) -> Goal | None:
        return self._by_label.get(label_id) if label_id else None

    def check_goal_ids(
        self, goal_ids: Collection[str], *, for_events: bool = False, already: Collection[str] = ()
    ) -> None:
        """Raise ValueError naming any id that isn't a goal, with the
        closest matches (by id or name) as suggestions. With `for_events`,
        a deleted goal is refused too -- no event can be given one -- unless
        it's in `already`, the goals the event has now."""
        unknown = [goal_id for goal_id in goal_ids if goal_id not in self.by_id]
        if for_events and OVERALL_ID in goal_ids:
            raise ValueError(
                "The overall goal can't be given to an event: every goal's events already count toward it"
            )
        deleted = [
            g for g in goal_ids
            if for_events and g in self.by_id and self.by_id[g].status == "deleted" and g not in already
        ]
        if deleted:
            names = ", ".join(f"{g} ({self.by_id[g].name})" for g in deleted)
            raise ValueError(f"Deleted goals can't be given to an event: {names}")
        if not unknown:
            return
        problems = []
        for goal_id in unknown:
            by_name = {(g.name or "").casefold(): g.id for g in self.goals if g.id}
            close = difflib.get_close_matches(goal_id, list(self.by_id), n=3) + [
                by_name[name] for name in difflib.get_close_matches(goal_id.casefold(), list(by_name), n=3)
            ]
            hint = ", ".join(dict.fromkeys(f"{i} ({self.by_id[i].name})" for i in close))
            problems.append(f"{goal_id!r} isn't a goal" + (f"; did you mean {hint}?" if hint else ""))
        raise ValueError("; ".join(problems) + " (get_goals lists them)")

    def children(self, goal_id: str) -> list[Goal]:
        """The goal's immediate sub-goals, in sheet order: for the overall
        goal, the top-level goals."""
        if goal_id == OVERALL_ID:
            return [g for g in self.goals if g.parent_id is None and g.id != OVERALL_ID]
        return [g for g in self.goals if g.parent_id == goal_id and g.id != goal_id]

    def under(self, goal_id: str, ancestor_id: str) -> bool:
        """Whether `goal_id` is `ancestor_id` or one of its descendants;
        every goal is under the overall goal."""
        if ancestor_id == OVERALL_ID:
            return goal_id in self.by_id
        return any(g.id == ancestor_id for g in self.chain(goal_id))

    def rated(self, goal_id: str) -> bool:
        """Whether the daily reflection rates the goal: it's active, and has
        a measure or active sub-goals that are rated themselves."""
        return self._rated(goal_id, set())

    def _rated(self, goal_id: str, seen: set[str]) -> bool:
        goal = self.by_id.get(goal_id)
        if goal is None or not goal.active or goal_id in seen:
            return False
        seen.add(goal_id)
        return goal.measure is not None or any(self._rated(c.id, seen) for c in self.children(goal_id))

    def rated_children(self, goal_id: str) -> list[Goal]:
        """The goal's immediate sub-goals the daily reflection rates."""
        return [g for g in self.children(goal_id) if self.rated(g.id)]

    def measure(self, goal_id: str) -> dict[str, Any] | None:
        """How the goal is rated: its own measure, or, without one, the
        mean of its rated sub-goals (DEFAULT_MEASURE); `None` if it isn't
        rated."""
        if not self.rated(goal_id):
            return None
        return self.by_id[goal_id].measure or dict(DEFAULT_MEASURE)

    def ordered(self) -> list[Goal]:
        """Parents before children (depth-first), siblings in sheet order;
        goals under a missing parent come last, as roots."""
        children: dict[str | None, list[Goal]] = {}
        for goal in self.goals:
            parent = goal.parent_id if goal.parent_id in self.by_id else None
            children.setdefault(parent, []).append(goal)
        ordered: list[Goal] = []
        seen: set[int] = set()

        def visit(goal: Goal) -> None:
            if id(goal) in seen:
                return
            seen.add(id(goal))
            ordered.append(goal)
            for child in children.get(goal.id, []):
                visit(child)

        # The overall goal first: it's above the rest.
        for root in sorted(children.get(None, []), key=lambda g: g.id != OVERALL_ID):
            visit(root)
        for goal in self.goals:  # anything only reachable through a cycle
            visit(goal)
        return ordered


class Goals:
    """A calendar's goals, kept in sync with its event labels -- see the
    module docstring."""

    def __init__(
        self,
        calendar_client: CalendarClient,
        sheets_client: SheetsClient,
        *,
        today: Callable[[], date] | None = None,
        last_compaction: Callable[[], datetime | None] | None = None,
    ) -> None:
        """`last_compaction` says when notes were last compacted into the
        calendar (see GoalList.as_of); without it, goals' recent time
        isn't counted."""
        self._calendar_client = calendar_client
        self._last_compaction = last_compaction
        # Days are the calendar's own, not this server's, and run from
        # waking to waking.
        self._today = today or (
            lambda: current_day_from(
                calendar_client.list_events,
                calendar_client.get_time_zone(),
                datetime.now(calendar_client.get_time_zone()),
            )
        )
        spreadsheet_id, is_new_spreadsheet = calendar_metadata_sheet.ensure_spreadsheet(
            calendar_client, sheets_client
        )
        self._sheet = GoalSheet.find(sheets_client, spreadsheet_id) or self._migrate(
            sheets_client, spreadsheet_id, is_new_spreadsheet
        )

    @property
    def spreadsheet_id(self) -> str:
        return self._sheet.spreadsheet_id

    @property
    def whole_tab(self) -> TabRange:
        """The goals tab, for `SheetsClient.prefetch` -- see
        `GoalSheet.whole_tab`."""
        return self._sheet.whole_tab

    def prefetch(self, ranges: list[TabRange]) -> None:
        """`GoalSheet.prefetch`."""
        self._sheet.prefetch(ranges)

    def tree(self) -> GoalTree:
        """The goals as they are in the sheet now -- read-only, never
        touches the calendar."""
        return GoalTree(self._sheet.read())

    def get_goals(self, statuses: Collection[str] | None = None) -> GoalList:
        """The goals with any of `statuses` (by default DEFAULT_STATUSES)."""
        statuses = _check_statuses(statuses)
        raw_labels, _etag = self._calendar_client.list_event_labels()
        return self._listing(self.tree(), raw_labels, statuses)

    def create_goal(self, goal: Goal) -> GoalList:
        if not goal.name:
            raise ValueError("A goal needs a name")
        tree = self.tree()
        new = replace(
            goal,
            **{name: None for name in _READ_ONLY_FIELDS},
            status=goal.status or "active",
        )
        new.id = self._new_id(tree)
        new.created = self._today()
        new.label_id = str(uuid.uuid5(_LABEL_ID_NAMESPACE, new.id))
        return self._commit(tree.goals + [new], check_measures={new.id})

    def update_goal(self, goal: Goal, clear_fields: Collection[str] = ()) -> GoalList:
        """Set whichever of `goal`'s fields aren't `None` (other than the
        read-only ones) on the goal with `goal.id`, and blank those named
        in `clear_fields`. Making it active, or anything else, adds or
        removes its label."""
        if not goal.id:
            raise ValueError("update_goal needs the goal's id")
        unknown = set(clear_fields) - CLEARABLE_FIELDS
        if unknown:
            raise ValueError(f"Can't clear {sorted(unknown)}; clearable fields are {sorted(CLEARABLE_FIELDS)}")
        both = {name for name in clear_fields if getattr(goal, name) is not None}
        if both:
            raise ValueError(f"Can't both set and clear {sorted(both)}")
        tree = self.tree()
        tree.check_goal_ids([goal.id])
        goals = [replace(g) for g in tree.goals]
        target = next(g for g in goals if g.id == goal.id)
        for field in fields(Goal):
            value = getattr(goal, field.name)
            if field.name not in _READ_ONLY_FIELDS and value is not None:
                setattr(target, field.name, value)
        for name in clear_fields:
            setattr(target, name, None)
        return self._commit(goals, check_measures={goal.id} if goal.measure is not None else ())

    def reorder_goals(self, goal_ids: list[str]) -> GoalList:
        """Put sibling goals (sharing a parent) in the order `goal_ids`
        gives, among the places they already hold -- the order they're
        listed in, siblings being listed in sheet order. Touches no labels."""
        if not goal_ids:
            raise ValueError("Say which goals to reorder")
        if len(set(goal_ids)) != len(goal_ids):
            raise ValueError("Each goal can be listed only once")
        tree = self.tree()
        tree.check_goal_ids(goal_ids)
        parents = {tree.by_id[goal_id].parent_id for goal_id in goal_ids}
        if len(parents) > 1:
            raise ValueError("Only sibling goals, which share a parent, can be reordered together")
        if OVERALL_ID in goal_ids:
            raise ValueError("The overall goal stays above the others; it can't be reordered")
        goals = [replace(g) for g in tree.goals]
        places = [i for i, goal in enumerate(goals) if goal.id in set(goal_ids)]
        by_id = {goal.id: goal for goal in goals}
        for place, goal_id in zip(places, goal_ids):
            goals[place] = by_id[goal_id]
        return self._commit(goals, check_measures=())

    def sync(self) -> GoalList:
        """Make the calendar's labels match the sheet's active goals (after
        hand edits to the sheet). Validates the sheet first."""
        return self._commit(self.tree().goals, write=False)

    def set_health(self, health: dict[str, tuple[int | None, str | None, str | None]]) -> None:
        """Set goals' health cache -- (health, health_period, health_trend)
        by goal id; see utilities/goal_health.py. Touches no labels."""
        goals = [replace(goal) for goal in self.tree().goals]
        changed = False
        for goal in goals:
            if goal.id in health and (goal.health, goal.health_period, goal.health_trend) != health[goal.id]:
                goal.health, goal.health_period, goal.health_trend = health[goal.id]
                changed = True
        if changed:
            self._sheet.write(goals)

    def _commit(
        self, goals: list[Goal], *, write: bool = True, check_measures: Collection[str] | None = None
    ) -> GoalList:
        """Validate `goals`, write them to the sheet (unless `write` is
        false), then make the calendar's labels match. Everything that can
        be refused is checked before anything is written. Measures are
        checked in full only for the goals in `check_measures` (all of
        them if `None`); see `_validate`."""
        _validate(goals, check_measures)
        tree = GoalTree(goals)
        raw_labels, etag = self._calendar_client.list_event_labels()
        if not any(g.id != OVERALL_ID for g in tree.goals) and any(label.name for label in raw_labels):
            # An empty goals tab next to named labels is almost certainly a
            # broken sheet, not a request to delete every label.
            raise ValueError(
                "The goals tab is empty, but the calendar still has named event labels; refusing to "
                "delete them all. Add the goals back to the tab, or remove the labels by hand."
            )
        desired = self._check_budget(tree, raw_labels)
        if write:
            self._sheet.write(goals)
        if {astuple(label) for label in desired} != {astuple(label) for label in raw_labels}:
            # The etag guards against a concurrent label change since the
            # read above (EventLabelConflictError).
            raw_labels = self._calendar_client.replace_event_labels(desired, etag)
        return self._listing(tree, raw_labels, DEFAULT_STATUSES)

    def _check_budget(self, tree: GoalTree, raw_labels: list[RawEventLabel]) -> list[RawEventLabel]:
        """The calendar's labels as they should be for `tree`; ValueError if
        that's more than the calendar can hold."""
        desired = [
            RawEventLabel(id=goal.label_id, name=goal.name, background_color=tree.color(goal))
            for goal in tree.ordered()
            if _holds_label(goal)
        ] + [label for label in raw_labels if not label.name]
        if len(desired) > MAX_LABELS:
            unnamed = len(desired) - sum(1 for goal in tree.goals if _holds_label(goal))
            raise ValueError(
                f"That would need {len(desired)} event labels, but a calendar holds {MAX_LABELS} "
                f"({unnamed} are Calendar's own unnamed ones), so at most {MAX_LABELS - unnamed} goals "
                "can be active at once. Make one you're not working on inactive, completed or archived "
                "(its history is kept)."
            )
        return desired

    def _listing(self, tree: GoalTree, raw_labels: list[RawEventLabel], statuses: Collection[str]) -> GoalList:
        unnamed = sum(1 for label in raw_labels if not label.name)
        today = self._today()
        as_of = self._last_compaction() if self._last_compaction else None
        recent, by_statuses = self._recent_minutes(tree, as_of) if as_of is not None else (None, None)
        return GoalList(
            goals=[
                ListedGoal(
                    **{f.name: getattr(goal, f.name) for f in fields(Goal)},
                    path=tree.path(goal.id),
                    effective_color=tree.color(goal),
                    stale_days=_stale_days(goal, tree, today),
                    minutes_24h=recent["24h"].get(goal.id, 0) if recent else None,
                    minutes_7d=recent["7d"].get(goal.id, 0) if recent else None,
                    minutes_by_statuses=by_statuses.get(goal.id, []) if by_statuses is not None else None,
                )
                for goal in tree.ordered()
                # The overall goal whatever's asked for: it's above them all.
                if goal.status in statuses or goal.id == OVERALL_ID
            ],
            label_slots_used=unnamed + sum(1 for goal in tree.goals if _holds_label(goal)),
            as_of=as_of,
            minutes_by_statuses=by_statuses.get(OVERALL_ID, []) if by_statuses is not None else None,
        )

    def _recent_minutes(
        self, tree: GoalTree, as_of: datetime
    ) -> tuple[dict[str, dict[str, int]], dict[str, list[StatusMinutes]]]:
        """Minutes per goal in each of utilities/goal_time.py's
        RECENT_WINDOWS up to `as_of`, and each goal's split by statuses (see
        StatusMinutes), from one listing of the calendar."""
        # Imported here, since both modules import this one.
        from utilities.goal_calendar import fill_in_from_goals
        from utilities.goal_time import RECENT_WINDOWS, goal_minutes, goal_status_minutes

        longest = max(RECENT_WINDOWS.values())
        events = fill_in_from_goals(self._calendar_client.list_events(as_of - longest, as_of), tree)
        per_goal = {name: goal_minutes(events, tree, as_of - window, as_of) for name, window in RECENT_WINDOWS.items()}
        day = goal_status_minutes(events, tree, as_of - RECENT_WINDOWS["24h"], as_of)
        week = goal_status_minutes(events, tree, as_of - RECENT_WINDOWS["7d"], as_of)
        by_statuses = {
            goal_id: [
                StatusMinutes(
                    statuses=sorted(statuses), minutes_24h=day.get(goal_id, {}).get(statuses, 0), minutes_7d=minutes
                )
                for statuses, minutes in sorted(split.items(), key=lambda item: sorted(item[0]))
            ]
            for goal_id, split in week.items()
        }
        return per_goal, by_statuses

    @staticmethod
    def _new_id(tree: GoalTree) -> str:
        while True:
            goal_id = "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))
            if goal_id not in tree.by_id:
                return goal_id

    def _migrate(self, sheets_client: SheetsClient, spreadsheet_id: str, is_new_spreadsheet: bool) -> GoalSheet:
        """Create the goals tab from the calendar's named event labels: one
        active, top-level goal per label, keeping its id."""
        raw_labels, _etag = self._calendar_client.list_event_labels()
        goals: list[Goal] = []
        taken: set[str] = set()
        for label in raw_labels:
            label_id, name = label.id, label.name
            if not name:
                continue  # Calendar's own default colors, not anyone's label
            unique, n = name[:MAX_NAME_LENGTH], 2
            while unique.casefold() in taken:
                suffix = f" ({n})"
                unique, n = name[: MAX_NAME_LENGTH - len(suffix)] + suffix, n + 1
            taken.add(unique.casefold())
            goal_id = self._new_id(GoalTree(goals))
            goals.append(
                Goal(
                    id=goal_id,
                    name=unique,
                    status="active",
                    label_id=label_id or str(uuid.uuid5(_LABEL_ID_NAMESPACE, goal_id)),
                    background_color=label.background_color,
                    created=self._today(),
                )
            )

        return GoalSheet.create(
            sheets_client, spreadsheet_id, goals, reuse_sheet_id=0 if is_new_spreadsheet else None
        )


def _holds_label(goal: Goal) -> bool:
    """Whether the goal takes one of the calendar's event labels: it's
    active, and isn't the overall goal."""
    return goal.active and goal.id != OVERALL_ID


def _check_statuses(statuses: Collection[str] | None) -> tuple[str, ...]:
    if statuses is None:
        return DEFAULT_STATUSES
    unknown = sorted(set(statuses) - set(GOAL_STATUSES))
    if unknown:
        raise ValueError(f"Unknown goal status(es) {unknown}; statuses are {', '.join(GOAL_STATUSES)}")
    return tuple(statuses)


def _stale_days(goal: Goal, tree: GoalTree, today: date) -> int | None:
    """See ListedGoal.stale_days. `today` hasn't ended, so isn't counted."""
    if not tree.rated(goal.id):
        return None
    try:
        latest = date.fromisoformat(goal.health_period) if goal.health_period else None
    except ValueError:
        latest = None  # Rated over a week or month, before goals were rated daily.
    if latest is not None:
        first = latest + timedelta(days=1)
    elif goal.created is not None:
        first = goal.created
    else:
        return None
    return max(0, (today - first).days)


def _validate(goals: list[Goal], check_measures: Collection[str] | None = None) -> None:
    """Raise ValueError listing everything wrong with `goals` as a whole.

    Measures are checked in full (utilities/goal_measures.py) only for the
    goals in `check_measures` -- those being created or given a measure --
    or for all of them if it's `None`, as when syncing hand edits. Any
    other goal's measure need only have a "kind", so one saved before the
    full checks existed can't block edits to other goals."""
    problems = []
    ids = [goal.id for goal in goals]
    for goal_id in sorted({i for i in ids if ids.count(i) > 1 and i}):
        problems.append(f"goal id {goal_id!r} is used more than once")
    tree = GoalTree(goals)
    siblings: dict[tuple[str | None, str], str] = {}
    for goal in goals:
        label = f"goal {goal.id or goal.name!r}"
        if not goal.id:
            problems.append(f"a goal named {goal.name!r} has no id")
        if not goal.name:
            problems.append(f"{label} has no name")
        elif len(goal.name) > MAX_NAME_LENGTH:
            problems.append(f"{label}'s name is longer than {MAX_NAME_LENGTH} characters")
        else:
            key = (goal.parent_id, goal.name.casefold())
            if key in siblings:
                problems.append(f"{label} has the same name as its sibling {siblings[key]!r}")
            siblings[key] = goal.id
        if goal.status not in GOAL_STATUSES:
            problems.append(f"{label}'s status must be one of {', '.join(GOAL_STATUSES)}")
        if goal.id == OVERALL_ID:
            if goal.status != "active":
                problems.append("the overall goal is always active")
            if goal.parent_id is not None:
                problems.append("the overall goal is above every other, so it has no parent")
        elif not goal.label_id:
            problems.append(f"{label} has no label_id")
        if goal.parent_id == OVERALL_ID:
            problems.append(
                f"{label} can't name the overall goal as its parent: every top-level goal is already under it"
            )
        elif goal.parent_id is not None and goal.id != OVERALL_ID:
            if goal.parent_id not in tree.by_id:
                problems.append(f"{label}'s parent {goal.parent_id!r} isn't a goal")
            elif goal.id and goal.id in [g.id for g in tree.chain(goal.parent_id)]:
                problems.append(f"{label} can't be its own ancestor")
        if goal.measure is not None:
            found = measure_problems(goal.measure, sub_goal_ids={c.id for c in tree.children(goal.id)})
            if check_measures is not None and goal.id not in check_measures:
                found = [p for p in found if p == MEASURE_SHAPE_PROBLEM]
            elif isinstance(goal.measure, dict) and isinstance(goal.measure.get("goal_ids"), list):
                unknown = [g for g in goal.measure["goal_ids"] if isinstance(g, str) and g not in tree.by_id]
                if unknown:
                    found.append(f"\"goal_ids\" names {unknown[0]!r}, which isn't a goal")
            problems.extend(f"{label}'s measure {problem}" for problem in found)
    if problems:
        raise ValueError("; ".join(problems))
