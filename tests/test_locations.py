import pytest

from tests.fake_sheets import FakeSheets
from utilities import calendar_metadata_sheet
from utilities.locations import Location, Locations


def _locations(sheets=None) -> tuple[Locations, FakeSheets]:
    sheets = sheets or FakeSheets()
    return Locations.ensure(sheets, "spreadsheet"), sheets


def _create(locations: Locations, **fields) -> str:
    return locations.create_location(Location(**fields)).created_id


def test_creates_and_finds_a_location_by_id_or_name():
    locations, sheets = _locations()

    created = locations.create_location(Location(id="mine", name="Home", hint="the apartment"))

    assert created.created_id != "mine"
    assert locations.get_location(created.created_id).name == "Home"
    assert locations.get_location("HOME").hint == "the apartment"
    assert sheets.tags[("sheet-role", calendar_metadata_sheet.LOCATIONS_SHEET_ROLE)] is not None


def test_names_are_unique():
    locations, _ = _locations()
    _create(locations, name="Home")

    with pytest.raises(ValueError, match=r"already a location named 'Home' \(.*\); location names must be unique"):
        _create(locations, name="home")


def test_needs_a_name():
    locations, _ = _locations()

    with pytest.raises(ValueError, match="needs a name"):
        locations.create_location(Location(hint="somewhere"))


def test_updates_and_clears():
    locations, _ = _locations()
    home = _create(locations, name="Home", hint="the apartment")

    assert locations.update_location(Location(id=home, name="Flat")).hint == "the apartment"
    assert locations.update_location(Location(id=home), clear_fields=["hint"]).hint is None


@pytest.mark.parametrize(
    "location, clear, message",
    [
        (Location(), [], "needs the location's id"),
        (Location(id="nope", name="X"), [], "no location with the id or name 'nope'"),
        (Location(id="x"), ["name"], r"Can't clear \['name'\]"),
    ],
)
def test_update_refuses_what_it_cant_do(location, clear, message):
    locations, _ = _locations()

    with pytest.raises(ValueError, match=message):
        locations.update_location(location, clear)


def test_deletes_by_id():
    locations, _ = _locations()
    home = _create(locations, name="Home")
    studio = _create(locations, name="Studio")

    assert locations.delete_location(home).name == "Home"
    assert [loc.id for loc in locations.all()] == [studio]
    with pytest.raises(ValueError, match=f"by its id: 'Studio' is {studio}"):
        locations.delete_location("studio")


def test_suggests_close_matches():
    locations, _ = _locations()
    studio = _create(locations, name="Salsa studio")

    with pytest.raises(ValueError, match=rf"did you mean {studio} \(Salsa studio\)"):
        locations.get_location("salsa studo")
