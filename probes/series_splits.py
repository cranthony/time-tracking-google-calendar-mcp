"""Find out empirically why ending a series that Google Calendar split
itself fails, and what ends it instead.

Recurrences.split (utilities/recurrences.py) copies a series from the
split event on, then ends the original by patching its RRULE with an
UNTIL just before that event. On 2026-10-05 that patch failed with a bare
400 "Bad Request" for a series whose id ended "_R20260915T223000": one
Google made itself when the series was edited "this and following" in the
Calendar UI. (Its instances' ids use the base id, without the "_R..."
part.) The copy had been made, so every later event was there twice.

A series like that can be made through the API too: importing (events.
import) an event whose iCalUID is "<series id>_R<an instance's start,
UTC>@google.com" makes Google split the series there itself.

It creates a throwaway calendar (this app's calendar.app.created scope
allows that) and, for each step, a fresh weekly Mon/Tue series (WKST=SU,
as the UI writes it) split by Google on its 3rd event. Then:

1. tries ending that "_R" series before its own 3rd event in several
   ways, on one left as Google made it and on one whose 1st event is
   cancelled (as the real one's was), whose 2nd is moved and whose 5th is
   described; and shows what's left of it, and of any later part;
2. runs Recurrences itself on such series: split, delete from an event
   on, an edit to the whole series' start (no rules), an edit to its
   rules (refused), and a split undone and retried.

Each step prints what it finds. It then deletes the calendar again --
nothing else in your account is touched.

Usage:
    python -m probes.series_splits [--keep]

Found (2026-10-05):

- Any change to an "_R" series' rules -- a patch or an update (PUT), with
  UNTIL or COUNT, with or without start/end -- deletes it: it's left
  cancelled, its rules replaced by "FREQ=DAILY;COUNT=1", with no
  instances. Deleting it leaves just the same. If its 1st event is
  cancelled the change fails instead, with a bare 400 "Bad Request" --
  the failure seen for real. Patches that leave its rules alone (summary,
  start/end, unchanged rules) work.
- Importing "<base id>_R<time>@google.com" splits it again, leaving
  just what Google's own "this and following" edit left on the real
  series (what the UI calls to do that isn't known): the "_R" series is
  ended at the local midnight before that
  time's day (UNTIL 23:59:59 the day before), and the rest is a new
  series "<base id>_R<time>" -- events after the time that were edited on
  their own move to it. Its private extended properties and color come
  from the import. That works with the 1st event cancelled too, and
  importing the same UID again returns the same series. A COUNT in it is
  set by Google, counting from the base series' first event.
- Importing an "_R" series' own UID with other rules changes nothing.
- Cancelling the part split off that way leaves no instances of it, not
  even cancelled ones, and the series before it as it was: so an "_R"
  series is ended before an event by splitting it there and cancelling
  the rest, as Recurrences now does.
- A cancelled series patched back to "confirmed" has its instances back.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from calendar_clients.google_auth import load_credentials
from calendar_clients.google_calendar import CalendarClient, Event
from calendar_clients.write_lock import WRITE_LOCK
from config import get_credentials_path, get_token_path
from utilities.recurrences import Recurrences, Repeat, split_series_id

_TIME_ZONE = "America/New_York"
_ZONE = ZoneInfo(_TIME_ZONE)
_RULE = "RRULE:FREQ=WEEKLY;WKST=SU;BYDAY=MO,TU"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="Don't delete the scratch calendar")
    args = parser.parse_args()

    service = build("calendar", "v3", credentials=load_credentials(get_token_path(), get_credentials_path()))
    calendar_id = service.calendars().insert(
        body={"summary": "CTT series split probe (safe to delete)", "timeZone": _TIME_ZONE}
    ).execute()["id"]
    print(f"Scratch calendar: {calendar_id}")
    try:
        with WRITE_LOCK:
            _probe(service, calendar_id)
    finally:
        if args.keep:
            print(f"Kept scratch calendar {calendar_id}")
        else:
            service.calendars().delete(calendarId=calendar_id).execute()
            print("Deleted scratch calendar")


def _probe(service, calendar_id: str) -> None:
    events = service.events()
    client = CalendarClient(service, calendar_id)
    recurrences = Recurrences(client, client.list_instances, client.get_time_zone)

    def when(moment: datetime) -> dict:
        return {"dateTime": moment.isoformat(), "timeZone": _TIME_ZONE}

    def patch(event_id: str, body: dict) -> dict:
        return events.patch(calendarId=calendar_id, eventId=event_id, body=body).execute()

    def get(event_id: str) -> dict:
        return events.get(calendarId=calendar_id, eventId=event_id).execute()

    def instances(series_id: str, deleted: bool = False) -> list[dict]:
        items = events.instances(calendarId=calendar_id, eventId=series_id, maxResults=8, showDeleted=deleted)
        return items.execute().get("items", [])

    monday = datetime.now(_ZONE).date() + timedelta(days=7 - datetime.now(_ZONE).weekday())
    first = datetime(monday.year, monday.month, monday.day, 18, 30, tzinfo=_ZONE)
    made = 0

    def import_split(series_id: str, at: datetime, **fields) -> dict:
        """Have Google split `series_id` (or the "_R" series made from it)
        at `at`, importing the rest with `fields`."""
        body = {
            "summary": "Probe",
            "start": when(at),
            "end": when(at + timedelta(hours=1)),
            "recurrence": [_RULE],
            **fields,
            "iCalUID": f"{series_id.split('_R')[0]}_R{_stamp(at)}@google.com",
        }
        return events.import_(calendarId=calendar_id, body=body).execute()

    def google_split(edited: bool) -> tuple[str, dict]:
        """A fresh series, split by Google on its 3rd event: the "_R"
        series' id, and its own 3rd event. If `edited`, its 1st event is
        then cancelled (as the real one's was), its 2nd moved an hour and
        its 5th described."""
        nonlocal made
        made += 1
        start = first + timedelta(weeks=made * 4)
        master = events.insert(
            calendarId=calendar_id,
            body={"summary": "Probe", "start": when(start), "end": when(start + timedelta(hours=1)), "recurrence": [_RULE]},
        ).execute()
        r_id = import_split(master["id"], _original_start(instances(master["id"])[2]))["id"]
        items = instances(r_id)
        print(f"\n  made {r_id} (its events like {items[0]['id']});")
        print(f"  the series it was split from now {get(master['id'])['recurrence']}")
        if edited:
            patch(items[0]["id"], {"status": "cancelled"})
            moved = _original_start(items[1]) + timedelta(hours=1)
            patch(items[1]["id"], {"start": when(moved), "end": when(moved + timedelta(hours=1))})
            patch(items[4]["id"], {"description": "edited on its own"})
            print("  cancelled its 1st event, moved its 2nd, described its 5th")
        return r_id, items[2]

    def show(series_id: str, label: str = "series") -> None:
        series = get(series_id)
        print(f"  {label} {series_id}: {series.get('status')}, {series.get('recurrence')}")
        for item in instances(series_id, deleted=True):
            fields = [f"orig={_original_start(item):%m-%d %H:%M}", f"start={_time(item['start'])}", item["status"]]
            if "description" in item:
                fields.append(repr(item["description"]))
            print("     " + "  ".join(fields))

    def show_later(series_id: str, at: datetime) -> None:
        listed = events.list(calendarId=calendar_id, iCalUID=f"{series_id.split('_R')[0]}_R{_stamp(at)}@google.com")
        for item in listed.execute().get("items", []):
            if item.get("recurrence"):
                show(item["id"], "rest")

    def until(item: dict) -> str:
        return (_original_start(item) - timedelta(seconds=1)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def attempt(label: str, write: Callable[[str, dict], object], edited: bool) -> None:
        r_id, cut = google_split(edited)
        print(label)
        try:
            write(r_id, cut)
            print("  -> OK")
        except HttpError as exc:
            print(f"  -> FAILED {exc.resp.status} {exc.reason!r}")
        show(r_id)
        show_later(r_id, _original_start(cut))

    print("\n1. End an \"_R\" series before its 3rd event")
    for edited in (False, True):
        print(f"\n===== {'Edited' if edited else 'Unedited'} =====")
        attempt(
            "a. patch: recurrence with UNTIL",
            lambda r, cut: patch(r, {"recurrence": [f"{_RULE};UNTIL={until(cut)}"]}),
            edited,
        )
        attempt(
            "b. patch: recurrence with UNTIL, plus start/end",
            lambda r, cut: patch(
                r, {"recurrence": [f"{_RULE};UNTIL={until(cut)}"], **{k: get(r)[k] for k in ("start", "end")}}
            ),
            edited,
        )
        attempt("c. patch: recurrence with COUNT=2", lambda r, cut: patch(r, {"recurrence": [f"{_RULE};COUNT=2"]}), edited)
        attempt("d. patch: recurrence unchanged", lambda r, cut: patch(r, {"recurrence": [_RULE]}), edited)
        attempt("e. patch: summary only", lambda r, cut: patch(r, {"summary": "renamed"}), edited)
        attempt(
            "f. update (PUT): the whole series, with UNTIL",
            lambda r, cut: events.update(
                calendarId=calendar_id, eventId=r, body={**get(r), "recurrence": [f"{_RULE};UNTIL={until(cut)}"]}
            ).execute(),
            edited,
        )
        attempt(
            "g. import <base id>_R<3rd event> (a split, as in the UI), then import it again",
            lambda r, cut: [import_split(r, _original_start(cut), colorId="5") for _ in range(2)],
            edited,
        )
        attempt(
            "h. import its own UID with other rules",
            lambda r, cut: import_split(r, _original_start(get(r)), recurrence=["RRULE:FREQ=WEEKLY;BYDAY=MO"]),
            edited,
        )
        attempt(
            "i. import <base id>_R<3rd event>, then cancel that",
            lambda r, cut: patch(import_split(r, _original_start(cut))["id"], {"status": "cancelled"}),
            edited,
        )

    print("\n2. Recurrences on \"_R\" series (1st event cancelled, etc.)")
    print("\na. split at its 3rd event")
    r_id, cut = google_split(edited=True)
    earlier, later = recurrences.split(cut["id"])
    show(earlier.id, "earlier")
    show(later.id, "later")
    show_later(r_id, _original_start(cut))

    print("\nb. delete from its 3rd event on")
    r_id, cut = google_split(edited=True)
    recurrences.delete(r_id, starting_at=cut["id"])
    show(r_id)
    show_later(r_id, _original_start(cut))

    print("\nc. move the whole series' start 30 minutes later (no rules)")
    r_id, cut = google_split(edited=True)
    series = client.get_event(r_id)
    for minutes in (30, 0):
        moved = timedelta(minutes=minutes)
        try:
            recurrences.update(Event(id=r_id, start=series.start + moved, end=series.end + timedelta(minutes=30)))
            print(f"  start +{minutes}m, end +30m -> OK")
        except (HttpError, ValueError) as exc:
            print(f"  start +{minutes}m, end +30m -> FAILED: {exc}")
    show(r_id)

    print("\nd. change the whole series' rules")
    r_id, cut = google_split(edited=True)
    try:
        recurrences.update(Event(id=r_id), repeat=Repeat(every="week", weekdays=["mon"]))
        print("  -> OK")
    except ValueError as exc:
        print(f"  -> refused: {exc}")
    show(r_id)

    print("\ne. a split undone (its copy cancelled), then retried")
    r_id, cut = google_split(edited=False)
    at = _original_start(cut)
    copy = client.create_event(
        Event(id=split_series_id(r_id, at), summary="Probe", start=at, end=at + timedelta(hours=1),
              time_zone=_TIME_ZONE, recurrence=[_RULE])
    )
    patch(copy.id, {"status": "cancelled"})
    print(f"  copy {copy.id} cancelled: {len(instances(copy.id))} instances listed")
    earlier, later = recurrences.split(cut["id"])
    show(earlier.id, "earlier")
    show(later.id, "later")


def _original_start(event: dict) -> datetime:
    return datetime.fromisoformat((event.get("originalStartTime") or event["start"])["dateTime"])


def _stamp(at: datetime) -> str:
    """`at` as Google writes it in an "_R" id: 20260915T223000, in UTC."""
    return at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _time(when: dict) -> str:
    moment = when.get("dateTime")
    return datetime.fromisoformat(moment).astimezone(_ZONE).strftime("%m-%d %H:%M") if moment else "-"


if __name__ == "__main__":
    main()
