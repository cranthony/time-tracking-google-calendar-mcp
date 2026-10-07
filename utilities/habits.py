"""Habits: what the user wants to do well, each scoped to one action or
action group -- "Practice guitar", "Cook" -- and rated by traits, as a
person is.

**Scope.** A habit is about the user's own events with its action, or
with any action under its group: only those count toward it, and only
those are judged for it. Whether an event is in scope is worked out from
the actions and groups as they are when it's used -- when an event is
judged, or a score worked out -- so moving an action between groups
changes what's in scope from then on.

**Traits.** Like a person's: `{"select": "all" or [trait ids], "parts":
{trait id: [parts]}}`, both optional (see utilities/traits.py's
`person_traits_problems`). Without it every active trait applies, with
the Traits tab's parts; `parts` give the habit its own rubrics. A habit
is always "with": judgment parts for those an event was done "for" don't
apply to it.

**Where they live.** The **Habits** tab of the calendar's metadata
spreadsheet, one row per habit, read by header name (utilities/
row_sheet.py), so it can be edited by hand. Names are unique. A habit is
archived or deleted rather than removed, so judgments naming it still
make sense.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Collection
from dataclasses import dataclass, fields, replace
from typing import Any, Literal

from calendar_clients.google_sheets import SheetsClient, TabRange
from utilities import calendar_metadata_sheet
from utilities.row_sheet import RowSheet, check_clear, new_id, updated
from utilities.traits import person_traits_problems

HabitStatus = Literal["active", "archived", "deleted"]
HABIT_STATUSES: tuple[str, ...] = ("active", "archived", "deleted")
"""Where a habit stands:

- `active`: one the user's working on.
- `archived`: set aside, and kept out of the way.
- `deleted`: shouldn't have existed. Kept only so judgments naming it
  still make sense."""

DEFAULT_STATUSES: tuple[str, ...] = ("active",)

MAX_NAME_LENGTH = 100

CLEARABLE_FIELDS = frozenset({"note", "traits"})

SUBJECT_PREFIX = "habit:"
"""What a habit's id is prefixed with where a person's id would go --
judgments on an event, cancellations -- so the two never collide."""


def subject_id(habit_id: str) -> str:
    """The id a habit goes by among people: `habit:<id>`."""
    return f"{SUBJECT_PREFIX}{habit_id}"


@dataclass(kw_only=True)
class Habit:
    """One habit -- see the module docstring."""

    id: str | None = None
    """Immutable short id, assigned on creation."""

    name: str | None = None
    """Unique among habits."""

    action_id: str | None = None
    """The action, or the action group, whose events it's about."""

    status: HabitStatus | None = None
    """See HABIT_STATUSES."""

    note: str | None = None
    """What it's for, and what doing it well looks like: context for
    judging its events."""

    traits: dict[str, Any] | None = None
    """Which traits apply to it, and parts replacing a trait's for it
    alone -- see the module docstring."""


@dataclass(kw_only=True)
class ListedHabit(Habit):
    """A habit as the habit tools return it: plus its action's or
    group's path."""

    action_path: str | None = None
    """Its action's or group's name, under its groups: "Creative ›
    Guitar"."""


@dataclass(kw_only=True)
class CreatedHabit:
    habit: ListedHabit
    created_id: str


class Habits:
    """A calendar's habits -- see the module docstring."""

    def __init__(
        self,
        sheet: RowSheet[Habit],
        scopes: Callable[[], dict[str, str]] | None = None,
        trait_ids: Callable[[], Collection[str]] | None = None,
    ) -> None:
        """`scopes` gives every action's and group's id, with its path
        (see `action_scopes`), which a habit's `action_id` must name; and
        `trait_ids` the ids in the Traits tab, which its `traits` must
        name. Without them, neither is checked (or pathed)."""
        self._sheet = sheet
        self._scopes = scopes
        self._trait_ids = trait_ids

    @staticmethod
    def ensure(
        sheets_client: SheetsClient,
        spreadsheet_id: str,
        scopes: Callable[[], dict[str, str]] | None = None,
        trait_ids: Callable[[], Collection[str]] | None = None,
    ) -> "Habits":
        """The calendar's habits, adding the Habits tab the first time."""
        return Habits(
            RowSheet.ensure(
                sheets_client,
                spreadsheet_id,
                role=calendar_metadata_sheet.HABITS_SHEET_ROLE,
                title=calendar_metadata_sheet.HABITS_SHEET_TITLE,
                row_type=Habit,
            ),
            scopes,
            trait_ids,
        )

    @property
    def whole_tab(self) -> TabRange:
        return self._sheet.whole_tab

    def prefetch(self, ranges: list[TabRange]) -> None:
        self._sheet.prefetch(ranges)

    # -- reading ------------------------------------------------------------

    def all(self) -> list[Habit]:
        """Every habit, whatever its status, in sheet order."""
        return self._sheet.read()

    def get_habits(self, statuses: Collection[str] | None = None) -> list[ListedHabit]:
        """The habits with any of `statuses` (by default active ones)."""
        statuses = _check_statuses(statuses)
        paths = self._paths()
        return [_listed(h, paths) for h in self.all() if h.status in statuses]

    def get_habit(self, id_or_name: str) -> ListedHabit:
        """The habit with this id, or else this name (ignoring case)."""
        return _listed(find(self.all(), id_or_name), self._paths())

    # -- writing ------------------------------------------------------------

    def create_habit(self, habit: Habit) -> CreatedHabit:
        """Add `habit` (active unless given a status), with a new id."""
        habits = self.all()
        new = replace(habit, id=new_id({h.id for h in habits}), status=habit.status or "active")
        self._write(habits + [new], new.id)
        return CreatedHabit(habit=_listed(new, self._paths()), created_id=new.id)

    def update_habit(self, habit: Habit, clear_fields: Collection[str] = ()) -> ListedHabit:
        """Set whichever of `habit`'s fields aren't `None` (but its id) on
        the habit with `habit.id`, and blank those in `clear_fields`."""
        if not habit.id:
            raise ValueError("update_habit needs the habit's id")
        check_clear(habit, clear_fields, CLEARABLE_FIELDS)
        habits = self.all()
        index = next((i for i, h in enumerate(habits) if h.id == habit.id), None)
        if index is None:
            find(habits, habit.id)  # Raises, suggesting close matches.
        habits[index] = updated(habits[index], habit, clear_fields)
        self._write(habits, habit.id)
        return _listed(habits[index], self._paths())

    def _write(self, habits: list[Habit], changed: str) -> None:
        """Write `habits`, checked -- `changed`'s action against the
        actions and groups there now: another's may be gone since."""
        scopes = self._scopes() if self._scopes else None
        trait_ids = self._trait_ids() if self._trait_ids and any(h.traits is not None for h in habits) else None
        problems = habit_problems(habits, scopes, trait_ids, changed=changed)
        if problems:
            raise ValueError("; ".join(problems))
        self._sheet.write(habits)

    def _paths(self) -> dict[str, str]:
        return self._scopes() if self._scopes else {}


def action_scopes(actions: Collection[Any], groups: Any) -> dict[str, str]:
    """Every action's and action group's id, with its path ("Creative ›
    Guitar"): what a habit's `action_id` may name -- not a deleted
    action. `groups` is a GroupTree (utilities/action_groups.py), for the
    paths."""
    scopes = {g.id: groups.path(g) for g in groups.groups if g.id}
    scopes.update({a.id: groups.path(a) for a in actions if a.id and a.status != "deleted"})
    return scopes


def find(habits: list[Habit], id_or_name: str) -> Habit:
    """The habit with this id, or else this name (ignoring case);
    ValueError suggesting close matches if there's none."""
    by_name = {(h.name or "").casefold(): h for h in habits}
    habit = next((h for h in habits if h.id == id_or_name), None) or by_name.get(id_or_name.casefold())
    if habit is None:
        by_id = {h.id: h for h in habits if h.id}
        close = [by_id[i] for i in difflib.get_close_matches(id_or_name, list(by_id), n=3)] + [
            by_name[n] for n in difflib.get_close_matches(id_or_name.casefold(), list(by_name), n=3)
        ]
        hint = ", ".join(dict.fromkeys(f"{h.id} ({h.name})" for h in close))
        raise ValueError(
            f"There's no habit with the id or name {id_or_name!r}"
            + (f"; did you mean {hint}?" if hint else "")
            + " (get_habits lists them)"
        )
    return habit


def habit_problems(
    habits: list[Habit],
    scopes: dict[str, str] | None = None,
    trait_ids: Collection[str] | None = None,
    *,
    changed: str | None = None,
) -> list[str]:
    """Everything wrong with the Habits tab's rows as a whole, as phrases.
    Their `traits` are checked against `trait_ids`, if given; and, against
    `scopes`, the `action_id` of the habit `changed` (or, with none, of
    every habit): an action or group deleted since another was written
    doesn't stop this one being."""
    problems = []
    ids = [h.id for h in habits]
    for habit_id in sorted({i for i in ids if i and ids.count(i) > 1}):
        problems.append(f"habit id {habit_id!r} is used more than once")
    names: dict[str, Habit] = {}
    for habit in habits:
        label = f"habit {habit.id}" if habit.id else f"habit {habit.name!r}"
        if not habit.id:
            problems.append(f"a habit named {habit.name!r} has no id")
        if not (isinstance(habit.name, str) and habit.name.strip()):
            problems.append(f"{label} needs a name")
        elif len(habit.name) > MAX_NAME_LENGTH:
            problems.append(f"{label}'s name is longer than {MAX_NAME_LENGTH} characters")
        else:
            key = habit.name.strip().casefold()
            if key in names:
                problems.append(
                    f"there's already a habit named {names[key].name!r} ({names[key].id}); habit names must be "
                    "unique -- use that one, or pick another name"
                )
            names[key] = habit
        if not habit.action_id:
            problems.append(f"{label} needs an action_id: the action, or the action group, it's about")
        elif scopes is not None and changed in (None, habit.id) and habit.action_id not in scopes:
            problems.append(
                f"{label}'s action_id {habit.action_id!r} isn't an action or an action group "
                "(get_actions and get_action_groups list them)"
            )
        if habit.status not in HABIT_STATUSES:
            problems.append(f"{label}'s status must be one of {', '.join(HABIT_STATUSES)}")
        if habit.traits is not None:
            problems += [f"{label}'s traits {p}" for p in person_traits_problems(habit.traits, trait_ids)]
    return problems


def _listed(habit: Habit, paths: dict[str, str]) -> ListedHabit:
    return ListedHabit(
        **{f.name: getattr(habit, f.name) for f in fields(Habit)},
        action_path=paths.get(habit.action_id) if habit.action_id else None,
    )


def _check_statuses(statuses: Collection[str] | None) -> tuple[str, ...]:
    if statuses is None:
        return DEFAULT_STATUSES
    unknown = sorted(set(statuses) - set(HABIT_STATUSES))
    if unknown:
        raise ValueError(f"Unknown habit status(es) {unknown}; statuses are {', '.join(HABIT_STATUSES)}")
    return tuple(statuses)
