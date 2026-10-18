//! The module's tests, against the fake Google.

use super::*;
use crate::sheet::rows::{Row, RowItem};
use crate::facts::Facts;
use crate::sheet::{ACTIONS, ACTION_GROUPS, HABITS, PEOPLE, TRAITS};
use crate::testing::app::{test_app, TestApp};
use crate::write_lock::WRITE_LOCK;

fn at() -> Moment {
    chrono::DateTime::parse_from_rfc3339("2026-09-02T10:00:00+00:00").unwrap()
}

fn strings(items: &[&str]) -> Vec<String> {
    crate::testing::strings(items)
}

struct Fixture {
    t: TestApp,
    store: Cancellations,
}

/// The store, with one trait, Reliable, of `parts`.
fn fixture(parts: Value, people: Vec<Value>) -> Fixture {
    let t = test_app();
    let store = t.app.cancellations().unwrap();
    t.seed(ACTIONS.1, &[
        json!({"id": "gym", "name": "Work out", "status": "active", "label_id": "label-gym", "group_id": "body"}),
        json!({"id": "eat", "name": "Eat", "status": "active", "label_id": "label-eat"}),
    ]);
    t.seed(ACTION_GROUPS.1, &[json!({"id": "body", "name": "Body"})]);
    t.seed(PEOPLE.1, &people);
    t.seed(TRAITS.1, &[json!({"id": "reliable", "name": "Reliable", "status": "active", "parts": parts})]);
    Fixture { t, store }
}

fn sam() -> Vec<Value> {
    vec![json!({"id": "sam", "name": "Sam", "status": "active"})]
}

fn event(id: &str, actions: &[&str], with: &[&str], for_: &[&str]) -> Event {
    let list = |ids: &[&str]| if ids.is_empty() { None } else { Some(json!(ids)) };
    Event {
        id: Some(id.into()),
        summary: Some("Gym".into()),
        start: Some(at()),
        end: Some(at() + chrono::Duration::hours(1)),
        action_ids: Some(crate::testing::shares(actions)),
        facts: Some(Facts { with_ids: list(with), for_ids: list(for_), ..Default::default() }),
        ..Default::default()
    }
}

impl Fixture {
    fn matches(&self, given: &Event) -> Vec<(String, String, String, Vec<String>, Vec<String>)> {
        self.store.matches(given).unwrap().into_iter().map(|m| (m.person_id, m.person_name, m.engagement, m.parts, m.trait_names)).collect()
    }

    fn people(&self, given: &Event) -> Vec<String> {
        self.matches(given).into_iter().map(|m| m.0).collect()
    }

    fn record(&self, given: &Event, source: &str, when: Moment) -> Vec<Row> {
        let _held = WRITE_LOCK.hold();
        self.store.record(given, source, Some(when)).unwrap().iter().map(RowItem::to_row).collect()
    }

    fn by_person(&self, person: &str) -> Vec<Value> {
        let found = self.store.by_person().unwrap().into_iter().find(|(p, _)| p == person).map(|(_, v)| v).unwrap_or_default();
        found.iter().map(|e| serde_json::to_value(e).unwrap()).collect()
    }

    /// The tab's rows.
    fn rows(&self) -> Vec<Row> {
        self.store.all().unwrap().iter().map(RowItem::to_row).collect()
    }
}

fn text(row: &Row, name: &str) -> String {
    rows::text(row, name).unwrap_or_default().to_string()
}

#[test]
fn a_with_part_counts_the_user_and_everyone_it_was_planned_with() {
    let f = fixture(json!([{"kind": "follow_through"}]), sam());

    let matches = f.matches(&event("c1", &["gym"], &["sam"], &[]));

    let part = strings(&["reliable/follow_through"]);
    let names = strings(&["Reliable"]);
    assert_eq!(
        matches.into_iter().map(|(p, _, e, parts, traits)| (p, e, parts, traits)).collect::<Vec<_>>(),
        vec![("self".to_string(), "with".to_string(), part.clone(), names.clone()), ("sam".to_string(), "with".to_string(), part, names)]
    );
    assert!(f.t.fake.tab_titles(&f.t.spreadsheet()).contains(&CANCELLATIONS.1.to_string()));
}

#[test]
fn a_for_part_counts_only_who_it_was_for() {
    let f = fixture(json!([{"kind": "follow_through", "engagement_type": "for"}]), sam());

    assert_eq!(f.people(&event("c1", &["gym"], &[], &["sam"])), strings(&["sam"]));
    assert!(f.people(&event("c1", &["gym"], &["sam"], &[])).is_empty());
}

#[test]
fn a_part_with_an_action_counts_only_events_of_it() {
    let f = fixture(json!([{"kind": "follow_through", "action": "eat"}]), vec![]);

    assert!(f.people(&event("c1", &["gym"], &[], &[])).is_empty());
    assert_eq!(f.people(&event("c1", &["eat"], &[], &[])), strings(&["self"]));
}

#[test]
fn someone_no_longer_active_isnt_counted() {
    let f = fixture(json!([{"kind": "follow_through"}]), vec![json!({"id": "old", "name": "Old", "status": "archived"})]);

    assert_eq!(f.people(&event("c1", &["gym"], &["old"], &[])), strings(&["self"]));
}

#[test]
fn only_a_matching_cancellation_is_recorded() {
    let f = fixture(json!([{"kind": "continuity"}]), vec![]);

    assert!(f.record(&event("c1", &["gym"], &[], &[]), "delete_event", at()).is_empty());
    assert!(f.store.all().unwrap().is_empty());
}

#[test]
fn recording_writes_a_row_per_person_and_again_harmlessly() {
    let f = fixture(json!([{"kind": "follow_through"}]), sam());
    let given = event("c1", &["gym"], &["sam"], &[]);

    f.record(&given, "compaction abc", at());
    f.record(&given, "compaction abc", at());

    let rows = f.rows();
    let shown: Vec<(String, String, Value)> = rows.iter().map(|r| (text(r, "id"), text(r, "source"), r["parts"].clone())).collect();
    assert_eq!(
        shown,
        vec![
            ("c1/sam/with".to_string(), "compaction abc".to_string(), json!(["reliable/follow_through"])),
            ("c1/self/with".to_string(), "compaction abc".to_string(), json!(["reliable/follow_through"])),
        ]
    );
    let sam = to_event(&rows::item_of(rows.iter().find(|r| text(r, "person_id") == "sam").unwrap().clone())).unwrap();
    assert_eq!((sam.start, sam.end, sam.action_ids.clone()), (Some(at()), Some(at() + chrono::Duration::hours(1)), Some(crate::testing::shares(&["gym"]))));
    assert_eq!(sam.facts.unwrap().with(), strings(&["sam"]));
}

#[test]
fn rows_older_than_it_keeps_are_dropped_as_new_ones_are_written() {
    let f = fixture(json!([{"kind": "follow_through"}]), vec![]);
    let mut old = event("old", &["gym"], &[], &[]);
    old.start = Some(old.start() - chrono::Duration::days(KEEP_DAYS + 1));
    old.end = Some(old.end() - chrono::Duration::days(KEEP_DAYS + 1));
    f.record(&old, "delete_event", at() - chrono::Duration::days(1));

    f.record(&event("new", &["gym"], &[], &[]), "delete_event", at());

    let ids: Vec<String> = f.rows().iter().map(|r| text(r, "event_id")).collect();
    assert_eq!(ids, strings(&["new"]));
}

#[test]
fn by_person_gives_each_persons_cancellations_newest_first() {
    let f = fixture(json!([{"kind": "follow_through"}]), sam());
    let mut earlier = event("early", &["gym"], &["sam"], &[]);
    earlier.start = Some(earlier.start() - chrono::Duration::days(1));
    earlier.end = Some(earlier.end() - chrono::Duration::days(1));
    f.record(&earlier, "delete_event", at());
    f.record(&event("late", &["gym"], &["sam"], &[]), "compaction abc", at());

    let ids = |person: &str| -> Vec<Value> { f.by_person(person).iter().map(|c| c["event_id"].clone()).collect() };
    assert_eq!(ids("sam"), vec![json!("late"), json!("early")]);
    let late = &f.by_person("sam")[0];
    let when = json!(crate::py::pydantic_datetime(&at()));
    assert_eq!((late["start"].clone(), late["engagement"].clone(), late["source"].clone(), late["cancelled_at"].clone()), (when.clone(), json!("with"), json!("compaction abc"), when));
    assert_eq!(ids("self"), vec![json!("late"), json!("early")]);
}

// -- habits ----------------------------------------------------------------------------------

fn with_habits(parts: Value, habits: Vec<Value>) -> Fixture {
    let f = fixture(parts, vec![]);
    f.t.seed(HABITS.1, &habits);
    f
}

#[test]
fn a_habit_counts_a_cancel_in_its_scope_under_its_id() {
    let f = with_habits(
        json!([{"kind": "follow_through"}]),
        vec![
            json!({"id": "fit", "name": "Fitness", "action_id": "body", "status": "active"}),
            json!({"id": "food", "name": "Eating well", "action_id": "eat", "status": "active"}),
            json!({"id": "old", "name": "Old", "action_id": "gym", "status": "archived"}),
        ],
    );

    let matches = f.matches(&event("c1", &["gym"], &[], &[]));

    let part = strings(&["reliable/follow_through"]);
    assert_eq!(
        matches.into_iter().map(|(p, n, e, parts, _)| (p, n, e, parts)).collect::<Vec<_>>(),
        vec![
            ("self".to_string(), "Me".to_string(), "with".to_string(), part.clone()),
            ("habit:fit".to_string(), "the Fitness habit".to_string(), "with".to_string(), part),
        ]
    );
}

#[test]
fn a_habits_for_parts_and_parts_of_other_actions_dont_count() {
    let f = with_habits(
        json!([{"kind": "follow_through", "engagement_type": "for"}, {"kind": "follow_through", "action": "eat"}]),
        vec![json!({"id": "fit", "name": "Fitness", "action_id": "body", "status": "active"})],
    );

    assert!(f.people(&event("c1", &["gym"], &[], &[])).is_empty());
}

#[test]
fn a_habits_cancellations_are_recorded_and_listed_under_its_id() {
    let f = with_habits(json!([{"kind": "follow_through"}]), vec![json!({"id": "fit", "name": "Fitness", "action_id": "body", "status": "active"})]);

    f.record(&event("c1", &["gym"], &[], &[]), "delete_event", at());

    let listed = f.by_person("habit:fit");
    assert_eq!(listed.len(), 1);
    assert_eq!((listed[0]["event_id"].clone(), listed[0]["engagement"].clone(), listed[0]["parts"].clone()), (json!("c1"), json!("with"), json!(["reliable/follow_through"])));
    let row = f.rows().into_iter().find(|r| text(r, "person_id") == "habit:fit").unwrap();
    // As follow-through reads it: the user's, with no one in its facts.
    assert!(to_event(&rows::item_of(row)).unwrap().facts.is_none_or(|facts| facts.is_empty()));
}
