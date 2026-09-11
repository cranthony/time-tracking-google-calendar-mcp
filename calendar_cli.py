"""Command-line utilities for the Google Calendar API, via CalendarClient.

This is a dev tool, not something the deployed server needs.

Usage:
    python calendar_cli.py list [from] [to]
    python calendar_cli.py get <id>

`list` shows events between `from` before now and `to` after now, each a
duration parsed with pytimeparse (e.g. "1h", "90m", "2d", "1:30") — default
window is 1 hour on each side of now. `get` shows a single event by its id.
"""

from __future__ import annotations

import argparse
import dataclasses
from datetime import datetime, timedelta, timezone

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


if __name__ == "__main__":
    main()
