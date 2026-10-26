"""Traits and habits: the seeded traits, creating and changing them with
each kind of part, and habits rated by them."""

from __future__ import annotations

import pytest

PARTS = [
    {"kind": "judgment", "rubric": "Was this activity or place new?", "ratings": {"0": "routine", "1": "a twist", "2": "new", "3": "adventurous"}, "facts": ["action", {"fact": "action_history", "lookback_days": 90}, "location"], "window_days": 30},
    {"kind": "continuity", "last_within_days": 14, "next_within_days": 14},
    {"kind": "count", "target": 1, "interval_days": 21, "zero_at_days": 42, "noun": "visits"},
    {"kind": "duration", "target_min": 120},
    {"kind": "follow_through", "penalty": 20, "recovery": 30, "look_back_days": 90},
]


async def test_seeded_traits(session):
    traits = await session.call("get_traits")
    assert traits and all(t["status"] in ("active", "off") for t in traits)
    every = await session.call("get_traits", statuses=["active", "off", "archived"])
    assert len(every) >= len(traits)


async def test_create_and_update_traits(session):
    created = await session.call("create_trait", trait={"name": "Daring", "definition": "Trying new things together.", "parts": PARTS})
    trait_id = created["id"]
    assert [p["kind"] for p in created["parts"]] == [p["kind"] for p in PARTS]
    await session.call_error("create_trait", trait={"name": "daring", "definition": "again", "parts": PARTS[:1]})
    await session.call_error("create_trait", trait={"name": "No parts", "definition": "d", "parts": []})
    await session.call_error("create_trait", trait={"name": "Bad part", "definition": "d", "parts": [{"kind": "count"}]})
    await session.call_error("create_trait", trait={"name": "Bad kind", "definition": "d", "parts": [{"kind": "vibes"}]})
    await session.call_error("create_trait", trait={"name": "x" * 51, "definition": "d", "parts": PARTS[:1]})
    renamed = await session.call("update_trait", trait={"id": trait_id, "name": "Bold"})
    assert renamed["id"] == trait_id and renamed["name"] == "Bold"
    await session.call("update_trait", trait={"id": trait_id, "parts": PARTS[1:3], "status": "off"})
    await session.call("update_trait", trait={"id": trait_id}, clear_fields=["definition"])
    await session.call("update_trait", trait={"id": trait_id, "status": "archived"})
    assert trait_id not in [t["id"] for t in await session.call("get_traits")]
    await session.call_error("update_trait", trait={"id": "nosuch", "name": "x"})


async def test_trait_part_with_action(session):
    action = (await session.call("create_action", action={"name": "go hiking", "status": "active"}))["created_id"]
    await session.call("create_trait", trait={"name": "Outdoorsy", "definition": "Time outside.", "parts": [{"kind": "count", "target": 2, "interval_days": 14, "noun": "hikes", "action": action}]})
    await session.call_error("create_trait", trait={"name": "Broken", "definition": "d", "parts": [{"kind": "count", "target": 2, "interval_days": 14, "noun": "x", "action": "nosuch"}]})


@pytest.mark.server_env(TIME_TRACKER_SHEETS_REFRESH="every_call")
async def test_traits_edited_by_hand(session, google):
    """A trait whose parts were broken by hand while the server runs is put
    back as the server keeps it, before the next call reads it."""
    await session.call("get_traits")
    spreadsheet = next(iter(google.spreadsheets.values()))
    tab = next(t for t in spreadsheet["sheets"] if t["properties"]["title"] == "Traits")
    parts_column = next(c for (r, c), v in tab["cells"].items() if r == 0 and v == "parts")
    tab["cells"][(1, parts_column)] = '[{"kind": "count", "target": "many"}]'
    traits = await session.call("get_traits")
    assert not traits[0].get("problems")


async def test_habits(session):
    group = (await session.call("create_action_group", group={"name": "Music"}))["created_id"]
    guitar = (await session.call("create_action", action={"name": "play guitar", "status": "active", "group_id": group}))["created_id"]
    trait = (await session.call("get_traits"))[0]["id"]
    habit = await session.call("create_habit", habit={"name": "Daily practice", "action_id": guitar, "note": "20 minutes a day"})
    habit_id = habit["created_id"]
    await session.call_error("create_habit", habit={"name": "daily practice", "action_id": guitar})
    await session.call_error("create_habit", habit={"name": "Nothing", "action_id": "nosuch"})
    await session.call("create_habit", habit={"name": "Music overall", "action_id": group, "traits": {"select": [trait]}})
    await session.call_error("create_habit", habit={"name": "Bad traits", "action_id": group, "traits": {"select": ["nosuch"]}})
    listed = await session.call("get_habits")
    assert [h["name"] for h in listed] == ["Daily practice", "Music overall"]
    assert listed[0]["action_path"] == "Music › play guitar"
    assert (await session.call("get_habit", id_or_name="daily PRACTICE"))["id"] == habit_id
    await session.call_error("get_habit", id_or_name="Daly practise")
    updated = await session.call("update_habit", habit={"id": habit_id, "name": "Guitar practice", "action_id": group}, clear_fields=["note"])
    assert updated["note"] is None and updated["action_path"] == "Music"
    await session.call("update_habit", habit={"id": habit_id, "traits": {"parts": {trait: PARTS[2:3]}}})
    await session.call("update_habit", habit={"id": habit_id, "status": "archived"})
    assert [h["name"] for h in await session.call("get_habits")] == ["Music overall"]
    assert len(await session.call("get_habits", statuses=["active", "archived", "deleted"])) == 2
    await session.call_error("update_habit", habit={"id": "nosuch", "name": "x"})



async def test_foundations(session):
    meal = (await session.call("create_action", action={"name": "eat a meal", "status": "active"}))["created_id"]
    sleep = (await session.call("create_action", action={"name": "sleep", "status": "active"}))["created_id"]
    spacing = {"kind": "spacing", "action": meal, "max_gap_hours": 6, "exceptions": [{"actions": [sleep], "reset_hours": 1}]}
    rest = (await session.call("create_habit", habit={"name": "Sleep well", "action_id": sleep}))["created_id"]
    eat = (await session.call("create_habit", habit={"name": "Eat well", "action_id": meal, "traits": {"parts": {"reliable": [spacing]}}}))["created_id"]
    await session.call_error("create_habit", habit={"name": "Bad gap", "action_id": meal, "traits": {"parts": {"reliable": [{**spacing, "max_gap_hours": 0}]}}})
    gym = (await session.call("create_habit", habit={"name": "Exercise", "action_id": meal, "foundations": [f"habit:{rest}", f"habit:{eat}"]}))["created_id"]
    family = (await session.call("create_person", person={"name": "Family Example"}))["created_id"]
    theirs = (await session.call("create_habit", habit={"person_id": family, "foundations": [f"habit:{gym}"]}))["created_id"]
    await session.call_error("create_habit", habit={"name": "Unknown", "action_id": meal, "foundations": [rest]})
    # A person is built on by their habits, not themselves.
    await session.call_error("update_habit", habit={"id": rest, "foundations": [family]})
    assert (await session.call("get_habit", id_or_name=theirs))["foundations"] == [f"habit:{gym}"]
    await session.call("update_habit", habit={"id": gym, "status": "deleted"})
    assert (await session.call("get_habit", id_or_name=theirs))["foundations"] == []


async def test_schedule_hints(session):
    empty = await session.call("get_compaction_schedule_hints")
    assert empty["hints"] == [] and empty["time_zone"] == "America/New_York"
    saved = await session.call("set_compaction_schedule_hints", hints=[{"time": "21:00", "label": "Evening"}, {"time": "07:30", "label": "Morning compaction"}, {"time": "12:00"}])
    assert [h["time"] for h in saved["hints"]] == ["07:30", "12:00", "21:00"]
    await session.call_error("set_compaction_schedule_hints", hints=[{"time": "07:30"}, {"time": "07:30"}])
    await session.call_error("set_compaction_schedule_hints", hints=[{"time": "7:30pm"}])
    await session.call_error("set_compaction_schedule_hints", hints=[{"time": "25:00"}])
    assert len((await session.call("get_compaction_schedule_hints"))["hints"]) == 3
    assert (await session.call("set_compaction_schedule_hints", hints=[]))["hints"] == []


async def test_part_ids_and_scoring(session):
    """Each part has an id its weight is kept under; scoring is checked and
    kept, at each level, and the defaults are set in a tab of their own."""
    judged = {**PARTS[0], "scoring": {"weight_by": "duration", "statistic": "percentile", "percentile": 25, "limit": 10, "limit_by": "best"}}
    created = await session.call("create_trait", trait={"name": "Daring", "definition": "d", "parts": [judged, PARTS[1]]})
    first, second = (p["id"] for p in created["parts"])
    scoring = {"weights": {first: 2}, "limit": 1, "limit_by": "worst", "cap_below": {"threshold": 40, "cap": 50}, "missing": "skip"}
    updated = await session.call("update_trait", trait={"id": created["id"], "scoring": scoring})
    assert updated["scoring"] == scoring
    # The second part let go, and a new one added: the first keeps its id.
    kept = await session.call("update_trait", trait={"id": created["id"], "parts": [created["parts"][0], PARTS[2]]})
    assert kept["parts"][0]["id"] == first and kept["parts"][1]["id"] not in (first, second)
    await session.call_error("update_trait", trait={"id": created["id"], "scoring": {"weights": {"nosuch": 1}}})
    await session.call_error("update_trait", trait={"id": created["id"], "parts": [{**PARTS[1], "weight": 2}]})
    await session.call_error("update_trait", trait={"id": created["id"], "parts": [{**PARTS[0], "scoring": {"limit": 2}}]})

    person = (await session.call("create_person", person={"name": "Sam", "traits": {"scoring": {"statistic": "percentile", "percentile": 0}, "part_scoring": {created["id"]: {"weights": {first: 3}}}}}))["created_id"]
    assert (await session.call("get_person", id_or_name=person))["traits"]["scoring"] == {"statistic": "percentile", "percentile": 0}
    circle = (await session.call("create_circle", circle={"name": "Friends", "scoring": {"weights": {person: 2}}}))["created_id"]
    await session.call("update_circle", circle={"id": circle}, clear_fields=["scoring"])
    await session.call_error("create_circle", circle={"name": "Bad", "scoring": {"statistic": "median"}})

    assert await session.call("get_scoring_defaults") == {"traits": None, "circles": None, "habit_layers": None}
    await session.call("set_scoring_defaults", defaults={"traits": {"statistic": "percentile", "percentile": 25}, "habit_layers": {"limit": 1, "limit_by": "worst"}})
    saved = await session.call("set_scoring_defaults", defaults={"circles": {"missing": 0}}, clear_fields=["habit_layers"])
    assert saved == {"traits": {"statistic": "percentile", "percentile": 25}, "circles": {"missing": 0}, "habit_layers": None}
    await session.call_error("set_scoring_defaults", defaults={"traits": {"limit": 2}})
