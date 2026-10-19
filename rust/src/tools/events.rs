//! The event tools (list_events, get_event, create_event, update_event,
//! delete_event), the calendar's time zone, schedule hints and time notes.

use super::stores::{prefetch, prefetch_stores};
use super::{from_store, refused, texts, ToolError};
use crate::app::App;
use crate::event::{parse_iso, Event, Moment};
use crate::contract::{tool, DateTimeText, Schema};
use crate::facts::{fact_problems, Facts as EventFacts, LocationShare, PersonShare};
use crate::py;
use crate::stores::action_calendar::{fill_in_from_actions, ActionCalendar};
use crate::stores::actions::{share_ids, ActionShare, ActionTree};
use crate::stores::event_changes::{self, Cancel, EventChanges, StaleEvent, StaleEvents};
use crate::stores::goals::{settled_shares, GoalShare};
use crate::stores::notes::NotedTime as StoredNote;
use crate::stores::schedule::ScheduleHint;
use super::recurrences::{stored_repeat, Repeat};
use indexmap::IndexMap;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

// -- the types -------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum EventField {
    #[serde(rename = "description")]
    Description,
    #[serde(rename = "location")]
    Location,
    #[serde(rename = "priority")]
    Priority,
    #[serde(rename = "facts")]
    Facts,
    #[serde(rename = "judgments")]
    Judgments,
}

/// What compaction established about an event: where it was
/// (location_ids, usually one place; several when it moved between
/// them), who was there with the user (with_ids), who it was for
/// (for_ids), a note on how it was for each person there (notes),
/// whether it was a commute, getting from one place to another (commute),
/// and what it changed in people's trait facts (logs: {person id --
/// "self", or one it was with or for: {"<trait id>/<fact id>": a log of
/// the change, at most 500 characters}}, each beside the summary the
/// change rewrote; see compact_notes' fact_summaries). Every field is
/// optional.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct Facts {
    pub location_ids: Option<Vec<LocationShare>>,
    pub with_ids: Option<Vec<PersonShare>>,
    pub for_ids: Option<Vec<PersonShare>>,
    pub notes: Option<IndexMap<String, String>>,
    pub commute: Option<bool>,
    pub logs: Option<IndexMap<String, IndexMap<String, String>>>,
}

/// A calendar event, as every event tool takes and returns it.
///
/// is_cancelled is read-only: update_event refuses to set it -- cancel
/// an event with delete_event, which says whether the cancellation counts
/// against follow-through.
///
/// action_ids are what was done at the event (see get_actions), the
/// first setting its label and color; setting them is how an event is
/// tied to actions. Each is an action's id, taking an even part of the
/// remaining time (what the minutes and percents leave), or an object
/// {action_id, minutes?, percent?, remaining?}: minutes (a fixed share),
/// percent (of what the minutes leave), and remaining true for its part of
/// the remaining time as well. The minutes must fit in the event and the
/// percents add up to no more than 100; read back, minutes are cut to fit
/// an event shortened since. action_names (their names, in the same order) and
/// event_label_id (derived from the first action) are read-only: an
/// update ignores them.
///
/// actions_from_label is true when an event was never given actions, so
/// its action_ids are inferred from its label (the action that holds the
/// label), not stored. Sending it back with actions_from_label still true
/// leaves them inferred, whatever action_ids says; to store actions, send
/// action_ids with actions_from_label false (or left out).
///
/// effective_priority is read-only: the priority that applies, i.e.
/// priority falling back,
/// when the event doesn't set one, to the highest (lowest-numbered)
/// priority among its actions (each action's own, or its nearest
/// group's). Kept separate from priority so that sending a listed event
/// straight back to update_event never copies its actions' priority onto
/// the event itself, which would stop it following later changes to the
/// actions. An update ignores it.
///
/// is_end_of_day_sleep/recurring_event_id are read-only too, and an
/// update ignores them as well. is_end_of_day_sleep decides where a day
/// ends for compaction, so a wrong mark would quietly change what later
/// updates shrink, move or cancel; it's only set by hand, with
/// calendar_cli. recurring_event_id is assigned by Google and can't be
/// set at all.
///
/// goals are the goals the event is for (see get_goals): each a goal_id,
/// with minutes (a fixed share of the event), percent (of what the
/// minutes leave), or neither (the remaining time, what the minutes and
/// percents leave); remaining true gives one with minutes or percent the
/// remaining time too. The minutes must fit in
/// the event, and the percents add up to no more than 100; read back,
/// minutes are cut to fit an event shortened since, and goals deleted
/// since are left out. Set them to replace them whole ([] for none).
/// Most events have none: tag an event with a goal only when that goal
/// is open (inactive or active), has attribution_hints, and the event
/// clearly matches them -- never guess for a goal without hints. Tag the
/// most specific goal that fits (a sub-goal rather than its parent), and
/// give minutes or percent when only part of the event was for it.
///
/// facts are what compaction established about a past event, for
/// traits' judgments later: location_ids (see
/// get_locations), with_ids (the people there with the user -- never
/// "self", who's at every event), for_ids (people it was done for who
/// weren't there), notes ({person id: a subjective note on how it was
/// for them}, "self" for the user) and commute (true for getting from
/// one place to another). Compaction writes them; set them to replace
/// them whole. Each place and person can take a share of the event:
/// {location_id or person_id, minutes?, percent?, remaining?} in place of
/// the id -- minutes (a fixed share), percent (of what the minutes leave),
/// and remaining true for the rest too. One given as a bare id takes the
/// remaining time, what the minutes and percents leave: each person all of
/// it, and the places an even part each. Each list's minutes must fit in
/// the event, and its percents add up to no more than 100.
///
/// judgments are how the event went for each person, judged after
/// compaction against each judgment part of the traits that apply to them
/// (see record_judgments): {person id: {trait id: {part key: {"rating",
/// "scale", "reasoning"}}}}. Set them to overwrite a rating by hand
/// (replacing them whole), or clear them.
///
/// repeat is write-only: given to create_event, or to update_event for an
/// event that isn't in a series, it makes the event the first of a
/// recurring series that repeats as it says (see Repeat), in the
/// calendar's time zone. The event's id becomes the series' own, and the
/// series' events (list_events) each get their own id, with the series'
/// as their recurring_event_id. It's never returned: get_recurrence says
/// how a series repeats, and update_recurrence changes it.
///
/// Everything before the last compaction (get_compaction_status's
/// last_compaction) is history: see update_event.
#[derive(Clone, Debug, Default, Serialize, Deserialize, Schema)]
pub struct PublicEvent {
    pub id: Option<String>,
    pub summary: Option<String>,
    pub start: Option<DateTimeText>,
    pub end: Option<DateTimeText>,
    pub description: Option<String>,
    pub location: Option<String>,
    pub priority: Option<i64>,
    pub action_ids: Option<Vec<ActionShare>>,
    pub action_names: Option<Vec<String>>,
    #[schema(default = "false")]
    #[serde(default)]
    pub actions_from_label: bool,
    pub goals: Option<Vec<GoalShare>>,
    pub event_label_id: Option<String>,
    #[schema(default = "false")]
    #[serde(default)]
    pub is_cancelled: bool,
    pub effective_priority: Option<i64>,
    pub is_end_of_day_sleep: Option<bool>,
    pub recurring_event_id: Option<String>,
    pub facts: Option<Facts>,
    pub judgments: Option<Map<String, Value>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub repeat: Option<Repeat>,
    /// Which version of the event this is, as Calendar gives it; it
    /// changes whenever the event does. Give it to update_event and delete_event -- the etag you last read: without it, the change is refused; changed since, the call is refused with every event that's changed, as it is now (_meta.conflicts) -- on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK.
    pub etag: Option<String>,
}

/// An event to cancel, and whether that counts against follow-through:
/// a commitment dropped (true) rather than just a change of plan (false).
/// Give etag -- the event's, as you last read it: without it, the cancel is refused; changed since, it's refused -- on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct EventCancel {
    pub event_id: String,
    pub counts_against_follow_through: bool,
    pub etag: Option<String>,
}

/// One event's update: `event` (its id and the fields to set), and the
/// fields to remove (`clear_fields`).
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct EventUpdate {
    pub event: PublicEvent,
    pub clear_fields: Option<Vec<EventField>>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct EventChangesResult {
    pub events: Vec<PublicEvent>,
    pub timeline: Option<String>,
    #[schema(default = "false")]
    pub dry_run: bool,
}

/// A single time note: some moment worth recording, with an optional
/// free-text description of what it marks.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct NotedTime {
    pub timestamp: DateTimeText,
    pub description: Option<String>,
    pub compaction_id: Option<String>,
    /// The note's own id, if a client chose it: given to note, the note is
    /// named by it, and the same note sent again is given as it was made.
    pub id: Option<String>,
}

/// A note, with the id that edit_note and delete_note (and compaction)
/// refer to it by.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct NoteWithId {
    pub id: String,
    pub timestamp: DateTimeText,
    pub description: Option<String>,
    pub compaction_id: Option<String>,
    /// Goes up by one each time it's edited. Give it to edit_note (version) and delete_note (if_version) -- the version you last read: without it, it's refused; changed since, it's refused -- on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK.
    pub version: Option<i64>,
}


#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct CompactionScheduleHints {
    pub hints: Vec<ScheduleHint>,
    pub time_zone: Option<String>,
}

/// An instant a client gave (with an offset, or without one: then it's
/// taken as UTC, as the arguments' check leaves it).
pub fn when(text: &DateTimeText) -> anyhow::Result<Moment> {
    parse_iso(&text.0, None)
}

pub fn when_given(text: Option<&DateTimeText>) -> anyhow::Result<Option<Moment>> {
    text.map(when).transpose()
}

impl From<&Facts> for EventFacts {
    /// The facts a client gave, as an event keeps them.
    fn from(facts: &Facts) -> EventFacts {
        EventFacts {
            location_ids: facts.location_ids.as_ref().map(|ids| json!(ids)),
            with_ids: facts.with_ids.as_ref().map(|ids| json!(ids)),
            for_ids: facts.for_ids.as_ref().map(|ids| json!(ids)),
            notes: facts.notes.as_ref().map(|notes| json!(notes)),
            commute: facts.commute.map(Value::Bool),
            logs: facts.logs.as_ref().map(|logs| json!(logs)),
        }
    }
}

// -- PublicEvent -----------------------------------------------------------------

/// An event as every tool returns it (PublicEvent), fields in order: its
/// goal shares settled (see `settled_shares`) against the goals there are
/// (`goal_ids`; all of them, if None), and its action shares settled
/// against its length.
#[cfg(test)]
pub fn public_event(event: &Event, tree: Option<&ActionTree>) -> Value {
    public_event_among(event, tree, None)
}

/// `public_event`, with only `goal_ids`' goals (all, if None) shown.
pub fn public_event_among(event: &Event, tree: Option<&ActionTree>, goal_ids: Option<&[String]>) -> Value {
    let action_ids = shown_action_shares(event);
    let names = match (tree, &action_ids) {
        (Some(tree), Some(ids)) => Some(ids.iter().map(|a| tree.name(&a.action_id)).collect::<Vec<_>>()),
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
    out.insert("action_ids".into(), json!(action_ids));
    out.insert("action_names".into(), json!(names));
    out.insert("actions_from_label".into(), json!(event.actions_from_label));
    out.insert("goals".into(), json!(shown_shares(event, goal_ids)));
    out.insert("event_label_id".into(), json!(event.event_label_id));
    out.insert("is_cancelled".into(), json!(event.is_cancelled()));
    out.insert("effective_priority".into(), json!(event.effective_priority()));
    out.insert("is_end_of_day_sleep".into(), json!(event.is_end_of_day_sleep));
    out.insert("recurring_event_id".into(), json!(event.recurring_event_id));
    out.insert("facts".into(), event.facts.as_ref().map(EventFacts::to_public).unwrap_or(Value::Null));
    out.insert("judgments".into(), event.judgments.clone().map(Value::Object).unwrap_or(Value::Null));
    out.insert("etag".into(), json!(event.etag.0));
    Value::Object(out)
}

/// `event`'s action shares, settled (see `public_event`) if any has
/// minutes or percent; as they are, if none does.
pub fn shown_action_shares(event: &Event) -> Option<Vec<ActionShare>> {
    let shares = event.action_ids.as_ref()?;
    if shares.iter().all(|s| s.minutes.is_none() && s.percent.is_none()) {
        return Some(shares.clone());
    }
    Some(settled_shares(shares, event_minutes(event), &|_| true))
}

/// `event`'s length in minutes, or no end to it if it has no start or end.
fn event_minutes(event: &Event) -> i64 {
    match (event.start, event.end) {
        (Some(start), Some(end)) => (end - start).num_minutes(),
        _ => i64::MAX,
    }
}

/// `event`'s goal shares, settled (see `public_event`).
pub fn shown_shares(event: &Event, goal_ids: Option<&[String]>) -> Option<Vec<GoalShare>> {
    let shares = event.goals.as_ref()?;
    Some(settled_shares(shares, event_minutes(event), &|id| goal_ids.is_none_or(|ids| ids.iter().any(|i| i == id))))
}

/// The ids of the goals there are, read only if any of `events` has goals.
pub fn goal_ids_for(app: &App, events: &[Event]) -> anyhow::Result<Option<Vec<String>>> {
    if events.iter().all(|e| e.goals.as_ref().is_none_or(Vec::is_empty)) {
        return Ok(None);
    }
    Ok(Some(app.goals()?.ids()?))
}

/// `_public_events`: with action names and priorities filled in.
pub fn public_events(app: &App, events: &[Event]) -> anyhow::Result<Value> {
    let tree = app.actions()?.tree()?;
    let goal_ids = goal_ids_for(app, events)?;
    Ok(Value::Array(fill_in_from_actions(events, &tree).iter().map(|e| public_event_among(e, Some(&tree), goal_ids.as_deref())).collect()))
}

/// `PublicEvent.to_event(clear_fields)`.
pub fn to_event(event: &PublicEvent, clear_fields: &[EventField]) -> Result<Event, ToolError> {
    let event = Event {
        id: event.id.clone(),
        summary: event.summary.clone(),
        start: when_given(event.start.as_ref())?,
        end: when_given(event.end.as_ref())?,
        description: event.description.clone(),
        location: event.location.clone(),
        action_ids: if event.actions_from_label { None } else { event.action_ids.clone() },
        goals: event.goals.clone(),
        priority: event.priority,
        status: if event.is_cancelled { Some("cancelled".into()) } else { None },
        facts: event.facts.as_ref().map(|f| EventFacts::from(f).normalized()),
        judgments: event.judgments.clone(),
        cleared: texts(clear_fields).into_iter().collect(),
        etag: crate::event::Etag(event.etag.clone()),
        ..Default::default()
    };
    event.check_cleared().map_err(|e| ToolError::Message(e.to_string()))?;
    Ok(event)
}

/// `_check_action_ids`: an event's (or series') action_ids must be
/// actions, not newly deleted ones -- unless they're from its label.
pub fn check_action_ids(app: &App, id: Option<&str>, action_ids: Option<&[ActionShare]>, from_label: bool, existing: bool) -> Result<(), ToolError> {
    let ids = share_ids(action_ids.unwrap_or_default());
    if ids.is_empty() || from_label {
        return Ok(());
    }
    let tree = app.actions()?.tree()?;
    let mut already: Vec<String> = Vec::new();
    if existing {
        if let Some(id) = id.filter(|i| !i.is_empty()) {
            let any_deleted =
                ids.iter().any(|a| tree.by_id(a).is_some_and(|action| action.status == Some(crate::stores::actions::ActionStatus::Deleted)));
            if any_deleted {
                let current = fill_in_from_actions(&[app.calendar_client()?.get_event(id)?], &tree).remove(0);
                already = share_ids(current.action_ids.as_deref().unwrap_or_default());
            }
        }
    }
    tree.check_action_ids(&ids, &already).map_err(|e| ToolError::Message(e.to_string()))
}

/// An event's (or series') goal shares must name goals there are.
pub fn check_goal_ids(app: &App, goals: Option<&[GoalShare]>) -> Result<(), ToolError> {
    let Some(goals) = goals.filter(|g| !g.is_empty()) else { return Ok(()) };
    let known = app.goals()?.ids()?;
    let unknown: Vec<String> = goals.iter().map(|g| g.goal_id.trim().to_string()).filter(|id| !known.contains(id)).collect();
    if !unknown.is_empty() {
        return Err(ToolError::Message(format!("Its goals name {}, which aren't goals (get_goals lists them)", py::repr_list(&unknown))));
    }
    Ok(())
}

/// `_check_facts`: well formed, naming people and a location that exist.
pub fn check_facts(app: &App, facts: Option<&Facts>) -> Result<(), ToolError> {
    let Some(given) = facts else { return Ok(()) };
    let facts = EventFacts::from(given).normalized();
    let problems = fact_problems(&facts);
    if !problems.is_empty() {
        return Err(ToolError::Message(format!("Its facts {}", problems.join("; "))));
    }
    let people: Vec<Value> = app.people()?.all()?.into_iter().map(|p| json!(p.id)).collect();
    let unknown: Vec<String> = facts.people().into_iter().filter(|i| !people.contains(&json!(i))).collect();
    if !unknown.is_empty() {
        return Err(ToolError::Message(format!("Its facts name {}, who aren't people (get_people lists them)", py::repr_list(&unknown))));
    }
    let locations = facts.locations();
    if !locations.is_empty() {
        let known: Vec<Value> = app.locations()?.all()?.into_iter().map(|l| json!(l.id)).collect();
        if let Some(location) = locations.iter().find(|l| !known.contains(&json!(l))) {
            return Err(ToolError::Message(format!(
                "Its facts' location {} isn't a location (get_locations lists them)",
                py::repr(location)
            )));
        }
    }
    Ok(())
}

/// `event`, made the first of a series that repeats as `repeat` says, if
/// it's given: its rules, and the calendar's time zone, which Google
/// needs on a series' start and end.
fn with_repeat(app: &App, mut event: Event, repeat: Option<&Repeat>) -> Result<Event, ToolError> {
    let Some(repeat) = repeat else { return Ok(event) };
    let repeat = stored_repeat(repeat)?;
    repeat.check().map_err(|e| ToolError::Message(e.to_string()))?;
    let zone = app.calendar_client()?.get_time_zone()?;
    event.recurrence = Some(repeat.to_rules(&zone).map_err(|e| ToolError::Message(e.to_string()))?);
    event.time_zone = Some(zone.name().to_string());
    Ok(event)
}

// -- the event tools ---------------------------------------------------------------

/// `_prefetch_for_changes`: the people's tabs and the journal, in one request.
pub fn prefetch_for_changes(app: &App) -> anyhow::Result<()> {
    let (actions, people, locations, cancellations, journal) =
        (app.actions()?, app.people()?, app.locations()?, app.cancellations()?, app.journal()?);
    let mut ranges = actions.whole_tabs();
    ranges.extend(people.whole_tabs());
    ranges.extend(locations.whole_tabs());
    ranges.extend(cancellations.whole_tabs());
    ranges.push(journal.whole_tab());
    prefetch(app, ranges)
}

#[allow(clippy::too_many_arguments)]
fn change_events(
    app: &App,
    tool_name: &str,
    updates: Vec<Event>,
    creates: Vec<Event>,
    cancels: Vec<Cancel>,
    allow_compacted: bool,
    dry_run: bool,
) -> Result<Value, ToolError> {
    let changes = EventChanges::new(ActionCalendar::new(app.calendar_client()?, app.actions()?), Some(app.cancellations()?));
    let history_until = app.journal()?.last_stamped_now()?;
    let checked = match changes.check(updates, creates, cancels, history_until, allow_compacted).map_err(|e| stale(app, e))? {
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
            .again
            .iter()
            .cloned()
            .chain(
                checked
                    .changes
                    .iter()
                    .map(|c| c.after.clone().unwrap_or_else(|| Event { status: Some("cancelled".into()), ..c.before.clone().unwrap() })),
            )
            .collect()
    } else {
        changes.apply(&checked, tool_name).map_err(|e| stale(app, e))?
    };
    Ok(json!({
        "events": public_events(app, &events)?,
        "timeline": drawn.map(|t| t.text),
        "dry_run": dry_run,
    }))
}

// -- time zone and schedule ----------------------------------------------------------

fn schedule_time_zone(app: &App) -> anyhow::Result<Value> {
    match app.calendar_client()?.get_time_zone() {
        Ok(zone) => Ok(json!(zone.name())),
        Err(error) if crate::google::calendar::is_time_zone_not_set(&error) => Ok(Value::Null),
        Err(error) => Err(error),
    }
}

// -- notes -----------------------------------------------------------------------------

fn to_cancels(cancels: Vec<EventCancel>) -> Vec<Cancel> {
    cancels
        .into_iter()
        .map(|c| Cancel { event_id: c.event_id, counts_against_follow_through: c.counts_against_follow_through, etag: c.etag })
        .collect()
}

/// `error`, as a conflict if it's a change to events that have changed
/// since the client read them -- each, as it is now -- saying how far the
/// batch got if it stopped partway.
fn stale(app: &App, error: anyhow::Error) -> ToolError {
    let all = match (error.downcast_ref::<StaleEvents>(), error.downcast_ref::<StaleEvent>()) {
        (Some(all), _) => all.clone(),
        (None, Some(one)) => StaleEvents { items: vec![one.clone()], partly: None },
        _ => return error.into(),
    };
    let mut items = Vec::new();
    for stale in &all.items {
        let current = match &stale.event {
            Some(event) => match public_events(app, std::slice::from_ref(event)) {
                Ok(Value::Array(mut shown)) => shown.pop().and_then(|e| e.as_object().cloned()),
                Ok(_) => None,
                Err(error) => return error.into(),
            },
            None => None,
        };
        items.push(crate::sheet::rows::Conflict {
            kind: "event",
            id: stale.event_id.clone(),
            expected: crate::sheet::rows::Seen::Etag(stale.expected.clone()),
            current,
        });
    }
    ToolError::Conflict(crate::sheet::rows::Conflicts { items, partly: all.partly })
}

// -- the tools ------------------------------------------------------------------

/// List events between min_time and max_time. action_ids are what was
/// done at each event, the first setting its color (action_names gives
/// their names). effective_priority is the priority that applies:
/// the event's own priority, falling back to the highest priority of its
/// actions.
/// is_end_of_day_sleep marks the sleep event that
/// ends a day; recurring_event_id is the id of an instance's recurring
/// series (see get_recurrence and update_recurrence to read and edit the
/// series as a whole); event_label_id is the calendar label derived from
/// its first action. facts are what compaction established about a past
/// event: where, who with, who for, and a note on each person there (see
/// PublicEvent). action_names, effective_priority, is_end_of_day_sleep,
/// recurring_event_id and event_label_id are read-only: update_event and
/// create_event ignore them. repeat is write-only, and never listed.
///
/// `person_id`: only the events whose facts name that person (with, for,
/// or noted) -- an id, or a name get_person takes. Quick over any span,
/// years included: search a person's history with a wide range.
#[tool]
pub fn list_events(app: &App, min_time: DateTimeText, max_time: DateTimeText, person_id: Option<String>) -> Result<Vec<PublicEvent>, ToolError> {
    prefetch_stores(app)?;
    let (min, max) = (when(&min_time)?, when(&max_time)?);
    let calendar = app.calendar_client()?;
    let listed = match person_id {
        Some(person) => {
            let id = app.people()?.get_person(&person).map_err(refused)?.id.unwrap_or(person);
            let property = format!("{}={}", crate::event::person_key(&id), crate::event::PERSON_INDEXED);
            calendar.list_event_resources(&min, &max, Some(&property))?.iter().map(Event::from_api).collect::<anyhow::Result<Vec<_>>>()?
        }
        None => calendar.list_events(&min, &max)?,
    };
    let events: Vec<Event> = listed.into_iter().filter(|e| !e.is_cancelled()).collect();
    from_store(public_events(app, &events)?)
}

/// Get a single event by its ID. See list_events for its fields.
#[tool]
pub fn get_event(app: &App, id: String) -> Result<PublicEvent, ToolError> {
    prefetch_stores(app)?;
    let event = app.calendar_client()?.get_event(&id)?;
    if event.is_cancelled() {
        return Err(ToolError::Message(format!("Event {id} has been cancelled.")));
    }
    from_store(public_events(app, &[event])?.as_array().unwrap()[0].clone())
}

/// Change several events at once, as one batch: `updates` (each an
/// event's id and the fields to set -- left out, a field keeps its value;
/// list it in that update's clear_fields to remove it instead -- clearing
/// priority makes it follow its actions' priority again), `creates` (new
/// events, optionally with actions: action_ids, the first setting its
/// color), `cancels` (each saying whether it counts against
/// follow-through: a commitment dropped, rather than a change of plan --
/// see delete_event). An event can be in only one of them; to move
/// events, update each with its new times. Set action_ids to change an event's actions ([] for none), and
/// facts to replace its facts whole (see list_events). An update can't
/// cancel an event (is_cancelled): put it in `cancels`.
/// WHERE an event is (facts.location_ids), when you set it: keep a
/// location it already has unless the events and notes around it
/// clearly show the user was somewhere else. Otherwise the user stays
/// put: it's where the previous event was, unless there's evidence they
/// moved in between -- the previous event, or one between, is a commute
/// (an action with commute true, see get_actions, or the commute fact);
/// the user said they were somewhere other than planned; or the event
/// clearly matches another location's hint (see get_locations). After a
/// commute they're somewhere other than before it: tell where from the
/// location of the event after it (that one event only), or else from
/// the locations' hints. A commute itself gets the commute fact and no
/// location_ids.
///
/// GOALS an event is for (goals), when you set them: only goals that
/// are open (inactive or active) and have attribution_hints (see
/// get_goals), and only when the event clearly matches a goal's hints --
/// never guess for a goal without hints, and leave an event untagged
/// when it's unclear (or when the user asks, tag it as they say). Tag
/// the most specific goal that fits, a sub-goal rather than its parent,
/// and when only part of the event was for it, give minutes or percent.
///
/// Set repeat, on a create or on an update of an event that isn't in a
/// series, to make it the first event of a recurring series (see
/// PublicEvent); only that first event is checked for overlaps, as for
/// update_recurrence. An update can't be of a series itself (the id a
/// series' events give as recurring_event_id) -- edit it with
/// update_recurrence -- nor give repeat to an event already in one.
///
/// BATCHES ARE CHECKED WHOLE, AND NOTHING IS MOVED TO MAKE ROOM: every
/// event the call creates or updates must end after it starts and must
/// not overlap any other event -- one on the calendar or another in the
/// call -- so move, shorten or cancel whatever's in the way in the same
/// call. A call that breaks this changes nothing, and the error lists
/// every problem and the events around them, to send a valid call at
/// once.
///
/// VERSIONS: each update and cancel gives the event's etag as you last
/// read it (list_events and get_event give it). One without is refused;
/// one changed since is refused, with every such event as it is now
/// (_meta.conflicts). Then read each again: if what changed elsewhere is
/// in fields you aren't changing, make your change again on it as it is
/// now; if it's in fields you're changing, or it was deleted or cancelled
/// (or, cancelling it, it was changed since), stop and ask the user --
/// showing what changed and what you meant to do -- and never overwrite
/// their change, or put back what was deleted, without their OK. Calendar
/// writes each event on its own, so one changed after the batch is
/// checked stops it partway: the error says which changes were written
/// (_meta.partly) before it stopped. Sending the batch again, from what's
/// there now, is safe -- what was written is given as it is -- except a
/// create made without an id of its own, which would be made again:
/// leave those out.
///
/// Everything before the last compaction (get_compaction_status's
/// last_compaction) is history: an event that started before it can't be
/// changed or cancelled, and no event can be created, or moved, to start
/// before it, unless allow_compacted_changes is set -- ONLY set it when
/// the user has explicitly approved changing history -- except that an
/// event still going on then may run on (an update moving only its end,
/// to no earlier than the last compaction). dry_run checks the batch
/// and returns what it would do without changing anything. Returns the
/// events changed, and a timeline of them beside the events around
/// them.
#[tool(writes)]
pub fn update_event(app: &App, updates: Option<Vec<EventUpdate>>, creates: Option<Vec<PublicEvent>>, cancels: Option<Vec<EventCancel>>, #[schema(default = "false")] allow_compacted_changes: bool, #[schema(default = "false")] dry_run: bool) -> Result<EventChangesResult, ToolError> {
    prefetch_for_changes(app)?;
    let updates = updates.unwrap_or_default();
    let creates = creates.unwrap_or_default();
    for update in &updates {
        if update.event.is_cancelled {
            return Err(ToolError::Message(
                "an update can't cancel an event: put it in cancels, saying with counts_against_follow_through whether the cancellation counts against follow-through".into(),
            ));
        }
        let event = &update.event;
        check_action_ids(app, event.id.as_deref(), event.action_ids.as_deref(), event.actions_from_label, true)?;
        check_goal_ids(app, event.goals.as_deref())?;
        check_facts(app, event.facts.as_ref())?;
    }
    for event in &creates {
        check_action_ids(app, event.id.as_deref(), event.action_ids.as_deref(), event.actions_from_label, false)?;
        check_goal_ids(app, event.goals.as_deref())?;
        check_facts(app, event.facts.as_ref())?;
    }
    let mut patches = Vec::new();
    for update in &updates {
        let patch = to_event(&update.event, update.clear_fields.as_deref().unwrap_or_default())?;
        patches.push(with_repeat(app, patch, update.event.repeat.as_ref())?);
    }
    let creates: Vec<Event> =
        creates.iter().map(|e| with_repeat(app, to_event(e, &[])?, e.repeat.as_ref())).collect::<Result<_, _>>()?;
    let cancels = to_cancels(cancels.unwrap_or_default());
    from_store(change_events(app, "update_event", patches, creates, cancels, allow_compacted_changes, dry_run)?)
}

/// Create new events, as one batch, optionally with actions
/// (action_ids, the first setting its color) and facts. update_event
/// takes creates too, beside updates and cancels, for a change that
/// makes room for them. An event given a repeat is the first of a new
/// recurring series (see PublicEvent); only it is checked for overlaps,
/// not the series' later events.
/// WHERE an event is (facts.location_ids), when you set it: keep a
/// location it already has unless the events and notes around it
/// clearly show the user was somewhere else. Otherwise the user stays
/// put: it's where the previous event was, unless there's evidence they
/// moved in between -- the previous event, or one between, is a commute
/// (an action with commute true, see get_actions, or the commute fact);
/// the user said they were somewhere other than planned; or the event
/// clearly matches another location's hint (see get_locations). After a
/// commute they're somewhere other than before it: tell where from the
/// location of the event after it (that one event only), or else from
/// the locations' hints. A commute itself gets the commute fact and no
/// location_ids.
///
/// GOALS an event is for (goals), when you set them: only goals that
/// are open (inactive or active) and have attribution_hints (see
/// get_goals), and only when the event clearly matches a goal's hints --
/// never guess for a goal without hints, and leave an event untagged
/// when it's unclear (or when the user asks, tag it as they say). Tag
/// the most specific goal that fits, a sub-goal rather than its parent,
/// and when only part of the event was for it, give minutes or percent.
///
/// BATCHES ARE CHECKED WHOLE, AND NOTHING IS MOVED TO MAKE ROOM: every
/// event the call creates or updates must end after it starts and must
/// not overlap any other event -- one on the calendar or another in the
/// call -- so move, shorten or cancel whatever's in the way in the same
/// call. A call that breaks this changes nothing, and the error lists
/// every problem and the events around them, to send a valid call at
/// once.
///
/// An event can't be created to start before the last compaction
/// (get_compaction_status's last_compaction): that's history, unless
/// allow_compacted_changes is set -- ONLY set it when the user has
/// explicitly approved changing history. dry_run checks them and returns
/// what it would do without changing anything. Returns the events created, and a timeline of them beside
/// the events around them.
#[tool(writes)]
pub fn create_event(app: &App, events: Vec<PublicEvent>, #[schema(default = "false")] allow_compacted_changes: bool, #[schema(default = "false")] dry_run: bool) -> Result<EventChangesResult, ToolError> {
    prefetch_for_changes(app)?;
    for event in &events {
        check_action_ids(app, event.id.as_deref(), event.action_ids.as_deref(), event.actions_from_label, false)?;
        check_goal_ids(app, event.goals.as_deref())?;
        check_facts(app, event.facts.as_ref())?;
    }
    let creates: Vec<Event> =
        events.iter().map(|e| with_repeat(app, to_event(e, &[])?, e.repeat.as_ref())).collect::<Result<_, _>>()?;
    from_store(change_events(app, "create_event", Vec::new(), creates, Vec::new(), allow_compacted_changes, dry_run)?)
}

/// Delete (cancel) events, as one batch. Given one event of a recurring
/// series, deletes only that event; to delete the whole series, or an
/// event and the ones after it, use delete_recurrence. Each cancel says,
/// with counts_against_follow_through, whether it counts against
/// follow-through: true records it against the follow-through of each
/// person a follow-through trait part matches it for -- the user and
/// everyone it was planned with (facts' with_ids), or for (for_ids), if
/// it's of the part's action -- as a commitment the user dropped; false
/// is just a change of plan, and counts against no one. An event that
/// started before the last compaction (get_compaction_status's
/// last_compaction) is history, and can't be cancelled unless
/// allow_compacted_changes is set -- ONLY set it when the user has
/// explicitly approved changing history. dry_run returns
/// what it would do without changing anything. Returns the events
/// cancelled.
#[tool(writes)]
pub fn delete_event(app: &App, cancels: Vec<EventCancel>, #[schema(default = "false")] allow_compacted_changes: bool, #[schema(default = "false")] dry_run: bool) -> Result<EventChangesResult, ToolError> {
    prefetch_for_changes(app)?;
    from_store(change_events(app, "delete_event", Vec::new(), Vec::new(), to_cancels(cancels), allow_compacted_changes, dry_run)?)
}

/// Record a new time note -- a timestamp, with an optional
/// description of what it marks. Returns the note as recorded, with the
/// id edit_note/delete_note refer to it by.
#[tool(writes)]
pub fn note(app: &App, noted_time: NotedTime) -> Result<NoteWithId, ToolError> {
    let recorded = StoredNote {
        timestamp: when(&noted_time.timestamp)?,
        description: noted_time.description,
        compaction_id: None,
    };
    from_store(app.noted_time_sheet()?.append(&recorded, noted_time.id.as_deref())?.with_id())
}

/// List the recorded time notes, sorted by timestamp, each with the id
/// edit_note/delete_note refer to it by. Only notes that haven't been
/// compacted yet, unless include_compacted is true. For browsing/review
/// only -- may span many days. To compact notes, use prepare_compaction
/// instead; it returns just the notes for the current round, which is
/// what compact_notes expects.
#[tool]
pub fn get_notes(app: &App, #[schema(default = "false")] include_compacted: bool) -> Result<Vec<NoteWithId>, ToolError> {
    let mut notes = app.noted_time_sheet()?.read_with_rows(include_compacted)?;
    notes.sort_by(|a, b| a.note.timestamp.cmp(&b.note.timestamp));
    from_store(Value::Array(notes.iter().map(|n| n.with_id()).collect()))
}

/// When the user's scheduled routines usually run -- the ones that
/// compact notes into a proposal, and answer the notes left on it: each
/// a time of day ("07:30", 24-hour, in the calendar's time_zone, given
/// too) with an optional label ("Morning compaction"), earliest first.
/// They're hints: nothing runs at them. The user's app fetches what a
/// routine made a while after each, so it has it to show. Read-only.
#[tool]
pub fn get_compaction_schedule_hints(app: &App) -> Result<CompactionScheduleHints, ToolError> {
    let hints = app.compaction_schedule()?.all().map_err(refused)?;
    from_store(json!({ "hints": hints, "time_zone": schedule_time_zone(app)? }))
}

/// Say when the user's scheduled routines run, replacing the hints
/// whole: each a time of day ("HH:MM", 24-hour, in the calendar's time
/// zone -- see get_compaction_schedule_hints) and an optional label
/// ("Morning compaction"). A scheduled routine that compacts notes into a
/// proposal, or answers the notes left on one, should keep these up to
/// date, its own time among them, so the user's app fetches what it made
/// soon after it runs; the app can't change them. Keep the other hints
/// there: get them first, and send them back with yours. Times are
/// unique; an empty list clears them. Returns them as saved.
#[tool(writes)]
pub fn set_compaction_schedule_hints(app: &App, hints: Vec<ScheduleHint>) -> Result<CompactionScheduleHints, ToolError> {
    let saved = app.compaction_schedule()?.set_hints(&hints).map_err(refused)?;
    from_store(json!({ "hints": saved, "time_zone": schedule_time_zone(app)? }))
}

/// Set the calendar's time zone to an IANA name, e.g.
/// "America/New_York" -- the user's own, so that times come back in their
/// local time and days, weeks and months are dated as they live them.
/// Set it when times come back in a zone that isn't the user's (e.g.
/// UTC, with a "Z"), or when they say they've moved or are travelling.
/// Events keep their moments; only how they're shown changes. Returns
/// the zone set.
#[tool(writes)]
pub fn set_time_zone(app: &App, time_zone: String) -> Result<String, ToolError> {
    let zone = app.calendar_client()?.set_time_zone(&time_zone).map_err(refused)?;
    Ok(zone.name().to_string())
}
