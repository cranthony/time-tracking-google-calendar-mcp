//! Keeping people's trait facts: each fact's summary of a person,
//! rewritten whole by compact_notes' fact_summaries, beside the log of
//! what changed on the event that showed it -- shown to Claude, checked
//! against each other, shown to the user with the summary as it was, and
//! written when the proposal is applied.

use super::*;
use crate::testing::facts;

fn caring() -> Value {
    json!({
        "id": "caring", "name": "Caring", "status": "active",
        "parts": [{"kind": "judgment", "rubric": "Did I care for them?", "ratings": {"0": "no", "1": "yes"}, "facts": ["general_notes", "fact:worries"]}],
        "facts": [
            {"id": "worries", "name": "Worries", "summary_prompt": "What weighs on them, as a list.", "log_prompt": "What new worry showed."},
            {"id": "joys", "name": "Joys", "summary_prompt": "What delights them."}
        ],
    })
}

fn people() -> Vec<Value> {
    vec![json!({"id": "sam", "name": "Sam", "status": "active"}), json!({"id": "kim", "name": "Kim", "status": "active", "traits": {"select": []}})]
}

fn setup() -> Setup {
    let setup = Setup::with(&[("09:05", "email")], None, "11:30", Stores { people: people(), traits: Some(vec![caring()]), ..Default::default() });
    let traits = setup.compactor.facts.as_ref().unwrap().traits.all().unwrap();
    let change = crate::stores::person_facts::SummaryChange {
        person_id: "sam".into(),
        trait_id: "caring".into(),
        fact_id: "worries".into(),
        summary: "- money".into(),
    };
    setup.call(|c| c.facts.as_ref().unwrap().person_facts.set_summaries(&[change], &traits, "user", time_at("08:00"))).unwrap();
    setup
}

fn with_sam(logs: Value) -> Vec<EventDecision> {
    vec![EventDecision { facts: Some(facts(json!({"with": ["sam"], "logs": logs}))), ..keep("e1") }]
}

fn summary(person: &str, fact: &str, text: &str) -> ProposedSummary {
    ProposedSummary { person_id: person.into(), fact: fact.into(), summary: text.into() }
}

fn proposing(summaries: Vec<ProposedSummary>) -> DryRun {
    DryRun { fact_summaries: summaries, ..Default::default() }
}

#[test]
fn prepare_describes_each_fact_once_and_gives_each_person_theirs() {
    let setup = setup();

    let context = setup.prepare().to_json();

    assert_eq!(
        context["trait_facts"],
        json!([
            {"fact": "caring/worries", "trait_name": "Caring", "name": "Worries", "summary_prompt": "What weighs on them, as a list.", "log_prompt": "What new worry showed."},
            {"fact": "caring/joys", "trait_name": "Caring", "name": "Joys", "summary_prompt": "What delights them.", "log_prompt": null}
        ])
    );
    let facts_of = |id: &str| context["people"].as_array().unwrap().iter().find(|p| p["id"] == json!(id)).unwrap()["facts"].clone();
    assert_eq!(facts_of("sam"), json!([{"fact": "caring/worries", "summary": "- money"}, {"fact": "caring/joys", "summary": null}]));
    assert_eq!(facts_of("self"), json!([{"fact": "caring/worries", "summary": null}, {"fact": "caring/joys", "summary": null}]));
    // Kim's traits select none.
    assert_eq!(facts_of("kim"), json!([]));
    let guide = context["instructions"].as_str().unwrap();
    assert!(guide.contains("NOT EVERY FACT IS NOTED ON EVERY EVENT"), "{guide}");
}

#[test]
fn a_summary_needs_its_log_and_a_log_its_summary() {
    let setup = setup();
    let refused = |decisions: Vec<EventDecision>, summaries: Vec<ProposedSummary>| {
        match setup.try_dry_run(decisions, proposing(summaries)) {
            Ok(_) => panic!("it wasn't refused"),
            Err(error) => error.to_string(),
        }
    };

    let error = refused(with_sam(json!(null)), vec![summary("sam", "caring/worries", "- money\n- a new job")]);
    assert!(error.contains("fact_summaries: 'sam''s caring/worries has no log"), "{error}");

    let error = refused(with_sam(json!({"sam": {"caring/worries": "a new job"}})), vec![]);
    assert!(error.contains("the log of 'sam''s caring/worries has no summary"), "{error}");

    let error = refused(with_sam(json!({"sam": {"caring/hobbies": "golf"}})), vec![summary("sam", "caring/hobbies", "golf")]);
    assert!(error.contains("'caring/hobbies' isn't one of 'sam''s facts (caring/worries, caring/joys)"), "{error}");

    let error = refused(
        vec![EventDecision { facts: Some(facts(json!({"with": ["kim"], "logs": {"kim": {"caring/worries": "x"}}}))), ..keep("e1") }],
        vec![summary("kim", "caring/worries", "x")],
    );
    assert!(error.contains("isn't one of 'kim''s facts (they have none)"), "{error}");

    let error = refused(with_sam(json!({"sam": {"caring/worries": "a new job"}})), vec![summary("sam", "caring/worries", "a"), summary("sam", "caring/worries", "b")]);
    assert!(error.contains("is given more than once"), "{error}");
}

#[test]
fn the_user_sees_each_summary_beside_what_it_was_and_applying_writes_them() {
    let setup = setup();
    let logs = json!({"sam": {"caring/worries": "Starts a new job Monday."}, "self": {"caring/joys": "The email was done early."}});
    let result = setup
        .try_dry_run(with_sam(logs), proposing(vec![summary("sam", "caring/worries", "- money\n- a new job"), summary("self", "caring/joys", "- finishing early")]))
        .unwrap();

    let proposal = setup.get_proposal().to_json();
    assert_eq!(
        proposal["fact_summaries"],
        json!([
            {"person_id": "sam", "fact": "caring/worries", "summary": "- money\n- a new job", "previous": "- money"},
            {"person_id": "self", "fact": "caring/joys", "summary": "- finishing early", "previous": null}
        ])
    );
    // Claude gets them back to send again.
    assert_eq!(setup.prepare().to_json()["proposal"]["fact_summaries"][0]["summary"], json!("- money\n- a new job"));
    // Nothing's written until it's applied.
    let store = setup.compactor.facts.as_ref().unwrap().person_facts.clone();
    assert_eq!(store.summary("sam", "caring", "worries").unwrap().as_deref(), Some("- money"));

    let applied = setup.commit(&result.compaction_id.unwrap());

    assert_eq!(store.summary("sam", "caring", "worries").unwrap().as_deref(), Some("- money\n- a new job"));
    assert_eq!(store.summary("self", "caring", "joys").unwrap().as_deref(), Some("- finishing early"));
    let logged = store.logs("sam", None, None).unwrap();
    assert_eq!((logged.len(), logged[0].event_id.as_deref(), logged[0].log.as_deref()), (1, Some("e1"), Some("Starts a new job Monday.")));
    assert_eq!(setup.event("e1").facts.unwrap().log_entries().len(), 2);
    // The judge sees the summary as it now is, for the parts naming it.
    let due = applied.judgments.unwrap().to_json();
    assert_eq!(due["facts"], json!({"sam": {"caring/worries": "- money\n- a new job"}}));
}

#[test]
fn a_person_with_prompts_of_their_own_is_given_them() {
    let mut people = people();
    people[0]["traits"] = json!({"facts": {"caring": {"worries": {"summary_prompt": {"text": "Their health, mostly.", "how": "append"}}}}});
    let setup = Setup::with(&[("09:05", "email")], None, "11:30", Stores { people, traits: Some(vec![caring()]), ..Default::default() });

    let context = setup.prepare().to_json();

    let sam = context["people"].as_array().unwrap().iter().find(|p| p["id"] == json!("sam")).unwrap()["facts"].clone();
    assert_eq!(
        sam[0],
        json!({
            "fact": "caring/worries", "summary": null,
            "summary_prompt": "What weighs on them, as a list.\n\nTheir health, mostly.", "summary_prompt_from": "the trait's, plus theirs",
            "log_prompt": "What new worry showed.", "log_prompt_from": "the trait's"
        })
    );
    // Their other fact goes by the trait's.
    assert_eq!(sam[1], json!({"fact": "caring/joys", "summary": null}));
    assert!(context["instructions"].as_str().unwrap().contains("follow the prompts given for that person, not the trait's"));
}
