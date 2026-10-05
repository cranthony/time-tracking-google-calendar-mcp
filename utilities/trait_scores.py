"""Scoring a goal by its traits: the `traits` measure (see
utilities/goal_measures.py), whose traits and their parts are kept in the
Traits tab (utilities/traits.py).

A goal's rating is the weighted mean of its selected traits' scores; each
trait's score is the weighted mean of its parts' scores (0-100), leaving
out a part with nothing to rate it by -- no events with an attention
score, say, or a judgment not made yet -- and a trait whose parts all
are. Parts read the goal's events: its own and its sub-goals', and their
facets (utilities/facets.py). "With events" are those it was at -- given
the goal, unless their facets say they were only *for* it, or naming it in
their facets' `with` -- and "for events" those whose facets name it in
`for`. Event creation times are never used: only when events happened.

Every score carries a one-line `said` and the ids of the events behind
it, so a score can be traced to the events that produced it.

**History.** A rating's metrics keep each trait's score (see
`TraitsRating.metrics`), so a trait's history is derived from the
confirmed ratings: each day, the mean of its scores across the goals
rated by it (`trait_history`). Nothing else is stored.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal

from calendar_clients.google_calendar import Event
from utilities.goal_health import (
    FOLLOW_THROUGH_LOOK_BACK_DAYS,
    _follow_through,
    _over_interval,
    _served,
)
from utilities.goal_sheet import Goal
from utilities.goals import GoalTree
from utilities.health_days import Assessment
from utilities.traits import (
    DEFAULT_WINDOW_DAYS,
    DEFAULT_WITHIN_DAYS,
    Trait,
    part_keys,
    part_problems,
)


@dataclass(kw_only=True)
class PartScore:
    """One part's score of a goal's day."""

    key: str
    """The part's name within its trait: its kind, or "kind#2" for a second
    part of the same kind (see utilities/traits.py's `part_keys`)."""

    kind: str
    weight: float
    score: int | None
    """0-100; `None` when there's nothing to rate it by, or for a judgment
    not made yet."""

    said: str
    """How the score was reached, in a line."""

    event_ids: list[str] = field(default_factory=list)
    """The events behind it."""

    rubric: str | None = None
    """A judgment's rubric."""


@dataclass(kw_only=True)
class TraitScore:
    trait_id: str
    name: str
    weight: float
    """Its weight in the goal's rating."""

    score: int | None
    """The weighted mean of its parts' scores; `None` if none has one."""

    parts: list[PartScore] = field(default_factory=list)


@dataclass(kw_only=True)
class TraitsRating:
    """A goal's day rated by its traits."""

    goal_id: str | None = None
    day: date | None = None
    rating: int | Literal["skip"]
    explanation: str
    traits: list[TraitScore]
    left_out: list[str] = field(default_factory=list)
    """Traits the measure names that aren't rated: off, archived or
    missing from the Traits tab."""

    window_days: float = DEFAULT_WINDOW_DAYS

    @property
    def judgments_due(self) -> list[tuple[str, PartScore]]:
        """(trait id, part) for each judgment part not made yet."""
        return [(t.trait_id, p) for t in self.traits for p in t.parts if p.kind == "judgment" and p.score is None]

    def metrics(self) -> dict[str, Any]:
        """What's kept with the rating: each trait's score and its parts'
        (judgments included, so they're kept next to the computed parts),
        by trait id then part key."""
        metrics: dict[str, Any] = {
            "traits": {t.trait_id: t.score for t in self.traits},
            "parts": {t.trait_id: {p.key: p.score for p in t.parts} for t in self.traits},
            "window_days": self.window_days,
        }
        if self.left_out:
            metrics["left_out"] = self.left_out
        return metrics


def selected_traits(measure: dict[str, Any], traits: list[Trait]) -> tuple[list[Trait], list[str]]:
    """The active traits `measure` rates by, in its order (or the tab's,
    for "all"), and the ids of those it names that aren't rated."""
    chosen = measure.get("traits")
    if chosen == "all":
        return [t for t in traits if t.status == "active" and t.id], []
    by_id = {t.id: t for t in traits if t.id}
    names = chosen if isinstance(chosen, list) else []
    return (
        [by_id[i] for i in names if i in by_id and by_id[i].status == "active"],
        [i for i in names if i not in by_id or by_id[i].status != "active"],
    )


def parts_of(trait: Trait, measure: dict[str, Any]) -> list[dict[str, Any]]:
    """`trait`'s parts for a goal with `measure`: the goal's own, if its
    measure gives them, else the trait's."""
    own = (measure.get("parts") or {}).get(trait.id) if isinstance(measure.get("parts"), dict) else None
    parts = own if isinstance(own, list) and own else trait.parts
    return parts if isinstance(parts, list) else []


def reach(measure: dict[str, Any], traits: list[Trait]) -> tuple[timedelta, timedelta, bool]:
    """How far before the end of the day being rated a traits measure
    reads events, how far after it (for continuity's next event), and
    whether it needs cancelled ones (for follow-through)."""
    window = measure.get("window_days", DEFAULT_WINDOW_DAYS)
    back, ahead, cancelled = timedelta(days=window), timedelta(), False
    for trait in selected_traits(measure, traits)[0]:
        for part in parts_of(trait, measure):
            if part_problems(part):
                continue
            kind = part["kind"]
            days = window
            if kind == "continuity":
                days = part.get("last_within_days", DEFAULT_WITHIN_DAYS)
                ahead = max(ahead, timedelta(days=part.get("next_within_days", DEFAULT_WITHIN_DAYS)))
            elif kind in ("count", "duration"):
                days = max(part.get("interval_days", window), part.get("zero_at_days", 0))
            elif kind == "follow_through":
                days = part.get("look_back_days", FOLLOW_THROUGH_LOOK_BACK_DAYS)
                cancelled = True
            back = max(back, timedelta(days=days))
    return back, ahead, cancelled


def score_traits(
    goal: Goal,
    measure: dict[str, Any],
    traits: list[Trait],
    window: tuple[datetime, datetime],
    events: list[Event],
    cancelled: list[Event],
    tree: GoalTree,
    judgments: dict[str, dict[str, int]] | None = None,
) -> TraitsRating:
    """`goal`'s day (`window`) rated by the traits `measure` selects, over
    `events` (kept) and `cancelled` -- see the module docstring.
    `judgments` gives judgment parts' scores, by trait id then part key."""
    judgments = judgments or {}
    chosen, left_out = selected_traits(measure, traits)
    window_days = measure.get("window_days", DEFAULT_WINDOW_DAYS)
    weights = measure.get("weights") or {}
    scope = Scope(goal, tree, events)
    scored = []
    for trait in chosen:
        parts = parts_of(trait, measure)
        part_scores = [
            _part(goal, part, key, window, window_days, events, cancelled, tree, scope, judgments.get(trait.id, {}))
            for part, key in zip(parts, part_keys(parts))
        ]
        scored.append(
            TraitScore(
                trait_id=trait.id,
                name=trait.name or trait.id,
                weight=weights.get(trait.id, 1),
                score=_weighted_mean([(p.score, p.weight) for p in part_scores]),
                parts=part_scores,
            )
        )
    rating = _weighted_mean([(t.score, t.weight) for t in scored])
    terms = ", ".join(
        f"{t.name} {'–' if t.score is None else t.score}{'' if t.weight == 1 else f'×{t.weight:g}'}" for t in scored
    )
    if rating is None:
        explanation = f"No trait had anything to rate it by ({terms or 'none selected'}) → skip"
    else:
        explanation = f"Traits ({terms}) → {rating}"
    return TraitsRating(
        rating="skip" if rating is None else rating,
        explanation=explanation,
        traits=scored,
        left_out=left_out,
        window_days=window_days,
    )


class Scope:
    """A goal's with and for events: see the module docstring."""

    def __init__(self, goal: Goal, tree: GoalTree, events: list[Event]) -> None:
        wanted = {g.id for g in tree.goals if tree.under(g.id, goal.id)}

        def names(ids) -> bool:
            return bool(set(ids or ()) & wanted)

        self.with_events = [
            e for e in events
            if names(e.facets.with_goal_ids if e.facets else ())
            or (names(e.goal_ids) and not names(e.facets.for_goal_ids if e.facets else ()))
        ]
        self.for_events = [e for e in events if e.facets is not None and names(e.facets.for_goal_ids)]


def _part(
    goal: Goal,
    part: Any,
    key: str,
    window: tuple[datetime, datetime],
    window_days: float,
    events: list[Event],
    cancelled: list[Event],
    tree: GoalTree,
    scope: Scope,
    judged: dict[str, int],
) -> PartScore:
    problems = part_problems(part)
    if problems:
        kind = part.get("kind") if isinstance(part, dict) and isinstance(part.get("kind"), str) else "?"
        return PartScore(key=key, kind=kind, weight=0, score=None, said=f"Not rated: it {problems[0]}")
    kind = part["kind"]
    weight = part.get("weight", 1)
    end = window[1]
    # A part's engagement says whose events it reads: those the goal was
    # at ("with"), or those done for it ("for").
    engaged = scope.for_events if part.get("engagement_type") == "for" else scope.with_events

    def score(value: int | None, said: str, used: list[Event] = ()) -> PartScore:
        return PartScore(
            key=key, kind=kind, weight=weight, score=value, said=said,
            event_ids=[e.id for e in used if e.id], rubric=part.get("rubric"),
        )

    if kind == "continuity":
        last_days = part.get("last_within_days", DEFAULT_WITHIN_DAYS)
        next_days = part.get("next_within_days", DEFAULT_WITHIN_DAYS)
        before = [e for e in engaged if e.start < end]
        after = [e for e in engaged if e.start >= end]
        last = max(before, key=lambda e: e.end, default=None)
        upcoming = min(after, key=lambda e: e.start, default=None)
        last_ok = last is not None and end - last.end <= timedelta(days=last_days)
        next_ok = upcoming is not None and upcoming.start - end <= timedelta(days=next_days)
        said = "; ".join([
            f"last {_ago(end - last.end)} ago" if last is not None else "none before",
            f"next in {_ago(upcoming.start - end)}" if upcoming is not None else "none planned",
        ]) + f" (within {last_days:g} and {next_days:g} days)"
        used = [e for e in (last, upcoming) if e is not None]
        return score(50 * last_ok + 50 * next_ok, said[0].upper() + said[1:], used)
    if kind == "judgment":
        if key in judged:
            return score(judged[key], "Judged in the reflection")
        return score(None, "To be judged in the reflection")
    if kind in ("count", "duration"):
        # A part's "action" isn't applied yet: events don't name actions.
        spec = {
            "interval_days": window_days,
            **{k: v for k, v in part.items() if k not in ("weight", "action", "engagement_type")},
        }
        counted = engaged if "engagement_type" in part else None
        rating, said, _metrics = _over_interval(goal, spec, window, events, tree, kind=kind, served=counted)
        interval = timedelta(days=spec["interval_days"])
        pool = counted if counted is not None else _served(events, goal, {}, tree)
        used = [e for e in pool if e.start < end and e.end > end - interval]
        return score(rating, said.rsplit(" → ", 1)[0], used)
    # follow_through
    spec = {k: v for k, v in part.items() if k not in ("weight", "action", "engagement_type")}
    rating, said, _metrics = _follow_through(goal, spec, window, events, cancelled, tree)
    look_back = timedelta(days=spec.get("look_back_days", FOLLOW_THROUGH_LOOK_BACK_DAYS))
    dropped = [e for e in _served(cancelled, goal, {}, tree) if end - look_back <= e.start < end]
    return score(rating, said.rsplit(" → ", 1)[0], dropped)


@dataclass(kw_only=True)
class GoalTraitScore:
    goal_id: str
    score: int


@dataclass(kw_only=True)
class TraitDay:
    """One trait's score of one day: the mean of its scores across the
    goals rated by it that day."""

    trait_id: str
    name: str
    day: date
    score: int
    goals: list[GoalTraitScore] = field(default_factory=list)
    """Each goal's score of it, which `score` is the mean of."""


def trait_history(
    assessments: list[Assessment], traits: list[Trait], trait_ids: Collection[str] | None = None
) -> list[TraitDay]:
    """Each trait's (or just `trait_ids`') score of each day `assessments`
    rate it, by trait (in the Traits tab's order) then day -- from the
    trait scores kept in their metrics (see `TraitsRating.metrics`)."""
    names = {t.id: t.name or t.id for t in traits if t.id}
    found: dict[tuple[str, date], list[GoalTraitScore]] = {}
    for a in assessments:
        scores = (a.metrics or {}).get("traits")
        if not isinstance(scores, dict):
            continue
        for trait_id, score in scores.items():
            if isinstance(score, (int, float)) and (trait_ids is None or trait_id in trait_ids):
                found.setdefault((trait_id, a.day), []).append(GoalTraitScore(goal_id=a.goal_id, score=round(score)))
    order = {trait_id: i for i, trait_id in enumerate(names)}
    return [
        TraitDay(
            trait_id=trait_id,
            name=names.get(trait_id, trait_id),
            day=day,
            score=round(sum(g.score for g in goals) / len(goals)),
            goals=goals,
        )
        for (trait_id, day), goals in sorted(found.items(), key=lambda item: (order.get(item[0][0], len(order)), item[0]))
    ]


def _weighted_mean(scores: list[tuple[int | None, float]]) -> int | None:
    counted = [(s, w) for s, w in scores if s is not None and w > 0]
    total = sum(w for _, w in counted)
    return round(sum(s * w for s, w in counted) / total) if total else None


def _ago(gap: timedelta) -> str:
    days = gap / timedelta(days=1)
    if days < 1:
        hours = max(0, round(gap / timedelta(hours=1)))
        return f"{hours} hour{'s' if hours != 1 else ''}"
    days = round(days)
    return f"{days} day{'s' if days != 1 else ''}"
