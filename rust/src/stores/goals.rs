//! Goals: outcomes the user means to reach and can mark done, in the
//! Outcome Goals tab. A goal can sit under another, to any depth -- a
//! sub-goal is a piece of reaching its parent -- and events are tagged
//! with goals (an event's goal shares, kept on the event; see
//! `GoalShare`), apart from their actions. Goals aren't tied to actions,
//! habits, people or places.
//!
//! The old goals (replaced by actions) left a Goals tab and events'
//! goal_ids behind; these are kept apart from them: their own tab, and
//! events' goal_shares.

use crate::contract::{DateTimeText, RowItem, Schema};
use crate::event::{parse_iso, Moment};
use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::sheet::rows::{self, RowSheet, RowType};
use crate::sheet::GOALS;
use crate::stores::groups::{set, str_or_none};
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
pub const CLEARABLE_FIELDS: [&str; 12] = [
    "parent_id",
    "required",
    "priority",
    "sort_order",
    "expected_minutes",
    "deadline",
    "deadline_offset_minutes",
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

/// One goal: an outcome the user means to reach (see get_goals).
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
    /// zone. Needed while it's active.
    pub deadline: Option<String>,
    /// Set, its deadline follows its parent's: this many minutes before
    /// it (0, the same), kept so whenever the parent's changes, while
    /// it's open. A deadline set without it unties it; so does moving it
    /// to the top level, keeping the deadline it had.
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
    /// (person ids; never "self", and never also in for_ids).
    pub with_ids: Option<Vec<String>>,
    /// The people it's done for, who aren't part of doing it: a promise
    /// to them, a gift, a favor (person ids; never "self", and never also
    /// in with_ids).
    pub for_ids: Option<Vec<String>>,
    /// Goes up by one each time it changes. Give it to an update (and to a delete, as if_version): it's the version you last read, required -- one saved before versions is version 0 -- changed since, the change is refused; on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK.
    pub version: Option<i64>,
}

/// A goal as the goal tools return it: plus its parents' titles (path,
/// outermost first), when it first became active (started_at) and, done,
/// missed or obsolete, when it became so (closed_at).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct ListedGoal {
    pub id: Option<String>,
    pub title: Option<String>,
    pub parent_id: Option<String>,
    pub required: Option<bool>,
    pub priority: Option<i64>,
    pub sort_order: Option<i64>,
    pub expected_minutes: Option<i64>,
    pub deadline: Option<String>,
    pub deadline_offset_minutes: Option<i64>,
    pub status: Option<GoalStatus>,
    pub history: Option<Vec<StatusChange>>,
    pub description: Option<String>,
    pub definition_of_done: Option<String>,
    pub attribution_hints: Option<String>,
    pub with_ids: Option<Vec<String>>,
    pub for_ids: Option<Vec<String>>,
    pub path: Vec<String>,
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

/// The deadline `minutes` before `parent`'s: a date, if `parent`'s is
/// one and it's whole days before; else a date and time.
pub fn deadline_before(parent: &str, minutes: i64) -> Option<String> {
    let text = parent.trim();
    if minutes == 0 {
        return Some(text.to_string());
    }
    if let Ok(date) = NaiveDate::parse_from_str(text, "%Y-%m-%d") {
        if minutes % (24 * 60) == 0 {
            return Some((date - Duration::days(minutes / (24 * 60))).format("%Y-%m-%d").to_string());
        }
    }
    Some((deadline_end(text)? - Duration::minutes(minutes)).format("%Y-%m-%dT%H:%M").to_string())
}

/// Each open goal whose deadline follows its parent's (see
/// `Goal::deadline_offset_minutes`) given the deadline that follows from
/// it, parents first -- and one at the top level untied. The ids of those
/// changed.
fn follow_parents(goals: &mut [Goal]) -> Vec<String> {
    let mut changed: Vec<String> = Vec::new();
    // A pass for each level, at most; a cycle is refused anyway.
    for _ in 0..goals.len() {
        let mut again = false;
        for i in 0..goals.len() {
            let goal = &goals[i];
            let Some(minutes) = goal.deadline_offset_minutes else { continue };
            if goal.status.is_some_and(GoalStatus::is_closed) || minutes < 0 {
                continue;
            }
            let next = if set(goal.parent_id.as_deref()).is_none() {
                None
            } else {
                let Some(parent) = parent_of(goals, goal) else { continue };
                let Some(deadline) = set(parent.deadline.as_deref()).and_then(|d| deadline_before(d, minutes)) else { continue };
                Some(deadline)
            };
            let goal = &mut goals[i];
            match next {
                None => goal.deadline_offset_minutes = None,
                Some(deadline) if goal.deadline.as_deref() != Some(deadline.as_str()) => {
                    goal.deadline = Some(deadline);
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

fn moment(text: &DateTimeText) -> Option<Moment> {
    parse_iso(&text.0, None).ok()
}

// -- the store -------------------------------------------------------------------

/// Every person's id, to check the people goals name against.
pub type PersonIds = Arc<dyn Fn() -> anyhow::Result<Vec<String>> + Send + Sync>;

#[derive(Clone)]
pub struct Goals {
    sheet: RowSheet,
    people: Option<PersonIds>,
}

impl Goals {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<Goals> {
        Ok(Goals { sheet: RowSheet::ensure(sheets, spreadsheet_id, GOALS.0, GOALS.1, GOAL_ROW, &["id", "title"])?, people: None })
    }

    /// It, checking the people goals name against `people`.
    pub fn with_people(mut self, people: PersonIds) -> Goals {
        self.people = Some(people);
        self
    }

    pub fn whole_tab(&self) -> TabRange {
        self.sheet.whole_tab()
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheet.prefetch(ranges)
    }

    pub fn all(&self) -> anyhow::Result<Vec<Goal>> {
        self.sheet.read_items::<Goal>()
    }

    /// Every id there is a goal by.
    pub fn ids(&self) -> anyhow::Result<Vec<String>> {
        Ok(self.all()?.into_iter().filter_map(|g| g.id).collect())
    }

    pub fn get_goals(&self, statuses: Option<&[String]>) -> anyhow::Result<Vec<ListedGoal>> {
        let statuses = check_statuses(statuses)?;
        let goals = self.all()?;
        Ok(goals.iter().filter(|g| g.status.is_some_and(|s| statuses.iter().any(|t| t == s.text()))).map(|g| listed(g, &goals)).collect())
    }

    pub fn get_goal(&self, id_or_title: &str) -> anyhow::Result<ListedGoal> {
        let goals = self.all()?;
        Ok(listed(&find(&goals, id_or_title)?, &goals))
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
        let mut goals = self.all()?;
        let now = DateTimeText(py::pydantic_datetime(&crate::determinism::now()));
        let mut touched: Vec<String> = Vec::new();
        let mut deleted: Vec<String> = Vec::new();
        // The goals whose history this change appends to itself: their
        // new entry takes the goal's deadline once every deadline is set.
        let mut stamped: Vec<String> = Vec::new();
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
            // A deadline of its own unties it from its parent's.
            if (edit.goal.deadline.is_some() || cleared("deadline")) && edit.goal.deadline_offset_minutes.is_none() && !cleared("deadline_offset_minutes") {
                after.deadline_offset_minutes = None;
            }
            if edit.goal.history.is_none() && after.status != before.status {
                if let Some(status) = after.status {
                    after.history.get_or_insert_with(Vec::new).push(StatusChange { status, at: now.clone(), deadline: None });
                    stamped.push(id.clone());
                }
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
            touched.push(id);
        }
        touched.extend(follow_parents(&mut goals));
        // A change the server records itself carries the goal's deadline
        // after the change, once a sub-goal's has followed its parent's.
        for goal in goals.iter_mut().filter(|g| g.id.as_ref().is_some_and(|id| stamped.contains(id))) {
            let deadline = goal.deadline.clone();
            if let Some(change) = goal.history.as_mut().and_then(|h| h.last_mut()) {
                change.deadline = deadline;
            }
        }
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
            .map(|g| listed(g, &goals))
            .collect();
        Ok(ChangedGoals { goals: dedup_listed(shown), deleted_ids: deleted })
    }
}

impl Goals {
    /// The people `touched` goals name who aren't people (with `people`
    /// to check against).
    fn people_problems(&self, goals: &[Goal], touched: &[String]) -> anyhow::Result<Vec<String>> {
        let Some(lookup) = &self.people else { return Ok(Vec::new()) };
        let named: Vec<&Goal> = goals.iter().filter(|g| g.id.as_ref().is_some_and(|i| touched.contains(i))).filter(|g| !people_of(g).is_empty()).collect();
        if named.is_empty() {
            return Ok(Vec::new());
        }
        let known = lookup()?;
        Ok(named
            .into_iter()
            .filter_map(|g| {
                let unknown: Vec<&String> = people_of(g).into_iter().filter(|p| p.as_str() != SELF_ID && !known.contains(p)).collect();
                (!unknown.is_empty()).then(|| format!("{} names {}, who aren't people (get_people lists them)", label(g), py::repr_list(&unknown)))
            })
            .collect())
    }
}

/// The people `goal` is done with, then those it's done for.
pub fn people_of(goal: &Goal) -> Vec<&String> {
    goal.with_ids.iter().chain(goal.for_ids.iter()).flatten().collect()
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
        let with = goal.with_ids.as_deref().unwrap_or_default();
        let for_ = goal.for_ids.as_deref().unwrap_or_default();
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
        if let Some(minutes) = goal.deadline_offset_minutes.filter(|m| *m < 0) {
            problems.push((me.clone(), format!("{name}'s deadline_offset_minutes must be 0 or more, not {minutes}")));
        }
        if status == GoalStatus::Active && set(goal.deadline.as_deref()).is_none() {
            problems.push((me.clone(), format!("{name} is active, so it needs a deadline")));
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
            } else if status == GoalStatus::Active && parent_status == GoalStatus::Inactive {
                problems.push((both.clone(), format!("{name} is active, but its parent {parent_name} is inactive: activate the parent too, or deactivate it")));
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

pub fn listed(goal: &Goal, goals: &[Goal]) -> ListedGoal {
    ListedGoal {
        id: goal.id.clone(),
        title: goal.title.clone(),
        parent_id: goal.parent_id.clone(),
        required: goal.required,
        priority: goal.priority,
        sort_order: goal.sort_order,
        expected_minutes: goal.expected_minutes,
        deadline: goal.deadline.clone(),
        deadline_offset_minutes: goal.deadline_offset_minutes,
        status: goal.status,
        history: goal.history.clone(),
        description: goal.description.clone(),
        definition_of_done: goal.definition_of_done.clone(),
        attribution_hints: goal.attribution_hints.clone(),
        with_ids: goal.with_ids.clone(),
        for_ids: goal.for_ids.clone(),
        path: path(goal, goals),
        started_at: started_at(goal).cloned(),
        closed_at: closed_at(goal).cloned(),
        version: goal.version,
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
