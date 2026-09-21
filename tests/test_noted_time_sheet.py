from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from utilities import calendar_metadata_sheet
from utilities.noted_time_sheet import NotedTime, NotedTimeSheet, SheetNote, parse_note_id

_HEADER_ROW = ["timestamp", "description", "compaction_id"]
_SHEET_ID = 42
_T1 = "2026-01-01T09:00:00+00:00"
_T2 = "2026-01-01T10:00:00+00:00"


def make_sheet(sheets_client=None, spreadsheet_id: str = "sheet-1", sheet_id: int = _SHEET_ID) -> NotedTimeSheet:
    return NotedTimeSheet(sheets_client or MagicMock(), spreadsheet_id, sheet_id)


def _sheets(rows, header=None):
    """A SheetsClient mock backed by an in-memory header/data range."""
    state = {"A1:C1": [header or _HEADER_ROW], "A2:C": rows}
    sheets_client = MagicMock()
    sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: state[rng]
    return sheets_client


class TestNotedTimeFromRow:
    def test_parses_a_full_row(self):
        noted_time = NotedTime.from_row(_HEADER_ROW, [_T1, "Started work", "abc"])

        assert noted_time == NotedTime(
            timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc),
            description="Started work",
            compaction_id="abc",
        )

    def test_blank_description_and_compaction_id_become_none(self):
        noted_time = NotedTime.from_row(_HEADER_ROW, [_T1, "", ""])

        assert noted_time.description is None
        assert noted_time.compaction_id is None

    def test_missing_trailing_cells_become_none(self):
        # Sheets omits trailing blank cells from a row entirely.
        noted_time = NotedTime.from_row(_HEADER_ROW, [_T1])

        assert noted_time == NotedTime(
            timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
        )

    def test_ignores_unknown_columns(self):
        noted_time = NotedTime.from_row(
            ["timestamp", "description", "notes"], [_T1, "Started work", "some note"]
        )

        assert noted_time == NotedTime(
            timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc), description="Started work"
        )

    def test_column_order_does_not_matter(self):
        noted_time = NotedTime.from_row(["description", "timestamp"], ["Started work", _T1])

        assert noted_time == NotedTime(
            timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc), description="Started work"
        )

    def test_raises_when_timestamp_is_missing(self):
        with pytest.raises(ValueError):
            NotedTime.from_row(_HEADER_ROW, ["", "Started work"])


class TestNotedTimeToRow:
    def test_writes_timestamp_as_isoformat_and_the_rest_as_strings(self):
        noted_time = NotedTime(
            timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc),
            description="Started work",
            compaction_id="abc",
        )

        assert noted_time.to_row(_HEADER_ROW) == [_T1, "Started work", "abc"]

    def test_blank_fields_become_empty_strings(self):
        noted_time = NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc))

        assert noted_time.to_row(_HEADER_ROW) == [_T1, "", ""]

    def test_preserves_unknown_columns_from_the_original_row(self):
        noted_time = NotedTime(
            timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc), description="Started work"
        )

        row = noted_time.to_row(
            ["timestamp", "description", "notes"], ["2025-01-01T00:00:00+00:00", "Old", "some note"]
        )

        assert row == [_T1, "Started work", "some note"]

    def test_blanks_unknown_columns_when_no_original_row(self):
        noted_time = NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc))

        assert noted_time.to_row(["timestamp", "notes"], None) == [_T1, ""]


class TestNotedTimeSheetEnsure:
    def test_reuses_an_already_tagged_tab_without_writing_anything(self):
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = _SHEET_ID

        sheet = NotedTimeSheet.ensure(sheets_client, "sheet-1")

        assert sheet.spreadsheet_id == "sheet-1"
        sheets_client.add_sheet.assert_not_called()
        sheets_client.write_rows_in_sheet.assert_not_called()

    def test_creates_and_tags_a_new_tab_with_a_header_row_including_compaction_id(self):
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = None
        sheets_client.add_sheet.return_value = 99

        NotedTimeSheet.ensure(sheets_client, "sheet-1")

        sheets_client.add_sheet.assert_called_once_with(
            "sheet-1",
            calendar_metadata_sheet.TIME_NOTES_SHEET_TITLE,
            tab_color=calendar_metadata_sheet._TAB_COLOR,
        )
        sheets_client.create_sheet_metadata.assert_called_once_with(
            "sheet-1", 99, "sheet-role", calendar_metadata_sheet.TIME_NOTES_SHEET_ROLE
        )
        sheets_client.write_rows_in_sheet.assert_called_once_with(
            "sheet-1", 99, "A1:C1", [_HEADER_ROW]
        )


class TestNotedTimeSheetSpreadsheetId:
    def test_exposes_the_given_id(self):
        assert make_sheet(spreadsheet_id="sheet-1").spreadsheet_id == "sheet-1"


class TestNotedTimeSheetRead:
    def test_reads_and_parses_data_rows(self):
        noted_time_sheet = make_sheet(_sheets([[_T1, "Started work"]]))

        assert noted_time_sheet.read() == [
            NotedTime(
                timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc), description="Started work"
            )
        ]

    def test_sorts_by_timestamp_regardless_of_row_order(self):
        noted_time_sheet = make_sheet(_sheets([[_T2, "Second"], [_T1, "First"]]))

        assert [n.description for n in noted_time_sheet.read()] == ["First", "Second"]

    def test_returns_empty_list_when_no_data_rows(self):
        assert make_sheet(_sheets([])).read() == []

    def test_only_returns_uncompacted_notes_by_default(self):
        noted_time_sheet = make_sheet(_sheets([[_T1, "Old", "cmp1"], [_T2, "New"]]))

        assert [n.description for n in noted_time_sheet.read()] == ["New"]
        assert [n.description for n in noted_time_sheet.read(include_compacted=True)] == ["Old", "New"]

    def test_raises_when_header_is_missing_expected_columns(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.return_value = [["timestamp", "description"]]

        with pytest.raises(ValueError):
            make_sheet(sheets_client).read()


class TestNotedTimeSheetReadWithRows:
    def test_numbers_each_note_by_its_sheet_row_in_sheet_order(self):
        noted_time_sheet = make_sheet(_sheets([[_T2, "Second"], [_T1, "First"]]))

        notes = noted_time_sheet.read_with_rows()

        assert [(n.row, n.id, n.note.description) for n in notes] == [
            (2, f"{_T2}#2", "Second"),
            (3, f"{_T1}#3", "First"),
        ]

    def test_skips_blank_rows_but_their_row_numbers_still_count(self):
        noted_time_sheet = make_sheet(_sheets([[_T1, "A"], [], [_T2, "B"]]))

        assert [n.id for n in noted_time_sheet.read_with_rows()] == [f"{_T1}#2", f"{_T2}#4"]

    def test_leaves_out_compacted_notes_without_renumbering_the_rest(self):
        noted_time_sheet = make_sheet(_sheets([[_T1, "Old", "cmp1"], [_T2, "New"]]))

        assert [n.id for n in noted_time_sheet.read_with_rows()] == [f"{_T2}#3"]
        assert [n.id for n in noted_time_sheet.read_with_rows(include_compacted=True)] == [
            f"{_T1}#2",
            f"{_T2}#3",
        ]


class TestNoteIds:
    def test_an_id_is_the_timestamp_and_the_row_together(self):
        note = SheetNote(row=7, note=NotedTime(timestamp=datetime(2026, 1, 1, 9, 5, tzinfo=timezone.utc)))

        assert note.id == "2026-01-01T09:05:00+00:00#7"

    def test_round_trips(self):
        note = SheetNote(row=7, note=NotedTime(timestamp=datetime(2026, 1, 1, 9, 5, tzinfo=timezone.utc)))

        assert parse_note_id(note.id) == (datetime(2026, 1, 1, 9, 5, tzinfo=timezone.utc), 7)

    def test_notes_with_the_same_timestamp_still_have_distinct_ids(self):
        first = SheetNote(row=2, note=NotedTime(timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        second = SheetNote(row=3, note=NotedTime(timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc)))

        assert first.id != second.id

    @pytest.mark.parametrize(
        "bad", ["7", "n7", "#7", "2026-01-01T09:00:00+00:00", "2026-01-01T09:00:00+00:00#x", "nope#7", ""]
    )
    def test_rejects_things_that_are_not_note_ids(self, bad):
        with pytest.raises(ValueError):
            parse_note_id(bad)


class TestNotedTimeSheetWrite:
    def test_overwrites_data_rows_preserving_unknown_columns(self):
        sheets_client = _sheets([["2025-01-01T00:00:00+00:00", "Old"]])

        make_sheet(sheets_client).write(
            [NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc), description="New")]
        )

        sheets_client.write_rows_in_sheet.assert_called_once_with(
            "sheet-1", _SHEET_ID, "A2:C", [[_T1, "New", ""]]
        )


class TestNotedTimeSheetAppend:
    def test_writes_only_the_new_row_after_the_existing_ones(self):
        sheets_client = _sheets([["2025-01-01T00:00:00+00:00", "Old"], [_T2, "Older"]])

        make_sheet(sheets_client).append(
            NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc), description="New")
        )

        # Rows 2 and 3 are taken, so the new note goes in row 4 -- and the
        # existing rows are never rewritten (which could clobber a
        # concurrent compaction stamp on one of them).
        sheets_client.write_rows_in_sheet.assert_called_once_with(
            "sheet-1", _SHEET_ID, "A4:C", [[_T1, "New", ""]]
        )

    def test_appends_to_an_empty_sheet(self):
        sheets_client = _sheets([])

        make_sheet(sheets_client).append(
            NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc))
        )

        sheets_client.write_rows_in_sheet.assert_called_once_with(
            "sheet-1", _SHEET_ID, "A2:C", [[_T1, "", ""]]
        )


def _id(timestamp: str, row: int) -> str:
    return f"{timestamp}#{row}"


class TestNotedTimeSheetMarkCompacted:
    def test_stamps_only_the_compaction_id_column_of_the_given_rows(self):
        sheets_client = _sheets([[_T1, "A"], [_T2, "B"]])

        make_sheet(sheets_client).mark_compacted([_id(_T2, 3)], "cmp1")

        sheets_client.write_rows_in_sheet.assert_called_once_with(
            "sheet-1", _SHEET_ID, "C3:C3", [["cmp1"]]
        )

    def test_writes_one_range_per_contiguous_run_of_rows(self):
        rows = [[_T1, "a"], [_T1, "b"], [_T1, "c"], [_T1, "d"], [_T1, "e"], [_T1, "f"]]
        sheets_client = _sheets(rows)

        make_sheet(sheets_client).mark_compacted(
            [_id(_T1, 7), _id(_T1, 2), _id(_T1, 3), _id(_T1, 4), _id(_T1, 3)], "cmp1"
        )

        assert [c.args[2:] for c in sheets_client.write_rows_in_sheet.call_args_list] == [
            ("C2:C4", [["cmp1"], ["cmp1"], ["cmp1"]]),
            ("C7:C7", [["cmp1"]]),
        ]

    def test_finds_the_column_from_the_header_not_a_fixed_position(self):
        sheets_client = _sheets([["cmp?", _T1, "A"]], header=["compaction_id", "timestamp", "description"])
        sheets_client.read_rows_in_sheet.side_effect = lambda s, i, rng: {
            "A1:C1": [["compaction_id", "timestamp", "description"]],
            "A2:C": [["", _T1, "A"]],
        }[rng]

        make_sheet(sheets_client).mark_compacted([_id(_T1, 2)], "cmp1")

        assert sheets_client.write_rows_in_sheet.call_args.args[2] == "A2:A2"

    def test_refuses_when_the_row_now_holds_a_different_note(self):
        sheets_client = _sheets([[_T2, "edited since it was read"]])

        with pytest.raises(ValueError, match="no longer holds the note"):
            make_sheet(sheets_client).mark_compacted([_id(_T1, 2)], "cmp1")

        sheets_client.write_rows_in_sheet.assert_not_called()

    def test_refuses_when_the_row_is_gone(self):
        sheets_client = _sheets([])

        with pytest.raises(ValueError, match="no longer holds the note"):
            make_sheet(sheets_client).mark_compacted([_id(_T1, 5)], "cmp1")

    def test_writes_nothing_if_any_one_note_is_stale(self):
        sheets_client = _sheets([[_T1, "A"], [_T2, "B"]])

        with pytest.raises(ValueError):
            make_sheet(sheets_client).mark_compacted([_id(_T1, 2), _id(_T1, 3)], "cmp1")

        sheets_client.write_rows_in_sheet.assert_not_called()

    def test_refuses_a_note_already_compacted_by_a_different_compaction(self):
        sheets_client = _sheets([[_T1, "A", "other"]])

        with pytest.raises(ValueError, match="already compacted by 'other'"):
            make_sheet(sheets_client).mark_compacted([_id(_T1, 2)], "cmp1")

    def test_stamping_again_with_the_same_compaction_is_harmless(self):
        sheets_client = _sheets([[_T1, "A", "cmp1"]])

        make_sheet(sheets_client).mark_compacted([_id(_T1, 2)], "cmp1")

        sheets_client.write_rows_in_sheet.assert_called_once()

    def test_does_nothing_for_no_notes(self):
        sheets_client = _sheets([])

        make_sheet(sheets_client).mark_compacted([], "cmp1")

        sheets_client.write_rows_in_sheet.assert_not_called()
