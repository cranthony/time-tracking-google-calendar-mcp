from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp

from calendar_clients.google_calendar import CalendarClient, Event, EventLabelConflictError
from calendar_clients.google_sheets import cached_sheet_reads
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
from utilities.goal_sheet import Cadence, Goal, GoalStatus
from utilities.goals import GoalList, Goals, GoalTree
from utilities.memory_diagnostics import track
from utilities.note_compaction import CompactionError, EventDecision
from utilities.note_compactor import CompactionContext, CompactionResult, NoteCompactor
from utilities.noted_time_sheet import NotedTime, NotedTimeSheet, NoteWithId
from utilities.reallocation import ReallocationOptions
from utilities.reallocating_calendar import ReallocatingCalendar
from utilities.recurrences import Recurrences, describe_rules
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
    {"status", "goal_priority", "goal_is_fixed_time", "recurrence", "time_zone", "original_start"}
)
"""Event fields the agent talking to this server should never see or set,
at all -- not just left null. A series' recurrence and time_zone are seen
and set through PublicRecurrence instead (see get_recurrence), and
original_start is only used to split one. Enforced by PublicEvent actually lacking
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

    effective_priority/effective_is_fixed_time are read-only: Event's
    properties of the same name, the values reallocation actually uses,
    i.e. priority/is_fixed_time falling back to the primary goal's (or
    its nearest ancestor's) when the event doesn't set one. Kept separate
    from priority/is_fixed_time so that sending a listed event straight
    back to update_event never copies its goal's values onto the event
    itself, which would stop it following later changes to the goal.
    to_event ignores them.

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
    effective_is_fixed_time: bool | None = None
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
            effective_is_fixed_time=event.effective_is_fixed_time,
            is_end_of_day_sleep=event.is_end_of_day_sleep,
            recurring_event_id=event.recurring_event_id,
        )

    def to_event(self) -> Event:
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
        )


@dataclass(kw_only=True)
class PublicRecurrence:
    """A recurring series of events, as a whole -- see utilities/
    recurrences.py. id is the series' own id, which is every one of its
    events' recurring_event_id. start/end are when its first event starts
    and ends; time_zone keeps its events at the same wall-clock time
    across daylight saving changes. rules are its RFC 5545 rule lines,
    e.g. ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20261231T000000Z"], with
    exactly one RRULE; schedule says them in words. The other fields are
    as for PublicEvent, and apply to every event in the series that
    hasn't been edited on its own. goals_from_label is as for PublicEvent.

    schedule, goal_names, event_label_id and the effective_* fields are
    read-only: update_recurrence ignores them, as it does time_zone."""

    id: str | None = None
    summary: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    time_zone: str | None = None
    rules: list[str] | None = None
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
    effective_is_fixed_time: bool | None = None

    @classmethod
    def from_event(cls, event: Event, tree: GoalTree) -> "PublicRecurrence":
        public = PublicEvent.from_event(event, tree)
        zone = ZoneInfo(event.time_zone) if event.time_zone else None
        return cls(
            id=event.id,
            summary=event.summary,
            start=event.start.astimezone(zone) if zone and event.start else event.start,
            end=event.end.astimezone(zone) if zone and event.end else event.end,
            time_zone=event.time_zone,
            rules=event.recurrence,
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
            effective_is_fixed_time=event.effective_is_fixed_time,
        )

    def to_event(self) -> Event:
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
            recurrence=self.rules,
        )


_calendar_client: CalendarClient | None = None
_reallocating_calendar: ReallocatingCalendar | None = None
_goals: Goals | None = None
_goal_health: GoalHealth | None = None
_reflections: Reflections | None = None
_noted_time_sheet: NotedTimeSheet | None = None
_note_compactor: NoteCompactor | None = None
_recurrences: Recurrences | None = None


def get_calendar_client() -> CalendarClient:
    """Lazily construct and cache the CalendarClient, so credential loading
    (and the OAuth consent flow, on first run) happens once per process
    rather than on every tool call."""
    global _calendar_client
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
        _reallocating_calendar = ReallocatingCalendar(GoalCalendar(get_calendar_client(), get_goal_store()))
    return _reallocating_calendar


def get_goal_health() -> GoalHealth:
    """Lazily construct and cache the GoalHealth, the same way the other
    get_* helpers cache theirs."""
    global _goal_health
    if _goal_health is None:
        _goal_health = GoalHealth(get_calendar_client(), get_goal_store())
    return _goal_health


def get_reflections() -> Reflections:
    """Lazily construct and cache the Reflections, the same way the other
    get_* helpers cache theirs."""
    global _reflections
    if _reflections is None:
        _reflections = Reflections(get_goal_health(), get_goal_store(), get_noted_time_sheet())
    return _reflections


def get_goal_store() -> Goals:
    """Lazily construct and cache the Goals, the same way
    get_calendar_client/get_reallocating_calendar cache theirs."""
    global _goals
    if _goals is None:
        _goals = build_goals()
    return _goals


def get_recurrences() -> Recurrences:
    """Lazily construct and cache the Recurrences, the same way the other
    get_* helpers cache theirs. Writes go through a GoalCalendar, so each
    series' label follows its goals."""
    global _recurrences
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
        _noted_time_sheet = build_noted_time_sheet()
    return _noted_time_sheet


def get_note_compactor() -> NoteCompactor:
    """Lazily construct and cache the NoteCompactor, the same way the
    other get_* helpers cache theirs."""
    global _note_compactor
    if _note_compactor is None:
        _note_compactor = NoteCompactor(
            calendar=get_reallocating_calendar(),
            # Written through goals, so each event's label follows them.
            client=GoalCalendar(get_calendar_client(), get_goal_store()),
            goals=get_goal_store(),
            notes=get_noted_time_sheet(),
            journal=build_compaction_journal(),
        )
    return _note_compactor


def _public_events(events: list[Event]) -> list[PublicEvent]:
    """`events` as PublicEvents, with goal_ids/goal_names and
    effective_priority/effective_is_fixed_time filled in from each one's
    goals -- see PublicEvent. Every event tool's result goes through
    this."""
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


@mcp.tool()
def list_events(min_time: datetime, max_time: datetime) -> list[PublicEvent]:
    """List events between min_time and max_time. goal_ids are the goals
    each event serves, primary first (goal_names gives their names).
    effective_priority/effective_is_fixed_time are what reallocation
    actually uses: the event's own priority/is_fixed_time, falling back
    to its primary goal's. is_end_of_day_sleep marks the sleep event that
    ends a day; recurring_event_id is the id of an instance's recurring
    series (see get_recurrence and update_recurrence to read and edit the
    series as a whole); event_label_id is the calendar label derived from
    its goals.
    goal_names, the effective_* fields, is_end_of_day_sleep,
    recurring_event_id and event_label_id are read-only: update_event and
    create_event ignore them."""
    with track("list_events"), cached_sheet_reads():
        events = get_calendar_client().list_events(min_time, max_time)
        return _public_events([event for event in events if event.status != "cancelled"])


@mcp.tool()
def get_event(id: str) -> PublicEvent:
    """Get a single event by its ID. See list_events for its fields."""
    with track("get_event"), cached_sheet_reads():
        event = get_calendar_client().get_event(id)
        if event.status == "cancelled":
            raise ToolError(f"Event {id} has been cancelled.")
        return _public_events([event])[0]


@mcp.tool()
def update_event(event: PublicEvent) -> list[PublicEvent]:
    """Update an existing event, reallocating time from the rest of its
    day as needed to make room for its new position. Set goal_ids to
    change its goals ([] for none). Returns the events affected by the
    update."""
    with track("update_event"), cached_sheet_reads():
        _check_goal_ids(event, existing=True)
        updated_event = event.to_event()
        try:
            applied = get_reallocating_calendar().update_event(
                updated_event, ReallocationOptions()
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return _public_events(applied)


@mcp.tool()
def get_recurrence(id: str) -> PublicRecurrence:
    """A recurring series, by its id or the id of any of its events (an
    event's recurring_event_id is its series' id). See PublicRecurrence
    for its fields."""
    with track("get_recurrence"), cached_sheet_reads():
        try:
            return _public_recurrences([get_recurrences().series(id)])[0]
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@mcp.tool()
def update_recurrence(
    recurrence: PublicRecurrence, starting_at_event_id: str | None = None
) -> list[PublicRecurrence]:
    """Edit a recurring series (recurrence.id is its id, or any of its
    events'): every field given is set, those left out are kept. Applies
    to all its events, except any edited on their own -- or, with
    starting_at_event_id, to that event and the ones after it only ("this
    and following"): the series is split there (see split_recurrence) and
    only the later part is edited. start/end are its first event's, as
    get_recurrence gave them; when it's split, the later part moves by as
    much as they changed. rules replace its rules whole. Series aren't
    reallocated. Returns the edited series, then the earlier part if it
    was split."""
    with track("update_recurrence"), cached_sheet_reads():
        _check_goal_ids(recurrence, existing=True)
        try:
            updated = get_recurrences().update(recurrence.to_event(), starting_at_event_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return _public_recurrences(updated)


@mcp.tool()
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


@mcp.tool()
def create_event(event: PublicEvent) -> list[PublicEvent]:
    """Create a new event, optionally serving goals (goal_ids, primary
    first). Returns the events affected by the creation."""
    with track("create_event"), cached_sheet_reads():
        _check_goal_ids(event)
        new_event = event.to_event()
        try:
            applied = get_reallocating_calendar().create_event(new_event, ReallocationOptions())
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return _public_events(applied)


@mcp.tool()
def delete_event(id: str) -> list[PublicEvent]:
    """Delete an event by its ID. Returns the events affected by the deletion."""
    with track("delete_event"), cached_sheet_reads():
        cancelled = get_calendar_client().update_event(Event(id=id, status="cancelled"))
        return _public_events([cancelled])


@mcp.tool()
def get_goals(statuses: list[GoalStatus] | None = None) -> GoalList:
    """The goal tree: the goals with any of these statuses (by default
    proposed, active and inactive -- not completed, archived or deleted
    ones), parents before their children, each with its path from the top.
    Also how many of the calendar's event labels are in use: each active
    goal takes one."""
    with track("get_goals"), cached_sheet_reads():
        try:
            return get_goal_store().get_goals(statuses)
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


@mcp.tool()
def create_goal(goal: Goal) -> GoalList:
    """Create a goal: a name (at most 50 characters, unique among its
    siblings), optionally a parent_id to make it a sub-goal, and any of
    its other properties. Its status is active unless given (e.g.
    "proposed" for one suggested but not taken on yet); each active goal
    takes one of the calendar's event labels, and its events are shown in
    its color (background_color, or derived from priority). priority and
    fixed_time are inherited by sub-goals and events that don't set their
    own. id, label_id and created are assigned. Returns the resulting
    proposed, active and inactive goals.

    A goal with a cadence can have a measure: how each period's health
    (0-100) is rated. Targets are per period of its cadence. One of:
    {"kind": "duration", "target_min": 600} (minutes of its events),
    {"kind": "count", "target": 1, "noun": "dinners"} (number of events),
    {"kind": "wake_time", "target": "07:00", "grace_min": 10,
    "zero_at_min": 60} (full marks within the grace, none at zero_at_min
    late), {"kind": "subjective", "prompt": "How did it go?"} (rated in a
    reflection), {"kind": "llm", "rubric": "..."} (you propose a rating
    against the rubric in a reflection), or {"kind": "rollup", "agg":
    "min"} (min or mean of its sub-goals' ratings). Only the fields shown
    are allowed; noun, grace_min, zero_at_min, prompt and agg are
    optional. A duration or count measure looks at the events of the goal
    and its sub-goals, or, given "goal_ids": [...], at those of these goals
    and their sub-goals instead; with "include_sub_goals": false, at just
    the goals' own events, not their sub-goals'."""
    with track("create_goal"), cached_sheet_reads():
        try:
            return get_goal_store().create_goal(goal)
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


GoalField = Literal[
    "parent_id", "background_color", "priority", "fixed_time", "cadence", "measure", "target", "deadline", "note"
]
"""Every Goal field update_goal can clear (see utilities/goals.py's
CLEARABLE_FIELDS)."""


@mcp.tool()
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
    is checked as for create_goal. Returns the resulting proposed, active
    and inactive goals."""
    with track("update_goal"), cached_sheet_reads():
        try:
            return get_goal_store().update_goal(goal, clear_fields or ())
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


@mcp.tool()
def reorder_goals(goal_ids: list[str]) -> GoalList:
    """Put sibling goals (sharing a parent) in this order, among the places
    they already hold: goals are listed parents before children, siblings
    in this order. Name all of a parent's sub-goals (or all the top-level
    goals) to order them all. Returns the resulting proposed, active and
    inactive goals."""
    with track("reorder_goals"), cached_sheet_reads():
        try:
            return get_goal_store().reorder_goals(goal_ids)
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


@mcp.tool()
def sync_goals_from_sheet() -> GoalList:
    """After hand edits to the Goals tab of the calendar metadata
    spreadsheet, make the calendar's event labels match it: one label per
    active goal (Calendar's own unnamed labels are left alone), and any
    other label removed. Returns the resulting proposed, active and
    inactive goals."""
    with track("sync_goals_from_sheet"), cached_sheet_reads():
        try:
            return get_goal_store().sync()
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


@mcp.tool()
def measure_goals(
    cadence: Cadence, period: str | None = None, goal_ids: list[str] | None = None
) -> list[Assessment]:
    """Proposed assessments for the active goals with this cadence whose
    measure the calendar can answer (duration, count, wake_time, rollup),
    for one period: e.g. "2026-09-30" (daily), "week-2026-09-27" (weekly,
    Sunday to Saturday), "2026-09" (monthly), "2026-09..10" (every two
    months). The default is the most recent period that has fully ended.
    Each has an explanation of how its 0-100 rating was reached. Writes
    nothing; ratings are only confirmed in a reflection."""
    with track("measure_goals"), cached_sheet_reads():
        try:
            return get_goal_health().measure(cadence, period, goal_ids)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@mcp.tool()
def record_assessments(assessments: list[Assessment]) -> list[Assessment]:
    """Record assessments (a 0-100 rating, or "skip", of a goal for one
    period of its cadence) as proposed -- e.g. a rating the user gives in
    passing, or one measure_goals proposed. Recording a goal's period
    again replaces it. They're only confirmed, and only count toward a
    goal's health, once a reflection confirms them. Returns them as
    recorded."""
    with track("record_assessments"), cached_sheet_reads():
        try:
            return get_goal_health().record_assessments(assessments)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@mcp.tool()
def get_goal_history(
    goal_ids: list[str],
    cadence: Cadence | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[Assessment]:
    """Assessments of these goals between start and end (both inclusive),
    proposed and confirmed, by goal then period. By default, the last 12
    periods of each goal's cadence."""
    with track("get_goal_history"), cached_sheet_reads():
        try:
            return get_goal_health().history(goal_ids, cadence, start, end)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@mcp.tool()
def rebuild_goal_health_cache() -> GoalList:
    """Recompute every goal's at-a-glance health (health, health_period,
    health_trend in the goals tab) from its confirmed assessments, e.g.
    after hand edits. Returns every goal, whatever its status."""
    with track("rebuild_goal_health_cache"), cached_sheet_reads():
        try:
            return get_goal_health().rebuild_cache()
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@mcp.tool()
def prepare_reflection(cadence: Cadence, period: str | None = None) -> ReflectionContext:
    """Start a reflection: everything needed to confirm the health ratings
    due for one period of a cadence -- by default the oldest recent period
    with no reflection yet. Returns the goals to rate (with recent ratings,
    and a proposed rating with its explanation where one was recorded or
    could be measured), shorter-cadence goals to review, minutes per goal,
    the period's events and notes (daily and weekly), the last reflection's
    intentions, and instructions for the conversation. Read-only."""
    with track("prepare_reflection"), cached_sheet_reads():
        try:
            return get_reflections().prepare(cadence, period)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@mcp.tool()
def record_reflection(
    cadence: Cadence,
    period: str,
    assessments: list[Assessment],
    journal: str | None = None,
    intentions: list[str] | None = None,
    dry_run: bool = True,
) -> ReflectionResult:
    """Finish a reflection: confirm its ratings (each for this cadence and
    period), and record an optional journal and up to 3 intentions for the
    next period. With dry_run (the default) nothing is written: it returns
    a preview to show the user. With dry_run=False, once they agree, the
    ratings are confirmed -- the only way a rating counts toward a goal's
    health -- and the reflection is recorded; doing it again replaces it."""
    with track("record_reflection"), cached_sheet_reads():
        try:
            return get_reflections().record(cadence, period, assessments, journal, intentions, dry_run=dry_run)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@mcp.tool()
def note(noted_time: NotedTime) -> NoteWithId:
    """Record a new time note -- a timestamp, with an optional
    description of what it marks. Returns the note as recorded, with the
    id edit_note/delete_note refer to it by."""
    with track("note"), cached_sheet_reads():
        # compaction_id is set only by compaction, never by a caller.
        recorded = replace(noted_time, compaction_id=None)
        return get_noted_time_sheet().append(recorded).with_id()


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def prepare_compaction() -> CompactionContext:
    """Step 1 of compacting notes into the calendar. Returns one day's
    worth of uncompacted notes (each with an id and the planned events
    nearest it), that day's planned events, a `timeline` showing the two
    side by side, and instructions. Compare the notes to the plan, decide
    which events the notes show happened differently, then call
    compact_notes with those decisions. Read-only."""
    with track("prepare_compaction"), cached_sheet_reads():
        return get_note_compactor().prepare()


@mcp.tool()
def compact_notes(
    decisions: list[EventDecision] | None = None,
    ignore_notes: list[str] | None = None,
    compaction_id: str | None = None,
    dry_run: bool = True,
) -> CompactionResult:
    """Steps 2 and 3 of compacting notes: realign the day's events to the
    notes and turn that into calendar changes. The past becomes fact and
    the future reflows around it.

    Step 2: call with `decisions` -- one per event the notes show happened
    differently from the plan (keep with moved edges, cancel, create, or
    merge; see prepare_compaction's instructions). Any past event you don't
    mention is recorded as on schedule. Notes that don't set an event edge
    are added to the event they fall within, except those in
    `ignore_notes`. dry_run=True (the default) changes nothing: you get the
    proposed changes, a compaction_id, and a `timeline` of the notes beside
    the resulting events -- show that to the user as two parallel lanes.
    If past events would overlap, the call fails naming them: decide which
    gives way (asking the user if the notes don't say) and call again.

    A 'keep' that moves a future event reschedules it, reflowing the rest
    of the day around it in the same plan. Moving the end-of-day sleep
    event moves where the day ends: an earlier bedtime shortens or cancels
    whatever no longer fits before it, a later one leaves the evening
    free. Its end (the wake-up time) starts the next day, which compaction
    never adjusts -- so move only its start to change only the bedtime. If
    the wake-up time does change, the plan warns; tell the user.

    Step 3: only after the user has explicitly approved this specific plan,
    having seen it -- never in the same turn as the dry run, and a request
    to compact made before they saw the plan isn't approval -- call with
    that compaction_id and dry_run=False to apply it. If that fails
    partway, calling it again resumes exactly where it stopped -- the plan
    is already approved, so that needs no new approval. With a compaction_id and
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


@mcp.tool()
def abandon_compaction(compaction_id: str) -> CompactionResult:
    """Give up on a compaction that can't be finished (or that you no
    longer want). Steps it already applied stay applied; its notes stay
    uncompacted, so a new compaction can be planned."""
    with track("abandon_compaction"), cached_sheet_reads():
        try:
            return get_note_compactor().abandon(compaction_id)
        except CompactionError as exc:
            raise ToolError(str(exc)) from exc


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
