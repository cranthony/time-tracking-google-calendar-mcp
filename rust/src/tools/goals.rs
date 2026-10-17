//! The goal tools: get_goals, get_goal, create_goal and update_goals.

use super::{refused, texts, ToolError};
use crate::app::App;
use crate::contract::{tool, Schema};
pub use crate::stores::goals::{ChangedGoals, CreatedGoal, Goal, GoalStatus, ListedGoal};
use crate::stores::goals::{GoalEdit, GoalRemoval};
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum GoalField {
    #[serde(rename = "parent_id")]
    ParentId,
    #[serde(rename = "required")]
    Required,
    #[serde(rename = "expected_minutes")]
    ExpectedMinutes,
    #[serde(rename = "deadline")]
    Deadline,
    #[serde(rename = "description")]
    Description,
    #[serde(rename = "definition_of_done")]
    DefinitionOfDone,
    #[serde(rename = "attribution_hints")]
    AttributionHints,
}

/// One goal's update: `goal` (its id and the fields to set) and the
/// fields to remove (`clear_fields`).
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct GoalUpdate {
    pub goal: Goal,
    pub clear_fields: Option<Vec<GoalField>>,
}

/// A goal to delete. Give if_version -- the version you last read: without it, a goal that has a version is refused; changed since, it's refused -- read it again and retry.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct GoalDelete {
    pub goal_id: String,
    pub if_version: Option<i64>,
}

// -- the tools ------------------------------------------------------------------

/// The user's goals: outcomes they mean to reach and can mark done (not
/// ongoing practices -- those are habits). By default the inactive and
/// active ones, not done or obsolete ones; every depth, each with its
/// parent_id and path (its parents' titles, outermost first). A goal
/// can sit under another to any depth: a sub-goal is a piece of reaching
/// its parent, and required says whether its parent needs it done.
/// Each has a title, an optional expected_minutes (how long it's
/// expected to take: a guide for comparing with the time spent, never
/// enforced), a deadline ("2026-11-01", due at the end of that day, or
/// "2026-11-01T17:00", in the calendar's time zone; needed while
/// active), a status (inactive, active, done or obsolete) and its
/// history (each change of status and when, oldest first; started_at is
/// when it first became active, closed_at when it was done or made
/// obsolete), a description and definition_of_done (HTML), and
/// attribution_hints (a guide for telling which events are for it).
/// Events are tagged with goals, apart from their actions (see
/// list_events' goals). Read-only.
#[tool]
pub fn get_goals(app: &App, statuses: Option<Vec<GoalStatus>>) -> Result<Vec<ListedGoal>, ToolError> {
    let statuses = statuses.map(|s| texts(&s));
    app.goals()?.get_goals(statuses.as_deref()).map_err(refused)
}

/// One goal, by id or else by title (ignoring case), as get_goals lists
/// it, whatever its status. If there's none, the error suggests close
/// matches. Read-only.
#[tool]
pub fn get_goal(app: &App, id_or_title: String) -> Result<ListedGoal, ToolError> {
    app.goals()?.get_goal(&id_or_title).map_err(refused)
}

/// Add a goal (see get_goals): a title, and optionally parent_id (the
/// goal it's a piece of), required, expected_minutes, deadline,
/// description, definition_of_done and attribution_hints. Active unless
/// given a status; an active goal needs a deadline. Its history starts
/// with its status, now, unless given one (to backdate it, say). A
/// sub-goal can't be due after its parent, or be expected to take
/// longer; it can't be active under an inactive parent, or open
/// (inactive or active) under a done or obsolete one. id is assigned
/// unless given (a client's own, so it can name the goal before it's
/// made; the same create sent again gives the goal as it was made).
/// Returns the goal and its id as created_id.
#[tool(writes)]
pub fn create_goal(app: &App, goal: Goal) -> Result<CreatedGoal, ToolError> {
    let zone = app.calendar_client()?.get_time_zone()?;
    app.goals()?.create_goal(&goal, &zone).map_err(refused)
}

/// Change goals, all at once, checked whole: `updates` (each a goal's id
/// and the fields to set -- left out, a field keeps its value; list it in
/// that update's clear_fields to remove it instead), `creates` (new goals,
/// as create_goal takes them; an update can name one by an id given
/// here) and `deletes`. A status set without a history adds the change,
/// now, to the goal's history; a history given replaces it whole (to
/// backdate a change, or remove a mistaken one), and must end with the
/// goal's status.
///
/// The rules (see get_goals and create_goal) hold for the result, not
/// for each step: to mark a goal done or obsolete, close or move its
/// open sub-goals in the same call; to delete one, delete or move its
/// sub-goals too; to make an earlier deadline fit, change its sub-goals'
/// in the same call. Nothing is moved or changed to make things fit: a
/// call that breaks a rule changes nothing, and the error names the goals
/// in the way. Deleting a goal can't be undone; events tagged with it
/// stop showing it. Give each update its version, and each delete its if_version -- the versions you last read (get_goals gives them): without, a goal that has a version is refused; changed since, the call is refused with every goal that's changed, as it is now (_meta.conflicts) -- read it again and retry. Returns each goal made or
/// changed, as it is now, and the ids deleted.
#[tool(writes)]
pub fn update_goals(app: &App, updates: Option<Vec<GoalUpdate>>, creates: Option<Vec<Goal>>, deletes: Option<Vec<GoalDelete>>) -> Result<ChangedGoals, ToolError> {
    let zone = app.calendar_client()?.get_time_zone()?;
    let edits: Vec<GoalEdit> = updates
        .unwrap_or_default()
        .into_iter()
        .map(|u| GoalEdit { goal: u.goal, clear_fields: texts(&u.clear_fields.unwrap_or_default()) })
        .collect();
    let removals: Vec<GoalRemoval> =
        deletes.unwrap_or_default().into_iter().map(|d| GoalRemoval { goal_id: d.goal_id, if_version: d.if_version }).collect();
    app.goals()?.change_goals(&creates.unwrap_or_default(), &edits, &removals, &zone).map_err(refused)
}

/// Update one goal by id: the fields given are set, those left out kept,
/// and those in clear_fields removed -- update_goals for one goal (see
/// there for status and history). Give version -- the version you last read: without it, a goal that has a version is refused; changed since, it's refused -- read it again and retry. Returns the goal as updated.
#[tool(writes)]
pub fn update_goal(app: &App, goal: Goal, clear_fields: Option<Vec<GoalField>>) -> Result<ListedGoal, ToolError> {
    let id = goal.id.clone().unwrap_or_default();
    let changed = update_goals(app, Some(vec![GoalUpdate { goal, clear_fields }]), None, None)?;
    changed.goals.into_iter().find(|g| g.id.as_deref() == Some(id.trim())).ok_or_else(|| ToolError::Message(format!("goal {id} wasn't updated")))
}

/// Delete one goal by id -- update_goals for one goal: refused while it
/// has sub-goals (delete or move them first). Can't be undone; events
/// tagged with it stop showing it. Given if_version -- the version last
/// seen -- it's refused if it's changed since; gone already, it's done.
/// Returns the ids deleted.
#[tool(writes)]
pub fn delete_goal(app: &App, goal_id: String, if_version: Option<i64>) -> Result<ChangedGoals, ToolError> {
    update_goals(app, None, None, Some(vec![GoalDelete { goal_id, if_version }]))
}
