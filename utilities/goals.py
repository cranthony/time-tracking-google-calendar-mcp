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
from datetime import date, datetime

from calendar_clients.google_calendar import CalendarClient, EventLabel as RawEventLabel, color_for_priority
from calendar_clients.google_sheets import SheetsClient
from utilities import calendar_metadata_sheet
from utilities.event_label_sheet import EventLabelSheet
from utilities.goal_measures import MEASURE_SHAPE_PROBLEM, measure_problems
from utilities.goal_periods import last_ended, parse_period, period_containing
from utilities.goal_sheet import CADENCES, GOAL_STATUSES, Goal, GoalSheet

MAX_LABELS = 200
"""The most event labels a calendar can have (named or not)."""

MAX_NAME_LENGTH = 50
"""A goal's name is its label's name, which Calendar caps at 50."""

_LABEL_ID_NAMESPACE = uuid.UUID("5b0e5b1e-6d1c-4b5e-9c55-2f0a3d6c7e41")
"""For deriving a new goal's label id from its goal id (labels need a
UUID; goal ids are short)."""

_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_ID_LENGTH = 6

_MIGRATED_TAB_TITLE = "Event Labels (migrated)"

CLEARABLE_FIELDS = frozenset(
    {"parent_id", "background_color", "priority", "fixed_time", "cadence", "measure", "target", "deadline", "note"}
)
"""Goal fields `update_goal` can blank. Not `name`/`status` (always
needed) nor the read-only `id`/`label_id`/`created`."""

DEFAULT_STATUSES: tuple[str, ...] = ("proposed", "active", "inactive")
"""The goals listed unless others are asked for: those still in play.
Completed, archived and deleted ones are listed only on request."""

_READ_ONLY_FIELDS = frozenset({"id", "label_id", "created", "health", "health_period", "health_trend"})


@dataclass(kw_only=True)
class ListedGoal(Goal):
    """A goal as tools return it: plus its `path` from the top of the
    tree, e.g. "Cooking › Vegetarian › Tofu tikka", and how many of its
    periods have ended with no confirmed assessment since its last one."""

    path: str | None = None

    effective_color: str | None = None
    """Read-only: the color its label is shown in -- its own
    background_color, or the one it inherits (see GoalTree.color)."""

    stale_periods: int | None = None
    """Fully ended periods of its cadence since `health_period` (or since
    it was created, if it's never been assessed); `None` unless it's
    active and has a cadence."""


@dataclass(kw_only=True)
class GoalList:
    goals: list[ListedGoal]
    """Parents before their children, siblings in sheet order."""

    label_slots_used: int
    """Labels on the calendar once synced: active goals plus unnamed ones."""

    label_slots_total: int = MAX_LABELS


class GoalTree:
    """Read-only lookups over one snapshot of the goals."""

    def __init__(self, goals: list[Goal]) -> None:
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

        for root in children.get(None, []):
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
    ) -> None:
        self._calendar_client = calendar_client
        # Days are the calendar's own, not this server's.
        self._today = today or (lambda: datetime.now(calendar_client.get_time_zone()).date())
        spreadsheet_id, is_new_spreadsheet = calendar_metadata_sheet.ensure_spreadsheet(
            calendar_client, sheets_client
        )
        self._sheet = GoalSheet.find(sheets_client, spreadsheet_id) or self._migrate(
            sheets_client, spreadsheet_id, is_new_spreadsheet
        )

    @property
    def spreadsheet_id(self) -> str:
        return self._sheet.spreadsheet_id

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
        if not tree.goals and any(label.name for label in raw_labels):
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
            if goal.active
        ] + [label for label in raw_labels if not label.name]
        if len(desired) > MAX_LABELS:
            unnamed = len(desired) - sum(1 for goal in tree.goals if goal.active)
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
        return GoalList(
            goals=[
                ListedGoal(
                    **{f.name: getattr(goal, f.name) for f in fields(Goal)},
                    path=tree.path(goal.id),
                    effective_color=tree.color(goal),
                    stale_periods=_stale_periods(goal, today),
                )
                for goal in tree.ordered()
                if goal.status in statuses
            ],
            label_slots_used=unnamed + sum(1 for goal in tree.goals if goal.active),
        )

    @staticmethod
    def _new_id(tree: GoalTree) -> str:
        while True:
            goal_id = "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))
            if goal_id not in tree.by_id:
                return goal_id

    def _migrate(self, sheets_client: SheetsClient, spreadsheet_id: str, is_new_spreadsheet: bool) -> GoalSheet:
        """Create the goals tab from the event labels tab (or, without one,
        the calendar's named labels): one active, top-level goal per
        label, keeping its id. The old tab is renamed, not deleted."""
        labels_tab = calendar_metadata_sheet.find_tab(
            sheets_client, spreadsheet_id, calendar_metadata_sheet.EVENT_LABELS_SHEET_ROLE
        )
        if labels_tab is not None:
            sources = [
                (label.id, label.name, label.background_color, label.priority, label.fixed_time, label.note)
                for label in EventLabelSheet(sheets_client, spreadsheet_id, labels_tab).read()
            ]
        else:
            raw_labels, _etag = self._calendar_client.list_event_labels()
            sources = [(label.id, label.name, label.background_color, None, None, None) for label in raw_labels]

        goals: list[Goal] = []
        taken: set[str] = set()
        for label_id, name, background_color, priority, fixed_time, note in sources:
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
                    background_color=background_color,
                    priority=priority,
                    fixed_time=fixed_time,
                    note=note,
                    created=self._today(),
                )
            )

        sheet = GoalSheet.create(
            sheets_client, spreadsheet_id, goals, reuse_sheet_id=0 if is_new_spreadsheet else None
        )
        if labels_tab is not None:
            sheets_client.update_sheet_properties(spreadsheet_id, labels_tab, title=_MIGRATED_TAB_TITLE)
        return sheet


def _check_statuses(statuses: Collection[str] | None) -> tuple[str, ...]:
    if statuses is None:
        return DEFAULT_STATUSES
    unknown = sorted(set(statuses) - set(GOAL_STATUSES))
    if unknown:
        raise ValueError(f"Unknown goal status(es) {unknown}; statuses are {', '.join(GOAL_STATUSES)}")
    return tuple(statuses)


def _stale_periods(goal: Goal, today: date) -> int | None:
    """See ListedGoal.stale_periods."""
    if goal.cadence not in CADENCES or not goal.active:
        return None
    try:
        latest = parse_period(goal.cadence, goal.health_period) if goal.health_period else None
    except ValueError:
        latest = None  # Assessed at a cadence it no longer has.
    if latest is not None:
        period = latest.next()
    elif goal.created is not None:
        period = period_containing(goal.cadence, goal.created)
    else:
        return None
    last = last_ended(goal.cadence, today)
    count = 0
    while period.start <= last.start and count < 1000:
        count += 1
        period = period.next()
    return count


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
        if not goal.label_id:
            problems.append(f"{label} has no label_id")
        if goal.parent_id is not None:
            if goal.parent_id not in tree.by_id:
                problems.append(f"{label}'s parent {goal.parent_id!r} isn't a goal")
            elif goal.id and goal.id in [g.id for g in tree.chain(goal.parent_id)]:
                problems.append(f"{label} can't be its own ancestor")
        if goal.cadence is not None and goal.cadence not in CADENCES:
            problems.append(f"{label}'s cadence must be one of {', '.join(CADENCES)}")
        if goal.measure is not None:
            found = measure_problems(goal.measure)
            if check_measures is not None and goal.id not in check_measures:
                found = [p for p in found if p == MEASURE_SHAPE_PROBLEM]
            problems.extend(f"{label}'s measure {problem}" for problem in found)
    if problems:
        raise ValueError("; ".join(problems))
