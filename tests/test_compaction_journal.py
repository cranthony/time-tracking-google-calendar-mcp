from unittest.mock import MagicMock, call

import pytest

from tests.event_time_helpers import event_at, time_at
from tests.fake_row_hints import FakeRowHints
from tests.fake_sheets import FakeSheets
from utilities import calendar_metadata_sheet
from utilities.compaction_journal import (
    ABANDONED,
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
    EventDecision,
    EventState,
)

_SHEET_ID = 7


def _journal(sheets=None, hints=None):
    sheets = sheets or FakeSheets()
    return CompactionJournal(sheets, "spreadsheet-1", _SHEET_ID, hints or FakeRowHints()), sheets


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


def _decisions():
    return [
        EventDecision(action="keep", event_id="e1", start_note="n2", end=time_at("10:20")),
        EventDecision(action="create", summary="Coffee", start_note="n3", end=time_at("10:40")),
    ]


def _start(journal, compaction_id="abc123", ignore_notes=None):
    journal.start(
        compaction_id,
        now=time_at("11:00"),
        note_ids=["n2", "n3"],
        decisions=_decisions(),
        plan=_plan(),
        ignore_notes=ignore_notes,
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
        assert loaded.decisions == _decisions()
        assert loaded.ignore_notes == []
        assert [(s.step, s.action, s.event_id, s.status) for s in loaded.steps] == [
            (1, "cancel", "e9", "pending"),
            (2, "update", "e1", "pending"),
            (3, "create", None, "pending"),
        ]
        plan = _plan()
        assert loaded.changes() == plan.changes

    def test_round_trips_ignored_notes(self):
        journal, _ = _journal()

        _start(journal, ignore_notes=["n3"])

        assert journal.load("abc123").ignore_notes == ["n3"]

    def test_skips_rows_of_the_retired_disposition_kind(self):
        journal, sheets = _journal()
        _start(journal)
        sheets.write_rows_in_sheet(
            "spreadsheet-1", _SHEET_ID, "A8:H8", [["abc123", "0", "disposition", "n2", "[]", "", "", ""]]
        )

        assert journal.load("abc123").decisions == _decisions()

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

        journal.start("big", now=time_at("11:00"), note_ids=[], decisions=[], plan=big)

        assert max(len(v) for v in sheets.cells[_SHEET_ID].values()) < 50_000
        assert len(journal.load("big").steps) == 200


class TestStartHints:
    def test_confirming_the_first_data_row_does_not_read_the_header_row(self):
        # Nothing's been written yet, so there's no row before the hint
        # to confirm -- reading row 1 would just be the header, not data,
        # and shouldn't be mistaken for it.
        sheets = FakeSheets()
        sheets.write_rows_in_sheet(
            "spreadsheet-1", _SHEET_ID, "A1:H1", [["compaction_id"] + ["x"] * 7]
        )
        hints = FakeRowHints()
        hints.set("journal_next_row", 2)
        client = MagicMock(wraps=sheets)
        journal = CompactionJournal(client, "spreadsheet-1", _SHEET_ID, hints)

        _start(journal)

        assert journal.load("abc123").row == 2
        assert call("spreadsheet-1", _SHEET_ID, "A2:H6") in client.read_rows_in_sheet.call_args_list
        assert call("spreadsheet-1", _SHEET_ID, "A1:H6") not in client.read_rows_in_sheet.call_args_list

    def test_uses_a_confirmed_hint_instead_of_a_full_read(self):
        sheets = FakeSheets()
        # Real data ending exactly where the hint says -- row 4 is the
        # last row of an earlier compaction, so the hint (5) is genuine.
        sheets.write_rows_in_sheet("spreadsheet-1", _SHEET_ID, "A2:H", [["x"] * 8 for _ in range(3)])
        hints = FakeRowHints()
        hints.set("journal_next_row", 5)
        client = MagicMock(wraps=sheets)
        journal = CompactionJournal(client, "spreadsheet-1", _SHEET_ID, hints)

        _start(journal)

        # load() itself does a full scan to find a compaction by id --
        # it's start()'s own read that this hint is meant to spare.
        assert not any(c.args[2] == "A2:H" for c in client.read_rows_in_sheet.call_args_list)
        assert journal.load("abc123").row == 5

    def test_falls_back_when_the_hinted_row_is_not_actually_blank(self):
        # The hint is far short of where the data actually ends (e.g. a
        # crash between an earlier start()'s write and its hint update) --
        # can't be trusted, so this recounts from the top.
        sheets = FakeSheets()
        sheets.write_rows_in_sheet("spreadsheet-1", _SHEET_ID, "A2:H", [["x"] * 8 for _ in range(20)])
        hints = FakeRowHints()
        hints.set("journal_next_row", 3)
        journal = CompactionJournal(sheets, "spreadsheet-1", _SHEET_ID, hints)

        _start(journal)

        assert journal.load("abc123").row == 22
        assert hints.get("journal_next_row") == 28  # 22 + 1 compaction + 2 decisions + 3 changes

    def test_falls_back_when_the_hinted_row_overshoots_past_a_gap(self):
        # Real data ends at row 6, but the hint points at row 20, with
        # nothing in between -- checking only the rows at and after the
        # hint would see that as "blank enough" and trust it, silently
        # wasting rows 7-19 forever instead of catching the drift.
        sheets = FakeSheets()
        sheets.write_rows_in_sheet("spreadsheet-1", _SHEET_ID, "A2:H", [["x"] * 8 for _ in range(5)])
        hints = FakeRowHints()
        hints.set("journal_next_row", 20)
        journal = CompactionJournal(sheets, "spreadsheet-1", _SHEET_ID, hints)

        _start(journal)

        assert journal.load("abc123").row == 7

    def test_updates_the_hint_after_a_full_read(self):
        hints = FakeRowHints()
        journal, _ = _journal(hints=hints)

        _start(journal)

        assert hints.get("journal_next_row") == 8  # 1 compaction + 2 decisions + 3 changes

    def test_sets_the_latest_compaction_row_hint(self):
        hints = FakeRowHints()
        journal, _ = _journal(hints=hints)

        _start(journal, "first")
        assert hints.get("journal_latest_compaction_row") == 2

        _start(journal, "second")
        assert hints.get("journal_latest_compaction_row") == 8  # after "first"'s 6 rows


class TestLoadHints:
    def test_uses_a_confirmed_hint_instead_of_a_full_scan(self):
        sheets = FakeSheets()
        client = MagicMock(wraps=sheets)
        hints = FakeRowHints()
        journal = CompactionJournal(client, "spreadsheet-1", _SHEET_ID, hints)
        _start(journal, "first")
        _start(journal, "second")
        client.read_rows_in_sheet.reset_mock()

        loaded = journal.load("second")

        assert loaded.id == "second"
        assert not any(c.args[2] == "A2:H" for c in client.read_rows_in_sheet.call_args_list)

    def test_the_first_ever_compaction_skips_the_before_check(self):
        sheets = FakeSheets()
        client = MagicMock(wraps=sheets)
        hints = FakeRowHints()
        journal = CompactionJournal(client, "spreadsheet-1", _SHEET_ID, hints)
        _start(journal)
        client.read_rows_in_sheet.reset_mock()

        loaded = journal.load("abc123")

        assert loaded.id == "abc123"
        assert not any(c.args[2] == "A1:A1" for c in client.read_rows_in_sheet.call_args_list)

    def test_falls_back_to_a_full_scan_for_an_older_compaction(self):
        # The hint points at "second" (the latest), but "first" was asked
        # for -- the mismatch is exactly what the hint check exists to
        # catch, so this recounts from the top.
        journal, _ = _journal()
        _start(journal, "first")
        _start(journal, "second")

        assert journal.load("first").id == "first"

    def test_falls_back_when_the_hint_points_partway_through_the_same_compaction(self):
        # The hint is nudged one row past "second"'s own first row (into
        # its first disposition) -- row[0] there is still "second", so
        # checking only the hinted row would wrongly confirm it and skip
        # "second"'s own compaction row and first disposition. The row
        # before the hint belongs to "second" too, which is what catches
        # it: a genuine boundary always has a *different* (or no)
        # compaction immediately before it.
        hints = FakeRowHints()
        journal, _ = _journal(hints=hints)
        _start(journal, "first")
        _start(journal, "second")
        hints.set("journal_latest_compaction_row", hints.get("journal_latest_compaction_row") + 1)

        loaded = journal.load("second")

        assert loaded.decisions == _decisions()

    def test_falls_back_when_the_hint_points_somewhere_else_entirely(self):
        hints = FakeRowHints()
        journal, _ = _journal(hints=hints)
        _start(journal, "first")
        hints.set("journal_latest_compaction_row", 999)

        assert journal.load("first").id == "first"


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


class TestGarbageCollect:
    _BLOCK_ROWS = 6  # 1 compaction row + 2 decisions + 3 changes, per _start()

    def test_does_nothing_under_the_row_budget(self):
        journal, _ = _journal()
        _start(journal, "only")

        journal.garbage_collect()

        assert journal.load("only").id == "only"

    def test_deletes_old_terminal_blocks_but_preserves_the_last_stamped_and_open_ones(self):
        hints = FakeRowHints()
        journal, _ = _journal(hints=hints)
        ids = [f"c{i}" for i in range(85)]
        for compaction_id in ids:
            _start(journal, compaction_id)
            journal.set_status(journal.load(compaction_id), STAMPED)
        # The last one is actually still open, not stamped.
        journal.set_status(journal.load(ids[-1]), PLANNED)
        total_rows = 85 * self._BLOCK_ROWS
        assert hints.get("journal_next_row") == 2 + total_rows

        journal.garbage_collect()

        # 10 rows over budget (510 - 500); each block is 6 rows, so the
        # oldest 2 blocks (12 rows) are deleted -- enough to clear it.
        with pytest.raises(CompactionError, match="no compaction with id 'c0'"):
            journal.load(ids[0])
        with pytest.raises(CompactionError, match="no compaction with id 'c1'"):
            journal.load(ids[1])
        assert journal.load(ids[2]).id == ids[2]  # stamped, but not needed to delete
        assert journal.load(ids[-2]).id == ids[-2]  # the last *stamped* one -- preserved
        assert journal.load(ids[-1]).status == PLANNED  # still open -- preserved
        assert hints.get("journal_next_row") == 2 + total_rows - 2 * self._BLOCK_ROWS
        assert hints.get("journal_latest_compaction_row") == 2 + 84 * self._BLOCK_ROWS - 2 * self._BLOCK_ROWS

    def test_leaves_the_tab_at_least_1000_rows_long(self):
        # Earlier garbage collection already shrank the tab's grid; without
        # topping it back up, it would eventually run out of rows to append
        # into.
        sheets = FakeSheets()
        sheets.row_counts[_SHEET_ID] = 600
        journal, _ = _journal(sheets=sheets)
        for i in range(85):
            _start(journal, f"c{i}")
            journal.set_status(journal.load(f"c{i}"), STAMPED)

        journal.garbage_collect()

        assert sheets.row_count(_SHEET_ID) == 1000

    def test_deletes_old_abandoned_blocks_too(self):
        journal, _ = _journal()
        for i in range(85):
            _start(journal, f"c{i}")
            journal.set_status(journal.load(f"c{i}"), ABANDONED if i < 84 else STAMPED)

        journal.garbage_collect()

        with pytest.raises(CompactionError):
            journal.load("c0")
        assert journal.load("c84").status == STAMPED

    def test_does_nothing_if_the_oldest_block_is_not_safely_deletable(self):
        # None of these are stamped or abandoned (start() leaves them
        # planned), so nothing at the top is safe to delete.
        journal, _ = _journal()
        for i in range(85):
            _start(journal, f"c{i}")

        journal.garbage_collect()

        assert journal.load("c0").id == "c0"

    def test_deletes_what_it_safely_can_even_when_that_cant_clear_the_whole_excess(self):
        journal, _ = _journal()
        _start(journal, "old1")
        journal.set_status(journal.load("old1"), STAMPED)
        _start(journal, "old2")
        journal.set_status(journal.load("old2"), STAMPED)  # the last *stamped* one
        big_plan = CompactionPlan(
            changes=[
                CompactionChange(
                    action="update",
                    event_id=f"e{i}",
                    reason="r",
                    before=EventState.from_event(event_at("09:00-10:00")),
                    after=EventState.from_event(event_at("09:00-10:00")),
                )
                for i in range(500)
            ]
        )
        journal.start("current", now=time_at("11:00"), note_ids=[], decisions=[], plan=big_plan)
        # "current" is left planned (open) -- can't be deleted, and its
        # size alone already exceeds the budget. "old2" is the last
        # *stamped* compaction, so it's preserved too -- only "old1" is
        # both deletable and not needed, even though that alone can't
        # clear the whole excess.

        journal.garbage_collect()

        with pytest.raises(CompactionError):
            journal.load("old1")
        assert journal.load("old2").id == "old2"
        assert journal.load("current").id == "current"


class TestEnsure:
    def test_creates_and_tags_the_tab_with_a_header_row(self):
        # Also ensures the shared row-hints tab (utilities/row_hints.py) --
        # its own tagged tab, created/tagged first, its header written
        # before the journal tab's own.
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = None
        sheets_client.add_sheet.side_effect = [55, 56]

        CompactionJournal.ensure(sheets_client, "spreadsheet-1")

        assert sheets_client.create_sheet_metadata.call_args_list == [
            call("spreadsheet-1", 55, "sheet-role", calendar_metadata_sheet.COMPACTIONS_SHEET_ROLE),
            call("spreadsheet-1", 56, "sheet-role", "row-hints"),
        ]
        header = sheets_client.write_rows_in_sheet.call_args_list[-1].args[3][0]
        assert header[:3] == ["compaction_id", "step", "kind"]

    def test_reuses_an_existing_tab(self):
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = 55

        CompactionJournal.ensure(sheets_client, "spreadsheet-1")

        sheets_client.add_sheet.assert_not_called()
        sheets_client.write_rows_in_sheet.assert_not_called()
