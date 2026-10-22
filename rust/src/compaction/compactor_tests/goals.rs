//! Goal shares: compaction tags events with the open goals that have
//! attribution hints, and writes the shares it gives them.

use super::*;
use crate::stores::goals::GoalShare;

fn goal(id: &str, title: &str, parent: Option<&str>, status: &str, hints: Option<&str>) -> Value {
    json!({"id": id, "title": title, "parent_id": parent, "status": status, "deadline": "2026-12-31", "attribution_hints": hints})
}

fn goals() -> Vec<Value> {
    vec![
        goal("g1", "Write a book", None, "active", Some("writing sessions")),
        goal("g2", "Draft chapter one", Some("g1"), "inactive", Some("work on chapter one")),
        goal("g3", "Learn to juggle", None, "active", None),
        goal("g4", "Run a marathon", None, "done", Some("long runs")),
        goal("g5", "Read more", None, "obsolete", Some("reading")),
        goal("g6", "Tidy up", None, "active", Some("   ")),
    ]
}

fn goals_setup() -> Setup {
    Setup::with(&[("09:05", "email"), ("10:20", "report")], None, "11:30", Stores { goals: goals(), ..Default::default() })
}

fn share(id: &str, minutes: Option<i64>, percent: Option<i64>) -> GoalShare {
    GoalShare { goal_id: id.into(), minutes, percent, remaining: false }
}

#[test]
fn prepare_lists_the_open_goals_those_with_hints_to_tag_events_with() {
    let context = goals_setup().prepare().to_json();

    let listed: Vec<(&str, Option<&str>)> =
        context["goals"].as_array().unwrap().iter().map(|g| (g["id"].as_str().unwrap(), g["attribution_hints"].as_str())).collect();
    assert_eq!(listed, vec![("g1", Some("writing sessions")), ("g2", Some("work on chapter one")), ("g3", None), ("g6", None)]);
    assert_eq!(context["goals"][1]["path"], json!(["Write a book"]));
    assert!(DECISION_GUIDE.contains("GOALS: `goals` lists the open goals"));
}

#[test]
fn a_keep_writes_the_goal_shares_it_gives() {
    let setup = goals_setup();
    let shares = vec![share("g2", Some(30), None), share("g1", None, Some(50))];

    let planned = setup.dry_run(vec![EventDecision { goals: Some(shares.clone()), ..keep("e1") }]);

    assert_eq!(change(&planned, "e1").after.as_ref().unwrap().goals, Some(shares.clone()));
    setup.commit(planned.compaction_id.as_ref().unwrap());
    assert_eq!(setup.patches().iter().filter(|(id, _)| id == "e1").count(), 1);
    assert_eq!(setup.event("e1").goals, Some(shares));
    // An event not tagged keeps what it had: none.
    assert_eq!(setup.event("e2").goals, None);
}

#[test]
fn a_create_writes_its_goal_shares() {
    let setup = Setup::with(&[], None, "11:30", Stores { goals: goals(), ..Default::default() });
    setup.append_note("09:00", None);
    setup.append_note("09:30", None);
    let mut decisions = setup.coffee_instead_of_email();
    decisions[0].goals = Some(vec![share("g1", None, None)]);

    let id = setup.dry_run(decisions).compaction_id.unwrap();
    setup.commit(&id);

    let creates = setup.creates();
    let created = creates.last().expect("a create")["id"].as_str().unwrap().to_string();
    assert_eq!(setup.event(&created).goals, Some(vec![share("g1", None, None)]));
}

#[test]
fn goal_shares_that_name_no_goal_or_dont_fit_are_refused() {
    let setup = goals_setup();
    let with = |shares: Vec<GoalShare>| vec![EventDecision { goals: Some(shares), ..keep("e1") }];

    refused(setup.try_dry_run(with(vec![share("nope", None, None)]), DryRun::default()), r"its goals name \['nope'\], which aren't goals");
    refused(setup.try_dry_run(with(vec![share("g1", Some(90), None)]), DryRun::default()), "its goals' minutes add up to 90, more than the event's 60");
    refused(setup.try_dry_run(with(vec![share("g1", None, Some(70)), share("g2", None, Some(40))]), DryRun::default()), "more than 100");
}

#[test]
fn proposals_keep_the_goal_shares_through_a_revision() {
    let decision = EventDecision { goals: Some(vec![share("g1", Some(15), None)]), ..keep("e1") };

    let back = EventDecision::from_json_dict(&decision.to_json_dict()).unwrap();

    assert_eq!(back, decision);
}

/// A goal made and closed (`status`) at `closed`, a time today, or the
/// day before when it's none.
fn closed_goal(id: &str, title: &str, status: &str, closed: Option<&str>) -> Value {
    let at = closed.map_or_else(|| (time_at("09:00") - chrono::Duration::days(1)).to_rfc3339(), |c| time_at(c).to_rfc3339());
    json!({"id": id, "title": title, "status": status, "history": [{"status": "active", "at": at}, {"status": status, "at": at}]})
}

fn closed_setup() -> Setup {
    let mut goals = goals();
    goals.push(closed_goal("c1", "Answer Sam's email", "done", Some("09:30")));
    goals.push(closed_goal("c2", "Fix the fence", "obsolete", Some("10:30")));
    goals.push(closed_goal("c3", "Old thing", "done", None));
    Setup::with(&[("09:05", "email"), ("10:20", "report")], None, "11:30", Stores { goals, ..Default::default() })
}

#[test]
fn prepare_lists_the_goals_closed_since_the_earliest_event_started_with_when() {
    let context = closed_setup().prepare().to_json();

    let listed: Vec<&str> = context["goals"].as_array().unwrap().iter().map(|g| g["id"].as_str().unwrap()).collect();
    assert_eq!(listed, vec!["g1", "g2", "g3", "g6", "c1", "c2"]);
    let c1 = context["goals"].as_array().unwrap().iter().find(|g| g["id"] == "c1").unwrap();
    assert_eq!(c1["closed_at"].as_str().map(|t| crate::event::parse_iso(t, None).unwrap()), Some(time_at("09:30")));
    assert_eq!(c1["status"], json!("done"));
    assert!(context["goals"][0].get("closed_at").is_none());
    assert!(DECISION_GUIDE.contains("A closed goal can be tagged only on events that started before its `closed_at`"));
}

#[test]
fn a_closed_goal_tags_only_events_that_started_before_it_closed() {
    let tag = |event: &str, id: &str| EventDecision { goals: Some(vec![share(id, None, None)]), ..keep(event) };

    // e1 started at 9:00, before both closed; e2 at 10:00, after c1 closed.
    let planned = closed_setup().dry_run(vec![tag("e1", "c1"), tag("e2", "c2")]);
    assert_eq!(change(&planned, "e1").after.as_ref().unwrap().goals, Some(vec![share("c1", None, None)]));
    refused(closed_setup().try_dry_run(vec![tag("e2", "c1")], DryRun::default()), r"its goals name \['c1'\], which closed before it started");
}
