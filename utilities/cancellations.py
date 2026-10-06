"""Cancellations: the events the user cancelled -- said didn't happen, or
dropped on purpose -- that count against someone's follow-through (a
part of their traits; the client scores it).

**What's recorded.** Only a deliberate cancellation: a compaction's
`cancel` decision ("it didn't happen"), once its day is applied (see
utilities/note_compactor.py), or `delete_event` asked to count it. A
merge, a cancel that says it doesn't count, a deleted series, or a plan
changed by hand isn't one. And only for the people a follow-through part
of their traits matches, as the event was planned: for a part with the
"with" engagement, the user and everyone the event's facts have there
(`with_ids`); for "for", everyone it was for (`for_ids`); and either way,
only an event of the part's `action`, if it names one. A cancellation no
part matches isn't recorded at all.

**Where they live.** The **Cancellations** tab of the calendar's metadata
spreadsheet, one row per cancelled event per person (and engagement),
read by header name (utilities/row_sheet.py) -- so a row recorded by
mistake can be deleted by hand. Rows older than `KEEP_DAYS` are dropped
as new ones are written.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from calendar_clients.google_calendar import Event
from calendar_clients.google_sheets import SheetsClient, TabRange
from utilities import calendar_metadata_sheet
from utilities.actions import Actions, ActionTree
from utilities.facts import SELF_ID, Facts
from utilities.people import CancelledEvent, People, Person
from utilities.row_sheet import RowSheet
from utilities.traits import Trait, Traits, part_keys, part_problems

KEEP_DAYS = 400 + 90
"""How long a cancellation is kept: 400 days of scores, and a generous
follow-through look-back before the oldest of them."""


@dataclass(kw_only=True)
class Cancellation:
    """One person's record of one event the user cancelled -- see the
    module docstring."""

    id: str | None = None
    """"<event id>/<person id>/<engagement>"."""

    event_id: str | None = None
    person_id: str | None = None
    engagement: str | None = None
    """"with" (they were to be there) or "for" (it was to be done for
    them while they weren't)."""

    summary: str | None = None
    start: str | None = None
    end: str | None = None
    """When it was planned, ISO 8601."""

    action_ids: list[str] | None = None
    parts: list[str] | None = None
    """The follow-through parts it counted against when it was recorded,
    "<trait id>/<part key>"."""

    cancelled_at: str | None = None
    source: str | None = None
    """What cancelled it: "compaction <id>", or "delete_event"."""

    def to_event(self) -> Event:
        """The event as follow-through reads it: when it was planned, its actions, and its person
        where their engagement looks for them."""
        facts = (
            Facts(for_ids=[self.person_id]) if self.engagement == "for"
            else Facts(with_ids=[self.person_id]) if self.person_id != SELF_ID
            else Facts()
        )
        return Event(
            id=self.event_id,
            summary=self.summary,
            start=datetime.fromisoformat(self.start),
            end=datetime.fromisoformat(self.end),
            action_ids=list(self.action_ids or []),
            facts=facts,
            status="cancelled",
        )


@dataclass(kw_only=True)
class FollowThroughMatch:
    """A person a cancelled event counts against, and the follow-through
    parts it counts against them under."""

    person_id: str
    person_name: str
    engagement: str
    parts: list[str]
    """"<trait id>/<part key>"."""

    trait_names: list[str]


class Cancellations:
    """A calendar's recorded cancellations -- see the module docstring."""

    def __init__(self, sheet: RowSheet[Cancellation], people: People, traits: Traits, actions: Actions) -> None:
        self._sheet = sheet
        self._people = people
        self._traits = traits
        self._actions = actions

    @staticmethod
    def ensure(
        sheets_client: SheetsClient, spreadsheet_id: str, people: People, traits: Traits, actions: Actions
    ) -> "Cancellations":
        """The calendar's cancellations, adding the Cancellations tab the
        first time."""
        sheet = RowSheet.ensure(
            sheets_client,
            spreadsheet_id,
            role=calendar_metadata_sheet.CANCELLATIONS_SHEET_ROLE,
            title=calendar_metadata_sheet.CANCELLATIONS_SHEET_TITLE,
            row_type=Cancellation,
            required=("id", "person_id"),
        )
        return Cancellations(sheet, people, traits, actions)

    @property
    def whole_tabs(self) -> list[TabRange]:
        """Every tab matching and recording read, for `SheetsClient.prefetch`."""
        return [self._sheet.whole_tab, self._traits.whole_tab, *self._people.whole_tabs, *self._actions.whole_tabs]

    def prefetch(self, ranges: list[TabRange]) -> None:
        self._sheet.prefetch(ranges)

    def all(self) -> list[Cancellation]:
        return self._sheet.read()

    def by_person(self) -> dict[str, list[CancelledEvent]]:
        """Each person's recorded cancellations, newest first."""
        found: dict[str, list[CancelledEvent]] = {}
        for row in sorted(self.all(), key=_start, reverse=True):
            if row.person_id:
                found.setdefault(row.person_id, []).append(
                    CancelledEvent(
                        event_id=row.event_id,
                        summary=row.summary,
                        start=_time(row.start),
                        end=_time(row.end),
                        action_ids=row.action_ids,
                        engagement=row.engagement,
                        parts=row.parts,
                        cancelled_at=_time(row.cancelled_at),
                        source=row.source,
                    )
                )
        return found

    def matches(self, event: Event) -> list[FollowThroughMatch]:
        """Who `event`, cancelled, counts against: each active person with
        a follow-through part it matches (see the module docstring), and
        those parts."""
        traits = self._traits.all()
        tree = self._actions.tree()
        facts = event.facts or Facts()
        found = []
        for person in self._people.get_people():
            by_engagement: dict[str, tuple[list[str], list[str]]] = {}
            for trait, parts in traits_for(person, traits):
                for part, key in zip(parts, part_keys(parts)):
                    if not isinstance(part, dict) or part.get("kind") != "follow_through" or part_problems(part):
                        continue
                    engagement = part.get("engagement_type", "with")
                    there = (
                        person.id == SELF_ID or person.id in (facts.with_ids or ())
                        if engagement == "with"
                        else person.id in (facts.for_ids or ())
                    )
                    if not there or not of_action([event], part.get("action"), tree):
                        continue
                    keys, names = by_engagement.setdefault(engagement, ([], []))
                    keys.append(f"{trait.id}/{key}")
                    if (trait.name or trait.id) not in names:
                        names.append(trait.name or trait.id)
            found += [
                FollowThroughMatch(
                    person_id=person.id, person_name=person.name or person.id, engagement=engagement,
                    parts=keys, trait_names=names,
                )
                for engagement, (keys, names) in by_engagement.items()
            ]
        return found

    def record(self, event: Event, source: str, at: datetime | None = None) -> list[Cancellation]:
        """Record `event` (as it was planned, before it was cancelled) as
        cancelled for everyone it counts against (`matches`) -- again,
        harmlessly, if it already is. Returns the rows written."""
        at = at or datetime.now(timezone.utc)
        rows = [
            Cancellation(
                id=f"{event.id}/{match.person_id}/{match.engagement}",
                event_id=event.id,
                person_id=match.person_id,
                engagement=match.engagement,
                summary=event.summary,
                start=event.start.isoformat(),
                end=event.end.isoformat(),
                action_ids=list(event.action_ids or []),
                parts=match.parts,
                cancelled_at=at.isoformat(),
                source=source,
            )
            for match in self.matches(event)
        ]
        if not rows:
            return []
        written = {r.id for r in rows}
        oldest = at - timedelta(days=KEEP_DAYS)
        kept = [r for r in self.all() if r.id not in written and _start(r) >= oldest]
        self._sheet.write(sorted(kept + rows, key=lambda r: (r.start or "", r.id or "")))
        return rows


def _time(text: str | None) -> datetime | None:
    """`text` as a time, or `None` if it's missing or can't be read (a hand
    edit)."""
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def _start(row: Cancellation) -> datetime:
    try:
        return datetime.fromisoformat(row.start)
    except (TypeError, ValueError):
        # A hand-edited row that can't be read: kept, rather than lost.
        return datetime.max.replace(tzinfo=timezone.utc)


def traits_for(person: Person, traits: list[Trait]) -> list[tuple[Trait, list[dict[str, Any]]]]:
    """The active traits that apply to `person` -- those their `traits`
    select, by default all -- each with its parts for them (their own, if
    they replace the trait's)."""
    active = [t for t in traits if t.status == "active" and t.id]
    spec = person.traits if isinstance(person.traits, dict) else {}
    select = spec.get("select", "all")
    overrides = spec.get("parts") if isinstance(spec.get("parts"), dict) else {}
    chosen = active if select == "all" else [t for t in active if t.id in (select if isinstance(select, list) else [])]
    return [(t, overrides.get(t.id) or t.parts or []) for t in chosen]


def of_action(events: list[Event], action_id: str | None, tree: ActionTree | None) -> list[Event]:
    """`events` of the action `action_id` -- or of any action in the group
    it names -- or all of them without one."""
    if not action_id:
        return events

    def matches(candidate: str) -> bool:
        if candidate == action_id:
            return True
        action = tree.by_id.get(candidate) if tree is not None else None
        return action is not None and any(getattr(g, "id", None) == action_id for g in tree.groups.chain(action)[1:])

    return [e for e in events if any(matches(a) for a in e.action_ids or ())]
