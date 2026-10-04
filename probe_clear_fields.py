"""Check against the live API that clearing an event's fields (Event.cleared,
which update_event's and update_recurrence's clear_fields set) really
removes them.

Event.to_api_body sends each cleared field as null in a patch: description
and location as top-level fields, the app's own fields (priority,
min_duration, is_fixed_duration, is_fixed_time) as keys of
extendedProperties.private, and colorId too for priority. Google documents
patch as replacing only the fields it's given, but not, in so many words,
that a null removes a top-level field, or that a null key of
extendedProperties.private removes that key while the rest are kept.
This script writes through CalendarClient and Recurrences themselves, so
it checks the bodies the tools actually send.

It creates a throwaway calendar (this app's calendar.app.created scope
allows that), then:

1. creates an event with every clearable field set, plus goal_ids, and
   clears them all while renaming it;
2. creates an event with a priority and a min_duration, and clears only
   the min_duration;
3. creates a daily series with a priority and a location, and clears its
   priority, checking the series and each of its instances;
4. clears another series' priority from its third event on ("this and
   following"), checking both parts.

Each check prints PASS or FAIL. It then deletes the calendar again --
nothing else in your account is touched.

Usage:
    python probe_clear_fields.py [--keep]

Found (2026-10-04): every check passed.

- A null top-level field in a patch removes it: description, location
  and colorId (the event then shows the calendar's default color).
- A null key of extendedProperties.private removes just that key; the
  others are kept, whether the patch sets them or leaves them out.
- On a series' master, the clear reaches every instance; split first
  ("this and following"), it reaches only the later part.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from calendar_clients.google_calendar import (
    _APP_EXTENDED_PROPERTY_KEY_PREFIX,
    CLEARABLE_EVENT_FIELDS,
    CalendarClient,
    Event,
)
from calendar_clients.write_lock import WRITE_LOCK
from config import get_credentials_path, get_token_path
from utilities.recurrences import Recurrences

_TIME_ZONE = "America/New_York"
_ZONE = ZoneInfo(_TIME_ZONE)

_failures = 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="Don't delete the scratch calendar")
    args = parser.parse_args()

    with WRITE_LOCK:
        # The calendar id is only needed to build the client; any of this
        # app's calendars will do until the scratch one exists.
        home = CalendarClient.from_credentials(get_token_path(), get_credentials_path(), "primary")
        calendar_id = home.create_calendar("CTT clear-fields probe (safe to delete)", time_zone=_TIME_ZONE)
        print(f"Scratch calendar: {calendar_id}")
        client = home.for_calendar(calendar_id)
        try:
            _probe(client)
        finally:
            if args.keep:
                print(f"Kept scratch calendar {calendar_id}")
            else:
                client._service.calendars().delete(calendarId=calendar_id).execute()
                print("Deleted scratch calendar")
    print(f"\n{'All checks passed' if not _failures else f'{_failures} check(s) FAILED'}")


def _check(what: str, ok: bool, detail: object = "") -> None:
    global _failures
    _failures += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {what}" + (f"  ({detail})" if not ok and detail != "" else ""))


def _raw(client: CalendarClient, event_id: str) -> dict:
    return client._service.events().get(calendarId=client.calendar_id, eventId=event_id).execute()


def _private(resource: dict) -> dict:
    return resource.get("extendedProperties", {}).get("private", {})


def _key(name: str) -> str:
    return f"{_APP_EXTENDED_PROPERTY_KEY_PREFIX}{name}"


def _probe(client: CalendarClient) -> None:
    tomorrow = datetime.now(_ZONE).date() + timedelta(days=1)
    nine = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 9, tzinfo=_ZONE)

    print("\n1. Clear every clearable field of an event, while renaming it")
    full = client.create_event(
        Event(
            summary="Probe event",
            start=nine,
            end=nine + timedelta(hours=1),
            time_zone=_TIME_ZONE,
            description="a description",
            location="a location",
            min_duration=timedelta(minutes=15),
            is_fixed_duration=True,
            is_fixed_time=True,
            priority=1,
            goal_ids=["goalA"],
        )
    )
    before = _raw(client, full.id)
    _check("set up with a colorId and all the app's fields", "colorId" in before and len(_private(before)) == 5, before)
    returned = client.update_event(Event(id=full.id, summary="Probe event (renamed)", cleared=CLEARABLE_EVENT_FIELDS))
    after = _raw(client, full.id)
    _check("summary renamed", after.get("summary") == "Probe event (renamed)", after.get("summary"))
    _check("description removed", not after.get("description"), after.get("description"))
    _check("location removed", not after.get("location"), after.get("location"))
    _check("colorId removed (calendar default)", "colorId" not in after, after.get("colorId"))
    _check(
        "only goal_ids left in extendedProperties.private",
        _private(after) == {_key("goal_ids"): "goalA"},
        _private(after),
    )
    _check("times kept", after["start"] == before["start"] and after["end"] == before["end"])
    _check(
        "the patch's response parses with every cleared field None",
        all(getattr(returned, name) is None for name in CLEARABLE_EVENT_FIELDS),
        returned,
    )

    print("\n2. Clear only an event's min_duration")
    partial = client.create_event(
        Event(
            summary="Probe partial",
            start=nine + timedelta(hours=2),
            end=nine + timedelta(hours=3),
            time_zone=_TIME_ZONE,
            min_duration=timedelta(minutes=20),
            priority=3,
        )
    )
    color = _raw(client, partial.id).get("colorId")
    client.update_event(Event(id=partial.id, cleared=frozenset({"min_duration"})))
    after = _raw(client, partial.id)
    _check("min_duration removed", _key("min_duration") not in _private(after), _private(after))
    _check("priority kept", _private(after).get(_key("priority")) == "3", _private(after))
    _check("colorId kept", after.get("colorId") == color, (color, after.get("colorId")))

    recurrences = Recurrences(client, client.list_instances, client.get_time_zone)

    def series(summary: str, hour: int) -> Event:
        start = nine + timedelta(hours=hour)
        return client.create_event(
            Event(
                summary=summary,
                start=start,
                end=start + timedelta(minutes=30),
                time_zone=_TIME_ZONE,
                recurrence=["RRULE:FREQ=DAILY;COUNT=4"],
                priority=1,
                location="Room 4",
            )
        )

    def instances(series_id: str) -> list[dict]:
        items = client._service.events().instances(calendarId=client.calendar_id, eventId=series_id).execute()
        return items.get("items", [])

    print("\n3. Clear a series' priority")
    whole = series("Probe series", 4)
    recurrences.update(Event(id=whole.id, cleared=frozenset({"priority"})))
    master = _raw(client, whole.id)
    _check("series' priority removed", _key("priority") not in _private(master), _private(master))
    _check("series' colorId removed", "colorId" not in master, master.get("colorId"))
    _check("series' location kept", master.get("location") == "Room 4", master.get("location"))
    items = instances(whole.id)
    _check(
        f"all {len(items)} instances lost their priority and color, and kept the location",
        len(items) == 4
        and all(
            _key("priority") not in _private(i) and "colorId" not in i and i.get("location") == "Room 4"
            for i in items
        ),
        [(_private(i), i.get("colorId"), i.get("location")) for i in items],
    )

    print("\n4. Clear another series' priority from its third event on")
    split = series("Probe split series", 6)
    third = sorted(instances(split.id), key=lambda i: i["start"]["dateTime"])[2]
    later, earlier = recurrences.update(
        Event(id=split.id, cleared=frozenset({"priority"})), starting_at=third["id"]
    )
    later_raw, earlier_raw = _raw(client, later.id), _raw(client, earlier.id)
    _check("later part's priority removed", _key("priority") not in _private(later_raw), _private(later_raw))
    _check("later part's colorId removed", "colorId" not in later_raw, later_raw.get("colorId"))
    _check(
        "earlier part kept its priority and colorId",
        _private(earlier_raw).get(_key("priority")) == "1" and "colorId" in earlier_raw,
        (_private(earlier_raw), earlier_raw.get("colorId")),
    )
    _check(
        "2 events in each part",
        (len(instances(earlier.id)), len(instances(later.id))) == (2, 2),
        (len(instances(earlier.id)), len(instances(later.id))),
    )


if __name__ == "__main__":
    main()
