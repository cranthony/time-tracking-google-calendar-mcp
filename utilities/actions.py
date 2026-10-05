"""Actions: what the user does with their time, each a verb for what
they're doing in a moment ("play guitar", "eat a meal"). They're the
leaves events are tagged with; action groups (see utilities/
action_groups.py) roll them up.

**Where they live.** The **Actions** tab of the calendar's metadata
spreadsheet (utilities/calendar_metadata_sheet.py), one row per action,
read by header name (utilities/row_sheet.py), so it can be edited by
hand.

**Labels.** Each action reserves one of
the calendar's event label ids when it's created, and holds that label --
its name and color shown on the calendar -- while it's in play:

- every `active` action holds its label; if they'd need more labels than
  the calendar has room for, the change is refused;
- `proposed` actions (made by the assistant and not yet reviewed) hold
  theirs too, in sheet order, while there's room left over;
- `archived` and `deleted` ones don't: their labels are removed, and come
  back under the same id if they're made active again.

Labels that aren't any action's (Calendar's own unnamed ones, and any
other app's, or the goals' that actions replaced) are always left alone, and count against the
calendar's room. An action without its own color or priority takes its
nearest group's, so changing a group's recolors its actions' labels.
"""

from __future__ import annotations

import difflib
import uuid
from collections.abc import Collection
from dataclasses import astuple, dataclass, fields, replace
from typing import Literal

from calendar_clients.google_calendar import CalendarClient, EventLabel as RawEventLabel
from calendar_clients.google_sheets import SheetsClient, TabRange
from utilities import calendar_metadata_sheet
from utilities.action_groups import (
    CLEARABLE_FIELDS as GROUP_CLEARABLE_FIELDS,
    ActionGroup,
    GroupTree,
    ListedActionGroup,
    find as find_group,
    group_problems,
)
from utilities.row_sheet import RowSheet, check_clear, new_id, updated

ActionStatus = Literal["proposed", "active", "archived", "deleted"]
ACTION_STATUSES: tuple[str, ...] = ("proposed", "active", "archived", "deleted")
"""Where an action stands:

- `proposed`: made by the assistant (one no existing action matched) and
  not yet reviewed by the user. Holds a label if there's room.
- `active`: in use. Always holds a label.
- `archived`: not done any more, and kept out of the way.
- `deleted`: shouldn't have existed. Kept only so events pointing at it
  still make sense."""

DEFAULT_STATUSES: tuple[str, ...] = ("proposed", "active")
"""The actions listed unless others are asked for."""

MAX_LABELS = 200
"""The most event labels a calendar can have (named or not)."""

MAX_NAME_LENGTH = 50
"""An action's name is its label's name, which Calendar caps at 50."""

_LABEL_ID_NAMESPACE = uuid.UUID("8f0d3c52-41b7-4a8e-9d0a-6c1e2b7f4a93")
"""For deriving a new action's label id from its id (labels need a UUID;
action ids are short). Not the namespace goals' labels used, so an
action can't get one of theirs."""


CLEARABLE_FIELDS = frozenset({"group_id", "background_color", "priority", "note"})
"""Action fields `update_action` can blank. Not `name`/`status` (always
needed) nor the read-only `id`/`label_id`."""

_READ_ONLY_FIELDS = frozenset({"id", "label_id"})


@dataclass(kw_only=True)
class Action:
    """One action -- see the module docstring."""

    id: str | None = None
    """Immutable short id (e.g. "a7k2qp"), assigned on creation."""

    group_id: str | None = None
    """The action group it's in (see utilities/action_groups.py); `None`
    for one in no group."""

    name: str | None = None
    """A verb phrase, unique among actions and at most 50 characters --
    also its calendar label's name."""

    status: ActionStatus | None = None
    """See ACTION_STATUSES."""

    label_id: str | None = None
    """Read-only: the calendar label id reserved for it, kept while it
    doesn't hold the label, so it gets the same one back."""

    background_color: str | None = None
    """Hex color for its label; inherited from its groups when unset, or
    else derived from its priority."""

    priority: int | None = None
    """Taken by its events that don't set their own; inherited from its
    groups when unset."""

    note: str | None = None
    """Short free text, e.g. what it covers."""


@dataclass(kw_only=True)
class ListedAction(Action):
    """An action as the action tools return it: plus where it sits, what
    it inherits from its groups, and whether it holds its label."""

    path: str | None = None
    """Its groups from the top, then its name: "Creative › Guitar › Play
    guitar"."""

    effective_color: str | None = None
    """Read-only: the color its label is shown in -- its own
    background_color, or its nearest group's, or else its effective
    priority's."""

    effective_priority: int | None = None
    """Read-only: the priority its events take -- its own, or its nearest
    group's; `None` if none of them has one."""

    holds_label: bool | None = None
    """Read-only: whether it holds one of the calendar's event labels --
    see the module docstring."""


@dataclass(kw_only=True)
class ActionList:
    actions: list[ListedAction]
    """In sheet order."""

    label_slots_used: int
    """Labels on the calendar once synced: actions holding theirs, plus
    every label that isn't an action's."""

    label_slots_total: int = MAX_LABELS


@dataclass(kw_only=True)
class ActionChanges:
    """What the action-writing tools return: just what the call changed."""

    changed: list[ListedAction]
    """The actions the call created or changed, as they are now, plus any
    proposed action that gained or lost its label as a result."""

    label_slots_used: int
    label_slots_total: int = MAX_LABELS


@dataclass(kw_only=True)
class CreatedAction(ActionChanges):
    """What `create_action` returns: the new action (in `changed`) and its
    id."""

    created_id: str


@dataclass(kw_only=True)
class ActionGroupChanges:
    """What the group-writing tools return: just what the call changed."""

    changed: list[ListedActionGroup]
    """The groups the call created or changed (for a deletion, the ones
    moved up out of it), as they are now."""

    affected_actions: list[ListedAction]
    """Actions whose group, path, effective_color or effective_priority
    changed as a result, as they are now."""

    label_slots_used: int
    label_slots_total: int = MAX_LABELS


@dataclass(kw_only=True)
class CreatedActionGroup(ActionGroupChanges):
    created_id: str


@dataclass(kw_only=True)
class DeletedActionGroup(ActionGroupChanges):
    deleted: ActionGroup
    """The group as it was."""


class ActionTree:
    """Read-only lookups over one snapshot of the actions and their
    groups: what events need of them."""

    def __init__(self, actions: list[Action], groups: GroupTree) -> None:
        self.actions = actions
        self.groups = groups
        self.by_id = {a.id: a for a in actions if a.id}
        self._by_label = {a.label_id: a for a in actions if a.label_id}

    def priority(self, action_id: str) -> int | None:
        """The action's priority, or its nearest group's; `None` for an
        unknown action, or one without any."""
        action = self.by_id.get(action_id)
        return self.groups.priority(action) if action is not None else None

    def name(self, action_id: str) -> str:
        action = self.by_id.get(action_id)
        return action.name or action_id if action is not None else f"(unknown action {action_id})"

    def action_for_label(self, label_id: str | None) -> Action | None:
        return self._by_label.get(label_id) if label_id else None

    def check_action_ids(self, action_ids: Collection[str], *, already: Collection[str] = ()) -> None:
        """Raise ValueError naming any id that isn't an action, with the
        closest matches as suggestions, or that gives an event a deleted
        action -- unless it's in `already`, the actions the event has now."""
        deleted = [i for i in action_ids if i in self.by_id and self.by_id[i].status == "deleted" and i not in already]
        if deleted:
            names = ", ".join(f"{i} ({self.by_id[i].name})" for i in deleted)
            raise ValueError(f"Deleted actions can't be given to an event: {names}")
        unknown = [i for i in action_ids if i not in self.by_id]
        if unknown:
            raise ValueError("; ".join(_unknown(self.actions, i) for i in unknown))


class Actions:
    """A calendar's actions and action groups, kept in sync with its event
    labels -- see the module docstring."""

    def __init__(
        self, calendar_client: CalendarClient, sheet: RowSheet[Action], group_sheet: RowSheet[ActionGroup]
    ) -> None:
        self._calendar_client = calendar_client
        self._sheet = sheet
        self._group_sheet = group_sheet

    @staticmethod
    def ensure(calendar_client: CalendarClient, sheets_client: SheetsClient, spreadsheet_id: str) -> "Actions":
        """The calendar's actions, adding the Actions and Action Groups tabs
        the first time."""
        return Actions(
            calendar_client,
            RowSheet.ensure(
                sheets_client,
                spreadsheet_id,
                role=calendar_metadata_sheet.ACTIONS_SHEET_ROLE,
                title=calendar_metadata_sheet.ACTIONS_SHEET_TITLE,
                row_type=Action,
            ),
            RowSheet.ensure(
                sheets_client,
                spreadsheet_id,
                role=calendar_metadata_sheet.ACTION_GROUPS_SHEET_ROLE,
                title=calendar_metadata_sheet.ACTION_GROUPS_SHEET_TITLE,
                row_type=ActionGroup,
            ),
        )

    @property
    def spreadsheet_id(self) -> str:
        return self._sheet.spreadsheet_id

    @property
    def whole_tabs(self) -> list[TabRange]:
        """Both tabs, for `SheetsClient.prefetch`."""
        return [self._sheet.whole_tab, self._group_sheet.whole_tab]

    def prefetch(self, ranges: list[TabRange]) -> None:
        self._sheet.prefetch(ranges)

    # -- reading ------------------------------------------------------------

    def all(self) -> list[Action]:
        """Every action, in sheet order. (Both tabs are read together, in
        one request, inside `cached_sheet_reads`: anything reading one
        reads the other.)"""
        self.prefetch(self.whole_tabs)
        return self._sheet.read()

    def groups(self) -> GroupTree:
        """Every group."""
        self.prefetch(self.whole_tabs)
        return GroupTree(self._group_sheet.read())

    def tree(self) -> ActionTree:
        """Every action and group, as they are in the sheet now."""
        return ActionTree(self.all(), self.groups())

    def get_actions(self, statuses: Collection[str] | None = None) -> ActionList:
        """The actions with any of `statuses` (by default DEFAULT_STATUSES)."""
        statuses = _check_statuses(statuses)
        actions, tree = self.all(), self.groups()
        raw_labels, _etag = self._calendar_client.list_event_labels()
        holders = _label_holders(actions, raw_labels)
        return ActionList(
            actions=[_listed(a, tree, holders) for a in actions if a.status in statuses],
            label_slots_used=_slots_used(actions, raw_labels, holders),
        )

    def get_action(self, id_or_name: str) -> ListedAction:
        """The action with this id, or else this name (ignoring case)."""
        actions = self.all()
        action = find(actions, id_or_name)
        raw_labels, _etag = self._calendar_client.list_event_labels()
        return _listed(action, self.groups(), _label_holders(actions, raw_labels))

    def get_action_groups(self) -> list[ListedActionGroup]:
        """Every group, enclosing groups before the ones inside them."""
        tree = self.groups()
        return [tree.listed(g) for g in tree.ordered()]

    def get_action_group(self, id_or_name: str) -> ListedActionGroup:
        """The group with this id, or else this name (ignoring case)."""
        tree = self.groups()
        return tree.listed(find_group(tree.groups, id_or_name))

    # -- writing actions ----------------------------------------------------

    def create_action(self, action: Action) -> CreatedAction:
        """Add `action` (proposed unless given a status), with a new id and
        label id."""
        actions = self.all()
        new = replace(action, **{name: None for name in _READ_ONLY_FIELDS}, status=action.status or "proposed")
        new.id = new_id({a.id for a in actions})
        new.label_id = str(uuid.uuid5(_LABEL_ID_NAMESPACE, new.id))
        groups = self.groups().groups
        changes = self._commit(actions, actions + [new], groups, groups, write_groups=False)
        return CreatedAction(
            changed=[a for a in changes.affected_actions if a.id == new.id]
            + [a for a in changes.affected_actions if a.id != new.id],
            label_slots_used=changes.label_slots_used,
            created_id=new.id,
        )

    def update_action(self, action: Action, clear_fields: Collection[str] = ()) -> ActionChanges:
        """Set whichever of `action`'s fields aren't `None` (other than the
        read-only ones) on the action with `action.id`, and blank those
        named in `clear_fields`."""
        if not action.id:
            raise ValueError("update_action needs the action's id")
        check_clear(action, clear_fields, CLEARABLE_FIELDS)
        actions = self.all()
        index = next((i for i, a in enumerate(actions) if a.id == action.id), None)
        if index is None:
            raise ValueError(_unknown(actions, action.id))
        after = list(actions)
        after[index] = updated(actions[index], action, clear_fields, _READ_ONLY_FIELDS)
        groups = self.groups().groups
        changes = self._commit(actions, after, groups, groups, write_groups=False, changed_actions={action.id})
        return ActionChanges(
            changed=sorted(changes.affected_actions, key=lambda a: a.id != action.id),
            label_slots_used=changes.label_slots_used,
        )

    # -- writing groups -----------------------------------------------------

    def create_action_group(self, group: ActionGroup) -> CreatedActionGroup:
        """Add `group`, with a new id."""
        groups = self.groups().groups
        new = replace(group, id=new_id({g.id for g in groups}))
        actions = self.all()
        changes = self._commit(actions, actions, groups, groups + [new], write_actions=False)
        return CreatedActionGroup(
            changed=changes.changed,
            affected_actions=changes.affected_actions,
            label_slots_used=changes.label_slots_used,
            created_id=new.id,
        )

    def update_action_group(self, group: ActionGroup, clear_fields: Collection[str] = ()) -> ActionGroupChanges:
        """Set whichever of `group`'s fields aren't `None` (other than its
        id) on the group with `group.id`, and blank those named in
        `clear_fields`. Its actions' labels follow its color."""
        if not group.id:
            raise ValueError("update_action_group needs the group's id")
        check_clear(group, clear_fields, GROUP_CLEARABLE_FIELDS)
        groups = self.groups().groups
        index = next((i for i, g in enumerate(groups) if g.id == group.id), None)
        if index is None:
            find_group(groups, group.id)  # Raises, suggesting close matches.
        after = list(groups)
        after[index] = updated(groups[index], group, clear_fields, frozenset({"id"}))
        actions = self.all()
        return self._commit(actions, actions, groups, after, write_actions=False, changed_groups={group.id})

    def delete_action_group(self, group_id: str) -> DeletedActionGroup:
        """Delete the group with `group_id`, moving its actions and the
        groups inside it up into its own enclosing group (or to the top)."""
        groups = self.groups().groups
        deleted = next((g for g in groups if g.id == group_id), None) or find_group(groups, group_id)
        if deleted.id != group_id:
            raise ValueError(f"Delete a group by its id: {deleted.name!r} is {deleted.id}")
        groups_after = [
            replace(g, group_id=deleted.group_id) if g.group_id == group_id else g for g in groups if g.id != group_id
        ]
        actions = self.all()
        actions_after = [replace(a, group_id=deleted.group_id) if a.group_id == group_id else a for a in actions]
        changes = self._commit(
            actions,
            actions_after,
            groups,
            groups_after,
            changed_groups={g.id for g in groups if g.group_id == group_id},
        )
        return DeletedActionGroup(**vars(changes), deleted=deleted)

    # -- committing ---------------------------------------------------------

    def _commit(
        self,
        actions_before: list[Action],
        actions_after: list[Action],
        groups_before: list[ActionGroup],
        groups_after: list[ActionGroup],
        *,
        write_actions: bool = True,
        write_groups: bool = True,
        changed_actions: Collection[str] = (),
        changed_groups: Collection[str] = (),
    ) -> ActionGroupChanges:
        """Validate the actions and groups after a change, write them, then
        make the calendar's labels match. Everything that can be refused is
        checked before anything is written. Reports as changed the groups
        in `changed_groups` and any new one; as affected, the actions in
        `changed_actions`, any new one, and any other whose placement or
        label changed."""
        problems = group_problems(groups_after) + action_problems(actions_after, groups_after)
        if problems:
            raise ValueError("; ".join(problems))
        tree_before, tree_after = GroupTree(groups_before), GroupTree(groups_after)
        raw_labels, etag = self._calendar_client.list_event_labels()
        held_before = _label_holders(actions_before, raw_labels)
        holders = _label_holders(actions_after, raw_labels)
        desired = _desired_labels(actions_after, tree_after, raw_labels, holders)
        if write_groups:
            self._group_sheet.write(groups_after)
        if write_actions:
            self._sheet.write(actions_after)
        if {astuple(label) for label in desired} != {astuple(label) for label in raw_labels}:
            # The etag guards against a concurrent label change since the
            # read above (EventLabelConflictError).
            raw_labels = self._calendar_client.replace_event_labels(desired, etag)

        before_by_id = {a.id: a for a in actions_before}
        old_groups = {g.id for g in groups_before}

        def placement(action: Action, tree: GroupTree, held: Collection[str]) -> tuple:
            return action.group_id, tree.path(action), tree.color(action), tree.priority(action), action.id in held

        return ActionGroupChanges(
            changed=[
                tree_after.listed(g) for g in tree_after.ordered() if g.id in changed_groups or g.id not in old_groups
            ],
            affected_actions=[
                _listed(a, tree_after, holders)
                for a in actions_after
                if a.id in changed_actions
                or a.id not in before_by_id
                or placement(before_by_id[a.id], tree_before, held_before) != placement(a, tree_after, holders)
            ],
            label_slots_used=_slots_used(actions_after, raw_labels, holders),
        )


def find(actions: list[Action], id_or_name: str) -> Action:
    """The action with this id, or else this name (ignoring case);
    ValueError suggesting close matches if there's none."""
    by_name = {(a.name or "").casefold(): a for a in actions}
    action = next((a for a in actions if a.id == id_or_name), None) or by_name.get(id_or_name.casefold())
    if action is None:
        raise ValueError(_unknown(actions, id_or_name))
    return action


def _unknown(actions: list[Action], id_or_name: str) -> str:
    by_name = {(a.name or "").casefold(): a for a in actions if a.id}
    by_id = {a.id: a for a in actions if a.id}
    close = [by_id[i] for i in difflib.get_close_matches(id_or_name, list(by_id), n=3)] + [
        by_name[n] for n in difflib.get_close_matches(id_or_name.casefold(), list(by_name), n=3)
    ]
    hint = ", ".join(dict.fromkeys(f"{a.id} ({a.name})" for a in close))
    return (
        f"There's no action with the id or name {id_or_name!r}"
        + (f"; did you mean {hint}?" if hint else "")
        + " (get_actions lists them)"
    )


def action_problems(actions: list[Action], groups: list[ActionGroup]) -> list[str]:
    """Everything wrong with `actions` as a whole, as phrases."""
    problems = []
    ids = [a.id for a in actions]
    for action_id in sorted({i for i in ids if i and ids.count(i) > 1}):
        problems.append(f"action id {action_id!r} is used more than once")
    group_ids = {g.id for g in groups}
    names: dict[str, Action] = {}
    for action in actions:
        label = f"action {action.id}" if action.id else f"action {action.name!r}"
        if not action.id:
            problems.append(f"an action named {action.name!r} has no id")
        if not action.label_id:
            problems.append(f"{label} has no label_id")
        if not (isinstance(action.name, str) and action.name.strip()):
            problems.append(f"{label} needs a name")
        elif len(action.name) > MAX_NAME_LENGTH:
            problems.append(f"{label}'s name {action.name!r} is longer than {MAX_NAME_LENGTH} characters")
        else:
            key = action.name.strip().casefold()
            if key in names:
                other = names[key]
                problems.append(
                    f"there's already an action named {other.name!r} ({other.id}, {other.status}); "
                    "action names must be unique -- use that one, or pick another name"
                )
            names[key] = action
        if action.status not in ACTION_STATUSES:
            problems.append(f"{label}'s status must be one of {', '.join(ACTION_STATUSES)}")
        if action.priority is not None and not isinstance(action.priority, int):
            problems.append(f"{label}'s priority must be a whole number")
        if action.group_id is not None and action.group_id not in group_ids:
            problems.append(
                f"{label}'s group_id {action.group_id!r} isn't an action group (get_action_groups lists them)"
            )
    return problems


def _listed(action: Action, tree: GroupTree, holders: Collection[str]) -> ListedAction:
    return ListedAction(
        **{f.name: getattr(action, f.name) for f in fields(Action)},
        path=tree.path(action),
        effective_color=tree.color(action),
        effective_priority=tree.priority(action),
        holds_label=action.id in holders,
    )


def _foreign(actions: list[Action], raw_labels: list[RawEventLabel]) -> list[RawEventLabel]:
    """The calendar's labels that aren't any action's: always left alone."""
    ours = {a.label_id for a in actions if a.label_id}
    return [label for label in raw_labels if label.id not in ours]


def _label_holders(actions: list[Action], raw_labels: list[RawEventLabel]) -> set[str]:
    """The ids of the actions holding a label: every active one, then
    proposed ones in sheet order while there's room."""
    room = MAX_LABELS - len(_foreign(actions, raw_labels))
    holders = {a.id for a in actions if a.status == "active"}
    room -= len(holders)
    for action in actions:
        if action.status == "proposed" and room > 0:
            holders.add(action.id)
            room -= 1
    return holders


def _desired_labels(
    actions: list[Action], tree: GroupTree, raw_labels: list[RawEventLabel], holders: set[str]
) -> list[RawEventLabel]:
    """The calendar's labels as they should be; ValueError if the active
    actions need more than there's room for."""
    foreign = _foreign(actions, raw_labels)
    active = sum(1 for a in actions if a.status == "active")
    if len(foreign) + active > MAX_LABELS:
        raise ValueError(
            f"That would need {len(foreign) + active} event labels, but a calendar holds {MAX_LABELS} "
            f"({len(foreign)} aren't actions'), so at most {MAX_LABELS - len(foreign)} actions can be active "
            "at once. Archive one that isn't done any more."
        )
    return foreign + [
        RawEventLabel(id=a.label_id, name=a.name, background_color=tree.color(a)) for a in actions if a.id in holders
    ]


def _slots_used(actions: list[Action], raw_labels: list[RawEventLabel], holders: set[str]) -> int:
    return len(_foreign(actions, raw_labels)) + len(holders)


def _check_statuses(statuses: Collection[str] | None) -> tuple[str, ...]:
    if statuses is None:
        return DEFAULT_STATUSES
    unknown = sorted(set(statuses) - set(ACTION_STATUSES))
    if unknown:
        raise ValueError(f"Unknown action status(es) {unknown}; statuses are {', '.join(ACTION_STATUSES)}")
    return tuple(statuses)
