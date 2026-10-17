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
        json_of(self.goals.get_goal(id).unwrap())
    }
}

#[test]
fn creates_an_active_goal_with_its_history_and_finds_it_by_id_or_title() {
    let f = fixture();

    let id = f.create(json!({"title": "Ship the app", "deadline": "2026-11-01", "expected_minutes": 600})).unwrap();

    let made = f.get(&id);
    assert_eq!((made["status"].clone(), made["version"].clone()), (json!("active"), json!(1)));
    assert_eq!(made["history"], json!([{"status": "active", "at": "2026-10-17T09:00:00-04:00"}]));
    assert_eq!(made["started_at"], json!("2026-10-17T09:00:00-04:00"));
    assert_eq!(made["closed_at"], Value::Null);
    assert_eq!(f.get("ship THE app")["id"], json!(id));
    refused(f.goals.get_goal("Ship teh app"), r"no goal with the id or title 'Ship teh app'; did you mean .*Ship the app");
    assert!(f.fake.tab_titles(&f.spreadsheet).contains(&GOALS.1.to_string()));
}

#[test]
fn an_active_goal_needs_a_deadline_but_an_inactive_one_doesnt() {
    let f = fixture();

    refused(f.create(json!({"title": "Someday"})), "is active, so it needs a deadline");
    let id = f.create(json!({"title": "Someday", "status": "inactive"})).unwrap();

    refused(f.change(&[], vec![edit(json!({"id": id, "status": "active"}))], vec![]), "is active, so it needs a deadline");
    f.change(&[], vec![edit(json!({"id": id, "status": "active", "deadline": "2026-12-01T17:00"}))], vec![]).unwrap();
    let history: Vec<Value> = f.get(&id)["history"].as_array().unwrap().iter().map(|c| c["status"].clone()).collect();
    assert_eq!(history, vec![json!("inactive"), json!("active")]);
}

#[test]
fn lists_inactive_and_active_goals_by_default_with_their_paths() {
    let f = fixture();
    let top = f.create(json!({"title": "Top", "deadline": "2026-12-01"})).unwrap();
    let mid = f.create(json!({"title": "Mid", "parent_id": top, "deadline": "2026-11-20"})).unwrap();
    f.create(json!({"title": "Leaf", "parent_id": mid, "status": "inactive"})).unwrap();
    f.create(json!({"title": "Gone", "status": "obsolete"})).unwrap();

    let listed = |statuses: Option<&[&str]>| -> Vec<(Value, Value)> {
        let statuses = statuses.map(crate::testing::strings);
        f.goals.get_goals(statuses.as_deref()).unwrap().into_iter().map(|g| (json!(g.title), json!(g.path))).collect()
    };
    assert_eq!(listed(None), vec![(json!("Top"), json!([])), (json!("Mid"), json!(["Top"])), (json!("Leaf"), json!(["Top", "Mid"]))]);
    assert_eq!(listed(Some(&["obsolete"])), vec![(json!("Gone"), json!([]))]);
    refused(f.goals.get_goals(Some(&crate::testing::strings(&["finished"]))), "Unknown goal status");
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
    assert_eq!(f.get(&sub)["deadline"], json!("2026-11-20T12:00"));
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
    let b = f.create(json!({"title": "B", "parent_id": top, "status": "inactive"})).unwrap();
    let c = f.create(json!({"title": "C", "parent_id": top, "deadline": "2026-11-15"})).unwrap();

    refused(f.change(&[], vec![edit(json!({"id": top, "status": "done"}))], vec![]), "is active, but its parent .* is done: mark it done or obsolete, move it");

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
fn an_inactive_goal_has_no_active_sub_goals() {
    let f = fixture();
    let top = f.create(json!({"title": "Top", "deadline": "2026-12-01"})).unwrap();
    f.create(json!({"title": "Sub", "parent_id": top, "deadline": "2026-11-01"})).unwrap();

    refused(f.change(&[], vec![edit(json!({"id": top, "status": "inactive"}))], vec![]), "is active, but its parent .* is inactive");
    let quiet = f.create(json!({"title": "Quiet", "status": "inactive"})).unwrap();
    refused(f.create(json!({"title": "Eager", "parent_id": quiet, "deadline": "2026-11-01"})), "activate the parent too");
}

#[test]
fn a_goal_cant_be_under_itself() {
    let f = fixture();
    let top = f.create(json!({"title": "Top", "status": "inactive"})).unwrap();
    let sub = f.create(json!({"title": "Sub", "parent_id": top, "status": "inactive"})).unwrap();

    refused(f.change(&[], vec![edit(json!({"id": top, "parent_id": top}))], vec![]), "can't be its own parent");
    refused(f.change(&[], vec![edit(json!({"id": top, "parent_id": sub}))], vec![]), "is under itself");
    refused(f.create(json!({"title": "Orphan", "parent_id": "nosuch", "status": "inactive"})), "parent_id 'nosuch' isn't a goal");
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
    GoalShare { goal_id: id.into(), minutes, percent }
}

#[test]
fn shares_are_kept_as_text_and_read_back() {
    let shares = vec![share("a", None, None), share("b", Some(45), None), share("c", None, Some(30))];

    assert_eq!(shares_text(&shares), "a b=45m c=30%");
    assert_eq!(parse_shares("a b=45m c=30%"), shares);
    assert_eq!(parse_shares("a =5m b=lots c=10%"), vec![share("a", None, None), share("c", None, Some(10))]);
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
