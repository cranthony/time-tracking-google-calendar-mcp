//! Encoding, the recorder, the Health tab and its writer, and get_health.
//! (Python's writer test of retrying to open the tab waits out
//! FLUSH_SECONDS here, so isn't ported; its HTTP timing test is with the
//! transport's tests.)

use super::*;
use crate::testing::app::test_app;

/// The recorder's clock from `start`, each reading 1234 ms on, and its
/// memory reading `rss`.
fn controlled(start: i64, rss: Option<i64>) {
    TEST_CLOCK_MS.with(|c| c.set(Some(start)));
    TEST_RSS.with(|c| c.set(Some(rss)));
}

fn recorder() -> HealthRecorder {
    controlled(1_791_000_000_000, Some(300_000));
    HealthRecorder::new()
}

fn call(at_ms: i64, work_ms: i64, total_ms: Option<i64>, ok: bool) -> CallSample {
    CallSample { at_ms, work_ms, total_ms, ok }
}

fn ids(rows: &[HealthRow]) -> Vec<String> {
    rows.iter().map(|r| r.id.as_deref().unwrap().to_string()).collect()
}

fn calls_of(recorder: &HealthRecorder, tool: &str) -> Vec<CallSample> {
    recorder.calls().into_iter().find(|(n, _)| n == tool).map(|(_, c)| c).unwrap_or_default()
}

// -- encoding ------------------------------------------------------------------------

#[test]
fn calls_round_trip() {
    let calls = vec![
        call(1_791_000_000_000, 312, Some(401), true),
        call(1_791_000_004_567, 0, None, false),
        call(1_791_000_004_567, 25_000, Some(25_000), true),
    ];

    assert_eq!(decode_calls(&encode_calls(&calls)).unwrap(), calls);
}

#[test]
fn memory_and_times_round_trip() {
    let memory: Vec<MemorySample> = (0..5).map(|i| MemorySample { at_ms: 1_791_000_000_000 + i * 997, rss_kib: 300_000 - i * 37 }).collect();
    let times = vec![1_791_000_000_000, 1_791_000_100_000];

    assert_eq!(decode_memory(&encode_memory(&memory)).unwrap(), memory);
    assert_eq!(decode_times(&encode_times(&times)).unwrap(), times);
    assert!(decode_times(&encode_times(&[])).unwrap().is_empty());
}

#[test]
fn a_hundred_calls_take_little_room() {
    let mut at = 1_791_000_000_000;
    let calls: Vec<CallSample> = (0..KEEP as i64)
        .map(|i| {
            at += 1234;
            call(at, 200 + i * 7 % 300, Some(260 + i * 7 % 300), i % 9 != 0)
        })
        .collect();

    let data = encode_calls(&calls);

    assert!(data.len() < 1000, "{}", data.len());
    assert_eq!(decode_calls(&data).unwrap(), calls);
}

#[test]
fn an_unknown_format_is_refused() {
    assert!(decode_calls("9abc").is_err());
}

// -- the recorder ----------------------------------------------------------------------

#[test]
fn keeps_each_tools_last_hundred_and_the_memory_across_them() {
    let recorder = recorder();

    for i in 0..(KEEP as i64 + 20) {
        recorder.record_call(if i % 2 == 1 { "get_notes" } else { "note" }, i, None, true);
    }

    assert_eq!(calls_of(&recorder, "get_notes").len(), 60);
    assert_eq!(calls_of(&recorder, "note").len(), 60);
    // Across tools, not per tool: a sample after each call, whichever, one
    // kept per interval (the first, its memory being no lower than later).
    let ats: Vec<i64> = calls_of(&recorder, "get_notes").into_iter().chain(calls_of(&recorder, "note")).map(|c| c.at_ms).collect();
    let mut intervals: Vec<i64> = ats.iter().map(|at| at.div_euclid(MEMORY_INTERVAL_SECONDS * 1000)).collect();
    intervals.sort();
    intervals.dedup();
    assert_eq!(recorder.memory().len(), intervals.len());
    assert_eq!(calls_of(&recorder, "note").last().unwrap().work_ms, KEEP as i64 + 18);

    recorder.record_call("note", 1, None, true);
    assert_eq!(calls_of(&recorder, "note").len(), 61);
}

#[test]
fn keeps_no_memory_where_it_cant_be_read() {
    controlled(1_791_000_000_000, None);
    let recorder = HealthRecorder::new();

    recorder.record_call("note", 5, None, true);

    assert!(recorder.memory().is_empty());
    let series: Vec<String> = recorder.take_dirty().unwrap().iter().map(|r| r.series.as_deref().unwrap().to_string()).collect();
    assert_eq!(series, vec!["tool:note"]);
}

#[test]
fn gives_what_changed_once() {
    let recorder = recorder();
    recorder.record_call("note", 5, Some(9), true);
    recorder.record_restart();

    let rows = recorder.take_dirty().unwrap();

    assert_eq!(ids(&rows), vec!["server:memory", "server:restarts", "server:tool:note"]);
    assert!(recorder.take_dirty().is_none());
    recorder.mark_dirty(&rows[..1]);
    let again = recorder.take_dirty().unwrap();
    assert_eq!(again[0].series.as_deref(), Some("memory"));
}

#[test]
fn carries_on_from_what_the_tab_kept() {
    let before = recorder();
    for i in 0..KEEP as i64 {
        before.record_call("note", i, Some(i + 3), true);
    }
    before.record_restart();
    let kept = before.take_dirty().unwrap();
    controlled(1_792_000_000_000, Some(1));
    let after = HealthRecorder::new();
    after.record_restart();
    after.record_call("note", 999, None, false);

    after.merge(&kept);

    let notes = calls_of(&after, "note");
    assert_eq!(notes.len(), KEEP);
    // The oldest go: the one recorded since is the latest.
    assert_eq!(notes[0].work_ms, 1);
    assert_eq!(*notes.last().unwrap(), call(notes.last().unwrap().at_ms, 999, None, false));
    assert_eq!(after.restarts().len(), 2);
    assert_eq!(after.memory().last().unwrap().rss_kib, 1);
}

#[test]
fn skips_a_row_it_cant_read() {
    let recorder = recorder();
    let row = |id: &str, source: &str, series: &str, data: &str| HealthRow {
        id: Some(id.into()),
        source: Some(source.into()),
        series: Some(series.into()),
        data: Some(data.into()),
        ..HealthRow::default()
    };

    recorder.merge(&[row("server:tool:note", "server", "tool:note", "1!!"), row("app:tool:x", "app", "tool:x", "whatever")]);

    assert!(recorder.calls().is_empty());
}

// -- the tab and its writer ----------------------------------------------------------------

#[test]
fn the_tab_is_written_under_its_own_lock_keeping_others_rows() {
    let t = test_app();
    let tab = t.app.health_tab().unwrap();
    let app_row = HealthRow {
        id: Some("app:tool:sync".into()),
        source: Some("app".into()),
        series: Some("tool:sync".into()),
        count: Some(1),
        data: Some("x".into()),
        ..HealthRow::default()
    };
    tab.write(&[app_row]).unwrap();
    let recorder = recorder();
    recorder.record_call("note", 5, Some(9), true);

    // No WRITE_LOCK held here: the tab takes HEALTH_TAB_LOCK itself.
    tab.write(&recorder.take_dirty().unwrap()).unwrap();

    let rows = tab.read().unwrap();
    let mut found = ids(&rows);
    found.sort();
    assert_eq!(found, vec!["app:tool:sync", "server:memory", "server:tool:note"]);
    let data = |id: &str| rows.iter().find(|r| r.id.as_deref() == Some(id)).and_then(|r| r.data.as_deref()).unwrap().to_string();
    assert_eq!(data("app:tool:sync"), "x");
    assert_eq!(decode_calls(&data("server:tool:note")).unwrap()[0].total_ms, Some(9));
}

#[test]
fn opening_the_tab_gives_a_spreadsheet_under_another_title_its_own() {
    let t = test_app();
    let spreadsheet = t.spreadsheet();
    {
        let (_, sheets) = t.app.clients().unwrap();
        let _held = crate::write_lock::WRITE_LOCK.hold();
        sheets.set_spreadsheet_title(&spreadsheet, "Calendar Metadata").unwrap();
    }

    t.app.health_tab().unwrap();

    assert_eq!(t.fake.spreadsheet_title(&spreadsheet), crate::sheet::SPREADSHEET_TITLE);
}

#[test]
fn nothing_else_writes_without_a_lock() {
    let t = test_app();
    let (_, sheets) = t.app.clients().unwrap();

    assert!(sheets.add_sheet(&t.spreadsheet(), "Anything", None).is_err());
}

fn health_rows(t: &crate::testing::app::TestApp) -> Vec<HealthRow> {
    t.app.health_tab().unwrap().read().unwrap()
}

#[test]
fn the_writer_takes_in_what_was_kept_records_the_start_and_writes_what_changes() {
    let t = test_app();
    controlled(1_790_000_000_000, Some(1));
    let earlier = HealthRecorder::new();
    earlier.record_restart();
    t.app.health_tab().unwrap().write(&earlier.take_dirty().unwrap()).unwrap();
    let requests = || t.fake.requests().iter().filter(|r| r.path.ends_with("values:batchUpdateByDataFilter")).count();

    let writer = HealthWriter::start(t.app.clone());
    writer.wait_loaded(10);
    writer.flush();

    assert_eq!(t.app.health.restarts().len(), 2);
    let restarts = health_rows(&t).into_iter().find(|r| r.id.as_deref() == Some("server:restarts")).unwrap();
    assert_eq!(decode_times(restarts.data.as_deref().unwrap()).unwrap().len(), 2);
    t.app.health.record_call("note", 5, None, true);
    writer.flush();
    assert!(ids(&health_rows(&t)).contains(&"server:tool:note".to_string()));
    let written = requests();
    writer.flush();
    assert_eq!(requests(), written); // Nothing changed.
    writer.stop();
}

#[test]
fn the_writer_writes_again_what_it_couldnt() {
    let t = test_app();
    let writer = HealthWriter::start(t.app.clone());
    writer.wait_loaded(10);
    writer.flush();
    t.fake.with(|s| s.fail_next.push(("POST".into(), "values:batchUpdateByDataFilter".into(), 500, "offline".into())));
    t.app.health.record_call("note", 5, None, true);

    writer.flush();
    writer.stop();

    assert!(ids(&health_rows(&t)).contains(&"server:tool:note".to_string()));
}

// -- get_health ------------------------------------------------------------------------------

#[test]
fn get_health_reports_each_tools_calls_the_memory_and_restarts() {
    let t = test_app();
    controlled(1_791_000_000_000, Some(300_000));
    t.app.health.record_restart();
    for i in 0..10 {
        t.app.health.record_call("note", 100 + i, Some(150 + i), i != 3);
    }

    let report = crate::tools::run(&t.app, "get_health", json!({"tool_name": "note"})).ok().unwrap().0;

    let note = &report["tools"][0];
    assert_eq!((note["count"].clone(), note["errors"].clone()), (json!(10), json!(1)));
    assert_eq!((note["work"]["median_ms"].clone(), note["work"]["max_ms"].clone(), note["total"]["p95_ms"].clone()), (json!(104), json!(109), json!(159)));
    assert_eq!(note["calls"][0]["overhead_ms"], 50);
    assert!(note["calls"][0]["at"].as_str().unwrap().ends_with('Z'));
    // Ten calls 1.234 s apart, in one interval: one sample.
    assert_eq!(report["memory"].as_array().unwrap().len(), 1);
    assert_eq!(report["memory_interval_seconds"], json!(MEMORY_INTERVAL_SECONDS));
    assert_eq!(report["memory_latest_mib"], json!(round1(300_000.0 / 1024.0)));
    assert_eq!(report["restarts"].as_array().unwrap().len(), 1);
    assert_eq!(report["auth"], json!(crate::auth_mode::name()));
    let summary = crate::tools::run(&t.app, "get_health", json!({"samples": false})).ok().unwrap().0;
    assert!(summary["tools"][0]["calls"].is_null());
}

#[test]
fn get_health_of_a_tool_never_called_says_which_were() {
    let t = test_app();
    controlled(1_791_000_000_000, None);
    t.app.health.record_call("note", 1, None, true);

    let Err(crate::tools::ToolError::Message(text)) = crate::tools::run(&t.app, "get_health", json!({"tool_name": "nothing"})) else { panic!("not refused") };

    assert!(text.contains("No calls of 'nothing' are recorded; those that are: note"), "{text}");
}

// -- spreadsheet warnings ----------------------------------------------------------------------

fn warning(at_ms: i64, tab: &str) -> SheetWarning {
    SheetWarning { at_ms, kind: "edited".into(), tab: Some(tab.into()), cells: Vec::new(), cell_count: 1, restored: true }
}

#[test]
fn a_refresh_becomes_a_warning_per_tab_naming_its_first_cells() {
    use crate::google::kept::Differs;
    use crate::google::sheets::{ChangedTab, DeletedTab, Refreshed};
    let cells: Vec<Differs> = (0..25).map(|row| Differs { row, column: 1, found: format!("x{row}"), kept: String::new() }).collect();
    let refreshed = Refreshed {
        changed: vec![ChangedTab { sheet_id: 1, title: Some("People".into()), cells, restored: true }],
        deleted: vec![DeletedTab { sheet_id: 2, title: None, restored: false }],
        deferred: false,
    };

    let warnings = SheetWarning::of(&refreshed, 5);

    assert_eq!(warnings.len(), 2);
    assert_eq!((warnings[0].kind.as_str(), warnings[0].cells.len(), warnings[0].cell_count), ("edited", WARNING_CELLS, 25));
    assert_eq!(warnings[0].cells[2], WarnedCell { cell: "B3".into(), found: "x2".into(), kept: String::new() });
    assert_eq!((warnings[1].kind.as_str(), warnings[1].tab.clone(), warnings[1].restored), ("deleted", None, false));
}

#[test]
fn warnings_are_written_to_the_tab_and_taken_in_again_after_a_restart() {
    let before = recorder();
    before.record_sheet_warnings(vec![warning(10, "People"), warning(20, "Actions")]);
    let rows = before.take_dirty().unwrap();
    assert_eq!(ids(&rows), vec!["server:sheet-warnings"]);
    assert_eq!(rows[0].count, Some(2));

    let after = recorder();
    after.record_sheet_warnings(vec![warning(30, "Locations")]);
    after.merge(&rows);

    let tabs: Vec<Option<String>> = after.sheet_warnings().into_iter().map(|w| w.tab).collect();
    assert_eq!(tabs, vec![Some("People".into()), Some("Actions".into()), Some("Locations".into())]);
}

#[test]
fn only_warnings_from_the_last_week_are_recent() {
    let recorder = recorder();
    let day = 24 * 60 * 60 * 1000;
    let now = 100 * day;
    recorder.record_sheet_warnings(vec![warning(now - 8 * day, "Old"), warning(now - 2 * day, "A"), warning(now - day, "B")]);

    assert_eq!(recorder.recent_sheet_warnings(now), (2, Some(now - day)));
    assert_eq!(recorder.recent_sheet_warnings(now + 7 * day), (0, None));
}

// -- memory by the second, and what made it up ----------------------------------------------

/// A recorder whose clock reads `times` (ms) in turn, its memory `rss`
/// KiB each time, and whose parts are `parts`.
fn sampled(samples: &[(i64, i64)], parts: MemoryParts) -> HealthRecorder {
    let recorder = HealthRecorder::new();
    recorder.set_parts_source(Arc::new(move || parts.clone()));
    for (at, rss) in samples {
        // The tests' clock reads 1234 ms on from what it's set to.
        TEST_CLOCK_MS.with(|c| c.set(Some(at - 1234)));
        TEST_RSS.with(|c| c.set(Some(Some(*rss))));
        recorder.sample_memory();
    }
    recorder
}

#[test]
fn memory_is_kept_per_interval_at_its_highest_in_each() {
    // Times within the 10th, 11th and 12th intervals.
    let at = |interval: i64, ms: i64| interval * MEMORY_INTERVAL_SECONDS * 1000 + ms;
    let recorder = sampled(&[(at(10, 100), 500), (at(10, 400), 700), (at(10, 900), 600), (at(11, 0), 400), (at(12, 500), 450)], vec![("Calendar".into(), 30)]);

    let kept: Vec<(i64, i64)> = recorder.memory().iter().map(|m| (m.at_ms, m.rss_kib)).collect();

    assert_eq!(kept, vec![(at(10, 400), 700), (at(11, 0), 400), (at(12, 500), 450)]);
    assert_eq!(recorder.memory_parts().keys().copied().collect::<Vec<_>>(), vec![at(10, 400), at(11, 0), at(12, 500)]);
}

#[test]
fn the_interval_is_a_minute() {
    assert_eq!(MEMORY_INTERVAL_SECONDS, 60);
}

#[test]
fn the_last_five_hundred_intervals_are_kept() {
    let interval = MEMORY_INTERVAL_SECONDS * 1000;
    let samples: Vec<(i64, i64)> = (0..(MEMORY_KEEP as i64 + 7)).map(|i| (i * interval, 100 + i)).collect();

    let recorder = sampled(&samples, vec![("Sheet: Actions".into(), 2)]);

    assert_eq!(recorder.memory().len(), MEMORY_KEEP);
    assert_eq!(recorder.memory()[0].at_ms, 7 * interval);
    assert_eq!(recorder.memory_parts().len(), MEMORY_KEEP);
}

#[test]
fn parts_are_written_to_the_tab_and_taken_in_again_after_a_restart() {
    let before = sampled(&[(5_000, 900), (6_000, 950)], vec![("Calendar".into(), 120), ("Sheet: Notes".into(), 3)]);
    let rows = before.take_dirty().unwrap();
    assert_eq!(ids(&rows), vec!["server:memory", "server:memory-parts"]);

    let after = HealthRecorder::new();
    after.merge(&rows);

    assert_eq!(after.memory_parts(), before.memory_parts());
    assert_eq!(after.memory_parts()[&6_000], vec![("Calendar".to_string(), 120), ("Sheet: Notes".to_string(), 3)]);
}

#[test]
fn parts_encode_compactly_with_a_part_missing_from_some_samples() {
    let parts: Vec<(i64, MemoryParts)> = vec![(1_000, vec![("Calendar".into(), 10)]), (2_000, vec![("Calendar".into(), 12), ("Sheet: People".into(), 4)])];

    assert_eq!(decode_parts(&encode_parts(&parts)).unwrap(), parts);
    // Five hundred samples of twenty parts fit a cell with room to spare.
    let many: Vec<(i64, MemoryParts)> = (0..500).map(|i| (i * 1000, (0..20).map(|p| (format!("Sheet: Tab {p}"), 100 + (i * p) % 37)).collect())).collect();
    assert!(encode_parts(&many).len() < 25_000, "{}", encode_parts(&many).len());
}

#[test]
fn get_health_gives_each_samples_parts_and_the_rest_of_the_server() {
    let t = test_app();
    TEST_CLOCK_MS.with(|c| c.set(Some(1_791_000_000_000)));
    TEST_RSS.with(|c| c.set(Some(Some(10 * 1024))));
    t.app.health.set_parts_source(Arc::new(|| vec![("Calendar".into(), 2048), ("Sheet: Actions".into(), 1024)]));
    t.app.health.sample_memory();

    let Ok(report) = crate::tools::run(&t.app, "get_health", json!({})).map(|o| o.0) else { panic!("get_health failed") };

    assert_eq!(
        report["memory"][0]["parts"],
        json!([{"name": "Calendar", "mib": 2.0}, {"name": "Sheet: Actions", "mib": 1.0}, {"name": "Rest of server", "mib": 7.0}])
    );
}

#[test]
fn get_health_splits_the_heap_and_gives_the_thread_count() {
    let t = test_app();
    TEST_CLOCK_MS.with(|c| c.set(Some(1_791_000_000_000)));
    TEST_RSS.with(|c| c.set(Some(Some(10 * 1024))));
    TEST_HEAP.with(|c| c.set(Some(Heap { in_use_kib: 5 * 1024, free_kib: 2 * 1024 })));
    TEST_THREADS.with(|c| c.set(Some(17)));
    t.app.health.set_parts_source(Arc::new(|| vec![("Calendar".into(), 2048), ("Sheet: Actions".into(), 1024)]));
    t.app.health.sample_memory();

    let Ok(report) = crate::tools::run(&t.app, "get_health", json!({})).map(|o| o.0) else { panic!("get_health failed") };

    assert_eq!(
        report["memory"][0]["parts"],
        json!([
            {"name": "Calendar", "mib": 2.0},
            {"name": "Sheet: Actions", "mib": 1.0},
            {"name": "Heap in use (untracked)", "mib": 2.0},
            {"name": "Allocator: free but held", "mib": 2.0},
            {"name": "Rest of server", "mib": 3.0},
        ])
    );
    assert_eq!(report["memory"][0]["threads"], json!(17));
}

#[test]
fn the_heap_in_use_beyond_the_parts_kept_is_never_less_than_none() {
    TEST_HEAP.with(|c| c.set(Some(Heap { in_use_kib: 1000, free_kib: 300 })));
    TEST_THREADS.with(|c| c.set(None));

    let parts = process_parts(&vec![("Calendar".into(), 1200)]);

    assert_eq!(parts, vec![(HEAP_UNTRACKED.to_string(), 0), (ALLOCATOR_FREE.to_string(), 300)]);
}

#[test]
fn without_the_allocator_or_threads_there_are_no_process_parts() {
    TEST_HEAP.with(|c| c.set(None));
    TEST_THREADS.with(|c| c.set(None));

    assert_eq!(process_parts(&vec![("Calendar".into(), 1200)]), Vec::new());
}

#[test]
fn the_thread_count_is_kept_across_restarts_with_the_parts() {
    let before = recorder();
    TEST_HEAP.with(|c| c.set(Some(Heap { in_use_kib: 4096, free_kib: 1024 })));
    TEST_THREADS.with(|c| c.set(Some(23)));
    before.sample_memory();

    let after = HealthRecorder::new();
    after.merge(&before.take_dirty().unwrap());

    let parts = after.memory_parts().into_values().next().unwrap();
    assert!(parts.contains(&(THREADS.to_string(), 23)), "{parts:?}");
    assert!(parts.contains(&(ALLOCATOR_FREE.to_string(), 1024)), "{parts:?}");
}

#[cfg(all(target_os = "linux", target_env = "gnu"))]
#[test]
fn glibc_and_proc_give_the_heap_and_the_thread_count() {
    let kept: Vec<u8> = vec![1; 4 << 20];
    let heap = glibc_heap().expect("mallinfo2");
    assert!(heap.in_use_kib >= 4096 && heap.free_kib >= 0, "{heap:?}");
    assert!(status_field("Threads:").unwrap() >= 1);
    drop(kept);
}

#[test]
fn the_app_measures_each_kept_tab_and_the_kept_calendar() {
    let t = test_app();
    t.app.actions().unwrap().all().unwrap();
    t.app.calendar_client().unwrap().list_events(&crate::testing::time_at("00:00"), &crate::testing::time_at("23:00")).unwrap();

    let parts = t.app.memory_parts();

    let names: Vec<&str> = parts.iter().map(|(n, _)| n.as_str()).collect();
    assert!(names.contains(&"Sheet: Actions") && names.contains(&"Calendar"), "{names:?}");
    assert!(parts.iter().all(|(_, kib)| *kib >= 0));
}
