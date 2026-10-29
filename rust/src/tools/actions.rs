//! The action, action group and priority color tools.

use super::stores::prefetch_stores;
use super::{refused, refused_or_conflict, texts, ToolError};
use crate::app::App;
use crate::contract::tool;
pub use crate::stores::actions::*;

// -- the tools ------------------------------------------------------------------


/// Every action: what the user does with their time, each a verb
/// phrase for what they're doing in a moment ("play guitar", "eat a
/// meal"). By default the proposed and active ones, not archived or
/// deleted ones. Each has its id, group_id (the action group it's in, if
/// any), name, status, label_id, background_color, priority, note and
/// commute (true for a way of getting from one place to another, such as
/// "drive" or "walk", which a travel time can be by), plus its path through its groups ("Creative › Guitar › Play guitar"),
/// its effective_color and effective_priority (its own, or else its
/// nearest group's), its chosen_color (its own background_color, or else
/// its nearest group's; null when its color is its priority's) and whether it holds_label: active actions always hold one of the calendar's event
/// labels, and proposed ones do while there's room. Also how many of the
/// calendar's label slots are in use. Read-only.
#[tool]
pub fn get_actions(app: &App, statuses: Option<Vec<ActionStatus>>) -> Result<ActionList, ToolError> {
    prefetch_stores(app)?;
    let statuses = statuses.map(|s| texts(&s));
    app.actions()?.get_actions(statuses.as_deref()).map_err(refused_or_conflict)
}

/// One action, by its id or else its name (ignoring case), as
/// get_actions lists it. If there's none, the error suggests close
/// matches. Read-only.
#[tool]
pub fn get_action(app: &App, id_or_name: String) -> Result<ListedAction, ToolError> {
    prefetch_stores(app)?;
    app.actions()?.get_action(&id_or_name).map_err(refused_or_conflict)
}

/// Create an action: a name (a verb phrase for what the user is doing,
/// e.g. "practice guitar"; at most 50 characters, and unique among all
/// actions, whatever their status), and optionally a group_id (an action
/// group, see get_action_groups), a background_color (hex; inherited
/// from its groups when unset, or else derived from priority), a priority
/// (taken by its events that don't set their own; inherited from its
/// groups when unset), a note and commute (true if it's a way of
/// getting from one place to another, e.g. "drive"). id and
/// label_id are assigned. status is "proposed" unless given: leave it so
/// when you're adding an action no existing one matched, so the user can
/// review it; give "active" when the user asked for it. Returns the new
/// action's id as created_id, and, as changed, the new action (plus any
/// proposed action that lost its label to make room), with
/// label_slots_used of label_slots_total.
#[tool(writes)]
pub fn create_action(app: &App, action: Action) -> Result<CreatedAction, ToolError> {
    prefetch_stores(app)?;
    app.actions()?.create_action(&action).map_err(refused_or_conflict)
}

/// Update an action by id: its group_id, name (still unique),
/// status, background_color, priority, note or commute. Omitted
/// properties keep their value; list one in clear_fields to blank it instead. label_id
/// can't be changed. status is proposed (not yet reviewed by the user;
/// holds a label while there's room), active (holds a label), archived
/// (not done any more) or deleted (shouldn't have existed); archiving or
/// deleting frees its label, and making it active again restores it.
/// Give version -- the version you last read (the get tools give it): without it, it's refused; changed since, it's refused, with it as it is now in _meta.conflicts -- on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK. Returns, as changed, the action as updated, plus any proposed
/// action that gained or lost its label as a result, with label_slots_used
/// of label_slots_total.
#[tool(writes)]
pub fn update_action(app: &App, action: Action, clear_fields: Option<Vec<ClearableField>>) -> Result<ActionChanges, ToolError> {
    prefetch_stores(app)?;
    let clear = texts(&clear_fields.unwrap_or_default());
    app.actions()?.update_action(&action, &clear).map_err(refused_or_conflict)
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
    app.actions()?.get_action_groups().map_err(refused)
}

/// One action group, by its id or else its name (ignoring case), as
/// get_action_groups lists it. If there's none, the error suggests close
/// matches. Read-only.
#[tool]
pub fn get_action_group(app: &App, id_or_name: String) -> Result<ListedActionGroup, ToolError> {
    prefetch_stores(app)?;
    app.actions()?.get_action_group(&id_or_name).map_err(refused)
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
    app.actions()?.create_action_group(&group).map_err(refused_or_conflict)
}

/// Each priority's color, 0 to 3: an event with no action's label is
/// colored by its priority's label (priority 2's if it has none), and an
/// action or group with no color of its own (nor its groups') takes its
/// priority's. Each has its priority, color (#rrggbb) and label_id.
/// Read-only.
#[tool]
pub fn get_priority_colors(app: &App) -> Result<Vec<PriorityColor>, ToolError> {
    prefetch_stores(app)?;
    app.actions()?.priority_colors().map_err(refused)
}

/// Change a priority's color (priority 0 to 3; color as #rrggbb): its
/// label's, so events colored by their priority change with it, and every
/// action's label that takes its color from that priority. Returns every
/// priority's color as colors, and as affected_actions, the actions
/// whose effective_color changed.
#[tool(writes)]
pub fn update_priority_color(app: &App, priority: i64, color: String) -> Result<PriorityColorChange, ToolError> {
    prefetch_stores(app)?;
    app.actions()?.set_priority_color(priority, &color).map_err(refused_or_conflict)
}

/// Update an action group by id: its group_id (move it inside another
/// group; clear it to make it top-level), name (still unique among
/// groups), background_color, priority or note. Omitted properties keep
/// their value; list one in clear_fields to blank it instead. Its actions
/// that don't set their own color follow its new one on the calendar.
/// Give version -- the version you last read (the get tools give it): without it, it's refused; changed since, it's refused, with it as it is now in _meta.conflicts -- on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK. Returns, as changed, the group as updated, and as affected_actions,
/// every action whose path, effective_color or effective_priority
/// changed as a result.
#[tool(writes)]
pub fn update_action_group(app: &App, group: ActionGroup, clear_fields: Option<Vec<ClearableField>>) -> Result<ActionGroupChanges, ToolError> {
    prefetch_stores(app)?;
    let clear = texts(&clear_fields.unwrap_or_default());
    app.actions()?.update_action_group(&group, &clear).map_err(refused_or_conflict)
}

/// Delete an action group, by id. Its actions and the groups inside it
/// aren't deleted: they move up into the group it was inside (or to the
/// top level, if it was top-level). Returns the group as it was
/// (deleted), as changed, the groups moved up out of it, and as
/// affected_actions, every action moved or recolored as a result. Give if_version -- the version you last read: without it, it's refused; changed since, it's refused, with it as it is now -- on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK; gone already, it's done.
#[tool(writes)]
pub fn delete_action_group(app: &App, group_id: String, if_version: Option<i64>) -> Result<DeletedActionGroup, ToolError> {
    prefetch_stores(app)?;
    app.actions()?.delete_action_group(&group_id, if_version).map_err(refused_or_conflict)
}
