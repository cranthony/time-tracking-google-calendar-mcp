"""Traits: qualities the user wants to show toward people (and toward
themselves) -- Thoughtful, Reliable, Creative, Adventurous, Generous --
each rated from **parts** computed over a goal's events and their facets
(see utilities/facets.py). A goal is rated by its traits with a `traits`
measure (utilities/goal_measures.py), which utilities/trait_scores.py
computes. See docs/goal-tree-and-traits-plan.md section 3.

**Where they live.** The **Traits** tab of the calendar's metadata
spreadsheet (utilities/calendar_metadata_sheet.py), one row per trait:
`id | name | status | definition | parts`, read by header name like the
goals tab, so it can be edited by hand. It's created the first time it's
needed, seeded with SEED_TRAITS.

- `id`: a short slug made from its first name (e.g. "thoughtful"), never
  changed, so a measure's `traits` keep naming it after a rename.
- `status`: `active` (rated), `off` (kept, but not rated for now) or
  `archived` (retired: not rated, and listed only when asked for).
- `parts`: JSON, a list of parts -- see below.

**Parts.** A part is a measure with no scope: the goal using the trait
supplies it (its events, and its sub-goals'). Each is an object with a
`kind`, the fields its kind takes (PART_KINDS) and an optional `weight`
(>= 0, default 1); a trait's score is the weighted mean of its parts'
scores (0-100), leaving out any with nothing to rate it by. "With events"
are the goal's events whose facets don't say they were only *for* it,
plus any whose facets name it in `with`; "for events" are those whose
facets name it in `for` (preparation while they weren't there). Counts
over the window (`window_days`, by default the measure's) are rated
against `target` in proportion, capped at 100.

| kind                | rates                                                   |
| ------------------- | ------------------------------------------------------- |
| `prep`              | for events in the window, against `target` (default 1)  |
| `prep_regularity`   | the share of the last `weeks` (default 4) 7-day spans   |
|                     | with at least one for event                             |
| `continuity`        | the last with event ended within `last_within_days`     |
|                     | (default 14) of the day's end, and the next starts      |
|                     | within `next_within_days` (default 14) after it: 100    |
|                     | for both, 50 for one, 0 for neither                     |
| `together_creative` | with events with `creative` >= `min_creative` (default  |
|                     | 2) in the window, against `target` (default 1)          |
| `novelty`           | with events whose `new` isn't none, against `target`    |
|                     | (default 1)                                             |
| `effort_paid`       | minutes x (1 + `effort`) over with and for events in    |
|                     | the window, against `target` (required)                 |
| `attention`         | the mean `attention` (0-3, as 0-100) of with events in  |
|                     | the window that have one                                |
| `judgment`          | a `rubric`, judged in the reflection                    |
| `count`, `duration` | as the measures of the same kind, over the goal's       |
|                     | events; `interval_days` defaults to the window. With    |
|                     | `activity`, only its with events whose facets name that |
|                     | activity: "visit every 21 days, drive every 30"         |
| `follow_through`    | as the measure of the same kind, over the goal's events |

**Per goal.** A goal's traits measure can replace any trait's parts for
that goal alone (its `parts`: {trait id: [parts]}, checked like the
trait's own) -- each person's own cadences for Reliable, say, while the
Traits tab keeps everyone else's.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass, fields, replace
from typing import Any, Literal

from calendar_clients.google_sheets import SheetsClient, TabRange
from utilities import calendar_metadata_sheet
from utilities.goal_measures import MEASURE_KINDS, measure_problems

TraitStatus = Literal["active", "off", "archived"]
TRAIT_STATUSES: tuple[str, ...] = ("active", "off", "archived")
DEFAULT_TRAIT_STATUSES: tuple[str, ...] = ("active", "off")
"""The traits listed unless others are asked for: archived ones are retired."""

MAX_NAME_LENGTH = 50
_MAX_ID_LENGTH = 30

_WINDOW = frozenset({"window_days"})

_REUSED_KINDS = ("count", "duration", "follow_through")
"""Measure kinds that are parts too, without their scope."""

_SCOPE = frozenset({"events_of", "include_sub_goals", "only_if"})
"""A measure's fields saying whose events it reads: a part has none."""

_ACTIVITY = frozenset({"activity"})
"""A count or duration part's: only events of that activity count."""

MAX_ACTIVITY_CHARS = 60
"""As long as a facet's activity may be (utilities/facets.py)."""

PART_KINDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "prep": (frozenset(), frozenset({"target"}) | _WINDOW),
    "prep_regularity": (frozenset(), frozenset({"weeks"})),
    "continuity": (frozenset(), frozenset({"last_within_days", "next_within_days"})),
    "together_creative": (frozenset(), frozenset({"target", "min_creative"}) | _WINDOW),
    "novelty": (frozenset(), frozenset({"target"}) | _WINDOW),
    "effort_paid": (frozenset({"target"}), _WINDOW),
    "attention": (frozenset(), _WINDOW),
    "judgment": (frozenset({"rubric"}), frozenset()),
    **{
        kind: (MEASURE_KINDS[kind][0], MEASURE_KINDS[kind][1] - _SCOPE | (_ACTIVITY if kind != "follow_through" else frozenset()))
        for kind in _REUSED_KINDS
    },
}
"""Each part kind's (required, optional) fields, besides `kind` and
`weight` -- see the module docstring."""

DEFAULT_WINDOW_DAYS = 30
DEFAULT_TARGET = 1
DEFAULT_WEEKS = 4
DEFAULT_WITHIN_DAYS = 14
DEFAULT_MIN_CREATIVE = 2


@dataclass(kw_only=True)
class Trait:
    """One trait -- see the module docstring."""

    id: str | None = None
    """Assigned on creation from its name; never changes."""

    name: str | None = None
    status: TraitStatus | None = None
    """active, off or archived."""

    definition: str | None = None
    """What it means, in a sentence or two."""

    parts: list[dict[str, Any]] | None = None
    """What it's rated from: see PART_KINDS."""

    @classmethod
    def from_row(cls, header_row: list[str], data: list[str]) -> "Trait":
        cells = {header: (data[i] if i < len(data) else "") for i, header in enumerate(header_row)}

        def text(name: str) -> str | None:
            return cells.get(name, "").strip() or None

        parts = text("parts")
        try:
            parsed = json.loads(parts) if parts is not None else None
        except ValueError:
            parsed = parts  # Left for trait_problems to report.
        return cls(
            id=text("id"),
            name=text("name"),
            status=(text("status") or "").lower() or None,
            definition=text("definition"),
            parts=parsed,
        )

    def to_row(self, header_row: list[str], original_row: list[str] | None = None) -> list[str]:
        names = {f.name for f in fields(self)}
        row = []
        for i, header in enumerate(header_row):
            if header in names:
                value = getattr(self, header)
                row.append("" if value is None else value if isinstance(value, str) else json.dumps(value, separators=(",", ":")))
            else:
                row.append(original_row[i] if original_row is not None and i < len(original_row) else "")
        return row


@dataclass(kw_only=True)
class ListedTrait(Trait):
    """A trait as get_traits lists it: plus anything wrong with it, as hand
    edits can leave it."""

    problems: list[str] | None = None
    """What's wrong with it (see `trait_problems`): its bad parts aren't
    rated. `None` if nothing is."""


HEADER_ROW = ["id", "name", "status", "definition", "parts"]
assert HEADER_ROW == [f.name for f in fields(Trait)]

_HEADER_RANGE = "A1:Z1"
_DATA_RANGE = "A2:Z"
_WHOLE_RANGE = "A1:Z"

SEED_TRAITS: list[Trait] = [
    Trait(
        id="thoughtful",
        name="Thoughtful",
        status="active",
        definition="Remember what matters to them, and prepare for it.",
        parts=[
            {"kind": "prep", "target": 2},
            {"kind": "prep_regularity", "weeks": 4},
            {
                "kind": "judgment",
                "rubric": 'Did the events and notes reflect what\'s in the goal\'s "What matters to them" section?',
            },
        ],
    ),
    Trait(
        id="reliable",
        name="Reliable",
        status="active",
        definition="Do what I said I would, and keep contact going -- reliable to myself too.",
        parts=[
            {"kind": "continuity", "last_within_days": 14, "next_within_days": 14},
            {"kind": "follow_through"},
            {"kind": "count", "target": 1, "interval_days": 14, "zero_at_days": 30, "noun": "meetings"},
        ],
    ),
    Trait(
        id="creative",
        name="Creative",
        status="active",
        definition="Make something together with them (making something alone is taking care of myself).",
        parts=[{"kind": "together_creative", "target": 1, "min_creative": 2}],
    ),
    Trait(
        id="adventurous",
        name="Adventurous",
        status="active",
        definition="Share new experiences with them: new activities, new places.",
        parts=[{"kind": "novelty", "target": 1}],
    ),
    Trait(
        id="generous",
        name="Generous",
        status="active",
        definition=(
            "Make an effort for them, including the attention I give them: the follow-through of "
            "thoughtfulness."
        ),
        parts=[{"kind": "effort_paid", "target": 600}, {"kind": "attention"}],
    ),
]
"""What a new Traits tab starts with -- see
docs/goal-tree-and-traits-plan.md section 3.3. The numbers are starting
points to tune."""


def trait_problems(trait: Trait) -> list[str]:
    """Everything wrong with a trait, as phrases (e.g. 'part 2 (prep) has
    no field "goal"; ...'); empty if it's fine. Like
    utilities/goal_measures.py's `measure_problems`, so a bad part is
    refused when it's saved rather than silently left unrated."""
    problems = []
    if not (isinstance(trait.name, str) and trait.name.strip()):
        problems.append("it needs a name")
    elif len(trait.name) > MAX_NAME_LENGTH:
        problems.append(f"its name is longer than {MAX_NAME_LENGTH} characters")
    if trait.status not in TRAIT_STATUSES:
        problems.append(f"its status must be one of {', '.join(TRAIT_STATUSES)}")
    if trait.definition is not None and not isinstance(trait.definition, str):
        problems.append("its definition must be text")
    if not (isinstance(trait.parts, list) and trait.parts):
        problems.append("it needs parts: a list of at least one part, each an object with a \"kind\"")
        return problems
    for number, part in enumerate(trait.parts, 1):
        problems += [f"part {number}{_kind_of(part)} {p}" for p in part_problems(part)]
    return problems


def part_problems(part: Any) -> list[str]:
    """Everything wrong with one part, as phrases following "its part"."""
    if not isinstance(part, dict) or not isinstance(part.get("kind"), str):
        return ['must be an object with a "kind"']
    kind = part["kind"]
    if kind not in PART_KINDS:
        return [f"has kind {kind!r}; a part's kind is one of {', '.join(PART_KINDS)}"]
    required, optional = PART_KINDS[kind]
    problems = [f'needs "{name}"' for name in sorted(required - part.keys())]
    for name in sorted(part.keys() - required - optional - {"kind", "weight"}):
        if name in _SCOPE:
            problems.append(f'has "{name}", but a part has no scope: the goal using the trait supplies it')
        else:
            takes = ", ".join(f'"{n}"' for n in sorted(required | optional | {"weight"}))
            problems.append(f'has no field "{name}"; a {kind} part takes {takes}')
    if problems:
        return problems
    if "weight" in part and not (_is_number(part["weight"]) and part["weight"] >= 0):
        problems.append('"weight" must be a number, 0 or more')
    if "activity" in part and not (
        isinstance(part["activity"], str) and part["activity"].strip() and len(part["activity"]) <= MAX_ACTIVITY_CHARS
    ):
        problems.append(f'"activity" must be a short label, at most {MAX_ACTIVITY_CHARS} characters')
    if kind in _REUSED_KINDS:
        measure = {k: v for k, v in part.items() if k not in ("weight", "activity")}
        return problems + measure_problems(measure)
    for name in ("target", "window_days", "last_within_days", "next_within_days"):
        if name in part and not (_is_number(part[name]) and part[name] > 0):
            problems.append(f'"{name}" must be a number above 0')
    if "weeks" in part and not (isinstance(part["weeks"], int) and not isinstance(part["weeks"], bool) and part["weeks"] >= 1):
        problems.append('"weeks" must be a whole number, 1 or more')
    if "min_creative" in part and part["min_creative"] not in (1, 2, 3):
        problems.append('"min_creative" must be 1, 2 or 3')
    if "rubric" in part and not (isinstance(part["rubric"], str) and part["rubric"].strip()):
        problems.append('"rubric" must be non-empty text')
    return problems


def parts_override_problems(overrides: Any, selected: list[str] | None) -> list[str]:
    """What's wrong with a traits measure's `parts`: {trait id: [parts]},
    each list checked as a trait's own; `selected` are the ids its
    `traits` names (`None` for "all"). Phrases follow "its measure"."""
    if not isinstance(overrides, dict):
        return ['"parts" must be {trait id: [parts]}']
    problems = []
    for trait_id, parts in overrides.items():
        if selected is not None and trait_id not in selected:
            problems.append(f'"parts" names {trait_id!r}, which "traits" doesn\'t select')
        elif not (isinstance(parts, list) and parts):
            problems.append(f'"parts" for {trait_id!r} must be a list of at least one part')
        else:
            for number, part in enumerate(parts, 1):
                problems += [
                    f'"parts" for {trait_id!r}: part {number}{_kind_of(part)} {p}' for p in part_problems(part)
                ]
    return problems


def activity_label(text: str) -> str:
    """An activity as facets keep it: trimmed, single-spaced, lowercase
    (see utilities/facets.py)."""
    return " ".join(text.split()).casefold()


def part_keys(parts: list[dict[str, Any]]) -> list[str]:
    """A name for each of `parts`, unique within them: its kind, then
    "kind#2", "kind#3" for later ones of the same kind. Ratings' metrics
    name parts by these."""
    seen: dict[str, int] = {}
    keys = []
    for part in parts:
        kind = part.get("kind") if isinstance(part, dict) else None
        kind = kind if isinstance(kind, str) else "part"
        seen[kind] = seen.get(kind, 0) + 1
        keys.append(kind if seen[kind] == 1 else f"{kind}#{seen[kind]}")
    return keys


def _kind_of(part: Any) -> str:
    kind = part.get("kind") if isinstance(part, dict) else None
    return f" ({kind})" if isinstance(kind, str) and kind in PART_KINDS else ""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")[:_MAX_ID_LENGTH].strip("-") or "trait"


CLEARABLE_TRAIT_FIELDS = frozenset({"definition"})
"""Trait fields `update_trait` can blank."""


class Traits:
    """A calendar's traits, in the Traits tab of its metadata spreadsheet --
    see the module docstring."""

    def __init__(self, sheets_client: SheetsClient, spreadsheet_id: str, sheet_id: int) -> None:
        self._sheets_client = sheets_client
        self._spreadsheet_id = spreadsheet_id
        self._sheet_id = sheet_id

    @staticmethod
    def ensure(sheets_client: SheetsClient, spreadsheet_id: str) -> "Traits":
        """The calendar's Traits tab within `spreadsheet_id`, creating it
        with SEED_TRAITS the first time only (tagged once they're written,
        so a failure partway leaves no half-seeded tab)."""
        sheet_id = calendar_metadata_sheet.find_tab(
            sheets_client, spreadsheet_id, calendar_metadata_sheet.TRAITS_SHEET_ROLE
        )
        if sheet_id is None:

            def populate(new_sheet_id: int) -> None:
                sheets_client.write_rows_in_sheet(spreadsheet_id, new_sheet_id, _HEADER_RANGE, [HEADER_ROW])
                sheets_client.write_rows_in_sheet(
                    spreadsheet_id, new_sheet_id, _DATA_RANGE, [t.to_row(HEADER_ROW) for t in SEED_TRAITS]
                )

            sheet_id = calendar_metadata_sheet.create_tab(
                sheets_client,
                spreadsheet_id,
                role=calendar_metadata_sheet.TRAITS_SHEET_ROLE,
                title=calendar_metadata_sheet.TRAITS_SHEET_TITLE,
                populate=populate,
            )
        return Traits(sheets_client, spreadsheet_id, sheet_id)

    @property
    def whole_tab(self) -> TabRange:
        """This whole tab, for `SheetsClient.prefetch`."""
        return TabRange(self._spreadsheet_id, self._sheet_id, _WHOLE_RANGE)

    def prefetch(self, ranges: list[TabRange]) -> None:
        self._sheets_client.prefetch(ranges)

    # -- reading ------------------------------------------------------------

    def all(self) -> list[Trait]:
        """Every trait, in sheet order; blank rows are skipped."""
        header_row, rows = self._read()
        return [Trait.from_row(header_row, row) for row in rows if any(cell.strip() for cell in row)]

    def by_id(self) -> dict[str, Trait]:
        return {t.id: t for t in self.all() if t.id}

    def get_traits(self, statuses: Collection[str] | None = None) -> list[ListedTrait]:
        """The traits with any of `statuses` (by default active and off),
        each with what's wrong with it, if anything."""
        statuses = _check_statuses(statuses)
        return [
            ListedTrait(**{f.name: getattr(t, f.name) for f in fields(Trait)}, problems=trait_problems(t) or None)
            for t in self.all()
            if t.status in statuses
        ]

    # -- writing ------------------------------------------------------------

    def create_trait(self, trait: Trait) -> Trait:
        """Add `trait` (active unless given a status), with an id made from
        its name. Returns it as written."""
        traits = self.all()
        new = replace(trait, status=trait.status or "active")
        problems = trait_problems(new)
        if problems:
            raise ValueError(_said(new, problems))
        taken = {t.id for t in traits}
        base = _slug(new.name)
        new.id, n = base, 2
        while new.id in taken:
            new.id, n = f"{base[: _MAX_ID_LENGTH - len(str(n)) - 1]}-{n}", n + 1
        self._check_unique_name(new, traits)
        self._write(traits + [new])
        return new

    def update_trait(self, trait: Trait, clear_fields: Collection[str] = ()) -> Trait:
        """Set whichever of `trait`'s fields aren't `None` on the trait with
        `trait.id` (new parts replace the old whole), and blank those in
        `clear_fields`. Returns it as written."""
        if not trait.id:
            raise ValueError("update_trait needs the trait's id")
        unknown = set(clear_fields) - CLEARABLE_TRAIT_FIELDS
        if unknown:
            raise ValueError(f"Can't clear {sorted(unknown)}; clearable fields are {sorted(CLEARABLE_TRAIT_FIELDS)}")
        if any(getattr(trait, name) is not None for name in clear_fields):
            raise ValueError(f"Can't both set and clear {sorted(n for n in clear_fields if getattr(trait, n) is not None)}")
        traits = self.all()
        index = next((i for i, t in enumerate(traits) if t.id == trait.id), None)
        if index is None:
            known = ", ".join(f"{t.id} ({t.name})" for t in traits)
            raise ValueError(f"{trait.id!r} isn't a trait; the traits are {known}")
        updated = replace(
            traits[index],
            **{f.name: getattr(trait, f.name) for f in fields(Trait) if f.name != "id" and getattr(trait, f.name) is not None},
            **{name: None for name in clear_fields},
        )
        problems = trait_problems(updated)
        if problems:
            raise ValueError(_said(updated, problems))
        self._check_unique_name(updated, traits)
        traits[index] = updated
        self._write(traits)
        return updated

    @staticmethod
    def _check_unique_name(trait: Trait, traits: list[Trait]) -> None:
        clash = next(
            (t for t in traits if t.id != trait.id and (t.name or "").casefold() == trait.name.casefold()), None
        )
        if clash is not None:
            raise ValueError(f"There's already a trait named {clash.name!r} ({clash.id})")

    def _read(self) -> tuple[list[str], list[list[str]]]:
        header, rows = self._sheets_client.read_ranges_in_sheet(
            self._spreadsheet_id, self._sheet_id, [_HEADER_RANGE, _DATA_RANGE]
        )
        header_row = [cell.strip() for cell in header[0]] if header else []
        missing = [column for column in ("id", "name", "status", "parts") if column not in header_row]
        if missing:
            raise ValueError(f"The Traits tab's header row is missing columns: {missing}")
        return header_row, rows

    def _write(self, traits: list[Trait]) -> None:
        """Overwrite the data rows, keeping unknown columns' cells, and
        blanking rows left over from a longer list."""
        header_row, previous = self._read()
        by_id = {Trait.from_row(header_row, row).id: row for row in previous if any(c.strip() for c in row)}
        rows = [t.to_row(header_row, by_id.get(t.id)) for t in traits]
        rows += [[""] * len(header_row)] * max(0, len(previous) - len(traits))
        self._sheets_client.write_rows_in_sheet(self._spreadsheet_id, self._sheet_id, _DATA_RANGE, rows)


def _said(trait: Trait, problems: list[str]) -> str:
    name = f"The trait {trait.name!r}" if isinstance(trait.name, str) and trait.name.strip() else "The trait"
    return f"{name}: " + "; ".join(problems)


def _check_statuses(statuses: Collection[str] | None) -> tuple[str, ...]:
    if statuses is None:
        return DEFAULT_TRAIT_STATUSES
    unknown = sorted(set(statuses) - set(TRAIT_STATUSES))
    if unknown:
        raise ValueError(f"Unknown trait status(es) {unknown}; statuses are {', '.join(TRAIT_STATUSES)}")
    return tuple(statuses)
