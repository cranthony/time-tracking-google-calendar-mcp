"""Reallocation: how creating an event makes room for itself.

**Status: design document.** `reallocate_for_new_event` below is fully
specified but not yet implemented (it raises `NotImplementedError`) — this
module exists to nail the algorithm down and get it reviewed before writing
the real thing. Error handling in particular is intentionally left vague
here (see the last numbered step) pending a separate conversation.

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

## The window

Reallocating for a new event only ever looks at the 24 hours starting at
that event's own `start` — not "now" (the new event's start may be in the
past) and not a calendar-day boundary (which would need a timezone; we
deliberately don't track one). 24 hours comfortably reaches into the next
day's events regardless of what day/time the new event is anchored to,
without needing to know where "midnight" falls:

    window = (new_event.start, new_event.start + timedelta(hours=24))

## Priority tiers, and free time as the lowest tier

Every event in the window has a `priority` (lower number = more important;
see `Event.priority`). Gaps between events — unscheduled time — are treated
as occupying the *lowest possible* priority tier, below every real event,
represented as `math.inf`. That single rule is what makes "prefer to shrink
the lowest-priority things first" also mean "use up empty space before
touching any real event" — free time always sorts last, with no special
case needed.

**Open question, flagged for review rather than decided here:** an event
with `priority=None` (never set) isn't free time and isn't obviously
reclaimable either. This module currently treats an unset priority as
*not reclaimable* — the same as if it were higher priority than anything a
new event could claim — on the theory that reallocation should only ever
touch time the user explicitly marked as lower priority, never time they
didn't get around to prioritizing yet. Reconsider this if it turns out
most events end up without an explicit priority in practice.

## The algorithm

Given a candidate `new_event` (not yet created — no `id`) with a real
`start`, `end`, and (implicitly) `priority`:

1. **Fetch the window.** `client.list_events(new_event.start, new_event.start
   + timedelta(hours=24))` — every existing event `new_event` might need to
   borrow time from.

2. **Compute how much time is needed.** `duration = new_event.end -
   new_event.start`. The same algorithm runs whether the window is packed
   solid or mostly empty — an empty (or nearly empty) window just means step
   4 reclaims everything it needs from the lowest tier and never reaches a
   real event.

3. **Build the reclaim pool.** Every span in the window — each existing
   event, plus every gap between/around them (including before the first
   event and after the last, within the window) — that has priority >=
   `new_event.priority` (gaps always qualify, per their `math.inf` tier) is
   a candidate. A span with strictly better (lower-numbered) priority than
   `new_event`, or an event with `priority=None` per the open question
   above, is off the table entirely — never touched, no matter how much
   time is needed.

4. **Reclaim greedily, worst tier first.** Sort the pool by tier
   (descending priority number, `math.inf` first). Within a tier, process
   spans earliest-`start`-first (the simplest deterministic tie-break;
   revisit if it produces surprising results in practice). For each span:
      - A gap contributes up to its full length.
      - A real event contributes up to `current_duration - min_duration`
        (zero if `is_fixed_duration`, or if `min_duration` is unset —
        treat an unset `min_duration` as "may not be shrunk").
      - Stop as soon as the running total meets `duration`.

5. **Check for a shortfall.** If the entire eligible pool (every gap, plus
   every eligible event shrunk to its floor) is still less than `duration`,
   reallocation cannot proceed — see "Error handling" below.

6. **Recompute durations.** Each event touched in step 4 gets a new,
   shrunk duration (`current_duration` minus whatever was reclaimed from
   it, floored at `min_duration`). Every other event in the window is
   unchanged.

7. **Compact the window into a single layout.** Walk the window's events
   in chronological order by original `start`. Place `new_event` at its own
   requested `start`/`end`. Then lay out the remaining events immediately
   after one another in that same original relative order, back-to-back,
   each keeping whatever duration step 6 gave it — no gaps except whatever
   free time step 4 didn't need to consume. This single pass is what
   actually moves anything; nothing is shoved iteratively.

8. **Apply and report.** `create_event(new_event)`, `update_event(...)` for
   every event whose duration or position changed, and return every
   affected `Event` (new plus updated) — matching the `list[Event]` return
   contract already used by `create_event`/`update_event`/`delete_event`.

## Error handling — deliberately not decided here

Step 5's shortfall is the main failure mode; there may be others (e.g. the
window fetch itself failing). What the caller sees when that happens —
exception type, message shape, whether `ToolError` is involved the way we
discussed for `create_event`, whether `is_end_of_day_sleep` should
constrain the algorithm at all yet — is intentionally left open. That's
the next thing to talk through, once this shape is agreed on.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

from calendar_clients.google_calendar import CalendarClient, Event

WINDOW = timedelta(hours=24)
"""How far past `new_event.start` reallocation looks for events to
reclaim time from. See "The window" above for why this is a fixed
duration rather than a calendar-day boundary."""


class Schedulable(Protocol):
    """The fields of an event that reallocation's algorithm actually reads:
    enough to place it in time, weigh it against other spans, and know how
    far it can be shrunk. `Event` satisfies this structurally — Protocols
    are duck-typed, so no inheritance is needed — and so would any other
    event-shaped object with a matching set of attributes.

    Typing reallocation's internal reasoning against `Schedulable` rather
    than `Event` directly keeps its real dependency surface visible: none of
    it needs `summary`, `description`, `location`, `id`, or
    `is_end_of_day_sleep` to decide what gets reclaimed. Python has no way
    to restrict a class's fields to one specific importer, so this is a
    static-typing aid rather than a runtime restriction — nothing in this
    codebase runs a type checker yet, so today it documents intent for a
    reader rather than being enforced. A function typed to take a
    `Schedulable` still receives a full `Event` at the call site; it just
    isn't supposed to look past these fields.
    """

    start: datetime
    end: datetime
    priority: int | None
    min_duration: timedelta | None
    is_fixed_duration: bool | None


def _reclaimable_seconds(span: Schedulable) -> float:
    """How many seconds could be reclaimed from `span` (step 4 of the
    algorithm above) if every one of them were needed: 0 if
    `is_fixed_duration`, or if `min_duration` is unset (an unset
    `min_duration` means "may not be shrunk," per the module docstring);
    otherwise `(end - start) - min_duration`, floored at 0.

    Only reads `span`'s `Schedulable` fields — it's given a window event or
    `new_event` itself, never anything that needs `summary`/`description`/
    `location`/`id`/`is_end_of_day_sleep` to answer this question.
    """
    raise NotImplementedError


def reallocate_for_new_event(client: CalendarClient, new_event: Event) -> list[Event]:
    """Make room for `new_event` by reallocating time from the lowest-priority
    events (and free time) in the 24 hours starting at `new_event.start`, then
    create it. Returns every `Event` affected: the newly created event plus
    every existing event whose duration or position changed.

    `new_event` must not have an `id` yet. Raises if the window doesn't have
    enough reclaimable time at or below `new_event`'s own priority to fit it
    (see "Error handling" in the module docstring — not yet decided).

    See the module docstring for the full algorithm.
    """
    raise NotImplementedError
