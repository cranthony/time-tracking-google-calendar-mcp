"""People: who the user wants to spend their time with, and circles: the
groups those people belong to ("Close friends", "Family"), a person
belonging to any number of them.

**Where they live.** The **People** and **Circles** tabs of the
calendar's metadata spreadsheet, one row per person or circle, read by
header name (utilities/row_sheet.py), so either can be edited by hand.

**Self.** The user is always one of the people, with the id `self`,
whether or not the People tab has a row for them: updating `self` adds
the row the first time, for their own circles, traits or what matters
to them.
Self is always active.

A person is identified by their name and context together ("Sam", "met
at salsa"), which must be unique; a circle by its name. A person is
archived or deleted rather than removed, so events naming them still
make sense; a circle is simply deleted, and its people leave it.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Collection
from dataclasses import dataclass, fields, replace
from typing import Any, Literal

from calendar_clients.google_sheets import SheetsClient, TabRange
from utilities import calendar_metadata_sheet
from utilities.traits import person_traits_problems
from utilities.row_sheet import RowSheet, check_clear, new_id, updated

SELF_ID = "self"
"""The user's own id among the people. (Generated ids are 6 characters,
so none can be this.)"""

SELF_NAME = "Me"
"""Self's name until one is given."""

PersonStatus = Literal["active", "archived", "deleted"]
PERSON_STATUSES: tuple[str, ...] = ("active", "archived", "deleted")
"""Where a person stands:

- `active`: someone the user spends time with.
- `archived`: out of touch, and kept out of the way.
- `deleted`: shouldn't have existed. Kept only so events naming them
  still make sense."""

DEFAULT_STATUSES: tuple[str, ...] = ("active",)

MAX_NAME_LENGTH = 100


PERSON_CLEARABLE_FIELDS = frozenset({"context", "circles", "what_matters", "traits"})
CIRCLE_CLEARABLE_FIELDS = frozenset({"note"})


@dataclass(kw_only=True)
class Person:
    """One person -- see the module docstring."""

    id: str | None = None
    """Immutable short id, assigned on creation; `self` for the user."""

    name: str | None = None

    context: str | None = None
    """What tells them apart from others of the same name, e.g. "met at
    salsa"."""

    status: PersonStatus | None = None
    """See PERSON_STATUSES."""

    circles: list[str] | None = None
    """The ids of the circles they belong to."""

    what_matters: str | None = None
    """What's important to them: facts, upcoming moments, preferences."""

    traits: dict[str, Any] | None = None
    """Which traits apply to them, and parts replacing a trait's for them
    alone: {"select": "all" or [trait ids], "parts": {trait id: [parts]}},
    both optional (see utilities/traits.py's `person_traits_problems`).
    Without it, every active trait applies, with the Traits tab's parts."""


@dataclass(kw_only=True)
class Circle:
    """One circle -- see the module docstring."""

    id: str | None = None
    """Immutable short id, assigned on creation."""

    name: str | None = None
    """Unique among circles (a person may share it)."""

    note: str | None = None


@dataclass(kw_only=True)
class ListedPerson(Person):
    """A person as the people tools return them: plus their circles'
    names."""

    circle_names: list[str] | None = None
    """Their circles' names, in the order of `circles`."""


@dataclass(kw_only=True)
class ListedCircle(Circle):
    """A circle as the circle tools return it: plus who's in it."""

    member_ids: list[str] | None = None
    """The ids of the people in it, whatever their status."""


@dataclass(kw_only=True)
class CreatedPerson:
    person: ListedPerson
    created_id: str


@dataclass(kw_only=True)
class CreatedCircle:
    circle: ListedCircle
    created_id: str


@dataclass(kw_only=True)
class DeletedCircle:
    deleted: Circle
    """The circle as it was."""

    left: list[str]
    """The ids of the people who were in it."""


def self_person() -> Person:
    """Self, as they are before the People tab has a row for them."""
    return Person(id=SELF_ID, name=SELF_NAME, status="active")


class People:
    """A calendar's people and circles -- see the module docstring."""

    def __init__(
        self,
        sheet: RowSheet[Person],
        circle_sheet: RowSheet[Circle],
        trait_ids: Callable[[], Collection[str]] | None = None,
    ) -> None:
        """`trait_ids` gives the ids in the Traits tab (utilities/
        traits.py), which a person's `traits` must name; without it,
        they aren't checked."""
        self._sheet = sheet
        self._circle_sheet = circle_sheet
        self._trait_ids = trait_ids

    @staticmethod
    def ensure(
        sheets_client: SheetsClient, spreadsheet_id: str, trait_ids: Callable[[], Collection[str]] | None = None
    ) -> "People":
        """The calendar's people, adding the People and Circles tabs the
        first time."""
        return People(
            RowSheet.ensure(
                sheets_client,
                spreadsheet_id,
                role=calendar_metadata_sheet.PEOPLE_SHEET_ROLE,
                title=calendar_metadata_sheet.PEOPLE_SHEET_TITLE,
                row_type=Person,
            ),
            RowSheet.ensure(
                sheets_client,
                spreadsheet_id,
                role=calendar_metadata_sheet.CIRCLES_SHEET_ROLE,
                title=calendar_metadata_sheet.CIRCLES_SHEET_TITLE,
                row_type=Circle,
            ),
            trait_ids,
        )

    @property
    def whole_tabs(self) -> list[TabRange]:
        """Both tabs, for `SheetsClient.prefetch`."""
        return [self._sheet.whole_tab, self._circle_sheet.whole_tab]

    def prefetch(self, ranges: list[TabRange]) -> None:
        self._sheet.prefetch(ranges)

    # -- reading ------------------------------------------------------------

    def all(self) -> list[Person]:
        """Every person, self first (supplied if the tab has no row for
        them), then in sheet order."""
        people = self._sheet.read()
        mine = next((p for p in people if p.id == SELF_ID), None)
        return [_as_self(mine)] + [p for p in people if p.id != SELF_ID]

    def circles(self) -> list[Circle]:
        return self._circle_sheet.read()

    def get_people(self, statuses: Collection[str] | None = None) -> list[ListedPerson]:
        """The people with any of `statuses` (by default active ones),
        self first."""
        statuses = _check_statuses(statuses)
        circles = self.circles()
        return [_listed(p, circles) for p in self.all() if p.status in statuses]

    def get_person(self, id_or_name: str) -> ListedPerson:
        """The person with this id, or else this name (ignoring case)."""
        return _listed(find_person(self.all(), id_or_name), self.circles())

    def get_circles(self) -> list[ListedCircle]:
        people = self.all()
        return [_listed_circle(c, people) for c in self.circles()]

    def get_circle(self, id_or_name: str) -> ListedCircle:
        return _listed_circle(find_circle(self.circles(), id_or_name), self.all())

    # -- writing people -----------------------------------------------------

    def create_person(self, person: Person) -> CreatedPerson:
        """Add `person` (active unless given a status), with a new id."""
        people = self._sheet.read()
        new = replace(person, id=new_id({p.id for p in people}), status=person.status or "active")
        circles = self.circles()
        self._write_people(people + [new], circles)
        return CreatedPerson(person=_listed(new, circles), created_id=new.id)

    def update_person(self, person: Person, clear_fields: Collection[str] = ()) -> ListedPerson:
        """Set whichever of `person`'s fields aren't `None` (but its id) on
        the person with `person.id`, and blank those in `clear_fields`.
        Updating self adds their row the first time."""
        if not person.id:
            raise ValueError("update_person needs the person's id")
        check_clear(person, clear_fields, PERSON_CLEARABLE_FIELDS)
        people = self._sheet.read()
        index = next((i for i, p in enumerate(people) if p.id == person.id), None)
        if index is None and person.id == SELF_ID:
            people, index = people + [self_person()], len(people)
        if index is None:
            find_person(self.all(), person.id)  # Raises, suggesting close matches.
        changed = updated(people[index], person, clear_fields)
        people[index] = changed
        circles = self.circles()
        self._write_people(people, circles)
        return _listed(_as_self(changed) if changed.id == SELF_ID else changed, circles)

    def _write_people(self, people: list[Person], circles: list[Circle]) -> None:
        # Self is checked too (a name of theirs can't be taken), row or not.
        checked = people if any(p.id == SELF_ID for p in people) else [self_person(), *people]
        trait_ids = self._trait_ids() if self._trait_ids and any(p.traits is not None for p in people) else None
        problems = person_problems(checked, circles, trait_ids)
        if problems:
            raise ValueError("; ".join(problems))
        self._sheet.write(people)

    # -- writing circles ----------------------------------------------------

    def create_circle(self, circle: Circle) -> CreatedCircle:
        circles = self.circles()
        new = replace(circle, id=new_id({c.id for c in circles}))
        self._write_circles(circles + [new])
        return CreatedCircle(circle=_listed_circle(new, []), created_id=new.id)

    def update_circle(self, circle: Circle, clear_fields: Collection[str] = ()) -> ListedCircle:
        if not circle.id:
            raise ValueError("update_circle needs the circle's id")
        check_clear(circle, clear_fields, CIRCLE_CLEARABLE_FIELDS)
        circles = self.circles()
        index = next((i for i, c in enumerate(circles) if c.id == circle.id), None)
        if index is None:
            find_circle(circles, circle.id)  # Raises, suggesting close matches.
        circles[index] = updated(circles[index], circle, clear_fields)
        self._write_circles(circles)
        return _listed_circle(circles[index], self.all())

    def delete_circle(self, circle_id: str) -> DeletedCircle:
        """Delete the circle with `circle_id`; its people leave it."""
        circles = self.circles()
        deleted = next((c for c in circles if c.id == circle_id), None) or find_circle(circles, circle_id)
        if deleted.id != circle_id:
            raise ValueError(f"Delete a circle by its id: {deleted.name!r} is {deleted.id}")
        people = self._sheet.read()
        left = [p.id for p in people if circle_id in (p.circles or [])]
        if left:
            people = [
                replace(p, circles=[c for c in p.circles if c != circle_id] or None) if p.id in left else p
                for p in people
            ]
            self._sheet.write(people)
        self._circle_sheet.write([c for c in circles if c.id != circle_id])
        return DeletedCircle(deleted=deleted, left=left)

    def _write_circles(self, circles: list[Circle]) -> None:
        problems = circle_problems(circles)
        if problems:
            raise ValueError("; ".join(problems))
        self._circle_sheet.write(circles)


def _as_self(row: Person | None) -> Person:
    """Self, from their row (if any), with whatever it leaves out filled in."""
    base = self_person()
    if row is None:
        return base
    return replace(row, name=row.name or base.name, status="active")


def find_person(people: list[Person], id_or_name: str) -> Person:
    """The person with this id, or else this name (ignoring case);
    ValueError if there's none, or more than one of that name."""
    person = next((p for p in people if p.id == id_or_name), None)
    if person is not None:
        return person
    named = [p for p in people if (p.name or "").casefold() == id_or_name.casefold()]
    if len(named) == 1:
        return named[0]
    if named:
        which = ", ".join(f"{p.id} ({p.context or 'no context'})" for p in named)
        raise ValueError(f"There's more than one person named {id_or_name!r}: {which}; give the id")
    raise ValueError(_no_match("person", "get_people", people, id_or_name))


def find_circle(circles: list[Circle], id_or_name: str) -> Circle:
    by_name = {(c.name or "").casefold(): c for c in circles}
    circle = next((c for c in circles if c.id == id_or_name), None) or by_name.get(id_or_name.casefold())
    if circle is None:
        raise ValueError(_no_match("circle", "get_circles", circles, id_or_name))
    return circle


def _no_match(kind: str, lister: str, items: list[Any], id_or_name: str) -> str:
    by_id = {i.id: i for i in items if i.id}
    by_name: dict[str, list[Any]] = {}
    for item in items:
        by_name.setdefault((item.name or "").casefold(), []).append(item)
    close = [by_id[i] for i in difflib.get_close_matches(id_or_name, list(by_id), n=3)] + [
        item for n in difflib.get_close_matches(id_or_name.casefold(), list(by_name), n=3) for item in by_name[n]
    ]
    hint = ", ".join(dict.fromkeys(f"{i.id} ({i.name})" for i in close))
    return f"There's no {kind} with the id or name {id_or_name!r}" + (f"; did you mean {hint}?" if hint else "") + (
        f" ({lister} lists them)"
    )


def person_problems(
    people: list[Person], circles: list[Circle], trait_ids: Collection[str] | None = None
) -> list[str]:
    """Everything wrong with the People tab's rows as a whole, as phrases.
    Their `traits` are checked against `trait_ids`, if given."""
    problems = []
    ids = [p.id for p in people]
    for person_id in sorted({i for i in ids if i and ids.count(i) > 1}):
        problems.append(f"person id {person_id!r} is used more than once")
    circle_ids = {c.id for c in circles}
    seen: dict[tuple[str, str], Person] = {}
    for person in people:
        label = f"person {person.id}" if person.id else f"person {person.name!r}"
        if not person.id:
            problems.append(f"a person named {person.name!r} has no id")
        name = person.name if person.id != SELF_ID else (person.name or SELF_NAME)
        if not (isinstance(name, str) and name.strip()):
            problems.append(f"{label} needs a name")
        elif len(name) > MAX_NAME_LENGTH:
            problems.append(f"{label}'s name is longer than {MAX_NAME_LENGTH} characters")
        else:
            key = (name.strip().casefold(), (person.context or "").strip().casefold())
            if key in seen:
                other = seen[key]
                where = f" with the context {other.context!r}" if other.context else " with no context"
                problems.append(
                    f"there's already a person named {other.name!r}{where} ({other.id}); a person's name and "
                    "context together must be unique -- give a context that tells them apart"
                )
            seen[key] = person
        if person.id == SELF_ID:
            if person.status not in (None, "active"):
                problems.append("self is always active")
        elif person.status not in PERSON_STATUSES:
            problems.append(f"{label}'s status must be one of {', '.join(PERSON_STATUSES)}")
        if person.circles is not None:
            if not (isinstance(person.circles, list) and all(isinstance(c, str) for c in person.circles)):
                problems.append(f"{label}'s circles must be a list of circle ids")
            else:
                unknown = [c for c in person.circles if c not in circle_ids]
                if unknown:
                    problems.append(f"{label}'s circles {unknown} aren't circles (get_circles lists them)")
        if person.traits is not None:
            problems += [f"{label}'s traits {p}" for p in person_traits_problems(person.traits, trait_ids)]
    return problems


def circle_problems(circles: list[Circle]) -> list[str]:
    problems = []
    names: dict[str, Circle] = {}
    for circle in circles:
        label = f"circle {circle.id}" if circle.id else f"circle {circle.name!r}"
        if not (isinstance(circle.name, str) and circle.name.strip()):
            problems.append(f"{label} needs a name")
            continue
        key = circle.name.strip().casefold()
        if key in names:
            problems.append(
                f"there's already a circle named {names[key].name!r} ({names[key].id}); circle names must be "
                "unique (a person may share one)"
            )
        names[key] = circle
    return problems


def _listed(person: Person, circles: list[Circle]) -> ListedPerson:
    by_id = {c.id: c for c in circles}
    return ListedPerson(
        **{f.name: getattr(person, f.name) for f in fields(Person)},
        circle_names=[by_id[c].name for c in person.circles or [] if c in by_id] or None,
    )


def _listed_circle(circle: Circle, people: list[Person]) -> ListedCircle:
    return ListedCircle(
        **{f.name: getattr(circle, f.name) for f in fields(Circle)},
        member_ids=[p.id for p in people if circle.id in (p.circles or [])],
    )


def _check_statuses(statuses: Collection[str] | None) -> tuple[str, ...]:
    if statuses is None:
        return DEFAULT_STATUSES
    unknown = sorted(set(statuses) - set(PERSON_STATUSES))
    if unknown:
        raise ValueError(f"Unknown person status(es) {unknown}; statuses are {', '.join(PERSON_STATUSES)}")
    return tuple(statuses)
