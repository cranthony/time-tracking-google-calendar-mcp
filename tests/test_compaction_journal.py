from unittest.mock import MagicMock

import pytest

from tests.event_time_helpers import event_at, time_at
from tests.fake_sheets import FakeSheets
from utilities import calendar_metadata_sheet
from utilities.compaction_journal import (
    APPLIED,
    APPLYING,
    PLANNED,
    STAMPED,
    CompactionJournal,
)
from utilities.note_compaction import (
    CompactionChange,
    CompactionError,
    CompactionPlan,
    EventState,
    NoteDisposition,
    NoteEffect,
)

_SHEET_ID = 7


def _journal(sheets=None):
    sheets = sheets or FakeSheets()
    return CompactionJournal(sheets, "spreadsheet-1", _SHEET_ID), sheets


def _plan():
    before = EventState.from_event(event_at("09:00-10:00", summary="Email", priority=2))
    after = EventState.from_event(
        event_at("09:05-10:20", summary="Email", priority=2, is_fixed_time=True)
    )
    return CompactionPlan(
        changes=[
            CompactionChange(action="cancel", event_id="e9", reason="in the past", before=before),
            CompactionChange(
                action="update", event_id="e1", reason="actual", before=before, after=after
            ),
            CompactionChange(action="create", reason="unplanned", after=after),
        ],
        warnings=["a warning"],
    )


def _dispositions():
    return [
        NoteDisposition(note_id="n2", effects=[NoteEffect(kind="starts", event_id="e1")]),
        NoteDisposition(
            note_id="n3",
            effects=[
                NoteEffect(kind="ends", event_id="e1"),
                NoteEffect(kind="starts_unplanned", summary="Coffee"),
            ],
        ),
    ]


def _start(journal, compaction_id="abc123"):
    journal.start(
        compaction_id,
        now=time_at("11:00"),
        note_ids=["n2", "n3"],
        dispositions=_dispositions(),
        plan=_plan(),
    )


class TestStartAndLoad:
    def test_round_trips_the_whole_approved_plan(self):
        journal, _ = _journal()
        _start(journal)

        loaded = journal.load("abc123")

        assert loaded.id == "abc123"
        assert loaded.status == PLANNED
        assert loaded.now == time_at("11:00")
        assert loaded.note_ids == ["n2", "n3"]
        assert loaded.warnings == ["a warning"]
        assert loaded.dispositions == _dispositions()
        assert [(s.step, s.action, s.event_id, s.status) for s in loaded.steps] == [
            (1, "cancel", "e9", "pending"),
            (2, "update", "e1", "pending"),
            (3, "create", None, "pending"),
        ]
        plan = _plan()
        assert loaded.changes() == plan.changes

    def test_writes_everything_in_one_write_so_a_crash_cannot_leave_half_a_plan(self):
        journal, sheets = _journal()

        _start(journal)

        assert len(sheets.writes) == 1

    def test_a_second_compaction_is_appended_below_the_first(self):
        journal, _ = _journal()
        _start(journal, "first")
        _start(journal, "second")

        assert journal.load("first").id == "first"
        assert journal.load("second").id == "second"
        assert journal.load("second").row > journal.load("first").row

    def test_loading_an_unknown_compaction_says_so(self):
        journal, _ = _journal()

        with pytest.raises(CompactionError, match="no compaction with id 'nope'"):
            journal.load("nope")

    def test_keeps_a_large_plan_out_of_any_single_cell(self):
        journal, sheets = _journal()
        big = CompactionPlan(
            changes=[
                CompactionChange(
                    action="update",
                    event_id=f"e{i}",
                    reason="r",
                    before=EventState.from_event(event_at("09:00-10:00", summary="x" * 500)),
                    after=EventState.from_event(event_at("09:00-10:00", summary="x" * 500)),
                )
                for i in range(200)
            ]
        )

        journal.start("big", now=time_at("11:00"), note_ids=[], dispositions=[], plan=big)

        assert max(len(v) for v in sheets.cells[_SHEET_ID].values()) < 50_000
        assert len(journal.load("big").steps) == 200


class TestStatus:
    def test_set_status_updates_the_compaction_row_only(self):
        journal, sheets = _journal()
        _start(journal)
        loaded = journal.load("abc123")

        journal.set_status(loaded, APPLYING)

        assert loaded.status == APPLYING
        assert journal.load("abc123").status == APPLYING
        assert all(s.status == "pending" for s in journal.load("abc123").steps)

    def test_mark_step_done_updates_that_step_only(self):
        journal, _ = _journal()
        _start(journal)
        loaded = journal.load("abc123")

        journal.mark_step_done(loaded.steps[1])

        assert [s.status for s in journal.load("abc123").steps] == ["pending", "done", "pending"]

    def test_only_applying_and_applied_compactions_are_open(self):
        journal, _ = _journal()
        for compaction_id, status in [
            ("p", PLANNED),
            ("a", APPLYING),
            ("d", APPLIED),
            ("s", STAMPED),
        ]:
            _start(journal, compaction_id)
            journal.set_status(journal.load(compaction_id), status)

        assert journal.open_compactions() == [("a", APPLYING), ("d", APPLIED)]

    def test_no_open_compactions_in_an_empty_journal(self):
        journal, _ = _journal()

        assert journal.open_compactions() == []


class TestEnsure:
    def test_creates_and_tags_the_tab_with_a_header_row(self):
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = None
        sheets_client.add_sheet.return_value = 55

        CompactionJournal.ensure(sheets_client, "spreadsheet-1")

        sheets_client.create_sheet_metadata.assert_called_once_with(
            "spreadsheet-1", 55, "sheet-role", calendar_metadata_sheet.COMPACTIONS_SHEET_ROLE
        )
        header = sheets_client.write_rows_in_sheet.call_args.args[3][0]
        assert header[:3] == ["compaction_id", "step", "kind"]

    def test_reuses_an_existing_tab(self):
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = 55

        CompactionJournal.ensure(sheets_client, "spreadsheet-1")

        sheets_client.add_sheet.assert_not_called()
        sheets_client.write_rows_in_sheet.assert_not_called()
