//! The event tools (list_events, get_event, create_event, update_event,
//! delete_event), the calendar's time zone, schedule hints and time notes.

use super::stores::{prefetch, prefetch_stores};
use super::{args_str, args_strings, refused, ToolError, ToolResult};
use crate::app::App;
use crate::event::{parse_iso, Event, Moment};
use crate::facts::{fact_problems, Facts};
use crate::py;
use crate::stores::action_calendar::{fill_in_from_actions, ActionCalendar};
use crate::stores::actions::ActionTree;
use crate::stores::event_changes::{self, Cancel, EventChanges, Shift};
use crate::stores::groups::python_repr;
use crate::stores::notes::NotedTime;
use crate::stores::schedule::HINT_ROW;
use serde_json::{json, Map, Value};

// -- PublicEvent -----------------------------------------------------------------

/// An event as every tool returns it (PublicEvent), fields in order.
pub fn public_event(event: &Event, tree: Option<&ActionTree>) -> Value {
    let names = match (tree, &event.action_ids) {
        (Some(tree), Some(ids)) => Some(ids.iter().map(|i| tree.name(i)).collect::<Vec<_>>()),
        _ => None,
    };
    let mut out = Map::new();
    out.insert("id".into(), json!(event.id));
    out.insert("summary".into(), json!(event.summary));
    out.insert("start".into(), json!(event.start.map(|s| py::pydantic_datetime(&s))));
    out.insert("end".into(), json!(event.end.map(|s| py::pydantic_datetime(&s))));
    out.insert("description".into(), json!(event.description));
    out.insert("location".into(), json!(event.location));
    out.insert("priority".into(), json!(event.priority));
    out.insert("action_ids".into(), json!(event.action_ids));
    out.insert("action_names".into(), json!(names));
    out.insert("actions_from_label".into(), json!(event.actions_from_label));
    out.insert("event_label_id".into(), json!(event.event_label_id));
    out.insert("is_cancelled".into(), json!(event.is_cancelled()));
    out.insert("effective_priority".into(), json!(event.effective_priority()));
    out.insert("is_end_of_day_sleep".into(), json!(event.is_end_of_day_sleep));
    out.insert("recurring_event_id".into(), json!(event.recurring_event_id));
    out.insert("facts".into(), event.facts.as_ref().map(Facts::to_public).unwrap_or(Value::Null));
    out.insert("judgments".into(), event.judgments.clone().map(Value::Object).unwrap_or(Value::Null));
    Value::Object(out)
}

/// `_public_events`: with action names and priorities filled in.
pub fn public_events(app: &App, events: &[Event]) -> anyhow::Result<Value> {
    let tree = app.actions()?.tree()?;
    Ok(Value::Array(fill_in_from_actions(events, &tree).iter().map(|e| public_event(e, Some(&tree))).collect()))
}

pub fn moment(value: Option<&Value>) -> anyhow::Result<Option<Moment>> {
    match value.and_then(Value::as_str) {
        Some(text) => Ok(Some(parse_iso(text, None)?)),
        None => Ok(None),
    }
}

/// `PublicEvent.to_event(clear_fields)`.
pub fn to_event(value: &Value, clear_fields: &[String]) -> Result<Event, ToolError> {
    let text = |key: &str| value.get(key).and_then(Value::as_str).map(str::to_string);
    let from_label = value.get("actions_from_label").and_then(Value::as_bool).unwrap_or(false);
    let action_ids = if from_label {
        None
    } else {
        value.get("action_ids").and_then(Value::as_array).map(|ids| ids.iter().filter_map(|i| i.as_str().map(str::to_string)).collect())
    };
    let event = Event {
        id: text("id"),
        summary: text("summary"),
        start: moment(value.get("start"))?,
        end: moment(value.get("end"))?,
        description: text("description"),
        location: text("location"),
        action_ids,
        priority: value.get("priority").and_then(Value::as_i64),
        status: if value.get("is_cancelled").and_then(Value::as_bool).unwrap_or(false) { Some("cancelled".into()) } else { None },
        facts: value.get("facts").filter(|f| !f.is_null()).map(|f| Facts::from_public(f).normalized()),
        judgments: value.get("judgments").and_then(Value::as_object).cloned(),
        cleared: clear_fields.iter().cloned().collect(),
        ..Default::default()
    };
    event.check_cleared().map_err(|e| ToolError::Message(e.to_string()))?;
    Ok(event)
}

/// `_check_action_ids`: action_ids must be actions, not newly deleted ones.
pub fn check_action_ids(app: &App, event: &Value, existing: bool) -> Result<(), ToolError> {
    let ids: Vec<String> = event
        .get("action_ids")
        .and_then(Value::as_array)
        .map(|a| a.iter().filter_map(|i| i.as_str().map(str::to_string)).collect())
        .unwrap_or_default();
    if ids.is_empty() || event.get("actions_from_label").and_then(Value::as_bool).unwrap_or(false) {
        return Ok(());
    }
    let tree = app.actions()?.tree()?;
    let mut already: Vec<String> = Vec::new();
    let id = event.get("id").and_then(Value::as_str).filter(|i| !i.is_empty());
    if existing {
        if let Some(id) = id {
            let any_deleted =
                ids.iter().any(|a| tree.by_id(a).is_some_and(|row| crate::sheet::rows::text(row, "status") == Some("deleted")));
            if any_deleted {
                let current = fill_in_from_actions(&[app.calendar_client()?.get_event(id)?], &tree).remove(0);
                already = current.action_ids.unwrap_or_default();
            }
        }
    }
    tree.check_action_ids(&ids, &already).map_err(|e| ToolError::Message(e.to_string()))
}

/// `_check_facts`: well formed, naming people and a location that exist.
pub fn check_facts(app: &App, event: &Value) -> Result<(), ToolError> {
    let Some(raw) = event.get("facts").filter(|f| !f.is_null()) else { return Ok(()) };
    let facts = Facts::from_public(raw).normalized();
    let problems = fact_problems(&facts);
    if !problems.is_empty() {
        return Err(ToolError::Message(format!("Its facts {}", problems.join("; "))));
    }
    let people: Vec<Value> = app.people()?.all()?.iter().map(|p| p.get("id").cloned().unwrap_or(Value::Null)).collect();
    let unknown: Vec<String> = facts.people().into_iter().filter(|i| !people.contains(&json!(i))).collect();
    if !unknown.is_empty() {
        return Err(ToolError::Message(format!("Its facts name {}, who aren't people (get_people lists them)", py::repr_list(&unknown))));
    }
    if let Some(location) = facts.location() {
        let known: Vec<Value> = app.locations()?.all()?.iter().map(|l| l.get("id").cloned().unwrap_or(Value::Null)).collect();
        if !known.contains(&json!(location)) {
            return Err(ToolError::Message(format!(
                "Its facts' location {} isn't a location (get_locations lists them)",
                py::repr(&location)
            )));
        }
    }
    Ok(())
}

// -- the event tools ---------------------------------------------------------------

pub fn list_events(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_stores(app)?;
    let (min, max) = (moment(args.get("min_time"))?.unwrap(), moment(args.get("max_time"))?.unwrap());
    let events: Vec<Event> = app.calendar_client()?.list_events(&min, &max)?.into_iter().filter(|e| !e.is_cancelled()).collect();
    Ok(public_events(app, &events)?)
}

pub fn get_event(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_stores(app)?;
    let id = args_str(args, "id");
    let event = app.calendar_client()?.get_event(id)?;
    if event.is_cancelled() {
        return Err(ToolError::Message(format!("Event {id} has been cancelled.")));
    }
    Ok(public_events(app, &[event])?.as_array().unwrap()[0].clone())
}

/// `_prefetch_for_changes`: the people's tabs and the journal, in one request.
pub fn prefetch_for_changes(app: &App) -> anyhow::Result<()> {
    let (actions, people, locations, cancellations, journal) =
        (app.actions()?, app.people()?, app.locations()?, app.cancellations()?, app.journal()?);
    let mut ranges = actions.whole_tabs();
    ranges.extend(people.whole_tabs());
    ranges.push(locations.whole_tab());
    ranges.extend(cancellations.whole_tabs());
    ranges.push(journal.whole_tab());
    prefetch(app, ranges)
}

fn list_of<'a>(args: &'a Map<String, Value>, name: &str) -> Vec<&'a Value> {
    args.get(name).and_then(Value::as_array).map(|a| a.iter().collect()).unwrap_or_default()
}

fn cancels(args: &Map<String, Value>) -> Vec<Cancel> {
    list_of(args, "cancels")
        .into_iter()
        .map(|c| Cancel {
            event_id: c.get("event_id").and_then(Value::as_str).unwrap_or_default().to_string(),
            counts_against_follow_through: c.get("counts_against_follow_through").and_then(Value::as_bool).unwrap_or(false),
        })
        .collect()
}

#[allow(clippy::too_many_arguments)]
fn change_events(
    app: &App,
    tool_name: &str,
    updates: Vec<Event>,
    creates: Vec<Event>,
    cancels: Vec<Cancel>,
    shifts: Vec<Shift>,
    allow_compacted: bool,
    dry_run: bool,
) -> ToolResult {
    let changes = EventChanges::new(ActionCalendar::new(app.calendar_client()?, app.actions()?), Some(app.cancellations()?));
    let history_until = app.journal()?.last_stamped_now()?;
    let checked = match changes.check(updates, creates, cancels, shifts, history_until, allow_compacted)? {
        Ok(batch) => batch,
        Err(error) => {
            tracing::warn!(
                "compaction rejected: tool={tool_name} categories={}: {}",
                error.categories.join(","),
                error.message.lines().collect::<Vec<_>>().join(" | ")
            );
            return Err(ToolError::Message(error.message));
        }
    };
    let drawn = event_changes::timeline(&checked);
    let events: Vec<Event> = if dry_run {
        checked
            .changes
            .iter()
            .map(|c| c.after.clone().unwrap_or_else(|| Event { status: Some("cancelled".into()), ..c.before.clone().unwrap() }))
            .collect()
    } else {
        changes.apply(&checked, tool_name)?
    };
    Ok(json!({
        "events": public_events(app, &events)?,
        "timeline": drawn.map(|t| t.text),
        "dry_run": dry_run,
    }))
}

fn flag(args: &Map<String, Value>, name: &str) -> bool {
    args.get(name).and_then(Value::as_bool).unwrap_or(false)
}

pub fn create_event(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_for_changes(app)?;
    let events = list_of(args, "events");
    for event in &events {
        check_action_ids(app, event, false)?;
        check_facts(app, event)?;
    }
    let creates: Vec<Event> = events.iter().map(|e| to_event(e, &[])).collect::<Result<_, _>>()?;
    change_events(app, "create_event", Vec::new(), creates, Vec::new(), Vec::new(), flag(args, "allow_compacted_changes"), flag(args, "dry_run"))
}

pub fn update_event(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_for_changes(app)?;
    let updates = list_of(args, "updates");
    for update in &updates {
        let event = &update["event"];
        if event.get("is_cancelled").and_then(Value::as_bool).unwrap_or(false) {
            return Err(ToolError::Message(
                "an update can't cancel an event: put it in cancels, saying with counts_against_follow_through whether the cancellation counts against follow-through".into(),
            ));
        }
        check_action_ids(app, event, true)?;
        check_facts(app, event)?;
    }
    let creates_raw = list_of(args, "creates");
    for event in &creates_raw {
        check_action_ids(app, event, false)?;
        check_facts(app, event)?;
    }
    let mut patches = Vec::new();
    for update in &updates {
        let clear: Vec<String> = update.get("clear_fields").and_then(Value::as_array).map(|a| a.iter().filter_map(|c| c.as_str().map(str::to_string)).collect()).unwrap_or_default();
        patches.push(to_event(&update["event"], &clear)?);
    }
    let creates: Vec<Event> = creates_raw.iter().map(|e| to_event(e, &[])).collect::<Result<_, _>>()?;
    let shifts: Vec<Shift> = list_of(args, "shifts")
        .into_iter()
        .map(|s| Shift {
            event_ids: s.get("event_ids").and_then(Value::as_array).map(|a| a.iter().filter_map(|i| i.as_str().map(str::to_string)).collect()).unwrap_or_default(),
            minutes: s.get("minutes").and_then(Value::as_i64).unwrap_or(0),
        })
        .collect();
    change_events(app, "update_event", patches, creates, cancels(args), shifts, flag(args, "allow_compacted_changes"), flag(args, "dry_run"))
}

pub fn delete_event(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_for_changes(app)?;
    change_events(app, "delete_event", Vec::new(), Vec::new(), cancels(args), Vec::new(), flag(args, "allow_compacted_changes"), flag(args, "dry_run"))
}

// -- time zone and schedule ----------------------------------------------------------

pub fn set_time_zone(app: &App, args: &Map<String, Value>) -> ToolResult {
    let zone = app.calendar_client()?.set_time_zone(args_str(args, "time_zone")).map_err(refused)?;
    Ok(json!(zone.name()))
}

fn schedule_time_zone(app: &App) -> anyhow::Result<Value> {
    match app.calendar_client()?.get_time_zone() {
        Ok(zone) => Ok(json!(zone.name())),
        Err(error) if crate::google::calendar::is_time_zone_not_set(&error) => Ok(Value::Null),
        Err(error) => Err(error),
    }
}

pub fn get_compaction_schedule_hints(app: &App, _args: &Map<String, Value>) -> ToolResult {
    let hints = app.compaction_schedule()?.all().map_err(refused)?;
    Ok(json!({ "hints": hints, "time_zone": schedule_time_zone(app)? }))
}

pub fn set_compaction_schedule_hints(app: &App, args: &Map<String, Value>) -> ToolResult {
    let given: Vec<crate::sheet::rows::Row> = list_of(args, "hints").into_iter().map(|h| HINT_ROW.from_value(h)).collect();
    let saved = app.compaction_schedule()?.set_hints(&given).map_err(refused)?;
    Ok(json!({ "hints": saved, "time_zone": schedule_time_zone(app)? }))
}

// -- notes -----------------------------------------------------------------------------

pub fn note(app: &App, args: &Map<String, Value>) -> ToolResult {
    let given = &args["noted_time"];
    let recorded = NotedTime {
        timestamp: moment(given.get("timestamp"))?.unwrap(),
        description: given.get("description").and_then(Value::as_str).map(str::to_string),
        compaction_id: None,
    };
    Ok(app.noted_time_sheet()?.append(&recorded)?.with_id())
}

pub fn get_notes(app: &App, args: &Map<String, Value>) -> ToolResult {
    let mut notes = app.noted_time_sheet()?.read_with_rows(flag(args, "include_compacted"))?;
    notes.sort_by(|a, b| a.note.timestamp.cmp(&b.note.timestamp));
    Ok(Value::Array(notes.iter().map(|n| n.with_id()).collect()))
}

#[allow(dead_code)]
fn unused(_: &[String]) -> String {
    python_repr(&Value::Null) + &args_strings(&Map::new(), "x").join("")
}
