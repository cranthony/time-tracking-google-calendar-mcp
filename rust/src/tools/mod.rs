//! The tools, as server.py defines them: each takes its checked arguments
//! (see mcp::schema) and returns its result as the JSON pydantic would
//! give -- fields in declaration order -- or a ToolError, which the client
//! sees as "Error executing tool <name>: <message>".

mod places;

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

pub fn record_timing(app: &Arc<App>, name: &str, work_ms: i64, ok: bool) {
    app.health.record_call(name, work_ms, None, ok);
}
