from __future__ import annotations

import contextlib
import functools
import logging
import os
from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Literal, ParamSpec, TypeVar
from zoneinfo import ZoneInfo

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp

from calendar_clients.google_calendar import (
    CalendarClient,
    Event,
    EventLabelConflictError,
    TimeZoneNotSetError,
    cached_calendar_listings,
)
from calendar_clients.google_sheets import cached_sheet_reads
from calendar_clients.write_lock import WRITE_LOCK
from config import (
    build_actions,
    build_calendar_client,
    build_compaction_journal,
    build_habits,
    build_compaction_schedule,
    build_locations,
    build_noted_time_sheet,
    build_people,
    build_cancellations,
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
    PriorityColor,
    PriorityColorChange,
)
from utilities.action_calendar import ActionCalendar, fill_in_from_actions
from utilities.compaction_additions import NewAction, NewLocation, NewPerson
from utilities.facts import Facts, fact_problems
from utilities.judgments import HabitJudgmentsDue, Judging, Judgment, JudgmentsDue, JudgmentsResult
from utilities import event_changes
from utilities.cancellations import Cancellations
from utilities.event_changes import Cancel, ChangeError, EventChanges, Shift
from utilities.habits import CreatedHabit, Habit, Habits, HabitStatus, ListedHabit, action_scopes, subject_id
from utilities.compaction_schedule import CompactionSchedule, CompactionScheduleHints, ScheduleHint
from utilities.locations import CreatedLocation, Location, Locations
from utilities.memory_diagnostics import track
from utilities.note_compaction import CompactionCreate, CompactionError, CompactionUpdate, EventDecision, NoteAnnotation
from utilities.compaction_journal import CompactionJournal
from utilities.compaction_proposals import (
    AdditionChoice,
    Feedback,
    FeedbackReply,
    NoteEdit,
    Proposal,
    ProposalResult,
    ProposalSummary,
)
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
from utilities.recurrences import Recurrences, Repeat, describe_rules
from utilities.traits import ListedTrait, Trait, Traits, TraitStatus
from workos_auth import WorkOSTokenVerifier

# Without this, INFO-level logs (utilities/memory_diagnostics.py's, e.g.)
# are silently dropped -- the root logger defaults to WARNING with no
# handler. Its default StreamHandler writes to stderr, never stdout, so
# this is safe under the stdio transport too, whose protocol messages
# themselves go over stdout.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

    is_cancelled is read-only: update_event refuses to set it -- cancel
    an event with delete_event, which says whether the cancellation counts
    against follow-through.

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
    the priority that applies, i.e. priority falling back,
    when the event doesn't set one, to the highest (lowest-numbered)
    priority among its actions (each action's own, or its nearest
    group's). Kept separate from priority so that sending a listed event
    straight back to update_event never copies its actions' priority onto
    the event itself, which would stop it following later changes to the
    actions. to_event ignores it.

    is_end_of_day_sleep/recurring_event_id are read-only too, and
    to_event ignores them as well. is_end_of_day_sleep decides where a
    day ends for compaction (see utilities/note_compactor.py),
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
    (replacing them whole), or clear them.

    Everything before the last compaction (get_compaction_status's
    last_compaction) is history: see update_event."""

    id: str | None = None
    summary: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    description: str | None = None
    location: str | None = None
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
    facts are those of every event in it: usually who it's with or for,
    and where (location_id) -- set them to replace them whole.

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
    priority: int | None = None
    action_ids: list[str] | None = None
    action_names: list[str] | None = None
    actions_from_label: bool = False
    event_label_id: str | None = None
    effective_priority: int | None = None
    facts: Facts | None = None

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
            priority=event.priority,
            action_ids=event.action_ids,
            action_names=public.action_names,
            actions_from_label=event.actions_from_label,
            event_label_id=event.event_label_id,
            effective_priority=event.effective_priority,
            facts=event.facts,
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
            priority=self.priority,
            action_ids=None if self.actions_from_label else self.action_ids,
            facts=self.facts.normalized() if self.facts is not None else None,
            cleared=frozenset(clear_fields),
        )


# Each get_* helper below builds its object under WRITE_LOCK, the first
# time only: building some writes to Google (creating a spreadsheet or
# tab), and two tool calls arriving together mustn't both build one.
_calendar_client: CalendarClient | None = None
_noted_time_sheet: NotedTimeSheet | None = None
_compaction_journal: CompactionJournal | None = None
_note_compactor: NoteCompactor | None = None
_recurrences: Recurrences | None = None
_traits: Traits | None = None
_actions: Actions | None = None
_people: People | None = None
_locations: Locations | None = None
_habits: Habits | None = None
_compaction_schedule: CompactionSchedule | None = None
_cancellations: Cancellations | None = None


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


def get_compaction_schedule_store() -> CompactionSchedule:
    """Lazily construct and cache the CompactionSchedule, the same way
    the other get_* helpers cache theirs. Building it the first time adds
    the Compaction Schedule tab."""
    global _compaction_schedule
    if _compaction_schedule is None:
        with WRITE_LOCK:
            if _compaction_schedule is None:
                _compaction_schedule = build_compaction_schedule()
    return _compaction_schedule


def get_habit_store() -> Habits:
    """Lazily construct and cache the Habits, the same way the other get_*
    helpers cache theirs. Building it the first time adds the Habits tab.
    A habit's action_id is checked against the actions and groups, and
    its traits against the Traits tab."""
    global _habits
    if _habits is None:
        with WRITE_LOCK:
            if _habits is None:
                _habits = build_habits(
                    scopes=lambda: action_scopes(get_action_store().all(), get_action_store().groups()),
                    trait_ids=lambda: [t.id for t in get_trait_store().all()],
                )
    return _habits


def _prefetch_habits(*, cancellations: bool = False) -> None:
    """The habits', actions' and traits' tabs, in one request: what the
    habit tools read (a habit's action and traits are checked, and its
    action pathed) -- and, `cancellations`, the Cancellations tab, and
    what it reads, for listing them."""
    tabs = [get_habit_store(), get_action_store(), get_trait_store()]
    _prefetch(*tabs, *([get_cancellation_store()] if cancellations else []))


def _habits_with_cancellations(habits: list[ListedHabit]) -> list[ListedHabit]:
    """`habits`, each with the events the user cancelled that count
    against its follow-through."""
    by_subject = get_cancellation_store().by_person()
    return [replace(h, cancelled_events=by_subject.get(subject_id(h.id), [])) for h in habits]


def get_event_changes() -> EventChanges:
    """What the event tools check and write their batches with (see
    utilities/event_changes.py): through the actions, so each event's
    label follows them."""
    return EventChanges(ActionCalendar(get_calendar_client(), get_action_store()), get_cancellation_store())


def get_cancellation_store() -> Cancellations:
    """Lazily construct and cache the Cancellations, the same way the
    other get_* helpers cache theirs. Building it the first time adds the
    Cancellations tab."""
    global _cancellations
    if _cancellations is None:
        with WRITE_LOCK:
            if _cancellations is None:
                _cancellations = build_cancellations(
                    get_action_store(), get_people_store(), get_trait_store(), habits=get_habit_store()
                )
    return _cancellations


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
                    calendar=ActionCalendar(get_calendar_client(), get_action_store()),
                    # Written through actions, so each event's label follows them.
                    client=ActionCalendar(get_calendar_client(), get_action_store()),
                    actions=get_action_store(),
                    people=get_people_store(),
                    locations=get_location_store(),
                    notes=get_noted_time_sheet(),
                    journal=get_compaction_journal(),
                    judging=Judging(
                        client=ActionCalendar(get_calendar_client(), get_action_store()),
                        actions=get_action_store(),
                        people=get_people_store(),
                        locations=get_location_store(),
                        traits=get_trait_store(),
                        habits=get_habit_store(),
                    ),
                    cancellations=get_cancellation_store(),
                )
    return _note_compactor


def _rejected(tool_name: str, exc: ChangeError) -> ToolError:
    """Log a tool refusing a call -- a compaction or a batch of event
    changes -- by the categories of mistake it found (see ChangeError),
    and return the ToolError to raise --
    so the logs show which instructions a model gets wrong, and how often."""
    logger.warning(
        "compaction rejected: tool=%s categories=%s: %s",
        tool_name,
        ",".join(exc.categories),
        " | ".join(str(exc).splitlines()),
    )
    return ToolError(str(exc))


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


def _check_facts(event: PublicEvent | PublicRecurrence) -> None:
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


@contextlib.contextmanager
def cached_reads() -> Iterator[None]:
    """What every tool call runs in: its spreadsheet reads
    (`cached_sheet_reads`) and its calendar listings
    (`cached_calendar_listings`) each answered from memory when they
    repeat one already made in the same call."""
    with cached_sheet_reads(), cached_calendar_listings():
        yield


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
    their names). effective_priority is the priority that applies:
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
    with track("list_events"), cached_reads():
        _prefetch_stores()
        events = get_calendar_client().list_events(min_time, max_time)
        return _public_events([event for event in events if event.status != "cancelled"])


@tool
def get_event(id: str) -> PublicEvent:
    """Get a single event by its ID. See list_events for its fields."""
    with track("get_event"), cached_reads():
        _prefetch_stores()
        event = get_calendar_client().get_event(id)
        if event.status == "cancelled":
            raise ToolError(f"Event {id} has been cancelled.")
        return _public_events([event])[0]


EventField = Literal[
    "description",
    "location",
    "priority",
    "facts",
    "judgments",
]
"""Every event field update_event and update_recurrence can clear (see
calendar_clients/google_calendar.py's CLEARABLE_EVENT_FIELDS)."""


@dataclass(kw_only=True)
class EventUpdate:
    """One event's update: `event` (its id and the fields to set), and the
    fields to remove (`clear_fields`)."""

    event: PublicEvent
    clear_fields: list[EventField] | None = None


@dataclass(kw_only=True)
class EventCancel:
    """An event to cancel, and whether that counts against follow-through:
    a commitment dropped (true) rather than just a change of plan (false)."""

    event_id: str
    counts_against_follow_through: bool


@dataclass(kw_only=True)
class ProposalUpdate(EventUpdate):
    """An edit of one of a proposal's events, as update_event takes one --
    `event` (its id, or a new event's key, and the fields to set) and
    `clear_fields` -- plus `start_note`/`end_note`: a note that sets that
    edge, at its time."""

    start_note: str | None = None
    end_note: str | None = None


@dataclass(kw_only=True)
class ProposalCreate(PublicEvent):
    """An event to add to a proposal, as create_event takes one, plus
    `start_note`/`end_note`: a note that sets that edge, at its time."""

    start_note: str | None = None
    end_note: str | None = None


def _proposal_decision(
    event: PublicEvent,
    action: str,
    *,
    start_note: str | None,
    end_note: str | None,
    clear_fields: Collection[str] = (),
) -> EventDecision:
    """An `amend_proposal` update or create, as the decision it makes. Its
    description is the event's whole description, notes and all: none are
    added to it. CompactionError for what a proposal can't take."""
    problems = []
    if event.is_cancelled:
        problems.append("to cancel an event, put it in `cancels`")
    if event.judgments is not None or "judgments" in clear_fields:
        problems.append("judgments are made once a proposal's applied: leave them out")
    if "priority" in clear_fields:
        problems.append("a proposal can't clear an event's priority: set one")
    if action == "keep" and not event.id:
        problems.append("an update needs the event's id (or a new event's key)")
    both = sorted(set(clear_fields) & {f for f in ("description", "location", "facts") if getattr(event, f) is not None})
    if both:
        problems.append(f"{', '.join(both)}: set or cleared, not both")
    if problems:
        raise CompactionError.of([f"{event.id or event.summary!r}: {p}" for p in problems], "malformed_decision")
    return EventDecision(
        action=action,
        event_id=event.id if action == "keep" else None,
        summary=event.summary,
        start=event.start,
        end=event.end,
        start_note=start_note,
        end_note=end_note,
        # Inferred actions, sent back, aren't the event's to store.
        action_ids=None if event.actions_from_label else event.action_ids,
        facts=Facts() if "facts" in clear_fields else event.facts,
        description="" if "description" in clear_fields else event.description,
        location="" if "location" in clear_fields else event.location,
        priority=event.priority,
    )


@dataclass(kw_only=True)
class EventShift:
    """Events to move together by `minutes` (later if positive, earlier if
    negative), keeping their lengths."""

    event_ids: list[str]
    minutes: int


@dataclass(kw_only=True)
class EventChangesResult:
    events: list[PublicEvent]
    """The events changed (or, in a dry run, as they would be)."""

    timeline: str | None = None
    """The changes beside the events around them, as compaction draws its
    plans: show it in a monospace block."""

    dry_run: bool = False


@tool
@writes
def update_event(
    updates: list[EventUpdate] | None = None,
    creates: list[PublicEvent] | None = None,
    cancels: list[EventCancel] | None = None,
    shifts: list[EventShift] | None = None,
    allow_compacted_changes: bool = False,
    dry_run: bool = False,
) -> EventChangesResult:
    """Change several events at once, as one batch: `updates` (each an
    event's id and the fields to set -- left out, a field keeps its value;
    list it in that update's clear_fields to remove it instead -- clearing
    priority makes it follow its actions' priority again), `creates` (new
    events, optionally with actions: action_ids, the first setting its
    color), `cancels` (each saying whether it counts against
    follow-through: a commitment dropped, rather than a change of plan --
    see delete_event) and `shifts` (events moved together by the same
    number of minutes, each as an update). An event can be in only one of
    them. Set action_ids to change an event's actions ([] for none), and
    facts to replace its facts whole (see list_events). An update can't
    cancel an event (is_cancelled): put it in `cancels`.

    BATCHES ARE CHECKED WHOLE, AND NOTHING IS MOVED TO MAKE ROOM: every
    event the call creates or updates must end after it starts and must
    not overlap any other event -- one on the calendar or another in the
    call -- so move, shorten or cancel whatever's in the way in the same
    call. A call that breaks this changes nothing, and the error lists
    every problem and the events around them, to send a valid call at
    once.

    Everything before the last compaction (get_compaction_status's
    last_compaction) is history: an event that started before it can't be
    changed or cancelled, and no event can be created, or moved, to start
    before it, unless allow_compacted_changes is set -- ONLY set it when
    the user has explicitly approved changing history -- except that an
    event still going on then may run on (an update moving only its end,
    to no earlier than the last compaction). dry_run checks the batch
    and returns what it would do without changing anything. Returns the
    events changed, and a timeline of them beside the events around
    them."""
    with track("update_event"), cached_reads():
        _prefetch_for_changes()
        for update in updates or ():
            if update.event.is_cancelled:
                raise ToolError(
                    "an update can't cancel an event: put it in cancels, saying with counts_against_follow_through "
                    "whether the cancellation counts against follow-through"
                )
            _check_action_ids(update.event, existing=True)
            _check_facts(update.event)
        for event in creates or ():
            _check_action_ids(event)
            _check_facts(event)
        try:
            patches = [u.event.to_event(u.clear_fields or ()) for u in updates or ()]
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return _change_events(
            "update_event",
            updates=patches,
            creates=[e.to_event() for e in creates or ()],
            cancels=[Cancel(event_id=c.event_id, counts_against_follow_through=c.counts_against_follow_through) for c in cancels or ()],
            shifts=[Shift(event_ids=s.event_ids, minutes=s.minutes) for s in shifts or ()],
            allow_compacted=allow_compacted_changes,
            dry_run=dry_run,
        )

@tool
def get_recurrence(id: str) -> PublicRecurrence:
    """A recurring series, by its id or the id of any of its events (an
    event's recurring_event_id is its series' id). See PublicRecurrence
    for its fields."""
    with track("get_recurrence"), cached_reads():
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
    skipped/added), so send every part of it you want kept. A series' edits
    aren't checked for overlaps. Returns the edited series, then the earlier part if it
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
    with track("update_recurrence"), cached_reads():
        _prefetch_stores()
        _check_action_ids(recurrence, existing=True)
        _check_facts(recurrence)
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
    with track("split_recurrence"), cached_reads():
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
    list_events -- past events too, whose time is then no longer counted
    as spent. Deleting this and following leaves no
    cancelled events: the series' events from that one on are gone (one
    edited on its own goes by where the series first put it, even if
    moved earlier). Neither counts against follow-through: only an event
    the user cancelled does (see delete_event). To stop
    a series that's already begun -- one that won't happen any more,
    rather than one that shouldn't have been -- delete from its next
    event on."""
    with track("delete_recurrence"), cached_reads():
        _prefetch_stores()
        try:
            left = get_recurrences().delete(id, starting_at_event_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return _public_recurrences([left] if left else [])


@tool
@writes
def create_event(
    events: list[PublicEvent], allow_compacted_changes: bool = False, dry_run: bool = False
) -> EventChangesResult:
    """Create new events, as one batch, optionally with actions
    (action_ids, the first setting its color) and facts. update_event
    takes creates too, beside updates and cancels, for a change that
    makes room for them.

    BATCHES ARE CHECKED WHOLE, AND NOTHING IS MOVED TO MAKE ROOM: every
    event the call creates or updates must end after it starts and must
    not overlap any other event -- one on the calendar or another in the
    call -- so move, shorten or cancel whatever's in the way in the same
    call. A call that breaks this changes nothing, and the error lists
    every problem and the events around them, to send a valid call at
    once.

    An event can't be created to start before the last compaction
    (get_compaction_status's last_compaction): that's history, unless
    allow_compacted_changes is set -- ONLY set it when the user has
    explicitly approved changing history. dry_run checks them and returns
    what it would do without changing anything. Returns the events created, and a timeline of them beside
    the events around them."""
    with track("create_event"), cached_reads():
        _prefetch_for_changes()
        for event in events:
            _check_action_ids(event)
            _check_facts(event)
        return _change_events(
            "create_event",
            creates=[e.to_event() for e in events],
            allow_compacted=allow_compacted_changes,
            dry_run=dry_run,
        )

@tool
@writes
def delete_event(
    cancels: list[EventCancel], allow_compacted_changes: bool = False, dry_run: bool = False
) -> EventChangesResult:
    """Delete (cancel) events, as one batch. Given one event of a recurring
    series, deletes only that event; to delete the whole series, or an
    event and the ones after it, use delete_recurrence. Each cancel says,
    with counts_against_follow_through, whether it counts against
    follow-through: true records it against the follow-through of each
    person a follow-through trait part matches it for -- the user and
    everyone it was planned with (facts' with_ids), or for (for_ids), if
    it's of the part's action -- as a commitment the user dropped; false
    is just a change of plan, and counts against no one. An event that
    started before the last compaction (get_compaction_status's
    last_compaction) is history, and can't be cancelled unless
    allow_compacted_changes is set -- ONLY set it when the user has
    explicitly approved changing history. dry_run returns
    what it would do without changing anything. Returns the events
    cancelled."""
    with track("delete_event"), cached_reads():
        _prefetch_for_changes()
        return _change_events(
            "delete_event",
            cancels=[Cancel(event_id=c.event_id, counts_against_follow_through=c.counts_against_follow_through) for c in cancels],
            allow_compacted=allow_compacted_changes,
            dry_run=dry_run,
        )


def _change_events(tool_name: str, *, dry_run: bool = False, **batch) -> EventChangesResult:
    """Check `batch` (see utilities/event_changes.py) and, unless `dry_run`,
    write it."""
    changes = get_event_changes()
    try:
        checked = changes.check(**batch, history_until=get_compaction_journal().last_stamped_now())
    except ChangeError as exc:
        raise _rejected(tool_name, exc) from exc
    drawn = event_changes.timeline(checked)
    if dry_run:
        return EventChangesResult(
            events=_public_events([c.after or replace(c.before, status="cancelled") for c in checked.changes]),
            timeline=drawn.text if drawn is not None else None,
            dry_run=True,
        )
    return EventChangesResult(
        events=_public_events(changes.apply(checked, tool_name)),
        timeline=drawn.text if drawn is not None else None,
    )

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
    by default);
    "continuity" (the last event ended within "last_within_days" of the
    day's end and the next starts within "next_within_days" after it,
    both default 14: 100 for both, 50 for one, 0 for neither); "count"
    ({"kind": "count", "target": 1, "interval_days": 21, "zero_at_days":
    42, "noun": "visits"}), "duration" (with "target_min") and
    "follow_through" ("penalty", "recovery", "look_back_days"), as the
    measures of the same kind -- follow_through counting the events the
    user cancelled: said in compaction didn't happen, or deleted with
    delete_event's counts_against_follow_through, never a plan merely
    changed. continuity, count, duration and
    follow_through can take an "action" (an action or action group id) to
    count only its events. A person's traits can select which traits
    apply to them and give them their own parts for any (see
    create_person). problems lists anything wrong with a trait edited by
    hand; a bad part isn't rated. Read-only."""
    with track("get_traits"), cached_reads():
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
    with track("create_trait"), cached_reads():
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
    with track("update_trait"), cached_reads():
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
    with track("get_actions"), cached_reads():
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
    with track("get_action"), cached_reads():
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
    with track("create_action"), cached_reads():
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
    with track("update_action"), cached_reads():
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
    with track("get_action_groups"), cached_reads():
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
    with track("get_action_group"), cached_reads():
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
    with track("create_action_group"), cached_reads():
        _prefetch_stores()
        try:
            return get_action_store().create_action_group(group)
        except (ValueError, EventLabelConflictError) as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_priority_colors() -> list[PriorityColor]:
    """Each priority's color, 0 to 3: an event with no action's label is
    colored by its priority's label (priority 2's if it has none), and an
    action or group with no color of its own (nor its groups') takes its
    priority's. Each has its priority, color (#rrggbb) and label_id.
    Read-only."""
    with track("get_priority_colors"), cached_reads():
        _prefetch_stores()
        try:
            return get_action_store().priority_colors()
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def update_priority_color(priority: int, color: str) -> PriorityColorChange:
    """Change a priority's color (priority 0 to 3; color as #rrggbb): its
    label's, so events colored by their priority change with it, and every
    action's label that takes its color from that priority. Returns every
    priority's color as colors, and as affected_actions, the actions
    whose effective_color changed."""
    with track("update_priority_color"), cached_reads():
        _prefetch_stores()
        try:
            return get_action_store().set_priority_color(priority, color)
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
    with track("update_action_group"), cached_reads():
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
    with track("delete_action_group"), cached_reads():
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
    has it). Each also has cancelled_events: the events the user cancelled
    that count against their follow-through, newest first -- when each was
    planned, its actions, whether they were to be there ("with") or it was
    for them ("for"), the follow-through parts it counted against, and
    what cancelled it. Read-only."""
    with track("get_people"), cached_reads():
        _prefetch_people()
        try:
            return _with_cancellations(get_people_store().get_people(statuses))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_person(id_or_name: str) -> ListedPerson:
    """One person, by id ("self" for the user) or else by name (ignoring
    case), as get_people lists them. If several share the name, the error
    lists them with their contexts; if there's none, it suggests close
    matches. Read-only."""
    with track("get_person"), cached_reads():
        _prefetch_people()
        try:
            return _with_cancellations([get_people_store().get_person(id_or_name)])[0]
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


def _prefetch_people() -> None:
    """`_prefetch_stores`, and the Cancellations tab, in one request."""
    _prefetch(get_action_store(), get_people_store(), get_location_store(), get_cancellation_store())


def _prefetch_for_changes() -> None:
    """`_prefetch_people`, and the compaction journal -- where history ends
    (see utilities/event_changes.py) -- in one request."""
    _prefetch(
        get_action_store(), get_people_store(), get_location_store(), get_cancellation_store(),
        get_compaction_journal(),
    )


def _with_cancellations(people: list[ListedPerson]) -> list[ListedPerson]:
    """`people`, each with the events the user cancelled that count
    against their follow-through (see utilities/cancellations.py)."""
    by_person = get_cancellation_store().by_person()
    return [replace(p, cancelled_events=by_person.get(p.id, [])) for p in people]


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
    with track("create_person"), cached_reads():
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
    with track("update_person"), cached_reads():
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
    with track("get_circles"), cached_reads():
        _prefetch_stores()
        try:
            return get_people_store().get_circles()
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_circle(id_or_name: str) -> ListedCircle:
    """One circle, by id or else by name (ignoring case), as get_circles
    lists it. Read-only."""
    with track("get_circle"), cached_reads():
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
    with track("create_circle"), cached_reads():
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
    with track("update_circle"), cached_reads():
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
    with track("delete_circle"), cached_reads():
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
    with track("get_locations"), cached_reads():
        try:
            return get_location_store().all()
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_location(id_or_name: str) -> Location:
    """One location, by id or else by name (ignoring case). If there's
    none, the error suggests close matches. Read-only."""
    with track("get_location"), cached_reads():
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
    with track("create_location"), cached_reads():
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
    with track("update_location"), cached_reads():
        try:
            return get_location_store().update_location(location, clear_fields or ())
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def delete_location(location_id: str) -> Location:
    """Delete a location, by id. Returns it as it was."""
    with track("delete_location"), cached_reads():
        try:
            return get_location_store().delete_location(location_id)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_habits(statuses: list[HabitStatus] | None = None) -> list[ListedHabit]:
    """The user's habits: by default the active ones, not archived (set
    aside) or deleted ones. Each is about the user's events with one
    action, or with any action under one action group (action_id, with
    its action_path), and is rated by traits as a person is: its traits
    say which apply (by default every active one) and give it its own
    parts -- its own rubrics -- for any (see create_habit). Each has an
    id, name, status and note (what it's for, and what doing it well
    looks like). Each also has cancelled_events: the events the user
    cancelled that count against its follow-through, newest first, as a
    person's (see get_people). Its events' judgments are kept under
    "habit:<id>", as a person's are under their id. Read-only."""
    with track("get_habits"), cached_reads():
        _prefetch_habits(cancellations=True)
        try:
            return _habits_with_cancellations(get_habit_store().get_habits(statuses))
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
def get_habit(id_or_name: str) -> ListedHabit:
    """One habit, by id or else by name (ignoring case), as get_habits
    lists it, whatever its status. If there's none, the error suggests
    close matches. Read-only."""
    with track("get_habit"), cached_reads():
        _prefetch_habits(cancellations=True)
        try:
            return _habits_with_cancellations([get_habit_store().get_habit(id_or_name)])[0]
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


@tool
@writes
def create_habit(habit: Habit) -> CreatedHabit:
    """Add a habit: a name (unique among habits), the action or action
    group whose events it's about (action_id: an action's or a group's
    id; only events with that action, or any action under that group,
    count toward it), and optionally a note (what it's for, and what
    doing it well looks like -- context for judging its events) and
    traits: {"select": "all" or [trait ids], "parts": {trait id:
    [parts]}}, both optional, as a person's (see create_person) -- the
    traits that apply to it (by default every active one), and parts
    replacing a trait's for it alone, a more specific rubric, say. It's
    always "with": judgment parts for those an event was done "for"
    don't apply to it. Active unless given a status. id is assigned.
    Returns the habit and its id as created_id."""
    with track("create_habit"), cached_reads():
        _prefetch_habits()
        try:
            return get_habit_store().create_habit(habit)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc


HabitField = Literal["note", "traits"]
"""Every Habit field update_habit can clear."""


@tool
@writes
def update_habit(habit: Habit, clear_fields: list[HabitField] | None = None) -> ListedHabit:
    """Update a habit by id: its name, action_id (an action's or a
    group's), status (active; archived, set aside; or deleted, shouldn't
    have existed), note or traits (replaced whole; see create_habit).
    Omitted properties keep their value; list one in clear_fields to
    blank it instead. Returns the habit as updated."""
    with track("update_habit"), cached_reads():
        _prefetch_habits()
        try:
            return get_habit_store().update_habit(habit, clear_fields or ())
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

    proposal: ProposalSummary | None = None
    """The open proposal, if there is one (see get_proposal)."""


@tool
def get_compaction_status() -> CompactionStatus:
    """When notes were last compacted into the calendar, the latest note
    compacted, whether that compaction still has judgments to make, and
    the open proposal, if any. Read-only."""
    with track("get_compaction_status"), cached_reads():
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
            judgments_pending=due.compaction_id if due is not None and due.events else None,
            proposal=get_note_compactor().proposal_summary(),
        )


@tool
@writes
def note(noted_time: NotedTime) -> NoteWithId:
    """Record a new time note -- a timestamp, with an optional
    description of what it marks. Returns the note as recorded, with the
    id edit_note/delete_note refer to it by."""
    with track("note"), cached_reads():
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
    with track("get_notes"), cached_reads():
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
    instead). A proposal that included this note is planned again when
    it's confirmed."""
    with track("edit_note"), cached_reads():
        try:
            return get_note_compactor().edit_note(
                note_id, timestamp=timestamp, description=description
            ).with_id()
        except CompactionError as exc:
            raise _rejected("edit_note", exc) from exc


@tool
@writes
def delete_note(note_id: str) -> NotedTime:
    """Delete an uncompacted note (by its id, from get_notes or
    prepare_compaction), returning what it was. Other notes' ids are
    unaffected. Compacted notes can't be deleted. A proposal that included
    this note is planned again when it's confirmed."""
    with track("delete_note"), cached_reads():
        try:
            return get_note_compactor().delete_note(note_id)
        except CompactionError as exc:
            raise _rejected("delete_note", exc) from exc


@tool
@writes
def prepare_compaction() -> CompactionContext:
    """Step 1 of compacting notes into the calendar: proposing what
    happened since the last compaction, for the user to confirm. Returns
    every day from the last compaction up to now (at most a week), notes
    or not -- each uncompacted note with an id and the planned events
    nearest it (`remaining_note_count` says how many are left for another
    round) -- those days' planned events, a `timeline` showing the two
    side by side, day by day, and instructions. Compare the notes to the
    plan, decide which events the notes show happened differently, and
    the facts of each past event -- its actions, where it was, who was
    there, who it was for, and a note on each person there -- then call
    compact_notes with those decisions. Also every action an event can be
    given (`actions`, their groups left out), every person (`people`, the
    user as "self") and every location (`locations`), to settle the facts
    from. `proposal` is the open proposal, if there is one: revise or
    extend it (see the instructions). judgments_pending names the last
    compaction if its judgments aren't all made: make them first
    (prepare_judgments, record_judgments). Read-only."""
    with track("prepare_compaction"), cached_reads():
        return get_note_compactor().prepare()


@tool
@writes
def compact_notes(
    updates: list[CompactionUpdate] | None = None,
    creates: list[CompactionCreate] | None = None,
    cancels: list[EventCancel] | None = None,
    ignore_notes: list[str] | None = None,
    proposal_id: str | None = None,
    revision: int | None = None,
    replies: list[FeedbackReply] | None = None,
    new_actions: list[NewAction] | None = None,
    new_people: list[NewPerson] | None = None,
    new_locations: list[NewLocation] | None = None,
    annotate_notes: list[NoteAnnotation] | None = None,
) -> CompactionResult:
    """Step 2 of compacting notes: propose what happened -- each day's
    events realigned to its notes, and the calendar changes that makes --
    for the user to review and confirm in the app. Nothing on the calendar
    changes, and you can't apply it: only the user's confirmation does.
    Each day is planned on its own, after the day before it, and never
    moves the next day's start; one call covers them all, to now.

    Call with what the notes show happened differently from the plan, as
    update_event takes a batch (see prepare_compaction's instructions):
    `updates` (events that happened, with edges moved to a time or a
    note's, renamed, annotated, their actions and facts set), `creates`
    (something unplanned that happened) and `cancels` (what didn't happen,
    each saying whether it counts against follow-through: the user
    dropped it, or the plan changed). Any past event you don't mention is
    recorded as on schedule. Notes that don't set an event edge are added
    to the event they fall within, except those in `ignore_notes` -- and
    those in `annotate_notes`, added to the event they name (an event's
    id, or a create's key from the open proposal) instead.
    new_actions, new_people and new_locations add what isn't there yet,
    each with a `ref` ("new:ukulele") the updates and creates use in
    place of its id; they're created when it's applied. You get the
    proposal's id and revision, the proposed changes, the additions, and
    a `timeline` of the notes beside the resulting events, each with its
    actions and facts -- in a conversation, show its `text` to the user in
    a code block.

    With a proposal open (prepare_compaction's `proposal`), every call
    revises or extends it: pass its `proposal_id` and the `revision` you
    started from, send all your decisions again (each create with the
    `key` it was given), and answer every open feedback item in
    `replies`. The user's own edits -- of events, and of what notes are
    for -- are laid over your decisions by the server; never send them.

    NOTHING IS MOVED TO MAKE ROOM: every event you update or create must
    end after it starts and must not overlap any other event -- past or
    still to come, the others you change included -- so move, shorten or
    cancel whatever's in the way in the same call. A call that breaks this
    changes nothing, and the error lists every problem and the day as it
    would leave it, to fix them all at once.

    An update that moves a future event reschedules it. Moving the
    end-of-day sleep event moves where the day ends: an earlier bedtime
    needs whatever runs past it shortened or cancelled too, a later one
    leaves the evening free. Its end (the wake-up time) is the border with the next day: a
    note ending it moves the border there. If that day is being compacted
    too, its morning is settled against the night; if not, compaction
    never adjusts the next day -- so move only its start to change only the
    bedtime, and if the wake-up time does change, the plan warns; tell the
    user. Cancelling a night (no sleep) makes its two days one.

    Once the user confirms it and it's applied, make its judgments with
    prepare_judgments and record_judgments: the compaction isn't complete
    until they're all recorded."""
    with track("compact_notes"), cached_reads():
        compactor = get_note_compactor()
        try:
            decisions = [
                *(u.decision() for u in updates or ()),
                *(c.decision() for c in creates or ()),
                *(
                    EventDecision(
                        action="cancel", event_id=c.event_id,
                        counts_against_follow_through=c.counts_against_follow_through,
                    )
                    for c in cancels or ()
                ),
            ]
            return compactor.dry_run(
                decisions, ignore_notes, new_actions, new_people, new_locations,
                proposal_id=proposal_id, revision=revision, replies=replies, annotate_notes=annotate_notes,
            )
        except CompactionError as exc:
            raise _rejected("compact_notes", exc) from exc


@tool
def get_proposal(proposal_id: str | None = None, since_revision: int | None = None) -> Proposal:
    """The open proposal (or the one named) -- its current revision,
    planned again on the calendar as it is now: its window (`window_start`
    to `through`), `state`, every event of its days as it leaves them
    (`events`, each with its id -- or a new event's key -- its times, what
    it was planned as, and who decided it), the notes, the calendar writes
    it makes, its `timeline`, the user's edits and the feedback with any
    replies. With since_revision, `changed_since` lists the events whose
    outcome changed after it. Read-only."""
    with track("get_proposal"), cached_reads():
        try:
            return get_note_compactor().get_proposal(proposal_id, since_revision)
        except CompactionError as exc:
            raise _rejected("get_proposal", exc) from exc


@tool
@writes
def amend_proposal(
    proposal_id: str,
    revision: int,
    updates: list[ProposalUpdate] | None = None,
    creates: list[ProposalCreate] | None = None,
    cancels: list[EventCancel] | None = None,
    as_planned: list[str] | None = None,
    notes: list[NoteEdit] | None = None,
    additions: list[AdditionChoice] | None = None,
) -> Proposal:
    """The user's edits to the open proposal, from the app: `updates`,
    `creates` and `cancels` as update_event takes them (an update's id may
    be a new event's key), each update and create also taking a
    `start_note`/`end_note` that sets that edge. A description given is
    the event's whole description, notes and all -- as the proposal
    shows it, edited: no notes are added to it. `notes`, what notes are for --
    each annotating the event it falls within, or the one named
    (`event_id`), or ignored; `additions`, settling an action, person or
    location the proposal adds (by its ref) now, without confirming the
    rest -- `create` it (with its name, a person's context or a
    location's hint corrected), say it's one that's `existing` (its id),
    or `drop` it from the events; and `as_planned` -- events (or keys)
    whose decisions to clear, notes to leave as Claude had them, and refs
    to leave unsettled. One created stays, whatever becomes of the
    proposal.
    `revision` is the one the user was looking at. They're laid over the current revision as a new one,
    which is returned, with `replaced`: the events whose newer change by
    Claude they overrode. Refused, changing nothing, if one names an
    event or a note that isn't in the proposal, adds a note to an event
    of another day, or the result overlaps, or changes history; nothing
    is moved to make room."""
    with track("amend_proposal"), cached_reads():
        try:
            decisions = [
                *(
                    _proposal_decision(
                        u.event, "keep", start_note=u.start_note, end_note=u.end_note,
                        clear_fields=u.clear_fields or (),
                    )
                    for u in updates or ()
                ),
                *(
                    _proposal_decision(c, "create", start_note=c.start_note, end_note=c.end_note)
                    for c in creates or ()
                ),
                *(
                    EventDecision(
                        action="cancel", event_id=c.event_id,
                        counts_against_follow_through=c.counts_against_follow_through,
                    )
                    for c in cancels or ()
                ),
            ]
            return get_note_compactor().amend(proposal_id, revision, decisions, as_planned, notes, additions)
        except CompactionError as exc:
            raise _rejected("amend_proposal", exc) from exc


@tool
@writes
def add_proposal_note(
    proposal_id: str,
    text: str,
    event_id: str | None = None,
    at: datetime | None = None,
    note_id: str | None = None,
) -> Feedback:
    """A note from the user for Claude on the open proposal, about an
    event (or a new event's key), a time or a time note, if it's about
    one. Claude answers it with a revised proposal -- and may then change
    what the user edited of that event or note; until then, the proposal
    can't be confirmed. Returns it, with its id."""
    with track("add_proposal_note"), cached_reads():
        try:
            return get_note_compactor().add_note(proposal_id, text, event_id, at, note_id)
        except CompactionError as exc:
            raise _rejected("add_proposal_note", exc) from exc


@tool
@writes
def withdraw_proposal_note(feedback_id: str) -> Feedback:
    """Take back an open note for Claude (by its id). One already
    answered can't be."""
    with track("withdraw_proposal_note"), cached_reads():
        try:
            return get_note_compactor().withdraw_note(feedback_id)
        except CompactionError as exc:
            raise _rejected("withdraw_proposal_note", exc) from exc


@tool
@writes
def confirm_proposal(proposal_id: str, revision: int) -> ProposalResult:
    """For the app: the user confirms the open proposal's current
    revision -- the one they reviewed -- as what happened, and it's
    applied. Not for Claude: only the user confirms. Refused while
    feedback is waiting for Claude, or if `revision` isn't the current
    one. If the notes or calendar changed since, it's planned again as a
    new revision to confirm instead (`rechecked`); if it no longer plans
    at all, it's handed to Claude (`needs_claude`)."""
    with track("confirm_proposal"), cached_reads():
        try:
            return get_note_compactor().confirm(proposal_id, revision)
        except CompactionError as exc:
            raise _rejected("confirm_proposal", exc) from exc


@tool
@writes
def finish_proposal(proposal_id: str) -> ProposalResult:
    """Finish applying a confirmed proposal that stopped partway: what was
    done stays done, and the rest is applied. It was confirmed, so this
    needs no new approval. If a change can never be made (its event is
    gone), what's left is proposed again as a new revision for the user
    to confirm (`rebuilt`)."""
    with track("finish_proposal"), cached_reads():
        try:
            return get_note_compactor().finish(proposal_id)
        except CompactionError as exc:
            raise _rejected("finish_proposal", exc) from exc


@tool
def prepare_judgments(compaction_id: str | None = None, redo: bool = False) -> JudgmentsDue:
    """The judgments a compaction (by default the last one applied) calls
    for, to make with record_judgments: each event with facts, with each
    person it was about (the user, "self", and everyone there, for a
    trait's "with" parts; everyone it was done for, for its "for" parts)
    and the judgment parts of the traits that apply to them; then each of
    those parts once -- its rubric and the ratings to choose from -- and
    each person's history (what they did and where) over the days their
    parts look back. Only those not made yet -- or with redo, all of
    them, each event with the judgments already made (`current`), to
    redo one with more context. Read-only."""
    with track("prepare_judgments"), cached_reads():
        compactor = get_note_compactor()
        compactor.prefetch()
        try:
            due = compactor.judgments_due(compaction_id, redo=redo)
        except CompactionError as exc:
            raise _rejected("prepare_judgments", exc) from exc
        if due is None:
            raise ToolError("there's no applied compaction to judge")
        return due


@tool
def prepare_habit_judgments(
    habit_id: str, since: datetime | None = None, redo: bool = False
) -> HabitJudgmentsDue:
    """A backfill of one habit's judgments (by id or name): for a habit
    just made, or given a new rubric, whose settled events -- already
    compacted -- aren't judged for it yet. Only when the user asks for
    one: it's never part of a compaction. Gives a backfill_id, then, as
    prepare_judgments does, each of its events in scope since `since`
    (by default as far back as its judgment parts average over, and a
    week of scores) until where history ends, with the parts to judge
    for it; each part once; and its history. Only those not judged yet --
    or with redo, all of them, each with the judgment already made
    (`current`), to judge again. Make them yourself and record them with
    record_judgments(backfill_id, judgments). Read-only."""
    with track("prepare_habit_judgments"), cached_reads():
        compactor = get_note_compactor()
        compactor.prefetch()
        try:
            return compactor.habit_judgments_due(habit_id, since, redo=redo)
        except CompactionError as exc:
            raise _rejected("prepare_habit_judgments", exc) from exc


@tool
@writes
def record_judgments(compaction_id: str, judgments: list[Judgment]) -> JudgmentsResult:
    """Record judgments of a compaction's events (see prepare_judgments, and
    the `judgments` an applied compaction returns) -- or, given a habit
    backfill's backfill_id as compaction_id, of that habit's events (see
    prepare_habit_judgments): each a request_id, a rating from that
    request's ratings, and one succinct line of reasoning. Make them
    yourself, never asking the user. They're kept on the events, by
    person (or habit), trait and part; a judgment recorded again replaces
    the earlier one. Returns how many were recorded, the ids of any
    requests still to judge, and whether that completes the compaction
    (or the backfill)."""
    with track("record_judgments"), cached_reads():
        compactor = get_note_compactor()
        compactor.prefetch()
        try:
            return compactor.record_judgments(compaction_id, judgments)
        except CompactionError as exc:
            raise _rejected("record_judgments", exc) from exc


@tool
@writes
def abandon_compaction(proposal_id: str) -> CompactionResult:
    """Give up on a proposal (or a compaction from before proposals, by
    its id). Changes it already applied stay applied; its notes stay
    uncompacted (except those of days it had already finished), so a new
    one can be proposed."""
    with track("abandon_compaction"), cached_reads():
        try:
            return get_note_compactor().abandon(proposal_id)
        except CompactionError as exc:
            raise _rejected("abandon_compaction", exc) from exc


def _schedule_time_zone() -> str | None:
    """The calendar's time zone, which schedule hints are in; None if it
    has none."""
    try:
        return get_calendar_client().get_time_zone().key
    except TimeZoneNotSetError:
        return None


@tool
def get_compaction_schedule_hints() -> CompactionScheduleHints:
    """When the user's scheduled routines usually run -- the ones that
    compact notes into a proposal, and answer the notes left on it: each
    a time of day ("07:30", 24-hour, in the calendar's time_zone, given
    too) with an optional label ("Morning compaction"), earliest first.
    They're hints: nothing runs at them. The user's app fetches what a
    routine made a while after each, so it has it to show. Read-only."""
    with track("get_compaction_schedule_hints"), cached_reads():
        try:
            hints = get_compaction_schedule_store().all()
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return CompactionScheduleHints(hints=hints, time_zone=_schedule_time_zone())


@tool
@writes
def set_compaction_schedule_hints(hints: list[ScheduleHint]) -> CompactionScheduleHints:
    """Say when the user's scheduled routines run, replacing the hints
    whole: each a time of day ("HH:MM", 24-hour, in the calendar's time
    zone -- see get_compaction_schedule_hints) and an optional label
    ("Morning compaction"). A scheduled routine that compacts notes into a
    proposal, or answers the notes left on one, should keep these up to
    date, its own time among them, so the user's app fetches what it made
    soon after it runs; the app can't change them. Keep the other hints
    there: get them first, and send them back with yours. Times are
    unique; an empty list clears them. Returns them as saved."""
    with track("set_compaction_schedule_hints"), cached_reads():
        try:
            saved = get_compaction_schedule_store().set_hints(hints)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
        return CompactionScheduleHints(hints=saved, time_zone=_schedule_time_zone())


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
    with track("set_time_zone"), cached_reads():
        try:
            zone = get_calendar_client().set_time_zone(time_zone)
        except ValueError as exc:
            raise ToolError(str(exc)) from exc
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
