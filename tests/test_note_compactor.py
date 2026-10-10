import contextlib
from dataclasses import replace
from datetime import timedelta, timezone
from unittest.mock import MagicMock

import pytest

import config
import server
from googleapiclient.errors import HttpError

from calendar_clients.google_calendar import Event, EventLabel
from calendar_clients.google_sheets import SheetsClient
from tests.event_time_helpers import event_at, time_at
from tests.fake_sheets import FakeSheets, FakeSheetsService
from utilities import calendar_metadata_sheet
from utilities.compaction_journal import (
    ABANDONED,
    APPLYING,
    FAILED,
    PLANNED,
    PROPOSED,
    STAMPED,
    SUPERSEDED,
    CompactionJournal,
)
from utilities import note_compactor
from utilities.note_compaction import (
    CompactionChange,
    CompactionError,
    CompactionPlan,
    EventDecision,
    EventState,
    NoteAnnotation,
    facts_from_dict,
)
from tests.fake_labels import FakeLabelCalendar
from utilities.actions import Action, Actions
from utilities.compaction_additions import NewAction, NewLocation, NewPerson
from utilities.compaction_proposals import AdditionChoice, AdditionSettled, FeedbackReply, NoteEdit
from utilities.cancellations import Cancellations
from utilities.facts import Facts
from utilities.habits import Habit, Habits
from utilities.judgments import Judging, Judgment
from utilities.traits import Trait, Traits
from utilities.locations import Location, Locations
from utilities.note_compactor import NoteCompactor
from utilities.noted_time_sheet import NotedTime, NotedTimeSheet
from utilities.people import People, Person

_NOTES_TAB = 1
_JOURNAL_TAB = 2


def _day():
    return [
        event_at("09:00-10:00", id="e1", summary="Email", priority=2),
        event_at("10:00-11:00", id="e2", summary="Report", priority=2),
        event_at("12:00-13:00", id="e3", summary="Lunch", priority=1),
        event_at("20:00-07:00+1", id="s1", summary="Sleep", priority=0, is_end_of_day_sleep=True),
    ]


class FakeCalendar:
    """Just the `list_events` the compactor reads a day through -- handing
    back fresh copies each call, like the real label-aware view -- and
    the reads and writes judging makes."""

    def __init__(self, events):
        self.events = events

    def get_event(self, event_id):
        return replace(next(e for e in self.events if e.id == event_id))

    def update_event(self, event):
        index = next(i for i, e in enumerate(self.events) if e.id == event.id)
        self.events[index] = replace(self.events[index], judgments=event.judgments)
        return event

    def list_events(self, time_min, time_max):
        # Like the Calendar API: everything overlapping the range.
        return sorted(
            (replace(e) for e in self.events if e.end > time_min and e.start < time_max),
            key=lambda e: e.start,
        )


class Setup:
    def __init__(
        self, notes, events=None, now="11:30", actions=None, people=None, locations=None, traits=None
    ):
        self.sheets = FakeSheets()
        self.sheets.write_rows_in_sheet(
            "s", _NOTES_TAB, "A1:C1", [["timestamp", "description", "compaction_id"]]
        )
        # The notes and journal tabs are made by hand; the stores' own
        # tabs come after them.
        self.sheets.titles.update({_NOTES_TAB: "Noted Times", _JOURNAL_TAB: "Compactions"})
        self.labels = FakeLabelCalendar()
        self.actions = Actions.ensure(self.labels, self.sheets, "s")
        self.people = People.ensure(self.sheets, "s")
        self.locations = Locations.ensure(self.sheets, "s")
        for store, rows in ((self.actions, actions), (self.people, people), (self.locations, locations)):
            if rows:
                store._sheet.write(rows)
        self.notes = NotedTimeSheet(self.sheets, "s", _NOTES_TAB)
        for at, description in notes:
            self.append_note(at, description)
        self.journal = CompactionJournal(self.sheets, "s", _JOURNAL_TAB)
        self.calendar = FakeCalendar(events if events is not None else _day())
        self.client = MagicMock()
        self.now = now
        self.judging = None
        self.cancellations = None
        if traits is not None:
            trait_store = Traits.ensure(self.sheets, "s")
            trait_store._write(traits)
            self.judging = Judging(
                client=self.calendar, actions=self.actions, people=self.people, locations=self.locations,
                traits=trait_store,
            )
            self.cancellations = Cancellations.ensure(self.sheets, "s", self.people, trait_store, self.actions)
        self.compactor = NoteCompactor(
            calendar=self.calendar,
            client=self.client,
            notes=self.notes,
            journal=self.journal,
            clock=lambda: time_at(self.now),
            actions=self.actions,
            people=self.people,
            locations=self.locations,
            judging=self.judging,
            cancellations=self.cancellations,
        )

    def append_note(self, at, description=None):
        self.notes.append(NotedTime(timestamp=time_at(at), description=description))

    def note_id(self, row):
        """The id of the note in sheet row `row` (see SheetNote.id)."""
        return next(n.id for n in self.notes.read_with_rows(include_compacted=True) if n.row == row)

    def email_then_report(self):
        """The 09:05 note started the email; the 10:20 note ended it and
        started the report, which then ran to its planned end."""
        return [
            EventDecision(action="keep", event_id="e1", start_note=self.note_id(2), end_note=self.note_id(3)),
            EventDecision(action="keep", event_id="e2", start_note=self.note_id(3)),
        ]

    def coffee_instead_of_email(self):
        """Coffee from the 09:00 note to the 09:30 one, then the email."""
        return [
            EventDecision(
                action="create", summary="Coffee", start_note=self.note_id(2), end_note=self.note_id(3)
            ),
            EventDecision(action="keep", event_id="e1", start_note=self.note_id(3)),
        ]


def _standard():
    return Setup([("09:05", "email"), ("10:20", "report")])


class TestPrepare:
    def test_offers_the_days_notes_with_candidates_and_the_days_events(self):
        setup = _standard()

        context = setup.compactor.prepare()

        assert [n.id for n in context.notes] == [setup.note_id(2), setup.note_id(3)]
        assert context.notes[0].description == "email"
        # 09:05 is inside Email, adjacent to Report (starts 10:00, within
        # the hour window) -- nearest first.
        assert context.notes[0].candidates[:2] == ["e1", "e2"]
        assert [e.id for e in context.events] == ["e1", "e2", "e3", "s1"]
        assert context.now == time_at("11:30")
        assert context.day_end == time_at("07:00+1")
        assert context.remaining_note_count == 0
        assert context.open_compaction is None
        assert "SILENCE MEANS ON SCHEDULE" in context.instructions
        assert "READ EACH NOTE'S TENSE" in context.instructions

    def test_the_timeline_shows_a_future_event_only_when_a_note_is_near_it(self):
        # "starting lunch" at 11:20 may start the lunch planned for noon.
        context = Setup([("11:20", "starting lunch")]).compactor.prepare()

        # (The day starts at the note, the first of it.) Sleep is still
        # ahead and nowhere near a note: offered, but not shown.
        assert [e.id for e in context.events] == ["e3", "s1"]
        assert context.notes[0].candidates == ["e3"]
        assert [e.event_id for e in context.timeline.events] == ["e3"]

    def test_offers_the_notes_beside_the_planned_events_as_a_timeline(self):
        context = _standard().compactor.prepare()

        assert [n.text for n in context.timeline.notes] == ["email", "report"]
        # Lunch (12:00) and Sleep are still ahead, and no note is near them.
        assert [e.event_id for e in context.timeline.events] == ["e1", "e2"]
        assert {e.status for e in context.timeline.events} == {"planned"}
        assert "● email" in context.timeline.text
        assert "┌ Email" in context.timeline.text

    def test_with_no_uncompacted_notes_there_is_nothing_to_offer(self):
        setup = Setup([])

        context = setup.compactor.prepare()

        assert context.notes == [] and context.events == []

    def test_offers_every_day_of_notes_up_to_now(self):
        setup = _standard()
        setup.append_note("08:00+1", "next day")
        setup.now = "09:00+1"

        context = setup.compactor.prepare()

        assert [n.id for n in context.notes] == [setup.note_id(2), setup.note_id(3), setup.note_id(4)]
        assert context.remaining_note_count == 0
        assert [(d.label, d.note_ids) for d in context.days] == [
            ("Thu 01 Jan", [setup.note_id(2), setup.note_id(3)]),
            ("Fri 02 Jan", [setup.note_id(4)]),
        ]

    def test_a_single_day_lists_no_days(self):
        assert _standard().compactor.prepare().days is None

    def test_leaves_notes_written_after_now_for_later(self):
        setup = _standard()
        setup.append_note("11:45", "not yet")

        context = setup.compactor.prepare()

        assert [n.id for n in context.notes] == [setup.note_id(2), setup.note_id(3)]
        assert context.remaining_note_count == 1

    def test_the_days_run_on_to_now_each_ending_with_its_night(self):
        setup = _standard()
        setup.now = "12:00+1"

        context = setup.compactor.prepare()

        assert [(d.compaction_window_start, d.day_end) for d in context.days] == [
            (time_at("09:05"), time_at("07:00+1")),
            (time_at("07:00+1"), time_at("07:00+1") + timedelta(days=1)),
        ]
        assert context.now == time_at("12:00+1")

    def test_skips_compacted_notes(self):
        setup = _standard()
        setup.notes.mark_compacted([setup.note_id(2)], "old")

        assert [n.id for n in setup.compactor.prepare().notes] == [setup.note_id(3)]

    def test_reports_an_open_compaction(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())
        setup.journal.set_status(setup.journal.load(planned.compaction_id), APPLYING)

        assert setup.compactor.prepare().open_compaction == planned.compaction_id


class TestTheCompactionWindow:
    """The notes are on the day after `DAY`; the night before it is
    `DAY`'s evening."""

    def _events(self):
        return [
            event_at("19:00-20:00", id="r0", summary="Reading", priority=2),
            event_at("20:00-07:00+1", id="s0", summary="Sleep", priority=0, is_end_of_day_sleep=True),
            event_at("07:00+1-08:00+1", id="gr", summary="Getting Ready", priority=2),
            event_at("09:00+1-10:00+1", id="w1", summary="Work", priority=2),
            event_at("10:00+1-11:00+1", id="w2", summary="Email", priority=2),
            Event(
                id="s1",
                summary="Sleep",
                start=time_at("22:00+1"),
                end=time_at("07:00+1") + timedelta(days=1),
                priority=0,
                is_end_of_day_sleep=True,
            ),
        ]

    def _setup(self, note_at="09:05+1"):
        return Setup([(note_at, "note")], events=self._events(), now="11:30+1")

    def _stamp_a_compaction_at(self, setup, now):
        """Record a finished compaction whose `now` was `now`, as if the
        previous round had been compacted then."""
        setup.journal.start(
            "prev", now=time_at(now), note_ids=[], decisions=[], plan=CompactionPlan(changes=[])
        )
        setup.journal.set_status(setup.journal.load("prev"), STAMPED)

    def test_with_no_earlier_compaction_it_starts_when_the_night_before_ended(self):
        context = self._setup().compactor.prepare()

        assert context.compaction_window_start == time_at("07:00+1")
        # The night's sleep ended right as the window starts, so it's
        # offered too -- it may need stretching ("slept in").
        assert [e.id for e in context.events] == ["s0", "gr", "w1", "w2", "s1"]
        assert context.day_end == time_at("07:00+1") + timedelta(days=1)

    def test_a_compaction_before_the_night_settles_the_rest_of_its_day_first(self):
        # Compacted at 19:45, before bed: reading ran on past it, and the
        # night hadn't begun -- so they're settled first, with no notes of
        # their own, before the morning the note is in.
        setup = self._setup()
        self._stamp_a_compaction_at(setup, "19:45")

        context = setup.compactor.prepare()

        assert context.compaction_window_start == time_at("19:45")
        assert [(d.compaction_window_start, d.note_ids) for d in context.days] == [
            (time_at("19:45"), []),
            (time_at("07:00+1"), [setup.note_id(2)]),
        ]
        assert [e.id for e in context.events] == ["r0", "s0", "gr", "w1", "w2", "s1"]

    def test_the_days_between_the_last_compaction_and_the_notes_are_proposed_too(self):
        # Notes two days on: the days in between, with no notes, are still
        # confirmed -- as planned, unless the user says otherwise.
        setup = self._setup()
        self._stamp_a_compaction_at(setup, "19:45")
        later = time_at("09:05+1") + timedelta(days=2)
        setup.notes.edit(setup.note_id(2), timestamp=later)
        setup.compactor._clock = lambda: later + timedelta(hours=1)

        context = setup.compactor.prepare()

        assert context.compaction_window_start == time_at("19:45")
        assert [d.note_ids for d in context.days][-1] == [setup.note_id(2)]
        assert all(d.note_ids == [] for d in context.days[:-1])

    def test_a_later_compaction_the_same_day_starts_the_window_there(self):
        # Compacted at 10:05, just after work ended -- so work is offered
        # too, to be stretched if it ran late, but nothing before it is.
        setup = self._setup(note_at="10:20+1")
        self._stamp_a_compaction_at(setup, "10:05+1")

        context = setup.compactor.prepare()

        assert context.compaction_window_start == time_at("10:05+1")
        assert [e.id for e in context.events] == ["w1", "w2", "s1"]

    def test_the_event_just_before_the_window_can_be_stretched(self):
        # Compacted as work ended: the email after it hadn't started yet.
        setup = self._setup(note_at="10:20+1")
        self._stamp_a_compaction_at(setup, "10:00+1")

        result = setup.compactor.dry_run(
            [
                EventDecision(action="keep", event_id="w1", end_note=setup.note_id(2)),
                EventDecision(action="keep", event_id="w2", start_note=setup.note_id(2)),
            ]
        )

        work = next(c for c in result.changes if c.event_id == "w1")
        assert work.after.end == time_at("10:20+1")

    def test_an_event_that_ended_longer_before_the_window_is_not_offered(self):
        setup = self._setup(note_at="10:40+1")
        self._stamp_a_compaction_at(setup, "10:30+1")

        context = setup.compactor.prepare()

        assert [e.id for e in context.events] == ["w2", "s1"]

    def _compacted_note(self, setup, at, description):
        setup.append_note(at, description)
        last = max(n.row for n in setup.notes.read_with_rows(include_compacted=True))
        setup.notes.mark_compacted([setup.note_id(last)], "prev")

    def test_the_last_compacted_note_is_offered_as_context(self):
        setup = self._setup(note_at="10:20+1")
        self._compacted_note(setup, "09:30+1", "earlier")
        self._compacted_note(setup, "09:55+1", "starting email")
        self._stamp_a_compaction_at(setup, "10:05+1")

        context = setup.compactor.prepare()

        assert context.previous_note.description == "starting email"
        assert context.previous_note.timestamp == time_at("09:55+1")
        # Context only: it's not one of this round's notes.
        assert [n.id for n in context.notes] == [setup.note_id(2)]
        assert "✓ starting email (compacted)" in context.timeline.text

    def test_the_last_compacted_note_is_offered_however_long_before_the_window(self):
        setup = self._setup(note_at="10:20+1")
        self._compacted_note(setup, "07:30+1", "starting email")
        self._stamp_a_compaction_at(setup, "10:05+1")

        context = setup.compactor.prepare()

        assert context.previous_note.description == "starting email"
        assert context.previous_note.timestamp == time_at("07:30+1")

    def test_both_timelines_show_the_latest_compacted_note_and_the_last_compaction(self):
        # However long before the window the note was written.
        setup = self._setup(note_at="10:20+1")
        self._compacted_note(setup, "09:45+1", "starting email")
        self._stamp_a_compaction_at(setup, "10:05+1")

        before = setup.compactor.prepare().timeline
        after = setup.compactor.dry_run([]).timeline

        for timeline in (before, after):
            assert timeline.last_compaction == time_at("10:05+1")
            assert "✓ starting email (compacted)" in timeline.text
            assert "10:05 ┄┄ last compaction ┄" in timeline.text

    def test_a_later_round_reads_its_notes_and_the_previous_note_in_one_request(self):
        # Google Sheets caps read requests per minute, so the notes tab
        # costs a later round's prepare just its header and its rows, in
        # one request (see TestSheetReadRequests), with the previous note
        # found among them.
        setup = Setup([("09:05+1", "email"), ("09:58+1", "done with work")], events=self._events(), now="10:05+1")
        setup.compactor.commit(setup.compactor.dry_run([]).compaction_id)
        setup.append_note("10:20+1", "report")
        setup.now = "10:30+1"
        reads = []
        read = setup.sheets.read_rows_in_sheet

        def counting(spreadsheet_id, sheet_id, rng):
            if sheet_id == _NOTES_TAB:
                reads.append(rng)
            return read(spreadsheet_id, sheet_id, rng)

        setup.sheets.read_rows_in_sheet = counting

        context = setup.compactor.prepare()

        assert context.previous_note.description == "done with work"
        assert reads == ["A1:C1", "A2:C"]

    def test_a_note_written_before_the_planned_wake_up_starts_the_day_there(self):
        context = self._setup(note_at="06:40+1").compactor.prepare()

        assert context.compaction_window_start == time_at("06:40+1")
        # The night's sleep runs into the day, so it's offered (to be
        # shortened), but it isn't the day's own end.
        assert [e.id for e in context.events] == ["s0", "gr", "w1", "w2", "s1"]
        assert context.day_end == time_at("07:00+1") + timedelta(days=1)


class TestDryRun:
    def test_plans_and_journals_without_touching_the_calendar_or_the_notes(self):
        setup = _standard()

        result = setup.compactor.dry_run(setup.email_then_report())

        assert result.status == "proposed"
        assert (result.revision, result.compaction_id) == (1, f"{result.proposal_id}r1")
        assert {c.event_id for c in result.changes} == {"e1", "e2"}
        assert setup.journal.load(result.compaction_id).status == PROPOSED
        setup.client.update_event.assert_not_called()
        setup.client.create_event.assert_not_called()
        assert [n.id for n in setup.notes.read_with_rows()] == [setup.note_id(2), setup.note_id(3)]

    def test_tells_the_model_only_the_user_confirms_it(self):
        setup = _standard()

        context = setup.compactor.prepare()
        result = setup.compactor.dry_run(setup.email_then_report())

        for text in (context.instructions, result.message):
            assert "Nothing is applied until the user confirms it, in the app" in text
            assert "confirm_proposal isn't yours to call" in text

    def test_returns_the_resulting_timeline_to_show_the_user(self):
        setup = _standard()

        result = setup.compactor.dry_run(setup.email_then_report())

        email = next(e for e in result.timeline.events if e.event_id == "e1")
        assert email.status == "adjusted"
        assert (email.start_note, email.end_note) == (setup.note_id(2), setup.note_id(3))
        assert "timeline" in result.message
        assert "└ Email ends · +20m (was 10:00)" in result.timeline.text

    def test_with_no_decisions_every_past_event_is_recorded_on_schedule(self):
        # Where they were planned, with their notes added -- nothing more:
        # the compaction's own time says they're settled.
        setup = _standard()

        result = setup.compactor.dry_run([])

        by_event = {c.event_id: c for c in result.changes}
        assert set(by_event) == {"e1", "e2"}
        for change in by_event.values():
            assert replace(change.after, description=change.before.description) == change.before

    def test_journals_the_decisions_and_ignored_notes(self):
        setup = _standard()

        result = setup.compactor.dry_run(setup.email_then_report(), ignore_notes=[setup.note_id(3)])

        journal = setup.journal.load(result.compaction_id)
        assert journal.decisions == setup.email_then_report()
        assert journal.ignore_notes == [setup.note_id(3)]

    def test_invalid_decisions_raise_with_what_to_fix(self):
        setup = _standard()

        with pytest.raises(CompactionError, match="isn't one of this day's events"):
            setup.compactor.dry_run([EventDecision(action="keep", event_id="nope")])

    def test_overlapping_past_events_are_rejected_for_the_model_to_resolve(self):
        setup = _standard()

        with pytest.raises(CompactionError, match="'Email'.*overlaps 'Report'"):
            setup.compactor.dry_run(
                [EventDecision(action="keep", event_id="e1", end_note=setup.note_id(3))]
            )

        assert setup.journal.compactions_with_status(PLANNED) == []

    def test_nothing_to_compact_when_there_are_no_notes(self):
        assert Setup([]).compactor.dry_run([]).status == "nothing_to_compact"

    def test_refuses_to_start_while_another_compaction_is_open(self):
        setup = _standard()
        first = setup.compactor.dry_run(setup.email_then_report())
        setup.journal.set_status(setup.journal.load(first.compaction_id), APPLYING)

        with pytest.raises(CompactionError, match=f"compaction {first.compaction_id} is applying"):
            setup.compactor.dry_run(setup.email_then_report())

    def test_with_a_proposal_open_a_new_one_is_refused_but_it_can_be_revised(self):
        setup = _standard()
        first = setup.compactor.dry_run(setup.email_then_report())

        with pytest.raises(CompactionError, match=f"proposal {first.proposal_id} is open"):
            setup.compactor.dry_run(setup.email_then_report())
        second = setup.compactor.dry_run(
            setup.email_then_report(), proposal_id=first.proposal_id, revision=1
        )

        assert (second.status, second.proposal_id, second.revision) == ("proposed", first.proposal_id, 2)
        assert setup.journal.load(first.compaction_id).status == SUPERSEDED
        assert setup.journal.load(second.compaction_id).status == PROPOSED

    def test_a_plan_from_before_proposals_is_replaced_by_a_new_proposal(self):
        setup = _standard()
        setup.journal.start("old", now=time_at("11:00"), note_ids=[], decisions=[], plan=CompactionPlan(changes=[]))

        result = setup.compactor.dry_run(setup.email_then_report())

        assert setup.journal.load("old").status == ABANDONED
        assert "Replaced 1 unapplied plan" in result.message

    def test_a_superseded_revision_can_no_longer_be_committed(self):
        setup = _standard()
        first = setup.compactor.dry_run(setup.email_then_report())
        setup.compactor.dry_run(setup.email_then_report(), proposal_id=first.proposal_id, revision=1)

        with pytest.raises(CompactionError, match="superseded"):
            setup.compactor.commit(first.compaction_id)

        setup.client.update_event.assert_not_called()

    def test_a_rejected_plan_can_be_redone_with_corrected_decisions(self):
        setup = _standard()
        first = setup.compactor.dry_run(setup.email_then_report())

        # The user says the report never happened -- redo it without
        # touching the calendar or calling prepare again.
        revised = setup.compactor.dry_run(
            [
                EventDecision(action="keep", event_id="e1", start_note=setup.note_id(2), end_note=setup.note_id(3)),
                EventDecision(action="cancel", event_id="e2"),
            ],
            proposal_id=first.proposal_id,
            revision=1,
        )

        by_event = {c.event_id: c for c in revised.changes}
        assert by_event["e1"].after.end == time_at("10:20")
        assert by_event["e2"].action == "cancel"
        setup.client.update_event.assert_not_called()

    def test_a_plan_that_fails_validation_leaves_the_earlier_plan_alone(self):
        setup = _standard()
        first = setup.compactor.dry_run(setup.email_then_report())

        with pytest.raises(CompactionError):
            setup.compactor.dry_run(
                [EventDecision(action="keep", event_id="nope")], proposal_id=first.proposal_id, revision=1
            )

        assert setup.journal.load(first.compaction_id).status == PROPOSED

    def test_a_rejected_plan_can_be_redone_with_a_rename_and_a_note(self):
        setup = _standard()
        first = setup.compactor.dry_run(setup.email_then_report())

        decisions = setup.email_then_report()
        decisions[0].summary = "Deep work"
        decisions[0].annotate = "phone rang"
        revised = setup.compactor.dry_run(decisions, proposal_id=first.proposal_id, revision=1)

        email = next(c for c in revised.changes if c.event_id == "e1")
        assert email.after.summary == "Deep work"
        assert email.after.description == "Notes:\n- phone rang"
        setup.client.update_event.assert_not_called()


class TestCommit:
    def test_applies_the_plan_journals_each_step_and_stamps_the_notes(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())

        result = setup.compactor.commit(planned.compaction_id)

        assert result.status == "applied"
        patches = {c.args[0].id: c.args[0] for c in setup.client.update_event.call_args_list}
        assert (patches["e1"].start, patches["e1"].end) == (time_at("09:05"), time_at("10:20"))
        # Only what changed: the report still ended as planned.
        assert (patches["e2"].start, patches["e2"].end) == (time_at("10:20"), None)
        journal = setup.journal.load(planned.compaction_id)
        assert journal.status == STAMPED
        assert all(s.status == "done" for s in journal.steps)
        assert setup.notes.read_with_rows() == []
        assert {n.note.compaction_id for n in setup.notes.read_with_rows(include_compacted=True)} == {
            planned.compaction_id
        }

    def test_the_patch_carries_only_what_changed(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())

        setup.compactor.commit(planned.compaction_id)

        patch = next(
            c.args[0] for c in setup.client.update_event.call_args_list if c.args[0].id == "e1"
        )
        assert patch.summary is None and patch.description is None

    def test_cancelled_events_are_patched_to_cancelled(self):
        setup = Setup([("09:00", None), ("09:30", None)])
        planned = setup.compactor.dry_run([EventDecision(action="cancel", event_id="e2")])

        setup.compactor.commit(planned.compaction_id)

        cancelled = next(
            c.args[0] for c in setup.client.update_event.call_args_list if c.args[0].id == "e2"
        )
        assert cancelled.status == "cancelled"

    def test_created_events_get_deterministic_ids(self):
        setup = Setup([("09:00", None), ("09:30", None)])
        planned = setup.compactor.dry_run(setup.coffee_instead_of_email())

        setup.compactor.commit(planned.compaction_id)

        created = setup.client.create_event.call_args.args[0]
        journal = setup.journal.load(planned.compaction_id)
        step = next(s for s in journal.steps if s.action == "create")
        assert created.id == f"cmp{planned.compaction_id}s{step.step:03d}"
        assert created.summary == "Coffee"

    def test_a_retried_create_that_already_exists_counts_as_done(self):
        setup = Setup([("09:00", None), ("09:30", None)])
        planned = setup.compactor.dry_run(setup.coffee_instead_of_email())
        setup.client.create_event.side_effect = HttpError(MagicMock(status=409), b"exists")

        result = setup.compactor.commit(planned.compaction_id)

        assert result.status == "applied"
        assert setup.journal.load(planned.compaction_id).status == STAMPED

    def test_other_create_errors_are_not_swallowed(self):
        setup = Setup([("09:00", None), ("09:30", None)])
        planned = setup.compactor.dry_run(setup.coffee_instead_of_email())
        setup.client.create_event.side_effect = HttpError(MagicMock(status=500), b"boom")

        with pytest.raises(HttpError):
            setup.compactor.commit(planned.compaction_id)

    def test_committing_twice_is_harmless(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())
        setup.compactor.commit(planned.compaction_id)
        calls = setup.client.update_event.call_count

        again = setup.compactor.commit(planned.compaction_id)

        assert again.status == "already_compacted"
        assert setup.client.update_event.call_count == calls

    def test_refuses_when_a_note_was_added_after_the_preview(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())
        setup.append_note("11:00", "one more")

        with pytest.raises(CompactionError, match="changed since.*run a new dry run"):
            setup.compactor.commit(planned.compaction_id)

        setup.client.update_event.assert_not_called()

    def test_refuses_when_the_calendar_changed_after_the_preview(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())
        setup.calendar.events[0].summary = "Someone renamed this"

        with pytest.raises(CompactionError, match="changed since"):
            setup.compactor.commit(planned.compaction_id)

        setup.client.update_event.assert_not_called()

    def test_refuses_an_abandoned_compaction(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())
        setup.compactor.abandon(planned.compaction_id)

        with pytest.raises(CompactionError, match="abandoned"):
            setup.compactor.commit(planned.compaction_id)

    def test_an_unknown_compaction_id_is_reported(self):
        with pytest.raises(CompactionError, match="no compaction"):
            _standard().compactor.commit("nope")


class TestResume:
    def _fail_on_second_write(self, setup):
        calls = []

        def update(event):
            calls.append(event.id)
            if len(calls) == 2:
                raise RuntimeError("calendar hiccup")

        setup.client.update_event.side_effect = update

    def test_a_failure_partway_leaves_the_journal_at_exactly_that_point(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())
        self._fail_on_second_write(setup)

        with pytest.raises(RuntimeError):
            setup.compactor.commit(planned.compaction_id)

        journal = setup.journal.load(planned.compaction_id)
        assert journal.status == APPLYING
        assert [s.status for s in journal.steps] == ["done", "pending"]
        # Its notes are still uncompacted, so nothing is lost.
        assert [n.id for n in setup.notes.read_with_rows()] == [setup.note_id(2), setup.note_id(3)]

    def test_committing_again_finishes_only_what_was_left(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())
        self._fail_on_second_write(setup)
        with pytest.raises(RuntimeError):
            setup.compactor.commit(planned.compaction_id)
        setup.client.update_event.reset_mock(side_effect=True)

        result = setup.compactor.commit(planned.compaction_id)

        assert result.status == "applied"
        assert [c.args[0].id for c in setup.client.update_event.call_args_list] == ["e2"]
        assert setup.journal.load(planned.compaction_id).status == STAMPED
        assert setup.notes.read_with_rows() == []

    def test_resuming_does_not_recheck_the_calendar_it_is_already_changing(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())
        self._fail_on_second_write(setup)
        with pytest.raises(RuntimeError):
            setup.compactor.commit(planned.compaction_id)
        setup.client.update_event.reset_mock(side_effect=True)
        # The first step already moved e1 on the real calendar.
        setup.calendar.events[0].start = time_at("09:05")
        setup.calendar.events[0].end = time_at("10:20")

        assert setup.compactor.commit(planned.compaction_id).status == "applied"


class TestGetProposalAndAbandon:
    def test_getting_the_proposal_returns_its_current_revision_without_applying_it(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())

        proposal = setup.compactor.get_proposal()

        assert (proposal.id, proposal.revision, proposal.state) == (planned.proposal_id, 1, "awaiting_review")
        assert proposal.changes == planned.changes
        email = next(e for e in proposal.events if e.id == "e1")
        assert (email.status, email.decided_by) == ("adjusted", "claude")
        assert (email.start, email.planned_start) == (time_at("09:05"), time_at("09:00"))
        assert next(e for e in proposal.events if e.id == "e3").status == "planned"
        setup.client.update_event.assert_not_called()

    def test_abandoning_leaves_the_notes_uncompacted_and_unblocks_a_new_compaction(self):
        setup = _standard()
        first = setup.compactor.dry_run(setup.email_then_report())
        setup.journal.set_status(setup.journal.load(first.compaction_id), APPLYING)

        result = setup.compactor.abandon(first.compaction_id)

        assert result.status == "abandoned"
        assert [n.id for n in setup.notes.read_with_rows()] == [setup.note_id(2), setup.note_id(3)]
        assert setup.compactor.dry_run(setup.email_then_report()).status == "proposed"

    def test_a_finished_compaction_cannot_be_abandoned(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())
        setup.compactor.commit(planned.compaction_id)

        with pytest.raises(CompactionError, match="already complete"):
            setup.compactor.abandon(planned.compaction_id)


class TestDayByDay:
    def test_a_later_days_notes_are_compacted_with_the_earlier_days(self):
        setup = Setup([("09:05", "email"), ("10:20", "report")])
        setup.append_note("08:00+1", "next day")
        setup.now = "09:00+1"
        planned = setup.compactor.dry_run(setup.email_then_report())
        setup.compactor.commit(planned.compaction_id)

        context = setup.compactor.prepare()

        assert context.notes == []
        # One compaction for both days.
        assert {n.note.compaction_id for n in setup.notes.read_with_rows(include_compacted=True)} == {
            planned.compaction_id,
        }

    def test_the_next_days_compaction_window_starts_where_the_last_one_left_off(self):
        events = [
            event_at("09:00-10:00", id="e1", summary="Email", priority=2),
            event_at("20:00-07:00+1", id="s1", summary="Sleep", priority=0, is_end_of_day_sleep=True),
            event_at("07:00+1-07:30+1", id="gr1", summary="Getting Ready", priority=2),
        ]
        setup = Setup([("09:05", "email")], events=events, now="07:10+1")
        planned = setup.compactor.dry_run([])
        setup.compactor.commit(planned.compaction_id)
        assert setup.journal.last_stamped_now() == time_at("07:10+1")
        setup.append_note("08:15+1", "left for work")
        setup.now = "08:30+1"

        context = setup.compactor.prepare()

        assert context.compaction_window_start == time_at("07:10+1")
        assert [e.id for e in context.events][:2] == ["s1", "gr1"]


class TestSeveralDays:
    """Notes on two days: an email on the first, and on the second a late
    wake-up, then work -- the night's sleep, `s1`, ran from the first
    day's bedtime to the second day's morning."""

    def _events(self):
        return [
            event_at("09:00-10:00", id="e1", summary="Email", priority=2),
            event_at("20:00-07:00+1", id="s1", summary="Sleep", priority=0, is_end_of_day_sleep=True),
            event_at("07:00+1-08:00+1", id="gr1", summary="Getting Ready", priority=2),
            event_at("08:00+1-12:00+1", id="w1", summary="Work", priority=3),
            event_at("22:00+1-23:30+1", id="s2", summary="Sleep", priority=0, is_end_of_day_sleep=True),
        ]

    def _setup(self, notes=(("09:05", "email"), ("08:30+1", "finally up"), ("08:50+1", "working"))):
        return Setup(list(notes), events=self._events(), now="09:00+1")

    def _late_wake_up(self, setup):
        """"finally up" ends the night, which takes the next morning's
        getting ready later with it -- and work, after it."""
        return [
            EventDecision(action="keep", event_id="e1", start_note=setup.note_id(2)),
            EventDecision(action="keep", event_id="s1", end_note=setup.note_id(3)),
            EventDecision(
                action="keep", event_id="gr1", start_note=setup.note_id(3), end=time_at("09:00+1")
            ),
            EventDecision(action="keep", event_id="w1", start=time_at("09:00+1")),
        ]

    def test_every_day_lists_within_one_listing_made_first(self):
        setup = self._setup()
        listed = []
        list_events = setup.calendar.list_events
        setup.calendar.list_events = lambda start, end: listed.append((start, end)) or list_events(start, end)

        setup.compactor.dry_run(self._late_wake_up(setup))

        # The rest are answered from the first, inside a tool call (see
        # cached_calendar_listings) -- all but suggesting actions, which
        # looks weeks back, and only prepare does.
        (first_start, first_end), *rest = listed
        assert len(rest) > 2
        assert all(first_start <= start and end <= first_end for start, end in rest)

    def test_each_day_starts_where_the_one_before_it_ends(self):
        context = self._setup().compactor.prepare()

        assert [(d.compaction_window_start, d.day_end) for d in context.days] == [
            (time_at("09:05"), time_at("07:00+1")),
            (time_at("07:00+1"), time_at("23:30+1")),
        ]
        # The night between them is offered once, and to the second day's
        # first note, to end if it ran late.
        assert [e.id for e in context.events] == ["e1", "s1", "gr1", "w1", "s2"]
        assert "s1" in context.notes[1].candidates

    def test_the_timeline_heads_each_day_with_its_date_and_only_the_last_has_now(self):
        text = self._setup().compactor.prepare().timeline.text

        assert text.index("━━ Thu 01 Jan") < text.index("● email") < text.index("━━ Fri 02 Jan")
        assert text.index("━━ Fri 02 Jan") < text.index("● finally up")
        assert text.count("┄┄ now") == 1

    def test_a_dry_run_plans_and_journals_the_days_as_one_compaction(self):
        setup = self._setup()

        planned = setup.compactor.dry_run(self._late_wake_up(setup))

        (day,) = setup.journal.load_batch(planned.compaction_id)
        assert (day.id, day.status) == (planned.compaction_id, PROPOSED)
        changes = {c.event_id: c for c in planned.changes}
        assert changes["s1"].after.end == time_at("08:30+1")
        assert changes["w1"].after.start == time_at("09:00+1")
        assert planned.timeline.text.count("━━ ") == 2
        assert planned.timeline.text.count("→ note above set this edge") == 1
        assert "over 2 days" in planned.message

    def test_the_note_ending_the_night_ends_it_and_starts_the_morning(self):
        setup = self._setup()

        planned = setup.compactor.dry_run(self._late_wake_up(setup))

        (day,) = setup.journal.load_batch(planned.compaction_id)
        assert day.note_ids == [setup.note_id(2), setup.note_id(3), setup.note_id(4)]
        assert day.now == time_at("09:00+1")
        # "finally up" ends the night, and starts getting ready.
        getting_ready = next(d for d in day.decisions if d.event_id == "gr1")
        assert getting_ready.start_note == setup.note_id(3)
        changes = {c.event_id: c for c in planned.changes}
        assert changes["gr1"].after.start == time_at("08:30+1")
        # The next day is in the span too, so there's nothing to warn of.
        assert not any("now ends at" in w for w in planned.warnings)

    def test_a_later_wake_up_is_checked_against_the_mornings_events(self):
        setup = self._setup()

        with pytest.raises(CompactionError, match="(?s)'Sleep'.*overlaps 'Getting Ready'"):
            setup.compactor.dry_run(
                [EventDecision(action="keep", event_id="s1", end_note=setup.note_id(3))]
            )

    def test_a_late_wake_up_settles_the_next_morning_even_without_its_own_notes(self):
        setup = self._setup(notes=(("09:05", "email"), ("08:30+1", "finally up")))

        planned = setup.compactor.dry_run(self._late_wake_up(setup))

        (day,) = setup.journal.load_batch(planned.compaction_id)
        assert day.note_ids == [setup.note_id(2), setup.note_id(3)]
        assert [d.event_id for d in day.decisions] == ["e1", "s1", "gr1", "w1"]
        assert setup.compactor.commit(planned.compaction_id).status == "applied"
        assert setup.notes.read_with_rows() == []

    def test_an_early_wake_up_ends_the_night_there(self):
        setup = self._setup(notes=(("09:05", "email"), ("05:30+1", "woke up early"), ("07:30+1", "breakfast")))

        planned = setup.compactor.dry_run(
            [EventDecision(action="keep", event_id="s1", end_note=setup.note_id(3))]
        )

        (day,) = setup.journal.load_batch(planned.compaction_id)
        assert day.note_ids == [setup.note_id(2), setup.note_id(3), setup.note_id(4)]
        assert next(c for c in day.changes() if c.event_id == "s1").after.end == time_at("05:30+1")
        assert planned.warnings == []

    def test_a_note_in_the_night_that_doesnt_end_it_leaves_it_as_it_was(self):
        setup = self._setup(notes=(("09:05", "email"), ("03:00+1", "can't sleep"), ("07:30+1", "breakfast")))

        planned = setup.compactor.dry_run([])

        (day,) = setup.journal.load_batch(planned.compaction_id)
        assert day.note_ids == [setup.note_id(2), setup.note_id(3), setup.note_id(4)]
        night = next(c for c in day.changes() if c.event_id == "s1")
        assert night.after.end == time_at("07:00+1")
        assert "can't sleep" in night.after.description

    def test_the_night_is_decided_once(self):
        setup = self._setup()
        decisions = self._late_wake_up(setup)
        decisions[1].start = time_at("21:00")

        planned = setup.compactor.dry_run(decisions)

        (day,) = setup.journal.load_batch(planned.compaction_id)
        assert [d.event_id for d in day.decisions] == ["e1", "s1", "gr1", "w1"]
        (night,) = [c for c in day.changes() if c.event_id == "s1"]
        assert (night.after.start, night.after.end) == (time_at("21:00"), time_at("08:30+1"))

    def test_a_second_decision_on_the_night_is_refused(self):
        setup = self._setup()
        decisions = self._late_wake_up(setup) + [
            EventDecision(action="keep", event_id="s1", annotate="slept badly")
        ]

        with pytest.raises(CompactionError, match="event s1 has more than one decision"):
            setup.compactor.dry_run(decisions)

    def test_a_night_without_sleep_makes_one_long_day(self):
        setup = self._setup(notes=(("09:05", "email"), ("03:00+1", "still up, coding"), ("08:30+1", "breakfast")))

        planned = setup.compactor.dry_run([EventDecision(action="cancel", event_id="s1")])

        (day,) = setup.journal.load_batch(planned.compaction_id)
        assert day.note_ids == [setup.note_id(2), setup.note_id(3), setup.note_id(4)]
        assert day.now == time_at("09:00+1")
        assert [c.event_id for c in day.changes() if c.action == "cancel"] == ["s1"]
        # The next morning is part of the same day: settled as planned, so
        # nothing to write.
        assert "gr1" not in {c.event_id for c in day.changes()}
        assert "━━ " not in planned.timeline.text

    def test_committing_applies_and_stamps_the_days(self):
        setup = self._setup()
        planned = setup.compactor.dry_run(self._late_wake_up(setup))

        result = setup.compactor.commit(planned.compaction_id)

        assert result.status == "applied"
        assert {d.status for d in setup.journal.load_batch(planned.compaction_id)} == {STAMPED}
        assert setup.notes.read_with_rows() == []
        assert setup.journal.last_stamped_now() == time_at("09:00+1")
        patches = {c.args[0].id: c.args[0] for c in setup.client.update_event.call_args_list}
        assert patches["s1"].end == time_at("08:30+1")

    def _fail_on_the_second_day(self, setup):
        def update(event):
            if event.id == "gr1":
                raise RuntimeError("calendar hiccup")

        setup.client.update_event.side_effect = update

    def test_a_failure_partway_resumes_where_it_stopped(self):
        setup = self._setup()
        planned = setup.compactor.dry_run(self._late_wake_up(setup))
        self._fail_on_the_second_day(setup)
        with pytest.raises(RuntimeError):
            setup.compactor.commit(planned.compaction_id)

        (day,) = setup.journal.load_batch(planned.compaction_id)
        assert day.status == APPLYING
        # Stamped once it's all applied, not before.
        assert len(setup.notes.read_with_rows()) == 3

        setup.client.update_event.side_effect = None
        assert setup.compactor.commit(planned.compaction_id).status == "applied"
        assert setup.notes.read_with_rows() == []

    def test_abandoning_partway_keeps_what_was_applied_and_the_notes(self):
        setup = self._setup()
        planned = setup.compactor.dry_run(self._late_wake_up(setup))
        self._fail_on_the_second_day(setup)
        with pytest.raises(RuntimeError):
            setup.compactor.commit(planned.compaction_id)

        result = setup.compactor.abandon(planned.compaction_id)

        assert "stay applied" in result.message
        assert [d.status for d in setup.journal.load_batch(planned.compaction_id)] == [ABANDONED]
        assert len(setup.notes.read_with_rows()) == 3

    def test_refuses_when_an_earlier_days_notes_changed_after_the_preview(self):
        setup = self._setup()
        planned = setup.compactor.dry_run(self._late_wake_up(setup))
        setup.append_note("10:20", "one more")

        with pytest.raises(CompactionError, match="changed since"):
            setup.compactor.commit(planned.compaction_id)

        setup.client.update_event.assert_not_called()

    def test_getting_a_proposal_over_two_days_shows_both(self):
        setup = self._setup()
        planned = setup.compactor.dry_run(self._late_wake_up(setup))

        proposal = setup.compactor.get_proposal()

        assert proposal.changes == planned.changes
        assert proposal.timeline.text.count("━━ ") == 2
        night = next(e for e in proposal.events if e.id == "s1")
        assert (night.status, night.end, night.is_end_of_day_sleep) == ("adjusted", time_at("08:30+1"), True)
        assert [n.id for n in proposal.notes] == [setup.note_id(2), setup.note_id(3), setup.note_id(4)]

    def test_an_open_batch_is_reported_by_its_own_id(self):
        setup = self._setup()
        planned = setup.compactor.dry_run(self._late_wake_up(setup))
        (day,) = setup.journal.load_batch(planned.compaction_id)
        setup.journal.set_status(day, APPLYING)

        assert setup.compactor.prepare().open_compaction == planned.compaction_id
        with pytest.raises(CompactionError, match=f"compaction {planned.compaction_id} is applying"):
            setup.compactor.dry_run([])

    def test_takes_on_at_most_a_week_at_a_time(self):
        start = time_at("09:00")
        events = [
            Event(
                id=f"s{day}",
                summary="Sleep",
                start=start + timedelta(days=day, hours=13),
                end=start + timedelta(days=day + 1, hours=-2),
                priority=0,
                is_end_of_day_sleep=True,
            )
            for day in range(10)
        ]
        setup = Setup([], events=events)
        for day in range(9):
            setup.notes.append(NotedTime(timestamp=start + timedelta(days=day), description=f"day {day}"))
        setup.compactor._clock = lambda: start + timedelta(days=8, hours=1)

        context = setup.compactor.prepare()

        assert len(context.days) == note_compactor._MAX_DAYS
        assert context.remaining_note_count == 9 - note_compactor._MAX_DAYS


class TestSleepingInPastTheSpan:
    """A compaction made in the night -- its span ends with that night --
    that says the night ran on past the morning's events: they're its to
    settle too."""

    def _setup(self):
        events = [
            event_at("21:00-22:00", id="e1", summary="Reading", priority=2),
            event_at("22:00-07:00+1", id="s1", summary="Sleep", priority=0, is_end_of_day_sleep=True),
            event_at("07:00+1-08:00+1", id="gr1", summary="Getting Ready", priority=2),
            event_at("09:15+1-12:00+1", id="w1", summary="Work", priority=3),
            event_at("22:00+1-23:30+1", id="s2", summary="Sleep", priority=0, is_end_of_day_sleep=True),
        ]
        return Setup([("21:05", "reading")], events=events, now="06:30+1")

    def test_the_span_ends_with_the_night(self):
        context = self._setup().compactor.prepare()

        assert [e.id for e in context.events] == ["e1", "s1"]

    def test_a_later_end_takes_in_what_it_runs_into(self):
        setup = self._setup()

        planned = setup.compactor.dry_run(
            [
                EventDecision(action="keep", event_id="s1", end=time_at("09:00+1")),
                EventDecision(action="cancel", event_id="gr1"),
            ]
        )

        changes = {c.event_id: c for c in planned.changes}
        assert changes["s1"].after.end == time_at("09:00+1")
        assert changes["gr1"].action == "cancel"
        # Work, at 9:15, it doesn't reach.
        assert "w1" not in changes

    def test_what_it_runs_into_must_be_settled(self):
        setup = self._setup()

        with pytest.raises(CompactionError, match="(?s)'Sleep'.*overlaps 'Getting Ready'"):
            setup.compactor.dry_run([EventDecision(action="keep", event_id="s1", end=time_at("09:00+1"))])

    def test_what_it_doesnt_reach_is_still_outside(self):
        setup = self._setup()

        with pytest.raises(CompactionError, match="w1' isn't one of this day's events"):
            setup.compactor.dry_run(
                [
                    EventDecision(action="keep", event_id="s1", end=time_at("09:00+1")),
                    EventDecision(action="cancel", event_id="gr1"),
                    EventDecision(action="keep", event_id="w1", start=time_at("09:30+1")),
                ]
            )


class TestSleepingIn:
    """The event that ended just before the compaction window -- here,
    last night's sleep -- is offered with it, so a late wake-up can extend
    it."""

    def _slept_in(self):
        events = [
            event_at("09:00-10:00", id="e1", summary="Email", priority=2),
            event_at("20:00-07:00+1", id="s1", summary="Sleep", priority=0, is_end_of_day_sleep=True),
            event_at("07:00+1-08:00+1", id="gr1", summary="Getting Ready", priority=2),
            event_at("08:00+1-12:00+1", id="w1", summary="Work", priority=3),
            event_at("22:00+1-23:30+1", id="s2", summary="Sleep", priority=0, is_end_of_day_sleep=True),
        ]
        setup = Setup([("09:05", "email")], events=events, now="07:00+1")
        planned = setup.compactor.dry_run([])
        setup.compactor.commit(planned.compaction_id)
        setup.client.reset_mock()
        setup.append_note("08:30+1", "finally up")
        setup.now = "09:00+1"
        return setup

    def test_prepare_offers_it_to_the_days_first_note(self):
        setup = self._slept_in()

        context = setup.compactor.prepare()

        assert [e.id for e in context.events] == ["s1", "gr1", "w1", "s2"]
        assert "s1" in context.notes[0].candidates
        assert context.day_end == time_at("23:30+1")

    def test_extending_it_over_the_morning_needs_the_morning_moved_too(self):
        setup = self._slept_in()

        with pytest.raises(CompactionError, match="(?s)'Sleep' \\(20:00–08:30, as asked\\) overlaps 'Getting Ready'"):
            setup.compactor.dry_run(
                [EventDecision(action="keep", event_id="s1", end_note=setup.note_id(3))]
            )

    def test_a_late_wake_up_extends_it_and_moves_the_rest_of_the_morning_as_decided(self):
        setup = self._slept_in()
        planned = setup.compactor.dry_run(
            [
                EventDecision(action="keep", event_id="s1", end_note=setup.note_id(3)),
                EventDecision(
                    action="keep", event_id="gr1", start_note=setup.note_id(3), end=time_at("09:00+1")
                ),
                EventDecision(action="keep", event_id="w1", start=time_at("09:00+1")),
            ]
        )
        changes = {c.event_id: c for c in planned.changes}
        assert (changes["s1"].after.start, changes["s1"].after.end) == (time_at("20:00"), time_at("08:30+1"))
        assert changes["w1"].after.start == time_at("09:00+1")

        setup.compactor.commit(planned.compaction_id)

        patches = {c.args[0].id: c.args[0] for c in setup.client.update_event.call_args_list}
        assert patches["s1"].end == time_at("08:30+1")
        assert "s2" not in patches


class TestEditAndDeleteNotes:
    def test_edits_and_deletes_uncompacted_notes(self):
        setup = _standard()

        edited = setup.compactor.edit_note(setup.note_id(2), description="inbox zero")
        setup.compactor.delete_note(setup.note_id(3))

        notes = setup.notes.read_with_rows()
        assert [(n.id, n.note.description) for n in notes] == [(edited.id, "inbox zero")]

    def test_a_stale_id_is_a_compaction_error(self):
        setup = _standard()
        stale = setup.note_id(2)
        setup.compactor.edit_note(stale, timestamp=time_at("09:10"))

        with pytest.raises(CompactionError, match="no longer holds the note"):
            setup.compactor.delete_note(stale)

    def test_a_planned_compaction_does_not_block_it_but_can_no_longer_be_committed(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())

        setup.compactor.edit_note(setup.note_id(3), timestamp=time_at("10:25"))

        with pytest.raises(CompactionError, match="changed since"):
            setup.compactor.commit(planned.compaction_id)

    @pytest.mark.parametrize("change", ["edit", "delete"])
    def test_refused_while_a_compaction_with_that_note_is_being_applied(self, change):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())
        setup.journal.set_status(setup.journal.load(planned.compaction_id), APPLYING)
        note_id = setup.note_id(2)

        with pytest.raises(CompactionError, match="is applying"):
            if change == "edit":
                setup.compactor.edit_note(note_id, description="x")
            else:
                setup.compactor.delete_note(note_id)

        assert setup.notes.read_with_rows()[0].note.description == "email"

    def test_a_note_outside_the_compaction_being_applied_can_still_change(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())
        setup.journal.set_status(setup.journal.load(planned.compaction_id), APPLYING)
        setup.append_note("08:00+1", "next day")

        setup.compactor.edit_note(setup.note_id(4), description="tomorrow")

        assert setup.notes.read_with_rows()[-1].note.description == "tomorrow"


class TestMovingFutureEvents:
    def test_a_keep_with_new_times_reschedules_a_future_event(self):
        setup = _standard()
        decisions = setup.email_then_report() + [
            EventDecision(action="keep", event_id="e3", start=time_at("12:15"), end=time_at("12:45"))
        ]

        planned = setup.compactor.dry_run(decisions)
        setup.compactor.commit(planned.compaction_id)

        assert {c.event_id for c in planned.changes} == {"e1", "e2", "e3"}
        patch = next(
            c.args[0] for c in setup.client.update_event.call_args_list if c.args[0].id == "e3"
        )
        assert (patch.start, patch.end) == (time_at("12:15"), time_at("12:45"))

    def test_moving_bedtime_near_the_end_of_the_day_plans_and_commits(self):
        setup = Setup([("09:05", "email"), ("10:20", "report")], now="19:45")
        bedtime = EventDecision(
            action="keep", event_id="s1", start=time_at("22:30"), end=time_at("08:00") + timedelta(days=1)
        )

        planned = setup.compactor.dry_run(setup.email_then_report() + [bedtime])
        setup.compactor.commit(planned.compaction_id)

        assert planned.status == "proposed"
        assert any("doesn't adjust the next day" in w for w in planned.warnings)
        patch = next(
            c.args[0] for c in setup.client.update_event.call_args_list if c.args[0].id == "s1"
        )
        assert (patch.start, patch.end) == (time_at("22:30"), time_at("08:00") + timedelta(days=1))

    def test_with_no_notes_at_all_there_is_nothing_to_compact(self):
        setup = Setup([])

        result = setup.compactor.dry_run(
            [EventDecision(action="keep", event_id="e3", start=time_at("12:15"))]
        )

        assert result.status == "nothing_to_compact"


class TestGarbageCollection:
    def test_prepare_garbage_collects_the_journal_first(self):
        setup = _standard()
        spy_journal = MagicMock(wraps=setup.journal)
        compactor = NoteCompactor(
            calendar=setup.calendar,
            client=setup.client,
            notes=setup.notes,
            journal=spy_journal,
            clock=lambda: time_at(setup.now),
        )

        compactor.prepare()

        spy_journal.garbage_collect.assert_called_once()


class TestEventLabels:
    """Calendar rejects inserting an event with a label it doesn't have
    (HTTP 400), so created events are checked against its labels."""

    def _with_labels(self, setup, *label_ids):
        setup.client.list_event_labels.return_value = (
            [EventLabel(id=i, name=f"Label {i}", background_color="#000000") for i in label_ids],
            "etag",
        )

    def test_other_created_events_drop_a_label_the_calendar_no_longer_has(self, monkeypatch):
        # E.g. a split continuation, cloned (label and all) from an event
        # whose label has since been removed -- not the model's to fix.
        setup = Setup([("09:00", None), ("09:30", None)])
        self._with_labels(setup, "real")
        continuation = CompactionChange(
            action="create",
            reason="split",
            after=EventState(summary="Report (cont.)", event_label_id="removed"),
        )
        monkeypatch.setattr(
            note_compactor, "plan_compaction", lambda *args, **kwargs: CompactionPlan(changes=[continuation])
        )

        planned = setup.compactor.dry_run(setup.email_then_report())

        assert planned.changes[0].after.event_label_id is None

    def test_labels_are_only_read_when_a_created_event_has_one(self):
        setup = Setup([("09:00", None), ("09:30", None)])

        setup.compactor.dry_run(setup.coffee_instead_of_email())

        setup.client.list_event_labels.assert_not_called()


def _action(action_id, name, status="active"):
    return Action(id=action_id, name=name, status=status, label_id=f"label-{action_id}")


class TestActions:
    _ACTIONS = [_action("work", "Time Tracker"), _action("mail", "Do email"), _action("gone", "Old", status="deleted")]

    def test_prepare_lists_actions_and_suggests_them_from_earlier_events_with_the_same_title(self):
        events = _day()
        yesterday = [
            replace(e, id=f"y-{e.id}", start=e.start - timedelta(days=1), end=e.end - timedelta(days=1))
            for e in events[:2]
        ]
        yesterday[0].action_ids = ["gone", "mail"]  # Email: a deleted action isn't suggested
        yesterday[1].action_ids = ["work"]
        events[1].action_ids = ["work"]  # Report already has one: nothing suggested
        setup = Setup([("09:05", "x")], events=yesterday + events, actions=self._ACTIONS)

        context = setup.compactor.prepare()

        by_id = {e.id: e for e in context.events}
        assert by_id["e1"].suggested_action_ids == ["mail"]
        assert by_id["e2"].suggested_action_ids is None
        assert by_id["e2"].action_names == ["Time Tracker"]
        assert [a.name for a in context.actions] == ["Time Tracker", "Do email"]  # not deleted ones
        text = context.timeline.text
        assert "┌ Email\n          ◇ Do email" in text
        assert "├ Report\n          ◆ Time Tracker" in text

    def test_prepare_gives_the_people_and_locations_to_settle_facts_from(self):
        setup = Setup(
            [("09:05", "x")],
            people=[Person(id="sam", name="Sam", context="salsa", status="active", what_matters="tea")],
            locations=[Location(id="home", name="Home", hint="the apartment")],
        )

        context = setup.compactor.prepare()

        assert [(p.id, p.name, p.context, p.what_matters) for p in context.people] == [
            ("self", "Me", None, None), ("sam", "Sam", "salsa", "tea")
        ]
        assert [(loc.id, loc.name, loc.hint) for loc in context.locations] == [("home", "Home", "the apartment")]
        assert "COMPACTION ESTABLISHES THE FACTS" in context.instructions

    def test_future_events_get_no_suggestions(self):
        events = _day()
        earlier = replace(events[2], id="y-e3", start=events[2].start - timedelta(days=1),
                          end=events[2].end - timedelta(days=1), action_ids=["work"])
        setup = Setup([("09:05", "x")], events=[earlier] + events, actions=self._ACTIONS)

        context = setup.compactor.prepare()

        assert {e.id: e for e in context.events}["e3"].suggested_action_ids is None  # Lunch is after now

    def test_a_dry_run_refuses_unknown_or_deleted_actions(self):
        setup = Setup([("09:00", None), ("09:30", None)], actions=self._ACTIONS)
        decisions = setup.coffee_instead_of_email()

        decisions[0].action_ids = ["wrk"]
        with pytest.raises(CompactionError, match="no action with the id or name 'wrk'; did you mean work"):
            setup.compactor.dry_run(decisions)
        decisions[0].action_ids = ["gone"]
        with pytest.raises(CompactionError, match="Deleted actions can't be given to an event"):
            setup.compactor.dry_run(decisions)

    def test_created_events_are_written_with_their_actions(self):
        setup = Setup([("09:00", None), ("09:30", None)], actions=self._ACTIONS)
        decisions = setup.coffee_instead_of_email()
        decisions[0].action_ids = ["work"]
        planned = setup.compactor.dry_run(decisions)

        setup.compactor.commit(planned.compaction_id)

        assert setup.client.create_event.call_args.args[0].action_ids == ["work"]

    def test_an_actions_only_change_is_patched_as_just_that(self):
        setup = Setup([("09:05", "x")], actions=self._ACTIONS)
        planned = setup.compactor.dry_run([EventDecision(action="keep", event_id="e1", action_ids=["mail"])])

        setup.compactor.commit(planned.compaction_id)

        patches = [c.args[0] for c in setup.client.update_event.call_args_list if c.args[0].id == "e1"]
        assert any(p.action_ids == ["mail"] for p in patches)


class _ProductionCalendar(FakeCalendar):
    """The CalendarClient calls the server's compaction makes, for
    `TestSheetReadRequests` -- the calendar isn't what's being counted."""

    def __init__(self, events, spreadsheet_id):
        super().__init__(events)
        self.metadata = {calendar_metadata_sheet._SPREADSHEET_ID_METADATA_KEY: spreadsheet_id}

    def get_calendar_metadata(self, key):
        return self.metadata.get(key)

    def set_calendar_metadata(self, key, value):
        self.metadata[key] = value

    def get_time_zone(self):
        return timezone.utc

    def list_event_labels(self):
        return [], "etag"

    def update_event(self, event):
        return event

    def create_event(self, event):
        return event


class TestSheetReadRequests:
    """Google Sheets throttles read requests to 60 a minute per user, and
    a compaction step that runs out waits for the quota to roll over (see
    calendar_clients/google_sheets.py's `_execute`) -- so every read
    request a compaction makes counts. These pin how many a typical
    round makes, running the production code end to end: the MCP tools
    in server.py, each in its own `cached_sheet_reads` as they are there,
    over the objects server.py's get_* helpers build with config.py's
    builders, all on the real `SheetsClient` -- with only Google itself
    faked, the Sheets service by `FakeSheetsService` (and memory
    diagnostics left out). If a count goes up,
    find a way not to (a hint, or folding the read into one already
    made); if one goes down, lower it here."""

    _SPREADSHEET = "spreadsheet-1"

    def _server(self, monkeypatch):
        service = FakeSheetsService(FakeSheets())
        sheets_client = SheetsClient(service)
        calendar = _ProductionCalendar(TestTheCompactionWindow()._events(), self._SPREADSHEET)
        # Where the builders would load credentials and build the Google
        # API clients.
        monkeypatch.setattr(config, "_build_calendar_and_sheets_clients", lambda calendar_id=None: (calendar, sheets_client))
        monkeypatch.setattr(server, "build_calendar_client", lambda: calendar)
        # Memory diagnostics never touch Sheets, and take seconds.
        monkeypatch.setattr(server, "track", lambda label: contextlib.nullcontext())
        for cached in (
            "_calendar_client", "_actions", "_people", "_locations", "_traits",
            "_noted_time_sheet", "_note_compactor", "_cancellations", "_habits",
        ):
            monkeypatch.setattr(server, cached, None)
        self.now = "09:10+1"
        # The one seam: the compactor's clock, so the rounds fall on the
        # test calendar's day.
        server.get_note_compactor()._clock = lambda: time_at(self.now)
        return service

    def _round(self, service, note_at, now):
        """One round of MCP tool calls: note, prepare_compaction,
        compact_notes (proposing), confirm_proposal. Returns each
        call's read requests."""
        self.now = now
        counts = {}

        def call(name, tool):
            before = len(service.read_requests)
            result = tool()
            counts[name] = len(service.read_requests) - before
            return result

        call("note", lambda: server.note(NotedTime(timestamp=time_at(note_at), description="note")))
        call("prepare_compaction", server.prepare_compaction)
        plan = call("compact_notes dry run", lambda: server.compact_notes())
        call("confirm_proposal", lambda: server.confirm_proposal(plan.proposal_id, plan.revision))
        return counts

    def test_a_typical_round_of_compaction_makes_no_more_read_requests_than_this(self, monkeypatch):
        service = self._server(monkeypatch)
        for note_at, now in [("09:05+1", "09:10+1"), ("09:50+1", "09:55+1"), ("10:20+1", "10:25+1")]:
            self._round(service, note_at, now)

        counts = self._round(service, "10:40+1", "10:45+1")

        # Each compaction call reads the notes tab, the journal and the
        # actions', people's and locations' tabs whole, together in one request (NoteCompactor's
        # prefetch); every later read of them in the call falls within
        # that, so it's served from the cache.
        assert counts == {
            "note": 1,
            "prepare_compaction": 1,
            "compact_notes dry run": 1,
            "confirm_proposal": 1,
        }


class TestFacts:
    """Compaction records each past event's facts: where, who with, who
    for, and a note on each person there."""

    _PEOPLE = [Person(id="sam", name="Sam", status="active"), Person(id="mom", name="Mom", status="active")]
    _LOCATIONS = [Location(id="home", name="Home")]

    def _setup(self, **kwargs):
        return Setup([("09:05", "email"), ("10:20", "report")], people=self._PEOPLE, locations=self._LOCATIONS, **kwargs)

    def test_a_keep_records_facts_and_shows_them_in_the_timeline(self):
        setup = self._setup()
        facts = Facts(location_id="home", with_ids=["sam"], for_ids=["mom"], notes={"self": "sleepy", "sam": " happy  "})

        planned = setup.compactor.dry_run([EventDecision(action="keep", event_id="e1", facts=facts)])

        (change,) = [c for c in planned.changes if c.event_id == "e1"]
        assert change.after.facts == {
            "location": "home", "with": ["sam"], "for": ["mom"], "notes": {"self": "sleepy", "sam": "happy"}
        }
        text = planned.timeline.text
        assert "▹ @ Home · with Sam · for Mom" in text
        assert "▹ Me: sleepy" in text and "▹ Sam: happy" in text
        setup.compactor.commit(planned.compaction_id)
        (patch,) = [c.args[0] for c in setup.client.update_event.call_args_list if c.args[0].id == "e1"]
        assert patch.facts == Facts(location_id="home", with_ids=["sam"], for_ids=["mom"], notes={"self": "sleepy", "sam": "happy"})

    def test_facts_that_arent_well_formed_or_name_strangers_are_refused(self):
        setup = self._setup()

        with pytest.raises(CompactionError, match=r'decision 1 \(keep e1\): its facts "with_ids" never names "self"'):
            setup.compactor.dry_run([EventDecision(action="keep", event_id="e1", facts=Facts(with_ids=["self"]))])
        with pytest.raises(CompactionError, match=r"its facts name \['nobody'\], who aren't people"):
            setup.compactor.dry_run([EventDecision(action="keep", event_id="e1", facts=Facts(for_ids=["nobody"]))])
        with pytest.raises(CompactionError, match="its facts' location 'nowhere' isn't a location"):
            setup.compactor.dry_run([EventDecision(action="keep", event_id="e1", facts=Facts(location_id="nowhere"))])
        with pytest.raises(CompactionError, match="facts only go with 'keep' or 'create'"):
            setup.compactor.dry_run([EventDecision(action="cancel", event_id="e1", facts=Facts(with_ids=["sam"]))])

    def test_a_created_event_takes_facts(self):
        setup = Setup([("09:00", None), ("09:30", None)], people=self._PEOPLE)
        decisions = setup.coffee_instead_of_email()
        decisions[0].facts = Facts(with_ids=["sam"], notes={"sam": "chatty"})

        setup.compactor.commit(setup.compactor.dry_run(decisions).compaction_id)

        assert setup.client.create_event.call_args.args[0].facts == Facts(with_ids=["sam"], notes={"sam": "chatty"})


class TestAdditions:
    """New actions, people and locations, added from the compaction itself."""

    def test_new_ones_are_named_by_ref_shown_and_created_when_applied(self):
        setup = Setup([("09:00", None), ("09:30", None)], people=[Person(id="sam", name="Sam", status="active")])
        decisions = setup.coffee_instead_of_email()
        decisions[0].action_ids = ["new:coffee"]
        decisions[0].facts = Facts(location_id="new:cafe", with_ids=["sam", "new:alex"], notes={"new:alex": "new in town"})

        planned = setup.compactor.dry_run(
            decisions,
            new_actions=[NewAction(ref="new:coffee", name="Drink coffee")],
            new_people=[NewPerson(ref="new:alex", name="Alex", context="Sam's friend")],
            new_locations=[NewLocation(ref="new:cafe", name="Corner cafe", hint="the cafe on Main")],
        )

        assert planned.additions == {
            "actions": [{"ref": "new:coffee", "name": "Drink coffee"}],
            "people": [{"ref": "new:alex", "name": "Alex", "context": "Sam's friend"}],
            "locations": [{"ref": "new:cafe", "name": "Corner cafe", "hint": "the cafe on Main"}],
        }
        text = planned.timeline.text
        assert "◇ Drink coffee (new)" in text
        # Wrapped to the timeline's width.
        assert "▹ @ Corner cafe (new) · with\n            Sam, Alex (new)\n" in text
        assert setup.actions.all() == []  # Nothing created yet.

        setup.compactor.commit(planned.compaction_id)

        (coffee,) = setup.actions.all()
        assert (coffee.name, coffee.status) == ("Drink coffee", "active")
        alex = setup.people.get_person("alex")
        cafe = setup.locations.get_location("corner cafe")
        created = setup.client.create_event.call_args.args[0]
        assert created.action_ids == [coffee.id]
        assert created.facts == Facts(location_id=cafe.id, with_ids=["sam", alex.id], notes={alex.id: "new in town"})

    def test_a_resumed_commit_reuses_what_it_already_added(self):
        setup = Setup([("09:00", None), ("09:30", None)])
        decisions = setup.coffee_instead_of_email()
        decisions[0].action_ids = ["new:coffee"]
        planned = setup.compactor.dry_run(decisions, new_actions=[NewAction(ref="new:coffee", name="Drink coffee")])
        setup.client.create_event.side_effect = [RuntimeError("network"), None]
        with pytest.raises(RuntimeError):
            setup.compactor.commit(planned.compaction_id)

        setup.compactor.commit(planned.compaction_id)

        assert [a.name for a in setup.actions.all()] == ["Drink coffee"]
        assert setup.client.create_event.call_args.args[0].action_ids == [setup.actions.all()[0].id]

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            ({"new_actions": [NewAction(ref="coffee", name="Coffee")]}, "'coffee' isn't a ref: refs start with 'new:'"),
            (
                {"new_actions": [NewAction(ref="new:a", name="Coffee")], "new_locations": [NewLocation(ref="new:a", name="Cafe")]},
                "the ref 'new:a' is used more than once",
            ),
            ({"new_actions": [NewAction(ref="new:a", name="Time Tracker")]}, "new actions: there's already an action named"),
            ({"new_people": [NewPerson(ref="new:p", name="Me")]}, "new people: there's already a person named 'Me'"),
            ({"new_locations": [NewLocation(ref="new:l", name="")]}, "new locations: location new:l needs a name"),
        ],
    )
    def test_additions_are_checked_as_their_stores_would(self, kwargs, message):
        setup = Setup([("09:05", "x")], actions=[_action("work", "Time Tracker")])

        with pytest.raises(CompactionError, match=message):
            setup.compactor.dry_run([], **kwargs)

    def test_a_ref_that_isnt_added_is_refused(self):
        setup = Setup([("09:05", "x")])

        with pytest.raises(CompactionError, match="a ref that isn't in new_actions"):
            setup.compactor.dry_run([EventDecision(action="keep", event_id="e1", action_ids=["new:nope"])])


class TestJudgments:
    """Once its facts are written, a compaction's events are judged, and it
    isn't complete until they are."""

    _TRAIT = Trait(
        id="heard", name="Heard", status="active", definition="Listen.",
        parts=[{"kind": "judgment", "rubric": "Were they heard?", "ratings": {"0": "no", "1": "yes"},
                "facts": ["person_notes"]}],
    )

    def _applied(self):
        setup = Setup(
            [("09:05", "email")], people=[Person(id="sam", name="Sam", status="active")], traits=[self._TRAIT]
        )
        facts = Facts(with_ids=["sam"], notes={"sam": "vented about work"})
        planned = setup.compactor.dry_run([EventDecision(action="keep", event_id="e1", facts=facts)])
        # The calendar as the commit leaves it, for judging to read.
        setup.client.update_event.side_effect = lambda patch: setup.calendar.events.__setitem__(
            0, replace(setup.calendar.events[0], facts=patch.facts or setup.calendar.events[0].facts)
        )
        return setup, setup.compactor.commit(planned.compaction_id)

    def test_applying_asks_for_the_judgments_and_isnt_complete_until_theyre_made(self):
        setup, applied = self._applied()

        (event,) = applied.judgments.events
        assert (event.event_id, event.summary, event.people) == (
            "e1", "Email", {"self": ["heard/judgment"], "sam": ["heard/judgment"]},
        )
        assert "isn't complete yet: make the 2 judgment(s)" in applied.message
        assert "JUDGE EACH ONE YOURSELF" in applied.judgments.instructions
        assert setup.compactor.prepare().judgments_pending == applied.compaction_id

    def test_each_part_is_given_once_however_many_judgments_name_it(self):
        _setup, applied = self._applied()

        (part,) = applied.judgments.parts
        assert (part.key, part.trait_name, part.engagement, part.rubric, part.ratings, part.facts) == (
            "heard/judgment", "Heard", "with", "Were they heard?", {"0": "no", "1": "yes"}, ["person_notes"],
        )

    def test_they_come_beside_the_final_timeline_the_user_approved(self):
        setup = Setup(
            [("09:05", "email")], people=[Person(id="sam", name="Sam", status="active")], traits=[self._TRAIT]
        )
        planned = setup.compactor.dry_run([EventDecision(action="keep", event_id="e1", facts=Facts(with_ids=["sam"]))])
        setup.client.update_event.side_effect = lambda patch: setup.calendar.events.__setitem__(
            0, replace(setup.calendar.events[0], facts=patch.facts or setup.calendar.events[0].facts)
        )

        applied = setup.compactor.commit(planned.compaction_id)

        assert applied.timeline.text == planned.timeline.text
        assert "of the events as `timeline` shows them" in applied.message

    def test_nothing_about_judging_is_sent_before_the_plan_is_approved(self):
        setup = Setup(
            [("09:05", "email")], people=[Person(id="sam", name="Sam", status="active")], traits=[self._TRAIT]
        )

        context = setup.compactor.prepare()
        planned = setup.compactor.dry_run([EventDecision(action="keep", event_id="e1", facts=Facts(with_ids=["sam"]))])

        assert "Were they heard?" not in repr(context) + repr(planned)

    def test_a_persons_own_parts_are_given_under_a_key_of_their_own(self):
        own = [{"kind": "judgment", "rubric": "Did Sam feel heard?", "ratings": {"0": "no", "1": "yes"},
                "facts": ["person_notes"]}]
        setup = Setup(
            [("09:05", "email")],
            people=[Person(id="sam", name="Sam", status="active", traits={"parts": {"heard": own}})],
            traits=[self._TRAIT],
        )
        planned = setup.compactor.dry_run([EventDecision(action="keep", event_id="e1", facts=Facts(with_ids=["sam"]))])
        setup.client.update_event.side_effect = lambda patch: setup.calendar.events.__setitem__(
            0, replace(setup.calendar.events[0], facts=patch.facts or setup.calendar.events[0].facts)
        )

        applied = setup.compactor.commit(planned.compaction_id)

        assert {p.key: p.rubric for p in applied.judgments.parts} == {
            "heard/judgment": "Were they heard?", "heard/judgment@sam": "Did Sam feel heard?",
        }

    def test_each_persons_recent_history_is_given_once(self):
        trait = replace(
            self._TRAIT, parts=[{**self._TRAIT.parts[0], "facts": [{"fact": "action_history", "lookback_days": 7}]}]
        )
        yesterday = event_at("09:00-10:00", id="g1", summary="Guitar", action_ids=["guitar"], facts=Facts(with_ids=["sam"]))
        yesterday.start -= timedelta(days=1)
        yesterday.end -= timedelta(days=1)
        setup = Setup(
            [("09:05", "email")], events=[yesterday, *_day()], people=[Person(id="sam", name="Sam", status="active")],
            traits=[trait], actions=[_action("guitar", "Play guitar")],
        )
        planned = setup.compactor.dry_run([EventDecision(action="keep", event_id="e1", facts=Facts(with_ids=["sam"]))])
        setup.client.update_event.side_effect = lambda patch: setup.calendar.events.__setitem__(
            1, replace(setup.calendar.events[1], facts=patch.facts or setup.calendar.events[1].facts)
        )

        applied = setup.compactor.commit(planned.compaction_id)

        assert {p: (h.days, h.actions) for p, h in applied.judgments.history.items()} == {
            "self": (7, {"Play guitar": 1}),
            "sam": (7, {"Play guitar": 1}),
        }

    def test_recording_them_all_completes_it(self):
        setup, applied = self._applied()

        partial = setup.compactor.record_judgments(
            applied.compaction_id, [Judgment(request_id="e1/sam/heard/judgment", rating=1, reasoning="Talked it out.")]
        )
        assert (partial.complete, partial.remaining) == (False, ["e1/self/heard/judgment"])

        done = setup.compactor.record_judgments(
            applied.compaction_id, [Judgment(request_id="e1/self/heard/judgment", rating=0, reasoning="Mostly listened.")]
        )

        assert done.complete and done.remaining == []
        assert setup.calendar.events[0].judgments["sam"]["heard"]["judgment"]["rating"] == 1
        assert setup.compactor.judgments_due().events == []
        assert setup.compactor.prepare().judgments_pending is None

    def test_a_judgment_can_be_redone(self):
        setup, applied = self._applied()
        judgment = Judgment(request_id="e1/sam/heard/judgment", rating=1, reasoning="Talked it out.")
        setup.compactor.record_judgments(applied.compaction_id, [judgment])

        redo = setup.compactor.judgments_due(applied.compaction_id, redo=True)
        setup.compactor.record_judgments(applied.compaction_id, [replace(judgment, rating=0, reasoning="Only half.")])

        (event,) = redo.events
        assert event.current == {"sam": {"heard/judgment": {"rating": 1, "reasoning": "Talked it out."}}}
        assert setup.calendar.events[0].judgments["sam"]["heard"]["judgment"]["reasoning"] == "Only half."

    def test_an_unapplied_compaction_has_nothing_to_judge(self):
        setup = Setup([("09:05", "email")], traits=[self._TRAIT])
        planned = setup.compactor.dry_run([])

        with pytest.raises(CompactionError, match="hasn't been applied"):
            setup.compactor.judgments_due(planned.compaction_id)


class TestHabitBackfill:
    """A habit's judgments of its settled events, backfilled by hand: from
    prepare_habit_judgments to record_judgments, by the backfill's id."""

    def _setup(self, status="active"):
        setup, applied = TestJudgments()._applied()
        # Email, and Lunch after the compaction, are both in the habit's
        # scope; only Email is settled.
        setup.actions._sheet.write([_action("mail", "Do email")])
        setup.calendar.events[0] = replace(setup.calendar.events[0], action_ids=["mail"])
        setup.calendar.events[2] = replace(
            setup.calendar.events[2], action_ids=["mail"], facts=Facts(notes={"self": "late"})
        )
        habits = Habits.ensure(setup.sheets, "s")
        habits._sheet.write([Habit(id="inbox", name="Inbox", action_id="mail", status=status)])
        setup.judging._habits = habits
        return setup

    def test_gives_a_backfill_of_its_settled_events_in_scope_and_records_them_by_its_id(self):
        setup = self._setup()

        due = setup.compactor.habit_judgments_due("Inbox")

        assert due.backfill_id == f"habit:inbox@{due.since.isoformat()}"
        assert due.until == time_at("11:30")
        assert due.since == due.until - timedelta(days=30 + 7)
        assert [(e.event_id, e.people) for e in due.events] == [("e1", {"habit:inbox": ["heard/judgment"]})]
        assert "backfill" in due.instructions

        result = setup.compactor.record_judgments(
            due.backfill_id,
            [Judgment(request_id="e1/habit:inbox/heard/judgment", rating=1, reasoning="Cleared it calmly.")],
        )

        assert (result.complete, result.remaining) == (True, [])
        assert setup.calendar.events[0].judgments["habit:inbox"]["heard"]["judgment"]["rating"] == 1
        assert setup.compactor.habit_judgments_due("inbox").events == []

    def test_redone_with_the_judgments_made(self):
        setup = self._setup()
        due = setup.compactor.habit_judgments_due("inbox")
        setup.compactor.record_judgments(
            due.backfill_id, [Judgment(request_id="e1/habit:inbox/heard/judgment", rating=1, reasoning="Calm.")]
        )

        redo = setup.compactor.habit_judgments_due("inbox", redo=True)
        setup.compactor.record_judgments(
            redo.backfill_id, [Judgment(request_id="e1/habit:inbox/heard/judgment", rating=0, reasoning="Rushed.")]
        )

        (event,) = redo.events
        assert event.current == {"habit:inbox": {"heard/judgment": {"rating": 1, "reasoning": "Calm."}}}
        assert setup.calendar.events[0].judgments["habit:inbox"]["heard"]["judgment"]["reasoning"] == "Rushed."

    def test_from_when_its_asked(self):
        setup = self._setup()

        # After Email started: nothing settled since.
        assert setup.compactor.habit_judgments_due("inbox", since=time_at("09:30")).events == []

    def test_only_an_active_habit_and_one_there(self):
        setup = self._setup(status="archived")

        with pytest.raises(CompactionError, match="is archived: only an active habit is judged"):
            setup.compactor.habit_judgments_due("inbox")
        with pytest.raises(CompactionError, match="There's no habit with the id or name 'nope'"):
            setup.compactor.habit_judgments_due("nope")

    def test_a_judgment_that_isnt_the_backfills_is_refused(self):
        setup = self._setup()
        due = setup.compactor.habit_judgments_due("inbox")

        with pytest.raises(CompactionError, match="isn't one of"):
            setup.compactor.record_judgments(
                due.backfill_id, [Judgment(request_id="e1/sam/heard/judgment", rating=1, reasoning="x")]
            )


class TestStayingUpPastTheLastCompaction:
    """The night that prompted settling up to the last compaction: compacted at 00:32 while
    work ran on (planned to 00:47, then getting ready for bed and sleep
    from 01:17), the next notes came at 01:56 and 02:55 -- during the
    planned night, but the user hadn't gone to bed."""

    def _events(self):
        return [
            event_at("22:22-23:07", id="dinner", summary="Dinner", priority=2),
            event_at("23:07-00:47+1", id="work", summary="Working", priority=2),
            event_at("00:47+1-01:17+1", id="gr", summary="Get ready for bed", priority=2),
            event_at("01:17+1-07:00+1", id="s0", summary="Sleep", priority=0, is_end_of_day_sleep=True),
            event_at("07:00+1-08:00+1", id="up", summary="Get up", priority=2),
            event_at("08:00+1-12:00+1", id="w1", summary="Work", priority=3),
            Event(id="s1", summary="Sleep", start=time_at("23:00+1"), end=time_at("07:00+1") + timedelta(days=1),
                  priority=0, is_end_of_day_sleep=True),
        ]

    def _setup(self):
        setup = Setup(
            [("01:56+1", "Restructuring goals"), ("02:55+1", "Getting ready for bed"), ("09:00+1", "up at last")],
            events=self._events(),
            now="09:30+1",
        )
        TestTheCompactionWindow._stamp_a_compaction_at(None, setup, "00:32+1")
        return setup

    def test_the_window_starts_at_the_last_compaction_not_the_first_note(self):
        setup = self._setup()

        context = setup.compactor.prepare()

        first, second = context.days
        assert first.compaction_window_start == time_at("00:32+1")
        assert first.note_ids == [setup.note_id(2), setup.note_id(3)]
        assert second.note_ids == [setup.note_id(4)]
        # The work going on at the last compaction, and the evening planned
        # after it, are offered -- and the work says how far it's settled.
        offered = {e.id: e for e in context.events}
        assert {"work", "gr", "s0"} <= set(offered)
        assert offered["work"].history_until == time_at("00:32+1")
        assert offered["gr"].history_until is None

    def test_the_work_runs_on_and_bedtime_moves_with_it(self):
        setup = self._setup()
        n2 = setup.note_id(3)

        planned = setup.compactor.dry_run([
            EventDecision(action="keep", event_id="work", end_note=n2),
            EventDecision(action="cancel", event_id="gr"),
            EventDecision(action="keep", event_id="s0", start=time_at("03:10+1")),
        ])

        changes = {c.event_id: c for c in planned.changes if c.event_id}
        assert (changes["work"].after.start, changes["work"].after.end) == (time_at("23:07"), time_at("02:55+1"))
        assert changes["gr"].action == "cancel"
        assert (changes["s0"].after.start, changes["s0"].after.end) == (time_at("03:10+1"), time_at("07:00+1"))

    @pytest.mark.parametrize(
        "decision, message",
        [
            (EventDecision(action="keep", event_id="work", end=time_at("00:20+1")), "going on until"),
            (EventDecision(action="keep", event_id="work", start=time_at("23:30")), "its start can't move"),
            (EventDecision(action="cancel", event_id="work"), "can't be cancelled"),
        ],
    )
    def test_what_the_last_compaction_settled_stays_settled(self, decision, message):
        setup = self._setup()

        with pytest.raises(CompactionError, match=message) as excinfo:
            setup.compactor.dry_run([decision])

        assert excinfo.value.categories == ["compacted"]

    def test_a_new_event_cant_reach_back_over_what_was_settled_before_the_window(self):
        setup = self._setup()

        with pytest.raises(CompactionError, match="runs back over 'Dinner'") as excinfo:
            setup.compactor.dry_run([
                EventDecision(action="create", summary="Snack", start=time_at("22:50"), end=time_at("23:05")),
            ])

        assert "overlap" in excinfo.value.categories


class TestFollowThrough:
    """An event the user says didn't happen counts against the
    follow-through of the people it was planned with: the timeline says so,
    and applying it records it (see utilities/cancellations.py)."""

    _TRAIT = Trait(id="reliable", name="Reliable", status="active", parts=[{"kind": "follow_through"}])

    def _setup(self):
        events = _day()
        events[0] = replace(events[0], facts=Facts(with_ids=["sam"]))
        return Setup(
            [("09:05", "email")], events=events, people=[Person(id="sam", name="Sam", status="active")],
            traits=[self._TRAIT],
        )

    def test_the_timeline_lists_whom_a_cancelled_event_counts_against(self):
        setup = self._setup()

        planned = setup.compactor.dry_run([EventDecision(action="cancel", event_id="e1")])

        assert "Follow-through:\n  ✗ Email: Me (Reliable), Sam (Reliable)\n" in planned.timeline.text
        assert "✗ counts against follow-through" in planned.timeline.text
        (email,) = [e for e in planned.timeline.events if e.event_id == "e1"]
        assert email.follow_through == ["Me (Reliable)", "Sam (Reliable)"]
        assert setup.cancellations.all() == []  # Nothing's recorded until it's applied.

    def test_applying_it_records_the_cancellation_for_each_of_them(self):
        setup = self._setup()
        planned = setup.compactor.dry_run([EventDecision(action="cancel", event_id="e1")])

        applied = setup.compactor.commit(planned.compaction_id)

        assert "Follow-through:" in applied.timeline.text
        assert [(r.id, r.summary, r.source) for r in setup.cancellations.all()] == [
            ("e1/sam/with", "Email", f"compaction {planned.compaction_id}"),
            ("e1/self/with", "Email", f"compaction {planned.compaction_id}"),
        ]

    def test_a_cancel_that_doesnt_count_is_neither_listed_nor_recorded(self):
        setup = self._setup()
        planned = setup.compactor.dry_run(
            [EventDecision(action="cancel", event_id="e1", counts_against_follow_through=False)]
        )

        setup.compactor.commit(planned.compaction_id)

        assert "Follow-through:" not in planned.timeline.text
        assert setup.cancellations.all() == []

    def test_the_proposal_tells_the_app_whether_a_cancel_counts_and_against_whom(self):
        setup = self._setup()
        planned = setup.compactor.dry_run([EventDecision(action="cancel", event_id="e1")])

        email = next(e for e in setup.compactor.get_proposal().events if e.id == "e1")

        assert (email.status, email.counts_against_follow_through) == ("cancelled", True)
        assert email.follow_through == ["Me (Reliable)", "Sam (Reliable)"]
        report = next(e for e in setup.compactor.get_proposal().events if e.id == "e2")
        assert (report.counts_against_follow_through, report.follow_through) == (None, [])

        # The user flips it by cancelling it again.
        flipped = setup.compactor.amend(
            planned.proposal_id, 1, [EventDecision(action="cancel", event_id="e1", counts_against_follow_through=False)]
        )

        email = next(e for e in flipped.events if e.id == "e1")
        assert (email.counts_against_follow_through, email.follow_through, email.decided_by) == (False, [], "user")

    def test_a_merge_isnt_a_cancellation(self):
        setup = self._setup()
        planned = setup.compactor.dry_run([EventDecision(action="merge", event_id="e1", into="e2")])

        setup.compactor.commit(planned.compaction_id)

        assert "Follow-through:" not in planned.timeline.text
        assert setup.cancellations.all() == []


class TestPrioritiesKept:
    """What compaction settles keeps the priority its actions gave it then."""

    def _setup(self):
        events = _day()
        events[0] = replace(events[0], priority=None, action_ids=["mail"])
        events[1] = replace(events[1], priority=None, action_ids=["mail"])
        events[2] = replace(events[2], priority=None, action_ids=["mail"])
        events[1].priority = 3  # Its own: kept.
        actions = [replace(_action("mail", "Do email"), priority=1)]
        return Setup([("09:05", "email")], events=events, actions=actions)

    def test_a_compacted_event_is_given_its_actions_priority_as_its_own(self):
        planned = self._setup().compactor.dry_run([])

        changes = {c.event_id: c for c in planned.changes if c.event_id}
        # Written even though it happened as planned.
        assert changes["e1"].after.priority == 1
        assert (changes["e1"].before.start, changes["e1"].before.end) == (
            changes["e1"].after.start, changes["e1"].after.end
        )
        assert "e2" not in changes  # Its own priority: nothing to write.
        # Lunch, still to come, keeps following its actions.
        assert "e3" not in changes or changes["e3"].after.priority is None


def _http_error(status):
    return HttpError(MagicMock(status=status), b"error")


def _keep(event_id, **fields):
    return EventDecision(action="keep", event_id=event_id, **fields)


class TestProposals:
    """A proposal: Claude proposes, the user edits it, leaves feedback or
    confirms it -- see docs/compaction-proposals.md."""

    def _proposed(self, decisions=None):
        setup = _standard()
        result = setup.compactor.dry_run(decisions if decisions is not None else setup.email_then_report())
        return setup, result.proposal_id

    @staticmethod
    def _event(proposal, event_id):
        return next(e for e in proposal.events if e.id == event_id)

    @staticmethod
    def _renamed(setup, event_index, summary):
        decisions = setup.email_then_report()
        decisions[event_index].summary = summary
        return decisions

    def test_a_day_that_went_to_plan_is_proposed_without_notes(self):
        setup = Setup([])
        setup.journal.start("prev", now=time_at("08:00"), note_ids=[], decisions=[], plan=CompactionPlan(changes=[]))
        setup.journal.set_status(setup.journal.load("prev"), STAMPED)

        assert setup.compactor.dry_run([]).status == "proposed"

        proposal = setup.compactor.get_proposal()
        assert (proposal.window_start, proposal.through) == (time_at("08:00"), time_at("11:30"))
        assert [(e.id, e.status) for e in proposal.events if e.id.startswith("e")] == [
            ("e1", "on_schedule"), ("e2", "on_schedule"), ("e3", "planned"),
        ]

    def test_prepare_hands_claude_the_open_proposal(self):
        setup, p = self._proposed()
        setup.compactor.amend(p, 1, [_keep("e1", summary="Inbox")])

        context = setup.compactor.prepare().proposal

        assert (context.id, context.revision, context.state, context.user_seq) == (p, 2, "awaiting_review", 1)
        assert [u["event_id"] for u in context.updates] == ["e1", "e2"]
        assert [e.edit for e in context.user_edits] == [{"action": "keep", "event_id": "e1", "summary": "Inbox"}]

    def test_the_status_summarizes_the_open_proposal(self):
        setup, p = self._proposed()
        setup.compactor.add_note(p, "one thing")

        summary = setup.compactor.proposal_summary()

        assert (summary.id, summary.revision, summary.state, summary.open_feedback) == (p, 1, "awaiting_claude", 1)
        assert summary.through == time_at("11:30")

    # -- the user's edits ------------------------------------------------------

    def test_an_edit_is_laid_over_claudes_decisions_field_by_field(self):
        setup, p = self._proposed()

        proposal = setup.compactor.amend(p, 1, [_keep("e1", summary="Inbox")])

        assert (proposal.revision, proposal.by, proposal.reason) == (2, "user", "user edit")
        email = self._event(proposal, "e1")
        assert (email.summary, email.start, email.end, email.decided_by) == (
            "Inbox", time_at("09:05"), time_at("10:20"), "user"
        )
        assert [(e.id, e.event_id, e.status, e.base_revision) for e in proposal.user_edits] == [
            (f"{p}u1", "e1", "active", 1)
        ]
        assert setup.journal.load(f"{p}r1").status == SUPERSEDED
        setup.client.update_event.assert_not_called()

    def test_an_edit_that_overlaps_is_refused_and_writes_nothing(self):
        setup, p = self._proposed()

        with pytest.raises(CompactionError, match="overlaps"):
            setup.compactor.amend(p, 1, [_keep("e1", end=time_at("10:40"))])

        assert setup.journal.revisions(p) == [(1, f"{p}r1", PROPOSED)]
        assert setup.journal.user_edits(p) == []

    def test_an_edit_changing_history_is_refused_unless_the_user_approved_it(self):
        # The last compaction ran at 09:30, as the email was going on.
        setup = Setup([])
        setup.journal.start("prev", now=time_at("09:30"), note_ids=[], decisions=[], plan=CompactionPlan(changes=[]))
        setup.journal.set_status(setup.journal.load("prev"), STAMPED)
        setup.compactor.dry_run([])
        p = setup.compactor.get_proposal().id

        with pytest.raises(CompactionError, match="its start can't move") as excinfo:
            setup.compactor.amend(p, 1, [_keep("e1", start=time_at("09:10"))])
        assert excinfo.value.categories == ["compacted"]

        proposal = setup.compactor.amend(p, 1, [_keep("e1", start=time_at("09:10"), allow_history=True)])

        assert self._event(proposal, "e1").start == time_at("09:10")
        assert proposal.user_edits[0].edit["allow_history"] is True
        # Kept with the edit: a revision planned again from it keeps to it.
        outcome = setup.compactor.confirm(p, proposal.revision)
        assert outcome.status == "applied"

    def test_an_edit_of_an_event_not_in_the_proposal_is_refused(self):
        setup, p = self._proposed()

        with pytest.raises(CompactionError, match="'nope'"):
            setup.compactor.amend(p, 1, [_keep("nope", summary="Something")])

    def test_as_planned_clears_claudes_decision(self):
        setup, p = self._proposed()

        proposal = setup.compactor.amend(p, 1, [], as_planned=["e1"])

        email = self._event(proposal, "e1")
        assert (email.start, email.end, email.status, email.decided_by) == (
            time_at("09:00"), time_at("10:00"), "on_schedule", None
        )

    def test_the_user_adds_an_event_and_edits_it_by_its_key(self):
        setup, p = self._proposed()
        walk = EventDecision(action="create", summary="Walk", start=time_at("11:00"), end=time_at("11:20"))

        first = setup.compactor.amend(p, 1, [walk])
        second = setup.compactor.amend(p, 2, [_keep(f"{p}u1", end=time_at("11:25"))])

        assert (self._event(first, f"{p}u1").status, self._event(first, f"{p}u1").decided_by) == ("new", "user")
        assert self._event(second, f"{p}u1").end == time_at("11:25")

    def test_the_user_edits_claudes_new_event_by_its_key_and_it_follows_the_key(self):
        setup = _standard()
        walk = EventDecision(action="create", summary="Walk", start=time_at("11:00"), end=time_at("11:20"))
        result = setup.compactor.dry_run(setup.email_then_report() + [walk])
        p = result.proposal_id
        key = f"{p}c1"
        assert [c.key for c in result.changes if c.action == "create"] == [key]

        proposal = setup.compactor.amend(p, 1, [_keep(key, summary="Long walk", end=time_at("11:25"))])

        assert (self._event(proposal, key).summary, self._event(proposal, key).end) == (
            "Long walk", time_at("11:25")
        )
        # Claude keeps it by sending its key back; the user's edit follows.
        assert setup.compactor.prepare().proposal.creates[0]["key"] == key
        revised = setup.compactor.dry_run(
            setup.email_then_report() + [replace(walk, key=key)], proposal_id=p, revision=2
        )
        (created,) = [c for c in revised.changes if c.action == "create"]
        assert (created.key, created.after.summary) == (key, "Long walk")

    def test_a_key_that_isnt_the_proposals_is_refused(self):
        setup, p = self._proposed()
        walk = EventDecision(
            action="create", summary="Walk", start=time_at("11:00"), end=time_at("11:20"), key=f"{p}c9"
        )

        with pytest.raises(CompactionError, match="isn't one of proposal"):
            setup.compactor.dry_run([walk], proposal_id=p, revision=1)

    def test_an_edit_from_an_older_revision_lands_on_the_current_one(self):
        setup, p = self._proposed()
        # Claude renames the report meanwhile.
        setup.compactor.dry_run(self._renamed(setup, 1, "Quarterly report"), proposal_id=p, revision=1)

        proposal = setup.compactor.amend(p, 1, [_keep("e2", summary="Report draft"), _keep("e1", summary="Inbox")])

        assert proposal.revision == 3
        assert (self._event(proposal, "e1").summary, self._event(proposal, "e2").summary) == (
            "Inbox", "Report draft"
        )
        assert proposal.replaced == ["e2"]

    def test_claudes_revision_keeps_the_users_edits(self):
        setup, p = self._proposed()
        setup.compactor.amend(p, 1, [_keep("e1", summary="Inbox")])

        # Claude started from revision 1, before the edit.
        result = setup.compactor.dry_run(self._renamed(setup, 0, "Mail"), proposal_id=p, revision=1)

        assert next(c for c in result.changes if c.event_id == "e1").after.summary == "Inbox"

    def test_changed_since_an_earlier_revision(self):
        setup, p = self._proposed()
        setup.compactor.amend(p, 1, [_keep("e1", summary="Inbox")])
        setup.compactor.amend(p, 2, [_keep("e2", summary="Draft")])

        assert setup.compactor.get_proposal(since_revision=1).changed_since == ["e1", "e2"]
        assert setup.compactor.get_proposal(since_revision=2).changed_since == ["e2"]

    # -- feedback ---------------------------------------------------------------

    def test_feedback_waits_for_claude_and_blocks_confirming(self):
        setup, p = self._proposed()

        item = setup.compactor.add_note(p, "the report was a draft", event_id="e2")

        assert (item.id, item.status, item.by) == (f"{p}f1", "open", "user")
        assert setup.compactor.get_proposal().state == "awaiting_claude"
        with pytest.raises(CompactionError, match="waiting for Claude"):
            setup.compactor.confirm(p, 1)

    def test_claude_answers_the_feedback_it_saw_with_a_revision(self):
        setup, p = self._proposed()
        setup.now = "11:31"
        item = setup.compactor.add_note(p, "call the report a draft", event_id="e2")
        setup.now = "11:32"
        setup.compactor.amend(p, 1, [_keep("e1", summary="Inbox")])  # Written after the note.
        decisions = self._renamed(setup, 1, "Report draft")

        with pytest.raises(CompactionError, match=f"answer every open feedback item.*{item.id}"):
            setup.compactor.dry_run(decisions, proposal_id=p, revision=2)
        setup.compactor.dry_run(
            decisions, proposal_id=p, revision=2, replies=[FeedbackReply(feedback_id=item.id, reply="Renamed it")]
        )

        proposal = setup.compactor.get_proposal()
        assert (proposal.revision, proposal.reason, proposal.state) == (3, "revised for notes", "awaiting_review")
        assert [(f.status, f.reply, f.answered_in) for f in proposal.feedback] == [("answered", "Renamed it", 3)]
        assert self._event(proposal, "e2").summary == "Report draft"

    def test_feedback_added_after_claude_began_may_stay_open(self):
        setup, p = self._proposed()
        setup.now = "11:31"
        setup.compactor.add_note(p, "one more thing")

        result = setup.compactor.dry_run(setup.email_then_report(), proposal_id=p, revision=1)

        assert "came in after your revision began" in result.message
        assert setup.compactor.get_proposal().state == "awaiting_claude"

    def test_a_note_can_be_withdrawn_but_not_once_answered(self):
        setup, p = self._proposed()
        first = setup.compactor.add_note(p, "never mind")
        assert setup.compactor.withdraw_note(first.id).status == "withdrawn"
        assert setup.compactor.withdraw_note(first.id).status == "withdrawn"
        setup.now = "11:31"
        second = setup.compactor.add_note(p, "rename the report")
        setup.compactor.dry_run(
            setup.email_then_report(), proposal_id=p, revision=1,
            replies=[FeedbackReply(feedback_id=second.id, reply="Kept it, it was the report")],
        )

        with pytest.raises(CompactionError, match="already answered, in revision 2"):
            setup.compactor.withdraw_note(second.id)
        assert setup.compactor.get_proposal().state == "awaiting_review"

    def test_claude_overrides_a_users_edit_only_answering_feedback_about_it(self):
        setup, p = self._proposed()
        setup.compactor.amend(p, 1, [_keep("e1", summary="Inbox")])
        decisions = self._renamed(setup, 0, "Mail")
        setup.compactor.dry_run(decisions, proposal_id=p, revision=2)
        assert self._event(setup.compactor.get_proposal(), "e1").summary == "Inbox"
        setup.now = "11:31"
        item = setup.compactor.add_note(p, "actually, call it Mail", event_id="e1")
        setup.now = "11:32"

        setup.compactor.dry_run(
            decisions, proposal_id=p, revision=3, replies=[FeedbackReply(feedback_id=item.id, reply="Renamed it")]
        )

        proposal = setup.compactor.get_proposal()
        assert self._event(proposal, "e1").summary == "Mail"
        assert [e.status for e in proposal.user_edits] == ["replaced"]

    def test_a_users_edit_made_after_claude_began_stands_over_its_answer(self):
        setup, p = self._proposed()
        setup.now = "11:31"
        item = setup.compactor.add_note(p, "call the email Mail", event_id="e1")
        setup.now = "11:32"
        setup.compactor.amend(p, 1, [_keep("e1", summary="Inbox")])

        # Claude started from revision 1, before the edit.
        setup.compactor.dry_run(
            self._renamed(setup, 0, "Mail"), proposal_id=p, revision=1,
            replies=[FeedbackReply(feedback_id=item.id, reply="Renamed it")],
        )

        proposal = setup.compactor.get_proposal()
        assert self._event(proposal, "e1").summary == "Inbox"
        assert proposal.feedback[0].superseded_by == [f"{p}u1"]
        assert [e.status for e in proposal.user_edits] == ["active"]

    # -- confirming ---------------------------------------------------------------

    def test_confirming_applies_it_and_history_runs_to_its_through(self):
        setup, p = self._proposed()

        result = setup.compactor.confirm(p, 1)

        assert (result.status, result.proposal.state) == ("applied", "applied")
        assert {c.args[0].id for c in setup.client.update_event.call_args_list} >= {"e1", "e2"}
        assert setup.journal.last_stamped_now() == time_at("11:30")
        assert setup.notes.read_with_rows() == []
        assert setup.journal.open_proposal() is None

    def test_only_the_current_revision_can_be_confirmed(self):
        setup, p = self._proposed()
        setup.compactor.amend(p, 1, [_keep("e1", summary="Inbox")])

        with pytest.raises(CompactionError, match="isn't proposal .* current one"):
            setup.compactor.confirm(p, 1)

        setup.client.update_event.assert_not_called()

    def test_a_change_since_it_was_proposed_makes_a_revision_to_confirm_again(self):
        setup, p = self._proposed()
        setup.append_note("10:40", "phone rang")

        result = setup.compactor.confirm(p, 1)

        assert result.status == "rechecked"
        assert (result.proposal.revision, result.proposal.by, result.proposal.reason) == (2, "server", "recheck")
        assert result.proposal.changed_since == ["e2"]
        setup.client.update_event.assert_not_called()
        assert setup.compactor.confirm(p, 2).status == "applied"

    def test_one_that_no_longer_plans_is_handed_to_claude(self):
        setup, p = self._proposed()
        setup.calendar.events.append(event_at("10:30-10:50", id="x1", summary="Call", priority=2))

        result = setup.compactor.confirm(p, 1)

        assert (result.status, result.proposal.state) == ("needs_claude", "awaiting_claude")
        assert [f.by for f in result.proposal.feedback] == ["server"]
        setup.client.update_event.assert_not_called()

    def test_an_apply_that_stops_partway_is_finished_later(self):
        setup, p = self._proposed()
        calls = []

        def update(event):
            calls.append(event.id)
            if len(calls) == 2:
                raise _http_error(500)

        setup.client.update_event.side_effect = update

        with pytest.raises(CompactionError, match="finish_proposal"):
            setup.compactor.confirm(p, 1)
        assert setup.compactor.proposal_summary().state == "applying"
        with pytest.raises(CompactionError, match="being applied"):
            setup.compactor.amend(p, 1, [_keep("e1", summary="Inbox")])
        setup.client.update_event.side_effect = None

        assert setup.compactor.finish(p).status == "applied"
        assert setup.journal.open_proposal() is None

    def test_a_write_that_can_never_succeed_proposes_whats_left(self):
        setup, p = self._proposed()

        def update(event):
            if event.id == "e2":
                # Deleted meanwhile.
                setup.calendar.events = [e for e in setup.calendar.events if e.id != "e2"]
                raise _http_error(404)

        setup.client.update_event.side_effect = update

        result = setup.compactor.confirm(p, 1)

        assert result.status == "rebuilt"
        assert setup.journal.load(f"{p}r1").status == FAILED
        proposal = result.proposal
        assert (proposal.revision, proposal.by, proposal.reason, proposal.state) == (
            2, "server", "apply failed", "awaiting_review"
        )
        assert "e2" not in {e.id for e in proposal.events}
        assert "leaving out what was about e2" in proposal.warnings[0]
        assert setup.compactor.confirm(p, 2).status == "applied"

    def test_a_proposal_can_be_abandoned(self):
        setup, p = self._proposed()

        assert setup.compactor.abandon(p).status == "abandoned"

        assert setup.journal.open_proposal() is None
        assert setup.compactor.dry_run(setup.email_then_report()).proposal_id != p

    def test_only_the_last_few_superseded_revisions_are_kept_whole(self):
        setup, p = self._proposed()
        for revision in range(1, 6):
            setup.compactor.dry_run(setup.email_then_report(), proposal_id=p, revision=revision)

        setup.journal.garbage_collect()

        # 1-5 superseded, 6 current: the newest three superseded kept whole,
        # the rest only their compaction rows.
        assert [bool(setup.journal.load(f"{p}r{n}").steps) for n in range(1, 7)] == [
            False, False, True, True, True, True,
        ]
        assert setup.journal.revision_meta(p, 1).revision == 1


class TestExtendingAProposal:
    """The user extends the open proposal past where Claude's revision ran
    to: the notes it takes in are added where they fall."""

    def _extendable(self):
        setup = _standard()
        p = setup.compactor.dry_run(setup.email_then_report()).proposal_id
        # Later, a note after the proposal's end.
        setup.append_note("12:05", "lunch")
        setup.now = "12:30"
        return setup, p

    @staticmethod
    def _note(proposal, note_id):
        return next(n for n in proposal.notes if n.id == note_id)

    def test_it_runs_on_and_takes_in_the_notes_there(self):
        setup, p = self._extendable()

        proposal = setup.compactor.amend(p, 1, [], through=time_at("12:15"))

        assert (proposal.through, proposal.claude_through) == (time_at("12:15"), time_at("11:30"))
        lunch = self._note(proposal, setup.note_id(4))
        assert (lunch.use, lunch.event_id, lunch.decided_by) == ("annotates", "e3", "user")
        assert [(e.event_id, e.edit) for e in proposal.user_edits] == [
            (setup.note_id(4), {"action": "note", "use": "annotate"})
        ]
        # Kept as the revision's end, as it's planned again.
        again = setup.compactor.get_proposal()
        assert (again.through, again.claude_through) == (time_at("12:15"), time_at("11:30"))

    def test_a_note_the_user_says_otherwise_about_is_left_as_they_say(self):
        setup, p = self._extendable()

        proposal = setup.compactor.amend(
            p, 1, [], notes=[NoteEdit(note_id=setup.note_id(4), use="ignore")], through=time_at("12:15")
        )

        assert self._note(proposal, setup.note_id(4)).use == "ignored"

    def test_with_no_notes_there_it_still_runs_on(self):
        setup = _standard()
        p = setup.compactor.dry_run(setup.email_then_report()).proposal_id
        setup.now = "12:30"

        proposal = setup.compactor.amend(p, 1, [], through=time_at("12:15"))

        assert proposal.through == time_at("12:15")
        assert proposal.user_edits == []

    def test_it_can_only_be_extended_and_never_past_now(self):
        setup, p = self._extendable()

        with pytest.raises(CompactionError, match="only be extended"):
            setup.compactor.amend(p, 1, [], through=time_at("11:00"))
        with pytest.raises(CompactionError, match="past now"):
            setup.compactor.amend(p, 1, [], through=time_at("13:00"))
        assert setup.journal.revisions(p) == [(1, f"{p}r1", PROPOSED)]

    def test_claudes_next_revision_runs_to_now(self):
        setup, p = self._extendable()
        setup.compactor.amend(p, 1, [], through=time_at("12:15"))

        setup.compactor.dry_run(setup.email_then_report(), proposal_id=p, revision=2)

        proposal = setup.compactor.get_proposal()
        assert proposal.through == proposal.claude_through == time_at("12:30")


class TestProposalNotes:
    """What notes are for: Claude ignores a note, or adds it to a
    particular event; the user's note edits override that."""

    def _ignoring_the_email_note(self):
        setup = _standard()
        result = setup.compactor.dry_run([], ignore_notes=[setup.note_id(2)])
        return setup, result.proposal_id

    @staticmethod
    def _note(proposal, note_id):
        return next(n for n in proposal.notes if n.id == note_id)

    @staticmethod
    def _event(proposal, event_id):
        return next(e for e in proposal.events if e.id == event_id)

    def test_the_proposal_says_what_each_note_is_for_and_who_said_so(self):
        setup, p = self._ignoring_the_email_note()

        proposal = setup.compactor.get_proposal()

        email, report = self._note(proposal, setup.note_id(2)), self._note(proposal, setup.note_id(3))
        assert (email.use, email.event_id, email.decided_by) == ("ignored", None, "claude")
        assert (report.use, report.event_id, report.decided_by) == ("annotates", "e2", None)

    def test_the_user_annotates_a_note_claude_ignored(self):
        setup, p = self._ignoring_the_email_note()

        proposal = setup.compactor.amend(p, 1, [], notes=[NoteEdit(note_id=setup.note_id(2), use="annotate")])

        email = self._note(proposal, setup.note_id(2))
        assert (email.use, email.event_id, email.decided_by) == ("annotates", "e1", "user")
        assert self._event(proposal, "e1").description == "Notes:\n- 09:05 email"
        assert [e.edit for e in proposal.user_edits] == [{"action": "note", "use": "annotate"}]

    def test_the_user_adds_a_note_to_another_event(self):
        setup, p = self._ignoring_the_email_note()

        proposal = setup.compactor.amend(
            p, 1, [], notes=[NoteEdit(note_id=setup.note_id(2), use="annotate", event_id="e2")]
        )

        assert self._note(proposal, setup.note_id(2)).event_id == "e2"
        assert self._event(proposal, "e2").description == "Notes:\n- 09:05 email\n- 10:20 report"

    def test_annotating_an_edge_note_adds_it_to_its_event_and_keeps_the_edge(self):
        setup = _standard()
        p = setup.compactor.dry_run(setup.email_then_report()).proposal_id
        before = self._note(setup.compactor.get_proposal(), setup.note_id(2))
        assert (before.use, before.event_id, before.edge_of) == ("edge", "e1", "e1")

        proposal = setup.compactor.amend(p, 1, [], notes=[NoteEdit(note_id=setup.note_id(2), use="annotate")])

        email = self._note(proposal, setup.note_id(2))
        assert (email.use, email.event_id, email.edge_of, email.decided_by) == ("annotates", "e1", "e1", "user")
        email_event = self._event(proposal, "e1")
        assert (email_event.start, email_event.description) == (time_at("09:05"), "Notes:\n- 09:05 email")

    def test_an_edge_note_added_to_another_event_still_sets_its_edge(self):
        setup = _standard()
        p = setup.compactor.dry_run(setup.email_then_report()).proposal_id

        proposal = setup.compactor.amend(
            p, 1, [], notes=[NoteEdit(note_id=setup.note_id(2), use="annotate", event_id="e2")]
        )

        email = self._note(proposal, setup.note_id(2))
        assert (email.use, email.event_id, email.edge_of) == ("annotates", "e2", "e1")
        assert self._event(proposal, "e1").start == time_at("09:05")
        assert self._event(proposal, "e2").description == "Notes:\n- 09:05 email"

    def test_as_planned_puts_a_note_back_as_claude_had_it(self):
        setup, p = self._ignoring_the_email_note()
        setup.compactor.amend(p, 1, [], notes=[NoteEdit(note_id=setup.note_id(2), use="annotate")])

        proposal = setup.compactor.amend(p, 2, [], as_planned=[setup.note_id(2)])

        assert self._note(proposal, setup.note_id(2)).use == "ignored"

    def test_claudes_revision_keeps_the_users_note_edits(self):
        setup, p = self._ignoring_the_email_note()
        setup.compactor.amend(p, 1, [], notes=[NoteEdit(note_id=setup.note_id(2), use="annotate")])

        setup.compactor.dry_run([], ignore_notes=[setup.note_id(2)], proposal_id=p, revision=2)

        assert self._note(setup.compactor.get_proposal(), setup.note_id(2)).use == "annotates"
        assert setup.compactor.prepare().proposal.ignore_notes == [setup.note_id(2)]

    def test_a_note_edit_naming_a_note_or_event_that_isnt_there_is_refused(self):
        setup, p = self._ignoring_the_email_note()

        with pytest.raises(CompactionError, match="aren't in the proposal"):
            setup.compactor.amend(p, 1, [], notes=[NoteEdit(note_id="2026-01-01T08:00:00+00:00#9", use="ignore")])
        with pytest.raises(CompactionError, match="aren't in the proposal"):
            setup.compactor.amend(p, 1, [], notes=[NoteEdit(note_id=setup.note_id(2), use="annotate", event_id="nope")])
        assert setup.journal.user_edits(p) == []

    def test_claude_adds_a_note_to_a_particular_event(self):
        setup = _standard()

        result = setup.compactor.dry_run([], annotate_notes=[NoteAnnotation(note_id=setup.note_id(2), event_id="e2")])

        assert next(c for c in result.changes if c.event_id == "e2").after.description == (
            "Notes:\n- 09:05 email\n- 10:20 report"
        )
        context = setup.compactor.prepare().proposal
        assert context.annotate_notes == [{"note_id": setup.note_id(2), "event_id": "e2"}]
        email = self._note(setup.compactor.get_proposal(), setup.note_id(2))
        assert (email.use, email.decided_by) == ("annotates", "claude")

    def test_claude_may_change_a_users_note_edit_answering_feedback_about_the_note(self):
        setup, p = self._ignoring_the_email_note()
        setup.compactor.amend(p, 1, [], notes=[NoteEdit(note_id=setup.note_id(2), use="annotate")])
        setup.now = "11:31"
        item = setup.compactor.add_note(p, "put this with the report", note_id=setup.note_id(2))
        setup.now = "11:32"

        setup.compactor.dry_run(
            [],
            annotate_notes=[NoteAnnotation(note_id=setup.note_id(2), event_id="e2")],
            proposal_id=p,
            revision=2,
            replies=[FeedbackReply(feedback_id=item.id, reply="Moved it to the report")],
        )

        proposal = setup.compactor.get_proposal()
        assert self._note(proposal, setup.note_id(2)).event_id == "e2"
        assert [e.status for e in proposal.user_edits] == ["replaced"]
        assert proposal.feedback[0].note_id == setup.note_id(2)

    def test_confirming_writes_the_note_where_the_user_put_it(self):
        setup, p = self._ignoring_the_email_note()
        setup.compactor.amend(p, 1, [], notes=[NoteEdit(note_id=setup.note_id(2), use="annotate", event_id="e2")])

        assert setup.compactor.confirm(p, 2).status == "applied"

        patches = {c.args[0].id: c.args[0] for c in setup.client.update_event.call_args_list}
        assert patches["e2"].description == "Notes:\n- 09:05 email\n- 10:20 report"


class TestSettlingAdditions:
    """The user settles what a proposal adds -- create it now, say it's
    one already there, or drop it -- without confirming the rest."""

    def _proposed(self):
        setup = Setup(
            [("09:00", None), ("09:30", None)],
            people=[Person(id="sam", name="Sam", status="active")],
        )
        decisions = setup.coffee_instead_of_email()
        decisions[0].action_ids = ["new:coffee"]
        decisions[0].facts = Facts(
            location_id="new:cafe", with_ids=["sam", "new:alex"], notes={"sam": "tired", "new:alex": "new in town"}
        )
        self.decisions = decisions
        self.additions = dict(
            new_actions=[NewAction(ref="new:coffee", name="Drink coffee")],
            new_people=[NewPerson(ref="new:alex", name="Alex", context="Sam's friend")],
            new_locations=[NewLocation(ref="new:cafe", name="Corner cafe", hint="the cafe on Main")],
        )
        result = setup.compactor.dry_run(decisions, **self.additions)
        self.key = next(c.key for c in result.changes if c.action == "create")
        return setup, result.proposal_id

    def _coffee(self, proposal):
        return next(e for e in proposal.events if e.id == self.key)

    def test_creating_a_person_now_names_them_by_id_from_then_on(self):
        setup, p = self._proposed()

        proposal = setup.compactor.amend(
            p, 1, [], additions=[AdditionChoice(ref="new:alex", use="create", context="Sam's college friend")]
        )

        alex = setup.people.get_person("alex")
        assert (alex.context, alex.status) == ("Sam's college friend", "active")
        assert proposal.settled_additions == [AdditionSettled(ref="new:alex", use="create", id=alex.id)]
        assert "people" not in (proposal.additions or {})
        facts = self._coffee(proposal).facts
        assert facts.with_ids == ["sam", alex.id]
        assert facts.notes == {"sam": "tired", alex.id: "new in town"}
        setup.client.create_event.assert_not_called()

    def test_one_already_there_is_named_by_its_id(self):
        setup, p = self._proposed()

        proposal = setup.compactor.amend(p, 1, [], additions=[AdditionChoice(ref="new:alex", use="existing", id="sam")])

        facts = self._coffee(proposal).facts
        # Sam was there already, and keeps their own note.
        assert (facts.with_ids, facts.notes) == (["sam"], {"sam": "tired"})
        assert [p.name for p in setup.people.all() if p.name == "Alex"] == []

    def test_a_dropped_one_is_left_out_of_the_events(self):
        setup, p = self._proposed()

        proposal = setup.compactor.amend(p, 1, [], additions=[AdditionChoice(ref="new:cafe", use="drop")])

        assert self._coffee(proposal).facts.location_id is None
        assert "locations" not in (proposal.additions or {})
        assert proposal.settled_additions == [AdditionSettled(ref="new:cafe", use="drop")]

    def test_an_action_approved_now_is_created_active(self):
        setup, p = self._proposed()

        proposal = setup.compactor.amend(p, 1, [], additions=[AdditionChoice(ref="new:coffee", use="create")])

        (coffee,) = setup.actions.all()
        assert (coffee.name, coffee.status) == ("Drink coffee", "active")
        assert self._coffee(proposal).action_ids == [coffee.id]

    def test_claudes_revision_keeps_what_the_user_settled(self):
        setup, p = self._proposed()
        setup.compactor.amend(p, 1, [], additions=[AdditionChoice(ref="new:alex", use="create")])
        alex = setup.people.get_person("alex")

        context = setup.compactor.prepare().proposal
        assert [n["ref"] for n in context.new_people] == ["new:alex"]
        assert context.settled_additions == [AdditionSettled(ref="new:alex", use="create", id=alex.id)]
        # Claude sends it all again: Alex isn't refused as already there.
        revised = setup.compactor.dry_run(self.decisions, proposal_id=p, revision=2, **self.additions)

        created = next(c for c in revised.changes if c.action == "create")
        assert facts_from_dict(created.after.facts).with_ids == ["sam", alex.id]
        assert "people" not in (revised.additions or {})

    def test_confirming_creates_only_whats_left(self):
        setup, p = self._proposed()
        setup.compactor.amend(p, 1, [], additions=[AdditionChoice(ref="new:alex", use="create")])

        assert setup.compactor.confirm(p, 2).status == "applied"

        assert [person.name for person in setup.people.all()].count("Alex") == 1
        assert [a.name for a in setup.actions.all()] == ["Drink coffee"]
        created = setup.client.create_event.call_args.args[0]
        assert created.facts.with_ids == ["sam", setup.people.get_person("alex").id]

    def test_as_planned_on_a_ref_leaves_it_unsettled(self):
        setup, p = self._proposed()
        setup.compactor.amend(p, 1, [], additions=[AdditionChoice(ref="new:cafe", use="drop")])

        proposal = setup.compactor.amend(p, 2, [], as_planned=["new:cafe"])

        assert proposal.settled_additions == []
        assert [loc["ref"] for loc in proposal.additions["locations"]] == ["new:cafe"]
        assert self._coffee(proposal).facts.location_id == "new:cafe"

    @pytest.mark.parametrize(
        "choice, message",
        [
            (AdditionChoice(ref="new:nobody", use="drop"), "isn't something the proposal adds"),
            (AdditionChoice(ref="new:alex", use="existing", id="nope"), "there's no person 'nope'"),
            (AdditionChoice(ref="new:alex", use="create", id="sam"), "only 'existing' takes an id"),
        ],
    )
    def test_a_choice_that_cant_be_made_is_refused(self, choice, message):
        setup, p = self._proposed()

        with pytest.raises(CompactionError, match=message):
            setup.compactor.amend(p, 1, [], additions=[choice])

        assert setup.journal.user_edits(p) == []

    def test_nothing_is_created_for_an_amend_thats_refused(self):
        setup, p = self._proposed()

        with pytest.raises(CompactionError):
            setup.compactor.amend(
                p, 1, [EventDecision(action="keep", event_id="nope", summary="x")],
                additions=[AdditionChoice(ref="new:alex", use="create")],
            )

        assert [person.name for person in setup.people.all()] == ["Me", "Sam"]


class TestEditingAProposalsEventFields:
    """The user sets an event's whole description, its location or its
    priority in a proposal, as update_event would on the calendar."""

    def _proposed(self, decisions=None):
        setup = _standard()
        result = setup.compactor.dry_run(decisions or [])
        return setup, result.proposal_id

    @staticmethod
    def _event(proposal, event_id):
        return next(e for e in proposal.events if e.id == event_id)

    @staticmethod
    def _note(proposal, note_id):
        return next(n for n in proposal.notes if n.id == note_id)

    def test_a_description_the_user_writes_is_the_last_word(self):
        setup, p = self._proposed()
        assert self._event(setup.compactor.get_proposal(), "e1").description == "Notes:\n- 09:05 email"

        proposal = setup.compactor.amend(p, 1, [_keep("e1", description="Inbox zero")])

        assert self._event(proposal, "e1").description == "Inbox zero"
        email = self._note(proposal, setup.note_id(2))
        assert (email.use, email.decided_by) == ("ignored", None)

    def test_a_note_the_users_description_keeps_still_counts_as_added(self):
        setup, p = self._proposed()

        proposal = setup.compactor.amend(p, 1, [_keep("e1", description="Inbox zero\n\nNotes:\n- 09:05 email")])

        assert self._event(proposal, "e1").description == "Inbox zero\n\nNotes:\n- 09:05 email"
        assert self._note(proposal, setup.note_id(2)).use == "annotates"

    def test_a_note_counts_as_kept_only_by_its_exact_line(self):
        setup, p = self._proposed()

        proposal = setup.compactor.amend(p, 1, [_keep("e1", description="Cleared my email backlog")])

        assert self._note(proposal, setup.note_id(2)).use == "ignored"
        assert [e.edit["dropped_notes"] for e in proposal.user_edits] == [[setup.note_id(2)]]

    def test_a_note_annotated_after_the_description_is_added_below_it(self):
        setup, p = self._proposed()
        setup.compactor.amend(p, 1, [_keep("e1", description="Inbox zero")])

        proposal = setup.compactor.amend(p, 2, [], notes=[NoteEdit(note_id=setup.note_id(2), use="annotate")])

        assert self._event(proposal, "e1").description == "Inbox zero\n\nNotes:\n- 09:05 email"
        assert self._note(proposal, setup.note_id(2)).use == "annotates"

    def test_a_note_written_later_is_added_below_it_and_the_left_out_one_stays_out(self):
        setup, p = self._proposed()
        setup.compactor.amend(p, 1, [_keep("e1", description="Inbox zero")])
        setup.append_note("09:30", "phone rang")

        proposal = setup.compactor.get_proposal()

        assert self._event(proposal, "e1").description == "Inbox zero\n\nNotes:\n- 09:30 phone rang"
        assert self._note(proposal, setup.note_id(2)).use == "ignored"
        # Confirming plans it again with the new note, to confirm that.
        result = setup.compactor.confirm(p, 2)
        assert result.status == "rechecked"
        assert self._event(result.proposal, "e1").description == "Inbox zero\n\nNotes:\n- 09:30 phone rang"

    def test_an_explicit_note_edit_in_the_same_call_wins(self):
        setup, p = self._proposed()

        proposal = setup.compactor.amend(
            p, 1, [_keep("e1", description="Inbox zero\n\nNotes:\n- 09:05 email")],
            notes=[NoteEdit(note_id=setup.note_id(2), use="ignore")],
        )

        assert self._note(proposal, setup.note_id(2)).use == "ignored"

    def test_what_it_left_out_stays_out_through_claudes_revisions(self):
        setup, p = self._proposed()
        setup.compactor.amend(p, 1, [_keep("e1", description="Inbox zero")])

        setup.compactor.dry_run([], proposal_id=p, revision=2)

        proposal = setup.compactor.get_proposal()
        assert self._event(proposal, "e1").description == "Inbox zero"
        assert self._note(proposal, setup.note_id(2)).use == "ignored"

    def test_it_replaces_claudes_annotate_text_too(self):
        setup, p = self._proposed([_keep("e1", annotate="mostly replies")])

        proposal = setup.compactor.amend(p, 1, [_keep("e1", description="Inbox zero")])

        assert self._event(proposal, "e1").description == "Inbox zero"

    def test_location_and_priority_are_set_and_written_on_confirming(self):
        setup, p = self._proposed()

        proposal = setup.compactor.amend(p, 1, [_keep("e2", location="Office", priority=3)])

        report = self._event(proposal, "e2")
        assert (report.location, report.priority) == ("Office", 3)
        assert setup.compactor.confirm(p, 2).status == "applied"
        patches = {c.args[0].id: c.args[0] for c in setup.client.update_event.call_args_list}
        assert (patches["e2"].location, patches["e2"].priority) == ("Office", 3)

    def test_a_description_too_long_for_calendar_is_refused(self):
        setup, p = self._proposed()

        with pytest.raises(CompactionError, match="over the 8192 Calendar keeps"):
            setup.compactor.amend(p, 1, [_keep("e1", description="x" * 9000)])

    def test_a_cleared_description_is_written_empty(self):
        setup, p = self._proposed()
        setup.compactor.amend(p, 1, [_keep("e1", description="")])

        setup.compactor.confirm(p, 2)

        patches = {c.args[0].id: c.args[0] for c in setup.client.update_event.call_args_list}
        assert patches["e1"].description == ""
