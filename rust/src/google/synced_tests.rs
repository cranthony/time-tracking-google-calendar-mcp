//! The store's tests: which changes a sync brings are the server's own.

use super::*;
use serde_json::json;

#[test]
fn a_write_makes_it_stale_and_its_event_and_series_the_servers_own() {
    let mut store = Store::default();

    store.writing(Some("run_20261031T110000Z"));

    assert!(store.stale && store.generation == 1);
    for ours in [
        json!({"id": "run_20261031T110000Z"}),
        json!({"id": "run"}),
        json!({"id": "run_20261101T110000Z", "recurringEventId": "run"}),
        json!({"id": "run_20261101T110000Z", "recurringEventId": "run_R20261101T110000"}),
    ] {
        assert!(store.is_ours(&ours), "{ours}");
    }
    assert!(!store.is_ours(&json!({"id": "walk"})));
    assert!(!store.is_ours(&json!({"id": "walk_20261031T110000Z", "recurringEventId": "walk"})));
}

#[test]
fn a_write_to_anything_makes_every_change_the_servers_own() {
    let mut store = Store::default();

    store.writing(None);

    assert!(store.is_ours(&json!({"id": "walk"})));
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
