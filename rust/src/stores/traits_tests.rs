//! The module's tests, against the fake Google.

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
    serde_json::from_value(value).unwrap()
}

fn json_of(traits: &[Trait]) -> Vec<Value> {
    traits.iter().map(to_value).collect()
}

fn to_value<T: serde::Serialize>(value: T) -> Value {
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
    fn held<T: serde::Serialize>(&self, f: impl FnOnce(&Traits) -> anyhow::Result<T>) -> anyhow::Result<Value> {
        let _held = WRITE_LOCK.hold();
        f(&self.traits).map(to_value)
    }

    fn update(&self, value: Value) -> Value {
        self.held(|t| t.update_trait(&given(value), &[])).unwrap()
    }

    fn by_id(&self, id: &str) -> Value {
        self.traits.by_id().unwrap().into_iter().find(|(k, _)| k == id).map(|(_, t)| to_value(t)).unwrap()
    }

    fn listed_ids(&self, statuses: Option<&[&str]>) -> Vec<Value> {
        let statuses = statuses.map(strings);
        self.traits.get_traits(statuses.as_deref()).unwrap().iter().map(|t| to_value(t)["id"].clone()).collect()
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
    // Their parts' ids are as they were named before parts had ids.
    let settled: Vec<Trait> = seed_traits().into_iter().map(settled).collect();
    assert_eq!(json_of(&all), json_of(&settled));
    let ids: Vec<&Value> = all[0].parts.iter().flatten().map(|p| &p["id"]).collect();
    assert_eq!(ids, [&json!("judgment"), &json!("judgment#2")]);
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

    assert_eq!(again.by_id().unwrap().into_iter().find(|(k, _)| k == "creative").unwrap().1.status, Some(TraitStatus::Off));
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
fn a_cell_that_isnt_a_list_of_parts_fails_the_tab() {
    let f = fixture();
    f.fake.set_cell(&f.spreadsheet, TRAITS.1, 1, 4, "not json");
    f.fake.set_cell(&f.spreadsheet, TRAITS.1, 2, 2, "Retired");

    refused(f.traits.get_traits(None), r"The Traits tab has cells that can't be read: row 2, column parts: 'not json' isn't valid JSON; row 3, column status: 'Retired' doesn't fit");
}

#[test]
fn a_hand_edited_trait_shows_its_problems() {
    let f = fixture();
    f.fake.set_cell(&f.spreadsheet, TRAITS.1, 1, 4, r#"[{"kind": "count", "target": 0}]"#);

    let listed = to_value(f.traits.get_traits(None).unwrap());

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
    assert_eq!(updated["parts"], json!(settled(seed_traits().remove(4)).parts));
    assert_eq!(f.by_id("generous"), updated);
}

#[test]
fn new_parts_replace_the_old_whole() {
    let f = fixture();

    f.update(json!({"id": "reliable", "parts": [{"kind": "follow_through", "penalty": 10}]}));

    let parts = f.by_id("reliable")["parts"].clone();
    let id = parts[0]["id"].as_str().unwrap().to_string();
    assert_eq!(id.len(), 6);
    assert_eq!(parts, json!([{"id": id, "kind": "follow_through", "penalty": 10}]));
}

#[test]
fn a_part_keeps_its_id_and_a_new_one_gets_its_own() {
    let f = fixture();
    let kept = json!({"id": "count", "kind": "count", "target": 2});

    f.update(json!({"id": "reliable", "parts": [{"kind": "continuity"}, kept, {"kind": "count", "target": 1}]}));

    let ids: Vec<String> = f.by_id("reliable")["parts"].as_array().unwrap().iter().map(|p| p["id"].as_str().unwrap().to_string()).collect();
    assert_eq!(ids[1], "count");
    // New parts never take an id a part had before, so they start with
    // none of its judgments.
    assert!(ids[0] != "continuity" && ids[2] != "count#2" && ids[0] != ids[2], "{ids:?}");
}

#[test]
fn weights_are_kept_in_its_scoring_and_go_with_their_parts() {
    let f = fixture();
    let scoring = json!({"statistic": "percentile", "percentile": 25, "weights": {"follow_through": 2, "count": 0.5}});
    f.update(json!({"id": "reliable", "scoring": scoring}));
    assert_eq!(f.by_id("reliable")["scoring"], scoring);

    // count let go, its weight goes with it.
    f.update(json!({"id": "reliable", "parts": [{"id": "follow_through", "kind": "follow_through"}]}));
    assert_eq!(f.by_id("reliable")["scoring"], json!({"statistic": "percentile", "percentile": 25, "weights": {"follow_through": 2}}));

    f.held(|t| t.update_trait(&given(json!({"id": "reliable"})), &strings(&["scoring"]))).unwrap();
    assert!(f.by_id("reliable")["scoring"].is_null());
}

#[test]
fn refuses_scoring_that_isnt_well_formed() {
    let f = fixture();
    refused(
        f.held(|t| t.update_trait(&given(json!({"id": "reliable", "scoring": {"weights": {"judgment": 2}}})), &[])),
        r#"its scoring: "weights" names 'judgment', which isn't one of its parts \(by id: continuity, follow_through, count\)"#,
    );
    refused(
        f.held(|t| t.update_trait(&given(json!({"id": "reliable", "parts": [{"kind": "count", "target": 1, "weight": 2}]})), &[])),
        r#"has no field "weight": a part's weight is set in its trait's scoring"#,
    );
    refused(
        f.held(|t| t.update_trait(&given(json!({"id": "reliable", "parts": [{"id": "a", "kind": "count", "target": 1}, {"id": "a", "kind": "follow_through"}]})), &[])),
        "its parts' id 'a' is used more than once",
    );
    refused(
        f.held(|t| t.update_trait(&given(json!({"id": "creative", "parts": [with(judgment(), "scoring", json!({"weight_by": "hours"}))]})), &[])),
        r#"part 1 \(judgment\) "scoring": "weight_by" must be "event" or "duration""#,
    );
}

#[test]
fn a_trait_kept_before_parts_had_ids_is_settled_when_read() {
    let t = crate::testing::app::test_app();
    let parts = json!([
        {"kind": "judgment", "rubric": "R", "ratings": {"0": "a", "1": "b"}, "facts": [], "weight": 2},
        {"kind": "judgment", "rubric": "S", "ratings": {"0": "a", "1": "b"}, "facts": [], "weight": 1},
        {"kind": "count", "target": 1, "weight": 0},
    ]);
    let traits = t.app.traits().unwrap();
    t.seed(TRAITS.1, &[json!({"id": "kind", "name": "Kind", "status": "active", "parts": parts})]);

    let all = traits.all().unwrap();

    let ids: Vec<&Value> = all[0].parts.iter().flatten().map(|p| &p["id"]).collect();
    assert_eq!(ids, [&json!("judgment"), &json!("judgment#2"), &json!("count")]);
    assert!(all[0].parts.iter().flatten().all(|p| !p.contains_key("weight")));
    assert_eq!(json!(all[0].scoring), json!({"weights": {"judgment": 2, "count": 0}}));
    assert!(trait_problems(&all[0]).is_empty(), "{:?}", trait_problems(&all[0]));
}

#[test]
fn part_keys_pass_over_ids_already_taken() {
    let parts = [json!({"kind": "judgment"}), json!({"kind": "judgment", "id": "judgment"}), json!({"kind": "count", "id": "x1"})];
    assert_eq!(part_keys(&parts), ["judgment#2", "judgment", "x1"]);
}

#[test]
fn clears_the_definition() {
    let f = fixture();

    f.held(|t| t.update_trait(&given(json!({"id": "creative"})), &strings(&["definition"]))).unwrap();

    assert!(f.by_id("creative")["definition"].is_null());
}

#[test]
fn update_refuses_what_it_cant_do() {
    // A status that isn't one is refused by the tool's schema.
    let cases: [(Value, &[&str], &str); 5] = [
        (json!({"id": "nope", "name": "X"}), &[], "'nope' isn't a trait; the traits are thoughtful"),
        (json!({"id": "creative", "parts": []}), &[], "needs parts"),
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
        with(with(with(judgment(), "engagement_type", json!("for")), "id", json!("judgment#2")), "facts", json!(["general_notes", "person_notes"])),
        with(judgment(), "scoring", json!({"weight_by": "duration", "statistic": "percentile", "percentile": 50, "limit": 4.5, "limit_by": "best"})),
        with(judgment(), "facts", json!([])),
        json!({"kind": "continuity", "last_within_days": 30, "next_within_days": 7, "engagement_type": "with"}),
        json!({"kind": "count", "target": 1, "interval_days": 14, "zero_at_days": 30, "action": "a1b2c3"}),
        json!({"kind": "duration", "target_min": 120, "engagement_type": "for", "id": "a1b2c3"}),
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
        (json!({"kind": "follow_through", "weight": 2}), "has no field \"weight\": a part's weight is set in its trait's scoring"),
        (json!({"kind": "follow_through", "id": "a b"}), "\"id\" must be text of at most 30 letters"),
        (json!({"kind": "count", "target": 1, "scoring": {}}), "has no field \"scoring\"; a count part takes"),
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
        json!({"scoring": {"limit": 2, "limit_by": "worst"}, "part_scoring": {"reliable": {"weights": {"count": 2}}}}),
        json!({"parts": {"creative": [with(judgment(), "id", json!("mine"))]}, "part_scoring": {"creative": {"weights": {"mine": 3}}}}),
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

#[test]
fn a_spacing_part_takes_an_action_a_gap_and_exceptions() {
    for part in [
        json!({"kind": "spacing", "action": "meals", "max_gap_hours": 6}),
        json!({"kind": "spacing", "action": "meals", "max_gap_hours": 6, "exceptions": [{"actions": ["sleep"], "reset_hours": 1}, {"actions": ["fly", "train"]}]}),
        json!({"kind": "spacing", "action": "meals", "max_gap_hours": 4.5, "exceptions": [{"actions": ["sleep"], "reset_hours": 0}]}),
    ] {
        assert!(part_problems(&part).is_empty(), "{part}: {:?}", part_problems(&part));
    }
    let cases = [
        (json!({"kind": "spacing", "max_gap_hours": 6}), "needs \"action\""),
        (json!({"kind": "spacing", "action": "meals"}), "needs \"max_gap_hours\""),
        (json!({"kind": "spacing", "action": "meals", "max_gap_hours": 0}), "\"max_gap_hours\" must be a number above 0"),
        (json!({"kind": "spacing", "action": "meals", "max_gap_hours": 6, "exceptions": {"actions": ["sleep"]}}), "\"exceptions\" must be a list"),
        (json!({"kind": "spacing", "action": "meals", "max_gap_hours": 6, "exceptions": ["sleep"]}), "exception 0 must be an object"),
        (json!({"kind": "spacing", "action": "meals", "max_gap_hours": 6, "exceptions": [{"actions": []}]}), "exception 0's \"actions\" must be a list"),
        (json!({"kind": "spacing", "action": "meals", "max_gap_hours": 6, "exceptions": [{"actions": ["sleep"], "reset_hours": -1}]}), "exception 0's \"reset_hours\" must be"),
        (json!({"kind": "spacing", "action": "meals", "max_gap_hours": 6, "exceptions": [{"actions": ["sleep"], "reset": 1}]}), "exception 0 has no field \"reset\""),
        (json!({"kind": "spacing", "action": "meals", "max_gap_hours": 6, "window_days": 7}), "has no field \"window_days\"; a spacing part takes"),
    ];
    for (part, problem) in cases {
        let problems = part_problems(&part);
        assert!(problems.first().is_some_and(|p| p.starts_with(problem)), "{part}: {problems:?}");
    }
}
