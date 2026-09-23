from dataclasses import replace
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from tests.event_time_helpers import event_at, time_at
from tests.fake_sheets import FakeSheets
from utilities.compaction_journal import ABANDONED, APPLYING, PLANNED, STAMPED, CompactionJournal
from utilities.note_compaction import CompactionError, NoteDisposition, NoteEffect
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
    """Just the `list_day_events` the compactor reads a day through --
    handing back fresh copies each call, like the real label-aware view."""

    def __init__(self, events):
        self.events = events

    def list_day_events(self, start):
        # Like the real one: what overlaps the next 24 hours, cut off after
        # the first end-of-day sleep event.
        window_end = start + timedelta(hours=24)
        events = sorted(
            (replace(e) for e in self.events if e.end > start and e.start < window_end),
            key=lambda e: e.start,
        )
        for i, event in enumerate(events):
            if event.is_end_of_day_sleep:
                return events[: i + 1]
        return events


class Setup:
    def __init__(self, notes, events=None, now="11:30"):
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

    def d(self, row, *effects):
        return NoteDisposition(note_id=self.note_id(row), effects=list(effects))

    def email_then_report(self):
        return [
            self.d(2, _e("starts", event_id="e1")),
            self.d(3, _e("ends", event_id="e1"), _e("starts", event_id="e2")),
        ]


def _e(kind, **fields):
    return NoteEffect(kind=kind, **fields)


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
        assert "starts_unplanned" in context.instructions

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

    def test_ambiguous_notes_come_back_as_questions_and_nothing_is_journaled(self):
        setup = _standard()

        result = setup.compactor.dry_run(
            [setup.d(2, _e("ambiguous", question="Was that the email?")), setup.d(3, _e("ignore"))]
        )

        assert result.status == "needs_clarification"
        assert "Was that the email?" in result.questions[0]
        assert result.compaction_id is None
        assert setup.journal.open_compactions() == []
        assert [w for w in setup.sheets.writes if w[0] == _JOURNAL_TAB] == []

    def test_invalid_dispositions_raise_with_what_to_fix(self):
        setup = _standard()

        with pytest.raises(CompactionError, match="no disposition for note"):
            setup.compactor.dry_run([setup.d(2, _e("starts", event_id="e1"))])

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

    def test_a_rejected_plan_can_be_redone_with_corrected_dispositions(self):
        setup = _standard()
        setup.compactor.dry_run(setup.email_then_report())

        # The user says the 10:20 note actually *ended* the email, and the
        # report didn't start until later -- so redo it without touching
        # the calendar or calling prepare again.
        revised = setup.compactor.dry_run(
            [setup.d(2, _e("starts", event_id="e1")), setup.d(3, _e("ends", event_id="e1"))]
        )

        by_event = {c.event_id: c for c in revised.changes}
        assert by_event["e1"].reason.startswith("recorded as what actually happened")
        assert by_event["e1"].after.end == time_at("10:20")
        # The report isn't recorded as started any more -- it's only nudged
        # aside to clear the email.
        assert not by_event["e2"].reason.startswith("recorded as what actually happened")
        setup.client.update_event.assert_not_called()

    def test_a_plan_that_fails_validation_leaves_the_earlier_plan_alone(self):
        setup = _standard()
        first = setup.compactor.dry_run(setup.email_then_report())

        with pytest.raises(CompactionError):
            setup.compactor.dry_run([setup.d(2, _e("starts", event_id="nope"))])

        assert setup.journal.load(first.compaction_id).status == PLANNED

    def test_an_activity_with_no_end_on_a_finished_day_asks_when_it_ended(self):
        # Compacting yesterday: nothing says when the report ended, and
        # there's no "now" to run it to, so it isn't guessed.
        setup = _standard()
        setup.now = "12:00+1"

        result = setup.compactor.dry_run(setup.email_then_report())

        assert result.status == "needs_clarification"
        assert "When did 'Report' end?" in result.questions[0]
        assert setup.journal.compactions_with_status(PLANNED) == []

    def test_running_late_on_the_current_day_runs_the_activity_to_now_not_to_bedtime(self):
        # 21:00 is past the 20:00 start of the sleep block but the day isn't
        # over: the report ran until now, and the sleep block gives way.
        setup = _standard()
        setup.now = "21:00"

        result = setup.compactor.dry_run(setup.email_then_report())

        assert result.status == "planned"
        report = next(c for c in result.changes if c.event_id == "e2")
        assert report.after.end == time_at("21:00")
        assert not any("bedtime" in w for w in result.warnings)

    def test_a_rejected_plan_can_be_redone_with_a_rename_and_a_note(self):
        # The user reviews the first dry run and asks for a different
        # title and a note -- redone the same way any other correction is,
        # with no new tool and without touching the calendar.
        setup = _standard()
        setup.compactor.dry_run(setup.email_then_report())

        revised = setup.compactor.dry_run(
            [
                setup.d(2, _e("starts", event_id="e1", rename="Deep work", annotate="phone rang")),
                setup.d(3, _e("ends", event_id="e1"), _e("starts", event_id="e2")),
            ]
        )

        email = next(c for c in revised.changes if c.event_id == "e1")
        assert email.after.summary == "Deep work"
        assert email.after.description == "Notes:\n- 09:05 phone rang"
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
        assert (patches["e2"].start, patches["e2"].end) == (time_at("10:20"), time_at("11:30"))
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
        planned = setup.compactor.dry_run(
            [setup.d(2, _e("starts", event_id="e1")), setup.d(3, _e("ends", event_id="e2"))]
        )

        setup.compactor.commit(planned.compaction_id)

        cancelled = next(
            c.args[0] for c in setup.client.update_event.call_args_list if c.args[0].id == "e2"
        )
        assert cancelled.status == "cancelled"

    def test_created_events_get_deterministic_ids(self):
        setup = Setup([("09:00", None), ("09:30", None)])
        planned = setup.compactor.dry_run(
            [
                setup.d(2, _e("starts_unplanned", summary="Coffee")),
                setup.d(3, _e("ends", started_by_note=setup.note_id(2))),
            ]
        )

        setup.compactor.commit(planned.compaction_id)

        created = setup.client.create_event.call_args.args[0]
        journal = setup.journal.load(planned.compaction_id)
        step = next(s for s in journal.steps if s.action == "create")
        assert created.id == f"cmp{planned.compaction_id}s{step.step:03d}"
        assert created.summary == "Coffee"

    def test_a_retried_create_that_already_exists_counts_as_done(self):
        setup = Setup([("09:00", None), ("09:30", None)])
        planned = setup.compactor.dry_run(
            [
                setup.d(2, _e("starts_unplanned", summary="Coffee")),
                setup.d(3, _e("ends", started_by_note=setup.note_id(2))),
            ]
        )
        setup.client.create_event.side_effect = HttpError(MagicMock(status=409), b"exists")

        result = setup.compactor.commit(planned.compaction_id)

        assert result.status == "applied"
        assert setup.journal.load(planned.compaction_id).status == STAMPED

    def test_other_create_errors_are_not_swallowed(self):
        setup = Setup([("09:00", None), ("09:30", None)])
        planned = setup.compactor.dry_run(
            [
                setup.d(2, _e("starts_unplanned", summary="Coffee")),
                setup.d(3, _e("ends", started_by_note=setup.note_id(2))),
            ]
        )
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
        setup = Setup([("09:05", "email"), ("10:20", "report"), ("11:00", "done")])
        setup.append_note("08:00+1", "next day")
        setup.now = "09:00+1"
        planned = setup.compactor.dry_run(
            [
                setup.d(2, _e("starts", event_id="e1")),
                setup.d(3, _e("ends", event_id="e1"), _e("starts", event_id="e2")),
                setup.d(4, _e("ends", event_id="e2")),
            ]
        )
        setup.compactor.commit(planned.compaction_id)

        context = setup.compactor.prepare()

        assert [n.id for n in context.notes] == [setup.note_id(5)]
        assert context.remaining_note_count == 0
