"""A goal's history digest: what its past events' facets (see
utilities/facets.py) say it's been -- each activity and place, with how
many times, and the first and last time -- over a window (by default 180
days). Compaction is shown it before writing facets, so it reuses the
same labels and can judge what's `new` directly; the client shows it on a
person goal's page. Computed on request, never stored.

The events are the goal's with and for events (see
utilities/trait_scores.py's `Scope`): its own and its sub-goals', and any
whose facets name it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo

from calendar_clients.google_calendar import Event
from utilities.goal_sheet import Goal
from utilities.goals import GoalTree
from utilities.trait_scores import Scope

DEFAULT_WINDOW_DAYS = 180


@dataclass(kw_only=True)
class DigestEntry:
    label: str
    count: int
    first: date
    last: date


@dataclass(kw_only=True)
class HistoryDigest:
    goal_id: str
    window_days: int
    events: int
    """The goal's events in the window (with or for it)."""

    with_facets: int
    """How many of them have facets."""

    activities: list[DigestEntry] = field(default_factory=list)
    """Most often first, then most recent."""

    places: list[DigestEntry] = field(default_factory=list)
    text: str = ""
    """The same, in a line each, e.g. "Activities: salsa social ×9
    (first 2026-04-03, last 2026-09-28); ..."."""


def history_digest(
    goal: Goal, tree: GoalTree, events: list[Event], end: datetime, tz: tzinfo,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> HistoryDigest:
    """`goal`'s digest of `events` (kept ones) that started in the
    `window_days` before `end` -- see the module docstring."""
    start = end - timedelta(days=window_days)
    scope = Scope(goal, tree, events)
    found = sorted(
        {id(e): e for e in scope.with_events + scope.for_events if start <= e.start < end}.values(),
        key=lambda e: e.start,
    )
    activities = _entries([(e.facets.activity, e) for e in found if e.facets and e.facets.activity], tz)
    places = _entries([(e.facets.place, e) for e in found if e.facets and e.facets.place], tz)
    with_facets = sum(1 for e in found if e.facets is not None and not e.facets.is_empty())

    def line(name: str, entries: list[DigestEntry]) -> str:
        listed = "; ".join(f"{d.label} ×{d.count} (first {d.first}, last {d.last})" for d in entries)
        return f"{name}: {listed or 'none recorded'}"

    return HistoryDigest(
        goal_id=goal.id,
        window_days=window_days,
        events=len(found),
        with_facets=with_facets,
        activities=activities,
        places=places,
        text="\n".join(
            [
                f"{tree.path(goal.id)}: {len(found)} events in the last {window_days} days, "
                f"{with_facets} with facets",
                line("Activities", activities),
                line("Places", places),
            ]
        ),
    )


def _entries(labelled: list[tuple[str, Event]], tz: tzinfo) -> list[DigestEntry]:
    grouped: dict[str, list[Event]] = {}
    for label, event in labelled:
        grouped.setdefault(label.casefold(), []).append(event)
    entries = [
        DigestEntry(
            label=key,
            count=len(found),
            first=min(e.start for e in found).astimezone(tz).date(),
            last=max(e.start for e in found).astimezone(tz).date(),
        )
        for key, found in grouped.items()
    ]
    return sorted(entries, key=lambda d: (-d.count, -d.last.toordinal(), d.label))
