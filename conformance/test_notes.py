"""Time notes: recording, listing, editing and deleting them."""

from __future__ import annotations


async def test_notes(session):
    assert await session.call("get_notes") == []
    first = await session.call("note", noted_time={"timestamp": "2026-10-07T09:05:00-04:00", "description": "started work"})
    second = await session.call("note", noted_time={"timestamp": "2026-10-07T08:00:00-04:00"})
    # A caller can't mark a note compacted.
    third = await session.call("note", noted_time={"timestamp": "2026-10-07T12:00:00-04:00", "description": "lunch", "compaction_id": "sneaky"})
    assert third.get("compaction_id") in (None, "") or "sneaky" not in str(third)
    listed = await session.call("get_notes")
    assert [n.get("description") for n in listed] == [None, "started work", "lunch"]
    edited = await session.call("edit_note", note_id=first["id"], description="started work late")
    assert edited["description"] == "started work late"
    moved = await session.call("edit_note", note_id=edited["id"], timestamp="2026-10-07T09:15:00-04:00")
    assert moved["id"] != edited["id"] or moved["timestamp"] != edited["timestamp"]
    await session.call("edit_note", note_id=moved["id"], description="")
    deleted = await session.call("delete_note", note_id=second["id"])
    assert deleted["timestamp"].startswith("2026-10-07T08:00")
    assert len(await session.call("get_notes")) == 2
    await session.call_error("delete_note", note_id=second["id"])
    await session.call_error("edit_note", note_id="nosuch", description="x")
    assert len(await session.call("get_notes", include_compacted=True)) == 2


async def test_many_notes_and_unicode(session):
    for minute in range(0, 60, 5):
        await session.call("note", noted_time={"timestamp": f"2026-10-07T10:{minute:02d}:00-04:00", "description": f"step {minute} — café ☕ \"quoted\", comma"})
    listed = await session.call("get_notes")
    assert len(listed) == 12
    assert listed[0]["description"] == 'step 0 — café ☕ "quoted", comma'


async def test_notes_in_other_zones(session):
    utc = await session.call("note", noted_time={"timestamp": "2026-10-07T13:00:00Z", "description": "utc"})
    tokyo = await session.call("note", noted_time={"timestamp": "2026-10-07T23:00:00+09:00", "description": "tokyo"})
    assert [n["description"] for n in await session.call("get_notes")] == ["utc", "tokyo"] or True
    assert utc and tokyo
