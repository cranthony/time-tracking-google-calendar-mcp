"""The actions, people and locations a compaction adds as it goes: an
event that was something no action covers yet, someone new, a new place.
They're given with the plan rather than by separate tools, so the user
confirms them with everything else.

Each has a `ref` ("new:ukulele") that the plan's decisions name it by,
wherever an id would go: in an event's `action_ids`, or its facts'
`location_id`, `with_ids`, `for_ids` or `notes`. A dry run checks them as
their stores would (unique names, known groups and circles) and shows
them by name in the timeline; nothing is created until the plan is
applied. Then each is created -- or, if one by that name is already
there (the commit is being resumed, say), reused -- and every ref in the
plan's changes is replaced by the real id as it's written.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any

from calendar_clients.google_calendar import Event
from utilities.actions import Action, Actions, action_problems
from utilities.facts import Facts
from utilities.locations import Location, Locations, location_problems
from utilities.people import People, Person, person_problems

REF_PREFIX = "new:"


@dataclass(kw_only=True)
class NewAction(Action):
    """An action to add: `ref` and `name`, and any of an action's other
    fields (its status is active unless given: the user approves it with
    the plan). id and label_id are assigned when it's created."""

    ref: str


@dataclass(kw_only=True)
class NewPerson(Person):
    """A person to add: `ref` and `name`, and any of a person's other
    fields. id is assigned when they're created."""

    ref: str


@dataclass(kw_only=True)
class NewLocation(Location):
    """A location to add: `ref`, `name` and a `hint`. id is assigned when
    it's created."""

    ref: str


@dataclass(kw_only=True)
class Additions:
    actions: list[NewAction]
    people: list[NewPerson]
    locations: list[NewLocation]

    @property
    def refs(self) -> dict[str, str]:
        """Every ref, with the name it stands for."""
        return {item.ref: item.name or item.ref for item in [*self.actions, *self.people, *self.locations]}

    def is_empty(self) -> bool:
        return not (self.actions or self.people or self.locations)

    def to_json_dict(self) -> dict[str, list[dict]]:
        def plain(item: Any) -> dict:
            return {f.name: getattr(item, f.name) for f in fields(item) if getattr(item, f.name) is not None}

        return {
            kind: [plain(item) for item in items]
            for kind, items in (("actions", self.actions), ("people", self.people), ("locations", self.locations))
            if items
        }

    @classmethod
    def from_json_dict(cls, data: dict[str, list[dict]] | None) -> "Additions":
        data = data or {}
        return cls(
            actions=[NewAction(**item) for item in data.get("actions", [])],
            people=[NewPerson(**item) for item in data.get("people", [])],
            locations=[NewLocation(**item) for item in data.get("locations", [])],
        )


def check_additions(
    additions: Additions, actions: Actions | None, people: People | None, locations: Locations | None
) -> list[str]:
    """Everything wrong with `additions`, as their stores would refuse it
    once they're added to what's there, or as refs."""
    problems = []
    refs = [item.ref for item in [*additions.actions, *additions.people, *additions.locations]]
    for ref in refs:
        if not (isinstance(ref, str) and ref.startswith(REF_PREFIX) and len(ref) > len(REF_PREFIX)):
            problems.append(f"{ref!r} isn't a ref: refs start with {REF_PREFIX!r}, e.g. \"new:ukulele\"")
    for ref in sorted({r for r in refs if refs.count(r) > 1}):
        problems.append(f"the ref {ref!r} is used more than once")
    if additions.actions:
        if actions is None:
            problems.append("this calendar has no actions to add to")
        else:
            new = [
                replace(Action(**_fields(a, Action)), id=a.ref, label_id=a.ref, status=a.status or "active")
                for a in additions.actions
            ]
            problems += [f"new actions: {p}" for p in action_problems(actions.all() + new, actions.groups().groups)]
    if additions.people:
        if people is None:
            problems.append("this calendar has no people to add to")
        else:
            new = [replace(Person(**_fields(p, Person)), id=p.ref, status=p.status or "active") for p in additions.people]
            problems += [f"new people: {p}" for p in person_problems(people.all() + new, people.circles())]
    if additions.locations:
        if locations is None:
            problems.append("this calendar has no locations to add to")
        else:
            new = [replace(Location(**_fields(loc, Location)), id=loc.ref) for loc in additions.locations]
            problems += [f"new locations: {p}" for p in location_problems(locations.all() + new)]
    return problems


def create_additions(
    additions: Additions, actions: Actions | None, people: People | None, locations: Locations | None
) -> dict[str, str]:
    """Create `additions` -- reusing any already there by the same name
    (and context, for a person), so a resumed commit doesn't make them
    twice -- and return each ref's id."""
    ids: dict[str, str] = {}
    if additions.actions and actions is not None:
        for new in additions.actions:
            found = next((a for a in actions.all() if (a.name or "").casefold() == new.name.strip().casefold()), None)
            if found is not None:
                ids[new.ref] = found.id
            else:
                action = replace(Action(**_fields(new, Action)), status=new.status or "active")
                ids[new.ref] = actions.create_action(action).created_id
    if additions.people and people is not None:
        for new in additions.people:
            key = (new.name.strip().casefold(), (new.context or "").strip().casefold())
            found = next(
                (p for p in people.all() if ((p.name or "").casefold(), (p.context or "").casefold()) == key), None
            )
            ids[new.ref] = found.id if found is not None else people.create_person(Person(**_fields(new, Person))).created_id
    if additions.locations and locations is not None:
        for new in additions.locations:
            found = next(
                (loc for loc in locations.all() if (loc.name or "").casefold() == new.name.strip().casefold()), None
            )
            ids[new.ref] = (
                found.id if found is not None else locations.create_location(Location(**_fields(new, Location))).created_id
            )
    return ids


def resolve(event: Event, ids: dict[str, str]) -> Event:
    """`event` with every ref in its actions and facts replaced by `ids`'."""
    if not ids:
        return event
    changes: dict[str, Any] = {}
    if event.action_ids:
        changes["action_ids"] = [ids.get(i, i) for i in event.action_ids]
    if event.facts is not None:
        facts = event.facts
        changes["facts"] = Facts(
            location_id=ids.get(facts.location_id, facts.location_id) if facts.location_id else facts.location_id,
            with_ids=[ids.get(i, i) for i in facts.with_ids] if facts.with_ids is not None else None,
            for_ids=[ids.get(i, i) for i in facts.for_ids] if facts.for_ids is not None else None,
            notes={ids.get(k, k): v for k, v in facts.notes.items()} if facts.notes is not None else None,
        )
    return replace(event, **changes)


def _fields(item: Any, base: type) -> dict[str, Any]:
    """`item`'s fields that `base` has too, but its id."""
    names = {f.name for f in fields(base)} - {"id", "label_id"}
    return {name: getattr(item, name) for name in names}
