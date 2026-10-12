//! The module's tests, against the fake Google.

use super::*;
use crate::testing::fake_google::FakeGoogle;
use crate::write_lock::WRITE_LOCK;
use serde_json::json;

const ROWS: RowType = RowType { name: "Row", fields: &[("id", Kind::Str), ("name", Kind::Str), ("count", Kind::Int), ("tags", Kind::Json)] };

struct Fixture {
    fake: FakeGoogle,
    sheets: SheetsClient,
    spreadsheet: String,
    sheet: RowSheet,
}

fn fixture() -> Fixture {
    let fake = FakeGoogle::start();
    let sheets = SheetsClient::new(fake.google());
    let _held = WRITE_LOCK.hold();
    let spreadsheet = sheets.create_spreadsheet("Calendar Metadata").unwrap();
    let sheet = RowSheet::ensure(&sheets, &spreadsheet, "rows", "Rows", ROWS, &["id", "name"]).unwrap();
    Fixture { fake, sheets, spreadsheet, sheet }
}

fn row(given: Value) -> Row {
    ROWS.from_value(&given)
}

fn strings(items: &[&str]) -> Vec<String> {
    crate::testing::strings(items)
}

impl Fixture {
    fn write(&self, items: &[Row]) {
        let _held = WRITE_LOCK.hold();
        self.sheet.write(items).unwrap();
    }

    fn set(&self, r: usize, cells: &[&str]) {
        for (c, text) in cells.iter().enumerate() {
            self.fake.set_cell(&self.spreadsheet, "Rows", r, c, text);
        }
    }

    fn cells(&self) -> Vec<Vec<String>> {
        self.fake.rows(&self.spreadsheet, "Rows")
    }
}

#[test]
fn a_new_tab_has_just_its_header() {
    let f = fixture();

    assert!(f.sheet.read().unwrap().is_empty());
    assert_eq!(f.cells(), vec![strings(&["id", "name", "count", "tags"])]);
}

#[test]
fn rows_round_trip_with_numbers_and_lists() {
    let f = fixture();
    let items = vec![row(json!({"id": "a", "name": "One", "count": 3, "tags": ["x", "y"]})), row(json!({"id": "b", "name": "Two"}))];

    f.write(&items);

    assert_eq!(f.sheet.read().unwrap(), items);
}

#[test]
fn finds_the_tab_again_by_its_role() {
    let f = fixture();
    f.write(&[row(json!({"id": "a", "name": "One"}))]);

    let again = {
        let _held = WRITE_LOCK.hold();
        RowSheet::ensure(&f.sheets, &f.spreadsheet, "rows", "Rows", ROWS, &["id", "name"]).unwrap()
    };

    assert_eq!(again.read().unwrap(), vec![row(json!({"id": "a", "name": "One"}))]);
}

#[test]
fn keeps_a_hand_added_columns_cells_and_reads_by_header() {
    let f = fixture();
    f.set(0, &["name", "id", "mine", "count", "tags"]);
    f.set(1, &["One", "a", "kept"]);

    f.write(&[row(json!({"id": "a", "name": "Uno", "count": 1}))]);

    assert_eq!(f.cells()[1], strings(&["Uno", "a", "kept", "1"]));
}

#[test]
fn adds_a_column_for_a_field_the_tab_lacks_once_it_has_a_value() {
    let f = fixture();
    f.set(0, &["id", "name", "", ""]);

    f.write(&[row(json!({"id": "a", "name": "One", "count": 2}))]);

    assert_eq!(f.cells()[0][..2], strings(&["id", "name"]));
    assert_eq!(f.sheet.read().unwrap(), vec![row(json!({"id": "a", "name": "One", "count": 2}))]);
}

#[test]
fn a_shorter_list_blanks_the_rows_left_over() {
    let f = fixture();
    f.write(&[row(json!({"id": "a", "name": "One"})), row(json!({"id": "b", "name": "Two"}))]);

    f.write(&[row(json!({"id": "b", "name": "Two"}))]);

    assert_eq!(f.sheet.read().unwrap(), vec![row(json!({"id": "b", "name": "Two"}))]);
}

#[test]
fn a_cell_that_isnt_its_kind_is_kept_as_text() {
    let f = fixture();
    f.set(1, &["a", "One", "lots"]);

    assert_eq!(f.sheet.read().unwrap(), vec![row(json!({"id": "a", "name": "One", "count": "lots"}))]);
}
