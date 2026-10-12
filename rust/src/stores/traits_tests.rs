//! tests/test_traits.py, against the fake Google.

use super::*;
use crate::sheet::TRAITS;
use crate::testing::fake_google::FakeGoogle;
use crate::write_lock::WRITE_LOCK;

struct Fixture {
    fake: FakeGoogle,
    sheets: SheetsClient,
    spreadsheet: String,
    traits: Traits,
}

fn fixture() -> Fixture {
    let fake = FakeGoogle::start();
    let sheets = SheetsClient::new(fake.google());
    let _held = WRITE_LOCK.hold();
    let spreadsheet = sheets.create_spreadsheet("Calendar Metadata").unwrap();
    let traits = Traits::ensure(&sheets, &spreadsheet).unwrap();
    Fixture { fake, sheets, spreadsheet, traits }
}

fn given(value: Value) -> Trait {
    Trait::from_value(&value)
}

fn json_of(traits: &[Trait]) -> Vec<Value> {
    traits.iter().map(|t| Value::Object(t.to_json())).collect()
}

fn strings(items: &[&str]) -> Vec<String> {
    crate::testing::strings(items)
}

fn refused<T>(result: anyhow::Result<T>, pattern: &str) {
    let Err(error) = result else { panic!("{pattern:?} wasn't refused") };
    assert!(regex::Regex::new(pattern).unwrap().is_match(&error.to_string()), "{pattern:?} isn't in: {error}");
}

impl Fixture {
    fn held<T>(&self, f: impl FnOnce(&Traits) -> anyhow::Result<T>) -> anyhow::Result<T> {
        let _held = WRITE_LOCK.hold();
        f(&self.traits)
    }

    fn update(&self, value: Value) -> Value {
        self.held(|t| t.update_trait(&given(value), &[])).unwrap()
    }

    fn by_id(&self, id: &str) -> Value {
        self.traits.by_id().unwrap().into_iter().find(|(k, _)| k == id).map(|(_, t)| Value::Object(t.to_json())).unwrap()
    }

    fn listed_ids(&self, statuses: Option<&[&str]>) -> Vec<Value> {
        let statuses = statuses.map(strings);
        self.traits.get_traits(statuses.as_deref()).unwrap().as_array().unwrap().iter().map(|t| t["id"].clone()).collect()
    }
}

fn judgment() -> Value {
    json!({
        "kind": "judgment",
        "rubric": "Was this activity or place new?",
        "ratings": {"0": "routine", "1": "a twist", "2": "new", "3": "adventurous"},
        "facts": ["action", {"fact": "location_history", "lookback_days": 90}],
    })
}

fn with(mut part: Value, key: &str, value: Value) -> Value {
    part[key] = value;
    part
}

// -- the tab ----------------------------------------------------------------------------

#[test]
fn a_new_tab_is_seeded_with_the_five_starting_traits() {
    let f = fixture();

    let all = f.traits.all().unwrap();

    let ids: Vec<Value> = json_of(&all).iter().map(|t| t["id"].clone()).collect();
    assert_eq!(ids, vec![json!("thoughtful"), json!("reliable"), json!("creative"), json!("adventurous"), json!("generous")]);
    assert_eq!(json_of(&all), json_of(&seed_traits()));
    assert!(f.fake.tab_titles(&f.spreadsheet).contains(&TRAITS.1.to_string()));
}

#[test]
fn an_existing_tab_isnt_seeded_again() {
    let f = fixture();
    f.update(json!({"id": "creative", "status": "off"}));

    let again = {
        let _held = WRITE_LOCK.hold();
        Traits::ensure(&f.sheets, &f.spreadsheet).unwrap()
    };

    assert_eq!(again.by_id().unwrap().into_iter().find(|(k, _)| k == "creative").unwrap().1.status_str(), Some("off"));
    assert_eq!(again.all().unwrap().len(), 5);
}

#[test]
fn the_seeds_are_well_formed() {
    assert!(seed_traits().iter().all(|t| trait_problems(t).is_empty()));
}

// -- listing ------------------------------------------------------------------------------

#[test]
fn lists_active_and_off_traits_by_default() {
    let f = fixture();
    f.update(json!({"id": "creative", "status": "off"}));
    f.update(json!({"id": "adventurous", "status": "archived"}));

    assert_eq!(f.listed_ids(None), vec![json!("thoughtful"), json!("reliable"), json!("creative"), json!("generous")]);
    assert_eq!(f.listed_ids(Some(&["archived"])), vec![json!("adventurous")]);
}

#[test]
fn a_hand_edited_trait_shows_its_problems() {
    let f = fixture();
    f.fake.set_cell(&f.spreadsheet, TRAITS.1, 1, 4, r#"[{"kind": "count", "target": 0}]"#);

    let listed = f.traits.get_traits(None).unwrap();

    assert_eq!(listed[0]["problems"], json!(["part 1 (count) \"target\" must be a number above 0"]));
    assert!(listed[1]["problems"].is_null());
}

#[test]
fn refuses_an_unknown_status() {
    refused(fixture().traits.get_traits(Some(&strings(&["gone"]))), "Unknown trait status");
}

// -- creating --------------------------------------------------------------------------------

#[test]
fn gets_an_id_from_its_name_and_is_active() {
    let f = fixture();

    let created = f.held(|t| t.create_trait(&given(json!({"name": "Playful Spirit", "parts": [judgment()]})))).unwrap();

    assert_eq!((created["id"].clone(), created["status"].clone()), (json!("playful-spirit"), json!("active")));
    assert_eq!(json_of(&f.traits.all().unwrap()).last(), Some(&created));
}

#[test]
fn a_taken_id_gets_a_number() {
    let f = fixture();

    let created = f.held(|t| t.create_trait(&given(json!({"name": "Thoughtful!", "parts": [judgment()]})))).unwrap();

    assert_eq!(created["id"], "thoughtful-2");
}

#[test]
fn refuses_a_name_already_used() {
    refused(fixture().held(|t| t.create_trait(&given(json!({"name": "generous", "parts": [judgment()]})))), "There's already a trait named 'Generous'");
}

#[test]
fn refuses_a_bad_part_saying_why() {
    let f = fixture();

    refused(
        f.held(|t| t.create_trait(&given(json!({"name": "Kind", "parts": [{"kind": "judgment", "rubric": "R", "ratings": {"0": "a", "1": "b"}}]})))),
        r#"The trait 'Kind': part 1 \(judgment\) needs "facts""#,
    );

    assert_eq!(f.traits.all().unwrap().len(), 5);
}

// -- updating ---------------------------------------------------------------------------------

#[test]
fn renames_and_rewords_keeping_its_id_and_parts() {
    let f = fixture();

    let updated = f.update(json!({"id": "generous", "name": "Giving", "definition": "Give."}));

    assert_eq!((updated["id"].clone(), updated["name"].clone(), updated["definition"].clone()), (json!("generous"), json!("Giving"), json!("Give.")));
    assert_eq!(updated["parts"], seed_traits()[4].parts.clone().unwrap());
    assert_eq!(f.by_id("generous"), updated);
}

#[test]
fn new_parts_replace_the_old_whole() {
    let f = fixture();

    f.update(json!({"id": "reliable", "parts": [{"kind": "follow_through", "weight": 2}]}));

    assert_eq!(f.by_id("reliable")["parts"], json!([{"kind": "follow_through", "weight": 2}]));
}

#[test]
fn clears_the_definition() {
    let f = fixture();

    f.held(|t| t.update_trait(&given(json!({"id": "creative"})), &strings(&["definition"]))).unwrap();

    assert!(f.by_id("creative")["definition"].is_null());
}

#[test]
fn update_refuses_what_it_cant_do() {
    let cases: [(Value, &[&str], &str); 6] = [
        (json!({"id": "nope", "name": "X"}), &[], "'nope' isn't a trait; the traits are thoughtful"),
        (json!({"id": "creative", "parts": []}), &[], "needs parts"),
        (json!({"id": "creative", "status": "gone"}), &[], "its status must be one of active, off, archived"),
        (json!({"id": "creative"}), &["name"], "Can't clear"),
        (json!({"id": "creative", "definition": "x"}), &["definition"], "Can't both set and clear"),
        (json!({"name": "x"}), &[], "needs the trait's id"),
    ];
    let f = fixture();

    for (value, clear, message) in cases {
        refused(f.held(|t| t.update_trait(&given(value), &strings(clear))), message);
    }
}

// -- parts ---------------------------------------------------------------------------------------

#[test]
fn accepts_every_kind_of_part() {
    let parts = [
        judgment(),
        with(with(with(judgment(), "engagement_type", json!("for")), "weight", json!(0)), "facts", json!(["general_notes", "person_notes"])),
        with(judgment(), "facts", json!([])),
        json!({"kind": "continuity", "last_within_days": 30, "next_within_days": 7, "engagement_type": "with"}),
        json!({"kind": "count", "target": 1, "interval_days": 14, "zero_at_days": 30, "action": "a1b2c3"}),
        json!({"kind": "duration", "target_min": 120, "engagement_type": "for", "weight": 2.5}),
        json!({"kind": "follow_through", "penalty": 20}),
    ];

    for part in parts {
        assert!(part_problems(&part).is_empty(), "{part}: {:?}", part_problems(&part));
    }
}

#[test]
fn refuses_what_isnt_a_part() {
    let cases = [
        (json!("judgment"), "must be an object with a \"kind\""),
        (json!({"kind": "prep"}), "has kind 'prep'; a part's kind is one of judgment, continuity, count, duration"),
        (json!({"kind": "count", "target": 1, "events_of": "g1"}), "has no field \"events_of\"; a count part takes"),
        (json!({"kind": "follow_through", "weight": -1}), "\"weight\" must be a number, 0 or more"),
        (json!({"kind": "continuity", "engagement_type": "at"}), "\"engagement_type\" must be \"with\" or \"for\""),
        (json!({"kind": "count", "target": 0}), "\"target\" must be a number above 0"),
        (json!({"kind": "count", "target": 1, "interval_days": 30, "zero_at_days": 14}), "\"zero_at_days\" must be"),
        (json!({"kind": "count", "target": 1, "action": ""}), "\"action\" must be an action or action group id"),
        (json!({"kind": "follow_through", "penalty": 101}), "\"penalty\" must be a number from 0 to 100"),
        (with(judgment(), "rubric", json!(" ")), "\"rubric\" must be non-empty text"),
        (with(judgment(), "ratings", json!({"0": "only one"})), "\"ratings\" must be an object of at least two ratings"),
        (with(judgment(), "ratings", json!({"low": "a", "high": "b"})), "\"ratings\" must be an object"),
        (with(judgment(), "facts", json!("action")), "\"facts\" must be a list of facts"),
        (with(judgment(), "facts", json!(["mood"])), "\"facts\" has 'mood'; a fact is one of action, action_history"),
        (with(judgment(), "facts", json!([{"fact": "action", "lookback_days": 7}])), "\"facts\": only action_history and"),
        (with(judgment(), "facts", json!([{"fact": "action_history", "lookback_days": 0}])), "\"facts\": action_history's"),
        (with(judgment(), "facts", json!([{"fact": "location", "days": 1}])), "\"facts\": location has no field 'days'"),
        (with(judgment(), "facts", json!(["action", {"fact": "action"}])), "\"facts\" names action more than once"),
    ];

    for (part, problem) in cases {
        let problems = part_problems(&part);
        assert!(problems.first().is_some_and(|p| p.starts_with(problem)), "{part}: {problems:?}");
    }
}

#[test]
fn judgment_scale_and_fact_lookbacks() {
    assert_eq!(judgment_scale(&judgment()), 3);
    let part = with(judgment(), "facts", json!(["action", "action_history", {"fact": "location_history", "lookback_days": 90}]));
    assert_eq!(fact_lookbacks(&part), vec![("action".to_string(), 0), ("action_history".to_string(), 30), ("location_history".to_string(), 90)]);
}

#[test]
fn accepts_valid_person_trait_specs() {
    let ids = strings(&["reliable", "creative"]);
    for spec in [
        json!({}),
        json!({"select": "all"}),
        json!({"select": ["reliable"], "parts": {"reliable": [{"kind": "count", "target": 1, "interval_days": 21}]}}),
        json!({"parts": {"creative": [judgment()]}}),
    ] {
        assert!(person_traits_problems(&spec, Some(&ids)).is_empty(), "{spec}");
    }
}

#[test]
fn refuses_invalid_person_trait_specs() {
    let ids = strings(&["reliable", "creative"]);
    let cases = [
        (json!([]), "must be an object"),
        (json!({"traits": "all"}), "has no field \"traits\""),
        (json!({"select": "some"}), "\"select\" must be \"all\" or a list of trait ids"),
        (json!({"select": ["kind"]}), "\"select\" names 'kind', which isn't a trait"),
        (json!({"select": ["reliable"], "parts": {"creative": [judgment()]}}), "\"parts\" names 'creative', which isn't selected"),
        (json!({"parts": {"reliable": []}}), "\"parts\" for 'reliable' must be a list of at least one part"),
        (json!({"parts": {"reliable": [{"kind": "count"}]}}), "\"parts\" for 'reliable': part 1 (count) needs \"target\""),
        (json!({"parts": []}), "\"parts\" must be {trait id: [parts]}"),
    ];

    for (spec, problem) in cases {
        let problems = person_traits_problems(&spec, Some(&ids));
        assert!(problems.first().is_some_and(|p| p.starts_with(problem)), "{spec}: {problems:?}");
    }
}

#[test]
fn part_keys_number_repeated_kinds() {
    assert_eq!(part_keys(&[json!({"kind": "count"}), json!({"kind": "judgment"}), json!({"kind": "count"})]), strings(&["count", "judgment", "count#2"]));
}
