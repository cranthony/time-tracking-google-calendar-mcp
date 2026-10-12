//! The module's tests, against the fake Google.

use super::*;
use crate::google::sheets::SheetsClient;
use crate::testing::fake_google::FakeGoogle;
use crate::write_lock::WRITE_LOCK;
use serde_json::{json, Map, Value};

struct Fixture {
    fake: FakeGoogle,
    spreadsheet: String,
    people: People,
}

fn fixture() -> Fixture {
    let fake = FakeGoogle::start();
    let sheets = SheetsClient::new(fake.google());
    let _held = WRITE_LOCK.hold();
    let spreadsheet = sheets.create_spreadsheet("Calendar Metadata").unwrap();
    let trait_ids: TraitIds = Arc::new(|| Ok(vec!["reliable".to_string(), "creative".to_string()]));
    let people = People::ensure(&sheets, &spreadsheet, Some(trait_ids)).unwrap();
    Fixture { fake, spreadsheet, people }
}

fn person(given: Value) -> Person {
    serde_json::from_value(given).unwrap()
}

fn circle(given: Value) -> Circle {
    serde_json::from_value(given).unwrap()
}

/// A typed result as a JSON object, to read as the tools' clients do.
fn object<T: serde::Serialize>(value: T) -> Map<String, Value> {
    match serde_json::to_value(value).unwrap() {
        Value::Object(map) => map,
        other => panic!("not an object: {other}"),
    }
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
    fn held<T: serde::Serialize>(&self, f: impl FnOnce(&People) -> anyhow::Result<T>) -> anyhow::Result<Value> {
        let _held = WRITE_LOCK.hold();
        f(&self.people).map(json_of)
    }

    fn create(&self, given: Value) -> String {
        self.held(|p| p.create_person(&person(given))).unwrap()["created_id"].as_str().unwrap().to_string()
    }

    fn circle(&self, given: Value) -> String {
        self.held(|p| p.create_circle(&circle(given))).unwrap()["created_id"].as_str().unwrap().to_string()
    }

    fn update(&self, given: Value, clear: &[&str]) -> anyhow::Result<Map<String, Value>> {
        let _held = WRITE_LOCK.hold();
        self.people.update_person(&person(given), &strings(clear)).map(object)
    }

    fn listed(&self, statuses: Option<&[&str]>) -> Vec<Map<String, Value>> {
        let statuses = statuses.map(strings);
        self.people.get_people(statuses.as_deref()).unwrap().into_iter().map(object).collect()
    }

    fn get(&self, id_or_name: &str) -> Map<String, Value> {
        object(json_of(self.people.get_person(id_or_name).unwrap()))
    }
}

fn field(listed: &[Map<String, Value>], name: &str) -> Vec<Value> {
    listed.iter().map(|p| p[name].clone()).collect()
}

// -- self ------------------------------------------------------------------------------

#[test]
fn self_is_always_there_first_without_a_row() {
    let f = fixture();

    let listed = f.listed(None);

    assert_eq!(listed.len(), 1);
    assert_eq!((listed[0]["id"].clone(), listed[0]["name"].clone(), listed[0]["status"].clone()), (json!(SELF_ID), json!("Me"), json!("active")));
    assert_eq!(f.get("self")["id"], SELF_ID);
    let tabs = f.fake.tab_titles(&f.spreadsheet);
    assert!(tabs.contains(&PEOPLE.1.to_string()) && tabs.contains(&CIRCLES.1.to_string()));
}

#[test]
fn updating_self_adds_their_row_once() {
    let f = fixture();
    f.create(json!({"name": "Sam"}));

    f.update(json!({"id": SELF_ID, "what_matters": "music"}), &[]).unwrap();
    f.update(json!({"id": SELF_ID, "name": "Chris"}), &[]).unwrap();

    let listed = f.listed(None);
    assert_eq!((listed[0]["id"].clone(), listed[0]["name"].clone(), listed[0]["what_matters"].clone()), (json!(SELF_ID), json!("Chris"), json!("music")));
    assert_eq!(field(&listed, "name"), vec![json!("Chris"), json!("Sam")]);
}

#[test]
fn self_is_always_active() {
    refused(fixture().update(json!({"id": SELF_ID, "status": "archived"}), &[]), "self is always active");
}

#[test]
fn another_person_cant_take_selfs_name_without_a_context() {
    refused(fixture().held(|p| p.create_person(&person(json!({"name": "me"})))), r"already a person named 'Me' with no context \(self\)");
}

// -- creating ---------------------------------------------------------------------------

#[test]
fn assigns_an_id_and_is_active() {
    let f = fixture();

    let created = f.held(|p| p.create_person(&person(json!({"id": "mine", "name": "Sam", "context": "met at salsa"})))).unwrap();

    let id = created["created_id"].as_str().unwrap();
    assert!(id != "mine" && id.len() == 6);
    assert_eq!(created["person"]["status"], "active");
    assert_eq!(f.get(id)["context"], "met at salsa");
}

#[test]
fn a_name_and_context_together_must_be_unique() {
    let f = fixture();
    f.create(json!({"name": "Sam", "context": "met at salsa"}));
    f.create(json!({"name": "Sam", "context": "work"}));

    refused(f.held(|p| p.create_person(&person(json!({"name": "sam", "context": " Work"})))), r"already a person named 'Sam' with the context 'work'.*give a context");
}

#[test]
fn a_name_someone_else_has_needs_a_context() {
    let f = fixture();
    f.create(json!({"name": "Sam", "context": "met at salsa"}));

    refused(f.held(|p| p.create_person(&person(json!({"name": "sam"})))), r"already a person named 'Sam' with the context 'met at salsa'.*needs a context");
    f.create(json!({"name": "Sam", "context": "work"}));
}

#[test]
fn people_already_sharing_a_name_arent_stuck() {
    let f = fixture();
    let first = f.create(json!({"name": "Sam"}));
    let second = f.create(json!({"name": "Sam", "context": "work"}));
    f.update(json!({"id": second, "what_matters": "music"}), &[]).unwrap();
    f.update(json!({"id": first, "what_matters": "books"}), &[]).unwrap();

    refused(f.update(json!({"id": second}), &["context"]), "needs a context");
}

#[test]
fn refuses_unknown_circles() {
    // (A status that isn't one can't be given: the schema refuses it, and
    // a cell holding one makes the tab unreadable.)
    let f = fixture();

    refused(f.held(|p| p.create_person(&person(json!({"name": "Sam", "circles": ["nope"]})))), r"circles \['nope'\] aren't circles");
}

// -- updating -----------------------------------------------------------------------------

#[test]
fn sets_given_fields_keeps_the_rest_and_clears() {
    let f = fixture();
    let family = f.circle(json!({"name": "Family"}));
    let sam = f.create(json!({"name": "Sam", "context": "cousin", "what_matters": "tea"}));

    let listed = f.update(json!({"id": sam, "circles": [family]}), &["context"]).unwrap();

    assert_eq!(
        (listed["name"].clone(), listed["context"].clone(), listed["circles"].clone(), listed["circle_names"].clone(), listed["what_matters"].clone()),
        (json!("Sam"), Value::Null, json!([family]), json!(["Family"]), json!("tea"))
    );
}

#[test]
fn update_refuses_what_it_cant_do() {
    let f = fixture();

    refused(f.update(json!({}), &[]), "needs the person's id");
    refused(f.update(json!({"id": "nope"}), &[]), "no person with the id or name 'nope'");
    refused(f.update(json!({"id": "x"}), &["name"]), r"Can't clear \['name'\]");
}

// -- listing --------------------------------------------------------------------------------

#[test]
fn lists_active_people_by_default() {
    let f = fixture();
    let sam = f.create(json!({"name": "Sam"}));
    f.create(json!({"name": "Alex", "status": "archived"}));

    assert_eq!(field(&f.listed(None), "id"), vec![json!(SELF_ID), json!(sam)]);
    assert_eq!(field(&f.listed(Some(&["archived"])), "name"), vec![json!("Alex")]);
}

#[test]
fn a_shared_name_asks_for_the_id_and_a_near_miss_is_suggested() {
    let f = fixture();
    let a = f.create(json!({"name": "Sam", "context": "salsa"}));
    let b = f.create(json!({"name": "Sam", "context": "work"}));
    let samantha = f.create(json!({"name": "Samantha"}));

    refused(f.people.get_person("sam"), &format!(r"more than one person named 'sam': {a} \(salsa\), {b} \(work\)"));
    refused(f.people.get_person("samanta"), &format!(r"did you mean {samantha} \(Samantha\)"));
}

// -- circles ----------------------------------------------------------------------------------

#[test]
fn circles_are_created_listed_and_found() {
    let f = fixture();
    let family = f.circle(json!({"name": "Family", "note": "by blood or choice"}));
    let sam = f.create(json!({"name": "Sam", "circles": [family]}));

    let listed = json_of(f.people.get_circles().unwrap());

    assert_eq!(listed, json!([{"id": family, "name": "Family", "note": "by blood or choice", "member_ids": [sam]}]));
    assert_eq!(json_of(f.people.get_circle("family").unwrap())["id"], json!(family));
}

#[test]
fn circle_names_are_unique_among_circles_but_may_match_a_person() {
    let f = fixture();
    f.create(json!({"name": "Family"}));
    f.circle(json!({"name": "Family"}));

    refused(f.held(|p| p.create_circle(&circle(json!({"name": "family"})))), r"already a circle named 'Family' \(.*\); circle names must be unique");
}

#[test]
fn updating_a_circle_renames_and_clears() {
    let f = fixture();
    let family = f.circle(json!({"name": "Family", "note": "n"}));

    let listed = f.held(|p| p.update_circle(&circle(json!({"id": family, "name": "Kin"})), &strings(&["note"]))).unwrap();

    assert_eq!((listed["name"].clone(), listed["note"].clone()), (json!("Kin"), Value::Null));
}

#[test]
fn deleting_a_circle_takes_its_people_out_of_it() {
    let f = fixture();
    let family = f.circle(json!({"name": "Family"}));
    let friends = f.circle(json!({"name": "Friends"}));
    let sam = f.create(json!({"name": "Sam", "circles": [family, friends]}));
    let alex = f.create(json!({"name": "Alex", "circles": [family]}));

    let deleted = f.held(|p| p.delete_circle(&family)).unwrap();

    assert_eq!((deleted["deleted"]["name"].clone(), deleted["left"].clone()), (json!("Family"), json!([sam, alex])));
    assert_eq!(f.get(&sam)["circles"], json!([friends]));
    assert!(f.get(&alex)["circles"].is_null());
    let names: Vec<Value> = json_of(f.people.get_circles().unwrap()).as_array().unwrap().iter().map(|c| c["name"].clone()).collect();
    assert_eq!(names, vec![json!("Friends")]);
}

#[test]
fn deleting_a_circle_needs_its_id() {
    let f = fixture();
    let family = f.circle(json!({"name": "Family"}));

    refused(f.held(|p| p.delete_circle("family")), &format!("by its id: 'Family' is {family}"));
    refused(f.held(|p| p.delete_circle("nothing")), "no circle");
}

// -- a person's traits -----------------------------------------------------------------------------

#[test]
fn a_person_can_select_traits_and_give_their_own_parts() {
    let f = fixture();
    let traits = json!({"select": ["reliable"], "parts": {"reliable": [{"kind": "count", "target": 1, "interval_days": 21}]}});

    let sam = f.create(json!({"name": "Sam", "traits": traits}));

    assert_eq!(f.get(&sam)["traits"], traits);
}

#[test]
fn self_can_have_traits_too_and_they_clear() {
    let f = fixture();

    f.update(json!({"id": SELF_ID, "traits": {"select": ["reliable"]}}), &[]).unwrap();
    assert_eq!(f.get(SELF_ID)["traits"], json!({"select": ["reliable"]}));

    f.update(json!({"id": SELF_ID}), &["traits"]).unwrap();
    assert!(f.get(SELF_ID)["traits"].is_null());
}

#[test]
fn refuses_traits_that_arent_traits_or_bad_parts() {
    let f = fixture();

    refused(f.held(|p| p.create_person(&person(json!({"name": "Sam", "traits": {"select": ["kind"]}})))), "traits \"select\" names 'kind', which isn't a trait");
    refused(f.held(|p| p.create_person(&person(json!({"name": "Sam", "traits": {"parts": {"reliable": [{"kind": "count"}]}}})))), r#"traits "parts" for 'reliable': part 1 \(count\) needs"#);
}

// -- foundations -----------------------------------------------------------------------

#[test]
fn a_person_with_foundations_even_none_is_on_the_habit_graph() {
    let f = fixture();
    let habits: NodesFn = Arc::new(|| Ok(vec![Node { subject: "habit:gym".into(), deleted: false, foundations: vec![] }]));
    let people = f.people.clone().with_habits(habits);
    let held = |given: Value, clear: &[&str]| {
        let _held = WRITE_LOCK.hold();
        people.update_person(&person(given), &strings(clear)).map(json_of)
    };
    let family = f.create(json!({"name": "Family"}));
    let friend = f.create(json!({"name": "Friend"}));

    assert!(f.get(&family)["foundations"].is_null());
    assert_eq!(held(json!({"id": family, "foundations": []}), &[]).unwrap()["foundations"], json!([]));
    assert_eq!(held(json!({"id": friend, "foundations": ["habit:gym", family]}), &[]).unwrap()["foundations"], json!(["habit:gym", family]));
    assert_eq!(held(json!({"id": "self", "foundations": [friend]}), &[]).unwrap()["foundations"], json!([friend]));
    refused(held(json!({"id": family, "foundations": [friend]}), &[]), &format!("person {family}'s foundations would make a cycle: {family} → {friend} → {family}"));
    refused(held(json!({"id": family, "foundations": ["habit:read"]}), &[]), r"foundations \['habit:read'\] aren't habits or people");
    assert!(held(json!({"id": family}), &["foundations"]).unwrap()["foundations"].is_null());
}

#[test]
fn a_person_deleted_leaves_the_others_foundations() {
    let f = fixture();
    let family = f.create(json!({"name": "Family"}));
    let friend = f.create(json!({"name": "Friend", "foundations": [family]}));

    f.update(json!({"id": family, "status": "deleted"}), &[]).unwrap();

    assert_eq!(f.get(&friend)["foundations"], json!([]));
    refused(f.update(json!({"id": friend, "foundations": [family]}), &[]), "aren't habits or people");
}
