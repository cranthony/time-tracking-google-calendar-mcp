//! TestSheetReadRequests, TestFacts and TestAdditions.

use super::*;
use crate::testing::facts;

// -- sheet read requests ----------------------------------------------------------------
// Google Sheets throttles read requests to 60 a minute per user, and a
// compaction step that runs out waits for the quota to roll over -- so
// every read request a compaction makes counts. This pins how many a
// typical round makes, running the tools end to end on the fake Google.
// If a count goes up, find a way not to (a hint, or folding the read into
// one already made); if one goes down, lower it here.

#[test]
fn a_typical_round_of_compaction_makes_no_more_read_requests_than_this() {
    let t = test_app();
    for event in super::prepare::window_events() {
        t.seed_event(&event);
    }
    let call = |name: &str, arguments: Value| -> (Value, usize) {
        let before = t.fake.requests().iter().filter(|r| r.path.ends_with("values:batchGetByDataFilter")).count();
        let outcome = match crate::tools::run(&t.app, name, arguments) {
            Ok(outcome) => outcome,
            Err(crate::tools::ToolError::Message(message)) => panic!("{name}: {message}"),
            Err(crate::tools::ToolError::Unexpected(error)) => panic!("{name}: {error:#}"),
            Err(_) => panic!("{name}: refused"),
        };
        let after = t.fake.requests().iter().filter(|r| r.path.ends_with("values:batchGetByDataFilter")).count();
        (outcome.0, after - before)
    };
    let round = |note_at: &str, now: &str| -> Vec<(String, usize)> {
        determinism::TEST_NOW.with(|c| c.set(Some(time_at(now))));
        let mut counts = Vec::new();
        let (_, n) = call("note", json!({"noted_time": {"timestamp": py::isoformat(&time_at(note_at)), "description": "note"}}));
        counts.push(("note".to_string(), n));
        let (_, n) = call("prepare_compaction", json!({}));
        counts.push(("prepare_compaction".to_string(), n));
        let (plan, n) =
            call("compact_notes", json!({"recap": {"overview": "As planned", "did": [{"text": "Kept it as planned"}], "unsure": []}}));
        counts.push(("compact_notes dry run".to_string(), n));
        let (_, n) = call("confirm_proposal", json!({"proposal_id": plan["proposal_id"], "revision": plan["revision"]}));
        counts.push(("confirm_proposal".to_string(), n));
        counts
    };
    for (note_at, now) in [("09:05+1", "09:10+1"), ("09:50+1", "09:55+1"), ("10:20+1", "10:25+1")] {
        round(note_at, now);
    }

    let counts = round("10:40+1", "10:45+1");

    // The first compaction call reads the notes tab, the journal and the
    // actions', people's and locations' tabs whole, together in one request
    // (NoteCompactor's prefetch); they're kept, so no later call reads
    // Sheets at all.
    let expected: Vec<(String, usize)> =
        ["note", "prepare_compaction", "compact_notes dry run", "confirm_proposal"].iter().map(|n| (n.to_string(), 0)).collect();
    assert_eq!(counts, expected);
}

// -- facts ----------------------------------------------------------------------------------
// Compaction records each past event's facts: where, who with, who for,
// and a note on each person there.

fn people() -> Vec<Value> {
    vec![json!({"id": "sam", "name": "Sam", "status": "active"}), json!({"id": "mom", "name": "Mom", "status": "active"})]
}

fn facts_setup() -> Setup {
    Setup::with(
        &[("09:05", "email"), ("10:20", "report")],
        None,
        "11:30",
        Stores { people: people(), locations: vec![json!({"id": "home", "name": "Home"})], ..Default::default() },
    )
}

#[test]
fn a_keep_records_facts_and_shows_them_in_the_timeline() {
    let setup = facts_setup();
    let given = facts(json!({"locations": ["home"], "with": ["sam"], "for": ["mom"], "notes": {"self": "sleepy", "sam": " happy  "}}));

    let planned = setup.dry_run(vec![EventDecision { facts: Some(given), ..keep("e1") }]);

    let recorded = json!({"locations": ["home"], "with": ["sam"], "for": ["mom"], "notes": {"self": "sleepy", "sam": "happy"}});
    assert_eq!(change(&planned, "e1").after.as_ref().unwrap().facts, Some(recorded.clone()));
    let text = &planned.timeline.as_ref().unwrap().text;
    assert!(text.contains("▹ @ Home · with Sam · for Mom"), "{text}");
    assert!(text.contains("▹ Me: sleepy") && text.contains("▹ Sam: happy"), "{text}");
    setup.commit(planned.compaction_id.as_ref().unwrap());
    assert_eq!(setup.patches().iter().filter(|(id, _)| id == "e1").count(), 1);
    assert_eq!(setup.event("e1").facts.map(|f| f.to_public()), Some(facts(recorded).to_public()));
}

#[test]
fn facts_that_arent_well_formed_or_name_strangers_are_refused() {
    let setup = facts_setup();
    let with = |action: &str, given: Value| vec![EventDecision { event_id: Some("e1".into()), facts: Some(facts(given)), ..EventDecision::new(action) }];

    refused(setup.try_dry_run(with("keep", json!({"with": ["self"]})), DryRun::default()), r#"decision 1 \(keep e1\): its facts "with_ids" never names "self""#);
    refused(setup.try_dry_run(with("keep", json!({"for": ["nobody"]})), DryRun::default()), r"its facts name \['nobody'\], who aren't people");
    refused(setup.try_dry_run(with("keep", json!({"locations": ["nowhere"]})), DryRun::default()), "its facts' location 'nowhere' isn't a location");
    refused(setup.try_dry_run(with("cancel", json!({"with": ["sam"]})), DryRun::default()), "facts only go with 'keep' or 'create'");
}

fn bare(stores: Stores) -> Setup {
    let setup = Setup::with(&[], None, "11:30", stores);
    setup.append_note("09:00", None);
    setup.append_note("09:30", None);
    setup
}

/// The one event the commit created, as the calendar now has it.
fn created(setup: &Setup) -> Event {
    let creates = setup.creates();
    let id = creates.last().expect("a create")["id"].as_str().unwrap().to_string();
    setup.event(&id)
}

#[test]
fn a_created_event_takes_facts() {
    let setup = bare(Stores { people: people(), ..Default::default() });
    let mut decisions = setup.coffee_instead_of_email();
    decisions[0].facts = Some(facts(json!({"with": ["sam"], "notes": {"sam": "chatty"}})));

    let id = setup.dry_run(decisions).compaction_id.unwrap();
    setup.commit(&id);

    assert_eq!(created(&setup).facts.map(|f| f.to_public()), Some(facts(json!({"with": ["sam"], "notes": {"sam": "chatty"}})).to_public()));
}

// -- additions ------------------------------------------------------------------------------
// New actions, people and locations, added from the compaction itself.

fn additions(actions: Value, people: Value, locations: Value) -> Additions {
    Additions::from_json_dict(Some(&json!({"actions": actions, "people": people, "locations": locations}))).unwrap()
}

#[test]
fn new_ones_are_named_by_ref_shown_and_created_when_applied() {
    let setup = bare(Stores { people: vec![json!({"id": "sam", "name": "Sam", "status": "active"})], ..Default::default() });
    let mut decisions = setup.coffee_instead_of_email();
    decisions[0].action_ids = Some(strings(&["new:coffee"]));
    decisions[0].facts = Some(facts(json!({"locations": ["new:cafe"], "with": ["sam", "new:alex"], "notes": {"new:alex": "new in town"}})));

    let planned = setup
        .try_dry_run(
            decisions,
            DryRun {
                additions: additions(
                    json!([{"ref": "new:coffee", "name": "Drink coffee"}]),
                    json!([{"ref": "new:alex", "name": "Alex", "context": "Sam's friend"}]),
                    json!([{"ref": "new:cafe", "name": "Corner cafe", "hint": "the cafe on Main"}]),
                ),
                ..Default::default()
            },
        )
        .unwrap();

    assert_eq!(
        planned.additions.clone().map(Value::Object),
        Some(json!({
            "actions": [{"ref": "new:coffee", "name": "Drink coffee"}],
            "people": [{"ref": "new:alex", "name": "Alex", "context": "Sam's friend"}],
            "locations": [{"ref": "new:cafe", "name": "Corner cafe", "hint": "the cafe on Main"}],
        }))
    );
    let text = &planned.timeline.as_ref().unwrap().text;
    assert!(text.contains("◇ Drink coffee (new)"), "{text}");
    // Wrapped to the timeline's width.
    assert!(text.contains("▹ @ Corner cafe (new) · with\n            Sam, Alex (new)\n"), "{text}");
    let actions = setup.compactor.actions.clone().unwrap();
    assert!(actions.all().unwrap().is_empty()); // Nothing created yet.

    setup.commit(planned.compaction_id.as_ref().unwrap());

    let all = actions.all().unwrap();
    assert_eq!(all.len(), 1);
    let coffee = &all[0];
    assert_eq!((coffee.name.as_deref(), coffee.status), (Some("Drink coffee"), Some(crate::stores::actions::ActionStatus::Active)));
    let alex = setup.compactor.people.as_ref().map(|p| serde_json::to_value(p.get_person("alex").unwrap()).unwrap()).unwrap()["id"].as_str().unwrap().to_string();
    let cafe = setup.compactor.locations.as_ref().unwrap().get_location("corner cafe").unwrap().id.unwrap();
    let event = created(&setup);
    assert_eq!(event.action_ids, Some(vec![coffee.id.clone().unwrap()]));
    assert_eq!(
        event.facts.map(|f| f.to_public()),
        Some(facts(json!({"locations": [cafe], "with": ["sam", alex.clone()], "notes": {alex: "new in town"}})).to_public())
    );
}

#[test]
fn a_resumed_commit_reuses_what_it_already_added() {
    let setup = bare(Stores::default());
    let mut decisions = setup.coffee_instead_of_email();
    decisions[0].action_ids = Some(strings(&["new:coffee"]));
    let id = setup
        .try_dry_run(decisions, DryRun { additions: additions(json!([{"ref": "new:coffee", "name": "Drink coffee"}]), json!([]), json!([])), ..Default::default() })
        .unwrap()
        .compaction_id
        .unwrap();
    setup.fail_next("POST", "/events", 500);
    assert!(setup.try_commit(&id).is_err());

    setup.commit(&id);

    let all = setup.compactor.actions.as_ref().unwrap().all().unwrap();
    let names: Vec<&str> = all.iter().map(|a| a.name.as_deref().unwrap()).collect();
    assert_eq!(names, vec!["Drink coffee"]);
    assert_eq!(created(&setup).action_ids, Some(vec![all[0].id.clone().unwrap()]));
}

#[test]
fn additions_are_checked_as_their_stores_would() {
    let cases: Vec<(Additions, &str)> = vec![
        (additions(json!([{"ref": "coffee", "name": "Coffee"}]), json!([]), json!([])), "'coffee' isn't a ref: refs start with 'new:'"),
        (additions(json!([{"ref": "new:a", "name": "Coffee"}]), json!([]), json!([{"ref": "new:a", "name": "Cafe"}])), "the ref 'new:a' is used more than once"),
        (additions(json!([{"ref": "new:a", "name": "Time Tracker"}]), json!([]), json!([])), "new actions: there's already an action named"),
        (additions(json!([]), json!([{"ref": "new:p", "name": "Me"}]), json!([])), "new people: there's already a person named 'Me'"),
        (additions(json!([]), json!([]), json!([{"ref": "new:l", "name": ""}])), "new locations: location new:l needs a name"),
    ];
    let setup = Setup::with(&[("09:05", "x")], None, "11:30", Stores { actions: vec![json!({"id": "work", "name": "Time Tracker", "status": "active"})], ..Default::default() });

    for (additions, message) in cases {
        refused(setup.try_dry_run(vec![], DryRun { additions, ..Default::default() }), message);
    }
}

#[test]
fn a_ref_that_isnt_added_is_refused() {
    let setup = Setup::new(&[("09:05", "x")], None, "11:30");

    refused(
        setup.try_dry_run(vec![EventDecision { action_ids: Some(strings(&["new:nope"])), ..keep("e1") }], DryRun::default()),
        "a ref that isn't in new_actions",
    );
}
