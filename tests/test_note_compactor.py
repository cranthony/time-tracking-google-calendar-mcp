import contextlib
from dataclasses import replace
from datetime import timedelta, timezone
from unittest.mock import MagicMock

import pytest

import config
import server
from googleapiclient.errors import HttpError

from calendar_clients.google_calendar import Event, EventLabel
from calendar_clients.google_sheets import SheetsClient, cached_sheet_reads
from tests.event_time_helpers import event_at, time_at
from tests.fake_sheets import FakeSheets, FakeSheetsService
from utilities import calendar_metadata_sheet
from utilities.compaction_journal import ABANDONED, APPLYING, PLANNED, STAMPED, CompactionJournal
from utilities import note_compactor
from utilities.note_compaction import (
    CompactionChange,
    CompactionError,
    CompactionPlan,
    EventDecision,
    EventState,
)
from utilities.note_compactor import NoteCompactor
from utilities.goal_sheet import Goal
from utilities.goals import GoalTree
from utilities.noted_time_sheet import NotedTime, NotedTimeSheet

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
    back fresh copies each call, like the real label-aware view."""

    def __init__(self, events):
        self.events = events

    def list_events(self, time_min, time_max):
        # Like the Calendar API: everything overlapping the range.
        return sorted(
            (replace(e) for e in self.events if e.end > time_min and e.start < time_max),
            key=lambda e: e.start,
        )


class Setup:
    def __init__(self, notes, events=None, now="11:30", goals=None):
        self.sheets = FakeSheets()
        self.sheets.write_rows_in_sheet(
            "s", _NOTES_TAB, "A1:C1", [["timestamp", "description", "compaction_id"]]
        )
        self.notes = NotedTimeSheet(self.sheets, "s", _NOTES_TAB)
        for at, description in notes:
            self.append_note(at, description)
        self.journal = CompactionJournal(self.sheets, "s", _JOURNAL_TAB)
        self.calendar = FakeCalendar(events if events is not None else _day())
        self.client = MagicMock()
        self.now = now
        self.goals = None
        if goals is not None:
            self.goals = MagicMock()
            self.goals.tree.return_value = GoalTree(goals)
        self.compactor = NoteCompactor(
            calendar=self.calendar,
            client=self.client,
            notes=self.notes,
            journal=self.journal,
            clock=lambda: time_at(self.now),
            goals=self.goals,
            marker=self.marker,
        )

    @property
    def marker(self):
        if not hasattr(self, "_marker"):
            self._marker = MagicMock()
        return self._marker

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

    def test_only_offers_one_day_at_a_time(self):
        setup = _standard()
        setup.append_note("08:00+1", "next day")
        setup.now = "09:00+1"

        context = setup.compactor.prepare()

        assert [n.id for n in context.notes] == [setup.note_id(2), setup.note_id(3)]
        assert context.remaining_note_count == 1

    def test_the_effective_now_never_passes_the_end_of_the_day(self):
        setup = _standard()
        setup.now = "12:00+1"

        assert setup.compactor.prepare().now == time_at("07:00+1")

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

    def test_a_compaction_before_the_day_started_does_not_reach_back_past_it(self):
        setup = self._setup()
        self._stamp_a_compaction_at(setup, "19:45")

        context = setup.compactor.prepare()

        assert context.compaction_window_start == time_at("07:00+1")
        assert [e.id for e in context.events] == ["s0", "gr", "w1", "w2", "s1"]

    def test_a_later_compaction_the_same_day_starts_the_window_there(self):
        # Compacted at 10:05, just after work ended -- so work is offered
        # too, to be stretched if it ran late, but nothing before it is.
        setup = self._setup(note_at="10:20+1")
        self._stamp_a_compaction_at(setup, "10:05+1")

        context = setup.compactor.prepare()

        assert context.compaction_window_start == time_at("10:05+1")
        assert [e.id for e in context.events] == ["w1", "w2", "s1"]

    def test_the_event_just_before_the_window_can_be_stretched(self):
        setup = self._setup(note_at="10:20+1")
        self._stamp_a_compaction_at(setup, "10:05+1")

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

    def test_the_last_compacted_note_just_before_the_window_is_offered_as_context(self):
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

    def test_a_compacted_note_longer_before_the_window_is_not_offered(self):
        setup = self._setup(note_at="10:20+1")
        self._compacted_note(setup, "09:45+1", "starting email")
        self._stamp_a_compaction_at(setup, "10:05+1")

        assert setup.compactor.prepare().previous_note is None

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
            assert "10:05  ┄" in timeline.text and "┄┄ last compaction" in timeline.text

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

        assert result.status == "planned"
        assert result.compaction_id
        assert {c.event_id for c in result.changes} == {"e1", "e2"}
        assert setup.journal.load(result.compaction_id).status == PLANNED
        setup.client.update_event.assert_not_called()
        setup.client.create_event.assert_not_called()
        assert [n.id for n in setup.notes.read_with_rows()] == [setup.note_id(2), setup.note_id(3)]

    def test_tells_the_model_to_wait_for_explicit_approval_before_applying(self):
        # The server can't tell whether the user replied, so both the
        # instructions and the dry run's own message have to say it.
        setup = _standard()

        context = setup.compactor.prepare()
        result = setup.compactor.dry_run(setup.email_then_report())

        for text in (context.instructions, result.message):
            assert "STOP and wait for the user's reply" in text
            assert "explicitly approved this plan after seeing it" in text
            assert "isn't approval" in text

    def test_returns_the_resulting_timeline_to_show_the_user(self):
        setup = _standard()

        result = setup.compactor.dry_run(setup.email_then_report())

        email = next(e for e in result.timeline.events if e.event_id == "e1")
        assert email.status == "adjusted"
        assert (email.start_note, email.end_note) == (setup.note_id(2), setup.note_id(3))
        assert "two lanes" in result.message
        assert "└ Email ends · 20m late (planned 10:00)" in result.timeline.text

    def test_with_no_decisions_every_past_event_is_recorded_on_schedule(self):
        setup = _standard()

        result = setup.compactor.dry_run([])

        by_event = {c.event_id: c for c in result.changes}
        assert set(by_event) == {"e1", "e2"}
        assert by_event["e1"].after.is_fixed_time is True
        assert (by_event["e1"].after.start, by_event["e1"].after.end) == (time_at("09:00"), time_at("10:00"))

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

    def test_a_planned_compaction_does_not_block_a_new_one_but_is_replaced_by_it(self):
        setup = _standard()
        first = setup.compactor.dry_run(setup.email_then_report())

        second = setup.compactor.dry_run(setup.email_then_report())

        assert second.status == "planned"
        assert second.compaction_id != first.compaction_id
        assert setup.journal.load(first.compaction_id).status == ABANDONED
        assert setup.journal.load(second.compaction_id).status == PLANNED
        assert "Replaced 1 earlier unapplied plan" in second.message

    def test_a_replaced_plan_can_no_longer_be_committed(self):
        setup = _standard()
        first = setup.compactor.dry_run(setup.email_then_report())
        setup.compactor.dry_run(setup.email_then_report())

        with pytest.raises(CompactionError, match="abandoned"):
            setup.compactor.commit(first.compaction_id)

        setup.client.update_event.assert_not_called()

    def test_a_rejected_plan_can_be_redone_with_corrected_decisions(self):
        setup = _standard()
        setup.compactor.dry_run(setup.email_then_report())

        # The user says the report never happened -- redo it without
        # touching the calendar or calling prepare again.
        revised = setup.compactor.dry_run(
            [
                EventDecision(action="keep", event_id="e1", start_note=setup.note_id(2), end_note=setup.note_id(3)),
                EventDecision(action="cancel", event_id="e2"),
            ]
        )

        by_event = {c.event_id: c for c in revised.changes}
        assert by_event["e1"].after.end == time_at("10:20")
        assert by_event["e2"].action == "cancel"
        setup.client.update_event.assert_not_called()

    def test_a_plan_that_fails_validation_leaves_the_earlier_plan_alone(self):
        setup = _standard()
        first = setup.compactor.dry_run(setup.email_then_report())

        with pytest.raises(CompactionError):
            setup.compactor.dry_run([EventDecision(action="keep", event_id="nope")])

        assert setup.journal.load(first.compaction_id).status == PLANNED

    def test_a_rejected_plan_can_be_redone_with_a_rename_and_a_note(self):
        setup = _standard()
        setup.compactor.dry_run(setup.email_then_report())

        decisions = setup.email_then_report()
        decisions[0].summary = "Deep work"
        decisions[0].annotate = "phone rang"
        revised = setup.compactor.dry_run(decisions)

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
        assert patches["e1"].is_fixed_time is True
        # Only what changed: the report still ended as planned.
        assert (patches["e2"].start, patches["e2"].end) == (time_at("10:20"), None)
        journal = setup.journal.load(planned.compaction_id)
        assert journal.status == STAMPED
        assert all(s.status == "done" for s in journal.steps)
        assert setup.notes.read_with_rows() == []
        assert {n.note.compaction_id for n in setup.notes.read_with_rows(include_compacted=True)} == {
            planned.compaction_id
        }

    def test_the_patch_carries_only_what_changed_plus_the_pin(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())

        setup.compactor.commit(planned.compaction_id)

        patch = next(
            c.args[0] for c in setup.client.update_event.call_args_list if c.args[0].id == "e1"
        )
        assert patch.summary is None and patch.description is None
        assert patch.min_duration is not None

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

    def test_moves_the_last_compaction_marker_to_its_time_once_stamped(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())
        setup.marker.mark.assert_not_called()  # Not for a dry run.

        setup.compactor.commit(planned.compaction_id)

        setup.marker.mark.assert_called_once_with(time_at("11:30"))

    def test_a_marker_that_cant_be_moved_doesnt_fail_the_compaction(self):
        setup = _standard()
        setup.marker.mark.side_effect = RuntimeError("calendar unavailable")
        planned = setup.compactor.dry_run(setup.email_then_report())

        result = setup.compactor.commit(planned.compaction_id)

        assert result.status == "applied"
        assert any("couldn't move the last-compaction marker" in w for w in result.warnings)
        assert setup.journal.load(planned.compaction_id).status == "stamped"

    def test_committing_again_moves_the_marker_again(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())
        setup.marker.mark.side_effect = [RuntimeError("down"), None]
        setup.compactor.commit(planned.compaction_id)

        again = setup.compactor.commit(planned.compaction_id)

        assert again.warnings == []
        assert setup.marker.mark.call_args_list[-1].args == (time_at("11:30"),)

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


class TestDescribeAndAbandon:
    def test_describing_returns_the_stored_plan_without_applying_it(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())

        described = setup.compactor.describe(planned.compaction_id)

        assert described.compaction_id == planned.compaction_id
        assert described.changes == planned.changes
        setup.client.update_event.assert_not_called()

    def test_abandoning_leaves_the_notes_uncompacted_and_unblocks_a_new_compaction(self):
        setup = _standard()
        first = setup.compactor.dry_run(setup.email_then_report())
        setup.journal.set_status(setup.journal.load(first.compaction_id), APPLYING)

        result = setup.compactor.abandon(first.compaction_id)

        assert result.status == "abandoned"
        assert [n.id for n in setup.notes.read_with_rows()] == [setup.note_id(2), setup.note_id(3)]
        assert setup.compactor.dry_run(setup.email_then_report()).status == "planned"

    def test_a_finished_compaction_cannot_be_abandoned(self):
        setup = _standard()
        planned = setup.compactor.dry_run(setup.email_then_report())
        setup.compactor.commit(planned.compaction_id)

        with pytest.raises(CompactionError, match="already complete"):
            setup.compactor.abandon(planned.compaction_id)


class TestDayByDay:
    def test_the_next_days_notes_become_available_once_the_first_is_compacted(self):
        setup = Setup([("09:05", "email"), ("10:20", "report")])
        setup.append_note("08:00+1", "next day")
        setup.now = "09:00+1"
        planned = setup.compactor.dry_run(setup.email_then_report())
        setup.compactor.commit(planned.compaction_id)

        context = setup.compactor.prepare()

        assert [n.id for n in context.notes] == [setup.note_id(4)]
        assert context.remaining_note_count == 0

    def test_the_next_days_compaction_window_starts_where_the_last_one_left_off(self):
        events = [
            event_at("09:00-10:00", id="e1", summary="Email", priority=2),
            event_at("20:00-07:00+1", id="s1", summary="Sleep", priority=0, is_end_of_day_sleep=True),
            event_at("07:00+1-07:30+1", id="gr1", summary="Getting Ready", priority=2),
        ]
        setup = Setup([("09:05", "email")], events=events, now="08:00+1")
        planned = setup.compactor.dry_run([])
        setup.compactor.commit(planned.compaction_id)
        assert setup.journal.last_stamped_now() == time_at("07:00+1")
        setup.append_note("08:15+1", "left for work")

        context = setup.compactor.prepare()

        assert context.compaction_window_start == time_at("07:00+1")
        assert [e.id for e in context.events][:2] == ["s1", "gr1"]


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
        setup = Setup([("09:05", "email")], events=events, now="08:00+1")
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

        with pytest.raises(CompactionError, match="'Sleep' \\(s1, as decided.*overlaps 'Getting Ready'"):
            setup.compactor.dry_run(
                [EventDecision(action="keep", event_id="s1", end_note=setup.note_id(3))]
            )

    def test_a_late_wake_up_extends_it_and_reflows_the_rest_of_the_morning(self):
        setup = self._slept_in()
        planned = setup.compactor.dry_run(
            [
                EventDecision(action="keep", event_id="s1", end_note=setup.note_id(3)),
                EventDecision(
                    action="keep", event_id="gr1", start_note=setup.note_id(3), end=time_at("09:00+1")
                ),
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
        assert patch.is_fixed_time is True

    def test_moving_bedtime_near_the_end_of_the_day_plans_and_commits(self):
        setup = Setup([("09:05", "email"), ("10:20", "report")], now="19:45")
        bedtime = EventDecision(
            action="keep", event_id="s1", start=time_at("22:30"), end=time_at("08:00") + timedelta(days=1)
        )

        planned = setup.compactor.dry_run(setup.email_then_report() + [bedtime])
        setup.compactor.commit(planned.compaction_id)

        assert planned.status == "planned"
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


def _goal(goal_id, name, status="active"):
    return Goal(id=goal_id, name=name, status=status, label_id=f"label-{goal_id}")


class TestGoals:
    _GOALS = [_goal("work", "Time Tracker"), _goal("mail", "Inbox zero"), _goal("gone", "Old", status="deleted")]

    def test_prepare_lists_goals_and_suggests_them_from_earlier_events_with_the_same_title(self):
        events = _day()
        yesterday = [
            replace(e, id=f"y-{e.id}", start=e.start - timedelta(days=1), end=e.end - timedelta(days=1))
            for e in events[:2]
        ]
        yesterday[0].goal_ids = ["gone", "mail"]  # Email: a deleted goal isn't suggested
        yesterday[1].goal_ids = ["work"]
        events[1].goal_ids = ["work"]  # Report already has one: nothing suggested
        setup = Setup([("09:05", "x")], events=yesterday + events, goals=self._GOALS)

        context = setup.compactor.prepare()

        by_id = {e.id: e for e in context.events}
        assert by_id["e1"].suggested_goal_ids == ["mail"]
        assert by_id["e2"].suggested_goal_ids is None
        assert by_id["e2"].goal_names == ["Time Tracker"]
        assert [g.path for g in context.goals] == ["Time Tracker", "Inbox zero"]  # active ones
        text = context.timeline.text
        assert "Email  ◇ Inbox zero" in text and "Report  ◆ Time Tracker" in text

    def test_future_events_get_no_suggestions(self):
        events = _day()
        earlier = replace(events[2], id="y-e3", start=events[2].start - timedelta(days=1),
                          end=events[2].end - timedelta(days=1), goal_ids=["work"])
        setup = Setup([("09:05", "x")], events=[earlier] + events, goals=self._GOALS)

        context = setup.compactor.prepare()

        assert {e.id: e for e in context.events}["e3"].suggested_goal_ids is None  # Lunch is after now

    def test_a_dry_run_refuses_unknown_or_deleted_goals(self):
        setup = Setup([("09:00", None), ("09:30", None)], goals=self._GOALS)
        decisions = setup.coffee_instead_of_email()

        decisions[0].goal_ids = ["wrk"]
        with pytest.raises(CompactionError, match="'wrk' isn't a goal; did you mean work"):
            setup.compactor.dry_run(decisions)
        decisions[0].goal_ids = ["gone"]
        with pytest.raises(CompactionError, match="Deleted goals can't be given to an event"):
            setup.compactor.dry_run(decisions)

    def test_created_events_are_written_with_their_goals(self):
        setup = Setup([("09:00", None), ("09:30", None)], goals=self._GOALS)
        decisions = setup.coffee_instead_of_email()
        decisions[0].goal_ids = ["work"]
        planned = setup.compactor.dry_run(decisions)

        setup.compactor.commit(planned.compaction_id)

        assert setup.client.create_event.call_args.args[0].goal_ids == ["work"]

    def test_a_goals_only_change_is_patched_as_just_that(self):
        setup = Setup([("09:05", "x")], goals=self._GOALS)
        planned = setup.compactor.dry_run([EventDecision(action="keep", event_id="e1", goal_ids=["mail"])])

        setup.compactor.commit(planned.compaction_id)

        patches = [c.args[0] for c in setup.client.update_event.call_args_list if c.args[0].id == "e1"]
        assert any(p.goal_ids == ["mail"] for p in patches)


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
        for cached in ("_calendar_client", "_reallocating_calendar", "_goals", "_noted_time_sheet", "_note_compactor"):
            monkeypatch.setattr(server, cached, None)
        self.now = "09:10+1"
        # The one seam: the compactor's clock, so the rounds fall on the
        # test calendar's day.
        server.get_note_compactor()._clock = lambda: time_at(self.now)
        return service

    def _round(self, service, note_at, now):
        """One round of MCP tool calls: note, prepare_compaction,
        compact_notes (dry run), compact_notes (apply). Returns each
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
        plan = call("compact_notes dry run", lambda: server.compact_notes(decisions=[]))
        call("compact_notes apply", lambda: server.compact_notes(compaction_id=plan.compaction_id, dry_run=False))
        return counts

    def test_a_typical_round_of_compaction_makes_no_more_read_requests_than_this(self, monkeypatch):
        service = self._server(monkeypatch)
        for note_at, now in [("09:05+1", "09:10+1"), ("09:50+1", "09:55+1"), ("10:20+1", "10:25+1")]:
            self._round(service, note_at, now)

        counts = self._round(service, "10:40+1", "10:45+1")

        # Each compaction call reads the notes tab, the journal and the
        # goals tab whole, together in one request (NoteCompactor's
        # prefetch); every later read of them in the call falls within
        # that, so it's served from the cache.
        assert counts == {
            "note": 1,
            "prepare_compaction": 1,
            "compact_notes dry run": 1,
            "compact_notes apply": 1,
        }
