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
    #[serde(rename = "priority")]
    Priority,
    #[serde(rename = "sort_order")]
    SortOrder,
    #[serde(rename = "expected_minutes")]
    ExpectedMinutes,
    #[serde(rename = "deadline")]
    Deadline,
    #[serde(rename = "deadline_offset_minutes")]
    DeadlineOffsetMinutes,
    #[serde(rename = "description")]
    Description,
    #[serde(rename = "definition_of_done")]
    DefinitionOfDone,
    #[serde(rename = "attribution_hints")]
    AttributionHints,
    #[serde(rename = "with_ids")]
    WithIds,
    #[serde(rename = "for_ids")]
    ForIds,
}

/// One goal's update: `goal` (its id and the fields to set) and the
/// fields to remove (`clear_fields`).
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct GoalUpdate {
    pub goal: Goal,
    pub clear_fields: Option<Vec<GoalField>>,
}

/// A goal to delete. Give if_version -- the version you last read: without it, it's refused; changed since, it's refused -- on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct GoalDelete {
    pub goal_id: String,
    pub if_version: Option<i64>,
}

// -- the tools ------------------------------------------------------------------

/// The user's goals: outcomes they mean to reach and can mark done (not
/// ongoing practices -- those are habits). By default the inactive and
/// active ones, not done, missed or obsolete ones; every depth, each with its
/// parent_id and path (its parents' titles, outermost first). A goal
/// can sit under another to any depth: a sub-goal is a piece of reaching
/// its parent, and required says whether its parent needs it done.
/// A priority (0 the most, as actions'; optional) says how much it
/// matters: without one, a goal takes its nearest parent's that has one,
/// and with none up its tree it's treated as 3. sort_order (optional)
/// is the user's own order among goals that otherwise list alike (the
/// same priority, due the same day): the lowest first, those without one
/// after them; leave it as it is unless asked to reorder goals.
/// Each has a title, an optional expected_minutes (how long it's
/// expected to take: a guide for comparing with the time spent, never
/// enforced), a deadline ("2026-11-01", due at the end of that day, or
/// "2026-11-01T17:00", in the calendar's time zone; needed while
/// active) -- a sub-goal's can follow its parent's: with
/// deadline_offset_minutes (0 for the same; 1440 a day before) it's kept
/// that far before the parent's while it's open, and setting a deadline
/// without it, or moving it to the top level, unties it -- a status (inactive, active, done, missed -- its
/// deadline passed without it -- or obsolete) and its history (each change of
/// status and when, oldest first, each with the deadline in force at
/// that change, if it had one; started_at is when it first became
/// active, closed_at when it was done, missed or made obsolete). A missed
/// goal can be reopened with a new deadline, and its history keeps each
/// deadline it missed, so a goal can be missed and retried any number
/// of times. Then a description and definition_of_done (HTML), and
/// attribution_hints (a guide for telling which events are for it).
/// with_ids are the people it's done with (who the user works on it
/// alongside) and for_ids those it's done for, who aren't part of doing
/// it (a promise to them, a gift, a favor): person ids from get_people,
/// never "self", and no one in both.
/// Events are tagged with goals, apart from their actions (see
/// list_events' goals): tag an event with a goal only when the goal is
/// open (inactive or active), has attribution_hints, and the event
/// clearly matches them -- never guess for a goal without hints -- and
/// with the most specific goal that fits (a sub-goal rather than its
/// parent). Read-only.
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
/// goal it's a piece of), required, priority, expected_minutes, deadline,
/// description, definition_of_done, attribution_hints, with_ids and
/// for_ids. Active unless
/// given a status; an active goal needs a deadline. Its history starts
/// with its status, now, unless given one (to backdate it, say). A
/// sub-goal can't be due after its parent, or be expected to take
/// longer; it can't be active under an inactive parent, or open
/// (inactive or active) under a done, missed or obsolete one. id is assigned
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
/// now, with the goal's deadline after the change, to the goal's history;
/// a history given replaces it whole (to backdate a change, or remove a
/// mistaken one), its entries' deadlines as given, and must end with the
/// goal's status. To retry a missed goal, set its new deadline and give
/// its history ending missed (with the deadline it missed) then active or
/// inactive (with the new one), both at the same moment -- how the app
/// records a retry.
///
/// The rules (see get_goals and create_goal) hold for the result, not
/// for each step: to mark a goal done, missed or obsolete, close or move its
/// open sub-goals in the same call; to delete one, delete or move its
/// sub-goals too; to make an earlier deadline fit, change its sub-goals'
/// in the same call. Nothing is moved or changed to make things fit: a
/// call that breaks a rule changes nothing, and the error names the goals
/// in the way. Deleting a goal can't be undone; events tagged with it
/// stop showing it. Give each update its version, and each delete its if_version -- the versions you last read (get_goals gives them): without, it's refused; changed since, the call is refused with every goal that's changed, as it is now (_meta.conflicts) -- on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK. Returns each goal made or
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
/// there for status and history). Give version -- the version you last read: without it, it's refused; changed since, it's refused -- on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK. Returns the goal as updated.
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
