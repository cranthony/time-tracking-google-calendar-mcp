"""People, circles and locations: creating, finding, updating and deleting
them, and what refers to them."""

from __future__ import annotations

import pytest


async def test_people(session):
    people = await session.call("get_people")
    assert people[0]["id"] == "self"
    alex = await session.call("create_person", person={"name": "Alex Example", "context": "met at salsa"}, facts={"thoughtful": {"what_matters": "music"}})
    assert alex["person"]["facts"] == {"thoughtful": {"what_matters": "music"}}
    alex_id = alex["created_id"]
    other = await session.call("create_person", person={"name": "Alex Example", "context": "from work"})
    await session.call_error("create_person", person={"name": "Alex Example", "context": "met at salsa"})
    await session.call_error("create_person", person={"name": "Alex Example"})
    ambiguous = await session.call_error("get_person", id_or_name="alex example")
    assert "salsa" in ambiguous and "work" in ambiguous
    assert (await session.call("get_person", id_or_name=alex_id))["context"] == "met at salsa"
    assert (await session.call("get_person", id_or_name="self"))["id"] == "self"
    await session.call_error("get_person", id_or_name="Alix Exampel")
    updated = await session.call("update_person", person={"id": alex_id, "name": "Alex E."}, facts={"thoughtful": {"what_matters": ""}})
    assert updated["name"] == "Alex E." and updated["facts"] == {}
    await session.call_error("update_person", person={"id": alex_id}, facts={"thoughtful": {"no_such_fact": "x"}})
    assert await session.call("get_fact_logs", person_id=alex_id) == []
    await session.call("update_person", person={"id": other["created_id"], "status": "archived"})
    assert "Alex E." in [p["name"] for p in await session.call("get_people")]
    await session.call("get_people", statuses=["active", "archived"])
    await session.call_error("update_person", person={"id": "self", "status": "archived"})
    await session.call_error("update_person", person={"id": "nobody", "name": "x"})
    await session.call("update_person", person={"id": "self"}, facts={"thoughtful": {"what_matters": "time well spent"}})


async def test_person_traits(session):
    traits = await session.call("get_traits")
    assert traits
    first = traits[0]["id"]
    person = await session.call("create_person", person={"name": "Sam Example", "traits": {"select": [first]}})
    assert person["person"]["traits"]["select"] == [first] if "person" in person else True
    await session.call_error("create_person", person={"name": "Pat Example", "traits": {"select": ["no-such-trait"]}})
    await session.call(
        "update_person",
        person={"id": person["created_id"], "traits": {"parts": {first: [{"kind": "count", "target": 1, "interval_days": 21, "noun": "visits"}]}}},
    )
    await session.call_error(
        "update_person",
        person={"id": person["created_id"], "traits": {"parts": {first: [{"kind": "count", "target": "lots"}]}}},
    )
    await session.call("update_person", person={"id": person["created_id"]}, clear_fields=["traits"])


async def test_circles(session):
    close = (await session.call("create_circle", circle={"name": "Close friends", "note": "the inner ring"}))["created_id"]
    family = (await session.call("create_circle", circle={"name": "Family"}))["created_id"]
    await session.call_error("create_circle", circle={"name": "close friends"})
    sam = (await session.call("create_person", person={"name": "Sam Example", "circles": [close, family]}))["created_id"]
    await session.call_error("create_person", person={"name": "Pat Example", "circles": ["nope"]})
    circles = await session.call("get_circles")
    assert [c["member_ids"] for c in circles] == [[sam], [sam]]
    assert (await session.call("get_person", id_or_name=sam))["circle_names"] == ["Close friends", "Family"]
    assert (await session.call("get_circle", id_or_name="family"))["id"] == family
    await session.call_error("get_circle", id_or_name="Famly")
    renamed = await session.call("update_circle", circle={"id": family, "name": "Relatives"}, clear_fields=[])
    assert renamed["name"] == "Relatives"
    await session.call("update_circle", circle={"id": close}, clear_fields=["note"])
    deleted = await session.call("delete_circle", circle_id=close)
    assert sam in str(deleted)
    assert (await session.call("get_person", id_or_name=sam))["circles"] == [family]
    await session.call_error("delete_circle", circle_id=close)


async def test_locations(session):
    home = (await session.call("create_location", location={"name": "Home", "hint": "the apartment; 'my place'"}))["created_id"]
    studio = (await session.call("create_location", location={"name": "Salsa studio"}))["created_id"]
    await session.call_error("create_location", location={"name": "home"})
    assert [loc["name"] for loc in await session.call("get_locations")] == ["Home", "Salsa studio"]
    assert (await session.call("get_location", id_or_name="HOME"))["id"] == home
    await session.call_error("get_location", id_or_name="Hom")
    updated = await session.call("update_location", location={"id": studio, "name": "Dance studio", "hint": "downtown"})
    assert updated["name"] == "Dance studio"
    cleared = await session.call("update_location", location={"id": studio}, clear_fields=["hint"])
    assert not cleared.get("hint")
    mapped = await session.call("update_location", location={"id": studio, "map_location": "12 Elm St"})
    assert mapped["map_location"] == "12 Elm St"
    unmapped = await session.call("update_location", location={"id": studio}, clear_fields=["map_location"])
    assert not unmapped.get("map_location")
    assert (await session.call("delete_location", location_id=home))["name"] == "Home"
    assert [loc["name"] for loc in await session.call("get_locations")] == ["Dance studio"]
    await session.call_error("delete_location", location_id=home)
    await session.call_error("update_location", location={"id": home, "name": "x"})


async def test_location_groups(session):
    city = (await session.call("create_location_group", group={"name": "New York"}))["created_id"]
    borough = (await session.call("create_location_group", group={"name": "Brooklyn", "group_id": city}))["created_id"]
    await session.call_error("create_location_group", group={"name": "brooklyn"})
    home = (await session.call("create_location", location={"name": "Home", "group_id": borough}))["created_id"]
    assert (await session.call("get_location", id_or_name=home))["path"] == "New York › Brooklyn › Home"
    assert [g["path"] for g in await session.call("get_location_groups")] == ["New York", "New York › Brooklyn"]
    assert (await session.call("get_location_group", id_or_name="brooklyn"))["id"] == borough
    await session.call_error("update_location_group", group={"id": city, "group_id": borough})
    noted = await session.call("update_location_group", group={"id": borough, "note": "across the bridge"})
    assert noted["note"] == "across the bridge"
    await session.call_error("update_location", location={"id": home, "group_id": "nowhere"})
    deleted = await session.call("delete_location_group", group_id=borough)
    assert [l["path"] for l in deleted["moved_locations"]] == ["New York › Home"]
    ungrouped = await session.call("update_location", location={"id": home}, clear_fields=["group_id"])
    assert not ungrouped.get("group_id")


async def test_travel_times(session):
    drive = (await session.call("create_action", action={"name": "Drive", "status": "active"}))["changed"][0]["id"]
    brooklyn = (await session.call("create_location_group", group={"name": "Brooklyn"}))["created_id"]
    home = (await session.call("create_location", location={"name": "Home", "group_id": brooklyn}))["created_id"]
    work = (await session.call("create_location", location={"name": "Work"}))["created_id"]
    created = await session.call(
        "create_travel_time",
        travel_time={"from_id": brooklyn, "to_id": work, "times": [{"mode_id": drive, "minutes": 25, "max_minutes": 40}], "note": "the bridge"},
    )
    both_ways = created["created_id"]
    await session.call_error("create_travel_time", travel_time={"from_id": work, "to_id": brooklyn})
    await session.call_error("create_travel_time", travel_time={"from_id": home, "to_id": work, "times": [{"mode_id": "fly", "minutes": 5}]})
    await session.call("create_travel_time", travel_time={"from_id": work, "to_id": home, "one_way": True, "times": [{"mode_id": drive, "minutes": 50}]})
    there = await session.call("estimate_travel_time", from_id=home, to_id=work, mode_id=drive)
    assert (there["from_id"], there["times"][0]["minutes"]) == (brooklyn, 25)
    back = await session.call("estimate_travel_time", from_id=work, to_id=home)
    assert back["travel_time"]["one_way"] is True and back["times"][0]["minutes"] == 50
    assert await session.call("estimate_travel_time", from_id=home, to_id=brooklyn) is None
    assert len(await session.call("get_travel_times", place_id=home)) == 1
    updated = await session.call("update_travel_time", travel_time={"id": both_ways}, clear_fields=["note"])
    assert not updated.get("note")
    assert (await session.call("delete_travel_time", travel_time_id=both_ways))["id"] == both_ways
    await session.call("delete_location", location_id=home)
    assert await session.call("get_travel_times") == []


@pytest.mark.server_env(TIME_TRACKER_SHEETS_REFRESH="every_call")
async def test_rows_edited_by_hand(session, google):
    """A tab edited in the spreadsheet by hand while the server runs is put
    back as the server keeps it, before the next call reads it."""
    await session.call("create_location", location={"name": "Home"})
    spreadsheet = next(iter(google.spreadsheets.values()))
    tab = next(t for t in spreadsheet["sheets"] if t["properties"]["title"] == "Locations")
    name_column = next(c for (r, c), v in tab["cells"].items() if r == 0 and v == "name")
    tab["cells"][(1, name_column)] = "Cabin"
    assert [loc["name"] for loc in await session.call("get_locations")] == ["Home"]
    assert tab["cells"][(1, name_column)] == "Home"
