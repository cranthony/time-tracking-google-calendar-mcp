from unittest.mock import MagicMock

import pytest

from calendar_clients.google_calendar import EventLabel
from utilities.event_label_sheet import EventLabelSheet

_SHEET_ID_KEY = "event-label-sheet-id"


def make_sheet(calendar_client=None, sheets_client=None) -> EventLabelSheet:
    return EventLabelSheet(calendar_client or MagicMock(), sheets_client or MagicMock())


class TestCreateSheet:
    def test_creates_sheet_with_header_and_narrow_id_column(self):
        calendar_client = MagicMock()
        calendar_client.list_event_labels.return_value = []
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
        calendar_client.list_event_labels.return_value = []
        sheets_client = MagicMock()
        sheets_client.create_spreadsheet.return_value = "sheet-1"
        event_label_sheet = make_sheet(calendar_client, sheets_client)

        event_label_sheet.create_sheet()

        calendar_client.set_calendar_metadata.assert_called_once_with(_SHEET_ID_KEY, "sheet-1")

    def test_prepopulates_current_labels(self):
        calendar_client = MagicMock()
        calendar_client.list_event_labels.return_value = [
            EventLabel(id="l1", background_color="#8e24aa", name="Design Work"),
            EventLabel(id="l2", background_color="#d50000"),
        ]
        sheets_client = MagicMock()
        sheets_client.create_spreadsheet.return_value = "sheet-1"
        event_label_sheet = make_sheet(calendar_client, sheets_client)

        event_label_sheet.create_sheet()

        assert sheets_client.write_rows.call_args_list[-1].args == (
            "sheet-1",
            "Sheet1!A2:D",
            [["l1", "Design Work", "#8e24aa", ""], ["l2", "", "#d50000", ""]],
        )

    def test_skips_data_write_when_no_labels_exist(self):
        calendar_client = MagicMock()
        calendar_client.list_event_labels.return_value = []
        sheets_client = MagicMock()
        sheets_client.create_spreadsheet.return_value = "sheet-1"
        event_label_sheet = make_sheet(calendar_client, sheets_client)

        event_label_sheet.create_sheet()

        # Only the header row write -- no second write_rows call for data.
        assert sheets_client.write_rows.call_count == 1

    def test_defaults_title(self):
        sheets_client = MagicMock()
        sheets_client.create_spreadsheet.return_value = "sheet-1"
        event_label_sheet = make_sheet(sheets_client=sheets_client)

        event_label_sheet.create_sheet()

        assert sheets_client.create_spreadsheet.call_args.args[0] == "Event Labels"


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


class TestSyncFromSheet:
    def test_raises_when_no_sheet_found_and_none_given(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = None
        event_label_sheet = make_sheet(calendar_client=calendar_client)

        with pytest.raises(ValueError):
            event_label_sheet.sync_from_sheet()

    def test_uses_explicit_spreadsheet_id_without_calling_find(self):
        calendar_client = MagicMock()
        calendar_client.replace_event_labels.return_value = []
        sheets_client = MagicMock()
        sheets_client.read_rows.return_value = []
        event_label_sheet = make_sheet(calendar_client, sheets_client)

        event_label_sheet.sync_from_sheet("explicit-sheet")

        calendar_client.get_calendar_metadata.assert_not_called()
        sheets_client.read_rows.assert_called_once_with("explicit-sheet", "Sheet1!A2:D")

    def test_creates_labels_for_blank_id_rows_and_writes_back_new_ids(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = "sheet-1"
        calendar_client.replace_event_labels.return_value = [
            EventLabel(id="new-id", background_color="#8e24aa", name="Design Work"),
        ]
        sheets_client = MagicMock()
        sheets_client.read_rows.return_value = [["", "Design Work", "#8e24aa", "2"]]
        event_label_sheet = make_sheet(calendar_client, sheets_client)

        result = event_label_sheet.sync_from_sheet()

        sent_labels = calendar_client.replace_event_labels.call_args.args[0]
        assert sent_labels == [
            EventLabel(id=None, background_color="#8e24aa", name="Design Work", priority=2)
        ]
        assert result == [
            EventLabel(id="new-id", background_color="#8e24aa", name="Design Work", priority=2)
        ]
        sheets_client.write_rows.assert_called_once_with(
            "sheet-1", "Sheet1!A2:D", [["new-id", "Design Work", "#8e24aa", "2"]]
        )

    def test_overwrites_existing_label_matched_by_id(self):
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = "sheet-1"
        calendar_client.replace_event_labels.return_value = [
            EventLabel(id="l1", background_color="#000000", name="Renamed"),
        ]
        sheets_client = MagicMock()
        sheets_client.read_rows.return_value = [["l1", "Renamed", "#000000", ""]]
        event_label_sheet = make_sheet(calendar_client, sheets_client)

        result = event_label_sheet.sync_from_sheet()

        assert result == [
            EventLabel(id="l1", background_color="#000000", name="Renamed", priority=None)
        ]

    def test_deletes_labels_missing_from_the_sheet(self):
        # A label not present in any sheet row simply isn't included in
        # what's sent to replace_event_labels -- CalendarClient.
        # replace_event_labels is what actually removes it.
        calendar_client = MagicMock()
        calendar_client.get_calendar_metadata.return_value = "sheet-1"
        calendar_client.replace_event_labels.return_value = []
        sheets_client = MagicMock()
        sheets_client.read_rows.return_value = []
        event_label_sheet = make_sheet(calendar_client, sheets_client)

        result = event_label_sheet.sync_from_sheet()

        calendar_client.replace_event_labels.assert_called_once_with([])
        assert result == []


class TestListLabelsWithPriority:
    def test_returns_plain_labels_when_no_sheet_found(self):
        calendar_client = MagicMock()
        calendar_client.list_event_labels.return_value = [
            EventLabel(id="l1", background_color="#8e24aa", name="Design Work")
        ]
        calendar_client.get_calendar_metadata.return_value = None
        sheets_client = MagicMock()
        event_label_sheet = make_sheet(calendar_client, sheets_client)

        result = event_label_sheet.list_labels_with_priority()

        assert result[0].priority is None
        sheets_client.read_rows.assert_not_called()

    def test_fills_in_priority_from_matching_sheet_row(self):
        calendar_client = MagicMock()
        calendar_client.list_event_labels.return_value = [
            EventLabel(id="l1", background_color="#8e24aa", name="Design Work"),
            EventLabel(id="l2", background_color="#d50000", name="Untracked"),
        ]
        calendar_client.get_calendar_metadata.return_value = "sheet-1"
        sheets_client = MagicMock()
        sheets_client.read_rows.return_value = [["l1", "Design Work", "#8e24aa", "1"]]
        event_label_sheet = make_sheet(calendar_client, sheets_client)

        result = event_label_sheet.list_labels_with_priority()

        assert result[0].priority == 1
        assert result[1].priority is None

    def test_uses_explicit_spreadsheet_id_without_calling_find(self):
        calendar_client = MagicMock()
        calendar_client.list_event_labels.return_value = []
        sheets_client = MagicMock()
        sheets_client.read_rows.return_value = []
        event_label_sheet = make_sheet(calendar_client, sheets_client)

        event_label_sheet.list_labels_with_priority("explicit-sheet")

        calendar_client.get_calendar_metadata.assert_not_called()
        sheets_client.read_rows.assert_called_once_with("explicit-sheet", "Sheet1!A2:D")
