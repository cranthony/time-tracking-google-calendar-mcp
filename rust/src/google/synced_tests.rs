//! The store's tests: which changes a sync brings are the server's own.

use super::*;
use serde_json::json;
use std::time::{Duration, Instant};

/// `MARK_SYNCS` syncs begun and taken in.
fn syncs_pass(store: &mut Store) {
    for _ in 0..MARK_SYNCS {
        let sync = store.sync_begins();
        store.synced(sync);
    }
}

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

    syncs_pass(&mut store);
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

    syncs_pass(&mut store);
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
fn a_mark_lasts_through_the_syncs_begun_after_its_write_ends() {
    // A sync every MARK_LAPSES or so, begun just before the write ended,
    // and the next only after the mark's time is up: the write must still
    // be the server's own when the feed brings it then.
    let mut store = Store::default();
    let now = Instant::now();
    let walk = json!({"id": "walk"});
    store.writing(Some("walk"));
    let under_way = store.sync_begins();
    store.wrote(Some("walk"), None, now);
    store.synced(under_way);

    let later = now + MARK_LAPSES * 2;
    store.lapse(later);
    assert!(store.is_ours(&walk, later));
    let first = store.sync_begins();
    assert!(store.is_ours(&walk, later));
    store.synced(first);
    let second = store.sync_begins();
    assert!(store.is_ours(&walk, later));
    store.synced(second);

    store.lapse(later);
    assert!(!store.is_ours(&walk, later));
}

#[test]
fn a_mark_lapses_at_most_so_long_after_its_write_ends_however_few_syncs() {
    let mut store = Store::default();
    let now = Instant::now();
    store.writing(Some("walk"));
    store.wrote(Some("walk"), None, now);

    let almost = now + MARK_HOLDS_AT_MOST - Duration::from_secs(1);
    store.lapse(almost);
    assert!(store.is_ours(&json!({"id": "walk"}), almost));
    store.lapse(now + MARK_HOLDS_AT_MOST);

    assert!(store.written.is_empty());
}

#[test]
fn only_the_last_writes_etag_coming_back_ends_a_mark() {
    // Written twice; the feed, lagging, brings the first version after both.
    let mut store = Store::default();
    let now = Instant::now();
    for etag in ["\"1\"", "\"2\""] {
        store.writing(Some("walk"));
        store.wrote(Some("walk"), Some(etag), now);
    }

    store.brought(&json!({"id": "walk", "etag": "\"1\""}));
    assert!(store.is_ours(&json!({"id": "walk", "etag": "\"2\""}), now));

    store.brought(&json!({"id": "walk", "etag": "\"2\""}));
    assert!(!store.is_ours(&json!({"id": "walk", "etag": "\"3\""}), now));
}

/// Whether taking in `item` keeps it to warn of.
fn warned_of(store: &mut Store, item: Value, now: Instant) -> bool {
    store.take_in(item, false, now);
    !std::mem::take(&mut store.outside).is_empty()
}

#[test]
fn what_a_write_was_answered_with_isnt_warned_of_however_late_it_comes() {
    let mut store = Store::default();
    let now = Instant::now();
    let answer = json!({"id": "walk", "etag": "\"2\"", "updated": "2026-11-03T10:00:00Z"});
    store.writing(Some("walk"));
    store.answered(&answer, now);
    store.wrote(Some("walk"), Some("\"2\""), now);
    // Its mark gone before the feed brings it.
    let late = now + MARK_HOLDS_AT_MOST;
    store.lapse(late);
    assert!(!store.is_ours(&answer, late));

    assert!(!warned_of(&mut store, answer.clone(), late));
    // Back, it's done with: a change after it is warned of.
    assert!(store.answers.is_empty());
    assert!(warned_of(&mut store, json!({"id": "walk", "etag": "\"3\"", "updated": "2026-11-03T10:05:00Z"}), late));
}

#[test]
fn an_answer_passed_by_a_newer_version_is_done_with() {
    let mut store = Store::default();
    let now = Instant::now();
    store.answered(&json!({"id": "walk", "etag": "\"2\"", "updated": "2026-11-03T10:00:00Z"}), now);

    // An older version, lagging, isn't the answer, nor newer than it.
    store.brought(&json!({"id": "walk", "etag": "\"1\"", "updated": "2026-11-03T09:00:00Z"}));
    assert!(store.answers.contains_key("walk"));
    store.brought(&json!({"id": "walk", "etag": "\"3\"", "updated": "2026-11-03T10:05:00Z"}));
    assert!(store.answers.is_empty());
}

#[test]
fn an_event_the_server_deleted_coming_back_cancelled_isnt_warned_of() {
    let mut store = Store::default();
    let now = Instant::now();
    store.deleted("walk", now);
    store.deleted("run", now);
    let late = now + MARK_HOLDS_AT_MOST;

    assert!(!warned_of(&mut store, json!({"id": "walk", "status": "cancelled", "etag": "\"9\""}), late));
    // Nor its series' instances, cancelled with it.
    let instance = json!({"id": "run_20261103T100000Z", "recurringEventId": "run", "status": "cancelled", "etag": "\"9\""});
    assert!(!warned_of(&mut store, instance, late));
    // Brought back elsewhere, it is.
    assert!(warned_of(&mut store, json!({"id": "run", "status": "confirmed", "etag": "\"10\""}), late));
    assert!(store.answers.is_empty());
}

#[test]
fn answers_lapse_a_day_after_the_write() {
    let mut store = Store::default();
    let now = Instant::now();
    store.deleted("walk", now);

    store.lapse(now + ANSWER_LAPSES - Duration::from_secs(1));
    assert!(store.answers.contains_key("walk"));
    store.lapse(now + ANSWER_LAPSES);
    assert!(store.answers.is_empty());
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
    syncs_pass(&mut store);
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
