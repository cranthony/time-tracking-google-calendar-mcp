from unittest.mock import MagicMock

import pytest

from calendar_clients.google_sheets import SheetsClient


def make_client(sheets_service: MagicMock) -> SheetsClient:
    return SheetsClient(sheets_service)


class TestCreateSpreadsheet:
    def test_creates_spreadsheet_and_returns_its_id(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.create.return_value.execute.return_value = {
            "spreadsheetId": "sheet-1"
        }
        client = make_client(sheets_service)

        spreadsheet_id = client.create_spreadsheet("Event Labels")

        assert spreadsheet_id == "sheet-1"
        sheets_service.spreadsheets.return_value.create.assert_called_once_with(
            body={"properties": {"title": "Event Labels"}}, fields="spreadsheetId"
        )


class TestRenameSpreadsheet:
    def test_sends_update_spreadsheet_properties_request(self):
        sheets_service = MagicMock()
        client = make_client(sheets_service)

        client.rename_spreadsheet("sheet-1", "Calendar Metadata")

        sheets_service.spreadsheets.return_value.batchUpdate.assert_called_once_with(
            spreadsheetId="sheet-1",
            body={
                "requests": [
                    {
                        "updateSpreadsheetProperties": {
                            "properties": {"title": "Calendar Metadata"},
                            "fields": "title",
                        }
                    }
                ]
            },
        )


class TestAddSheet:
    def test_sends_add_sheet_request_and_returns_new_sheet_id(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {
            "replies": [{"addSheet": {"properties": {"sheetId": 42}}}]
        }
        client = make_client(sheets_service)

        sheet_id = client.add_sheet("sheet-1", "Uncompacted Time Notes")

        assert sheet_id == 42
        sheets_service.spreadsheets.return_value.batchUpdate.assert_called_once_with(
            spreadsheetId="sheet-1",
            body={"requests": [{"addSheet": {"properties": {"title": "Uncompacted Time Notes"}}}]},
        )

    def test_includes_tab_color_when_given(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {
            "replies": [{"addSheet": {"properties": {"sheetId": 42}}}]
        }
        client = make_client(sheets_service)

        client.add_sheet("sheet-1", "Uncompacted Time Notes", tab_color={"red": 0.26})

        sheets_service.spreadsheets.return_value.batchUpdate.assert_called_once_with(
            spreadsheetId="sheet-1",
            body={
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": "Uncompacted Time Notes",
                                "tabColor": {"red": 0.26},
                            }
                        }
                    }
                ]
            },
        )


class TestUpdateSheetProperties:
    def test_updates_title_only(self):
        sheets_service = MagicMock()
        client = make_client(sheets_service)

        client.update_sheet_properties("sheet-1", 0, title="Event Labels")

        sheets_service.spreadsheets.return_value.batchUpdate.assert_called_once_with(
            spreadsheetId="sheet-1",
            body={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {"sheetId": 0, "title": "Event Labels"},
                            "fields": "title",
                        }
                    }
                ]
            },
        )

    def test_updates_title_and_tab_color_together(self):
        sheets_service = MagicMock()
        client = make_client(sheets_service)

        client.update_sheet_properties("sheet-1", 0, title="Event Labels", tab_color={"red": 0.26})

        sheets_service.spreadsheets.return_value.batchUpdate.assert_called_once_with(
            spreadsheetId="sheet-1",
            body={
                "requests": [
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": 0,
                                "title": "Event Labels",
                                "tabColor": {"red": 0.26},
                            },
                            "fields": "title,tabColor",
                        }
                    }
                ]
            },
        )

    def test_does_nothing_when_neither_is_given(self):
        sheets_service = MagicMock()
        client = make_client(sheets_service)

        client.update_sheet_properties("sheet-1", 0)

        sheets_service.spreadsheets.return_value.batchUpdate.assert_not_called()


class TestGetSheetTitle:
    def test_returns_the_title_of_the_matching_sheet_id(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [
                {"properties": {"sheetId": 0, "title": "Sheet1"}},
                {"properties": {"sheetId": 42, "title": "Uncompacted Time Notes"}},
            ]
        }
        client = make_client(sheets_service)

        title = client.get_sheet_title("sheet-1", 42)

        assert title == "Uncompacted Time Notes"
        sheets_service.spreadsheets.return_value.get.assert_called_once_with(
            spreadsheetId="sheet-1", fields="sheets.properties"
        )

    def test_raises_when_sheet_id_is_not_found(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"sheetId": 0, "title": "Sheet1"}}]
        }
        client = make_client(sheets_service)

        with pytest.raises(ValueError):
            client.get_sheet_title("sheet-1", 99)


class TestCreateSheetMetadata:
    def test_sends_create_developer_metadata_request(self):
        sheets_service = MagicMock()
        client = make_client(sheets_service)

        client.create_sheet_metadata("sheet-1", 42, "sheet-role", "event-labels")

        sheets_service.spreadsheets.return_value.batchUpdate.assert_called_once_with(
            spreadsheetId="sheet-1",
            body={
                "requests": [
                    {
                        "createDeveloperMetadata": {
                            "developerMetadata": {
                                "metadataKey": "sheet-role",
                                "metadataValue": "event-labels",
                                "location": {"sheetId": 42},
                                "visibility": "PROJECT",
                            }
                        }
                    }
                ]
            },
        )


class TestFindSheetId:
    def test_returns_sheet_id_of_first_match(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.developerMetadata.return_value.search.return_value.execute.return_value = {
            "matchedDeveloperMetadata": [{"developerMetadata": {"location": {"sheetId": 42}}}]
        }
        client = make_client(sheets_service)

        sheet_id = client.find_sheet_id("sheet-1", "sheet-role", "event-labels")

        assert sheet_id == 42
        sheets_service.spreadsheets.return_value.developerMetadata.return_value.search.assert_called_once_with(
            spreadsheetId="sheet-1",
            body={
                "dataFilters": [
                    {"developerMetadataLookup": {"metadataKey": "sheet-role", "metadataValue": "event-labels"}}
                ]
            },
        )

    def test_returns_none_when_no_match(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.developerMetadata.return_value.search.return_value.execute.return_value = {}
        client = make_client(sheets_service)

        assert client.find_sheet_id("sheet-1", "sheet-role", "event-labels") is None


class TestReadRowsInSheet:
    def test_qualifies_range_with_current_sheet_title(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"sheetId": 42, "title": "Event Labels"}}]
        }
        sheets_service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["l1", "Design Work", "#8e24aa", "1"]]
        }
        client = make_client(sheets_service)

        rows = client.read_rows_in_sheet("sheet-1", 42, "A2:D")

        assert rows == [["l1", "Design Work", "#8e24aa", "1"]]
        sheets_service.spreadsheets.return_value.values.return_value.get.assert_called_once_with(
            spreadsheetId="sheet-1", range="'Event Labels'!A2:D"
        )

    def test_escapes_single_quotes_in_the_sheet_title(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"sheetId": 42, "title": "Chris's Labels"}}]
        }
        sheets_service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {}
        client = make_client(sheets_service)

        client.read_rows_in_sheet("sheet-1", 42, "A2:D")

        sheets_service.spreadsheets.return_value.values.return_value.get.assert_called_once_with(
            spreadsheetId="sheet-1", range="'Chris''s Labels'!A2:D"
        )


class TestWriteRowsInSheet:
    def test_qualifies_range_with_current_sheet_title(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"sheetId": 42, "title": "Event Labels"}}]
        }
        client = make_client(sheets_service)

        client.write_rows_in_sheet("sheet-1", 42, "A2:D", [["l1", "Design Work", "#8e24aa", "1"]])

        sheets_service.spreadsheets.return_value.values.return_value.update.assert_called_once_with(
            spreadsheetId="sheet-1",
            range="'Event Labels'!A2:D",
            valueInputOption="RAW",
            body={"values": [["l1", "Design Work", "#8e24aa", "1"]]},
        )


class TestSetColumnWidth:
    def test_sends_update_dimension_properties_request(self):
        sheets_service = MagicMock()
        client = make_client(sheets_service)

        client.set_column_width("sheet-1", sheet_id=0, column_index=0, pixel_width=60)

        sheets_service.spreadsheets.return_value.batchUpdate.assert_called_once_with(
            spreadsheetId="sheet-1",
            body={
                "requests": [
                    {
                        "updateDimensionProperties": {
                            "range": {
                                "sheetId": 0,
                                "dimension": "COLUMNS",
                                "startIndex": 0,
                                "endIndex": 1,
                            },
                            "properties": {"pixelSize": 60},
                            "fields": "pixelSize",
                        }
                    }
                ]
            },
        )


class TestReadRows:
    def test_returns_values_from_response(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["l1", "Design Work", "#8e24aa", "1"]]
        }
        client = make_client(sheets_service)

        rows = client.read_rows("sheet-1", "Sheet1!A2:D")

        assert rows == [["l1", "Design Work", "#8e24aa", "1"]]
        sheets_service.spreadsheets.return_value.values.return_value.get.assert_called_once_with(
            spreadsheetId="sheet-1", range="Sheet1!A2:D"
        )

    def test_returns_empty_list_when_no_values(self):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {}
        client = make_client(sheets_service)

        assert client.read_rows("sheet-1", "Sheet1!A2:D") == []


class TestWriteRows:
    def test_updates_values_with_raw_input_option(self):
        sheets_service = MagicMock()
        client = make_client(sheets_service)

        client.write_rows("sheet-1", "Sheet1!A2:D", [["l1", "Design Work", "#8e24aa", "1"]])

        sheets_service.spreadsheets.return_value.values.return_value.update.assert_called_once_with(
            spreadsheetId="sheet-1",
            range="Sheet1!A2:D",
            valueInputOption="RAW",
            body={"values": [["l1", "Design Work", "#8e24aa", "1"]]},
        )
