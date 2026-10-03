from datetime import datetime, timezone
from unittest.mock import MagicMock, call

import pytest

from tests.fake_sheets import FakeSheets
from utilities import calendar_metadata_sheet
from utilities.noted_time_sheet import NotedTime, NotedTimeSheet, SheetNote, parse_note_id

_HEADER_ROW = ["timestamp", "description", "compaction_id"]
_SHEET_ID = 42
_T1 = "2026-01-01T09:00:00+00:00"
_T2 = "2026-01-01T10:00:00+00:00"


def make_sheet(
    sheets_client=None,
    spreadsheet_id: str = "sheet-1",
    sheet_id: int = _SHEET_ID,
) -> NotedTimeSheet:
    return NotedTimeSheet(sheets_client or MagicMock(), spreadsheet_id, sheet_id)


def _sheets(rows, header=None):
    """A SheetsClient mock backed by an in-memory header/data range."""
    state = {"A1:C1": [header or _HEADER_ROW], "A2:C": rows}
    sheets_client = _reading_ranges_one_by_one(MagicMock())
    sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: state[rng]
    return sheets_client


def _reading_ranges_one_by_one(sheets_client):
    """`sheets_client`, a mock, with `read_ranges_in_sheet` answered by
    its `read_rows_in_sheet`, range by range."""
    sheets_client.read_ranges_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, ranges: [
        sheets_client.read_rows_in_sheet(spreadsheet_id, sheet_id, rng) for rng in ranges
    ]
    return sheets_client


def _requests(client):
    """The ranges each read request asked for, for a mock wrapping a
    FakeSheets -- `read_rows_in_sheet` reads one range per request,
    `read_ranges_in_sheet` several."""
    return [
        [args[2]] if name == "read_rows_in_sheet" else list(args[2])
        for name, args, _kwargs in client.mock_calls
        if name in ("read_rows_in_sheet", "read_ranges_in_sheet")
    ]


def _ranges_read(client):
    return [rng for ranges in _requests(client) for rng in ranges]


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

        assert sheets_client.add_sheet.call_args_list == [
            call(
                "sheet-1",
                calendar_metadata_sheet.TIME_NOTES_SHEET_TITLE,
                tab_color=calendar_metadata_sheet._TAB_COLOR,
            ),
        ]
        assert sheets_client.create_sheet_metadata.call_args_list == [
            call("sheet-1", 99, "sheet-role", calendar_metadata_sheet.TIME_NOTES_SHEET_ROLE),
        ]
        assert sheets_client.write_rows_in_sheet.call_args_list == [
            call("sheet-1", 99, "A1:C1", [_HEADER_ROW]),
        ]


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
        sheets_client = _reading_ranges_one_by_one(MagicMock())
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


class TestNotedTimeSheetReadsTheWholeTab:
    def _sheet(self, rows):
        fake = FakeSheets()
        fake.write_rows_in_sheet("sheet-1", _SHEET_ID, "A1:C1", [_HEADER_ROW])
        fake.write_rows_in_sheet("sheet-1", _SHEET_ID, "A2:C", rows)
        client = MagicMock(wraps=fake)
        return client, NotedTimeSheet(client, "sheet-1", _SHEET_ID)

    def test_reads_the_header_and_every_row_in_one_request(self):
        client, sheet = self._sheet([[_T1, "a", "cmp1"], [_T2, "open"]])

        notes, latest = sheet.read_with_latest_compacted()

        assert [n.note.description for n in notes] == ["open"]
        assert latest.description == "a"
        assert _requests(client) == [["A1:C1", "A2:C"]]

    def test_the_latest_compacted_note_has_the_latest_timestamp(self):
        _client, sheet = self._sheet([[_T2, "later", "cmp1"], [_T1, "earlier", "cmp1"], [_T2, "open"]])

        assert sheet.read_with_latest_compacted()[1].description == "later"

    def test_of_two_compacted_notes_at_the_same_time_the_later_row_is_the_latest(self):
        _client, sheet = self._sheet([[_T1, "first", "cmp1"], [_T1, "second", "cmp1"]])

        assert sheet.read_with_latest_compacted()[1].description == "second"

    def test_there_is_no_latest_compacted_note_when_nothing_is_compacted(self):
        _client, sheet = self._sheet([[_T1, "open"], []])

        assert sheet.read_with_latest_compacted()[1] is None


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

    def test_reads_the_tab_once_in_one_request(self):
        fake = FakeSheets()
        fake.write_rows_in_sheet("sheet-1", _SHEET_ID, "A1:C1", [_HEADER_ROW])
        fake.write_rows_in_sheet("sheet-1", _SHEET_ID, "A2:C", [[_T1, "a"], [], [_T1, "c"]])
        client = MagicMock(wraps=fake)

        appended = NotedTimeSheet(client, "sheet-1", _SHEET_ID).append(
            NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc))
        )

        # After the last row, even with a blank one before it.
        assert appended.row == 5
        assert _requests(client) == [["A1:C1", "A2:C"]]


class TestNotedTimeSheetGarbageCollect:
    _OLD = "2025-12-31T09:00:00+00:00"

    def _sheet(self, rows):
        fake = FakeSheets()
        fake.write_rows_in_sheet("sheet-1", _SHEET_ID, "A1:C1", [_HEADER_ROW])
        fake.write_rows_in_sheet("sheet-1", _SHEET_ID, "A2:C", rows)
        return fake, NotedTimeSheet(fake, "sheet-1", _SHEET_ID)

    def test_does_nothing_under_the_row_budget(self):
        rows = [[_T1, "a", "cmp1"] for _ in range(50)]
        fake, sheet = self._sheet(rows)

        sheet.garbage_collect()

        assert fake.read_rows_in_sheet("sheet-1", _SHEET_ID, "A2:C") == rows

    def test_deletes_the_oldest_compacted_rows_once_over_budget(self):
        # 55 compacted, then one uncompacted -- 56 in all, over the 50-row
        # budget, so it's trimmed to 25.
        fake, sheet = self._sheet([[_T1, "a", "cmp1"] for _ in range(55)] + [[_T2, "recent"]])

        sheet.garbage_collect()

        remaining = fake.read_rows_in_sheet("sheet-1", _SHEET_ID, "A2:C")
        assert len(remaining) == 25
        assert remaining[-1] == [_T2, "recent"]

    def test_always_keeps_the_latest_compacted_note(self):
        # The latest compacted note is row 4, inside the 31 rows that
        # would otherwise go -- so only the two rows before it do, and it
        # and everything after it are kept, for the next compaction's
        # `previous_note`.
        rows = (
            [[self._OLD, "a", "cmp1"], [self._OLD, "b", "cmp1"], [_T2, "latest", "cmp1"]]
            + [[_T1, f"older {i}", "cmp1"] for i in range(52)]
            + [[_T2, "open"]]
        )
        fake, sheet = self._sheet(rows)

        sheet.garbage_collect()

        remaining = fake.read_rows_in_sheet("sheet-1", _SHEET_ID, "A2:C")
        assert len(remaining) == 54
        assert remaining[0] == [_T2, "latest", "cmp1"]
        assert sheet.read_with_latest_compacted()[1].description == "latest"

    def test_leaves_the_tab_at_least_1000_rows_long(self):
        # Earlier garbage collection already shrank the tab's grid; without
        # topping it back up, it would eventually run out of rows to append
        # into.
        fake, sheet = self._sheet([[_T1, "a", "cmp1"] for _ in range(55)] + [[_T2, "recent"]])
        fake.row_counts[_SHEET_ID] = 100

        sheet.garbage_collect()

        assert fake.row_count(_SHEET_ID) == 1000

    def test_does_nothing_if_the_oldest_row_is_already_uncompacted(self):
        fake, sheet = self._sheet([[_T1, "a"]] + [[_T1, "b", "cmp1"] for _ in range(55)])

        sheet.garbage_collect()

        assert len(fake.read_rows_in_sheet("sheet-1", _SHEET_ID, "A2:C")) == 56

    def test_deletes_only_the_compacted_prefix_when_it_falls_short_of_the_excess(self):
        # An uncompacted note (row 5) sits within the 31 rows over budget --
        # only the 3 compacted rows before it are safe to delete.
        fake, sheet = self._sheet(
            [[_T1, "a", "cmp1"] for _ in range(3)] + [[_T1, "b"]] + [[_T1, "c", "cmp1"] for _ in range(52)]
        )

        sheet.garbage_collect()

        assert len(fake.read_rows_in_sheet("sheet-1", _SHEET_ID, "A2:C")) == 53

    def test_blank_rows_count_and_are_deleted_with_the_prefix(self):
        fake, sheet = self._sheet([[], [_T1, "a", "cmp1"]] * 27 + [[_T2, "open"]])

        sheet.garbage_collect()

        remaining = fake.read_rows_in_sheet("sheet-1", _SHEET_ID, "A2:C")
        assert len(remaining) == 25
        assert remaining[-1] == [_T2, "open"]

    def test_append_garbage_collects_first_from_the_same_read(self):
        fake, _ = self._sheet([[_T1, "a", "cmp1"] for _ in range(55)])
        client = MagicMock(wraps=fake)

        appended = NotedTimeSheet(client, "sheet-1", _SHEET_ID).append(
            NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc))
        )

        remaining = fake.read_rows_in_sheet("sheet-1", _SHEET_ID, "A2:C")
        assert len(remaining) == 26  # trimmed from 55 to 25, + 1 appended
        assert remaining[-1] == [_T1]
        assert appended.row == 27
        assert len(_requests(client)) == 1


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


def _fake_sheet(rows):
    """A NotedTimeSheet over an in-memory FakeSheets tab holding `rows`."""
    sheets = FakeSheets()
    sheets.write_rows_in_sheet("sheet-1", _SHEET_ID, "A1:C1", [_HEADER_ROW])
    if rows:
        sheets.write_rows_in_sheet("sheet-1", _SHEET_ID, f"A2:C{len(rows) + 1}", rows)
    return make_sheet(sheets)


class TestNotedTimeSheetAppendReturnsTheRow:
    def test_returns_the_note_with_the_row_it_went_in(self):
        sheet = _fake_sheet([[_T1, "a"]])

        appended = sheet.append(NotedTime(timestamp=datetime.fromisoformat(_T2), description="b"))

        assert appended.id == _id(_T2, 3)
        assert [n.id for n in sheet.read_with_rows()] == [_id(_T1, 2), _id(_T2, 3)]


class TestNotedTimeSheetEdit:
    def test_changes_the_description_in_place(self):
        sheet = _fake_sheet([[_T1, "a"], [_T2, "b"]])

        edited = sheet.edit(_id(_T1, 2), description="started work")

        assert edited.id == _id(_T1, 2)
        notes = sheet.read_with_rows()
        assert [(n.id, n.note.description) for n in notes] == [
            (_id(_T1, 2), "started work"),
            (_id(_T2, 3), "b"),
        ]

    def test_changing_the_timestamp_changes_the_id_but_not_the_row(self):
        sheet = _fake_sheet([[_T1, "a"], [_T2, "b"]])
        later = "2026-01-01T09:30:00+00:00"

        edited = sheet.edit(_id(_T1, 2), timestamp=datetime.fromisoformat(later))

        assert edited.id == _id(later, 2)
        assert edited.note.description == "a"
        assert [n.id for n in sheet.read_with_rows()] == [_id(later, 2), _id(_T2, 3)]

    def test_an_empty_description_clears_it(self):
        sheet = _fake_sheet([[_T1, "a"]])

        sheet.edit(_id(_T1, 2), description="  ")

        assert sheet.read_with_rows()[0].note.description is None

    def test_refuses_a_stale_id(self):
        sheet = _fake_sheet([[_T2, "edited since it was read"]])

        with pytest.raises(ValueError, match="no longer holds the note"):
            sheet.edit(_id(_T1, 2), description="x")

    def test_refuses_a_compacted_note(self):
        sheet = _fake_sheet([[_T1, "a", "cmp1"]])

        with pytest.raises(ValueError, match="already compacted"):
            sheet.edit(_id(_T1, 2), description="x")

        assert sheet.read_with_rows(include_compacted=True)[0].note.description == "a"


class TestNotedTimeSheetDelete:
    def test_blanks_the_row_without_moving_any_other_note(self):
        sheet = _fake_sheet([[_T1, "a"], [_T2, "b"], [_T2, "c"]])

        deleted = sheet.delete(_id(_T2, 3))

        assert deleted.description == "b"
        assert [(n.id, n.note.description) for n in sheet.read_with_rows()] == [
            (_id(_T1, 2), "a"),
            (_id(_T2, 4), "c"),
        ]

    def test_a_deleted_id_is_stale_afterward(self):
        sheet = _fake_sheet([[_T1, "a"]])
        sheet.delete(_id(_T1, 2))

        with pytest.raises(ValueError, match="no longer holds the note"):
            sheet.delete(_id(_T1, 2))

    def test_refuses_a_compacted_note(self):
        sheet = _fake_sheet([[_T1, "a", "cmp1"]])

        with pytest.raises(ValueError, match="already compacted"):
            sheet.delete(_id(_T1, 2))

    def test_the_next_note_still_appends_after_the_last_one(self):
        sheet = _fake_sheet([[_T1, "a"], [_T2, "b"]])
        sheet.delete(_id(_T1, 2))

        appended = sheet.append(NotedTime(timestamp=datetime.fromisoformat(_T2), description="c"))

        assert appended.id == _id(_T2, 4)
