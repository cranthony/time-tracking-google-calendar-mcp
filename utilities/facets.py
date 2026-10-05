"""An event's facets: what happened at it, as far as traits are concerned
-- who it was with and who it was for, what it was and where, and four
0-3 judgments. Compaction writes them on past events that carry a goal
measured by traits (see utilities/traits.py), and `update_event` can edit
them later. See docs/goal-tree-and-traits-plan.md section 3.2.

They're kept on the event itself, as JSON in one private extended
property (`cascading-time-tracker-facets`), so they travel with it and
outlive the notes they were judged from. Calendar keeps at most 1024
characters per value, so `facet_problems` refuses facets longer than that
as JSON rather than let Calendar cut them.

| field       | in JSON     | what                                                  |
| ----------- | ----------- | ----------------------------------------------------- |
| with_goal_ids | `with`    | goals of the people present                           |
| for_goal_ids  | `for`     | goals of people it was done for, who weren't there    |
|             |             | (preparation: a gift, a plan, a dish to bring)        |
| activity    | `activity`  | what it was, e.g. "salsa social", reusing the labels  |
|             |             | of the goal's history digest                          |
| place       | `place`     | where, e.g. a venue's name                            |
| creative    | `creative`  | 0-3: how much they made something together            |
| new         | `new`       | none, activity, place or both: what was new to them,  |
|             |             | judged against the digest                             |
| effort      | `effort`    | 0-3: effort beyond showing up (prepared, cooked,      |
|             |             | hosted, traveled)                                     |
| attention   | `attention` | 0-3: the quality of attention given, from the notes   |
| why         | `why`       | one line of evidence for the judgments                |

Activity and place are normalized (trimmed, single-spaced, lowercase), so
the digest groups them reliably.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields
from typing import Literal

MAX_FACETS_CHARS = 1024
"""Calendar's limit on one extended property's value."""

MAX_WHY_CHARS = 200
"""The evidence is one line."""

MAX_LABEL_CHARS = 60
"""An activity or place is a short label."""

NewKind = Literal["none", "activity", "place", "both"]
NEW_KINDS: tuple[str, ...] = ("none", "activity", "place", "both")

SCORES = ("creative", "effort", "attention")
"""The 0-3 judgments."""

_JSON_KEYS = {"with_goal_ids": "with", "for_goal_ids": "for"}
"""Fields stored under a shorter key (the rest under their own name)."""

_SPACES = re.compile(r"\s+")


@dataclass(kw_only=True)
class Facets:
    """What happened at an event, for its goals' traits -- see the module
    docstring. Every field is optional."""

    with_goal_ids: list[str] | None = None
    """The goals of the people present."""

    for_goal_ids: list[str] | None = None
    """The goals of the people it was done for, while they weren't there."""

    activity: str | None = None
    place: str | None = None
    creative: int | None = None
    """0-3: made something together."""

    new: NewKind | None = None
    """What was new to them: none, activity, place or both."""

    effort: int | None = None
    """0-3: effort beyond showing up."""

    attention: int | None = None
    """0-3: quality of attention."""

    why: str | None = None
    """One line of evidence."""

    def is_empty(self) -> bool:
        return all(getattr(self, f.name) in (None, []) for f in fields(self))

    def normalized(self) -> "Facets":
        """These facets with activity and place normalized (see the module
        docstring), blank text dropped, and goal ids deduplicated."""

        def label(value: str | None) -> str | None:
            return _SPACES.sub(" ", value).strip().casefold() or None if value is not None else None

        def ids(value: list[str] | None) -> list[str] | None:
            return list(dict.fromkeys(i.strip() for i in value if i.strip())) if value is not None else None

        return Facets(
            with_goal_ids=ids(self.with_goal_ids),
            for_goal_ids=ids(self.for_goal_ids),
            activity=label(self.activity),
            place=label(self.place),
            creative=self.creative,
            new=self.new,
            effort=self.effort,
            attention=self.attention,
            why=_SPACES.sub(" ", self.why).strip() or None if self.why is not None else None,
        )

    def to_json(self) -> str:
        """Compact JSON, leaving out what's unset."""
        data = {
            _JSON_KEYS.get(f.name, f.name): getattr(self, f.name)
            for f in fields(self)
            if getattr(self, f.name) not in (None, [])
        }
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> "Facets | None":
        """Facets from what `to_json` wrote; `None` if it isn't facets at
        all (a broken hand edit, say), and unknown keys are ignored."""
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        names = {_JSON_KEYS.get(f.name, f.name): f.name for f in fields(cls)}
        return cls(**{names[key]: value for key, value in data.items() if key in names})


def facet_problems(facets: Facets) -> list[str]:
    """Everything wrong with `facets`, as phrases to follow "its facets"
    (e.g. '"creative" must be a whole number from 0 to 3'); empty if
    they're fine. Goal ids aren't checked here: see server.py."""
    problems = []
    for name in ("with_goal_ids", "for_goal_ids"):
        value = getattr(facets, name)
        if value is not None and not (isinstance(value, list) and all(isinstance(i, str) and i for i in value)):
            problems.append(f'"{name}" must be a list of goal ids')
    for name in ("activity", "place"):
        value = getattr(facets, name)
        if value is not None and not isinstance(value, str):
            problems.append(f'"{name}" must be text')
        elif value is not None and len(value) > MAX_LABEL_CHARS:
            problems.append(f'"{name}" must be a short label, at most {MAX_LABEL_CHARS} characters')
    for name in SCORES:
        value = getattr(facets, name)
        if value is not None and not (isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 3):
            problems.append(f'"{name}" must be a whole number from 0 to 3')
    if facets.new is not None and facets.new not in NEW_KINDS:
        problems.append(f'"new" must be one of {", ".join(NEW_KINDS)}')
    if facets.why is not None and not isinstance(facets.why, str):
        problems.append('"why" must be text')
    elif facets.why is not None and len(facets.why) > MAX_WHY_CHARS:
        problems.append(f'"why" must be one line, at most {MAX_WHY_CHARS} characters')
    if not problems and len(facets.to_json()) > MAX_FACETS_CHARS:
        problems.append(f"are longer than {MAX_FACETS_CHARS} characters as JSON")
    return problems


def goal_ids_in(facets: Facets | None) -> list[str]:
    """Every goal id `facets` name, with first."""
    if facets is None:
        return []
    return list(dict.fromkeys([*(facets.with_goal_ids or ()), *(facets.for_goal_ids or ())]))

