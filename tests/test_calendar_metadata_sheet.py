from unittest.mock import MagicMock

from utilities import calendar_metadata_sheet
from utilities.calendar_metadata_sheet import ensure_spreadsheet, ensure_tab

_NEW_KEY = "calendar-metadata-spreadsheet-id"


class TestEnsureSpreadsheet:
    def test_reuses_an_already_tracked_spreadsheet(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = "sheet-1"
        sheets_client = MagicMock()

        spreadsheet_id, is_new = ensure_spreadsheet(calendar_client, sheets_client)

        assert (spreadsheet_id, is_new) == ("sheet-1", False)
        calendar_client.get_calendar_metadata.assert_called_once_with(_NEW_KEY)
        sheets_client.create_spreadsheet.assert_not_called()
        calendar_client.set_calendar_metadata.assert_not_called()

    def test_creates_a_new_spreadsheet_when_nothing_is_tracked(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = None
        sheets_client = MagicMock()
        sheets_client.create_spreadsheet.return_value = "new-sheet"

        spreadsheet_id, is_new = ensure_spreadsheet(calendar_client, sheets_client)

        assert (spreadsheet_id, is_new) == ("new-sheet", True)
        sheets_client.create_spreadsheet.assert_called_once_with(calendar_metadata_sheet.SPREADSHEET_TITLE)
        calendar_client.set_calendar_metadata.assert_called_once_with(_NEW_KEY, "new-sheet")


class TestEnsureTab:
    def test_reuses_an_already_tagged_tab(self):
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = 42

        sheet_id, created = ensure_tab(sheets_client, "sheet-1", role="compactions", title="Compactions")

        assert (sheet_id, created) == (42, False)
        sheets_client.find_sheet_id.assert_called_once_with("sheet-1", "sheet-role", "compactions")
        sheets_client.add_sheet.assert_not_called()
        sheets_client.update_sheet_properties.assert_not_called()
        sheets_client.create_sheet_metadata.assert_not_called()

    def test_adds_a_new_tab_when_untagged(self):
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = None
        sheets_client.add_sheet.return_value = 99

        sheet_id, created = ensure_tab(
            sheets_client, "sheet-1", role="uncompacted-time-notes", title="Uncompacted Time Notes"
        )

        assert (sheet_id, created) == (99, True)
        sheets_client.add_sheet.assert_called_once_with(
            "sheet-1", "Uncompacted Time Notes", tab_color=calendar_metadata_sheet._TAB_COLOR
        )
        sheets_client.update_sheet_properties.assert_not_called()
        sheets_client.create_sheet_metadata.assert_called_once_with(
            "sheet-1", 99, "sheet-role", "uncompacted-time-notes"
        )
