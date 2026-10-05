"""Traits: how the user wants to be with people (and with themselves) --
Thoughtful, Reliable, Creative, Adventurous, Generous -- each rated, for
a person, from **parts** read off the events they were part of. Nothing
about a trait is hard-coded: the Traits tab holds them all.

**Where they live.** The **Traits** tab of the calendar's metadata
spreadsheet (utilities/calendar_metadata_sheet.py), one row per trait:
`id | name | status | definition | parts`, read by header name, so it can
be edited by hand. It's created the first time it's needed, seeded with
SEED_TRAITS.

- `id`: a short slug made from its first name (e.g. "thoughtful"), never
  changed, so a person's `traits` keep naming it after a rename.
- `status`: `active` (rated), `off` (kept, but not rated for now) or
  `archived` (retired: not rated, and listed only when asked for).
- `parts`: JSON, a list of parts -- see below.

**Parts.** Each part is an object with a `kind`, the fields its kind
takes (PART_KINDS), an optional `weight` (>= 0, default 1) and an
optional `engagement_type`: "with" (the default) reads the events the
person was at with the user, "for" those the user did for them while
they weren't there (preparation, a gift). A trait's score is the
weighted mean of its parts' scores (0-100), leaving out any with nothing
to rate it by.

| kind             | rates                                                    |
| ---------------- | -------------------------------------------------------- |
| `judgment`       | each event, judged by the assistant against a `rubric`   |
|                  | on a scale of `ratings`, from the `facts` it names       |
| `continuity`     | the last event ended within `last_within_days` (default  |
|                  | 14) of the day's end, and the next starts within         |
|                  | `next_within_days` (default 14) after it: 100 for both,  |
|                  | 50 for one, 0 for neither                                |
| `count`          | events over `interval_days` against `target`, as the     |
|                  | count measure: "see them every 21 days"                  |
| `duration`       | minutes over `interval_days` against `target_min`        |
| `follow_through` | a running score that drops for each cancelled event and  |
|                  | recovers on days one is kept                             |

`continuity`, `count`, `duration` and `follow_through` also take an
optional `action`: an action or action group id (utilities/actions.py),
counting only events of it.

**Judgments.** A judgment part is how a trait is read off what actually
happened. Its `rubric` is the question ("Was this activity or place
new?"), `ratings` the scale ({"0": "The activity and place were
routine", ..., "3": "Both were new, or it was otherwise adventurous"}),
and `facts` what the assistant is shown to answer it -- each a name or
{"fact": name, "lookback_days": n} (see FACTS). A judgment is always made
for one person, or a named group of people, and made by the assistant on
its own, with a line of reasoning, never by asking the user.

**Per person.** A person's `traits` (utilities/people.py) can select
which traits apply to them and replace a trait's parts for them alone --
see `person_traits_problems`. Without it, every active trait applies,
with the Traits tab's parts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection
from dataclasses import dataclass, fields, replace
from typing import Any, Literal

from calendar_clients.google_sheets import SheetsClient, TabRange
from utilities import calendar_metadata_sheet

TraitStatus = Literal["active", "off", "archived"]
TRAIT_STATUSES: tuple[str, ...] = ("active", "off", "archived")
DEFAULT_TRAIT_STATUSES: tuple[str, ...] = ("active", "off")
"""The traits listed unless others are asked for: archived ones are retired."""

MAX_NAME_LENGTH = 50
_MAX_ID_LENGTH = 30

ENGAGEMENT_TYPES = ("with", "for")
"""A part's `engagement_type`: the events the person was at with the user,
or those the user did for them while they weren't there."""

_COMMON = frozenset({"weight", "engagement_type"})
"""Fields every part takes, besides `kind`."""

_ACTION = frozenset({"action"})

PART_KINDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "judgment": (frozenset({"rubric", "ratings", "facts"}), frozenset()),
    "continuity": (frozenset(), frozenset({"last_within_days", "next_within_days"}) | _ACTION),
    "count": (frozenset({"target"}), frozenset({"noun", "interval_days", "zero_at_days"}) | _ACTION),
    "duration": (frozenset({"target_min"}), frozenset({"interval_days", "zero_at_days"}) | _ACTION),
    "follow_through": (frozenset(), frozenset({"penalty", "recovery", "look_back_days"}) | _ACTION),
}
"""Each part kind's (required, optional) fields, besides `kind`,
`weight` and `engagement_type` -- see the module docstring."""

FACTS = ("action", "action_history", "location", "location_history", "general_notes", "person_notes")
"""What a judgment can be shown about an event, to judge it by:

- `action`: what the user was doing (its actions).
- `action_history`: what they've done with the person before, over the
  last `lookback_days`.
- `location`: where it was.
- `location_history`: where they've been with the person before, over
  the last `lookback_days`.
- `general_notes`: the event's own notes.
- `person_notes`: the notes about the person -- the user's own for a
  "for" engagement, the other person's for a "with" one."""

HISTORY_FACTS = frozenset({"action_history", "location_history"})
"""The facts that look back, over `lookback_days`."""

DEFAULT_LOOKBACK_DAYS = 30
DEFAULT_TARGET = 1
DEFAULT_WITHIN_DAYS = 14
DEFAULT_WINDOW_DAYS = 30


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

_ZERO_TO_THREE_EFFORT = {
    "0": "Little effort, or my attention was elsewhere",
    "1": "Some effort or attention",
    "2": "Real effort, or my full attention",
    "3": "I went out of my way for them and was fully present",
}

SEED_TRAITS: list[Trait] = [
    Trait(
        id="thoughtful",
        name="Thoughtful",
        status="active",
        definition="Remember what matters to them, and prepare for it.",
        parts=[
            {
                "kind": "judgment",
                "rubric": "Did this event show that I remembered what matters to them?",
                "ratings": {
                    "0": "Nothing in it touched on what matters to them",
                    "1": "A small sign that I remembered",
                    "2": "It clearly reflected what matters to them",
                    "3": "It was shaped around what matters to them",
                },
                "facts": ["action", "general_notes", "person_notes"],
            },
            {
                "kind": "judgment",
                "engagement_type": "for",
                "rubric": "Was this preparation for them, built on what matters to them?",
                "ratings": {
                    "0": "It wasn't really for them",
                    "1": "It was for them, but generic",
                    "2": "It was for them, and fit what matters to them",
                    "3": "It was carefully made around what matters to them",
                },
                "facts": ["action", "general_notes", "person_notes"],
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
        parts=[
            {
                "kind": "judgment",
                "rubric": "Did we make something together?",
                "ratings": {
                    "0": "Nothing was made",
                    "1": "We shared ideas, but made nothing",
                    "2": "We made something small together",
                    "3": "We made something substantial together",
                },
                "facts": ["action", {"fact": "action_history", "lookback_days": 30}, "general_notes"],
            }
        ],
    ),
    Trait(
        id="adventurous",
        name="Adventurous",
        status="active",
        definition="Share new experiences with them: new activities, new places.",
        parts=[
            {
                "kind": "judgment",
                "rubric": "Was this activity or place new?",
                "ratings": {
                    "0": "The activity and place were routine",
                    "1": "There was a twist on the activity or place",
                    "2": "The activity or the place were new",
                    "3": "Both the activity and place were new, or the event was otherwise adventurous",
                },
                "facts": [
                    "action",
                    {"fact": "action_history", "lookback_days": 90},
                    "location",
                    {"fact": "location_history", "lookback_days": 90},
                ],
            }
        ],
    ),
    Trait(
        id="generous",
        name="Generous",
        status="active",
        definition=(
            "Make an effort for them, including the attention I give them: the follow-through of "
            "thoughtfulness."
        ),
        parts=[
            {
                "kind": "judgment",
                "rubric": "How much effort and attention did I give them?",
                "ratings": _ZERO_TO_THREE_EFFORT,
                "facts": ["action", "general_notes", "person_notes"],
            },
            {
                "kind": "judgment",
                "engagement_type": "for",
                "rubric": "How much effort did I put into doing this for them?",
                "ratings": _ZERO_TO_THREE_EFFORT,
                "facts": ["action", "general_notes"],
            },
        ],
    ),
]
"""What a new Traits tab starts with. The numbers and wording are
starting points to tune."""


def trait_problems(trait: Trait) -> list[str]:
    """Everything wrong with a trait, as phrases (e.g. 'part 2 (count)
    needs "target"; ...'); empty if it's fine, so a bad part is refused
    when it's saved rather than silently left unrated."""
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
    for name in sorted(part.keys() - required - optional - _COMMON - {"kind"}):
        takes = ", ".join(f'"{n}"' for n in sorted(required | optional | _COMMON))
        problems.append(f'has no field "{name}"; a {kind} part takes {takes}')
    if problems:
        return problems
    if "weight" in part and not (_is_number(part["weight"]) and part["weight"] >= 0):
        problems.append('"weight" must be a number, 0 or more')
    if "engagement_type" in part and part["engagement_type"] not in ENGAGEMENT_TYPES:
        problems.append('"engagement_type" must be "with" or "for"')
    if "action" in part and not (isinstance(part["action"], str) and part["action"].strip()):
        problems.append('"action" must be an action or action group id')
    for name in ("target", "target_min", "interval_days", "last_within_days", "next_within_days", "look_back_days"):
        if name in part and not (_is_number(part[name]) and part[name] > 0):
            problems.append(f'"{name}" must be a number above 0')
    for name in ("penalty", "recovery"):
        if name in part and not (_is_number(part[name]) and 0 <= part[name] <= 100):
            problems.append(f'"{name}" must be a number from 0 to 100')
    if "zero_at_days" in part:
        interval = part.get("interval_days", DEFAULT_WINDOW_DAYS)
        if not (_is_number(part["zero_at_days"]) and _is_number(interval) and part["zero_at_days"] > interval):
            problems.append('"zero_at_days" must be a number above "interval_days"')
    if "noun" in part and not (isinstance(part["noun"], str) and part["noun"].strip()):
        problems.append('"noun" must be non-empty text')
    if kind == "judgment":
        problems += _judgment_problems(part)
    return problems


def _judgment_problems(part: dict[str, Any]) -> list[str]:
    problems = []
    if not (isinstance(part["rubric"], str) and part["rubric"].strip()):
        problems.append('"rubric" must be non-empty text: the question to judge each event by')
    ratings = part["ratings"]
    if not (
        isinstance(ratings, dict)
        and len(ratings) >= 2
        and all(isinstance(k, str) and k.isdigit() for k in ratings)
        and all(isinstance(v, str) and v.strip() for v in ratings.values())
    ):
        problems.append(
            '"ratings" must be an object of at least two ratings, each a whole number (as text, from "0") '
            'saying what it means: {"0": "routine", "1": "a twist", ...}'
        )
    facts = part["facts"]
    if not isinstance(facts, list):
        return problems + [f'"facts" must be a list of facts to judge by: {", ".join(FACTS)}']
    names = []
    for fact in facts:
        name = fact.get("fact") if isinstance(fact, dict) else fact
        if name not in FACTS:
            problems.append(f'"facts" has {fact!r}; a fact is one of {", ".join(FACTS)}')
            continue
        names.append(name)
        if isinstance(fact, dict):
            extra = sorted(fact.keys() - {"fact", "lookback_days"})
            if extra:
                problems.append(f'"facts": {name} has no field {extra[0]!r}; it takes "fact" and "lookback_days"')
            if "lookback_days" in fact:
                if name not in HISTORY_FACTS:
                    problems.append(f'"facts": only {" and ".join(sorted(HISTORY_FACTS))} look back, not {name}')
                elif not (isinstance(fact["lookback_days"], int) and fact["lookback_days"] >= 1):
                    problems.append(f'"facts": {name}\'s "lookback_days" must be a whole number, 1 or more')
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        problems.append(f'"facts" names {", ".join(repeated)} more than once')
    return problems


def judgment_scale(part: dict[str, Any]) -> int:
    """The highest rating a judgment part's `ratings` allow: a rating of
    it scores rating / this x 100."""
    return max(int(k) for k in part["ratings"])


def fact_lookbacks(part: dict[str, Any]) -> dict[str, int]:
    """A judgment part's facts, each with its lookback in days (0 for one
    that doesn't look back)."""
    lookbacks = {}
    for fact in part["facts"]:
        name = fact.get("fact") if isinstance(fact, dict) else fact
        default = DEFAULT_LOOKBACK_DAYS if name in HISTORY_FACTS else 0
        lookbacks[name] = fact.get("lookback_days", default) if isinstance(fact, dict) else default
    return lookbacks


def person_traits_problems(spec: Any, trait_ids: Collection[str] | None = None) -> list[str]:
    """What's wrong with a person's `traits`: {"select": "all" or [trait
    ids], "parts": {trait id: [parts]}}, both optional -- the traits that
    apply to them (by default all active ones), and parts replacing a
    trait's for them alone, each list checked as a trait's own. Trait ids
    are checked against `trait_ids`, if given. Phrases follow "its
    traits"."""
    if not isinstance(spec, dict):
        return ['must be an object: {"select": "all" or [trait ids], "parts": {trait id: [parts]}}']
    problems = [
        f'has no field "{name}"; it takes "select" and "parts"' for name in sorted(spec.keys() - {"select", "parts"})
    ]
    select = spec.get("select", "all")
    if select != "all" and not (isinstance(select, list) and all(isinstance(i, str) for i in select)):
        problems.append('"select" must be "all" or a list of trait ids')
        select = "all"
    known = set(trait_ids) if trait_ids is not None else None
    if known is not None and isinstance(select, list):
        problems += [f'"select" names {i!r}, which isn\'t a trait' for i in select if i not in known]
    overrides = spec.get("parts", {})
    if not isinstance(overrides, dict):
        return problems + ['"parts" must be {trait id: [parts]}']
    for trait_id, parts in overrides.items():
        if known is not None and trait_id not in known:
            problems.append(f'"parts" names {trait_id!r}, which isn\'t a trait')
        elif isinstance(select, list) and trait_id not in select:
            problems.append(f'"parts" names {trait_id!r}, which isn\'t selected')
        elif not (isinstance(parts, list) and parts):
            problems.append(f'"parts" for {trait_id!r} must be a list of at least one part')
        else:
            for number, part in enumerate(parts, 1):
                problems += [
                    f'"parts" for {trait_id!r}: part {number}{_kind_of(part)} {p}' for p in part_problems(part)
                ]
    return problems


def activity_label(text: str) -> str:
    """An activity label as it's compared: trimmed, single-spaced,
    lowercase."""
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
