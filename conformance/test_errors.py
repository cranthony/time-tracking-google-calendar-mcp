"""Errors every tool gives for what it can't take: arguments its schema
refuses, and ids or states that aren't there -- each tool's error text is
part of what a client sees. Plus get_health, whose timings are masked in
the golden record (see harness._masked_timings)."""

from __future__ import annotations

import pytest


async def test_schema_refusals(session):
    """Arguments the input schema refuses, before the tool runs."""
    await session.call_error("list_events", min_time="yesterday", max_time="2026-10-08T00:00:00-04:00")
    await session.call_error("list_events", min_time="2026-10-07T00:00:00-04:00")
    await session.call_error("note", noted_time={"description": "no timestamp"})
    await session.call_error("get_actions", statuses=["sleeping"])
    await session.call_error("get_people", statuses=["gone"])
    await session.call_error("get_habits", statuses=["paused"])
    await session.call_error("get_traits", statuses=["maybe"])
    await session.call_error("create_event", events="not a list")
    await session.call_error("update_event", updates=[{"event": {"id": "x"}, "clear_fields": ["summary"]}])
    await session.call_error("set_compaction_schedule_hints", hints="07:30")
    await session.call_error("update_priority_color", priority="high", color="#000000")
    await session.call_error("create_location", location={"name": "Home", "unknown_field": 1})


async def test_missing_things(session):
    await session.call_error("abandon_compaction", proposal_id="nosuch")
    await session.call_error("add_proposal_note", proposal_id="nosuch", text="hello")
    await session.call_error("withdraw_proposal_note", feedback_id="nosuch")
    await session.call_error("split_recurrence", event_id="nosuch")
    await session.call_error("update_circle", circle={"id": "nosuch", "name": "x"})
    await session.call_error("confirm_proposal", proposal_id="nosuch", revision=1)
    await session.call_error("get_proposal")
    await session.call_error("compact_notes", recap={"overview": "x", "did": [], "unsure": []}, proposal_id="nosuch", revision=1)


@pytest.mark.server_env(TIME_TRACKER_SHEETS_REFRESH="every_call")
async def test_damaged_tabs(session, google):
    """Tabs damaged by hand while the server runs -- a header renamed --
    are put back before the next call reads them."""
    await session.call("create_action", action={"name": "play guitar", "status": "active"})
    await session.call("create_circle", circle={"name": "Family"})
    await session.call("set_compaction_schedule_hints", hints=[{"time": "07:30"}])
    await session.call("note", noted_time={"timestamp": "2026-10-07T09:00:00-04:00", "description": "x"})
    spreadsheet = next(iter(google.spreadsheets.values()))
    tabs = {t["properties"]["title"]: t for t in spreadsheet["sheets"]}
    for title, tab in tabs.items():
        if title in ("Health",):
            continue
        cells = tab["cells"]
        if (1, 0) in cells:
            cells[(1, 0)] = cells[(1, 0)] + " (edited)"
    for tool, args in [
        ("get_actions", {}), ("get_action_groups", {}), ("get_circles", {}), ("get_people", {}), ("get_locations", {}),
        ("get_habits", {}), ("get_traits", {}), ("get_notes", {}), ("get_compaction_schedule_hints", {}),
        ("get_compaction_status", {}), ("prepare_compaction", {}), ("get_priority_colors", {}),
        ("list_events", {"min_time": "2026-10-07T00:00:00-04:00", "max_time": "2026-10-08T00:00:00-04:00"}),
    ]:
        await session.attempt(tool, **args)


async def test_health(session):
    await session.call("get_locations")
    await session.call("get_locations")
    await session.call_error("get_location", id_or_name="nowhere")
    report = await session.call("get_health")
    by_tool = {t["tool"]: t for t in report["tools"]}
    assert by_tool["get_locations"]["count"] == 2
    assert by_tool["get_location"]["errors"] == 1
    summaries = await session.call("get_health", samples=False)
    assert all(t.get("calls") is None for t in summaries["tools"])
    one = await session.call("get_health", tool_name="get_locations")
    assert [t["tool"] for t in one["tools"]] == ["get_locations"]
    await session.call_error("get_health", tool_name="never_called")


def set_cell(google, title: str, row: int, column: str, text: str) -> None:
    """Edit one cell of a tab by hand: data row `row` (1 for the first),
    the column headed `column`."""
    spreadsheet = next(iter(google.spreadsheets.values()))
    tab = next(t for t in spreadsheet["sheets"] if t["properties"]["title"] == title)
    index = next(c for (r, c), v in tab["cells"].items() if r == 0 and v == column)
    tab["cells"][(row, index)] = text


@pytest.mark.server_env(TIME_TRACKER_SHEETS_REFRESH="every_call")
async def test_bad_cells(session, google):
    """Cells edited by hand while the server runs that can't be read as
    their column's kind -- a number that isn't one, JSON that isn't JSON --
    are put back before the next call reads them. (Read as they are, after
    a restart, they make every tool reading the tab say which cell: see
    the Rust tests.)"""
    group = (await session.call("create_action_group", group={"name": "Music", "priority": 1}))["created_id"]
    await session.call("create_action", action={"name": "play guitar", "status": "active", "group_id": group, "priority": 2})
    family = (await session.call("create_circle", circle={"name": "Family"}))["created_id"]
    await session.call("create_person", person={"name": "Sam Example", "circles": [family]})
    await session.call("create_location", location={"name": "Home"})
    set_cell(google, "Actions", 1, "priority", "high")
    for tool, args in [
        ("get_actions", {}), ("get_action", {"id_or_name": "play guitar"}), ("get_action_groups", {}),
        ("list_events", {"min_time": "2026-10-07T00:00:00-04:00", "max_time": "2026-10-08T00:00:00-04:00"}),
        ("create_action", {"action": {"name": "read"}}), ("get_locations", {}),
    ]:
        await session.attempt(tool, **args)
    set_cell(google, "Actions", 1, "priority", "2")
    set_cell(google, "People", 1, "circles", "family")
    for tool, args in [("get_people", {}), ("get_person", {"id_or_name": "Sam Example"}), ("get_circles", {}), ("get_actions", {})]:
        await session.attempt(tool, **args)
