"""Find out empirically which of a recurring series' instances an edit to
the series reaches, and what it does to instances edited on their own.

Google's docs (https://developers.google.com/workspace/calendar/api/guides/
recurringevents) say instances are generated from the series' master
event, and that an instance written on its own becomes an exception --
but not whether a later patch to the master still reaches the fields an
exception didn't change, nor what a change to the master's times does to
exceptions. update_recurrence (utilities/recurrences.py) depends on both:
it patches the master with whichever fields it's given -- priority and
goal_ids as private extended properties (priority once also set colorId, only
to show it), times etc. This script asks the API directly, writing each
field the way Event.to_api_body does.

It creates a throwaway calendar (this app's calendar.app.created scope
allows that) holding a daily series of six events, then:

1. edits four of the instances on their own: one moved an hour, one given
   another priority, one given other goal_ids, one given a description --
   the first and last are left alone;
2. patches the master's summary, priority and goal_ids -- no times -- and
   shows what each instance has now;
3. patches the master's goal_ids alone, as update_recurrence sends a
   goals-only edit, and shows whether the master (and its instances) kept
   the priority property -- that is, whether a patch merges
   extendedProperties.private rather than replacing it;
4. patches the master's start and end 30 minutes later, and shows which
   instances (and exceptions) are left, cancelled ones included.

Each step prints every instance's times and fields. It then deletes the
calendar again -- nothing else in your account is touched. Pass --pause
to stop after each step, so you can look at the series in the Google
Calendar UI too.

Usage:
    python -m probes.series_edits [--pause] [--keep] [--raw]

Found (2026-10-04):

- A patch to the master with no times reaches every instance, exceptions
  included, and resets all their fields except times to the master's --
  not just the fields it sets: the reprioritized and regoaled instances
  took the series' priority and goal_ids, and the described one lost its
  description, though the patch didn't mention it. The moved instance
  kept its own time. No instance's times changed.
- extendedProperties.private is merged by a patch, not replaced: a
  goals-only patch left the master's priority (and its instances') as it
  was.
- A patch to the master's start/end moves every instance, exceptions
  included: the moved instance went back to the series' new time, and
  every instance's originalStartTime moved with it. None was cancelled
  or dropped.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build

from calendar_clients.google_auth import load_credentials
from calendar_clients.google_calendar import _APP_EXTENDED_PROPERTY_KEY_PREFIX
from config import get_credentials_path, get_token_path

_TIME_ZONE = "America/New_York"
_ZONE = ZoneInfo(_TIME_ZONE)

_PRIORITY = f"{_APP_EXTENDED_PROPERTY_KEY_PREFIX}priority"
_GOAL_IDS = f"{_APP_EXTENDED_PROPERTY_KEY_PREFIX}goal_ids"

_ROLES = ("untouched", "moved", "reprioritized", "regoaled", "described", "untouched")
"""What step 1 does to each instance, in order."""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pause", action="store_true", help="Pause so you can check the Calendar UI")
    parser.add_argument("--keep", action="store_true", help="Don't delete the scratch calendar")
    parser.add_argument("--raw", action="store_true", help="Print each instance's raw JSON too")
    args = parser.parse_args()

    service = build("calendar", "v3", credentials=load_credentials(get_token_path(), get_credentials_path()))
    calendar_id = service.calendars().insert(
        body={"summary": "CTT series probe (safe to delete)", "timeZone": _TIME_ZONE}
    ).execute()["id"]
    print(f"Scratch calendar: {calendar_id}")
    try:
        _probe(service, calendar_id, args.pause, args.raw)
    finally:
        if args.keep:
            print(f"Kept scratch calendar {calendar_id}")
        else:
            service.calendars().delete(calendarId=calendar_id).execute()
            print("Deleted scratch calendar")


def _with_priority(priority: int, private: dict | None = None) -> dict:
    """A body setting `priority` as the app does: the private property
    (no colorId -- see Event.to_api_body)."""
    return {"extendedProperties": {"private": {**(private or {}), _PRIORITY: str(priority)}}}


def _probe(service, calendar_id: str, pause: bool, raw: bool) -> None:
    events = service.events()

    def when(moment: datetime) -> dict:
        return {"dateTime": moment.isoformat(), "timeZone": _TIME_ZONE}

    def patch(event_id: str, body: dict) -> dict:
        return events.patch(calendarId=calendar_id, eventId=event_id, body=body).execute()

    def instances(master_id: str) -> list[dict]:
        items = events.instances(calendarId=calendar_id, eventId=master_id, showDeleted=True).execute()
        return sorted(items.get("items", []), key=_original_start)

    def show(step: str, master_id: str) -> list[dict]:
        master = events.get(calendarId=calendar_id, eventId=master_id).execute()
        print(f"\n{step}")
        print(f"  {'master':<13} {_describe(master)}")
        print(f"    recurrence: {master.get('recurrence')}")
        items = instances(master_id)
        for role, item in zip(_ROLES + ("?",) * len(items), items):
            print(f"  {role:<13} {_describe(item)}")
            if raw:
                print("    " + json.dumps(item, indent=2).replace("\n", "\n    "))
        if pause:
            input("  Check the series in the Calendar UI, then press Enter... ")
        return items

    tomorrow = datetime.now(_ZONE).date() + timedelta(days=1)
    start = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 9, tzinfo=_ZONE)
    master = events.insert(
        calendarId=calendar_id,
        body={
            "summary": "Probe series",
            "start": when(start),
            "end": when(start + timedelta(minutes=30)),
            "recurrence": [f"RRULE:FREQ=DAILY;COUNT={len(_ROLES)}"],
            **_with_priority(1, {_GOAL_IDS: "goalA"}),
        },
    ).execute()
    master_id = master["id"]
    print(f"Series: {master_id}, daily from {start:%Y-%m-%d %H:%M %Z}, priority 1, goals goalA")

    print("\n1. Edit four instances on their own")
    items = instances(master_id)
    by_role = dict(zip(_ROLES[1:5], items[1:5]))
    moved = by_role["moved"]
    moved_start = datetime.fromisoformat(moved["start"]["dateTime"]) + timedelta(hours=1)
    patch(moved["id"], {"start": when(moved_start), "end": when(moved_start + timedelta(minutes=30))})
    patch(by_role["reprioritized"]["id"], _with_priority(5))
    patch(by_role["regoaled"]["id"], {"extendedProperties": {"private": {_GOAL_IDS: "goalX"}}})
    patch(by_role["described"]["id"], {"description": "edited on its own"})
    before = show("After step 1 (instance edits)", master_id)

    print("\n2. Patch the master's summary, priority (3) and goal_ids (goalB) -- no times")
    patch(master_id, {"summary": "Probe series (edited)", **_with_priority(3, {_GOAL_IDS: "goalB"})})
    after = show("After step 2 (series edit, no times)", master_id)
    _report_moves(before, after)

    print("\n3. Patch the master's goal_ids (goalC) alone -- no priority in the body")
    patch(master_id, {"extendedProperties": {"private": {_GOAL_IDS: "goalC"}}})
    after_goals = show("After step 3 (goals-only series edit)", master_id)
    _report_moves(after, after_goals)

    print("\n4. Patch the master's start and end 30 minutes later")
    later = start + timedelta(minutes=30)
    patch(master_id, {"start": when(later), "end": when(later + timedelta(minutes=30))})
    show("After step 4 (series times moved)", master_id)


def _report_moves(before: list[dict], after: list[dict]) -> None:
    """Flag any instance whose times differ between `before` and `after`."""
    moved = [
        (role, old, new)
        for role, old, new in zip(_ROLES, before, after)
        if old.get("start") != new.get("start") or old.get("end") != new.get("end")
    ]
    for role, old, new in moved:
        print(f"  !! {role} instance's times changed: {_start(old)} -> {_start(new)}")
    if not moved:
        print("  (no instance's times changed)")


def _original_start(event: dict) -> str:
    original = event.get("originalStartTime") or event.get("start") or {}
    return original.get("dateTime") or original.get("date") or ""


def _start(event: dict) -> str:
    moment = (event.get("start") or {}).get("dateTime")
    return datetime.fromisoformat(moment).astimezone(_ZONE).strftime("%m-%d %H:%M") if moment else "-"


def _describe(event: dict) -> str:
    private = event.get("extendedProperties", {}).get("private", {})
    original = _original_start(event)
    original_text = datetime.fromisoformat(original).astimezone(_ZONE).strftime("%m-%d %H:%M") if original else "-"
    fields = [
        f"orig={original_text}",
        f"start={_start(event)}",
        f"status={event.get('status', '?')}",
        f"priority={private.get(_PRIORITY, '-')}",
        f"goals={private.get(_GOAL_IDS, '-')}",
        f"color={event.get('colorId', '-')}",
        f"summary={event.get('summary', '-')!r}",
    ]
    if "description" in event:
        fields.append(f"description={event['description']!r}")
    return "  ".join(fields)


if __name__ == "__main__":
    main()
