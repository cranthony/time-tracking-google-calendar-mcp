use super::*;
use crate::stores::actions::ActionShare;
use crate::stores::goals::GoalShare;
use serde_json::json;

fn action(id: &str, minutes: Option<i64>, percent: Option<i64>, remaining: bool) -> ActionShare {
    ActionShare::new(id.into(), minutes, percent, remaining)
}

#[test]
fn remaining_is_kept_as_a_plus_r() {
    let shares = vec![action("a", None, None, false), action("b", Some(45), None, true), action("c", None, Some(30), true), action("d", Some(5), None, false)];

    assert_eq!(shares_text(&shares), "a b=45m+r c=30%+r d=5m");
    assert_eq!(parse_shares::<ActionShare>("a b=45m+r c=30%+r d=5m"), shares);
}

#[test]
fn ids_kept_before_shares_read_as_actions_with_neither() {
    assert_eq!(parse_shares::<ActionShare>("a b"), vec![ActionShare::whole("a"), ActionShare::whole("b")]);
}

#[test]
fn remaining_means_nothing_without_minutes_or_percent() {
    assert_eq!(action("a", None, None, true), ActionShare::whole("a"));
    assert!(!GoalShare::new("g".into(), None, None, true).remaining);
}

#[test]
fn an_action_with_neither_is_sent_as_its_id() {
    let shares = vec![ActionShare::whole("a"), action("b", Some(45), None, false), action("c", None, Some(30), true)];
    let sent = json!(shares);

    assert_eq!(sent, json!(["a", {"action_id": "b", "minutes": 45}, {"action_id": "c", "percent": 30, "remaining": true}]));
    assert_eq!(serde_json::from_value::<Vec<ActionShare>>(sent).unwrap(), shares);
}

#[test]
fn an_action_share_with_an_unknown_field_is_refused() {
    assert!(serde_json::from_value::<ActionShare>(json!({"action_id": "a", "hours": 1})).is_err());
}

#[test]
fn action_shares_fit_in_their_event() {
    assert!(share_problems(&[action("a", Some(40), None, true), action("b", None, Some(100), false), action("c", None, None, false)], 60).is_empty());

    let problems = share_problems(&[action("a", Some(40), None, false), action("b", Some(21), None, true)], 60);
    assert_eq!(problems, vec!["its actions' minutes add up to 61, more than the event's 60"]);
    let problems = share_problems(&[action("a", None, None, false), action("a", Some(5), None, false)], 60);
    assert_eq!(problems, vec!["action a is given more than once"]);
}

#[test]
fn settled_shares_keep_remaining() {
    let settled = settled_shares(&[action("a", Some(90), None, true), action("b", None, None, false)], 60, &|_| true);

    assert_eq!(settled, vec![action("a", Some(60), None, true), ActionShare::whole("b")]);
}
