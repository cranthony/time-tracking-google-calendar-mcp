//! The module's tests, against the fake Google. (A location's unknown
//! fields are refused by the tools' argument checking.)

use super::*;
use crate::testing::fake_google::FakeGoogle;
use crate::write_lock::WRITE_LOCK;

struct Fixture {
    fake: FakeGoogle,
    spreadsheet: String,
    locations: Locations,
}

fn fixture() -> Fixture {
    let fake = FakeGoogle::start();
    let sheets = SheetsClient::new(fake.google());
    let _held = WRITE_LOCK.hold();
    let spreadsheet = sheets.create_spreadsheet("Calendar Metadata").unwrap();
    let locations = Locations::ensure(&sheets, &spreadsheet).unwrap();
    Fixture { fake, spreadsheet, locations }
}

fn location(given: Value) -> Row {
    ROW.from_value(&given)
}

fn refused<T>(result: anyhow::Result<T>, pattern: &str) {
    let Err(error) = result else { panic!("{pattern:?} wasn't refused") };
    assert!(regex::Regex::new(pattern).unwrap().is_match(&error.to_string()), "{pattern:?} isn't in: {error}");
}

impl Fixture {
    fn held<T>(&self, f: impl FnOnce(&Locations) -> anyhow::Result<T>) -> anyhow::Result<T> {
        let _held = WRITE_LOCK.hold();
        f(&self.locations)
    }

    fn create(&self, given: Value) -> String {
        self.held(|l| l.create_location(&location(given))).unwrap()["created_id"].as_str().unwrap().to_string()
    }
}

#[test]
fn creates_and_finds_a_location_by_id_or_name() {
    let f = fixture();

    let id = f.create(json!({"id": "mine", "name": "Home", "hint": "the apartment"}));

    assert_ne!(id, "mine");
    assert_eq!(f.locations.get_location(&id).unwrap()["name"], "Home");
    assert_eq!(f.locations.get_location("HOME").unwrap()["hint"], "the apartment");
    assert!(f.fake.tab_titles(&f.spreadsheet).contains(&LOCATIONS.1.to_string()));
}

#[test]
fn names_are_unique() {
    let f = fixture();
    f.create(json!({"name": "Home"}));

    refused(f.held(|l| l.create_location(&location(json!({"name": "home"})))), r"already a location named 'Home' \(.*\); location names must be unique");
}

#[test]
fn needs_a_name() {
    refused(fixture().held(|l| l.create_location(&location(json!({"hint": "somewhere"})))), "needs a name");
}

#[test]
fn updates_and_clears() {
    let f = fixture();
    let home = f.create(json!({"name": "Home", "hint": "the apartment"}));

    assert_eq!(f.held(|l| l.update_location(&location(json!({"id": home, "name": "Flat"})), &[])).unwrap()["hint"], "the apartment");
    assert!(f.held(|l| l.update_location(&location(json!({"id": home})), &["hint".to_string()])).unwrap()["hint"].is_null());
}

#[test]
fn update_refuses_what_it_cant_do() {
    let f = fixture();

    refused(f.held(|l| l.update_location(&location(json!({})), &[])), "needs the location's id");
    refused(f.held(|l| l.update_location(&location(json!({"id": "nope", "name": "X"})), &[])), "no location with the id or name 'nope'");
    refused(f.held(|l| l.update_location(&location(json!({"id": "x"})), &["name".to_string()])), r"Can't clear \['name'\]");
}

#[test]
fn deletes_by_id() {
    let f = fixture();
    let home = f.create(json!({"name": "Home"}));
    let studio = f.create(json!({"name": "Studio"}));

    assert_eq!(f.held(|l| l.delete_location(&home)).unwrap()["name"], "Home");
    let ids: Vec<Value> = f.locations.all().unwrap().iter().map(|l| l["id"].clone()).collect();
    assert_eq!(ids, vec![json!(studio)]);
    refused(f.held(|l| l.delete_location("studio")), &format!("by its id: 'Studio' is {studio}"));
}

#[test]
fn suggests_close_matches() {
    let f = fixture();
    let studio = f.create(json!({"name": "Salsa studio"}));

    refused(f.locations.get_location("salsa studo"), &format!(r"did you mean {studio} \(Salsa studio\)"));
}
