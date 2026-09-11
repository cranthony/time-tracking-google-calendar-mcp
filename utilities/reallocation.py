"""Reallocation: how creating an event makes room for itself.

**Status: design document.** `reallocate_for_new_event` below is fully
specified but not yet implemented (it raises `NotImplementedError`) — this
module exists to nail the algorithm down and get it reviewed before writing
the real thing. Error handling in particular is intentionally left vague
here (see the last numbered step) pending a separate conversation.

This module has no Calendar API dependency and does no fetching of its own
— it's a pure function of the event data it's given. See "The day" below.

## Why "reallocate" and not "cascade"

An earlier draft of this described a domino/cascade model: inserting an
event shrinks or shoves its *immediate* neighbor, which may then shove
*its* neighbor, and so on. That's the wrong mental model for a calendar
that's usually completely full and is meant to reflect a day's actual
priorities: it only ever looks one hop away, so it has no way to prefer
shrinking a low-priority event somewhere else in the day over shoving a
high-priority one right next door.

Reallocation instead treats the day as a fixed pool of time that gets
redistributed as a whole, in one pass, whenever a new event needs room.

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

**Open question, flagged for review rather than decided here:** an event
with `priority=None` (never set) isn't free time and isn't obviously
reclaimable either. This module currently treats an unset priority as
*not reclaimable* — the same as if it were higher priority than anything a
new event could claim — on the theory that reallocation should only ever
touch time the user explicitly marked as lower priority, never time they
didn't get around to prioritizing yet. Reconsider this if it turns out
most events end up without an explicit priority in practice.

## The algorithm

Given `day_events` and a candidate `new_event` (not yet created — no `id`)
with a real `start`, `end`, and (implicitly) `priority`:

1. **Start from `day_events`.** Every existing event `new_event` might need
   to borrow time from — already fetched and handed in by the caller.

2. **Compute how much time is needed.** `duration = new_event.end -
   new_event.start`. The same algorithm runs whether `day_events` is packed
   solid or mostly empty — an empty (or nearly empty) day just means step 4
   reclaims everything it needs from the lowest tier and never reaches a
   real event.

3. **Build the reclaim pool.** Every span among `day_events` — each
   existing event, plus every gap between/around them — that has priority
   >= `new_event.priority` (gaps always qualify, per their `math.inf` tier)
   is a candidate. A span with strictly better (lower-numbered) priority
   than `new_event`, or an event with `priority=None` per the open question
   above, is off the table entirely — never touched, no matter how much
   time is needed.

4. **Reclaim greedily, worst tier first.** Sort the pool by tier
   (descending priority number, `math.inf` first). Within a tier, process
   spans earliest-`start`-first (the simplest deterministic tie-break;
   revisit if it produces surprising results in practice). For each span:
      - A gap contributes up to its full length.
      - A real event contributes up to `current_duration - min_duration`,
        treating an unset `min_duration` as `0` (i.e. the event may be
        shrunk arbitrarily far, down to nothing) — except `0` if
        `is_fixed_duration`, which always overrides `min_duration`.
      - Stop as soon as the running total meets `duration`.

5. **Check for a shortfall.** If the entire eligible pool (every gap, plus
   every eligible event shrunk to its floor) is still less than `duration`,
   reallocation cannot proceed — see "Error handling" below. (This can also
   mean `day_events` didn't actually cover a full day; this module has no
   way to tell the difference and doesn't try to.)

6. **Recompute durations.** Each event touched in step 4 gets a new,
   shrunk duration (`current_duration` minus whatever was reclaimed from
   it, floored per step 4). Every other event is unchanged.

7. **Compact into a single layout.** Walk `day_events` in chronological
   order by original `start`. Place `new_event` at its own requested
   `start`/`end`. Then lay out the remaining events immediately after one
   another in that same original relative order, back-to-back, each
   keeping whatever duration step 6 gave it — no gaps except whatever free
   time step 4 didn't need to consume. This single pass is what actually
   moves anything; nothing is shoved iteratively.

8. **Report the plan.** Return every `Event` that needs to be created or
   updated to realize this layout: `new_event` (unchanged — it's already
   placed where it was asked to be) plus every existing event whose
   duration or position changed. This module doesn't call
   `create_event`/`update_event` itself — applying the plan (and deciding
   what to do if that application partially fails) is the caller's job.

## Error handling — deliberately not decided here

Step 5's shortfall is this module's own main failure mode. What the caller
sees when that happens — exception type, message shape, whether `ToolError`
is involved the way we discussed for `create_event`, whether
`is_end_of_day_sleep` should constrain the algorithm at all yet — is
intentionally left open. That's the next thing to talk through, once this
shape is agreed on. Fetching `day_events` and applying the returned plan
are both the caller's responsibility now too, with their own separate
failure modes this module doesn't need to account for.
"""

from __future__ import annotations

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
    """How many seconds could be reclaimed from `span` (step 4 of the
    algorithm above) if every one of them were needed: 0 if
    `is_fixed_duration`; otherwise `(end - start) - min_duration`, treating
    an unset `min_duration` as `0` (fully reclaimable), floored at 0.

    Only reads `span`'s `Schedulable` fields — it's given a `day_events`
    entry or `new_event` itself, never anything that needs `summary`/
    `description`/`location`/`id`/`is_end_of_day_sleep` to answer this
    question.
    """
    raise NotImplementedError


def reallocate_for_new_event(day_events: list[Event], new_event: Event) -> list[Event]:
    """Make room for `new_event` by reallocating time from the
    lowest-priority events (and free time) in `day_events` — the complete
    list of events for `new_event`'s day, gathered by the caller. Returns
    every `Event` that needs to be created or updated to realize the
    resulting layout: `new_event` plus every existing event whose duration
    or position changed. Doesn't call `create_event`/`update_event` itself
    — applying the plan is the caller's job.

    `new_event` must not have an `id` yet. Raises if `day_events` doesn't
    have enough reclaimable time at or below `new_event`'s own priority to
    fit it (see "Error handling" in the module docstring — not yet
    decided).

    See the module docstring for the full algorithm.
    """
    raise NotImplementedError
