from unittest.mock import MagicMock

import pytest

from utilities.event_label_sheet import EventLabelSheet

_SHEET_ID_KEY = "event-label-sheet-id"


def make_sheet(calendar_client=None, sheets_client=None) -> EventLabelSheet:
    return EventLabelSheet(calendar_client or MagicMock(), sheets_client or MagicMock())


class TestCreateSheet:
    def test_creates_sheet_with_header_and_narrow_id_column(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = None
        sheets_client = MagicMock()
        sheets_client.create_spreadsheet.return_value = "sheet-1"
        event_label_sheet = make_sheet(calendar_client, sheets_client)

        result = event_label_sheet.create_sheet("My Labels")

        assert result == "sheet-1"
        sheets_client.create_spreadsheet.assert_called_once_with("My Labels")
        sheets_client.set_column_width.assert_called_once_with(
            "sheet-1", sheet_id=0, column_index=0, pixel_width=60
        )
        sheets_client.write_rows.assert_called_once_with(
            "sheet-1", "Sheet1!A1:D1", [["ID", "Name", "Background Color", "Priority"]]
        )

    def test_records_the_new_sheet_id_on_the_calendar(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = None
        sheets_client = MagicMock()
        sheets_client.create_spreadsheet.return_value = "sheet-1"
        event_label_sheet = make_sheet(calendar_client, sheets_client)

        event_label_sheet.create_sheet()

        calendar_client.set_calendar_metadata.assert_called_once_with(_SHEET_ID_KEY, "sheet-1")

    def test_writes_given_initial_rows(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = None
        sheets_client = MagicMock()
        sheets_client.create_spreadsheet.return_value = "sheet-1"
        event_label_sheet = make_sheet(calendar_client, sheets_client)

        event_label_sheet.create_sheet(initial_rows=[["l1", "Design Work", "#8e24aa", ""]])

        assert sheets_client.write_rows.call_args_list[-1].args == (
            "sheet-1",
            "Sheet1!A2:D",
            [["l1", "Design Work", "#8e24aa", ""]],
        )

    def test_skips_data_write_when_no_initial_rows_given(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = None
        sheets_client = MagicMock()
        sheets_client.create_spreadsheet.return_value = "sheet-1"
        event_label_sheet = make_sheet(calendar_client, sheets_client)

        event_label_sheet.create_sheet()

        # Only the header row write -- no second write_rows call for data.
        assert sheets_client.write_rows.call_count == 1

    def test_defaults_title(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = None
        sheets_client = MagicMock()
        sheets_client.create_spreadsheet.return_value = "sheet-1"
        event_label_sheet = make_sheet(calendar_client, sheets_client)

        event_label_sheet.create_sheet()

        assert sheets_client.create_spreadsheet.call_args.args[0] == "Event Labels"

    def test_raises_when_a_sheet_is_already_tracked(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = "existing-sheet"
        sheets_client = MagicMock()
        event_label_sheet = make_sheet(calendar_client, sheets_client)

        with pytest.raises(ValueError):
            event_label_sheet.create_sheet()

        sheets_client.create_spreadsheet.assert_not_called()
        calendar_client.set_calendar_metadata.assert_not_called()


class TestFindSheet:
    def test_delegates_to_calendar_client_metadata(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = "sheet-1"
        event_label_sheet = make_sheet(calendar_client=calendar_client)

        assert event_label_sheet.find_sheet() == "sheet-1"
        calendar_client.get_calendar_metadata.assert_called_once_with(_SHEET_ID_KEY)

    def test_returns_none_when_not_found(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = None
        event_label_sheet = make_sheet(calendar_client=calendar_client)

        assert event_label_sheet.find_sheet() is None


class TestResolveSheetId:
    def test_returns_tracked_sheet_id(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = "sheet-1"
        event_label_sheet = make_sheet(calendar_client=calendar_client)

        assert event_label_sheet.resolve_sheet_id() == "sheet-1"

    def test_raises_when_no_sheet_is_tracked(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = None
        event_label_sheet = make_sheet(calendar_client=calendar_client)

        with pytest.raises(ValueError):
            event_label_sheet.resolve_sheet_id()


class TestReadRows:
    def test_delegates_to_sheets_client_with_data_range(self):
        sheets_client = MagicMock()
        sheets_client.read_rows.return_value = [["l1", "Design Work", "#8e24aa", "1"]]
        event_label_sheet = make_sheet(sheets_client=sheets_client)

        rows = event_label_sheet.read_rows("sheet-1")

        assert rows == [["l1", "Design Work", "#8e24aa", "1"]]
        sheets_client.read_rows.assert_called_once_with("sheet-1", "Sheet1!A2:D")


class TestWriteRows:
    def test_delegates_to_sheets_client_with_data_range(self):
        sheets_client = MagicMock()
        event_label_sheet = make_sheet(sheets_client=sheets_client)

        event_label_sheet.write_rows("sheet-1", [["l1", "Design Work", "#8e24aa", "1"]])

        sheets_client.write_rows.assert_called_once_with(
            "sheet-1", "Sheet1!A2:D", [["l1", "Design Work", "#8e24aa", "1"]]
        )
