//! The tools: each takes its checked arguments (see mcp::schema) and
//! returns its result as the JSON pydantic would give -- fields in
//! declaration order -- or a ToolError, which the client sees as "Error
//! executing tool <name>: <message>".

mod actions;
mod compaction;
mod events;
mod goals;
mod health;
mod people;
mod places;
mod recurrences;
mod setup;
mod stores;

use crate::app::App;
use crate::google::calendar::{is_label_conflict, is_time_zone_not_set};
use crate::write_lock::WRITE_LOCK;
use serde_json::{Map, Value};
use std::sync::Arc;

pub struct Outcome(pub Value);

pub enum ToolError {
    /// A refusal the client should read: its message is shown.
    Message(String),
    /// A write refused because what it changes isn't the version the
    /// client last saw: its message is shown, and each item that's
    /// changed, as it is now, is given (in the result's `_meta`), for the
    /// client to resolve.
    Conflict(crate::sheet::rows::Conflicts),
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
/// ToolError(str(exc))` does. Errors from Google stay unexpected; a
/// write to a version that isn't the one kept is a conflict.
pub fn refused(error: anyhow::Error) -> ToolError {
    if let Some(conflicts) = crate::sheet::rows::conflicts_of(&error) {
        return ToolError::Conflict(conflicts);
    }
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

/// Literals (such as clear_fields') as their text.
pub fn texts<T: serde::Serialize>(items: &[T]) -> Vec<String> {
    items.iter().map(crate::contract::wire_text).collect()
}

/// Every tool, in the order tools/list sends them.
pub fn registry() -> &'static [crate::contract::ToolDef] {
    static TOOLS: std::sync::LazyLock<Vec<crate::contract::ToolDef>> = std::sync::LazyLock::new(|| {
        vec![
            events::list_events_tool(),
            events::get_event_tool(),
            events::update_event_tool(),
            recurrences::get_recurrence_tool(),
            recurrences::update_recurrence_tool(),
            recurrences::split_recurrence_tool(),
            recurrences::delete_recurrence_tool(),
            events::create_event_tool(),
            events::delete_event_tool(),
            people::get_traits_tool(),
            people::create_trait_tool(),
            people::update_trait_tool(),
            actions::get_actions_tool(),
            actions::get_action_tool(),
            actions::create_action_tool(),
            actions::update_action_tool(),
            actions::get_action_groups_tool(),
            actions::get_action_group_tool(),
            actions::create_action_group_tool(),
            actions::get_priority_colors_tool(),
            actions::update_priority_color_tool(),
            actions::update_action_group_tool(),
            actions::delete_action_group_tool(),
            people::get_people_tool(),
            people::get_person_tool(),
            people::create_person_tool(),
            people::update_person_tool(),
            people::get_circles_tool(),
            people::get_circle_tool(),
            people::create_circle_tool(),
            people::update_circle_tool(),
            people::delete_circle_tool(),
            places::get_locations_tool(),
            places::get_location_tool(),
            places::create_location_tool(),
            places::update_location_tool(),
            places::delete_location_tool(),
            places::get_location_groups_tool(),
            places::get_location_group_tool(),
            places::create_location_group_tool(),
            places::update_location_group_tool(),
            places::delete_location_group_tool(),
            places::get_travel_times_tool(),
            places::create_travel_time_tool(),
            places::update_travel_time_tool(),
            places::delete_travel_time_tool(),
            places::estimate_travel_time_tool(),
            people::get_habits_tool(),
            people::get_habit_tool(),
            people::create_habit_tool(),
            people::update_habit_tool(),
            goals::get_goals_tool(),
            goals::get_goal_tool(),
            goals::create_goal_tool(),
            goals::update_goal_tool(),
            goals::delete_goal_tool(),
            goals::update_goals_tool(),
            people::get_scoring_defaults_tool(),
            people::set_scoring_defaults_tool(),
            compaction::get_compaction_status_tool(),
            events::note_tool(),
            events::get_notes_tool(),
            compaction::edit_note_tool(),
            compaction::delete_note_tool(),
            compaction::prepare_compaction_tool(),
            compaction::compact_notes_tool(),
            compaction::get_proposal_tool(),
            compaction::amend_proposal_tool(),
            compaction::add_proposal_note_tool(),
            compaction::withdraw_proposal_note_tool(),
            compaction::confirm_proposal_tool(),
            compaction::finish_proposal_tool(),
            compaction::prepare_judgments_tool(),
            compaction::prepare_habit_judgments_tool(),
            compaction::record_judgments_tool(),
            compaction::abandon_compaction_tool(),
            events::get_compaction_schedule_hints_tool(),
            events::set_compaction_schedule_hints_tool(),
            events::set_time_zone_tool(),
            health::get_health_tool(),
            setup::google_setup_link_tool(),
        ]
    });
    &TOOLS
}

/// Every tool's name, and whether it may write.
pub fn listed() -> Vec<(&'static str, bool)> {
    registry().iter().map(|t| (t.name, t.writes)).collect()
}

/// Run `name` with `arguments`: holding WRITE_LOCK if it writes (and
/// marking what's kept of the calendar stale, so its first read of it
/// syncs, and it writes on what's there now), telling the model to set a time zone if the
/// calendar has none. A write sent again -- its answer lost on the way --
/// is made once by what it names and the version it was made from (see
/// rows::Versioned), not by anything kept of the call.
pub fn run(app: &Arc<App>, name: &str, arguments: Value) -> Result<Outcome, ToolError> {
    let arguments = match arguments {
        Value::Object(map) => map,
        _ => Map::new(),
    };
    let Some(tool) = registry().iter().find(|t| t.name == name) else {
        return Err(ToolError::Unexpected(anyhow::anyhow!("no tool is called {name}")));
    };
    if app.sheets_refresh() == crate::app::Refresh::EveryCall {
        app.refresh_sheets(true);
    }
    let _held = tool.writes.then(|| WRITE_LOCK.hold());
    if app.calendar_sync() == crate::app::Refresh::EveryCall {
        app.sync_calendar();
    } else if tool.writes {
        app.calendar_stale();
    }
    let written = crate::sheet::rows::Written::start();
    // A write says which version of each item it changes it last saw.
    let _versions = tool.writes.then(crate::sheet::rows::VersionsRequired::start);
    let outcome = (tool.run)(app, Value::Object(arguments));
    app.warn_of_outside_changes();
    let outcome = outcome.map(|mut value| {
        written.stamp(&mut value);
        value
    });
    match outcome {
        Ok(value) => Ok(Outcome(value)),
        Err(ToolError::Unexpected(error)) if crate::sheet::rows::conflicts_of(&error).is_some() => {
            Err(ToolError::Conflict(crate::sheet::rows::conflicts_of(&error).expect("checked above")))
        }
        Err(ToolError::Unexpected(error)) if error.downcast_ref::<crate::sheet::rows::CellError>().is_some() => {
            Err(ToolError::Message(error.to_string()))
        }
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
    let mut named: Vec<&str> = listed.iter().filter_map(|p| p.get("action").and_then(Value::as_str)).collect();
    for part in &listed {
        for exception in part.get("exceptions").and_then(Value::as_array).into_iter().flatten() {
            named.extend(exception.get("actions").and_then(Value::as_array).into_iter().flatten().filter_map(Value::as_str));
        }
    }
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
