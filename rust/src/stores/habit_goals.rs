//! A habit's recurring goal: one goal for each calendar period (a day, a
//! week from Sunday, a month), made from the habit's template by the
//! server as each period begins -- once the goal before it is done, or
//! made obsolete -- and tried again for the next period when it's missed.
//! A habit has at most one open goal at a time.
//!
//! A goal made so is the habit's (its `habit_id`) and has the id
//! "<habit id>_<the date its period starts>": no other id has a "_" (the
//! server's own are letters and digits, and those a client picks may
//! only add "-"), so an id names the habit it's from.

use crate::contract::{DateTimeText, Schema};
use crate::stores::goals::{Goal, GoalStatus, StatusChange};
use crate::stores::groups::set;
use chrono::{Datelike, Duration, NaiveDate, NaiveDateTime};
use chrono_tz::Tz;
use serde::{Deserialize, Serialize};

/// What separates a habit's id from its period's start in the id of a
/// goal made from it.
pub const ID_MARK: char = '_';

/// How many periods a habit's goal rate counts back unless it says.
pub const DEFAULT_RATE_PERIODS: i64 = 6;

/// The longest a recurring goal's title can be.
pub const MAX_TITLE_LENGTH: usize = 200;

/// How long each of a recurring goal's periods is, on the calendar: a
/// day, a week (Sunday to Saturday) or a month.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize, Schema)]
pub enum Period {
    #[serde(rename = "day")]
    Day,
    #[serde(rename = "week")]
    Week,
    #[serde(rename = "month")]
    Month,
}

/// A habit's recurring goal: the goal made for each period, in place of
/// an action, a person or a circle. Its fields are the goal's (see
/// get_goals); `title` defaults to the habit's name. `rate_periods` is
/// how many of the periods before the one under way its goal rate counts
/// (6 by default): the share of them with a goal done in them.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema)]
pub struct RecurringGoal {
    pub period: Option<Period>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub priority: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expected_minutes: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub definition_of_done: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attribution_hints: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub with_ids: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub for_ids: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rate_periods: Option<i64>,
}

/// What's wrong with a habit's recurring goal, as phrases following "its
/// recurring_goal".
pub fn problems(goal: &RecurringGoal) -> Vec<String> {
    let mut problems = Vec::new();
    if goal.period.is_none() {
        problems.push("needs a period: day, week or month".to_string());
    }
    if goal.title.as_deref().is_some_and(|t| t.chars().count() > MAX_TITLE_LENGTH) {
        problems.push(format!("title is longer than {MAX_TITLE_LENGTH} characters"));
    }
    if goal.priority.is_some_and(|p| p < 0) {
        problems.push("priority can't be below 0".to_string());
    }
    if goal.expected_minutes.is_some_and(|m| m <= 0) {
        problems.push("expected_minutes must be more than 0".to_string());
    }
    if goal.rate_periods.is_some_and(|n| n < 1) {
        problems.push("rate_periods must be at least 1".to_string());
    }
    problems
}

/// The habit a goal's `id` says it's from, if it's shaped as one made
/// from a habit.
pub fn habit_of_id(id: &str) -> Option<&str> {
    let (habit, start) = id.split_once(ID_MARK)?;
    (!habit.is_empty() && NaiveDate::parse_from_str(start, "%Y-%m-%d").is_ok()).then_some(habit)
}

/// The id of `habit_id`'s goal for the period starting `start`.
pub fn goal_id(habit_id: &str, start: NaiveDate) -> String {
    format!("{habit_id}{ID_MARK}{}", start.format("%Y-%m-%d"))
}

/// The first and last day of the `period` `day` falls in.
pub fn period_of(day: NaiveDate, period: Period) -> (NaiveDate, NaiveDate) {
    match period {
        Period::Day => (day, day),
        Period::Week => {
            let start = day - Duration::days(i64::from(day.weekday().num_days_from_sunday()));
            (start, start + Duration::days(6))
        }
        Period::Month => {
            let start = day.with_day(1).unwrap_or(day);
            let next = if start.month() == 12 {
                NaiveDate::from_ymd_opt(start.year() + 1, 1, 1)
            } else {
                NaiveDate::from_ymd_opt(start.year(), start.month() + 1, 1)
            };
            (start, next.map_or(start, |n| n - Duration::days(1)))
        }
    }
}

/// The first and last day of the `period` after the one `day` falls in.
pub fn next_period(day: NaiveDate, period: Period) -> (NaiveDate, NaiveDate) {
    let (_, end) = period_of(day, period);
    period_of(end + Duration::days(1), period)
}

/// A habit with a recurring goal, as goals are made from it.
#[derive(Clone, Debug, PartialEq)]
pub struct Template {
    pub habit_id: String,
    /// The habit's name: its goals' title, unless the template gives one.
    pub name: String,
    pub goal: RecurringGoal,
}

/// The last day of a deadline ("2026-11-07" or "2026-11-07T17:00").
fn deadline_day(deadline: &str) -> Option<NaiveDate> {
    let text = deadline.trim();
    NaiveDate::parse_from_str(text.get(..10)?, "%Y-%m-%d").ok()
}

/// `goal`'s last status change, local.
fn last_change(goal: &Goal, zone: &Tz) -> Option<NaiveDateTime> {
    let at = &goal.history.as_deref()?.last()?.at;
    Some(crate::event::parse_iso(&at.0, None).ok()?.with_timezone(zone).naive_local())
}

/// `at` (local) as a history entry's time.
fn stamp(at: NaiveDateTime, zone: &Tz) -> DateTimeText {
    use chrono::TimeZone;
    let moment = zone.from_local_datetime(&at).earliest().map(|m| m.fixed_offset()).unwrap_or_else(crate::determinism::now);
    DateTimeText(crate::py::pydantic_datetime(&moment))
}

/// `templates`' goals as they are at `now` (local): for each habit with
/// no open goal, the one before missed is tried again -- reopened, due at
/// the end of the period after the one it missed, at the moment it was
/// missed -- and otherwise, once the period under way has begun after
/// the one its last goal covered (or it's had none), that period's goal
/// is made, active from its start (or, if the goal before closed later,
/// from then). The ids of those changed or made.
pub fn renew(goals: &mut Vec<Goal>, templates: &[Template], zone: &Tz, now: NaiveDateTime) -> Vec<String> {
    let mut changed = Vec::new();
    for template in templates {
        let Some(period) = template.goal.period else { continue };
        let theirs: Vec<usize> = (0..goals.len()).filter(|i| set(goals[*i].habit_id.as_deref()) == Some(template.habit_id.as_str())).collect();
        if theirs.iter().any(|i| goals[*i].status.is_some_and(|s| !s.is_closed())) {
            continue;
        }
        // The goal that covered the latest period: its deadline's.
        let last = theirs.iter().copied().max_by_key(|i| goals[*i].deadline.as_deref().and_then(deadline_day));
        let today = now.date();
        if let Some(i) = last {
            let goal = &goals[i];
            let Some(covered) = goal.deadline.as_deref().and_then(deadline_day) else { continue };
            if goal.status == Some(GoalStatus::Missed) {
                let Some(at) = goal.history.as_deref().and_then(|h| h.last()).map(|c| c.at.clone()) else { continue };
                let prior = goal
                    .history
                    .as_deref()
                    .unwrap_or_default()
                    .iter()
                    .rev()
                    .map(|c| c.status)
                    .find(|s| !s.is_closed())
                    .unwrap_or(GoalStatus::Active);
                let deadline = next_period(covered, period).1.format("%Y-%m-%d").to_string();
                let goal = &mut goals[i];
                goal.history.get_or_insert_with(Vec::new).push(StatusChange { status: prior, at, deadline: Some(deadline.clone()) });
                goal.status = Some(prior);
                goal.deadline = Some(deadline);
                changed.extend(goal.id.clone());
                continue;
            }
            let (start, _) = next_period(covered, period);
            if start > today {
                continue;
            }
        }
        // The period under way, whenever the last one was.
        let (start, end) = period_of(today, period);
        let id = goal_id(&template.habit_id, start);
        if goals.iter().any(|g| g.id.as_deref() == Some(id.as_str())) {
            continue;
        }
        let begun = start.and_hms_opt(0, 0, 0).unwrap_or(now);
        let from = last.and_then(|i| last_change(&goals[i], zone)).map_or(begun, |closed| closed.max(begun)).min(now);
        let deadline = end.format("%Y-%m-%d").to_string();
        let t = &template.goal;
        goals.push(Goal {
            id: Some(id.clone()),
            title: Some(set(t.title.as_deref()).unwrap_or(&template.name).to_string()),
            priority: t.priority,
            expected_minutes: t.expected_minutes,
            deadline: Some(deadline.clone()),
            status: Some(GoalStatus::Active),
            history: Some(vec![StatusChange { status: GoalStatus::Active, at: stamp(from, zone), deadline: Some(deadline) }]),
            description: t.description.clone(),
            definition_of_done: t.definition_of_done.clone(),
            attribution_hints: t.attribution_hints.clone(),
            with_ids: t.with_ids.clone(),
            for_ids: t.for_ids.clone(),
            habit_id: Some(template.habit_id.clone()),
            ..Goal::default()
        });
        changed.push(id);
    }
    changed
}

#[cfg(test)]
#[path = "habit_goals_tests.rs"]
mod tests;
