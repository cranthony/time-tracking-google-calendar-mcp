//! The tools, as server.py defines them: each takes its checked arguments
//! (see mcp::schema) and returns its result as the JSON pydantic would
//! give -- fields in declaration order -- or a ToolError, which the client
//! sees as "Error executing tool <name>: <message>".

mod events;
mod places;
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

fn registry() -> &'static [Tool] {
    &[
        Tool { name: "get_locations", writes: false, run: places::get_locations },
        Tool { name: "get_location", writes: false, run: places::get_location },
        Tool { name: "create_location", writes: true, run: places::create_location },
        Tool { name: "update_location", writes: true, run: places::update_location },
        Tool { name: "delete_location", writes: true, run: places::delete_location },
        Tool { name: "get_actions", writes: false, run: stores::get_actions },
        Tool { name: "get_action", writes: false, run: stores::get_action },
        Tool { name: "create_action", writes: true, run: stores::create_action },
        Tool { name: "update_action", writes: true, run: stores::update_action },
        Tool { name: "get_action_groups", writes: false, run: stores::get_action_groups },
        Tool { name: "get_action_group", writes: false, run: stores::get_action_group },
        Tool { name: "create_action_group", writes: true, run: stores::create_action_group },
        Tool { name: "update_action_group", writes: true, run: stores::update_action_group },
        Tool { name: "delete_action_group", writes: true, run: stores::delete_action_group },
        Tool { name: "get_priority_colors", writes: false, run: stores::get_priority_colors },
        Tool { name: "update_priority_color", writes: true, run: stores::update_priority_color },
        Tool { name: "get_people", writes: false, run: stores::get_people },
        Tool { name: "get_person", writes: false, run: stores::get_person },
        Tool { name: "create_person", writes: true, run: stores::create_person },
        Tool { name: "update_person", writes: true, run: stores::update_person },
        Tool { name: "get_circles", writes: false, run: stores::get_circles },
        Tool { name: "get_circle", writes: false, run: stores::get_circle },
        Tool { name: "create_circle", writes: true, run: stores::create_circle },
        Tool { name: "update_circle", writes: true, run: stores::update_circle },
        Tool { name: "delete_circle", writes: true, run: stores::delete_circle },
        Tool { name: "get_traits", writes: false, run: stores::get_traits },
        Tool { name: "create_trait", writes: true, run: stores::create_trait },
        Tool { name: "update_trait", writes: true, run: stores::update_trait },
        Tool { name: "get_habits", writes: false, run: stores::get_habits },
        Tool { name: "get_habit", writes: false, run: stores::get_habit },
        Tool { name: "create_habit", writes: true, run: stores::create_habit },
        Tool { name: "update_habit", writes: true, run: stores::update_habit },
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
    ]
}

/// Run `name` with `arguments`: inside one call's read caches, holding
/// WRITE_LOCK if it writes, telling the model to set a time zone if the
/// calendar has none.
pub fn run(app: &Arc<App>, name: &str, arguments: Value) -> Result<Outcome, ToolError> {
    let Some(tool) = registry().iter().find(|t| t.name == name) else {
        return Err(ToolError::Unexpected(anyhow::anyhow!("{name} isn't ported yet")));
    };
    let arguments = match arguments {
        Value::Object(map) => map,
        _ => Map::new(),
    };
    let _held = tool.writes.then(|| WRITE_LOCK.hold());
    let _reads = CachedReads::start();
    let _listings = CachedListings::start();
    match (tool.run)(app, &arguments) {
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

pub fn record_timing(app: &Arc<App>, name: &str, work_ms: i64, ok: bool) {
    app.health.record_call(name, work_ms, None, ok);
}
