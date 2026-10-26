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

    let created = f.held(|h| h.create_habit(&habit(json!({"name": "Guitar", "action_id": "guitar", "note": "Daily, mostly scales"})))).unwrap();

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

/// The fixture's habits, with Sam, the family circle and two traits to
/// be about and named by.
fn with_targets(f: &Fixture) -> Habits {
    let targets = Targets {
        people: vec![("sam".into(), "Sam".into())],
        circles: vec![("family".into(), "Family".into())],
        traits: vec![("reliable".into(), "Reliable".into()), ("kind".into(), "Kind".into()), ("present".into(), "Present".into())],
    };
    f.habits.clone().with_targets(Arc::new(move || Ok(targets.clone())))
}

#[test]
fn a_persons_or_circles_habit_is_named_by_them_and_its_traits() {
    let f = fixture(Some(strings(&["reliable", "kind", "present"])));
    let habits = with_targets(&f);
    let _held = WRITE_LOCK.hold();
    let name = |given: Value| habits.create_habit(&habit(given)).unwrap().habit.name.unwrap();

    assert_eq!(name(json!({"person_id": "sam"})), "Sam");
    assert_eq!(name(json!({"person_id": "sam", "traits": {"select": ["reliable"]}})), "Sam · Reliable");
    assert_eq!(name(json!({"person_id": "sam", "traits": {"select": ["reliable", "kind", "present"]}})), "Sam · Reliable, Kind +1");
    assert_eq!(name(json!({"circle_id": "family", "traits": {"except": ["reliable"]}})), "Family · except Reliable");
    let own = habits.create_habit(&habit(json!({"person_id": "sam", "name": "Sam, closer"}))).unwrap().habit;
    assert_eq!((own.name.unwrap(), own.default_name.unwrap()), ("Sam, closer".to_string(), "Sam".to_string()));
}

#[test]
fn a_persons_habit_is_about_them_alone_and_scored_as_they_are() {
    let f = fixture(Some(strings(&["reliable", "kind", "present"])));
    let habits = with_targets(&f);
    let _held = WRITE_LOCK.hold();

    refused(habits.create_habit(&habit(json!({"person_id": "ana"}))), "person_id 'ana' isn't a person");
    refused(habits.create_habit(&habit(json!({"person_id": "sam", "action_id": "walk"}))), "is about one thing");
    refused(
        habits.create_habit(&habit(json!({"person_id": "sam", "traits": {"parts": {"kind": [{"kind": "count", "target": 1}]}}}))),
        "takes only \"select\" and \"except\"",
    );
    refused(habits.create_habit(&habit(json!({"person_id": "sam", "traits": {"except": ["nope"]}}))), "\"except\" names 'nope'");
}

#[test]
fn a_persons_habit_loses_its_own_name_when_its_traits_change() {
    let f = fixture(Some(strings(&["reliable", "kind", "present"])));
    let habits = with_targets(&f);
    let _held = WRITE_LOCK.hold();
    let id = habits.create_habit(&habit(json!({"person_id": "sam", "name": "Mine"}))).unwrap().created_id;

    let kept = habits.update_habit(&habit(json!({"id": id, "note": "x"})), &[]).unwrap();
    let reset = habits.update_habit(&habit(json!({"id": id, "traits": {"except": ["kind"]}})), &[]).unwrap();
    let renamed = habits.update_habit(&habit(json!({"id": id, "name": "Again", "traits": {"select": "all"}})), &[]).unwrap();

    assert_eq!((kept.name.unwrap(), reset.name.unwrap(), renamed.name.unwrap()), ("Mine".into(), "Sam · except Kind".into(), "Again".to_string()));
}

#[test]
fn a_persons_habit_given_its_default_name_has_none_of_its_own() {
    let f = fixture(None);
    let habits = with_targets(&f);
    let _held = WRITE_LOCK.hold();
    let id = habits.create_habit(&habit(json!({"person_id": "sam", "name": "Sam"}))).unwrap().created_id;

    let selected = habits.update_habit(&habit(json!({"id": id, "name": "Sam · Reliable", "traits": {"select": ["reliable"]}})), &[]).unwrap();

    assert_eq!(selected.name.as_deref(), Some("Sam · Reliable"));
    let stored = habits.all().unwrap().into_iter().find(|h| h.id.as_deref() == Some(id.as_str())).unwrap();
    assert_eq!(stored.name, None);
}

#[test]
fn a_persons_habits_go_with_them() {
    let f = fixture(None);
    let habits = with_targets(&f);
    let _held = WRITE_LOCK.hold();
    let sams = habits.create_habit(&habit(json!({"person_id": "sam"}))).unwrap().created_id;
    let walk = habits.create_habit(&habit(json!({"name": "Walk", "action_id": "walk", "foundations": [subject_id(&sams)]}))).unwrap().created_id;

    assert_eq!(habits.delete_about("sam").unwrap(), vec![subject_id(&sams)]);

    let all = habits.all().unwrap();
    let of = |id: &str| all.iter().find(|h| h.id.as_deref() == Some(id)).unwrap().clone();
    assert_eq!((of(&sams).status, of(&walk).foundations), (Some(Status::Deleted), Some(vec![])));
}

#[test]
fn a_recurring_goals_habit_needs_no_action_and_is_about_it_alone() {
    let f = fixture(Some(strings(&["reliable"])));
    let _held = WRITE_LOCK.hold();

    let made = f.habits.create_habit(&habit(json!({"name": "Call Mom", "recurring_goal": {"period": "week", "rate_periods": 4}}))).unwrap().habit;

    assert_eq!(made.action_id, None);
    assert_eq!(json_of(&made.recurring_goal), json!({"period": "week", "rate_periods": 4}));
    // Scored by its goal rate alone till it's given traits.
    assert_eq!(made.traits.map(Value::Object), Some(json!({"select": []})));
    let templates = f.habits.templates().unwrap();
    assert_eq!(templates.len(), 1);
    assert_eq!((templates[0].habit_id.as_str(), templates[0].name.as_str()), (made.id.as_deref().unwrap(), "Call Mom"));
    refused(f.habits.create_habit(&habit(json!({"name": "Both", "action_id": "walk", "recurring_goal": {"period": "week"}}))), "is about one thing");
    refused(f.habits.create_habit(&habit(json!({"name": "When", "recurring_goal": {}}))), "recurring_goal needs a period");
    refused(f.habits.create_habit(&habit(json!({"name": "Neither"}))), "circle_id or recurring_goal");
}

#[test]
fn only_active_habits_make_goals() {
    let f = fixture(None);
    let _held = WRITE_LOCK.hold();
    let id = f.habits.create_habit(&habit(json!({"name": "Call Mom", "recurring_goal": {"period": "week"}}))).unwrap().created_id;
    f.habits.create_habit(&habit(json!({"name": "Guitar", "action_id": "guitar"}))).unwrap();

    assert_eq!(f.habits.templates().unwrap().len(), 1);
    f.habits.update_habit(&habit(json!({"id": id, "status": "archived"})), &[]).unwrap();
    assert!(f.habits.templates().unwrap().is_empty());
}

#[test]
fn another_habits_traits_gone_wrong_dont_keep_one_from_being_saved() {
    let f = fixture(Some(strings(&["patient", "present"])));
    let id = f.create(json!({"name": "Guitar", "action_id": "guitar", "traits": {"select": ["patient"]}})).unwrap();
    // A trait it named is gone.
    let trait_ids: TraitIds = Arc::new(|| Ok(vec!["present".to_string()]));
    let sheets = SheetsClient::new(f.fake.google());
    let habits = {
        let _held = WRITE_LOCK.hold();
        Habits::ensure(&sheets, &f.spreadsheet, None, Some(trait_ids)).unwrap()
    };
    let _held = WRITE_LOCK.hold();

    habits.create_habit(&habit(json!({"name": "Walk", "action_id": "walk"}))).unwrap();

    refused(habits.update_habit(&habit(json!({"id": id, "note": "x"})), &[]), "names 'patient', which isn't a trait");
}
