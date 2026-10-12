//! The module's tests, against the fake Google.

use super::*;
use crate::sheet::TIME_NOTES;
use crate::testing::fake_google::FakeGoogle;
use crate::write_lock::WRITE_LOCK;

const T1: &str = "2026-01-01T09:00:00+00:00";
const T2: &str = "2026-01-01T10:00:00+00:00";
const OLD: &str = "2025-12-31T09:00:00+00:00";

fn header() -> Vec<String> {
    FIELDS.iter().map(|f| f.to_string()).collect()
}

fn at(text: &str) -> Moment {
    parse_iso(text, None).unwrap()
}

fn row(cells: &[&str]) -> Vec<String> {
    cells.iter().map(|c| c.to_string()).collect()
}

fn id(timestamp: &str, row: i64) -> String {
    format!("{timestamp}#{row}")
}

fn noted(timestamp: &str, description: Option<&str>) -> NotedTime {
    NotedTime { timestamp: at(timestamp), description: description.map(str::to_string), compaction_id: None }
}

struct Fixture {
    fake: FakeGoogle,
    sheet: NotedTimeSheet,
    spreadsheet: String,
}

/// A notes tab holding `rows` below its header.
fn fixture(rows: Vec<Vec<String>>) -> Fixture {
    let fake = FakeGoogle::start();
    let sheets = SheetsClient::new(fake.google());
    let _held = WRITE_LOCK.hold();
    let spreadsheet = sheets.create_spreadsheet("Calendar Metadata").unwrap();
    let sheet = NotedTimeSheet::ensure(&sheets, &spreadsheet).unwrap();
    fake.replace_rows(&spreadsheet, TIME_NOTES.1, rows);
    fake.clear_requests();
    Fixture { fake, sheet, spreadsheet }
}

impl Fixture {
    fn set_header(&self, names: &[&str]) {
        for (column, name) in names.iter().enumerate() {
            self.fake.set_cell(&self.spreadsheet, TIME_NOTES.1, 0, column, name);
        }
        for column in names.len()..3 {
            self.fake.set_cell(&self.spreadsheet, TIME_NOTES.1, 0, column, "");
        }
    }

    fn rows(&self) -> Vec<Vec<String>> {
        let mut rows = self.fake.rows(&self.spreadsheet, TIME_NOTES.1);
        rows.remove(0);
        rows
    }

    /// Each write: the A1 range it covered, and its values.
    fn writes(&self) -> Vec<(String, Value)> {
        self.fake
            .requests()
            .into_iter()
            .filter(|r| r.path.ends_with("values:batchUpdateByDataFilter"))
            .map(|r| {
                let data = &r.body["data"][0];
                let grid = &data["dataFilter"]["gridRange"];
                let column = |index: &Value| ((b'A' + index.as_u64().unwrap() as u8) as char).to_string();
                let range = format!(
                    "{}{}:{}{}",
                    column(&grid["startColumnIndex"]),
                    grid["startRowIndex"].as_i64().unwrap() + 1,
                    column(&json!(grid["endColumnIndex"].as_u64().unwrap() - 1)),
                    grid["endRowIndex"].as_i64().unwrap()
                );
                (range, data["values"].clone())
            })
            .collect()
    }

    fn reads(&self) -> Vec<Vec<Value>> {
        self.fake
            .requests()
            .into_iter()
            .filter(|r| r.path.ends_with("values:batchGetByDataFilter"))
            .map(|r| r.body["dataFilters"].as_array().unwrap().iter().map(|f| f["gridRange"].clone()).collect())
            .collect()
    }

    fn held<T>(&self, f: impl FnOnce(&NotedTimeSheet) -> T) -> T {
        let _held = WRITE_LOCK.hold();
        f(&self.sheet)
    }

    fn ids(&self, include_compacted: bool) -> Vec<String> {
        self.sheet.read_with_rows(include_compacted).unwrap().iter().map(SheetNote::id).collect()
    }
}

fn compacted(n: usize, timestamp: &str, description: &str) -> Vec<Vec<String>> {
    (0..n).map(|_| row(&[timestamp, description, "cmp1"])).collect()
}

// -- a note from and to a row -------------------------------------------------------

#[test]
fn parses_a_full_row() {
    let note = NotedTime::from_row(&header(), &row(&[T1, "Started work", "abc"])).unwrap();

    assert_eq!(note, NotedTime { timestamp: at(T1), description: Some("Started work".into()), compaction_id: Some("abc".into()) });
}

#[test]
fn blank_and_missing_cells_become_none() {
    // Sheets omits trailing blank cells from a row entirely.
    assert_eq!(NotedTime::from_row(&header(), &row(&[T1, "", ""])).unwrap(), noted(T1, None));
    assert_eq!(NotedTime::from_row(&header(), &row(&[T1])).unwrap(), noted(T1, None));
}

#[test]
fn reads_columns_by_name_ignoring_unknown_ones() {
    assert_eq!(NotedTime::from_row(&row(&["timestamp", "description", "notes"]), &row(&[T1, "Started work", "some note"])).unwrap(), noted(T1, Some("Started work")));
    assert_eq!(NotedTime::from_row(&row(&["description", "timestamp"]), &row(&["Started work", T1])).unwrap(), noted(T1, Some("Started work")));
}

#[test]
fn a_row_without_a_timestamp_is_refused() {
    assert!(NotedTime::from_row(&header(), &row(&["", "Started work"])).is_err());
}

#[test]
fn writes_a_row_preserving_unknown_columns() {
    let note = NotedTime { compaction_id: Some("abc".into()), ..noted(T1, Some("Started work")) };
    assert_eq!(note.to_row(&header(), None), row(&[T1, "Started work", "abc"]));
    assert_eq!(noted(T1, None).to_row(&header(), None), row(&[T1, "", ""]));

    let original = row(&["2025-01-01T00:00:00+00:00", "Old", "some note"]);
    assert_eq!(noted(T1, Some("Started work")).to_row(&row(&["timestamp", "description", "notes"]), Some(&original)), row(&[T1, "Started work", "some note"]));
    assert_eq!(noted(T1, None).to_row(&row(&["timestamp", "notes"]), None), row(&[T1, ""]));
}

// -- the tab --------------------------------------------------------------------------

#[test]
fn ensure_creates_and_tags_a_tab_with_its_header_then_reuses_it() {
    let f = fixture(vec![]);
    assert_eq!(f.fake.rows(&f.spreadsheet, TIME_NOTES.1)[0], header());
    assert_eq!(f.sheet.spreadsheet_id(), f.spreadsheet);

    let again = {
        let _held = WRITE_LOCK.hold();
        NotedTimeSheet::ensure(&SheetsClient::new(f.fake.google()), &f.spreadsheet).unwrap()
    };

    assert_eq!(again.sheet_id, f.sheet.sheet_id);
    assert!(f.fake.requests().iter().all(|r| !r.path.ends_with(":batchUpdate") && !r.path.ends_with("batchUpdateByDataFilter")));
}

#[test]
fn reads_notes_sorted_by_timestamp() {
    let f = fixture(vec![row(&[T2, "Second"]), row(&[T1, "First"])]);

    let read: Vec<Option<String>> = f.sheet.read(false).unwrap().into_iter().map(|n| n.description).collect();

    assert_eq!(read, vec![Some("First".to_string()), Some("Second".to_string())]);
    assert!(fixture(vec![]).sheet.read(false).unwrap().is_empty());
}

#[test]
fn only_uncompacted_notes_are_read_unless_asked() {
    let f = fixture(vec![row(&[T1, "Old", "cmp1"]), row(&[T2, "New"])]);

    let descriptions = |include: bool| -> Vec<String> { f.sheet.read(include).unwrap().into_iter().filter_map(|n| n.description).collect() };
    assert_eq!(descriptions(false), vec!["New"]);
    assert_eq!(descriptions(true), vec!["Old", "New"]);
}

#[test]
fn a_header_missing_its_columns_is_refused() {
    let f = fixture(vec![]);
    f.set_header(&["timestamp", "description"]);

    let error = f.sheet.read(false).unwrap_err();

    assert!(error.to_string().contains("Missing expected header columns"), "{error}");
}

#[test]
fn numbers_each_note_by_its_row_skipping_blank_ones() {
    let f = fixture(vec![row(&[T2, "Second"]), row(&[T1, "First"])]);
    let notes: Vec<(i64, String)> = f.sheet.read_with_rows(false).unwrap().iter().map(|n| (n.row, n.id())).collect();
    assert_eq!(notes, vec![(2, id(T2, 2)), (3, id(T1, 3))]);

    let f = fixture(vec![row(&[T1, "A"]), vec![], row(&[T2, "B"])]);
    assert_eq!(f.ids(false), vec![id(T1, 2), id(T2, 4)]);
}

#[test]
fn leaves_out_compacted_notes_without_renumbering_the_rest() {
    let f = fixture(vec![row(&[T1, "Old", "cmp1"]), row(&[T2, "New"])]);

    assert_eq!(f.ids(false), vec![id(T2, 3)]);
    assert_eq!(f.ids(true), vec![id(T1, 2), id(T2, 3)]);
}

#[test]
fn reads_the_header_and_every_row_in_one_request() {
    let f = fixture(vec![row(&[T1, "a", "cmp1"]), row(&[T2, "open"])]);

    let (notes, latest) = f.sheet.read_with_latest_compacted().unwrap();

    let open: Vec<Option<String>> = notes.into_iter().map(|n| n.note.description).collect();
    assert_eq!(open, vec![Some("open".to_string())]);
    assert_eq!(latest.unwrap().description.as_deref(), Some("a"));
    let reads = f.reads();
    assert_eq!(reads.len(), 1);
    assert_eq!(reads[0].len(), 2);
}

#[test]
fn the_latest_compacted_note_has_the_latest_timestamp_then_the_latest_row() {
    let f = fixture(vec![row(&[T2, "later", "cmp1"]), row(&[T1, "earlier", "cmp1"]), row(&[T2, "open"])]);
    assert_eq!(f.sheet.read_with_latest_compacted().unwrap().1.unwrap().description.as_deref(), Some("later"));

    let f = fixture(vec![row(&[T1, "first", "cmp1"]), row(&[T1, "second", "cmp1"])]);
    assert_eq!(f.sheet.read_with_latest_compacted().unwrap().1.unwrap().description.as_deref(), Some("second"));

    let f = fixture(vec![row(&[T1, "open"]), vec![]]);
    assert_eq!(f.sheet.read_with_latest_compacted().unwrap().1, None);
}

// -- note ids --------------------------------------------------------------------------

#[test]
fn an_id_is_the_timestamp_and_the_row_together_and_round_trips() {
    let note = SheetNote { row: 7, note: noted("2026-01-01T09:05:00+00:00", None) };

    assert_eq!(note.id(), "2026-01-01T09:05:00+00:00#7");
    assert_eq!(parse_note_id(&note.id()).unwrap(), (at("2026-01-01T09:05:00+00:00"), 7));
    let other = SheetNote { row: 8, ..note.clone() };
    assert_ne!(note.id(), other.id());
}

#[test]
fn rejects_things_that_are_not_note_ids() {
    for bad in ["7", "n7", "#7", "2026-01-01T09:00:00+00:00", "2026-01-01T09:00:00+00:00#x", "nope#7", ""] {
        assert!(parse_note_id(bad).is_err(), "{bad:?}");
    }
}

// -- appending ---------------------------------------------------------------------------

#[test]
fn appends_only_the_new_row_after_the_existing_ones() {
    // Rows 2 and 3 are taken, so the new note goes in row 4 -- and the
    // existing rows are never rewritten (which could clobber a concurrent
    // compaction stamp on one of them).
    let f = fixture(vec![row(&["2025-01-01T00:00:00+00:00", "Old"]), row(&[T2, "Older"])]);

    let appended = f.held(|s| s.append(&noted(T1, Some("New")))).unwrap();

    assert_eq!(f.writes(), vec![("A4:C4".to_string(), json!([[T1, "New", ""]]))]);
    assert_eq!(appended.id(), id(T1, 4));
}

#[test]
fn appends_to_an_empty_tab() {
    let f = fixture(vec![]);

    f.held(|s| s.append(&noted(T1, None))).unwrap();

    assert_eq!(f.writes(), vec![("A2:C2".to_string(), json!([[T1, "", ""]]))]);
}

#[test]
fn appends_after_the_last_row_reading_the_tab_once() {
    let f = fixture(vec![row(&[T1, "a"]), vec![], row(&[T1, "c"])]);

    let appended = f.held(|s| s.append(&noted(T1, None))).unwrap();

    // After the last row, even with a blank one before it.
    assert_eq!(appended.row, 5);
    assert_eq!(f.reads().len(), 1);
}

#[test]
fn the_appended_note_is_read_back_by_its_id() {
    let f = fixture(vec![row(&[T1, "a"])]);

    let appended = f.held(|s| s.append(&noted(T2, Some("b")))).unwrap();

    assert_eq!(appended.id(), id(T2, 3));
    assert_eq!(f.ids(false), vec![id(T1, 2), id(T2, 3)]);
}

// -- garbage collection --------------------------------------------------------------------

fn collect(f: &Fixture) {
    f.held(|s| s.garbage_collect()).unwrap();
}

#[test]
fn does_nothing_under_the_row_budget() {
    let rows = compacted(50, T1, "a");
    let f = fixture(rows.clone());

    collect(&f);

    assert_eq!(f.rows(), rows);
}

#[test]
fn deletes_the_oldest_compacted_rows_once_over_budget() {
    // 55 compacted, then one uncompacted -- 56 in all, over the 50-row
    // budget, so it's trimmed to 25.
    let mut rows = compacted(55, T1, "a");
    rows.push(row(&[T2, "recent"]));
    let f = fixture(rows);

    collect(&f);

    let remaining = f.rows();
    assert_eq!(remaining.len(), 25);
    assert_eq!(remaining.last().unwrap(), &row(&[T2, "recent"]));
}

#[test]
fn always_keeps_the_latest_compacted_note() {
    // The latest compacted note is row 4, inside the 31 rows that would
    // otherwise go -- so only the two rows before it do, and it and
    // everything after it are kept, for the next compaction's
    // `previous_note`.
    let mut rows = vec![row(&[OLD, "a", "cmp1"]), row(&[OLD, "b", "cmp1"]), row(&[T2, "latest", "cmp1"])];
    rows.extend((0..52).map(|i| row(&[T1, &format!("older {i}"), "cmp1"])));
    rows.push(row(&[T2, "open"]));
    let f = fixture(rows);

    collect(&f);

    let remaining = f.rows();
    assert_eq!(remaining.len(), 54);
    assert_eq!(remaining[0], row(&[T2, "latest", "cmp1"]));
    assert_eq!(f.sheet.read_with_latest_compacted().unwrap().1.unwrap().description.as_deref(), Some("latest"));
}

#[test]
fn leaves_the_tab_at_least_1000_rows_long() {
    // Earlier garbage collection already shrank the tab's grid; without
    // topping it back up, it would eventually run out of rows to append
    // into.
    let mut rows = compacted(55, T1, "a");
    rows.push(row(&[T2, "recent"]));
    let f = fixture(rows);
    f.fake.set_row_count(&f.spreadsheet, TIME_NOTES.1, 100);

    collect(&f);

    assert_eq!(f.fake.row_count(&f.spreadsheet, TIME_NOTES.1), 1000);
}

#[test]
fn does_nothing_if_the_oldest_row_is_already_uncompacted() {
    let mut rows = vec![row(&[T1, "a"])];
    rows.extend(compacted(55, T1, "b"));
    let f = fixture(rows);

    collect(&f);

    assert_eq!(f.rows().len(), 56);
}

#[test]
fn deletes_only_the_compacted_prefix_when_it_falls_short_of_the_excess() {
    // An uncompacted note (row 5) sits within the 31 rows over budget --
    // only the 3 compacted rows before it are safe to delete.
    let mut rows = compacted(3, T1, "a");
    rows.push(row(&[T1, "b"]));
    rows.extend(compacted(52, T1, "c"));
    let f = fixture(rows);

    collect(&f);

    assert_eq!(f.rows().len(), 53);
}

#[test]
fn blank_rows_count_and_are_deleted_with_the_prefix() {
    let mut rows: Vec<Vec<String>> = (0..27).flat_map(|_| [vec![], row(&[T1, "a", "cmp1"])]).collect();
    rows.push(row(&[T2, "open"]));
    let f = fixture(rows);

    collect(&f);

    let remaining = f.rows();
    assert_eq!(remaining.len(), 25);
    assert_eq!(remaining.last().unwrap(), &row(&[T2, "open"]));
}

#[test]
fn append_garbage_collects_first_from_the_same_read() {
    let f = fixture(compacted(55, T1, "a"));

    let appended = f.held(|s| s.append(&noted(T1, None))).unwrap();

    let remaining = f.rows();
    assert_eq!(remaining.len(), 26); // trimmed from 55 to 25, + 1 appended
    assert_eq!(remaining.last().unwrap(), &row(&[T1]));
    assert_eq!(appended.row, 27);
    assert_eq!(f.reads().len(), 1);
}

// -- stamping notes compacted --------------------------------------------------------------

fn stamp(f: &Fixture, ids: &[String], compaction: &str) -> anyhow::Result<()> {
    f.held(|s| s.mark_compacted(ids, compaction))
}

#[test]
fn stamps_only_the_compaction_id_column_of_the_given_rows() {
    let f = fixture(vec![row(&[T1, "A"]), row(&[T2, "B"])]);

    stamp(&f, &[id(T2, 3)], "cmp1").unwrap();

    assert_eq!(f.writes(), vec![("C3:C3".to_string(), json!([["cmp1"]]))]);
}

#[test]
fn writes_one_range_per_contiguous_run_of_rows() {
    let f = fixture((0..6).map(|i| row(&[T1, &i.to_string()])).collect());

    stamp(&f, &[id(T1, 7), id(T1, 2), id(T1, 3), id(T1, 4), id(T1, 3)], "cmp1").unwrap();

    assert_eq!(f.writes(), vec![("C2:C4".to_string(), json!([["cmp1"], ["cmp1"], ["cmp1"]])), ("C7:C7".to_string(), json!([["cmp1"]]))]);
}

#[test]
fn finds_the_column_from_the_header_not_a_fixed_position() {
    let f = fixture(vec![row(&["", T1, "A"])]);
    f.set_header(&["compaction_id", "timestamp", "description"]);

    stamp(&f, &[id(T1, 2)], "cmp1").unwrap();

    assert_eq!(f.writes()[0].0, "A2:A2");
}

#[test]
fn refuses_a_row_that_now_holds_a_different_note_or_none() {
    let f = fixture(vec![row(&[T2, "edited since it was read"])]);
    let error = stamp(&f, &[id(T1, 2)], "cmp1").unwrap_err();
    assert!(error.to_string().contains("no longer holds the note"), "{error}");

    let f = fixture(vec![]);
    assert!(stamp(&f, &[id(T1, 5)], "cmp1").unwrap_err().to_string().contains("no longer holds the note"));
    assert!(f.writes().is_empty());
}

#[test]
fn writes_nothing_if_any_one_note_is_stale() {
    let f = fixture(vec![row(&[T1, "A"]), row(&[T2, "B"])]);

    assert!(stamp(&f, &[id(T1, 2), id(T1, 3)], "cmp1").is_err());

    assert!(f.writes().is_empty());
}

#[test]
fn refuses_a_note_already_compacted_by_a_different_compaction() {
    let f = fixture(vec![row(&[T1, "A", "other"])]);

    let error = stamp(&f, &[id(T1, 2)], "cmp1").unwrap_err();

    assert!(error.to_string().contains("already compacted by 'other'"), "{error}");
}

#[test]
fn stamping_again_with_the_same_compaction_is_harmless() {
    let f = fixture(vec![row(&[T1, "A", "cmp1"])]);

    stamp(&f, &[id(T1, 2)], "cmp1").unwrap();

    assert_eq!(f.writes().len(), 1);
}

#[test]
fn stamping_no_notes_does_nothing() {
    let f = fixture(vec![]);

    stamp(&f, &[], "cmp1").unwrap();

    assert!(f.fake.requests().is_empty());
}

// -- editing and deleting ----------------------------------------------------------------------

#[test]
fn edit_changes_the_description_in_place() {
    let f = fixture(vec![row(&[T1, "a"]), row(&[T2, "b"])]);

    let edited = f.held(|s| s.edit(&id(T1, 2), None, Some("started work"))).unwrap();

    assert_eq!(edited.id(), id(T1, 2));
    let notes: Vec<(String, Option<String>)> = f.sheet.read_with_rows(false).unwrap().into_iter().map(|n| (n.id(), n.note.description)).collect();
    assert_eq!(notes, vec![(id(T1, 2), Some("started work".to_string())), (id(T2, 3), Some("b".to_string()))]);
}

#[test]
fn changing_the_timestamp_changes_the_id_but_not_the_row() {
    let f = fixture(vec![row(&[T1, "a"]), row(&[T2, "b"])]);
    let later = "2026-01-01T09:30:00+00:00";

    let edited = f.held(|s| s.edit(&id(T1, 2), Some(at(later)), None)).unwrap();

    assert_eq!(edited.id(), id(later, 2));
    assert_eq!(edited.note.description.as_deref(), Some("a"));
    assert_eq!(f.ids(false), vec![id(later, 2), id(T2, 3)]);
}

#[test]
fn an_empty_description_clears_it() {
    let f = fixture(vec![row(&[T1, "a"])]);

    f.held(|s| s.edit(&id(T1, 2), None, Some("  "))).unwrap();

    assert_eq!(f.sheet.read_with_rows(false).unwrap()[0].note.description, None);
}

#[test]
fn edit_refuses_a_stale_id_or_a_compacted_note() {
    let f = fixture(vec![row(&[T2, "edited since it was read"])]);
    assert!(f.held(|s| s.edit(&id(T1, 2), None, Some("x"))).unwrap_err().to_string().contains("no longer holds the note"));

    let f = fixture(vec![row(&[T1, "a", "cmp1"])]);
    assert!(f.held(|s| s.edit(&id(T1, 2), None, Some("x"))).unwrap_err().to_string().contains("already compacted"));
    assert_eq!(f.sheet.read_with_rows(true).unwrap()[0].note.description.as_deref(), Some("a"));
}

#[test]
fn delete_blanks_the_row_without_moving_any_other_note() {
    let f = fixture(vec![row(&[T1, "a"]), row(&[T2, "b"]), row(&[T2, "c"])]);

    let deleted = f.held(|s| s.delete(&id(T2, 3))).unwrap();

    assert_eq!(deleted.description.as_deref(), Some("b"));
    let notes: Vec<(String, Option<String>)> = f.sheet.read_with_rows(false).unwrap().into_iter().map(|n| (n.id(), n.note.description)).collect();
    assert_eq!(notes, vec![(id(T1, 2), Some("a".to_string())), (id(T2, 4), Some("c".to_string()))]);
}

#[test]
fn a_deleted_id_is_stale_afterward() {
    let f = fixture(vec![row(&[T1, "a"])]);
    f.held(|s| s.delete(&id(T1, 2))).unwrap();

    assert!(f.held(|s| s.delete(&id(T1, 2))).unwrap_err().to_string().contains("no longer holds the note"));
}

#[test]
fn delete_refuses_a_compacted_note() {
    let f = fixture(vec![row(&[T1, "a", "cmp1"])]);

    assert!(f.held(|s| s.delete(&id(T1, 2))).unwrap_err().to_string().contains("already compacted"));
}

#[test]
fn the_next_note_still_appends_after_the_last_one() {
    let f = fixture(vec![row(&[T1, "a"]), row(&[T2, "b"])]);
    f.held(|s| s.delete(&id(T1, 2))).unwrap();

    let appended = f.held(|s| s.append(&noted(T2, Some("c")))).unwrap();

    assert_eq!(appended.id(), id(T2, 4));
}
