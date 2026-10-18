//! Compaction planning: realigning a day's planned events to its notes,
//! from a model's decisions, into calendar changes and a timeline --
//! deterministic, no API access.

use crate::stores::actions::ActionShare;
use crate::event::{parse_iso, Event, Moment, MAX_DESCRIPTION_BYTES};
use crate::facts::Facts;
use crate::google::calendar::python_splitlines;
use crate::py;
use crate::stores::goals::GoalShare;
use crate::stores::event_changes::{overlap_problems, rejection, ChangeError, Placed, Problem};
use crate::timeline::{build_timeline, Timeline, TimelineEvent, TimelineNote};
use serde_json::{json, Map, Value};

/// `CompactionError`: a ChangeError the planner and compactor raise.
pub type CompactionError = ChangeError;

/// A problem as Python's lists mix them: a tagged Problem, or a plain
/// message that takes the error's default category.
#[derive(Debug, Clone)]
pub struct Issue {
    pub category: Option<String>,
    pub message: String,
}

impl Issue {
    pub fn tagged(category: &str, message: impl Into<String>) -> Issue {
        Issue { category: Some(category.into()), message: message.into() }
    }

    pub fn plain(message: impl Into<String>) -> Issue {
        Issue { category: None, message: message.into() }
    }
}

/// `CompactionError.of(problems, category)`.
pub fn error_of(issues: &[Issue], category: &str) -> CompactionError {
    let problems: Vec<Problem> =
        issues.iter().map(|i| Problem::new(i.category.as_deref().unwrap_or(category), i.message.clone())).collect();
    ChangeError::of(&problems, None)
}

pub fn error(message: impl Into<String>, category: &str) -> CompactionError {
    ChangeError::new(message, category)
}

/// `{x!r}` of an optional string.
pub fn repr_opt(text: Option<&str>) -> String {
    text.map(py::repr).unwrap_or_else(|| "None".into())
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct EventDecision {
    pub action: String,
    pub event_id: Option<String>,
    pub summary: Option<String>,
    pub start: Option<Moment>,
    pub end: Option<Moment>,
    pub start_note: Option<String>,
    pub end_note: Option<String>,
    pub into: Option<String>,
    pub annotate: Option<String>,
    pub description: Option<String>,
    pub dropped_notes: Option<Vec<String>>,
    pub location: Option<String>,
    pub priority: Option<i64>,
    pub action_ids: Option<Vec<ActionShare>>,
    pub goals: Option<Vec<GoalShare>>,
    pub facts: Option<Facts>,
    pub counts_against_follow_through: Option<bool>,
    pub key: Option<String>,
    pub allow_history: Option<bool>,
}

impl EventDecision {
    pub fn new(action: &str) -> EventDecision {
        EventDecision { action: action.into(), ..Default::default() }
    }

    /// `to_json_dict`: its set fields, in field order.
    pub fn to_json_dict(&self) -> Value {
        let mut out = Map::new();
        out.insert("action".into(), json!(self.action));
        let mut put = |key: &str, value: Option<Value>| {
            if let Some(v) = value {
                out.insert(key.into(), v);
            }
        };
        put("event_id", self.event_id.clone().map(Value::String));
        put("summary", self.summary.clone().map(Value::String));
        put("start", self.start.map(|m| json!(py::isoformat(&m))));
        put("end", self.end.map(|m| json!(py::isoformat(&m))));
        put("start_note", self.start_note.clone().map(Value::String));
        put("end_note", self.end_note.clone().map(Value::String));
        put("into", self.into.clone().map(Value::String));
        put("annotate", self.annotate.clone().map(Value::String));
        put("description", self.description.clone().map(Value::String));
        put("dropped_notes", self.dropped_notes.clone().map(|d| json!(d)));
        put("location", self.location.clone().map(Value::String));
        put("priority", self.priority.map(|p| json!(p)));
        put("action_ids", self.action_ids.clone().map(|a| json!(a)));
        put("goals", self.goals.as_ref().map(|g| json!(g)));
        // `facts_dict(value)`: None (kept, though: the key is set) for empty ones.
        put("facts", self.facts.as_ref().map(|f| facts_dict(Some(f)).unwrap_or(Value::Null)));
        put("counts_against_follow_through", self.counts_against_follow_through.map(|c| json!(c)));
        put("key", self.key.clone().map(Value::String));
        put("allow_history", self.allow_history.map(|a| json!(a)));
        Value::Object(out)
    }

    pub fn from_json_dict(data: &Value) -> anyhow::Result<EventDecision> {
        let text = |key: &str| data.get(key).and_then(Value::as_str).map(str::to_string);
        let when = |key: &str| -> anyhow::Result<Option<Moment>> {
            match data.get(key).and_then(Value::as_str) {
                Some(t) => Ok(Some(parse_iso(t, None)?)),
                None => Ok(None),
            }
        };
        Ok(EventDecision {
            action: text("action").unwrap_or_default(),
            event_id: text("event_id"),
            summary: text("summary"),
            start: when("start")?,
            end: when("end")?,
            start_note: text("start_note"),
            end_note: text("end_note"),
            into: text("into"),
            annotate: text("annotate"),
            description: text("description"),
            dropped_notes: data.get("dropped_notes").and_then(Value::as_array).map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()),
            location: text("location"),
            priority: data.get("priority").and_then(Value::as_i64),
            action_ids: data.get("action_ids").and_then(Value::as_array).map(|a| a.iter().filter_map(|v| serde_json::from_value(v.clone()).ok()).collect()),
            goals: shares_from(data.get("goals")),
            facts: data.get("facts").map(|f| facts_from_dict(Some(f))),
            counts_against_follow_through: data.get("counts_against_follow_through").and_then(Value::as_bool),
            key: text("key"),
            allow_history: data.get("allow_history").and_then(Value::as_bool),
        })
    }
}

/// Goal shares as a decision or state keeps them; None if not given.
fn shares_from(data: Option<&Value>) -> Option<Vec<GoalShare>> {
    data.filter(|g| !g.is_null()).and_then(|g| serde_json::from_value(g.clone()).ok())
}

/// `facts_dict`: as the JSON object they're stored as; None for none or
/// empty ones.
pub fn facts_dict(facts: Option<&Facts>) -> Option<Value> {
    let facts = facts?;
    if facts.is_empty() {
        return None;
    }
    serde_json::from_str(&facts.to_json()).ok()
}

/// `facts_from_dict`: empty facts for None/null.
pub fn facts_from_dict(data: Option<&Value>) -> Facts {
    match data {
        Some(value) if !value.is_null() => Facts::from_json(&py::json_default(value)).unwrap_or_default(),
        _ => Facts::default(),
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct PlanNote {
    pub id: String,
    pub timestamp: Moment,
    pub description: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Default)]
pub struct EventState {
    pub summary: Option<String>,
    pub start: Option<Moment>,
    pub end: Option<Moment>,
    pub description: Option<String>,
    pub location: Option<String>,
    pub status: Option<String>,
    pub priority: Option<i64>,
    pub event_label_id: Option<String>,
    pub action_ids: Option<Vec<ActionShare>>,
    pub goals: Option<Vec<GoalShare>>,
    pub facts: Option<Value>,
}

impl EventState {
    pub fn from_event(event: &Event) -> EventState {
        EventState {
            summary: event.summary.clone(),
            start: event.start,
            end: event.end,
            description: event.description.clone(),
            location: event.location.clone(),
            status: event.status.clone(),
            priority: event.priority,
            event_label_id: event.event_label_id.clone(),
            action_ids: event.action_ids.clone(),
            goals: event.goals.clone(),
            facts: facts_dict(event.facts.as_ref()),
        }
    }

    pub fn to_event(&self, event_id: Option<&str>) -> Event {
        Event {
            id: event_id.map(str::to_string),
            summary: self.summary.clone(),
            start: self.start,
            end: self.end,
            description: self.description.clone(),
            location: self.location.clone(),
            status: self.status.clone(),
            priority: self.priority,
            event_label_id: self.event_label_id.clone(),
            action_ids: self.action_ids.clone(),
            goals: self.goals.clone(),
            facts: self.facts.as_ref().map(|f| facts_from_dict(Some(f))),
            ..Default::default()
        }
    }

    pub fn to_json_dict(&self) -> Value {
        let mut out = Map::new();
        let mut put = |key: &str, value: Option<Value>| {
            if let Some(v) = value {
                out.insert(key.into(), v);
            }
        };
        put("summary", self.summary.clone().map(Value::String));
        put("start", self.start.map(|m| json!(py::isoformat(&m))));
        put("end", self.end.map(|m| json!(py::isoformat(&m))));
        put("description", self.description.clone().map(Value::String));
        put("location", self.location.clone().map(Value::String));
        put("status", self.status.clone().map(Value::String));
        put("priority", self.priority.map(|p| json!(p)));
        put("event_label_id", self.event_label_id.clone().map(Value::String));
        put("action_ids", self.action_ids.clone().map(|a| json!(a)));
        put("goals", self.goals.as_ref().map(|g| json!(g)));
        put("facts", self.facts.clone());
        Value::Object(out)
    }

    pub fn from_json_dict(data: &Value) -> anyhow::Result<EventState> {
        let text = |key: &str| data.get(key).and_then(Value::as_str).map(str::to_string);
        let when = |key: &str| -> anyhow::Result<Option<Moment>> {
            match data.get(key).and_then(Value::as_str) {
                Some(t) => Ok(Some(parse_iso(t, None)?)),
                None => Ok(None),
            }
        };
        Ok(EventState {
            summary: text("summary"),
            start: when("start")?,
            end: when("end")?,
            description: text("description"),
            location: text("location"),
            status: text("status"),
            priority: data.get("priority").and_then(Value::as_i64),
            event_label_id: text("event_label_id"),
            action_ids: data.get("action_ids").and_then(Value::as_array).map(|a| a.iter().filter_map(|v| serde_json::from_value(v.clone()).ok()).collect()),
            goals: shares_from(data.get("goals")),
            facts: data.get("facts").filter(|f| !f.is_null()).cloned(),
        })
    }

    pub fn start(&self) -> Moment {
        self.start.expect("an event state has a start")
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct CompactionChange {
    pub action: String,
    pub reason: String,
    pub event_id: Option<String>,
    pub key: Option<String>,
    pub before: Option<EventState>,
    pub after: Option<EventState>,
}

#[derive(Debug, Clone, Default)]
pub struct CompactionPlan {
    pub changes: Vec<CompactionChange>,
    pub warnings: Vec<String>,
    pub timeline: Option<Timeline>,
    /// Note id -> (use, the event it's used with), in note order.
    pub note_uses: Vec<(String, (String, Option<String>))>,
    pub note_edges: Vec<(String, String)>,
    pub kept_out: Vec<(String, String)>,
}

#[derive(Debug, Clone)]
struct Fact {
    key: String,
    start: Moment,
    end: Moment,
    event: Event,
    base: Option<Event>,
    reason: String,
    start_note: Option<PlanNote>,
    end_note: Option<PlanNote>,
    default: bool,
    moves_day_end: bool,
    annotations: Vec<String>,
    fixed_description: bool,
    dropped_notes: Option<Vec<String>>,
    created_key: Option<String>,
}

impl Fact {
    fn base_id(&self) -> Option<&str> {
        self.base.as_ref().and_then(|b| b.id.as_deref())
    }
}

#[derive(Default)]
pub struct PlanOptions<'a> {
    pub ignore_notes: Option<&'a [String]>,
    pub day_start: Option<Moment>,
    pub names: Option<&'a [(String, String)]>,
    pub previous_note: Option<&'a PlanNote>,
    pub last_compaction: Option<Moment>,
    pub next_day_follows: bool,
    pub history_until: Option<Moment>,
    pub note_targets: Option<&'a [(String, Option<String>)]>,
    pub closing_id: Option<&'a str>,
    pub nights: Option<&'a [String]>,
}

fn name_of(names: &[(String, String)], id: &str) -> String {
    names.iter().rev().find(|(k, _)| k == id).map(|(_, v)| v.clone()).unwrap_or_else(|| id.to_string())
}

fn valid_notes(notes: &[PlanNote]) -> String {
    let ids: Vec<&str> = notes.iter().map(|n| n.id.as_str()).collect();
    format!("valid note ids: {}", if ids.is_empty() { "(none)".to_string() } else { ids.join(", ") })
}

fn strip(text: Option<&str>) -> String {
    text.unwrap_or("").trim().to_string()
}

/// `plan_compaction`.
pub fn plan_compaction(
    notes: &[PlanNote],
    decisions: &[EventDecision],
    day_events: &[Event],
    now: Moment,
    options: PlanOptions,
) -> Result<CompactionPlan, CompactionError> {
    let ignored: Vec<String> = options.ignore_notes.map(|i| i.to_vec()).unwrap_or_default();
    let note_targets: Vec<(String, Option<String>)> = options.note_targets.map(|t| t.to_vec()).unwrap_or_default();
    let mut problems: Vec<Issue> = Vec::new();
    let mut warnings: Vec<String> = Vec::new();

    let mut ordered: Vec<PlanNote> = notes.to_vec();
    ordered.sort_by_key(|n| n.timestamp);
    let note_by_id = |id: &str| notes.iter().rev().find(|n| n.id == id);
    for note in &ordered {
        if note.timestamp > now {
            problems.push(Issue::tagged("note_after_now", format!("note {} is timestamped after now ({})", note.id, py::isoformat(&now))));
        }
    }
    let mut unknown_ignored: Vec<&String> = ignored.iter().filter(|i| note_by_id(i).is_none()).collect();
    unknown_ignored.sort();
    unknown_ignored.dedup();
    for note_id in unknown_ignored {
        problems.push(Issue::tagged("unknown_note", format!("ignore_notes: {} isn't one of this round's notes; {}", py::repr(note_id), valid_notes(notes))));
    }
    let mut unknown_targets: Vec<&String> = note_targets.iter().map(|(k, _)| k).filter(|i| note_by_id(i).is_none()).collect();
    unknown_targets.sort();
    unknown_targets.dedup();
    for note_id in unknown_targets {
        problems.push(Issue::tagged("unknown_note", format!("annotate_notes: {} isn't one of this round's notes; {}", py::repr(note_id), valid_notes(notes))));
    }
    let mut both: Vec<&String> = note_targets.iter().map(|(k, _)| k).filter(|i| ignored.contains(i)).collect();
    both.sort();
    both.dedup();
    for note_id in both {
        problems.push(Issue::plain(format!("note {note_id} can't be both ignored and added to an event")));
    }

    let mut live: Vec<Event> = day_events.iter().filter(|e| e.id.as_deref().is_some_and(|i| !i.is_empty()) && !e.is_cancelled()).cloned().collect();
    live.sort_by_key(|e| e.start());
    // `{e.id: e}` keeps the last of an id, at the first one's place.
    let mut events_by_id: Vec<(String, Event)> = Vec::new();
    for event in &live {
        let id = event.id.clone().unwrap();
        match events_by_id.iter_mut().find(|(k, _)| *k == id) {
            Some(slot) => slot.1 = event.clone(),
            None => events_by_id.push((id, event.clone())),
        }
    }
    let mut copies: Vec<(String, Event)> = events_by_id.clone();
    let sleepless: Vec<Option<&str>> = decisions.iter().filter(|d| d.action == "cancel").map(|d| d.event_id.as_deref()).collect();
    let closing: Option<Event> = match options.closing_id {
        Some(id) => events_by_id.iter().find(|(k, _)| k == id).map(|(_, e)| e.clone()),
        None => live
            .iter()
            .filter(|e| e.is_end_of_day_sleep == Some(true))
            .find(|e| options.day_start.is_none_or(|d| e.start() > d) && !sleepless.contains(&e.id.as_deref()))
            .cloned(),
    };
    let mut unmergeable: Vec<String> = options.nights.map(|n| n.to_vec()).unwrap_or_default();
    if let Some(closing) = &closing {
        unmergeable.push(closing.id.clone().unwrap());
    }

    let resolved = resolve(decisions, notes, &events_by_id, closing.as_ref(), now, &mut problems, options.history_until, &unmergeable);
    if !problems.is_empty() {
        return Err(error_of(&problems, "malformed_decision"));
    }
    let (mut facts, cancels, merged_into, touched) = resolved;

    let decided_ids: Vec<String> = facts
        .iter()
        .filter_map(|f| f.base_id().map(str::to_string))
        .chain(cancels.iter().map(|(k, _)| k.clone()))
        .chain(touched.iter().map(|(k, _)| k.clone()))
        .collect();
    for event in &live {
        let id = event.id.clone().unwrap();
        if decided_ids.contains(&id) || event.end() > now {
            continue;
        }
        let copy = copies.iter().find(|(k, _)| *k == id).unwrap().1.clone();
        facts.push(Fact {
            key: id.clone(),
            start: copy.start(),
            end: copy.end(),
            event: copy,
            base: Some(event.clone()),
            reason: "happened as planned -- no note says otherwise".into(),
            start_note: None,
            end_note: None,
            default: true,
            moves_day_end: false,
            annotations: Vec::new(),
            fixed_description: false,
            dropped_notes: None,
            created_key: None,
        });
    }
    facts.sort_by_key(|f| (f.start, f.end));

    for (event_id, decision) in &touched {
        let copy = &mut copies.iter_mut().find(|(k, _)| k == event_id).unwrap().1;
        if decision.summary.as_deref().is_some_and(|s| !s.is_empty()) {
            copy.summary = decision.summary.clone();
        }
        if let Some(ids) = &decision.action_ids {
            copy.action_ids = Some(ids.clone());
        }
        if let Some(f) = &decision.facts {
            copy.facts = Some(f.clone());
        }
        set_fields(copy, decision);
    }
    let (annotated, annotated_keys, kept_out) =
        annotate(&ordered, &ignored, &mut facts, &touched, &mut copies, &cancels, &mut warnings, &mut problems, &note_targets);
    if !problems.is_empty() {
        return Err(error_of(&problems, "description_too_long"));
    }
    // A default fact's event is its copy, one object in Python: what was
    // added to the one is the other's too.
    for fact in facts.iter().filter(|f| f.default) {
        if let Some(slot) = copies.iter_mut().find(|(k, _)| *k == fact.key) {
            slot.1 = fact.event.clone();
        }
    }
    let simulated = place(&facts, &copies, &cancels, &mut warnings, options.next_day_follows, options.history_until)?;
    let changes = changes_of(&facts, &events_by_id, &copies, &cancels);
    if changes.is_empty() {
        warnings.push("nothing on the calendar needs to change".into());
    }
    let names: Vec<(String, String)> = options.names.map(|n| n.to_vec()).unwrap_or_default();
    let timeline = timeline_of(&ordered, &ignored, &facts, &live, &copies, &cancels, &merged_into, &annotated, now, &names);
    let timeline = with_context(timeline, options.previous_note, options.last_compaction);
    let _ = simulated;
    let mut edges: Vec<(String, String)> = Vec::new();
    for fact in &facts {
        for note in [&fact.start_note, &fact.end_note].into_iter().flatten() {
            match edges.iter_mut().find(|(k, _)| *k == note.id) {
                Some(slot) => slot.1 = fact.key.clone(),
                None => edges.push((note.id.clone(), fact.key.clone())),
            }
        }
    }
    let mut note_uses: Vec<(String, (String, Option<String>))> = Vec::new();
    for note in &ordered {
        let using = if let Some((_, key)) = annotated_keys.iter().find(|(k, _)| *k == note.id) {
            ("annotates".to_string(), Some(key.clone()))
        } else if let Some((_, key)) = edges.iter().find(|(k, _)| *k == note.id) {
            ("edge".to_string(), Some(key.clone()))
        } else if ignored.contains(&note.id) || kept_out.iter().any(|(k, _)| *k == note.id) {
            ("ignored".to_string(), None)
        } else {
            ("unused".to_string(), None)
        };
        match note_uses.iter_mut().find(|(k, _)| *k == note.id) {
            Some(slot) => slot.1 = using,
            None => note_uses.push((note.id.clone(), using)),
        }
    }
    Ok(CompactionPlan { changes, warnings, timeline: Some(timeline), note_uses, note_edges: edges, kept_out })
}

type Resolved = (Vec<Fact>, Vec<(String, String)>, Vec<(String, String)>, Vec<(String, EventDecision)>);

#[allow(clippy::too_many_arguments)]
fn resolve(
    decisions: &[EventDecision],
    notes: &[PlanNote],
    events_by_id: &[(String, Event)],
    closing: Option<&Event>,
    now: Moment,
    problems: &mut Vec<Issue>,
    history_until: Option<Moment>,
    unmergeable: &[String],
) -> Resolved {
    let mut sorted_ids: Vec<&String> = events_by_id.iter().map(|(k, _)| k).collect();
    sorted_ids.sort();
    let valid_events = format!(
        "valid event ids: {}",
        if sorted_ids.is_empty() { "(none)".to_string() } else { sorted_ids.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", ") }
    );
    let event = |id: &str| events_by_id.iter().find(|(k, _)| k == id).map(|(_, e)| e);
    let note_by_id = |id: &str| notes.iter().rev().find(|n| n.id == id).cloned();

    let edge = |problems: &mut Vec<Issue>, label: &str, explicit: Option<Moment>, note_id: Option<&str>, fallback: Option<Moment>| -> (Option<Moment>, Option<PlanNote>) {
        let mut note = None;
        if let Some(note_id) = note_id {
            note = note_by_id(note_id);
            if note.is_none() {
                problems.push(Issue::tagged(
                    "unknown_note",
                    format!("{label}: {} isn't one of this round's notes; {}", py::repr(note_id), valid_notes(notes)),
                ));
            }
        }
        if let Some(explicit) = explicit {
            return (Some(explicit), note);
        }
        (note.as_ref().map(|n| n.timestamp).or(fallback), note)
    };

    let mut by_event: Vec<(String, EventDecision)> = Vec::new();
    let mut creates: Vec<(String, EventDecision)> = Vec::new();
    for (number, decision) in decisions.iter().enumerate() {
        let label = format!(
            "decision {} ({}{})",
            number + 1,
            decision.action,
            decision.event_id.as_deref().filter(|i| !i.is_empty()).map(|i| format!(" {i}")).unwrap_or_default()
        );
        let moves = decision.start.is_some() || decision.end.is_some() || decision.start_note.is_some() || decision.end_note.is_some();
        if decision.action == "create" {
            if decision.event_id.is_some() || decision.into.is_some() {
                problems.push(Issue::plain(format!("{label}: 'create' takes a summary and times, not event_id or into")));
            } else if strip(decision.summary.as_deref()).is_empty() {
                problems.push(Issue::plain(format!("{label}: 'create' needs a summary")));
            } else {
                creates.push((label, decision.clone()));
            }
            continue;
        }
        if !["keep", "cancel", "merge"].contains(&decision.action.as_str()) {
            problems.push(Issue::plain(format!("{label}: unknown action {}; use keep, cancel, create or merge", py::repr(&decision.action))));
            continue;
        }
        let Some(event_id) = decision.event_id.clone().filter(|i| event(i).is_some()) else {
            problems.push(Issue::tagged(
                "unknown_event",
                format!("{label}: {} isn't one of this day's events; {valid_events}", repr_opt(decision.event_id.as_deref())),
            ));
            continue;
        };
        if by_event.iter().any(|(k, _)| *k == event_id) {
            problems.push(Issue::tagged("duplicate_decision", format!("{label}: event {event_id} has more than one decision")));
            continue;
        }
        let facts_truthy = decision.facts.is_some();
        if decision.action != "keep"
            && (moves
                || decision.summary.as_deref().is_some_and(|s| !s.is_empty())
                || decision.annotate.as_deref().is_some_and(|s| !s.is_empty())
                || facts_truthy
                || decision.description.is_some()
                || decision.location.is_some()
                || decision.priority.is_some())
        {
            problems.push(Issue::plain(format!(
                "{label}: times, notes, summary, annotate, description, location, priority and facts only go with 'keep' or 'create'"
            )));
            continue;
        }
        if decision.action == "merge" {
            if decision.into.as_deref().is_none_or(|i| event(i).is_none()) || decision.into.as_deref() == Some(event_id.as_str()) {
                problems.push(Issue::plain(format!("{label}: 'into' must be the id of another of this day's events; {valid_events}")));
                continue;
            }
            let into = decision.into.clone().unwrap();
            if unmergeable.contains(&event_id) || unmergeable.contains(&into) {
                problems.push(Issue::plain(format!("{label}: the end-of-day sleep event can't be merged")));
                continue;
            }
        } else if decision.into.is_some() {
            problems.push(Issue::plain(format!("{label}: 'into' only goes with 'merge'")));
            continue;
        }
        by_event.push((event_id, decision.clone()));
    }

    let mut merges: Vec<(String, Vec<String>)> = Vec::new();
    for (event_id, decision) in &by_event {
        if decision.action != "merge" {
            continue;
        }
        let into = decision.into.clone().unwrap();
        if let Some((_, target)) = by_event.iter().find(|(k, _)| *k == into) {
            if target.action != "keep" {
                problems.push(Issue::plain(format!(
                    "can't merge {event_id} into {into}: that event is itself being {}",
                    if target.action == "cancel" { "cancelled" } else { "merged" }
                )));
                continue;
            }
        }
        match merges.iter_mut().find(|(k, _)| *k == into) {
            Some(slot) => slot.1.push(event_id.clone()),
            None => merges.push((into, vec![event_id.clone()])),
        }
    }

    let mut facts: Vec<Fact> = Vec::new();
    let mut cancels: Vec<(String, String)> = Vec::new();
    let mut merged_into: Vec<(String, String)> = Vec::new();
    let mut touched: Vec<(String, EventDecision)> = Vec::new();
    let settled = |e: &Event| -> Option<Moment> {
        let history = history_until?;
        if e.start() >= history {
            return None;
        }
        Some(e.end().min(history))
    };

    for (event_id, decision) in &by_event {
        let base = event(event_id).unwrap();
        let compacted = if decision.allow_history == Some(true) { None } else { settled(base) };
        if (decision.action == "cancel" || decision.action == "merge") && compacted.is_some() {
            problems.push(Issue::tagged(
                "compacted",
                format!(
                    "{} ({event_id}) can't be {}: an earlier compaction recorded it happening, until {}. If it ended sooner than planned, end it no earlier than that with 'keep'.",
                    repr_opt(base.summary.as_deref()),
                    if decision.action == "cancel" { "cancelled" } else { "merged away" },
                    py::isoformat(&compacted.unwrap())
                ),
            ));
            continue;
        }
        if decision.action == "cancel" {
            cancels.push((event_id.clone(), "cancelled -- it didn't happen".into()));
        } else if decision.action == "merge" {
            let into = decision.into.clone().unwrap();
            if merges.iter().any(|(k, _)| *k == into) {
                merged_into.push((event_id.clone(), into.clone()));
                cancels.push((event_id.clone(), format!("merged into {}", repr_opt(event(&into).unwrap().summary.as_deref()))));
            }
        }
    }

    let mut keeps: Vec<String> = merges.iter().map(|(k, _)| k.clone()).chain(by_event.iter().filter(|(_, d)| d.action == "keep").map(|(k, _)| k.clone())).collect();
    keeps.sort();
    keeps.dedup();
    for event_id in keeps {
        let base = event(&event_id).unwrap().clone();
        let decision = by_event.iter().find(|(k, _)| *k == event_id).map(|(_, d)| d.clone()).unwrap_or_else(|| EventDecision {
            event_id: Some(event_id.clone()),
            ..EventDecision::new("keep")
        });
        let label = format!("keep {event_id}");
        let (start, start_note) = edge(problems, &label, decision.start, decision.start_note.as_deref(), base.start);
        let (end, end_note) = edge(problems, &label, decision.end, decision.end_note.as_deref(), base.end);
        let (mut start, mut end) = (start.unwrap(), end.unwrap());
        let mut members: Vec<Event> = vec![base.clone()];
        if let Some((_, merged)) = merges.iter().find(|(k, _)| *k == event_id) {
            members.extend(merged.iter().map(|i| event(i).unwrap().clone()));
        }
        for member in &members[1..] {
            start = start.min(member.start());
            // max(): the first of equals, as Python keeps it.
            if member.end() > end {
                end = member.end();
            }
        }
        if end <= start {
            problems.push(Issue::tagged(
                "nonpositive_length",
                format!(
                    "{label}: {} would run from {} to {}, which isn't a positive length",
                    repr_opt(base.summary.as_deref()),
                    py::isoformat(&start),
                    py::isoformat(&end)
                ),
            ));
            continue;
        }
        let compacted = if decision.allow_history == Some(true) { None } else { settled(&base) };
        if compacted.is_some() && start != base.start() {
            problems.push(Issue::tagged(
                "compacted",
                format!(
                    "{label}: {} started at {}, as an earlier compaction recorded -- its start can't move. If something else happened first, give that its own event.",
                    repr_opt(base.summary.as_deref()),
                    py::isoformat(&base.start())
                ),
            ));
            continue;
        }
        if let Some(compacted) = compacted {
            if end < compacted {
                problems.push(Issue::tagged(
                    "compacted",
                    format!(
                        "{label}: {} can't end at {}: an earlier compaction recorded it going on until {}, so it ends then or later.",
                        repr_opt(base.summary.as_deref()),
                        py::isoformat(&end),
                        py::isoformat(&compacted)
                    ),
                ));
                continue;
            }
        }
        let merged = merges.iter().any(|(k, _)| *k == event_id);
        let changed = (start, end) != (base.start(), base.end());
        if !changed && end > now && !merged {
            touched.push((event_id.clone(), decision));
            continue;
        }
        let mut fact_event = base.clone();
        let mut names: Vec<String> = Vec::new();
        for member in &members {
            if let Some(summary) = member.summary.as_deref().filter(|s| !s.is_empty()) {
                if !names.iter().any(|n| n == summary) {
                    names.push(summary.to_string());
                }
            }
        }
        let renamed = strip(decision.summary.as_deref());
        fact_event.summary = if !renamed.is_empty() {
            Some(renamed)
        } else if !names.is_empty() {
            Some(names.join(" and "))
        } else {
            base.summary.clone()
        };
        if let Some(ids) = &decision.action_ids {
            fact_event.action_ids = Some(ids.clone());
        }
        if let Some(f) = &decision.facts {
            fact_event.facts = Some(f.clone());
        }
        set_fields(&mut fact_event, &decision);
        fact_event.start = Some(start);
        fact_event.end = Some(end);
        let reason = if merged {
            format!(
                "merged with {} -- where one ended and the next began isn't remembered",
                members[1..].iter().map(|m| repr_opt(m.summary.as_deref())).collect::<Vec<_>>().join(", ")
            )
        } else if !changed {
            "happened as planned".into()
        } else if start >= now {
            "moved as requested".into()
        } else {
            "realigned to match the notes".into()
        };
        let annotation = strip(decision.annotate.as_deref());
        facts.push(Fact {
            key: event_id.clone(),
            start,
            end,
            event: fact_event,
            base: Some(base.clone()),
            reason,
            start_note,
            end_note,
            default: false,
            moves_day_end: closing.is_some_and(|c| c.id.as_deref() == Some(event_id.as_str())),
            annotations: if annotation.is_empty() { Vec::new() } else { vec![annotation] },
            fixed_description: decision.description.is_some(),
            dropped_notes: decision.dropped_notes.clone(),
            created_key: None,
        });
    }

    for (number, (label, decision)) in creates.iter().enumerate() {
        let (start, start_note) = edge(problems, label, decision.start, decision.start_note.as_deref(), None);
        let (end, end_note) = edge(problems, label, decision.end, decision.end_note.as_deref(), None);
        let (Some(start), Some(end)) = (start, end) else {
            problems.push(Issue::plain(format!(
                "{label}: 'create' needs both a start and an end (a time or a note each; for something still going on, end it at now)"
            )));
            continue;
        };
        if end <= start {
            problems.push(Issue::tagged(
                "nonpositive_length",
                format!(
                    "{label}: {} would run from {} to {}, which isn't a positive length",
                    repr_opt(decision.summary.as_deref()),
                    py::isoformat(&start),
                    py::isoformat(&end)
                ),
            ));
            continue;
        }
        let annotation = strip(decision.annotate.as_deref());
        facts.push(Fact {
            key: decision.key.clone().unwrap_or_else(|| format!("new:{}", number + 1)),
            created_key: decision.key.clone(),
            start,
            end,
            event: Event {
                summary: Some(strip(decision.summary.as_deref())),
                start: Some(start),
                end: Some(end),
                action_ids: decision.action_ids.clone(),
                goals: decision.goals.clone(),
                facts: decision.facts.clone(),
                description: decision.description.clone().filter(|d| !d.is_empty()),
                location: decision.location.clone().filter(|l| !l.is_empty()),
                priority: decision.priority,
                ..Default::default()
            },
            base: None,
            reason: "something the notes show happened that wasn't planned".into(),
            start_note,
            end_note,
            default: false,
            moves_day_end: false,
            annotations: if annotation.is_empty() { Vec::new() } else { vec![annotation] },
            fixed_description: decision.description.is_some(),
            dropped_notes: decision.dropped_notes.clone(),
        });
    }
    (facts, cancels, merged_into, touched)
}

fn set_fields(event: &mut Event, decision: &EventDecision) {
    if let Some(description) = &decision.description {
        event.description = Some(description.clone());
    }
    if let Some(location) = &decision.location {
        event.location = Some(location.clone());
    }
    if let Some(priority) = decision.priority {
        event.priority = Some(priority);
    }
    if let Some(goals) = &decision.goals {
        event.goals = Some(goals.clone());
    }
}

fn hm_in(moment: &Moment, like: &Moment) -> String {
    moment.with_timezone(like.offset()).format("%H:%M").to_string()
}

/// Where a candidate event ends up, for annotating: facts exactly, the
/// rest where they are. (start, end, key, which fact or copy it is.)
#[derive(Clone, Copy)]
enum Slot {
    Fact(usize),
    Copy(usize),
}

#[allow(clippy::too_many_arguments)]
fn annotate(
    ordered: &[PlanNote],
    ignored: &[String],
    facts: &mut [Fact],
    touched: &[(String, EventDecision)],
    copies: &mut [(String, Event)],
    cancels: &[(String, String)],
    warnings: &mut Vec<String>,
    problems: &mut Vec<Issue>,
    note_targets: &[(String, Option<String>)],
) -> (Vec<(String, String)>, Vec<(String, String)>, Vec<(String, String)>) {
    let anchors: Vec<String> =
        facts.iter().flat_map(|f| [&f.start_note, &f.end_note]).flatten().map(|n| n.id.clone()).collect();
    let fact_ids: Vec<String> = facts.iter().filter_map(|f| f.base_id().map(str::to_string)).collect();
    let mut targets: Vec<(Moment, Moment, String, Slot)> =
        facts.iter().enumerate().map(|(i, f)| (f.start, f.end, f.key.clone(), Slot::Fact(i))).collect();
    for (i, (event_id, e)) in copies.iter().enumerate() {
        if !fact_ids.contains(event_id) && !cancels.iter().any(|(k, _)| k == event_id) {
            targets.push((e.start(), e.end(), event_id.clone(), Slot::Copy(i)));
        }
    }
    let by_key = |key: &str| targets.iter().rev().find(|t| t.2 == key).cloned();
    let mut edge_of: Vec<(String, String)> = Vec::new();
    for fact in facts.iter() {
        for note in [&fact.start_note, &fact.end_note].into_iter().flatten() {
            match edge_of.iter_mut().find(|(k, _)| *k == note.id) {
                Some(slot) => slot.1 = fact.key.clone(),
                None => edge_of.push((note.id.clone(), fact.key.clone())),
            }
        }
    }
    let event_at = |facts: &[Fact], copies: &[(String, Event)], slot: Slot| -> Event {
        match slot {
            Slot::Fact(i) => facts[i].event.clone(),
            Slot::Copy(i) => copies[i].1.clone(),
        }
    };
    let mut lines: Vec<(String, Vec<(Option<Moment>, String)>)> = Vec::new();
    let mut slots: Vec<(String, Slot)> = Vec::new();
    let mut note_ids: Vec<(String, Vec<String>)> = Vec::new();
    let mut annotated: Vec<(String, String)> = Vec::new();
    let mut annotated_keys: Vec<(String, String)> = Vec::new();
    let mut dropped_for: Vec<(String, Option<Vec<String>>)> =
        facts.iter().filter(|f| f.fixed_description).map(|f| (f.key.clone(), f.dropped_notes.clone())).collect();
    for (event_id, decision) in touched {
        if decision.description.is_some() {
            match dropped_for.iter_mut().find(|(k, _)| k == event_id) {
                Some(slot) => slot.1 = decision.dropped_notes.clone(),
                None => dropped_for.push((event_id.clone(), decision.dropped_notes.clone())),
            }
        }
    }
    let fixed: Vec<String> = dropped_for.iter().map(|(k, _)| k.clone()).collect();
    let mut kept_out: Vec<(String, String)> = Vec::new();
    let add_line = |lines: &mut Vec<(String, Vec<(Option<Moment>, String)>)>, key: &str, entry: (Option<Moment>, String)| {
        match lines.iter_mut().find(|(k, _)| k == key) {
            Some(slot) => slot.1.push(entry),
            None => lines.push((key.to_string(), vec![entry])),
        }
    };
    let set_slot = |slots: &mut Vec<(String, Slot)>, key: &str, slot: Slot| match slots.iter_mut().find(|(k, _)| k == key) {
        Some(existing) => existing.1 = slot,
        None => slots.push((key.to_string(), slot)),
    };
    let put = |map: &mut Vec<(String, String)>, k: &str, v: String| match map.iter_mut().find(|(key, _)| key == k) {
        Some(slot) => slot.1 = v,
        None => map.push((k.to_string(), v)),
    };
    for note in ordered {
        let text = strip(note.description.as_deref());
        let targeted = note_targets.iter().rev().find(|(k, _)| *k == note.id);
        let mut target = targeted.and_then(|(_, t)| t.clone());
        if (anchors.contains(&note.id) && targeted.is_none()) || ignored.contains(&note.id) || text.is_empty() {
            continue;
        }
        if target.is_none() && anchors.contains(&note.id) {
            target = edge_of.iter().find(|(k, _)| *k == note.id).map(|(_, v)| v.clone());
        }
        let mut hit = None;
        if let Some(target) = &target {
            hit = by_key(target);
            if hit.is_none() && cancels.iter().any(|(k, _)| k == target) {
                warnings.push(format!(
                    "note {} ({}) was to go with {target}, which is cancelled, so it was added where it falls instead",
                    note.id,
                    py::repr(&text)
                ));
            } else if hit.is_none() {
                problems.push(Issue::tagged(
                    "unknown_event",
                    format!("note {} ({}) can't go with {}: it isn't one of this day's events", note.id, py::repr(&text), py::repr(target)),
                ));
                continue;
            }
        }
        let hit = hit
            .or_else(|| targets.iter().find(|t| t.0 <= note.timestamp && note.timestamp < t.1).cloned())
            .or_else(|| targets.iter().find(|t| t.0 < note.timestamp && note.timestamp <= t.1).cloned());
        let Some((_, _, key, slot)) = hit else {
            warnings.push(format!(
                "note {} ({}) doesn't fall within any event, so its text wasn't added anywhere",
                note.id,
                py::repr(&text)
            ));
            continue;
        };
        let hit_event = event_at(facts, copies, slot);
        if fixed.contains(&key) {
            let line = format!("- {} {text}", hm_in(&note.timestamp, &hit_event.start()));
            if python_splitlines(hit_event.description.as_deref().unwrap_or("")).contains(&line) {
                put(&mut annotated, &note.id, hit_event.summary.clone().filter(|s| !s.is_empty()).unwrap_or(key.clone()));
                put(&mut annotated_keys, &note.id, key.clone());
                continue;
            }
            let dropped = &dropped_for.iter().find(|(k, _)| *k == key).unwrap().1;
            if dropped.as_ref().is_none_or(|d| d.contains(&note.id)) {
                put(&mut kept_out, &note.id, key.clone());
                continue;
            }
        }
        add_line(&mut lines, &key, (Some(note.timestamp), text.clone()));
        set_slot(&mut slots, &key, slot);
        match note_ids.iter_mut().find(|(k, _)| *k == key) {
            Some(entry) => entry.1.push(note.id.clone()),
            None => note_ids.push((key.clone(), vec![note.id.clone()])),
        }
        put(&mut annotated, &note.id, hit_event.summary.clone().filter(|s| !s.is_empty()).unwrap_or(key.clone()));
        put(&mut annotated_keys, &note.id, key.clone());
    }
    for (i, fact) in facts.iter().enumerate() {
        if fixed.contains(&fact.key) {
            continue;
        }
        for text in &fact.annotations {
            add_line(&mut lines, &fact.key, (None, text.clone()));
            set_slot(&mut slots, &fact.key, Slot::Fact(i));
        }
    }
    for (event_id, decision) in touched {
        let annotation = strip(decision.annotate.as_deref());
        if !annotation.is_empty() && !fixed.contains(event_id) {
            add_line(&mut lines, event_id, (None, annotation));
            let index = copies.iter().position(|(k, _)| k == event_id).unwrap();
            set_slot(&mut slots, event_id, Slot::Copy(index));
        }
    }

    for (key, entries) in &lines {
        let slot = slots.iter().find(|(k, _)| k == key).unwrap().1;
        let event = match slot {
            Slot::Fact(i) => &mut facts[i].event,
            Slot::Copy(i) => &mut copies[i].1,
        };
        let mut timed: Vec<&(Option<Moment>, String)> = entries.iter().filter(|e| e.0.is_some()).collect();
        timed.sort_by_key(|e| e.0.unwrap());
        let start = event.start();
        let mut formatted: Vec<String> = timed.iter().map(|(m, text)| format!("- {} {text}", hm_in(&m.unwrap(), &start))).collect();
        formatted.extend(entries.iter().filter(|e| e.0.is_none()).map(|(_, text)| format!("- {text}")));
        let existing = python_splitlines(event.description.as_deref().unwrap_or(""));
        let formatted: Vec<String> = formatted.into_iter().filter(|l| !existing.contains(l)).collect();
        if formatted.is_empty() {
            continue;
        }
        let description = event.description.clone().unwrap_or_default();
        let prefix = if existing.iter().any(|l| l == "Notes:") {
            format!("{description}\n")
        } else if !description.is_empty() {
            format!("{description}\n\nNotes:\n")
        } else {
            "Notes:\n".to_string()
        };
        event.description = Some(prefix + &formatted.join("\n"));
        let size = event.description.as_ref().unwrap().len();
        if size > MAX_DESCRIPTION_BYTES {
            let mut fixes: Vec<String> = if entries.iter().any(|e| e.0.is_none()) { vec!["shorten its `annotate`".into()] } else { Vec::new() };
            if let Some((_, ids)) = note_ids.iter().find(|(k, _)| k == key).filter(|(_, ids)| !ids.is_empty()) {
                fixes.insert(0, format!("leave notes out with ignore_notes ({})", ids.join(", ")));
            }
            let name = event.summary.clone().filter(|s| !s.is_empty()).unwrap_or(key.clone());
            problems.push(Issue::plain(format!(
                "the description of {} would be {size} bytes with these notes added, over the {MAX_DESCRIPTION_BYTES} Calendar keeps (it silently cuts the rest){}",
                py::repr(&name),
                if fixes.is_empty() { String::new() } else { format!("; {}", fixes.join(" or ")) }
            )));
        }
    }
    for key in &fixed {
        let event = match by_key(key) {
            Some((_, _, _, slot)) => Some(event_at(facts, copies, slot)),
            None => copies.iter().find(|(k, _)| k == key).map(|(_, e)| e.clone()),
        };
        if let Some(event) = event {
            let size = event.description.as_deref().unwrap_or("").len();
            if size > MAX_DESCRIPTION_BYTES {
                let name = event.summary.clone().filter(|s| !s.is_empty()).unwrap_or(key.clone());
                problems.push(Issue::plain(format!(
                    "the description of {} is {size} bytes, over the {MAX_DESCRIPTION_BYTES} Calendar keeps (it silently cuts the rest): shorten it",
                    py::repr(&name)
                )));
            }
        }
    }
    (annotated, annotated_keys, kept_out)
}

fn place(
    facts: &[Fact],
    copies: &[(String, Event)],
    cancels: &[(String, String)],
    warnings: &mut Vec<String>,
    next_day_follows: bool,
    history_until: Option<Moment>,
) -> Result<Vec<Event>, CompactionError> {
    let decided: Vec<&str> = facts.iter().filter(|f| !f.default).filter_map(|f| f.base_id()).collect();
    let mut placed: Vec<Placed> =
        facts.iter().filter(|f| !f.default).map(|f| Placed { event: f.event.clone(), key: f.key.clone(), moved: true, asked: true }).collect();
    for (event_id, copy) in copies {
        if !decided.contains(&event_id.as_str()) && !cancels.iter().any(|(k, _)| k == event_id) {
            placed.push(Placed { event: copy.clone(), key: event_id.clone(), moved: false, asked: false });
        }
    }
    let problems = overlap_problems(&placed, None);
    if !problems.is_empty() {
        let since = placed.iter().map(|p| p.event.start()).min().unwrap();
        let until = placed.iter().map(|p| p.event.end()).reduce(|a, b| if b > a { b } else { a }).unwrap();
        let refusal = rejection(&problems, &placed, &since, &until, history_until.as_ref());
        let mut wrapped = ChangeError::of(&problems, Some(refusal.message));
        if problems.is_empty() {
            wrapped.categories = vec!["overlap".into()];
        }
        return Err(wrapped);
    }
    for fact in facts {
        if let Some(base) = &fact.base {
            if fact.moves_day_end && fact.end != base.end() && !next_day_follows {
                warnings.push(format!(
                    "{} now ends at {} instead of {}. Compaction doesn't adjust the next day for that -- check the next day's first events, and move them with update_event if they now overlap.",
                    repr_opt(fact.event.summary.as_deref()),
                    py::isoformat(&fact.end),
                    py::isoformat(&base.end())
                ));
            }
        }
    }
    Ok(placed.into_iter().map(|p| p.event).collect())
}

fn changes_of(facts: &[Fact], events_by_id: &[(String, Event)], copies: &[(String, Event)], cancels: &[(String, String)]) -> Vec<CompactionChange> {
    let mut changes: Vec<CompactionChange> = Vec::new();
    let decided = |id: &str| facts.iter().rev().find(|f| !f.default && f.base_id() == Some(id));
    let defaults: Vec<&str> = facts.iter().filter(|f| f.default).filter_map(|f| f.base_id()).collect();
    for (event_id, original) in events_by_id {
        let before = EventState::from_event(original);
        if let Some((_, reason)) = cancels.iter().find(|(k, _)| k == event_id) {
            changes.push(CompactionChange {
                action: "cancel".into(),
                reason: reason.clone(),
                event_id: Some(event_id.clone()),
                key: None,
                before: Some(before),
                after: None,
            });
            continue;
        }
        let (after, reason) = if let Some(fact) = decided(event_id) {
            (EventState::from_event(&fact.event), fact.reason.clone())
        } else {
            let after = EventState::from_event(&copies.iter().find(|(k, _)| k == event_id).unwrap().1);
            let only_notes = EventState { description: before.description.clone(), ..after.clone() } == before;
            let only_actions = EventState { description: before.description.clone(), action_ids: before.action_ids.clone(), ..after.clone() } == before;
            let only_facts = EventState {
                description: before.description.clone(),
                action_ids: before.action_ids.clone(),
                goals: before.goals.clone(),
                facts: before.facts.clone(),
                ..after.clone()
            } == before;
            let reason = if defaults.contains(&event_id.as_str()) {
                facts.iter().find(|f| f.default && f.base_id() == Some(event_id.as_str())).unwrap().reason.clone()
            } else if only_notes {
                "added the notes that fall during it".into()
            } else if only_actions {
                "set what was done at it (its actions)".into()
            } else if only_facts {
                "recorded what happened at it (its facts)".into()
            } else {
                "updated".into()
            };
            (after, reason)
        };
        if after != before {
            changes.push(CompactionChange { action: "update".into(), reason, event_id: Some(event_id.clone()), key: None, before: Some(before), after: Some(after) });
        }
    }
    for fact in facts {
        if fact.base.is_none() {
            changes.push(CompactionChange {
                action: "create".into(),
                reason: fact.reason.clone(),
                event_id: None,
                key: fact.created_key.clone(),
                before: None,
                after: Some(EventState::from_event(&fact.event)),
            });
        }
    }
    let order = |a: &str| match a {
        "cancel" => 0,
        "update" => 1,
        _ => 2,
    };
    changes.sort_by(|a, b| {
        let start = |c: &CompactionChange| c.after.as_ref().or(c.before.as_ref()).unwrap().start();
        (order(&a.action), start(a), a.event_id.clone().unwrap_or_default()).cmp(&(order(&b.action), start(b), b.event_id.clone().unwrap_or_default()))
    });
    changes
}

/// `facts_lines`: where, who with and who for in one, then each person's note.
pub fn facts_lines(facts: Option<&Facts>, names: &[(String, String)]) -> Vec<String> {
    let Some(facts) = facts.filter(|f| !f.is_empty()) else { return Vec::new() };
    let named = |ids: &[String]| ids.iter().map(|i| name_of(names, i)).collect::<Vec<_>>().join(", ");
    let mut parts: Vec<String> = Vec::new();
    if facts.is_commute() {
        parts.push("commute".into());
    }
    let locations = facts.locations();
    if !locations.is_empty() {
        parts.push(format!("@ {}", locations.iter().map(|l| name_of(names, l)).collect::<Vec<_>>().join(", ")));
    }
    if crate::stores::groups::truthy(facts.with_ids.as_ref()) {
        parts.push(format!("with {}", named(&facts.with())));
    }
    if crate::stores::groups::truthy(facts.for_ids.as_ref()) {
        parts.push(format!("for {}", named(&facts.for_())));
    }
    let mut lines: Vec<String> = if parts.is_empty() { Vec::new() } else { vec![parts.join(" · ")] };
    if let Some(Value::Object(notes)) = &facts.notes {
        for (person, note) in notes {
            lines.push(format!("{}: {}", name_of(names, person), crate::stores::groups::python_str(Some(note))));
        }
    }
    lines
}

#[allow(clippy::too_many_arguments)]
fn timeline_of(
    ordered: &[PlanNote],
    ignored: &[String],
    facts: &[Fact],
    live: &[Event],
    copies: &[(String, Event)],
    cancels: &[(String, String)],
    merged_into: &[(String, String)],
    annotated: &[(String, String)],
    now: Moment,
    names: &[(String, String)],
) -> Timeline {
    let named = |ids: Option<&Vec<ActionShare>>| -> Vec<String> { ids.into_iter().flatten().map(|a| name_of(names, &a.action_id)).collect() };
    let fact_fields = |final_: &Event, original: Option<&Event>| -> (Vec<String>, bool) {
        let said = facts_lines(final_.facts.as_ref(), names);
        let changed = !said.is_empty() && original.is_none_or(|o| facts_dict(final_.facts.as_ref()) != facts_dict(o.facts.as_ref()));
        (said, changed)
    };
    let missing = |final_: &Event, start: Moment| -> Vec<String> {
        if start >= now {
            return Vec::new();
        }
        let mut gaps: Vec<String> = if final_.action_ids.as_ref().is_some_and(|a| !a.is_empty()) { Vec::new() } else { vec!["action".into()] };
        // A commute is between places, not at one.
        if !final_.facts.as_ref().is_some_and(|f| !f.locations().is_empty() || f.is_commute()) {
            gaps.push("location".into());
        }
        gaps
    };
    let decided = |id: &str| facts.iter().rev().find(|f| !f.default && f.base_id() == Some(id));
    let defaults: Vec<&str> = facts.iter().filter(|f| f.default).filter_map(|f| f.base_id()).collect();
    let mut events: Vec<TimelineEvent> = Vec::new();
    for original in live {
        let id = original.id.clone().unwrap();
        if cancels.iter().any(|(k, _)| *k == id) {
            let target = merged_into.iter().find(|(k, _)| *k == id).and_then(|(_, into)| decided(into));
            events.push(TimelineEvent {
                summary: original.summary.clone().filter(|s| !s.is_empty()).unwrap_or(id.clone()),
                status: if target.is_some() { "merged" } else { "cancelled" }.into(),
                merged_into: target.and_then(|t| t.event.summary.clone()),
                event_id: Some(id.clone()),
                planned_start: original.start,
                planned_end: original.end,
                ..Default::default()
            });
            continue;
        }
        let fact = decided(&id);
        let final_ = match fact {
            Some(f) => f.event.clone(),
            None => copies.iter().find(|(k, _)| *k == id).unwrap().1.clone(),
        };
        if fact.is_none() && original.start() >= now && EventState::from_event(&final_) == EventState::from_event(original) {
            continue;
        }
        let same = (final_.start, final_.end) == (original.start, original.end);
        let status = if fact.is_some() {
            if same {
                if final_.end() <= now { "on_schedule" } else { "planned" }
            } else {
                "adjusted"
            }
        } else if defaults.contains(&id.as_str()) {
            "on_schedule"
        } else {
            "planned"
        };
        let actions = named(final_.action_ids.as_ref());
        let before_actions = named(original.action_ids.as_ref());
        let (facts_said, new_facts) = fact_fields(&final_, Some(original));
        events.push(TimelineEvent {
            summary: final_.summary.clone().filter(|s| !s.is_empty()).unwrap_or(id.clone()),
            status: status.into(),
            start: final_.start,
            end: final_.end,
            start_note: fact.and_then(|f| f.start_note.as_ref().map(|n| n.id.clone())),
            end_note: fact.and_then(|f| f.end_note.as_ref().map(|n| n.id.clone())),
            new_actions: actions.iter().filter(|n| !before_actions.contains(n)).cloned().collect(),
            actions,
            facts: facts_said,
            new_facts,
            missing: missing(&final_, final_.start()),
            event_id: Some(id.clone()),
            planned_start: original.start,
            planned_end: original.end,
            ..Default::default()
        });
    }
    for fact in facts {
        if fact.base.is_none() {
            let actions = named(fact.event.action_ids.as_ref());
            let (facts_said, new_facts) = fact_fields(&fact.event, None);
            events.push(TimelineEvent {
                summary: fact.event.summary.clone().unwrap_or_default(),
                status: "new".into(),
                event_id: fact.created_key.clone(),
                start: Some(fact.start),
                end: Some(fact.end),
                start_note: fact.start_note.as_ref().map(|n| n.id.clone()),
                end_note: fact.end_note.as_ref().map(|n| n.id.clone()),
                new_actions: actions.clone(),
                actions,
                facts: facts_said,
                new_facts,
                missing: missing(&fact.event, fact.start),
                ..Default::default()
            });
        }
    }
    let mut anchors: Vec<(String, Vec<String>)> = Vec::new();
    let mut add = |id: &str, text: String| match anchors.iter_mut().find(|(k, _)| k == id) {
        Some(slot) => slot.1.push(text),
        None => anchors.push((id.to_string(), vec![text])),
    };
    for fact in facts {
        let summary = fact.event.summary.clone().unwrap_or_else(|| "None".into());
        if let Some(note) = &fact.start_note {
            add(&note.id, format!("start of {summary}"));
        }
        if let Some(note) = &fact.end_note {
            add(&note.id, format!("end of {summary}"));
        }
    }
    let notes: Vec<TimelineNote> = ordered
        .iter()
        .map(|note| TimelineNote {
            id: note.id.clone(),
            time: Some(note.timestamp),
            text: note.description.clone(),
            anchors: anchors.iter().find(|(k, _)| *k == note.id).map(|(_, v)| v.clone()).unwrap_or_default(),
            annotates: annotated.iter().find(|(k, _)| *k == note.id).map(|(_, v)| v.clone()),
            ignored: ignored.contains(&note.id),
            compacted: false,
        })
        .collect();
    build_timeline(notes, events, Some(now), true, None)
}

pub fn with_context(timeline: Timeline, previous_note: Option<&PlanNote>, last_compaction: Option<Moment>) -> Timeline {
    let mut notes = timeline.notes.clone();
    if let Some(previous) = previous_note {
        notes.push(TimelineNote {
            id: previous.id.clone(),
            time: Some(previous.timestamp),
            text: previous.description.clone(),
            compacted: true,
            ..Default::default()
        });
    }
    build_timeline(notes, timeline.events, timeline.now, timeline.decided, last_compaction)
}

/// The timeline before anything is decided.
pub fn planned_timeline(
    notes: &[PlanNote],
    day_events: &[Event],
    now: Moment,
    names: &[(String, String)],
    suggested: &[(String, Vec<String>)],
    previous_note: Option<&PlanNote>,
    last_compaction: Option<Moment>,
) -> Timeline {
    let timeline_notes: Vec<TimelineNote> =
        notes.iter().map(|n| TimelineNote { id: n.id.clone(), time: Some(n.timestamp), text: n.description.clone(), ..Default::default() }).collect();
    let events: Vec<TimelineEvent> = day_events
        .iter()
        .filter(|e| e.id.as_deref().is_some_and(|i| !i.is_empty()) && !e.is_cancelled())
        .map(|e| {
            let id = e.id.clone().unwrap();
            TimelineEvent {
                summary: e.summary.clone().filter(|s| !s.is_empty()).unwrap_or(id.clone()),
                status: "planned".into(),
                event_id: Some(id.clone()),
                start: e.start,
                end: e.end,
                planned_start: e.start,
                planned_end: e.end,
                actions: e.action_ids.iter().flatten().map(|a| name_of(names, &a.action_id)).collect(),
                new_actions: suggested.iter().find(|(k, _)| *k == id).map(|(_, v)| v.iter().map(|a| name_of(names, a)).collect()).unwrap_or_default(),
                facts: facts_lines(e.facts.as_ref(), names),
                ..Default::default()
            }
        })
        .collect();
    let timeline = build_timeline(timeline_notes, events, Some(now), false, None);
    with_context(timeline, previous_note, last_compaction)
}

#[cfg(test)]
#[path = "plan_tests.rs"]
mod tests;
