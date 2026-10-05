"""Actions: what the user does with their time, each a verb for what
they're doing in a moment ("play guitar", "eat a meal"). They're the
leaves events are tagged with; action groups (see utilities/
action_groups.py) roll them up.

**Where they live.** The **Actions** tab of the calendar's metadata
spreadsheet (utilities/calendar_metadata_sheet.py), one row per action,
read by header name (utilities/row_sheet.py), so it can be edited by
hand.

**Labels.** Like goals (utilities/goals.py), each action reserves one of
the calendar's event label ids when it's created, and holds that label --
its name and color shown on the calendar -- while it's in play:

- every `active` action holds its label; if they'd need more labels than
  the calendar has room for, the change is refused;
- `proposed` actions (made by the assistant and not yet reviewed) hold
  theirs too, in sheet order, while there's room left over;
- `archived` and `deleted` ones don't: their labels are removed, and come
  back under the same id if they're made active again.

Labels that aren't any action's (Calendar's own unnamed ones, and any
other app's or goal's) are always left alone, and count against the
calendar's room.
"""

from __future__ import annotations

import difflib
import secrets
import uuid
from collections.abc import Collection
from dataclasses import astuple, dataclass, fields, replace
from typing import Literal

from calendar_clients.google_calendar import CalendarClient, EventLabel as RawEventLabel, color_for_priority
from calendar_clients.google_sheets import SheetsClient, TabRange
from utilities import calendar_metadata_sheet
from utilities.row_sheet import RowSheet

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
action ids are short). Not goals' namespace, so an action can't get a
goal's label id."""

_ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_ID_LENGTH = 6

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
    """Hex color for its label; derived from its priority when unset."""

    priority: int | None = None
    """Taken by its events that don't set their own."""

    note: str | None = None
    """Short free text, e.g. what it covers."""


@dataclass(kw_only=True)
class ListedAction(Action):
    """An action as the action tools return it: plus what it inherits and
    whether it holds its label."""

    effective_color: str | None = None
    """Read-only: the color its label is shown in -- its own
    background_color, or else its priority's."""

    effective_priority: int | None = None
    """Read-only: the priority its events take, if it has one."""

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


class Actions:
    """A calendar's actions, kept in sync with its event labels -- see the
    module docstring."""

    def __init__(self, calendar_client: CalendarClient, sheet: RowSheet[Action]) -> None:
        self._calendar_client = calendar_client
        self._sheet = sheet

    @staticmethod
    def ensure(calendar_client: CalendarClient, sheets_client: SheetsClient, spreadsheet_id: str) -> "Actions":
        """The calendar's actions, adding the Actions tab the first time."""
        return Actions(
            calendar_client,
            RowSheet.ensure(
                sheets_client,
                spreadsheet_id,
                role=calendar_metadata_sheet.ACTIONS_SHEET_ROLE,
                title=calendar_metadata_sheet.ACTIONS_SHEET_TITLE,
                row_type=Action,
            ),
        )

    @property
    def whole_tab(self) -> TabRange:
        return self._sheet.whole_tab

    def prefetch(self, ranges: list[TabRange]) -> None:
        self._sheet.prefetch(ranges)

    # -- reading ------------------------------------------------------------

    def all(self) -> list[Action]:
        """Every action, in sheet order."""
        return self._sheet.read()

    def get_actions(self, statuses: Collection[str] | None = None) -> ActionList:
        """The actions with any of `statuses` (by default DEFAULT_STATUSES)."""
        statuses = _check_statuses(statuses)
        actions = self.all()
        raw_labels, _etag = self._calendar_client.list_event_labels()
        holders = _label_holders(actions, raw_labels)
        return ActionList(
            actions=[_listed(a, holders) for a in actions if a.status in statuses],
            label_slots_used=_slots_used(actions, raw_labels, holders),
        )

    def get_action(self, id_or_name: str) -> ListedAction:
        """The action with this id, or else this name (ignoring case)."""
        actions = self.all()
        action = find(actions, id_or_name)
        raw_labels, _etag = self._calendar_client.list_event_labels()
        return _listed(action, _label_holders(actions, raw_labels))

    # -- writing ------------------------------------------------------------

    def create_action(self, action: Action) -> CreatedAction:
        """Add `action` (proposed unless given a status), with a new id and
        label id."""
        actions = self.all()
        new = replace(action, **{name: None for name in _READ_ONLY_FIELDS}, status=action.status or "proposed")
        taken = {a.id for a in actions}
        new.id = _new_id(taken)
        new.label_id = str(uuid.uuid5(_LABEL_ID_NAMESPACE, new.id))
        changes = self._commit(actions, actions + [new], new.id)
        return CreatedAction(**{f.name: getattr(changes, f.name) for f in fields(ActionChanges)}, created_id=new.id)

    def update_action(self, action: Action, clear_fields: Collection[str] = ()) -> ActionChanges:
        """Set whichever of `action`'s fields aren't `None` (other than the
        read-only ones) on the action with `action.id`, and blank those
        named in `clear_fields`."""
        if not action.id:
            raise ValueError("update_action needs the action's id")
        unknown = set(clear_fields) - CLEARABLE_FIELDS
        if unknown:
            raise ValueError(f"Can't clear {sorted(unknown)}; clearable fields are {sorted(CLEARABLE_FIELDS)}")
        both = sorted(name for name in clear_fields if getattr(action, name) is not None)
        if both:
            raise ValueError(f"Can't both set and clear {both}")
        actions = self.all()
        index = next((i for i, a in enumerate(actions) if a.id == action.id), None)
        if index is None:
            raise ValueError(_unknown(actions, action.id))
        updated = replace(
            actions[index],
            **{
                f.name: getattr(action, f.name)
                for f in fields(Action)
                if f.name not in _READ_ONLY_FIELDS and getattr(action, f.name) is not None
            },
            **{name: None for name in clear_fields},
        )
        after = list(actions)
        after[index] = updated
        return self._commit(actions, after, action.id)

    def _commit(self, before: list[Action], after: list[Action], changed_id: str) -> ActionChanges:
        """Validate `after`, write it to the sheet, then make the
        calendar's labels match. Everything that can be refused is checked
        before anything is written."""
        problems = action_problems(after)
        if problems:
            raise ValueError("; ".join(problems))
        raw_labels, etag = self._calendar_client.list_event_labels()
        holders = _label_holders(after, raw_labels)
        desired = _desired_labels(after, raw_labels, holders)
        held_before = _label_holders(before, raw_labels)
        self._sheet.write(after)
        if {astuple(label) for label in desired} != {astuple(label) for label in raw_labels}:
            # The etag guards against a concurrent label change since the
            # read above (EventLabelConflictError).
            raw_labels = self._calendar_client.replace_event_labels(desired, etag)
        changed = [
            _listed(a, holders)
            for a in after
            if a.id == changed_id or (a.id in held_before) != (a.id in holders)
        ]
        return ActionChanges(changed=changed, label_slots_used=_slots_used(after, raw_labels, holders))


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


def action_problems(actions: list[Action]) -> list[str]:
    """Everything wrong with `actions` as a whole, as phrases."""
    problems = []
    ids = [a.id for a in actions]
    for action_id in sorted({i for i in ids if i and ids.count(i) > 1}):
        problems.append(f"action id {action_id!r} is used more than once")
    names: dict[str, Action] = {}
    for action in actions:
        label = f"action {action.id or action.name!r}"
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
    return problems


def _new_id(taken: Collection[str | None]) -> str:
    while True:
        action_id = "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))
        if action_id not in taken:
            return action_id


def _color(action: Action) -> str:
    return action.background_color or color_for_priority(action.priority)[1]


def _listed(action: Action, holders: Collection[str]) -> ListedAction:
    return ListedAction(
        **{f.name: getattr(action, f.name) for f in fields(Action)},
        effective_color=_color(action),
        effective_priority=action.priority,
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


def _desired_labels(actions: list[Action], raw_labels: list[RawEventLabel], holders: set[str]) -> list[RawEventLabel]:
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
        RawEventLabel(id=a.label_id, name=a.name, background_color=_color(a)) for a in actions if a.id in holders
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
