//! The module's tests, against the fake Google.

use super::*;
use crate::sheet::HABITS;
use crate::stores::actions::ACTION_ROW;
use crate::stores::groups::{palette, GROUP_ROW};
use crate::testing::fake_google::FakeGoogle;
use crate::write_lock::WRITE_LOCK;
use std::sync::Mutex;

/// Every action's and group's id, with its path, as action_scopes gives
/// them.
fn scopes() -> Scopes {
    vec![
        ("guitar".into(), "Creative › Guitar".into()),
        ("practice".into(), "Creative › Guitar › Practice guitar".into()),
        ("walk".into(), "Walk".into()),
    ]
}

struct Fixture {
    fake: FakeGoogle,
    spreadsheet: String,
    habits: Habits,
    scopes: Arc<Mutex<Scopes>>,
}

fn fixture(trait_ids: Option<Vec<String>>) -> Fixture {
    let fake = FakeGoogle::start();
    let sheets = SheetsClient::new(fake.google());
    let _held = WRITE_LOCK.hold();
    let spreadsheet = sheets.create_spreadsheet("Calendar Metadata").unwrap();
    let scopes = Arc::new(Mutex::new(super::tests::scopes()));
    let shared = scopes.clone();
    let scopes_fn: ScopesFn = Arc::new(move || Ok(shared.lock().unwrap().clone()));
    let trait_ids: Option<TraitIds> = trait_ids.map(|ids| Arc::new(move || Ok(ids.clone())) as TraitIds);
    let habits = Habits::ensure(&sheets, &spreadsheet, Some(scopes_fn), trait_ids).unwrap();
    Fixture { fake, spreadsheet, habits, scopes }
}

fn habit(given: Value) -> Habit {
    serde_json::from_value(given).unwrap()
}

fn json_of<T: serde::Serialize>(value: T) -> Value {
    serde_json::to_value(value).unwrap()
}

fn strings(items: &[&str]) -> Vec<String> {
    crate::testing::strings(items)
}

fn refused<T>(result: anyhow::Result<T>, pattern: &str) {
    let Err(error) = result else { panic!("{pattern:?} wasn't refused") };
    assert!(regex::Regex::new(pattern).unwrap().is_match(&error.to_string()), "{pattern:?} isn't in: {error}");
}

impl Fixture {
    fn held<T: serde::Serialize>(&self, f: impl FnOnce(&Habits) -> anyhow::Result<T>) -> anyhow::Result<Value> {
        let _held = WRITE_LOCK.hold();
        f(&self.habits).map(json_of)
    }

    fn create(&self, given: Value) -> anyhow::Result<String> {
        Ok(self.held(|h| h.create_habit(&habit(given)))?["created_id"].as_str().unwrap().to_string())
    }

    fn get(&self, id_or_name: &str) -> Value {
        json_of(self.habits.get_habit(id_or_name).unwrap())
    }
}

#[test]
fn creates_and_finds_a_habit_by_id_or_name_with_its_actions_path() {
    let f = fixture(None);

    let created = f.held(|h| h.create_habit(&habit(json!({"id": "mine", "name": "Guitar", "action_id": "guitar", "note": "Daily, mostly scales"})))).unwrap();

    let id = created["created_id"].as_str().unwrap();
    assert_ne!(id, "mine");
    assert_eq!((created["habit"]["status"].clone(), created["habit"]["action_path"].clone()), (json!("active"), json!("Creative › Guitar")));
    assert_eq!(f.get(id)["note"], "Daily, mostly scales");
    assert_eq!(f.get("GUITAR")["id"], json!(id));
    assert!(f.fake.tab_titles(&f.spreadsheet).contains(&HABITS.1.to_string()));
}

#[test]
fn lists_active_habits_by_default() {
    let f = fixture(None);
    f.create(json!({"name": "Guitar", "action_id": "guitar"})).unwrap();
    f.create(json!({"name": "Walk", "action_id": "walk", "status": "archived"})).unwrap();

    let names = |statuses: Option<&[&str]>| -> Vec<Value> {
        let statuses = statuses.map(strings);
        f.habits.get_habits(statuses.as_deref()).unwrap().into_iter().map(|h| json_of(h)["name"].clone()).collect()
    };
    assert_eq!(names(None), vec![json!("Guitar")]);
    assert_eq!(names(Some(&["active", "archived"])), vec![json!("Guitar"), json!("Walk")]);
    refused(f.habits.get_habits(Some(&strings(&["gone"]))), "Unknown habit status");
}

#[test]
fn needs_a_unique_name_and_an_action_or_group() {
    let f = fixture(None);
    f.create(json!({"name": "Guitar", "action_id": "guitar"})).unwrap();

    refused(f.create(json!({"name": "guitar", "action_id": "practice"})), r"already a habit named 'Guitar' \(.*\); habit names must be unique");
    refused(f.create(json!({"action_id": "walk"})), "needs a name");
    refused(f.create(json!({"name": "Walk"})), "needs an action_id");
    refused(f.create(json!({"name": "Walk", "action_id": "nope"})), "'nope' isn't an action or an action group");
}

#[test]
fn traits_are_checked_as_a_persons() {
    let f = fixture(Some(strings(&["patient", "present"])));

    let created = f
        .create(json!({"name": "Guitar", "action_id": "guitar", "traits": {"select": ["patient"], "parts": {"patient": [{"kind": "count", "target": 5, "noun": "sessions"}]}}}))
        .unwrap();

    assert_eq!(f.get(&created)["traits"]["select"], json!(["patient"]));
    refused(f.create(json!({"name": "Walk", "action_id": "walk", "traits": {"select": ["kind"]}})), "names 'kind', which isn't a trait");
    refused(f.create(json!({"name": "Walk", "action_id": "walk", "traits": {"people": []}})), "has no field \"people\"");
}

#[test]
fn updates_and_clears() {
    let f = fixture(None);
    let id = f.create(json!({"name": "Guitar", "action_id": "guitar", "note": "Daily", "traits": {"select": "all"}})).unwrap();

    let updated = f.held(|h| h.update_habit(&habit(json!({"id": id, "action_id": "practice", "status": "archived"})), &strings(&["note", "traits"]))).unwrap();

    assert_eq!(
        (updated["action_id"].clone(), updated["action_path"].clone(), updated["status"].clone()),
        (json!("practice"), json!("Creative › Guitar › Practice guitar"), json!("archived"))
    );
    assert!(updated["note"].is_null() && updated["traits"].is_null());
    refused(f.held(|h| h.update_habit(&habit(json!({"name": "x"})), &[])), "needs the habit's id");
    refused(f.held(|h| h.update_habit(&habit(json!({"id": "nope", "name": "x"})), &[])), "There's no habit with the id or name 'nope'");
}

#[test]
fn another_habits_action_gone_doesnt_stop_writing_this_one() {
    let f = fixture(None);
    f.create(json!({"name": "Walk", "action_id": "walk"})).unwrap();
    let guitar = f.create(json!({"name": "Guitar", "action_id": "guitar"})).unwrap();
    f.scopes.lock().unwrap().retain(|(id, _)| id != "walk"); // Its group deleted, say.

    f.held(|h| h.update_habit(&habit(json!({"id": guitar, "note": "Scales"})), &[])).unwrap();

    let walk = f.get("Walk");
    assert!(walk["action_path"].is_null());
    refused(f.held(|h| h.update_habit(&habit(json!({"id": walk["id"], "note": "Outside"})), &[])), "'walk' isn't an action or an action group");
}

#[test]
fn suggests_close_matches() {
    let f = fixture(None);
    f.create(json!({"name": "Guitar", "action_id": "guitar"})).unwrap();

    refused(f.habits.get_habit("Guitr"), r"did you mean .*\(Guitar\)");
}

#[test]
fn scopes_are_actions_and_groups_with_their_paths_but_not_deleted_actions() {
    let groups = GroupTree::new(
        vec![crate::sheet::rows::item_of(GROUP_ROW.from_value(&json!({"id": "creative", "name": "Creative"}))), crate::sheet::rows::item_of(GROUP_ROW.from_value(&json!({"id": "g", "name": "Guitar", "group_id": "creative"})))],
        palette(&[]),
    );
    let actions = vec![
        crate::sheet::rows::item_of(ACTION_ROW.from_value(&json!({"id": "practice", "name": "Practice guitar", "group_id": "g", "status": "active"}))),
        crate::sheet::rows::item_of(ACTION_ROW.from_value(&json!({"id": "old", "name": "Old", "status": "deleted"}))),
    ];

    let expected: Scopes = vec![
        ("creative".into(), "Creative".into()),
        ("g".into(), "Creative › Guitar".into()),
        ("practice".into(), "Creative › Guitar › Practice guitar".into()),
    ];
    assert_eq!(action_scopes(&actions, &groups), expected);
}

#[test]
fn a_habit_goes_by_a_prefixed_id_where_a_persons_would() {
    assert_eq!(subject_id("a7k2qp"), "habit:a7k2qp");
}

#[test]
fn foundations_are_other_habits_or_people_without_a_cycle() {
    let f = fixture(None);
    let people: NodesFn = Arc::new(|| Ok(vec![Node { subject: "family".into(), deleted: false, foundations: vec!["habit:nope".into()] }]));
    let habits = f.habits.clone().with_people(people);
    let held = |given: Value| {
        let _held = WRITE_LOCK.hold();
        habits.create_habit(&habit(given)).map(|c| c.created_id)
    };
    let sleep = held(json!({"name": "Sleep", "action_id": "walk"})).unwrap();
    let guitar = held(json!({"name": "Guitar", "action_id": "guitar", "foundations": [subject_id(&sleep), "family"]})).unwrap();

    assert_eq!(f.get(&guitar)["foundations"], json!([subject_id(&sleep), "family"]));
    assert!(f.get(&sleep)["foundations"].is_null());
    refused(held(json!({"name": "Walk", "action_id": "walk", "foundations": [sleep.clone()]})), &format!(r"habit .*'s foundations \['{sleep}'\] aren't habits or people"));
    let update = |given: Value| {
        let _held = WRITE_LOCK.hold();
        habits.update_habit(&habit(given), &[])
    };
    refused(update(json!({"id": sleep, "foundations": [subject_id(&guitar)]})), &format!(r"habit {sleep}'s foundations would make a cycle: habit:{sleep} → habit:{guitar} → habit:{sleep}"));
}

#[test]
fn a_habit_deleted_leaves_the_others_foundations() {
    let f = fixture(None);
    let sleep = f.create(json!({"name": "Sleep", "action_id": "walk"})).unwrap();
    let guitar = f.create(json!({"name": "Guitar", "action_id": "guitar", "foundations": [subject_id(&sleep)]})).unwrap();

    f.held(|h| h.update_habit(&habit(json!({"id": sleep, "status": "deleted"})), &[])).unwrap();

    assert_eq!(f.get(&guitar)["foundations"], json!([]));
    let walk = f.create(json!({"name": "Walk", "action_id": "walk"})).unwrap();
    f.held(|h| h.update_habit(&habit(json!({"id": guitar, "foundations": [subject_id(&walk)]})), &[])).unwrap();
    f.held(|h| h.drop_foundation(&subject_id(&walk))).unwrap();
    assert_eq!(f.get(&guitar)["foundations"], json!([]));
}
