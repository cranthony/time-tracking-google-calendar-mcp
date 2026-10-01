from unittest.mock import MagicMock

import httplib2
import pytest
from googleapiclient.errors import HttpError

from calendar_clients.google_sheets import SheetsClient, _execute, cached_sheet_reads


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


class TestDeleteRows:
    def test_sends_delete_dimension_request_with_zero_based_half_open_range(self):
        sheets_service = MagicMock()
        client = make_client(sheets_service)

        client.delete_rows("sheet-1", 42, start_row=2, end_row=5)

        sheets_service.spreadsheets.return_value.batchUpdate.assert_called_once_with(
            spreadsheetId="sheet-1",
            body={
                "requests": [
                    {
                        "deleteDimension": {
                            "range": {
                                "sheetId": 42,
                                "dimension": "ROWS",
                                "startIndex": 1,
                                "endIndex": 5,
                            }
                        }
                    }
                ],
                "includeSpreadsheetInResponse": True,
                "responseIncludeGridData": False,
            },
            fields="updatedSpreadsheet(sheets(properties(sheetId,gridProperties(rowCount))))",
        )

    @staticmethod
    def _rows_left(sheets_service, row_count):
        sheets_service.spreadsheets.return_value.batchUpdate.return_value.execute.return_value = {
            "updatedSpreadsheet": {
                "sheets": [
                    {"properties": {"sheetId": 7, "gridProperties": {"rowCount": 5000}}},
                    {"properties": {"sheetId": 42, "gridProperties": {"rowCount": row_count}}},
                ]
            }
        }

    def test_adds_rows_back_when_the_tab_is_left_with_too_few(self):
        sheets_service = MagicMock()
        self._rows_left(sheets_service, 940)
        client = make_client(sheets_service)

        client.delete_rows("sheet-1", 42, start_row=2, end_row=61, keep_at_least=1000)

        batch_update = sheets_service.spreadsheets.return_value.batchUpdate
        assert batch_update.call_count == 2
        assert batch_update.call_args.kwargs == {
            "spreadsheetId": "sheet-1",
            "body": {
                "requests": [
                    {"appendDimension": {"sheetId": 42, "dimension": "ROWS", "length": 60}}
                ]
            },
        }

    @pytest.mark.parametrize("row_count", [1000, 1200])
    def test_adds_nothing_when_the_tab_still_has_enough(self, row_count):
        sheets_service = MagicMock()
        self._rows_left(sheets_service, row_count)
        client = make_client(sheets_service)

        client.delete_rows("sheet-1", 42, start_row=2, end_row=61, keep_at_least=1000)

        assert sheets_service.spreadsheets.return_value.batchUpdate.call_count == 1

    def test_a_single_row_range_deletes_just_that_row(self):
        sheets_service = MagicMock()
        client = make_client(sheets_service)

        client.delete_rows("sheet-1", 42, start_row=7, end_row=7)

        range_ = sheets_service.spreadsheets.return_value.batchUpdate.call_args.kwargs["body"][
            "requests"
        ][0]["deleteDimension"]["range"]
        assert (range_["startIndex"], range_["endIndex"]) == (6, 7)


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
    def test_reads_by_sheet_id_without_looking_up_the_title(self):
        sheets_service = MagicMock()
        values = sheets_service.spreadsheets.return_value.values.return_value
        values.batchGetByDataFilter.return_value.execute.return_value = {
            "valueRanges": [{"valueRange": {"values": [["l1", "Design Work", "#8e24aa", "1"]]}}]
        }
        client = make_client(sheets_service)

        rows = client.read_rows_in_sheet("sheet-1", 42, "A2:D")

        assert rows == [["l1", "Design Work", "#8e24aa", "1"]]
        values.batchGetByDataFilter.assert_called_once_with(
            spreadsheetId="sheet-1",
            body={
                "dataFilters": [
                    {
                        "gridRange": {
                            "sheetId": 42,
                            "startRowIndex": 1,
                            "startColumnIndex": 0,
                            "endColumnIndex": 4,
                        }
                    }
                ],
                "majorDimension": "ROWS",
            },
        )
        sheets_service.spreadsheets.return_value.get.assert_not_called()

    def test_a_bounded_range_gets_an_end_row(self):
        sheets_service = MagicMock()
        values = sheets_service.spreadsheets.return_value.values.return_value
        values.batchGetByDataFilter.return_value.execute.return_value = {}
        client = make_client(sheets_service)

        client.read_rows_in_sheet("sheet-1", 42, "B5:AA7")

        grid_range = values.batchGetByDataFilter.call_args.kwargs["body"]["dataFilters"][0]["gridRange"]
        assert grid_range == {
            "sheetId": 42,
            "startRowIndex": 4,
            "endRowIndex": 7,
            "startColumnIndex": 1,
            "endColumnIndex": 27,
        }

    @pytest.mark.parametrize(
        "response", [{}, {"valueRanges": [{"valueRange": {}}]}], ids=["no match", "empty range"]
    )
    def test_returns_no_rows_when_the_range_is_empty(self, response):
        sheets_service = MagicMock()
        values = sheets_service.spreadsheets.return_value.values.return_value
        values.batchGetByDataFilter.return_value.execute.return_value = response
        client = make_client(sheets_service)

        assert client.read_rows_in_sheet("sheet-1", 42, "A2:D") == []


def _http_error(status: int) -> HttpError:
    return HttpError(httplib2.Response({"status": status}), b"{}")


class TestWriteRowsInSheet:
    ROWS = [["l1", "Design Work", "#8e24aa", "1"], ["l2", "Email", "", "2"]]

    def _service(self, *, updated_cells=None, error=None):
        sheets_service = MagicMock()
        sheets_service.spreadsheets.return_value.get.return_value.execute.return_value = {
            "sheets": [{"properties": {"sheetId": 42, "title": "Chris's Labels"}}]
        }
        by_filter = sheets_service.spreadsheets.return_value.values.return_value.batchUpdateByDataFilter
        if error is not None:
            by_filter.return_value.execute.side_effect = error
        else:
            by_filter.return_value.execute.return_value = {"totalUpdatedCells": updated_cells}
        return sheets_service

    def test_writes_by_sheet_id_bounded_to_the_rows_given(self):
        sheets_service = self._service(updated_cells=8)
        client = make_client(sheets_service)

        client.write_rows_in_sheet("sheet-1", 42, "A2:D", self.ROWS)

        values = sheets_service.spreadsheets.return_value.values.return_value
        values.batchUpdateByDataFilter.assert_called_once_with(
            spreadsheetId="sheet-1",
            body={
                "valueInputOption": "RAW",
                "data": [
                    {
                        "dataFilter": {
                            "gridRange": {
                                "sheetId": 42,
                                "startRowIndex": 1,
                                "endRowIndex": 3,
                                "startColumnIndex": 0,
                                "endColumnIndex": 4,
                            }
                        },
                        "majorDimension": "ROWS",
                        "values": self.ROWS,
                    }
                ],
            },
        )
        sheets_service.spreadsheets.return_value.get.assert_not_called()
        values.update.assert_not_called()

    @pytest.mark.parametrize(
        "outcome",
        [{"error": _http_error(400)}, {"updated_cells": 0}],
        ids=["refused", "wrote nothing"],
    )
    def test_falls_back_to_the_title_qualified_range_past_the_end_of_the_grid(self, outcome):
        sheets_service = self._service(**outcome)
        client = make_client(sheets_service)

        client.write_rows_in_sheet("sheet-1", 42, "A2:D", self.ROWS)

        sheets_service.spreadsheets.return_value.values.return_value.update.assert_called_once_with(
            spreadsheetId="sheet-1",
            range="'Chris''s Labels'!A2:D",
            valueInputOption="RAW",
            body={"values": self.ROWS},
        )

    def test_other_errors_are_not_swallowed(self):
        sheets_service = self._service(error=_http_error(403))
        client = make_client(sheets_service)

        with pytest.raises(HttpError):
            client.write_rows_in_sheet("sheet-1", 42, "A2:D", self.ROWS)

        sheets_service.spreadsheets.return_value.values.return_value.update.assert_not_called()

    def test_writing_no_rows_sends_nothing(self):
        sheets_service = MagicMock()
        client = make_client(sheets_service)

        client.write_rows_in_sheet("sheet-1", 42, "A2:D", [])

        assert sheets_service.mock_calls == []


def _read_counting_service(values_by_call):
    """A MagicMock Sheets service whose batchGetByDataFilter answers
    successive reads with successive entries of `values_by_call`."""
    sheets_service = MagicMock()
    values = sheets_service.spreadsheets.return_value.values.return_value
    values.batchGetByDataFilter.return_value.execute.side_effect = [
        {"valueRanges": [{"valueRange": {"values": v}}]} for v in values_by_call
    ]
    values.batchUpdateByDataFilter.return_value.execute.return_value = {"totalUpdatedCells": 1}
    return sheets_service, values.batchGetByDataFilter


class TestCachedSheetReads:
    def test_repeated_reads_in_a_scope_are_served_from_memory(self):
        sheets_service, read = _read_counting_service([[["a"]]])
        client = make_client(sheets_service)

        with cached_sheet_reads():
            first = client.read_rows_in_sheet("sheet-1", 42, "A2:D")
            second = client.read_rows_in_sheet("sheet-1", 42, "A2:D")

        assert first == second == [["a"]]
        assert read.call_count == 1

    def test_the_cache_is_shared_across_clients(self):
        sheets_service, read = _read_counting_service([[["a"]]])

        with cached_sheet_reads():
            make_client(sheets_service).read_rows_in_sheet("sheet-1", 42, "A2:D")
            make_client(sheets_service).read_rows_in_sheet("sheet-1", 42, "A2:D")

        assert read.call_count == 1

    def test_different_ranges_and_tabs_are_read_separately(self):
        sheets_service, read = _read_counting_service([[["a"]], [["b"]], [["c"]]])
        client = make_client(sheets_service)

        with cached_sheet_reads():
            client.read_rows_in_sheet("sheet-1", 42, "A2:D")
            client.read_rows_in_sheet("sheet-1", 42, "A1:D1")
            client.read_rows_in_sheet("sheet-1", 43, "A2:D")

        assert read.call_count == 3

    def test_a_caller_mutating_what_it_read_does_not_change_the_cache(self):
        sheets_service, _ = _read_counting_service([[["a"]]])
        client = make_client(sheets_service)

        with cached_sheet_reads():
            client.read_rows_in_sheet("sheet-1", 42, "A2:D")[0].append("mutated")
            client.read_rows_in_sheet("sheet-1", 42, "A2:D").append(["mutated"])

            assert client.read_rows_in_sheet("sheet-1", 42, "A2:D") == [["a"]]

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda c: c.write_rows_in_sheet("sheet-1", 42, "A5:A5", [["x"]]),
            lambda c: c.delete_rows("sheet-1", 42, start_row=2, end_row=2),
            lambda c: c.write_rows("sheet-1", "'Tab'!A5", [["x"]]),
        ],
        ids=["write_rows_in_sheet", "delete_rows", "write_rows"],
    )
    def test_a_write_to_the_tab_through_any_client_forgets_its_cached_reads(self, mutate):
        sheets_service, read = _read_counting_service([[["a"]], [["b"]]])

        with cached_sheet_reads():
            make_client(sheets_service).read_rows_in_sheet("sheet-1", 42, "A2:D")
            mutate(make_client(sheets_service))
            rows = make_client(sheets_service).read_rows_in_sheet("sheet-1", 42, "A2:D")

        assert rows == [["b"]]
        assert read.call_count == 2

    def test_a_write_to_another_tab_keeps_this_tabs_cached_reads(self):
        sheets_service, read = _read_counting_service([[["a"]]])
        client = make_client(sheets_service)

        with cached_sheet_reads():
            client.read_rows_in_sheet("sheet-1", 42, "A2:D")
            client.write_rows_in_sheet("sheet-1", 43, "A2:D", [["x"]])
            client.read_rows_in_sheet("sheet-1", 42, "A2:D")

        assert read.call_count == 1

    def test_nothing_is_cached_outside_a_scope_or_across_scopes(self):
        sheets_service, read = _read_counting_service([[["a"]], [["b"]], [["c"]]])
        client = make_client(sheets_service)

        with cached_sheet_reads():
            client.read_rows_in_sheet("sheet-1", 42, "A2:D")
        with cached_sheet_reads():
            client.read_rows_in_sheet("sheet-1", 42, "A2:D")
        client.read_rows_in_sheet("sheet-1", 42, "A2:D")

        assert read.call_count == 3

    def test_a_nested_scope_shares_the_outer_ones_cache(self):
        sheets_service, read = _read_counting_service([[["a"]]])
        client = make_client(sheets_service)

        with cached_sheet_reads():
            client.read_rows_in_sheet("sheet-1", 42, "A2:D")
            with cached_sheet_reads():
                client.read_rows_in_sheet("sheet-1", 42, "A2:D")
            client.read_rows_in_sheet("sheet-1", 42, "A2:D")

        assert read.call_count == 1


class TestExecute:
    def test_retries_a_rate_limited_request_with_growing_backoff(self):
        request = MagicMock()
        request.execute.side_effect = [_http_error(429), _http_error(429), {"ok": True}]
        sleeps = []

        result = _execute(request, sleep=sleeps.append, rand=lambda: 1.0)

        assert result == {"ok": True}
        assert sleeps == [1, 2]

    def test_gives_up_after_about_a_minute_of_retries(self):
        request = MagicMock()
        request.execute.side_effect = _http_error(429)
        sleeps = []

        with pytest.raises(HttpError):
            _execute(request, sleep=sleeps.append, rand=lambda: 1.0)

        assert sum(sleeps) == 63

    @pytest.mark.parametrize("status", [400, 403, 500, 503])
    def test_never_retries_anything_but_a_rate_limit(self, status):
        request = MagicMock()
        request.execute.side_effect = _http_error(status)
        sleeps = []

        with pytest.raises(HttpError):
            _execute(request, sleep=sleeps.append)

        assert request.execute.call_count == 1
        assert sleeps == []


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
