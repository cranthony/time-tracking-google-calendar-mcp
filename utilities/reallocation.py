"""Reallocation: how creating an event makes room for itself.

`reallocate_for_new_event` implements the algorithm for making room for a
new event by treating the day as a fixed pool of time, redistributed in one
pass. It has no Calendar API dependency and doesn't import `Event` at all —
it's a pure function of the `Schedulable` objects it's given; see "The day"
below.

## The day

Reallocation operates on `day_events`: the complete list of events for the
day the new event belongs to, provided by the caller — this module doesn't
fetch anything itself and has no `CalendarClient` dependency. It's the
caller's job to decide what "the day" means and gather that list, using
`Event.is_end_of_day_sleep` to find its boundaries (the day runs from the
end of one sleep block to the end of the next). Nothing here assumes a
specific window length or a calendar-day/timezone boundary; it just works
with whatever `day_events` it's handed.

## Priority tiers

Every event has a `priority` (lower number = more important); a
higher-priority event is never shrunk to make room for a lower-priority
one. Free time is the lowest possible priority (`math.inf`). An event with
no `priority` set is treated as priority `1`.

## Minimum duration

An event's `min_duration` (default `0`, i.e. fully reclaimable) bounds how
far step 5 may shrink it. This module doesn't special-case a fixed-duration
event itself — a `Schedulable` whose duration must never change should
simply have its `min_duration` already equal to its own duration (e.g.
`Event.from_api` does this at load time). `ReallocationOptions.
min_duration_overrides` (event id → minutes) overrides an event's effective
`min_duration` for this call only.

## The algorithm

Given `day_events`, a candidate `new_event` with a real `start` and `end`
(it may or may not already have an `id` — if it's already present in
`day_events`, it's removed first, so its old slot becomes reclaimable free
time like any other gap), and required `options: ReallocationOptions`
(every field of which is itself optional — see `ReallocationOptions.
resolved()`):

0. **Validate inputs.** Assert that `day_events` is sorted by `start` and
   that none of its events overlap.

1. **Resolve the immediately preceding overlap.** If an event in
   `day_events` starts before `new_event.start` and ends after it (at most
   one, since `day_events` is otherwise non-overlapping), it shrinks first
   so its `end` becomes `new_event.start`, respecting its effective
   `min_duration` — raising a well-structured exception if it can't shrink
   that far. If the portion of its original span after `new_event.start`
   is at least `options.resolved().split_threshold_minutes`, a clone of it
   is also inserted into `day_events`, representing that portion: summary
   suffixed `" (continued)"` (unless already present), `min_duration`
   reduced by however much of the original event preceded
   `new_event.start`, and no `id` (so callers can tell it's new).

2. **Insert `new_event` into the list.** After any event with a smaller
   `start`, and before any event (e.g. a continuation from step 1) with the
   same `start`.

3. **Compute how much time is needed.** `duration_to_reclaim =
   _duration(new_event)`.

4. **Build the reclaim pool.** Walk `day_events` in order and build a
   singly linked list of `Span`s — one per event, plus one per non-zero
   gap between them (`schedulable=None`, `min_duration=0`) — grouping them
   by priority (gaps at `math.inf`) as they're created. `new_event`'s own
   span is linked in (for step 7) but excluded from the pool: it would
   make no sense to reclaim time from the very event being placed.

5. **Reclaim greedily.** Process spans from lowest priority (`math.inf`)
   to highest, in the order they were appended within a tier, shrinking
   each down to its `min_duration` and subtracting from
   `duration_to_reclaim`, until it reaches `0`.

6. **Check for a shortfall.** If the pool, fully reclaimed, still falls
   short, raise a well-structured exception with the remaining duration
   still needed, the higher-priority events (by descending duration), and
   the same-or-lower-priority events already at their `min_duration` floor
   (by descending `min_duration`).

7. **Compact into a layout.** Walk the `Span` linked list from its head,
   tracking the current end time (starting at the earliest event's
   original `start`):
   - A span with a `Schedulable` reduced to `0` duration is marked
     `status = "cancelled"` (if not already) instead of moved.
   - A span with a `Schedulable` of non-zero duration is placed starting
     at the current end time; the end time then advances by the span's
     duration. An event with no `id` is always reported as changed (it
     still needs creating), even if its position happens to match.
   - A gap span just advances the current end time by its duration.

8. **Report the plan.** Return every changed or newly-created event,
   sorted by `start`.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol


class Schedulable(Protocol):
    """The fields of an event that reallocation's algorithm actually reads
    or writes."""

    id: str | None
    summary: str | None
    start: datetime
    end: datetime
    status: str | None
    priority: int | None
    min_duration: timedelta | None

    def clone(self) -> "Schedulable":
        """A copy of the underlying object (not just this `Schedulable`
        view), independently mutable -- used to represent a split-off
        continuation event (step 1)."""
        ...


def _duration(span: Schedulable) -> timedelta:
    """`span.end - span.start`."""
    return span.end - span.start


def _effective_priority(span: Schedulable) -> float:
    """`span.priority`, or `1` if unset (see "Priority tiers" above)."""
    return span.priority if span.priority is not None else 1


def _effective_min_duration(
    span: Schedulable, min_duration_overrides: dict[str, int]
) -> timedelta:
    """`span.min_duration`, or the override for `span.id` in
    `min_duration_overrides` (minutes) if there is one, or `0` if neither is
    set (see "Minimum duration" above)."""
    if span.id is not None and span.id in min_duration_overrides:
        return timedelta(minutes=min_duration_overrides[span.id])
    return span.min_duration or timedelta(0)


def _reclaimable_seconds(span: Schedulable) -> float:
    """How many seconds could be reclaimed from `span` (step 5 of the
    algorithm above) if every one of them were needed: `_duration(span) -
    span.min_duration`, treating an unset `min_duration` as `0` (fully
    reclaimable), floored at 0.

    Only reads `span`'s `Schedulable` fields, never `description`/
    `location`/`is_end_of_day_sleep`. Doesn't apply `ReallocationOptions.
    min_duration_overrides` itself -- that's `_effective_min_duration`'s
    job.
    """
    return max(0.0, (_duration(span) - (span.min_duration or timedelta(0))).total_seconds())


@dataclass
class Span:
    """One event or gap on `day_events`' timeline, in chronological order
    (step 4). `schedulable` is `None` for a gap. `next` links to the
    following `Span`, forming a singly linked list for step 7's
    compaction."""

    schedulable: Schedulable | None
    duration: timedelta
    min_duration: timedelta
    next: "Span | None" = None


@dataclass(kw_only=True)
class ReallocationOptions:
    """Tunable knobs for `reallocate_for_new_event`. Every field is
    optional; call `resolved()` to fill in defaults."""

    split_threshold_minutes: int | None = None
    min_duration_overrides: dict[str, int] | None = None

    def resolved(self) -> "ReallocationOptions":
        """A copy with every unset field filled in: `split_threshold_minutes`
        defaults to `15`, `min_duration_overrides` to `{}`."""
        return ReallocationOptions(
            split_threshold_minutes=(
                self.split_threshold_minutes if self.split_threshold_minutes is not None else 15
            ),
            min_duration_overrides=(
                self.min_duration_overrides if self.min_duration_overrides is not None else {}
            ),
        )


class ReallocationError(Exception):
    """Raised by `reallocate_for_new_event` when the event immediately
    preceding `new_event` can't shrink enough to clear its start (step 1),
    or when `day_events` doesn't have enough reclaimable time to fit
    `new_event` at all (step 6)."""

    def __init__(
        self,
        message: str,
        *,
        remaining: timedelta | None = None,
        higher_priority_events: list[Schedulable] = (),
        events_at_floor: list[Schedulable] = (),
    ) -> None:
        super().__init__(message)
        self.remaining = remaining
        self.higher_priority_events = list(higher_priority_events)
        self.events_at_floor = list(events_at_floor)


def reallocate_for_new_event(
    day_events: list[Schedulable], new_event: Schedulable, options: ReallocationOptions
) -> list[Schedulable]:
    """Make room for `new_event` by reallocating time from the
    lowest-priority events (and free time) in `day_events` — the complete
    list of events for `new_event`'s day, gathered by the caller. Returns
    every event that needs to be created or updated to realize the
    resulting layout, sorted by `start` (step 8). Doesn't apply the plan
    itself (no `create_event`/`update_event` calls) — that's the caller's
    job.

    `new_event` may already exist (have an `id`) or not; if it's already
    present in `day_events`, it's treated as being moved (its old slot
    becomes reclaimable free time). Raises `ReallocationError` if the
    immediately preceding event can't shrink enough to clear
    `new_event.start` (step 1), or if `day_events` doesn't have enough
    reclaimable time at or below `new_event`'s own priority to fit it
    (step 6).

    See the module docstring for the full algorithm.
    """
    resolved_options = options.resolved()

    day_events = [
        event
        for event in day_events
        if event is not new_event and (new_event.id is None or event.id != new_event.id)
    ]

    # Snapshot each event's original (start, end, status), before step 1
    # mutates any of them in place -- step 7 needs these to tell what
    # actually changed, since by then the live objects have already moved.
    original_positions: dict[int, tuple[datetime, datetime, str | None]] = {
        id(event): (event.start, event.end, event.status) for event in day_events
    }

    # Step 0: validate inputs.
    for earlier, later in zip(day_events, day_events[1:]):
        if earlier.start > later.start:
            raise ValueError("day_events must be sorted by start")
        if earlier.end > later.start:
            raise ValueError("day_events must not overlap")

    # Step 1: resolve the immediately preceding overlap, if any.
    preceding_index = next(
        (i for i, event in enumerate(day_events) if event.start < new_event.start < event.end),
        None,
    )
    if preceding_index is not None:
        if preceding_index > 0:
            raise ValueError("day_events must start at the new event's start time")
        preceding = day_events[preceding_index]
        preceding_min = _effective_min_duration(preceding, resolved_options.min_duration_overrides)
        time_before_new_event = new_event.start - preceding.start
        if time_before_new_event < preceding_min:
            raise ReallocationError(
                f"Can't make room for the new event starting at {new_event.start}: "
                f"the preceding event ({preceding.summary!r}) can't shrink below its "
                f"min_duration of {preceding_min}."
            )

        overlap_after_new_event = preceding.end - new_event.start
        split_threshold = timedelta(minutes=resolved_options.split_threshold_minutes)
        if overlap_after_new_event >= split_threshold:
            continuation = preceding.clone()
            continuation.id = None
            suffix = " (continued)"
            if not (continuation.summary or "").endswith(suffix):
                continuation.summary = f"{continuation.summary or ''}{suffix}"
            continuation.min_duration = max(timedelta(0), preceding_min - time_before_new_event)
            continuation.start = new_event.start
            continuation.end = preceding.end
            day_events.insert(preceding_index + 1, continuation)

        preceding.end = new_event.start

    # Step 2: insert new_event, before any event with an equal start.
    insert_at = next(
        (i for i, event in enumerate(day_events) if event.start >= new_event.start),
        len(day_events),
    )
    day_events.insert(insert_at, new_event)

    # Step 3: compute how much time is needed.
    duration_to_reclaim = _duration(new_event)

    # Step 4: build the reclaim pool.
    new_event_priority = _effective_priority(new_event)
    spans_by_priority: dict[float, list[Span]] = defaultdict(list)
    head: Span | None = None
    tail: Span | None = None

    def append_span(span: Span) -> None:
        nonlocal head, tail
        if tail is None:
            head = span
        else:
            tail.next = span
        tail = span
        
        priority = (
            math.inf if span.schedulable is None else _effective_priority(span.schedulable)
        )
        spans_by_priority[priority].append(span)

    for i, event in enumerate(day_events):
        min_duration = _effective_min_duration(event, resolved_options.min_duration_overrides)
        append_span(
            Span(schedulable=event, duration=_duration(event), min_duration=min_duration)
        )
        if i + 1 < len(day_events):
            gap = day_events[i + 1].start - event.end
            if gap > timedelta(0):
                append_span(
                    Span(schedulable=None, duration=gap, min_duration=timedelta(0))
                )

    # Step 5: reclaim greedily, worst tier first.
    remaining = duration_to_reclaim
    eligible_tiers = sorted(
        (tier for tier in spans_by_priority if tier >= new_event_priority), reverse=True
    )
    for tier in eligible_tiers:
        for span in spans_by_priority[tier]:
            if remaining <= timedelta(0):
                break
            reclaimable = span.duration - span.min_duration
            if reclaimable <= timedelta(0):
                continue
            taken = min(reclaimable, remaining)
            span.duration -= taken
            remaining -= taken
        if remaining <= timedelta(0):
            break

    # Step 6: check for a shortfall.
    if remaining > timedelta(0):
        higher_priority_events = sorted(
            (event for event in day_events if _effective_priority(event) < new_event_priority),
            key=_duration,
            reverse=True,
        )
        overrides = resolved_options.min_duration_overrides
        events_at_floor = sorted(
            (
                span.schedulable
                for tier in eligible_tiers
                for span in spans_by_priority[tier]
                if span.schedulable is not None and span.duration <= span.min_duration
            ),
            key=lambda event: _effective_min_duration(event, overrides),
            reverse=True,
        )
        raise ReallocationError(
            f"Not enough reclaimable time for the new event: still short by "
            f"{remaining} after reclaiming everything eligible.",
            remaining=remaining,
            higher_priority_events=higher_priority_events,
            events_at_floor=events_at_floor,
        )

    # Step 7: compact into a single layout.
    changed: list[Schedulable] = []
    current_end = day_events[0].start
    span = head
    while span is not None:
        if span.schedulable is not None:
            event = span.schedulable
            original = original_positions.get(id(event))
            if span.duration <= timedelta(0):
                already_cancelled = original is not None and original[2] == "cancelled"
                event.status = "cancelled"
                if not already_cancelled:
                    changed.append(event)
            else:
                new_start = current_end
                new_end = new_start + span.duration
                if original is None or original[0] != new_start or original[1] != new_end:
                    changed.append(event)
                event.start = new_start
                event.end = new_end
                current_end = new_end
        else:
            current_end += span.duration
        span = span.next

    # Step 8: report the plan.
    return sorted(changed, key=lambda event: event.start)
