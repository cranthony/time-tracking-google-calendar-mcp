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
use anyhow::bail;
use chrono::{Duration, NaiveDate, NaiveDateTime};
use chrono_tz::Tz;
use serde::{Deserialize, Serialize};

pub const GOAL_STATUSES: [&str; 5] = ["inactive", "active", "done", "missed", "obsolete"];
pub const DEFAULT_STATUSES: [&str; 2] = ["inactive", "active"];
pub const MAX_TITLE_LENGTH: usize = 200;
pub const CLEARABLE_FIELDS: [&str; 7] =
    ["parent_id", "required", "expected_minutes", "deadline", "description", "definition_of_done", "attribution_hints"];

// -- the types -------------------------------------------------------------------

/// Where a goal stands: inactive (not started, or set aside), active
/// (being worked on), done (reached), missed (not reached, and no longer
/// pursued) or obsolete (no longer wanted).
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

/// One change of a goal's status: what it became, and when.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct StatusChange {
    pub status: GoalStatus,
    pub at: DateTimeText,
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
    /// How long it's expected to take, in minutes of the user's time: a
    /// guide to compare the time spent with, never enforced.
    pub expected_minutes: Option<i64>,
    /// When it's due: a date ("2026-11-01", due at the end of that day)
    /// or a date and time ("2026-11-01T17:00"), in the calendar's time
    /// zone. Needed while it's active.
    pub deadline: Option<String>,
    #[row(text)]
    pub status: Option<GoalStatus>,
    /// Every change of its status, oldest first, the last its status now.
    pub history: Option<Vec<StatusChange>>,
    /// What it is, as HTML.
    pub description: Option<String>,
    /// What being done looks like, as HTML.
    pub definition_of_done: Option<String>,
    /// A guide for telling which events are for it.
    pub attribution_hints: Option<String>,
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
    pub expected_minutes: Option<i64>,
    pub deadline: Option<String>,
    pub status: Option<GoalStatus>,
    pub history: Option<Vec<StatusChange>>,
    pub description: Option<String>,
    pub definition_of_done: Option<String>,
    pub attribution_hints: Option<String>,
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

#[derive(Clone)]
pub struct Goals {
    sheet: RowSheet,
}

impl Goals {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<Goals> {
        Ok(Goals { sheet: RowSheet::ensure(sheets, spreadsheet_id, GOALS.0, GOALS.1, GOAL_ROW, &["id", "title"])? })
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
            if edit.goal.history.is_none() && after.status != before.status {
                if let Some(status) = after.status {
                    after.history.get_or_insert_with(Vec::new).push(StatusChange { status, at: now.clone() });
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
            let history = given.history.clone().unwrap_or_else(|| vec![StatusChange { status, at: now.clone() }]);
            goals.push(Goal { id: Some(id.clone()), status: Some(status), history: Some(history), version: None, ..given.clone() });
            touched.push(id);
        }
        let mut involved = touched.clone();
        involved.extend(deleted.iter().cloned());
        let problems: Vec<String> =
            goal_problems(&goals, zone).into_iter().filter(|(ids, _)| ids.iter().any(|i| involved.contains(i))).map(|(_, p)| p).collect();
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
        expected_minutes: goal.expected_minutes,
        deadline: goal.deadline.clone(),
        status: goal.status,
        history: goal.history.clone(),
        description: goal.description.clone(),
        definition_of_done: goal.definition_of_done.clone(),
        attribution_hints: goal.attribution_hints.clone(),
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

/// One goal an event is tagged with, and how much of the event it takes:
/// minutes (a fixed share), percent (of what the fixed shares leave), or
/// neither -- the whole event.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct GoalShare {
    pub goal_id: String,
    pub minutes: Option<i64>,
    pub percent: Option<i64>,
}

/// Shares as an event keeps them: "a b=45m c=30%".
pub fn shares_text(shares: &[GoalShare]) -> String {
    shares
        .iter()
        .map(|s| match (s.minutes, s.percent) {
            (Some(m), _) => format!("{}={m}m", s.goal_id),
            (None, Some(p)) => format!("{}={p}%", s.goal_id),
            (None, None) => s.goal_id.clone(),
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// Shares as `shares_text` wrote them; a token that isn't one is skipped.
pub fn parse_shares(text: &str) -> Vec<GoalShare> {
    text.split_whitespace()
        .filter_map(|token| match token.split_once('=') {
            None => Some(GoalShare { goal_id: token.into(), minutes: None, percent: None }),
            Some((id, amount)) if !id.is_empty() => {
                if let Some(m) = amount.strip_suffix('m').and_then(|m| m.parse().ok()) {
                    Some(GoalShare { goal_id: id.into(), minutes: Some(m), percent: None })
                } else {
                    amount.strip_suffix('%').and_then(|p| p.parse().ok()).map(|p| GoalShare { goal_id: id.into(), minutes: None, percent: Some(p) })
                }
            }
            Some(_) => None,
        })
        .collect()
}

/// What's wrong with `shares` on an event `event_minutes` long.
pub fn share_problems(shares: &[GoalShare], event_minutes: i64) -> Vec<String> {
    let mut problems = Vec::new();
    let mut seen: Vec<&str> = Vec::new();
    let (mut minutes, mut percent) = (0, 0);
    for share in shares {
        let id = share.goal_id.trim();
        if id.is_empty() || id.contains(char::is_whitespace) || id.contains('=') {
            problems.push(format!("goal id {} isn't one", py::repr(&share.goal_id)));
            continue;
        }
        if seen.contains(&id) {
            problems.push(format!("goal {id} is given more than once"));
        }
        seen.push(id);
        match (share.minutes, share.percent) {
            (Some(_), Some(_)) => problems.push(format!("goal {id} is given both minutes and percent: give one, or neither for the whole event")),
            (Some(m), None) if m <= 0 => problems.push(format!("goal {id}'s minutes must be more than 0, not {m}")),
            (Some(m), None) => minutes += m,
            (None, Some(p)) if !(1..=100).contains(&p) => problems.push(format!("goal {id}'s percent must be 1 to 100, not {p}")),
            (None, Some(p)) => percent += p,
            (None, None) => {}
        }
    }
    if minutes > event_minutes {
        problems.push(format!("its goals' minutes add up to {minutes}, more than the event's {event_minutes}"));
    }
    if percent > 100 {
        problems.push(format!("its goals' percents add up to {percent}, more than 100"));
    }
    problems
}

/// `shares` as an event `event_minutes` long gives them: only those whose
/// goal is still there (`known`), minutes cut down to fit in the event
/// (an event shortened since they were given), and percents to 100.
pub fn settled_shares(shares: &[GoalShare], event_minutes: i64, known: &dyn Fn(&str) -> bool) -> Vec<GoalShare> {
    let (mut minutes_left, mut percent_left) = (event_minutes.max(0), 100);
    let mut out: Vec<GoalShare> = Vec::new();
    for share in shares.iter().filter(|s| known(&s.goal_id)) {
        if out.iter().any(|s| s.goal_id == share.goal_id) {
            continue;
        }
        let mut share = share.clone();
        if let Some(m) = share.minutes {
            let m = m.clamp(0, minutes_left);
            minutes_left -= m;
            share.minutes = Some(m);
            share.percent = None;
        } else if let Some(p) = share.percent {
            let p = p.clamp(0, percent_left);
            percent_left -= p;
            share.percent = Some(p);
        }
        out.push(share);
    }
    out
}

// Written by the app: made once by their ids and versions.
impl rows::Versioned for Goal {}

#[cfg(test)]
#[path = "goals_tests.rs"]
mod tests;
