"""Judging traits: the last step of a compaction, once its events' facts
are written (see utilities/note_compactor.py).

Each of a compaction's events with facts is judged for each person it
was about -- the user ("self") and everyone there with them for a
trait's "with" judgment parts, and everyone it was done for for its
"for" parts -- against every judgment part of the traits that apply to
that person (utilities/traits.py; a person's own `traits` choose them,
and can replace a trait's parts). It's judged, too, for each of the
user's habits it's in the scope of (utilities/habits.py) -- by its
action, or its action's group -- for their "with" parts, as a person
is: a habit goes by `habit:<id>` where a person goes by their id. Each judgment is one **request**: the
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

**Asked for once a compaction is applied**, beside its final timeline,
in one compact list (`JudgmentsDue`): each event with the people it was
about and the parts to judge for each of them, then each of those parts
once -- its rubric and ratings -- and each person's recent history
(`Judging.due`), rather than every request whole, its rubric, ratings
and framing repeated in each. `Judging.requests` is what they're
checked against when they're recorded.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from calendar_clients.google_calendar import Event
from utilities.actions import Actions, ActionTree
from utilities.cancellations import of_action
from utilities.facts import SELF_ID
from utilities.habits import Habit, Habits, find as find_habit, subject_id
from utilities.locations import Locations
from utilities.people import People, Person
from utilities.traits import Trait, Traits, fact_lookbacks, judgment_scale, part_keys, part_problems

MAX_REASONING_CHARS = 300

INSTRUCTIONS = (
    "JUDGE EACH ONE YOURSELF, without asking the user: they've already confirmed the facts, and "
    "the judgments are yours to make. `events` lists each event the compaction settled with "
    "facts, each person it was about, and the parts to judge for each of them (\"<trait id>/<part "
    "key>\"). Judge each event as the applied compaction's final timeline shows it -- its times, "
    "notes, actions and facts -- for that person alone, as it was for them. A part is in `parts` "
    "under \"<trait id>/<part key>@<person id>\" if there's one (that person's own), or else "
    "\"<trait id>/<part key>\": answer its rubric -- a \"with\" part for someone who was there, a "
    "\"for\" part for someone it was done for while they weren't -- from what the timeline, the "
    "people's what_matters and `history` (what each person did and where over the days before) "
    "show, choosing one of its ratings (the number), with one succinct line of reasoning that "
    "names the fact it rests on. A habit of the user's (\"habit:<id>\") is judged like a person "
    "there, for how the event went as practice of that habit, its note in what_matters. Where "
    "they don't say, rate what they do show rather than guess -- "
    "a judgment can be redone later. Record them all with record_judgments(compaction_id, "
    "judgments), each with request_id \"<event id>/<person id>/<trait id>/<part key>\"; the "
    "compaction isn't complete until every one is. Don't show the user the judgments unless they "
    "ask."
)


BACKFILL_INSTRUCTIONS = (
    "JUDGE EACH ONE YOURSELF, without asking the user: this is a backfill of one of their habits, "
    "the judgments of its settled events -- already compacted -- since `since`. `events` lists "
    "each, with the parts to judge for the habit (\"<trait id>/<part key>\"); `parts` gives each "
    "part's rubric and ratings once (\"...@habit:<id>\" for the habit's own), and `history` what "
    "the user did and where in its scope over the days before. Judge each event as it is on the "
    "calendar -- its times, notes, actions and facts -- for how it went as practice of the habit, "
    "its note in what_matters, choosing one of the part's ratings (the number), with one succinct "
    "line of reasoning that names the fact it rests on. Record them all with "
    "record_judgments(backfill_id, judgments) -- the backfill_id where a compaction's id would "
    "go -- each with request_id \"<event id>/habit:<id>/<trait id>/<part key>\". With `current`, "
    "they're being redone: rate them afresh. Don't show the user the judgments unless they ask."
)

SCORE_DAYS = 7
"""The days of scores the app shows: a backfill reaches this far back
past the longest a habit's judgment parts average over."""

DEFAULT_WINDOW_DAYS = 30
"""How far back a judgment part averages over unless it says
(`window_days`), as the app scores it."""


@dataclass(kw_only=True)
class JudgmentPart:
    """A judgment part of a trait, given once for every judgment of it --
    see `Judging.context`."""

    key: str
    """"<trait id>/<part key>", or, for a person's own parts in place of
    the trait's, "<trait id>/<part key>@<person id>"."""

    trait_name: str
    definition: str | None
    engagement: str
    """"with" (judged for people who were there) or "for" (for people it
    was done for while they weren't)."""

    rubric: str
    ratings: dict[str, str]
    facts: list[str]
    """The facts it rests on, by name (see utilities/traits.py)."""


@dataclass(kw_only=True)
class PersonHistory:
    """What one person did and where, over the days before a compaction."""

    days: int
    actions: dict[str, int]
    """Each action's name, with how many events had it, most first."""

    locations: dict[str, int]


@dataclass(kw_only=True)
class EventJudgmentsDue:
    """The judgments one event calls for."""

    event_id: str
    summary: str | None
    start: datetime
    end: datetime
    people: dict[str, list[str]]
    """Each person it was about, with the parts to judge for them --
    "<trait id>/<part key>"."""

    current: dict[str, dict[str, dict[str, Any]]] | None = None
    """When they're being redone: the judgments already made, by person
    and then part ("<trait id>/<part key>")."""


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
    """The judgments a compaction calls for -- those not made yet, or all
    of them when they're being redone -- each part, and each person's
    history, given once (see the module docstring)."""

    compaction_id: str
    events: list[EventJudgmentsDue]
    parts: list[JudgmentPart]
    """Every part `events` name, once."""

    history: dict[str, PersonHistory]
    """By person id, for each of `events`' people whose parts look back."""

    instructions: str = INSTRUCTIONS

    @property
    def count(self) -> int:
        return sum(len(parts) for event in self.events for parts in event.people.values())


@dataclass(kw_only=True)
class HabitJudgmentsDue:
    """The judgments a backfill of one habit calls for: its settled events
    in scope since `since`, until where history ends (`until`) -- those
    not judged yet, or all of them, being redone -- each part, and its
    history, given once."""

    backfill_id: str
    """What record_judgments takes in place of a compaction's id:
    "habit:<habit id>@<since>"."""

    habit_id: str
    habit_name: str
    since: datetime
    until: datetime
    events: list[EventJudgmentsDue]
    parts: list[JudgmentPart]
    history: dict[str, PersonHistory]
    instructions: str = BACKFILL_INSTRUCTIONS

    @property
    def count(self) -> int:
        return sum(len(parts) for event in self.events for parts in event.people.values())


@dataclass(kw_only=True)
class JudgmentsResult:
    compaction_id: str
    recorded: int
    remaining: list[str] = field(default_factory=list)
    """The ids of the requests still to judge."""

    complete: bool = False
    message: str = ""


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
        habits: Habits | None = None,
        list_events: Callable[[datetime, datetime], list[Event]] | None = None,
    ) -> None:
        """`client` reads and writes the events (`get_event`/`update_event`);
        `list_events` reads their history (by default, `client`'s).
        Without `habits`, only people are judged for."""
        self._client = client
        self._actions = actions
        self._people = people
        self._locations = locations
        self._traits = traits
        self._habits = habits
        self._list_events = list_events or client.list_events

    @property
    def whole_tabs(self):
        """The tabs judging reads that compaction doesn't already (the
        Traits tab, and the Habits tab), for `SheetsClient.prefetch`."""
        return [self._traits.whole_tab, *([self._habits.whole_tab] if self._habits else [])]

    def _subjects(self) -> tuple[dict[str, Person], dict[str, str]]:
        """Everyone and everything an event can be judged for, by id --
        each person (self included, row or not), and each active habit as
        a person-like subject under `habit:<id>`, its note standing in
        for what matters to it -- and each habit's scope, its action or
        group, by that id."""
        people = {p.id: p for p in self._people.all() if p.id}
        people.setdefault(SELF_ID, Person(id=SELF_ID, name="Me"))
        scopes: dict[str, str] = {}
        for habit in self._habits.all() if self._habits else []:
            if habit.status != "active" or not habit.id or not habit.action_id:
                continue
            key = subject_id(habit.id)
            people[key] = Person(id=key, name=habit.name or habit.id, what_matters=habit.note, traits=habit.traits)
            scopes[key] = habit.action_id
        return people, scopes

    @staticmethod
    def _part_of(scopes: dict[str, str], tree: ActionTree | None) -> Callable[[Event, str], bool]:
        """Whether a subject took part in an event: the user always did; a
        habit if the event's in its scope; anyone else if it was with or
        for them."""

        def part_of(event: Event, subject: str) -> bool:
            if subject in scopes:
                return bool(of_action([event], scopes[subject], tree))
            return _with(event, subject)

        return part_of

    def requests(
        self, event_ids: list[str], span: tuple[datetime, datetime], *, include_judged: bool = False
    ) -> list[JudgmentRequest]:
        """Every judgment `event_ids`' events call for (see the module
        docstring), in event order; only those not made yet unless
        `include_judged`. `span`: when the events are, read in one listing
        with the history the judgments look back over."""
        if not event_ids:
            return []
        listed = [
            e for e in self._list_events(span[0] - timedelta(days=self.lookback()), span[1] + timedelta(seconds=1))
            if e.status != "cancelled"
        ]
        wanted = set(event_ids)
        events = [e for e in listed if e.id in wanted]
        return self.requests_for(events, [e for e in listed if e.facts is not None], include_judged=include_judged)

    def habit(self, id_or_name: str) -> Habit:
        """The active habit with this id, or else this name; ValueError if
        there's none -- only an active habit is judged."""
        habit = find_habit(self._habits.all() if self._habits else [], id_or_name)
        if habit.status != "active":
            raise ValueError(f"{habit.name!r} ({habit.id}) is {habit.status}: only an active habit is judged")
        return habit

    def backfill_days(self, habit: Habit) -> int:
        """How far back a backfill of `habit` reaches by default: the most
        days its judgment parts average over, and the days of scores
        shown."""
        traits = self._active_traits()
        windows = [
            int(part.get("window_days") or DEFAULT_WINDOW_DAYS)
            for _trait, parts in _traits_for(Person(id=subject_id(habit.id), traits=habit.traits), traits)
            for part in parts
            if _is_judgment(part)
        ]
        return max(windows, default=DEFAULT_WINDOW_DAYS) + SCORE_DAYS

    def habit_requests(
        self, habit: Habit, since: datetime, until: datetime, *, include_judged: bool = False
    ) -> list[JudgmentRequest]:
        """Every judgment `habit` calls for on its settled events in scope
        from `since` to `until` -- only those not made yet unless
        `include_judged` -- read in one listing with their history."""
        listed = [
            e for e in self._list_events(since - timedelta(days=self.lookback()), until)
            if e.status != "cancelled"
        ]
        events = [e for e in listed if since <= e.start < until]
        return self.requests_for(
            events, [e for e in listed if e.facts is not None], include_judged=include_judged,
            only=subject_id(habit.id),
        )

    def requests_for(
        self, events: list[Event], history: list[Event], *, include_judged: bool = False, only: str | None = None
    ) -> list[JudgmentRequest]:
        """Every judgment `events` call for, as `requests` -- for events
        given whole (as a plan would leave them), with the `history` the
        judgments' facts look back over -- or, `only`, just those for that
        person or habit."""
        traits = self._active_traits()
        events = [e for e in events if _judgeable(e)]
        if not events:
            return []
        people, scopes = self._subjects()
        tree = self._actions.tree() if scopes else None
        part_of = self._part_of(scopes, tree)
        names = self._names(people)
        requests = []
        for event in sorted(events, key=lambda e: e.start):
            facts = event.facts
            about = [
                (SELF_ID, "with"),
                *((p, "with") for p in facts.with_ids or ()),
                *((p, "for") for p in facts.for_ids or ()),
                # A habit is always "with": the user was there.
                *((h, "with") for h in scopes if part_of(event, h)),
            ]
            for person_id, engagement in about:
                if only is not None and person_id != only:
                    continue
                person = people.get(person_id) or Person(id=person_id, name=person_id)
                for trait, parts in _traits_for(person, traits):
                    for part, key in zip(parts, part_keys(parts)):
                        if not _is_judgment(part) or part.get("engagement_type", "with") != engagement:
                            continue
                        current = ((event.judgments or {}).get(person_id) or {}).get(trait.id, {}).get(key)
                        if current is not None and not include_judged:
                            continue
                        requests.append(
                            self._request(
                                event, person, engagement, trait, part, key, history, names, current, part_of,
                                habit=person_id in scopes,
                            )
                        )
        return requests

    def record(self, requests: list[JudgmentRequest], judgments: list[Judgment]) -> int:
        """Write `judgments` (each answering one of `requests`) onto their
        events, beside any already made. ValueError, before writing
        anything, for one that doesn't answer a request or whose rating
        isn't on its scale. Returns how many were recorded."""
        problems = check(requests, judgments)
        if problems:
            raise ValueError("; ".join(problems))
        by_id = {r.id: r for r in requests}
        for event_id in dict.fromkeys(by_id[j.request_id].event_id for j in judgments):
            existing = self._client.get_event(event_id).judgments
            self._client.update_event(Event(id=event_id, judgments=merged(existing, requests, judgments, event_id)))
        return len(judgments)

    def lookback(self) -> int:
        """The most days any active judgment part looks back."""
        traits = self._active_traits()
        return max(
            (
                days
                for person in self._subjects()[0].values()
                for _trait, parts in _traits_for(person, traits)
                for part in parts
                if _is_judgment(part)
                for days in fact_lookbacks(part).values()
            ),
            default=0,
        )

    def due(self, compaction_id: str, requests: list[JudgmentRequest]) -> JudgmentsDue:
        """`requests`, as they're asked for: by event and person, with each
        part they name once, and the history of each of their people over
        the days that person's parts look back from the first event."""
        if not requests:
            return JudgmentsDue(compaction_id=compaction_id, events=[], parts=[], history={})
        parts, history = self._parts_and_history(min(r.start for r in requests))
        people = {r.person_id for r in requests}
        named = {(r.person_id, f"{r.trait_id}/{r.part}") for r in requests}
        return JudgmentsDue(
            compaction_id=compaction_id,
            events=due_by_event(requests),
            parts=[
                p for p in parts
                if any(key == p.key or f"{key}@{person}" == p.key for person, key in named)
            ],
            history={person: h for person, h in history.items() if person in people},
        )

    def _parts_and_history(self, before: datetime) -> tuple[list[JudgmentPart], dict[str, PersonHistory]]:
        """Every judgment part of the traits that apply to anyone, each
        once -- a person's own parts in place of a trait's under a key of
        their own -- and what each person did and where over the days
        their parts look back from `before` (one listing)."""
        traits = self._active_traits()
        subjects, scopes = self._subjects()
        people = list(subjects.values())
        part_of = self._part_of(scopes, self._actions.tree() if scopes else None)
        names = self._names(subjects)
        parts: dict[str, JudgmentPart] = {}
        looks: dict[str, int] = {}
        for person in people:
            spec = person.traits if isinstance(person.traits, dict) else {}
            overrides = spec.get("parts") if isinstance(spec.get("parts"), dict) else {}
            for trait, trait_parts in _traits_for(person, traits):
                own = bool(overrides.get(trait.id))
                for part, key in zip(trait_parts, part_keys(trait_parts)):
                    if not _is_judgment(part):
                        continue
                    lookbacks = fact_lookbacks(part)
                    looks[person.id] = max([looks.get(person.id, 0), *lookbacks.values()])
                    name = f"{trait.id}/{key}" + (f"@{person.id}" if own else "")
                    parts.setdefault(
                        name,
                        JudgmentPart(
                            key=name,
                            trait_name=trait.name or trait.id,
                            definition=trait.definition,
                            engagement=part.get("engagement_type", "with"),
                            rubric=part["rubric"],
                            ratings=dict(part["ratings"]),
                            facts=list(lookbacks),
                        ),
                    )
        history: dict[str, PersonHistory] = {}
        longest = max(looks.values(), default=0)
        if longest:
            listed = [
                e for e in self._list_events(before - timedelta(days=longest), before)
                if e.status != "cancelled" and e.facts is not None and e.start < before
            ]
            for person_id, days in looks.items():
                if not days:
                    continue
                past = [e for e in listed if e.start >= before - timedelta(days=days) and part_of(e, person_id)]
                actions = Counter(names.get(a, a) for e in past for a in e.action_ids or ())
                locations = Counter(
                    names.get(e.facts.location_id, e.facts.location_id) for e in past if e.facts.location_id
                )
                history[person_id] = PersonHistory(
                    days=days, actions=dict(actions.most_common()), locations=dict(locations.most_common())
                )
        return list(parts.values()), history

    def _active_traits(self) -> list[Trait]:
        return [t for t in self._traits.all() if t.status == "active" and t.id]

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
        part_of: Callable[[Event, str], bool],
        *,
        habit: bool = False,
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
                # wasn't there. So are a habit's: it's the user's.
                about = person.id if engagement == "with" and not habit else SELF_ID
                resolved[fact] = (facts.notes or {}).get(about)
            elif fact == "what_matters":
                resolved[fact] = person.what_matters
            else:  # action_history, location_history
                since = event.start - timedelta(days=days)
                past = [
                    e for e in history
                    if since <= e.start < event.start and e.id != event.id and part_of(e, person.id)
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
            f"the user, as practice of their habit \"{person.name}\"" if habit
            else f"{who}, who was there" if engagement == "with"
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


def due_by_event(requests: list[JudgmentRequest]) -> list[EventJudgmentsDue]:
    """`requests`, grouped by event and then person, each as just its
    part -- what a dry run says is due (see the module docstring)."""
    due: dict[str, EventJudgmentsDue] = {}
    for request in requests:
        event = due.setdefault(
            request.event_id,
            EventJudgmentsDue(
                event_id=request.event_id, summary=request.summary, start=request.start, end=request.end, people={}
            ),
        )
        key = f"{request.trait_id}/{request.part}"
        event.people.setdefault(request.person_id, []).append(key)
        if request.current is not None:
            event.current = event.current or {}
            event.current.setdefault(request.person_id, {})[key] = {
                k: v for k, v in request.current.items() if k in ("rating", "reasoning")
            }
    return list(due.values())


def check(requests: list[JudgmentRequest], judgments: list[Judgment]) -> list[str]:
    """Everything wrong with `judgments` as answers to `requests`: one
    that answers none of them, or twice, a rating off its scale, missing
    or overlong reasoning."""
    by_id = {r.id: r for r in requests}
    problems = []
    answered: set[str] = set()
    for judgment in judgments:
        request = by_id.get(judgment.request_id)
        if request is None:
            problems.append(f"{judgment.request_id!r} isn't one of the compaction's judgment requests")
        elif judgment.request_id in answered:
            problems.append(f"{judgment.request_id}: judged more than once")
        elif str(judgment.rating) not in request.ratings:
            problems.append(
                f"{judgment.request_id}: the rating {judgment.rating} isn't one of its ratings "
                f"({', '.join(request.ratings)})"
            )
        elif not judgment.reasoning.strip():
            problems.append(f"{judgment.request_id}: give a line of reasoning")
        elif len(judgment.reasoning) > MAX_REASONING_CHARS:
            problems.append(f"{judgment.request_id}: keep the reasoning to one line, {MAX_REASONING_CHARS} characters")
        answered.add(judgment.request_id)
    return problems


def merged(
    existing: dict[str, Any] | None,
    requests: list[JudgmentRequest],
    judgments: list[Judgment],
    event_id: str,
) -> dict[str, Any]:
    """`existing` (an event's judgments) with those of `judgments` about
    `event_id` added -- each kept by person, trait and part, with the
    scale it was rated on."""
    by_id = {r.id: r for r in requests}
    result = dict(existing or {})
    for judgment in judgments:
        request = by_id[judgment.request_id]
        if request.event_id != event_id:
            continue
        person_id = request.person_id
        person = dict(result.get(person_id) or {})
        trait = dict(person.get(request.trait_id) or {})
        trait[request.part] = {
            "rating": judgment.rating,
            "scale": max(int(k) for k in request.ratings),
            "reasoning": " ".join(judgment.reasoning.split()),
        }
        person[request.trait_id] = trait
        result[person_id] = person
    return result


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
