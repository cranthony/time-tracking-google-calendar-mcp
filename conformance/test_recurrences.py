"""Recurring series: reading one, editing it whole or from an event on,
splitting it, deleting it whole or from an event on -- series made in
Google Calendar itself (seeded straight into the fake), including one
Google split itself ("_R"), and series made by giving an event a repeat."""

from __future__ import annotations

ZONE = "America/New_York"


def series(google, calendar_id, *, summary="Standup", start="2026-10-05T09:00:00", end="2026-10-05T09:30:00", rules=("RRULE:FREQ=DAILY;COUNT=10",), event_id=None, **fields) -> str:
    body = {
        "summary": summary,
        "start": {"dateTime": start, "timeZone": ZONE},
        "end": {"dateTime": end, "timeZone": ZONE},
        "recurrence": list(rules),
        **fields,
    }
    if event_id:
        body["id"] = event_id
    return google.add_event(calendar_id, body)


async def instances(session, day_from="2026-10-01", day_to="2026-10-31"):
    return await session.call("list_events", min_time=f"{day_from}T00:00:00-04:00", max_time=f"{day_to}T00:00:00-04:00")


async def test_read_series(session, google, calendar_id):
    master = series(google, calendar_id, description="daily sync", rules=("RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR;UNTIL=20261030T235959Z",))
    listed = await instances(session)
    assert len(listed) == 12
    assert all(e["recurring_event_id"] == master for e in listed)
    by_series = await session.call("get_recurrence", id=master)
    by_instance = await session.call("get_recurrence", id=listed[3]["id"])
    assert by_series == by_instance
    assert by_series["repeat"]["every"] == "week"
    assert by_series["schedule"]
    await session.call_error("get_recurrence", id="nosuch")
    single = (await session.call("create_event", events=[{"summary": "One-off", "start": "2026-10-07T11:00:00-04:00", "end": "2026-10-07T11:30:00-04:00"}]))["events"][0]
    await session.call_error("get_recurrence", id=single["id"])


async def test_rules_repeat_cant_say(session, google, calendar_id):
    master = series(google, calendar_id, rules=("RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1;COUNT=4",))
    got = await session.call("get_recurrence", id=master)
    assert got["repeat"] is None
    assert "BYSETPOS" in got["schedule"]
    # Edits that leave repeat out keep them.
    await session.call("update_recurrence", recurrence={"id": master, "summary": "Month-end review"})
    assert google.calendars[calendar_id].events[master]["recurrence"] == ["RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1;COUNT=4"]


async def test_update_whole_series(session, google, calendar_id):
    action = (await session.call("create_action", action={"name": "attend standup", "status": "active", "priority": 1}))["created_id"]
    master = series(google, calendar_id)
    listed = await instances(session)
    # One instance edited on its own first.
    await session.call("update_event", updates=[{"event": {"id": listed[4]["id"], "description": "special", "start": "2026-10-09T09:15:00-04:00", "end": "2026-10-09T09:45:00-04:00"}}])
    [updated] = await session.call("update_recurrence", recurrence={"id": master, "summary": "Team standup", "action_ids": [action], "location": "Room 1"})
    assert updated["summary"] == "Team standup"
    assert updated["action_names"] == ["attend standup"]
    after = await instances(session)
    assert {e["summary"] for e in after} == {"Team standup"}
    assert after[4]["description"] is None and after[4]["start"] == "2026-10-09T09:15:00-04:00"
    # Repeat replaced whole; a time change moves every event.
    await session.call(
        "update_recurrence",
        recurrence={"id": master, "start": "2026-10-05T10:00:00-04:00", "end": "2026-10-05T10:15:00-04:00", "repeat": {"every": "week", "weekdays": ["mon", "thu"], "count": 6}},
    )
    moved = await instances(session)
    assert len(moved) == 6 and all(e["start"].endswith("T10:00:00-04:00") for e in moved)
    await session.call("update_recurrence", recurrence={"id": master}, clear_fields=["location"])
    await session.call_error("update_recurrence", recurrence={"id": master, "location": "x"}, clear_fields=["location"])
    await session.call_error("update_recurrence", recurrence={"id": master, "repeat": {"every": "fortnight"}})
    await session.call_error("update_recurrence", recurrence={"id": master, "action_ids": ["nosuch"]})


async def test_repeat_forms(session, google, calendar_id):
    master = series(google, calendar_id)
    forms = [
        {"every": "day", "interval": 2, "until": "2026-10-20"},
        {"every": "week", "weekdays": ["tue"], "week_starts_on": "sun", "until": "2026-11-15T23:59:00-05:00"},
        {"every": "month", "month_days": [5, 20], "count": 4},
        {"every": "month", "nth_weekdays": [{"nth": -1, "weekday": "fri"}], "count": 3},
        {"every": "year", "months": [10], "month_days": [5], "count": 2},
        {"every": "day", "count": 5, "skipped": ["2026-10-07T09:00:00-04:00"], "added": ["2026-10-11T09:00:00-04:00"]},
    ]
    for repeat in forms:
        [got] = await session.call("update_recurrence", recurrence={"id": master, "repeat": repeat})
        assert got["repeat"]["every"] == repeat["every"]
        await instances(session, "2026-10-01", "2027-12-31")


async def test_split(session, google, calendar_id):
    master = series(google, calendar_id)
    listed = await instances(session)
    later, earlier = await session.call("split_recurrence", event_id=listed[4]["id"])
    assert earlier["id"] == master and later["id"] != master
    assert later["repeat"]["count"] == 6
    assert len(await instances(session)) == 10
    # Splitting at a series' first event leaves nothing to split.
    only = await session.call("split_recurrence", event_id=(await instances(session))[0]["id"])
    assert len(only) == 1


async def test_update_this_and_following(session, google, calendar_id):
    master = series(google, calendar_id, rules=("RRULE:FREQ=DAILY;UNTIL=20261020T130000Z",))
    listed = await instances(session)
    later, earlier = await session.call(
        "update_recurrence",
        recurrence={"id": master, "summary": "Late standup", "start": "2026-10-05T09:30:00-04:00", "end": "2026-10-05T10:00:00-04:00"},
        starting_at_event_id=listed[6]["id"],
    )
    after = await instances(session)
    assert [e["summary"] for e in after[:6]] == ["Standup"] * 6
    assert {e["summary"] for e in after[6:]} == {"Late standup"}
    assert after[6]["start"] == "2026-10-11T09:30:00-04:00"
    assert later and earlier


async def test_delete(session, google, calendar_id):
    a = series(google, calendar_id, summary="A")
    b = series(google, calendar_id, summary="B", start="2026-10-05T12:00:00", end="2026-10-05T12:30:00")
    listed_a = [e for e in await instances(session) if e["summary"] == "A"]
    left = await session.call("delete_recurrence", id=a, starting_at_event_id=listed_a[3]["id"])
    assert len(left) == 1
    assert len([e for e in await instances(session) if e["summary"] == "A"]) == 3
    # One instance cancelled on its own, then the whole series deleted.
    listed_b = [e for e in await instances(session) if e["summary"] == "B"]
    await session.call("delete_event", cancels=[{"event_id": listed_b[1]["id"], "counts_against_follow_through": False}])
    assert await session.call("delete_recurrence", id=listed_b[0]["id"]) == []
    assert [e["summary"] for e in await instances(session)] == ["A"] * 3
    # From the first event on is the whole series.
    assert await session.call("delete_recurrence", id=a, starting_at_event_id=listed_a[0]["id"]) == []
    assert await instances(session) == []
    await session.call_error("delete_recurrence", id="nosuch")


async def test_series_split_by_google(session, google, calendar_id):
    base = series(google, calendar_id, summary="Gym", event_id="gymbase0001", start="2026-09-21T18:00:00", end="2026-09-21T19:00:00", rules=("RRULE:FREQ=WEEKLY;WKST=SU;BYDAY=MO,TU;UNTIL=20261004T035959Z",))
    split = series(google, calendar_id, summary="Gym", event_id="gymbase0001_R20261005T220000", start="2026-10-05T18:00:00", end="2026-10-05T19:00:00", rules=("RRULE:FREQ=WEEKLY;WKST=SU;BYDAY=MO,TU",))
    listed = [e for e in await instances(session, "2026-10-01", "2026-11-01")]
    assert {e["recurring_event_id"] for e in listed} == {split}
    # Its rules or start can't be changed whole...
    await session.call_error("update_recurrence", recurrence={"id": split, "repeat": {"every": "week", "weekdays": ["mon"]}})
    # ...but it can be ended from a later event, and edited otherwise.
    await session.call("update_recurrence", recurrence={"id": split, "summary": "Gym session"})
    left = await session.call("delete_recurrence", id=split, starting_at_event_id=listed[4]["id"])
    assert len(left) == 1
    remaining = await instances(session, "2026-10-01", "2026-11-01")
    assert len(remaining) == 4
    assert base


async def test_single_instance_edits(session, google, calendar_id):
    master = series(google, calendar_id)
    listed = await instances(session)
    await session.call("update_event", updates=[{"event": {"id": listed[2]["id"], "summary": "Standup (moved)", "start": "2026-10-07T09:45:00-04:00", "end": "2026-10-07T10:15:00-04:00", "priority": 0}}])
    await session.call("delete_event", cancels=[{"event_id": listed[3]["id"], "counts_against_follow_through": True}])
    after = await instances(session)
    assert len(after) == 9
    assert after[2]["summary"] == "Standup (moved)"
    # Clearing a property an instance doesn't have is fine.
    await session.call("update_event", updates=[{"event": {"id": listed[5]["id"]}, "clear_fields": ["facts", "judgments"]}])
    assert master


async def test_series_from_events(session, google, calendar_id):
    created = (
        await session.call(
            "create_event",
            events=[{"summary": "Run", "start": "2026-10-05T07:00:00-04:00", "end": "2026-10-05T07:30:00-04:00", "repeat": {"every": "week", "weekdays": ["mon", "thu"], "count": 4}}],
        )
    )["events"][0]
    # Write-only: never given back.
    assert "repeat" not in created
    runs = [e for e in await instances(session) if e["recurring_event_id"] == created["id"]]
    assert len(runs) == 4 and all("repeat" not in e for e in runs)
    got = await session.call("get_recurrence", id=created["id"])
    assert got["repeat"]["weekdays"] == ["mon", "thu"] and got["time_zone"]
    # An event that doesn't repeat yet becomes the first of a series.
    single = (await session.call("create_event", events=[{"summary": "Review", "start": "2026-10-07T11:00:00-04:00", "end": "2026-10-07T11:30:00-04:00"}]))["events"][0]
    await session.call("update_event", updates=[{"event": {"id": single["id"], "repeat": {"every": "day", "count": 2}}}])
    assert (await session.call("get_recurrence", id=single["id"]))["repeat"]["count"] == 2
    # A series itself is update_recurrence's to change; an event in one
    # can't be given a repeat; a repeat that breaks a rule is refused.
    await session.call_error("update_event", updates=[{"event": {"id": single["id"], "summary": "Weekly review"}}])
    await session.call_error("update_event", updates=[{"event": {"id": runs[0]["id"], "repeat": {"every": "day"}}}])
    await session.call_error(
        "create_event",
        events=[{"summary": "Swim", "start": "2026-10-08T07:00:00-04:00", "end": "2026-10-08T07:30:00-04:00", "repeat": {"every": "day", "count": 2, "until": "2026-10-20"}}],
    )
