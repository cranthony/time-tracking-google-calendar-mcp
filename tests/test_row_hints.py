from unittest.mock import MagicMock

from tests.fake_sheets import FakeSheets
from utilities import calendar_metadata_sheet
from utilities.row_hints import RowHints

_SHEET_ID = 9


def _hints(sheets=None) -> tuple[RowHints, FakeSheets]:
    sheets = sheets or FakeSheets()
    return RowHints(sheets, "spreadsheet-1", _SHEET_ID), sheets


class TestEnsure:
    def test_creates_and_tags_the_tab_with_a_header_row(self):
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = None
        sheets_client.add_sheet.return_value = 55

        RowHints.ensure(sheets_client, "spreadsheet-1")

        sheets_client.add_sheet.assert_called_once_with(
            "spreadsheet-1", "Row Hints", tab_color=calendar_metadata_sheet._TAB_COLOR
        )
        sheets_client.create_sheet_metadata.assert_called_once_with(
            "spreadsheet-1", 55, "sheet-role", "row-hints"
        )
        sheets_client.write_rows_in_sheet.assert_called_once_with(
            "spreadsheet-1", 55, "A1:B1", [["hint", "row"]]
        )

    def test_reuses_an_existing_tab(self):
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = 55

        RowHints.ensure(sheets_client, "spreadsheet-1")

        sheets_client.add_sheet.assert_not_called()
        sheets_client.write_rows_in_sheet.assert_not_called()


class TestGetAndSet:
    def test_an_unset_hint_is_none(self):
        hints, _ = _hints()

        assert hints.get("notes_next_row") is None

    def test_round_trips_a_hint(self):
        hints, _ = _hints()

        hints.set("notes_next_row", 42)

        assert hints.get("notes_next_row") == 42

    def test_distinct_hints_get_their_own_rows(self):
        hints, sheets = _hints()

        hints.set("notes_next_row", 42)
        hints.set("journal_next_row", 7)

        assert hints.get("notes_next_row") == 42
        assert hints.get("journal_next_row") == 7
        assert sheets.read_rows_in_sheet("spreadsheet-1", _SHEET_ID, "A2:B") == [
            ["notes_next_row", "42"],
            ["journal_next_row", "7"],
        ]

    def test_setting_again_overwrites_in_place_rather_than_appending(self):
        hints, sheets = _hints()
        hints.set("notes_next_row", 42)

        hints.set("notes_next_row", 43)

        assert hints.get("notes_next_row") == 43
        assert sheets.read_rows_in_sheet("spreadsheet-1", _SHEET_ID, "A2:B") == [
            ["notes_next_row", "43"]
        ]
