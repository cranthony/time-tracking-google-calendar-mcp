//! The action, action group and priority color tools.

use super::stores::prefetch_stores;
use super::{from_store, refused, refused_or_conflict, texts, to_row, ToolError};
use crate::app::App;
use crate::contract::{tool, Schema};
use crate::stores::actions::ACTION_ROW;
use crate::stores::groups::GROUP_ROW;
use serde::{Deserialize, Serialize};
use serde_json::json;

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum ActionStatus {
    #[serde(rename = "proposed")]
    Proposed,
    #[serde(rename = "active")]
    Active,
    #[serde(rename = "archived")]
    Archived,
    #[serde(rename = "deleted")]
    Deleted,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum ClearableField {
    #[serde(rename = "group_id")]
    GroupId,
    #[serde(rename = "background_color")]
    BackgroundColor,
    #[serde(rename = "priority")]
    Priority,
    #[serde(rename = "note")]
    Note,
}

/// An action as the action tools return it: plus where it sits, what
/// it inherits from its groups, and whether it holds its label.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ListedAction {
    pub id: Option<String>,
    pub group_id: Option<String>,
    pub name: Option<String>,
    pub status: Option<ActionStatus>,
    pub label_id: Option<String>,
    pub background_color: Option<String>,
    pub priority: Option<i64>,
    pub note: Option<String>,
    pub path: Option<String>,
    pub effective_color: Option<String>,
    pub effective_priority: Option<i64>,
    pub holds_label: Option<bool>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ActionList {
    pub actions: Vec<ListedAction>,
    pub label_slots_used: i64,
    #[schema(default = "200")]
    pub label_slots_total: i64,
}

/// One action: what the user does with their time, a verb phrase (see
/// get_actions).
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct Action {
    pub id: Option<String>,
    pub group_id: Option<String>,
    pub name: Option<String>,
    pub status: Option<ActionStatus>,
    pub label_id: Option<String>,
    pub background_color: Option<String>,
    pub priority: Option<i64>,
    pub note: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct CreatedAction {
    pub changed: Vec<ListedAction>,
    pub label_slots_used: i64,
    #[schema(default = "200")]
    pub label_slots_total: i64,
    pub created_id: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ActionChanges {
    pub changed: Vec<ListedAction>,
    pub label_slots_used: i64,
    #[schema(default = "200")]
    pub label_slots_total: i64,
}

/// A group as the group tools return it: plus where it sits, and what
/// it inherits.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ListedActionGroup {
    pub id: Option<String>,
    pub group_id: Option<String>,
    pub name: Option<String>,
    pub background_color: Option<String>,
    pub priority: Option<i64>,
    pub note: Option<String>,
    pub path: Option<String>,
    pub effective_color: Option<String>,
    pub effective_priority: Option<i64>,
}

/// One action group: a name that rolls actions up (see
/// get_action_groups).
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ActionGroup {
    pub id: Option<String>,
    pub group_id: Option<String>,
    pub name: Option<String>,
    pub background_color: Option<String>,
    pub priority: Option<i64>,
    pub note: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct CreatedActionGroup {
    pub changed: Vec<ListedActionGroup>,
    pub affected_actions: Vec<ListedAction>,
    pub label_slots_used: i64,
    #[schema(default = "200")]
    pub label_slots_total: i64,
    pub created_id: String,
}

/// One priority's color: its label's.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct PriorityColor {
    pub priority: i64,
    pub color: String,
    pub label_id: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct PriorityColorChange {
    pub colors: Vec<PriorityColor>,
    pub affected_actions: Vec<ListedAction>,
    pub label_slots_used: i64,
    #[schema(default = "200")]
    pub label_slots_total: i64,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ActionGroupChanges {
    pub changed: Vec<ListedActionGroup>,
    pub affected_actions: Vec<ListedAction>,
    pub label_slots_used: i64,
    #[schema(default = "200")]
    pub label_slots_total: i64,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct DeletedActionGroup {
    pub changed: Vec<ListedActionGroup>,
    pub affected_actions: Vec<ListedAction>,
    pub label_slots_used: i64,
    #[schema(default = "200")]
    pub label_slots_total: i64,
    pub deleted: ActionGroup,
}

// -- the tools ------------------------------------------------------------------


/// Every action: what the user does with their time, each a verb
/// phrase for what they're doing in a moment ("play guitar", "eat a
/// meal"). By default the proposed and active ones, not archived or
/// deleted ones. Each has its id, group_id (the action group it's in, if
/// any), name, status, label_id, background_color, priority and note,
/// plus its path through its groups ("Creative › Guitar › Play guitar"),
/// its effective_color and effective_priority (its own, or else its
/// nearest group's) and whether it holds_label: active actions always hold one of the calendar's event
/// labels, and proposed ones do while there's room. Also how many of the
/// calendar's label slots are in use. Read-only.
#[tool]
pub fn get_actions(app: &App, statuses: Option<Vec<ActionStatus>>) -> Result<ActionList, ToolError> {
    prefetch_stores(app)?;
    let statuses = statuses.map(|s| texts(&s));
    from_store(app.actions()?.get_actions(statuses.as_deref()).map_err(refused_or_conflict)?)
}

/// One action, by its id or else its name (ignoring case), as
/// get_actions lists it. If there's none, the error suggests close
/// matches. Read-only.
#[tool]
pub fn get_action(app: &App, id_or_name: String) -> Result<ListedAction, ToolError> {
    prefetch_stores(app)?;
    from_store(app.actions()?.get_action(&id_or_name).map_err(refused_or_conflict)?)
}

/// Create an action: a name (a verb phrase for what the user is doing,
/// e.g. "practice guitar"; at most 50 characters, and unique among all
/// actions, whatever their status), and optionally a group_id (an action
/// group, see get_action_groups), a background_color (hex; inherited
/// from its groups when unset, or else derived from priority), a priority
/// (taken by its events that don't set their own; inherited from its
/// groups when unset) and a note. id and
/// label_id are assigned. status is "proposed" unless given: leave it so
/// when you're adding an action no existing one matched, so the user can
/// review it; give "active" when the user asked for it. Returns the new
/// action's id as created_id, and, as changed, the new action (plus any
/// proposed action that lost its label to make room), with
/// label_slots_used of label_slots_total.
#[tool(writes)]
pub fn create_action(app: &App, action: Action) -> Result<CreatedAction, ToolError> {
    prefetch_stores(app)?;
    from_store(app.actions()?.create_action(&to_row(ACTION_ROW, &action)).map_err(refused_or_conflict)?)
}

/// Update an action by id: its group_id, name (still unique),
/// status, background_color, priority or note. Omitted properties keep
/// their value; list one in clear_fields to blank it instead. label_id
/// can't be changed. status is proposed (not yet reviewed by the user;
/// holds a label while there's room), active (holds a label), archived
/// (not done any more) or deleted (shouldn't have existed); archiving or
/// deleting frees its label, and making it active again restores it.
/// Returns, as changed, the action as updated, plus any proposed action
/// that gained or lost its label as a result, with label_slots_used of
/// label_slots_total.
#[tool(writes)]
pub fn update_action(app: &App, action: Action, clear_fields: Option<Vec<ClearableField>>) -> Result<ActionChanges, ToolError> {
    prefetch_stores(app)?;
    let clear = texts(&clear_fields.unwrap_or_default());
    from_store(app.actions()?.update_action(&to_row(ACTION_ROW, &action), &clear).map_err(refused_or_conflict)?)
}

/// Every action group: a name that rolls actions up ("Creative",
/// "Guitar"), for targets and finding one's way around. A group isn't an
/// action -- no event can be tagged with one -- and groups can be inside
/// other groups (group_id), so actions are always the leaves. Enclosing
/// groups come before the ones inside them. Each has its id, group_id,
/// name, background_color, priority and note, plus its path ("Creative ›
/// Guitar") and its effective_color and effective_priority (its own, or
/// else its nearest enclosing group's), which its actions and groups
/// inherit when they don't set their own. Read-only.
#[tool]
pub fn get_action_groups(app: &App) -> Result<Vec<ListedActionGroup>, ToolError> {
    prefetch_stores(app)?;
    from_store(app.actions()?.get_action_groups().map_err(refused)?)
}

/// One action group, by its id or else its name (ignoring case), as
/// get_action_groups lists it. If there's none, the error suggests close
/// matches. Read-only.
#[tool]
pub fn get_action_group(app: &App, id_or_name: String) -> Result<ListedActionGroup, ToolError> {
    prefetch_stores(app)?;
    from_store(app.actions()?.get_action_group(&id_or_name).map_err(refused)?)
}

/// Create an action group: a name (unique among groups, though an
/// action may share it; at most 50 characters), and optionally a group_id
/// (the group it's inside), a background_color, a priority and a note,
/// which its actions and groups inherit when they don't set their own.
/// id is assigned. Put actions in it with update_action's group_id.
/// Returns the new group's id as created_id, and as changed, the group
/// with its path and what it inherits.
#[tool(writes)]
pub fn create_action_group(app: &App, group: ActionGroup) -> Result<CreatedActionGroup, ToolError> {
    prefetch_stores(app)?;
    from_store(app.actions()?.create_action_group(&to_row(GROUP_ROW, &group)).map_err(refused_or_conflict)?)
}

/// Each priority's color, 0 to 3: an event with no action's label is
/// colored by its priority's label (priority 2's if it has none), and an
/// action or group with no color of its own (nor its groups') takes its
/// priority's. Each has its priority, color (#rrggbb) and label_id.
/// Read-only.
#[tool]
pub fn get_priority_colors(app: &App) -> Result<Vec<PriorityColor>, ToolError> {
    prefetch_stores(app)?;
    from_store(app.actions()?.priority_colors().map_err(refused)?)
}

/// Change a priority's color (priority 0 to 3; color as #rrggbb): its
/// label's, so events colored by their priority change with it, and every
/// action's label that takes its color from that priority. Returns every
/// priority's color as colors, and as affected_actions, the actions
/// whose effective_color changed.
#[tool(writes)]
pub fn update_priority_color(app: &App, priority: i64, color: String) -> Result<PriorityColorChange, ToolError> {
    prefetch_stores(app)?;
    from_store(app.actions()?.set_priority_color(&json!(priority), &json!(color)).map_err(refused_or_conflict)?)
}

/// Update an action group by id: its group_id (move it inside another
/// group; clear it to make it top-level), name (still unique among
/// groups), background_color, priority or note. Omitted properties keep
/// their value; list one in clear_fields to blank it instead. Its actions
/// that don't set their own color follow its new one on the calendar.
/// Returns, as changed, the group as updated, and as affected_actions,
/// every action whose path, effective_color or effective_priority
/// changed as a result.
#[tool(writes)]
pub fn update_action_group(app: &App, group: ActionGroup, clear_fields: Option<Vec<ClearableField>>) -> Result<ActionGroupChanges, ToolError> {
    prefetch_stores(app)?;
    let clear = texts(&clear_fields.unwrap_or_default());
    from_store(app.actions()?.update_action_group(&to_row(GROUP_ROW, &group), &clear).map_err(refused_or_conflict)?)
}

/// Delete an action group, by id. Its actions and the groups inside it
/// aren't deleted: they move up into the group it was inside (or to the
/// top level, if it was top-level). Returns the group as it was
/// (deleted), as changed, the groups moved up out of it, and as
/// affected_actions, every action moved or recolored as a result.
#[tool(writes)]
pub fn delete_action_group(app: &App, group_id: String) -> Result<DeletedActionGroup, ToolError> {
    prefetch_stores(app)?;
    from_store(app.actions()?.delete_action_group(&group_id).map_err(refused_or_conflict)?)
}
