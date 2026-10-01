from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from calendar_clients.google_calendar import Event
from tests.event_time_helpers import event_at, time_at
from tests.fake_row_hints import FakeRowHints
from tests.fake_sheets import FakeSheets
from utilities.compaction_journal import ABANDONED, APPLYING, PLANNED, STAMPED, CompactionJournal
from utilities.note_compaction import CompactionError, CompactionPlan, EventDecision
from utilities.note_compactor import NoteCompactor
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
    def __init__(self, notes, events=None, now="11:30"):
        self.sheets = FakeSheets()
        self.sheets.write_rows_in_sheet(
            "s", _NOTES_TAB, "A1:C1", [["timestamp", "description", "compaction_id"]]
        )
        self.hints = FakeRowHints()
        self.notes = NotedTimeSheet(self.sheets, "s", _NOTES_TAB, self.hints)
        for at, description in notes:
            self.append_note(at, description)
        self.journal = CompactionJournal(self.sheets, "s", _JOURNAL_TAB, self.hints)
        self.calendar = FakeCalendar(events if events is not None else _day())
        self.client = MagicMock()
        self.now = now
        self.compactor = NoteCompactor(
            calendar=self.calendar,
            client=self.client,
            notes=self.notes,
            journal=self.journal,
            clock=lambda: time_at(self.now),
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

    def test_offers_the_notes_beside_the_planned_events_as_a_timeline(self):
        context = _standard().compactor.prepare()

        assert [n.text for n in context.timeline.notes] == ["email", "report"]
        assert [e.event_id for e in context.timeline.events] == ["e1", "e2", "e3", "s1"]
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
