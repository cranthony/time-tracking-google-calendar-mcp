from unittest.mock import MagicMock

from calendar_clients.google_sheets import SheetsClient


def make_client(sheets_service: MagicMock, drive_service: MagicMock) -> SheetsClient:
    return SheetsClient(sheets_service, drive_service)


class TestCreateSpreadsheet:
    def test_creates_and_tags_spreadsheet(self):
        sheets_service = MagicMock()
        drive_service = MagicMock()
        sheets_service.spreadsheets.return_value.create.return_value.execute.return_value = {
            "spreadsheetId": "sheet-1"
        }
        client = make_client(sheets_service, drive_service)

        spreadsheet_id = client.create_spreadsheet("Event Labels", {"kind": "event-labels"})

        assert spreadsheet_id == "sheet-1"
        sheets_service.spreadsheets.return_value.create.assert_called_once_with(
            body={"properties": {"title": "Event Labels"}}, fields="spreadsheetId"
        )
        drive_service.files.return_value.update.assert_called_once_with(
            fileId="sheet-1", body={"appProperties": {"kind": "event-labels"}}
        )


class TestFindSpreadsheet:
    def test_builds_query_from_properties_and_returns_first_match(self):
        sheets_service = MagicMock()
        drive_service = MagicMock()
        drive_service.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "sheet-1"}, {"id": "sheet-2"}]
        }
        client = make_client(sheets_service, drive_service)

        result = client.find_spreadsheet({"kind": "event-labels"})

        assert result == "sheet-1"
        drive_service.files.return_value.list.assert_called_once_with(
            q=(
                "mimeType='application/vnd.google-apps.spreadsheet' and trashed=false and "
                "appProperties has {key='kind' and value='event-labels'}"
            ),
            orderBy="modifiedTime desc",
            pageSize=1,
            fields="files(id)",
        )

    def test_returns_none_when_no_match(self):
        sheets_service = MagicMock()
        drive_service = MagicMock()
        drive_service.files.return_value.list.return_value.execute.return_value = {"files": []}
        client = make_client(sheets_service, drive_service)

        assert client.find_spreadsheet({"kind": "event-labels"}) is None

    def test_escapes_single_quotes_in_property_values(self):
        sheets_service = MagicMock()
        drive_service = MagicMock()
        drive_service.files.return_value.list.return_value.execute.return_value = {"files": []}
        client = make_client(sheets_service, drive_service)

        client.find_spreadsheet({"kind": "it's-mine"})

        query = drive_service.files.return_value.list.call_args.kwargs["q"]
        assert "it\\'s-mine" in query


class TestSetColumnWidth:
    def test_sends_update_dimension_properties_request(self):
        sheets_service = MagicMock()
        drive_service = MagicMock()
        client = make_client(sheets_service, drive_service)

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
        drive_service = MagicMock()
        sheets_service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {
            "values": [["l1", "Design Work", "#8e24aa", "1"]]
        }
        client = make_client(sheets_service, drive_service)

        rows = client.read_rows("sheet-1", "Sheet1!A2:D")

        assert rows == [["l1", "Design Work", "#8e24aa", "1"]]
        sheets_service.spreadsheets.return_value.values.return_value.get.assert_called_once_with(
            spreadsheetId="sheet-1", range="Sheet1!A2:D"
        )

    def test_returns_empty_list_when_no_values(self):
        sheets_service = MagicMock()
        drive_service = MagicMock()
        sheets_service.spreadsheets.return_value.values.return_value.get.return_value.execute.return_value = {}
        client = make_client(sheets_service, drive_service)

        assert client.read_rows("sheet-1", "Sheet1!A2:D") == []


class TestWriteRows:
    def test_updates_values_with_raw_input_option(self):
        sheets_service = MagicMock()
        drive_service = MagicMock()
        client = make_client(sheets_service, drive_service)

        client.write_rows("sheet-1", "Sheet1!A2:D", [["l1", "Design Work", "#8e24aa", "1"]])

        sheets_service.spreadsheets.return_value.values.return_value.update.assert_called_once_with(
            spreadsheetId="sheet-1",
            range="Sheet1!A2:D",
            valueInputOption="RAW",
            body={"values": [["l1", "Design Work", "#8e24aa", "1"]]},
        )
