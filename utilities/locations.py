"""Locations: the places the user's events happen ("Home", "Salsa
studio"), each with a hint for recognizing when an event or note refers
to it ("anything at the apartment; 'home', 'my place'").

**Where they live.** The **Locations** tab of the calendar's metadata
spreadsheet, one row per location, read by header name (utilities/
row_sheet.py), so it can be edited by hand. Names are unique. A location
has no status: one no longer wanted is deleted.
"""

from __future__ import annotations

import difflib
from collections.abc import Collection
from dataclasses import dataclass, replace

from calendar_clients.google_sheets import SheetsClient, TabRange
from utilities import calendar_metadata_sheet
from utilities.row_sheet import RowSheet, check_clear, new_id, updated

MAX_NAME_LENGTH = 100

CLEARABLE_FIELDS = frozenset({"hint"})


@dataclass(kw_only=True)
class Location:
    """One location -- see the module docstring."""

    id: str | None = None
    """Immutable short id, assigned on creation."""

    name: str | None = None
    """Unique among locations."""

    hint: str | None = None
    """How to tell that an event or note refers to it: other names for it,
    an address, what happens there."""


@dataclass(kw_only=True)
class CreatedLocation:
    location: Location
    created_id: str


class Locations:
    """A calendar's locations -- see the module docstring."""

    def __init__(self, sheet: RowSheet[Location]) -> None:
        self._sheet = sheet

    @staticmethod
    def ensure(sheets_client: SheetsClient, spreadsheet_id: str) -> "Locations":
        """The calendar's locations, adding the Locations tab the first
        time."""
        return Locations(
            RowSheet.ensure(
                sheets_client,
                spreadsheet_id,
                role=calendar_metadata_sheet.LOCATIONS_SHEET_ROLE,
                title=calendar_metadata_sheet.LOCATIONS_SHEET_TITLE,
                row_type=Location,
            )
        )

    @property
    def whole_tab(self) -> TabRange:
        return self._sheet.whole_tab

    def prefetch(self, ranges: list[TabRange]) -> None:
        self._sheet.prefetch(ranges)

    def all(self) -> list[Location]:
        """Every location, in sheet order."""
        return self._sheet.read()

    def get_location(self, id_or_name: str) -> Location:
        return find(self.all(), id_or_name)

    def create_location(self, location: Location) -> CreatedLocation:
        locations = self.all()
        new = replace(location, id=new_id({loc.id for loc in locations}))
        self._write(locations + [new])
        return CreatedLocation(location=new, created_id=new.id)

    def update_location(self, location: Location, clear_fields: Collection[str] = ()) -> Location:
        if not location.id:
            raise ValueError("update_location needs the location's id")
        check_clear(location, clear_fields, CLEARABLE_FIELDS)
        locations = self.all()
        index = next((i for i, loc in enumerate(locations) if loc.id == location.id), None)
        if index is None:
            find(locations, location.id)  # Raises, suggesting close matches.
        locations[index] = updated(locations[index], location, clear_fields)
        self._write(locations)
        return locations[index]

    def delete_location(self, location_id: str) -> Location:
        """Delete the location with `location_id`; returns it as it was."""
        locations = self.all()
        deleted = next((loc for loc in locations if loc.id == location_id), None) or find(locations, location_id)
        if deleted.id != location_id:
            raise ValueError(f"Delete a location by its id: {deleted.name!r} is {deleted.id}")
        self._sheet.write([loc for loc in locations if loc.id != location_id])
        return deleted

    def _write(self, locations: list[Location]) -> None:
        problems = location_problems(locations)
        if problems:
            raise ValueError("; ".join(problems))
        self._sheet.write(locations)


def find(locations: list[Location], id_or_name: str) -> Location:
    """The location with this id, or else this name (ignoring case);
    ValueError suggesting close matches if there's none."""
    by_name = {(loc.name or "").casefold(): loc for loc in locations}
    location = next((loc for loc in locations if loc.id == id_or_name), None) or by_name.get(id_or_name.casefold())
    if location is None:
        by_id = {loc.id: loc for loc in locations if loc.id}
        close = [by_id[i] for i in difflib.get_close_matches(id_or_name, list(by_id), n=3)] + [
            by_name[n] for n in difflib.get_close_matches(id_or_name.casefold(), list(by_name), n=3)
        ]
        hint = ", ".join(dict.fromkeys(f"{loc.id} ({loc.name})" for loc in close))
        raise ValueError(
            f"There's no location with the id or name {id_or_name!r}"
            + (f"; did you mean {hint}?" if hint else "")
            + " (get_locations lists them)"
        )
    return location


def location_problems(locations: list[Location]) -> list[str]:
    problems = []
    names: dict[str, Location] = {}
    for location in locations:
        label = f"location {location.id}" if location.id else f"location {location.name!r}"
        if not (isinstance(location.name, str) and location.name.strip()):
            problems.append(f"{label} needs a name")
            continue
        if len(location.name) > MAX_NAME_LENGTH:
            problems.append(f"{label}'s name is longer than {MAX_NAME_LENGTH} characters")
        key = location.name.strip().casefold()
        if key in names:
            problems.append(
                f"there's already a location named {names[key].name!r} ({names[key].id}); location names must "
                "be unique -- use that one, or pick another name"
            )
        names[key] = location
    return problems
