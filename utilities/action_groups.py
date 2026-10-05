"""Action groups: names that roll actions up ("Creative", "Guitar"), for
targets and for finding one's way around. A group isn't an action itself
-- no event can be tagged with one -- and groups can sit inside other
groups, so actions are always the leaves of a tree of groups.

**Where they live.** The **Action Groups** tab of the calendar's metadata
spreadsheet, one row per group, read by header name (utilities/
row_sheet.py). utilities/actions.py's `Actions` keeps this tab along with
its own, since a group's color and priority are what its actions inherit
(and so what their labels show).

Groups have no status: one no longer wanted is deleted, its actions and
sub-groups moving up to its own parent group.
"""

from __future__ import annotations

import difflib
from collections.abc import Collection
from dataclasses import dataclass
from typing import Protocol

from calendar_clients.google_calendar import color_for_priority

MAX_NAME_LENGTH = 50

CLEARABLE_FIELDS = frozenset({"group_id", "background_color", "priority", "note"})
"""Group fields `update_action_group` can blank. Not `name` (always
needed) nor the read-only `id`."""


@dataclass(kw_only=True)
class ActionGroup:
    """One action group -- see the module docstring."""

    id: str | None = None
    """Immutable short id, assigned on creation."""

    group_id: str | None = None
    """The group this one is inside; `None` for a top-level group."""

    name: str | None = None
    """Unique among groups (an action may share it), at most 50
    characters."""

    background_color: str | None = None
    """Hex color its actions (and sub-groups) inherit when they don't set
    their own."""

    priority: int | None = None
    """Inherited by its actions (and sub-groups) that don't set their own."""

    note: str | None = None
    """Short free text, e.g. what it covers."""


@dataclass(kw_only=True)
class ListedActionGroup(ActionGroup):
    """A group as the group tools return it: plus where it sits, and what
    it inherits."""

    path: str | None = None
    """Its groups from the top, e.g. "Creative › Guitar"."""

    effective_color: str | None = None
    """Its own background_color, or its nearest enclosing group's, or else
    its effective priority's color."""

    effective_priority: int | None = None
    """Its own priority, or its nearest enclosing group's, if any."""


class Placed(Protocol):
    """An action or a group: anything that sits in a group."""

    group_id: str | None
    background_color: str | None
    priority: int | None
    name: str | None


class GroupTree:
    """Read-only lookups over one snapshot of the groups."""

    def __init__(self, groups: list[ActionGroup]) -> None:
        self.groups = groups
        self.by_id = {g.id: g for g in groups if g.id}

    def chain(self, item: Placed) -> list[Placed]:
        """`item` and the groups it's inside, nearest first (stopping at a
        missing group, or a cycle a hand edit made)."""
        chain: list[Placed] = [item]
        group = self.by_id.get(item.group_id) if item.group_id else None
        while group is not None and group not in chain:
            chain.append(group)
            group = self.by_id.get(group.group_id) if group.group_id else None
        return chain

    def path(self, item: Placed) -> str:
        return " › ".join(i.name or "" for i in reversed(self.chain(item)))

    def priority(self, item: Placed) -> int | None:
        return next((i.priority for i in self.chain(item) if i.priority is not None), None)

    def color(self, item: Placed) -> str:
        return next(
            (i.background_color for i in self.chain(item) if i.background_color),
            color_for_priority(self.priority(item))[1],
        )

    def listed(self, group: ActionGroup) -> ListedActionGroup:
        return ListedActionGroup(
            **vars(group), path=self.path(group), effective_color=self.color(group), effective_priority=self.priority(group)
        )

    def ordered(self) -> list[ActionGroup]:
        """Enclosing groups before the ones inside them (depth-first),
        siblings in sheet order; groups inside a missing one come last."""
        children: dict[str | None, list[ActionGroup]] = {}
        for group in self.groups:
            children.setdefault(group.group_id if group.group_id in self.by_id else None, []).append(group)
        ordered: list[ActionGroup] = []

        def visit(group: ActionGroup) -> None:
            if any(g is group for g in ordered):
                return
            ordered.append(group)
            for child in children.get(group.id, []):
                visit(child)

        for group in children.get(None, []) + self.groups:
            visit(group)
        return ordered


def find(groups: list[ActionGroup], id_or_name: str) -> ActionGroup:
    """The group with this id, or else this name (ignoring case);
    ValueError suggesting close matches if there's none."""
    by_name = {(g.name or "").casefold(): g for g in groups}
    group = next((g for g in groups if g.id == id_or_name), None) or by_name.get(id_or_name.casefold())
    if group is None:
        by_id = {g.id: g for g in groups if g.id}
        close = [by_id[i] for i in difflib.get_close_matches(id_or_name, list(by_id), n=3)] + [
            by_name[n] for n in difflib.get_close_matches(id_or_name.casefold(), list(by_name), n=3)
        ]
        hint = ", ".join(dict.fromkeys(f"{g.id} ({g.name})" for g in close))
        raise ValueError(
            f"There's no action group with the id or name {id_or_name!r}"
            + (f"; did you mean {hint}?" if hint else "")
            + " (get_action_groups lists them)"
        )
    return group


def group_problems(groups: list[ActionGroup]) -> list[str]:
    """Everything wrong with `groups` as a whole, as phrases."""
    problems = []
    tree = GroupTree(groups)
    ids = [g.id for g in groups]
    for group_id in sorted({i for i in ids if i and ids.count(i) > 1}):
        problems.append(f"action group id {group_id!r} is used more than once")
    names: dict[str, ActionGroup] = {}
    for group in groups:
        label = f"action group {group.id}" if group.id else f"action group {group.name!r}"
        if not group.id:
            problems.append(f"an action group named {group.name!r} has no id")
        if not (isinstance(group.name, str) and group.name.strip()):
            problems.append(f"{label} needs a name")
        elif len(group.name) > MAX_NAME_LENGTH:
            problems.append(f"{label}'s name {group.name!r} is longer than {MAX_NAME_LENGTH} characters")
        else:
            key = group.name.strip().casefold()
            if key in names:
                problems.append(
                    f"there's already an action group named {names[key].name!r} ({names[key].id}); "
                    "group names must be unique (an action may share one)"
                )
            names[key] = group
        if group.group_id is not None:
            if group.group_id not in tree.by_id:
                problems.append(f"{label} is inside {group.group_id!r}, which isn't an action group")
            elif any(g.id == group.id for g in tree.chain(tree.by_id[group.group_id])):
                problems.append(f"{label} can't be inside itself, directly or through other groups")
        if group.priority is not None and not isinstance(group.priority, int):
            problems.append(f"{label}'s priority must be a whole number")
    return problems


def check_group_ids(group_ids: Collection[str | None], groups: list[ActionGroup]) -> list[str]:
    """Phrases for each of `group_ids` that isn't a group."""
    known = {g.id for g in groups}
    return [f"{group_id!r} isn't an action group" for group_id in group_ids if group_id is not None and group_id not in known]
