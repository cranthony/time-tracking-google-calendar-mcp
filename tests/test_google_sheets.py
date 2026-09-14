from unittest.mock import MagicMock

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
