//! The tools over the metadata tabs: people and circles, traits, habits --
//! and the prefetches that make a tool read all its tabs in one request.

use super::{args_str, args_strings, refused, ToolResult};
use crate::app::App;
use crate::google::sheets::TabRange;
use crate::sheet::rows::RowType;
use crate::stores::habits::{subject_id, HABIT_ROW};
use crate::stores::people::{CIRCLE_ROW, PERSON_ROW};
use crate::stores::traits::Trait;
use serde_json::{Map, Value};

/// `_prefetch(*tabs)`: every tab's whole range, in one read request.
pub fn prefetch(app: &App, ranges: Vec<TabRange>) -> anyhow::Result<()> {
    app.actions()?.prefetch(&ranges)
}

/// `_prefetch_stores`: the actions', people's and locations' tabs.
pub fn prefetch_stores(app: &App) -> anyhow::Result<()> {
    let (actions, people) = (app.actions()?, app.people()?);
    let locations = app.locations()?;
    let mut ranges = actions.whole_tabs();
    ranges.extend(people.whole_tabs());
    ranges.push(locations.whole_tab());
    actions.prefetch(&ranges)
}

/// `_prefetch_people`: those, and the Cancellations tab (and what it reads).
pub fn prefetch_people(app: &App) -> anyhow::Result<()> {
    let (actions, people, locations, cancellations) = (app.actions()?, app.people()?, app.locations()?, app.cancellations()?);
    let mut ranges = actions.whole_tabs();
    ranges.extend(people.whole_tabs());
    ranges.push(locations.whole_tab());
    ranges.extend(cancellations.whole_tabs());
    actions.prefetch(&ranges)
}

/// `_prefetch_habits`: the habits', actions' and traits' tabs -- and, with
/// `cancellations`, the Cancellations tab and what it reads.
pub fn prefetch_habits(app: &App, cancellations: bool) -> anyhow::Result<()> {
    let (habits, actions, traits) = (app.habits()?, app.actions()?, app.traits()?);
    let mut ranges = vec![habits.whole_tab()];
    ranges.extend(actions.whole_tabs());
    ranges.push(traits.whole_tab());
    if cancellations {
        ranges.extend(app.cancellations()?.whole_tabs());
    }
    habits.prefetch(&ranges)
}

fn row(row_type: RowType, args: &Map<String, Value>, name: &str) -> crate::sheet::rows::Row {
    row_type.from_value(args.get(name).unwrap_or(&Value::Null))
}

fn statuses(args: &Map<String, Value>) -> Option<Vec<String>> {
    args.get("statuses").filter(|s| !s.is_null()).map(|_| args_strings(args, "statuses"))
}

// -- actions and groups --------------------------------------------------------












// -- people and circles ----------------------------------------------------------

/// Each person (or habit, by its subject id) with the events the user
/// cancelled that count against their follow-through.
fn with_cancellations(app: &App, mut items: Vec<Map<String, Value>>, subject: impl Fn(&Map<String, Value>) -> String) -> anyhow::Result<Vec<Value>> {
    let by_person = app.cancellations()?.by_person()?;
    for item in items.iter_mut() {
        let id = subject(item);
        let events = by_person.iter().find(|(k, _)| *k == id).map(|(_, v)| v.clone()).unwrap_or_default();
        item.insert("cancelled_events".into(), Value::Array(events));
    }
    Ok(items.into_iter().map(Value::Object).collect())
}

fn person_id(item: &Map<String, Value>) -> String {
    crate::stores::groups::python_str(item.get("id"))
}

pub fn get_people(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_people(app)?;
    let people = app.people()?.get_people(statuses(args).as_deref()).map_err(refused)?;
    Ok(Value::Array(with_cancellations(app, people, person_id)?))
}

pub fn get_person(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_people(app)?;
    let person = app.people()?.get_person(args_str(args, "id_or_name")).map_err(refused)?;
    Ok(with_cancellations(app, vec![person], person_id)?.remove(0))
}

pub fn create_person(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_stores(app)?;
    let person = row(PERSON_ROW, args, "person");
    super::check_part_actions(app, person.get("traits").and_then(|t| t.get("parts")))?;
    app.people()?.create_person(&person).map_err(refused)
}

pub fn update_person(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_stores(app)?;
    let person = row(PERSON_ROW, args, "person");
    super::check_part_actions(app, person.get("traits").and_then(|t| t.get("parts")))?;
    Ok(Value::Object(app.people()?.update_person(&person, &args_strings(args, "clear_fields")).map_err(refused)?))
}

pub fn get_circles(app: &App, _args: &Map<String, Value>) -> ToolResult {
    prefetch_stores(app)?;
    app.people()?.get_circles().map_err(refused)
}

pub fn get_circle(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_stores(app)?;
    app.people()?.get_circle(args_str(args, "id_or_name")).map_err(refused)
}

pub fn create_circle(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_stores(app)?;
    app.people()?.create_circle(&row(CIRCLE_ROW, args, "circle")).map_err(refused)
}

pub fn update_circle(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_stores(app)?;
    app.people()?.update_circle(&row(CIRCLE_ROW, args, "circle"), &args_strings(args, "clear_fields")).map_err(refused)
}

pub fn delete_circle(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_stores(app)?;
    app.people()?.delete_circle(args_str(args, "circle_id")).map_err(refused)
}

// -- traits ------------------------------------------------------------------------

pub fn get_traits(app: &App, args: &Map<String, Value>) -> ToolResult {
    app.traits()?.get_traits(statuses(args).as_deref()).map_err(refused)
}

pub fn create_trait(app: &App, args: &Map<String, Value>) -> ToolResult {
    let t = Trait::from_value(args.get("trait").unwrap_or(&Value::Null));
    super::check_part_actions(app, t.parts.as_ref())?;
    app.traits()?.create_trait(&t).map_err(refused)
}

pub fn update_trait(app: &App, args: &Map<String, Value>) -> ToolResult {
    let t = Trait::from_value(args.get("trait").unwrap_or(&Value::Null));
    super::check_part_actions(app, t.parts.as_ref())?;
    app.traits()?.update_trait(&t, &args_strings(args, "clear_fields")).map_err(refused)
}

// -- habits --------------------------------------------------------------------------

fn habit_subject(item: &Map<String, Value>) -> String {
    subject_id(&crate::stores::groups::python_str(item.get("id")))
}

pub fn get_habits(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_habits(app, true)?;
    let habits = app.habits()?.get_habits(statuses(args).as_deref()).map_err(refused)?;
    Ok(Value::Array(with_cancellations(app, habits, habit_subject)?))
}

pub fn get_habit(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_habits(app, true)?;
    let habit = app.habits()?.get_habit(args_str(args, "id_or_name")).map_err(refused)?;
    Ok(with_cancellations(app, vec![habit], habit_subject)?.remove(0))
}

pub fn create_habit(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_habits(app, false)?;
    let habit = row(HABIT_ROW, args, "habit");
    super::check_part_actions(app, habit.get("traits").and_then(|t| t.get("parts")))?;
    app.habits()?.create_habit(&habit).map_err(refused)
}

pub fn update_habit(app: &App, args: &Map<String, Value>) -> ToolResult {
    prefetch_habits(app, false)?;
    let habit = row(HABIT_ROW, args, "habit");
    super::check_part_actions(app, habit.get("traits").and_then(|t| t.get("parts")))?;
    Ok(Value::Object(app.habits()?.update_habit(&habit, &args_strings(args, "clear_fields")).map_err(refused)?))
}
