from __future__ import annotations

import functools
import logging
import os
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Literal, ParamSpec, TypeVar
from zoneinfo import ZoneInfo

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp

from calendar_clients.google_calendar import CalendarClient, Event, EventLabelConflictError, TimeZoneNotSetError
from calendar_clients.google_sheets import cached_sheet_reads
from calendar_clients.write_lock import WRITE_LOCK
from config import (
    build_calendar_client,
    build_compaction_journal,
    build_goals,
    build_noted_time_sheet,
    get_allowed_user_ids,
    get_cors_allowed_origins,
    get_mcp_resource_url,
    get_workos_authkit_domain,
)
from oauth_proxy import oauth_proxy_handlers
from utilities.goal_calendar import GoalCalendar, fill_in_from_goals
from utilities.goal_health import Assessment, GoalHealth
from utilities.reflection import ReflectionContext, ReflectionResult, Reflections
from utilities.goal_sheet import Goal, GoalStatus
from utilities.goals import CreatedGoal, GoalList, Goals, GoalTree
from utilities.memory_diagnostics import track
from utilities.note_compaction import CompactionError, EventDecision
from utilities.compaction_journal import CompactionJournal
from utilities.compaction_marker import CompactionMarker
from utilities.note_compactor import CompactionContext, CompactionResult, NoteCompactor
from utilities.noted_time_sheet import NotedTime, NotedTimeSheet, NoteWithId
from utilities.reallocation import ReallocationOptions
from utilities.reallocating_calendar import ReallocatingCalendar
from utilities.recurrences import Recurrences, Repeat, describe_rules
from workos_auth import WorkOSTokenVerifier

# Without this, INFO-level logs (utilities/memory_diagnostics.py's, e.g.)
# are silently dropped -- the root logger defaults to WARNING with no
# handler. Its default StreamHandler writes to stderr, never stdout, so
# this is safe under the stdio transport too, whose protocol messages
# themselves go over stdout.
logging.basicConfig(level=logging.INFO)

# "stdio" (the default) is for local use -- a client spawns this process
# directly (Claude Desktop's local config, `mcp dev`). "streamable-http" is
# for hosting this remotely (e.g. on Render); see the README's "Deploying"
# section. Read once, at import time, since it also decides how MCPServer
# itself gets constructed below.
_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio")

if _TRANSPORT == "streamable-http":
    _resource_url = get_mcp_resource_url()
    mcp = MCPServer(
        "time-tracking-google-calendar-mcp",
        token_verifier=WorkOSTokenVerifier(
            authkit_domain=get_workos_authkit_domain(),
            resource=_resource_url,
            allowed_user_ids=get_allowed_user_ids(),
        ),
        auth=AuthSettings(
            issuer_url=get_workos_authkit_domain(),
            resource_server_url=_resource_url,
            required_scopes=[],
            # False: WorkOSTokenVerifier already checks the token's audience
            # itself (via jwt.decode's audience=), so the SDK doesn't need
            # to check AccessToken.resource against resource_server_url too.
            validate_token_resource=False,
        ),
    )
    # For browser-based clients only; see oauth_proxy.py for why.
    for _path, _handler in oauth_proxy_handlers(get_workos_authkit_domain()).items():
        mcp.custom_route(_path, methods=["POST"])(_handler)
else:
    mcp = MCPServer("time-tracking-google-calendar-mcp")

INTERNAL_EVENT_FIELDS = frozenset(
    {"status", "goal_priority", "recurrence", "time_zone", "original_start", "cleared"}
)
"""Event fields the agent talking to this server should never see or set,
at all -- not just left null. A series' recurrence and time_zone are seen
and set through PublicRecurrence instead (see get_recurrence),
original_start is only used to split one, and cleared is set through
update_event's and update_recurrence's clear_fields. Enforced by PublicEvent actually lacking
these fields (so they never appear in a tool's schema or result), not by
convention -- see PublicEvent below. tests/test_server.py's
TestPublicEvent asserts these are exactly the fields PublicEvent is
missing relative to Event, so this stays in sync with PublicEvent."""


@dataclass(kw_only=True)
class PublicEvent:
    """Event, minus the fields named in INTERNAL_EVENT_FIELDS, plus
    is_cancelled (which has no Event equivalent -- Event's status is one
    of INTERNAL_EVENT_FIELDS, hidden entirely). Every MCP tool
    returns/accepts this instead of Event directly, so those fields never
    appear in the tool schema the agent sees (via tools/list) or in any
    tool result -- the agent has no way to know they exist, not just that
    their value is hidden.

    is_cancelled only ever moves from False to True: setting it False has
    no effect (see to_event), since there's no way to un-cancel a
    cancelled event.

    goal_ids are the goals the event serves, primary goal first; setting
    them is how an event is tied to goals. goal_names (their names, in
    the same order) and event_label_id (derived from the primary goal --
    see utilities/goal_calendar.py) are read-only: to_event ignores them.

    goals_from_label is true when an event was never given goals, so its
    goal_ids are inferred from its label (the goal that owns the label),
    not stored. Sending it back with goals_from_label still true leaves
    them inferred, whatever goal_ids says; to store goals, send goal_ids
    with goals_from_label false (or left out).

    effective_priority is read-only: Event's property of the same name,
    the priority reallocation actually uses, i.e. priority falling back,
    when the event doesn't set one, to the highest (lowest-numbered)
    priority among its goals (each goal's own, or its nearest
    ancestor's). Kept separate from priority so that sending a listed
    event straight back to update_event never copies its goals' priority
    onto the event itself, which would stop it following later changes to
    the goals. to_event ignores it.

    is_end_of_day_sleep/recurring_event_id are read-only too, and
    to_event ignores them as well. is_end_of_day_sleep decides where a
    day ends for reallocation (see utilities/reallocating_calendar.py),
    so a wrong mark would quietly change what later updates shrink, move
    or cancel; it's only set by hand, via calendar_cli.py.
    recurring_event_id is assigned by Google and can't be set at all."""

    id: str | None = None
    summary: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    description: str | None = None
    location: str | None = None
    min_duration: timedelta | None = None
    is_fixed_duration: bool | None = None
    is_fixed_time: bool | None = None
    priority: int | None = None
    goal_ids: list[str] | None = None
    goal_names: list[str] | None = None
    goals_from_label: bool = False
    event_label_id: str | None = None
    is_cancelled: bool = False
    effective_priority: int | None = None
    is_end_of_day_sleep: bool | None = None
    recurring_event_id: str | None = None

    @classmethod
    def from_event(cls, event: Event, tree: GoalTree | None = None) -> "PublicEvent":
        """`tree` fills in goal_names; without it they're left `None`."""
        names = None
        if tree is not None and event.goal_ids is not None:
            names = [
                tree.by_id[i].name if i in tree.by_id else f"(unknown goal {i})" for i in event.goal_ids
            ]
        return cls(
            id=event.id,
            summary=event.summary,
            start=event.start,
            end=event.end,
            description=event.description,
            location=event.location,
            min_duration=event.min_duration,
            is_fixed_duration=event.is_fixed_duration,
            is_fixed_time=event.is_fixed_time,
            priority=event.priority,
            goal_ids=event.goal_ids,
            goal_names=names,
            goals_from_label=event.goals_from_label,
            event_label_id=event.event_label_id,
            is_cancelled=event.status == "cancelled",
            effective_priority=event.effective_priority,
            is_end_of_day_sleep=event.is_end_of_day_sleep,
            recurring_event_id=event.recurring_event_id,
        )

    def to_event(self, clear_fields: Collection[str] = ()) -> Event:
        """`clear_fields`: see Event.cleared. Raises ValueError for one
        that can't be cleared, or that's also set."""
        return Event(
            id=self.id,
            summary=self.summary,
            start=self.start,
            end=self.end,
            description=self.description,
            location=self.location,
            min_duration=self.min_duration,
            is_fixed_duration=self.is_fixed_duration,
            is_fixed_time=self.is_fixed_time,
            # Inferred goals, sent back, aren't the event's to store.
            goal_ids=None if self.goals_from_label else self.goal_ids,
            priority=self.priority,
            status="cancelled" if self.is_cancelled else None,
            cleared=frozenset(clear_fields),
        )


@dataclass(kw_only=True)
class PublicRecurrence:
    """A recurring series of events, as a whole -- see utilities/
    recurrences.py. id is the series' own id, which is every one of its
    events' recurring_event_id. start/end are when its first event starts
    and ends; time_zone keeps its events at the same wall-clock time
    across daylight saving changes. repeat says which days its events
    fall on and when it ends (see Repeat), and schedule says that in
    words. A series made elsewhere may repeat in ways repeat can't say
    (e.g. "the last weekday of the month"): its repeat is then null and
    its schedule shows its raw RFC 5545 rules; leave repeat out when
    updating it to keep them. The other fields are as for PublicEvent,
    and apply to every event in the series except where one was edited
    on its own -- until the series is next edited, which resets them
    (see update_recurrence). goals_from_label is as for PublicEvent.

    schedule, goal_names, event_label_id and effective_priority are
    read-only: update_recurrence ignores them, as it does time_zone."""

    id: str | None = None
    summary: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    time_zone: str | None = None
    repeat: Repeat | None = None
    schedule: str | None = None
    description: str | None = None
    location: str | None = None
    min_duration: timedelta | None = None
    is_fixed_duration: bool | None = None
    is_fixed_time: bool | None = None
    priority: int | None = None
    goal_ids: list[str] | None = None
    goal_names: list[str] | None = None
    goals_from_label: bool = False
    event_label_id: str | None = None
    effective_priority: int | None = None

    @classmethod
    def from_event(cls, event: Event, tree: GoalTree) -> "PublicRecurrence":
        public = PublicEvent.from_event(event, tree)
        zone = ZoneInfo(event.time_zone) if event.time_zone else None
        # A series always has a time zone; UTC is only for one that somehow doesn't.
        repeat = Repeat.from_rules(event.recurrence, zone or timezone.utc)
        return cls(
            id=event.id,
            summary=event.summary,
            start=event.start.astimezone(zone) if zone and event.start else event.start,
            end=event.end.astimezone(zone) if zone and event.end else event.end,
            time_zone=event.time_zone,
            repeat=repeat,
            schedule=describe_rules(event.recurrence, zone),
            description=event.description,
            location=event.location,
            min_duration=event.min_duration,
            is_fixed_duration=event.is_fixed_duration,
            is_fixed_time=event.is_fixed_time,
            priority=event.priority,
            goal_ids=event.goal_ids,
            goal_names=public.goal_names,
            goals_from_label=event.goals_from_label,
            event_label_id=event.event_label_id,
            effective_priority=event.effective_priority,
        )

    def to_event(self, clear_fields: Collection[str] = ()) -> Event:
        """`clear_fields`: as for PublicEvent.to_event."""
        return Event(
            id=self.id,
            summary=self.summary,
            start=self.start,
            end=self.end,
            description=self.description,
            location=self.location,
            min_duration=self.min_duration,
            is_fixed_duration=self.is_fixed_duration,
            is_fixed_time=self.is_fixed_time,
            priority=self.priority,
            goal_ids=None if self.goals_from_label else self.goal_ids,
            cleared=frozenset(clear_fields),
        )


# Each get_* helper below builds its object under WRITE_LOCK, the first
# time only: building some writes to Google (creating a spreadsheet or
# tab), and two tool calls arriving together mustn't both build one.
_calendar_client: CalendarClient | None = None
_reallocating_calendar: ReallocatingCalendar | None = None
_goals: Goals | None = None
_goal_health: GoalHealth | None = None
_reflections: Reflections | None = None
_noted_time_sheet: NotedTimeSheet | None = None
_compaction_journal: CompactionJournal | None = None
_note_compactor: NoteCompactor | None = None
_recurrences: Recurrences | None = None


def get_calendar_client() -> CalendarClient:
    """Lazily construct and cache the CalendarClient, so credential loading
    (and the OAuth consent flow, on first run) happens once per process
    rather than on every tool call."""
    global _calendar_client
    if _calendar_client is None:
        with WRITE_LOCK:
            if _calendar_client is None:
                _calendar_client = build_calendar_client()
    return _calendar_client


def get_reallocating_calendar() -> ReallocatingCalendar:
    """Lazily construct and cache the ReallocatingCalendar, the same way
    get_calendar_client caches its CalendarClient. Wraps a GoalCalendar
    (get_calendar_client() plus get_goal_store()) rather than
    get_calendar_client() directly, so reallocation sees an event's
    goal-derived priority as the fallback whenever the event itself
    doesn't set one, and every write derives its label from its goals."""
    global _reallocating_calendar
    if _reallocating_calendar is None:
        with WRITE_LOCK:
            if _reallocating_calendar is None:
                _reallocating_calendar = ReallocatingCalendar(GoalCalendar(get_calendar_client(), get_goal_store()))
    return _reallocating_calendar


def get_goal_health() -> GoalHealth:
    """Lazily construct and cache the GoalHealth, the same way the other
    get_* helpers cache theirs."""
    global _goal_health
    if _goal_health is None:
        with WRITE_LOCK:
            if _goal_health is None:
                _goal_health = GoalHealth(get_calendar_client(), get_goal_store())
    return _goal_health


def get_reflections() -> Reflections:
    """Lazily construct and cache the Reflections, the same way the other
    get_* helpers cache theirs."""
    global _reflections
    if _reflections is None:
        with WRITE_LOCK:
            if _reflections is None:
                _reflections = Reflections(get_goal_health(), get_goal_store(), get_noted_time_sheet())
    return _reflections


def get_goal_store() -> Goals:
    """Lazily construct and cache the Goals, the same way
    get_calendar_client/get_reallocating_calendar cache theirs."""
    global _goals
    if _goals is None:
        with WRITE_LOCK:
            if _goals is None:
                # Goals' recent time is counted up to the last compaction.
                _goals = build_goals(last_compaction=lambda: get_compaction_journal().last_stamped_now())
    return _goals


def get_compaction_journal() -> CompactionJournal:
    """Lazily construct and cache the CompactionJournal, the same way the
    other get_* helpers cache theirs."""
    global _compaction_journal
    if _compaction_journal is None:
        with WRITE_LOCK:
            if _compaction_journal is None:
                _compaction_journal = build_compaction_journal()
    return _compaction_journal


def get_recurrences() -> Recurrences:
    """Lazily construct and cache the Recurrences, the same way the other
    get_* helpers cache theirs. Writes go through a GoalCalendar, so each
    series' label follows its goals."""
    global _recurrences
    if _recurrences is None:
        with WRITE_LOCK:
            if _recurrences is None:
                client = get_calendar_client()
                _recurrences = Recurrences(
                    GoalCalendar(client, get_goal_store()), client.list_instances, client.get_time_zone
                )
    return _recurrences


def _public_recurrences(events: list[Event]) -> list[PublicRecurrence]:
    tree = get_goal_store().tree()
    return [PublicRecurrence.from_event(event, tree) for event in fill_in_from_goals(events, tree)]


def get_noted_time_sheet() -> NotedTimeSheet:
    """Lazily construct and cache the NotedTimeSheet, the same way
    get_goal_store caches its Goals."""
    global _noted_time_sheet
    if _noted_time_sheet is None:
        with WRITE_LOCK:
            if _noted_time_sheet is None:
                _noted_time_sheet = build_noted_time_sheet()
    return _noted_time_sheet


def get_note_compactor() -> NoteCompactor:
    """Lazily construct and cache the NoteCompactor, the same way the
    other get_* helpers cache theirs."""
    global _note_compactor
    if _note_compactor is None:
        with WRITE_LOCK:
            if _note_compactor is None:
                _note_compactor = NoteCompactor(
                    calendar=get_reallocating_calendar(),
                    # Written through goals, so each event's label follows them.
                    client=GoalCalendar(get_calendar_client(), get_goal_store()),
                    goals=get_goal_store(),
                    notes=get_noted_time_sheet(),
                    journal=get_compaction_journal(),
                    # A red event in Google Calendar, at the last compaction.
                    marker=CompactionMarker(get_calendar_client()),
                )
    return _note_compactor


def _public_events(events: list[Event]) -> list[PublicEvent]:
    """`events` as PublicEvents, with goal_ids/goal_names and
    effective_priority filled in from each one's goals -- see
    PublicEvent. Every event tool's result goes through this."""
    tree = get_goal_store().tree()
    return [PublicEvent.from_event(event, tree) for event in fill_in_from_goals(events, tree)]


def _check_goal_ids(event: PublicEvent, *, existing: bool = False) -> None:
    """Refuse goal_ids that don't name goals, suggesting close matches, or
    that add a deleted goal to the event. An `existing` event may keep a
    deleted goal it already has, so sending it back unchanged still works.
    Goals inferred from a label (goals_from_label) aren't written, so aren't
    checked."""
    if not event.goal_ids or event.goals_from_label:
        return
    tree = get_goal_store().tree()
    already: list[str] = []
    if existing and event.id and any(
        tree.by_id[g].status == "deleted" for g in event.goal_ids if g in tree.by_id
    ):
        current = fill_in_from_goals([get_calendar_client().get_event(event.id)], tree)[0]
        already = current.goal_ids or []
    try:
        tree.check_goal_ids(event.goal_ids, for_events=True, already=already)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


P = ParamSpec("P")
R = TypeVar("R")


def _prefetch(*tabs) -> None:
    """Read each of `tabs` -- objects for tabs of the calendar's metadata
    spreadsheet, with a `whole_tab` and a `prefetch` (`Goals`,
    `NotedTimeSheet`, `CompactionJournal`) -- whole, in one read request,
    so the rest of this tool call's reads of them come from its
    `cached_sheet_reads` (see `SheetsClient.prefetch`). For a tool that
    reads more than one tab: Google Sheets caps read requests at 60 a
    minute, and each tab read on its own would cost one. Pinned by
    tests/test_sheet_read_requests.py."""
    tabs[0].prefetch([tab.whole_tab for tab in tabs])


def writes(tool: Callable[P, R]) -> Callable[P, R]:
    """Mark `tool` as one that may write to Google: it holds WRITE_LOCK
    for its whole call (see calendar_clients/write_lock.py), so it never
    interleaves with another tool's writes. A tool without this only
    reads, and runs alongside anything -- and fails, rather than racing,
    if it ever reaches a write."""

    @functools.wraps(tool)
    def locked(*args: P.args, **kwargs: P.kwargs) -> R:
        with WRITE_LOCK:
            return tool(*args, **kwargs)

    locked.writes = True
    return locked


def tool(fn: Callable[P, R]) -> Callable[P, R]:
    """Register `fn` as an MCP tool, like `mcp.tool()`, telling the model
    how to recover if the calendar has no time zone set (see
    `CalendarClient.get_time_zone`): set one, then retry."""

    @functools.wraps(fn)
    def explained(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except TimeZoneNotSetError as exc:
            raise ToolError(
                "The calendar has no time zone set, so its days and times can't be put in the user's local "
                "time. Call set_time_zone with the user's IANA time zone (e.g. \"America/New_York\"), asking "
                f"them for it if you don't know it, then call {fn.__name__} again."
            ) from exc

    return mcp.tool()(explained)


@tool
def list_events(min_time: datetime, max_time: datetime) -> list[PublicEvent]:
    """List events between min_time and max_time. goal_ids are the goals
    each event serves, primary first (goal_names gives their names).
    effective_priority is what reallocation actually uses: the event's
    own priority, falling back to the highest priority of its goals.
    is_end_of_day_sleep marks the sleep event that
    ends a day; recurring_event_id is the id of an instance's recurring
    series (see get_recurrence and update_recurrence to read and edit the
    series as a whole); event_label_id is the calendar label derived from
    its goals.
    goal_names, effective_priority, is_end_of_day_sleep,
    recurring_event_id and event_label_id are read-only: update_event and
    create_event ignore them."""
    with track("list_events"), cached_sheet_reads():
        events = get_calendar_client().list_events(min_time, max_time)
        return _public_events([event for event in events if event.status != "cancelled"])


@tool
def get_event(id: str) -> PublicEvent:
    """Get a single event by its ID. See list_events for its fields."""
    with track("get_event"), cached_sheet_reads():
        event = get_calendar_client().get_event(id)
        if event.status == "cancelled":
            raise ToolError(f"Event {id} has been cancelled.")
        return _public_events([event])[0]


EventField = Literal["description", "location", "min_duration", "is_fixed_duration", "is_fixed_time", "priority"]
"""Every event field update_event and update_recurrence can clear (see
calendar_clients/google_calendar.py's CLEARABLE_EVENT_FIELDS)."""


@tool
@writes
def update_event(
    event: PublicEvent, clear_fields: list[EventField] | None = None, reallocate: bool = True
) -> list[PublicEvent]:
    """Update an existing event, reallocating time from the rest of its
    day as needed to make room for its new position. With reallocate
    false, it's moved only if nothing else has to change: otherwise
    nothing is written, and the error lists what would have changed. An
    update that doesn't move it (start and end left out, or unchanged)
    changes only its other fields: nothing else is touched, even on a day
    whose events overlap. Fields left out keep their current value; list
    one in clear_fields to remove it instead (clearing priority makes the
    event follow its goals' priority again; clearing min_duration lets it
    shrink to nothing). Set goal_ids to change its goals ([] for none).
    Returns the events affected by the update."""
    with track("update_event"), cached_sheet_reads():
        _check_goal_ids(event, existing=True)
        try:
            applied = get_reallocating_calendar().update_event(
                event.to_event(clear_fields or ()), ReallocationOptions(), reallocate=reallocate
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return _public_events(applied)


@tool
def get_recurrence(id: str) -> PublicRecurrence:
    """A recurring series, by its id or the id of any of its events (an
    event's recurring_event_id is its series' id). See PublicRecurrence
    for its fields."""
    with track("get_recurrence"), cached_sheet_reads():
        try:
            return _public_recurrences([get_recurrences().series(id)])[0]
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def update_recurrence(
    recurrence: PublicRecurrence,
    starting_at_event_id: str | None = None,
    clear_fields: list[EventField] | None = None,
) -> list[PublicRecurrence]:
    """Edit a recurring series (recurrence.id is its id, or any of its
    events'): every field given is set, those left out are kept, and
    those in clear_fields are removed (as for update_event). Applies
    to all its events -- or, with starting_at_event_id, to that event and
    the ones after it only ("this and following"): the series is split
    there (see split_recurrence) and only the later part is edited.
    start/end are its first event's, as get_recurrence gave them; when
    it's split, the later part moves by as much as they changed. repeat,
    if given, replaces how it repeats whole (including count/until and
    skipped/added), so send every part of it you want kept. Series aren't
    reallocated. Returns the edited series, then the earlier part if it
    was split.

    Events edited on their own don't keep those edits (found with
    probes/series_edits.py): any edit resets every field but their times
    to the series' -- even fields it leaves out, so an event's own
    priority, goal_ids or description are lost to a goals-only edit --
    and an edit to start/end moves them back onto the series' times too.
    Leave start/end out to keep each event's own time. A field left out
    is kept on the series itself, priority and goal_ids included."""
    with track("update_recurrence"), cached_sheet_reads():
        _check_goal_ids(recurrence, existing=True)
        try:
            updated = get_recurrences().update(
                recurrence.to_event(clear_fields or ()), starting_at_event_id, recurrence.repeat
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return _public_recurrences(updated)


@tool
@writes
def split_recurrence(event_id: str) -> list[PublicRecurrence]:
    """Split the recurring series event_id is one of at that event: the
    series ends just before it, and a copy starts at it, so the two can be
    edited apart. A COUNT is shared between them; events after the split
    that were edited on their own lose those edits, as in Google Calendar.
    Returns the series from the event on, then the one before it (none if
    it was the series' first event, which leaves nothing to split)."""
    with track("split_recurrence"), cached_sheet_reads():
        try:
            earlier, later = get_recurrences().split(event_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return _public_recurrences([later] + ([earlier] if earlier else []))


@tool
@writes
def delete_recurrence(id: str, starting_at_event_id: str | None = None) -> list[PublicRecurrence]:
    """Delete a recurring series (id is its id, or any of its events'):
    every one of its events, past ones and any edited on their own
    included. Or, with starting_at_event_id, delete that event and the
    ones after it only ("this and following"): the series is ended just
    before it, and the events before it are kept as they are. To delete
    one event of a series, use delete_event with that event's id instead.
    Returns what's left of the series: nothing if it was deleted whole
    (or from its first event on), else the series, now ending before the
    event.

    The two leave different things behind. Deleting a series whole
    cancels each of its events, as delete_event does one: they stay on
    the calendar as cancelled events, hidden from the user and from
    list_events, and each counts against its goals' follow_through
    measures as a cancellation -- past events too, whose time is then no
    longer counted as spent. Deleting this and following leaves no
    cancelled events: the series' events from that one on are gone (one
    edited on its own goes by where the series first put it, even if
    moved earlier), so follow_through doesn't count them at all. To stop
    a series that's already begun -- one that won't happen any more,
    rather than one that shouldn't have been -- delete from its next
    event on."""
    with track("delete_recurrence"), cached_sheet_reads():
        try:
            left = get_recurrences().delete(id, starting_at_event_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return _public_recurrences([left] if left else [])


@tool
@writes
def create_event(event: PublicEvent, reallocate: bool = True) -> list[PublicEvent]:
    """Create a new event, optionally serving goals (goal_ids, primary
    first), reallocating time from the rest of its day as needed to make
    room. With reallocate false, it's created only if nothing else has to
    change: otherwise nothing is written, and the error lists what would
    have changed. Returns the events affected by the creation."""
    with track("create_event"), cached_sheet_reads():
        _check_goal_ids(event)
        new_event = event.to_event()
        try:
            applied = get_reallocating_calendar().create_event(
                new_event, ReallocationOptions(), reallocate=reallocate
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return _public_events(applied)


@tool
@writes
def delete_event(id: str) -> list[PublicEvent]:
    """Delete an event by its ID. Given one event of a recurring series,
    deletes only that event; to delete the whole series, or an event and
    the ones after it, use delete_recurrence. Returns the events affected
    by the deletion."""
    with track("delete_event"), cached_sheet_reads():
        cancelled = get_calendar_client().update_event(Event(id=id, status="cancelled"))
        return _public_events([cancelled])


@tool
def get_goals(statuses: list[GoalStatus] | None = None) -> GoalList:
    """The goal tree: the goals with any of these statuses (by default
    proposed, active and inactive -- not completed, archived or deleted
    ones), parents before their children, each with its path from the top.
    Also how many of the calendar's event labels are in use: each active
    goal takes one. Each goal has minutes_24h and minutes_7d: the time
    spent on it and its sub-goals in the 24 hours and 7 days (wall-clock)
    up to as_of, when notes were last compacted into the calendar.
    Each goal's minutes_by_statuses splits its time by the statuses of the
    goals each event is given among it and its sub-goals (not their
    ancestors), so its time through goals of any statuses is the sum of
    the entries naming any of them, each event counted once; the list's
    own minutes_by_statuses is the overall goal's. The list's
    minutes_by_priority splits the same 24 hours and 7 days by the
    priority each moment went to -- the highest effective priority among
    the events then, so overlaps count once -- with priority null for the
    rest (no event, or none with a priority); each window's entries add up
    to all of it.

    The first goal is always the overall goal (id "overall"), listed
    whatever the statuses: its sub-goals are implied to be every top-level
    goal, so it's rated like any goal -- by a measure of its own, or the
    mean of theirs -- but rates everything together, and its minutes are
    the time spent on any goal. It holds no event label, can't be given to
    an event, and stays active and top-level."""
    with track("get_goals"), cached_sheet_reads():
        _prefetch(get_goal_store(), get_compaction_journal())
        try:
            return get_goal_store().get_goals(statuses)
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def create_goal(goal: Goal) -> CreatedGoal:
    """Create a goal: a name (at most 50 characters, unique among its
    siblings), optionally a parent_id to make it a sub-goal, and any of
    its other properties. Its status is active unless given (e.g.
    "proposed" for one suggested but not taken on yet); each active goal
    takes one of the calendar's event labels, and its events are shown in
    its color (background_color, or derived from priority). priority is
    inherited by sub-goals and events that don't set their own (an
    event takes the highest of its goals'). id and label_id are assigned. Returns the resulting
    proposed, active and inactive goals, and the new goal's id as
    created_id.

    Every active goal is reflected on daily, and its measure says how its
    health (0-100) is rated each day; one without a measure is rated as
    the mean of its sub-goals', if it has any rated ones. One of:
    {"kind": "duration", "target_min": 600, "interval_days": 7} (minutes
    of its events over the last interval_days, default 1),
    {"kind": "count", "target": 1, "noun": "visits", "interval_days": 60,
    "zero_at_days": 90} (number of its events over the interval: 100
    while the target's met; past it, falling to 0 by zero_at_days since
    the interval's start -- without zero_at_days, a shortfall is rated in
    proportion, as for duration), {"kind": "time_constraint", "edge":
    "start", "target": "09:30", "when": "by", "grace_min": 10,
    "zero_at_min": 60} (when the day's first event of the goal starts --
    or, with edge "end", its last ends -- against the target: full marks
    by it, plus the grace, none at zero_at_min late; when "after", the
    other way round; a day without any is skipped), {"kind":
    "time_window", "from": "11:30", "to": "13:30", "grace_min": 0,
    "zero_at_min": 60} (whether one of the day's events of the goal falls
    in the window, each event as far outside it as its closest edge, so
    one overlapping it at all is in: full marks for the closest event in
    it, plus the grace, none at zero_at_min out; a day without any is
    rated 0 -- e.g. lunch, measuring an "Eat well" goal's events with
    events_of), {"kind": "follow_through", "penalty": 25, "recovery":
    25, "look_back_days": 30} (a running score that carries over from day
    to day: from 100 look_back_days ago, each day loses penalty per event
    of the goal that was cancelled -- pushed off by reallocation, or
    cancelled in a compaction or by hand -- and regains recovery if any
    was kept, within 0-100; a cancelled event overlapped by a kept one of
    the goal, such as one merged into another, isn't counted; deleting a
    recurring series whole cancels each of its events, and counts, while
    deleting this and following doesn't (see delete_recurrence) -- e.g.
    "Do what I say I will"), {"kind": "subjective", "prompt": "How did it go?", "interval_days": 7} (asked
    in a reflection once interval_days,
    default 1, have passed since it was last answered; carried over from
    the day before in between), {"kind": "llm", "rubric": "..."} (you
    propose a rating against the rubric in a reflection; it may refer to
    the immediate sub-goals' ratings), or {"kind": "rollup", "agg":
    "mean"} (from the immediate sub-goals' ratings that day: "mean";
    "weighted" with "weights": {sub-goal id: weight}, a sub-goal not
    listed weighing 0, and a weight either a number or a temporary one,
    {"weight": 0, "until": "2026-11-05", "then": 1}, weighing "weight"
    on days before "until" and "then" from it on -- to set a sub-goal
    aside for a while (the daily reflection mentions it once its date
    has come, until it's extended or replaced by a plain number); or "percentile" with "percentile": 0-100, 0 being
    the lowest and 100 the highest). Only the fields shown are allowed. A
    duration, count, time_constraint, time_window or follow_through measure looks at the events of the
    goal and its sub-goals, or, given "events_of": "<goal id>", at those of
    that goal and its sub-goals instead, as though it were that goal (e.g.
    a "work 40 hours a week" sub-goal measuring its parent's events,
    without tagging any event with it); with "include_sub_goals": false,
    at just that goal's own events, not its sub-goals'. Any measure can
    also take "only_if": {"events_of": "<goal id>", "include_sub_goals":
    true} (both optional, meaning the same; {} is the goal itself and its
    sub-goals): it's rated only on days with such an event, and skipped on
    the rest without asking its prompt -- e.g. "How did practice go?" only
    on days of practice. A subjective measure's interval passes over those
    skipped days."""
    with track("create_goal"), cached_sheet_reads():
        _prefetch(get_goal_store(), get_compaction_journal())
        try:
            return get_goal_store().create_goal(goal)
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


GoalField = Literal["parent_id", "background_color", "priority", "measure", "note"]
"""Every Goal field update_goal can clear (see utilities/goals.py's
CLEARABLE_FIELDS)."""


@tool
@writes
def update_goal(goal: Goal, clear_fields: list[GoalField] | None = None) -> GoalList:
    """Update a goal by id. Omitted properties keep their current value;
    list one in clear_fields to blank it instead (clearing parent_id
    makes it a top-level goal; clearing background_color makes its color
    follow its priority). status is one of: proposed (suggested, not taken
    on yet), active (being worked on), inactive (paused), completed
    (achieved), archived (no longer relevant) or deleted (shouldn't have
    existed; no event can be given it). Only an active goal holds an event
    label and is assessed; any other status frees its label but keeps its
    history, and making it active again restores the label, and its past
    events' color with it. A new measure replaces the old one whole, and
    is checked as for create_goal. The overall goal (id "overall") can be
    given a name, measure or note, but stays active and
    has no parent; no goal can name it as its parent, since every
    top-level goal is already under it. Returns the resulting proposed,
    active and inactive goals."""
    with track("update_goal"), cached_sheet_reads():
        _prefetch(get_goal_store(), get_compaction_journal())
        try:
            return get_goal_store().update_goal(goal, clear_fields or ())
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def reorder_goals(goal_ids: list[str]) -> GoalList:
    """Put sibling goals (sharing a parent) in this order, among the places
    they already hold: goals are listed parents before children, siblings
    in this order. Name all of a parent's sub-goals (or all the top-level
    goals) to order them all. Returns the resulting proposed, active and
    inactive goals."""
    with track("reorder_goals"), cached_sheet_reads():
        _prefetch(get_goal_store(), get_compaction_journal())
        try:
            return get_goal_store().reorder_goals(goal_ids)
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def sync_goals_from_sheet() -> GoalList:
    """After hand edits to the Goals tab of the calendar metadata
    spreadsheet, make the calendar's event labels match it: one label per
    active goal (Calendar's own unnamed labels are left alone), and any
    other label removed. Returns the resulting proposed, active and
    inactive goals."""
    with track("sync_goals_from_sheet"), cached_sheet_reads():
        _prefetch(get_goal_store(), get_compaction_journal())
        try:
            return get_goal_store().sync()
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


@tool
def measure_goals(day: date | None = None, goal_ids: list[str] | None = None) -> list[Assessment]:
    """Proposed ratings of one day (from waking on it to waking the next;
    by default the last one that's over) for the goals whose measure the
    calendar can answer: duration, count, time_constraint, time_window,
    follow_through, and rollups whose
    sub-goals are all rated that day. Each has an explanation of how its
    0-100 rating was reached. Writes nothing; ratings are only confirmed
    in a reflection."""
    with track("measure_goals"), cached_sheet_reads():
        try:
            return get_goal_health().measure(day, goal_ids)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def record_assessments(assessments: list[Assessment]) -> list[Assessment]:
    """Record assessments (a 0-100 rating, or "skip", of a goal for one
    day) as proposed -- e.g. a rating the user gives in passing, or one
    measure_goals proposed. Recording a goal's day again replaces it. A
    subjective rating given this way (method "subjective") restarts its
    interval: it isn't asked again until interval_days have passed. They're
    only confirmed, and only count toward a goal's health, once a
    reflection confirms them. Returns them as recorded."""
    with track("record_assessments"), cached_sheet_reads():
        try:
            return get_goal_health().record_assessments(assessments)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_goal_history(goal_ids: list[str], start: date | None = None, end: date | None = None) -> list[Assessment]:
    """Daily assessments of these goals from start to end (both
    inclusive), proposed and confirmed, by goal then day. By default, the
    last 12 days and today."""
    with track("get_goal_history"), cached_sheet_reads():
        try:
            return get_goal_health().history(goal_ids, start, end)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def rebuild_goal_health_cache() -> GoalList:
    """Recompute every goal's at-a-glance health (health, health_period,
    health_trend in the goals tab) from its confirmed assessments, e.g.
    after hand edits. Returns every goal, whatever its status."""
    with track("rebuild_goal_health_cache"), cached_sheet_reads():
        _prefetch(get_goal_store(), get_compaction_journal())
        try:
            return get_goal_health().rebuild_cache()
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
def prepare_reflection(day: date | None = None) -> ReflectionContext:
    """Start (or continue) the daily reflection: rating every goal for one
    day. Days run from waking to waking, bounded by the end-of-day sleep
    events: a day whose bounding sleeps aren't in the calendar can't be
    reflected on (the error says which days need one), and the choices
    below flag them. With no day named, returns only choices -- the most
    recent completed days not fully reflected on -- to ask the user about;
    then call this again with the day picked. Otherwise returns the
    questions: the goals that need judgement -- llm goals to rate, and
    subjective goals whose prompt is due -- since everything the calendar
    can rate is filled in automatically; plus minutes per goal, the day's
    events and notes, and instructions for the conversation. Read-only."""
    with track("prepare_reflection"), cached_sheet_reads():
        try:
            return get_reflections().prepare(day)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def record_reflection(
    day: date,
    assessments: list[Assessment],
    proposed: list[str] | None = None,
    dry_run: bool = True,
) -> ReflectionResult:
    """Rate a day: `assessments` (each of this day) answer the questions
    prepare_reflection asked -- or change any goal's rating -- and every
    other rating the calendar can give is filled in automatically, rolling
    up to the overall goal. With dry_run (the default) nothing is written:
    it returns the summary to show, in which goals still waiting on
    answers (and any rating listed in `proposed`, an llm rating to check
    with the user) are provisional. With dry_run=False, every final rating
    is confirmed at once -- the only way a rating counts toward a goal's
    health -- and the summary is final, unless questions are still
    unanswered. Rating a goal again replaces its rating."""
    with track("record_reflection"), cached_sheet_reads():
        try:
            return get_reflections().record(day, assessments, proposed, dry_run=dry_run)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@dataclass(kw_only=True)
class CompactionStatus:
    last_compaction: datetime | None = None
    """When notes were last compacted into the calendar: it's settled
    fact up to then. `None` if they never have been."""

    latest_compacted_note: NotedTime | None = None
    """The compacted note with the latest timestamp, if any."""


@tool
def get_compaction_status() -> CompactionStatus:
    """When notes were last compacted into the calendar, and the latest
    note compacted. Read-only."""
    with track("get_compaction_status"), cached_sheet_reads():
        _prefetch(get_noted_time_sheet(), get_compaction_journal())
        _notes, latest = get_noted_time_sheet().read_with_latest_compacted()
        return CompactionStatus(
            last_compaction=get_compaction_journal().last_stamped_now(),
            latest_compacted_note=latest,
        )


@tool
@writes
def note(noted_time: NotedTime) -> NoteWithId:
    """Record a new time note -- a timestamp, with an optional
    description of what it marks. Returns the note as recorded, with the
    id edit_note/delete_note refer to it by."""
    with track("note"), cached_sheet_reads():
        # compaction_id is set only by compaction, never by a caller.
        recorded = replace(noted_time, compaction_id=None)
        return get_noted_time_sheet().append(recorded).with_id()


@tool
def get_notes(include_compacted: bool = False) -> list[NoteWithId]:
    """List the recorded time notes, sorted by timestamp, each with the id
    edit_note/delete_note refer to it by. Only notes that haven't been
    compacted yet, unless include_compacted is true. For browsing/review
    only -- may span many days. To compact notes, use prepare_compaction
    instead; it returns just the notes for the current round, which is
    what compact_notes expects."""
    with track("get_notes"), cached_sheet_reads():
        notes = get_noted_time_sheet().read_with_rows(include_compacted=include_compacted)
        return [n.with_id() for n in sorted(notes, key=lambda n: n.note.timestamp)]


@tool
@writes
def edit_note(
    note_id: str, timestamp: datetime | None = None, description: str | None = None
) -> NoteWithId:
    """Correct an uncompacted note (by its id, from get_notes or
    prepare_compaction): a new timestamp and/or description. Whichever is
    left out keeps its current value; an empty description clears it.
    Returns the edited note -- if the timestamp changed, so did its id.
    Compacted notes can't be changed (edit the calendar event they became
    instead). If a dry-run plan included this note, run a new dry run
    afterward -- that plan can no longer be committed."""
    with track("edit_note"), cached_sheet_reads():
        try:
            return get_note_compactor().edit_note(
                note_id, timestamp=timestamp, description=description
            ).with_id()
        except CompactionError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def delete_note(note_id: str) -> NotedTime:
    """Delete an uncompacted note (by its id, from get_notes or
    prepare_compaction), returning what it was. Other notes' ids are
    unaffected. Compacted notes can't be deleted. If a dry-run plan
    included this note, run a new dry run afterward."""
    with track("delete_note"), cached_sheet_reads():
        try:
            return get_note_compactor().delete_note(note_id)
        except CompactionError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def prepare_compaction() -> CompactionContext:
    """Step 1 of compacting notes into the calendar. Returns every day of
    uncompacted notes up to now (each note with an id and the planned
    events nearest it; at most a week -- `remaining_note_count` says how
    many are left for another round), those days' planned events, a
    `timeline` showing the two side by side, day by day, and
    instructions. Compare the notes to the plan, decide which events the
    notes show happened differently, then call compact_notes with those
    decisions. Read-only."""
    with track("prepare_compaction"), cached_sheet_reads():
        return get_note_compactor().prepare()


@tool
@writes
def compact_notes(
    decisions: list[EventDecision] | None = None,
    ignore_notes: list[str] | None = None,
    compaction_id: str | None = None,
    dry_run: bool = True,
) -> CompactionResult:
    """Steps 2 and 3 of compacting notes: realign each day's events to
    its notes and turn that into calendar changes. The past becomes fact
    and the future reflows around it. Each day is planned on its own,
    after the day before it, and never moves the next day's start; one
    list of decisions covers them all.

    Step 2: call with `decisions` -- one per event the notes show happened
    differently from the plan (keep with moved edges, cancel, create, or
    merge; see prepare_compaction's instructions). Any past event you don't
    mention is recorded as on schedule. Notes that don't set an event edge
    are added to the event they fall within, except those in
    `ignore_notes`. dry_run=True (the default) changes nothing: you get the
    proposed changes, a compaction_id, and a `timeline` of the notes beside
    the resulting events -- show its `text` to the user in a code block.
    If past events would overlap, the call fails naming them: decide which
    gives way (asking the user if the notes don't say) and call again.

    A 'keep' that moves a future event reschedules it, reflowing the rest
    of the day around it in the same plan. Moving the end-of-day sleep
    event moves where the day ends: an earlier bedtime shortens or cancels
    whatever no longer fits before it, a later one leaves the evening
    free. Its end (the wake-up time) is the border with the next day: a
    note ending it moves the border there. If that day is being compacted
    too, its morning is settled against the night; if not, compaction
    never adjusts the next day -- so move only its start to change only the
    bedtime, and if the wake-up time does change, the plan warns; tell the
    user. Cancelling a night (no sleep) makes its two days one.

    Step 3: only after the user has explicitly approved this specific plan,
    having seen it -- never in the same turn as the dry run, and a request
    to compact made before they saw the plan isn't approval -- call with
    that compaction_id and dry_run=False to apply it. If that fails
    partway, calling it again resumes exactly where it stopped -- the plan
    is already approved, so that needs no new approval. Days are applied in
    order, each one's notes marked compacted once it's done. With a compaction_id and
    dry_run=True you just get that compaction's stored plan back."""
    with track("compact_notes"), cached_sheet_reads():
        compactor = get_note_compactor()
        try:
            if compaction_id is None:
                if not dry_run:
                    raise CompactionError(
                        "run a dry run first (decisions, dry_run=True) and pass its compaction_id"
                    )
                return compactor.dry_run(decisions or [], ignore_notes)
            if dry_run:
                return compactor.describe(compaction_id)
            return compactor.commit(compaction_id)
        except CompactionError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def abandon_compaction(compaction_id: str) -> CompactionResult:
    """Give up on a compaction that can't be finished (or that you no
    longer want). Steps it already applied stay applied; its notes stay
    uncompacted (except those of days it had already finished), so a new
    compaction can be planned."""
    with track("abandon_compaction"), cached_sheet_reads():
        try:
            return get_note_compactor().abandon(compaction_id)
        except CompactionError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def set_time_zone(time_zone: str) -> str:
    """Set the calendar's time zone to an IANA name, e.g.
    "America/New_York" -- the user's own, so that times come back in their
    local time and days, weeks and months are dated as they live them.
    Set it when times come back in a zone that isn't the user's (e.g.
    UTC, with a "Z"), or when they say they've moved or are travelling.
    Events keep their moments; only how they're shown changes. Returns
    the zone set."""
    with track("set_time_zone"), cached_sheet_reads():
        try:
            zone = get_calendar_client().set_time_zone(time_zone)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        # Keep the Goal Health and Compactions calendars' days the same as
        # the main one's.
        health = get_goal_health().health_calendar(create=False)
        if health is not None:
            health.set_time_zone(zone.key)
        compactions = CompactionMarker(get_calendar_client()).calendar(create=False)
        if compactions is not None:
            compactions.set_time_zone(zone.key)
        return zone.key


def with_cors(app: ASGIApp) -> ASGIApp:
    """Lets browser-based MCP clients (e.g. the Time Tracker web app) call
    [app] cross-origin: answers their CORS preflights -- before auth, which
    would otherwise reject them with a 401, since preflights never carry a
    bearer token -- and exposes Mcp-Session-Id, which the Streamable HTTP
    transport needs the client to read back. Allows localhost on any port,
    for local development, plus MCP_CORS_ALLOWED_ORIGINS.

    This grants a page nothing it could use without a bearer token of its
    own: auth is the Authorization header, never cookies, so it isn't
    something a cross-origin page can borrow from the user's browser."""
    return CORSMiddleware(
        app,
        allow_origins=get_cors_allowed_origins(),
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?",
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Last-Event-ID",
            "Mcp-Protocol-Version",
            "Mcp-Session-Id",
        ],
        expose_headers=["Mcp-Session-Id", "WWW-Authenticate"],
    )


if __name__ == "__main__":
    if _TRANSPORT == "streamable-http":
        import uvicorn

        # Every Render web service must bind 0.0.0.0 and the $PORT it
        # assigns (default 10000 locally, to match Render's own default).
        # Same as mcp.run(transport="streamable-http"), plus with_cors.
        host = "0.0.0.0"
        uvicorn.run(
            with_cors(mcp.streamable_http_app(host=host)),
            host=host,
            port=int(os.environ.get("PORT", 10000)),
            log_level=mcp.settings.log_level.lower(),
        )
    else:
        mcp.run()
