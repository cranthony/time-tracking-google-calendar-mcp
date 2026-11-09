//! Goal changes: the status changes compaction proposes to goals there
//! are -- done, missed or obsolete -- from what the notes say happened
//! ('sent Kopal the recipe') or a deadline that passed in the window with
//! nothing showing the goal done. Claude sends them with compact_notes'
//! `goal_changes`; a proposal keeps them through its revisions; the user
//! settles one sooner in review (applied now, dropped, or changed), and
//! the rest are applied, each as one update of the goal, when the
//! proposal is.

use super::proposals::Dict;
use crate::event::{parse_iso, Moment};
use crate::py;
use crate::stores::goals::{self, goal_problems, Goal, GoalEdit, GoalStatus, Goals, ListedGoal, StatusChange};
use chrono_tz::Tz;
use serde_json::{json, Map, Value};

pub const USES: [&str; 3] = ["apply", "drop", "change"];

/// One change: `goal_id` becomes `status` at `at` (a time in the
/// window; the proposal's `through` when not given), from `note_ids`.
/// With `retry_deadline` (status missed), the goal is missed and tried
/// again by then: its history gains missed (with the deadline it missed)
/// then its prior status (with the new one), both at `at`, and it stays
/// open. With `retry_fit` too, what's in the way of that new deadline is
/// changed with it.
#[derive(Debug, Clone, PartialEq)]
pub struct GoalChange {
    pub goal_id: String,
    pub status: GoalStatus,
    pub at: Option<Moment>,
    pub note_ids: Vec<String>,
    pub retry_deadline: Option<String>,
    pub retry_fit: Option<RetryFit>,
}

/// How a retry's new deadline is made to fit, when it's after a parent's
/// -- the user's choice in review, nothing the server does by itself.
#[derive(Debug, Clone, PartialEq)]
pub enum RetryFit {
    /// Each open goal above it due before the new deadline is due by it
    /// too (see `extended`).
    ExtendParents,
    /// It goes under `parent_id`, or to the top level with none.
    Move { parent_id: Option<String> },
}

impl RetryFit {
    pub fn to_json(&self) -> Value {
        match self {
            RetryFit::ExtendParents => json!({"kind": "extend_parents"}),
            RetryFit::Move { parent_id } => json!({"kind": "move", "parent_id": parent_id}),
        }
    }

    pub fn from_json(value: Option<&Value>) -> Option<RetryFit> {
        let value = value?;
        match value.get("kind")?.as_str()? {
            "extend_parents" => Some(RetryFit::ExtendParents),
            "move" => Some(RetryFit::Move { parent_id: value.get("parent_id").and_then(Value::as_str).map(str::to_string) }),
            _ => None,
        }
    }
}

fn when(moment: &Option<Moment>) -> Value {
    moment.map(|m| json!(py::pydantic_datetime(&m))).unwrap_or(Value::Null)
}

fn moment_of(value: Option<&Value>) -> Option<Moment> {
    value.and_then(Value::as_str).and_then(|t| parse_iso(t, None).ok())
}

fn status_of(value: Option<&Value>) -> Option<GoalStatus> {
    value.and_then(|s| serde_json::from_value(s.clone()).ok())
}

fn strings(value: Option<&Value>) -> Vec<String> {
    value.and_then(Value::as_array).map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()).unwrap_or_default()
}

impl GoalChange {
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("goal_id".into(), json!(self.goal_id));
        out.insert("status".into(), json!(self.status));
        out.insert("at".into(), when(&self.at));
        if !self.note_ids.is_empty() {
            out.insert("note_ids".into(), json!(self.note_ids));
        }
        if let Some(deadline) = &self.retry_deadline {
            out.insert("retry_deadline".into(), json!(deadline));
        }
        if let Some(fit) = &self.retry_fit {
            out.insert("retry_fit".into(), fit.to_json());
        }
        Value::Object(out)
    }

    pub fn from_json(value: &Value) -> Option<GoalChange> {
        Some(GoalChange {
            goal_id: value.get("goal_id")?.as_str()?.to_string(),
            status: status_of(value.get("status"))?,
            at: moment_of(value.get("at")),
            note_ids: strings(value.get("note_ids")),
            retry_deadline: value.get("retry_deadline").and_then(Value::as_str).map(str::to_string),
            retry_fit: RetryFit::from_json(value.get("retry_fit")),
        })
    }

    /// The changes a revision keeps (its `claude_goal_changes`).
    pub fn all_from(value: Option<&Value>) -> Vec<GoalChange> {
        value.and_then(Value::as_array).into_iter().flatten().filter_map(GoalChange::from_json).collect()
    }

    pub fn all_to_json(changes: &[GoalChange]) -> Option<Value> {
        if changes.is_empty() {
            None
        } else {
            Some(Value::Array(changes.iter().map(GoalChange::to_json).collect()))
        }
    }

    /// It, with `at` filled in: `through` when it gives none.
    pub fn resolved(&self, through: Moment) -> GoalChange {
        GoalChange { at: Some(self.at.unwrap_or(through)), ..self.clone() }
    }

    /// Its time (resolved).
    pub fn at(&self) -> Moment {
        self.at.expect("a goal change kept has its time")
    }
}

/// The user's say on a goal change (amend_proposal's `goal_changes`):
/// `apply` it now, `drop` it, or `change` it -- their status, time, retry
/// deadline and how it's made to fit over Claude's, or a change of their
/// own for a goal the proposal doesn't change.
#[derive(Debug, Clone)]
pub struct GoalChangeChoice {
    pub goal_id: String,
    pub use_: String,
    pub status: Option<GoalStatus>,
    pub at: Option<Moment>,
    pub retry_deadline: Option<String>,
    pub retry_fit: Option<RetryFit>,
}

impl GoalChangeChoice {
    pub fn from_value(value: &Value) -> GoalChangeChoice {
        let text = |key: &str| value.get(key).and_then(Value::as_str).map(str::to_string);
        GoalChangeChoice {
            goal_id: text("goal_id").unwrap_or_default(),
            use_: text("use").unwrap_or_default(),
            status: status_of(value.get("status")),
            at: moment_of(value.get("at")),
            retry_deadline: text("retry_deadline"),
            retry_fit: RetryFit::from_json(value.get("retry_fit")),
        }
    }
}

/// A goal change the user settled, as the ledger keeps it: applied,
/// dropped, or changed to their `status`, `at`, `retry_deadline` and
/// `retry_fit`.
#[derive(Debug, Clone, PartialEq)]
pub struct GoalChangeSettled {
    pub goal_id: String,
    pub use_: String,
    pub status: Option<GoalStatus>,
    pub at: Option<Moment>,
    pub retry_deadline: Option<String>,
    pub retry_fit: Option<RetryFit>,
}

impl GoalChangeSettled {
    /// From a `goal_change` ledger entry (`goal_id` from its event_id
    /// column).
    pub fn from_edit(goal_id: &str, edit: &Map<String, Value>) -> GoalChangeSettled {
        GoalChangeSettled {
            goal_id: goal_id.to_string(),
            use_: edit.get("use").and_then(Value::as_str).unwrap_or_default().into(),
            status: status_of(edit.get("status")),
            at: moment_of(edit.get("at")),
            retry_deadline: edit.get("retry_deadline").and_then(Value::as_str).map(str::to_string),
            retry_fit: RetryFit::from_json(edit.get("retry_fit")),
        }
    }

    /// The ledger entry recording it.
    pub fn edit(&self) -> Map<String, Value> {
        let mut out = Map::new();
        out.insert("action".into(), json!("goal_change"));
        out.insert("use".into(), json!(self.use_));
        if let Some(status) = self.status {
            out.insert("status".into(), json!(status));
        }
        if let Some(at) = self.at {
            out.insert("at".into(), json!(py::isoformat(&at)));
        }
        if let Some(deadline) = &self.retry_deadline {
            out.insert("retry_deadline".into(), json!(deadline));
        }
        if let Some(fit) = &self.retry_fit {
            out.insert("retry_fit".into(), fit.to_json());
        }
        out
    }

    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("goal_id".into(), json!(self.goal_id));
        out.insert("use".into(), json!(self.use_));
        out.insert("status".into(), json!(self.status));
        out.insert("at".into(), when(&self.at));
        out.insert("retry_deadline".into(), json!(self.retry_deadline));
        if let Some(fit) = &self.retry_fit {
            out.insert("retry_fit".into(), fit.to_json());
        }
        Value::Object(out)
    }

    /// The change it keeps in the proposal (`change`): the user's fields
    /// over `under`'s, Claude's, if there is one.
    pub fn change(&self, under: Option<&GoalChange>) -> Option<GoalChange> {
        let status = self.status.or(under.map(|c| c.status))?;
        Some(GoalChange {
            goal_id: self.goal_id.clone(),
            status,
            at: self.at.or(under.and_then(|c| c.at)),
            note_ids: under.map(|c| c.note_ids.clone()).unwrap_or_default(),
            // A status other than missed takes no retry deadline.
            retry_deadline: if status == GoalStatus::Missed { self.retry_deadline.clone().or(under.and_then(|c| c.retry_deadline.clone())) } else { None },
            // How it's made to fit goes with the deadline it's for: a new
            // deadline comes with its own, or none.
            retry_fit: match status {
                GoalStatus::Missed if self.retry_deadline.is_some() => self.retry_fit.clone(),
                GoalStatus::Missed => self.retry_fit.clone().or(under.and_then(|c| c.retry_fit.clone())),
                _ => None,
            },
        })
    }
}

/// Claude's changes, less those for goals the user settled.
pub fn unsettled(claude: &[GoalChange], settled: &Dict<GoalChangeSettled>) -> Vec<GoalChange> {
    claude.iter().filter(|c| !settled.contains(&c.goal_id)).cloned().collect()
}

/// The changes a revision makes, each with who decided it: Claude's,
/// less those the user applied or dropped, the user's own fields over
/// Claude's where they changed one, then the user's changes of goals
/// Claude didn't change.
pub fn effective(claude: &[GoalChange], settled: &Dict<GoalChangeSettled>) -> Vec<(GoalChange, &'static str)> {
    let mut out: Vec<(GoalChange, &'static str)> = Vec::new();
    for change in claude {
        match settled.get(&change.goal_id) {
            None => out.push((change.clone(), "claude")),
            Some(theirs) if theirs.use_ == "change" => {
                if let Some(changed) = theirs.change(Some(change)) {
                    out.push((changed, "user"));
                }
            }
            Some(_) => {}
        }
    }
    for theirs in settled.values() {
        if theirs.use_ == "change" && !claude.iter().any(|c| c.goal_id == theirs.goal_id) {
            if let Some(changed) = theirs.change(None) {
                out.push((changed, "user"));
            }
        }
    }
    out
}

fn label(goal: &Goal) -> String {
    format!("goal {} ({})", goal.id.as_deref().unwrap_or_default(), py::repr(goal.title.as_deref().unwrap_or_default()))
}

/// `goal` after `change`: its history gaining the change at its time,
/// stamped with the deadline in force; a retry gaining missed (the old
/// deadline) then the prior status (the new one), with the new deadline
/// as its own.
fn changed(goal: &Goal, change: &GoalChange) -> Goal {
    let at = crate::contract::DateTimeText(py::pydantic_datetime(&change.at()));
    let mut after = goal.clone();
    let history = after.history.get_or_insert_with(Vec::new);
    match (&change.retry_deadline, goal.status) {
        (Some(deadline), Some(prior)) if change.status == GoalStatus::Missed => {
            history.push(StatusChange { status: GoalStatus::Missed, at: at.clone(), deadline: goal.deadline.clone(), note: None });
            history.push(StatusChange { status: prior, at, deadline: Some(deadline.clone()), note: None });
            after.deadline = Some(deadline.clone());
            // A deadline of its own unties it from what it was measured
            // from.
            after.deadline_rule = None;
        }
        _ => {
            history.push(StatusChange { status: change.status, at, deadline: goal.deadline.clone(), note: None });
            after.status = Some(change.status);
        }
    }
    after
}

/// `goal`, an open goal above a retried one, due by `deadline` too. If
/// it's active and its own deadline has passed, that's a miss, recorded
/// as a retry's is -- dated `now`, since it's now it's found out, and no
/// earlier than its last change, so its history stays in order. Otherwise
/// just its deadline moves.
fn extended(goal: &Goal, deadline: &str, overdue: bool, now: Moment) -> Goal {
    let mut after = goal.clone();
    after.deadline = Some(deadline.to_string());
    after.deadline_rule = None;
    if overdue && goal.status == Some(GoalStatus::Active) {
        let history = after.history.get_or_insert_with(Vec::new);
        let last = history.last().and_then(|entry| parse_iso(&entry.at.0, None).ok());
        let at = crate::contract::DateTimeText(py::pydantic_datetime(&last.filter(|l| *l > now).unwrap_or(now)));
        history.push(StatusChange { status: GoalStatus::Missed, at: at.clone(), deadline: goal.deadline.clone(), note: None });
        history.push(StatusChange { status: GoalStatus::Active, at, deadline: Some(deadline.to_string()), note: None });
    }
    after
}

/// The open goals above `goal` due before `deadline`, each due by it too.
fn parents_extended(goal: &Goal, deadline: &str, all: &[Goal], zone: &Tz) -> Vec<Goal> {
    let Some(new_due) = goals::deadline_end(deadline) else { return Vec::new() };
    let now = crate::determinism::now();
    let today = now.with_timezone(zone).naive_local();
    let mut out = Vec::new();
    let mut seen = vec![goal.id.as_deref()];
    let mut at = goals::parent_of(all, goal);
    while let Some(parent) = at {
        if seen.contains(&parent.id.as_deref()) {
            break;
        }
        seen.push(parent.id.as_deref());
        at = goals::parent_of(all, parent);
        let due = parent.deadline.as_deref().and_then(goals::deadline_end);
        if parent.status.is_some_and(|s| s.is_closed()) || !due.is_some_and(|d| d < new_due) {
            continue;
        }
        out.push(extended(parent, deadline, due.is_some_and(|d| d <= today), now));
    }
    out
}

/// Every goal `change` changes, as it leaves it: its own first, then any
/// its `retry_fit` extends.
fn goals_after(goal: &Goal, change: &GoalChange, all: &[Goal], zone: &Tz) -> Vec<Goal> {
    let mut after = changed(goal, change);
    let others = match (&change.retry_fit, &change.retry_deadline) {
        (Some(RetryFit::ExtendParents), Some(deadline)) => parents_extended(goal, deadline, all, zone),
        (Some(RetryFit::Move { parent_id }), Some(_)) => {
            after.parent_id = parent_id.clone();
            Vec::new()
        }
        _ => Vec::new(),
    };
    let mut out = vec![after];
    out.extend(others);
    out
}

/// `before` made `after`, as update_goals takes it: its status, new
/// deadline and parent and its whole history, made to the version read.
fn edit_of(before: &Goal, after: Goal) -> GoalEdit {
    let moved = after.parent_id != before.parent_id;
    let clear_fields = if moved && after.parent_id.is_none() { vec!["parent_id".to_string()] } else { Vec::new() };
    GoalEdit {
        goal: Goal {
            id: before.id.clone(),
            status: after.status,
            history: after.history,
            deadline: if after.deadline != before.deadline { after.deadline } else { None },
            parent_id: if moved { after.parent_id } else { None },
            version: Some(before.version.unwrap_or(0)),
            ..Default::default()
        },
        clear_fields,
        status_note: None,
    }
}

/// `change` as update_goals takes it: an edit of each goal it changes.
fn edits_for(goal: &Goal, change: &GoalChange, all: &[Goal], zone: &Tz) -> Vec<GoalEdit> {
    goals_after(goal, change, all, zone)
        .into_iter()
        .filter_map(|after| {
            let before = all.iter().find(|g| g.id == after.id)?;
            Some(edit_of(before, after))
        })
        .collect()
}

/// Everything wrong with `changes` as the goals are now: a goal named
/// twice, a status that isn't done, missed or obsolete, a goal that
/// isn't there or isn't open, a time outside the window (`window`:
/// its start to `through`), a retry deadline on a change that isn't
/// missed, a retry_fit without a retry deadline, a note that isn't one of `notes`, and anything the goal
/// rules refuse once they're all made (open sub-goals, say), naming the
/// goals in the way.
pub fn problems(changes: &[GoalChange], all: &[Goal], zone: &Tz, window: (Moment, Moment), notes: &[String]) -> Vec<String> {
    let mut problems: Vec<String> = Vec::new();
    let mut seen: Vec<&String> = Vec::new();
    let mut after: Vec<Goal> = all.to_vec();
    let mut involved: Vec<String> = Vec::new();
    for change in changes {
        let id = change.goal_id.trim();
        if seen.contains(&&change.goal_id) {
            problems.push(format!("goal_changes: goal {} is given more than once", py::repr(id)));
            continue;
        }
        seen.push(&change.goal_id);
        let Some(index) = after.iter().position(|g| g.id.as_deref() == Some(id)) else {
            problems.push(format!(
                "goal_changes: {}",
                goals::find(all, id).err().map(|e| e.to_string()).unwrap_or_else(|| format!("there's no goal {}", py::repr(id)))
            ));
            continue;
        };
        let goal = after[index].clone();
        let who = label(&goal);
        let mut wrong = false;
        if !change.status.is_closed() {
            problems.push(format!("goal_changes: {who}: a goal change marks it done, missed or obsolete, not {}", change.status.text()));
            wrong = true;
        }
        match goal.status {
            Some(status) if status.is_closed() => {
                problems.push(format!("goal_changes: {who} is {} already, not open", status.text()));
                wrong = true;
            }
            _ => {}
        }
        let at = change.at();
        if at < window.0 || at > window.1 {
            problems.push(format!(
                "goal_changes: {who}: its time ({}) is outside the window ({} to {})",
                py::isoformat(&at),
                py::isoformat(&window.0),
                py::isoformat(&window.1)
            ));
            wrong = true;
        }
        if change.retry_deadline.is_some() && change.status != GoalStatus::Missed {
            problems.push(format!("goal_changes: {who}: retry_deadline goes with status missed -- the goal is missed and tried again by then"));
            wrong = true;
        }
        if change.retry_fit.is_some() && change.retry_deadline.is_none() {
            problems.push(format!("goal_changes: {who}: retry_fit goes with a retry_deadline -- it's how the new deadline is made to fit"));
            wrong = true;
        }
        for note in &change.note_ids {
            if !notes.contains(note) {
                problems.push(format!("goal_changes: {who}: {} isn't one of the proposal's notes", py::repr(note)));
            }
        }
        if wrong {
            continue;
        }
        for made in goals_after(&goal, change, &after, zone) {
            involved.extend(made.id.clone());
            if let Some(at) = after.iter().position(|g| g.id == made.id) {
                after[at] = made;
            }
        }
    }
    if !involved.is_empty() {
        problems.extend(
            goal_problems(&after, zone)
                .into_iter()
                .filter(|(ids, _)| ids.iter().any(|i| involved.contains(i)))
                .map(|(_, p)| format!("goal_changes: {p}")),
        );
    }
    problems
}

/// Whether `goal` is already as `change` leaves it: made before, by an
/// apply that stopped before it was recorded, say.
fn already_made(goal: &Goal, change: &GoalChange) -> bool {
    let at = |entry: &StatusChange| parse_iso(&entry.at.0, None).ok() == Some(change.at());
    let history = goal.history.as_deref().unwrap_or_default();
    match (&change.retry_deadline, history) {
        (Some(deadline), [.., missed, retried]) if change.status == GoalStatus::Missed => {
            missed.status == GoalStatus::Missed
                && at(missed)
                && at(retried)
                && Some(retried.status) == goal.status
                && goal.deadline.as_deref() == Some(deadline.as_str())
        }
        (None, [.., last]) => goal.status == Some(change.status) && last.status == change.status && at(last),
        _ => false,
    }
}

/// Make `change` now, as one update_goals call -- refused as it would
/// refuse it -- and the goal as it is after. One made already is left
/// as it is.
pub fn apply(store: &Goals, change: &GoalChange, zone: &Tz) -> anyhow::Result<ListedGoal> {
    let all = store.all()?;
    let goal = goals::find(&all, change.goal_id.trim())?;
    if already_made(&goal, change) {
        return Ok(goals::listed(&goal, &all, zone));
    }
    if let Some(status) = goal.status.filter(|s| s.is_closed()) {
        anyhow::bail!("{} is {} already, not open", label(&goal), status.text());
    }
    if change.retry_deadline.is_some() && change.status != GoalStatus::Missed {
        anyhow::bail!("{}: retry_deadline goes with status missed", label(&goal));
    }
    if change.retry_fit.is_some() && change.retry_deadline.is_none() {
        anyhow::bail!("{}: retry_fit goes with a retry_deadline", label(&goal));
    }
    let made = store.change_goals(&[], &edits_for(&goal, change, &all, zone), &[], zone)?;
    // Its own goal, not one activated on the way.
    made.goals.into_iter().find(|g| g.id == goal.id).ok_or_else(|| anyhow::anyhow!("{} wasn't changed", label(&goal)))
}

/// The changes as a proposal shows them: each with its goal's title and
/// path now, and who decided it.
pub fn shown(changes: &[(GoalChange, &str)], all: &[Goal]) -> Vec<Value> {
    changes
        .iter()
        .map(|(change, by)| {
            let goal = all.iter().find(|g| g.id.as_deref() == Some(change.goal_id.trim()));
            let mut out = json!({
                "goal_id": change.goal_id,
                "title": goal.and_then(|g| g.title.clone()),
                "path": goal.map(|g| goals::path(g, all)).unwrap_or_default(),
                "status": change.status,
                "at": when(&change.at),
                "note_ids": change.note_ids,
                "retry_deadline": change.retry_deadline,
                "decided_by": by,
            });
            if let Some(fit) = &change.retry_fit {
                out["retry_fit"] = fit.to_json();
            }
            out
        })
        .collect()
}
