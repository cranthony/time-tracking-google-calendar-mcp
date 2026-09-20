from unittest.mock import MagicMock

import pytest

from calendar_clients.google_calendar import EventLabel as RawEventLabel
from utilities import calendar_metadata_sheet
from utilities.event_label_sheet import DEFAULT_SHEET_TITLE, EventLabel, EventLabelSheet

_HEADER_ROW = ["id", "name", "background_color", "priority"]
_HEADER_ROW_WITH_FIXED_TIME = ["id", "name", "background_color", "priority", "fixed_time"]
_SHEET_ID = 42


def make_sheet(sheets_client=None, spreadsheet_id: str = "sheet-1", sheet_id: int = _SHEET_ID) -> EventLabelSheet:
    return EventLabelSheet(sheets_client or MagicMock(), spreadsheet_id, sheet_id)


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

    def test_parses_fixed_time_true(self):
        label = EventLabel.from_row(
            _HEADER_ROW_WITH_FIXED_TIME, ["l1", "Design Work", "#8e24aa", "1", "TRUE"]
        )

        assert label.fixed_time is True

    def test_parses_fixed_time_false(self):
        label = EventLabel.from_row(
            _HEADER_ROW_WITH_FIXED_TIME, ["l1", "Design Work", "#8e24aa", "1", "FALSE"]
        )

        assert label.fixed_time is False

    def test_parses_fixed_time_case_insensitively(self):
        label = EventLabel.from_row(
            _HEADER_ROW_WITH_FIXED_TIME, ["l1", "Design Work", "#8e24aa", "1", "true"]
        )

        assert label.fixed_time is True

    def test_blank_fixed_time_becomes_none(self):
        label = EventLabel.from_row(
            _HEADER_ROW_WITH_FIXED_TIME, ["l1", "Design Work", "#8e24aa", "1", ""]
        )

        assert label.fixed_time is None

    def test_missing_fixed_time_column_becomes_none(self):
        label = EventLabel.from_row(_HEADER_ROW, ["l1", "Design Work", "#8e24aa", "1"])

        assert label.fixed_time is None


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

    def test_writes_fixed_time_true_as_upper_case_true(self):
        label = EventLabel(id="l1", fixed_time=True)

        row = label.to_row(_HEADER_ROW_WITH_FIXED_TIME)

        assert row[-1] == "TRUE"

    def test_writes_fixed_time_false_as_upper_case_false(self):
        label = EventLabel(id="l1", fixed_time=False)

        row = label.to_row(_HEADER_ROW_WITH_FIXED_TIME)

        assert row[-1] == "FALSE"

    def test_writes_unset_fixed_time_as_empty_string(self):
        label = EventLabel(id="l1")

        row = label.to_row(_HEADER_ROW_WITH_FIXED_TIME)

        assert row[-1] == ""


class TestEventLabelSheetEnsure:
    def test_reuses_an_already_tagged_tab_without_writing_anything(self):
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = _SHEET_ID

        sheet = EventLabelSheet.ensure(sheets_client, "sheet-1", is_new_spreadsheet=False)

        assert sheet.spreadsheet_id == "sheet-1"
        sheets_client.add_sheet.assert_not_called()
        sheets_client.update_sheet_properties.assert_not_called()
        sheets_client.set_column_width.assert_not_called()
        sheets_client.write_rows_in_sheet.assert_not_called()

    def test_adopts_sheet_zero_narrows_column_and_writes_header_for_a_new_spreadsheet(self):
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = None

        EventLabelSheet.ensure(sheets_client, "sheet-1", is_new_spreadsheet=True)

        sheets_client.update_sheet_properties.assert_called_once_with(
            "sheet-1", 0, title=DEFAULT_SHEET_TITLE, tab_color=calendar_metadata_sheet._TAB_COLOR
        )
        sheets_client.create_sheet_metadata.assert_called_once_with(
            "sheet-1", 0, "sheet-role", "event-labels"
        )
        sheets_client.set_column_width.assert_called_once_with(
            "sheet-1", sheet_id=0, column_index=0, pixel_width=60
        )
        sheets_client.write_rows_in_sheet.assert_called_once_with(
            "sheet-1", 0, "A1:E1", [_HEADER_ROW_WITH_FIXED_TIME]
        )

    def test_writes_initial_labels_as_data_rows_for_a_new_spreadsheet(self):
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = None
        initial_labels = [
            EventLabel(id="l1", name="Design Work", background_color="#8e24aa"),
            EventLabel(id="l2", background_color="#d50000"),
        ]

        EventLabelSheet.ensure(sheets_client, "sheet-1", is_new_spreadsheet=True, initial_labels=initial_labels)

        assert sheets_client.write_rows_in_sheet.call_args_list[-1].args == (
            "sheet-1",
            0,
            "A2:E",
            [["l1", "Design Work", "#8e24aa", "", ""], ["l2", "", "#d50000", "", ""]],
        )

    def test_skips_data_write_when_no_initial_labels_given(self):
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = None

        EventLabelSheet.ensure(sheets_client, "sheet-1", is_new_spreadsheet=True)

        # Only the header row write -- no second write for data.
        assert sheets_client.write_rows_in_sheet.call_count == 1

    def test_does_not_repopulate_an_adopted_legacy_spreadsheet(self):
        # Not yet tagged (this is the first time this spreadsheet's tab
        # gets tagged), but it's an *adopted* pre-existing spreadsheet
        # (is_new_spreadsheet=False) -- its sheetId 0 already has real
        # header/data rows from before per-tab tagging existed, so they
        # must not be overwritten, even though the tab itself still gets
        # renamed/colored/tagged.
        sheets_client = MagicMock()
        sheets_client.find_sheet_id.return_value = None

        EventLabelSheet.ensure(
            sheets_client,
            "sheet-1",
            is_new_spreadsheet=False,
            initial_labels=[EventLabel(id="l1", background_color="#8e24aa")],
        )

        sheets_client.update_sheet_properties.assert_called_once_with(
            "sheet-1", 0, title=DEFAULT_SHEET_TITLE, tab_color=calendar_metadata_sheet._TAB_COLOR
        )
        sheets_client.create_sheet_metadata.assert_called_once_with(
            "sheet-1", 0, "sheet-role", "event-labels"
        )
        sheets_client.set_column_width.assert_not_called()
        sheets_client.write_rows_in_sheet.assert_not_called()


class TestEventLabelSheetSpreadsheetId:
    def test_exposes_the_given_id(self):
        event_label_sheet = make_sheet(spreadsheet_id="sheet-1")

        assert event_label_sheet.spreadsheet_id == "sheet-1"


class TestEventLabelSheetRead:
    def test_reads_and_parses_data_rows(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: {
            "A1:E1": [_HEADER_ROW_WITH_FIXED_TIME],
            "A2:E": [["l1", "Design Work", "#8e24aa", "1", "TRUE"]],
        }[rng]
        event_label_sheet = make_sheet(sheets_client)

        labels = event_label_sheet.read()

        assert labels == [
            EventLabel(
                id="l1", name="Design Work", background_color="#8e24aa", priority=1, fixed_time=True
            )
        ]

    def test_returns_empty_list_when_no_data_rows(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: {
            "A1:E1": [_HEADER_ROW_WITH_FIXED_TIME],
            "A2:E": [],
        }[rng]
        event_label_sheet = make_sheet(sheets_client)

        assert event_label_sheet.read() == []

    def test_raises_when_header_is_missing_expected_columns(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.return_value = [["id", "name"]]
        event_label_sheet = make_sheet(sheets_client)

        with pytest.raises(ValueError):
            event_label_sheet.read()


class TestEventLabelSheetWrite:
    def test_overwrites_data_rows_preserving_unknown_columns(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: {
            "A1:E1": [_HEADER_ROW_WITH_FIXED_TIME],
            "A2:E": [["l1", "Old Name", "#000000", "", "TRUE"]],
        }[rng]
        event_label_sheet = make_sheet(sheets_client)

        event_label_sheet.write(
            [EventLabel(id="l1", name="New Name", background_color="#8e24aa", priority=2)]
        )

        sheets_client.write_rows_in_sheet.assert_called_once_with(
            "sheet-1", _SHEET_ID, "A2:E", [["l1", "New Name", "#8e24aa", "2", ""]]
        )

    def test_handles_more_labels_than_previous_rows(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: {
            "A1:E1": [_HEADER_ROW_WITH_FIXED_TIME],
            "A2:E": [],
        }[rng]
        event_label_sheet = make_sheet(sheets_client)

        event_label_sheet.write([EventLabel(id="l1", background_color="#8e24aa")])

        sheets_client.write_rows_in_sheet.assert_called_once_with(
            "sheet-1", _SHEET_ID, "A2:E", [["l1", "", "#8e24aa", "", ""]]
        )


class TestEventLabelSheetAppend:
    def test_adds_a_new_row_after_existing_rows(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: {
            "A1:E1": [_HEADER_ROW_WITH_FIXED_TIME],
            "A2:E": [["l1", "Design Work", "#8e24aa", "", ""]],
        }[rng]
        event_label_sheet = make_sheet(sheets_client)

        event_label_sheet.append(EventLabel(name="New One", priority=2))

        sheets_client.write_rows_in_sheet.assert_called_once_with(
            "sheet-1",
            _SHEET_ID,
            "A2:E",
            [["l1", "Design Work", "#8e24aa", "", ""], ["", "New One", "", "2", ""]],
        )

    def test_appends_to_an_empty_sheet(self):
        sheets_client = MagicMock()
        sheets_client.read_rows_in_sheet.side_effect = lambda spreadsheet_id, sheet_id, rng: {
            "A1:E1": [_HEADER_ROW_WITH_FIXED_TIME],
            "A2:E": [],
        }[rng]
        event_label_sheet = make_sheet(sheets_client)

        event_label_sheet.append(EventLabel(name="New One"))

        sheets_client.write_rows_in_sheet.assert_called_once_with(
            "sheet-1", _SHEET_ID, "A2:E", [["", "New One", "", "", ""]]
        )
