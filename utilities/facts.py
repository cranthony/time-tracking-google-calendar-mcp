"""An event's facts: what compaction established about it, for traits'
judgments to read later -- where it was, who it was with and who it was
for, and a subjective note on each person who was there, the user
included. What the user was doing is the event's actions (its
`action_ids`), kept apart since they also set its label.

| field        | in JSON    | what                                                 |
| ------------ | ---------- | ---------------------------------------------------- |
| location_id  | `location` | where it was (utilities/locations.py)                |
| with_ids     | `with`     | the people who were there with the user              |
| for_ids      | `for`      | the people it was done for, who weren't there        |
|              |            | (preparation: a gift, a plan, a dish to bring)       |
| notes        | `notes`    | {person id: a subjective note on them at it} --      |
|              |            | "self" for the user -- whatever might help judge     |
|              |            | how it went for them later                           |

People are utilities/people.py's ids; the user is always "self" and is
never in `with_ids` (they're at every event). Facts are kept on the event
itself as JSON in private extended properties -- split across several if
it runs past Calendar's 1024 characters a value (see calendar_clients/
google_calendar.py) -- so they travel with it and outlive the notes they
came from.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields

SELF_ID = "self"
"""The user's own id among the people (utilities/people.py's, defined
here so the calendar client can read facts without importing the
people store)."""

MAX_FACTS_CHARS = 8 * 1024
"""The most JSON an event's facts may take: eight extended properties."""

MAX_NOTE_CHARS = 1000
"""A person's note is a few sentences at most."""

_JSON_KEYS = {"location_id": "location", "with_ids": "with", "for_ids": "for"}
"""Fields stored under a shorter key (the rest under their own name)."""

_SPACES = re.compile(r"\s+")


@dataclass(kw_only=True)
class Facts:
    """What compaction established about an event -- see the module
    docstring. Every field is optional."""

    location_id: str | None = None
    """Where it was: a location's id."""

    with_ids: list[str] | None = None
    """The people who were there with the user: person ids, never "self"."""

    for_ids: list[str] | None = None
    """The people it was done for, while they weren't there."""

    notes: dict[str, str] | None = None
    """A subjective note on each person who was there ("self" for the
    user), for judging how it went for them later."""

    def is_empty(self) -> bool:
        return all(getattr(self, f.name) in (None, [], {}) for f in fields(self))

    def normalized(self) -> "Facts":
        """These facts with ids trimmed and deduplicated, and blank notes
        dropped."""

        def ids(value: list[str] | None) -> list[str] | None:
            if not isinstance(value, list):
                return value
            return list(dict.fromkeys(i.strip() for i in value if isinstance(i, str) and i.strip()))

        notes = self.notes
        if isinstance(notes, dict):
            notes = {
                k.strip(): _SPACES.sub(" ", v).strip()
                for k, v in notes.items()
                if isinstance(k, str) and isinstance(v, str) and v.strip()
            }
        location = self.location_id.strip() or None if isinstance(self.location_id, str) else self.location_id
        return Facts(location_id=location, with_ids=ids(self.with_ids), for_ids=ids(self.for_ids), notes=notes)

    def people(self) -> list[str]:
        """Every person these facts name: with, for, then noted."""
        return list(dict.fromkeys([*(self.with_ids or ()), *(self.for_ids or ()), *(self.notes or {})]))

    def to_json(self) -> str:
        """Compact JSON, leaving out what's unset."""
        data = {
            _JSON_KEYS.get(f.name, f.name): getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) not in (None, [], {})
        }
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "Facts | None":
        """Facts from what `to_json` wrote; `None` if it isn't facts at all
        (a broken hand edit, say). Unknown keys are ignored."""
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        names = {_JSON_KEYS.get(f.name, f.name): f.name for f in fields(cls)}
        return cls(**{names[key]: value for key, value in data.items() if key in names})


def fact_problems(facts: Facts) -> list[str]:
    """Everything wrong with `facts`' shape, as phrases to follow "its
    facts"; empty if they're fine. Whether the ids name real people and
    locations is checked by the caller, which knows them."""
    problems = []
    if facts.location_id is not None and not isinstance(facts.location_id, str):
        problems.append('"location_id" must be a location id')
    for name in ("with_ids", "for_ids"):
        value = getattr(facts, name)
        if value is not None and not (isinstance(value, list) and all(isinstance(i, str) and i for i in value)):
            problems.append(f'"{name}" must be a list of person ids')
    if SELF_ID in (facts.with_ids or []) and isinstance(facts.with_ids, list):
        problems.append('"with_ids" never names "self": the user is at every event')
    both = sorted(set(facts.with_ids or []) & set(facts.for_ids or [])) if not problems else []
    if both:
        problems.append(f'{both} can\'t be both "with" (there) and "for" (not there)')
    if facts.notes is not None:
        if not (isinstance(facts.notes, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in facts.notes.items())):
            problems.append('"notes" must be {person id: a note}')
        else:
            long = sorted(k for k, v in facts.notes.items() if len(v) > MAX_NOTE_CHARS)
            if long:
                problems.append(f'"notes" for {long} must be at most {MAX_NOTE_CHARS} characters')
            absent = sorted(set(facts.notes) - set(facts.with_ids or []) - {SELF_ID})
            if absent:
                problems.append(f'"notes" are for the people who were there ("self" and "with"), not {absent}')
    if not problems and len(facts.to_json()) > MAX_FACTS_CHARS:
        problems.append(f"are longer than {MAX_FACTS_CHARS} characters as JSON")
    return problems
