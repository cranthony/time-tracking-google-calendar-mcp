//! tests/test_google_sheets.py, against the fake Google.

use super::*;
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

fn cached<T>(f: impl FnOnce() -> T) -> T {
    let _reads = CachedReads::start();
    f()
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
        json!({"dataFilters": [{"gridRange": {"sheetId": 1, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 4}}], "majorDimension": "ROWS"})
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
    assert!(fallback.path.ends_with("/values/'Chris''s Labels'!A2:D"), "{}", fallback.path);
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

// -- several ranges, and the read cache ------------------------------------------------------------

#[test]
fn reads_every_range_in_one_request() {
    let f = fixture();
    f.write(1, "A1:C3", rows(&[&["header"], &["a"], &["b"]]));
    f.fake.clear_requests();

    let result = f.client.read_ranges_in_sheet(&f.spreadsheet, 1, &["A1:C1", "A2:C"]).unwrap();

    assert_eq!(result, vec![rows(&[&["header"]]), rows(&[&["a"], &["b"]])]);
    assert_eq!(f.reads(), 1);
    let starts: Vec<Value> = f.requests("values:batchGetByDataFilter")[0].body["dataFilters"].as_array().unwrap().iter().map(|d| d["gridRange"]["startRowIndex"].clone()).collect();
    assert_eq!(starts, vec![json!(0), json!(1)]);
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
fn caches_each_range_and_requests_only_the_ones_not_cached() {
    let f = fixture();
    f.write(1, "A1:C2", rows(&[&["header"], &["a"]]));
    f.fake.clear_requests();

    let (both, header, data) = cached(|| {
        f.read(1, "A1:C1");
        let both = f.client.read_ranges_in_sheet(&f.spreadsheet, 1, &["A1:C1", "A2:C"]).unwrap();
        (both, f.read(1, "A1:C1"), f.read(1, "A2:C"))
    });

    assert_eq!(both, vec![rows(&[&["header"]]), rows(&[&["a"]])]);
    assert_eq!((header, data), (rows(&[&["header"]]), rows(&[&["a"]])));
    let reads = f.requests("values:batchGetByDataFilter");
    assert_eq!(reads.len(), 2);
    assert_eq!(reads[1].body["dataFilters"].as_array().unwrap().len(), 1);
}

#[test]
fn a_range_within_one_read_is_cut_out_of_it_as_the_api_would_return_it() {
    for range in ["A1:D1", "A2:D", "A3:D4", "B2:C", "A2:B3", "D2:D", "A4:D9", "C1:C1", "A6:D"] {
        let f = fixture();
        f.seeded();
        let expected = f.read(0, range);
        f.fake.clear_requests();

        let within = cached(|| {
            f.read(0, "A1:D");
            f.read(0, range)
        });

        assert_eq!(within, expected, "{range}");
        assert_eq!(f.reads(), 1, "{range}");
    }
}

#[test]
fn a_range_reaching_outside_what_was_read_is_requested() {
    let f = fixture();
    f.seeded();

    cached(|| {
        f.read(0, "A2:B");
        f.read(0, "A2:C"); // a column more
        f.read(0, "A1:B"); // a row higher
    });
    cached(|| {
        f.read(0, "A1:D3");
        f.read(0, "A1:D"); // open-ended, past a bounded one
    });

    assert_eq!(f.reads(), 5);
}

#[test]
fn another_tabs_range_is_never_used() {
    let f = fixture();
    f.seeded();

    let other = cached(|| {
        f.read(0, "A1:D");
        f.read(1, "A1:B1")
    });

    assert_eq!(other, rows(&[&["other", "tab"]]));
    assert_eq!(f.reads(), 2);
}

#[test]
fn a_write_to_the_tab_forgets_the_range_it_was_within() {
    let f = fixture();
    f.seeded();

    let read = cached(|| {
        f.read(0, "A1:D");
        f.write(0, "A3:A3", rows(&[&["new"]]));
        f.read(0, "A3:A3")
    });

    assert_eq!(read, rows(&[&["new"]]));
    assert_eq!(f.reads(), 2);
}

#[test]
fn prefetch_reads_several_tabs_in_one_request_for_later_reads_within_them() {
    let f = fixture();
    f.write(0, "A1:B2", rows(&[&["h"], &["one"]]));
    f.write(1, "A1:B2", rows(&[&["h"], &["two"]]));
    f.fake.clear_requests();

    let (one, two) = cached(|| {
        f.client.prefetch(&[TabRange::new(&f.spreadsheet, 0, "A1:B"), TabRange::new(&f.spreadsheet, 1, "A1:B")]).unwrap();
        (f.client.read_ranges_in_sheet(&f.spreadsheet, 0, &["A1:B1", "A2:B"]).unwrap(), f.read(1, "A2:B"))
    });

    assert_eq!((one, two), (vec![rows(&[&["h"]]), rows(&[&["one"]])], rows(&[&["two"]])));
    assert_eq!(f.reads(), 1);
}

#[test]
fn prefetch_skips_what_is_already_cached() {
    let f = fixture();

    cached(|| {
        f.read(0, "A1:B");
        f.client.prefetch(&[TabRange::new(&f.spreadsheet, 0, "A2:B"), TabRange::new(&f.spreadsheet, 1, "A1:B")]).unwrap();
    });

    let last = f.requests("values:batchGetByDataFilter").pop().unwrap();
    let filters = last.body["dataFilters"].as_array().unwrap().clone();
    assert_eq!(filters.len(), 1);
    assert_eq!(filters[0]["gridRange"]["sheetId"], json!(1));
}

#[test]
fn prefetch_does_nothing_outside_a_tool_calls_cache() {
    let f = fixture();

    f.client.prefetch(&[TabRange::new(&f.spreadsheet, 0, "A1:B")]).unwrap();

    assert_eq!(f.reads(), 0);
}

#[test]
fn repeated_reads_in_a_scope_are_served_from_memory_across_clients() {
    let f = fixture();
    f.write(1, "A2:A2", rows(&[&["a"]]));
    f.fake.clear_requests();
    let other = SheetsClient::new(f.fake.google());

    let (first, second) = cached(|| (f.read(1, "A2:D"), other.read_rows_in_sheet(&f.spreadsheet, 1, "A2:D").unwrap()));

    assert_eq!((first.clone(), second), (rows(&[&["a"]]), rows(&[&["a"]])));
    assert_eq!(f.reads(), 1);
}

#[test]
fn different_ranges_and_tabs_are_read_separately() {
    let f = fixture();

    cached(|| {
        f.read(1, "A2:D");
        f.read(1, "A1:D1");
        f.read(0, "A2:D");
    });

    assert_eq!(f.reads(), 3);
}

#[test]
fn a_caller_mutating_what_it_read_does_not_change_the_cache() {
    let f = fixture();
    f.write(1, "A2:A2", rows(&[&["a"]]));

    let again = cached(|| {
        f.read(1, "A2:D")[0].push("mutated".into());
        f.read(1, "A2:D").push(vec!["mutated".into()]);
        f.read(1, "A2:D")
    });

    assert_eq!(again, rows(&[&["a"]]));
}

#[test]
fn a_write_to_the_tab_through_any_client_forgets_its_cached_reads() {
    for how in ["write_rows_in_sheet", "delete_rows", "write_rows"] {
        let f = fixture();
        f.write(1, "A2:A2", rows(&[&["a"]]));
        f.fake.clear_requests();
        let other = SheetsClient::new(f.fake.google());

        let read = cached(|| {
            f.read(1, "A2:D");
            let _held = WRITE_LOCK.hold();
            match how {
                "write_rows_in_sheet" => other.write_rows_in_sheet(&f.spreadsheet, 1, "A2:A2", &rows(&[&["b"]])).unwrap(),
                "delete_rows" => other.delete_rows(&f.spreadsheet, 1, 3, 3, 0).unwrap(),
                _ => other.write_rows(&f.spreadsheet, "'Second'!A2", &rows(&[&["b"]])).unwrap(),
            }
            f.read(1, "A2:D")
        });

        assert_eq!(f.reads(), 2, "{how}");
        if how != "delete_rows" {
            assert_eq!(read, rows(&[&["b"]]), "{how}");
        }
    }
}

#[test]
fn a_write_to_another_tab_keeps_this_tabs_cached_reads() {
    let f = fixture();

    cached(|| {
        f.read(1, "A2:D");
        f.write(0, "A2:D2", rows(&[&["x"]]));
        f.read(1, "A2:D");
    });

    assert_eq!(f.reads(), 1);
}

#[test]
fn nothing_is_cached_outside_a_scope_or_across_scopes() {
    let f = fixture();

    cached(|| f.read(1, "A2:D"));
    cached(|| f.read(1, "A2:D"));
    f.read(1, "A2:D");

    assert_eq!(f.reads(), 3);
}

#[test]
fn a_nested_scope_shares_the_outer_ones_cache() {
    let f = fixture();

    cached(|| {
        f.read(1, "A2:D");
        cached(|| f.read(1, "A2:D"));
        f.read(1, "A2:D");
    });

    assert_eq!(f.reads(), 1);
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
