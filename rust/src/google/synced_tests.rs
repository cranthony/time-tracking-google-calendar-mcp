//! The store's tests: which changes a sync brings are the server's own.

use super::*;
use serde_json::json;
use std::time::{Duration, Instant};

#[test]
fn a_write_makes_it_stale_and_its_event_and_series_the_servers_own() {
    let mut store = Store::default();

    let now = Instant::now();
    store.writing(Some("run_20261031T110000Z"));

    assert!(store.stale && store.generation == 1);
    for ours in [
        json!({"id": "run_20261031T110000Z"}),
        json!({"id": "run"}),
        json!({"id": "run_20261101T110000Z", "recurringEventId": "run"}),
        json!({"id": "run_20261101T110000Z", "recurringEventId": "run_R20261101T110000"}),
    ] {
        assert!(store.is_ours(&ours, now), "{ours}");
    }
    assert!(!store.is_ours(&json!({"id": "walk"}), now));
    assert!(!store.is_ours(&json!({"id": "walk_20261031T110000Z", "recurringEventId": "walk"}), now));
}

#[test]
fn a_write_to_anything_makes_every_change_the_servers_own() {
    let mut store = Store::default();

    store.writing(None);

    assert!(store.is_ours(&json!({"id": "walk"}), Instant::now()));
}

#[test]
fn a_write_stays_the_servers_own_until_a_sync_brings_back_its_etag() {
    let mut store = Store::default();
    let now = Instant::now();
    store.writing(Some("walk"));
    store.wrote(Some("walk"), Some("\"2\""), now);

    // A sync that doesn't bring it (Calendar's feed lagging) leaves it marked.
    store.lapse(now);
    store.brought(&json!({"id": "run", "etag": "\"3\""}));
    assert!(store.is_ours(&json!({"id": "walk", "etag": "\"2\""}), now));
    // An older version of it isn't the write coming back.
    store.brought(&json!({"id": "walk", "etag": "\"1\""}));
    assert!(store.is_ours(&json!({"id": "walk", "etag": "\"2\""}), now));

    store.brought(&json!({"id": "walk", "etag": "\"2\""}));

    assert!(!store.is_ours(&json!({"id": "walk", "etag": "\"4\""}), now));
}

#[test]
fn a_mark_never_matched_lapses_after_its_write_ends() {
    let mut store = Store::default();
    let now = Instant::now();
    store.writing(Some("walk"));
    store.writing(None);
    // Under way, it holds however long it takes.
    assert!(store.is_ours(&json!({"id": "walk"}), now + MARK_LAPSES * 2));
    store.wrote(Some("walk"), None, now);
    store.wrote(None, None, now);

    let almost = now + MARK_LAPSES - Duration::from_secs(1);
    store.lapse(almost);
    assert!(store.is_ours(&json!({"id": "walk"}), almost));
    assert!(store.is_ours(&json!({"id": "run"}), almost));

    store.lapse(now + MARK_LAPSES);

    assert!(store.written.is_empty() && store.written_all.is_none());
    assert!(!store.is_ours(&json!({"id": "walk"}), now + MARK_LAPSES));
}

#[test]
fn a_series_write_coming_back_keeps_the_instances_it_changed_the_servers_own() {
    let mut store = Store::default();
    let now = Instant::now();
    store.writing(Some("run"));
    store.wrote(Some("run"), Some("\"2\""), now);

    // Ending the series cancels its edited instance after the end, with
    // the series' own etag, listed after it.
    let series = json!({"id": "run", "etag": "\"2\"", "recurrence": ["RRULE:FREQ=WEEKLY;UNTIL=20261020T215959Z"]});
    assert!(store.is_ours(&series, now));
    store.brought(&series);
    let instance = json!({"id": "run_20261020T220000Z", "recurringEventId": "run", "status": "cancelled", "etag": "\"2\""});
    assert!(store.is_ours(&instance, now));

    store.lapse(now + MARK_LAPSES);

    assert!(!store.is_ours(&instance, now + MARK_LAPSES));
}

#[test]
fn a_write_under_way_keeps_its_event_marked_though_an_earlier_one_comes_back() {
    let mut store = Store::default();
    let now = Instant::now();
    store.writing(Some("walk"));
    store.wrote(Some("walk"), Some("\"2\""), now);
    store.writing(Some("walk"));

    store.brought(&json!({"id": "walk", "etag": "\"2\""}));

    assert!(store.is_ours(&json!({"id": "walk", "etag": "\"3\""}), now));
}

#[test]
fn forgetting_starts_afresh_but_keeps_counting_writes() {
    let root = format!("synced-test-{}", uuid::Uuid::new_v4());
    with(&root, "cal", |store| {
        store.token = Some("7".into());
        store.events.insert("e".into(), json!({"id": "e"}));
        store.writing(Some("e"));
    });

    forget(&root, "cal");

    with(&root, "cal", |store| {
        assert!(store.token.is_none() && store.events.is_empty() && !store.stale);
        assert_eq!(store.generation, 2);
    });
}

#[test]
fn an_older_version_doesnt_replace_a_newer_one_kept() {
    let mut store = Store::default();
    store.keep(json!({"id": "walk", "etag": "\"2\"", "updated": "2026-10-19T01:29:50.614Z"}));

    store.keep(json!({"id": "walk", "etag": "\"1\"", "updated": "2026-10-19T01:20:00.000Z"}));
    assert_eq!(store.events["walk"]["etag"], json!("\"2\""));

    store.keep(json!({"id": "walk", "etag": "\"3\"", "updated": "2026-10-19T01:30:23.276Z"}));
    assert_eq!(store.events["walk"]["etag"], json!("\"3\""));
    // One without `updated` can't be ordered: it's taken.
    store.keep(json!({"id": "walk", "etag": "\"4\""}));
    assert_eq!(store.events["walk"]["etag"], json!("\"4\""));
}

#[test]
fn a_full_listing_drops_what_it_doesnt_list_but_what_the_server_just_wrote() {
    let mut store = Store::default();
    let now = Instant::now();
    for id in ["walk", "run", "made"] {
        store.keep(json!({"id": id}));
    }
    store.writing(Some("made"));
    store.wrote(Some("made"), Some("\"1\""), now);

    store.keep_only(&["walk".to_string()].into(), now);

    assert_eq!(store.events.keys().collect::<Vec<_>>(), ["made", "walk"]);
    store.keep_only(&["walk".to_string()].into(), now + MARK_LAPSES);
    assert_eq!(store.events.keys().collect::<Vec<_>>(), ["walk"]);
}

fn at(text: &str) -> chrono::DateTime<chrono::Utc> {
    chrono::DateTime::parse_from_rfc3339(text).unwrap().with_timezone(&chrono::Utc)
}

fn timed(id: &str, start: &str, end: &str) -> Value {
    json!({"id": id, "start": {"dateTime": start}, "end": {"dateTime": end}, "updated": "2026-01-01T00:00:00Z"})
}

fn having(mut event: Value, key: &str, value: Value) -> Value {
    event[key] = value;
    event
}

#[test]
fn keeps_only_what_ends_after_where_its_kept_from_and_every_series() {
    let mut store = Store::default();
    store.start_at(at("2026-01-10T00:00:00Z"));

    store.keep(timed("old", "2026-01-01T09:00:00Z", "2026-01-01T10:00:00Z"));
    store.keep(timed("across", "2026-01-08T09:00:00Z", "2026-01-10T10:00:00Z"));
    store.keep(timed("new", "2026-01-11T09:00:00Z", "2026-01-11T10:00:00Z"));
    store.keep(having(timed("series", "2025-01-01T09:00:00Z", "2025-01-01T10:00:00Z"), "recurrence", json!(["RRULE:FREQ=DAILY"])));
    // Moved back from when it was to have been: its series would show it there.
    store.keep(having(timed("moved", "2025-12-01T09:00:00Z", "2025-12-01T10:00:00Z"), "originalStartTime", json!({"dateTime": "2026-01-12T09:00:00Z"})));

    let kept: Vec<&str> = store.events.keys().map(String::as_str).collect();
    assert_eq!(kept, vec!["across", "moved", "new", "series"]);
}

#[test]
fn starting_later_drops_what_falls_out_and_never_starts_earlier() {
    let mut store = Store::default();
    store.start_at(at("2026-01-01T00:00:00Z"));
    store.keep(timed("first", "2026-01-02T09:00:00Z", "2026-01-02T10:00:00Z"));
    store.keep(timed("second", "2026-02-02T09:00:00Z", "2026-02-02T10:00:00Z"));

    store.start_at(at("2026-02-01T00:00:00Z"));
    store.start_at(at("2026-01-01T00:00:00Z"));

    assert_eq!(store.from, Some(at("2026-02-01T00:00:00Z")));
    assert_eq!(store.events.keys().collect::<Vec<_>>(), vec!["second"]);
}
