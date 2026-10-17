"""Goals: making them, nesting them, closing a goal with its sub-goals,
and tagging events with them."""

from __future__ import annotations


async def test_goals(session):
    top = (await session.call("create_goal", goal={"title": "Ship the app", "deadline": "2026-12-01", "expected_minutes": 3000}))["created_id"]
    await session.call_error("create_goal", goal={"title": "No deadline"})
    sub = (await session.call("create_goal", goal={"title": "Write the pane", "parent_id": top, "deadline": "2026-11-15", "required": True}))["created_id"]
    later = (await session.call("create_goal", goal={"title": "Polish", "parent_id": top, "status": "inactive"}))["created_id"]
    await session.call_error("create_goal", goal={"title": "Too late", "parent_id": top, "deadline": "2026-12-02"})
    await session.call_error("create_goal", goal={"title": "Too long", "parent_id": top, "deadline": "2026-11-01", "expected_minutes": 3001})

    listed = await session.call("get_goals")
    assert [g["title"] for g in listed] == ["Ship the app", "Write the pane", "Polish"]
    assert (await session.call("get_goal", id_or_title="write THE pane"))["path"] == ["Ship the app"]
    await session.call_error("get_goal", id_or_title="nosuch")

    await session.call_error("update_goals", updates=[{"goal": {"id": top, "status": "done"}}])
    changed = await session.call(
        "update_goals",
        creates=[{"id": "inbox1", "title": "Prioritize unstructured goals", "status": "inactive"}],
        updates=[
            {"goal": {"id": top, "status": "done"}},
            {"goal": {"id": sub, "status": "done"}},
            {"goal": {"id": later, "parent_id": "inbox1"}},
        ],
    )
    assert [g["status"] for g in changed["goals"]] == ["done", "done", "inactive", "inactive"]
    assert (await session.call("get_goals", statuses=["done"]))[0]["closed_at"]
    await session.call("update_goals", deletes=[{"goal_id": "inbox1"}, {"goal_id": later}])


async def test_events_tagged_with_goals(session):
    goal = (await session.call("create_goal", goal={"title": "Learn Rust", "deadline": "2026-12-01"}))["created_id"]
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
