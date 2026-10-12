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

fn ids(rows: &[Row]) -> Vec<String> {
    rows.iter().map(|r| rows::text(r, "id").unwrap().to_string()).collect()
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
    // Across tools, not per tool: the last hundred calls, whichever.
    assert_eq!(recorder.memory().len(), KEEP);
    assert_eq!(recorder.memory().last().unwrap().at_ms, calls_of(&recorder, "get_notes").last().unwrap().at_ms);
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
    let series: Vec<String> = recorder.take_dirty().unwrap().iter().map(|r| rows::text(r, "series").unwrap().to_string()).collect();
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
    assert_eq!(rows::text(&again[0], "series"), Some("memory"));
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
    let row = |id: &str, source: &str, series: &str, data: &str| {
        let mut row = HEALTH_ROW.empty();
        for (k, v) in [("id", id), ("source", source), ("series", series), ("data", data)] {
            row.insert(k.into(), json!(v));
        }
        row
    };

    recorder.merge(&[row("server:tool:note", "server", "tool:note", "1!!"), row("app:tool:x", "app", "tool:x", "whatever")]);

    assert!(recorder.calls().is_empty());
}

// -- the tab and its writer ----------------------------------------------------------------

#[test]
fn the_tab_is_written_under_its_own_lock_keeping_others_rows() {
    let t = test_app();
    let tab = t.app.health_tab().unwrap();
    let mut app_row = HEALTH_ROW.empty();
    for (k, v) in [("id", json!("app:tool:sync")), ("source", json!("app")), ("series", json!("tool:sync")), ("count", json!(1)), ("data", json!("x"))] {
        app_row.insert(k.into(), v);
    }
    tab.write(&[app_row]).unwrap();
    let recorder = recorder();
    recorder.record_call("note", 5, Some(9), true);

    // No WRITE_LOCK held here: the tab takes HEALTH_TAB_LOCK itself.
    tab.write(&recorder.take_dirty().unwrap()).unwrap();

    let rows = tab.read().unwrap();
    let mut found = ids(&rows);
    found.sort();
    assert_eq!(found, vec!["app:tool:sync", "server:memory", "server:tool:note"]);
    let data = |id: &str| rows.iter().find(|r| rows::text(r, "id") == Some(id)).and_then(|r| rows::text(r, "data")).unwrap().to_string();
    assert_eq!(data("app:tool:sync"), "x");
    assert_eq!(decode_calls(&data("server:tool:note")).unwrap()[0].total_ms, Some(9));
}

#[test]
fn nothing_else_writes_without_a_lock() {
    let t = test_app();
    let (_, sheets) = t.app.clients().unwrap();

    assert!(sheets.add_sheet(&t.spreadsheet(), "Anything", None).is_err());
}

fn health_rows(t: &crate::testing::app::TestApp) -> Vec<Row> {
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
    let restarts = health_rows(&t).into_iter().find(|r| rows::text(r, "id") == Some("server:restarts")).unwrap();
    assert_eq!(decode_times(rows::text(&restarts, "data").unwrap()).unwrap().len(), 2);
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
    assert_eq!(report["memory"].as_array().unwrap().len(), 10);
    assert_eq!(report["memory_latest_mib"], json!(round1(300_000.0 / 1024.0)));
    assert_eq!(report["restarts"].as_array().unwrap().len(), 1);
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
