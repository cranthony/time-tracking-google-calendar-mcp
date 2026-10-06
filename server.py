from __future__ import annotations

import functools
import logging
import os
from collections.abc import Callable, Collection
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, ParamSpec, TypeVar
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
    build_actions,
    build_calendar_client,
    build_compaction_journal,
    build_locations,
    build_noted_time_sheet,
    build_people,
    build_trait_rollup,
    build_traits,
    get_allowed_user_ids,
    get_cors_allowed_origins,
    get_mcp_resource_url,
    get_workos_authkit_domain,
)
from oauth_proxy import oauth_proxy_handlers
from utilities.action_groups import ActionGroup, ListedActionGroup
from utilities.actions import (
    Action,
    ActionChanges,
    ActionGroupChanges,
    ActionList,
    Actions,
    ActionStatus,
    ActionTree,
    CreatedAction,
    CreatedActionGroup,
    DeletedActionGroup,
    ListedAction,
)
from utilities.action_calendar import ActionCalendar, fill_in_from_actions
from utilities.compaction_additions import NewAction, NewLocation, NewPerson
from utilities.facts import Facts, fact_problems
from utilities.judgments import Judging, Judgment, JudgmentsDue, JudgmentsResult
from utilities.trait_rollup import TraitRollup, TraitScoreRow
from utilities.locations import CreatedLocation, Location, Locations
from utilities.memory_diagnostics import track
from utilities.note_compaction import CompactionError, EventDecision
from utilities.compaction_journal import CompactionJournal
from utilities.compaction_marker import CompactionMarker
from utilities.note_compactor import CompactionContext, CompactionResult, NoteCompactor
from utilities.noted_time_sheet import NotedTime, NotedTimeSheet, NoteWithId
from utilities.people import (
    Circle,
    CreatedCircle,
    CreatedPerson,
    DeletedCircle,
    ListedCircle,
    ListedPerson,
    People,
    Person,
    PersonStatus,
)
from utilities.reallocation import ReallocationOptions
from utilities.reallocating_calendar import ReallocatingCalendar
from utilities.recurrences import Recurrences, Repeat, describe_rules
from utilities.traits import ListedTrait, Trait, Traits, TraitStatus
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
    {"status", "action_priority", "recurrence", "time_zone", "original_start", "cleared"}
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

    action_ids are what was done at the event (see get_actions), the
    first setting its label and color; setting them is how an event is
    tied to actions. action_names (their names, in the same order) and
    event_label_id (derived from the first action -- see utilities/
    action_calendar.py) are read-only: to_event ignores them.

    actions_from_label is true when an event was never given actions, so
    its action_ids are inferred from its label (the action that holds the
    label), not stored. Sending it back with actions_from_label still true
    leaves them inferred, whatever action_ids says; to store actions, send
    action_ids with actions_from_label false (or left out).

    effective_priority is read-only: Event's property of the same name,
    the priority reallocation actually uses, i.e. priority falling back,
    when the event doesn't set one, to the highest (lowest-numbered)
    priority among its actions (each action's own, or its nearest
    group's). Kept separate from priority so that sending a listed event
    straight back to update_event never copies its actions' priority onto
    the event itself, which would stop it following later changes to the
    actions. to_event ignores it.

    is_end_of_day_sleep/recurring_event_id are read-only too, and
    to_event ignores them as well. is_end_of_day_sleep decides where a
    day ends for reallocation (see utilities/reallocating_calendar.py),
    so a wrong mark would quietly change what later updates shrink, move
    or cancel; it's only set by hand, via calendar_cli.py.
    recurring_event_id is assigned by Google and can't be set at all.

    facts are what compaction established about a past event (see
    utilities/facts.py), for traits' judgments later: location_id (see
    get_locations), with_ids (the people there with the user -- never
    "self", who's at every event), for_ids (people it was done for who
    weren't there) and notes ({person id: a subjective note on how it was
    for them}, "self" for the user). Compaction writes them; set them to
    replace them whole.

    judgments are how the event went for each person, judged after
    compaction against each judgment part of the traits that apply to them
    (see record_judgments): {person id: {trait id: {part key: {"rating",
    "scale", "reasoning"}}}}. Set them to overwrite a rating by hand
    (replacing them whole), or clear them."""

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
    action_ids: list[str] | None = None
    action_names: list[str] | None = None
    actions_from_label: bool = False
    event_label_id: str | None = None
    is_cancelled: bool = False
    effective_priority: int | None = None
    is_end_of_day_sleep: bool | None = None
    recurring_event_id: str | None = None
    facts: Facts | None = None
    judgments: dict[str, Any] | None = None

    @classmethod
    def from_event(cls, event: Event, tree: ActionTree | None = None) -> "PublicEvent":
        """`tree` fills in action_names; without it they're left `None`."""
        names = None
        if tree is not None and event.action_ids is not None:
            names = [tree.name(i) for i in event.action_ids]
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
            action_ids=event.action_ids,
            action_names=names,
            actions_from_label=event.actions_from_label,
            event_label_id=event.event_label_id,
            is_cancelled=event.status == "cancelled",
            effective_priority=event.effective_priority,
            is_end_of_day_sleep=event.is_end_of_day_sleep,
            recurring_event_id=event.recurring_event_id,
            facts=event.facts,
            judgments=event.judgments,
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
            # Inferred actions, sent back, aren't the event's to store.
            action_ids=None if self.actions_from_label else self.action_ids,
            priority=self.priority,
            status="cancelled" if self.is_cancelled else None,
            facts=self.facts.normalized() if self.facts is not None else None,
            judgments=self.judgments,
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
    (see update_recurrence). actions_from_label is as for PublicEvent.

    schedule, action_names, event_label_id and effective_priority are
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
    action_ids: list[str] | None = None
    action_names: list[str] | None = None
    actions_from_label: bool = False
    event_label_id: str | None = None
    effective_priority: int | None = None

    @classmethod
    def from_event(cls, event: Event, tree: ActionTree) -> "PublicRecurrence":
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
            action_ids=event.action_ids,
            action_names=public.action_names,
            actions_from_label=event.actions_from_label,
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
            action_ids=None if self.actions_from_label else self.action_ids,
            cleared=frozenset(clear_fields),
        )


# Each get_* helper below builds its object under WRITE_LOCK, the first
# time only: building some writes to Google (creating a spreadsheet or
# tab), and two tool calls arriving together mustn't both build one.
_calendar_client: CalendarClient | None = None
_reallocating_calendar: ReallocatingCalendar | None = None
_noted_time_sheet: NotedTimeSheet | None = None
_compaction_journal: CompactionJournal | None = None
_note_compactor: NoteCompactor | None = None
_recurrences: Recurrences | None = None
_traits: Traits | None = None
_actions: Actions | None = None
_people: People | None = None
_locations: Locations | None = None
_trait_rollup: TraitRollup | None = None


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
    get_calendar_client caches its CalendarClient. Wraps an ActionCalendar
    (get_calendar_client() plus get_action_store()) rather than
    get_calendar_client() directly, so reallocation sees an event's
    action-derived priority as the fallback whenever the event itself
    doesn't set one, and every write derives its label from its actions."""
    global _reallocating_calendar
    if _reallocating_calendar is None:
        with WRITE_LOCK:
            if _reallocating_calendar is None:
                _reallocating_calendar = ReallocatingCalendar(ActionCalendar(get_calendar_client(), get_action_store()))
    return _reallocating_calendar


def get_action_store() -> Actions:
    """Lazily construct and cache the Actions, the same way the other get_*
    helpers cache theirs. Building it the first time adds the Actions tab."""
    global _actions
    if _actions is None:
        with WRITE_LOCK:
            if _actions is None:
                _actions = build_actions()
    return _actions


def get_people_store() -> People:
    """Lazily construct and cache the People, the same way the other get_*
    helpers cache theirs. Building it the first time adds the People and
    Circles tabs."""
    global _people
    if _people is None:
        with WRITE_LOCK:
            if _people is None:
                _people = build_people(trait_ids=lambda: [t.id for t in get_trait_store().all()])
    return _people


def get_location_store() -> Locations:
    """Lazily construct and cache the Locations, the same way the other
    get_* helpers cache theirs. Building it the first time adds the
    Locations tab."""
    global _locations
    if _locations is None:
        with WRITE_LOCK:
            if _locations is None:
                _locations = build_locations()
    return _locations


def get_trait_rollup() -> TraitRollup:
    """Lazily construct and cache the TraitRollup, the same way the other
    get_* helpers cache theirs. Building it the first time adds the Trait
    Scores tab."""
    global _trait_rollup
    if _trait_rollup is None:
        with WRITE_LOCK:
            if _trait_rollup is None:
                _trait_rollup = build_trait_rollup(get_action_store(), get_people_store(), get_trait_store())
    return _trait_rollup


def _prefetch_stores() -> None:
    """Read the actions', people's and locations' tabs in one request (see
    `_prefetch`): what the event, action and people tools read between
    them."""
    actions, people = get_action_store(), get_people_store()
    _prefetch(actions, people, get_location_store())


def get_trait_store() -> Traits:
    """Lazily construct and cache the Traits, the same way the other get_*
    helpers cache theirs. Building it the first time creates the Traits
    tab, seeded with the starting traits."""
    global _traits
    if _traits is None:
        with WRITE_LOCK:
            if _traits is None:
                _traits = build_traits()
    return _traits


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
    get_* helpers cache theirs. Writes go through an ActionCalendar, so
    each series' label follows its actions."""
    global _recurrences
    if _recurrences is None:
        with WRITE_LOCK:
            if _recurrences is None:
                client = get_calendar_client()
                _recurrences = Recurrences(
                    ActionCalendar(client, get_action_store()), client.list_instances, client.get_time_zone
                )
    return _recurrences


def _public_recurrences(events: list[Event]) -> list[PublicRecurrence]:
    tree = get_action_store().tree()
    return [PublicRecurrence.from_event(event, tree) for event in fill_in_from_actions(events, tree)]


def get_noted_time_sheet() -> NotedTimeSheet:
    """Lazily construct and cache the NotedTimeSheet, the same way
    the other get_* helpers cache theirs."""
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
                    # Written through actions, so each event's label follows them.
                    client=ActionCalendar(get_calendar_client(), get_action_store()),
                    actions=get_action_store(),
                    people=get_people_store(),
                    locations=get_location_store(),
                    notes=get_noted_time_sheet(),
                    journal=get_compaction_journal(),
                    # A red event in Google Calendar, at the last compaction.
                    marker=CompactionMarker(get_calendar_client()),
                    judging=Judging(
                        client=ActionCalendar(get_calendar_client(), get_action_store()),
                        actions=get_action_store(),
                        people=get_people_store(),
                        locations=get_location_store(),
                        traits=get_trait_store(),
                    ),
                    rollup=get_trait_rollup(),
                )
    return _note_compactor


def _public_events(events: list[Event]) -> list[PublicEvent]:
    """`events` as PublicEvents, with action_ids/action_names and
    effective_priority filled in from each one's actions -- see
    PublicEvent. Every event tool's result goes through this."""
    tree = get_action_store().tree()
    return [PublicEvent.from_event(event, tree) for event in fill_in_from_actions(events, tree)]


def _check_action_ids(event: PublicEvent | PublicRecurrence, *, existing: bool = False) -> None:
    """Refuse action_ids that don't name actions, suggesting close
    matches, or that add a deleted action to the event. An `existing`
    event may keep a deleted action it already has, so sending it back
    unchanged still works. Actions inferred from a label
    (actions_from_label) aren't written, so aren't checked."""
    if not event.action_ids or event.actions_from_label:
        return
    tree = get_action_store().tree()
    already: list[str] = []
    if existing and event.id and any(
        tree.by_id[a].status == "deleted" for a in event.action_ids if a in tree.by_id
    ):
        current = fill_in_from_actions([get_calendar_client().get_event(event.id)], tree)[0]
        already = current.action_ids or []
    try:
        tree.check_action_ids(event.action_ids, already=already)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def _check_facts(event: PublicEvent) -> None:
    """Refuse facts that aren't well formed (see utilities/facts.py), or
    that name people or a location that aren't there."""
    if event.facts is None:
        return
    facts = event.facts.normalized()
    problems = fact_problems(facts)
    if problems:
        raise ToolError("Its facts " + "; ".join(problems))
    people = {p.id for p in get_people_store().all()}
    unknown = [i for i in facts.people() if i not in people]
    if unknown:
        raise ToolError(f"Its facts name {unknown}, who aren't people (get_people lists them)")
    if facts.location_id and facts.location_id not in {loc.id for loc in get_location_store().all()}:
        raise ToolError(f"Its facts' location {facts.location_id!r} isn't a location (get_locations lists them)")


P = ParamSpec("P")
R = TypeVar("R")


def _prefetch(*tabs) -> None:
    """Read each of `tabs` -- objects for tabs of the calendar's metadata
    spreadsheet, with a `whole_tab` (or, for a store keeping two, its
    `whole_tabs`) and a `prefetch` (`Actions`, `People`, `Locations`,
    `NotedTimeSheet`, `CompactionJournal`) -- whole, in one read request,
    so the rest of this tool call's reads of them come from its
    `cached_sheet_reads` (see `SheetsClient.prefetch`). For a tool that
    reads more than one tab: Google Sheets caps read requests at 60 a
    minute, and each tab read on its own would cost one. Pinned by
    tests/test_sheet_read_requests.py."""
    tabs[0].prefetch([r for tab in tabs for r in getattr(tab, "whole_tabs", None) or [tab.whole_tab]])


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
    """List events between min_time and max_time. action_ids are what was
    done at each event, the first setting its color (action_names gives
    their names). effective_priority is what reallocation actually uses:
    the event's own priority, falling back to the highest priority of its
    actions.
    is_end_of_day_sleep marks the sleep event that
    ends a day; recurring_event_id is the id of an instance's recurring
    series (see get_recurrence and update_recurrence to read and edit the
    series as a whole); event_label_id is the calendar label derived from
    its first action. facts are what compaction established about a past
    event: where, who with, who for, and a note on each person there (see
    PublicEvent). action_names, effective_priority, is_end_of_day_sleep,
    recurring_event_id and event_label_id are read-only: update_event and
    create_event ignore them."""
    with track("list_events"), cached_sheet_reads():
        _prefetch_stores()
        events = get_calendar_client().list_events(min_time, max_time)
        return _public_events([event for event in events if event.status != "cancelled"])


@tool
def get_event(id: str) -> PublicEvent:
    """Get a single event by its ID. See list_events for its fields."""
    with track("get_event"), cached_sheet_reads():
        _prefetch_stores()
        event = get_calendar_client().get_event(id)
        if event.status == "cancelled":
            raise ToolError(f"Event {id} has been cancelled.")
        return _public_events([event])[0]


EventField = Literal[
    "description", "location", "min_duration", "is_fixed_duration", "is_fixed_time", "priority", "facts", "judgments"
]
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
    event follow its actions' priority again; clearing min_duration lets
    it shrink to nothing). Set action_ids to change its actions ([] for
    none). Set facts to replace its facts whole (see list_events).
    Returns the events affected by the update."""
    with track("update_event"), cached_sheet_reads():
        _prefetch_stores()
        _check_action_ids(event, existing=True)
        _check_facts(event)
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
        _prefetch_stores()
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
    priority, action_ids, facts or description are lost to an actions-only edit --
    and an edit to start/end moves them back onto the series' times too.
    Leave start/end out to keep each event's own time. A field left out
    is kept on the series itself, priority and action_ids included.

    A series split in Google Calendar itself (an id like
    "abc123_R20260915T223000") can't have its repeat or start changed
    whole -- Google refuses -- only from one of its later events on."""
    with track("update_recurrence"), cached_sheet_reads():
        _prefetch_stores()
        _check_action_ids(recurrence, existing=True)
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
    If the series can't be ended, the copy is cancelled again and the
    error says so. Returns the series from the event on, then the one before it (none if
    it was the series' first event, which leaves nothing to split)."""
    with track("split_recurrence"), cached_sheet_reads():
        _prefetch_stores()
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
    list_events, and each counts as a cancellation for follow_through
    trait parts -- past events too, whose time is then no longer counted
    as spent. Deleting this and following leaves no
    cancelled events: the series' events from that one on are gone (one
    edited on its own goes by where the series first put it, even if
    moved earlier), so follow_through doesn't count them at all. To stop
    a series that's already begun -- one that won't happen any more,
    rather than one that shouldn't have been -- delete from its next
    event on."""
    with track("delete_recurrence"), cached_sheet_reads():
        _prefetch_stores()
        try:
            left = get_recurrences().delete(id, starting_at_event_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return _public_recurrences([left] if left else [])


@tool
@writes
def create_event(event: PublicEvent, reallocate: bool = True) -> list[PublicEvent]:
    """Create a new event, optionally with actions (action_ids, the first
    setting its color), reallocating time from the rest of its day as needed to make
    room. With reallocate false, it's created only if nothing else has to
    change: otherwise nothing is written, and the error lists what would
    have changed. Returns the events affected by the creation."""
    with track("create_event"), cached_sheet_reads():
        _prefetch_stores()
        _check_action_ids(event)
        _check_facts(event)
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
        _prefetch_stores()
        cancelled = get_calendar_client().update_event(Event(id=id, status="cancelled"))
        return _public_events([cancelled])


@tool
def get_traits(statuses: list[TraitStatus] | None = None) -> list[ListedTrait]:
    """The traits: how the user wants to be with people (and with
    themselves), in order -- by default the active ones (rated) and those
    turned off (kept, but not rated for now), not archived ones
    (retired). Each has an id (fixed when it's created, and what a
    person's traits name), name, definition and parts. A trait's score is
    the weighted mean of its parts' scores (0-100), leaving out any with
    nothing to rate it by. Each part is {"kind": ..., "weight": 1,
    "engagement_type": "with", ...}: engagement_type "with" (the default)
    reads the events the person was at with the user, "for" those the
    user did for them while they weren't there. The kinds:
    "judgment" -- each event judged by the assistant on its own, never by
    asking the user, for one person or a named group, against a "rubric"
    (the question: "Was this activity or place new?") on a scale of
    "ratings" ({"0": "The activity and place were routine", "1": "There
    was a twist on the activity or place", "2": "The activity or the
    place were new", "3": "Both were new, or it was otherwise
    adventurous"}), from the "facts" it names: "action" (what was done),
    "action_history" (what's been done with them before), "location",
    "location_history" (where they've been together before),
    "general_notes" (the event's notes), "person_notes" (the notes on
    the person: the user's own for a "for" engagement, the other
    person's for "with") and "what_matters" (what's important to the
    person, as their what_matters says), each a name or {"fact": "action_history",
    "lookback_days": 90} (history facts look back 30 days by default),
    and scored over the person's events in its last "window_days" (30
    by default -- see get_trait_scores);
    "continuity" (the last event ended within "last_within_days" of the
    day's end and the next starts within "next_within_days" after it,
    both default 14: 100 for both, 50 for one, 0 for neither); "count"
    ({"kind": "count", "target": 1, "interval_days": 21, "zero_at_days":
    42, "noun": "visits"}), "duration" (with "target_min") and
    "follow_through" ("penalty", "recovery", "look_back_days"), as the
    measures of the same kind. continuity, count, duration and
    follow_through can take an "action" (an action or action group id) to
    count only its events. A person's traits can select which traits
    apply to them and give them their own parts for any (see
    create_person). problems lists anything wrong with a trait edited by
    hand; a bad part isn't rated. Read-only."""
    with track("get_traits"), cached_sheet_reads():
        try:
            return get_trait_store().get_traits(statuses)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def create_trait(trait: Trait) -> Trait:
    """Add a trait: a name (unique, at most 50 characters), a definition
    (what it means, in a sentence or two) and parts (at least one; see
    get_traits for each kind and its fields). Its status is active unless
    given ("off" to keep it unrated for now). Its id is made from its name
    and never changes, so renaming it later doesn't touch the measures
    naming it. A trait with a part that isn't well formed is refused,
    saying what's wrong. It applies to every person whose traits don't
    select others. Returns the trait as created."""
    with track("create_trait"), cached_sheet_reads():
        try:
            return get_trait_store().create_trait(trait)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


TraitField = Literal["definition"]
"""Every Trait field update_trait can clear (see utilities/traits.py's
CLEARABLE_TRAIT_FIELDS)."""


@tool
@writes
def update_trait(trait: Trait, clear_fields: list[TraitField] | None = None) -> Trait:
    """Change a trait, by id: rename it, reword its definition, change its
    status, or give it new parts. Fields left out keep their value; parts,
    if given, replace them whole, so send every part to keep. status is
    active (rated), off (kept, but not rated for now) or archived (retired:
    not rated, and get_traits leaves it out unless asked) -- archive a
    trait rather than delete it, so its history stays readable. Checked as
    for create_trait. Ratings already recorded aren't changed. Returns the
    trait as updated."""
    with track("update_trait"), cached_sheet_reads():
        try:
            return get_trait_store().update_trait(trait, clear_fields or ())
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_actions(statuses: list[ActionStatus] | None = None) -> ActionList:
    """Every action: what the user does with their time, each a verb
    phrase for what they're doing in a moment ("play guitar", "eat a
    meal"). By default the proposed and active ones, not archived or
    deleted ones. Each has its id, group_id (the action group it's in, if
    any), name, status, label_id, background_color, priority and note,
    plus its path through its groups ("Creative › Guitar › Play guitar"),
    its effective_color and effective_priority (its own, or else its
    nearest group's) and whether it holds_label: active actions always hold one of the calendar's event
    labels, and proposed ones do while there's room. Also how many of the
    calendar's label slots are in use. Read-only."""
    with track("get_actions"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_action_store().get_actions(statuses)
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_action(id_or_name: str) -> ListedAction:
    """One action, by its id or else its name (ignoring case), as
    get_actions lists it. If there's none, the error suggests close
    matches. Read-only."""
    with track("get_action"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_action_store().get_action(id_or_name)
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def create_action(action: Action) -> CreatedAction:
    """Create an action: a name (a verb phrase for what the user is doing,
    e.g. "practice guitar"; at most 50 characters, and unique among all
    actions, whatever their status), and optionally a group_id (an action
    group, see get_action_groups), a background_color (hex; inherited
    from its groups when unset, or else derived from priority), a priority
    (taken by its events that don't set their own; inherited from its
    groups when unset) and a note. id and
    label_id are assigned. status is "proposed" unless given: leave it so
    when you're adding an action no existing one matched, so the user can
    review it; give "active" when the user asked for it. Returns the new
    action's id as created_id, and, as changed, the new action (plus any
    proposed action that lost its label to make room), with
    label_slots_used of label_slots_total."""
    with track("create_action"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_action_store().create_action(action)
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


ActionField = Literal["group_id", "background_color", "priority", "note"]
"""Every Action field update_action can clear (see utilities/actions.py's
CLEARABLE_FIELDS)."""


@tool
@writes
def update_action(action: Action, clear_fields: list[ActionField] | None = None) -> ActionChanges:
    """Update an action by id: its group_id, name (still unique),
    status, background_color, priority or note. Omitted properties keep
    their value; list one in clear_fields to blank it instead. label_id
    can't be changed. status is proposed (not yet reviewed by the user;
    holds a label while there's room), active (holds a label), archived
    (not done any more) or deleted (shouldn't have existed); archiving or
    deleting frees its label, and making it active again restores it.
    Returns, as changed, the action as updated, plus any proposed action
    that gained or lost its label as a result, with label_slots_used of
    label_slots_total."""
    with track("update_action"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_action_store().update_action(action, clear_fields or ())
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_action_groups() -> list[ListedActionGroup]:
    """Every action group: a name that rolls actions up ("Creative",
    "Guitar"), for targets and finding one's way around. A group isn't an
    action -- no event can be tagged with one -- and groups can be inside
    other groups (group_id), so actions are always the leaves. Enclosing
    groups come before the ones inside them. Each has its id, group_id,
    name, background_color, priority and note, plus its path ("Creative ›
    Guitar") and its effective_color and effective_priority (its own, or
    else its nearest enclosing group's), which its actions and groups
    inherit when they don't set their own. Read-only."""
    with track("get_action_groups"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_action_store().get_action_groups()
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_action_group(id_or_name: str) -> ListedActionGroup:
    """One action group, by its id or else its name (ignoring case), as
    get_action_groups lists it. If there's none, the error suggests close
    matches. Read-only."""
    with track("get_action_group"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_action_store().get_action_group(id_or_name)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def create_action_group(group: ActionGroup) -> CreatedActionGroup:
    """Create an action group: a name (unique among groups, though an
    action may share it; at most 50 characters), and optionally a group_id
    (the group it's inside), a background_color, a priority and a note,
    which its actions and groups inherit when they don't set their own.
    id is assigned. Put actions in it with update_action's group_id.
    Returns the new group's id as created_id, and as changed, the group
    with its path and what it inherits."""
    with track("create_action_group"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_action_store().create_action_group(group)
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


ActionGroupField = Literal["group_id", "background_color", "priority", "note"]
"""Every ActionGroup field update_action_group can clear (see utilities/
action_groups.py's CLEARABLE_FIELDS)."""


@tool
@writes
def update_action_group(group: ActionGroup, clear_fields: list[ActionGroupField] | None = None) -> ActionGroupChanges:
    """Update an action group by id: its group_id (move it inside another
    group; clear it to make it top-level), name (still unique among
    groups), background_color, priority or note. Omitted properties keep
    their value; list one in clear_fields to blank it instead. Its actions
    that don't set their own color follow its new one on the calendar.
    Returns, as changed, the group as updated, and as affected_actions,
    every action whose path, effective_color or effective_priority
    changed as a result."""
    with track("update_action_group"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_action_store().update_action_group(group, clear_fields or ())
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def delete_action_group(group_id: str) -> DeletedActionGroup:
    """Delete an action group, by id. Its actions and the groups inside it
    aren't deleted: they move up into the group it was inside (or to the
    top level, if it was top-level). Returns the group as it was
    (deleted), as changed, the groups moved up out of it, and as
    affected_actions, every action moved or recolored as a result."""
    with track("delete_action_group"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_action_store().delete_action_group(group_id)
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_people(statuses: list[PersonStatus] | None = None) -> list[ListedPerson]:
    """The people the user spends time with: by default the active ones,
    not archived (out of touch) or deleted ones. The user themself is
    always first, with the id "self". Each has an id, name, context (what
    tells them apart from others of the same name, e.g. "met at salsa"),
    status, circles (the ids of the circles they're in) with their
    circle_names, what_matters (what's important to them) and traits:
    which traits apply to them, and their own parts for any (see
    create_person; without it, every active trait applies as get_traits
    has it). Read-only."""
    with track("get_people"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_people_store().get_people(statuses)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_person(id_or_name: str) -> ListedPerson:
    """One person, by id ("self" for the user) or else by name (ignoring
    case), as get_people lists them. If several share the name, the error
    lists them with their contexts; if there's none, it suggests close
    matches. Read-only."""
    with track("get_person"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_people_store().get_person(id_or_name)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def create_person(person: Person) -> CreatedPerson:
    """Add a person: a name, and optionally a context (what tells them
    apart, e.g. "met at salsa" -- a name and context together must be
    unique, so give one when the name's taken), circles (circle ids, see
    get_circles), what_matters (what's important to them) and traits:
    {"select": "all" or [trait ids], "parts": {trait id: [parts]}}, both
    optional -- the traits that apply to them (by default every active
    one), and parts replacing a trait's for them alone, each checked as a
    trait's own (see get_traits): a person's own cadence, say,
    {"parts": {"reliable": [{"kind": "count", "target": 1,
    "interval_days": 21, "noun": "visits"}]}}. Active unless given a
    status. id is assigned. Returns the person and their id as
    created_id."""
    with track("create_person"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_people_store().create_person(person)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


PersonField = Literal["context", "circles", "what_matters", "traits"]
"""Every Person field update_person can clear."""


@tool
@writes
def update_person(person: Person, clear_fields: list[PersonField] | None = None) -> ListedPerson:
    """Update a person by id ("self" for the user): their name, context,
    status (active; archived, out of touch; or deleted, shouldn't have
    existed -- self is always active), circles (replaced whole, so send
    every circle to keep), what_matters or traits (replaced whole; see
    create_person). Omitted properties keep their
    value; list one in clear_fields to blank it instead. Returns the
    person as updated."""
    with track("update_person"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_people_store().update_person(person, clear_fields or ())
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_circles() -> list[ListedCircle]:
    """Every circle: a group people belong to ("Close friends",
    "Family"), a person belonging to any number of them. Each has an id,
    name, note and member_ids (the people in it). Read-only."""
    with track("get_circles"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_people_store().get_circles()
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_circle(id_or_name: str) -> ListedCircle:
    """One circle, by id or else by name (ignoring case), as get_circles
    lists it. Read-only."""
    with track("get_circle"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_people_store().get_circle(id_or_name)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def create_circle(circle: Circle) -> CreatedCircle:
    """Add a circle: a name (unique among circles, though a person may
    share it) and optionally a note. id is assigned. Put people in it with
    update_person's circles. Returns the circle and its id as
    created_id."""
    with track("create_circle"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_people_store().create_circle(circle)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


CircleField = Literal["note"]
"""Every Circle field update_circle can clear."""


@tool
@writes
def update_circle(circle: Circle, clear_fields: list[CircleField] | None = None) -> ListedCircle:
    """Rename a circle or change its note, by id. Returns it as updated."""
    with track("update_circle"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_people_store().update_circle(circle, clear_fields or ())
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def delete_circle(circle_id: str) -> DeletedCircle:
    """Delete a circle, by id. Its people aren't deleted: they just leave
    it. Returns the circle as it was, and the ids of the people who
    left."""
    with track("delete_circle"), cached_sheet_reads():
        _prefetch_stores()
        try:
            return get_people_store().delete_circle(circle_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_locations() -> list[Location]:
    """Every location: a place the user's events happen ("Home", "Salsa
    studio"), each with an id, a name and a hint for recognizing when an
    event or note refers to it. Read-only."""
    with track("get_locations"), cached_sheet_reads():
        try:
            return get_location_store().all()
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_location(id_or_name: str) -> Location:
    """One location, by id or else by name (ignoring case). If there's
    none, the error suggests close matches. Read-only."""
    with track("get_location"), cached_sheet_reads():
        try:
            return get_location_store().get_location(id_or_name)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def create_location(location: Location) -> CreatedLocation:
    """Add a location: a name (unique among locations) and a hint for
    recognizing when an event or note refers to it -- other names for it,
    an address, what happens there ("the apartment; 'home', 'my place'").
    id is assigned. Returns the location and its id as created_id."""
    with track("create_location"), cached_sheet_reads():
        try:
            return get_location_store().create_location(location)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


LocationField = Literal["hint"]
"""Every Location field update_location can clear."""


@tool
@writes
def update_location(location: Location, clear_fields: list[LocationField] | None = None) -> Location:
    """Rename a location or change its hint, by id. Omitted properties keep
    their value; list hint in clear_fields to blank it. Returns it as
    updated."""
    with track("update_location"), cached_sheet_reads():
        try:
            return get_location_store().update_location(location, clear_fields or ())
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def delete_location(location_id: str) -> Location:
    """Delete a location, by id. Returns it as it was."""
    with track("delete_location"), cached_sheet_reads():
        try:
            return get_location_store().delete_location(location_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@dataclass(kw_only=True)
class CompactionStatus:
    last_compaction: datetime | None = None
    """When notes were last compacted into the calendar: it's settled
    fact up to then. `None` if they never have been."""

    latest_compacted_note: NotedTime | None = None
    """The compacted note with the latest timestamp, if any."""

    judgments_pending: str | None = None
    """The last compaction's id, if its judgments aren't all made yet:
    it isn't complete until they are (see prepare_judgments)."""


@tool
def get_compaction_status() -> CompactionStatus:
    """When notes were last compacted into the calendar, the latest note
    compacted, and whether that compaction still has judgments to make.
    Read-only."""
    with track("get_compaction_status"), cached_sheet_reads():
        # The notes, the journal, and what judging reads, together.
        get_note_compactor().prefetch()
        _notes, latest = get_noted_time_sheet().read_with_latest_compacted()
        try:
            due = get_note_compactor().judgments_due()
        except CompactionError:
            due = None
        return CompactionStatus(
            last_compaction=get_compaction_journal().last_stamped_now(),
            latest_compacted_note=latest,
            judgments_pending=due.compaction_id if due is not None and due.requests else None,
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
    notes show happened differently, and the facts of each past event --
    its actions, where it was, who was there, who it was for, and a note
    on each person there -- then call compact_notes with those decisions.
    Also every action an event can be given (`actions`, their groups left
    out), every person (`people`, the user as "self") and every location
    (`locations`), to settle the facts from. judgments_pending names the
    last compaction if its judgments aren't all made: make them first
    (prepare_judgments, record_judgments). Read-only."""
    with track("prepare_compaction"), cached_sheet_reads():
        return get_note_compactor().prepare()


@tool
@writes
def compact_notes(
    decisions: list[EventDecision] | None = None,
    ignore_notes: list[str] | None = None,
    compaction_id: str | None = None,
    dry_run: bool = True,
    new_actions: list[NewAction] | None = None,
    new_people: list[NewPerson] | None = None,
    new_locations: list[NewLocation] | None = None,
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
    `ignore_notes`. A 'keep' or 'create' also sets an event's action_ids
    and facts (where, who with, who for, a note on each person there).
    new_actions, new_people and new_locations add what isn't there yet,
    each with a `ref` ("new:ukulele") the decisions use in place of its
    id; they're created when the plan is applied (see
    prepare_compaction's instructions). dry_run=True (the default)
    changes nothing: you get the proposed changes, a compaction_id, the
    additions, and a `timeline` of the notes beside the resulting events,
    each with its actions and facts -- show its `text` to the user in a
    code block.
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
    dry_run=True you just get that compaction's stored plan back.

    Step 4: once it's applied, the result's `judgments` are the traits to
    judge for each person its events were about: make every one yourself,
    right away, without asking the user, and record them with
    record_judgments. The compaction isn't complete until they're all
    recorded."""
    with track("compact_notes"), cached_sheet_reads():
        compactor = get_note_compactor()
        try:
            if compaction_id is None:
                if not dry_run:
                    raise CompactionError(
                        "run a dry run first (decisions, dry_run=True) and pass its compaction_id"
                    )
                return compactor.dry_run(decisions or [], ignore_notes, new_actions, new_people, new_locations)
            if dry_run:
                return compactor.describe(compaction_id)
            return compactor.commit(compaction_id)
        except CompactionError as exc:
            raise ToolError(str(exc)) from exc


@tool
def prepare_judgments(compaction_id: str | None = None, redo: bool = False) -> JudgmentsDue:
    """The judgments a compaction (by default the last one applied) calls
    for, to make with record_judgments: one request per event with facts,
    per person it was about (the user, "self", and everyone there, for a
    trait's "with" parts; everyone it was done for, for its "for" parts),
    per judgment part of the traits that apply to them. Each has the
    rubric, the ratings to choose from, the facts the part names (its
    actions, where it was, the history with that person over the part's
    lookback, the event's notes, the notes on them), and the framing to
    judge it in. Only those not made yet -- or with redo, all of them,
    each with the judgment already made, to redo one with more context.
    Read-only."""
    with track("prepare_judgments"), cached_sheet_reads():
        compactor = get_note_compactor()
        compactor.prefetch()
        try:
            due = compactor.judgments_due(compaction_id, redo=redo)
        except CompactionError as exc:
            raise ToolError(str(exc)) from exc
        if due is None:
            raise ToolError("there's no applied compaction to judge")
        return due


@tool
@writes
def record_judgments(compaction_id: str, judgments: list[Judgment]) -> JudgmentsResult:
    """Record judgments of a compaction's events (see prepare_judgments, and
    the `judgments` an applied compaction returns): each a request_id, a
    rating from that request's ratings, and one succinct line of
    reasoning. Make them yourself, never asking the user. They're kept on
    the events, by person, trait and part; a judgment recorded again
    replaces the earlier one. Returns how many were recorded, the ids of
    any requests still to judge, and whether that completes the
    compaction."""
    with track("record_judgments"), cached_sheet_reads():
        compactor = get_note_compactor()
        compactor.prefetch()
        try:
            return compactor.record_judgments(compaction_id, judgments)
        except CompactionError as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_trait_scores(
    person_id: str | None = None, start: date | None = None, end: date | None = None
) -> list[TraitScoreRow]:
    """Each person's daily trait scores (0-100): one row per person per day
    (or just person_id's), from start to end inclusive, each with its
    traits' scores and, under parts, each part's score and how it was
    reached. A trait's score is the weighted mean of its parts': its
    judgments (the mean of their ratings on the person's events in the
    part's window) and its computed parts (continuity, count, duration and
    follow_through over the person's events -- see get_traits). A day is
    rolled up once a compaction that settled it is complete; days without
    a row haven't been (see rebuild_trait_scores). Read-only."""
    with track("get_trait_scores"), cached_sheet_reads():
        return get_trait_rollup().get(person_id, start, end)


@tool
@writes
def rebuild_trait_scores(start: date, end: date) -> list[TraitScoreRow]:
    """Roll every active person's trait scores up again for each day from
    start to end inclusive that's over -- to backfill days from before
    scores were kept, or after a judgment was redone or a trait changed.
    Replaces those days' rows; returns them."""
    with track("rebuild_trait_scores"), cached_sheet_reads():
        if end < start:
            raise ToolError("end is before start")
        days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
        return get_trait_rollup().roll_up(days)


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
        # Keep the Compactions calendar's days the same as the main one's.
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
