//! The module's tests, against the fake Google. (A location's unknown
//! fields are refused by the tools' argument checking.)

use super::*;
use crate::testing::fake_google::FakeGoogle;
use crate::write_lock::WRITE_LOCK;
use serde_json::{json, Value};

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

fn location(given: Value) -> Location {
    serde_json::from_value(given).unwrap()
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
        self.held(|l| l.create_location(&location(given))).unwrap().created_id
    }
}

#[test]
fn creates_and_finds_a_location_by_id_or_name() {
    let f = fixture();

    let id = f.create(json!({"id": "mine", "name": "Home", "hint": "the apartment"}));

    assert_ne!(id, "mine");
    assert_eq!(f.locations.get_location(&id).unwrap().name.as_deref(), Some("Home"));
    assert_eq!(f.locations.get_location("HOME").unwrap().hint.as_deref(), Some("the apartment"));
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

    assert_eq!(f.held(|l| l.update_location(&location(json!({"id": home, "name": "Flat"})), &[])).unwrap().hint.as_deref(), Some("the apartment"));
    assert!(f.held(|l| l.update_location(&location(json!({"id": home})), &["hint".to_string()])).unwrap().hint.is_none());
}

#[test]
fn keeps_and_clears_a_map_location() {
    let f = fixture();
    let home = f.create(json!({"name": "Home", "map_location": "1 Main St"}));

    assert_eq!(f.locations.get_location(&home).unwrap().map_location.as_deref(), Some("1 Main St"));
    let moved = f.held(|l| l.update_location(&location(json!({"id": home, "map_location": "2 Main St"})), &[])).unwrap();
    assert_eq!(moved.map_location.as_deref(), Some("2 Main St"));
    assert!(f.held(|l| l.update_location(&location(json!({"id": home})), &["map_location".to_string()])).unwrap().map_location.is_none());
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

    assert_eq!(f.held(|l| l.delete_location(&home, None)).unwrap().name.as_deref(), Some("Home"));
    let ids: Vec<Option<String>> = f.locations.all().unwrap().into_iter().map(|l| l.id).collect();
    assert_eq!(ids, vec![Some(studio.clone())]);
    refused(f.held(|l| l.delete_location("studio", None)), &format!("by its id: 'Studio' is {studio}"));
}

#[test]
fn suggests_close_matches() {
    let f = fixture();
    let studio = f.create(json!({"name": "Salsa studio"}));

    refused(f.locations.get_location("salsa studo"), &format!(r"did you mean {studio} \(Salsa studio\)"));
}

// -- location groups ---------------------------------------------------------------------------------

impl Fixture {
    fn group(&self, given: Value) -> String {
        self.held(|l| l.create_location_group(&serde_json::from_value(given).unwrap())).unwrap().created_id
    }
}

#[test]
fn groups_nest_and_give_their_locations_paths() {
    let f = fixture();
    let city = f.group(json!({"name": "New York"}));
    let borough = f.group(json!({"name": "Brooklyn", "group_id": city, "note": "across the bridge"}));
    let home = f.create(json!({"name": "Home", "group_id": borough}));
    let cafe = f.create(json!({"name": "Cafe"}));

    let groups = f.locations.get_location_groups().unwrap();
    assert_eq!(groups.iter().map(|g| g.path.as_str()).collect::<Vec<_>>(), ["New York", "New York › Brooklyn"]);
    assert_eq!(f.locations.get_location_group("BROOKLYN").unwrap().note.as_deref(), Some("across the bridge"));
    let listed = f.locations.listed_locations().unwrap();
    assert_eq!(listed.iter().map(|l| l.path.as_str()).collect::<Vec<_>>(), ["New York › Brooklyn › Home", "Cafe"]);
    assert_eq!(f.locations.listed_location(&home).unwrap().group_id.as_deref(), Some(borough.as_str()));
    // Ids are never shared between a location and a group.
    assert!(![&city, &borough].contains(&&cafe));
}

#[test]
fn a_location_is_only_in_a_group_that_is_there() {
    let f = fixture();
    refused(f.held(|l| l.create_location(&location(json!({"name": "Home", "group_id": "nowhere"})))), "isn't a location group");
    let home = f.create(json!({"name": "Home"}));
    let group = f.group(json!({"name": "Brooklyn"}));
    f.held(|l| l.update_location(&location(json!({"id": home, "group_id": group})), &[])).unwrap();
    let cleared = f.held(|l| l.update_location(&location(json!({"id": home})), &["group_id".to_string()])).unwrap();
    assert!(cleared.group_id.is_none());
}

#[test]
fn group_names_are_unique_and_groups_never_hold_themselves() {
    let f = fixture();
    let city = f.group(json!({"name": "New York"}));
    let borough = f.group(json!({"name": "Brooklyn", "group_id": city}));
    let group = |given: Value| serde_json::from_value::<LocationGroup>(given).unwrap();

    refused(f.held(|l| l.create_location_group(&group(json!({"name": "brooklyn"})))), "already a location group named 'Brooklyn'");
    refused(f.held(|l| l.create_location_group(&group(json!({"name": "Queens", "group_id": "nowhere"})))), "isn't a location group");
    refused(f.held(|l| l.update_location_group(&group(json!({"id": city, "group_id": borough})), &[])), "can't be inside itself");
    refused(f.held(|l| l.update_location_group(&group(json!({"id": city, "group_id": city})), &[])), "can't be inside itself");
    // A location may share a group's name.
    f.create(json!({"name": "Brooklyn"}));
    let moved = f.held(|l| l.update_location_group(&group(json!({"id": borough})), &["group_id".to_string()])).unwrap();
    assert_eq!(moved.path, "Brooklyn");
}

#[test]
fn deleting_a_group_moves_what_was_in_it_up() {
    let f = fixture();
    let city = f.group(json!({"name": "New York"}));
    let borough = f.group(json!({"name": "Brooklyn", "group_id": city}));
    let slope = f.group(json!({"name": "Park Slope", "group_id": borough}));
    let home = f.create(json!({"name": "Home", "group_id": borough}));
    f.create(json!({"name": "Cafe", "group_id": slope}));

    let deleted = f.held(|l| l.delete_location_group(&borough, None)).unwrap();

    assert_eq!(deleted.deleted.name.as_deref(), Some("Brooklyn"));
    assert_eq!(deleted.moved_groups.iter().map(|g| g.path.as_str()).collect::<Vec<_>>(), ["New York › Park Slope"]);
    assert_eq!(deleted.moved_locations.iter().map(|l| l.path.as_str()).collect::<Vec<_>>(), ["New York › Home"]);
    assert_eq!(f.locations.get_location(&home).unwrap().group_id.as_deref(), Some(city.as_str()));
    refused(f.held(|l| l.delete_location_group("Park Slope", None)), "Delete a location group by its id");
    refused(f.held(|l| l.delete_location_group("Brooklyn", None)), "no location group with the id or name 'Brooklyn'");
}
