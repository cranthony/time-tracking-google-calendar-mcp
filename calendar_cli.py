"""Command-line utilities for the Google Calendar API, via CalendarClient.

This is a dev tool, not something the deployed server needs.

Usage:
    python calendar_cli.py list [from] [to]
    python calendar_cli.py get <id>
    python calendar_cli.py update_properties <id> key=value [key=value ...]
    python calendar_cli.py update <id> key=value [key=value ...]
    python calendar_cli.py create key=value [key=value ...]
    python calendar_cli.py delete <id>
    python calendar_cli.py list_raw_labels
    python calendar_cli.py create_raw_label key=value [key=value ...]
    python calendar_cli.py update_raw_label <label_id> key=value [key=value ...]
    python calendar_cli.py delete_raw_label <label_id>
    python calendar_cli.py list_actions [--status status ...] [--all]
    python calendar_cli.py create_action key=value [key=value ...]
    python calendar_cli.py update_action <action_id> [key=value ...] [--clear attribute ...]
    python calendar_cli.py note <ago> [description]
    python calendar_cli.py get_notes
    python calendar_cli.py edit_note <note_id> [--ago <ago> | --at <time>] [--description <text>]
    python calendar_cli.py delete_note <note_id>

- `list` shows events between `from` before now and `to` after now, each a
  duration parsed with pytimeparse (e.g. "1h", "90m", "2d", "1:30") —
  default window is 1 hour on each side of now.
- `get` shows a single event by its id.
- `update_properties` sets the given attributes on the event and patches
  them in, without fetching it first — any attribute not given is left
  untouched. `action_ids` is comma-separated (an empty value clears them);
  setting it also sets the event's label from its first action (see
  utilities/action_calendar.py). `facts` is JSON as stored, e.g.
  '{"location":"l7k2qp","with":["p3x9aa"],"notes":{"self":"tired"}}' (see
  utilities/facts.py), and replaces the event's facts whole.
- `update` moves/resizes an existing event (at least one of `start`/`end`
  is required; whichever is omitted is kept as the event's current
  value) via ReallocatingCalendar.update_event, reallocating time from
  the rest of its day as needed to make room for its new position — see
  utilities/reallocating_calendar.py. Prints every event that was
  created or changed as a result. Use `update_properties` instead for a
  plain patch that doesn't need to make room for anything (e.g. renaming
  an event without moving it).
- `create` builds an Event from the given attributes (`summary`, `start`,
  and `end` are required) and creates it via
  ReallocatingCalendar.create_event, reallocating time from the rest of
  its day as needed to make room — see
  utilities/reallocating_calendar.py. Prints every event that was
  created or changed as a result.
- `delete` deletes a single event by its id.
- `list_raw_labels`/`create_raw_label`/`update_raw_label`/`delete_raw_label`
  manage this calendar's custom event labels exactly as Google Calendar's
  API represents them (`CalendarClient.list_event_labels`/
  `create_event_label`/`update_event_label`/`delete_event_label`,
  `calendar_clients/google_calendar.py`'s `EventLabel`) -- a richer,
  arbitrary-hex-color alternative to `Event.colorId`'s 11 fixed colors,
  but with no concept of priority: Calendar itself has no field for one,
  so `create_raw_label`/`update_raw_label` take only
  `background_color=value`/`name=value` pairs, and `background_color` is
  required for `create_raw_label`. Labels are normally managed through
  actions (below), which hold one label each while in play; a raw label
  that isn't an action's is left alone.
- `list_actions`/`create_action`/`update_action` manage this calendar's
  actions (`utilities/actions.py`'s `Actions`), stored in the Actions tab
  of its metadata spreadsheet. Each prints the resulting actions, one per
  line: id, status, and path. `list_actions` shows the proposed and
  active ones (`--status` to pick others, repeatable; `--all` for every
  status) without changing anything. `create_action` needs `name=...`;
  `update_action` sets whichever attributes are given and blanks any
  named with `--clear`. `status=archived` (or deleted) frees the
  action's label; `status=active` restores it.
- `note` records a new uncompacted time note -- `ago` is required, and
  (like `list`'s `from`/`to` above) a pytimeparse duration (e.g. "1h",
  "90m", "0s" for right now) giving how long before *now* this note is
  for, resolved the same way `list`'s window is (see `resolve_note_
  timestamp`). `description` is an optional free-text note about what
  that moment marks (quote it if it contains spaces). Appended to this
  calendar's tracked noted-times tab (`utilities/noted_time_sheet.py`'s
  `NotedTimeSheet`, creating that tab, pre-populated with just its
  header row, the first time this runs if it doesn't exist yet).
- `get_notes` lists every uncompacted note, sorted by timestamp, each
  with its id (`utilities/noted_time_sheet.py`'s `SheetNote.id`) --
  what `edit_note`/`delete_note` take.
- `edit_note` corrects an uncompacted note: a new time (`--ago`, a
  duration before now like `note`'s, or `--at`, an ISO 8601 time) and/or
  a new `--description` (`--description ""` clears it). Whatever isn't
  given keeps its current value. Prints the edited note -- its id changes
  if its time did. `delete_note` removes one. Both refuse a compacted
  note, a stale id (list the notes again), or a note in a compaction
  that's partway through being applied (see
  `utilities/note_compactor.py`'s `edit_note`/`delete_note`).
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import pytimeparse

from calendar_clients.google_calendar import CalendarClient, Event
from calendar_clients.google_calendar import EventLabel as RawEventLabel
from config import (
    build_calendar_client,
    build_actions,
    build_compaction_journal,
    build_noted_time_sheet,
)
from utilities.action_calendar import ActionCalendar
from utilities.actions import ACTION_STATUSES, CLEARABLE_FIELDS, Action, ActionChanges, ActionList
from utilities.facts import Facts
from utilities.note_compaction import CompactionError
from utilities.note_compactor import delete_note, edit_note
from utilities.noted_time_sheet import NotedTime, SheetNote
from utilities.reallocation import ReallocationOptions
from utilities.reallocating_calendar import ReallocatingCalendar

DEFAULT_WINDOW = "1h"


def _parse_duration(value: str) -> float:
    """Parse a pytimeparse duration string into seconds, for use as an
    argparse `type`."""
    seconds = pytimeparse.parse(value)
    if seconds is None:
        raise argparse.ArgumentTypeError(f"could not parse duration: {value!r}")
    return seconds


def _parse_bool(value: str) -> bool:
    lowered = value.lower()
    if lowered in ("true", "1", "yes"):
        return True
    if lowered in ("false", "0", "no"):
        return False
    raise ValueError(f"could not parse boolean: {value!r}")


def _parse_status(value: str) -> str:
    if value not in ACTION_STATUSES:
        raise ValueError(f"expected one of {', '.join(ACTION_STATUSES)}")
    return value


def _parse_iso_datetime(value: str) -> datetime:
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError(f"datetime {value!r} must include a UTC offset/timezone")
    return parsed


# Every Event attribute that update_properties may set, other than `id`
# (changing id would repoint the patch at a different event) and
# `recurring_event_id` (assigned by Google, never sent to the API -- setting
# it here would silently have no effect) or `action_priority` (the event's
# actions', never sent to the API either), mapped to a function parsing its
# command-line string value into the right type.
_UPDATABLE_ATTRIBUTE_PARSERS: dict[str, Callable[[str], Any]] = {
    "summary": str,
    "start": _parse_iso_datetime,
    "end": _parse_iso_datetime,
    "description": str,
    "location": str,
    "status": str,
    "min_duration": lambda s: timedelta(seconds=_parse_duration(s)),
    "is_fixed_duration": _parse_bool,
    "is_fixed_time": _parse_bool,
    "priority": int,
    "is_end_of_day_sleep": _parse_bool,
    "event_label_id": str,
    "action_ids": lambda s: [action_id.strip() for action_id in s.split(",") if action_id.strip()],
    "facts": lambda s: _parse_facts(s),
}


def _parse_facts(value: str) -> Facts:
    facts = Facts.from_json(value)
    if facts is None:
        raise ValueError(f"facts must be a JSON object, e.g. '{{\"with\":[\"p3x9aa\"]}}', not {value!r}")
    return facts.normalized()


_REQUIRED_CREATE_ATTRIBUTES = frozenset({"summary", "start", "end"})
"""Event attributes `create` won't build an event without."""

_UPDATE_POSITION_ATTRIBUTES = frozenset({"start", "end"})
"""`update` requires at least one of these -- reallocation needs a real
position to make room for, unlike `update_properties`'s plain patch. Either
may be omitted: ReallocatingCalendar.update_event fills in whichever one
is missing from the event's current value."""


_RAW_LABEL_ATTRIBUTE_PARSERS: dict[str, Callable[[str], Any]] = {
    "background_color": str,
    "name": str,
}
"""Every calendar_clients.google_calendar.EventLabel attribute
create_raw_label/update_raw_label may set, mapped to a function parsing
its command-line string value into the right type. create_raw_label
requires background_color (enforced by CalendarClient.create_event_label
itself, not here)."""


_ACTION_ATTRIBUTE_PARSERS: dict[str, Callable[[str], Any]] = {
    "group_id": str,
    "name": str,
    "status": _parse_status,
    "background_color": str,
    "priority": int,
    "note": str,
}
"""Every utilities.actions.Action attribute create_action/update_action
may set, mapped to a function parsing its command-line string value."""


def _parse_key_value_pair(
    value: str, attribute_parsers: dict[str, Callable[[str], Any]]
) -> tuple[str, Any]:
    """Parse a "key=value" command-line argument into (attribute name,
    parsed value), looking `key` up in `attribute_parsers` to find how to
    parse `value` -- the shared logic behind `_parse_event_key_value`
    (Event attributes), `_parse_raw_label_key_value` (raw EventLabel
    attributes), and `_parse_action_key_value` (Action attributes)."""
    if "=" not in value:
        raise argparse.ArgumentTypeError(f"expected key=value, got {value!r}")
    key, raw_value = value.split("=", 1)
    parse = attribute_parsers.get(key)
    if parse is None:
        valid = ", ".join(sorted(attribute_parsers))
        raise argparse.ArgumentTypeError(f"unknown attribute {key!r}; expected one of: {valid}")
    try:
        return key, parse(raw_value)
    except (ValueError, argparse.ArgumentTypeError) as exc:
        raise argparse.ArgumentTypeError(f"invalid value for {key!r}: {exc}") from exc


def _parse_event_key_value(value: str) -> tuple[str, Any]:
    """Parse a "key=value" command-line argument into (Event attribute
    name, parsed value), for use as an argparse `type`."""
    return _parse_key_value_pair(value, _UPDATABLE_ATTRIBUTE_PARSERS)


def _parse_raw_label_key_value(value: str) -> tuple[str, Any]:
    """Parse a "key=value" command-line argument into (raw EventLabel
    attribute name, parsed value), for use as an argparse `type`."""
    return _parse_key_value_pair(value, _RAW_LABEL_ATTRIBUTE_PARSERS)


def _parse_action_key_value(value: str) -> tuple[str, Any]:
    """Parse a "key=value" command-line argument into (Action attribute
    name, parsed value), for use as an argparse `type`."""
    return _parse_key_value_pair(value, _ACTION_ATTRIBUTE_PARSERS)


def resolve_window(
    from_seconds: float, to_seconds: float, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """Resolve a (from, to) pair of second-offsets around `now` (default: the
    current time) into a (time_min, time_max) pair of datetimes."""
    if now is None:
        now = datetime.now(timezone.utc)
    return now - timedelta(seconds=from_seconds), now + timedelta(seconds=to_seconds)


def resolve_note_timestamp(seconds_ago: float, now: datetime | None = None) -> datetime:
    """Resolve `seconds_ago` (how long before `now` -- default: the
    current time -- a note is for) into an absolute datetime, the same
    "duration relative to now" convention `resolve_window` uses for
    `list`'s `from`/`to`."""
    if now is None:
        now = datetime.now(timezone.utc)
    return now - timedelta(seconds=seconds_ago)


def _format_event_line(event: Event) -> str:
    return f"{event.id}\t{event.start.isoformat()} - {event.end.isoformat()}\t{event.summary}"


def _format_event_details(event: Event | RawEventLabel | NotedTime) -> str:
    lines = []
    for field in dataclasses.fields(event):
        value = getattr(event, field.name)
        if value is not None:
            lines.append(f"{field.name}: {value}")
    return "\n".join(lines)


def _format_raw_event_label_line(label: RawEventLabel) -> str:
    return f"{label.id}\t{label.background_color}\t{label.name or ''}"


def _print_actions(action_list: ActionList) -> None:
    if not action_list.actions:
        print("No actions found.")
    for action in action_list.actions:
        print(f"{action.id}\t{action.status}\t{action.path}")
    print(f"({action_list.label_slots_used} of {action_list.label_slots_total} event labels in use)")


def _print_action_changes(changes: ActionChanges) -> None:
    if not changes.changed:
        print("No actions changed.")
    for action in changes.changed:
        print(f"{action.id}\t{action.status}\t{action.path}")
    print(f"({changes.label_slots_used} of {changes.label_slots_total} event labels in use)")


def _format_sheet_note_line(sheet_note: SheetNote) -> str:
    note = sheet_note.note
    return f"{sheet_note.id}\t{note.timestamp.isoformat()}\t{note.description or ''}"


def _parse_time(value: str) -> datetime:
    """An ISO 8601 time; one without a UTC offset is taken as local time."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{value!r} isn't an ISO 8601 time") from exc
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Google Calendar API command-line utilities.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List events in a window around now.")
    list_parser.add_argument(
        "from_seconds",
        metavar="from",
        nargs="?",
        type=_parse_duration,
        default=DEFAULT_WINDOW,
        help=(
            "How far before now to start, as a pytimeparse duration "
            f'(e.g. "1h", "90m"). Default: {DEFAULT_WINDOW}.'
        ),
    )
    list_parser.add_argument(
        "to_seconds",
        metavar="to",
        nargs="?",
        type=_parse_duration,
        default=DEFAULT_WINDOW,
        help=f"How far after now to end, as a pytimeparse duration. Default: {DEFAULT_WINDOW}.",
    )

    get_parser = subparsers.add_parser("get", help="Get a single event by id.")
    get_parser.add_argument("id", help="The event id.")

    update_parser = subparsers.add_parser(
        "update_properties", help="Set one or more properties on an existing event."
    )
    update_parser.add_argument("id", help="The event id.")
    update_parser.add_argument(
        "properties",
        metavar="key=value",
        nargs="+",
        type=_parse_event_key_value,
        help=(
            "One or more Event attribute=value pairs to set. Valid "
            f"attributes: {', '.join(sorted(_UPDATABLE_ATTRIBUTE_PARSERS))}."
        ),
    )

    update_reallocate_parser = subparsers.add_parser(
        "update",
        help="Move/resize an existing event, reallocating time from its day as needed.",
    )
    update_reallocate_parser.add_argument("id", help="The event id.")
    update_reallocate_parser.add_argument(
        "properties",
        metavar="key=value",
        nargs="+",
        type=_parse_event_key_value,
        help=(
            "One or more Event attribute=value pairs; at least one of "
            f"{', '.join(sorted(_UPDATE_POSITION_ATTRIBUTES))} is required (the other is "
            "kept as-is if omitted). Valid attributes: "
            f"{', '.join(sorted(_UPDATABLE_ATTRIBUTE_PARSERS))}."
        ),
    )

    create_parser = subparsers.add_parser(
        "create", help="Create a new event, reallocating time from its day as needed."
    )
    create_parser.add_argument(
        "properties",
        metavar="key=value",
        nargs="+",
        type=_parse_event_key_value,
        help=(
            "One or more Event attribute=value pairs; "
            f"{', '.join(sorted(_REQUIRED_CREATE_ATTRIBUTES))} are required. Valid "
            f"attributes: {', '.join(sorted(_UPDATABLE_ATTRIBUTE_PARSERS))}."
        ),
    )

    delete_parser = subparsers.add_parser("delete", help="Delete an event by id.")
    delete_parser.add_argument("id", help="The event id.")

    subparsers.add_parser(
        "list_raw_labels", help="List this calendar's custom event labels, as Calendar stores them."
    )

    create_raw_label_parser = subparsers.add_parser(
        "create_raw_label", help="Create a new event label, exactly as Calendar stores it."
    )
    create_raw_label_parser.add_argument(
        "properties",
        metavar="key=value",
        nargs="+",
        type=_parse_raw_label_key_value,
        help=(
            "One or more raw EventLabel attribute=value pairs; background_color is "
            f"required. Valid attributes: {', '.join(sorted(_RAW_LABEL_ATTRIBUTE_PARSERS))}."
        ),
    )

    update_raw_label_parser = subparsers.add_parser(
        "update_raw_label", help="Update an existing event label's color and/or name."
    )
    update_raw_label_parser.add_argument("label_id", help="The label id.")
    update_raw_label_parser.add_argument(
        "properties",
        metavar="key=value",
        nargs="+",
        type=_parse_raw_label_key_value,
        help=(
            "One or more raw EventLabel attribute=value pairs to set. Valid "
            f"attributes: {', '.join(sorted(_RAW_LABEL_ATTRIBUTE_PARSERS))}."
        ),
    )

    delete_raw_label_parser = subparsers.add_parser(
        "delete_raw_label", help="Delete an event label by id."
    )
    delete_raw_label_parser.add_argument("label_id", help="The label id.")

    list_actions_parser = subparsers.add_parser("list_actions", help="List this calendar's actions.")
    list_actions_parser.add_argument(
        "--status",
        action="append",
        choices=ACTION_STATUSES,
        help="A status to list; repeat for several. Default: proposed and active.",
    )
    list_actions_parser.add_argument("--all", action="store_true", help="List actions of every status.")

    create_action_parser = subparsers.add_parser("create_action", help="Create a new action.")
    create_action_parser.add_argument(
        "properties",
        metavar="key=value",
        nargs="+",
        type=_parse_action_key_value,
        help=(
            "Action attribute=value pairs; name is required. Valid attributes: "
            f"{', '.join(sorted(_ACTION_ATTRIBUTE_PARSERS))}."
        ),
    )

    update_action_parser = subparsers.add_parser("update_action", help="Update any of an action's properties.")
    update_action_parser.add_argument("action_id", help="The action id.")
    update_action_parser.add_argument(
        "properties",
        metavar="key=value",
        nargs="*",
        type=_parse_action_key_value,
        help=f"Action attribute=value pairs to set. Valid attributes: {', '.join(sorted(_ACTION_ATTRIBUTE_PARSERS))}.",
    )
    update_action_parser.add_argument(
        "--clear",
        action="append",
        default=[],
        choices=sorted(CLEARABLE_FIELDS),
        help="An attribute to blank; repeat for several.",
    )

    note_parser = subparsers.add_parser("note", help="Record a new uncompacted time note.")
    note_parser.add_argument(
        "ago",
        type=_parse_duration,
        help=(
            "How long before now this note is for, as a pytimeparse duration "
            '(e.g. "1h", "90m", "0s" for right now).'
        ),
    )
    note_parser.add_argument(
        "description", nargs="?", help="Optional free-text description of what this marks."
    )

    subparsers.add_parser(
        "get_notes", help="List every uncompacted time note, with its id, sorted by timestamp."
    )

    edit_note_parser = subparsers.add_parser(
        "edit_note", help="Change an uncompacted note's time and/or description."
    )
    edit_note_parser.add_argument("note_id", help="The note's id, as get_notes prints it.")
    time_group = edit_note_parser.add_mutually_exclusive_group()
    time_group.add_argument(
        "--ago",
        type=_parse_duration,
        help='New time, as a pytimeparse duration before now (e.g. "1h", "90m").',
    )
    time_group.add_argument(
        "--at", type=_parse_time, help="New time, in ISO 8601 (local time if no UTC offset)."
    )
    edit_note_parser.add_argument(
        "--description", help='New description ("" clears it).'
    )

    delete_note_parser = subparsers.add_parser("delete_note", help="Delete an uncompacted note.")
    delete_note_parser.add_argument("note_id", help="The note's id, as get_notes prints it.")

    return parser


def _build_reallocating_calendar(client: CalendarClient) -> ReallocatingCalendar:
    """A ReallocatingCalendar wrapping client plus an ActionCalendar, so
    reallocation sees an event's action-derived priority as the fallback
    whenever the event itself doesn't set one. Built lazily -- only
    `update`/`create` below need it -- since constructing an Actions may
    create this calendar's Actions tab on first use."""
    return ReallocatingCalendar(ActionCalendar(client, build_actions()))


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    client = build_calendar_client()

    if args.command == "list":
        time_min, time_max = resolve_window(args.from_seconds, args.to_seconds)
        events = client.list_events(time_min, time_max)
        if not events:
            print("No events found.")
        for event in events:
            print(_format_event_line(event))
    elif args.command == "get":
        event = client.get_event(args.id)
        print(_format_event_details(event))
    elif args.command == "update_properties":
        event = Event(id=args.id)
        for key, value in args.properties:
            setattr(event, key, value)
        # Setting actions also sets the label they imply.
        writer = ActionCalendar(client, build_actions()) if event.action_ids is not None else client
        updated_event = writer.update_event(event)
        print(_format_event_details(updated_event))
    elif args.command == "update":
        fields = dict(args.properties)
        if not _UPDATE_POSITION_ATTRIBUTES & fields.keys():
            parser.error(
                f"update requires at least one of: {', '.join(sorted(_UPDATE_POSITION_ATTRIBUTES))}"
            )
        updated_event = Event(id=args.id, **fields)
        applied_events = _build_reallocating_calendar(client).update_event(
            updated_event, ReallocationOptions()
        )
        for event in applied_events:
            print(_format_event_details(event))
            print()
    elif args.command == "create":
        fields = dict(args.properties)
        missing = _REQUIRED_CREATE_ATTRIBUTES - fields.keys()
        if missing:
            parser.error(f"create requires: {', '.join(sorted(missing))}")
        new_event = Event(**fields)
        applied_events = _build_reallocating_calendar(client).create_event(
            new_event, ReallocationOptions()
        )
        for event in applied_events:
            print(_format_event_details(event))
            print()
    elif args.command == "delete":
        client.delete_event(args.id)
        print(f"Deleted event {args.id}.")
    elif args.command == "list_raw_labels":
        labels, _etag = client.list_event_labels()
        if not labels:
            print("No event labels found.")
        for label in labels:
            print(_format_raw_event_label_line(label))
    elif args.command == "create_raw_label":
        fields = dict(args.properties)
        if "background_color" not in fields:
            parser.error("create_raw_label requires: background_color")
        label = client.create_event_label(fields["background_color"], fields.get("name"))
        print(_format_event_details(label))
    elif args.command == "update_raw_label":
        fields = dict(args.properties)
        label = client.update_event_label(
            args.label_id,
            background_color=fields.get("background_color"),
            name=fields.get("name"),
        )
        print(_format_event_details(label))
    elif args.command == "delete_raw_label":
        label = client.delete_event_label(args.label_id)
        print(f"Deleted event label {label.id}.")
    elif args.command in ("list_actions", "create_action", "update_action"):
        try:
            if args.command == "list_actions":
                _print_actions(build_actions().get_actions(ACTION_STATUSES if args.all else args.status))
            elif args.command == "create_action":
                _print_action_changes(build_actions().create_action(Action(**dict(args.properties))))
            else:
                if not args.properties and not args.clear:
                    parser.error("update_action requires at least one key=value or --clear")
                action = Action(id=args.action_id, **dict(args.properties))
                _print_action_changes(build_actions().update_action(action, args.clear))
        except ValueError as exc:
            sys.exit(f"error: {exc}")
    elif args.command == "note":
        noted_time = NotedTime(
            timestamp=resolve_note_timestamp(args.ago), description=args.description
        )
        appended = build_noted_time_sheet().append(noted_time)
        print(f"id: {appended.id}")
        print(_format_event_details(noted_time))
    elif args.command == "get_notes":
        sheet_notes = sorted(build_noted_time_sheet().read_with_rows(), key=lambda n: n.note.timestamp)
        if not sheet_notes:
            print("No notes found.")
        for sheet_note in sheet_notes:
            print(_format_sheet_note_line(sheet_note))
    elif args.command == "edit_note":
        if args.ago is None and args.at is None and args.description is None:
            parser.error("edit_note needs at least one of --ago, --at or --description")
        timestamp = resolve_note_timestamp(args.ago) if args.ago is not None else args.at
        try:
            edited = edit_note(
                build_noted_time_sheet(),
                build_compaction_journal(),
                args.note_id,
                timestamp=timestamp,
                description=args.description,
            )
        except CompactionError as exc:
            sys.exit(f"error: {exc}")
        print(f"id: {edited.id}")
        print(_format_event_details(edited.note))
    elif args.command == "delete_note":
        try:
            deleted = delete_note(build_noted_time_sheet(), build_compaction_journal(), args.note_id)
        except CompactionError as exc:
            sys.exit(f"error: {exc}")
        print("Deleted:")
        print(_format_event_details(deleted))


if __name__ == "__main__":
    main()
