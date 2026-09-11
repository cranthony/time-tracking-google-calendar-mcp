"""Reallocation: how creating an event makes room for itself.

**Status: design document.** `reallocate_for_new_event` below is fully
specified but not yet implemented (it raises `NotImplementedError`) — this
module exists to nail the algorithm down and get it reviewed before writing
the real thing.

This module has no Calendar API dependency and does no fetching of its own
— it's a pure function of the event data it's given. See "The day" below.
Reallocation treats the day as a fixed pool of time that gets redistributed
as a whole, in one pass, whenever a new event needs room.

## The day

Reallocation operates on `day_events`: the complete list of events for the
day the new event belongs to, provided by the caller — this module doesn't
fetch anything itself and has no `CalendarClient` dependency. It's the
caller's job to decide what "the day" means and gather that list, using
`Event.is_end_of_day_sleep` to find its boundaries (the day runs from the
end of one sleep block to the end of the next). Nothing here assumes a
specific window length or a calendar-day/timezone boundary; it just works
with whatever `day_events` it's handed.

## Priority tiers, and free time as the lowest tier

Every event has a `priority` (lower number = more important; see
`Event.priority`). Gaps between events in `day_events` — unscheduled time —
are treated as occupying the *lowest possible* priority tier, below every
real event, represented as `math.inf`. That single rule is what makes
"prefer to shrink the lowest-priority things first" also mean "use up empty
space before touching any real event" — free time always sorts last, with
no special case needed.

An event with `priority=None` (never set) is treated as `-math.inf` — the
highest possible priority, i.e. never reclaimed — on the theory that
reallocation should only ever touch time the user explicitly marked as
lower priority than the new event, never time they didn't get around to
prioritizing yet. This falls out of ordinary numeric comparison rather than
needing a special case: it's just what `-math.inf` sorts as. The one
exception is `new_event` itself having no priority (also `-math.inf`) —
then other `priority=None` spans tie with it under step 4's `>=` test
below, the same as any other tie, rather than being unconditionally
protected.

## The algorithm

Given `day_events`, a candidate `new_event` (not yet created — no `id`)
with a real `start`, `end`, and (implicitly) `priority`, and optional
`options: ReallocationOptions`:

1. **Resolve the immediately preceding overlap.** If an event in
   `day_events` starts before `new_event.start` and ends after it (there
   can be at most one, since `day_events` is otherwise non-overlapping),
   it always shrinks first so its `end` becomes `new_event.start` —
   respecting its `min_duration` (or ignoring it, if its `id` is in
   `options.ignore_min_duration_for_event_ids`; see below). If it can't
   shrink that far because of `min_duration`, raise a well-structured
   exception describing why — the only failure mode at this stage.

   If the portion of that event's *original* span that fell after
   `new_event.start` was at least `options.split_threshold_minutes`
   (default `15`, whether `options` itself or just this field is
   omitted), a *new* `Event` is also created: a copy of the preceding
   event, its summary suffixed with `" (continued)"` (unless already
   present), and its `min_duration` reduced by however much of the
   original event preceded `new_event.start`. Callers can tell this is a
   new event rather than an update because it has no `id`. Below the
   threshold, nothing is carried forward — the preceding event just
   shrinks.

   `options.ignore_min_duration_for_event_ids` (a set of event IDs)
   applies everywhere in this algorithm, not just this step: wherever a
   `min_duration` is read from one of these events, it's treated as unset
   (`0`) instead.

2. **Start from `day_events`.** Every existing event `new_event` might
   need to borrow time from — already fetched and handed in by the
   caller, as adjusted by step 1.

3. **Compute how much time is needed.** `duration = new_event.end -
   new_event.start`. The same algorithm runs whether `day_events` is packed
   solid or mostly empty — an empty (or nearly empty) day just means step 5
   reclaims everything it needs from the lowest tier and never reaches a
   real event.

4. **Build the reclaim pool.** Every span among `day_events` — each
   existing event, plus every gap between/around them — that has priority
   >= `new_event.priority` (gaps always qualify, per their `math.inf` tier)
   is a candidate. A span with strictly better (lower-numbered) priority
   than `new_event` is off the table entirely — never touched, no matter
   how much time is needed.

5. **Reclaim greedily, worst tier first.** Sort the pool by tier
   (descending priority number, `math.inf` first). Within a tier, process
   spans earliest-`start`-first (the simplest deterministic tie-break;
   revisit if it produces surprising results in practice). For each span:
      - A gap contributes up to its full length.
      - A real event contributes up to `current_duration - min_duration`,
        treating an unset `min_duration` (or one ignored per
        `options.ignore_min_duration_for_event_ids`) as `0` (i.e. the
        event may be shrunk arbitrarily far, down to nothing) — except `0`
        if `is_fixed_duration`, which always overrides `min_duration`.
      - Stop as soon as the running total meets `duration`.

6. **Check for a shortfall.** If the entire eligible pool (every gap, plus
   every eligible event shrunk to its floor) is still less than `duration`,
   reallocation cannot proceed: raise a well-structured exception stating
   that there isn't enough time left in the day, listing the
   higher-priority events (sorted by descending duration) and the
   same-or-lower-priority events that hit their `min_duration` floor
   (sorted by ascending `min_duration`). (A shortfall can also mean
   `day_events` didn't actually cover a full day; this module has no way
   to tell the difference and doesn't try to.)

7. **Recompute durations.** Each event touched in step 5 gets a new,
   shrunk duration (`current_duration` minus whatever was reclaimed from
   it, floored per step 5). Every other event is unchanged. An event
   shrunk all the way to zero duration is marked cancelled (`status =
   "cancelled"`) rather than given a zero-length span.

8. **Compact into a single layout.** Walk `day_events` in chronological
   order by original `start`. Place `new_event` at its own requested
   `start`/`end`. Then lay out the remaining events immediately after one
   another in that same original relative order, back-to-back, each
   keeping whatever duration step 7 gave it — no gaps except whatever free
   time step 5 didn't need to consume. This single pass is what actually
   moves anything; nothing is shoved iteratively.

9. **Report the plan.** Return every `Event` that needs to be created or
   updated to realize this layout, sorted by `start`: `new_event`
   (unchanged — it's already placed where it was asked to be) plus every
   existing event whose duration, position, or status changed, plus any
   new "(continued)" event split off in step 1. This module doesn't call
   `create_event`/`update_event` itself — applying the plan (and deciding
   what to do if that application partially fails) is the caller's job.
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


def _reclaimable_seconds(span: Schedulable) -> float:
    """How many seconds could be reclaimed from `span` (step 5 of the
    algorithm above) if every one of them were needed: 0 if
    `is_fixed_duration`; otherwise `(end - start) - min_duration`, treating
    an unset `min_duration` as `0` (fully reclaimable), floored at 0.

    Only reads `span`'s `Schedulable` fields — it's given a `day_events`
    entry or `new_event` itself, never anything that needs `summary`/
    `description`/`location`/`id`/`is_end_of_day_sleep` to answer this
    question. In particular it has no notion of event identity: applying
    `ReallocationOptions.ignore_min_duration_for_event_ids` is the caller's
    job, by substituting an effective `min_duration` of `0` before calling
    this for a span whose id is in that set.
    """
    raise NotImplementedError


@dataclass(kw_only=True)
class ReallocationOptions:
    """Tunable knobs for `reallocate_for_new_event`. See the module
    docstring's step 1 for `split_threshold_minutes`, and steps 1 and 5 for
    `ignore_min_duration_for_event_ids`."""

    split_threshold_minutes: int = 15
    ignore_min_duration_for_event_ids: frozenset[str] = field(default_factory=frozenset)


def reallocate_for_new_event(
    day_events: list[Event], new_event: Event, options: ReallocationOptions | None = None
) -> list[Event]:
    """Make room for `new_event` by reallocating time from the
    lowest-priority events (and free time) in `day_events` — the complete
    list of events for `new_event`'s day, gathered by the caller. Returns
    every `Event` that needs to be created or updated to realize the
    resulting layout, sorted by `start`: `new_event` plus every existing
    event whose duration, position, or status changed, plus any new
    "(continued)" event split off by step 1. Doesn't call
    `create_event`/`update_event` itself — applying the plan is the
    caller's job.

    `new_event` must not have an `id` yet. `options` defaults to
    `ReallocationOptions()` if omitted. Raises a well-structured exception
    if the immediately preceding event can't shrink enough to clear
    `new_event.start` (step 1), or if `day_events` doesn't have enough
    reclaimable time at or below `new_event`'s own priority to fit it
    (step 6).

    See the module docstring for the full algorithm.
    """
    raise NotImplementedError
