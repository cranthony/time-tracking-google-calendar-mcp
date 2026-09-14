from unittest.mock import MagicMock

import pytest

from calendar_clients.google_calendar import EventLabel as RawEventLabel
from utilities.event_label_sheet import DEFAULT_SHEET_TITLE, EventLabel, EventLabelSheet

_HEADER_ROW = ["id", "name", "background_color", "priority"]


def make_sheet(sheets_client=None, spreadsheet_id: str = "sheet-1") -> EventLabelSheet:
    return EventLabelSheet(sheets_client or MagicMock(), spreadsheet_id)


class TestEventLabelFromRaw:
    def test_copies_every_raw_field_and_leaves_priority_none(self):
        raw = RawEventLabel(id="l1", background_color="#8e24aa", name="Design Work")

        label = EventLabel.from_raw(raw)

        assert label == EventLabel(
            id="l1", background_color="#8e24aa", name="Design Work", priority=None
        )


class TestEventLabelToRaw:
    def test_keeps_explicit_background_color(self):
        label = EventLabel(id="l1", name="Design Work", background_color="#123456", priority=1)

        raw = label.to_raw()

        assert raw == RawEventLabel(id="l1", name="Design Work", background_color="#123456")

    def test_derives_background_color_from_priority_when_unset(self):
        label = EventLabel(name="Design Work", priority=1)

        raw = label.to_raw()

        assert raw.background_color == "#fbd75b"

    def test_defaults_background_color_when_neither_given(self):
        label = EventLabel(name="No color, no priority")

        raw = label.to_raw()

        assert raw.background_color == "#a4bdfc"


class TestEventLabelFromRow:
    def test_parses_a_full_row(self):
        label = EventLabel.from_row(_HEADER_ROW, ["l1", "Design Work", "#8e24aa", "1"])

        assert label == EventLabel(
            id="l1", name="Design Work", background_color="#8e24aa", priority=1
        )

    def test_blank_cells_become_none(self):
        label = EventLabel.from_row(_HEADER_ROW, ["", "", "", ""])

        assert label == EventLabel(id=None, name=None, background_color=None, priority=None)

    def test_missing_trailing_cells_become_none(self):
        # Sheets omits trailing blank cells from a row entirely.
        label = EventLabel.from_row(_HEADER_ROW, ["l1"])

        assert label == EventLabel(id="l1", name=None, background_color=None, priority=None)

    def test_ignores_unknown_columns(self):
        label = EventLabel.from_row(
            ["id", "name", "background_color", "priority", "notes"],
            ["l1", "Design Work", "#8e24aa", "1", "some note"],
        )

        assert label == EventLabel(
            id="l1", name="Design Work", background_color="#8e24aa", priority=1
        )

    def test_column_order_does_not_matter(self):
        label = EventLabel.from_row(
            ["priority", "id", "background_color", "name"],
            ["1", "l1", "#8e24aa", "Design Work"],
        )

        assert label == EventLabel(
            id="l1", name="Design Work", background_color="#8e24aa", priority=1
        )


class TestEventLabelToRow:
    def test_writes_every_field_as_a_string(self):
        label = EventLabel(id="l1", name="Design Work", background_color="#8e24aa", priority=1)

        assert label.to_row(_HEADER_ROW) == ["l1", "Design Work", "#8e24aa", "1"]

    def test_blank_fields_become_empty_strings(self):
        label = EventLabel()

        assert label.to_row(_HEADER_ROW) == ["", "", "", ""]

    def test_preserves_unknown_columns_from_the_original_row(self):
        label = EventLabel(id="l1", name="Design Work", background_color="#8e24aa", priority=1)

        row = label.to_row(
            ["id", "name", "background_color", "priority", "notes"],
            ["l1", "Old Name", "#000000", "2", "some note"],
        )

        assert row == ["l1", "Design Work", "#8e24aa", "1", "some note"]

    def test_blanks_unknown_columns_when_no_original_row(self):
        label = EventLabel(id="l1")

        row = label.to_row(["id", "notes"], None)

        assert row == ["l1", ""]


class TestEventLabelSheetCreate:
    def test_creates_spreadsheet_narrows_id_column_and_writes_header(self):
        sheets_client = MagicMock()
        sheets_client.create_spreadsheet.return_value = "sheet-1"

        spreadsheet_id = EventLabelSheet.create(sheets_client, "My Labels")

        assert spreadsheet_id == "sheet-1"
        sheets_client.create_spreadsheet.assert_called_once_with("My Labels")
        sheets_client.set_column_width.assert_called_once_with(
            "sheet-1", sheet_id=0, column_index=0, pixel_width=60
        )
        sheets_client.write_rows.assert_called_once_with("sheet-1", "A1:D1", [_HEADER_ROW])

    def test_defaults_title(self):
        sheets_client = MagicMock()
        sheets_client.create_spreadsheet.return_value = "sheet-1"

        EventLabelSheet.create(sheets_client)

        sheets_client.create_spreadsheet.assert_called_once_with(DEFAULT_SHEET_TITLE)

    def test_writes_initial_labels_as_data_rows(self):
        sheets_client = MagicMock()
        sheets_client.create_spreadsheet.return_value = "sheet-1"
        initial_labels = [
            EventLabel(id="l1", name="Design Work", background_color="#8e24aa"),
            EventLabel(id="l2", background_color="#d50000"),
        ]

        EventLabelSheet.create(sheets_client, initial_labels=initial_labels)

        assert sheets_client.write_rows.call_args_list[-1].args == (
            "sheet-1",
            "A2:D",
            [["l1", "Design Work", "#8e24aa", ""], ["l2", "", "#d50000", ""]],
        )

    def test_skips_data_write_when_no_initial_labels_given(self):
        sheets_client = MagicMock()
        sheets_client.create_spreadsheet.return_value = "sheet-1"

        EventLabelSheet.create(sheets_client)

        # Only the header row write -- no second write_rows call for data.
        assert sheets_client.write_rows.call_count == 1


class TestEventLabelSheetSpreadsheetId:
    def test_exposes_the_given_id(self):
        event_label_sheet = make_sheet(spreadsheet_id="sheet-1")

        assert event_label_sheet.spreadsheet_id == "sheet-1"


class TestEventLabelSheetRead:
    def test_reads_and_parses_data_rows(self):
        sheets_client = MagicMock()
        sheets_client.read_rows.side_effect = lambda spreadsheet_id, rng: {
            "A1:D1": [_HEADER_ROW],
            "A2:D": [["l1", "Design Work", "#8e24aa", "1"]],
        }[rng]
        event_label_sheet = make_sheet(sheets_client)

        labels = event_label_sheet.read()

        assert labels == [
            EventLabel(id="l1", name="Design Work", background_color="#8e24aa", priority=1)
        ]

    def test_returns_empty_list_when_no_data_rows(self):
        sheets_client = MagicMock()
        sheets_client.read_rows.side_effect = lambda spreadsheet_id, rng: {
            "A1:D1": [_HEADER_ROW],
            "A2:D": [],
        }[rng]
        event_label_sheet = make_sheet(sheets_client)

        assert event_label_sheet.read() == []

    def test_raises_when_header_is_missing_expected_columns(self):
        sheets_client = MagicMock()
        sheets_client.read_rows.return_value = [["id", "name"]]
        event_label_sheet = make_sheet(sheets_client)

        with pytest.raises(ValueError):
            event_label_sheet.read()


class TestEventLabelSheetWrite:
    def test_overwrites_data_rows_preserving_unknown_columns(self):
        sheets_client = MagicMock()
        sheets_client.read_rows.side_effect = lambda spreadsheet_id, rng: {
            "A1:D1": [_HEADER_ROW],
            "A2:D": [["l1", "Old Name", "#000000", ""]],
        }[rng]
        event_label_sheet = make_sheet(sheets_client)

        event_label_sheet.write(
            [EventLabel(id="l1", name="New Name", background_color="#8e24aa", priority=2)]
        )

        sheets_client.write_rows.assert_called_once_with(
            "sheet-1", "A2:D", [["l1", "New Name", "#8e24aa", "2"]]
        )

    def test_handles_more_labels_than_previous_rows(self):
        sheets_client = MagicMock()
        sheets_client.read_rows.side_effect = lambda spreadsheet_id, rng: {
            "A1:D1": [_HEADER_ROW],
            "A2:D": [],
        }[rng]
        event_label_sheet = make_sheet(sheets_client)

        event_label_sheet.write([EventLabel(id="l1", background_color="#8e24aa")])

        sheets_client.write_rows.assert_called_once_with(
            "sheet-1", "A2:D", [["l1", "", "#8e24aa", ""]]
        )


class TestEventLabelSheetAppend:
    def test_adds_a_new_row_after_existing_rows(self):
        sheets_client = MagicMock()
        sheets_client.read_rows.side_effect = lambda spreadsheet_id, rng: {
            "A1:D1": [_HEADER_ROW],
            "A2:D": [["l1", "Design Work", "#8e24aa", ""]],
        }[rng]
        event_label_sheet = make_sheet(sheets_client)

        event_label_sheet.append(EventLabel(name="New One", priority=2))

        sheets_client.write_rows.assert_called_once_with(
            "sheet-1",
            "A2:D",
            [["l1", "Design Work", "#8e24aa", ""], ["", "New One", "", "2"]],
        )

    def test_appends_to_an_empty_sheet(self):
        sheets_client = MagicMock()
        sheets_client.read_rows.side_effect = lambda spreadsheet_id, rng: {
            "A1:D1": [_HEADER_ROW],
            "A2:D": [],
        }[rng]
        event_label_sheet = make_sheet(sheets_client)

        event_label_sheet.append(EventLabel(name="New One"))

        sheets_client.write_rows.assert_called_once_with(
            "sheet-1", "A2:D", [["", "New One", "", ""]]
        )
