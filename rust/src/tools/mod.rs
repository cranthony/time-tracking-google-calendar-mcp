//! The tools: each takes its checked arguments (see mcp::schema) and
//! returns its result as the JSON pydantic would give -- fields in
//! declaration order -- or a ToolError, which the client sees as "Error
//! executing tool <name>: <message>".

mod actions;
mod compaction;
mod events;
mod health;
mod people;
mod places;
mod recurrences;
mod stores;

use crate::app::App;
use crate::google::calendar::{is_label_conflict, is_time_zone_not_set};
use crate::google::sheets::CachedReads;
use crate::google::calendar::CachedListings;
use crate::write_lock::WRITE_LOCK;
use serde_json::{Map, Value};
use std::sync::Arc;

pub struct Outcome(pub Value);

pub enum ToolError {
    /// A refusal the client should read: its message is shown.
    Message(String),
    /// Anything else: the client sees only that the tool failed.
    Unexpected(anyhow::Error),
}

pub type ToolResult = Result<Value, ToolError>;

impl From<anyhow::Error> for ToolError {
    fn from(error: anyhow::Error) -> ToolError {
        ToolError::Unexpected(error)
    }
}

/// A store's ValueError (anything but a failure talking to Google),
/// shown to the client -- what a tool's `except ValueError as exc: raise
/// ToolError(str(exc))` does. Errors from Google stay unexpected.
pub fn refused(error: anyhow::Error) -> ToolError {
    if error.downcast_ref::<crate::google::GoogleError>().is_some() {
        ToolError::Unexpected(error)
    } else {
        ToolError::Message(error.to_string())
    }
}

/// Like `refused`, also showing a lost race on the calendar's labels
/// (EventLabelConflictError), as the action tools do.
pub fn refused_or_conflict(error: anyhow::Error) -> ToolError {
    if is_label_conflict(&error) {
        ToolError::Message(error.to_string())
    } else {
        refused(error)
    }
}

/// A store's result, as the tool's result type.
pub fn from_store<T: serde::de::DeserializeOwned>(value: Value) -> Result<T, ToolError> {
    serde_json::from_value(value).map_err(|e| ToolError::Unexpected(anyhow::anyhow!("the store's result doesn't fit the tool's: {e}")))
}

/// A typed argument, as the store's row of `row_type`.
pub fn to_row<T: serde::Serialize>(row_type: crate::sheet::rows::RowType, value: &T) -> crate::sheet::rows::Row {
    row_type.from_value(&serde_json::to_value(value).expect("an argument is JSON"))
}

/// Literals (such as clear_fields') as their text.
pub fn texts<T: serde::Serialize>(items: &[T]) -> Vec<String> {
    items.iter().map(crate::contract::wire_text).collect()
}

pub fn args_str<'a>(args: &'a Map<String, Value>, name: &str) -> &'a str {
    args.get(name).and_then(Value::as_str).unwrap_or_default()
}

pub fn args_strings(args: &Map<String, Value>, name: &str) -> Vec<String> {
    args.get(name)
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(|i| i.as_str().map(str::to_string)).collect())
        .unwrap_or_default()
}

struct Tool {
    name: &'static str,
    writes: bool,
    run: fn(&App, &Map<String, Value>) -> ToolResult,
}

/// The tools with typed arguments and results (see `contract`).
pub fn typed() -> &'static [crate::contract::ToolDef] {
    static TYPED: std::sync::LazyLock<Vec<crate::contract::ToolDef>> = std::sync::LazyLock::new(|| {
        vec![
            places::get_locations_tool(),
            places::get_location_tool(),
            places::create_location_tool(),
            places::update_location_tool(),
            places::delete_location_tool(),
            actions::get_actions_tool(),
            actions::get_action_tool(),
            actions::create_action_tool(),
            actions::update_action_tool(),
            actions::get_action_groups_tool(),
            actions::get_action_group_tool(),
            actions::create_action_group_tool(),
            actions::update_action_group_tool(),
            actions::delete_action_group_tool(),
            actions::get_priority_colors_tool(),
            actions::update_priority_color_tool(),
            people::get_people_tool(),
            people::get_person_tool(),
            people::create_person_tool(),
            people::update_person_tool(),
            people::get_circles_tool(),
            people::get_circle_tool(),
            people::create_circle_tool(),
            people::update_circle_tool(),
            people::delete_circle_tool(),
            people::get_traits_tool(),
            people::create_trait_tool(),
            people::update_trait_tool(),
            people::get_habits_tool(),
            people::get_habit_tool(),
            people::create_habit_tool(),
            people::update_habit_tool(),
        ]
    });
    &TYPED
}

fn registry() -> &'static [Tool] {
    &[
        Tool { name: "list_events", writes: false, run: events::list_events },
        Tool { name: "get_event", writes: false, run: events::get_event },
        Tool { name: "create_event", writes: true, run: events::create_event },
        Tool { name: "update_event", writes: true, run: events::update_event },
        Tool { name: "delete_event", writes: true, run: events::delete_event },
        Tool { name: "set_time_zone", writes: true, run: events::set_time_zone },
        Tool { name: "get_compaction_schedule_hints", writes: false, run: events::get_compaction_schedule_hints },
        Tool { name: "set_compaction_schedule_hints", writes: true, run: events::set_compaction_schedule_hints },
        Tool { name: "note", writes: true, run: events::note },
        Tool { name: "get_notes", writes: false, run: events::get_notes },
        Tool { name: "get_recurrence", writes: false, run: recurrences::get_recurrence },
        Tool { name: "update_recurrence", writes: true, run: recurrences::update_recurrence },
        Tool { name: "split_recurrence", writes: true, run: recurrences::split_recurrence },
        Tool { name: "delete_recurrence", writes: true, run: recurrences::delete_recurrence },
        Tool { name: "get_health", writes: false, run: health::get_health },
        Tool { name: "get_compaction_status", writes: false, run: compaction::get_compaction_status },
        Tool { name: "edit_note", writes: true, run: compaction::edit_note },
        Tool { name: "delete_note", writes: true, run: compaction::delete_note },
        Tool { name: "prepare_compaction", writes: true, run: compaction::prepare_compaction },
        Tool { name: "compact_notes", writes: true, run: compaction::compact_notes },
        Tool { name: "get_proposal", writes: false, run: compaction::get_proposal },
        Tool { name: "amend_proposal", writes: true, run: compaction::amend_proposal },
        Tool { name: "add_proposal_note", writes: true, run: compaction::add_proposal_note },
        Tool { name: "withdraw_proposal_note", writes: true, run: compaction::withdraw_proposal_note },
        Tool { name: "confirm_proposal", writes: true, run: compaction::confirm_proposal },
        Tool { name: "finish_proposal", writes: true, run: compaction::finish_proposal },
        Tool { name: "prepare_judgments", writes: false, run: compaction::prepare_judgments },
        Tool { name: "prepare_habit_judgments", writes: false, run: compaction::prepare_habit_judgments },
        Tool { name: "record_judgments", writes: true, run: compaction::record_judgments },
        Tool { name: "abandon_compaction", writes: true, run: compaction::abandon_compaction },
    ]
}

/// Every tool's name, and whether it may write: typed or not.
pub fn listed() -> Vec<(&'static str, bool)> {
    typed().iter().map(|t| (t.name, t.writes)).chain(registry().iter().map(|t| (t.name, t.writes))).collect()
}

/// Run `name` with `arguments`: inside one call's read caches, holding
/// WRITE_LOCK if it writes, telling the model to set a time zone if the
/// calendar has none.
pub fn run(app: &Arc<App>, name: &str, arguments: Value) -> Result<Outcome, ToolError> {
    let arguments = match arguments {
        Value::Object(map) => map,
        _ => Map::new(),
    };
    let typed = typed().iter().find(|t| t.name == name);
    let untyped = registry().iter().find(|t| t.name == name);
    let writes = match (typed, untyped) {
        (Some(tool), _) => tool.writes,
        (None, Some(tool)) => tool.writes,
        (None, None) => return Err(ToolError::Unexpected(anyhow::anyhow!("{name} isn't ported yet"))),
    };
    let _held = writes.then(|| WRITE_LOCK.hold());
    let _reads = CachedReads::start();
    let _listings = CachedListings::start();
    let outcome = match (typed, untyped) {
        (Some(tool), _) => (tool.run)(app, Value::Object(arguments)),
        (None, Some(tool)) => (tool.run)(app, &arguments),
        (None, None) => unreachable!(),
    };
    match outcome {
        Ok(value) => Ok(Outcome(value)),
        Err(ToolError::Unexpected(error)) if is_time_zone_not_set(&error) => Err(ToolError::Message(format!(
            "The calendar has no time zone set, so its days and times can't be put in the user's local time. Call set_time_zone with the user's IANA time zone (e.g. \"America/New_York\"), asking them for it if you don't know it, then call {name} again."
        ))),
        Err(other) => Err(other),
    }
}

/// Refuse trait parts whose "action" isn't an action (not a deleted one)
/// or a group -- a trait's own list, or a person's or habit's
/// {trait id: [parts]}.
pub fn check_part_actions(app: &App, parts: Option<&Value>) -> Result<(), ToolError> {
    let listed: Vec<&Value> = match parts {
        Some(Value::Object(map)) => map.values().filter_map(Value::as_array).flatten().collect(),
        Some(Value::Array(items)) => items.iter().collect(),
        _ => Vec::new(),
    };
    let named: Vec<&str> = listed.iter().filter_map(|p| p.get("action").and_then(Value::as_str)).collect();
    if named.is_empty() {
        return Ok(());
    }
    let actions = app.actions()?;
    let scopes = crate::stores::habits::action_scopes(&actions.all()?, &actions.groups()?);
    let unknown: Vec<String> =
        named.iter().filter(|a| !scopes.iter().any(|(k, _)| k == *a)).map(|a| crate::py::repr(a)).collect();
    if !unknown.is_empty() {
        return Err(ToolError::Message(format!(
            "A part's action {} isn't an action or action group (get_actions and get_action_groups list them)",
            unknown.join(", ")
        )));
    }
    Ok(())
}

#[cfg(test)]
#[path = "tests/mod.rs"]
mod tests;
