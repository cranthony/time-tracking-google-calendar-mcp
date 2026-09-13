"""Create a new Google Calendar for this app to use, and print its ID.

This app requests the calendar.app.created OAuth scope (see
calendar_clients/google_calendar.py), so it can only see/manage calendars
it has created itself — never a user's existing calendars, including
"primary" (see the README's "Calendar access model"). Run this script once
to create that dedicated calendar, then set GOOGLE_CALENDAR_ID to the ID it
prints.

This also creates that calendar's event label sheet (see utilities/
event_labels.py's EventLabels.create_sheet) in the same step, since there's
no MCP tool or CLI command for that either -- same one-time, human-run
bootstrap reasoning as the calendar itself.

If GOOGLE_CALENDAR_ID is already set when this runs, no new calendar is
created at all: this just adds an event label sheet to that
already-configured calendar instead (useful if you set this app up before
event label sheets existed, and are adding one to your existing calendar).

Usage:
    python create_calendar.py ["Calendar name"] [--description "..."]
"""

from __future__ import annotations

import argparse
import os
import sys

from googleapiclient.discovery import build

from calendar_clients.google_auth import load_credentials
from calendar_clients.google_calendar import Calendar
from config import build_event_labels, get_credentials_path, get_token_path

DEFAULT_SUMMARY = "Time Tracking"
DEFAULT_DESCRIPTION = "Calendar managed by Cascading Time Tracker"


def create_calendar(summary: str, description: str | None = None) -> Calendar:
    creds = load_credentials(get_token_path(), get_credentials_path())
    service = build("calendar", "v3", credentials=creds)
    calendar = Calendar(summary=summary, description=description)
    response = service.calendars().insert(body=calendar.to_api_body()).execute()
    return Calendar.from_api(response)


def create_event_label_sheet_for_calendar(calendar_id: str) -> str:
    """Create `calendar_id`'s event label sheet (see EventLabels.
    create_sheet), returning its spreadsheet id. Raises `ValueError` if
    that calendar already has one tracked."""
    return build_event_labels(calendar_id).create_sheet()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("summary", nargs="?", default=DEFAULT_SUMMARY, help="Calendar name")
    parser.add_argument(
        "--description", default=DEFAULT_DESCRIPTION, help="Calendar description"
    )
    args = parser.parse_args()

    existing_calendar_id = os.environ.get("GOOGLE_CALENDAR_ID")
    if existing_calendar_id:
        calendar_id = existing_calendar_id
        print(
            f"GOOGLE_CALENDAR_ID is already set to {calendar_id!r} -- adding an event label "
            "sheet to it instead of creating a new calendar."
        )
    else:
        calendar = create_calendar(args.summary, args.description)
        calendar_id = calendar.id
        print(f"Created calendar {calendar.summary!r} with id: {calendar_id}")
        print("Set GOOGLE_CALENDAR_ID to this value.")

    try:
        spreadsheet_id = create_event_label_sheet_for_calendar(calendar_id)
    except ValueError as exc:
        sys.exit(str(exc))
    print(f"Created event label sheet: https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit")


if __name__ == "__main__":
    main()
