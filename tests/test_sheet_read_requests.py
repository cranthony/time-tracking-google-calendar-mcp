"""How many Google Sheets read requests each MCP tool makes.

Google Sheets throttles read requests to 60 a minute per user, and a tool
call that runs out waits for the quota to roll over (see
calendar_clients/google_sheets.py's `_execute`) -- so every read request
counts. These pin how many each tool makes, running the production code
end to end: the MCP tools in server.py, each in its own
`cached_sheet_reads` as they are there, over the objects server.py's get_*
helpers build with config.py's builders, all on the real `SheetsClient` --
with only Google itself faked: the Sheets service by `FakeSheetsService`,
the calendar by `_ProductionCalendar` below (and memory
diagnostics left out). Compaction's tools are pinned the same way in
tests/test_note_compactor.py's `TestSheetReadRequests`.

Every tool here reads the spreadsheet in one request: a tool that reads
more than one tab prefetches them together (server.py's `_prefetch`). If
a count goes up, find a way not to -- usually by prefetching whatever tab
the tool newly reads -- and if one goes down, lower it here.
"""

import contextlib
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

import config
import server
from calendar_clients.google_calendar import Event
from calendar_clients.google_sheets import SheetsClient
from tests.fake_labels import FakeLabelCalendar
from tests.fake_sheets import FakeSheets, FakeSheetsService
from utilities.action_groups import ActionGroup
from utilities.actions import Action
from utilities.facts import Facts
from utilities.habits import Habit
from utilities.locations import Location
from utilities.noted_time_sheet import NotedTime
from utilities.people import Circle, Person

TZ = ZoneInfo("America/New_York")
NOW = datetime.combine(date(2026, 10, 2), time(21), TZ)


class _ProductionCalendar(FakeLabelCalendar):
    """The calendar calls the server makes, with its events in memory --
    the calendar isn't what's being counted."""

    def __init__(self):
        super().__init__()
        self.events: list[Event] = []

    def get_time_zone(self):
        return TZ

    def list_events(self, time_min, time_max):
        return [e for e in self.events if e.end > time_min and e.start < time_max]

    def get_event(self, event_id):
        return next(e for e in self.events if e.id == event_id)

    def update_event(self, event):
        return event

    def create_event(self, event):
        return event


class _Server:
    def __init__(self, monkeypatch):
        self.service = FakeSheetsService(FakeSheets())
        sheets_client = SheetsClient(self.service)
        self.calendar = _ProductionCalendar()
        # Where the builders would load credentials and build the Google
        # API clients.
        monkeypatch.setattr(
            config, "_build_calendar_and_sheets_clients", lambda calendar_id=None: (self.calendar, sheets_client)
        )
        monkeypatch.setattr(server, "build_calendar_client", lambda: self.calendar)
        # Memory diagnostics never touch Sheets, and take seconds.
        monkeypatch.setattr(server, "track", lambda label: contextlib.nullcontext())
        for cached in (
            "_calendar_client", "_noted_time_sheet", "_compaction_journal",
            "_note_compactor", "_recurrences", "_actions", "_people", "_locations", "_traits", "_cancellations",
            "_habits",
        ):
            monkeypatch.setattr(server, cached, None)
        self._fill()

    def _fill(self) -> None:
        """An action in a group, a person in a circle, a location, an event
        with an action and facts, and a note, so every tab the tools read
        has something in it."""
        self.outdoors = server.create_action_group(ActionGroup(name="Outdoors")).created_id
        self.walk = server.create_action(Action(name="Walk", group_id=self.outdoors, status="active")).created_id
        self.family = server.create_circle(Circle(name="Family")).created_id
        self.sam = server.create_person(Person(name="Sam", circles=[self.family])).created_id
        self.home = server.create_location(Location(name="Home")).created_id
        self.outside = server.create_habit(Habit(name="Outside", action_id=self.outdoors)).created_id
        evening = (NOW - timedelta(days=1)).replace(hour=18, minute=0)
        self.calendar.events.append(
            Event(
                id="walked", summary="Walk", start=evening, end=evening + timedelta(hours=1), action_ids=[self.walk],
                facts=Facts(location_id=self.home, with_ids=[self.sam]),
            )
        )
        # Tonight, still to come: an evening event, then the night's sleep
        # that ends the day -- what reallocation makes room in.
        tonight = NOW.replace(minute=0, second=0, microsecond=0)
        self.calendar.events += [
            Event(id="evening", summary="Evening", start=tonight, end=tonight + timedelta(hours=2), priority=2),
            Event(
                id="sleep", summary="Sleep", start=tonight + timedelta(hours=2), end=tonight + timedelta(hours=10),
                priority=0, is_end_of_day_sleep=True,
            ),
        ]
        server.note(NotedTime(timestamp=NOW - timedelta(hours=2), description="walked"))
        # Finding a tab is a one-time read, whichever tool's first.
        server.get_compaction_journal()
        server.get_note_compactor()

    def reads(self, tool) -> int:
        """The read requests the one tool call `tool()` makes."""
        before = len(self.service.read_requests)
        tool()
        return len(self.service.read_requests) - before


@pytest.fixture
def tools(monkeypatch):
    return _Server(monkeypatch)


def test_every_tool_reads_the_spreadsheet_in_one_request(tools):
    note_id = server.get_notes()[0].id
    later_evening = replace(server.get_event("evening"), start=NOW + timedelta(minutes=15))
    with_facts = replace(server.get_event("walked"), facts=Facts(location_id=tools.home, notes={"self": "nice"}))
    calls = {
        # Actions and action groups, prefetched together.
        "get_actions": lambda: server.get_actions(),
        "get_action": lambda: server.get_action("walk"),
        "create_action": lambda: server.create_action(Action(name="Run")),
        "update_action": lambda: server.update_action(Action(id=tools.walk, note="outside")),
        "get_action_groups": lambda: server.get_action_groups(),
        "get_action_group": lambda: server.get_action_group("outdoors"),
        "create_action_group": lambda: server.create_action_group(ActionGroup(name="Indoors")),
        "update_action_group": lambda: server.update_action_group(ActionGroup(id=tools.outdoors, priority=1)),
        # People and circles, prefetched together.
        "get_people": lambda: server.get_people(),
        "get_person": lambda: server.get_person("sam"),
        "create_person": lambda: server.create_person(Person(name="Alex")),
        "update_person": lambda: server.update_person(Person(id=tools.sam, what_matters="tea")),
        "get_circles": lambda: server.get_circles(),
        "get_circle": lambda: server.get_circle("family"),
        "create_circle": lambda: server.create_circle(Circle(name="Friends")),
        "update_circle": lambda: server.update_circle(Circle(id=tools.family, note="n")),
        # Locations only.
        "get_locations": lambda: server.get_locations(),
        "get_location": lambda: server.get_location("home"),
        "create_location": lambda: server.create_location(Location(name="Studio")),
        "update_location": lambda: server.update_location(Location(id=tools.home, hint="the apartment")),
        # Habits, with the actions and traits they're checked against.
        "get_habits": lambda: server.get_habits(),
        "get_habit": lambda: server.get_habit("outside"),
        "create_habit": lambda: server.create_habit(Habit(name="Walks", action_id=tools.walk)),
        "update_habit": lambda: server.update_habit(Habit(id=tools.outside, traits={"select": "all"})),
        # Events: the actions', people's and locations' tabs together.
        "list_events": lambda: server.list_events(NOW - timedelta(days=2), NOW),
        "get_event": lambda: server.get_event("walked"),
        "update_event (facts)": lambda: server.update_event(updates=[server.EventUpdate(event=with_facts)]),
        "update_event": lambda: server.update_event(updates=[server.EventUpdate(event=later_evening)]),
        "create_event": lambda: server.create_event([
            server.PublicEvent(summary="Stroll", start=NOW + timedelta(days=3), end=NOW + timedelta(days=3, hours=1))
        ]),
        "delete_event": lambda: server.delete_event(
            [server.EventCancel(event_id="walked", counts_against_follow_through=False)]
        ),
        # Notes, and the journal (the last compaction; any open one).
        "get_compaction_status": lambda: server.get_compaction_status(),
        "edit_note": lambda: server.edit_note(note_id, description="walked far"),
        "delete_note": lambda: server.delete_note(note_id),
        # Notes only.
        "get_notes": lambda: server.get_notes(),
        "note": lambda: server.note(NotedTime(timestamp=NOW, description="washed up")),
        # Deleting tabs' rows: last, since they remove what others use.
        "delete_circle": lambda: server.delete_circle(tools.family),
        "delete_location": lambda: server.delete_location(tools.home),
    }

    counts = {name: tools.reads(call) for name, call in calls.items()}

    assert counts == {name: 1 for name in calls}


def test_an_event_tool_reads_every_tab_it_needs_in_one_request(tools):
    before = len(tools.service.read_requests)

    server.list_events(NOW - timedelta(days=2), NOW)

    (request,) = tools.service.read_requests[before:]
    assert request[0] is None  # across tabs
    titles = tools.service.sheets.titles
    read = {titles[int(part.split(":")[0])] for part in request[1].split(" + ")}
    assert read == {"Actions", "Action Groups", "People", "Circles", "Locations"}
