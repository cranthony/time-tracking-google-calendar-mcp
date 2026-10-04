"""Find out empirically what delete_recurrence's two writes do to a
recurring series' instances, and to those edited on their own.

utilities/recurrences.py deletes a whole series by cancelling its master
(a patch with status "cancelled"), and deletes "this and following" by
ending the master's RRULE just before the event (UNTIL, in place of any
COUNT). Google's docs (https://developers.google.com/workspace/calendar/
api/guides/recurringevents) don't say what either does to an instance
edited on its own (an exception) -- in particular, whether one whose
original start is past a new UNTIL, or one moved from there to before
it, is still listed. This script runs Recurrences.delete itself, through
a CalendarClient, and asks the API what's left.

It creates a throwaway calendar (this app's calendar.app.created scope
allows that) holding two daily series of six events, then:

1. edits four instances of series A on their own: one before the cut
   moved an hour, one after it given a description, one after it moved
   to before the cut, one after it moved later still;
2. deletes series A from its third event on, and shows its master, every
   instance (cancelled ones included), and what a plain list of the
   calendar's events -- what list_events reads -- still holds;
3. edits one instance of series B on its own, deletes series B whole,
   and shows the same.

Each step prints what it finds. It then deletes the calendar again --
nothing else in your account is touched. Pass --pause to stop after each
step, so you can look at the series in the Google Calendar UI too.

Usage:
    python -m probes.series_deletes [--pause] [--keep] [--raw]

Found (2026-10-04):

- Ending a series early (UNTIL just before an event) removes every
  instance from that event on outright -- not cancelled, but gone, even
  with showDeleted. Exceptions go too, by their original start: the one
  described, the one moved later, and the one moved to before the cut
  (its original start was after it). Instances before the cut, the one
  moved an hour among them, are left as they were.
- Cancelling the master cancels every instance, past ones and exceptions
  included: with showDeleted all six are listed, cancelled; without it,
  none is.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from calendar_clients.google_auth import load_credentials
from calendar_clients.google_calendar import CalendarClient
from calendar_clients.write_lock import WRITE_LOCK
from config import get_credentials_path, get_token_path
from utilities.recurrences import Recurrences

_TIME_ZONE = "America/New_York"
_ZONE = ZoneInfo(_TIME_ZONE)

_ROLES_A = ("untouched", "moved-1h", "cut-here", "described", "moved-before-cut", "moved-later")
"""What step 1 does to each of series A's instances, in order; it's
deleted from "cut-here" on."""

_ROLES_B = ("untouched", "described", "untouched", "untouched", "untouched", "untouched")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pause", action="store_true", help="Pause so you can check the Calendar UI")
    parser.add_argument("--keep", action="store_true", help="Don't delete the scratch calendar")
    parser.add_argument("--raw", action="store_true", help="Print each event's raw JSON too")
    args = parser.parse_args()

    service = build("calendar", "v3", credentials=load_credentials(get_token_path(), get_credentials_path()))
    calendar_id = service.calendars().insert(
        body={"summary": "CTT series delete probe (safe to delete)", "timeZone": _TIME_ZONE}
    ).execute()["id"]
    print(f"Scratch calendar: {calendar_id}")
    try:
        with WRITE_LOCK:
            _probe(service, calendar_id, args.pause, args.raw)
    finally:
        if args.keep:
            print(f"Kept scratch calendar {calendar_id}")
        else:
            service.calendars().delete(calendarId=calendar_id).execute()
            print("Deleted scratch calendar")


def _probe(service, calendar_id: str, pause: bool, raw: bool) -> None:
    events = service.events()
    client = CalendarClient(service, calendar_id)
    recurrences = Recurrences(client, client.list_instances, client.get_time_zone)

    def when(moment: datetime) -> dict:
        return {"dateTime": moment.isoformat(), "timeZone": _TIME_ZONE}

    def patch(event_id: str, body: dict) -> dict:
        return events.patch(calendarId=calendar_id, eventId=event_id, body=body).execute()

    def move(item: dict, to: datetime) -> None:
        patch(item["id"], {"start": when(to), "end": when(to + timedelta(minutes=30))})

    def instances(master_id: str) -> list[dict]:
        items = events.instances(calendarId=calendar_id, eventId=master_id, showDeleted=True).execute()
        return sorted(items.get("items", []), key=_original_start)

    def insert(summary: str, start: datetime) -> str:
        body = {
            "summary": summary,
            "start": when(start),
            "end": when(start + timedelta(minutes=30)),
            "recurrence": ["RRULE:FREQ=DAILY;COUNT=6"],
        }
        master_id = events.insert(calendarId=calendar_id, body=body).execute()["id"]
        print(f"{summary}: {master_id}, daily from {start:%Y-%m-%d %H:%M %Z}, 6 times")
        return master_id

    def show(step: str, master_id: str, roles: tuple[str, ...]) -> None:
        master = events.get(calendarId=calendar_id, eventId=master_id).execute()
        print(f"\n{step}")
        print(f"  {'master':<17} {_describe(master)}")
        print(f"    recurrence: {master.get('recurrence')}")
        items = instances(master_id)
        print("  instances (showDeleted):")
        by_id = {}
        for role, item in zip(roles + ("?",) * len(items), items):
            by_id[item["id"]] = role
            print(f"    {role:<17} {_describe(item)}")
            if raw:
                print("      " + json.dumps(item, indent=2).replace("\n", "\n      "))
        listed = events.list(calendarId=calendar_id, singleEvents=True, orderBy="startTime").execute()
        mine = [item for item in listed.get("items", []) if item.get("recurringEventId") == master_id]
        print("  listed (singleEvents, no showDeleted -- what list_events sees):")
        for item in mine:
            print(f"    {by_id.get(item['id'], '?'):<17} {_describe(item)}")
        if not mine:
            print("    (none)")
        if pause:
            input("  Check the series in the Calendar UI, then press Enter... ")

    tomorrow = datetime.now(_ZONE).date() + timedelta(days=1)
    start = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 9, tzinfo=_ZONE)

    a_id = insert("Probe series A", start)
    b_id = insert("Probe series B", start + timedelta(hours=3))

    print("\n1. Edit four of series A's instances on their own")
    items = dict(zip(_ROLES_A, instances(a_id)))
    move(items["moved-1h"], start + timedelta(days=1, hours=1))
    patch(items["described"]["id"], {"description": "edited on its own"})
    move(items["moved-before-cut"], start + timedelta(days=1, hours=6))
    move(items["moved-later"], start + timedelta(days=5, hours=6))
    show("After step 1 (series A's instance edits)", a_id, _ROLES_A)

    print('\n2. Delete series A from its "cut-here" event on (Recurrences.delete)')
    left = recurrences.delete(a_id, starting_at=items["cut-here"]["id"])
    print(f"  returned: recurrence={left.recurrence if left else None}")
    show("After step 2 (series A, this and following deleted)", a_id, _ROLES_A)

    print("\n3. Edit one of series B's instances on its own, then delete series B whole")
    patch(instances(b_id)[1]["id"], {"description": "edited on its own"})
    left = recurrences.delete(b_id)
    print(f"  returned: {left}")
    show("After step 3 (series B deleted)", b_id, _ROLES_B)


def _original_start(event: dict) -> str:
    original = event.get("originalStartTime") or event.get("start") or {}
    return original.get("dateTime") or original.get("date") or ""


def _time(moment: str | None) -> str:
    return datetime.fromisoformat(moment).astimezone(_ZONE).strftime("%m-%d %H:%M") if moment else "-"


def _describe(event: dict) -> str:
    fields = [
        f"orig={_time(_original_start(event))}",
        f"start={_time((event.get('start') or {}).get('dateTime'))}",
        f"status={event.get('status', '?')}",
        f"summary={event.get('summary', '-')!r}",
    ]
    if "description" in event:
        fields.append(f"description={event['description']!r}")
    return "  ".join(fields)


if __name__ == "__main__":
    main()
