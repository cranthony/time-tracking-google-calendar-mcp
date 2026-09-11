"""Command-line utilities for the Google Calendar API, via CalendarClient.

This is a dev tool, not something the deployed server needs.

Usage:
    python calendar_cli.py list [from] [to]
    python calendar_cli.py get <id>
    python calendar_cli.py update_properties <id> key=value [key=value ...]

- `list` shows events between `from` before now and `to` after now, each a
  duration parsed with pytimeparse (e.g. "1h", "90m", "2d", "1:30") —
  default window is 1 hour on each side of now.
- `get` shows a single event by its id.
- `update_properties` sets the given attributes on the event and patches
  them in, without fetching it first — any attribute not given is left
  untouched.
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
# (changing id would repoint the patch at a different event), mapped to a
# function parsing its command-line string value into the right type.
_UPDATABLE_ATTRIBUTE_PARSERS: dict[str, Callable[[str], Any]] = {
    "summary": str,
    "start": _parse_iso_datetime,
    "end": _parse_iso_datetime,
    "description": str,
    "location": str,
    "min_duration": lambda s: timedelta(seconds=_parse_duration(s)),
    "is_fixed_duration": _parse_bool,
    "priority": int,
    "is_end_of_day_sleep": _parse_bool,
}


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

    return parser


def main() -> None:
    args = _build_parser().parse_args()
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


if __name__ == "__main__":
    main()
