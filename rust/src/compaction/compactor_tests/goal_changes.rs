//! Goal changes: compaction proposes marking goals there are done, missed
//! or obsolete, from the notes or a deadline that passed in the window;
//! the user settles each in review, and the rest are applied, each as
//! one update, when the proposal is.

use super::*;
use crate::compaction::proposals::RecapPoint;
use crate::stores::goals::{Goal, GoalEdit, GoalStatus, StatusChange};

const RECIPE: &str = "Recipe sent to Sam";

fn started(at: &str) -> Value {
    json!([{"status": "active", "at": format!("2025-12-30T{at}:00+00:00"), "deadline": "2026-01-10"}])
}

fn goals() -> Vec<Value> {
    vec![
        json!({"id": "g1", "title": RECIPE, "status": "active", "deadline": "2026-01-10", "history": started("09:00")}),
        json!({"id": "g2", "title": "Fence fixed", "status": "active", "deadline": "2026-01-01T10:00", "history": started("09:00")}),
        json!({"id": "g3", "title": "Posts bought", "parent_id": "g2", "status": "active", "deadline": "2026-01-01T09:30", "history": started("09:30")}),
        json!({"id": "g4", "title": "Juggle", "status": "inactive", "deadline": "2026-01-01", "history": [{"status": "inactive", "at": "2025-12-30T09:00:00+00:00", "deadline": "2026-01-01"}]}),
        json!({"id": "g5", "title": "Read", "status": "done", "deadline": "2025-12-31", "history": [{"status": "done", "at": "2025-12-30T09:00:00+00:00", "deadline": "2025-12-31"}]}),
    ]
}

/// A day with the recipe sent at 10:20.
fn setup() -> Setup {
    Setup::with(&[("09:05", "email"), ("10:20", "sent Sam the recipe")], None, "11:30", Stores { goals: goals(), ..Default::default() })
}

fn change(goal_id: &str, status: GoalStatus, at: Option<&str>) -> GoalChange {
    GoalChange { goal_id: goal_id.into(), status, at: at.map(time_at), note_ids: Vec::new(), retry_deadline: None }
}

fn recap(goal_ids: &[&str]) -> Recap {
    Recap {
        overview: "Recorded the morning".into(),
        did: vec![RecapPoint { text: "Marked goals".into(), goal_ids: strings(goal_ids), ..Default::default() }],
        unsure: Vec::new(),
    }
}

fn proposed(setup: &Setup, changes: Vec<GoalChange>) -> anyhow::Result<CompactionResult> {
    setup.try_dry_run(setup.email_then_report(), DryRun { goal_changes: changes, ..Default::default() })
}

fn revised(setup: &Setup, proposal: &str, revision: i64, changes: Vec<GoalChange>) -> anyhow::Result<CompactionResult> {
    setup.try_dry_run(setup.email_then_report(), DryRun { goal_changes: changes, ..DryRun::revising(proposal, revision) })
}

fn settle(setup: &Setup, proposal: &str, revision: i64, choices: Vec<Value>) -> anyhow::Result<Proposal> {
    setup.try_amend(proposal, revision, Amend { goal_changes: choices.iter().map(GoalChangeChoice::from_value).collect(), ..Default::default() })
}

fn goal(setup: &Setup, id: &str) -> Goal {
    setup.compactor.goals.as_ref().unwrap().all().unwrap().into_iter().find(|g| g.id.as_deref() == Some(id)).expect("the goal")
}

fn entry(status: GoalStatus, at: &str, deadline: &str) -> StatusChange {
    StatusChange { status, at: crate::contract::DateTimeText(py::pydantic_datetime(&time_at(at))), deadline: Some(deadline.into()) }
}

fn shown_ids(proposal: &Proposal) -> Vec<(String, String, String)> {
    proposal
        .goal_changes
        .iter()
        .map(|c| (c["goal_id"].as_str().unwrap().into(), c["status"].as_str().unwrap().into(), c["decided_by"].as_str().unwrap().into()))
        .collect()
}

#[test]
fn prepare_marks_the_goals_whose_deadline_fell_in_the_window() {
    let context = setup().prepare().to_json();

    let due: Vec<(&str, bool)> = context["goals"].as_array().unwrap().iter().map(|g| (g["id"].as_str().unwrap(), g["due_in_window"] == json!(true))).collect();
    // g2 and g3 fell due this morning; g1 is due next week, g4 at the end
    // of today, and g5 is closed.
    assert_eq!(due, vec![("g1", false), ("g2", true), ("g3", true), ("g4", false)]);
    assert!(DECISION_GUIDE.contains("GOAL CHANGES: `goals` are the goals as they stand"));
    assert!(DECISION_GUIDE.contains("TRIAGE: for every goal with `due_in_window`"));
    assert!(DECISION_GUIDE.contains("never give a `retry_deadline` yourself"));
}

#[test]
fn a_note_saying_a_goal_was_reached_proposes_it_done_and_applying_the_proposal_closes_it() {
    let setup = setup();
    let mut done = change("g1", GoalStatus::Done, Some("10:20"));
    done.note_ids = vec![setup.note_id(3)];

    let result = setup.try_dry_run(setup.email_then_report(), DryRun { goal_changes: vec![done], recap: Some(recap(&["g1"])), ..Default::default() }).unwrap();

    assert!(result.message.contains("and 1 goal change(s)"), "{}", result.message);
    let p = result.proposal_id.clone().unwrap();
    let proposal = setup.get_proposal();
    assert_eq!(
        proposal.goal_changes,
        vec![json!({
            "goal_id": "g1", "title": RECIPE, "path": [], "status": "done", "at": py::pydantic_datetime(&time_at("10:20")),
            "note_ids": [setup.note_id(3)], "retry_deadline": null, "decided_by": "claude",
        })]
    );
    assert_eq!(proposal.recap.unwrap().did[0].goal_ids, strings(&["g1"]));
    // Claude gets its changes back to send again.
    assert_eq!(setup.prepare().proposal.unwrap().goal_changes[0]["goal_id"], json!("g1"));
    assert_eq!(goal(&setup, "g1").status, Some(GoalStatus::Active));

    let applied = setup.call(|c| c.confirm(&p, 1)).unwrap();

    assert_eq!(applied.status, "applied");
    let made = goal(&setup, "g1");
    assert_eq!(made.status, Some(GoalStatus::Done));
    assert_eq!(made.history.unwrap().last(), Some(&entry(GoalStatus::Done, "10:20", "2026-01-10")));
    // After the calendar writes.
    let requests = setup.t.fake.requests();
    let last_calendar_write = requests.iter().rposition(|r| r.path.starts_with("/calendar/") && r.method != "GET").unwrap();
    let goal_write = requests.iter().position(|r| r.method != "GET" && !r.path.starts_with("/calendar/") && r.body.to_string().contains(RECIPE)).unwrap();
    assert!(last_calendar_write < goal_write);
    assert!(applied.proposal.unwrap().warnings.is_empty());
}

#[test]
fn a_change_without_a_time_is_made_at_the_proposals_through() {
    let setup = setup();

    let p = proposed(&setup, vec![change("g1", GoalStatus::Obsolete, None)]).unwrap().proposal_id.unwrap();

    assert_eq!(setup.get_proposal().goal_changes[0]["at"], json!(py::pydantic_datetime(&time_at("11:30"))));
    setup.call(|c| c.confirm(&p, 1)).unwrap();
    assert_eq!(goal(&setup, "g1").history.unwrap().last(), Some(&entry(GoalStatus::Obsolete, "11:30", "2026-01-10")));
}

#[test]
fn a_retry_marks_the_goal_missed_then_open_again_by_its_new_deadline() {
    let setup = setup();
    let retry = GoalChange { retry_deadline: Some("2026-02-01".into()), ..change("g1", GoalStatus::Missed, Some("10:00")) };

    let p = proposed(&setup, vec![retry]).unwrap().proposal_id.unwrap();
    setup.call(|c| c.confirm(&p, 1)).unwrap();

    let made = goal(&setup, "g1");
    assert_eq!((made.status, made.deadline.as_deref()), (Some(GoalStatus::Active), Some("2026-02-01")));
    let history = made.history.unwrap();
    assert_eq!(&history[1..], &[entry(GoalStatus::Missed, "10:00", "2026-01-10"), entry(GoalStatus::Active, "10:00", "2026-02-01")]);
}

#[test]
fn closing_a_goal_with_open_sub_goals_is_refused_unless_they_close_too() {
    let setup = setup();

    refused(proposed(&setup, vec![change("g2", GoalStatus::Done, Some("10:20"))]), r"goal g3 \('Posts bought'\) is active, but its parent goal g2 \('Fence fixed'\) is done");

    let both = vec![change("g2", GoalStatus::Done, Some("10:20")), change("g3", GoalStatus::Done, Some("10:00"))];
    let p = proposed(&setup, both).unwrap().proposal_id.unwrap();
    setup.call(|c| c.confirm(&p, 1)).unwrap();
    assert_eq!((goal(&setup, "g2").status, goal(&setup, "g3").status), (Some(GoalStatus::Done), Some(GoalStatus::Done)));
}

#[test]
fn malformed_goal_changes_are_refused_naming_the_goal() {
    let setup = setup();
    let one = |c: GoalChange| proposed(&setup, vec![c]);

    refused(one(change("nope", GoalStatus::Done, None)), "There's no goal with the id or title 'nope'");
    refused(one(change("g5", GoalStatus::Done, None)), r"goal g5 \('Read'\) is done already, not open");
    refused(one(change("g1", GoalStatus::Active, None)), r"goal g1 \('Recipe sent to Sam'\): a goal change marks it done, missed or obsolete, not active");
    refused(one(change("g1", GoalStatus::Done, Some("08:00"))), r"its time \(2026-01-01T08:00:00\+00:00\) is outside the window");
    refused(one(GoalChange { retry_deadline: Some("2026-02-01".into()), ..change("g1", GoalStatus::Done, None) }), "retry_deadline goes with status missed");
    refused(one(GoalChange { note_ids: strings(&["nope"]), ..change("g1", GoalStatus::Done, None) }), "'nope' isn't one of the proposal's notes");
    refused(one(GoalChange { retry_deadline: Some("someday".into()), ..change("g1", GoalStatus::Missed, None) }), "'someday' isn't a date");
    refused(proposed(&setup, vec![change("g1", GoalStatus::Done, None), change("g1", GoalStatus::Obsolete, None)]), "goal 'g1' is given more than once");
    let cited = setup.try_dry_run(setup.email_then_report(), DryRun { recap: Some(recap(&["g1"])), ..Default::default() });
    refused(cited, "'g1' isn't a goal the proposal changes");
    // Nothing was written.
    assert_eq!(goal(&setup, "g1").status, Some(GoalStatus::Active));
}

#[test]
fn the_user_drops_applies_or_changes_a_goal_change_and_claude_leaves_the_settled_ones_out() {
    let setup = setup();
    let both = vec![change("g1", GoalStatus::Done, Some("10:20")), change("g3", GoalStatus::Done, Some("10:00"))];
    let p = proposed(&setup, both.clone()).unwrap().proposal_id.unwrap();

    // Dropped: the goal is left alone.
    let proposal = settle(&setup, &p, 1, vec![json!({"goal_id": "g1", "use": "drop"})]).unwrap();
    assert_eq!(shown_ids(&proposal), vec![("g3".into(), "done".into(), "claude".into())]);
    assert_eq!(proposal.settled_goal_changes[0].to_json(), json!({"goal_id": "g1", "use": "drop", "status": null, "at": null, "retry_deadline": null}));

    // Applied now, as proposed.
    let proposal = settle(&setup, &p, 2, vec![json!({"goal_id": "g3", "use": "apply"})]).unwrap();
    assert!(proposal.goal_changes.is_empty());
    let made = goal(&setup, "g3");
    assert_eq!(made.history.unwrap().last(), Some(&entry(GoalStatus::Done, "10:00", "2026-01-01T09:30")));
    assert_eq!(proposal.settled_goal_changes.len(), 2);

    // Claude sends both again: neither is refused, and both stay settled.
    let result = revised(&setup, &p, 3, both).unwrap();
    assert!(!result.message.contains("goal change"), "{}", result.message);
    let context = setup.prepare().proposal.unwrap();
    assert_eq!(context.goal_changes.len(), 2);
    let uses: Vec<(String, String)> = context.settled_goal_changes.iter().map(|s| (s.goal_id.clone(), s.use_.clone())).collect();
    assert_eq!(uses, vec![("g1".into(), "drop".into()), ("g3".into(), "apply".into())]);

    // Changed: the user's status and time over Claude's, kept through
    // Claude's next revision; and a change of their own.
    let choices = vec![
        json!({"goal_id": "g1", "use": "change", "status": "obsolete", "at": py::isoformat(&time_at("10:00"))}),
        json!({"goal_id": "g2", "use": "change", "status": "missed", "retry_deadline": "2026-02-01"}),
    ];
    let proposal = settle(&setup, &p, 4, choices).unwrap();
    assert_eq!(shown_ids(&proposal), vec![("g1".into(), "obsolete".into(), "user".into()), ("g2".into(), "missed".into(), "user".into())]);
    assert_eq!(proposal.goal_changes[0]["at"], json!(py::pydantic_datetime(&time_at("10:00"))));
    assert_eq!(proposal.goal_changes[1]["at"], json!(py::pydantic_datetime(&time_at("11:30"))));
    revised(&setup, &p, 5, vec![change("g1", GoalStatus::Done, Some("10:20"))]).unwrap();
    assert_eq!(shown_ids(&setup.get_proposal()), vec![("g1".into(), "obsolete".into(), "user".into()), ("g2".into(), "missed".into(), "user".into())]);

    setup.call(|c| c.confirm(&p, 6)).unwrap();
    assert_eq!(goal(&setup, "g1").status, Some(GoalStatus::Obsolete));
    let fence = goal(&setup, "g2");
    assert_eq!((fence.status, fence.deadline.as_deref()), (Some(GoalStatus::Active), Some("2026-02-01")));
}

#[test]
fn a_users_goal_change_is_checked_as_claudes_is() {
    let setup = setup();
    let p = proposed(&setup, vec![change("g1", GoalStatus::Done, Some("10:20"))]).unwrap().proposal_id.unwrap();

    refused(settle(&setup, &p, 1, vec![json!({"goal_id": "g3", "use": "drop"})]), "g3 isn't a goal the proposal changes, so there's nothing to drop");
    refused(settle(&setup, &p, 1, vec![json!({"goal_id": "g3", "use": "apply"})]), "g3 isn't a goal the proposal changes: give its status");
    refused(settle(&setup, &p, 1, vec![json!({"goal_id": "g3", "use": "change"})]), "its change needs a status");
    refused(settle(&setup, &p, 1, vec![json!({"goal_id": "g2", "use": "change", "status": "done"})]), r"goal g3 \('Posts bought'\) is active, but its parent");
    refused(settle(&setup, &p, 1, vec![json!({"goal_id": "g1", "use": "stop"})]), "use is one of apply, drop, change");
    refused(settle(&setup, &p, 1, vec![json!({"goal_id": "g5", "use": "change", "status": "obsolete"})]), "is done already, not open");
    // An apply the goals refuse is refused whole: nothing is settled.
    setup.call(|c| c.goals.as_ref().unwrap().change_goals(&[], &[closed("g1")], &[], &chrono_tz::UTC)).unwrap();
    refused(settle(&setup, &p, 1, vec![json!({"goal_id": "g1", "use": "apply"})]), "is obsolete already, not open");
    assert!(setup.get_proposal().settled_goal_changes.is_empty());
}

/// `goal_id` made obsolete behind the proposal's back.
fn closed(goal_id: &str) -> GoalEdit {
    GoalEdit { goal: Goal { id: Some(goal_id.into()), status: Some(GoalStatus::Obsolete), version: Some(0), ..Default::default() }, clear_fields: Vec::new() }
}

#[test]
fn a_change_the_goals_refuse_at_apply_is_dropped_with_a_warning() {
    let setup = setup();
    let both = vec![change("g1", GoalStatus::Done, Some("10:20")), change("g4", GoalStatus::Obsolete, None)];
    let p = proposed(&setup, both).unwrap().proposal_id.unwrap();
    setup.call(|c| c.goals.as_ref().unwrap().change_goals(&[], &[closed("g1")], &[], &chrono_tz::UTC)).unwrap();

    let applied = setup.call(|c| c.confirm(&p, 1)).unwrap();

    assert_eq!(applied.status, "applied");
    let warnings = applied.proposal.unwrap().warnings;
    assert_eq!(warnings.len(), 1, "{warnings:?}");
    assert!(warnings[0].starts_with("the goal change to g1 (done) wasn't made: goal g1 ('Recipe sent to Sam') is obsolete already, not open."), "{}", warnings[0]);
    assert!(warnings[0].ends_with("Make it with update_goals if it still applies."));
    // The rest were made, and the warning is kept with the revision.
    assert_eq!(goal(&setup, "g4").status, Some(GoalStatus::Obsolete));
    assert_eq!(setup.call(|c| c.get_proposal(Some(&p), None)).unwrap().warnings, warnings);
}

#[test]
fn a_goal_change_made_already_is_left_as_it_is() {
    // An apply that stopped between making a change and recording it
    // makes it again on finishing: the goal is as it left it.
    let setup = setup();
    let store = setup.compactor.goals.as_ref().unwrap();
    let done = change("g1", GoalStatus::Done, Some("10:20"));
    let retry = GoalChange { retry_deadline: Some("2026-02-01".into()), ..change("g2", GoalStatus::Missed, Some("10:00")) };
    let apply = |c: &GoalChange| setup.call(|_| crate::compaction::goal_changes::apply(store, c, &chrono_tz::UTC));

    for c in [&done, &retry, &done, &retry] {
        apply(c).unwrap();
    }

    assert_eq!(goal(&setup, "g1").history.unwrap().len(), 2);
    assert_eq!(goal(&setup, "g2").history.unwrap().len(), 3);
    // The same goal changed otherwise is refused as update_goals would.
    refused_plainly(apply(&change("g1", GoalStatus::Obsolete, Some("10:20"))), "is done already, not open");
}

/// The error `result` failed with, matching `pattern`.
fn refused_plainly<T>(result: anyhow::Result<T>, pattern: &str) {
    let Err(error) = result else { panic!("{pattern:?} wasn't refused") };
    assert!(error.to_string().contains(pattern), "{pattern:?} isn't in: {error:#}");
}
