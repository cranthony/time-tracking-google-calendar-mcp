from unittest.mock import MagicMock, call

from utilities import calendar_metadata_sheet
from utilities.calendar_metadata_sheet import ensure_spreadsheet, ensure_tab

_NEW_KEY = "calendar-metadata-spreadsheet-id"
_LEGACY_KEY = "event-label-sheet-id"


class TestEnsureSpreadsheet:
    def test_reuses_an_already_tracked_spreadsheet(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = "sheet-1"
        sheets_client = MagicMock()

        spreadsheet_id, is_new = ensure_spreadsheet(calendar_client, sheets_client)

        assert (spreadsheet_id, is_new) == ("sheet-1", False)
        calendar_client.get_calendar_metadata.assert_called_once_with(_NEW_KEY)
        sheets_client.create_spreadsheet.assert_not_called()
        sheets_client.rename_spreadsheet.assert_not_called()
        calendar_client.set_calendar_metadata.assert_not_called()

    def test_adopts_a_legacy_spreadsheet_when_present(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.side_effect = lambda key: {
            _NEW_KEY: None,
            _LEGACY_KEY: "legacy-sheet",
        }[key]
        sheets_client = MagicMock()

        spreadsheet_id, is_new = ensure_spreadsheet(calendar_client, sheets_client)

        assert (spreadsheet_id, is_new) == ("legacy-sheet", False)
        sheets_client.rename_spreadsheet.assert_called_once_with(
            "legacy-sheet", calendar_metadata_sheet.SPREADSHEET_TITLE
        )
        sheets_client.create_spreadsheet.assert_not_called()
        calendar_client.set_calendar_metadata.assert_has_calls(
            [call(_NEW_KEY, "legacy-sheet"), call(_LEGACY_KEY, None)]
        )

    def test_creates_a_new_spreadsheet_when_nothing_is_tracked(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = None
        sheets_client = MagicMock()
        sheets_client.create_spreadsheet.return_value = "new-sheet"

        spreadsheet_id, is_new = ensure_spreadsheet(calendar_client, sheets_client)

        assert (spreadsheet_id, is_new) == ("new-sheet", True)
        sheets_client.create_spreadsheet.assert_called_once_with(calendar_metadata_sheet.SPREADSHEET_TITLE)
        calendar_client.set_calendar_metadata.assert_called_once_with(_NEW_KEY, "new-sheet")
        sheets_client.rename_spreadsheet.assert_not_called()


class TestEnsureTab:
    def test_reuses_an_already_tagged_tab(self):
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = 42

        sheet_id, created = ensure_tab(sheets_client, "sheet-1", role="event-labels", title="Event Labels")

        assert (sheet_id, created) == (42, False)
        sheets_client.find_sheet_id.assert_called_once_with("sheet-1", "sheet-role", "event-labels")
        sheets_client.add_sheet.assert_not_called()
        sheets_client.update_sheet_properties.assert_not_called()
        sheets_client.create_sheet_metadata.assert_not_called()

    def test_adopts_reuse_sheet_id_when_given_and_untagged(self):
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = None

        sheet_id, created = ensure_tab(
            sheets_client, "sheet-1", role="event-labels", title="Event Labels", reuse_sheet_id=0
        )

        assert (sheet_id, created) == (0, True)
        sheets_client.update_sheet_properties.assert_called_once_with(
            "sheet-1", 0, title="Event Labels", tab_color=calendar_metadata_sheet._TAB_COLOR
        )
        sheets_client.add_sheet.assert_not_called()
        sheets_client.create_sheet_metadata.assert_called_once_with(
            "sheet-1", 0, "sheet-role", "event-labels"
        )

    def test_adds_a_new_tab_when_untagged_and_nothing_to_reuse(self):
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
