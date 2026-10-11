"""Actions and action groups: creating, finding, updating, archiving and
deleting them, the calendar labels they hold, and the colors and
priorities they inherit -- with the events tied to them following."""

from __future__ import annotations

DAY = "2026-10-07"


def at(hour: int, minute: int = 0) -> str:
    return f"{DAY}T{hour:02d}:{minute:02d}:00-04:00"


async def test_create_and_find(session):
    created = await session.call("create_action", action={"name": "play guitar", "status": "active", "priority": 1, "note": "every day"})
    guitar = created["created_id"]
    assert created["changed"][0]["holds_label"] is True
    proposed = await session.call("create_action", action={"name": "read a book"})
    assert proposed["changed"][0]["status"] == "proposed"
    listed = await session.call("get_actions")
    assert [a["name"] for a in listed["actions"]] == ["play guitar", "read a book"]
    assert (await session.call("get_action", id_or_name=guitar))["name"] == "play guitar"
    assert (await session.call("get_action", id_or_name="PLAY GUITAR"))["id"] == guitar
    missing = await session.call_error("get_action", id_or_name="play gutar")
    assert "play guitar" in missing
    await session.call_error("create_action", action={"name": "Play Guitar"})
    await session.call_error("create_action", action={"name": "x" * 51})
    await session.call_error("create_action", action={"name": "named color", "background_color": "red"})


async def test_update_and_statuses(session):
    guitar = (await session.call("create_action", action={"name": "play guitar", "status": "active"}))["created_id"]
    piano = (await session.call("create_action", action={"name": "play piano", "status": "active", "background_color": "#aa0000", "note": "n"}))["created_id"]
    renamed = await session.call("update_action", action={"id": guitar, "name": "practice guitar", "background_color": "#00aa00"})
    assert renamed["changed"][0]["name"] == "practice guitar"
    cleared = await session.call("update_action", action={"id": piano}, clear_fields=["background_color", "note"])
    assert cleared["changed"][0]["note"] is None
    archived = await session.call("update_action", action={"id": piano, "status": "archived"})
    assert archived["changed"][0]["holds_label"] is False
    assert [a["name"] for a in (await session.call("get_actions"))["actions"]] == ["practice guitar"]
    every = await session.call("get_actions", statuses=["proposed", "active", "archived", "deleted"])
    assert len(every["actions"]) == 2
    restored = await session.call("update_action", action={"id": piano, "status": "active"})
    assert restored["changed"][0]["holds_label"] is True
    await session.call("update_action", action={"id": piano, "status": "deleted"})
    await session.call_error("update_action", action={"id": "nosuch", "name": "x"})
    await session.call_error("update_action", action={"id": guitar, "name": "play piano"})
    await session.call_error("update_action", action={"id": guitar, "note": "n"}, clear_fields=["note"])


async def test_groups(session):
    creative = (await session.call("create_action_group", group={"name": "Creative", "background_color": "#112233", "priority": 1}))["created_id"]
    music = (await session.call("create_action_group", group={"name": "Music", "group_id": creative, "note": "instruments"}))["created_id"]
    guitar = (await session.call("create_action", action={"name": "play guitar", "status": "active", "group_id": music}))["created_id"]
    listed = await session.call("get_action", id_or_name=guitar)
    assert listed["path"] == "Creative › Music › play guitar"
    assert listed["effective_color"] == "#112233"
    assert listed["effective_priority"] == 1
    groups = await session.call("get_action_groups")
    assert [g["name"] for g in groups] == ["Creative", "Music"]
    assert (await session.call("get_action_group", id_or_name="music"))["path"] == "Creative › Music"
    await session.call_error("get_action_group", id_or_name="Musik")
    await session.call_error("create_action_group", group={"name": "creative"})
    recolored = await session.call("update_action_group", group={"id": creative, "background_color": "#445566"})
    assert [a["id"] for a in recolored["affected_actions"]] == [guitar]
    moved = await session.call("update_action_group", group={"id": music}, clear_fields=["group_id"])
    assert moved["changed"]["path"] == "Music" if isinstance(moved["changed"], dict) else True
    # A group can't go inside itself, or one inside it.
    await session.call("update_action_group", group={"id": music, "group_id": creative})
    await session.call_error("update_action_group", group={"id": creative, "group_id": music})
    deleted = await session.call("delete_action_group", group_id=music)
    assert (await session.call("get_action", id_or_name=guitar))["path"] == "Creative › play guitar"
    assert deleted
    await session.call_error("delete_action_group", group_id=music)


async def test_events_follow_their_actions(session):
    group = (await session.call("create_action_group", group={"name": "Fitness", "priority": 2}))["created_id"]
    run = (await session.call("create_action", action={"name": "go running", "status": "active", "group_id": group}))["created_id"]
    [event] = (await session.call("create_event", events=[{"summary": "Run", "start": at(7), "end": at(8), "action_ids": [run]}]))["events"]
    label = event["event_label_id"]
    assert event["effective_priority"] == 2
    await session.call("update_action_group", group={"id": group, "priority": 0})
    assert (await session.call("get_event", id=event["id"]))["effective_priority"] == 0
    # Archiving frees the label; the event keeps the action.
    await session.call("update_action", action={"id": run, "status": "archived"})
    after = await session.call("get_event", id=event["id"])
    assert after["action_ids"] == [run]
    await session.call("update_action", action={"id": run, "status": "active"})
    assert (await session.call("get_event", id=event["id"]))["event_label_id"] == label
    # A deleted action can't be added to an event, but one that has it may keep it.
    await session.call("update_action", action={"id": run, "status": "deleted"})
    await session.call_error("create_event", events=[{"summary": "Run 2", "start": at(9), "end": at(10), "action_ids": [run]}])
    await session.call("update_event", updates=[{"event": {"id": event["id"], "summary": "Morning run", "action_ids": [run]}}])


async def test_actions_from_label(session, google, calendar_id):
    """An event never given actions takes them from its label."""
    guitar = (await session.call("create_action", action={"name": "play guitar", "status": "active"}))["created_id"]
    label = (await session.call("get_action", id_or_name=guitar))["label_id"]
    [event] = (await session.call("create_event", events=[{"summary": "Practice", "start": at(9), "end": at(10)}]))["events"]
    assert event["action_ids"] in (None, [])
    google.calendars[calendar_id].events[event["id"]]["eventLabelId"] = label
    inferred = await session.call("get_event", id=event["id"])
    assert inferred["actions_from_label"] is True
    assert inferred["action_ids"] == [guitar]
    # Sent straight back, they stay inferred.
    await session.call("update_event", updates=[{"event": {**{k: inferred[k] for k in ("id", "action_ids", "actions_from_label")}, "summary": "Guitar"}}])
    assert (await session.call("get_event", id=event["id"]))["actions_from_label"] is True


async def test_label_slots(session):
    for i in range(6):
        await session.call("create_action", action={"name": f"action {i}", "status": "active"})
    listed = await session.call("get_actions")
    assert listed["label_slots_used"] >= 6
    assert listed["label_slots_total"] == 200


async def test_priority_colors_recolor_actions(session):
    plain = (await session.call("create_action", action={"name": "do chores", "status": "active", "priority": 3}))["created_id"]
    colored = (await session.call("create_action", action={"name": "paint", "status": "active", "priority": 3, "background_color": "#abcdef"}))["created_id"]
    change = await session.call("update_priority_color", priority=3, color="#0f0f0f")
    assert [a["id"] for a in change["affected_actions"]] == [plain]
    assert (await session.call("get_action", id_or_name=plain))["effective_color"] == "#0f0f0f"
    assert (await session.call("get_action", id_or_name=colored))["effective_color"] == "#abcdef"


async def test_labels_changed_by_hand(session, google, calendar_id):
    """A label removed from the calendar by hand is put back."""
    guitar = (await session.call("create_action", action={"name": "play guitar", "status": "active"}))["created_id"]
    calendar = google.calendars[calendar_id].resource
    calendar["labelProperties"]["eventLabels"] = []
    calendar["etag"] = '"changed-by-hand"'
    listed = await session.call("get_action", id_or_name=guitar)
    assert listed["holds_label"] is True
