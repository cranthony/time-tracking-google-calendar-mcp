"""Create a new Google Calendar for this app to use, and print its ID.

This app requests the calendar.app.created OAuth scope (see
calendar_clients/google_calendar.py), so it can only see/manage calendars
it has created itself — never a user's existing calendars, including
"primary" (see the README's "Calendar access model"). Run this script once
to create that dedicated calendar, then set GOOGLE_CALENDAR_ID to the ID it
prints.

Usage:
    python create_calendar.py ["Calendar name"] [--description "..."]
"""

from __future__ import annotations

import argparse

from googleapiclient.discovery import build

from calendar_clients.google_calendar import Calendar, load_credentials
from config import get_credentials_path, get_token_path

DEFAULT_SUMMARY = "Time Tracking"
DEFAULT_DESCRIPTION = "Calendar managed by Cascading Time Tracker"


def create_calendar(summary: str, description: str | None = None) -> Calendar:
    creds = load_credentials(get_token_path(), get_credentials_path())
    service = build("calendar", "v3", credentials=creds)
    calendar = Calendar(summary=summary, description=description)
    response = service.calendars().insert(body=calendar.to_api_body()).execute()
    return Calendar.from_api(response)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("summary", nargs="?", default=DEFAULT_SUMMARY, help="Calendar name")
    parser.add_argument(
        "--description", default=DEFAULT_DESCRIPTION, help="Calendar description"
    )
    args = parser.parse_args()

    calendar = create_calendar(args.summary, args.description)
    print(f"Created calendar {calendar.summary!r} with id: {calendar.id}")
    print("Set GOOGLE_CALENDAR_ID to this value.")


if __name__ == "__main__":
    main()
