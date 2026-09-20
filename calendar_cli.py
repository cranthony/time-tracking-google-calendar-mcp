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
    python calendar_cli.py create_label key=value [key=value ...]
    python calendar_cli.py update_label <label_id> key=value [key=value ...]
    python calendar_cli.py sync_labels
    python calendar_cli.py note <ago> [description]
    python calendar_cli.py get_notes
    python calendar_cli.py clear_notes

- `list` shows events between `from` before now and `to` after now, each a
  duration parsed with pytimeparse (e.g. "1h", "90m", "2d", "1:30") —
  default window is 1 hour on each side of now.
- `get` shows a single event by its id.
- `update_properties` sets the given attributes on the event and patches
  them in, without fetching it first — any attribute not given is left
  untouched.
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
  required for `create_raw_label`. Prefer `create_label`/`update_label`
  below unless you specifically want to bypass priority-derived colors
  and the event label sheet.
- `create_label`/`update_label` manage the same labels, but as
  `utilities/event_labels.py`'s richer `EventLabel` (via `EventLabels`),
  which also has a `priority` and a `fixed_time` flag
  (`background_color=value`/`name=value`/`priority=value`/
  `fixed_time=value` pairs; `background_color` may be left unset if
  `priority` is given, deriving it the same way `Event.colorId` does;
  whichever is omitted on `update_label` keeps its current value). Both
  read and write through this calendar's event label sheet (creating
  one, pre-populated with the calendar's current labels, the first time
  either of them runs if it doesn't exist yet), which is the only place
  `priority`/`fixed_time` are remembered, and both print the *entire*
  resulting label list, not just the one label touched -- along with
  `sync_labels` below, that's also how you list the current labels;
  there's no separate `list_labels` command, since it would just be
  `sync_labels` under a misleading name. There's no `delete_label`
  either -- delete a row from the sheet directly (e.g. by opening it in
  Google Sheets) and run `sync_labels` to apply that. Defining a label here doesn't do
  anything to any event on its own -- set an event's `event_label_id`
  (via `create`/`update`/`update_properties` above) to assign one. See
  https://developers.google.com/workspace/calendar/api/guides/labels
- `sync_labels` makes this calendar's event labels match its event label
  sheet exactly (via `EventLabels.sync_labels`) -- change a row's
  color/priority, add a row with a blank ID to create a new label, or
  delete a row to delete its label, then run this to apply those changes
  back to the calendar (and to see the resulting labels, even with no
  sheet changes pending).
- `note` records a new uncompacted time note -- `ago` is required, and
  (like `list`'s `from`/`to` above) a pytimeparse duration (e.g. "1h",
  "90m", "0s" for right now) giving how long before *now* this note is
  for, resolved the same way `list`'s window is (see `resolve_note_
  timestamp`). `description` is an optional free-text note about what
  that moment marks (quote it if it contains spaces). Appended to this
  calendar's tracked noted-times tab (`utilities/noted_time_sheet.py`'s
  `NotedTimeSheet`, creating that tab, pre-populated with just its
  header row, the first time this runs if it doesn't exist yet).
- `get_notes` lists every recorded note, sorted by timestamp (via
  `NotedTimeSheet.read`).
- `clear_notes` removes every recorded note (via `NotedTimeSheet.clear`)
  and prints the ones that were cleared, sorted by timestamp -- run
  `get_notes` first if you want to see them before clearing. There's
  still no command to delete a single note; open the sheet directly for
  that.
"""

from __future__ import annotations

import argparse
import dataclasses
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import pytimeparse

from calendar_clients.google_calendar import CalendarClient, Event
from calendar_clients.google_calendar import EventLabel as RawEventLabel
from config import build_calendar_client, build_event_labels, build_noted_time_sheet
from utilities.event_labels import EventLabel
from utilities.label_priority_calendar import LabelPriorityCalendar
from utilities.noted_time_sheet import NotedTime
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


def _parse_iso_datetime(value: str) -> datetime:
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError(f"datetime {value!r} must include a UTC offset/timezone")
    return parsed


# Every Event attribute that update_properties may set, other than `id`
# (changing id would repoint the patch at a different event) and
# `recurring_event_id` (assigned by Google, never sent to the API -- setting
# it here would silently have no effect), mapped to a function parsing its
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
}


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


_LABEL_ATTRIBUTE_PARSERS: dict[str, Callable[[str], Any]] = {
    "background_color": str,
    "name": str,
    "priority": int,
    "fixed_time": _parse_bool,
}
"""Every utilities.event_labels.EventLabel attribute create_label/
update_label may set, mapped to a function parsing its command-line
string value into the right type. create_label needs at least one of
background_color/priority (background_color is derived from priority if
omitted, defaulting even further if neither is given -- see
EventLabels.create_label) -- there's no fixed set of "required" keys the
way _REQUIRED_CREATE_ATTRIBUTES is for Event, since either one alone is
enough."""


def _parse_key_value_pair(
    value: str, attribute_parsers: dict[str, Callable[[str], Any]]
) -> tuple[str, Any]:
    """Parse a "key=value" command-line argument into (attribute name,
    parsed value), looking `key` up in `attribute_parsers` to find how to
    parse `value` -- the shared logic behind `_parse_event_key_value`
    (Event attributes), `_parse_raw_label_key_value` (raw EventLabel
    attributes), and `_parse_label_key_value` (utilities.event_labels.
    EventLabel attributes)."""
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


def _parse_label_key_value(value: str) -> tuple[str, Any]:
    """Parse a "key=value" command-line argument into (EventLabel
    attribute name, parsed value), for use as an argparse `type`."""
    return _parse_key_value_pair(value, _LABEL_ATTRIBUTE_PARSERS)


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


def _format_event_details(event: Event | RawEventLabel | EventLabel | NotedTime) -> str:
    lines = []
    for field in dataclasses.fields(event):
        value = getattr(event, field.name)
        if value is not None:
            lines.append(f"{field.name}: {value}")
    return "\n".join(lines)


def _format_raw_event_label_line(label: RawEventLabel) -> str:
    return f"{label.id}\t{label.background_color}\t{label.name or ''}"


def _format_event_label_line(label: EventLabel) -> str:
    priority = label.priority if label.priority is not None else ""
    return f"{label.id}\t{label.background_color}\t{priority}\t{label.name or ''}"


def _format_noted_time_line(noted_time: NotedTime) -> str:
    return f"{noted_time.timestamp.isoformat()}\t{noted_time.description or ''}"


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

    create_label_parser = subparsers.add_parser("create_label", help="Create a new event label.")
    create_label_parser.add_argument(
        "properties",
        metavar="key=value",
        nargs="+",
        type=_parse_label_key_value,
        help=(
            "One or more EventLabel attribute=value pairs; at least one of "
            "background_color/priority is required (background_color is derived from "
            f"priority if omitted). Valid attributes: {', '.join(sorted(_LABEL_ATTRIBUTE_PARSERS))}."
        ),
    )

    update_label_parser = subparsers.add_parser(
        "update_label", help="Update an existing event label's color, name, and/or priority."
    )
    update_label_parser.add_argument("label_id", help="The label id.")
    update_label_parser.add_argument(
        "properties",
        metavar="key=value",
        nargs="+",
        type=_parse_label_key_value,
        help=(
            "One or more EventLabel attribute=value pairs to set. Valid "
            f"attributes: {', '.join(sorted(_LABEL_ATTRIBUTE_PARSERS))}."
        ),
    )

    subparsers.add_parser(
        "sync_labels", help="Sync event labels from this calendar's tracked event label sheet."
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
        "get_notes", help="List every recorded uncompacted time note, sorted by timestamp."
    )

    subparsers.add_parser(
        "clear_notes", help="Clear every recorded uncompacted time note."
    )

    return parser


def _build_reallocating_calendar(client: CalendarClient) -> ReallocatingCalendar:
    """A ReallocatingCalendar wrapping client plus a LabelPriorityCalendar,
    so reallocation sees an event's label-derived priority as the fallback
    whenever the event itself doesn't set one. Built lazily -- only
    `update`/`create` below need it -- since constructing an EventLabels
    may create this calendar's event label sheet on first use (see
    utilities/event_labels.py)."""
    return ReallocatingCalendar(LabelPriorityCalendar(client, build_event_labels()))


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
        updated_event = client.update_event(event)
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
    elif args.command == "create_label":
        fields = dict(args.properties)
        new_label = EventLabel(
            background_color=fields.get("background_color"),
            name=fields.get("name"),
            priority=fields.get("priority"),
        )
        labels = build_event_labels().create_label(new_label)
        for label in labels:
            print(_format_event_details(label))
            print()
    elif args.command == "update_label":
        fields = dict(args.properties)
        updated_label = EventLabel(
            id=args.label_id,
            background_color=fields.get("background_color"),
            name=fields.get("name"),
            priority=fields.get("priority"),
        )
        labels = build_event_labels().update_label(updated_label)
        for label in labels:
            print(_format_event_details(label))
            print()
    elif args.command == "sync_labels":
        labels = build_event_labels().sync_labels()
        if not labels:
            print("No event labels found.")
        for label in labels:
            print(_format_event_label_line(label))
    elif args.command == "note":
        noted_time = NotedTime(
            timestamp=resolve_note_timestamp(args.ago), description=args.description
        )
        build_noted_time_sheet().append(noted_time)
        print(_format_event_details(noted_time))
    elif args.command == "get_notes":
        noted_times = build_noted_time_sheet().read()
        if not noted_times:
            print("No notes found.")
        for noted_time in noted_times:
            print(_format_noted_time_line(noted_time))
    elif args.command == "clear_notes":
        cleared = build_noted_time_sheet().clear()
        if not cleared:
            print("No notes to clear.")
        for noted_time in cleared:
            print(_format_noted_time_line(noted_time))


if __name__ == "__main__":
    main()
