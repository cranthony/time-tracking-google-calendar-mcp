"""Find out empirically what Google Calendar does with an event whose
event label is removed from the calendar, and then added back.

Google's docs (https://developers.google.com/workspace/calendar/api/guides/
labels and the Calendars resource reference) say a label's `id` may be
client-supplied (a UUID) and that removing a label from
`labelProperties.eventLabels` deletes it -- but say nothing about events
still pointing at it. docs/goals-design.md ("Label lifecycle") depends on
the answers, so this script asks the API directly.

It creates a throwaway calendar (this app's calendar.app.created scope
allows that), runs every step against it, prints what happened, and
deletes the calendar again -- nothing else in your account is touched.
Pass --pause to stop after removing the label and after re-adding it, so
you can look at the events in the Google Calendar UI too.

Usage:
    python -m probes.label_lifecycle [--pause] [--keep]
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from calendar_clients.google_auth import load_credentials
from config import get_credentials_path, get_token_path

_LABEL_ID = str(uuid.uuid4())
_LABEL = {"id": _LABEL_ID, "name": "Probe label", "backgroundColor": "#8e24aa"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pause", action="store_true", help="Pause so you can check the Calendar UI")
    parser.add_argument("--keep", action="store_true", help="Don't delete the scratch calendar")
    args = parser.parse_args()

    service = build("calendar", "v3", credentials=load_credentials(get_token_path(), get_credentials_path()))
    calendar_id = service.calendars().insert(body={"summary": "CTT label probe (safe to delete)"}).execute()["id"]
    print(f"Scratch calendar: {calendar_id}")
    try:
        _probe(service, calendar_id, args.pause)
    finally:
        if args.keep:
            print(f"Kept scratch calendar {calendar_id}")
        else:
            service.calendars().delete(calendarId=calendar_id).execute()
            print("Deleted scratch calendar")


def _probe(service, calendar_id: str, pause: bool) -> None:
    events = service.events()

    def set_labels(labels: list[dict]) -> list[dict]:
        response = service.calendars().patch(
            calendarId=calendar_id, body={"labelProperties": {"eventLabels": labels}}
        ).execute()
        return response.get("labelProperties", {}).get("eventLabels", [])

    def insert(summary: str, hour: int, label_id: str | None) -> dict:
        start = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) + timedelta(hours=hour)
        body = {
            "summary": summary,
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": (start + timedelta(minutes=30)).isoformat()},
        }
        if label_id is not None:
            body["eventLabelId"] = label_id
        return events.insert(calendarId=calendar_id, body=body, eventLabelVersion=1).execute()

    def label_of(event_id: str) -> str | None:
        return events.get(calendarId=calendar_id, eventId=event_id).execute().get("eventLabelId")

    def attempt(step: str, call) -> None:
        try:
            result = call()
            print(f"  {step}: OK" + (f" -> {result}" if result is not None else ""))
        except HttpError as exc:
            print(f"  {step}: HTTP {exc.resp.status} {exc.reason}")

    print("1. Create a label with a client-chosen UUID")
    labels = set_labels([_LABEL])
    print(f"  labels now: {json.dumps(labels)}")
    print(f"  client id kept: {any(l.get('id') == _LABEL_ID for l in labels)}")
    e1 = insert("Probe 1 (untouched)", 1, _LABEL_ID)["id"]
    e2 = insert("Probe 2 (patched while label gone)", 2, _LABEL_ID)["id"]
    e3 = insert("Probe 3 (patched with stale id)", 3, _LABEL_ID)["id"]

    print("2. Remove the label from the calendar")
    set_labels([])
    print(f"  event 1 eventLabelId after removal: {label_of(e1)!r}")
    if pause:
        input("  Check the three probe events in the Calendar UI, then press Enter... ")

    print("3. Write events while the label is gone")
    attempt(
        "patch event 2's summary only (no eventLabelId in body)",
        lambda: events.patch(
            calendarId=calendar_id, eventId=e2, body={"summary": "Probe 2 (patched)"}, eventLabelVersion=1
        ).execute().get("eventLabelId"),
    )
    attempt(
        "patch event 3, re-sending the stale eventLabelId",
        lambda: events.patch(
            calendarId=calendar_id, eventId=e3, body={"eventLabelId": _LABEL_ID}, eventLabelVersion=1
        ).execute().get("eventLabelId"),
    )
    attempt("insert a new event with the stale eventLabelId", lambda: insert("Probe 4", 4, _LABEL_ID).get("eventLabelId"))

    print("4. Add the label back with the same UUID (different color)")
    attempt("re-add", lambda: [l.get("id") for l in set_labels([{**_LABEL, "backgroundColor": "#0b8043"}])])
    for name, event_id in (("1", e1), ("2", e2), ("3", e3)):
        print(f"  event {name} eventLabelId after re-adding: {label_of(event_id)!r}")
    if pause:
        input("  Check whether the probe events show the label (now green) again, then press Enter... ")

    print("5. Filter by a private extended property")
    events.patch(
        calendarId=calendar_id, eventId=e1, body={"extendedProperties": {"private": {"probe-goal": "g1"}}}
    ).execute()
    found = events.list(calendarId=calendar_id, privateExtendedProperty="probe-goal=g1").execute()
    print(f"  matched: {[item['summary'] for item in found.get('items', [])]}")

    print("6. Store a long description (event notes live there)")
    for length in (8_000, 32_000, 128_000):
        attempt(
            f"{length:,}-character description, length read back",
            lambda length=length: len(events.patch(
                calendarId=calendar_id, eventId=e1, body={"description": "x" * length}
            ).execute().get("description", "")),
        )


if __name__ == "__main__":
    main()
