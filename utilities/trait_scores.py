"""Scoring a person's traits for a day, from their events: each part 0-100,
each trait the weighted mean of its parts (leaving out any with nothing to
rate it by), over a trailing window that ends with the day.

**A person's events.** The user ("self") was at every event. Anyone else's
"with" events are those whose facts name them in `with_ids`; their "for"
events, those naming them in `for_ids` (done for them while they weren't
there). A part reads one or the other by its `engagement_type`. Planned
events are tagged with their people and actions ahead of time, so a
cancelled event still says whom it was planned with -- which is what
follow-through counts.

| kind             | scores                                                  |
| ---------------- | ------------------------------------------------------- |
| `judgment`       | the mean of its judgments (rating / scale) on the       |
|                  | person's events in the last `window_days` (default 30)  |
| `continuity`     | 100 if the last event ended within `last_within_days`   |
|                  | of the day's end and the next starts within             |
|                  | `next_within_days` after it; 50 for one; 0 for neither  |
| `count`          | events over `interval_days` (default 30) against        |
|                  | `target`: 100 while it's met; short of it, in           |
|                  | proportion -- or, with `zero_at_days`, falling from 100 |
|                  | when it was last met to 0 by `zero_at_days`             |
| `duration`       | the same with minutes, against `target_min`             |
| `follow_through` | a running score over `look_back_days` (default 30):     |
|                  | from 100, each day loses `penalty` (25) per cancelled   |
|                  | event and regains `recovery` (25) if one was kept       |

`continuity`, `count`, `duration` and `follow_through` with an `action`
count only events of that action -- or, for an action group, of any
action in it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from calendar_clients.google_calendar import Event
from utilities.actions import ActionTree
from utilities.facts import SELF_ID
from utilities.people import Person
from utilities.traits import (
    DEFAULT_TARGET,
    DEFAULT_WINDOW_DAYS,
    DEFAULT_WITHIN_DAYS,
    Trait,
    part_keys,
    part_problems,
)

FOLLOW_THROUGH_PENALTY = 25
FOLLOW_THROUGH_RECOVERY = 25
FOLLOW_THROUGH_LOOK_BACK_DAYS = 30


@dataclass(kw_only=True)
class PartScore:
    key: str
    """The part's name within its trait ("judgment", "count#2")."""

    kind: str
    weight: float
    score: int | None
    """0-100; `None` with nothing to rate it by."""

    said: str
    """How it was reached, in a line."""


@dataclass(kw_only=True)
class TraitScore:
    trait_id: str
    score: int | None
    """The weighted mean of its parts' scores; `None` if none has one."""

    parts: list[PartScore] = field(default_factory=list)


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


def reach(parts: list[dict[str, Any]]) -> tuple[timedelta, timedelta]:
    """How far before a day's end and after it `parts` read events."""
    back, ahead = timedelta(days=DEFAULT_WINDOW_DAYS), timedelta()
    for part in parts:
        if part_problems(part):
            continue
        kind = part["kind"]
        if kind == "judgment":
            days = part.get("window_days", DEFAULT_WINDOW_DAYS)
        elif kind == "continuity":
            days = part.get("last_within_days", DEFAULT_WITHIN_DAYS)
            ahead = max(ahead, timedelta(days=part.get("next_within_days", DEFAULT_WITHIN_DAYS)))
        elif kind in ("count", "duration"):
            days = max(part.get("interval_days", DEFAULT_WINDOW_DAYS), part.get("zero_at_days", 0))
        else:  # follow_through
            days = part.get("look_back_days", FOLLOW_THROUGH_LOOK_BACK_DAYS)
        back = max(back, timedelta(days=days))
    return back, ahead


def score_person(
    person: Person,
    traits: list[Trait],
    day: tuple[datetime, datetime],
    events: list[Event],
    cancelled: list[Event],
    tree: ActionTree | None = None,
) -> list[TraitScore]:
    """`person`'s score of each trait that applies to them, for the day
    `day` (its start and end), from `events` (kept) and `cancelled` -- see
    the module docstring. `tree` resolves an action group named by a
    part's `action`."""
    scored = []
    for trait, parts in traits_for(person, traits):
        part_scores = [
            _part(person, trait.id, part, key, day, events, cancelled, tree)
            for part, key in zip(parts, part_keys(parts))
        ]
        scored.append(
            TraitScore(
                trait_id=trait.id,
                score=_weighted_mean([(p.score, p.weight) for p in part_scores]),
                parts=part_scores,
            )
        )
    return scored


def _engaged(person_id: str, engagement: str, events: list[Event]) -> list[Event]:
    """The events `person_id` took part in by `engagement` -- see the module
    docstring."""
    if engagement == "with" and person_id == SELF_ID:
        return list(events)
    if engagement == "with":
        return [e for e in events if e.facts is not None and person_id in (e.facts.with_ids or ())]
    return [e for e in events if e.facts is not None and person_id in (e.facts.for_ids or ())]


def _of_action(events: list[Event], action_id: str | None, tree: ActionTree | None) -> list[Event]:
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


def _part(
    person: Person,
    trait_id: str,
    part: Any,
    key: str,
    day: tuple[datetime, datetime],
    events: list[Event],
    cancelled: list[Event],
    tree: ActionTree | None,
) -> PartScore:
    problems = part_problems(part)
    if problems:
        kind = part.get("kind") if isinstance(part, dict) and isinstance(part.get("kind"), str) else "?"
        return PartScore(key=key, kind=kind, weight=0, score=None, said=f"Not scored: it {problems[0]}")
    kind = part["kind"]
    weight = part.get("weight", 1)
    end = day[1]
    engaged = _engaged(person.id, part.get("engagement_type", "with"), events)

    def score(value: int | None, said: str) -> PartScore:
        return PartScore(key=key, kind=kind, weight=weight, score=value, said=said)

    if kind == "judgment":
        days = part.get("window_days", DEFAULT_WINDOW_DAYS)
        ratings = [
            j["rating"] / j["scale"]
            for e in engaged
            if end - timedelta(days=days) <= e.start < end
            for j in [(((e.judgments or {}).get(person.id) or {}).get(trait_id) or {}).get(key)]
            if isinstance(j, dict) and isinstance(j.get("rating"), (int, float)) and j.get("scale")
        ]
        if not ratings:
            return score(None, f"No judgments in the last {days:g} days")
        mean = sum(ratings) / len(ratings)
        return score(round(100 * mean), f"Mean of {len(ratings)} judgment(s) in the last {days:g} days")
    engaged = _of_action(engaged, part.get("action"), tree)
    if kind == "continuity":
        last_days = part.get("last_within_days", DEFAULT_WITHIN_DAYS)
        next_days = part.get("next_within_days", DEFAULT_WITHIN_DAYS)
        last = max((e for e in engaged if e.start < end), key=lambda e: e.end, default=None)
        upcoming = min((e for e in engaged if e.start >= end), key=lambda e: e.start, default=None)
        last_ok = last is not None and end - last.end <= timedelta(days=last_days)
        next_ok = upcoming is not None and upcoming.start - end <= timedelta(days=next_days)
        said = "; ".join([
            f"last {_ago(end - last.end)} ago" if last is not None else "none before",
            f"next in {_ago(upcoming.start - end)}" if upcoming is not None else "none planned",
        ])
        return score(50 * last_ok + 50 * next_ok, f"{said[0].upper()}{said[1:]} (within {last_days:g} and {next_days:g} days)")
    if kind in ("count", "duration"):
        rating, said = _over_interval(part, kind, end, engaged)
        return score(rating, said)
    rating, said = _follow_through(part, day, engaged, _engaged(person.id, part.get("engagement_type", "with"), cancelled), tree)
    return score(rating, said)


def _over_interval(part: dict[str, Any], kind: str, end: datetime, events: list[Event]) -> tuple[int, str]:
    """A count or duration part's score -- see the module docstring."""
    interval_days = part.get("interval_days", DEFAULT_WINDOW_DAYS)
    interval = timedelta(days=interval_days)
    if kind == "duration":
        target = part["target_min"]

        def value(at: datetime) -> float:
            return _minutes_in(events, at - interval, at)

        amount = round(value(end))
        said = f"{amount} of {target:g} minutes"
    else:
        target = part.get("target", DEFAULT_TARGET)

        def value(at: datetime) -> float:
            return sum(1 for e in events if e.start < at and e.end > at - interval)

        amount = round(value(end))
        said = f"{amount} of {target:g} {part.get('noun', 'events')}"
    within = f"the last {interval_days:g} days"
    zero_at_days = part.get("zero_at_days")
    if zero_at_days is None or amount >= target:
        return _capped(amount, target), f"{said} in {within}"
    met = _last_met(events, value, end, interval, timedelta(days=zero_at_days), target, continuous=kind == "duration")
    grace = timedelta(days=zero_at_days - interval_days)
    if met is None:
        return 0, f"{said} in {within}; not met in the last {zero_at_days:g} days"
    lapsed = end - met
    rating = max(0, min(100, round(100 * (1 - lapsed / grace))))
    return rating, f"{said} in {within}; met until {_ago(lapsed)} ago, 0 after {zero_at_days:g} days"


def _follow_through(
    part: dict[str, Any],
    day: tuple[datetime, datetime],
    kept: list[Event],
    cancelled: list[Event],
    tree: ActionTree | None,
) -> tuple[int, str]:
    """A follow-through part's running score -- see the module docstring. A
    cancelled event overlapped by a kept one (merged into it, say) isn't
    counted."""
    penalty = part.get("penalty", FOLLOW_THROUGH_PENALTY)
    recovery = part.get("recovery", FOLLOW_THROUGH_RECOVERY)
    look_back_days = part.get("look_back_days", FOLLOW_THROUGH_LOOK_BACK_DAYS)
    dropped = [e for e in _of_action(cancelled, part.get("action"), tree) if not any(_overlap(k, e) for k in kept)]
    start, end = day
    days = [(start - timedelta(days=back), start - timedelta(days=back - 1)) for back in range(look_back_days - 1, 0, -1)]
    days.append((start, end))
    score = before = 100.0
    lost = gained = 0
    for day_start, day_end in days:
        before = score
        lost = sum(1 for e in dropped if day_start <= e.start < day_end)
        gained = sum(1 for e in kept if day_start <= e.start < day_end)
        score = max(0.0, min(100.0, score - penalty * lost + (recovery if gained else 0)))
    said = ", ".join(
        p for p in (f"{lost} cancelled (−{penalty * lost:g})" if lost else "", f"{gained} kept (+{recovery:g})" if gained else "") if p
    ) or "Nothing cancelled or kept"
    return round(score), f"{said} that day, from {round(before)}"


def _last_met(
    events: list[Event], value: Callable[[datetime], float], end: datetime, interval: timedelta,
    horizon: timedelta, target: float, *, continuous: bool,
) -> datetime | None:
    """The latest time, from `end - horizon` to `end`, at which the window
    of `interval` before it met `target` -- `value(t)` being the window
    ending at `t`'s value -- or `None` if there's none. `value` changes
    only where an event's start or end enters or leaves the window: between
    those, linearly if `continuous` (minutes), else not at all (a count)."""
    first = end - horizon
    points = {end, first}
    for event in events:
        for edge in (event.start, event.end, event.start + interval, event.end + interval):
            if first < edge < end:
                points.add(edge)
    ordered = sorted(points, reverse=True)
    if value(end) >= target:
        return end
    for high, low in zip(ordered, ordered[1:]):
        if continuous:
            at_low = value(low)
            if at_low >= target:
                at_high = value(high)
                return low + (high - low) * ((at_low - target) / (at_low - at_high))
        elif value(low + (high - low) / 2) >= target:
            return high
        elif value(low) >= target:
            return low
    return None


def _overlap(kept: Event, dropped: Event) -> bool:
    if dropped.end > dropped.start:
        return kept.start < dropped.end and kept.end > dropped.start
    return kept.start <= dropped.start < kept.end


def _minutes_in(events: list[Event], start: datetime, end: datetime) -> float:
    return sum(max(0.0, (min(e.end, end) - max(e.start, start)).total_seconds() / 60) for e in events)


def _capped(value: float, target: float) -> int:
    return min(100, round(100 * value / target)) if target > 0 else 100


def _weighted_mean(scores: list[tuple[int | None, float]]) -> int | None:
    rated = [(s, w) for s, w in scores if s is not None]
    total = sum(w for _, w in rated)
    if not rated or total <= 0:
        return None
    return round(sum(s * w for s, w in rated) / total)


def _ago(gap: timedelta) -> str:
    days = gap.total_seconds() / 86400
    if days >= 1:
        return f"{days:.0f} day{'s' if round(days) != 1 else ''}"
    hours = gap.total_seconds() / 3600
    return f"{hours:.0f} hour{'s' if round(hours) != 1 else ''}"
