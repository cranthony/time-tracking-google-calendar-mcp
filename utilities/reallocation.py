"""Reallocation: how creating an event makes room for itself.

`reallocate_for_new_event` implements the algorithm for making room for a
new event by treating the day as a fixed pool of time. It has no Calendar
API dependency and doesn't import `Event` at all — it's a pure function of
the `Schedulable` objects it's given; see "The day" below.

## The day

Reallocation operates on `day_events`: the complete list of events for the
day the new event belongs to, provided by the caller — this module doesn't
fetch anything itself and has no `CalendarClient` dependency. It's the
caller's job to decide what "the day" means and gather that list, using
`Event.is_end_of_day_sleep` to find its boundaries (the day runs from the
end of one sleep block to the end of the next). Nothing here assumes a
specific window length or a calendar-day/timezone boundary; it just works
with whatever `day_events` it's handed.

## Priority

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
(it may or may not already have an `id`), and required
`options: ReallocationOptions` (every field of which is itself optional —
see `ReallocationOptions.resolved()`):

0. **Validate inputs.** `day_events` must be sorted by `start` and none of
   its events may overlap. `new_event`'s `id`, if set, must not match any
   event already in `day_events` -- a caller that wants to move an
   existing event must exclude it from `day_events` first. The last event
   in `day_events` must end after `new_event.end` -- there must always be
   something for reallocation to potentially draw from beyond `new_event`
   itself.

1. **Resolve the immediately preceding overlap.** At most one event in
   `day_events` can start before `new_event.start` and end after it (since
   `day_events` is otherwise non-overlapping); if `day_events` has one, it
   must be `day_events[0]`. That event shrinks first so its `end` becomes
   `new_event.start`, respecting its effective `min_duration` — raising a
   well-structured exception, naming the event, if it can't shrink that
   far. If what's left of its original span after `new_event.end` is at
   least `options.resolved().split_threshold_minutes`, a clone of it is
   also inserted into `day_events` right after `new_event`, representing
   that portion: `start` set to `new_event.end`, summary suffixed
   `" (continued)"` (unless already present), `min_duration` reduced by
   however much of the original event now precedes it, and no `id` (so
   callers can tell it's new).

2. **Insert `new_event` into the list.** After any event with a smaller
   `start`, and before any event (e.g. a continuation from step 1) with
   the same `start`.

3. **Compute how much time is needed.** `new_event` displaces whatever
   already-scheduled time its own span overlaps -- not necessarily its
   full duration, since part (or all) of it may land in already-free
   time, which costs nothing. `duration_to_reclaim` is the total overlap
   between `new_event`'s span and every event in the original
   `day_events`, minus however much of that overlap the immediately
   preceding event's shrink in step 1 already resolved (that overlap is
   exactly what step 1 shrinks it by, so it isn't reclaimed twice).

4. **Build the reclaim pool.** Walk `day_events` (as of step 2) in order,
   building a singly linked list of `Span`s -- one per event, plus one
   per non-zero gap between them (`event=None`, `min_duration=0`) -- so
   step 7 can walk them in order. Group them by priority as they're built
   (a gap's is `math.inf`).

5. **Reclaim greedily.** Process spans by priority, worst (highest
   number, `math.inf` first) to best, in the order they were built within
   a priority, shrinking each down to its `min_duration` and subtracting
   from `duration_to_reclaim` until it reaches `0`. Never reclaims from
   `new_event`'s own span.

6. **Check for a shortfall.** If the pool, fully reclaimed, still falls
   short, raise a well-structured exception with the remaining duration
   still needed, the higher-priority events (by descending duration), and
   the same-or-lower-priority events already at their `min_duration`
   floor (by descending `min_duration`).

7. **Compact into a layout.** Walk the `Span` list from its head,
   tracking the current end time (starting at the earliest event's
   original `start`). A span with an event reduced to `0` duration is
   marked `status = "cancelled"` (if not already) instead of moved;
   otherwise it's placed starting at the current end time (which then
   advances by its duration). A gap span just advances the current end
   time by its duration. This is what lets reclaiming from a span that
   doesn't immediately border `new_event` still ripple forward exactly
   as far as it needs to, and no further: once a span's original
   duration survives untouched, the running position it produces already
   matches that span's own original start, so nothing after it moves.

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


def _duration(event: Schedulable) -> timedelta:
    """`event.end - event.start`."""
    return event.end - event.start


def _overlap(event: Schedulable, new_event: Schedulable) -> timedelta:
    """How much of `new_event`'s span falls within `event`'s span, `0` if
    none (step 3)."""
    overlap = min(event.end, new_event.end) - max(event.start, new_event.start)
    return max(timedelta(0), overlap)


def _effective_priority(event: Schedulable) -> float:
    """`event.priority`, or `1` if unset (see "Priority" above)."""
    return event.priority if event.priority is not None else 1


def _effective_min_duration(
    event: Schedulable, min_duration_overrides: dict[str, int]
) -> timedelta:
    """`event.min_duration`, or the override for `event.id` in
    `min_duration_overrides` (minutes) if there is one, or `0` if neither
    is set (see "Minimum duration" above)."""
    if event.id is not None and event.id in min_duration_overrides:
        return timedelta(minutes=min_duration_overrides[event.id])
    return event.min_duration or timedelta(0)


def _reclaimable_minutes(event: Schedulable) -> float:
    """How many minutes could be reclaimed from `event` (step 5 of the
    algorithm above) if every one of them were needed: `_duration(event) -
    event.min_duration`, treating an unset `min_duration` as `0` (fully
    reclaimable), floored at 0.

    Doesn't apply `ReallocationOptions.min_duration_overrides` itself
    -- that's `_effective_min_duration`'s job.
    """
    minutes = (_duration(event) - (event.min_duration or timedelta(0))).total_seconds() / 60
    return max(0.0, minutes)


def _validate_sorted_and_nonoverlapping(events: list[Schedulable], exception_type: type[Exception] = ValueError) -> None:
    # Ignore cancelled events; we expect those to disappear.
    events = [event for event in events if event.status != "cancelled"]

    for earlier, later in zip(events, events[1:]):
        if earlier.start > later.start:
            raise exception_type(
                f"events must be sorted by start: {earlier.id!r} starts after {later.id!r}"
            )
        if earlier.end > later.start:
            raise exception_type(f"events must not overlap: {earlier.id!r} and {later.id!r}")


@dataclass
class Span:
    """One event or gap on `day_events`' timeline, in chronological order
    (step 4). `event` is `None` for a gap. `next` links to the following
    `Span`, forming a singly linked list for step 7's walk."""

    event: Schedulable | None
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


class ReallocationConflictError(Exception):
    """Raised by `reallocate_for_new_event` when the event immediately
    preceding `new_event` can't shrink enough to clear its start."""

    def __init__(
        self,
        message: str,
        *,
        preceding_event: Schedulable,
        preceding_min_duration: timedelta,
        new_start_time: datetime
    ) -> None:
        super().__init__(message)
        self.preceding_event = preceding_event
        self.preceding_min_duration = preceding_min_duration
        self.new_start_time = new_start_time


class ReallocationShortfallError(Exception):
    """Raised by `reallocate_for_new_event` when `day_events` doesn't have enough reclaimable time to fit
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


class _Reallocation:
    """One `reallocate_for_new_event` call's state, so the steps below can
    read/write it as attributes instead of threading everything (`options`
    especially) through every method call. See `reallocate_for_new_event`
    and the module docstring for the algorithm each step below implements."""

    def __init__(
        self,
        day_events: list[Schedulable],
        new_event: Schedulable,
        options: ReallocationOptions,
    ):
        self.day_events = list(day_events)
        self.new_event = new_event
        self.options = options.resolved()
        self.new_event_priority = _effective_priority(new_event)

        # Snapshot each event's original (start, end, status) before step 1
        # can mutate any of them in place -- step 7 needs these to tell
        # what actually changed.
        self.original_positions: dict[int, tuple[datetime, datetime, str | None]] = {
            id(event): (event.start, event.end, event.status) for event in self.day_events
        }

        self.spans_by_priority: dict[float, list[Span]] = defaultdict(list)

        # A singly linked list of spans, in order of ascending start time.
        # This will be used for the compaction step.
        self.head: Span | None = None
        self.tail: Span | None = None

    def run(self) -> list[Schedulable]:
        self._validate()
        duration_to_reclaim = _duration(self.new_event)
        self._resolve_preceding_overlap()
        self._insert_new_event()
        self._build_reclaim_pool()
        remaining = self._reclaim(duration_to_reclaim)
        if remaining > timedelta(0):
            self._raise_shortfall(remaining)
        return self._apply_plan()

    def _validate(self) -> None:
        _validate_sorted_and_nonoverlapping(self.day_events)

        if self.new_event.id is not None:
            for event in self.day_events:
                if event.id == self.new_event.id:
                    raise ValueError(
                        f"day_events already contains an event with id {self.new_event.id!r}; "
                        "exclude it first if you're moving it"
                    )

        if not self.day_events or self.day_events[-1].end <= self.new_event.end:
            raise ValueError(
                "day_events must contain something ending after new_event.end but "
                f"last event was {self.day_events[-1].id if self.day_events else None!r}")

    def _resolve_preceding_overlap(self) -> None:
        """Step 1. Returns how much of the total overlap (step 3) this
        resolves, so `run` doesn't reclaim it a second time."""
        new_event = self.new_event
        preceding_index = next(
            (
                i
                for i, event in enumerate(self.day_events)
                if event.start < new_event.start < event.end
            ),
            None,
        )
        if preceding_index is None:
            return timedelta(0)
        if preceding_index > 0:
            raise ValueError("day_events must start at the new event's start time")

        preceding = self.day_events[preceding_index]
        preceding_min = _effective_min_duration(preceding, self.options.min_duration_overrides)
        time_before_new_event = new_event.start - preceding.start
        if time_before_new_event < preceding_min:
            raise ReallocationConflictError(
                f"Can't make room for the new event starting at {new_event.start}: the "
                f"preceding event ({preceding.id!r}, {preceding.summary!r}) can't shrink "
                f"below its min_duration of {preceding_min}.",
                preceding_event=preceding,
                preceding_min_duration=preceding_min,
                new_start_time=new_event.start
            )

        new_preceding_duration = new_event.start - preceding.start
        leftover_preceding_duration = _duration(preceding) - new_preceding_duration
        split_threshold = timedelta(minutes=self.options.split_threshold_minutes)
        if leftover_preceding_duration >= split_threshold:
            # The leftover is big enough that we want to split it into a new event.
            continuation = preceding.clone()
            continuation.id = None
            suffix = " (continued)"
            if not (continuation.summary or "").endswith(suffix):
                continuation.summary = f"{continuation.summary or ''}{suffix}"
            continuation.min_duration = max(timedelta(0), preceding_min - new_preceding_duration)
            continuation.start = new_event.end
            continuation.end = preceding.end
            self.day_events.insert(preceding_index + 1, continuation)

        preceding.end = new_event.start

    def _insert_new_event(self) -> None:
        # Insert the new event before any event that starts at the same time.
        # The rest of the algorithm will preserve this order, keeping our new
        # event's start time intact.
        new_event = self.new_event
        insert_at = next(
            (i for i, event in enumerate(self.day_events) if event.start >= new_event.start),
            len(self.day_events),
        )
        self.day_events.insert(insert_at, new_event)

    def _build_reclaim_pool(self) -> None:
        overrides = self.options.min_duration_overrides
        day_events = self.day_events
        for i, event in enumerate(day_events):
            min_duration = _effective_min_duration(event, overrides)
            self._add_span(Span(event=event, duration=_duration(event), min_duration=min_duration))
            if i + 1 < len(day_events):
                gap = day_events[i + 1].start - event.end
                if gap > timedelta(0):
                    self._add_span(Span(event=None, duration=gap, min_duration=timedelta(0)))

    def _add_span(self, span: Span) -> None:
        if self.tail is None:
            self.head = span
        else:
            self.tail.next = span
        self.tail = span
        # Treat spans representing empty space as the lowest possible priority.
        priority = math.inf if span.event is None else _effective_priority(span.event)
        self.spans_by_priority[priority].append(span)

    def _eligible_priorities(self) -> list[float]:
        """Returns priorities that are eligible to reclaim, in order of lowest priority to highest."""
        threshold = self.new_event_priority
        eligible = (priority for priority in self.spans_by_priority if priority >= threshold)
        return sorted(eligible, reverse=True)

    def _reclaim(self, duration_to_reclaim: timedelta) -> timedelta:
        """Mark the spans that will be reduced to claim space for the new event."""
        remaining = duration_to_reclaim
        for priority in self._eligible_priorities():
            for span in self.spans_by_priority[priority]:
                if remaining <= timedelta(0):
                    break
                if span.event is self.new_event:
                    # Treat the new event as a higher priority than the existing
                    # events at its priority.
                    continue
                reclaimable = span.duration - span.min_duration
                if reclaimable <= timedelta(0):
                    continue
                taken = min(reclaimable, remaining)
                span.duration -= taken
                remaining -= taken
            if remaining <= timedelta(0):
                break
        return remaining

    def _raise_shortfall(self, remaining: timedelta) -> None:
        threshold = self.new_event_priority
        higher_priority_events = sorted(
            (event for event in self.day_events if _effective_priority(event) < threshold),
            key=_duration,
            reverse=True,
        )
        overrides = self.options.min_duration_overrides
        events_at_floor = sorted(
            (
                span.event
                for priority in self._eligible_priorities()
                for span in self.spans_by_priority[priority]
                # span.min_duration is already the effective min duration.
                if span.event is not None and span.duration <= span.min_duration
            ),
            key=lambda event: _effective_min_duration(event, overrides),
            reverse=True,
        )
        raise ReallocationShortfallError(
            f"Not enough reclaimable time for the new event: still short by "
            f"{remaining} after reclaiming everything eligible.",
            remaining=remaining,
            higher_priority_events=higher_priority_events,
            events_at_floor=events_at_floor,
        )

    def _apply_plan(self) -> list[Schedulable]:
        changed: list[Schedulable] = []
        current_end = self.day_events[0].start
        span = self.head
        while span is not None:
            event = span.event
            if event is None:
                current_end += span.duration
                span = span.next
                continue

            original = self.original_positions.get(id(event))
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
            span = span.next

        _validate_sorted_and_nonoverlapping(changed, exception_type=RuntimeError)
        return changed


def reallocate_for_new_event(
    day_events: list[Schedulable], new_event: Schedulable, options: ReallocationOptions
) -> list[Schedulable]:
    """Make room for `new_event` by reallocating time from the
    lowest-priority events (and free time) in `day_events` — the complete
    list of events for `new_event`'s day, gathered by the caller. Returns
    every event that needs to be created or updated to realize the
    result, sorted by `start` (step 8). Doesn't apply the plan itself (no
    `create_event`/`update_event` calls) — that's the caller's job.

    Raises `ValueError` if `day_events` fails step 0's validation (not
    sorted, overlapping, already contains `new_event`'s `id`, or has
    nothing ending after `new_event.end`). Raises `ReallocationError` if
    the immediately preceding event can't shrink enough to clear
    `new_event.start` (step 1), or if `day_events` doesn't have enough
    reclaimable time at or below `new_event`'s own priority to fit it
    (step 6).

    See the module docstring for the full algorithm.
    """
    return _Reallocation(day_events, new_event, options).run()
