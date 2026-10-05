"""How many Google Sheets read requests each MCP tool makes.

Google Sheets throttles read requests to 60 a minute per user, and a tool
call that runs out waits for the quota to roll over (see
calendar_clients/google_sheets.py's `_execute`) -- so every read request
counts. These pin how many each tool makes, running the production code
end to end: the MCP tools in server.py, each in its own
`cached_sheet_reads` as they are there, over the objects server.py's get_*
helpers build with config.py's builders, all on the real `SheetsClient` --
with only Google itself faked: the Sheets service by `FakeSheetsService`,
the calendar by tests/test_goal_health.py's `FakeCalendar` (and memory
diagnostics left out). Compaction's tools are pinned the same way in
tests/test_note_compactor.py's `TestSheetReadRequests`.

Every tool here reads the spreadsheet in one request: a tool that reads
more than one tab prefetches them together (server.py's `_prefetch`). If
a count goes up, find a way not to -- usually by prefetching whatever tab
the tool newly reads -- and if one goes down, lower it here.
"""

import contextlib
from dataclasses import replace
from datetime import timedelta

import pytest

import config
import server
from calendar_clients.google_calendar import Event
from calendar_clients.google_sheets import SheetsClient
from tests.fake_sheets import FakeSheets, FakeSheetsService
from tests.test_goal_health import NOW, TODAY, YESTERDAY, FakeCalendar, _event
from utilities.actions import Action
from utilities.goal_health import Assessment
from utilities.goal_sheet import Goal
from utilities.goals import OVERALL_ID
from utilities.noted_time_sheet import NotedTime


class _ProductionCalendar(FakeCalendar):
    """tests/test_goal_health.py's calendar, plus the event calls the
    server's goal-aware calendar makes -- the calendar isn't what's being
    counted."""

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
            "_calendar_client", "_reallocating_calendar", "_goals", "_goal_health", "_reflections",
            "_noted_time_sheet", "_compaction_journal", "_note_compactor", "_recurrences", "_actions",
        ):
            monkeypatch.setattr(server, cached, None)
        # The seams: the clocks, so days fall on the test calendar's.
        server.get_goal_store()._today = lambda: TODAY
        server.get_goal_health()._now = lambda: NOW
        self._fill()

    def _fill(self) -> None:
        """Goals (one measured), an event serving one, a note and an
        assessment, so every tab the tools read has something in it."""
        listed = server.create_goal(Goal(name="Cooking", measure={"kind": "subjective", "prompt": "How was it?"}))
        self.cooking = cooking = listed.changed[0]
        server.create_goal(Goal(name="Reading"))
        self.walk = server.create_action(Action(name="Walk")).created_id
        evening = (NOW - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
        self.calendar.events.append(
            _event(evening.isoformat()[:19], (evening + timedelta(hours=1)).isoformat()[:19], goal_ids=[cooking.id])
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
        server.note(NotedTime(timestamp=NOW - timedelta(hours=2), description="cooked"))
        server.record_assessments([self.assessment(80)])
        # Finding its tab is a one-time read, whichever tool's first.
        server.get_compaction_journal()

    def assessment(self, rating: int) -> Assessment:
        """A rating of the measured goal for yesterday."""
        return Assessment(goal_id=self.cooking.id, day=YESTERDAY, rating=rating, method="subjective")

    def reads(self, tool) -> int:
        """The read requests the one tool call `tool()` makes."""
        before = len(self.service.read_requests)
        tool()
        return len(self.service.read_requests) - before


@pytest.fixture
def tools(monkeypatch):
    return _Server(monkeypatch)


def test_every_tool_reads_the_spreadsheet_in_one_request(tools):
    # Cooking's siblings (the overall goal stays first, so isn't among them).
    siblings = [
        g.id for g in server.get_goal_store().tree().goals
        if g.parent_id == tools.cooking.parent_id and g.id != OVERALL_ID
    ]
    event_id = tools.calendar.events[0].id
    note_id = server.get_notes()[0].id
    later_evening = replace(server.get_event("evening"), start=NOW + timedelta(minutes=15))
    calls = {
        # Goals, and the journal for the last compaction's time (how far
        # get_goals' goal time goes).
        "get_goals": lambda: server.get_goals(),
        # Goals only.
        "create_goal": lambda: server.create_goal(Goal(name="Writing")),
        "update_goal": lambda: server.update_goal(Goal(id=tools.cooking.id, note="dinners")),
        "reorder_goals": lambda: server.reorder_goals(siblings[::-1]),
        "sync_goals_from_sheet": lambda: server.sync_goals_from_sheet(),
        "rebuild_goal_health_cache": lambda: server.rebuild_goal_health_cache(),
        "measure_goals": lambda: server.measure_goals(YESTERDAY),
        "record_assessments": lambda: server.record_assessments([tools.assessment(70)]),
        "get_goal_history": lambda: server.get_goal_history([tools.cooking.id]),
        "record_reflection (dry run)": lambda: server.record_reflection(YESTERDAY, [tools.assessment(75)]),
        "record_reflection (apply)": lambda: server.record_reflection(
            YESTERDAY, [tools.assessment(75)], dry_run=False
        ),
        # Actions only.
        "get_actions": lambda: server.get_actions(),
        "get_action": lambda: server.get_action("walk"),
        "create_action": lambda: server.create_action(Action(name="Run")),
        "update_action": lambda: server.update_action(Action(id=tools.walk, status="active")),
        "list_events": lambda: server.list_events(NOW - timedelta(days=2), NOW),
        "get_event": lambda: server.get_event(event_id),
        "delete_event": lambda: server.delete_event(event_id),
        "create_event": lambda: server.create_event(
            server.PublicEvent(summary="Walk", start=NOW + timedelta(minutes=30), end=NOW + timedelta(hours=1))
        ),
        "update_event": lambda: server.update_event(later_evening),
        # Goals, and the notes the day held.
        "prepare_reflection": lambda: server.prepare_reflection(YESTERDAY),
        # Notes, and the journal (the last compaction; any open one).
        "get_compaction_status": lambda: server.get_compaction_status(),
        "edit_note": lambda: server.edit_note(note_id, description="cooked dinner"),
        "delete_note": lambda: server.delete_note(note_id),
        # Notes only.
        "get_notes": lambda: server.get_notes(),
        "note": lambda: server.note(NotedTime(timestamp=NOW, description="washed up")),
    }

    counts = {name: tools.reads(call) for name, call in calls.items()}

    assert counts == {name: 1 for name in calls}


def test_a_tool_that_reads_two_tabs_makes_one_request_for_both(tools):
    before = len(tools.service.read_requests)

    server.get_goals()

    (request,) = tools.service.read_requests[before:]
    assert request[0] is None  # across tabs
    titles = tools.service.sheets.titles
    read = {titles[int(part.split(":")[0])] for part in request[1].split(" + ")}
    assert read == {"Goals", "Compactions"}
