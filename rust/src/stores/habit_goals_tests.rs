//! The module's tests.

use super::*;

const ZONE: Tz = chrono_tz::America::New_York;

fn day(text: &str) -> NaiveDate {
    NaiveDate::parse_from_str(text, "%Y-%m-%d").unwrap()
}

fn at(text: &str) -> NaiveDateTime {
    NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M").unwrap()
}

fn weekly() -> Template {
    Template { habit_id: "hab1".into(), name: "Call Mom".into(), goal: RecurringGoal { period: Some(Period::Week), ..RecurringGoal::default() } }
}

/// A goal of weekly()'s, closed `status` at `closed` (local).
fn closed(id: &str, deadline: &str, status: GoalStatus, closed: &str) -> Goal {
    Goal {
        id: Some(id.into()),
        title: Some("Call Mom".into()),
        habit_id: Some("hab1".into()),
        deadline: Some(deadline.into()),
        status: Some(status),
        history: Some(vec![
            StatusChange { status: GoalStatus::Active, at: stamp(at("2026-10-25T00:00"), &ZONE), deadline: Some(deadline.into()), note: None },
            StatusChange { status, at: stamp(at(closed), &ZONE), deadline: Some(deadline.into()), note: None },
        ]),
        ..Goal::default()
    }
}

#[test]
fn periods_follow_the_calendar_weeks_from_sunday() {
    // 2026-10-28 is a Wednesday.
    assert_eq!(period_of(day("2026-10-28"), Period::Week), (day("2026-10-25"), day("2026-10-31")));
    assert_eq!(period_of(day("2026-10-25"), Period::Week), (day("2026-10-25"), day("2026-10-31")));
    assert_eq!(period_of(day("2026-10-31"), Period::Week), (day("2026-10-25"), day("2026-10-31")));
    assert_eq!(period_of(day("2026-12-15"), Period::Month), (day("2026-12-01"), day("2026-12-31")));
    assert_eq!(period_of(day("2026-02-10"), Period::Month), (day("2026-02-01"), day("2026-02-28")));
    assert_eq!(period_of(day("2026-10-28"), Period::Day), (day("2026-10-28"), day("2026-10-28")));
    assert_eq!(next_period(day("2026-10-31"), Period::Week), (day("2026-11-01"), day("2026-11-07")));
    assert_eq!(next_period(day("2026-12-31"), Period::Month), (day("2027-01-01"), day("2027-01-31")));
}

#[test]
fn quarters_run_from_january_and_years_are_calendar_years() {
    assert_eq!(period_of(day("2026-02-14"), Period::Quarter), (day("2026-01-01"), day("2026-03-31")));
    assert_eq!(period_of(day("2026-05-01"), Period::Quarter), (day("2026-04-01"), day("2026-06-30")));
    assert_eq!(period_of(day("2026-11-30"), Period::Quarter), (day("2026-10-01"), day("2026-12-31")));
    assert_eq!(next_period(day("2026-12-31"), Period::Quarter), (day("2027-01-01"), day("2027-03-31")));
    assert_eq!(period_of(day("2026-07-04"), Period::Year), (day("2026-01-01"), day("2026-12-31")));
    assert_eq!(next_period(day("2026-07-04"), Period::Year), (day("2027-01-01"), day("2027-12-31")));
}

#[test]
fn the_longer_the_period_the_fewer_a_goal_rate_counts() {
    let counts: Vec<i64> = [Period::Day, Period::Week, Period::Month, Period::Quarter, Period::Year].iter().map(|p| p.default_rate_periods()).collect();
    assert_eq!(counts, vec![7, 6, 4, 4, 2]);
}

#[test]
fn a_goals_id_names_its_habit_and_nothing_else_does() {
    assert_eq!(goal_id("hab1", day("2026-11-01")), "hab1_2026-11-01");
    assert_eq!(habit_of_id("hab1_2026-11-01"), Some("hab1"));
    assert_eq!(habit_of_id("hab1"), None);
    assert_eq!(habit_of_id("hab1-2026-11-01"), None);
    assert_eq!(habit_of_id("hab1_soon"), None);
    assert_eq!(habit_of_id("_2026-11-01"), None);
}

#[test]
fn a_habit_with_no_goal_gets_the_period_under_ways_from_its_start() {
    let mut goals = Vec::new();
    let made = renew(&mut goals, &[weekly()], &ZONE, at("2026-10-28T09:30"));
    assert_eq!(made, vec!["hab1_2026-10-25"]);
    let goal = &goals[0];
    assert_eq!(goal.title.as_deref(), Some("Call Mom"));
    assert_eq!(goal.habit_id.as_deref(), Some("hab1"));
    assert_eq!(goal.status, Some(GoalStatus::Active));
    assert_eq!(goal.deadline.as_deref(), Some("2026-10-31"));
    let history = goal.history.as_deref().unwrap();
    assert_eq!(history.len(), 1);
    assert_eq!(history[0].at, stamp(at("2026-10-25T00:00"), &ZONE));
    // Made the same again, while it isn't written.
    assert!(renew(&mut goals, &[weekly()], &ZONE, at("2026-10-28T10:00")).is_empty());
}

#[test]
fn the_template_gives_the_goals_fields_and_title() {
    let template = Template {
        goal: RecurringGoal {
            title: Some("Phone Mom".into()),
            priority: Some(1),
            expected_minutes: Some(30),
            attribution_hints: Some("a call with Mom".into()),
            ..weekly().goal
        },
        ..weekly()
    };
    let mut goals = Vec::new();
    renew(&mut goals, &[template], &ZONE, at("2026-10-28T09:30"));
    assert_eq!(goals[0].title.as_deref(), Some("Phone Mom"));
    assert_eq!(goals[0].priority, Some(1));
    assert_eq!(goals[0].expected_minutes, Some(30));
    assert_eq!(goals[0].attribution_hints.as_deref(), Some("a call with Mom"));
}

#[test]
fn an_open_goal_is_left_alone() {
    let mut goals = vec![Goal { status: Some(GoalStatus::Active), ..closed("hab1_2026-10-25", "2026-10-31", GoalStatus::Active, "2026-10-25T00:00") }];
    assert!(renew(&mut goals, &[weekly()], &ZONE, at("2026-11-03T09:00")).is_empty());
    assert_eq!(goals.len(), 1);
}

#[test]
fn a_goal_done_early_has_the_next_wait_for_its_period() {
    let mut goals = vec![closed("hab1_2026-10-25", "2026-10-31", GoalStatus::Done, "2026-10-27T18:00")];
    assert!(renew(&mut goals, &[weekly()], &ZONE, at("2026-10-31T23:59")).is_empty());
    assert_eq!(renew(&mut goals, &[weekly()], &ZONE, at("2026-11-01T08:00")), vec!["hab1_2026-11-01"]);
    let next = &goals[1];
    assert_eq!(next.deadline.as_deref(), Some("2026-11-07"));
    assert_eq!(next.history.as_deref().unwrap()[0].at, stamp(at("2026-11-01T00:00"), &ZONE));
}

#[test]
fn a_goal_settled_done_after_its_period_has_the_next_start_then() {
    let mut goals = vec![closed("hab1_2026-10-25", "2026-10-31", GoalStatus::Done, "2026-11-02T07:00")];
    renew(&mut goals, &[weekly()], &ZONE, at("2026-11-02T07:05"));
    assert_eq!(goals[1].id.as_deref(), Some("hab1_2026-11-01"));
    assert_eq!(goals[1].history.as_deref().unwrap()[0].at, stamp(at("2026-11-02T07:00"), &ZONE));
}

#[test]
fn a_missed_goal_is_tried_again_through_the_next_period() {
    let mut goals = vec![closed("hab1_2026-10-25", "2026-10-31", GoalStatus::Missed, "2026-11-01T00:00")];
    assert_eq!(renew(&mut goals, &[weekly()], &ZONE, at("2026-11-02T07:05")), vec!["hab1_2026-10-25"]);
    assert_eq!(goals.len(), 1);
    let goal = &goals[0];
    assert_eq!(goal.status, Some(GoalStatus::Active));
    assert_eq!(goal.deadline.as_deref(), Some("2026-11-07"));
    let history = goal.history.as_deref().unwrap();
    assert_eq!(history.len(), 3);
    assert_eq!((history[1].status, history[1].deadline.as_deref()), (GoalStatus::Missed, Some("2026-10-31")));
    assert_eq!((history[2].status, history[2].deadline.as_deref()), (GoalStatus::Active, Some("2026-11-07")));
    assert_eq!(history[2].at, history[1].at);
    // Done in its second week, the next is the week after.
    let mut goals = vec![Goal { status: Some(GoalStatus::Done), ..goal.clone() }];
    goals[0].history.as_mut().unwrap().push(StatusChange { status: GoalStatus::Done, at: stamp(at("2026-11-05T12:00"), &ZONE), deadline: Some("2026-11-07".into()), note: None });
    assert!(renew(&mut goals, &[weekly()], &ZONE, at("2026-11-07T12:00")).is_empty());
    assert_eq!(renew(&mut goals, &[weekly()], &ZONE, at("2026-11-08T12:00")), vec!["hab1_2026-11-08"]);
}

#[test]
fn after_a_long_gap_the_next_goal_is_the_period_under_ways() {
    let mut goals = vec![closed("hab1_2026-09-06", "2026-09-12", GoalStatus::Obsolete, "2026-09-10T12:00")];
    assert_eq!(renew(&mut goals, &[weekly()], &ZONE, at("2026-10-28T09:30")), vec!["hab1_2026-10-25"]);
    assert_eq!(goals[1].history.as_deref().unwrap()[0].at, stamp(at("2026-10-25T00:00"), &ZONE));
}

#[test]
fn only_a_template_with_a_period_makes_goals() {
    let template = Template { goal: RecurringGoal::default(), ..weekly() };
    let mut goals = Vec::new();
    assert!(renew(&mut goals, &[template], &ZONE, at("2026-10-28T09:30")).is_empty());
    assert_eq!(problems(&RecurringGoal::default()), vec!["needs a period: day, week, month, quarter or year"]);
    assert!(problems(&weekly().goal).is_empty());
    assert_eq!(problems(&RecurringGoal { rate_periods: Some(0), ..weekly().goal }), vec!["rate_periods must be at least 1"]);
}
