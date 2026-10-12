//! The recurring-series tools (server.py's get_recurrence,
//! update_recurrence, split_recurrence, delete_recurrence).

use super::events::{check_action_ids, check_facts, moment};
use super::stores::prefetch_stores;
use super::{args_str, ToolError, ToolResult};
use crate::app::App;
use crate::event::Event;
use crate::facts::Facts;
use crate::py;
use crate::stores::action_calendar::fill_in_from_actions;
use crate::stores::actions::ActionTree;
use crate::stores::recurrences::{describe_rules, is_refusal, Repeat};
use crate::zoned;
use chrono_tz::Tz;
use serde_json::{json, Map, Value};

/// A series as the recurrence tools return it (PublicRecurrence).
fn public_recurrence(event: &Event, tree: &ActionTree) -> Value {
    let zone: Option<Tz> = event.time_zone.as_deref().filter(|z| !z.is_empty()).and_then(|z| z.parse().ok());
    let names = event.action_ids.as_ref().map(|ids| ids.iter().map(|i| tree.name(i)).collect::<Vec<_>>());
    let shown = |m: Option<crate::event::Moment>| m.map(|m| match &zone {
        Some(zone) => py::pydantic_datetime(&zoned::in_zone(&m, zone)),
        None => py::pydantic_datetime(&m),
    });
    let repeat = Repeat::from_rules(event.recurrence.as_deref(), zone.as_ref().unwrap_or(&Tz::UTC));
    let mut out = Map::new();
    out.insert("id".into(), json!(event.id));
    out.insert("summary".into(), json!(event.summary));
    out.insert("start".into(), json!(shown(event.start)));
    out.insert("end".into(), json!(shown(event.end)));
    out.insert("time_zone".into(), json!(event.time_zone));
    out.insert("repeat".into(), repeat.map(|r| r.to_json()).unwrap_or(Value::Null));
    out.insert("schedule".into(), json!(describe_rules(event.recurrence.as_deref(), zone.as_ref())));
    out.insert("description".into(), json!(event.description));
    out.insert("location".into(), json!(event.location));
    out.insert("priority".into(), json!(event.priority));
    out.insert("action_ids".into(), json!(event.action_ids));
    out.insert("action_names".into(), json!(names));
    out.insert("actions_from_label".into(), json!(event.actions_from_label));
    out.insert("event_label_id".into(), json!(event.event_label_id));
    out.insert("effective_priority".into(), json!(event.effective_priority()));
    out.insert("facts".into(), event.facts.as_ref().map(Facts::to_public).unwrap_or(Value::Null));
    Value::Object(out)
}

fn public_recurrences(app: &App, events: &[Event]) -> anyhow::Result<Value> {
    let tree = app.actions()?.tree()?;
    Ok(Value::Array(fill_in_from_actions(events, &tree).iter().map(|e| public_recurrence(e, &tree)).collect()))
}

fn shown_refusal(error: anyhow::Error) -> ToolError {
    if is_refusal(&error) {
        ToolError::Message(error.to_string())
    } else {
        ToolError::Unexpected(error)
    }
}

pub fn get_recurrence(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_stores(app)?;
    let series = app.recurrences()?.series(args_str(args, "id")).map_err(shown_refusal)?;
    Ok(public_recurrences(app, &[series])?.as_array().unwrap()[0].clone())
}

/// `PublicRecurrence.to_event(clear_fields)`.
fn to_event(value: &Value, clear_fields: &[String]) -> Result<Event, ToolError> {
    let text = |key: &str| value.get(key).and_then(Value::as_str).map(str::to_string);
    let from_label = value.get("actions_from_label").and_then(Value::as_bool).unwrap_or(false);
    let event = Event {
        id: text("id"),
        summary: text("summary"),
        start: moment(value.get("start"))?,
        end: moment(value.get("end"))?,
        description: text("description"),
        location: text("location"),
        priority: value.get("priority").and_then(Value::as_i64),
        action_ids: if from_label {
            None
        } else {
            value.get("action_ids").and_then(Value::as_array).map(|a| a.iter().filter_map(|i| i.as_str().map(str::to_string)).collect())
        },
        facts: value.get("facts").filter(|f| !f.is_null()).map(|f| Facts::from_public(f).normalized()),
        cleared: clear_fields.iter().cloned().collect(),
        ..Default::default()
    };
    event.check_cleared().map_err(|e| ToolError::Message(e.to_string()))?;
    Ok(event)
}

pub fn update_recurrence(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_stores(app)?;
    let recurrence = &args["recurrence"];
    check_action_ids(app, recurrence, true)?;
    check_facts(app, recurrence)?;
    let clear: Vec<String> = super::args_strings(args, "clear_fields");
    let changes = to_event(recurrence, &clear)?;
    let repeat = match recurrence.get("repeat").filter(|r| !r.is_null()) {
        Some(r) => Some(Repeat::from_value(r)?),
        None => None,
    };
    let starting_at = args.get("starting_at_event_id").and_then(Value::as_str);
    let updated = app.recurrences()?.update(&changes, starting_at, repeat.as_ref()).map_err(shown_refusal)?;
    Ok(public_recurrences(app, &updated)?)
}

pub fn split_recurrence(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_stores(app)?;
    let (earlier, later) = app.recurrences()?.split(args_str(args, "event_id")).map_err(shown_refusal)?;
    let mut events = vec![later];
    events.extend(earlier);
    Ok(public_recurrences(app, &events)?)
}

pub fn delete_recurrence(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_stores(app)?;
    let starting_at = args.get("starting_at_event_id").and_then(Value::as_str);
    let left = app.recurrences()?.delete(args_str(args, "id"), starting_at).map_err(shown_refusal)?;
    Ok(public_recurrences(app, &left.into_iter().collect::<Vec<_>>())?)
}
