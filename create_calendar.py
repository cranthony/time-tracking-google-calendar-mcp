"""Create a new Google Calendar for this app to use, and print its ID.

This app requests the calendar.app.created OAuth scope (see
calendar_clients/google_calendar.py), so it can only see/manage calendars
it has created itself — never a user's existing calendars, including
"primary" (see the README's "Calendar access model"). Run this script once
to create that dedicated calendar, then set GOOGLE_CALENDAR_ID to the ID it
prints.

This also creates that calendar's event label sheet (see utilities/
event_labels.py's EventLabels) in the same step, since there's no MCP tool
or CLI command for that either -- same one-time, human-run bootstrap
reasoning as the calendar itself. (Constructing an EventLabels for a
calendar that doesn't have one yet creates it automatically -- see
EventLabels.__init__ -- so this is really just that constructor call, not
a separate step; it's also safe to run again later, since it reuses
whatever sheet is already tracked instead of creating another one.)

If GOOGLE_CALENDAR_ID is already set when this runs, no new calendar is
created at all: this just ensures that already-configured calendar has an
event label sheet (useful if you set this app up before event label
sheets existed).

Usage:
    python create_calendar.py ["Calendar name"] [--description "..."]
"""

from __future__ import annotations

import argparse
import os
import sys

from googleapiclient.discovery import build

from calendar_clients.google_auth import load_credentials
from calendar_clients.google_calendar import Calendar, EventLabelConflictError
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
    """Ensure `calendar_id` has an event label sheet (creating one,
    pre-populated with its current labels, if it doesn't already -- see
    `EventLabels.__init__`), returning its spreadsheet id."""
    return build_event_labels(calendar_id).sheet_id


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
        print(f"GOOGLE_CALENDAR_ID is already set to {calendar_id!r} -- not creating a new calendar.")
    else:
        calendar = create_calendar(args.summary, args.description)
        calendar_id = calendar.id
        print(f"Created calendar {calendar.summary!r} with id: {calendar_id}")
        print("Set GOOGLE_CALENDAR_ID to this value.")

    try:
        spreadsheet_id = create_event_label_sheet_for_calendar(calendar_id)
    except EventLabelConflictError as exc:
        # Another writer changed the calendar (e.g. a concurrent run of
        # this same script) between checking for a tracked sheet and
        # recording a new one -- rerunning will just find that one.
        sys.exit(str(exc))
    print(f"Event label sheet: https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit")


if __name__ == "__main__":
    main()
