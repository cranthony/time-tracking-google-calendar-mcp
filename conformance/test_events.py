"""Events: listing, getting, creating, updating, cancelling and shifting
them, through the event tools -- with the batch checks (overlaps, empty
events, history) and the errors they give."""

from __future__ import annotations

import pytest

DAY = "2026-10-07"


def at(hour: int, minute: int = 0, day: str = DAY) -> str:
    return f"{day}T{hour:02d}:{minute:02d}:00-04:00"


async def make_action(session, name: str, **fields) -> str:
    created = await session.call("create_action", action={"name": name, "status": "active", **fields})
    return created["created_id"]


async def test_empty_calendar(session):
    assert await session.call("list_events", min_time=at(0), max_time=at(23)) == []
    await session.call_error("get_event", id="nosuchevent")


async def test_create_list_get(session):
    guitar = await make_action(session, "play guitar", priority=2)
    created = await session.call(
        "create_event",
        events=[
            {"summary": "Practice", "start": at(9), "end": at(10), "action_ids": [guitar], "description": "scales", "location": "home"},
            {"summary": "Lunch", "start": at(12), "end": at(13), "priority": 3},
        ],
    )
    assert [e["summary"] for e in created["events"]] == ["Practice", "Lunch"]
    assert created["timeline"]
    listed = await session.call("list_events", min_time=at(0), max_time=at(23, 59))
    assert [e["summary"] for e in listed] == ["Practice", "Lunch"]
    practice = listed[0]
    assert practice["action_ids"] == [guitar]
    assert practice["action_names"] == ["play guitar"]
    assert practice["effective_priority"] == 2
    assert practice["event_label_id"]
    assert listed[1]["effective_priority"] == 3
    got = await session.call("get_event", id=practice["id"])
    assert got == practice
    # A listing that only touches the edge of an event leaves it out.
    assert await session.call("list_events", min_time=at(10), max_time=at(12)) == []
    assert len(await session.call("list_events", min_time=at(9, 30), max_time=at(12, 30))) == 2


async def test_many_events_page_through(session):
    events = [{"summary": f"Block {i}", "start": at(6 + i // 2, 30 * (i % 2)), "end": at(6 + i // 2, 30 * (i % 2) + 29)} for i in range(24)]
    await session.call("create_event", events=events)
    listed = await session.call("list_events", min_time=at(0), max_time=at(23, 59))
    assert [e["summary"] for e in listed] == [f"Block {i}" for i in range(24)]


async def test_create_refuses_overlaps_and_empty(session):
    await session.call("create_event", events=[{"summary": "Meeting", "start": at(9), "end": at(10)}])
    overlap = await session.call_error("create_event", events=[{"summary": "Clash", "start": at(9, 30), "end": at(10, 30)}])
    assert "Meeting" in overlap
    within = await session.call_error(
        "create_event",
        events=[{"summary": "A", "start": at(11), "end": at(12)}, {"summary": "B", "start": at(11, 30), "end": at(12, 30)}],
    )
    assert "A" in within and "B" in within
    await session.call_error("create_event", events=[{"summary": "Backwards", "start": at(15), "end": at(14)}])
    await session.call_error("create_event", events=[{"summary": "Empty", "start": at(15), "end": at(15)}])
    # Nothing was written by the refused batches.
    assert [e["summary"] for e in await session.call("list_events", min_time=at(0), max_time=at(23))] == ["Meeting"]


async def test_dry_run_changes_nothing(session):
    result = await session.call("create_event", events=[{"summary": "Maybe", "start": at(9), "end": at(10)}], dry_run=True)
    assert result["dry_run"] is True
    assert result["events"][0]["summary"] == "Maybe"
    assert await session.call("list_events", min_time=at(0), max_time=at(23)) == []


async def test_update_fields_and_clear(session):
    guitar = await make_action(session, "play guitar")
    piano = await make_action(session, "play piano", priority=1)
    [event] = (await session.call(
        "create_event",
        events=[{"summary": "Practice", "start": at(9), "end": at(10), "action_ids": [guitar], "description": "d", "location": "l", "priority": 4}],
    ))["events"]
    updated = await session.call(
        "update_event",
        updates=[{"event": {"id": event["id"], "summary": "Piano practice", "action_ids": [piano, guitar]}, "clear_fields": ["description", "priority"]}],
    )
    [after] = updated["events"]
    assert after["summary"] == "Piano practice"
    assert after["description"] is None
    assert after["location"] == "l"
    assert after["priority"] is None
    assert after["effective_priority"] == 1
    assert after["action_names"] == ["play piano", "play guitar"]
    # No actions at all.
    [none] = (await session.call("update_event", updates=[{"event": {"id": event["id"], "action_ids": []}}]))["events"]
    assert none["action_ids"] == [] or none["action_ids"] is None
    # Clearing a field that's also set, or one that can't be cleared.
    await session.call_error("update_event", updates=[{"event": {"id": event["id"], "location": "x"}, "clear_fields": ["location"]}])
    await session.call_error("update_event", updates=[{"event": {"id": event["id"], "is_cancelled": True}}])


async def test_update_refuses_unknown_actions(session):
    await make_action(session, "play guitar")
    [event] = (await session.call("create_event", events=[{"summary": "Practice", "start": at(9), "end": at(10)}]))["events"]
    error = await session.call_error("update_event", updates=[{"event": {"id": event["id"], "action_ids": ["play gitar"]}}])
    assert "gitar" in error
    await session.call_error("create_event", events=[{"summary": "X", "start": at(11), "end": at(12), "action_ids": ["nope"]}])


async def test_move_resize_and_overlap_in_batch(session):
    first, second = (await session.call(
        "create_event",
        events=[{"summary": "First", "start": at(9), "end": at(10)}, {"summary": "Second", "start": at(10), "end": at(11)}],
    ))["events"]
    # Lengthening First into Second is refused on its own...
    await session.call_error("update_event", updates=[{"event": {"id": first["id"], "end": at(10, 30)}}])
    # ...but fine with Second moved out of the way in the same batch.
    result = await session.call(
        "update_event",
        updates=[
            {"event": {"id": first["id"], "end": at(10, 30)}},
            {"event": {"id": second["id"], "start": at(10, 30), "end": at(11, 30)}},
        ],
    )
    assert [e["end"] for e in result["events"]] == ["2026-10-07T10:30:00-04:00", "2026-10-07T11:30:00-04:00"]


async def test_shifts(session):
    events = (await session.call(
        "create_event",
        events=[{"summary": "A", "start": at(9), "end": at(10)}, {"summary": "B", "start": at(10), "end": at(11)}, {"summary": "C", "start": at(13), "end": at(14)}],
    ))["events"]
    a, b, c = (e["id"] for e in events)
    shifted = await session.call("update_event", shifts=[{"event_ids": [a, b], "minutes": 30}])
    assert [e["start"] for e in shifted["events"]] == ["2026-10-07T09:30:00-04:00", "2026-10-07T10:30:00-04:00"]
    await session.call_error("update_event", shifts=[{"event_ids": [b], "minutes": 150}])
    await session.call("update_event", shifts=[{"event_ids": [c], "minutes": -60}])
    # The same event twice in a batch.
    await session.call_error("update_event", shifts=[{"event_ids": [a], "minutes": 5}], updates=[{"event": {"id": a, "summary": "x"}}])


async def test_cancel(session):
    a, b = (e["id"] for e in (await session.call(
        "create_event",
        events=[{"summary": "A", "start": at(9), "end": at(10)}, {"summary": "B", "start": at(11), "end": at(12)}],
    ))["events"])
    dry = await session.call("delete_event", cancels=[{"event_id": a, "counts_against_follow_through": False}], dry_run=True)
    assert dry["events"][0]["is_cancelled"] is True
    assert len(await session.call("list_events", min_time=at(0), max_time=at(23))) == 2
    done = await session.call("delete_event", cancels=[{"event_id": a, "counts_against_follow_through": True}])
    assert done["events"][0]["is_cancelled"] is True
    assert [e["summary"] for e in await session.call("list_events", min_time=at(0), max_time=at(23))] == ["B"]
    error = await session.call_error("get_event", id=a)
    assert "cancelled" in error
    await session.call_error("delete_event", cancels=[{"event_id": a, "counts_against_follow_through": False}])
    # A cancelled slot is free again.
    await session.call("update_event", creates=[{"summary": "A2", "start": at(9), "end": at(10)}], cancels=[{"event_id": b, "counts_against_follow_through": False}])
    assert [e["summary"] for e in await session.call("list_events", min_time=at(0), max_time=at(23))] == ["A2"]


async def test_facts_and_judgments(session):
    friend = (await session.call("create_person", person={"name": "Friend One"}))
    friend_id = friend["id"] if "id" in friend else friend["created_id"]
    park = await session.call("create_location", location={"name": "The Park"})
    park_id = park["id"] if "id" in park else park["created_id"]
    [event] = (await session.call(
        "create_event",
        events=[{"summary": "Walk", "start": at(9), "end": at(10), "facts": {"location_ids": [park_id], "with_ids": [friend_id], "notes": {friend_id: "enjoyed it", "self": "tired"}}}],
    ))["events"]
    assert event["facts"]["location_ids"] == [park_id]
    await session.call_error("create_event", events=[{"summary": "Bad", "start": at(11), "end": at(12), "facts": {"with_ids": ["nobody"]}}])
    await session.call_error("create_event", events=[{"summary": "Bad", "start": at(11), "end": at(12), "facts": {"location_ids": ["nowhere"]}}])
    await session.call_error("create_event", events=[{"summary": "Bad", "start": at(11), "end": at(12), "facts": {"with_ids": ["self"]}}])
    [commute] = (await session.call("create_event", events=[{"summary": "Drive", "start": at(10), "end": at(10, 30), "facts": {"commute": True}}]))["events"]
    assert commute["facts"]["commute"] is True
    judged = await session.call(
        "update_event",
        updates=[{"event": {"id": event["id"], "judgments": {"self": {"kindness": {"novelty": {"rating": 2, "scale": 3, "reasoning": "new park"}}}}}}],
    )
    assert judged["events"][0]["judgments"]["self"]["kindness"]["novelty"]["rating"] == 2
    cleared = await session.call("update_event", updates=[{"event": {"id": event["id"]}, "clear_fields": ["judgments", "facts"]}])
    assert cleared["events"][0]["judgments"] is None
    assert cleared["events"][0]["facts"] is None


async def test_long_description_round_trips(session):
    text = "\n".join(f"line {i}: " + "x" * 90 for i in range(120))
    [event] = (await session.call("create_event", events=[{"summary": "Notes", "start": at(9), "end": at(10), "description": text}]))["events"]
    assert (await session.call("get_event", id=event["id"]))["description"] == text


async def test_time_zone(session, google, calendar_id):
    await session.call("set_time_zone", time_zone="Europe/London")
    [event] = (await session.call("create_event", events=[{"summary": "Tea", "start": at(9), "end": at(10)}]))["events"]
    assert event["start"].endswith("+01:00")
    await session.call_error("set_time_zone", time_zone="Mars/Olympus")
    assert google.calendars[calendar_id].resource["timeZone"] == "Europe/London"


@pytest.mark.calendar(time_zone=None)
async def test_no_time_zone(session):
    """Schedule hints are in the calendar's zone: none until it has one."""
    before = await session.call("get_compaction_schedule_hints")
    await session.call("set_time_zone", time_zone="America/Chicago")
    after = await session.call("get_compaction_schedule_hints")
    assert before != after


async def test_priority_colors(session):
    colors = await session.call("get_priority_colors")
    assert colors
    await session.call("update_priority_color", priority=2, color="#123456")
    after = await session.call("get_priority_colors")
    assert "#123456" in str(after)
    await session.call_error("update_priority_color", priority=2, color="blue")
    await session.call_error("update_priority_color", priority=99, color="#123456")
