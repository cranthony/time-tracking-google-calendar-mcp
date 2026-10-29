"""Goals: making them, nesting them, closing a goal with its sub-goals,
and tagging events with them."""

from __future__ import annotations


async def test_goals(session):
    top = (await session.call("create_goal", goal={"title": "Ship the app", "deadline": {"at": "2026-12-01"}, "expected_minutes": 3000, "background_color": "#123456"}))["created_id"]
    await session.call_error("create_goal", goal={"title": "No deadline"})
    sub = (await session.call("create_goal", goal={"title": "Write the pane", "parent_id": top, "deadline": {"at": "2026-11-15"}, "required": True}))["created_id"]
    later = (await session.call("create_goal", goal={"title": "Polish", "parent_id": top, "status": "inactive", "deadline": {"after_start": {"days": 7}}}))["created_id"]
    await session.call_error("create_goal", goal={"title": "Too late", "parent_id": top, "deadline": {"at": "2026-12-02"}})
    await session.call_error("create_goal", goal={"title": "Too long", "parent_id": top, "deadline": {"at": "2026-11-01"}, "expected_minutes": 3001})
    await session.call_error("create_goal", goal={"title": "Odd", "deadline": {"at": "2026-11-01"}, "background_color": "blue"})

    listed = await session.call("get_goals")
    assert [g["title"] for g in listed] == ["Ship the app", "Write the pane", "Polish"]
    # A sub-goal with no color of its own takes its nearest parent's.
    assert [(g["background_color"], g["effective_color"]) for g in listed] == [("#123456", "#123456"), (None, "#123456"), (None, "#123456")]
    assert (await session.call("get_goal", id_or_title="write THE pane"))["path"] == ["Ship the app"]
    await session.call_error("get_goal", id_or_title="nosuch")

    await session.call_error("update_goals", updates=[{"goal": {"id": top, "status": "done"}}])
    changed = await session.call(
        "update_goals",
        creates=[{"id": "inbox1", "title": "Prioritize unstructured goals", "status": "inactive", "deadline": {"at": "2026-12-31"}}],
        updates=[
            {"goal": {"id": top, "status": "done"}},
            {"goal": {"id": sub, "status": "done"}},
            {"goal": {"id": later, "parent_id": "inbox1"}},
        ],
    )
    assert [g["status"] for g in changed["goals"]] == ["done", "done", "inactive", "inactive"]
    assert (await session.call("get_goals", statuses=["done"]))[0]["closed_at"]
    await session.call_error("delete_goal", goal_id="inbox1")
    renamed = await session.call("update_goal", goal={"id": later, "title": "Polish it", "description": "**Shine**", "description_format": "markdown"}, clear_fields=["parent_id"])
    assert renamed["path"] == []
    assert renamed["description_format"] == "markdown"
    # A description cleared takes its format with it.
    plain = await session.call("update_goal", goal={"id": later}, clear_fields=["description"])
    assert (plain["description"], plain["description_format"]) == (None, None)
    await session.call("delete_goal", goal_id="inbox1")
    await session.call("update_goals", deletes=[{"goal_id": later}])


async def test_missed_goals(session):
    top = (await session.call("create_goal", goal={"title": "Run a marathon", "deadline": {"at": "2026-12-01"}}))["created_id"]
    sub = (await session.call("create_goal", goal={"title": "Run a half first", "parent_id": top, "deadline": {"at": "2026-11-01"}, "required": True}))["created_id"]

    await session.call_error("update_goals", updates=[{"goal": {"id": top, "status": "missed"}}])
    # A required sub-goal missed doesn't keep its parent from being done.
    changed = await session.call("update_goals", updates=[{"goal": {"id": sub, "status": "missed"}}, {"goal": {"id": top, "status": "done"}}])
    assert [g["status"] for g in changed["goals"]] == ["missed", "done"]
    assert await session.call("get_goals") == []
    missed = await session.call("get_goals", statuses=["missed"])
    assert [g["title"] for g in missed] == ["Run a half first"] and missed[0]["closed_at"]


async def test_events_tagged_with_goals(session):
    goal = (await session.call("create_goal", goal={"title": "Learn Rust", "deadline": {"at": "2026-12-01"}}))["created_id"]
    made = await session.call(
        "create_event",
        events=[{"summary": "Read the book", "start": "2026-10-17T09:00:00+00:00", "end": "2026-10-17T10:00:00+00:00", "goals": [{"goal_id": goal, "minutes": 45}]}],
    )
    event = made["events"][0]
    assert event["goals"] == [{"goal_id": goal, "minutes": 45, "percent": None}]
    await session.call_error("update_event", updates=[{"event": {"id": event["id"], "goals": [{"goal_id": goal, "minutes": 61}]}}])
    await session.call_error("update_event", updates=[{"event": {"id": event["id"], "goals": [{"goal_id": "nosuch"}]}}])
    await session.call("update_event", updates=[{"event": {"id": event["id"], "goals": [{"goal_id": goal, "percent": 50}]}}])
    assert (await session.call("get_event", id=event["id"]))["goals"] == [{"goal_id": goal, "minutes": None, "percent": 50}]


async def test_a_goal_waits_for_another_to_close(session):
    first = (await session.call("create_goal", goal={"title": "Draft the plan", "deadline": {"at": "2099-11-01"}}))["created_id"]
    waiting = {"after": [{"goal_closed": first}], "delay": "next_day", "hold_while": [{"active_at_least": 5, "priority": {"at_or_above": "own"}}]}
    then = (await session.call("create_goal", goal={"title": "Send the plan", "status": "inactive", "deadline": {"after_start": {"days": 2}}, "activate_when": waiting}))["goal"]
    assert (then["status"], then["due"], then["expected_start"]) == ("inactive", None, "2099-11-02")
    await session.call_error("update_goal", goal={"id": first, "activate_when": {"after": [{"goal_closed": first}]}})
    after = (await session.call("create_goal", goal={"title": "Follow up", "deadline": {"after_goal": {"id": first, "days": 3}}}))["goal"]
    assert (after["deadline"], after["due"]) == ({"after_goal": {"id": first, "days": 3}}, "2099-11-04")
