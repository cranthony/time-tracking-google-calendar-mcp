"""Changing events, several at once: creating, updating, shifting and
cancelling them together, in one batch that's checked as a whole and
then written. The event tools (server.py's create_event, update_event and
delete_event) are thin fronts to it, and compaction (utilities/
note_compaction.py) checks its plans with the same guard.

**Nothing is moved to make room.** The server never shifts, shrinks,
splits or cancels an event the batch doesn't name. Instead it refuses a
batch whose result would have an event overlap another, or take no time
-- and the refusal says everything needed to send a valid one at once:
every problem, the rule, and the stretch of calendar the batch touches as
it would leave it (`rejection`). Overlaps between events the batch
doesn't move or create are left alone: it's what a batch changes that it
answers for.

**What a batch holds.** Updates (an event's id and the fields to set or
clear), creates, cancels (each saying whether it counts against
follow-through -- see utilities/cancellations.py), and shifts: several
events moved by the same amount, which become updates of each. An event
appears in at most one of them.

**History.** An event compaction settled (`Event.compacted_until`) can't
be changed or cancelled unless the batch says history may be changed
(`allow_compacted`) -- except an event still going on when it was
compacted, which may still run on: an update that only moves its end, to
no earlier than its `compacted_until`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from calendar_clients.google_calendar import Event
from utilities.compaction_timeline import Timeline, TimelineEvent, build_timeline

RESEND_RULE = (
    "Nothing was changed. Resend the whole batch so that, together, it is valid: every event it "
    "creates or updates must end after it starts, and must not overlap any other event -- neither one "
    "already on the calendar nor another in the batch. Fix each problem below by moving, shortening or "
    "cancelling events in the same call (each cancel saying whether it counts against follow-through), "
    "and check the result against every event listed here, not just the ones named."
)
"""Opens every refusal of a batch -- see `rejection`."""


class Problem(str):
    """One thing wrong, as a message, tagged with the `category` of
    mistake it is -- so a `ChangeError` made of several can say which
    kinds it holds. It's still a `str`, so a list of them joins like any
    other list of messages."""

    category: str

    def __new__(cls, category: str, message: str) -> "Problem":
        problem = super().__new__(cls, message)
        problem.category = category
        return problem


class ChangeError(ValueError):
    """Changes that can't be made as asked. The message says what to fix,
    and lists the valid choices where there are any -- it's written to be
    read by the model that asked for them.

    `categories` names the kinds of mistake it holds ("overlap",
    "unknown_event", ...), sorted, for the server to log every refusal by
    -- so the logs show which instructions a model gets wrong."""

    def __init__(self, message: str, *, category: str = "other") -> None:
        super().__init__(message)
        self.categories: list[str] = [category]

    @classmethod
    def of(cls, problems: list[str], category: str = "other", *, text: str | None = None) -> "ChangeError":
        """One error for all of `problems`: each a `Problem` with its own
        category, or a plain message that's `category`. `text`, if given,
        is the message instead of the problems joined."""
        error = cls(text if text is not None else "\n".join(problems), category=category)
        error.categories = sorted({getattr(p, "category", category) for p in problems})
        return error

    @classmethod
    def wrapping(cls, message: str, cause: "ChangeError") -> "ChangeError":
        """`cause` reworded as `message`, keeping its categories."""
        error = cls(message)
        error.categories = list(cause.categories)
        return error


@dataclass(kw_only=True)
class Cancel:
    """An event to cancel."""

    event_id: str
    counts_against_follow_through: bool
    """Whether it was a commitment dropped -- recorded against the
    follow-through of the people a follow-through part tracks it for --
    rather than just a change of plan."""


@dataclass(kw_only=True)
class Shift:
    """Events to move by the same amount, keeping their lengths: an update
    of each."""

    event_ids: list[str]
    minutes: int
    """How far: later if positive, earlier if negative."""


@dataclass(kw_only=True)
class Placed:
    """An event as a batch would leave it, for the overlap guard: `moved`
    if the batch creates it or changes its times."""

    event: Event
    key: str
    moved: bool = False
    asked: bool = False
    """Whether the batch names it, created or changed in any way -- marked
    in the refusal's listing."""


def overlap_problems(placed: list[Placed], *, describe=None) -> list[Problem]:
    """Every pair of `placed` events that overlap where one of them is
    `moved` -- and every moved one that takes no time. `describe` names
    an event in a message (by default its title and times)."""
    describe = describe or _describe
    problems: list[Problem] = []
    for p in placed:
        if p.moved and p.event.end <= p.event.start:
            problems.append(
                Problem(
                    "nonpositive_length",
                    f"{describe(p)} would end no later than it starts: give it some time, or cancel it.",
                )
            )
    ordered = sorted(placed, key=lambda p: (p.event.start, p.event.end))
    for i, earlier in enumerate(ordered):
        for later in ordered[i + 1:]:
            if later.event.start >= earlier.event.end:
                break
            if (earlier.moved or later.moved) and earlier.event.end > earlier.event.start and later.event.end > later.event.start:
                problems.append(Problem("overlap", f"{describe(earlier)} overlaps {describe(later)}."))
    return problems


def rejection(problems: list[str], listing: list[Placed], *, since: datetime, until: datetime) -> ChangeError:
    """The refusal of a batch with `problems`: the rule, every problem, and
    `listing` -- the events from `since` to `until`, as the batch would
    leave them, those it names starred -- so a valid batch can be sent at
    once (see the module docstring)."""
    tz = since.tzinfo

    def hm(moment: datetime) -> str:
        return moment.astimezone(tz).strftime("%H:%M")

    lines = [RESEND_RULE, "", "Problems:", *(f"- {p}" for p in problems), ""]
    lines.append(
        f"The calendar from {since.astimezone(tz).strftime('%a %d %b %H:%M')} to "
        f"{until.astimezone(tz).strftime('%a %d %b %H:%M')}, as this batch would leave it (* = in this batch):"
    )
    for p in sorted(listing, key=lambda p: (p.event.start, p.event.end)):
        notes = []
        if p.event.is_end_of_day_sleep:
            notes.append("end of day")
        if p.event.compacted_until is not None:
            notes.append(f"compacted until {hm(p.event.compacted_until)}")
        lines.append(
            f"  {hm(p.event.start)}–{hm(p.event.end)}  {p.event.summary or '(no title)'}{' *' if p.asked else ''}"
            f"  {p.event.id or p.key}" + (f"  ({', '.join(notes)})" if notes else "")
        )
    return ChangeError.of(problems, "overlap", text="\n".join(lines))


def _describe(p: Placed) -> str:
    tz = p.event.start.tzinfo
    span = f"{p.event.start.astimezone(tz).strftime('%H:%M')}–{p.event.end.astimezone(tz).strftime('%H:%M')}"
    name = repr(p.event.summary) if p.event.summary else (p.event.id or p.key)
    return f"{name} ({span}{', as asked' if p.moved else ''})"


@dataclass(kw_only=True)
class Change:
    """One change a batch makes: `before` is the event as it was (none for
    a create), `after` as it will be (none for a cancel)."""

    action: str
    """"create", "update" or "cancel"."""

    before: Event | None = None
    after: Event | None = None
    patch: Event | None = None
    """What an update sends: only what it sets (and clears)."""

    counts_against_follow_through: bool = False
    moved: bool = False
    """For an update: whether it changes the event's times."""


@dataclass(kw_only=True)
class Batch:
    """A batch checked and ready to write -- see `EventChanges.check`."""

    changes: list[Change] = field(default_factory=list)
    listing: list[Placed] = field(default_factory=list)
    since: datetime | None = None
    until: datetime | None = None


class EventChanges:
    """Checks and writes batches of event changes -- see the module
    docstring."""

    def __init__(self, client, cancellations=None) -> None:
        """`client` reads and writes events, actions filled in (an
        ActionCalendar); `cancellations`, if given, records those that
        count against follow-through (see utilities/cancellations.py)."""
        self._client = client
        self._cancellations = cancellations

    def check(
        self,
        updates: Iterable[Event] = (),
        creates: Iterable[Event] = (),
        cancels: Iterable[Cancel] = (),
        shifts: Iterable[Shift] = (),
        *,
        allow_compacted: bool = False,
    ) -> Batch:
        """The batch these make, checked -- or ChangeError, naming every
        problem, before anything's written."""
        updates, creates, cancels, shifts = list(updates), list(creates), list(cancels), list(shifts)
        problems: list[str] = []
        named: dict[str, str] = {}

        def claim(event_id: str | None, how: str) -> bool:
            if not event_id:
                problems.append(Problem("malformed_change", f"{how} needs an event id"))
                return False
            if event_id in named:
                problems.append(
                    Problem("duplicate_change", f"event {event_id} is in the batch twice ({named[event_id]} and {how}): give it one change")
                )
                return False
            named[event_id] = how
            return True

        current: dict[str, Event] = {}

        def load(event_id: str) -> Event | None:
            if event_id not in current:
                try:
                    current[event_id] = self._client.get_event(event_id)
                except Exception:  # Any failure to find it: reported as unknown.
                    problems.append(Problem("unknown_event", f"there's no event {event_id!r}"))
                    return None
            event = current[event_id]
            if event.status == "cancelled":
                problems.append(Problem("unknown_event", f"event {event_id} ({event.summary!r}) is cancelled already"))
                return None
            return event

        # A shift is an update of each of its events.
        patches: list[tuple[Event, str]] = [(patch, "an update") for patch in updates]
        for shift in shifts:
            by = timedelta(minutes=shift.minutes)
            for event_id in shift.event_ids:
                if (event := load(event_id)) is not None:
                    patches.append((Event(id=event_id, start=event.start + by, end=event.end + by), "a shift"))
        changes: list[Change] = []
        for patch, how in patches:
            if not claim(patch.id, how):
                continue
            before = load(patch.id)
            if before is None:
                continue
            after = _patched(before, patch)
            if before.compacted_until is not None and not allow_compacted and not _only_runs_on(before, after):
                problems.append(_compacted(before, "changed"))
                continue
            changes.append(
                Change(
                    action="update", before=before, after=after, patch=patch,
                    moved=(after.start, after.end) != (before.start, before.end),
                )
            )
        for cancel in cancels:
            if not claim(cancel.event_id, "a cancel"):
                continue
            before = load(cancel.event_id)
            if before is None:
                continue
            if before.compacted_until is not None and not allow_compacted:
                problems.append(_compacted(before, "cancelled"))
                continue
            changes.append(
                Change(action="cancel", before=before, counts_against_follow_through=cancel.counts_against_follow_through)
            )
        for number, event in enumerate(creates, start=1):
            if event.start is None or event.end is None:
                problems.append(Problem("malformed_change", f"new event {number} ({event.summary!r}) needs a start and an end"))
                continue
            changes.append(Change(action="create", after=replace(event, id=None)))
        if problems:
            raise ChangeError.of(problems, "malformed_change")

        batch = Batch(changes=changes)
        spans = [
            (e.start, e.end)
            for c in changes
            if c.action == "create" or c.moved
            for e in (c.before, c.after)
            if e is not None
        ]
        if not spans:
            return batch
        since = min(s for s, _ in spans)
        until = self._day_end(max(e for _, e in spans))
        listed = [e for e in self._client.list_events(since, until) if e.status != "cancelled"]
        changed = {c.before.id: c for c in changes if c.before is not None}
        placed = []
        for event in listed:
            change = changed.get(event.id)
            if change is None:
                placed.append(Placed(event=event, key=event.id))
            elif change.action == "update":
                placed.append(Placed(event=change.after, key=event.id, moved=change.moved, asked=True))
        for number, change in enumerate((c for c in changes if c.action == "create"), start=1):
            placed.append(Placed(event=change.after, key=f"new {number}", moved=True, asked=True))
        # A moved event listed from its new time but not its old one.
        listed_ids = {e.id for e in listed}
        for change in changes:
            if change.action == "update" and change.before.id not in listed_ids and change.after.end > since:
                placed.append(Placed(event=change.after, key=change.before.id, moved=change.moved, asked=True))
        problems = overlap_problems(placed)
        if problems:
            raise rejection(problems, placed, since=since, until=until)
        batch.listing, batch.since, batch.until = placed, since, until
        return batch

    def apply(self, batch: Batch, source: str) -> list[Event]:
        """Write `batch`'s changes; the events as written. Cancellations
        that count against follow-through are recorded, as made by
        `source` (the tool)."""
        written = []
        for change in batch.changes:
            if change.action == "update":
                written.append(self._client.update_event(change.patch))
            elif change.action == "create":
                written.append(self._client.create_event(change.after))
            else:
                written.append(self._client.update_event(Event(id=change.before.id, status="cancelled")))
                if change.counts_against_follow_through and self._cancellations is not None:
                    self._cancellations.record(change.before, source)
        return written

    def _day_end(self, moment: datetime) -> datetime:
        """The end of the night after `moment` -- as far as one change can
        reach into what follows it -- or a day on, without one."""
        ahead = moment + timedelta(hours=24)
        nights = [
            e for e in self._client.list_events(moment, ahead)
            if e.is_end_of_day_sleep and e.status != "cancelled" and e.end > moment
        ]
        return min((e.end for e in nights), default=ahead)


def timeline(batch: Batch) -> Timeline | None:
    """`batch`'s changes beside the events around them, drawn as
    compaction draws its plans (see utilities/compaction_timeline.py):
    moved events with how far, new ones, cancelled ones."""
    by_id = {c.before.id: c for c in batch.changes if c.before is not None}
    shown: list[TimelineEvent] = []
    for p in batch.listing:
        change = by_id.get(p.event.id)
        before = change.before if change is not None else p.event
        status = "new" if p.event.id is None else "adjusted" if change is not None and change.moved else "planned"
        shown.append(
            TimelineEvent(
                summary=p.event.summary or p.key, status=status, event_id=p.event.id,
                start=p.event.start, end=p.event.end,
                planned_start=None if p.event.id is None else before.start,
                planned_end=None if p.event.id is None else before.end,
            )
        )
    for change in batch.changes:
        if change.action == "cancel":
            shown.append(
                TimelineEvent(
                    summary=change.before.summary or change.before.id, status="cancelled", event_id=change.before.id,
                    planned_start=change.before.start, planned_end=change.before.end,
                )
            )
    return build_timeline([], shown, None) if shown else None


def _patched(before: Event, patch: Event) -> Event:
    """`before` with `patch`'s set fields, and those it clears removed."""
    changes: dict[str, Any] = {
        name: value
        for name, value in vars(patch).items()
        if name not in ("id", "cleared") and value is not None and value != getattr(Event(), name, None)
    }
    after = replace(before, **changes)
    for name in patch.cleared:
        setattr(after, name, None)
    return after


def _only_runs_on(before: Event, after: Event) -> bool:
    """Whether `after` only lets `before`, an event compacted while it was
    going on, run on: its end moved, to no earlier than what was settled."""
    if before.compacted_until is None or before.compacted_until >= before.end:
        return False
    return (
        replace(after, end=before.end) == before
        and after.end >= before.compacted_until
    )


def _compacted(event: Event, how: str) -> Problem:
    return Problem(
        "compacted",
        f"{event.summary!r} ({event.id}) is history -- compaction settled it until "
        f"{event.compacted_until.isoformat()} -- so it can't be {how} unless the user has explicitly approved "
        "changing history (allow_compacted_changes). An event still going on may run on: move only its end, "
        "to no earlier than that.",
    )
