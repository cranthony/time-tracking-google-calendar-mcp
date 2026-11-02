//! A habit's recurring goal: one goal for each calendar period (a day, a
//! week from Sunday, a month, a quarter, a year), made from the habit's template by the
//! server as each period begins -- once the goal before it is done, or
//! made obsolete -- and tried again for the next period when it's missed.
//! A habit has at most one open goal at a time.
//!
//! A goal made so is the habit's (its `habit_id`) and has the id
//! "<habit id>_<the date its period starts>": no other id has a "_" (the
//! server's own are letters and digits, and those a client picks may
//! only add "-"), so an id names the habit it's from. The template's
//! sub-goals are made under it, as it's made: each the habit's too, with
//! the goal's id and "_<its key>", and due as far before the goal as the
//! template says, so moving the goal's deadline moves the whole chain.

use crate::contract::{DateTimeText, Schema};
use crate::stores::goals::{deadline_end, shifted, ActivateWhen, Deadline, Goal, GoalStatus, Offset, StatusChange, TextFormat, Trigger};
use crate::stores::groups::set;
use chrono::{Datelike, Duration, NaiveDate, NaiveDateTime};
use chrono_tz::Tz;
use serde::{Deserialize, Serialize};

/// What separates a habit's id from its period's start in the id of a
/// goal made from it.
pub const ID_MARK: char = '_';


/// The longest a recurring goal's title can be.
pub const MAX_TITLE_LENGTH: usize = 200;

/// The longest a sub-goal's key can be.
pub const MAX_KEY_LENGTH: usize = 40;

/// How long each of a recurring goal's periods is, on the calendar: a
/// day, a week (Sunday to Saturday), a month, a quarter (from January,
/// April, July or October) or a year.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize, Schema)]
pub enum Period {
    #[serde(rename = "day")]
    Day,
    #[serde(rename = "week")]
    Week,
    #[serde(rename = "month")]
    Month,
    #[serde(rename = "quarter")]
    Quarter,
    #[serde(rename = "year")]
    Year,
}

impl Period {
    /// How many periods a goal rate counts back unless its habit says:
    /// fewer of the longer ones.
    pub fn default_rate_periods(self) -> i64 {
        match self {
            Period::Day => 7,
            Period::Week => 6,
            Period::Month | Period::Quarter => 4,
            Period::Year => 2,
        }
    }
}

/// A habit's recurring goal: the goal made for each period, in place of
/// an action, a person or a circle. Its fields are the goal's (see
/// get_goals); `title` defaults to the habit's name. `rate_periods` is
/// how many of the periods before the one under way its goal rate counts
/// (by default 7 days, 6 weeks, 4 months, 4 quarters or 2 years): the
/// share of them with a goal done in them.
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
    pub description_format: Option<TextFormat>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub definition_of_done: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub definition_of_done_format: Option<TextFormat>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attribution_hints: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub with_ids: Option<Vec<crate::attendees::Named>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub for_ids: Option<Vec<crate::attendees::Named>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub rate_periods: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub sub_goals: Option<Vec<SubGoal>>,
}

/// One of a recurring goal's sub-goals, made under each of its goals as
/// that's made. Its fields are a goal's (see get_goals), and: key, which
/// names it among them (letters, digits and "-"; its goal's id is the
/// goal's and "_<key>"); deadline, how far before the goal's deadline
/// it's due (days or minutes, as an Offset; none, the goal's own), kept
/// so while it's open; and activate_when, what it waits for, as a goal's
/// but with goal_closed naming another sub-goal's key, and with no `at`
/// (a time before the goal's deadline is before_parent). With
/// activate_when, it's made inactive, to wait; without, active.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema)]
pub struct SubGoal {
    pub key: Option<String>,
    pub title: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub required: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub priority: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub expected_minutes: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub description_format: Option<TextFormat>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub definition_of_done: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub definition_of_done_format: Option<TextFormat>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub attribution_hints: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub with_ids: Option<Vec<crate::attendees::Named>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub for_ids: Option<Vec<crate::attendees::Named>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub deadline: Option<Offset>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub activate_when: Option<ActivateWhen>,
}

/// What's wrong with `key` as a sub-goal's: it's letters, digits and "-".
fn key_problem(key: &str) -> Option<String> {
    if key.is_empty() {
        Some("is empty".into())
    } else if key.chars().count() > MAX_KEY_LENGTH {
        Some(format!("is longer than {MAX_KEY_LENGTH} characters"))
    } else if !key.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') {
        Some(format!("{} has more than letters, digits and \"-\"", crate::py::repr(key)))
    } else {
        None
    }
}

/// What's wrong with a habit's recurring goal, as phrases following "its
/// recurring_goal".
pub fn problems(goal: &RecurringGoal) -> Vec<String> {
    let mut problems = Vec::new();
    if goal.period.is_none() {
        problems.push("needs a period: day, week, month, quarter or year".to_string());
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
    problems.extend(sub_goal_problems(goal));
    problems
}

/// What's wrong with a recurring goal's sub_goals: their keys and what
/// they wait for, and the rest as the goals they'd make -- under one made
/// for a sample period -- would be refused for.
fn sub_goal_problems(goal: &RecurringGoal) -> Vec<String> {
    let subs = goal.sub_goals.as_deref().unwrap_or_default();
    let mut problems = Vec::new();
    let mut keys: Vec<&str> = Vec::new();
    for (n, sub) in subs.iter().enumerate() {
        match sub.key.as_deref() {
            None => problems.push(format!("sub_goals[{n}] needs a key")),
            Some(key) => match key_problem(key) {
                Some(p) => problems.push(format!("sub_goals[{n}]'s key {p}")),
                None if keys.contains(&key) => problems.push(format!("sub_goals[{n}]'s key {} is used more than once", crate::py::repr(key))),
                None => keys.push(key),
            },
        }
    }
    for (n, sub) in subs.iter().enumerate() {
        for trigger in sub.activate_when.as_ref().and_then(|w| w.after.as_deref()).unwrap_or_default() {
            if set(trigger.at.as_deref()).is_some() {
                problems.push(format!("sub_goals[{n}]'s activate_when waits for a time given: a sub-goal waits for one before its goal's deadline (before_parent)"));
            }
            if let Some(key) = set(trigger.goal_closed.as_deref()) {
                if !keys.contains(&key) {
                    problems.push(format!("sub_goals[{n}]'s activate_when waits for {}, which isn't another sub-goal's key", crate::py::repr(key)));
                }
            }
        }
    }
    let Some(period) = goal.period else { return problems };
    if !problems.is_empty() || subs.is_empty() {
        return problems;
    }
    // Would the goals made for a period be refused?
    let template = Template { habit_id: "habit".into(), name: "goal".into(), goal: goal.clone() };
    let start = NaiveDate::from_ymd_opt(2026, 1, 1).unwrap_or_default();
    let (_, end) = period_of(start, period);
    let zone = chrono_tz::UTC;
    let goals = made(&template, start, end, start.and_hms_opt(0, 0, 0).unwrap_or_default(), &zone);
    let parent = goal_id("habit", start);
    let sub_prefix = format!("{parent}{ID_MARK}");
    for (_, problem) in crate::stores::goals::goal_problems(&goals, &zone) {
        // Named by their keys, not the sample goals' ids.
        problems.push(format!("sub_goals: {}", problem.replace(&sub_prefix, "").replace(&parent, "the goal")));
    }
    problems
}

/// The habit a goal's `id` says it's from, if it's shaped as one made
/// from a habit, or one of its sub-goals.
pub fn habit_of_id(id: &str) -> Option<&str> {
    let (habit, rest) = id.split_once(ID_MARK)?;
    let (start, key) = rest.split_once(ID_MARK).map_or((rest, None), |(start, key)| (start, Some(key)));
    let keyed = key.is_none_or(|k| key_problem(k).is_none());
    (!habit.is_empty() && keyed && NaiveDate::parse_from_str(start, "%Y-%m-%d").is_ok()).then_some(habit)
}

/// The id of the sub-goal `key` of the goal `parent_id`.
pub fn sub_goal_id(parent_id: &str, key: &str) -> String {
    format!("{parent_id}{ID_MARK}{key}")
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
        Period::Month => months(day, 1),
        Period::Quarter => months(day, 3),
        Period::Year => months(day, 12),
    }
}

/// The first and last day of the run of `length` months, counted from
/// January, that `day` falls in.
fn months(day: NaiveDate, length: u32) -> (NaiveDate, NaiveDate) {
    let first = (day.month0() / length) * length;
    let start = NaiveDate::from_ymd_opt(day.year(), first + 1, 1).unwrap_or(day);
    let after = first + length;
    let next = NaiveDate::from_ymd_opt(day.year() + i32::try_from(after / 12).unwrap_or(0), after % 12 + 1, 1);
    (start, next.map_or(start, |n| n - Duration::days(1)))
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
        // Its goals, not their sub-goals.
        let theirs: Vec<usize> = (0..goals.len())
            .filter(|i| set(goals[*i].habit_id.as_deref()) == Some(template.habit_id.as_str()) && set(goals[*i].parent_id.as_deref()).is_none())
            .collect();
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
                goal.history.get_or_insert_with(Vec::new).push(StatusChange { status: prior, at: at.clone(), deadline: Some(deadline.clone()), note: None });
                goal.status = Some(prior);
                goal.deadline = Some(deadline);
                changed.extend(goal.id.clone());
                let parent = goal.clone();
                changed.extend(retried_sub_goals(goals, &parent, template, &at, zone));
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
        let made = made(template, start, end, from, zone);
        changed.extend(made.iter().filter_map(|g| g.id.clone()));
        goals.extend(made);
    }
    changed
}

/// The goals `template` makes for the period from `start` to `end`: its
/// goal, active from `from`, then its sub-goals under it.
fn made(template: &Template, start: NaiveDate, end: NaiveDate, from: NaiveDateTime, zone: &Tz) -> Vec<Goal> {
    let deadline = end.format("%Y-%m-%d").to_string();
    let t = &template.goal;
    let goal = Goal {
        id: Some(goal_id(&template.habit_id, start)),
        title: Some(set(t.title.as_deref()).unwrap_or(&template.name).to_string()),
        priority: t.priority,
        expected_minutes: t.expected_minutes,
        deadline: Some(deadline.clone()),
        status: Some(GoalStatus::Active),
        history: Some(vec![StatusChange { status: GoalStatus::Active, at: stamp(from, zone), deadline: Some(deadline), note: None }]),
        description: t.description.clone(),
        description_format: t.description_format,
        definition_of_done: t.definition_of_done.clone(),
        definition_of_done_format: t.definition_of_done_format,
        attribution_hints: t.attribution_hints.clone(),
        with_ids: t.with_ids.clone(),
        for_ids: t.for_ids.clone(),
        habit_id: Some(template.habit_id.clone()),
        ..Goal::default()
    };
    let subs: Vec<Goal> = t.sub_goals.as_deref().unwrap_or_default().iter().filter_map(|sub| sub_goal(&goal, sub, from, zone)).collect();
    std::iter::once(goal).chain(subs).collect()
}

/// `sub` made under `parent` at `at` (local): due as far before the
/// parent as it says, kept so, and waiting, if it waits, for its sibling
/// goals by their ids -- inactive, as it is too when it would start past
/// its deadline.
fn sub_goal(parent: &Goal, sub: &SubGoal, at: NaiveDateTime, zone: &Tz) -> Option<Goal> {
    let parent_id = parent.id.as_deref()?;
    let key = set(sub.key.as_deref())?;
    let offset = sub.deadline.clone().unwrap_or(Offset { days: None, minutes: Some(0) });
    let deadline = parent.deadline.as_deref().and_then(|d| shifted(d, offset.days, offset.minutes, -1));
    let activate_when = sub.activate_when.clone().map(|mut when| {
        for trigger in when.after.iter_mut().flatten() {
            if let Some(key) = set(trigger.goal_closed.as_deref()) {
                *trigger = Trigger { goal_closed: Some(sub_goal_id(parent_id, key)), ..trigger.clone() };
            }
        }
        when
    });
    let late = deadline.as_deref().and_then(deadline_end).is_some_and(|due| due <= at);
    let status = if activate_when.is_some() || late { GoalStatus::Inactive } else { GoalStatus::Active };
    Some(Goal {
        id: Some(sub_goal_id(parent_id, key)),
        title: sub.title.clone(),
        parent_id: Some(parent_id.to_string()),
        required: sub.required,
        priority: sub.priority,
        expected_minutes: sub.expected_minutes,
        deadline: deadline.clone(),
        deadline_rule: Some(Deadline { before_parent: Some(offset), ..Deadline::default() }),
        activate_when,
        status: Some(status),
        history: Some(vec![StatusChange { status, at: stamp(at, zone), deadline, note: None }]),
        description: sub.description.clone(),
        description_format: sub.description_format,
        definition_of_done: sub.definition_of_done.clone(),
        definition_of_done_format: sub.definition_of_done_format,
        attribution_hints: sub.attribution_hints.clone(),
        with_ids: sub.with_ids.clone(),
        for_ids: sub.for_ids.clone(),
        habit_id: parent.habit_id.clone(),
        ..Goal::default()
    })
}

/// `parent`'s missed sub-goals tried again with it, at `at`: each
/// reopened as `template` makes it -- waiting again for what it waits
/// for, due as far before the parent's new deadline -- its history
/// keeping the deadline it missed. Those done, obsolete or gone are left
/// so. Their ids.
fn retried_sub_goals(goals: &mut [Goal], parent: &Goal, template: &Template, at: &DateTimeText, zone: &Tz) -> Vec<String> {
    let Ok(moment) = crate::event::parse_iso(&at.0, None) else { return Vec::new() };
    let local = moment.with_timezone(zone).naive_local();
    let mut changed = Vec::new();
    for sub in template.goal.sub_goals.as_deref().unwrap_or_default() {
        let Some(fresh) = sub_goal(parent, sub, local, zone) else { continue };
        let Some(goal) = goals.iter_mut().find(|g| g.id == fresh.id && g.status == Some(GoalStatus::Missed)) else { continue };
        let status = fresh.status.unwrap_or(GoalStatus::Active);
        goal.history.get_or_insert_with(Vec::new).push(StatusChange { status, at: at.clone(), deadline: fresh.deadline.clone(), note: None });
        goal.status = Some(status);
        goal.deadline = fresh.deadline;
        goal.deadline_rule = fresh.deadline_rule;
        goal.activate_when = fresh.activate_when;
        changed.extend(goal.id.clone());
    }
    changed
}

#[cfg(test)]
#[path = "habit_goals_tests.rs"]
mod tests;
