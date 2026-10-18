//! The module's tests, against the fake Google.

use super::*;
use crate::google::kept;
use crate::testing::fake_google::{FakeGoogle, LoggedRequest};
use crate::write_lock::WRITE_LOCK;

struct Fixture {
    fake: FakeGoogle,
    client: SheetsClient,
    spreadsheet: String,
}

/// A spreadsheet with its first tab, "Sheet1" (id 0), and "Second" (id 1).
fn fixture() -> Fixture {
    let fake = FakeGoogle::start();
    let client = SheetsClient::new(fake.google());
    let _held = WRITE_LOCK.hold();
    let spreadsheet = client.create_spreadsheet("Calendar Metadata").unwrap();
    client.batch_update(&spreadsheet, json!({"requests": [{"addSheet": {"properties": {"title": "Second", "sheetId": 1}}}]}), &[]).unwrap();
    fake.clear_requests();
    Fixture { fake, client, spreadsheet }
}

fn rows(items: &[&[&str]]) -> Rows {
    items.iter().map(|row| row.iter().map(|c| c.to_string()).collect()).collect()
}

impl Fixture {
    fn requests(&self, ending: &str) -> Vec<LoggedRequest> {
        self.fake.requests().into_iter().filter(|r| r.path.ends_with(ending)).collect()
    }

    fn batch_updates(&self) -> Vec<Value> {
        self.requests(":batchUpdate").into_iter().map(|r| r.body).collect()
    }

    fn reads(&self) -> usize {
        self.requests("values:batchGetByDataFilter").len()
    }

    fn write(&self, sheet_id: i64, range: &str, values: Rows) {
        let _held = WRITE_LOCK.hold();
        self.client.write_rows_in_sheet(&self.spreadsheet, sheet_id, range, &values).unwrap();
    }

    fn read(&self, sheet_id: i64, range: &str) -> Rows {
        self.client.read_rows_in_sheet(&self.spreadsheet, sheet_id, range).unwrap()
    }

    /// A tab like Python's: a header, then rows with gaps in them.
    fn seeded(&self) {
        self.write(0, "A1:D1", rows(&[&["h1", "h2", "h3", "h4"]]));
        self.write(0, "A2:D5", rows(&[&["a", "", "c"], &[], &["x", "y"], &["", "", "", "z"]]));
        self.write(1, "A1:B1", rows(&[&["other", "tab"]]));
        self.fake.clear_requests();
    }
}

#[test]
fn creates_a_spreadsheet_and_returns_its_id() {
    let fake = FakeGoogle::start();
    let client = SheetsClient::new(fake.google());

    let id = {
        let _held = WRITE_LOCK.hold();
        client.create_spreadsheet("Goals").unwrap()
    };

    assert!(!id.is_empty());
    let created = &fake.requests()[0];
    assert_eq!(created.body, json!({"properties": {"title": "Goals"}}));
    assert_eq!(created.query, vec![("fields".to_string(), "spreadsheetId".to_string())]);
}

// -- deleting rows --------------------------------------------------------------------------

#[test]
fn delete_rows_sends_a_zero_based_half_open_range() {
    let f = fixture();
    let _held = WRITE_LOCK.hold();

    f.client.delete_rows(&f.spreadsheet, 1, 2, 5, 0).unwrap();

    assert_eq!(
        f.batch_updates()[0],
        json!({
            "requests": [{"deleteDimension": {"range": {"sheetId": 1, "dimension": "ROWS", "startIndex": 1, "endIndex": 5}}}],
            "includeSpreadsheetInResponse": true,
            "responseIncludeGridData": false,
        })
    );
    assert_eq!(f.requests(":batchUpdate")[0].query, vec![("fields".to_string(), "updatedSpreadsheet(sheets(properties(sheetId,gridProperties(rowCount))))".to_string())]);
}

#[test]
fn delete_rows_adds_rows_back_when_the_tab_is_left_with_too_few() {
    let f = fixture();
    let _held = WRITE_LOCK.hold();

    f.client.delete_rows(&f.spreadsheet, 1, 2, 61, 1000).unwrap();

    let updates = f.batch_updates();
    assert_eq!(updates.len(), 2);
    assert_eq!(updates[1], json!({"requests": [{"appendDimension": {"sheetId": 1, "dimension": "ROWS", "length": 60}}]}));
    assert_eq!(f.fake.row_count(&f.spreadsheet, "Second"), 1000);
}

#[test]
fn delete_rows_adds_nothing_when_the_tab_still_has_enough() {
    for rows_before in [1060, 1260] {
        let f = fixture();
        f.fake.set_row_count(&f.spreadsheet, "Second", rows_before);
        let _held = WRITE_LOCK.hold();

        f.client.delete_rows(&f.spreadsheet, 1, 2, 61, 1000).unwrap();

        assert_eq!(f.batch_updates().len(), 1);
    }
}

#[test]
fn a_single_row_range_deletes_just_that_row() {
    let f = fixture();
    let _held = WRITE_LOCK.hold();

    f.client.delete_rows(&f.spreadsheet, 1, 7, 7, 0).unwrap();

    let range = &f.batch_updates()[0]["requests"][0]["deleteDimension"]["range"];
    assert_eq!((range["startIndex"].clone(), range["endIndex"].clone()), (json!(6), json!(7)));
}

// -- tabs ---------------------------------------------------------------------------------------

#[test]
fn add_sheet_returns_the_new_tab_id_with_its_color() {
    let f = fixture();
    let _held = WRITE_LOCK.hold();

    let plain = f.client.add_sheet(&f.spreadsheet, "Uncompacted Time Notes", None).unwrap();
    let colored = f.client.add_sheet(&f.spreadsheet, "Colored", Some(&json!({"red": 0.26}))).unwrap();

    assert_ne!(plain, colored);
    let updates = f.batch_updates();
    assert_eq!(updates[0], json!({"requests": [{"addSheet": {"properties": {"title": "Uncompacted Time Notes"}}}]}));
    assert_eq!(updates[1], json!({"requests": [{"addSheet": {"properties": {"title": "Colored", "tabColor": {"red": 0.26}}}}]}));
}

#[test]
fn update_sheet_properties_sends_only_what_is_given() {
    let f = fixture();
    let _held = WRITE_LOCK.hold();

    f.client.update_sheet_properties(&f.spreadsheet, 0, Some("Goals"), None).unwrap();
    f.client.update_sheet_properties(&f.spreadsheet, 0, Some("Goals"), Some(&json!({"red": 0.26}))).unwrap();
    f.client.update_sheet_properties(&f.spreadsheet, 0, None, None).unwrap();

    let updates = f.batch_updates();
    assert_eq!(updates.len(), 2);
    assert_eq!(updates[0], json!({"requests": [{"updateSheetProperties": {"properties": {"sheetId": 0, "title": "Goals"}, "fields": "title"}}]}));
    assert_eq!(
        updates[1],
        json!({"requests": [{"updateSheetProperties": {"properties": {"sheetId": 0, "title": "Goals", "tabColor": {"red": 0.26}}, "fields": "title,tabColor"}}]})
    );
    assert_eq!(f.fake.tab_titles(&f.spreadsheet)[0], "Goals");
}

#[test]
fn get_sheet_title_finds_the_tab_by_id() {
    let f = fixture();

    assert_eq!(f.client.get_sheet_title(&f.spreadsheet, 1).unwrap(), "Second");
    assert_eq!(f.requests(&f.spreadsheet)[0].query, vec![("fields".to_string(), "sheets.properties".to_string())]);
    assert!(f.client.get_sheet_title(&f.spreadsheet, 99).is_err());
}

#[test]
fn tags_a_tab_and_finds_it_by_its_tag() {
    let f = fixture();
    assert_eq!(f.client.find_sheet_id(&f.spreadsheet, "sheet-role", "goals").unwrap(), None);

    {
        let _held = WRITE_LOCK.hold();
        f.client.create_sheet_metadata(&f.spreadsheet, 1, "sheet-role", "goals").unwrap();
    }

    assert_eq!(
        f.batch_updates()[0],
        json!({"requests": [{"createDeveloperMetadata": {"developerMetadata": {
            "metadataKey": "sheet-role", "metadataValue": "goals", "location": {"sheetId": 1}, "visibility": "PROJECT"
        }}}]})
    );
    assert_eq!(f.client.find_sheet_id(&f.spreadsheet, "sheet-role", "goals").unwrap(), Some(1));
    let search = f.requests("developerMetadata:search").pop().unwrap();
    assert_eq!(search.body, json!({"dataFilters": [{"developerMetadataLookup": {"metadataKey": "sheet-role", "metadataValue": "goals"}}]}));
}

#[test]
fn set_column_width_sends_update_dimension_properties() {
    let f = fixture();
    let _held = WRITE_LOCK.hold();

    f.client.set_column_width(&f.spreadsheet, 0, 0, 60).unwrap();

    assert_eq!(
        f.batch_updates()[0],
        json!({"requests": [{"updateDimensionProperties": {
            "range": {"sheetId": 0, "dimension": "COLUMNS", "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": 60},
            "fields": "pixelSize",
        }}]})
    );
}

// -- reading and writing a tab by id -----------------------------------------------------------

#[test]
fn reads_by_tab_id_without_looking_up_the_title() {
    let f = fixture();
    f.write(1, "A2:D2", rows(&[&["l1", "Design Work", "#8e24aa", "1"]]));
    f.fake.clear_requests();

    assert_eq!(f.read(1, "A2:D"), rows(&[&["l1", "Design Work", "#8e24aa", "1"]]));

    let read = &f.requests("values:batchGetByDataFilter")[0];
    assert_eq!(
        read.body,
        json!({"dataFilters": [{"gridRange": {"sheetId": 1, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": 26}}], "majorDimension": "ROWS"})
    );
    assert_eq!(f.fake.requests().len(), 1);
}

#[test]
fn a_bounded_range_gets_an_end_row() {
    let f = fixture();

    f.read(1, "B5:AA7");

    assert_eq!(
        f.requests("values:batchGetByDataFilter")[0].body["dataFilters"][0]["gridRange"],
        json!({"sheetId": 1, "startRowIndex": 4, "endRowIndex": 7, "startColumnIndex": 1, "endColumnIndex": 27})
    );
}

#[test]
fn an_empty_range_has_no_rows() {
    let f = fixture();

    assert!(f.read(1, "A2:D").is_empty());
}

#[test]
fn writes_by_tab_id_bounded_to_the_rows_given() {
    let f = fixture();
    let values = rows(&[&["l1", "Design Work", "#8e24aa", "1"], &["l2", "Email", "", "2"]]);

    f.write(1, "A2:D", values.clone());

    let writes = f.requests("values:batchUpdateByDataFilter");
    assert_eq!(writes.len(), 1);
    assert_eq!(
        writes[0].body,
        json!({
            "valueInputOption": "RAW",
            "data": [{
                "dataFilter": {"gridRange": {"sheetId": 1, "startRowIndex": 1, "endRowIndex": 3, "startColumnIndex": 0, "endColumnIndex": 4}},
                "majorDimension": "ROWS",
                "values": values,
            }],
        })
    );
    assert_eq!(f.fake.requests().len(), 1);
    assert_eq!(f.read(1, "A2:D"), rows(&[&["l1", "Design Work", "#8e24aa", "1"], &["l2", "Email", "", "2"]]));
}

#[test]
fn falls_back_to_the_title_qualified_range_past_the_end_of_the_grid() {
    // Past the grid's last row, a GridRange write is refused (or writes
    // nothing): an A1 range with the tab's title grows the grid.
    let f = fixture();
    {
        let _held = WRITE_LOCK.hold();
        f.client.update_sheet_properties(&f.spreadsheet, 1, Some("Chris's Labels"), None).unwrap();
    }
    f.fake.set_row_count(&f.spreadsheet, "Chris's Labels", 2);
    f.fake.clear_requests();

    f.write(1, "A2:D", rows(&[&["a"], &["b"]]));

    let fallback = f.fake.requests().into_iter().find(|r| r.method == "PUT").expect("a fallback write");
    assert!(fallback.path.ends_with("/values/'Chris''s Labels'!A2:D3"), "{}", fallback.path);
    assert_eq!(fallback.body, json!({"values": [["a"], ["b"]]}));
    assert_eq!(f.read(1, "A2:D"), rows(&[&["a"], &["b"]]));
}

#[test]
fn other_write_errors_are_not_swallowed() {
    let f = fixture();
    f.fake.with(|s| s.fail_next.push(("POST".into(), "values:batchUpdateByDataFilter".into(), 403, "forbidden".into())));
    let _held = WRITE_LOCK.hold();

    let error = f.client.write_rows_in_sheet(&f.spreadsheet, 1, "A2:D", &rows(&[&["a"]])).unwrap_err();

    assert_eq!(status_of(&error), Some(403));
    assert!(f.fake.requests().iter().all(|r| r.method != "PUT"));
}

#[test]
fn writing_no_rows_sends_nothing() {
    let f = fixture();

    f.write(1, "A2:D", vec![]);

    assert!(f.fake.requests().is_empty());
}

// -- several ranges, and what's kept -----------------------------------------------------------------

#[test]
fn reads_a_tab_whole_once_and_cuts_every_range_from_it() {
    let f = fixture();
    f.write(1, "A1:C3", rows(&[&["header"], &["a"], &["b"]]));
    f.fake.clear_requests();

    let result = f.client.read_ranges_in_sheet(&f.spreadsheet, 1, &["A1:C1", "A2:C"]).unwrap();

    assert_eq!(result, vec![rows(&[&["header"]]), rows(&[&["a"], &["b"]])]);
    assert_eq!(f.reads(), 1);
    assert_eq!(
        f.requests("values:batchGetByDataFilter")[0].body["dataFilters"],
        json!([{"gridRange": {"sheetId": 1, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": 26}}])
    );
}

#[test]
fn matches_each_range_by_the_filters_it_came_back_with() {
    let header = GridRange { sheet_id: 42, start_row: 0, start_column: 0, end_column: 3, end_row: Some(1) };
    let data = GridRange { sheet_id: 42, start_row: 1, start_column: 0, end_column: 3, end_row: None };
    let response = json!({"valueRanges": [
        {"dataFilters": [{"gridRange": data.to_json()}], "valueRange": {"values": [["a"]]}},
        {"dataFilters": [{"gridRange": header.to_json()}], "valueRange": {"values": [["header"]]}},
    ]});

    assert_eq!(values_by_filter(&response, &[header, data]), vec![rows(&[&["header"]]), rows(&[&["a"]])]);
}

#[test]
fn a_range_with_nothing_in_it_is_empty() {
    let f = fixture();
    f.write(1, "A2:A2", rows(&[&["a"]]));

    assert_eq!(f.client.read_ranges_in_sheet(&f.spreadsheet, 1, &["A1:C1", "A2:C"]).unwrap(), vec![vec![], rows(&[&["a"]])]);
}

#[test]
fn a_kept_tab_is_read_from_memory_across_calls_and_clients() {
    let f = fixture();
    f.write(1, "A2:A2", rows(&[&["a"]]));
    f.fake.clear_requests();
    let other = SheetsClient::new(f.fake.google());

    let first = f.read(1, "A2:D");
    let second = other.read_rows_in_sheet(&f.spreadsheet, 1, "A2:D").unwrap();
    let header = f.read(1, "A1:D1");

    assert_eq!((first, second, header), (rows(&[&["a"]]), rows(&[&["a"]]), vec![]));
    assert_eq!(f.reads(), 1);
}

#[test]
fn a_range_within_the_kept_cells_is_cut_out_as_the_api_would_return_it() {
    for range in ["A1:D1", "A2:D", "A3:D4", "B2:C", "A2:B3", "D2:D", "A4:D9", "C1:C1", "A6:D", "A1:Z"] {
        let f = fixture();
        f.seeded();
        let expected = f.client.fetch(&f.spreadsheet, &[TabRange::new(&f.spreadsheet, 0, range)]).unwrap().remove(0).1;
        f.fake.clear_requests();

        assert_eq!(f.read(0, range), expected, "{range}");
        assert_eq!(f.reads(), 1, "{range}");
    }
}

#[test]
fn a_range_past_the_kept_columns_is_read_from_sheets_every_time() {
    let f = fixture();

    f.read(1, "B5:AA7");
    f.read(1, "B5:AA7");

    assert_eq!(f.reads(), 2);
}

#[test]
fn another_tab_is_read_on_its_own() {
    let f = fixture();
    f.seeded();
    f.read(0, "A1:D");

    assert_eq!(f.read(1, "A1:B1"), rows(&[&["other", "tab"]]));
    assert_eq!(f.reads(), 2);
}

#[test]
fn prefetch_reads_the_tabs_not_kept_in_one_request() {
    let f = fixture();
    f.write(0, "A1:B2", rows(&[&["h"], &["one"]]));
    f.write(1, "A1:B2", rows(&[&["h"], &["two"]]));
    f.fake.clear_requests();

    f.client.prefetch(&[TabRange::new(&f.spreadsheet, 0, "A1:B"), TabRange::new(&f.spreadsheet, 1, "A1:B")]).unwrap();
    let one = f.client.read_ranges_in_sheet(&f.spreadsheet, 0, &["A1:B1", "A2:B"]).unwrap();
    let two = f.read(1, "A2:B");
    f.client.prefetch(&[TabRange::new(&f.spreadsheet, 0, "A1:B")]).unwrap();

    assert_eq!((one, two), (vec![rows(&[&["h"]]), rows(&[&["one"]])], rows(&[&["two"]])));
    assert_eq!(f.reads(), 1);
    assert_eq!(f.requests("values:batchGetByDataFilter")[0].body["dataFilters"].as_array().unwrap().len(), 2);
}

#[test]
fn a_caller_changing_what_it_read_does_not_change_what_is_kept() {
    let f = fixture();
    f.write(1, "A2:A2", rows(&[&["a"]]));

    f.read(1, "A2:D")[0].push("mutated".into());
    f.read(1, "A2:D").push(vec!["mutated".into()]);

    assert_eq!(f.read(1, "A2:D"), rows(&[&["a"]]));
}

// -- writes keep what's kept current, and send only what changes -------------------------------------

/// Tab "Second" kept, with a header and three rows.
fn kept_fixture() -> Fixture {
    let f = fixture();
    f.write(1, "A1:C4", rows(&[&["id", "name", "n"], &["a", "Ann", "1"], &["b", "Bo", "2"], &["c", "Cy", "3"]]));
    f.read(1, "A1:C");
    f.fake.clear_requests();
    f
}

fn written_ranges(f: &Fixture) -> Vec<Vec<(i64, i64)>> {
    f.requests("values:batchUpdateByDataFilter")
        .iter()
        .map(|r| {
            r.body["data"]
                .as_array()
                .unwrap()
                .iter()
                .map(|d| {
                    let grid = &d["dataFilter"]["gridRange"];
                    (grid["startRowIndex"].as_i64().unwrap(), grid["endRowIndex"].as_i64().unwrap())
                })
                .collect()
        })
        .collect()
}

#[test]
fn a_write_sends_only_the_rows_it_changes_in_one_request() {
    let f = kept_fixture();

    f.write(1, "A2:C", rows(&[&["a", "Ann", "1"], &["b", "Bob", "2"], &["c", "Cy", "3"]]));
    f.write(1, "A2:C", rows(&[&["a", "Anne", "1"], &["b", "Bob", "2"], &["c", "Cy", ""]]));

    assert_eq!(written_ranges(&f), vec![vec![(2, 3)], vec![(1, 2), (3, 4)]]);
    assert_eq!(f.read(1, "A2:C"), rows(&[&["a", "Anne", "1"], &["b", "Bob", "2"], &["c", "Cy"]]));
    assert_eq!(f.fake.rows(&f.spreadsheet, "Second"), rows(&[&["id", "name", "n"], &["a", "Anne", "1"], &["b", "Bob", "2"], &["c", "Cy"]]));
    assert_eq!(f.reads(), 0);
}

#[test]
fn a_write_that_changes_nothing_sends_nothing() {
    let f = kept_fixture();

    f.write(1, "A2:C", rows(&[&["a", "Ann", "1"], &["b", "Bo", "2"]]));
    {
        let _held = WRITE_LOCK.hold();
        f.client.write_tabs(&f.spreadsheet, &[TabWrite { sheet_id: 1, range: "A1:C1".into(), rows: rows(&[&["id", "name", "n"]]) }]).unwrap();
    }

    assert!(f.fake.requests().is_empty());
}

#[test]
fn rows_added_and_blanked_are_written_and_kept() {
    let f = kept_fixture();

    f.write(1, "A2:C", rows(&[&["a", "Ann", "1"], &["c", "Cy", "3"], &["", "", ""], &["d", "Di"]]));

    assert_eq!(written_ranges(&f), vec![vec![(2, 5)]]);
    assert_eq!(f.read(1, "A2:C"), rows(&[&["a", "Ann", "1"], &["c", "Cy", "3"], &[], &["d", "Di"]]));
    assert_eq!(f.reads(), 0);
}

#[test]
fn writes_to_several_tabs_are_one_request() {
    let f = kept_fixture();
    {
        let _held = WRITE_LOCK.hold();
        f.client.write_tabs(&f.spreadsheet, &[
            TabWrite { sheet_id: 1, range: "A2:C2".into(), rows: rows(&[&["a", "Al", "1"]]) },
            TabWrite { sheet_id: 0, range: "A1:A1".into(), rows: rows(&[&["new"]]) },
        ]).unwrap();
    }

    assert_eq!(written_ranges(&f), vec![vec![(1, 2), (0, 1)]]);
}

#[test]
fn deleting_rows_moves_the_kept_rows_up() {
    let f = kept_fixture();
    {
        let _held = WRITE_LOCK.hold();
        f.client.delete_rows(&f.spreadsheet, 1, 2, 3, 0).unwrap();
    }

    assert_eq!(f.read(1, "A1:C"), rows(&[&["id", "name", "n"], &["c", "Cy", "3"]]));
    assert_eq!(f.reads(), 0);
}

#[test]
fn an_a1_write_or_a_failed_one_forgets_what_is_kept() {
    for how in ["write_rows", "failed write", "failed delete"] {
        let f = kept_fixture();
        {
            let _held = WRITE_LOCK.hold();
            match how {
                "write_rows" => f.client.write_rows(&f.spreadsheet, "'Second'!A2", &rows(&[&["z"]])).unwrap(),
                "failed write" => {
                    f.fake.with(|s| s.fail_next.push(("POST".into(), "values:batchUpdateByDataFilter".into(), 500, "no".into())));
                    f.client.write_rows_in_sheet(&f.spreadsheet, 1, "A2:A2", &rows(&[&["z"]])).unwrap_err();
                }
                _ => {
                    f.fake.with(|s| s.fail_next.push(("POST".into(), ":batchUpdate".into(), 500, "no".into())));
                    f.client.delete_rows(&f.spreadsheet, 1, 2, 2, 0).unwrap_err();
                }
            }
        }

        f.read(1, "A1:C");

        assert_eq!(f.reads(), 1, "{how}");
    }
}

#[test]
fn a_new_tab_is_kept_empty_without_reading_it() {
    let f = fixture();
    let id = {
        let _held = WRITE_LOCK.hold();
        f.client.add_sheet(&f.spreadsheet, "New", None).unwrap()
    };

    assert!(f.read(id, "A1:Z").is_empty());
    assert_eq!(f.reads(), 0);
}

#[test]
fn a_write_moves_its_tab_on_as_it_begins_and_again_once_it_is_kept() {
    // So neither a read nor a refresh that saw the tab mid-write takes
    // what it saw as current.
    let f = kept_fixture();
    let generation = || f.client.kept(&f.spreadsheet, |tabs| tabs[&1].generation);
    let before = generation();

    f.write(1, "A2:A2", rows(&[&["z"]]));
    {
        let _held = WRITE_LOCK.hold();
        f.client.delete_rows(&f.spreadsheet, 1, 4, 4, 0).unwrap();
    }

    assert_eq!(generation(), before + 4);
}

// -- refreshing: what was changed by hand is put back -----------------------------------------------

fn refresh(f: &Fixture) -> Refreshed {
    f.client.refresh(&f.spreadsheet, &[], true).unwrap()
}

#[test]
fn a_refresh_that_finds_nothing_changed_reads_once_and_writes_nothing() {
    let f = kept_fixture();

    assert_eq!(refresh(&f), Refreshed::default());
    assert_eq!(f.reads(), 1);
    assert!(f.requests("values:batchUpdateByDataFilter").is_empty());
}

#[test]
fn a_refresh_puts_back_cells_changed_by_hand_and_says_which() {
    let f = kept_fixture();
    f.fake.edit_by_hand(&f.spreadsheet, "Second", 2, 1, "Robert");
    f.fake.edit_by_hand(&f.spreadsheet, "Second", 2, 4, "note");
    f.fake.edit_by_hand(&f.spreadsheet, "Second", 0, 2, "count");

    let found = refresh(&f);

    let cells: Vec<(String, String, String)> = found.changed[0].cells.iter().map(|d| (kept::a1(d.row, d.column), d.found.clone(), d.kept.clone())).collect();
    assert_eq!(
        cells,
        vec![
            ("C1".into(), "count".into(), "n".into()),
            ("B3".into(), "Robert".into(), "Bo".into()),
            ("E3".into(), "note".into(), String::new()),
        ]
    );
    assert!(found.changed[0].restored && !found.deferred);
    assert_eq!(f.fake.rows(&f.spreadsheet, "Second"), rows(&[&["id", "name", "n"], &["a", "Ann", "1"], &["b", "Bo", "2"], &["c", "Cy", "3"]]));
    assert_eq!(written_ranges(&f), vec![vec![(0, 1), (2, 3)]]);
    assert_eq!(refresh(&f), Refreshed::default());
}

#[test]
fn a_row_inserted_by_hand_is_taken_out_again() {
    let f = kept_fixture();
    // Row 3 inserted: what was rows 3 and 4 is now rows 4 and 5.
    for (row, cells) in [(2, ["inserted", "", ""]), (3, ["b", "Bo", "2"]), (4, ["c", "Cy", "3"])] {
        for (column, text) in cells.iter().enumerate() {
            f.fake.edit_by_hand(&f.spreadsheet, "Second", row, column, text);
        }
    }

    let found = refresh(&f);

    assert!(found.changed[0].restored);
    assert_eq!(f.fake.rows(&f.spreadsheet, "Second"), rows(&[&["id", "name", "n"], &["a", "Ann", "1"], &["b", "Bo", "2"], &["c", "Cy", "3"]]));
}

#[test]
fn a_tab_deleted_by_hand_is_made_again_as_it_was() {
    let f = kept_fixture();
    let identity = TabIdentity { title: "Second".into(), color: Some(json!({"red": 0.26})), key: "sheet-role".into(), value: "second".into() };
    f.client.identify(&f.spreadsheet, 1, identity);
    f.fake.delete_tab_by_hand(&f.spreadsheet, "Second");

    let found = refresh(&f);

    assert_eq!(found.deleted, vec![DeletedTab { sheet_id: 1, title: Some("Second".into()), restored: true }]);
    assert_eq!(f.fake.rows(&f.spreadsheet, "Second"), rows(&[&["id", "name", "n"], &["a", "Ann", "1"], &["b", "Bo", "2"], &["c", "Cy", "3"]]));
    assert_eq!(f.client.find_sheet_id(&f.spreadsheet, "sheet-role", "second").unwrap(), Some(1));
    assert!(f.fake.tab_ids(&f.spreadsheet).contains(&(1, "Second".to_string())));
}

#[test]
fn a_tab_deleted_by_hand_whose_title_was_taken_comes_back_beside_it() {
    let f = kept_fixture();
    f.client.identify(&f.spreadsheet, 1, TabIdentity { title: "Second".into(), color: None, key: "sheet-role".into(), value: "second".into() });
    f.fake.delete_tab_by_hand(&f.spreadsheet, "Second");
    {
        let _held = WRITE_LOCK.hold();
        f.client.batch_update(&f.spreadsheet, json!({"requests": [{"addSheet": {"properties": {"title": "Second", "sheetId": 7}}}]}), &[]).unwrap();
    }

    refresh(&f);

    assert!(f.fake.tab_ids(&f.spreadsheet).contains(&(1, "Second (restored)".to_string())));
}

#[test]
fn a_deleted_tab_it_cannot_make_again_is_forgotten() {
    let f = kept_fixture();
    f.fake.delete_tab_by_hand(&f.spreadsheet, "Second");

    let found = refresh(&f);

    assert_eq!(found.deleted, vec![DeletedTab { sheet_id: 1, title: None, restored: false }]);
    assert!(f.client.kept_rows(&f.spreadsheet, 1).is_none());
}

#[test]
fn a_refresh_gives_way_to_a_tool_holding_the_write_lock() {
    let f = kept_fixture();
    f.fake.edit_by_hand(&f.spreadsheet, "Second", 2, 1, "Robert");
    let (held_tx, held_rx) = std::sync::mpsc::channel();
    let (done_tx, done_rx) = std::sync::mpsc::channel::<()>();
    let holder = std::thread::spawn(move || {
        let _held = WRITE_LOCK.hold();
        held_tx.send(()).unwrap();
        done_rx.recv().unwrap();
    });
    held_rx.recv().unwrap();

    let found = f.client.refresh(&f.spreadsheet, &[], false).unwrap();

    done_tx.send(()).unwrap();
    holder.join().unwrap();
    assert!(found.deferred && !found.changed[0].restored);
    assert_eq!(f.fake.rows(&f.spreadsheet, "Second")[2][1], "Robert");
    assert!(refresh(&f).changed[0].restored);
    assert_eq!(f.fake.rows(&f.spreadsheet, "Second")[2][1], "Bo");
}

#[test]
fn a_refresh_leaves_out_the_tabs_it_is_told_to_skip_and_those_not_kept() {
    let f = kept_fixture();
    f.client.identify(&f.spreadsheet, 1, TabIdentity { title: "Second".into(), color: None, key: "sheet-role".into(), value: "health".into() });
    f.fake.edit_by_hand(&f.spreadsheet, "Second", 2, 1, "Robert");
    f.fake.edit_by_hand(&f.spreadsheet, "Sheet1", 0, 0, "never read");

    assert_eq!(f.client.refresh(&f.spreadsheet, &["health"], true).unwrap(), Refreshed::default());
    assert_eq!(f.reads(), 0);
}

// -- retrying a rate limit --------------------------------------------------------------------------

fn pauses() -> Vec<f64> {
    PAUSES.with(|p| std::mem::take(&mut *p.borrow_mut()))
}

#[test]
fn retries_a_rate_limited_request_with_growing_backoff() {
    let f = fixture();
    f.write(1, "A2:A2", rows(&[&["a"]]));
    pauses();
    f.fake.with(|s| s.rate_limit_next = 2);

    assert_eq!(f.read(1, "A2:D"), rows(&[&["a"]]));

    let waited = pauses();
    assert_eq!(waited.len(), 2);
    assert!(waited[0] < 1.0 && waited[1] < 2.0, "{waited:?}");
}

#[test]
fn gives_up_after_about_a_minute_of_retries() {
    let f = fixture();
    pauses();
    f.fake.with(|s| s.rate_limit_next = 100);

    let error = f.client.read_rows_in_sheet(&f.spreadsheet, 1, "A2:D").unwrap_err();

    assert_eq!(status_of(&error), Some(429));
    let waited = pauses();
    assert_eq!(waited.len(), 6);
    // At most 1 + 2 + 4 + 8 + 16 + 32 seconds.
    assert!(waited.iter().enumerate().all(|(i, w)| *w < 2f64.powi(i as i32)), "{waited:?}");
    assert_eq!(f.reads(), 7);
}

#[test]
fn never_retries_anything_but_a_rate_limit() {
    for status in [400, 403, 500, 503] {
        let f = fixture();
        pauses();
        f.fake.with(|s| s.fail_next.push(("POST".into(), "values:batchGetByDataFilter".into(), status, "no".into())));

        let error = f.client.read_rows_in_sheet(&f.spreadsheet, 1, "A2:D").unwrap_err();

        assert_eq!(status_of(&error), Some(status));
        assert_eq!(f.reads(), 1);
        assert!(pauses().is_empty());
    }
}

// -- A1 reads and writes ------------------------------------------------------------------------------

#[test]
fn reads_and_writes_an_a1_range() {
    let f = fixture();
    assert!(f.client.read_rows(&f.spreadsheet, "Sheet1!A2:D").unwrap().is_empty());

    {
        let _held = WRITE_LOCK.hold();
        f.client.write_rows(&f.spreadsheet, "Sheet1!A2:D", &rows(&[&["l1", "Design Work", "#8e24aa", "1"]])).unwrap();
    }

    let write = f.fake.requests().into_iter().find(|r| r.method == "PUT").unwrap();
    assert!(write.path.ends_with("/values/Sheet1!A2:D"), "{}", write.path);
    assert_eq!(write.query, vec![("valueInputOption".to_string(), "RAW".to_string())]);
    assert_eq!(write.body, json!({"values": [["l1", "Design Work", "#8e24aa", "1"]]}));
    assert_eq!(f.client.read_rows(&f.spreadsheet, "Sheet1!A2:D").unwrap(), rows(&[&["l1", "Design Work", "#8e24aa", "1"]]));
}

// -- the write lock ----------------------------------------------------------------------------------

#[test]
fn a_write_refuses_without_the_write_lock_and_sends_nothing() {
    let f = fixture();
    let one = rows(&[&["x"]]);
    let writes: Vec<(&str, anyhow::Result<()>)> = vec![
        ("create_spreadsheet", f.client.create_spreadsheet("x").map(drop)),
        ("add_sheet", f.client.add_sheet(&f.spreadsheet, "x", None).map(drop)),
        ("update_sheet_properties", f.client.update_sheet_properties(&f.spreadsheet, 0, Some("x"), None)),
        ("create_sheet_metadata", f.client.create_sheet_metadata(&f.spreadsheet, 0, "k", "v")),
        ("set_column_width", f.client.set_column_width(&f.spreadsheet, 0, 0, 60)),
        ("delete_rows", f.client.delete_rows(&f.spreadsheet, 0, 2, 2, 0)),
        ("write_rows", f.client.write_rows(&f.spreadsheet, "Sheet1!A1", &one)),
        ("write_rows_in_sheet", f.client.write_rows_in_sheet(&f.spreadsheet, 0, "A1:A1", &one)),
    ];

    for (name, result) in writes {
        let error = result.expect_err(name);
        assert!(error.to_string().contains("must hold WRITE_LOCK"), "{name}: {error:#}");
    }
    assert!(f.fake.requests().is_empty());
}

#[test]
fn a_read_needs_no_lock() {
    let f = fixture();

    assert!(f.client.read_rows(&f.spreadsheet, "Sheet1!A1:A1").unwrap().is_empty());
}
