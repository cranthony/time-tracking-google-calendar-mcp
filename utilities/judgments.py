"""Judging traits: the last step of a compaction, once its events' facts
are written (see utilities/note_compactor.py).

Each of a compaction's events with facts is judged for each person it
was about -- the user ("self") and everyone there with them for a
trait's "with" judgment parts, and everyone it was done for for its
"for" parts -- against every judgment part of the traits that apply to
that person (utilities/traits.py; a person's own `traits` choose them,
and can replace a trait's parts). Each judgment is one **request**: the
rubric, the scale of ratings, and the facts the part names, resolved
for that event and person -- its actions, where it was, their history
together over the part's lookback, the event's notes, the notes on the
person, what matters to them -- with the framing the assistant judges it
in.

The assistant that drives the MCP tools makes every judgment itself,
without asking the user: a rating from the scale and one succinct line
of reasoning each. They're kept on the event (`Event.judgments`), by
person, trait and part, with the scale they were rated on, so a score
(rating / scale) can be worked out from them later. Nothing rolls them
up yet. A compaction isn't complete until every request is judged; a
judgment can be redone at any time, with different context, or a rating
simply overwritten by hand (`update_event`).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from calendar_clients.google_calendar import Event
from utilities.actions import Actions
from utilities.facts import SELF_ID
from utilities.locations import Locations
from utilities.people import People, Person
from utilities.traits import Trait, Traits, fact_lookbacks, judgment_scale, part_keys, part_problems

MAX_REASONING_CHARS = 300

INSTRUCTIONS = (
    "JUDGE EACH REQUEST YOURSELF, without asking the user: they've already confirmed the facts, "
    "and the judgments are yours to make. Each request is about one event and one person -- the "
    "user themself (\"self\") or someone else -- and one judgment of a trait: read its `framing`, "
    "then answer its `rubric` for that person alone, as the event was for them, choosing one of "
    "its `ratings` (the number), with one succinct line of reasoning that names the fact it rests "
    "on. Use only the `facts` given; where they don't say, rate what they do show rather than "
    "guess -- a judgment can be redone later with more context. Record them all with "
    "record_judgments(compaction_id, judgments); the compaction isn't complete until every request "
    "is judged. Don't show the user the judgments unless they ask."
)


@dataclass(kw_only=True)
class JudgmentRequest:
    """One judgment to make -- see the module docstring."""

    id: str
    """"<event id>/<person id>/<trait id>/<part key>"."""

    event_id: str
    summary: str | None
    start: datetime
    end: datetime
    person_id: str
    person_name: str
    trait_id: str
    trait_name: str
    definition: str | None
    part: str
    """The judgment part's key within its trait ("judgment", "judgment#2")."""

    engagement: str
    """"with" (they were there) or "for" (it was done for them while they
    weren't)."""

    rubric: str
    ratings: dict[str, str]
    facts: dict[str, Any]
    """The facts the part names, resolved for this event and person."""

    framing: str
    """How to judge it, in a sentence or two."""

    current: dict[str, Any] | None = None
    """The judgment already made, if it's being redone."""


@dataclass(kw_only=True)
class Judgment:
    """A judgment made: the request's id, a rating from its ratings, and
    one succinct line of reasoning."""

    request_id: str
    rating: int
    reasoning: str


@dataclass(kw_only=True)
class JudgmentsDue:
    compaction_id: str
    requests: list[JudgmentRequest]
    """Every judgment the compaction's events call for -- those not made
    yet, or all of them when they're being redone."""

    instructions: str = INSTRUCTIONS


@dataclass(kw_only=True)
class JudgmentsResult:
    compaction_id: str
    recorded: int
    remaining: list[str] = field(default_factory=list)
    """The ids of the requests still to judge."""

    complete: bool = False
    message: str = ""
    scored_days: list[str] | None = None
    """Once complete: the days whose trait scores it rolled up (see
    utilities/trait_rollup.py)."""


class Judging:
    """Makes a compaction's judgment requests, and records the judgments
    on its events."""

    def __init__(
        self,
        *,
        client,
        actions: Actions,
        people: People,
        locations: Locations,
        traits: Traits,
        list_events: Callable[[datetime, datetime], list[Event]] | None = None,
    ) -> None:
        """`client` reads and writes the events (`get_event`/`update_event`);
        `list_events` reads their history (by default, `client`'s)."""
        self._client = client
        self._actions = actions
        self._people = people
        self._locations = locations
        self._traits = traits
        self._list_events = list_events or client.list_events

    @property
    def whole_tabs(self):
        """The tabs judging reads that compaction doesn't already (the
        Traits tab), for `SheetsClient.prefetch`."""
        return [self._traits.whole_tab]

    def requests(
        self, event_ids: list[str], span: tuple[datetime, datetime], *, include_judged: bool = False
    ) -> list[JudgmentRequest]:
        """Every judgment `event_ids`' events call for (see the module
        docstring), in event order; only those not made yet unless
        `include_judged`. `span`: when the events are, read in one listing
        with the history the judgments look back over."""
        if not event_ids:
            return []
        traits = [t for t in self._traits.all() if t.status == "active" and t.id]
        people = {p.id: p for p in self._people.all() if p.id}
        lookback = max(
            (
                days
                for person in people.values()
                for _trait, parts in _traits_for(person, traits)
                for part in parts
                if _is_judgment(part)
                for days in fact_lookbacks(part).values()
            ),
            default=0,
        )
        listed = [
            e for e in self._list_events(span[0] - timedelta(days=lookback), span[1] + timedelta(seconds=1))
            if e.status != "cancelled"
        ]
        wanted = set(event_ids)
        events = [e for e in listed if e.id in wanted and _judgeable(e)]
        if not events:
            return []
        names = self._names(people)
        history = [e for e in listed if e.facts is not None]
        requests = []
        for event in sorted(events, key=lambda e: e.start):
            facts = event.facts
            about = [(SELF_ID, "with"), *((p, "with") for p in facts.with_ids or ()), *((p, "for") for p in facts.for_ids or ())]
            for person_id, engagement in about:
                person = people.get(person_id) or Person(id=person_id, name=person_id)
                for trait, parts in _traits_for(person, traits):
                    for part, key in zip(parts, part_keys(parts)):
                        if not _is_judgment(part) or part.get("engagement_type", "with") != engagement:
                            continue
                        current = ((event.judgments or {}).get(person_id) or {}).get(trait.id, {}).get(key)
                        if current is not None and not include_judged:
                            continue
                        requests.append(
                            self._request(event, person, engagement, trait, part, key, history, names, current)
                        )
        return requests

    def record(self, requests: list[JudgmentRequest], judgments: list[Judgment]) -> int:
        """Write `judgments` (each answering one of `requests`) onto their
        events, beside any already made. ValueError, before writing
        anything, for one that doesn't answer a request or whose rating
        isn't on its scale. Returns how many were recorded."""
        by_id = {r.id: r for r in requests}
        problems = []
        for judgment in judgments:
            request = by_id.get(judgment.request_id)
            if request is None:
                problems.append(f"{judgment.request_id!r} isn't one of the compaction's judgment requests")
            elif str(judgment.rating) not in request.ratings:
                problems.append(
                    f"{judgment.request_id}: the rating {judgment.rating} isn't one of its ratings "
                    f"({', '.join(request.ratings)})"
                )
            elif not judgment.reasoning.strip():
                problems.append(f"{judgment.request_id}: give a line of reasoning")
            elif len(judgment.reasoning) > MAX_REASONING_CHARS:
                problems.append(f"{judgment.request_id}: keep the reasoning to one line, {MAX_REASONING_CHARS} characters")
        if problems:
            raise ValueError("; ".join(problems))
        for event_id in dict.fromkeys(by_id[j.request_id].event_id for j in judgments):
            merged = dict(self._client.get_event(event_id).judgments or {})
            for judgment in judgments:
                request = by_id[judgment.request_id]
                if request.event_id != event_id:
                    continue
                person = dict(merged.get(request.person_id) or {})
                trait = dict(person.get(request.trait_id) or {})
                trait[request.part] = {
                    "rating": judgment.rating,
                    "scale": max(int(k) for k in request.ratings),
                    "reasoning": " ".join(judgment.reasoning.split()),
                }
                person[request.trait_id] = trait
                merged[request.person_id] = person
            self._client.update_event(Event(id=event_id, judgments=merged))
        return len(judgments)

    def _names(self, people: dict[str, Person]) -> dict[str, str]:
        names = {i: p.name or i for i, p in people.items()}
        names.update((a.id, a.name or a.id) for a in self._actions.all() if a.id)
        names.update((loc.id, loc.name or loc.id) for loc in self._locations.all() if loc.id)
        return names

    def _request(
        self,
        event: Event,
        person: Person,
        engagement: str,
        trait: Trait,
        part: dict[str, Any],
        key: str,
        history: list[Event],
        names: dict[str, str],
        current: dict[str, Any] | None,
    ) -> JudgmentRequest:
        facts = event.facts
        resolved: dict[str, Any] = {}
        for fact, days in fact_lookbacks(part).items():
            if fact == "action":
                resolved[fact] = [names.get(a, a) for a in event.action_ids or ()]
            elif fact == "location":
                resolved[fact] = names.get(facts.location_id, facts.location_id) if facts.location_id else None
            elif fact == "general_notes":
                resolved[fact] = event.description
            elif fact == "person_notes":
                # A "for" engagement's notes are the user's own: the person
                # wasn't there.
                about = person.id if engagement == "with" else SELF_ID
                resolved[fact] = (facts.notes or {}).get(about)
            elif fact == "what_matters":
                resolved[fact] = person.what_matters
            else:  # action_history, location_history
                since = event.start - timedelta(days=days)
                past = [
                    e for e in history
                    if since <= e.start < event.start and e.id != event.id and _with(e, person.id)
                ]
                if fact == "action_history":
                    counted = Counter(names.get(a, a) for e in past for a in e.action_ids or ())
                else:
                    counted = Counter(names.get(e.facts.location_id, e.facts.location_id) for e in past if e.facts.location_id)
                resolved[fact] = {
                    "days": days,
                    "times": dict(counted.most_common()),
                }
        who = "the user themself" if person.id == SELF_ID else f"{person.name} (one person{', ' + person.context if person.context else ''})"
        how = (
            f"{who}, who was there" if engagement == "with"
            else f"{who}, for whom the user did this while they weren't there"
        )
        return JudgmentRequest(
            id=f"{event.id}/{person.id}/{trait.id}/{key}",
            event_id=event.id,
            summary=event.summary,
            start=event.start,
            end=event.end,
            person_id=person.id,
            person_name=person.name or person.id,
            trait_id=trait.id,
            trait_name=trait.name or trait.id,
            definition=trait.definition,
            part=key,
            engagement=engagement,
            rubric=part["rubric"],
            ratings=dict(part["ratings"]),
            facts=resolved,
            framing=(
                f"For the trait {trait.name}{f' ({trait.definition})' if trait.definition else ''}, judge how "
                f"\"{event.summary}\" went for {how}: {part['rubric']} Rate it 0-{judgment_scale(part)} from the "
                "ratings, on your own, with one succinct line of reasoning."
            ),
            current=current,
        )


def _judgeable(event: Event) -> bool:
    return event.status != "cancelled" and event.facts is not None and not event.facts.is_empty()


def _is_judgment(part: Any) -> bool:
    return isinstance(part, dict) and part.get("kind") == "judgment" and not part_problems(part)


def _with(event: Event, person_id: str) -> bool:
    """Whether `person_id` took part in `event`: the user always did; anyone
    else if it was with or for them."""
    if person_id == SELF_ID:
        return True
    facts = event.facts
    return person_id in (facts.with_ids or ()) or person_id in (facts.for_ids or ())


def _traits_for(person: Person, traits: list[Trait]) -> list[tuple[Trait, list[dict[str, Any]]]]:
    """The traits that apply to `person` -- those their `traits` select, by
    default every active one -- each with its parts for them."""
    spec = person.traits if isinstance(person.traits, dict) else {}
    select = spec.get("select", "all")
    overrides = spec.get("parts") if isinstance(spec.get("parts"), dict) else {}
    chosen = traits if select == "all" else [t for t in traits if t.id in (select if isinstance(select, list) else [])]
    return [(t, overrides.get(t.id) or t.parts or []) for t in chosen]
