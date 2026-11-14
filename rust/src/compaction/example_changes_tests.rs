use super::*;

fn example(value: Value) -> TraitExample {
    let mut base = json!({"id": "a1", "trait_id": "generous", "when": "2026-01-01", "person_id": "p1", "what": "Cooked.", "why": "Effort."});
    for (k, v) in value.as_object().unwrap() {
        base[k] = v.clone();
    }
    serde_json::from_value(base).unwrap()
}

fn settled(choices: &[ExampleSettled]) -> Dict<ExampleSettled> {
    let mut out = Dict::new();
    for c in choices {
        out.set(&c.id, c.clone());
    }
    out
}

fn choice(id: &str, use_: &str, fields: Value) -> ExampleSettled {
    let mut edit = fields.as_object().cloned().unwrap();
    edit.insert("use".into(), json!(use_));
    ExampleSettled::from_edit(id, &edit)
}

#[test]
fn each_gets_an_id_none_other_has() {
    let made = with_ids(vec![example(json!({"id": null})), example(json!({"id": "a1"}))], &["b2".to_string()]);

    let ids: Vec<String> = made.iter().map(|e| e.id.clone().unwrap()).collect();
    assert_eq!(ids[1], "a1");
    assert!(!["a1", "b2"].contains(&ids[0].as_str()) && rows::chosen_id_shape(&ids[0]), "{ids:?}");
}

#[test]
fn the_users_say_lays_over_claudes() {
    let claude = vec![example(json!({})), example(json!({"id": "a2"})), example(json!({"id": "a3"}))];
    let choices = [
        choice("a1", "change", json!({"what": "Cooked for six.", "clear_fields": ["person_id"], "who": "the neighbours"})),
        choice("a2", "drop", json!({})),
        choice("u1", "change", json!({"trait_id": "brave", "when": "2026-01-02", "who": "a stranger", "what": "Stepped in.", "why": "Risked it."})),
    ];

    let made = effective(&claude, &settled(&choices));

    let shown: Vec<(Option<&str>, Option<&str>, Option<&str>, &str)> =
        made.iter().map(|(e, by)| (e.id.as_deref(), e.what.as_deref(), e.person_id.as_deref(), *by)).collect();
    assert_eq!(shown, [(Some("a1"), Some("Cooked for six."), None, "user"), (Some("a3"), Some("Cooked."), Some("p1"), "claude"), (Some("u1"), Some("Stepped in."), None, "user")]);
    assert_eq!(made[0].0.who.as_deref(), Some("the neighbours"));
    assert_eq!(unsettled(&claude, &settled(&choices)).len(), 1);
}

#[test]
fn a_settlement_round_trips_through_the_ledger() {
    let given = choice("a1", "change", json!({"why": "Gave the evening.", "clear_fields": ["event_id"]}));

    let again = ExampleSettled::from_edit("a1", &given.edit());

    assert_eq!(again, given);
    assert_eq!(given.edit()["action"], json!("trait_example"));
}

#[test]
fn problems_name_each_example() {
    let trait_ids = vec!["generous".to_string()];
    let people = vec!["p1".to_string(), "new:sam".to_string()];
    let known = Known { trait_ids: &trait_ids, person_ids: &people };
    let events = vec!["e1".to_string()];

    let found = problems(
        &[
            example(json!({"event_id": "e1", "person_id": "new:sam"})),
            example(json!({"id": "a2", "event_id": "e7"})),
            example(json!({"id": "a2"})),
            example(json!({"id": "Bad Id", "trait_id": "kind"})),
        ],
        &known,
        &events,
    );

    assert_eq!(
        found,
        [
            "trait_examples: example 'a2': its event_id 'e7' isn't one of the proposal's events",
            "trait_examples: example 'a2' is given more than once",
            "trait_examples: example 'Bad Id': its id must be 2 to 40 lowercase letters, digits and dashes (or left out, and one is made)",
            "trait_examples: example 'Bad Id': its trait_id 'kind' isn't a trait (get_traits lists them)",
        ]
    );
}

#[test]
fn choices_are_checked_against_the_proposal() {
    let claude = vec![example(json!({}))];

    let found = choice_problems(&[choice("a1", "apply", json!({})), choice("zz", "drop", json!({})), choice("a1", "change", json!({"clear_fields": ["what"]}))], &claude);

    assert_eq!(
        found,
        [
            "trait_examples: example 'a1': use is one of drop, change, not 'apply'",
            "trait_examples: example 'zz' isn't one the proposal makes, so there's nothing to drop",
            "trait_examples: example 'a1' is given more than once",
            "trait_examples: example 'a1': 'what' can't be cleared; only person_id, who, event_id can",
        ]
    );
}
