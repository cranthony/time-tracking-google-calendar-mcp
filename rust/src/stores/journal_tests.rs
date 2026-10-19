//! The module's tests, against the fake Google.

use super::*;
use crate::testing::fake_google::FakeGoogle;
use crate::testing::{event_at, strings, time_at};

struct Fixture {
    fake: FakeGoogle,
    sheets: SheetsClient,
    spreadsheet: String,
    journal: CompactionJournal,
}

fn fixture() -> Fixture {
    let fake = FakeGoogle::start();
    let sheets = SheetsClient::new(fake.google());
    let _held = crate::write_lock::WRITE_LOCK.hold();
    let spreadsheet = sheets.create_spreadsheet("Calendar Metadata").unwrap();
    let journal = CompactionJournal::ensure(&sheets, &spreadsheet).unwrap();
    Fixture { fake, sheets, spreadsheet, journal }
}

impl Fixture {
    fn rows(&self) -> Rows {
        self.journal.read_rows().unwrap()
    }

    fn write(&self, range: &str, rows: Rows) {
        let _held = crate::write_lock::WRITE_LOCK.hold();
        self.sheets.write_rows_in_sheet(&self.spreadsheet, self.journal.sheet_id, range, &rows).unwrap();
    }

    fn set_status(&self, compaction_id: &str, status: &str) {
        let _held = crate::write_lock::WRITE_LOCK.hold();
        let mut loaded = self.journal.load(compaction_id).unwrap();
        self.journal.set_status(&mut loaded, status).unwrap();
    }

    fn garbage_collect(&self) {
        let _held = crate::write_lock::WRITE_LOCK.hold();
        self.journal.garbage_collect().unwrap();
    }
}

fn state(event: crate::event::Event) -> Option<EventState> {
    Some(EventState::from_event(&event))
}

fn plan() -> CompactionPlan {
    let before = state(event_at("09:00-10:00").summary("Email").priority(2).build());
    let after = state(event_at("09:05-10:20").summary("Email").priority(2).build());
    let change = |action: &str, event_id: Option<&str>, reason: &str, before: Option<EventState>, after: Option<EventState>| CompactionChange {
        action: action.into(),
        reason: reason.into(),
        event_id: event_id.map(str::to_string),
        key: None,
        before,
        after,
    };
    CompactionPlan {
        changes: vec![
            change("cancel", Some("e9"), "in the past", before.clone(), None),
            change("update", Some("e1"), "actual", before, after.clone()),
            change("create", None, "unplanned", None, after),
        ],
        warnings: strings(&["a warning"]),
        ..Default::default()
    }
}

fn decisions() -> Vec<EventDecision> {
    vec![
        EventDecision { event_id: Some("e1".into()), start_note: Some("n2".into()), end: Some(time_at("10:20")), ..EventDecision::new("keep") },
        EventDecision {
            summary: Some("Coffee".into()),
            start_note: Some("n3".into()),
            end: Some(time_at("10:40")),
            ..EventDecision::new("create")
        },
    ]
}

fn start_with(journal: &CompactionJournal, compaction_id: &str, decisions: &[EventDecision], plan: &CompactionPlan, note_ids: &[&str], ignore_notes: &[&str]) {
    let day = PlannedDay {
        compaction_id: compaction_id.into(),
        now: time_at("11:00"),
        note_ids: strings(note_ids),
        decisions,
        plan,
        ignore_notes: strings(ignore_notes),
        additions: Map::new(),
        note_targets: Vec::new(),
        revisits: Vec::new(),
    };
    let _held = crate::write_lock::WRITE_LOCK.hold();
    journal.start_batch(&[day], None, &[]).unwrap();
}

fn start(journal: &CompactionJournal, compaction_id: &str) {
    start_with(journal, compaction_id, &decisions(), &plan(), &["n2", "n3"], &[]);
}

fn unknown(journal: &CompactionJournal, compaction_id: &str) -> bool {
    match journal.load(compaction_id) {
        Ok(_) => false,
        Err(error) => error.to_string().contains(&format!("no compaction with id '{compaction_id}'")),
    }
}

fn big_plan(steps: usize, summary: &str) -> CompactionPlan {
    let state = state(event_at("09:00-10:00").summary(summary).build());
    CompactionPlan {
        changes: (0..steps)
            .map(|i| CompactionChange {
                action: "update".into(),
                reason: "r".into(),
                event_id: Some(format!("e{i}")),
                key: None,
                before: state.clone(),
                after: state.clone(),
            })
            .collect(),
        ..Default::default()
    }
}

// -- start and load ---------------------------------------------------------------

#[test]
fn round_trips_the_whole_approved_plan() {
    let f = fixture();
    start(&f.journal, "abc123");

    let loaded = f.journal.load("abc123").unwrap();

    assert_eq!(loaded.id, "abc123");
    assert_eq!(loaded.status, PLANNED);
    assert_eq!(loaded.now, time_at("11:00"));
    assert_eq!(loaded.note_ids, strings(&["n2", "n3"]));
    assert_eq!(loaded.warnings, strings(&["a warning"]));
    assert_eq!(loaded.decisions, decisions());
    assert!(loaded.ignore_notes.is_empty());
    let steps: Vec<(i64, &str, Option<&str>, &str)> =
        loaded.steps.iter().map(|s| (s.step, s.action.as_str(), s.event_id.as_deref(), s.status.as_str())).collect();
    assert_eq!(steps, vec![(1, "cancel", Some("e9"), "pending"), (2, "update", Some("e1"), "pending"), (3, "create", None, "pending")]);
    assert_eq!(loaded.changes(), plan().changes);
}

#[test]
fn round_trips_ignored_notes() {
    let f = fixture();

    start_with(&f.journal, "abc123", &decisions(), &plan(), &["n2", "n3"], &["n3"]);

    assert_eq!(f.journal.load("abc123").unwrap().ignore_notes, strings(&["n3"]));
}

#[test]
fn skips_rows_of_the_retired_disposition_kind() {
    let f = fixture();
    start(&f.journal, "abc123");
    f.write("A8:H8", vec![strings(&["abc123", "0", "disposition", "n2", "[]", "", "", ""])]);

    assert_eq!(f.journal.load("abc123").unwrap().decisions, decisions());
}

#[test]
fn writes_everything_in_one_write_so_a_crash_cannot_leave_half_a_plan() {
    let f = fixture();
    f.fake.clear_requests();

    start(&f.journal, "abc123");

    let writes = f.fake.requests().into_iter().filter(|r| r.path.ends_with("values:batchUpdateByDataFilter")).count();
    assert_eq!(writes, 1);
}

#[test]
fn a_second_compaction_is_appended_below_the_first() {
    let f = fixture();
    start(&f.journal, "first");
    start(&f.journal, "second");

    assert_eq!(f.journal.load("first").unwrap().id, "first");
    assert_eq!(f.journal.load("second").unwrap().id, "second");
    assert!(f.journal.load("second").unwrap().row > f.journal.load("first").unwrap().row);
}

#[test]
fn loading_an_unknown_compaction_says_so() {
    let f = fixture();

    assert!(unknown(&f.journal, "nope"));
}

#[test]
fn keeps_a_large_plan_out_of_any_single_cell() {
    let f = fixture();

    start_with(&f.journal, "big", &[], &big_plan(200, &"x".repeat(500)), &[], &[]);

    let longest = f.rows().iter().flatten().map(String::len).max().unwrap();
    assert!(longest < 50_000);
    assert_eq!(f.journal.load("big").unwrap().steps.len(), 200);
}

// -- reading the whole tab --------------------------------------------------------
// Every step reads this tab whole anyway, so starting and loading work from
// that same read -- one read request per tool call, when reads are cached.

#[test]
fn start_appends_after_the_last_row() {
    let f = fixture();
    f.write("A2:H", (0..3).map(|_| strings(&["x"; 8])).collect());

    start(&f.journal, "abc123");

    assert_eq!(f.journal.load("abc123").unwrap().row, 5);
}

#[test]
fn start_in_an_empty_tab_begins_at_the_first_data_row() {
    let f = fixture();

    start(&f.journal, "abc123");

    assert_eq!(f.journal.load("abc123").unwrap().row, 2);
}

#[test]
fn load_finds_any_compaction_not_just_the_latest() {
    let f = fixture();
    start(&f.journal, "first");
    start(&f.journal, "second");

    assert_eq!(f.journal.load("first").unwrap().decisions, decisions());
    assert_eq!(f.journal.load("second").unwrap().row, 8); // after "first"'s 6 rows
}

#[test]
fn reads_its_tab_once_then_from_what_is_kept() {
    let f = fixture();
    start(&f.journal, "first");
    f.fake.forget_kept(&f.journal.spreadsheet_id);
    f.fake.clear_requests();

    f.journal.open_compactions().unwrap();
    f.journal.last_stamped_now().unwrap();
    f.journal.load("first").unwrap();
    f.garbage_collect();
    start(&f.journal, "second");

    let ranges: Vec<Value> = f
        .fake
        .requests()
        .into_iter()
        .filter(|r| r.path.ends_with("values:batchGetByDataFilter"))
        .flat_map(|r| r.body["dataFilters"].as_array().unwrap().clone())
        .map(|filter| filter["gridRange"].clone())
        .collect();
    let whole = json!({"sheetId": f.journal.sheet_id, "startRowIndex": 0, "startColumnIndex": 0, "endColumnIndex": 26});
    assert_eq!(ranges, vec![whole]);
}

// -- status -----------------------------------------------------------------------

#[test]
fn set_status_updates_the_compaction_row_only() {
    let f = fixture();
    start(&f.journal, "abc123");
    let mut loaded = f.journal.load("abc123").unwrap();

    {
        let _held = crate::write_lock::WRITE_LOCK.hold();
        f.journal.set_status(&mut loaded, APPLYING).unwrap();
    }

    assert_eq!(loaded.status, APPLYING);
    let reloaded = f.journal.load("abc123").unwrap();
    assert_eq!(reloaded.status, APPLYING);
    assert!(reloaded.steps.iter().all(|s| s.status == "pending"));
}

#[test]
fn writing_statuses_updates_those_rows_only() {
    let f = fixture();
    start(&f.journal, "abc123");
    let loaded = f.journal.load("abc123").unwrap();
    f.fake.clear_requests();

    {
        let _held = crate::write_lock::WRITE_LOCK.hold();
        f.journal.write_statuses(&[(loaded.steps[0].row, "done"), (loaded.steps[2].row, "done"), (loaded.row, "applied")]).unwrap();
    }

    assert_eq!(f.fake.quota_counts()[1], 1);
    let reloaded = f.journal.load("abc123").unwrap();
    let statuses: Vec<String> = reloaded.steps.into_iter().map(|s| s.status).collect();
    assert_eq!(statuses, strings(&["done", "pending", "done"]));
    assert_eq!(reloaded.status, "applied");
}

#[test]
fn only_applying_and_applied_compactions_are_open() {
    let f = fixture();
    for (compaction_id, status) in [("p", PLANNED), ("a", APPLYING), ("d", APPLIED), ("s", STAMPED)] {
        start(&f.journal, compaction_id);
        f.set_status(compaction_id, status);
    }

    assert_eq!(
        f.journal.open_compactions().unwrap(),
        vec![("a".to_string(), APPLYING.to_string()), ("d".to_string(), APPLIED.to_string())]
    );
}

#[test]
fn no_open_compactions_in_an_empty_journal() {
    let f = fixture();

    assert!(f.journal.open_compactions().unwrap().is_empty());
}

// -- garbage collection -----------------------------------------------------------

/// 1 compaction row + 2 decisions + 3 changes, per `start`.
const BLOCK_ROWS: usize = 6;

#[test]
fn garbage_collect_does_nothing_under_the_row_budget() {
    let f = fixture();
    start(&f.journal, "only");

    f.garbage_collect();

    assert_eq!(f.journal.load("only").unwrap().id, "only");
}

#[test]
fn deletes_old_terminal_blocks_but_preserves_the_last_stamped_and_open_ones() {
    let f = fixture();
    let ids: Vec<String> = (0..17).map(|i| format!("c{i}")).collect();
    for id in &ids {
        start(&f.journal, id);
        f.set_status(id, STAMPED);
    }
    // The last one is actually still open, not stamped.
    f.set_status(&ids[16], PLANNED);

    f.garbage_collect();

    // 102 rows is over the 100-row budget, so it's trimmed to 50: each
    // block is 6 rows, so the oldest 9 blocks (54 rows) are deleted -- the
    // fewest that get it there.
    for id in &ids[..9] {
        assert!(unknown(&f.journal, id), "{id} should be gone");
    }
    assert_eq!(f.journal.load(&ids[9]).unwrap().id, ids[9]); // stamped, but not needed to delete
    assert_eq!(f.journal.load(&ids[15]).unwrap().id, ids[15]); // the last *stamped* one -- preserved
    assert_eq!(f.journal.load(&ids[16]).unwrap().status, PLANNED); // still open -- preserved
    assert_eq!(f.rows().len(), 8 * BLOCK_ROWS);
    // The next compaction goes right after what's left.
    start(&f.journal, "next");
    assert_eq!(f.journal.load("next").unwrap().row, 2 + 8 * BLOCK_ROWS as i64);
}

#[test]
fn leaves_the_tab_at_least_1000_rows_long() {
    // Earlier garbage collection already shrank the tab's grid; without
    // topping it back up, it would eventually run out of rows to append
    // into.
    let f = fixture();
    f.fake.set_row_count(&f.spreadsheet, COMPACTIONS.1, 600);
    for i in 0..85 {
        let id = format!("c{i}");
        start(&f.journal, &id);
        f.set_status(&id, STAMPED);
    }

    f.garbage_collect();

    assert_eq!(f.fake.row_count(&f.spreadsheet, COMPACTIONS.1), 1000);
}

#[test]
fn deletes_old_abandoned_blocks_too() {
    let f = fixture();
    for i in 0..85 {
        let id = format!("c{i}");
        start(&f.journal, &id);
        f.set_status(&id, if i < 84 { ABANDONED } else { STAMPED });
    }

    f.garbage_collect();

    assert!(unknown(&f.journal, "c0"));
    assert_eq!(f.journal.load("c84").unwrap().status, STAMPED);
}

#[test]
fn does_nothing_if_the_oldest_block_is_not_safely_deletable() {
    // None of these are stamped or abandoned (starting leaves them
    // planned), so nothing at the top is safe to delete.
    let f = fixture();
    for i in 0..85 {
        start(&f.journal, &format!("c{i}"));
    }

    f.garbage_collect();

    assert_eq!(f.journal.load("c0").unwrap().id, "c0");
}

#[test]
fn deletes_what_it_safely_can_even_when_that_cant_clear_the_whole_excess() {
    let f = fixture();
    start(&f.journal, "old1");
    f.set_status("old1", STAMPED);
    start(&f.journal, "old2");
    f.set_status("old2", STAMPED); // the last *stamped* one
    start_with(&f.journal, "current", &[], &big_plan(500, "Event"), &[], &[]);
    // "current" is left planned (open) -- can't be deleted, and its size
    // alone already exceeds the budget. "old2" is the last *stamped*
    // compaction, so it's preserved too -- only "old1" is both deletable
    // and not needed, even though that alone can't clear the whole excess.

    f.garbage_collect();

    assert!(unknown(&f.journal, "old1"));
    assert_eq!(f.journal.load("old2").unwrap().id, "old2");
    assert_eq!(f.journal.load("current").unwrap().id, "current");
}

// -- ensure -----------------------------------------------------------------------

#[test]
fn ensure_creates_and_tags_the_tab_with_a_header_row() {
    let f = fixture();

    let tagged: Vec<Value> = f
        .fake
        .requests()
        .into_iter()
        .filter(|r| r.path.ends_with(":batchUpdate"))
        .flat_map(|r| r.body["requests"].as_array().cloned().unwrap_or_default())
        .filter_map(|request| request.get("createDeveloperMetadata").cloned())
        .collect();
    assert_eq!(tagged.len(), 1);
    let metadata = &tagged[0]["developerMetadata"];
    assert_eq!(metadata["metadataKey"], "sheet-role");
    assert_eq!(metadata["metadataValue"], COMPACTIONS.0);
    assert_eq!(metadata["location"]["sheetId"], f.journal.sheet_id);
    let header = &f.fake.rows(&f.spreadsheet, COMPACTIONS.1)[0];
    assert_eq!(header[..3], strings(&["compaction_id", "step", "kind"]));
}

#[test]
fn ensure_reuses_an_existing_tab() {
    let f = fixture();
    f.fake.clear_requests();

    let again = {
        let _held = crate::write_lock::WRITE_LOCK.hold();
        CompactionJournal::ensure(&f.sheets, &f.spreadsheet).unwrap()
    };

    assert_eq!(again.sheet_id, f.journal.sheet_id);
    let writes: Vec<String> = f.fake.requests().into_iter().filter(|r| r.method != "GET" && !r.path.contains("Get") && !r.path.contains(":search")).map(|r| r.path).collect();
    assert!(writes.is_empty(), "{writes:?}");
}
