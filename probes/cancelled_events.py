"""Find out empirically whether Google Calendar lists your cancelled
events, and what's kept on them.

Google's docs say events.list leaves cancelled events out unless asked
for them with showDeleted, and that a cancelled event is only guaranteed
to keep its `id`. The follow_through measure (utilities/goal_health.py)
depends on both: it lists with showDeleted, and needs a cancelled event's
start and goal_ids (or label) to tell when it was and whose it was. This
script asks the API directly.

It lists the configured calendar's events (GOOGLE_CALENDAR_ID) over the
last --days days twice, without showDeleted (as list_events always has)
and with it, and prints how many cancelled events each returned and, for
each cancelled one, which of the fields follow_through needs it kept. It
only reads: nothing is written.

Usage:
    python -m probes.cancelled_events [--days 14] [--raw]
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from calendar_clients.google_auth import load_credentials
from calendar_clients.google_calendar import _APP_EXTENDED_PROPERTY_KEY_PREFIX
from config import get_calendar_id, get_credentials_path, get_token_path

_FIELDS = ("summary", "start", "end", "originalStartTime", "recurringEventId", "eventLabelId")

_GOAL_IDS = f"{_APP_EXTENDED_PROPERTY_KEY_PREFIX}goal_ids"
"""The private extended property an event's goal ids are kept in."""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=14, help="How many days back to look (default 14)")
    parser.add_argument("--raw", action="store_true", help="Print each cancelled event's raw JSON too")
    args = parser.parse_args()

    service = build("calendar", "v3", credentials=load_credentials(get_token_path(), get_credentials_path()))
    calendar_id = get_calendar_id()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=args.days)
    print(f"Calendar {calendar_id}, {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} UTC\n")

    plain = _list(service, calendar_id, start, end, show_deleted=False)
    deleted = _list(service, calendar_id, start, end, show_deleted=True)
    plain_cancelled = [e for e in plain if e.get("status") == "cancelled"]
    cancelled = [e for e in deleted if e.get("status") == "cancelled"]
    print(f"Without showDeleted: {len(plain)} events, {len(plain_cancelled)} cancelled")
    print(f"With showDeleted:    {len(deleted)} events, {len(cancelled)} cancelled\n")

    series: dict[str, dict | None] = {}
    for event in cancelled:
        private = event.get("extendedProperties", {}).get("private", {})
        kept = [f for f in _FIELDS if f in event] + (["goal_ids"] if _GOAL_IDS in private else [])
        missing = [f for f in ("summary", "start", "end", "goal_ids") if f not in kept]
        when = (event.get("start") or event.get("originalStartTime") or {}).get("dateTime", "?")
        listed = "also listed without showDeleted" if any(e.get("id") == event.get("id") for e in plain) else ""
        print(f"- {when}  {event.get('summary', '(no summary)')!r}  id={event.get('id')}  {listed}")
        print(f"    kept: {', '.join(kept) or 'nothing but its id'}")
        if missing:
            print(f"    missing: {', '.join(missing)}")
        if private.get(_GOAL_IDS):
            print(f"    goal_ids: {private[_GOAL_IDS]}")
        elif series_id := event.get("recurringEventId"):
            if series_id not in series:
                series[series_id] = _get(service, calendar_id, series_id)
            master = series[series_id]
            master_goals = (master or {}).get("extendedProperties", {}).get("private", {}).get(_GOAL_IDS)
            print(f"    its series' goal_ids: {master_goals or '(none)'}" if master else "    its series is gone")
        if args.raw:
            print("    " + json.dumps(event, indent=2).replace("\n", "\n    "))

    if not cancelled:
        print("No cancelled events in that range: try a longer --days, or cancel a test event first.")


def _get(service, calendar_id: str, event_id: str) -> dict | None:
    try:
        return service.events().get(calendarId=calendar_id, eventId=event_id).execute()
    except HttpError:
        return None


def _list(service, calendar_id: str, start: datetime, end: datetime, *, show_deleted: bool) -> list[dict]:
    """Every event from `start` to `end`, the way CalendarClient.list_events
    asks for them, following nextPageToken."""
    items: list[dict] = []
    page_token = None
    while True:
        response = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=2500,
                showDeleted=show_deleted,
                **({"pageToken": page_token} if page_token else {}),
            )
            .execute()
        )
        items.extend(response.get("items", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return items


if __name__ == "__main__":
    main()
