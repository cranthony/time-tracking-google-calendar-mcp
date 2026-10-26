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
    // Seeded before any write: version 0, as anything saved before versions.
    let settled: Vec<Trait> = seed_traits().into_iter().map(settled).map(|t| Trait { version: Some(0), ..t }).collect();
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
    // Kept at version 1: a tool gives that (see rows::Written), the store
    // what it was given.
    let mut kept = json_of(&f.traits.all().unwrap()).last().cloned().unwrap();
    assert_eq!(kept["version"], json!(1));
    kept["version"] = Value::Null;
    assert_eq!(kept, created);
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
    let mut kept = f.by_id("generous");
    assert_eq!(kept["version"], json!(1));
    kept["version"] = updated["version"].clone();
    assert_eq!(kept, updated);
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
        with(with(with(judgment(), "engagement_type", json!("for")), "id", json!("judgment#2")), "facts", json!(["general_notes", "fact:interests"])),
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
        assert!(person_traits_problems(&spec, Some(&ids), SpecOf::Person).is_empty(), "{spec}");
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
        let problems = person_traits_problems(&spec, Some(&ids), SpecOf::Person);
        assert!(problems.first().is_some_and(|p| p.starts_with(problem)), "{spec}: {problems:?}");
    }
}

#[test]
fn part_keys_number_repeated_kinds() {
    assert_eq!(part_keys(&[json!({"kind": "count"}), json!({"kind": "judgment"}), json!({"kind": "count"})]), strings(&["count", "judgment", "count#2"]));
}

#[test]
fn a_schedule_part_takes_an_action_a_window_a_measure_and_its_days() {
    for part in [
        json!({"kind": "schedule", "action": "sleep", "from": "06:30", "to": "07:30", "measure": "ends"}),
        json!({"kind": "schedule", "action": "bed", "from": "22:30", "to": "00:30", "measure": "starts", "days": ["fri", "sat"], "window_days": 14}),
        json!({"kind": "schedule", "action": "work", "from": "10:00", "to": "18:00", "measure": "covered", "days": ["mon", "tue", "wed", "thu", "fri"], "slack_minutes": 60}),
        json!({"kind": "schedule", "action": "work", "from": "10:00", "to": "18:00", "measure": "covered", "slack_minutes": 0}),
    ] {
        assert!(part_problems(&part).is_empty(), "{part}: {:?}", part_problems(&part));
    }
    let part = |extra: Value| {
        let mut p = json!({"kind": "schedule", "action": "work", "from": "10:00", "to": "18:00", "measure": "covered"});
        p.as_object_mut().unwrap().extend(extra.as_object().unwrap().clone());
        p
    };
    let cases = [
        (json!({"kind": "schedule", "from": "06:30", "to": "07:30", "measure": "ends"}), "needs \"action\""),
        (json!({"kind": "schedule", "action": "sleep", "to": "07:30", "measure": "ends"}), "needs \"from\""),
        (json!({"kind": "schedule", "action": "sleep", "from": "06:30", "to": "07:30"}), "needs \"measure\""),
        (part(json!({"from": "6:30"})), "\"from\" must be a time of day"),
        (part(json!({"to": "24:00"})), "\"to\" must be a time of day"),
        (part(json!({"to": "10:00"})), "\"from\" and \"to\" must differ"),
        (part(json!({"measure": "during"})), "\"measure\" must be one of starts, ends, covered"),
        (part(json!({"days": ["monday"]})), "\"days\" must be a list of weekdays"),
        (part(json!({"days": []})), "\"days\" must be a list of weekdays"),
        (part(json!({"days": ["mon", "mon"]})), "\"days\" names a day more than once"),
        (part(json!({"window_days": 0})), "\"window_days\" must be a number above 0"),
        (part(json!({"measure": "starts", "slack_minutes": 10})), "\"slack_minutes\" is only for the \"covered\" measure"),
        (part(json!({"slack_minutes": -5})), "\"slack_minutes\" must be a number, 0 or more"),
        (part(json!({"slack_minutes": 480})), "\"slack_minutes\" must be less than its window's 480 minutes"),
        (part(json!({"max_gap_hours": 6})), "has no field \"max_gap_hours\"; a schedule part takes"),
    ];
    for (part, problem) in cases {
        let problems = part_problems(&part);
        assert!(problems.first().is_some_and(|p| p.starts_with(problem)), "{part}: {problems:?}");
    }
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

// -- facts ----------------------------------------------------------------------------------------

fn interests() -> Value {
    json!({"name": "Their interests", "summary_prompt": "What they like doing.", "log_prompt": "What new interest showed."})
}

#[test]
fn the_seeded_thoughtful_judges_from_its_own_what_matters_fact() {
    let f = fixture();
    let thoughtful = f.by_id("thoughtful");
    assert_eq!(thoughtful["facts"][0]["id"], json!("what_matters"));
    assert_eq!(thoughtful["parts"][0]["facts"], json!(["action", "general_notes", "fact:what_matters"]));
    let seeded = seed_traits();
    assert!(seeded.iter().all(|t| trait_problems(t).is_empty()), "{:?}", seeded.iter().map(trait_problems).collect::<Vec<_>>());
}

#[test]
fn a_new_fact_gets_an_id_from_its_name_and_parts_can_name_it() {
    let f = fixture();
    let part = with(judgment(), "facts", json!(["action", "fact:their_interests"]));
    let made = f.held(|t| t.create_trait(&given(json!({"name": "Curious", "parts": [part], "facts": [interests(), interests()]})))).unwrap_err();
    assert!(made.to_string().contains("its facts' name 'Their interests' is used more than once"), "{made}");

    let made = f
        .held(|t| t.create_trait(&given(json!({"name": "Curious", "parts": [with(judgment(), "facts", json!(["fact:their_interests"]))], "facts": [interests()]}))))
        .unwrap();
    assert_eq!(made["facts"][0]["id"], json!("their_interests"));
    assert_eq!(made["facts"][0]["summary_prompt"], json!("What they like doing."));

    // A second one of the same name is told apart.
    assert_eq!(with_new_fact_ids(&[given_fact(json!({"id": "their_interests"})), given_fact(interests())])[1]["id"], json!("their_interests_2"));
}

fn given_fact(value: Value) -> Map<String, Value> {
    value.as_object().unwrap().clone()
}

#[test]
fn a_part_cant_name_a_fact_its_trait_doesnt_have() {
    let f = fixture();
    refused(
        f.held(|t| t.create_trait(&given(json!({"name": "Curious", "parts": [with(judgment(), "facts", json!(["fact:hobbies"]))]})))),
        r#"part 1 \(judgment\) "facts" names 'fact:hobbies', which isn't one of its trait's facts \(it has none\)"#,
    );
    // Nor can a fact a part names be let go of.
    let thoughtful = f.by_id("thoughtful");
    let mut change = json!({"id": "thoughtful", "version": thoughtful["version"], "facts": []});
    refused(f.held(|t| t.update_trait(&given(change.clone()), &[])), "isn't one of its trait's facts");
    change["facts"] = json!([thoughtful["facts"][0].clone(), interests()]);
    let updated = f.update(change);
    assert_eq!(updated["facts"][1]["id"], json!("their_interests"));
}

#[test]
fn refuses_what_isnt_a_fact() {
    let f = fixture();
    let cases = [
        (json!({"name": "X"}), "fact 1 needs a \"summary_prompt\""),
        (json!({"summary_prompt": "x"}), "fact 1 needs a \"name\""),
        (json!({"id": "bad id", "name": "X", "summary_prompt": "x"}), "fact 1's \"id\" must be text"),
        (json!({"name": "X", "summary_prompt": "x", "log_prompt": ""}), "fact 1's \"log_prompt\" must be non-empty text"),
        (json!({"name": "X", "summary_prompt": "x", "rubric": "y"}), "fact 1 has no field \"rubric\""),
    ];
    for (fact, message) in cases {
        let error = f.held(|t| t.create_trait(&given(json!({"name": "Curious", "parts": [judgment()], "facts": [fact]})))).unwrap_err();
        assert!(error.to_string().contains(message), "{message}: {error}");
    }
}


// -- a person's own fact prompts -------------------------------------------------------------------

fn worries_trait() -> Trait {
    given(json!({"id": "caring", "name": "Caring", "status": "active", "parts": [judgment()], "facts": [
        {"id": "worries", "name": "Worries", "summary_prompt": "What weighs on them.", "log_prompt": "What new worry showed."},
        {"id": "joys", "name": "Joys", "summary_prompt": "What delights them."}
    ]}))
}

fn spec_map(value: Value) -> Map<String, Value> {
    value.as_object().unwrap().clone()
}

#[test]
fn a_persons_own_fact_prompts_are_checked_for_their_shape() {
    let ids = strings(&["caring", "reliable"]);
    let fine = json!({"select": ["reliable"], "facts": {"caring": {"worries": {
        "summary_prompt": {"text": "Their health, mostly.", "how": "replace"},
        "log_prompt": {"text": "Say who told me."}
    }}}});
    // Kept for a trait that doesn't apply to them, too.
    assert!(person_traits_problems(&fine, Some(&ids), SpecOf::Person).is_empty());
    let long = "x".repeat(MAX_PROMPT_CHARS + 1);
    for (facts, problem) in [
        (json!([]), "\"facts\" must be {trait id"),
        (json!({"kind": {}}), "\"facts\" names 'kind', which isn't a trait"),
        (json!({"caring": {"worries": "x"}}), "\"facts\" for 'caring''s 'worries' must be"),
        (json!({"caring": {"worries": {"rubric": {}}}}), "\"facts\" for 'caring''s 'worries' has no field \"rubric\""),
        (json!({"caring": {"worries": {"summary_prompt": {"text": " "}}}}), "\"facts\" for 'caring''s 'worries''s \"summary_prompt\" needs \"text\""),
        (json!({"caring": {"worries": {"summary_prompt": {"text": "x", "how": "prepend"}}}}), "\"facts\" for 'caring''s 'worries''s \"summary_prompt\" \"how\" must be"),
        (json!({"caring": {"worries": {"log_prompt": {"text": long}}}}), "\"facts\" for 'caring''s 'worries''s \"log_prompt\" text is longer"),
    ] {
        let problems = person_traits_problems(&json!({"facts": facts}), Some(&ids), SpecOf::Person);
        assert!(problems.first().is_some_and(|p| p.starts_with(problem)), "{facts}: {problems:?}");
    }
    // A habit has none of its own.
    let problems = person_traits_problems(&fine, Some(&ids), SpecOf::Habit);
    assert!(problems.first().is_some_and(|p| p.contains("only a person has prompts of their own")), "{problems:?}");
}

#[test]
fn a_fact_its_trait_doesnt_have_is_refused_when_given_and_let_go_of_when_kept() {
    let traits = vec![worries_trait()];
    let stale = spec_map(json!({"facts": {"caring": {"gone": {"summary_prompt": {"text": "x", "how": "replace"}}}}}));
    let problems = fact_override_problems(&stale, None, &traits);
    assert_eq!(problems, vec!["\"facts\" for 'caring' names fact 'gone', which it doesn't have (worries, joys)"]);
    // Kept as it was, it isn't refused, and is let go of.
    assert!(fact_override_problems(&stale, Some(&stale), &traits).is_empty());
    assert_eq!(settled_fact_overrides(&stale, Some(&traits)), Map::new());
    // Settled, each prompt says how, and empty ones are dropped.
    let given = spec_map(json!({"select": "all", "facts": {"caring": {"worries": {"summary_prompt": {"text": "x"}}, "joys": {}}, "reliable": {}}}));
    assert_eq!(
        Value::Object(settled_fact_overrides(&given, None)),
        json!({"select": "all", "facts": {"caring": {"worries": {"summary_prompt": {"text": "x", "how": "replace"}}}}})
    );
}

#[test]
fn a_persons_prompts_replace_or_follow_the_traits() {
    let t = worries_trait();
    let (worries, joys) = (t.fact("worries").unwrap(), t.fact("joys").unwrap());
    let spec = json!({"facts": {"caring": {
        "worries": {"summary_prompt": {"text": "Their health.", "how": "append"}, "log_prompt": {"text": "Who told me.", "how": "replace"}},
        "joys": {"log_prompt": {"text": "Say when.", "how": "append"}}
    }}});
    assert_eq!(
        effective_prompts(worries, Some(&spec), "caring"),
        (("What weighs on them.\n\nTheir health.".to_string(), PROMPT_BOTH), Some(("Who told me.".to_string(), PROMPT_THEIR_OWN)))
    );
    // The trait has no log prompt: theirs is all there is.
    assert_eq!(
        effective_prompts(joys, Some(&spec), "caring"),
        (("What delights them.".to_string(), PROMPT_THE_TRAITS), Some(("Say when.".to_string(), PROMPT_THEIR_OWN)))
    );
    assert_eq!(effective_prompts(joys, None, "caring"), (("What delights them.".to_string(), PROMPT_THE_TRAITS), None));
}
