"""Reallocation: how creating an event makes room for itself.

`reallocate_for_new_event` documents (but does not yet implement — it
raises `NotImplementedError`) the algorithm for making room for a new
event: treating the day as a fixed pool of time, redistributed in one
pass. It has no Calendar API dependency — it's a pure function of the
`day_events` list it's given; see "The day" below.

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
far we may shrink it — except `0` always, if `is_fixed_duration`.
`ReallocationOptions.min_duration_overrides` (event id → minutes) overrides
an event's effective `min_duration` for this call only.

## The algorithm

Given `day_events`, a candidate `new_event` with a real `start` and `end`
(it may or may not already have an `id`), and required
`options: ReallocationOptions` (every field of which is itself optional —
see `ReallocationOptions.resolved()`):

0. **Validate inputs** Assert that the `day_events` object is sorted and
   that none of its events overlap.

1. **Resolve the immediately preceding overlap.** If an event in
   `day_events` starts before `new_event.start` and ends after it (at most
   one, since `day_events` is otherwise non-overlapping), it shrinks first
   so its `end` becomes `new_event.start`, respecting its effective
   `min_duration` — raising a well-structured exception if it can't shrink
   that far. If the portion of its original span after `new_event.start`
   is at least `options.resolved().split_threshold_minutes`, then a new `Event`
   is created to represent this portion and inserted into `day_events`. The new split event is otherwise
   a copy of the preceding event but with a summary suffixed with
   `" (continued)"` (unless already present) and a `min_duration` reduced by
   the duration of the other portion of this split event. It has
   no `id`, so callers can tell it's new.

2. **Insert new event into the list** Insert `new_event` into `day_events`,
   after any event that has a small start time than it. To insert before a
   split event created above, insert `new_event` before any event that has
   the same start time.

3. **Compute how much time is needed.** `duration_to_reclaim = _duration(new_event)`.

4. **Build the reclaim pool.** Walk `day_events`, assuming that it's in
   ascending `start` order, and build a singly linked list of `Span` objects.
   Each `Span` object contains its parent `Schedulable`, a duration, a
   `min_duration`, and a link to the next `Span` in the linked list.  `Span`s
   are created for each non-zero-duration gap between `Schedulable`s,
   these carry the same fields as other `Span`s, except are missing the `Schedulable`
   and always have a `0` `min_duration`.
   A `Span` is created for every event in `day_events`, and every non-zero-duration
   gap. As `Span`s are created, they're appended into lists depending on
   their priority.

5. **Reclaim greedily.** Process spans from lowest priority (`math.inf`)
   to highest, in the order in which they were appended into their lists. shrinking each down
   to its `min_duration` and subtracting from our `duration_to_reclaim` until
   it reaches `0`.

6. **Check for a shortfall.** If the eligible pool, fully reclaimed, still
   falls short of `duration_to_reclaim`, raise a well-structured exception listing
   the remaining duration needed to reclaim, the higher-priority events (by descending duration)
   and the same-or-lower-priority events already at their `min_duration` floor (by
   descending `min_duration`).

7. **Compact into a layout.** Walk the singly-linked list of `Span`s generated
   above, starting from its head, and tracking the current end time.  As this
   list is traversed:
     * if the span has a `Schedulable`:
       * if it's 0 duration and wasn't marked as `"cancelled"`,
         mark it as such and append it to the list of changed events.
       * otherwise, compute the new start time as the current end time, and the
         new end time as the new start time plus the span's `duration`. Set the
         `Schedulable`'s start and end accordingly, and append the `Schedulable`
         to the list of changed events if either of those fields changed.
     * if the span doesn't have a `Schedulable`, simply add its duration to the
       current end time and continue.

9. **Report the plan.** Return the list of changed `Schedulable`s generated by
   the previous step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol

from calendar_clients.google_calendar import Event


class Schedulable(Protocol):
    """The fields of an event that reallocation's algorithm actually reads."""

    start: datetime
    end: datetime
    priority: int | None
    min_duration: timedelta | None
    is_fixed_duration: bool | None

    def clone(self):
        # TODO: implement.  This will be used when splitting a calendar event.
        # It's important that this clones the underlying object, and not just
        # the `Schedulable` view.
        pass


def _duration(span: Schedulable) -> timedelta:
    """`span.end - span.start`."""
    return span.end - span.start


def _reclaimable_seconds(span: Schedulable) -> float:
    """How many seconds could be reclaimed from `span` (step 4 of the
    algorithm above) if every one of them were needed: 0 if
    `is_fixed_duration`; otherwise `_duration(span) - min_duration`,
    treating an unset `min_duration` as `0` (fully reclaimable), floored at
    0.

    Only reads `span`'s `Schedulable` fields — it's given a `day_events`
    entry or `new_event` itself, never anything that needs `summary`/
    `description`/`location`/`id`/`is_end_of_day_sleep` to answer this
    question. In particular it has no notion of event identity: applying
    `ReallocationOptions.min_duration_overrides` is the caller's job, by
    substituting the overridden `min_duration` before calling this.
    """
    raise NotImplementedError


@dataclass
class Span:
    """One event or gap on `day_events`' timeline, in chronological order
    (step 3). `schedulable` is `None` for a gap, which instead records the
    real events immediately before/after it so it can still be located.
    `next` links to the following `Span`, forming a singly linked list for
    step 7's compaction."""

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


def reallocate_for_new_event(
    day_events: list[Event], new_event: Event, options: ReallocationOptions
) -> list[Event]:
    """Make room for `new_event` by reallocating time from the
    lowest-priority events (and free time) in `day_events` — the complete
    list of events for `new_event`'s day, gathered by the caller. Returns
    every `Event` that needs to be created or updated to realize the
    resulting layout, sorted by `start` (see the module docstring's step 8).
    Doesn't call `create_event`/`update_event` itself — applying the plan
    is the caller's job.

    `new_event` may already exist (have an `id`) or not. Raises a
    well-structured exception if the immediately preceding event can't
    shrink enough to clear `new_event.start` (step 1), or if `day_events`
    doesn't have enough reclaimable time at or below `new_event`'s own
    priority to fit it (step 5).

    See the module docstring for the full algorithm.
    """
    raise NotImplementedError
