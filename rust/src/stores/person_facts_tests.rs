//! The module's tests, against the fake Google.

use super::*;
use crate::sheet::{PEOPLE, TRAITS};
use crate::testing::app::test_app;
use crate::write_lock::WRITE_LOCK;
use serde_json::json;

fn at(text: &str) -> Moment {
    chrono::DateTime::parse_from_rfc3339(text).unwrap()
}

fn change(person: &str, trait_id: &str, fact: &str, summary: &str) -> SummaryChange {
    SummaryChange { person_id: person.into(), trait_id: trait_id.into(), fact_id: fact.into(), summary: summary.into() }
}

fn thoughtful() -> serde_json::Value {
    json!({"id": "thoughtful", "name": "Thoughtful", "status": "active", "parts": [
        {"id": "judgment", "kind": "judgment", "rubric": "Did it show I remembered?", "ratings": {"0": "No", "1": "Yes"},
         "facts": ["action", "person_notes", "what_matters"]}
    ]})
}

#[test]
fn what_matters_moves_into_each_trait_that_judged_from_it() {
    let t = test_app();
    t.app.traits().unwrap();
    // The People tab as it was, with what_matters.
    let spreadsheet = t.spreadsheet();
    {
        let _held = WRITE_LOCK.hold();
        RowSheet::ensure(&t.app.clients().unwrap().1, &spreadsheet, PEOPLE.0, PEOPLE.1, KEPT_WHAT_MATTERS_ROW, &["id"]).unwrap();
    }
    t.seed(TRAITS.1, &[thoughtful()]);
    t.seed(PEOPLE.1, &[json!({"id": "sam", "what_matters": "- starts a new job"}), json!({"id": "kim"})]);
    let _held = WRITE_LOCK.hold();
    let store = t.app.person_facts().unwrap();
    assert_eq!(
        store.by_person().unwrap(),
        vec![("sam".to_string(), vec![("thoughtful".to_string(), "what_matters".to_string(), "- starts a new job".to_string())])]
    );
    // The trait reads as judging from its fact, and person_notes is gone.
    let traits = t.app.traits().unwrap().all().unwrap();
    assert_eq!(traits[0].parts.as_ref().unwrap()[0]["facts"], json!(["action", "fact:what_matters"]));
    assert_eq!(traits[0].fact_ids(), vec!["what_matters"]);
    assert!(crate::stores::traits::trait_problems(&traits[0]).is_empty());
}

#[test]
fn summaries_are_set_kept_and_cleared() {
    let t = test_app();
    let _held = WRITE_LOCK.hold();
    let store = t.app.person_facts().unwrap();
    let traits = t.app.traits().unwrap().all().unwrap();
    let now = at("2026-10-19T10:00:00+00:00");
    assert_eq!(store.set_summaries(&[change("sam", "thoughtful", "what_matters", " tea ")], &traits, "user", now).unwrap(), 1);
    assert_eq!(store.summary("sam", "thoughtful", "what_matters").unwrap().as_deref(), Some("tea"));
    // The same again changes nothing.
    assert_eq!(store.set_summaries(&[change("sam", "thoughtful", "what_matters", "tea")], &traits, "user", now).unwrap(), 0);
    assert_eq!(store.set_summaries(&[change("sam", "thoughtful", "what_matters", "")], &traits, "user", now).unwrap(), 1);
    assert_eq!(store.summary("sam", "thoughtful", "what_matters").unwrap(), None);
    assert!(store.by_person().unwrap().is_empty());

    let refused = store
        .set_summaries(&[change("sam", "thoughtful", "hobbies", "x"), change("sam", "nope", "x", "y")], &traits, "user", now)
        .unwrap_err()
        .to_string();
    assert!(refused.contains("'hobbies' isn't one of thoughtful's facts (what_matters)"), "{refused}");
    assert!(refused.contains("'nope' isn't a trait"), "{refused}");
    let long = "x".repeat(MAX_SUMMARY_CHARS + 1);
    assert!(store.set_summaries(&[change("sam", "thoughtful", "what_matters", &long)], &traits, "user", now).is_err());
}

#[test]
fn logs_are_indexed_by_event_and_listed_newest_first() {
    let t = test_app();
    let _held = WRITE_LOCK.hold();
    let store = t.app.person_facts().unwrap();
    let now = at("2026-10-19T10:00:00+00:00");
    let log = |person: &str, key: &str, text: &str| (person.to_string(), key.to_string(), text.to_string());
    store
        .record_logs("e1", Some(at("2026-10-17T18:00:00+00:00")), Some("Dinner"), &[log("sam", "thoughtful/what_matters", "new job")], "compaction p", now)
        .unwrap();
    store
        .record_logs(
            "e2",
            Some(at("2026-10-18T18:00:00+00:00")),
            Some("Walk"),
            &[log("sam", "thoughtful/what_matters", "moving"), log("kim", "thoughtful/what_matters", "a dog")],
            "compaction p",
            now,
        )
        .unwrap();
    let logs: Vec<(Option<String>, Option<String>)> = store.logs("sam", None, None).unwrap().into_iter().map(|l| (l.event_id, l.log)).collect();
    assert_eq!(logs, vec![(Some("e2".into()), Some("moving".into())), (Some("e1".into()), Some("new job".into()))]);
    assert!(store.logs("sam", Some("thoughtful"), Some("other")).unwrap().is_empty());

    // An event's logs again replace its own, and only its own.
    store.record_logs("e2", Some(at("2026-10-18T18:00:00+00:00")), Some("Walk"), &[log("kim", "thoughtful/what_matters", "two dogs")], "compaction q", now).unwrap();
    assert_eq!(store.logs("sam", None, None).unwrap().len(), 1);
    assert_eq!(store.logs("kim", None, None).unwrap()[0].log.as_deref(), Some("two dogs"));
    assert_eq!(store.all_logs().unwrap().len(), 2);
}
