//! The module's tests, against the fake Google.

use super::*;
use crate::google::sheets::SheetsClient;
use crate::testing::fake_google::FakeGoogle;
use crate::write_lock::WRITE_LOCK;
use serde_json::{json, Value};

struct Fixture {
    _fake: FakeGoogle,
    locations: Locations,
}

/// The modes the tests travel by: their ids as an action's or group's.
fn modes() -> Vec<String> {
    ["drive", "subway", "transit", "walk"].map(String::from).to_vec()
}

fn fixture() -> Fixture {
    let fake = FakeGoogle::start();
    let sheets = SheetsClient::new(fake.google());
    let _held = WRITE_LOCK.hold();
    let spreadsheet = sheets.create_spreadsheet("Calendar Metadata").unwrap();
    let locations = Locations::ensure(&sheets, &spreadsheet).unwrap();
    Fixture { _fake: fake, locations }
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

    fn location(&self, given: Value) -> String {
        self.held(|l| l.create_location(&serde_json::from_value(given).unwrap())).unwrap().created_id
    }

    fn group(&self, given: Value) -> String {
        self.held(|l| l.create_location_group(&serde_json::from_value(given).unwrap())).unwrap().created_id
    }

    fn travel(&self, given: Value) -> anyhow::Result<String> {
        self.held(|l| l.create_travel_time(&serde_json::from_value(given).unwrap(), &modes())).map(|c| c.created_id)
    }

    /// The places the estimate is between, and the minutes it gives for
    /// each of its times.
    fn estimate(&self, from: &str, to: &str, mode: Option<&[&str]>) -> Option<(String, String, Vec<(String, i64)>)> {
        let modes: Option<Vec<String>> = mode.map(|m| m.iter().map(|s| s.to_string()).collect());
        self.locations
            .estimate_travel_time(from, to, modes.as_deref())
            .unwrap()
            .map(|e| (e.from_id, e.to_id, e.times.into_iter().map(|t| (t.mode_id, t.minutes)).collect()))
    }
}

/// Home and the cafe, in Park Slope, in Brooklyn; work, in Manhattan.
struct Places {
    f: Fixture,
    home: String,
    cafe: String,
    work: String,
    slope: String,
    brooklyn: String,
    manhattan: String,
}

fn places() -> Places {
    let f = fixture();
    let brooklyn = f.group(json!({"name": "Brooklyn"}));
    let slope = f.group(json!({"name": "Park Slope", "group_id": brooklyn}));
    let manhattan = f.group(json!({"name": "Manhattan"}));
    let home = f.location(json!({"name": "Home", "group_id": slope}));
    let cafe = f.location(json!({"name": "Cafe", "group_id": slope}));
    let work = f.location(json!({"name": "Work", "group_id": manhattan}));
    Places { f, home, cafe, work, slope, brooklyn, manhattan }
}

#[test]
fn keeps_travel_times_between_locations_and_groups() {
    let p = places();
    let id = p
        .f
        .travel(json!({"from_id": p.home, "to_id": p.manhattan, "times": [{"mode_id": "subway", "minutes": 35, "max_minutes": 50}], "note": "the F"}))
        .unwrap();

    let listed = p.f.locations.get_travel_times(Some(&p.manhattan)).unwrap();
    assert_eq!(listed.len(), 1);
    assert_eq!(listed[0].times.as_ref().unwrap()[0].max_minutes, Some(50));
    assert!(p.f.locations.get_travel_times(Some(&p.cafe)).unwrap().is_empty());

    let update = |given: Value, clear: &[String]| p.f.held(|l| l.update_travel_time(&serde_json::from_value(given).unwrap(), clear, &modes()));
    let updated = update(json!({"id": id, "one_way": true}), &[]).unwrap();
    assert_eq!((updated.one_way, updated.note.as_deref()), (Some(true), Some("the F")));
    assert!(update(json!({"id": id}), &["note".to_string()]).unwrap().note.is_none());
    assert_eq!(p.f.held(|l| l.delete_travel_time(&id)).unwrap().id, Some(id.clone()));
    refused(p.f.held(|l| l.delete_travel_time(&id)), "no travel time with the id");
}

#[test]
fn refuses_what_cant_be_a_travel_time() {
    let p = places();
    let time = |mode: &str, minutes: i64| json!([{"mode_id": mode, "minutes": minutes}]);
    refused(p.f.travel(json!({"from_id": p.home, "to_id": "nowhere"})), "to_id 'nowhere' isn't a location or a location group");
    refused(p.f.travel(json!({"to_id": p.home})), "needs a from_id");
    refused(p.f.travel(json!({"from_id": p.home, "to_id": p.home})), "from a place to itself");
    refused(p.f.travel(json!({"from_id": p.home, "to_id": p.work, "times": time("fly", 5)})), "'fly' isn't an action or an action group");
    refused(p.f.travel(json!({"from_id": p.home, "to_id": p.work, "times": time("walk", 0)})), "must be more than 0");
    refused(
        p.f.travel(json!({"from_id": p.home, "to_id": p.work, "times": [{"mode_id": "walk", "minutes": 9}, {"mode_id": "walk", "minutes": 8}]})),
        "more than one time for 'walk'",
    );
    refused(
        p.f.travel(json!({"from_id": p.home, "to_id": p.work, "times": [{"mode_id": "walk", "minutes": 90, "max_minutes": 80}]})),
        "max_minutes for 'walk' is less than its minutes",
    );
}

#[test]
fn each_way_between_two_places_is_said_once() {
    let p = places();
    p.f.travel(json!({"from_id": p.home, "to_id": p.work})).unwrap();
    // Either way round, a second two-way one says the same.
    refused(p.f.travel(json!({"from_id": p.work, "to_id": p.home})), "between the same places as travel time");
    // A one-way one can say a way is different, once.
    p.f.travel(json!({"from_id": p.work, "to_id": p.home, "one_way": true})).unwrap();
    refused(p.f.travel(json!({"from_id": p.work, "to_id": p.home, "one_way": true})), "the same way");
    p.f.travel(json!({"from_id": p.home, "to_id": p.work, "one_way": true})).unwrap();
}

#[test]
fn estimates_from_the_nearest_places_that_say() {
    let p = places();
    let f = &p.f;
    f.travel(json!({"from_id": p.slope, "to_id": p.manhattan, "times": [{"mode_id": "transit", "minutes": 40}, {"mode_id": "drive", "minutes": 30}]}))
        .unwrap();
    f.travel(json!({"from_id": p.brooklyn, "to_id": p.work, "times": [{"mode_id": "transit", "minutes": 45}]})).unwrap();
    f.travel(json!({"from_id": p.home, "to_id": p.cafe, "times": [{"mode_id": "walk", "minutes": 5}]})).unwrap();

    // The places themselves, either way round.
    assert_eq!(f.estimate(&p.cafe, &p.home, None), Some((p.cafe.clone(), p.home.clone(), vec![("walk".into(), 5)])));
    // Else the groups they're in: Park Slope to Manhattan and Brooklyn to
    // Work are both two steps out, but the first is one step each side.
    assert_eq!(
        f.estimate(&p.home, &p.work, None),
        Some((p.slope.clone(), p.manhattan.clone(), vec![("transit".into(), 40), ("drive".into(), 30)]))
    );
    // Back the other way, the same two-way travel time.
    assert_eq!(f.estimate(&p.work, &p.home, None).unwrap().0, p.manhattan);
    // From work to Park Slope: Brooklyn's, one step out, before Manhattan's.
    assert_eq!(f.estimate(&p.work, &p.slope, None).unwrap().1, p.brooklyn);
    // A mode, by itself or its nearest group that has a time.
    assert_eq!(f.estimate(&p.home, &p.work, Some(&["drive"])).unwrap().2, vec![("drive".into(), 30)]);
    assert_eq!(f.estimate(&p.home, &p.work, Some(&["subway", "transit"])).unwrap().2, vec![("transit".into(), 40)]);
    // A mode nothing has a time for, between those places or theirs.
    assert_eq!(f.estimate(&p.home, &p.cafe, Some(&["drive"])), None);
    refused(f.locations.estimate_travel_time("nowhere", &p.home, None), "'nowhere' isn't a location or a location group");
}

#[test]
fn a_one_way_travel_time_beats_a_two_way_one() {
    let p = places();
    p.f.travel(json!({"from_id": p.home, "to_id": p.work, "times": [{"mode_id": "drive", "minutes": 30}]})).unwrap();
    p.f.travel(json!({"from_id": p.work, "to_id": p.home, "one_way": true, "times": [{"mode_id": "drive", "minutes": 50}]})).unwrap();

    assert_eq!(p.f.estimate(&p.home, &p.work, Some(&["drive"])).unwrap().2, vec![("drive".into(), 30)]);
    assert_eq!(p.f.estimate(&p.work, &p.home, Some(&["drive"])).unwrap().2, vec![("drive".into(), 50)]);
}

#[test]
fn deleting_a_place_deletes_its_travel_times() {
    let p = places();
    p.f.travel(json!({"from_id": p.home, "to_id": p.work})).unwrap();
    p.f.travel(json!({"from_id": p.cafe, "to_id": p.manhattan})).unwrap();
    p.f.travel(json!({"from_id": p.cafe, "to_id": p.brooklyn})).unwrap();

    p.f.held(|l| l.delete_location(&p.home)).unwrap();
    p.f.held(|l| l.delete_location_group(&p.manhattan)).unwrap();

    let left = p.f.locations.get_travel_times(None).unwrap();
    assert_eq!(left.iter().map(|t| t.to_id.clone().unwrap()).collect::<Vec<_>>(), [p.brooklyn.clone()]);
}
