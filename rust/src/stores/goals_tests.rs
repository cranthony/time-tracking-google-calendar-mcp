//! The module's tests, against the fake Google.

use super::*;
use crate::testing::fake_google::FakeGoogle;
use crate::write_lock::WRITE_LOCK;
use chrono::{DateTime, FixedOffset};
use serde_json::{json, Value};

const ZONE: Tz = chrono_tz::America::New_York;

struct Fixture {
    fake: FakeGoogle,
    spreadsheet: String,
    goals: Goals,
}

fn fixture() -> Fixture {
    crate::determinism::TEST_NOW.with(|n| n.set(Some(at("2026-10-17T09:00:00-04:00"))));
    let fake = FakeGoogle::start();
    let sheets = SheetsClient::new(fake.google());
    let _held = WRITE_LOCK.hold();
    let spreadsheet = sheets.create_spreadsheet("Calendar Metadata").unwrap();
    let goals = Goals::ensure(&sheets, &spreadsheet).unwrap();
    Fixture { fake, spreadsheet, goals }
}

fn at(text: &str) -> DateTime<FixedOffset> {
    DateTime::parse_from_rfc3339(text).unwrap()
}

fn goal(given: Value) -> Goal {
    serde_json::from_value(given).unwrap()
}

fn edit(given: Value) -> GoalEdit {
    GoalEdit { goal: goal(given), clear_fields: Vec::new() }
}

fn json_of<T: serde::Serialize>(value: T) -> Value {
    serde_json::to_value(value).unwrap()
}

fn refused<T: std::fmt::Debug>(result: anyhow::Result<T>, pattern: &str) {
    let error = match result {
        Err(error) => error,
        Ok(value) => panic!("{pattern:?} wasn't refused: {value:?}"),
    };
    assert!(regex::Regex::new(pattern).unwrap().is_match(&error.to_string()), "{pattern:?} isn't in: {error}");
}

impl Fixture {
    fn create(&self, given: Value) -> anyhow::Result<String> {
        let _held = WRITE_LOCK.hold();
        Ok(self.goals.create_goal(&goal(given), &ZONE)?.created_id)
    }

    fn change(&self, creates: &[Value], updates: Vec<GoalEdit>, removals: Vec<GoalRemoval>) -> anyhow::Result<ChangedGoals> {
        let _held = WRITE_LOCK.hold();
        let creates: Vec<Goal> = creates.iter().cloned().map(goal).collect();
        self.goals.change_goals(&creates, &updates, &removals, &ZONE)
    }

    fn get(&self, id: &str) -> Value {
        json_of(self.goals.get_goal(id, &ZONE).unwrap())
    }
}

#[test]
fn creates_an_active_goal_with_its_history_and_finds_it_by_id_or_title() {
    let f = fixture();

    let id = f.create(json!({"title": "Ship the app", "deadline": "2026-11-01", "expected_minutes": 600})).unwrap();

    let made = f.get(&id);
    assert_eq!((made["status"].clone(), made["version"].clone()), (json!("active"), json!(1)));
    assert_eq!(made["history"], json!([{"status": "active", "at": "2026-10-17T09:00:00-04:00", "deadline": "2026-11-01"}]));
    assert_eq!(made["started_at"], json!("2026-10-17T09:00:00-04:00"));
    assert_eq!(made["closed_at"], Value::Null);
    assert_eq!(f.get("ship THE app")["id"], json!(id));
    refused(f.goals.get_goal("Ship teh app", &ZONE), r"no goal with the id or title 'Ship teh app'; did you mean .*Ship the app");
    assert!(f.fake.tab_titles(&f.spreadsheet).contains(&GOALS.1.to_string()));
}

#[test]
fn an_open_goal_needs_a_deadline_an_inactive_one_maybe_one_from_its_start() {
    let f = fixture();

    refused(f.create(json!({"title": "Someday"})), "is active, so it needs a deadline");
    refused(f.create(json!({"title": "Someday", "status": "inactive"})), "needs a deadline, to have when it starts");
    let id = f.create(json!({"title": "Someday", "status": "inactive", "deadline_rule": {"after_start": {"days": 3}}})).unwrap();
    let waiting = f.get(&id);
    assert_eq!((waiting["deadline"].clone(), waiting["due"].clone()), (json!({"after_start": {"days": 3}}), Value::Null));

    // Started, it's due the end of the third day after the one it started.
    f.change(&[], vec![edit(json!({"id": id, "status": "active"}))], vec![]).unwrap();
    let started = f.get(&id);
    assert_eq!((started["deadline"].clone(), started["due"].clone()), (json!({"after_start": {"days": 3}}), json!("2026-10-20")));
    assert_eq!(started["history"][1], json!({"status": "active", "at": "2026-10-17T09:00:00-04:00", "deadline": "2026-10-20"}));

    // In minutes, a date and time; given a date, it's untied.
    let timed = f.create(json!({"title": "Soon", "deadline_rule": {"after_start": {"minutes": 90}}})).unwrap();
    assert_eq!(f.get(&timed)["due"], json!("2026-10-17T10:30"));
    f.change(&[], vec![edit(json!({"id": timed, "deadline": "2026-12-01T17:00"}))], vec![]).unwrap();
    let untied = f.get(&timed);
    assert_eq!((untied["deadline"].clone(), untied["due"].clone()), (json!({"at": "2026-12-01T17:00"}), json!("2026-12-01T17:00")));
}

#[test]
fn lists_inactive_and_active_goals_by_default_with_their_paths() {
    let f = fixture();
    let top = f.create(json!({"title": "Top", "deadline": "2026-12-01"})).unwrap();
    let mid = f.create(json!({"title": "Mid", "parent_id": top, "deadline": "2026-11-20"})).unwrap();
    f.create(json!({"title": "Leaf", "parent_id": mid, "status": "inactive", "deadline": "2026-11-10"})).unwrap();
    f.create(json!({"title": "Gone", "status": "obsolete"})).unwrap();

    let listed = |statuses: Option<&[&str]>| -> Vec<(Value, Value)> {
        let statuses = statuses.map(crate::testing::strings);
        f.goals.get_goals(statuses.as_deref(), &ZONE).unwrap().into_iter().map(|g| (json!(g.title), json!(g.path))).collect()
    };
    assert_eq!(listed(None), vec![(json!("Top"), json!([])), (json!("Mid"), json!(["Top"])), (json!("Leaf"), json!(["Top", "Mid"]))]);
    assert_eq!(listed(Some(&["obsolete"])), vec![(json!("Gone"), json!([]))]);
    refused(f.goals.get_goals(Some(&crate::testing::strings(&["finished"])), &ZONE), "Unknown goal status");
}

#[test]
fn a_sub_goal_is_due_no_later_and_expected_no_longer_than_its_parent() {
    let f = fixture();
    let top = f.create(json!({"title": "Top", "deadline": "2026-11-20T17:00", "expected_minutes": 600})).unwrap();

    refused(f.create(json!({"title": "Late", "parent_id": top, "deadline": "2026-11-20"})), r"'Late'\)'s deadline \(2026-11-20\) is after its parent goal .* \('Top'\)'s \(2026-11-20T17:00\)");
    refused(f.create(json!({"title": "Long", "parent_id": top, "deadline": "2026-11-19", "expected_minutes": 601})), "expected to take longer \\(601 minutes\\) than its parent");
    let sub = f.create(json!({"title": "Sub", "parent_id": top, "deadline": "2026-11-20T12:00", "expected_minutes": 600})).unwrap();

    // Moving the parent's deadline earlier is refused, naming the sub-goal in the way: nothing moves by itself.
    refused(f.change(&[], vec![edit(json!({"id": top, "deadline": "2026-11-19"}))], vec![]), &format!("goal {sub} \\('Sub'\\)'s deadline"));
    assert_eq!(f.get(&sub)["due"], json!("2026-11-20T12:00"));
}

#[test]
fn a_tied_sub_goals_deadline_follows_its_parents_at_any_depth() {
    let f = fixture();
    let top = f.create(json!({"title": "Top", "deadline": "2026-11-20"})).unwrap();
    let mid = f.create(json!({"title": "Mid", "parent_id": top, "deadline_rule": {"before_parent": {"days": 1}}})).unwrap();
    let low = f.create(json!({"title": "Low", "parent_id": mid, "deadline_rule": {"before_parent": {"minutes": 60}}})).unwrap();
    assert_eq!((f.get(&mid)["due"].clone(), f.get(&low)["due"].clone()), (json!("2026-11-19"), json!("2026-11-19T23:00")));

    // Moving the parent's moves them all, and gives them as changed.
    let changed = f.change(&[], vec![edit(json!({"id": top, "deadline": "2026-11-10T17:00"}))], vec![]).unwrap();
    assert_eq!(changed.goals.len(), 3);
    assert_eq!((f.get(&mid)["due"].clone(), f.get(&low)["due"].clone()), (json!("2026-11-09T17:00"), json!("2026-11-09T16:00")));
    assert_eq!(f.get(&low)["version"], json!(2));
}

#[test]
fn a_deadline_of_its_own_or_a_move_to_the_top_unties_a_sub_goal() {
    let f = fixture();
    let top = f.create(json!({"title": "Top", "deadline": "2026-11-20"})).unwrap();
    let same = f.create(json!({"title": "Same", "parent_id": top, "deadline_rule": {"before_parent": {"days": 0}}})).unwrap();
    let own = f.create(json!({"title": "Own", "parent_id": top, "deadline_rule": {"before_parent": {"minutes": 0}}})).unwrap();
    assert_eq!(f.get(&same)["due"], json!("2026-11-20"));
    refused(f.create(json!({"title": "Odd", "parent_id": top, "deadline_rule": {"before_parent": {"minutes": -5}}})), "deadline can't be -5");
    refused(f.create(json!({"title": "Odd", "parent_id": top, "deadline_rule": {"before_parent": {"days": 1, "minutes": 5}}})), "gives days or minutes, one of them");
    refused(f.create(json!({"title": "Odd", "parent_id": top, "deadline_rule": {"before_parent": {"days": 1}, "after_start": {"days": 1}}})), "is measured from one thing");

    f.change(&[], vec![edit(json!({"id": own, "deadline": "2026-11-01"}))], vec![]).unwrap();
    let mut moved = edit(json!({"id": same}));
    moved.clear_fields = vec!["parent_id".into()];
    f.change(&[], vec![moved], vec![]).unwrap();
    f.change(&[], vec![edit(json!({"id": top, "deadline": "2026-11-25"}))], vec![]).unwrap();

    assert_eq!(f.get(&own)["deadline"], json!({"at": "2026-11-01"}));
    assert_eq!(f.get(&same)["deadline"], json!({"at": "2026-11-20"}));
}

#[test]
fn a_closed_sub_goal_keeps_the_deadline_it_had() {
    let f = fixture();
    let top = f.create(json!({"title": "Top", "deadline": "2026-11-20"})).unwrap();
    let sub = f.create(json!({"title": "Sub", "parent_id": top, "deadline_rule": {"before_parent": {"days": 7}}})).unwrap();
    f.change(&[], vec![edit(json!({"id": sub, "status": "done"}))], vec![]).unwrap();

    f.change(&[], vec![edit(json!({"id": top, "deadline": "2026-12-20"}))], vec![]).unwrap();

    assert_eq!((f.get(&sub)["due"].clone(), f.get(&sub)["deadline"].clone()), (json!("2026-11-13"), json!({"before_parent": {"days": 7}})));
}

#[test]
fn a_sub_goal_may_start_before_its_parent_but_never_after_its_own_deadline() {
    let f = fixture();
    let top = f.create(json!({"title": "Top", "deadline": "2026-12-01"})).unwrap();

    let early = json!([{"status": "active", "at": "2026-09-01T08:00:00-04:00"}]);
    f.create(json!({"title": "Groundwork", "parent_id": top, "deadline": "2026-10-01", "history": early})).unwrap();
    refused(
        f.create(json!({"title": "Backwards", "deadline": "2026-10-16", "history": [{"status": "active", "at": "2026-10-17T00:30:00-04:00"}]})),
        r"started \(2026-10-17T00:30:00-04:00\) after its deadline \(2026-10-16\)",
    );
}

#[test]
fn a_goal_cant_be_closed_with_open_sub_goals_but_can_be_with_them_closed_or_moved_at_once() {
    let f = fixture();
    let top = f.create(json!({"title": "Top", "deadline": "2026-12-01"})).unwrap();
    let a = f.create(json!({"title": "A", "parent_id": top, "deadline": "2026-11-01", "required": true})).unwrap();
    let b = f.create(json!({"title": "B", "parent_id": top, "status": "inactive", "deadline": "2026-11-10"})).unwrap();
    let c = f.create(json!({"title": "C", "parent_id": top, "deadline": "2026-11-15"})).unwrap();

    refused(f.change(&[], vec![edit(json!({"id": top, "status": "done"}))], vec![]), "is active, but its parent .* is done: mark it done, missed or obsolete, move it");

    // Done, with A done, B obsolete and C moved under a new top-level goal made in the same change.
    let changed = f
        .change(
            &[json!({"id": "later1", "title": "Prioritize unstructured goals", "deadline": "2026-12-31"})],
            vec![
                edit(json!({"id": top, "status": "done"})),
                edit(json!({"id": a, "status": "done"})),
                edit(json!({"id": b, "status": "obsolete"})),
                edit(json!({"id": c, "parent_id": "later1"})),
            ],
            vec![],
        )
        .unwrap();

    let titles: Vec<Value> = changed.goals.iter().map(|g| json!(g.title)).collect();
    assert_eq!(titles, vec![json!("Top"), json!("A"), json!("B"), json!("C"), json!("Prioritize unstructured goals")]);
    assert_eq!(f.get(&top)["closed_at"], json!("2026-10-17T09:00:00-04:00"));
    assert_eq!(f.get(&c)["path"], json!(["Prioritize unstructured goals"]));
    // Closed under a closed parent, a sub-goal can't be reopened until the parent is.
    refused(f.change(&[], vec![edit(json!({"id": a, "status": "active"}))], vec![]), "reopen its parent");
}

#[test]
fn a_missed_goal_is_closed_like_a_done_one() {
    let f = fixture();
    let top = f.create(json!({"title": "Top", "deadline": "2026-12-01"})).unwrap();
    let a = f.create(json!({"title": "A", "parent_id": top, "deadline": "2026-11-01", "required": true})).unwrap();

    // Missed, it can't keep an open sub-goal.
    refused(f.change(&[], vec![edit(json!({"id": top, "status": "missed"}))], vec![]), "is active, but its parent .* is missed");

    // A required sub-goal missed doesn't stop its parent being done.
    f.change(&[], vec![edit(json!({"id": a, "status": "missed"})), edit(json!({"id": top, "status": "done"}))], vec![]).unwrap();
    assert_eq!(f.get(&a)["closed_at"], json!("2026-10-17T09:00:00-04:00"));
    let missed: Vec<Option<String>> = f.goals.get_goals(Some(&crate::testing::strings(&["missed"])), &ZONE).unwrap().into_iter().map(|g| g.title).collect();
    assert_eq!(missed, vec![Some("A".to_string())]);
    assert!(f.goals.get_goals(None, &ZONE).unwrap().is_empty());

    // Reopened, with its parent.
    f.change(&[], vec![edit(json!({"id": top, "status": "active"})), edit(json!({"id": a, "status": "active"}))], vec![]).unwrap();
    assert_eq!(f.get(&a)["closed_at"], Value::Null);
}

#[test]
fn activating_a_goal_activates_its_inactive_parents_but_an_inactive_one_may_have_active_sub_goals() {
    let f = fixture();
    let top = f.create(json!({"title": "Top", "deadline": "2026-12-01"})).unwrap();
    let sub = f.create(json!({"title": "Sub", "parent_id": top, "deadline": "2026-11-01"})).unwrap();
    f.change(&[], vec![edit(json!({"id": top, "status": "inactive"}))], vec![]).unwrap();
    assert_eq!(f.get(&sub)["status"], json!("active"));

    // Activated by hand, it takes its inactive parents with it, each
    // with the deadline that follows from its start.
    let quiet = f.create(json!({"title": "Quiet", "status": "inactive", "deadline_rule": {"after_start": {"days": 30}}})).unwrap();
    let eager = f.create(json!({"title": "Eager", "parent_id": quiet, "status": "inactive", "deadline": "2026-11-01"})).unwrap();
    let changed = f.change(&[], vec![edit(json!({"id": eager, "status": "active"}))], vec![]).unwrap();
    assert_eq!(changed.goals.len(), 2);
    let parent = f.get(&quiet);
    assert_eq!((parent["status"].clone(), parent["due"].clone()), (json!("active"), json!("2026-11-16")));
    assert_eq!(parent["history"][1], json!({"status": "active", "at": "2026-10-17T09:00:00-04:00", "deadline": "2026-11-16"}));
}

#[test]
fn a_goal_cant_be_under_itself() {
    let f = fixture();
    let top = f.create(json!({"title": "Top", "status": "inactive", "deadline": "2026-12-01"})).unwrap();
    let sub = f.create(json!({"title": "Sub", "parent_id": top, "status": "inactive", "deadline": "2026-11-01"})).unwrap();

    refused(f.change(&[], vec![edit(json!({"id": top, "parent_id": top}))], vec![]), "can't be its own parent");
    refused(f.change(&[], vec![edit(json!({"id": top, "parent_id": sub}))], vec![]), "is under itself");
    refused(f.create(json!({"title": "Orphan", "parent_id": "nosuch", "status": "inactive", "deadline": "2026-11-01"})), "parent_id 'nosuch' isn't a goal");
}

#[test]
fn history_is_checked_and_can_be_edited_whole() {
    let f = fixture();
    let id = f.create(json!({"title": "Top", "deadline": "2026-12-01"})).unwrap();

    refused(f.change(&[], vec![edit(json!({"id": id, "history": []}))], vec![]), "history is empty");
    refused(
        f.change(&[], vec![edit(json!({"id": id, "history": [{"status": "inactive", "at": "2026-10-01T09:00:00Z"}]}))], vec![]),
        "history ends with inactive, but the goal is active",
    );
    refused(
        f.change(
            &[],
            vec![edit(json!({"id": id, "history": [
                {"status": "inactive", "at": "2026-10-02T09:00:00Z"},
                {"status": "active", "at": "2026-10-01T09:00:00Z"}
            ]}))],
            vec![],
        ),
        "entry 2 .* is before the one ahead of it",
    );
    // Backdated, and an entry deleted: the history as given.
    let backdated = json!([{"status": "active", "at": "2026-10-01T09:00:00-04:00"}]);
    f.change(&[], vec![edit(json!({"id": id, "history": backdated}))], vec![]).unwrap();
    assert_eq!(f.get(&id)["started_at"], json!("2026-10-01T09:00:00-04:00"));
}

#[test]
fn history_entries_carry_the_deadline_in_force_at_each_change() {
    let f = fixture();
    let now = "2026-10-17T09:00:00-04:00";

    // A create stamps the deadline it's made with; an inactive goal
    // without one yet is stamped without one, and reads back so.
    let id = f.create(json!({"title": "Top", "deadline": "2026-12-01"})).unwrap();
    assert_eq!(f.get(&id)["history"], json!([{"status": "active", "at": now, "deadline": "2026-12-01"}]));
    let someday = f.create(json!({"title": "Someday", "status": "inactive", "deadline_rule": {"after_start": {"days": 7}}})).unwrap();
    assert_eq!(f.get(&someday)["history"], json!([{"status": "inactive", "at": now, "deadline": null}]));

    // A status change stamps the deadline after the change, even one set
    // in the same update, or one that follows the parent's.
    f.change(&[], vec![edit(json!({"id": someday, "status": "active", "deadline": "2026-11-15"}))], vec![]).unwrap();
    assert_eq!(f.get(&someday)["history"][1], json!({"status": "active", "at": now, "deadline": "2026-11-15"}));
    let sub = f.create(json!({"title": "Sub", "parent_id": id, "deadline_rule": {"before_parent": {"days": 1}}})).unwrap();
    assert_eq!(f.get(&sub)["history"][0]["deadline"], json!("2026-11-30"));
    f.change(&[], vec![edit(json!({"id": sub, "status": "done"}))], vec![]).unwrap();
    assert_eq!(f.get(&sub)["history"][1], json!({"status": "done", "at": now, "deadline": "2026-11-30"}));

    // A history given whole keeps its entries' deadlines, with or without.
    let given = json!([
        {"status": "inactive", "at": "2026-10-01T09:00:00-04:00"},
        {"status": "active", "at": "2026-10-02T09:00:00-04:00", "deadline": "2026-10-15"},
        {"status": "active", "at": "2026-10-02T09:00:00-04:00", "deadline": "2026-10-16"}
    ]);
    refused(f.change(&[], vec![edit(json!({"id": id, "history": given}))], vec![]), "entry 3 repeats the status before it");
    let given = json!([
        {"status": "inactive", "at": "2026-10-01T09:00:00-04:00"},
        {"status": "active", "at": "2026-10-02T09:00:00-04:00", "deadline": "2026-10-15"}
    ]);
    f.change(&[], vec![edit(json!({"id": id, "deadline": "2026-10-15", "history": given.clone()}))], vec![]).unwrap();
    assert_eq!(f.get(&id)["history"], json!([{"status": "inactive", "at": "2026-10-01T09:00:00-04:00", "deadline": null}, given[1]]));

    // Missed, then retried: a new deadline and a history ending missed
    // (the deadline it missed) then active (the new one) at one moment.
    f.change(&[], vec![edit(json!({"id": id, "status": "missed"}))], vec![]).unwrap();
    let missed = f.get(&id);
    assert_eq!((missed["status"].clone(), missed["closed_at"].clone()), (json!("missed"), json!(now)));
    assert_eq!(missed["history"][2], json!({"status": "missed", "at": now, "deadline": "2026-10-15"}));
    let retried = json!([
        given[0], given[1],
        {"status": "missed", "at": "2026-10-16T00:00:00-04:00", "deadline": "2026-10-15"},
        {"status": "active", "at": "2026-10-16T00:00:00-04:00", "deadline": "2026-12-01"}
    ]);
    f.change(&[], vec![edit(json!({"id": id, "status": "active", "deadline": "2026-12-01", "history": retried.clone()}))], vec![]).unwrap();
    let again = f.get(&id);
    assert_eq!((again["status"].clone(), again["due"].clone(), again["closed_at"].clone()), (json!("active"), json!("2026-12-01"), Value::Null));
    assert_eq!(again["history"].as_array().unwrap()[2..], retried.as_array().unwrap()[2..]);
    // And missed again, for another go.
    f.change(&[], vec![edit(json!({"id": id, "status": "missed"}))], vec![]).unwrap();
    assert_eq!(f.get(&id)["history"][4], json!({"status": "missed", "at": now, "deadline": "2026-12-01"}));
}

#[test]
fn deletes_a_goal_with_its_sub_goals_or_once_they_re_moved() {
    let f = fixture();
    let top = f.create(json!({"title": "Top", "deadline": "2026-12-01"})).unwrap();
    let sub = f.create(json!({"title": "Sub", "parent_id": top, "deadline": "2026-11-01"})).unwrap();
    let removal = |id: &str| GoalRemoval { goal_id: id.into(), if_version: None };

    refused(f.change(&[], vec![], vec![removal(&top)]), &format!("goal {sub} \\('Sub'\\)'s parent_id '{top}' isn't a goal"));
    let mut cleared = edit(json!({"id": sub}));
    cleared.clear_fields = vec!["parent_id".into()];
    let changed = f.change(&[], vec![cleared], vec![removal(&top)]).unwrap();
    assert_eq!(changed.deleted_ids, vec![top.clone()]);
    assert_eq!(f.get(&sub)["path"], json!([]));

    let deeper = f.create(json!({"title": "Deeper", "parent_id": sub, "deadline": "2026-10-30"})).unwrap();
    f.change(&[], vec![], vec![removal(&sub), removal(&deeper)]).unwrap();
    assert!(f.goals.all().unwrap().is_empty());
}

#[test]
fn writes_sent_again_are_made_once() {
    let f = fixture();
    let made = json!({"id": "mine01", "title": "Top", "deadline": "2026-12-01"});
    f.create(made.clone()).unwrap();
    f.create(made).unwrap();
    assert_eq!(f.goals.all().unwrap().len(), 1);

    let done = || edit(json!({"id": "mine01", "status": "done", "version": 1}));
    f.change(&[], vec![done()], vec![]).unwrap();
    f.change(&[], vec![done()], vec![]).unwrap();
    let after = f.get("mine01");
    assert_eq!((after["version"].clone(), after["history"].as_array().unwrap().len()), (json!(2), 2));
    // A stale one is a conflict, not a second change.
    let conflict = f.change(&[], vec![edit(json!({"id": "mine01", "title": "Renamed", "version": 1}))], vec![]).unwrap_err();
    assert!(conflict.downcast_ref::<rows::Conflict>().is_some(), "{conflict}");

    let gone = || GoalRemoval { goal_id: "mine01".into(), if_version: Some(2) };
    f.change(&[], vec![], vec![gone()]).unwrap();
    f.change(&[], vec![], vec![gone()]).unwrap();
}

#[test]
fn deadlines_fall_due_at_the_end_of_their_day_unless_given_a_time() {
    let day = deadline_end("2026-11-01").unwrap();
    assert_eq!(day.to_string(), "2026-11-02 00:00:00");
    assert_eq!(deadline_end("2026-11-01T17:00").unwrap().to_string(), "2026-11-01 17:00:00");
    assert_eq!(deadline_end("Friday"), None);
}

// -- goal shares -----------------------------------------------------------------

fn share(id: &str, minutes: Option<i64>, percent: Option<i64>) -> GoalShare {
    GoalShare { goal_id: id.into(), minutes, percent, remaining: false }
}

#[test]
fn shares_are_kept_as_text_and_read_back() {
    let shares = vec![share("a", None, None), share("b", Some(45), None), share("c", None, Some(30))];

    assert_eq!(shares_text(&shares), "a b=45m c=30%");
    assert_eq!(parse_shares::<GoalShare>("a b=45m c=30%"), shares);
    assert_eq!(parse_shares::<GoalShare>("a =5m b=lots c=10%"), vec![share("a", None, None), share("c", None, Some(10))]);
}

#[test]
fn shares_fit_in_their_event() {
    let ok = vec![share("a", Some(40), None), share("b", Some(20), None), share("c", None, Some(60)), share("d", None, Some(40)), share("e", None, None)];
    assert!(share_problems(&ok, 60).is_empty());

    let problems = share_problems(&[share("a", Some(40), None), share("b", Some(21), None)], 60);
    assert_eq!(problems, vec!["its goals' minutes add up to 61, more than the event's 60"]);
    let problems = share_problems(&[share("a", None, Some(70)), share("b", None, Some(31)), share("a", None, None)], 60);
    assert_eq!(problems, vec!["goal a is given more than once", "its goals' percents add up to 101, more than 100"]);
    let problems = share_problems(&[share("a", Some(5), Some(5)), share("b", Some(0), None), share("c", None, Some(0)), share("", None, None)], 60);
    assert_eq!(problems.len(), 4, "{problems:?}");
}

#[test]
fn shares_read_from_a_shortened_event_are_cut_to_fit_and_deleted_goals_left_out() {
    let shares = vec![share("a", Some(40), None), share("gone", None, None), share("b", Some(30), None), share("c", None, Some(50)), share("d", None, Some(80))];
    let known = |id: &str| id != "gone";

    let settled = settled_shares(&shares, 60, &known);

    assert_eq!(settled, vec![share("a", Some(40), None), share("b", Some(20), None), share("c", None, Some(50)), share("d", None, Some(50))]);
}

#[test]
fn a_goal_keeps_its_priority_until_cleared_and_it_cant_be_negative() {
    let f = fixture();
    let id = f.create(json!({"title": "Ship", "deadline": "2026-11-01", "priority": 1})).unwrap();
    assert_eq!(f.get(&id)["priority"], json!(1));

    refused(f.create(json!({"title": "Odd", "deadline": "2026-11-01", "priority": -1})), r"'Odd'\)'s priority must be 0 or more, not -1");
    refused(f.change(&[], vec![edit(json!({"id": id, "priority": -2, "version": 1}))], vec![]), "priority must be 0 or more, not -2");

    f.change(&[], vec![edit(json!({"id": id, "priority": 0, "version": 1}))], vec![]).unwrap();
    assert_eq!(f.get(&id)["priority"], json!(0));
    let mut cleared = edit(json!({"id": id, "version": 2}));
    cleared.clear_fields = vec!["priority".into()];
    f.change(&[], vec![cleared], vec![]).unwrap();
    assert_eq!(f.get(&id)["priority"], Value::Null);
}

#[test]
fn a_goal_keeps_its_sort_order_until_cleared() {
    let f = fixture();
    let id = f.create(json!({"title": "Ship", "deadline": "2026-11-01", "sort_order": 2000})).unwrap();
    assert_eq!(f.get(&id)["sort_order"], json!(2000));

    f.change(&[], vec![edit(json!({"id": id, "sort_order": -5, "version": 1}))], vec![]).unwrap();
    assert_eq!(f.get(&id)["sort_order"], json!(-5));
    let mut cleared = edit(json!({"id": id, "version": 2}));
    cleared.clear_fields = vec!["sort_order".into()];
    f.change(&[], vec![cleared], vec![]).unwrap();
    assert_eq!(f.get(&id)["sort_order"], Value::Null);
}

#[test]
fn a_goal_names_the_people_its_done_with_and_for() {
    let mut f = fixture();
    f.goals = f.goals.clone().with_people(Arc::new(|| Ok(vec!["self".into(), "sam".into(), "ana".into()])));
    let id = f.create(json!({"title": "Help Sam move", "deadline": "2026-11-01", "with_ids": ["sam"], "for_ids": ["ana"]})).unwrap();
    assert_eq!((f.get(&id)["with_ids"].clone(), f.get(&id)["for_ids"].clone()), (json!(["sam"]), json!(["ana"])));

    refused(f.create(json!({"title": "Odd", "deadline": "2026-11-01", "with_ids": ["sam"], "for_ids": ["sam"]})), r"names \['sam'\] both with");
    refused(f.create(json!({"title": "Odd", "deadline": "2026-11-01", "for_ids": ["self"]})), r#"can't name "self""#);
    refused(f.create(json!({"title": "Odd", "deadline": "2026-11-01", "with_ids": ["bo"]})), r"names \['bo'\], who aren't people");

    let mut cleared = edit(json!({"id": id, "version": 1}));
    cleared.clear_fields = vec!["for_ids".into()];
    f.change(&[], vec![cleared], vec![]).unwrap();
    assert_eq!((f.get(&id)["with_ids"].clone(), f.get(&id)["for_ids"].clone()), (json!(["sam"]), Value::Null));
}

fn set_now(text: &str) {
    crate::determinism::TEST_NOW.with(|n| n.set(Some(at(text))));
}

#[test]
fn a_goal_waiting_for_another_to_close_is_activated_when_it_does_dated_then() {
    let f = fixture();
    let first = f.create(json!({"title": "Draft", "deadline": "2026-10-20"})).unwrap();
    let next = f
        .create(json!({"title": "Send", "status": "inactive", "deadline_rule": {"after_start": {"days": 2}}, "activate_when": {"after": [{"goal_closed": first}]}}))
        .unwrap();
    let waiting = f.get(&next);
    assert_eq!((waiting["status"].clone(), waiting["expected_start"].clone()), (json!("inactive"), json!("2026-10-21")));

    // Missed, then tried again by a later day: still waiting, expected later.
    let retried = json!([
        f.get(&first)["history"][0],
        {"status": "missed", "at": "2026-10-17T09:00:00-04:00", "deadline": "2026-10-20"},
        {"status": "active", "at": "2026-10-17T09:00:00-04:00", "deadline": "2026-10-24"}
    ]);
    f.change(&[], vec![edit(json!({"id": first, "deadline": "2026-10-24", "history": retried}))], vec![]).unwrap();
    assert_eq!(f.get(&next)["expected_start"], json!("2026-10-25"));

    // Done later: activated as of then, its deadline from that start, what
    // it waited for cleared.
    set_now("2026-10-18T14:00:00-04:00");
    let changed = f.change(&[], vec![edit(json!({"id": first, "status": "done"}))], vec![]).unwrap();
    assert!(changed.goals.iter().any(|g| g.id.as_deref() == Some(next.as_str())));
    let started = f.get(&next);
    assert_eq!((started["status"].clone(), started["due"].clone(), started["activate_when"].clone()), (json!("active"), json!("2026-10-20"), Value::Null));
    assert_eq!(started["history"][1], json!({"status": "active", "at": "2026-10-18T14:00:00-04:00", "deadline": "2026-10-20"}));
}

#[test]
fn a_time_come_activates_a_goal_on_the_next_read_and_the_next_write_keeps_it() {
    let f = fixture();
    let id = f.create(json!({"title": "Plan the trip", "status": "inactive", "deadline": "2026-10-30", "activate_when": {"after": [{"at": "2026-10-19"}]}})).unwrap();
    assert_eq!(f.get(&id)["expected_start"], json!("2026-10-19"));

    set_now("2026-10-20T08:00:00-04:00");
    let read = f.get(&id);
    assert_eq!((read["status"].clone(), read["history"][1]["at"].clone()), (json!("active"), json!("2026-10-19T00:00:00-04:00")));
    // Not written by the read; written, as read, by the next change.
    assert_eq!(f.goals.all().unwrap()[0].status, Some(GoalStatus::Inactive));
    f.create(json!({"title": "Other", "deadline": "2026-11-01"})).unwrap();
    let kept = f.goals.all().unwrap().into_iter().find(|g| g.id.as_deref() == Some(id.as_str())).unwrap();
    assert_eq!((kept.status, kept.activate_when), (Some(GoalStatus::Active), None));
}

#[test]
fn a_delay_waits_until_the_next_day_or_some_minutes() {
    let f = fixture();
    let first = f.create(json!({"title": "First", "deadline": "2026-10-20"})).unwrap();
    let tomorrow = f
        .create(json!({"title": "Tomorrow", "status": "inactive", "deadline": "2026-10-30", "activate_when": {"after": [{"goal_closed": first}], "delay": "next_day"}}))
        .unwrap();
    let later = f
        .create(json!({"title": "Later", "status": "inactive", "deadline": "2026-10-30", "activate_when": {"after": [{"goal_closed": first}], "delay": {"minutes": 90}}}))
        .unwrap();
    f.change(&[], vec![edit(json!({"id": first, "status": "done"}))], vec![]).unwrap();
    assert_eq!((f.get(&tomorrow)["expected_start"].clone(), f.get(&later)["expected_start"].clone()), (json!("2026-10-18"), json!("2026-10-17T10:30")));

    set_now("2026-10-17T11:00:00-04:00");
    assert_eq!((f.get(&tomorrow)["status"].clone(), f.get(&later)["status"].clone()), (json!("inactive"), json!("active")));
    assert_eq!(f.get(&later)["history"][1]["at"], json!("2026-10-17T10:30:00-04:00"));
    set_now("2026-10-18T06:00:00-04:00");
    assert_eq!(f.get(&tomorrow)["history"][1]["at"], json!("2026-10-18T00:00:00-04:00"));
}

#[test]
fn a_hold_keeps_a_goal_waiting_while_enough_others_of_its_priority_are_active() {
    let f = fixture();
    let a = f.create(json!({"title": "A", "priority": 1, "deadline": "2026-11-01"})).unwrap();
    f.create(json!({"title": "B", "priority": 0, "deadline": "2026-11-01"})).unwrap();
    f.create(json!({"title": "Low", "priority": 3, "deadline": "2026-11-01"})).unwrap();
    let held = json!({"after": [{"at": "2026-10-10"}], "hold_while": [{"active_at_least": 2, "priority": {"at_or_above": "own"}}]});
    let wait = f.create(json!({"title": "Wait", "priority": 1, "status": "inactive", "deadline": "2026-11-10", "activate_when": held})).unwrap();
    let exact = json!({"hold_while": [{"active_at_least": 1, "priority": {"exactly": 3}}]});
    let low = f.create(json!({"title": "Wait low", "status": "inactive", "deadline": "2026-11-10", "activate_when": exact})).unwrap();
    let listed = f.get(&wait);
    assert_eq!((listed["status"].clone(), listed["expected_start"].clone()), (json!("inactive"), Value::Null));
    assert_eq!(f.get(&low)["status"], json!("inactive"));

    // One of them done: room for it, dated then.
    set_now("2026-10-17T12:00:00-04:00");
    f.change(&[], vec![edit(json!({"id": a, "status": "done"}))], vec![]).unwrap();
    let started = f.get(&wait);
    assert_eq!((started["status"].clone(), started["history"][1]["at"].clone()), (json!("active"), json!("2026-10-17T12:00:00-04:00")));
}

#[test]
fn of_two_ready_for_one_place_the_higher_priority_goes_first() {
    let f = fixture();
    f.create(json!({"title": "Busy", "priority": 2, "deadline": "2026-11-01"})).unwrap();
    let hold = json!({"hold_while": [{"active_at_least": 2}]});
    let (minor, major) = ("minor", "major");
    f.change(
        &[
            json!({"id": minor, "title": "Minor", "priority": 2, "status": "inactive", "deadline": "2026-11-01", "activate_when": hold}),
            json!({"id": major, "title": "Major", "priority": 0, "status": "inactive", "deadline": "2026-11-05", "activate_when": hold}),
        ],
        vec![],
        vec![],
    )
    .unwrap();
    assert_eq!((f.get(major)["status"].clone(), f.get(minor)["status"].clone()), (json!("active"), json!("inactive")));
}

#[test]
fn a_goal_activated_waiting_takes_its_inactive_parents_and_by_hand_ignores_what_it_waits_for() {
    let f = fixture();
    let parent = f.create(json!({"title": "Parent", "status": "inactive", "deadline": "2026-12-01"})).unwrap();
    let child = f
        .create(json!({"title": "Child", "parent_id": parent, "status": "inactive", "deadline": "2026-11-01", "activate_when": {"after": [{"at": "2026-10-16"}]}}))
        .unwrap();
    assert_eq!((f.get(&child)["status"].clone(), f.get(&parent)["status"].clone()), (json!("active"), json!("active")));

    let later = f.create(json!({"title": "Later", "status": "inactive", "deadline": "2026-12-01", "activate_when": {"after": [{"at": "2026-11-20"}]}})).unwrap();
    f.change(&[], vec![edit(json!({"id": later, "status": "active"}))], vec![]).unwrap();
    assert_eq!((f.get(&later)["status"].clone(), f.get(&later)["activate_when"].clone()), (json!("active"), Value::Null));
}

#[test]
fn a_goal_past_its_deadline_by_the_time_it_could_start_keeps_waiting() {
    let f = fixture();
    let id = f.create(json!({"title": "Late", "status": "inactive", "deadline": "2026-10-16", "activate_when": {"after": [{"at": "2026-10-17"}]}})).unwrap();
    assert_eq!(f.get(&id)["status"], json!("inactive"));
}

#[test]
fn what_a_goal_waits_for_is_checked() {
    let f = fixture();
    let top = f.create(json!({"title": "Top", "deadline": "2026-12-01"})).unwrap();
    let a = f.create(json!({"title": "A", "status": "inactive", "deadline": "2026-11-01"})).unwrap();
    let waits = |after: Value| json!({"title": "W", "parent_id": top, "status": "inactive", "deadline": "2026-11-01", "activate_when": {"after": after}});

    refused(f.create(waits(json!([{"goal_closed": "nosuch"}]))), "waits for goal 'nosuch', which isn't a goal");
    refused(f.create(waits(json!([{"goal_closed": top}]))), "it's one of its parents");
    refused(f.create(waits(json!([{"at": "soon"}]))), "'soon', which isn't a date");
    refused(f.create(waits(json!([{}]))), "give goal_closed or at, one of them");
    refused(f.change(&[], vec![edit(json!({"id": top, "activate_when": {}}))], vec![]), "is only for an inactive goal: this one is active");
    refused(
        f.create(json!({"title": "H", "status": "inactive", "deadline": "2026-11-01", "activate_when": {"hold_while": [{"active_at_least": 0, "priority": {}}]}})),
        "give 1 or more.*at_or_above or exactly, one of them",
    );
    // Round, through another.
    let b = f.create(json!({"title": "B", "status": "inactive", "deadline": "2026-11-01", "activate_when": {"after": [{"goal_closed": a}]}})).unwrap();
    refused(f.change(&[], vec![edit(json!({"id": a, "activate_when": {"after": [{"goal_closed": b}]}}))], vec![]), "waits, through others, for the goal itself");
    refused(f.change(&[], vec![edit(json!({"id": a, "activate_when": {"after": [{"goal_closed": a}]}}))], vec![]), "can't wait for the goal itself");
}

#[test]
fn a_deadline_measured_from_another_goals_follows_it_and_never_comes_round() {
    let f = fixture();
    let first = f.create(json!({"title": "First", "deadline": "2026-10-20"})).unwrap();
    let after = f.create(json!({"title": "After", "deadline_rule": {"after_goal": {"id": first, "days": 3}}})).unwrap();
    assert_eq!(f.get(&after)["due"], json!("2026-10-23"));
    f.change(&[], vec![edit(json!({"id": first, "deadline": "2026-10-21T17:00"}))], vec![]).unwrap();
    assert_eq!(f.get(&after)["due"], json!("2026-10-24T17:00"));

    refused(f.change(&[], vec![edit(json!({"id": first, "deadline_rule": {"after_goal": {"id": after, "minutes": 0}}}))], vec![]), "is measured, through others, from its own");
    refused(f.create(json!({"title": "Odd", "deadline_rule": {"after_goal": {"id": "nosuch", "days": 1}}})), "after goal 'nosuch', which isn't a goal");

    // Deleting what it's measured from keeps the date it had; what waited
    // for it doesn't any more.
    let waiting = f.create(json!({"title": "Waiting", "status": "inactive", "deadline": "2026-12-01", "activate_when": {"after": [{"goal_closed": first}, {"at": "2026-11-01"}]}})).unwrap();
    f.change(&[], vec![], vec![GoalRemoval { goal_id: first.clone(), if_version: None }]).unwrap();
    assert_eq!(f.get(&after)["deadline"], json!({"at": "2026-10-24T17:00"}));
    assert_eq!(f.get(&waiting)["activate_when"], json!({"after": [{"at": "2026-11-01"}]}));
}

#[test]
fn a_deadline_offset_from_before_deadline_rule_reads_as_one_before_the_parents() {
    let f = fixture();
    let top = f.create(json!({"title": "Top", "deadline": "2026-11-20"})).unwrap();
    let sub = f.create(json!({"title": "Sub", "parent_id": top, "deadline": "2026-11-19", "deadline_offset_minutes": 1440})).unwrap();
    assert_eq!(f.get(&sub)["deadline"], json!({"before_parent": {"days": 1}}));
    f.change(&[], vec![edit(json!({"id": top, "deadline": "2026-11-25"}))], vec![]).unwrap();
    assert_eq!(f.get(&sub)["due"], json!("2026-11-24"));
}
