from dataclasses import dataclass

from tests.fake_sheets import FakeSheets
from utilities.row_sheet import RowSheet


@dataclass(kw_only=True)
class _Row:
    id: str | None = None
    name: str | None = None
    count: int | None = None
    tags: list[str] | None = None


def _sheet(sheets=None) -> tuple[RowSheet[_Row], FakeSheets]:
    sheets = sheets or FakeSheets()
    return RowSheet.ensure(sheets, "spreadsheet", role="rows", title="Rows", row_type=_Row), sheets


def test_a_new_tab_has_just_its_header():
    sheet, sheets = _sheet()

    assert sheet.read() == []
    assert sheets.read_rows_in_sheet("spreadsheet", 1, "A1:D1") == [["id", "name", "count", "tags"]]


def test_rows_round_trip_with_numbers_and_lists():
    sheet, _ = _sheet()
    rows = [_Row(id="a", name="One", count=3, tags=["x", "y"]), _Row(id="b", name="Two")]

    sheet.write(rows)

    assert sheet.read() == rows


def test_finds_the_tab_again_by_its_role():
    sheet, sheets = _sheet()
    sheet.write([_Row(id="a", name="One")])

    again, _ = _sheet(sheets)

    assert again.read() == [_Row(id="a", name="One")]


def test_keeps_a_hand_added_columns_cells_and_reads_by_header():
    sheet, sheets = _sheet()
    sheets.write_rows_in_sheet("spreadsheet", 1, "A1:E1", [["name", "id", "mine", "count", "tags"]])
    sheets.write_rows_in_sheet("spreadsheet", 1, "A2:C2", [["One", "a", "kept"]])

    sheet.write([_Row(id="a", name="Uno", count=1)])

    assert sheets.read_rows_in_sheet("spreadsheet", 1, "A2:D2") == [["Uno", "a", "kept", "1"]]


def test_adds_a_column_for_a_field_the_tab_lacks_once_it_has_a_value():
    sheet, sheets = _sheet()
    sheets.write_rows_in_sheet("spreadsheet", 1, "A1:D1", [["id", "name", "", ""]])

    sheet.write([_Row(id="a", name="One", count=2)])

    assert sheets.read_rows_in_sheet("spreadsheet", 1, "A1:C1")[0][:2] == ["id", "name"]
    assert sheet.read() == [_Row(id="a", name="One", count=2)]


def test_a_shorter_list_blanks_the_rows_left_over():
    sheet, _ = _sheet()
    sheet.write([_Row(id="a", name="One"), _Row(id="b", name="Two")])

    sheet.write([_Row(id="b", name="Two")])

    assert sheet.read() == [_Row(id="b", name="Two")]


def test_a_cell_that_isnt_its_kind_is_kept_as_text():
    sheet, sheets = _sheet()
    sheets.write_rows_in_sheet("spreadsheet", 1, "A2:C2", [["a", "One", "lots"]])

    assert sheet.read() == [_Row(id="a", name="One", count="lots")]
