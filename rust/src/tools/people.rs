//! The people, circle, trait and habit tools.

use super::stores::{prefetch_habits, prefetch_people, prefetch_stores};
use super::{check_part_actions, from_store, refused, texts, to_row, ToolError};
use crate::app::App;
use crate::contract::{tool, DateTimeText, Schema};
use crate::stores::habits::{subject_id, HABIT_ROW};
use crate::stores::people::{CIRCLE_ROW, PERSON_ROW};
use crate::stores::traits::Trait as StoredTrait;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// A typed argument as JSON.
fn to_json<T: Serialize>(value: &T) -> Value {
    serde_json::to_value(value).expect("an argument is JSON")
}

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

fn habit_subject(item: &Map<String, Value>) -> String {
    subject_id(&crate::stores::groups::python_str(item.get("id")))
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum TraitStatus {
    #[serde(rename = "active")]
    Active,
    #[serde(rename = "off")]
    Off,
    #[serde(rename = "archived")]
    Archived,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum TraitField {
    #[serde(rename = "definition")]
    Definition,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum Status {
    #[serde(rename = "active")]
    Active,
    #[serde(rename = "archived")]
    Archived,
    #[serde(rename = "deleted")]
    Deleted,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum PersonField {
    #[serde(rename = "context")]
    Context,
    #[serde(rename = "circles")]
    Circles,
    #[serde(rename = "what_matters")]
    WhatMatters,
    #[serde(rename = "traits")]
    Traits,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum CircleField {
    #[serde(rename = "note")]
    Note,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum HabitField {
    #[serde(rename = "note")]
    Note,
    #[serde(rename = "traits")]
    Traits,
}

/// A trait as get_traits lists it: plus anything wrong with it, as hand
/// edits can leave it.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ListedTrait {
    pub id: Option<String>,
    pub name: Option<String>,
    pub status: Option<TraitStatus>,
    pub definition: Option<String>,
    pub parts: Option<Vec<Map<String, Value>>>,
    pub problems: Option<Vec<String>>,
}

/// One trait -- see the module docstring.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct Trait {
    pub id: Option<String>,
    pub name: Option<String>,
    pub status: Option<TraitStatus>,
    pub definition: Option<String>,
    pub parts: Option<Vec<Map<String, Value>>>,
}

/// An event the user cancelled, as it counts against one person's
/// follow-through -- one row of the Cancellations tab (see utilities/
/// cancellations.py).
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct CancelledEvent {
    pub event_id: Option<String>,
    pub summary: Option<String>,
    pub start: Option<DateTimeText>,
    pub end: Option<DateTimeText>,
    pub action_ids: Option<Vec<String>>,
    pub engagement: Option<String>,
    pub parts: Option<Vec<String>>,
    pub cancelled_at: Option<DateTimeText>,
    pub source: Option<String>,
}

/// A person as the people tools return them: plus their circles'
/// names, and the events the user cancelled that count against their
/// follow-through.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ListedPerson {
    pub id: Option<String>,
    pub name: Option<String>,
    pub context: Option<String>,
    pub status: Option<Status>,
    pub circles: Option<Vec<String>>,
    pub what_matters: Option<String>,
    pub traits: Option<Map<String, Value>>,
    pub circle_names: Option<Vec<String>>,
    pub cancelled_events: Option<Vec<CancelledEvent>>,
}

/// One person -- see the module docstring.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct Person {
    pub id: Option<String>,
    pub name: Option<String>,
    pub context: Option<String>,
    pub status: Option<Status>,
    pub circles: Option<Vec<String>>,
    pub what_matters: Option<String>,
    pub traits: Option<Map<String, Value>>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct CreatedPerson {
    pub person: ListedPerson,
    pub created_id: String,
}

/// A circle as the circle tools return it: plus who's in it.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ListedCircle {
    pub id: Option<String>,
    pub name: Option<String>,
    pub note: Option<String>,
    pub member_ids: Option<Vec<String>>,
}

/// One circle -- see the module docstring.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct Circle {
    pub id: Option<String>,
    pub name: Option<String>,
    pub note: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct CreatedCircle {
    pub circle: ListedCircle,
    pub created_id: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct DeletedCircle {
    pub deleted: Circle,
    pub left: Vec<String>,
}

/// A habit as the habit tools return it: plus its action's or
/// group's path.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ListedHabit {
    pub id: Option<String>,
    pub name: Option<String>,
    pub action_id: Option<String>,
    pub status: Option<Status>,
    pub note: Option<String>,
    pub traits: Option<Map<String, Value>>,
    pub action_path: Option<String>,
    pub cancelled_events: Option<Vec<CancelledEvent>>,
}

/// One habit -- see the module docstring.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct Habit {
    pub id: Option<String>,
    pub name: Option<String>,
    pub action_id: Option<String>,
    pub status: Option<Status>,
    pub note: Option<String>,
    pub traits: Option<Map<String, Value>>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct CreatedHabit {
    pub habit: ListedHabit,
    pub created_id: String,
}

// -- the tools ------------------------------------------------------------------


/// The traits: how the user wants to be with people (and with
/// themselves), in order -- by default the active ones (rated) and those
/// turned off (kept, but not rated for now), not archived ones
/// (retired). Each has an id (fixed when it's created, and what a
/// person's traits name), name, definition and parts. A trait's score is
/// the weighted mean of its parts' scores (0-100), leaving out any with
/// nothing to rate it by. Each part is {"kind": ..., "weight": 1,
/// "engagement_type": "with", ...}: engagement_type "with" (the default)
/// reads the events the person was at with the user, "for" those the
/// user did for them while they weren't there. The kinds:
/// "judgment" -- each event judged by the assistant on its own, never by
/// asking the user, for one person or a named group, against a "rubric"
/// (the question: "Was this activity or place new?") on a scale of
/// "ratings" ({"0": "The activity and place were routine", "1": "There
/// was a twist on the activity or place", "2": "The activity or the
/// place were new", "3": "Both were new, or it was otherwise
/// adventurous"}), from the "facts" it names: "action" (what was done),
/// "action_history" (what's been done with them before), "location",
/// "location_history" (where they've been together before),
/// "general_notes" (the event's notes), "person_notes" (the notes on
/// the person: the user's own for a "for" engagement, the other
/// person's for "with") and "what_matters" (what's important to the
/// person, as their what_matters says), each a name or {"fact": "action_history",
/// "lookback_days": 90} (history facts look back 30 days by default),
/// and scored over the person's events in its last "window_days" (30
/// by default);
/// "continuity" (the last event ended within "last_within_days" of the
/// day's end and the next starts within "next_within_days" after it,
/// both default 14: 100 for both, 50 for one, 0 for neither); "count"
/// ({"kind": "count", "target": 1, "interval_days": 21, "zero_at_days":
/// 42, "noun": "visits"}), "duration" (with "target_min") and
/// "follow_through" ("penalty", "recovery", "look_back_days"), as the
/// measures of the same kind -- follow_through counting the events the
/// user cancelled: said in compaction didn't happen, or deleted with
/// delete_event's counts_against_follow_through, never a plan merely
/// changed. continuity, count, duration and
/// follow_through can take an "action" (an action or action group id) to
/// count only its events. A person's traits can select which traits
/// apply to them and give them their own parts for any (see
/// create_person). problems lists anything wrong with a trait edited by
/// hand; a bad part isn't rated. Read-only.
#[tool]
pub fn get_traits(app: &App, statuses: Option<Vec<TraitStatus>>) -> Result<Vec<ListedTrait>, ToolError> {
    let statuses = statuses.map(|s| texts(&s));
    from_store(app.traits()?.get_traits(statuses.as_deref()).map_err(refused)?)
}

/// Add a trait: a name (unique, at most 50 characters), a definition
/// (what it means, in a sentence or two) and parts (at least one; see
/// get_traits for each kind and its fields). Its status is active unless
/// given ("off" to keep it unrated for now). Its id is made from its name
/// and never changes, so renaming it later doesn't touch the measures
/// naming it. A trait with a part that isn't well formed is refused,
/// saying what's wrong. It applies to every person whose traits don't
/// select others. Returns the trait as created.
#[tool(writes)]
pub fn create_trait(app: &App, r#trait: Trait) -> Result<Trait, ToolError> {
    let t = StoredTrait::from_value(&to_json(&r#trait));
    check_part_actions(app, t.parts.as_ref())?;
    from_store(app.traits()?.create_trait(&t).map_err(refused)?)
}

/// Change a trait, by id: rename it, reword its definition, change its
/// status, or give it new parts. Fields left out keep their value; parts,
/// if given, replace them whole, so send every part to keep. status is
/// active (rated), off (kept, but not rated for now) or archived (retired:
/// not rated, and get_traits leaves it out unless asked) -- archive a
/// trait rather than delete it, so its history stays readable. Checked as
/// for create_trait. Ratings already recorded aren't changed. Returns the
/// trait as updated.
#[tool(writes)]
pub fn update_trait(app: &App, r#trait: Trait, clear_fields: Option<Vec<TraitField>>) -> Result<Trait, ToolError> {
    let t = StoredTrait::from_value(&to_json(&r#trait));
    check_part_actions(app, t.parts.as_ref())?;
    from_store(app.traits()?.update_trait(&t, &texts(&clear_fields.unwrap_or_default())).map_err(refused)?)
}

/// The people the user spends time with: by default the active ones,
/// not archived (out of touch) or deleted ones. The user themself is
/// always first, with the id "self". Each has an id, name, context (what
/// tells them apart from others of the same name, e.g. "met at salsa"),
/// status, circles (the ids of the circles they're in) with their
/// circle_names, what_matters (what's important to them) and traits:
/// which traits apply to them, and their own parts for any (see
/// create_person; without it, every active trait applies as get_traits
/// has it). Each also has cancelled_events: the events the user cancelled
/// that count against their follow-through, newest first -- when each was
/// planned, its actions, whether they were to be there ("with") or it was
/// for them ("for"), the follow-through parts it counted against, and
/// what cancelled it. Read-only.
#[tool]
pub fn get_people(app: &App, statuses: Option<Vec<Status>>) -> Result<Vec<ListedPerson>, ToolError> {
    prefetch_people(app)?;
    let statuses = statuses.map(|s| texts(&s));
    let people = app.people()?.get_people(statuses.as_deref()).map_err(refused)?;
    from_store(Value::Array(with_cancellations(app, people, person_id)?))
}

/// One person, by id ("self" for the user) or else by name (ignoring
/// case), as get_people lists them. If several share the name, the error
/// lists them with their contexts; if there's none, it suggests close
/// matches. Read-only.
#[tool]
pub fn get_person(app: &App, id_or_name: String) -> Result<ListedPerson, ToolError> {
    prefetch_people(app)?;
    let person = app.people()?.get_person(&id_or_name).map_err(refused)?;
    from_store(with_cancellations(app, vec![person], person_id)?.remove(0))
}

/// Add a person: a name, and optionally a context (what tells them
/// apart, e.g. "met at salsa" -- a name and context together must be
/// unique, so give one when the name's taken), circles (circle ids, see
/// get_circles), what_matters (what's important to them) and traits:
/// {"select": "all" or [trait ids], "parts": {trait id: [parts]}}, both
/// optional -- the traits that apply to them (by default every active
/// one), and parts replacing a trait's for them alone, each checked as a
/// trait's own (see get_traits): a person's own cadence, say,
/// {"parts": {"reliable": [{"kind": "count", "target": 1,
/// "interval_days": 21, "noun": "visits"}]}}. Active unless given a
/// status. id is assigned. Returns the person and their id as
/// created_id.
#[tool(writes)]
pub fn create_person(app: &App, person: Person) -> Result<CreatedPerson, ToolError> {
    prefetch_stores(app)?;
    let person = to_row(PERSON_ROW, &person);
    check_part_actions(app, person.get("traits").and_then(|t| t.get("parts")))?;
    from_store(app.people()?.create_person(&person).map_err(refused)?)
}

/// Update a person by id ("self" for the user): their name, context,
/// status (active; archived, out of touch; or deleted, shouldn't have
/// existed -- self is always active), circles (replaced whole, so send
/// every circle to keep), what_matters or traits (replaced whole; see
/// create_person). Omitted properties keep their
/// value; list one in clear_fields to blank it instead. Returns the
/// person as updated.
#[tool(writes)]
pub fn update_person(app: &App, person: Person, clear_fields: Option<Vec<PersonField>>) -> Result<ListedPerson, ToolError> {
    prefetch_stores(app)?;
    let person = to_row(PERSON_ROW, &person);
    check_part_actions(app, person.get("traits").and_then(|t| t.get("parts")))?;
    let clear = texts(&clear_fields.unwrap_or_default());
    from_store(Value::Object(app.people()?.update_person(&person, &clear).map_err(refused)?))
}

/// Every circle: a group people belong to ("Close friends",
/// "Family"), a person belonging to any number of them. Each has an id,
/// name, note and member_ids (the people in it). Read-only.
#[tool]
pub fn get_circles(app: &App) -> Result<Vec<ListedCircle>, ToolError> {
    prefetch_stores(app)?;
    from_store(app.people()?.get_circles().map_err(refused)?)
}

/// One circle, by id or else by name (ignoring case), as get_circles
/// lists it. Read-only.
#[tool]
pub fn get_circle(app: &App, id_or_name: String) -> Result<ListedCircle, ToolError> {
    prefetch_stores(app)?;
    from_store(app.people()?.get_circle(&id_or_name).map_err(refused)?)
}

/// Add a circle: a name (unique among circles, though a person may
/// share it) and optionally a note. id is assigned. Put people in it with
/// update_person's circles. Returns the circle and its id as
/// created_id.
#[tool(writes)]
pub fn create_circle(app: &App, circle: Circle) -> Result<CreatedCircle, ToolError> {
    prefetch_stores(app)?;
    from_store(app.people()?.create_circle(&to_row(CIRCLE_ROW, &circle)).map_err(refused)?)
}

/// Rename a circle or change its note, by id. Returns it as updated.
#[tool(writes)]
pub fn update_circle(app: &App, circle: Circle, clear_fields: Option<Vec<CircleField>>) -> Result<ListedCircle, ToolError> {
    prefetch_stores(app)?;
    let clear = texts(&clear_fields.unwrap_or_default());
    from_store(app.people()?.update_circle(&to_row(CIRCLE_ROW, &circle), &clear).map_err(refused)?)
}

/// Delete a circle, by id. Its people aren't deleted: they just leave
/// it. Returns the circle as it was, and the ids of the people who
/// left.
#[tool(writes)]
pub fn delete_circle(app: &App, circle_id: String) -> Result<DeletedCircle, ToolError> {
    prefetch_stores(app)?;
    from_store(app.people()?.delete_circle(&circle_id).map_err(refused)?)
}

/// The user's habits: by default the active ones, not archived (set
/// aside) or deleted ones. Each is about the user's events with one
/// action, or with any action under one action group (action_id, with
/// its action_path), and is rated by traits as a person is: its traits
/// say which apply (by default every active one) and give it its own
/// parts -- its own rubrics -- for any (see create_habit). Each has an
/// id, name, status and note (what it's for, and what doing it well
/// looks like). Each also has cancelled_events: the events the user
/// cancelled that count against its follow-through, newest first, as a
/// person's (see get_people). Its events' judgments are kept under
/// "habit:<id>", as a person's are under their id. Read-only.
#[tool]
pub fn get_habits(app: &App, statuses: Option<Vec<Status>>) -> Result<Vec<ListedHabit>, ToolError> {
    prefetch_habits(app, true)?;
    let statuses = statuses.map(|s| texts(&s));
    let habits = app.habits()?.get_habits(statuses.as_deref()).map_err(refused)?;
    from_store(Value::Array(with_cancellations(app, habits, habit_subject)?))
}

/// One habit, by id or else by name (ignoring case), as get_habits
/// lists it, whatever its status. If there's none, the error suggests
/// close matches. Read-only.
#[tool]
pub fn get_habit(app: &App, id_or_name: String) -> Result<ListedHabit, ToolError> {
    prefetch_habits(app, true)?;
    let habit = app.habits()?.get_habit(&id_or_name).map_err(refused)?;
    from_store(with_cancellations(app, vec![habit], habit_subject)?.remove(0))
}

/// Add a habit: a name (unique among habits), the action or action
/// group whose events it's about (action_id: an action's or a group's
/// id; only events with that action, or any action under that group,
/// count toward it), and optionally a note (what it's for, and what
/// doing it well looks like -- context for judging its events) and
/// traits: {"select": "all" or [trait ids], "parts": {trait id:
/// [parts]}}, both optional, as a person's (see create_person) -- the
/// traits that apply to it (by default every active one), and parts
/// replacing a trait's for it alone, a more specific rubric, say. It's
/// always "with": judgment parts for those an event was done "for"
/// don't apply to it. Active unless given a status. id is assigned.
/// Returns the habit and its id as created_id.
#[tool(writes)]
pub fn create_habit(app: &App, habit: Habit) -> Result<CreatedHabit, ToolError> {
    prefetch_habits(app, false)?;
    let habit = to_row(HABIT_ROW, &habit);
    check_part_actions(app, habit.get("traits").and_then(|t| t.get("parts")))?;
    from_store(app.habits()?.create_habit(&habit).map_err(refused)?)
}

/// Update a habit by id: its name, action_id (an action's or a
/// group's), status (active; archived, set aside; or deleted, shouldn't
/// have existed), note or traits (replaced whole; see create_habit).
/// Omitted properties keep their value; list one in clear_fields to
/// blank it instead. Returns the habit as updated.
#[tool(writes)]
pub fn update_habit(app: &App, habit: Habit, clear_fields: Option<Vec<HabitField>>) -> Result<ListedHabit, ToolError> {
    prefetch_habits(app, false)?;
    let habit = to_row(HABIT_ROW, &habit);
    check_part_actions(app, habit.get("traits").and_then(|t| t.get("parts")))?;
    let clear = texts(&clear_fields.unwrap_or_default());
    from_store(Value::Object(app.habits()?.update_habit(&habit, &clear).map_err(refused)?))
}
