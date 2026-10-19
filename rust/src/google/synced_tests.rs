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
