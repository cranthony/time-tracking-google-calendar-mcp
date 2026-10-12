//! The module's tests.

use super::*;
use serde_json::json;

fn n(subject: &str, foundations: &[&str]) -> Node {
    Node { subject: subject.into(), deleted: false, foundations: foundations.iter().map(|f| f.to_string()).collect() }
}

fn graph() -> Vec<Node> {
    vec![n("habit:sleep", &[]), n("habit:eat", &[]), n("habit:gym", &["habit:sleep", "habit:eat"]), n("family", &["habit:gym"])]
}

#[test]
fn accepts_habits_and_people_as_foundations() {
    assert!(foundation_problems("habit:gym", &json!(["habit:sleep", "habit:eat"]), &graph()).is_empty());
    assert!(foundation_problems("habit:read", &json!(["family", "habit:sleep"]), &graph()).is_empty());
    assert!(foundation_problems("family", &json!([]), &graph()).is_empty());
}

#[test]
fn refuses_what_isnt_a_list_of_ids() {
    for value in [json!("habit:sleep"), json!([1]), json!([""]), json!({"habit:sleep": true})] {
        let problems = foundation_problems("habit:gym", &value, &graph());
        assert_eq!(problems.len(), 1, "{value}");
        assert!(problems[0].starts_with("foundations must be a list of ids"), "{value}: {problems:?}");
    }
}

#[test]
fn refuses_repeats_itself_and_unknown_or_deleted_ids() {
    let mut nodes = graph();
    nodes.push(Node { subject: "habit:gone".into(), deleted: true, foundations: vec![] });

    assert_eq!(foundation_problems("habit:gym", &json!(["habit:eat", "habit:eat"]), &nodes), vec!["foundations name ['habit:eat'] more than once"]);
    assert_eq!(foundation_problems("habit:gym", &json!(["habit:gym"]), &nodes), vec!["foundations can't include itself"]);
    let problems = foundation_problems("habit:gym", &json!(["sleep", "habit:gone"]), &nodes);
    assert!(problems[0].starts_with("foundations ['sleep', 'habit:gone'] aren't habits or people"), "{problems:?}");
}

#[test]
fn refuses_a_cycle_through_habits_and_people() {
    assert_eq!(
        foundation_problems("habit:sleep", &json!(["family"]), &graph()),
        vec!["foundations would make a cycle: habit:sleep → family → habit:gym → habit:sleep"]
    );
    assert!(foundation_problems("habit:sleep", &json!(["habit:eat"]), &graph()).is_empty());
}

#[test]
fn without_takes_a_subject_out_of_every_rows_foundations() {
    let mut rows: Vec<Row> = vec![
        json!({"id": "a", "foundations": ["habit:x", "b"]}).as_object().unwrap().clone(),
        json!({"id": "b", "foundations": ["b"]}).as_object().unwrap().clone(),
        json!({"id": "c"}).as_object().unwrap().clone(),
    ];

    assert!(without(&mut rows, "habit:x"));
    assert_eq!(rows[0]["foundations"], json!(["b"]));
    assert!(!without(&mut rows, "habit:x"));
    assert!(without(&mut rows, "b"));
    assert_eq!((rows[0]["foundations"].clone(), rows[1]["foundations"].clone()), (json!([]), json!([])));
    assert!(!rows[2].contains_key("foundations"));
}
