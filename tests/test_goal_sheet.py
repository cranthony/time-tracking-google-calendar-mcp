
import pytest

from tests.fake_sheets import FakeSheets
from utilities.goal_sheet import HEADER_ROW, Goal, GoalSheet

_SPREADSHEET = "spreadsheet-1"


def _sheet(goals=()) -> tuple[GoalSheet, FakeSheets]:
    sheets = FakeSheets()
    return GoalSheet.create(sheets, _SPREADSHEET, list(goals)), sheets


def _full_goal() -> Goal:
    return Goal(
        id="g7k2qp",
        parent_id="g1",
        name="Tofu tikka",
        status="inactive",
        background_color="#123456",
        priority=2,
        measure={"kind": "duration", "target_min": 300},
        note="for hosting",
        label_id="5b0e5b1e-0000-0000-0000-000000000000",
    )


class TestGoalSheet:
    def test_round_trips_every_field(self):
        sheet, _ = _sheet([_full_goal()])

        assert sheet.read() == [_full_goal()]

    def test_writes_cells_the_way_a_person_would(self):
        _, sheets = _sheet([_full_goal()])
        row = sheets.read_rows_in_sheet(_SPREADSHEET, 1, "A2:Z2")[0]

        cells = dict(zip(HEADER_ROW, row))
        assert cells["status"] == "inactive"
        assert cells["measure"] == '{"kind":"duration","target_min":300}'

    def test_is_tagged_and_found_again(self):
        sheet, sheets = _sheet()

        found = GoalSheet.find(sheets, _SPREADSHEET)

        assert found is not None and found._sheet_id == sheet._sheet_id
        assert sheets.titles[sheet._sheet_id] == "Goals"

    def test_keeps_a_column_it_doesnt_know_on_write(self):
        sheet, sheets = _sheet([Goal(id="a", name="A", status="active", label_id="la")])
        sheets.write_rows_in_sheet(_SPREADSHEET, 1, "O1:O1", [["my notes"]])
        sheets.write_rows_in_sheet(_SPREADSHEET, 1, "O2:O2", [["keep me"]])

        sheet.write([Goal(id="a", name="Renamed", status="active", label_id="la")])

        assert sheets.cell(1, "C2") == "Renamed"
        assert sheets.cell(1, "O2") == "keep me"

    def test_skips_blank_rows_and_blanks_leftovers(self):
        goals = [Goal(id=i, name=i, status="active", label_id=f"l{i}") for i in "abc"]
        sheet, sheets = _sheet(goals)
        sheets.write_rows_in_sheet(_SPREADSHEET, 1, "A3:N3", [[""] * 14])  # a hand-blanked row

        assert [g.id for g in sheet.read()] == ["a", "c"]

        sheet.write([goals[0]])

        assert [g.id for g in sheet.read()] == ["a"]
        assert sheets.cell(1, "A4") == ""

    def test_removes_retired_columns_on_write_shifting_the_rest_left(self):
        sheets = FakeSheets()
        sheet = GoalSheet.create(sheets, _SPREADSHEET, [])
        header = ["id", "name", "target", "status", "label_id", "deadline", "active", "created", "fixed_time", "mine"]
        sheets.write_rows_in_sheet(_SPREADSHEET, 1, "A1:Z1", [header + [""] * 16])
        sheets.write_rows_in_sheet(
            _SPREADSHEET,
            1,
            "A2:J2",
            [["a", "A", "weekly", "active", "la", "2026-12-31", "TRUE", "2026-10-02", "TRUE", "kept"]],
        )

        (a,) = sheet.read()
        sheet.write([a])

        rows = sheets.read_rows_in_sheet(_SPREADSHEET, 1, "A1:Z2")
        # Nothing left after them: the cells they shifted from are blank.
        assert rows[0] == ["id", "name", "status", "label_id", "mine"]
        assert rows[1] == ["a", "A", "active", "la", "kept"]
        assert sheet.read() == [a]

    def test_refuses_a_header_missing_required_columns(self):
        sheet, sheets = _sheet()
        sheets.write_rows_in_sheet(_SPREADSHEET, 1, "A1:N1", [["id", "title"] + [""] * 12])

        with pytest.raises(ValueError, match="missing columns"):
            sheet.read()
