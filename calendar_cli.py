"""Command-line utilities for the Google Calendar API, via CalendarClient.

This is a dev tool, not something the deployed server needs.

Usage:
    python calendar_cli.py list [from] [to]
    python calendar_cli.py get <id>
    python calendar_cli.py update_properties <id> key=value [key=value ...]
    python calendar_cli.py update <id> key=value [key=value ...]
    python calendar_cli.py create key=value [key=value ...]
    python calendar_cli.py delete <id>

- `list` shows events between `from` before now and `to` after now, each a
  duration parsed with pytimeparse (e.g. "1h", "90m", "2d", "1:30") —
  default window is 1 hour on each side of now.
- `get` shows a single event by its id.
- `update_properties` sets the given attributes on the event and patches
  them in, without fetching it first — any attribute not given is left
  untouched.
- `update` moves/resizes an existing event (`start` and `end` are
  required) via CalendarClient.update_event_and_reallocate, reallocating
  time from the rest of its day as needed to make room for its new
  position — see utilities/reallocation.py. Prints every event that was
  created or changed as a result. Use `update_properties` instead for a
  plain patch that doesn't need to make room for anything (e.g. renaming
  an event without moving it).
- `create` builds an Event from the given attributes (`summary`, `start`,
  and `end` are required) and creates it via
  CalendarClient.create_event_with_reallocation, reallocating time from
  the rest of its day as needed to make room — see
  utilities/reallocation.py. Prints every event that was created or
  changed as a result.
- `delete` deletes a single event by its id.
"""

from __future__ import annotations

import argparse
import dataclasses
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

import pytimeparse

from calendar_clients.google_calendar import Event
from config import build_calendar_client
from utilities.reallocation import ReallocationOptions

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
    "priority": int,
    "is_end_of_day_sleep": _parse_bool,
}


_REQUIRED_CREATE_ATTRIBUTES = frozenset({"summary", "start", "end"})
"""Event attributes `create` won't build an event without."""

_REQUIRED_UPDATE_ATTRIBUTES = frozenset({"start", "end"})
"""Event attributes `update` won't build an event without -- reallocation
needs a real (start, end) span to make room for, unlike `update_properties`'s
plain patch."""


def _parse_key_value(value: str) -> tuple[str, Any]:
    """Parse a "key=value" command-line argument into (attribute name,
    parsed value), for use as an argparse `type`."""
    if "=" not in value:
        raise argparse.ArgumentTypeError(f"expected key=value, got {value!r}")
    key, raw_value = value.split("=", 1)
    parse = _UPDATABLE_ATTRIBUTE_PARSERS.get(key)
    if parse is None:
        valid = ", ".join(sorted(_UPDATABLE_ATTRIBUTE_PARSERS))
        raise argparse.ArgumentTypeError(
            f"unknown Event attribute {key!r}; expected one of: {valid}"
        )
    try:
        return key, parse(raw_value)
    except (ValueError, argparse.ArgumentTypeError) as exc:
        raise argparse.ArgumentTypeError(f"invalid value for {key!r}: {exc}") from exc


def resolve_window(
    from_seconds: float, to_seconds: float, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """Resolve a (from, to) pair of second-offsets around `now` (default: the
    current time) into a (time_min, time_max) pair of datetimes."""
    if now is None:
        now = datetime.now(timezone.utc)
    return now - timedelta(seconds=from_seconds), now + timedelta(seconds=to_seconds)


def _format_event_line(event: Event) -> str:
    return f"{event.id}\t{event.start.isoformat()} - {event.end.isoformat()}\t{event.summary}"


def _format_event_details(event: Event) -> str:
    lines = []
    for field in dataclasses.fields(event):
        value = getattr(event, field.name)
        if value is not None:
            lines.append(f"{field.name}: {value}")
    return "\n".join(lines)


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
        type=_parse_key_value,
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
        type=_parse_key_value,
        help=(
            "One or more Event attribute=value pairs; "
            f"{', '.join(sorted(_REQUIRED_UPDATE_ATTRIBUTES))} are required. Valid "
            f"attributes: {', '.join(sorted(_UPDATABLE_ATTRIBUTE_PARSERS))}."
        ),
    )

    create_parser = subparsers.add_parser(
        "create", help="Create a new event, reallocating time from its day as needed."
    )
    create_parser.add_argument(
        "properties",
        metavar="key=value",
        nargs="+",
        type=_parse_key_value,
        help=(
            "One or more Event attribute=value pairs; "
            f"{', '.join(sorted(_REQUIRED_CREATE_ATTRIBUTES))} are required. Valid "
            f"attributes: {', '.join(sorted(_UPDATABLE_ATTRIBUTE_PARSERS))}."
        ),
    )

    delete_parser = subparsers.add_parser("delete", help="Delete an event by id.")
    delete_parser.add_argument("id", help="The event id.")

    return parser


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
        missing = _REQUIRED_UPDATE_ATTRIBUTES - fields.keys()
        if missing:
            parser.error(f"update requires: {', '.join(sorted(missing))}")
        updated_event = Event(id=args.id, **fields)
        applied_events = client.update_event_and_reallocate(updated_event, ReallocationOptions())
        for event in applied_events:
            print(_format_event_details(event))
            print()
    elif args.command == "create":
        fields = dict(args.properties)
        missing = _REQUIRED_CREATE_ATTRIBUTES - fields.keys()
        if missing:
            parser.error(f"create requires: {', '.join(sorted(missing))}")
        new_event = Event(**fields)
        applied_events = client.create_event_with_reallocation(new_event, ReallocationOptions())
        for event in applied_events:
            print(_format_event_details(event))
            print()
    elif args.command == "delete":
        client.delete_event(args.id)
        print(f"Deleted event {args.id}.")


if __name__ == "__main__":
    main()
