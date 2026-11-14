//! Trait examples in compaction: the active traits shown to Claude,
//! examples proposed with compact_notes' trait_examples -- checked, kept
//! by id through revisions, shown to the user, dropped or changed in
//! review -- and created when the proposal is applied.

use super::*;
use crate::compaction::example_changes::ExampleSettled;
use crate::stores::trait_examples::TraitExample;

fn generous() -> Value {
    json!({
        "id": "generous", "name": "Generous", "status": "active", "definition": "Make an effort for them.",
        "parts": [{"kind": "judgment", "rubric": "How much effort?", "ratings": {"0": "little", "1": "some"}, "facts": ["general_notes"]}],
    })
}

fn setup() -> Setup {
    let people = vec![json!({"id": "sam", "name": "Sam", "status": "active"})];
    Setup::with(&[("09:00", "coffee"), ("09:30", "email")], None, "11:30", Stores { people, traits: Some(vec![generous()]), ..Default::default() })
}

fn example(value: Value) -> TraitExample {
    let mut base = json!({"trait_id": "generous", "when": "2026-01-01", "person_id": "sam", "what": "Brought coffee for everyone.", "why": "Went out of their way."});
    for (k, v) in value.as_object().unwrap() {
        base[k] = v.clone();
    }
    serde_json::from_value(base).unwrap()
}

fn proposing(examples: Vec<TraitExample>) -> DryRun {
    DryRun { trait_examples: examples, create_refs: vec![Some("coffee".into())], ..Default::default() }
}

fn settle(id: &str, use_: &str, fields: Value) -> ExampleSettled {
    let mut edit = fields.as_object().cloned().unwrap_or_default();
    edit.insert("use".into(), json!(use_));
    ExampleSettled::from_edit(id, &edit)
}

#[test]
fn prepare_lists_the_active_traits() {
    let context = setup().prepare().to_json();

    assert_eq!(context["traits"], json!([{"id": "generous", "name": "Generous", "definition": "Make an effort for them."}]));
    assert!(context["instructions"].as_str().unwrap().contains("TRAIT EXAMPLES"));
}

#[test]
fn examples_are_checked_when_proposed() {
    let setup = setup();
    let refused = |examples: Vec<TraitExample>| match setup.try_dry_run(setup.coffee_instead_of_email(), proposing(examples)) {
        Ok(_) => panic!("it wasn't refused"),
        Err(error) => error.to_string(),
    };

    let error = refused(vec![example(json!({"trait_id": "kind", "event_id": "e9"}))]);
    assert!(error.contains("its trait_id 'kind' isn't a trait"), "{error}");
    assert!(error.contains("its event_id 'e9' isn't one of the proposal's events"), "{error}");
    let error = refused(vec![example(json!({"person_id": "alex"}))]);
    assert!(error.contains("its person_id 'alex' isn't a person or a community"), "{error}");
    let error = refused(vec![example(json!({"id": "same"})), example(json!({"id": "same"}))]);
    assert!(error.contains("example 'same' is given more than once"), "{error}");
}

#[test]
fn proposed_examples_are_shown_settled_by_the_user_and_made_when_applied() {
    let setup = setup();
    let result = setup
        .try_dry_run(
            setup.coffee_instead_of_email(),
            proposing(vec![
                example(json!({"event_id": "coffee"})),
                example(json!({"id": "patience", "person_id": "self", "what": "Waited.", "why": "Calm.", "event_id": "e1"})),
            ]),
        )
        .unwrap();
    let p = result.proposal_id.clone().unwrap();

    let shown = setup.get_proposal().to_json()["trait_examples"].clone();
    let made_id = shown[0]["id"].as_str().unwrap().to_string();
    let key = shown[0]["event_id"].as_str().unwrap().to_string();
    assert!(key.starts_with(&p), "the create's ref is its key: {key}");
    assert_eq!((shown[0]["trait_name"].clone(), shown[0]["person_name"].clone(), shown[0]["decided_by"].clone()), (json!("Generous"), json!("Sam"), json!("claude")));
    assert_eq!(shown[1]["id"], json!("patience"));
    // Claude gets them back, with their ids, to send again.
    let context = setup.prepare().to_json();
    assert_eq!(context["proposal"]["trait_examples"][0]["id"], json!(made_id));

    // The user changes one and drops the other.
    let amended = setup
        .try_amend(&p, 1, Amend { trait_examples: vec![settle(&made_id, "change", json!({"what": "Brought coffee and stayed to talk."})), settle("patience", "drop", json!({}))], ..Default::default() })
        .unwrap()
        .to_json();
    assert_eq!(amended["trait_examples"].as_array().unwrap().len(), 1);
    assert_eq!((amended["trait_examples"][0]["what"].clone(), amended["trait_examples"][0]["decided_by"].clone()), (json!("Brought coffee and stayed to talk."), json!("user")));
    assert_eq!(amended["settled_trait_examples"][1]["use"], json!("drop"));

    // Claude revises, sending both again: the user's say stands.
    let sent: Vec<TraitExample> = serde_json::from_value(context["proposal"]["trait_examples"].clone()).unwrap();
    let mut decisions = setup.coffee_instead_of_email();
    decisions[0].key = Some(key.clone());
    let revised = setup.try_dry_run(decisions, DryRun { trait_examples: sent, ..DryRun::revising(&p, 2) }).unwrap();
    assert_eq!(setup.get_proposal().to_json()["trait_examples"].as_array().unwrap().len(), 1);

    let store = setup.compactor.facts.as_ref().unwrap().examples.clone();
    assert!(store.all().unwrap().is_empty());
    let applied = setup.commit(&revised.compaction_id.unwrap());
    assert!(applied.warnings.is_empty(), "{:?}", applied.warnings);

    let made = store.all().unwrap();
    assert_eq!(made.len(), 1);
    assert_eq!((made[0].id.as_deref(), made[0].what.as_deref()), (Some(made_id.as_str()), Some("Brought coffee and stayed to talk.")));
    // The new event's key became the id it was made with.
    let event = made[0].event_id.clone().unwrap();
    assert!(event.starts_with("cmp"), "{event}");
    assert_eq!(setup.event(&event).summary.as_deref(), Some("Coffee"));
}

#[test]
fn a_users_choice_is_checked() {
    let setup = setup();
    let p = setup.try_dry_run(setup.coffee_instead_of_email(), proposing(vec![example(json!({"id": "story"}))])).unwrap().proposal_id.unwrap();

    let error = setup.try_amend(&p, 1, Amend { trait_examples: vec![settle("other", "drop", json!({}))], ..Default::default() }).unwrap_err().to_string();
    assert!(error.contains("example 'other' isn't one the proposal makes"), "{error}");
    let error = setup
        .try_amend(&p, 1, Amend { trait_examples: vec![settle("story", "change", json!({"clear_fields": ["person_id"]}))], ..Default::default() })
        .unwrap_err()
        .to_string();
    assert!(error.contains("example 'story': it needs who did it"), "{error}");
    // One of the user's own needs every field.
    let error = setup
        .try_amend(&p, 1, Amend { trait_examples: vec![settle("mine", "change", json!({"trait_id": "generous"}))], ..Default::default() })
        .unwrap_err()
        .to_string();
    assert!(error.contains("example 'mine': it needs a when"), "{error}");
}
