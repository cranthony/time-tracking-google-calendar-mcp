//! Goals: outcomes the user means to reach and can mark done, in the
//! Outcome Goals tab. A goal can sit under another, to any depth -- a
//! sub-goal is a piece of reaching its parent -- and events are tagged
//! with goals (an event's goal shares, kept on the event; see
//! `GoalShare`), apart from their actions. Goals aren't tied to actions,
//! people or places; a habit's recurring goal makes one goal for each of
//! its periods (see habit_goals), the habit's.
//!
//! The old goals (replaced by actions) left a Goals tab and events'
//! goal_ids behind; these are kept apart from them: their own tab, and
//! events' goal_shares.

use crate::attendees::{named_entries, Named};
use crate::contract::{DateTimeText, RowItem, Schema};
use crate::event::{parse_iso, Moment};
use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::sheet::rows::{self, RowSheet, RowType};
use crate::sheet::GOALS;
use crate::stores::groups::{set, str_or_none};
use crate::stores::habit_goals::{self, Template};
use crate::stores::locations::dedup;
use crate::stores::people::SELF_ID;
use anyhow::bail;
use chrono::{Duration, NaiveDate, NaiveDateTime};
use chrono_tz::Tz;
use serde::{Deserialize, Serialize};
use std::sync::Arc;

pub const GOAL_STATUSES: [&str; 5] = ["inactive", "active", "done", "missed", "obsolete"];
pub const DEFAULT_STATUSES: [&str; 2] = ["inactive", "active"];
pub const MAX_TITLE_LENGTH: usize = 200;
pub const CLEARABLE_FIELDS: [&str; 13] = [
    "parent_id",
    "required",
    "priority",
    "sort_order",
    "expected_minutes",
    "deadline",
    "deadline_rule",
    "activate_when",
    "description",
    "definition_of_done",
    "attribution_hints",
    "with_ids",
    "for_ids",
];

// -- the types -------------------------------------------------------------------

/// Where a goal stands: inactive (not started, or set aside), active
/// (being worked on), done (reached), missed (its deadline passed without
/// it) or obsolete (no longer wanted). A missed goal can be reopened with
/// a new deadline: its history keeps each deadline it missed, so a goal
/// can be missed and retried any number of times.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize, Schema)]
pub enum GoalStatus {
    #[serde(rename = "inactive")]
    Inactive,
    #[serde(rename = "active")]
    Active,
    #[serde(rename = "done")]
    Done,
    #[serde(rename = "missed")]
    Missed,
    #[serde(rename = "obsolete")]
    Obsolete,
}

impl GoalStatus {
    /// Done, missed or obsolete: nothing more to do for it.
    pub fn is_closed(self) -> bool {
        matches!(self, GoalStatus::Done | GoalStatus::Missed | GoalStatus::Obsolete)
    }

    pub fn text(self) -> &'static str {
        match self {
            GoalStatus::Inactive => "inactive",
            GoalStatus::Active => "active",
            GoalStatus::Done => "done",
            GoalStatus::Missed => "missed",
            GoalStatus::Obsolete => "obsolete",
        }
    }
}

/// One change of a goal's status: what it became, when, and the deadline
/// it had then.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct StatusChange {
    pub status: GoalStatus,
    pub at: DateTimeText,
    /// The goal's deadline in force at this change, if it had one: the
    /// one a missed entry missed, the new one a reopening entry starts
    /// toward. The server fills it in for a change it records itself
    /// (a status set without a history); a history given whole keeps
    /// its entries' as given.
    pub deadline: Option<String>,
}

/// How far a deadline is from what it's measured from: whole days (a
/// date stays a date; from a start, the end of that many days after the
/// day it started) or minutes (giving a date and time). One of them, 0 or
/// more.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema)]
pub struct Offset {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub days: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub minutes: Option<i64>,
}

/// How far after another goal's deadline: that goal's id, and days or
/// minutes (one of them, 0 or more), as an Offset.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema)]
pub struct GoalOffset {
    pub id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub days: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub minutes: Option<i64>,
}

/// A goal's deadline as set: exactly one of at (a date, "2026-11-01", due
/// at the end of that day, or a date and time, "2026-11-01T17:00", in the
/// calendar's time zone), before_parent (that far before its parent's
/// deadline), after_start (that far after it becomes active) or
/// after_goal (that far after another goal's deadline). One measured from
/// something is kept so while the goal is open: its due follows what it's
/// measured from. One measured from its start has no due until the goal
/// starts, and keeps the one it gets then.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema)]
pub struct Deadline {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub at: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub before_parent: Option<Offset>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub after_start: Option<Offset>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub after_goal: Option<GoalOffset>,
}

impl Deadline {
    /// How many of its kinds it gives.
    fn kinds(&self) -> usize {
        [self.at.is_some(), self.before_parent.is_some(), self.after_start.is_some(), self.after_goal.is_some()].iter().filter(|k| **k).count()
    }
}

/// One thing a goal waits for before it's activated: another goal closed
/// (goal_closed, its id: done, missed or obsolete), or a time come (at: a
/// date, from the start of that day, or a date and time). One of them.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema)]
pub struct Trigger {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub goal_closed: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub at: Option<String>,
}

/// A wait of this many minutes.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct DelayMinutes {
    pub minutes: i64,
}

/// The literal "next_day".
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub enum NextDay {
    #[serde(rename = "next_day")]
    NextDay,
}

/// How long after the last of a goal's triggers it's activated:
/// "next_day" (at the start of the day after) or {"minutes": n}.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum Delay {
    NextDay(NextDay),
    Minutes(DelayMinutes),
}

impl Schema for Delay {
    fn schema(defs: &mut crate::contract::Defs) -> serde_json::Value {
        serde_json::json!({"anyOf": [crate::contract::literal(&["next_day"]), DelayMinutes::schema(defs)]})
    }
}

/// The literal "own".
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize)]
pub enum Own {
    #[serde(rename = "own")]
    Own,
}

/// A priority (0 the most), or "own": the goal's own, as it is when it
/// would be activated.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum PriorityRef {
    Level(i64),
    Own(Own),
}

impl Schema for PriorityRef {
    fn schema(_: &mut crate::contract::Defs) -> serde_json::Value {
        serde_json::json!({"anyOf": [{"type": "integer"}, crate::contract::literal(&["own"])]})
    }
}

/// Which goals a hold counts, by the priority that applies to each (its
/// own or its nearest parent's, else 3): at_or_above a priority (that one
/// or more important) or exactly one. One of them.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema)]
pub struct PriorityFilter {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub at_or_above: Option<PriorityRef>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub exactly: Option<PriorityRef>,
}

/// A goal is held back -- not activated, though its triggers have come
/// -- while at least active_at_least other goals are active: those of the
/// priority given, or any. Its own parents and sub-goals aren't counted.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct Hold {
    pub active_at_least: i64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub priority: Option<PriorityFilter>,
}

/// What an inactive goal waits for before the server activates it: every
/// trigger in after (none, it's ready now), then the delay, if any; and
/// while any of hold_while holds, it waits on. Activated, by the server or
/// by hand (which ignores it), it's cleared.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema)]
pub struct ActivateWhen {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub after: Option<Vec<Trigger>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub delay: Option<Delay>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub hold_while: Option<Vec<Hold>>,
}

/// One goal: an outcome the user means to reach (see get_goals), as the
/// tab keeps it.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema, RowItem)]
pub struct Goal {
    pub id: Option<String>,
    pub title: Option<String>,
    /// The goal it's a piece of; none for a top-level goal.
    pub parent_id: Option<String>,
    /// Whether its parent needs it done to be done.
    pub required: Option<bool>,
    /// How much it matters, 0 the most, as actions' priorities; none, it
    /// takes its nearest parent's that has one, and with none up its tree,
    /// it's treated as 3.
    pub priority: Option<i64>,
    /// Where it's listed among goals that otherwise list alike (the same
    /// priority, due the same day): the lowest first, those without one
    /// after them. The user's own order, set by dragging goals in the app.
    pub sort_order: Option<i64>,
    /// How long it's expected to take, in minutes of the user's time: a
    /// guide to compare the time spent with, never enforced.
    pub expected_minutes: Option<i64>,
    /// When it's due: a date ("2026-11-01", due at the end of that day)
    /// or a date and time ("2026-11-01T17:00"), in the calendar's time
    /// zone -- given, or worked out from deadline_rule. Needed while it's
    /// active (see goal_problems for an inactive one).
    pub deadline: Option<String>,
    /// From before deadline_rule: a deadline this many minutes before its
    /// parent's. Read as deadline_rule's before_parent, and cleared.
    pub deadline_offset_minutes: Option<i64>,
    #[row(text)]
    pub status: Option<GoalStatus>,
    /// Every change of its status, oldest first, the last its status now,
    /// each with the deadline in force at that change. A missed goal
    /// retried has a new deadline and a history ending missed (with the
    /// deadline it missed) then active or inactive (with the new one),
    /// both at the moment of the retry.
    pub history: Option<Vec<StatusChange>>,
    /// What it is, as HTML.
    pub description: Option<String>,
    /// What being done looks like, as HTML.
    pub definition_of_done: Option<String>,
    /// A guide for telling which events are for it.
    pub attribution_hints: Option<String>,
    /// The people it's done with: who the user works on it alongside
    /// (person ids; never "self", and never also in for_ids). A circle's
    /// id names a plain circle's members, or a community itself;
    /// {circle_id, except} a plain circle's but those in except.
    pub with_ids: Option<Vec<crate::attendees::Named>>,
    /// The people it's done for, who aren't part of doing it: a promise
    /// to them, a gift, a favor (person ids; never "self", and never also
    /// in with_ids, a plain circle's members included). Circles as in
    /// with_ids.
    pub for_ids: Option<Vec<crate::attendees::Named>>,
    /// What its deadline is measured from, when it isn't a date given (see
    /// Deadline; never `at`): kept so while it's open, deadline following
    /// it. A deadline given without it unties it; so does moving a goal
    /// measured from its parent's to the top level, keeping the deadline
    /// it had.
    pub deadline_rule: Option<Deadline>,
    /// What it waits for before it's activated, while it's inactive (see
    /// ActivateWhen).
    pub activate_when: Option<ActivateWhen>,
    /// The habit it was made from, for one of its periods (see
    /// habit_goals): set by the server, never given.
    pub habit_id: Option<String>,
    /// Goes up by one each time it changes. Give it to an update (and to a delete, as if_version): it's the version you last read, required -- one saved before versions is version 0 -- changed since, the change is refused; on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK.
    pub version: Option<i64>,
}

/// A goal as the goal tools return it: its deadline as set and when that
/// falls due (due: a date or a date and time; none, if it can't be worked
/// out yet -- one measured from a start that hasn't come), what it waits
/// for (activate_when) and, waiting, when it's expected to be activated
/// (expected_start: a date, from its start, or a date and time; none
/// while it's held, or what it waits for has no deadline yet); plus its
/// parents' titles (path, outermost first), when it first became active
/// (started_at) and, done, missed or obsolete, when it became so
/// (closed_at).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct ListedGoal {
    pub id: Option<String>,
    pub title: Option<String>,
    pub parent_id: Option<String>,
    pub required: Option<bool>,
    pub priority: Option<i64>,
    pub sort_order: Option<i64>,
    pub expected_minutes: Option<i64>,
    pub deadline: Option<Deadline>,
    pub due: Option<String>,
    pub activate_when: Option<ActivateWhen>,
    pub expected_start: Option<String>,
    pub status: Option<GoalStatus>,
    pub history: Option<Vec<StatusChange>>,
    pub description: Option<String>,
    pub definition_of_done: Option<String>,
    pub attribution_hints: Option<String>,
    pub with_ids: Option<Vec<crate::attendees::Named>>,
    pub for_ids: Option<Vec<crate::attendees::Named>>,
    pub path: Vec<String>,
    /// The habit whose recurring goal it is, for one of its periods (its
    /// id is the habit's and the period's start: "<habit id>_2026-11-01").
    pub habit_id: Option<String>,
    pub started_at: Option<DateTimeText>,
    pub closed_at: Option<DateTimeText>,
    pub version: Option<i64>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct CreatedGoal {
    pub goal: ListedGoal,
    pub created_id: String,
}

/// What a change of goals came to: each goal it made or changed, as it is
/// now, and the ids of those it deleted.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct ChangedGoals {
    pub goals: Vec<ListedGoal>,
    pub deleted_ids: Vec<String>,
}

pub const GOAL_ROW: RowType = <Goal as rows::RowItem>::ROW_TYPE;

/// A goal update: its id, the fields to set, and those to clear.
pub struct GoalEdit {
    pub goal: Goal,
    pub clear_fields: Vec<String>,
}

/// A goal to delete, and the version last seen of it.
pub struct GoalRemoval {
    pub goal_id: String,
    pub if_version: Option<i64>,
}

// -- deadlines -------------------------------------------------------------------

/// When `deadline` falls due, as a local time: a date's is the midnight
/// after it.
pub fn deadline_end(deadline: &str) -> Option<NaiveDateTime> {
    let text = deadline.trim();
    if let Ok(date) = NaiveDate::parse_from_str(text, "%Y-%m-%d") {
        return Some(date.and_hms_opt(0, 0, 0)? + Duration::days(1));
    }
    NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M").or_else(|_| NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S")).ok()
}

const DATE: &str = "%Y-%m-%d";
const DATE_TIME: &str = "%Y-%m-%dT%H:%M";

/// The deadline `days` or `minutes` after `base` (a deadline), or before
/// it if `sign` is -1: days keep a date a date, and minutes give a date
/// and time (0 minutes, `base` itself).
pub fn shifted(base: &str, days: Option<i64>, minutes: Option<i64>, sign: i64) -> Option<String> {
    let text = base.trim();
    if let Some(days) = days {
        if let Ok(date) = NaiveDate::parse_from_str(text, DATE) {
            return Some((date + Duration::days(sign * days)).format(DATE).to_string());
        }
        return Some((deadline_end(text)? + Duration::days(sign * days)).format(DATE_TIME).to_string());
    }
    match minutes? {
        0 => Some(text.to_string()),
        minutes => Some((deadline_end(text)? + Duration::minutes(sign * minutes)).format(DATE_TIME).to_string()),
    }
}

/// The deadline `offset` after a start at `start`: days, the end of that
/// many days after its day; minutes, that many minutes after it.
fn after_start(start: NaiveDateTime, offset: &Offset) -> Option<String> {
    if let Some(days) = offset.days {
        return Some((start.date() + Duration::days(days)).format(DATE).to_string());
    }
    Some((start + Duration::minutes(offset.minutes?)).format(DATE_TIME).to_string())
}

/// When `goal` last became active, local; none if it never has.
fn last_started(goal: &Goal, zone: &Tz) -> Option<NaiveDateTime> {
    let change = goal.history.as_deref().unwrap_or_default().iter().rev().find(|c| c.status == GoalStatus::Active)?;
    Some(local(&moment(&change.at)?, zone))
}

/// The goal `id` names, if there is one.
fn by_id<'a>(goals: &'a [Goal], id: &str) -> Option<&'a Goal> {
    goals.iter().find(|g| g.id.as_deref() == Some(id))
}

/// Each open goal whose deadline is measured from something (its
/// deadline_rule) given the deadline that follows from it, those it's
/// measured from first -- one measured from its start, once it's active,
/// and none until then -- and one measured from its parent's at the top
/// level untied. The ids of those changed.
fn resolve_deadlines(goals: &mut [Goal], zone: &Tz) -> Vec<String> {
    let mut changed: Vec<String> = Vec::new();
    // A pass for each goal, at most; a cycle is refused anyway.
    for _ in 0..goals.len() {
        let mut again = false;
        for i in 0..goals.len() {
            let goal = &goals[i];
            let Some(rule) = &goal.deadline_rule else { continue };
            if goal.status.is_some_and(GoalStatus::is_closed) {
                continue;
            }
            // None: untie it; Some(due): its deadline.
            let next: Option<Option<String>> = if let Some(offset) = &rule.before_parent {
                if set(goal.parent_id.as_deref()).is_none() {
                    None
                } else {
                    let Some(parent) = parent_of(goals, goal) else { continue };
                    Some(set(parent.deadline.as_deref()).and_then(|d| shifted(d, offset.days, offset.minutes, -1)))
                }
            } else if let Some(offset) = &rule.after_goal {
                let Some(other) = by_id(goals, &offset.id) else { continue };
                Some(set(other.deadline.as_deref()).and_then(|d| shifted(d, offset.days, offset.minutes, 1)))
            } else if let Some(offset) = &rule.after_start {
                match goal.status {
                    Some(GoalStatus::Active) => Some(last_started(goal, zone).and_then(|s| after_start(s, offset))),
                    _ => Some(None),
                }
            } else {
                continue;
            };
            let goal = &mut goals[i];
            match next {
                None => goal.deadline_rule = None,
                Some(deadline) if goal.deadline != deadline => {
                    goal.deadline = deadline;
                    again = true;
                }
                Some(_) => continue,
            }
            changed.extend(goal.id.clone());
        }
        if !again {
            break;
        }
    }
    changed
}

/// `goals` with any deadline_offset_minutes from before deadline_rule
/// read as a deadline_rule's before_parent: whole days as days.
fn upgraded(mut goals: Vec<Goal>) -> Vec<Goal> {
    for goal in &mut goals {
        let Some(minutes) = goal.deadline_offset_minutes.take() else { continue };
        if goal.deadline_rule.is_none() && minutes >= 0 {
            let offset = if minutes % (24 * 60) == 0 { Offset { days: Some(minutes / (24 * 60)), minutes: None } } else { Offset { days: None, minutes: Some(minutes) } };
            goal.deadline_rule = Some(Deadline { before_parent: Some(offset), ..Deadline::default() });
        }
    }
    goals
}

fn local(moment: &Moment, zone: &Tz) -> NaiveDateTime {
    moment.with_timezone(zone).naive_local()
}

/// When `goal` first became active, if it has.
pub fn started_at(goal: &Goal) -> Option<&DateTimeText> {
    goal.history.as_deref().unwrap_or_default().iter().find(|c| c.status == GoalStatus::Active).map(|c| &c.at)
}

/// When `goal` was done, missed or made obsolete, if it is now.
pub fn closed_at(goal: &Goal) -> Option<&DateTimeText> {
    if !goal.status.is_some_and(GoalStatus::is_closed) {
        return None;
    }
    goal.history.as_deref().unwrap_or_default().last().map(|c| &c.at)
}

/// The moment `goal` was done, missed or made obsolete, if it is now.
pub fn closed_moment(goal: &Goal) -> Option<Moment> {
    closed_at(goal).and_then(moment)
}

fn moment(text: &DateTimeText) -> Option<Moment> {
    parse_iso(&text.0, None).ok()
}

/// `at` (local) as a history entry's time, in `zone`.
fn stamp_of(at: NaiveDateTime, zone: &Tz) -> DateTimeText {
    use chrono::TimeZone;
    let moment = zone.from_local_datetime(&at).earliest().map(|m| m.fixed_offset()).unwrap_or_else(crate::determinism::now);
    DateTimeText(py::pydantic_datetime(&moment))
}

// -- activation ------------------------------------------------------------------

/// When a trigger's time comes, local: a date's at its start.
fn start_of(at: &str) -> Option<NaiveDateTime> {
    let text = at.trim();
    if let Ok(date) = NaiveDate::parse_from_str(text, DATE) {
        return date.and_hms_opt(0, 0, 0);
    }
    deadline_end(text)
}

/// The priority that applies to `goal`: its own, or its nearest parent's
/// that has one, or 3.
pub fn priority_of(goal: &Goal, goals: &[Goal]) -> i64 {
    let mut seen: Vec<&str> = Vec::new();
    let mut at = Some(goal);
    while let Some(g) = at {
        if let Some(p) = g.priority {
            return p;
        }
        match g.id.as_deref() {
            Some(id) if !seen.contains(&id) => seen.push(id),
            _ => break,
        }
        at = parent_of(goals, g);
    }
    3
}

/// The ids of `goal`'s parents, nearest first (stopping at a cycle).
fn ancestor_ids(goal: &Goal, goals: &[Goal]) -> Vec<String> {
    let mut ids: Vec<String> = Vec::new();
    let mut at = parent_of(goals, goal);
    while let Some(parent) = at {
        let Some(id) = parent.id.clone() else { break };
        if ids.contains(&id) || goal.id.as_ref() == Some(&id) {
            break;
        }
        ids.push(id);
        at = parent_of(goals, parent);
    }
    ids
}

/// When the last of `when`'s triggers came, then its delay, local: when
/// it's ready, if every trigger has come; none if one hasn't (or names
/// no goal: deleting one takes it out of what waits for it). With no triggers, from `since`.
fn ready_at(when: &ActivateWhen, goals: &[Goal], zone: &Tz, since: NaiveDateTime) -> Option<NaiveDateTime> {
    let mut last: Option<NaiveDateTime> = None;
    for trigger in when.after.as_deref().unwrap_or_default() {
        let came = if let Some(id) = set(trigger.goal_closed.as_deref()) {
            local(&closed_moment(by_id(goals, id)?)?, zone)
        } else if let Some(at) = set(trigger.at.as_deref()) {
            start_of(at)?
        } else {
            continue;
        };
        last = Some(last.map_or(came, |l| l.max(came)));
    }
    Some(delayed(last.unwrap_or(since), when.delay.as_ref()))
}

fn delayed(at: NaiveDateTime, delay: Option<&Delay>) -> NaiveDateTime {
    match delay {
        None => at,
        Some(Delay::NextDay(_)) => (at.date() + Duration::days(1)).and_hms_opt(0, 0, 0).unwrap_or(at),
        Some(Delay::Minutes(d)) => at + Duration::minutes(d.minutes),
    }
}

/// Whether one of `goal`'s holds (in `when`) holds it back: enough other
/// goals of the priority it names are active, not counting its parents
/// or sub-goals.
fn held(goal: &Goal, when: &ActivateWhen, goals: &[Goal]) -> bool {
    let holds = when.hold_while.as_deref().unwrap_or_default();
    if holds.is_empty() {
        return false;
    }
    let own = priority_of(goal, goals);
    let level = |r: &PriorityRef| match r {
        PriorityRef::Level(p) => *p,
        PriorityRef::Own(_) => own,
    };
    let kin = |g: &Goal| {
        g.id == goal.id || ancestor_ids(goal, goals).contains(g.id.as_ref().unwrap_or(&String::new())) || ancestor_ids(g, goals).iter().any(|a| Some(a) == goal.id.as_ref())
    };
    holds.iter().any(|hold| {
        let fits = |g: &Goal| {
            let p = priority_of(g, goals);
            match &hold.priority {
                None => true,
                Some(f) => match (&f.at_or_above, &f.exactly) {
                    (Some(r), _) => p <= level(r),
                    (None, Some(r)) => p == level(r),
                    (None, None) => true,
                },
            }
        };
        let active = goals.iter().filter(|g| g.status == Some(GoalStatus::Active) && !kin(g) && fits(g)).count();
        active as i64 >= hold.active_at_least
    })
}

/// The last time, by `now`, a goal stopped being active, local: when a
/// hold may have let go.
fn last_freed(goals: &[Goal], zone: &Tz, now: NaiveDateTime) -> Option<NaiveDateTime> {
    goals
        .iter()
        .flat_map(|g| g.history.as_deref().unwrap_or_default().windows(2))
        .filter(|w| w[0].status == GoalStatus::Active && w[1].status != GoalStatus::Active)
        .filter_map(|w| moment(&w[1].at).map(|m| local(&m, zone)))
        .filter(|at| *at <= now)
        .max()
}

/// Whether `goal` and its inactive parents can start at `at`: each with
/// a deadline after it, or one measured from its start or another's --
/// not one it's already past, or none at all.
fn can_start(goal: &Goal, goals: &[Goal], at: NaiveDateTime) -> bool {
    let parents = ancestor_ids(goal, goals);
    std::iter::once(goal).chain(parents.iter().filter_map(|id| by_id(goals, id)).filter(|g| g.status == Some(GoalStatus::Inactive))).all(|g| {
        match (set(g.deadline.as_deref()).and_then(deadline_end), &g.deadline_rule) {
            (Some(due), _) => due > at,
            (None, rule) => rule.is_some(),
        }
    })
}

/// Activates the goal at `index` at `at` -- and its inactive parents --
/// clearing what each waited for. The ids of those activated.
fn activate(goals: &mut [Goal], index: usize, at: &DateTimeText) -> Vec<String> {
    let mut ids: Vec<String> = goals[index].id.clone().into_iter().collect();
    ids.extend(ancestor_ids(&goals[index], goals));
    let mut activated = Vec::new();
    for id in ids {
        let Some(goal) = goals.iter_mut().find(|g| g.id.as_deref() == Some(id.as_str())) else { continue };
        goal.activate_when = None;
        if goal.status != Some(GoalStatus::Inactive) {
            continue;
        }
        goal.status = Some(GoalStatus::Active);
        goal.history.get_or_insert_with(Vec::new).push(StatusChange { status: GoalStatus::Active, at: at.clone(), deadline: None });
        activated.push(id);
    }
    activated
}

/// Activates each inactive goal whose activate_when is ready by `now`
/// and isn't held -- the highest priority first, then the soonest due,
/// counting those activated before it -- each at the moment it became
/// ready (or, held, at the latest a hold may have let go), and its
/// inactive parents with it. The ids of those activated.
fn sweep(goals: &mut [Goal], zone: &Tz, now: &Moment) -> Vec<String> {
    let now_local = local(now, zone);
    let mut activated: Vec<String> = Vec::new();
    // Once for each goal, at most: each round activates one.
    for _ in 0..goals.len() {
        let freed = last_freed(goals, zone, now_local);
        let mut ready: Vec<(usize, NaiveDateTime)> = goals
            .iter()
            .enumerate()
            .filter(|(_, g)| g.status == Some(GoalStatus::Inactive))
            .filter_map(|(i, g)| {
                let when = g.activate_when.as_ref()?;
                // One that isn't right waits to be put right.
                if !trigger_problems(g, goals, GoalStatus::Inactive).is_empty() {
                    return None;
                }
                let last = g.history.as_deref().unwrap_or_default().last().and_then(|c| moment(&c.at)).map(|m| local(&m, zone));
                let mut at = ready_at(when, goals, zone, last.unwrap_or(now_local))?;
                if at > now_local || held(g, when, goals) {
                    return None;
                }
                if when.hold_while.as_deref().is_some_and(|h| !h.is_empty()) {
                    at = at.max(freed.unwrap_or(at));
                }
                // Never before its last change, or after now.
                let at = at.max(last.unwrap_or(at)).min(now_local);
                can_start(g, goals, at).then_some((i, at))
            })
            .collect();
        ready.sort_by_key(|(i, _)| (priority_of(&goals[*i], goals), goals[*i].deadline.as_deref().and_then(deadline_end)));
        let Some((index, at)) = ready.first().copied() else { break };
        activated.extend(activate(goals, index, &stamp_of(at, zone)));
    }
    activated
}

/// When `goal`, waiting to be activated, is expected to be, local: when
/// its triggers are expected to come -- each goal it waits for at the
/// deadline it's due by -- then its delay; none if one has no deadline to
/// go by, or it's ready already (so held back).
fn expected_start_at(goal: &Goal, goals: &[Goal], zone: &Tz, now: NaiveDateTime, depth: usize) -> Option<NaiveDateTime> {
    if goal.status != Some(GoalStatus::Inactive) || depth > goals.len() {
        return None;
    }
    let when = goal.activate_when.as_ref()?;
    let mut last: Option<NaiveDateTime> = None;
    // A goal expected to close by its deadline closes within its day, not
    // at the midnight after: so the day after is the one after that.
    let mut last_day: Option<NaiveDateTime> = None;
    for trigger in when.after.as_deref().unwrap_or_default() {
        let (comes, day) = if let Some(id) = set(trigger.goal_closed.as_deref()) {
            let other = by_id(goals, id)?;
            match closed_moment(other) {
                Some(closed) => (local(&closed, zone), local(&closed, zone)),
                None => {
                    let end = deadline_end(&expected_deadline(other, goals, zone, now, depth + 1)?)?;
                    (end, end - Duration::minutes(1))
                }
            }
        } else if let Some(at) = set(trigger.at.as_deref()) {
            (start_of(at)?, start_of(at)?)
        } else {
            continue;
        };
        last = Some(last.map_or(comes, |l| l.max(comes)));
        last_day = Some(last_day.map_or(day, |l| l.max(day)));
    }
    let at = match when.delay {
        Some(Delay::NextDay(_)) => delayed(last_day.unwrap_or(now), when.delay.as_ref()),
        _ => delayed(last.unwrap_or(now), when.delay.as_ref()),
    };
    (at > now).then_some(at)
}

/// `goal`'s deadline, or, waiting for one, the one it's expected to have.
fn expected_deadline(goal: &Goal, goals: &[Goal], zone: &Tz, now: NaiveDateTime, depth: usize) -> Option<String> {
    if let Some(deadline) = set(goal.deadline.as_deref()) {
        return Some(deadline.to_string());
    }
    if depth > goals.len() {
        return None;
    }
    let rule = goal.deadline_rule.as_ref()?;
    if let Some(offset) = &rule.after_start {
        return after_start(expected_start_at(goal, goals, zone, now, depth + 1)?, offset);
    }
    if let Some(offset) = &rule.before_parent {
        let parent = parent_of(goals, goal)?;
        return shifted(&expected_deadline(parent, goals, zone, now, depth + 1)?, offset.days, offset.minutes, -1);
    }
    let offset = rule.after_goal.as_ref()?;
    shifted(&expected_deadline(by_id(goals, &offset.id)?, goals, zone, now, depth + 1)?, offset.days, offset.minutes, 1)
}

/// A local time as a goal shows it: a date, at a day's start; else a
/// date and time.
fn shown(at: NaiveDateTime) -> String {
    if at.time() == chrono::NaiveTime::MIN {
        at.format(DATE).to_string()
    } else {
        at.format(DATE_TIME).to_string()
    }
}

// -- the store -------------------------------------------------------------------

/// Every person's id, and every circle with who's in it, to check the
/// people and circles goals name against.
pub type PersonIds = Arc<dyn Fn() -> anyhow::Result<(Vec<String>, crate::attendees::Circles)> + Send + Sync>;

/// The active habits with a recurring goal, to make their goals from,
/// and the calendar's time zone, their periods' -- none without any.
pub type TemplatesFn = Arc<dyn Fn() -> anyhow::Result<Option<(Vec<Template>, Tz)>> + Send + Sync>;

/// Where the habits are, read with the goals, and their templates.
#[derive(Clone)]
pub struct Renewal {
    pub habits_tab: TabRange,
    pub templates: TemplatesFn,
}

#[derive(Clone)]
pub struct Goals {
    sheet: RowSheet,
    people: Option<PersonIds>,
    renewal: Option<Renewal>,
}

impl Goals {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<Goals> {
        Ok(Goals { sheet: RowSheet::ensure(sheets, spreadsheet_id, GOALS.0, GOALS.1, GOAL_ROW, &["id", "title"])?, people: None, renewal: None })
    }

    /// It, checking the people goals name against `people`.
    pub fn with_people(mut self, people: PersonIds) -> Goals {
        self.people = Some(people);
        self
    }

    /// It, making the goals of `renewal`'s habits as their periods come
    /// (see habit_goals::renew).
    pub fn with_renewal(mut self, renewal: Renewal) -> Goals {
        self.renewal = Some(renewal);
        self
    }

    pub fn whole_tab(&self) -> TabRange {
        self.sheet.whole_tab()
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheet.prefetch(ranges)
    }

    /// Every goal, as the tab keeps it -- with each habit's goal for the
    /// period under way made, or its missed one tried again (see
    /// habit_goals::renew). A read doesn't write those; the next change
    /// of goals does, as they are here -- made the same till then.
    pub fn all(&self) -> anyhow::Result<Vec<Goal>> {
        let Some(renewal) = &self.renewal else { return Ok(upgraded(self.sheet.read_items::<Goal>()?)) };
        self.sheet.prefetch(&[self.sheet.whole_tab(), renewal.habits_tab.clone()])?;
        let mut goals = upgraded(self.sheet.read_items::<Goal>()?);
        if let Some((templates, zone)) = (renewal.templates)()? {
            habit_goals::renew(&mut goals, &templates, &zone, local(&crate::determinism::now(), &zone));
        }
        Ok(goals)
    }

    /// Every goal as it stands now: those whose activate_when is ready
    /// activated (see `sweep`), with the deadlines that follow. A read
    /// doesn't write them; the next change of goals does, as they are
    /// here. With the ids of those it activated.
    pub fn current(&self, zone: &Tz) -> anyhow::Result<(Vec<Goal>, Vec<String>)> {
        let mut goals = self.all()?;
        let activated = sweep(&mut goals, zone, &crate::determinism::now());
        if !activated.is_empty() {
            resolve_deadlines(&mut goals, zone);
            stamp_deadlines(&mut goals, &activated);
        }
        Ok((goals, activated))
    }

    /// Every id there is a goal by.
    pub fn ids(&self) -> anyhow::Result<Vec<String>> {
        Ok(self.all()?.into_iter().filter_map(|g| g.id).collect())
    }

    pub fn get_goals(&self, statuses: Option<&[String]>, zone: &Tz) -> anyhow::Result<Vec<ListedGoal>> {
        let statuses = check_statuses(statuses)?;
        let (goals, _) = self.current(zone)?;
        Ok(goals.iter().filter(|g| g.status.is_some_and(|s| statuses.iter().any(|t| t == s.text()))).map(|g| listed(g, &goals, zone)).collect())
    }

    pub fn get_goal(&self, id_or_title: &str, zone: &Tz) -> anyhow::Result<ListedGoal> {
        let (goals, _) = self.current(zone)?;
        Ok(listed(&find(&goals, id_or_title)?, &goals, zone))
    }

    pub fn create_goal(&self, goal: &Goal, zone: &Tz) -> anyhow::Result<CreatedGoal> {
        let changed = self.change_goals(std::slice::from_ref(goal), &[], &[], zone)?;
        let made = changed.goals.into_iter().next().expect("a create gives its goal");
        Ok(CreatedGoal { created_id: made.id.clone().unwrap_or_default(), goal: made })
    }

    /// Make `creates`, apply `updates` and delete `removals`, all at once:
    /// checked whole, so a goal can be marked done while its sub-goals
    /// are closed or moved, and a new parent made for them, in one change.
    pub fn change_goals(&self, creates: &[Goal], updates: &[GoalEdit], removals: &[GoalRemoval], zone: &Tz) -> anyhow::Result<ChangedGoals> {
        // Those ready activated first, as a read shows them.
        let (mut goals, swept) = self.current(zone)?;
        let instant = crate::determinism::now();
        let now = DateTimeText(py::pydantic_datetime(&instant));
        let mut touched: Vec<String> = swept.clone();
        let mut deleted: Vec<String> = Vec::new();
        // The goals whose history this change appends to itself: their
        // new entry takes the goal's deadline once every deadline is set.
        let mut stamped: Vec<String> = swept;
        // Those activated by hand: their inactive parents are too.
        let mut started: Vec<String> = Vec::new();
        // Every goal changed since it was read, not just the first.
        let mut stale: Vec<rows::Conflict> = Vec::new();
        let gather = |result: anyhow::Result<bool>, stale: &mut Vec<rows::Conflict>| -> anyhow::Result<Option<bool>> {
            match result {
                Ok(go) => Ok(Some(go)),
                Err(error) => match error.downcast::<rows::Conflict>() {
                    Ok(one) => {
                        stale.push(one);
                        Ok(None)
                    }
                    Err(error) => Err(error),
                },
            }
        };
        for removal in removals {
            let id = removal.goal_id.trim();
            let current = goals.iter().find(|g| g.id.as_deref() == Some(id));
            let Some(go) = gather(rows::deleting(id, current, removal.if_version), &mut stale)? else { continue };
            if go {
                if current.is_none() {
                    find(&goals, id)?;
                }
                goals.retain(|g| g.id.as_deref() != Some(id));
            }
            deleted.push(id.to_string());
        }
        // What waited for a goal deleted doesn't; a deadline measured from
        // one keeps the date it had.
        for goal in goals.iter_mut() {
            let mut changed = false;
            if let Some(when) = goal.activate_when.as_mut() {
                if let Some(after) = when.after.as_mut() {
                    let before = after.len();
                    after.retain(|t| !t.goal_closed.as_ref().is_some_and(|id| deleted.iter().any(|d| d == id.trim())));
                    changed |= after.len() != before;
                }
            }
            if goal.deadline_rule.as_ref().and_then(|r| r.after_goal.as_ref()).is_some_and(|o| deleted.iter().any(|d| d == o.id.trim())) {
                goal.deadline_rule = None;
                changed = true;
            }
            if changed {
                touched.extend(goal.id.clone());
            }
        }
        for edit in updates {
            let Some(id) = set(edit.goal.id.as_deref()).map(str::to_string) else {
                bail!("a goal update needs the goal's id");
            };
            if deleted.contains(&id) {
                bail!("goal {id} is both updated and deleted: give it one change");
            }
            rows::check_clear_item(&edit.goal, &edit.clear_fields, &CLEARABLE_FIELDS)?;
            let Some(index) = goals.iter().position(|g| g.id.as_deref() == Some(id.as_str())) else {
                let gone = rows::expect_item_version::<Goal>(&id, None, edit.goal.version).map(|_| true);
                if gather(gone, &mut stale)?.is_none() {
                    continue;
                }
                find(&goals, &id)?;
                unreachable!()
            };
            let before = goals[index].clone();
            let mut after = rows::updated_item(&before, &edit.goal, &edit.clear_fields, &["id"])?;
            let cleared = |field: &str| edit.clear_fields.iter().any(|f| f == field);
            // A deadline of its own unties it from what it was measured
            // from.
            if (edit.goal.deadline.is_some() || cleared("deadline")) && edit.goal.deadline_rule.is_none() && !cleared("deadline_rule") {
                after.deadline_rule = None;
            }
            after.deadline_offset_minutes = None;
            if edit.goal.history.is_none() && after.status != before.status {
                if let Some(status) = after.status {
                    after.history.get_or_insert_with(Vec::new).push(StatusChange { status, at: now.clone(), deadline: None });
                    stamped.push(id.clone());
                }
            }
            // Activated by hand: what it waited for no longer matters.
            if after.status == Some(GoalStatus::Active) && before.status != Some(GoalStatus::Active) {
                after.activate_when = None;
                started.push(id.clone());
            }
            goals[index] = after;
            touched.push(id);
        }
        for given in creates {
            let id = match rows::created(given, &goals, &[])? {
                rows::Created::New(id) => id,
                rows::Created::Again(made) => {
                    touched.extend(made.id.clone());
                    continue;
                }
            };
            let status = given.status.unwrap_or(GoalStatus::Active);
            let history = given.history.clone().unwrap_or_else(|| {
                stamped.push(id.clone());
                vec![StatusChange { status, at: now.clone(), deadline: None }]
            });
            goals.push(Goal { id: Some(id.clone()), status: Some(status), history: Some(history), version: None, ..given.clone() });
            if status == GoalStatus::Active {
                started.push(id.clone());
            }
            touched.push(id);
        }
        for id in &started {
            let Some(index) = goals.iter().position(|g| g.id.as_deref() == Some(id.as_str())) else { continue };
            let parents = activate(&mut goals, index, &now);
            touched.extend(parents.iter().cloned());
            stamped.extend(parents);
        }
        touched.extend(resolve_deadlines(&mut goals, zone));
        // What this change closed may let others start.
        let ready = sweep(&mut goals, zone, &instant);
        if !ready.is_empty() {
            touched.extend(ready.iter().cloned());
            stamped.extend(ready);
            touched.extend(resolve_deadlines(&mut goals, zone));
        }
        // A change the server records itself carries the goal's deadline
        // after the change, once every deadline has followed what it's
        // measured from.
        stamp_deadlines(&mut goals, &stamped);
        let mut involved = touched.clone();
        involved.extend(deleted.iter().cloned());
        let mut problems: Vec<String> =
            goal_problems(&goals, zone).into_iter().filter(|(ids, _)| ids.iter().any(|i| involved.contains(i))).map(|(_, p)| p).collect();
        problems.extend(self.people_problems(&goals, &touched)?);
        if !stale.is_empty() {
            // With those of the goals that are there, as the write finds
            // them.
            if let Err(error) = self.sheet.prepared_items(&goals) {
                match rows::conflicts_of(&error) {
                    Some(found) => stale.extend(found.items),
                    None => return Err(error),
                }
            }
            return Err(rows::Conflicts::of(stale).into());
        }
        if !problems.is_empty() {
            bail!("{}", dedup(problems).join("; "));
        }
        self.sheet.write_items(&goals)?;
        let shown = touched
            .iter()
            .filter_map(|id| goals.iter().find(|g| g.id.as_deref() == Some(id.as_str())))
            .map(|g| listed(g, &goals, zone))
            .collect();
        Ok(ChangedGoals { goals: dedup_listed(shown), deleted_ids: deleted })
    }
}

/// Each of `ids`' goals' last history entry given the goal's deadline.
fn stamp_deadlines(goals: &mut [Goal], ids: &[String]) {
    for goal in goals.iter_mut().filter(|g| g.id.as_ref().is_some_and(|id| ids.contains(id))) {
        let deadline = goal.deadline.clone();
        if let Some(change) = goal.history.as_mut().and_then(|h| h.last_mut()) {
            change.deadline = deadline;
        }
    }
}

impl Goals {
    /// What's wrong with the people and circles `touched` goals name: who
    /// aren't people or circles, an except a circle can't take, and who
    /// would be both with and for through a plain circle (with `people`
    /// to check against).
    fn people_problems(&self, goals: &[Goal], touched: &[String]) -> anyhow::Result<Vec<String>> {
        let Some(lookup) = &self.people else { return Ok(Vec::new()) };
        let named: Vec<&Goal> = goals.iter().filter(|g| g.id.as_ref().is_some_and(|i| touched.contains(i))).filter(|g| !people_of(g).is_empty()).collect();
        if named.is_empty() {
            return Ok(Vec::new());
        }
        let (known, circles) = lookup()?;
        let mut problems = Vec::new();
        for g in named {
            let is_person = |p: &str| known.iter().any(|k| k == p);
            let naming = circles.naming(&named_entries(g.with_ids.as_deref()), &named_entries(g.for_ids.as_deref()), &is_person);
            if !naming.unknown.is_empty() {
                problems.push(format!("{} names {}, who aren't people or circles (get_people and get_circles list them)", label(g), py::repr_list(&naming.unknown)));
            }
            problems.extend(naming.other.iter().map(|p| format!("{}: {p}", label(g))));
            if !naming.both.is_empty() {
                problems.push(format!(
                    "{} names {} both with (doing it alongside) and for (not part of doing it): a plain circle stands for its members",
                    label(g),
                    py::repr_list(&naming.both)
                ));
            }
        }
        Ok(problems)
    }
}

/// The ids `goal` names -- people's and circles' -- it's done with, then
/// those it's done for.
pub fn people_of(goal: &Goal) -> Vec<&String> {
    goal.with_ids.iter().chain(goal.for_ids.iter()).flatten().map(|n| &n.id).collect()
}

/// Who `goal` is with, then who it's for, its plain circles' members
/// among them (see crate::attendees).
pub fn attendees_of(goal: &Goal, circles: &crate::attendees::Circles) -> (Vec<String>, Vec<String>) {
    (circles.attendees(&named_entries(goal.with_ids.as_deref())), circles.attendees(&named_entries(goal.for_ids.as_deref())))
}

fn dedup_listed(goals: Vec<ListedGoal>) -> Vec<ListedGoal> {
    let mut out: Vec<ListedGoal> = Vec::new();
    for goal in goals {
        if !out.iter().any(|g| g.id == goal.id) {
            out.push(goal);
        }
    }
    out
}

/// The goal `id_or_title` names: by id, or else by title (ignoring case).
pub fn find(goals: &[Goal], id_or_title: &str) -> anyhow::Result<Goal> {
    if let Some(g) = goals.iter().find(|g| g.id.as_deref() == Some(id_or_title)) {
        return Ok(g.clone());
    }
    let key = |g: &Goal| g.title.as_deref().unwrap_or_default().trim().to_lowercase();
    let wanted = id_or_title.trim().to_lowercase();
    if let Some(g) = goals.iter().rev().find(|g| key(g) == wanted) {
        return Ok(g.clone());
    }
    let ids = dedup(goals.iter().filter_map(|g| set(g.id.as_deref()).map(str::to_string)));
    let titles = dedup(goals.iter().map(key));
    let mut close: Vec<&Goal> = Vec::new();
    for id in crate::difflib::get_close_matches(id_or_title, &ids, 3) {
        close.extend(goals.iter().rev().find(|g| g.id.as_deref() == Some(id.as_str())));
    }
    for title in crate::difflib::get_close_matches(&wanted, &titles, 3) {
        close.extend(goals.iter().rev().find(|g| key(g) == title));
    }
    let hints = dedup(close.iter().map(|g| format!("{} ({})", str_or_none(g.id.as_deref()), str_or_none(g.title.as_deref()))));
    let hint = hints.join(", ");
    bail!(
        "There's no goal with the id or title {}{} (get_goals lists them)",
        py::repr(id_or_title),
        if hint.is_empty() { String::new() } else { format!("; did you mean {hint}?") }
    )
}

fn label(goal: &Goal) -> String {
    match (set(goal.id.as_deref()), set(goal.title.as_deref())) {
        (Some(id), Some(title)) => format!("goal {id} ({})", py::repr(title)),
        (Some(id), None) => format!("goal {id}"),
        (None, title) => format!("a goal titled {}", title.map(py::repr).unwrap_or_else(|| "None".into())),
    }
}

fn parent_of<'a>(goals: &'a [Goal], goal: &Goal) -> Option<&'a Goal> {
    let parent = set(goal.parent_id.as_deref())?;
    goals.iter().find(|g| g.id.as_deref() == Some(parent))
}

/// `goal`'s parents' titles, outermost first (stopping at a cycle).
pub fn path(goal: &Goal, goals: &[Goal]) -> Vec<String> {
    let mut titles = Vec::new();
    let mut seen: Vec<&str> = goal.id.as_deref().into_iter().collect();
    let mut at = parent_of(goals, goal);
    while let Some(parent) = at {
        let Some(id) = parent.id.as_deref() else { break };
        if seen.contains(&id) {
            break;
        }
        seen.push(id);
        titles.push(parent.title.clone().unwrap_or_default());
        at = parent_of(goals, parent);
    }
    titles.reverse();
    titles
}

/// What's wrong with `goals`, each problem with the ids of the goals it's
/// about (so a write is refused only for problems it's part of).
pub fn goal_problems(goals: &[Goal], zone: &Tz) -> Vec<(Vec<String>, String)> {
    let mut problems: Vec<(Vec<String>, String)> = Vec::new();
    let id_of = |g: &Goal| g.id.clone().unwrap_or_default();
    let mut seen: Vec<&str> = Vec::new();
    for goal in goals {
        let Some(id) = set(goal.id.as_deref()) else {
            problems.push((Vec::new(), format!("{} has no id", label(goal))));
            continue;
        };
        if seen.contains(&id) {
            problems.push((vec![id.to_string()], format!("goal id {} is used more than once", py::repr(id))));
        }
        seen.push(id);
    }
    for goal in goals {
        let me = vec![id_of(goal)];
        let name = label(goal);
        match goal.title.as_deref().map(str::trim).filter(|t| !t.is_empty()) {
            None => problems.push((me.clone(), format!("{name} needs a title"))),
            Some(t) if t.chars().count() > MAX_TITLE_LENGTH => {
                problems.push((me.clone(), format!("{name}'s title is longer than {MAX_TITLE_LENGTH} characters")))
            }
            Some(_) => {}
        }
        let ids = |named: Option<&[Named]>| named.unwrap_or_default().iter().map(|n| n.id.clone()).collect::<Vec<String>>();
        let (with, for_) = (ids(goal.with_ids.as_deref()), ids(goal.for_ids.as_deref()));
        let for_ = &for_;
        if with.iter().chain(for_).any(|p| p == SELF_ID) {
            problems.push((me.clone(), format!("{name} can't name \"self\" in with_ids or for_ids: the user is part of every goal")));
        }
        let mut both: Vec<&String> = with.iter().filter(|p| for_.contains(p)).collect();
        both.dedup();
        if !both.is_empty() {
            problems.push((me.clone(), format!("{name} names {} both with (doing it alongside) and for (not part of doing it)", py::repr_list(&both))));
        }
        if let Some(priority) = goal.priority.filter(|p| *p < 0) {
            problems.push((me.clone(), format!("{name}'s priority must be 0 or more, not {priority}")));
        }
        // Only a habit's goal is named for it, and always.
        let named_for = goal.id.as_deref().and_then(habit_goals::habit_of_id);
        if named_for != set(goal.habit_id.as_deref()) {
            problems.push((me.clone(), format!("{name} is a habit's goal exactly when its id is \"<habit id>_<its period's start>\": the server makes them")));
        }
        if let Some(minutes) = goal.expected_minutes.filter(|m| *m <= 0) {
            problems.push((me.clone(), format!("{name}'s expected_minutes must be more than 0, not {minutes}")));
        }
        let due = match set(goal.deadline.as_deref()) {
            None => None,
            Some(text) => match deadline_end(text) {
                Some(due) => Some(due),
                None => {
                    problems.push((
                        me.clone(),
                        format!("{name}'s deadline {} isn't a date (\"2026-11-01\") or a date and time (\"2026-11-01T17:00\")", py::repr(text)),
                    ));
                    None
                }
            },
        };
        let Some(status) = goal.status else {
            problems.push((me.clone(), format!("{name}'s status must be one of {}", GOAL_STATUSES.join(", "))));
            continue;
        };
        problems.extend(rule_problems(goal, goals).into_iter().map(|p| (me.clone(), format!("{name}'s deadline {p}"))));
        problems.extend(trigger_problems(goal, goals, status).into_iter().map(|p| (me.clone(), format!("{name}'s activate_when {p}"))));
        if status == GoalStatus::Active && set(goal.deadline.as_deref()).is_none() {
            problems.push((me.clone(), format!("{name} is active, so it needs a deadline")));
        }
        if status == GoalStatus::Inactive && set(goal.deadline.as_deref()).is_none() && goal.deadline_rule.is_none() {
            problems.push((
                me.clone(),
                format!("{name} needs a deadline, to have when it starts: a date, or one measured from its start, its parent's deadline or another goal's"),
            ));
        }
        problems.extend(history_problems(goal, status).into_iter().map(|p| (me.clone(), format!("{name}'s history {p}"))));
        if let (Some(due), Some(started)) = (due, started_at(goal).and_then(moment)) {
            if local(&started, zone) >= due {
                problems.push((me.clone(), format!("{name} started ({}) after its deadline ({})", started_at(goal).unwrap().0, goal.deadline.as_deref().unwrap_or_default())));
            }
        }
        if let Some(parent_id) = set(goal.parent_id.as_deref()) {
            if Some(parent_id) == goal.id.as_deref() {
                problems.push((me.clone(), format!("{name} can't be its own parent")));
                continue;
            }
            let Some(parent) = parent_of(goals, goal) else {
                problems.push((vec![id_of(goal), parent_id.to_string()], format!("{name}'s parent_id {} isn't a goal (get_goals lists them)", py::repr(parent_id))));
                continue;
            };
            if cycles(goal, goals) {
                problems.push((me.clone(), format!("{name} is under itself: a goal can't be its own sub-goal, at any depth")));
                continue;
            }
            let both = vec![id_of(goal), id_of(parent)];
            let parent_name = label(parent);
            let Some(parent_status) = parent.status else { continue };
            if status.is_closed() {
                continue;
            }
            if parent_status.is_closed() {
                problems.push((
                    both.clone(),
                    format!(
                        "{name} is {}, but its parent {parent_name} is {}: mark it done, missed or obsolete, move it under another goal, or reopen its parent",
                        status.text(),
                        parent_status.text()
                    ),
                ));
            }
            if let (Some(due), Some(parent_due)) = (due, set(parent.deadline.as_deref()).and_then(deadline_end)) {
                if due > parent_due {
                    problems.push((
                        both.clone(),
                        format!(
                            "{name}'s deadline ({}) is after its parent {parent_name}'s ({})",
                            goal.deadline.as_deref().unwrap_or_default(),
                            parent.deadline.as_deref().unwrap_or_default()
                        ),
                    ));
                }
            }
            if let (Some(minutes), Some(parent_minutes)) = (goal.expected_minutes, parent.expected_minutes) {
                if minutes > parent_minutes {
                    problems.push((
                        both,
                        format!("{name} is expected to take longer ({minutes} minutes) than its parent {parent_name} ({parent_minutes} minutes)"),
                    ));
                }
            }
        }
    }
    problems
}

/// What's wrong with how `offset` (days or minutes) is given.
fn offset_problems(days: Option<i64>, minutes: Option<i64>) -> Vec<String> {
    match (days, minutes) {
        (Some(_), Some(_)) | (None, None) => vec!["gives days or minutes, one of them".into()],
        (Some(n), None) | (None, Some(n)) if n < 0 => vec![format!("can't be {n}: give 0 or more")],
        _ => Vec::new(),
    }
}

/// What's wrong with what `goal`'s deadline is measured from.
fn rule_problems(goal: &Goal, goals: &[Goal]) -> Vec<String> {
    let Some(rule) = &goal.deadline_rule else { return Vec::new() };
    let mut problems = Vec::new();
    if rule.kinds() != 1 || rule.at.is_some() {
        problems.push("is measured from one thing: before_parent, after_start or after_goal".into());
        return problems;
    }
    let offset = rule.before_parent.as_ref().or(rule.after_start.as_ref()).map(|o| (o.days, o.minutes));
    let (days, minutes) = offset.or(rule.after_goal.as_ref().map(|o| (o.days, o.minutes))).unwrap_or_default();
    problems.extend(offset_problems(days, minutes));
    if let Some(after) = &rule.after_goal {
        let id = after.id.trim();
        if Some(id) == goal.id.as_deref() {
            problems.push("can't be measured from its own".into());
        } else if by_id(goals, id).is_none() {
            problems.push(format!("is after goal {}, which isn't a goal (get_goals lists them)", py::repr(id)));
        }
    }
    if problems.is_empty() && measured_from_itself(goal, goals) {
        problems.push("is measured, through others, from its own".into());
    }
    problems
}

/// The goal `goal`'s deadline is measured from, if it's another's.
fn measured_from<'a>(goal: &Goal, goals: &'a [Goal]) -> Option<&'a Goal> {
    let rule = goal.deadline_rule.as_ref()?;
    if rule.before_parent.is_some() {
        return parent_of(goals, goal);
    }
    by_id(goals, rule.after_goal.as_ref()?.id.trim())
}

/// Whether following what `goal`'s deadline is measured from comes back
/// to it.
fn measured_from_itself(goal: &Goal, goals: &[Goal]) -> bool {
    let mut seen: Vec<&str> = Vec::new();
    let mut at = measured_from(goal, goals);
    while let Some(g) = at {
        let Some(id) = g.id.as_deref() else { return false };
        if Some(id) == goal.id.as_deref() {
            return true;
        }
        if seen.contains(&id) {
            return false;
        }
        seen.push(id);
        at = measured_from(g, goals);
    }
    false
}

/// What's wrong with what `goal`, `status`, waits for.
fn trigger_problems(goal: &Goal, goals: &[Goal], status: GoalStatus) -> Vec<String> {
    let Some(when) = &goal.activate_when else { return Vec::new() };
    let mut problems = Vec::new();
    if status != GoalStatus::Inactive {
        problems.push(format!("is only for an inactive goal: this one is {}", status.text()));
    }
    let parents = ancestor_ids(goal, goals);
    for trigger in when.after.as_deref().unwrap_or_default() {
        match (set(trigger.goal_closed.as_deref()), set(trigger.at.as_deref())) {
            (Some(id), None) => {
                if Some(id) == goal.id.as_deref() {
                    problems.push("can't wait for the goal itself".into());
                } else if by_id(goals, id).is_none() {
                    problems.push(format!("waits for goal {}, which isn't a goal (get_goals lists them)", py::repr(id)));
                } else if parents.iter().any(|p| p == id) {
                    problems.push(format!("waits for goal {} to close, but it's one of its parents, which can't close while it's open", py::repr(id)));
                }
            }
            (None, Some(at)) => {
                if start_of(at).is_none() {
                    problems.push(format!("waits for {}, which isn't a date (\"2026-11-01\") or a date and time (\"2026-11-01T17:00\")", py::repr(at)));
                }
            }
            _ => problems.push("has a trigger that isn't one: give goal_closed or at, one of them".into()),
        }
    }
    if let Some(Delay::Minutes(d)) = &when.delay {
        if d.minutes < 0 {
            problems.push(format!("delay can't be {} minutes: give 0 or more", d.minutes));
        }
    }
    for hold in when.hold_while.as_deref().unwrap_or_default() {
        if hold.active_at_least < 1 {
            problems.push(format!("holds while at least {} goals are active: give 1 or more", hold.active_at_least));
        }
        if let Some(filter) = &hold.priority {
            match (&filter.at_or_above, &filter.exactly) {
                (Some(r), None) | (None, Some(r)) => {
                    if let PriorityRef::Level(p) = r {
                        if *p < 0 {
                            problems.push(format!("holds for priority {p}: give 0 or more, or \"own\""));
                        }
                    }
                }
                _ => problems.push("holds for a priority given as at_or_above or exactly, one of them".into()),
            }
        }
    }
    if problems.is_empty() && waits_on_itself(goal, goals) {
        problems.push("waits, through others, for the goal itself".into());
    }
    problems
}

/// The goals `goal` waits to close.
fn waits_for<'a>(goal: &Goal, goals: &'a [Goal]) -> Vec<&'a Goal> {
    let Some(when) = &goal.activate_when else { return Vec::new() };
    when.after.as_deref().unwrap_or_default().iter().filter_map(|t| set(t.goal_closed.as_deref())).filter_map(|id| by_id(goals, id)).collect()
}

/// Whether following what `goal` waits for comes back to it.
fn waits_on_itself(goal: &Goal, goals: &[Goal]) -> bool {
    let mut seen: Vec<&str> = Vec::new();
    let mut next: Vec<&Goal> = waits_for(goal, goals);
    while let Some(g) = next.pop() {
        let Some(id) = g.id.as_deref() else { continue };
        if Some(id) == goal.id.as_deref() {
            return true;
        }
        if seen.contains(&id) {
            continue;
        }
        seen.push(id);
        next.extend(waits_for(g, goals));
    }
    false
}

/// Whether `goal` is among its own parents.
fn cycles(goal: &Goal, goals: &[Goal]) -> bool {
    let Some(id) = goal.id.as_deref() else { return false };
    let mut seen: Vec<&str> = Vec::new();
    let mut at = parent_of(goals, goal);
    while let Some(parent) = at {
        let Some(pid) = parent.id.as_deref() else { return false };
        if pid == id {
            return true;
        }
        if seen.contains(&pid) {
            return false;
        }
        seen.push(pid);
        at = parent_of(goals, parent);
    }
    false
}

fn history_problems(goal: &Goal, status: GoalStatus) -> Vec<String> {
    let history = goal.history.as_deref().unwrap_or_default();
    let Some(last) = history.last() else {
        return vec!["is empty: it needs at least the status the goal was made in".into()];
    };
    let mut problems = Vec::new();
    let mut previous: Option<(Moment, GoalStatus)> = None;
    for (n, change) in history.iter().enumerate() {
        let Some(at) = moment(&change.at) else {
            problems.push(format!("entry {} has a time that isn't one: {}", n + 1, py::repr(&change.at.0)));
            continue;
        };
        if let Some((before, was)) = previous {
            if at < before {
                problems.push(format!("entry {} ({}) is before the one ahead of it: keep it oldest first", n + 1, change.at.0));
            }
            if was == change.status {
                problems.push(format!("entry {} repeats the status before it ({})", n + 1, was.text()));
            }
        }
        previous = Some((at, change.status));
    }
    if last.status != status {
        problems.push(format!("ends with {}, but the goal is {}", last.status.text(), status.text()));
    }
    problems
}

/// `goal`'s deadline as set: what it's measured from, or the date given.
pub fn deadline_of(goal: &Goal) -> Option<Deadline> {
    goal.deadline_rule.clone().or_else(|| set(goal.deadline.as_deref()).map(|d| Deadline { at: Some(d.to_string()), ..Deadline::default() }))
}

pub fn listed(goal: &Goal, goals: &[Goal], zone: &Tz) -> ListedGoal {
    let now = local(&crate::determinism::now(), zone);
    ListedGoal {
        id: goal.id.clone(),
        title: goal.title.clone(),
        parent_id: goal.parent_id.clone(),
        required: goal.required,
        priority: goal.priority,
        sort_order: goal.sort_order,
        expected_minutes: goal.expected_minutes,
        deadline: deadline_of(goal),
        due: set(goal.deadline.as_deref()).map(str::to_string),
        activate_when: goal.activate_when.clone(),
        expected_start: expected_start_at(goal, goals, zone, now, 0).map(shown),
        status: goal.status,
        history: goal.history.clone(),
        description: goal.description.clone(),
        definition_of_done: goal.definition_of_done.clone(),
        attribution_hints: goal.attribution_hints.clone(),
        with_ids: goal.with_ids.clone(),
        for_ids: goal.for_ids.clone(),
        path: path(goal, goals),
        habit_id: goal.habit_id.clone(),
        started_at: started_at(goal).cloned(),
        closed_at: closed_at(goal).cloned(),
        // As a change to it is checked: one with none -- a habit's goal
        // made for the period under way and not written yet, or one
        // written before versions -- is version 0.
        version: Some(goal.version.unwrap_or(0)),
    }
}

fn check_statuses(statuses: Option<&[String]>) -> anyhow::Result<Vec<String>> {
    match statuses {
        None => Ok(DEFAULT_STATUSES.iter().map(|s| s.to_string()).collect()),
        Some(given) => {
            let mut unknown: Vec<&String> = given.iter().filter(|s| !GOAL_STATUSES.contains(&s.as_str())).collect();
            unknown.sort();
            unknown.dedup();
            if !unknown.is_empty() {
                bail!("Unknown goal status {}; goal statuses are {}", py::repr_list(&unknown), GOAL_STATUSES.join(", "));
            }
            Ok(given.to_vec())
        }
    }
}

// -- events' goal shares -------------------------------------------------------

pub use crate::stores::shares::{parse_shares, settled_shares, share_problems, shares_text};
use crate::stores::shares::{is_false, Share};

/// One goal an event is tagged with, and how much of the event it takes:
/// minutes (a fixed share), percent (of what the fixed shares leave), or
/// neither -- the remaining time, what the minutes and percents leave.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema)]
pub struct GoalShare {
    pub goal_id: String,
    pub minutes: Option<i64>,
    pub percent: Option<i64>,
    /// With minutes or percent: the remaining time too, as a goal with
    /// neither takes.
    #[schema(default = "false")]
    #[serde(default, skip_serializing_if = "is_false")]
    pub remaining: bool,
}

impl Share for GoalShare {
    const KIND: &'static str = "goal";
    const THEIRS: &'static str = "goals'";
    const NEITHER: &'static str = "the remaining time";

    fn new(goal_id: String, minutes: Option<i64>, percent: Option<i64>, remaining: bool) -> Self {
        GoalShare { goal_id, minutes, percent, remaining: remaining && (minutes.is_some() || percent.is_some()) }
    }
    fn id(&self) -> &str {
        &self.goal_id
    }
    fn minutes(&self) -> Option<i64> {
        self.minutes
    }
    fn percent(&self) -> Option<i64> {
        self.percent
    }
    fn remaining(&self) -> bool {
        self.remaining
    }
}

// Written by the app: made once by their ids and versions.
impl rows::Versioned for Goal {}

#[cfg(test)]
#[path = "goals_tests.rs"]
mod tests;
