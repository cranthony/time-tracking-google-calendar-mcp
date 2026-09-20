from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from utilities import calendar_metadata_sheet
from utilities.noted_time_sheet import NotedTime, NotedTimeSheet

_HEADER_ROW = ["timestamp", "description"]
_SHEET_ID = 42


def make_sheet(sheets_client=None, spreadsheet_id: str = "sheet-1", sheet_id: int = _SHEET_ID) -> NotedTimeSheet:
    return NotedTimeSheet(sheets_client or MagicMock(), spreadsheet_id, sheet_id)


class TestNotedTimeFromRow:
    def test_parses_a_full_row(self):
        noted_time = NotedTime.from_row(_HEADER_ROW, ["2026-01-01T09:00:00+00:00", "Started work"])

        assert noted_time == NotedTime(
            timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc), description="Started work"
        )

    def test_blank_description_becomes_none(self):
        noted_time = NotedTime.from_row(_HEADER_ROW, ["2026-01-01T09:00:00+00:00", ""])

        assert noted_time.description is None

    def test_missing_trailing_cells_become_none(self):
        # Sheets omits trailing blank cells from a row entirely.
        noted_time = NotedTime.from_row(_HEADER_ROW, ["2026-01-01T09:00:00+00:00"])

        assert noted_time == NotedTime(
            timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc), description=None
        )

    def test_ignores_unknown_columns(self):
        noted_time = NotedTime.from_row(
            ["timestamp", "description", "notes"],
            ["2026-01-01T09:00:00+00:00", "Started work", "some note"],
        )

        assert noted_time == NotedTime(
            timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc), description="Started work"
        )

    def test_column_order_does_not_matter(self):
        noted_time = NotedTime.from_row(
            ["description", "timestamp"], ["Started work", "2026-01-01T09:00:00+00:00"]
        )

        assert noted_time == NotedTime(
            timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc), description="Started work"
        )

    def test_raises_when_timestamp_is_missing(self):
        with pytest.raises(ValueError):
            NotedTime.from_row(_HEADER_ROW, ["", "Started work"])


class TestNotedTimeToRow:
    def test_writes_timestamp_as_isoformat_and_description_as_a_string(self):
        noted_time = NotedTime(
            timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc), description="Started work"
        )

        assert noted_time.to_row(_HEADER_ROW) == ["2026-01-01T09:00:00+00:00", "Started work"]

    def test_blank_description_becomes_an_empty_string(self):
        noted_time = NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc))

        assert noted_time.to_row(_HEADER_ROW) == ["2026-01-01T09:00:00+00:00", ""]

    def test_preserves_unknown_columns_from_the_original_row(self):
        noted_time = NotedTime(
            timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc), description="Started work"
        )

        row = noted_time.to_row(
            ["timestamp", "description", "notes"],
            ["2025-01-01T00:00:00+00:00", "Old", "some note"],
        )

        assert row == ["2026-01-01T09:00:00+00:00", "Started work", "some note"]

    def test_blanks_unknown_columns_when_no_original_row(self):
        noted_time = NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc))

        row = noted_time.to_row(["timestamp", "notes"], None)

        assert row == ["2026-01-01T09:00:00+00:00", ""]


class TestNotedTimeSheetEnsure:
    def test_reuses_an_already_tagged_tab_without_writing_anything(self):
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = _SHEET_ID

        sheet = NotedTimeSheet.ensure(sheets_client, "sheet-1")

        assert sheet.spreadsheet_id == "sheet-1"
        sheets_client.add_sheet.assert_not_called()
        sheets_client.write_rows_in_sheet.assert_not_called()

    def test_creates_and_tags_a_new_tab_with_a_header_row(self):
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
            "sheet-1", 99, "A1:B1", [_HEADER_ROW]
        )


class TestNotedTimeSheetSpreadsheetId:
    def test_exposes_the_given_id(self):
        noted_time_sheet = make_sheet(spreadsheet_id="sheet-1")

        assert noted_time_sheet.spreadsheet_id == "sheet-1"


class TestNotedTimeSheetRead:
    def test_reads_and_parses_data_rows(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: {
            "A1:B1": [_HEADER_ROW],
            "A2:B": [["2026-01-01T09:00:00+00:00", "Started work"]],
        }[rng]
        noted_time_sheet = make_sheet(sheets_client)

        noted_times = noted_time_sheet.read()

        assert noted_times == [
            NotedTime(
                timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc), description="Started work"
            )
        ]

    def test_sorts_by_timestamp_regardless_of_row_order(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: {
            "A1:B1": [_HEADER_ROW],
            "A2:B": [
                ["2026-01-02T09:00:00+00:00", "Second"],
                ["2026-01-01T09:00:00+00:00", "First"],
            ],
        }[rng]
        noted_time_sheet = make_sheet(sheets_client)

        noted_times = noted_time_sheet.read()

        assert [n.description for n in noted_times] == ["First", "Second"]

    def test_returns_empty_list_when_no_data_rows(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: {
            "A1:B1": [_HEADER_ROW],
            "A2:B": [],
        }[rng]
        noted_time_sheet = make_sheet(sheets_client)

        assert noted_time_sheet.read() == []

    def test_raises_when_header_is_missing_expected_columns(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.return_value = [["timestamp"]]
        noted_time_sheet = make_sheet(sheets_client)

        with pytest.raises(ValueError):
            noted_time_sheet.read()


class TestNotedTimeSheetWrite:
    def test_overwrites_data_rows_preserving_unknown_columns(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: {
            "A1:B1": [_HEADER_ROW],
            "A2:B": [["2025-01-01T00:00:00+00:00", "Old"]],
        }[rng]
        noted_time_sheet = make_sheet(sheets_client)

        noted_time_sheet.write(
            [NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc), description="New")]
        )

        sheets_client.write_rows_in_sheet.assert_called_once_with(
            "sheet-1", _SHEET_ID, "A2:B", [["2026-01-01T09:00:00+00:00", "New"]]
        )

    def test_handles_more_notes_than_previous_rows(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: {
            "A1:B1": [_HEADER_ROW],
            "A2:B": [],
        }[rng]
        noted_time_sheet = make_sheet(sheets_client)

        noted_time_sheet.write([NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc))])

        sheets_client.write_rows_in_sheet.assert_called_once_with(
            "sheet-1", _SHEET_ID, "A2:B", [["2026-01-01T09:00:00+00:00", ""]]
        )


class TestNotedTimeSheetAppend:
    def test_adds_a_new_row_after_existing_rows(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: {
            "A1:B1": [_HEADER_ROW],
            "A2:B": [["2025-01-01T00:00:00+00:00", "Old"]],
        }[rng]
        noted_time_sheet = make_sheet(sheets_client)

        noted_time_sheet.append(
            NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc), description="New")
        )

        sheets_client.write_rows_in_sheet.assert_called_once_with(
            "sheet-1",
            _SHEET_ID,
            "A2:B",
            [["2025-01-01T00:00:00+00:00", "Old"], ["2026-01-01T09:00:00+00:00", "New"]],
        )

    def test_appends_to_an_empty_sheet(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: {
            "A1:B1": [_HEADER_ROW],
            "A2:B": [],
        }[rng]
        noted_time_sheet = make_sheet(sheets_client)

        noted_time_sheet.append(NotedTime(timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)))

        sheets_client.write_rows_in_sheet.assert_called_once_with(
            "sheet-1", _SHEET_ID, "A2:B", [["2026-01-01T09:00:00+00:00", ""]]
        )


class TestNotedTimeSheetClear:
    def test_clears_the_data_range_and_returns_the_cleared_notes(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: {
            "A1:B1": [_HEADER_ROW],
            "A2:B": [
                ["2026-01-01T09:00:00+00:00", "Started work"],
                ["2025-12-31T08:00:00+00:00", "Earlier note"],
            ],
        }[rng]
        noted_time_sheet = make_sheet(sheets_client)

        cleared = noted_time_sheet.clear()

        # Sorted by timestamp, same as read().
        assert cleared == [
            NotedTime(
                timestamp=datetime(2025, 12, 31, 8, 0, 0, tzinfo=timezone.utc),
                description="Earlier note",
            ),
            NotedTime(
                timestamp=datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc), description="Started work"
            ),
        ]
        sheets_client.clear_rows_in_sheet.assert_called_once_with("sheet-1", _SHEET_ID, "A2:B")

    def test_returns_empty_list_when_nothing_to_clear(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: {
            "A1:B1": [_HEADER_ROW],
            "A2:B": [],
        }[rng]
        noted_time_sheet = make_sheet(sheets_client)

        cleared = noted_time_sheet.clear()

        assert cleared == []
        sheets_client.clear_rows_in_sheet.assert_called_once_with("sheet-1", _SHEET_ID, "A2:B")
