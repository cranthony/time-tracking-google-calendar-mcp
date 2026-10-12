//! Compaction proposals (utilities/compaction_proposals.py): the user's
//! edits and feedback as they're kept, the shapes the tools hand back, and
//! how a revision's decisions are made from Claude's and the user's
//! (`merge`).

use super::additions::REF_PREFIX;
use super::plan::{CompactionChange, EventDecision};
use crate::event::Moment;
use crate::facts::Facts;
use crate::py;
use crate::timeline::Timeline;
use serde_json::{json, Map, Value};

pub const EDIT_FIELDS: [&str; 11] =
    ["summary", "start", "end", "start_note", "end_note", "annotate", "action_ids", "facts", "description", "location", "priority"];

fn when(moment: &Option<Moment>) -> Value {
    moment.map(|m| json!(py::pydantic_datetime(&m))).unwrap_or(Value::Null)
}

pub fn new_proposal_id(hex_id: &str) -> String {
    hex_id.chars().take(12).collect()
}

pub fn revision_id(proposal: &str, revision: i64) -> String {
    format!("{proposal}r{revision}")
}

pub fn claude_key(proposal: &str, number: i64) -> String {
    format!("{proposal}c{number}")
}

pub fn edit_id(proposal: &str, seq: i64) -> String {
    format!("{proposal}u{seq}")
}

pub fn feedback_id(proposal: &str, seq: i64) -> String {
    format!("{proposal}f{seq}")
}

pub fn is_note_id(name: Option<&str>) -> bool {
    name.is_some_and(|n| n.contains('#'))
}

pub fn is_ref(name: Option<&str>) -> bool {
    name.is_some_and(|n| n.starts_with(REF_PREFIX))
}

fn digits(text: &str) -> Option<i64> {
    if !text.is_empty() && text.chars().all(|c| c.is_ascii_digit()) {
        text.parse().ok()
    } else {
        None
    }
}

pub fn is_key(proposal: &str, name: Option<&str>) -> bool {
    let Some(rest) = name.and_then(|n| n.strip_prefix(proposal)) else { return false };
    let mut chars = rest.chars();
    matches!(chars.next(), Some('c') | Some('u')) && digits(chars.as_str()).is_some()
}

pub fn claude_key_number(proposal: &str, name: Option<&str>) -> Option<i64> {
    name.and_then(|n| n.strip_prefix(proposal)).and_then(|r| r.strip_prefix('c')).and_then(digits)
}

pub fn split_feedback_id(name: &str) -> Option<(String, i64)> {
    let chars: Vec<char> = name.chars().collect();
    if chars.len() < 14 || chars[12] != 'f' {
        return None;
    }
    let proposal: String = chars[..12].iter().collect();
    if !proposal.chars().all(|c| c.is_ascii_digit() || ('a'..='f').contains(&c)) {
        return None;
    }
    let seq = digits(&chars[13..].iter().collect::<String>())?;
    Some((proposal, seq))
}

#[derive(Debug, Clone, PartialEq)]
pub struct UserEdit {
    pub id: String,
    pub seq: i64,
    pub event_id: Option<String>,
    pub edit: Map<String, Value>,
    pub status: String,
    pub created: Moment,
    pub base_revision: i64,
}

impl UserEdit {
    pub fn action(&self) -> Option<&str> {
        self.edit.get("action").and_then(Value::as_str)
    }

    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("id".into(), json!(self.id));
        out.insert("seq".into(), json!(self.seq));
        out.insert("event_id".into(), json!(self.event_id));
        out.insert("edit".into(), Value::Object(self.edit.clone()));
        out.insert("status".into(), json!(self.status));
        out.insert("created".into(), json!(py::pydantic_datetime(&self.created)));
        out.insert("base_revision".into(), json!(self.base_revision));
        Value::Object(out)
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct Feedback {
    pub id: String,
    pub seq: i64,
    pub text: String,
    pub event_id: Option<String>,
    pub note_id: Option<String>,
    pub at: Option<Moment>,
    pub by: String,
    pub created: Moment,
    pub status: String,
    pub reply: Option<String>,
    pub answered_in: Option<i64>,
    pub superseded_by: Option<Vec<String>>,
}

impl Feedback {
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("id".into(), json!(self.id));
        out.insert("seq".into(), json!(self.seq));
        out.insert("text".into(), json!(self.text));
        out.insert("event_id".into(), json!(self.event_id));
        out.insert("note_id".into(), json!(self.note_id));
        out.insert("at".into(), when(&self.at));
        out.insert("by".into(), json!(self.by));
        out.insert("created".into(), json!(py::pydantic_datetime(&self.created)));
        out.insert("status".into(), json!(self.status));
        out.insert("reply".into(), json!(self.reply));
        out.insert("answered_in".into(), json!(self.answered_in));
        out.insert("superseded_by".into(), json!(self.superseded_by));
        Value::Object(out)
    }
}

#[derive(Debug, Clone)]
pub struct NoteEdit {
    pub note_id: String,
    pub use_: String,
    pub event_id: Option<String>,
}

impl NoteEdit {
    pub fn from_value(value: &Value) -> NoteEdit {
        NoteEdit {
            note_id: value["note_id"].as_str().unwrap_or_default().into(),
            use_: value["use"].as_str().unwrap_or_default().into(),
            event_id: value.get("event_id").and_then(Value::as_str).map(str::to_string),
        }
    }

    pub fn edit(&self) -> Map<String, Value> {
        let mut out = Map::new();
        out.insert("action".into(), json!("note"));
        out.insert("use".into(), json!(self.use_));
        if let Some(event_id) = &self.event_id {
            out.insert("event_id".into(), json!(event_id));
        }
        out
    }
}

#[derive(Debug, Clone)]
pub struct AdditionChoice {
    pub ref_: String,
    pub use_: String,
    pub id: Option<String>,
    pub name: Option<String>,
    pub context: Option<String>,
    pub hint: Option<String>,
}

impl AdditionChoice {
    pub fn from_value(value: &Value) -> AdditionChoice {
        let text = |key: &str| value.get(key).and_then(Value::as_str).map(str::to_string);
        AdditionChoice {
            ref_: text("ref").unwrap_or_default(),
            use_: text("use").unwrap_or_default(),
            id: text("id"),
            name: text("name"),
            context: text("context"),
            hint: text("hint"),
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct AdditionSettled {
    pub ref_: String,
    pub use_: String,
    pub id: Option<String>,
}

impl AdditionSettled {
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("ref".into(), json!(self.ref_));
        out.insert("use".into(), json!(self.use_));
        out.insert("id".into(), json!(self.id));
        Value::Object(out)
    }
}

#[derive(Debug, Clone)]
pub struct FeedbackReply {
    pub feedback_id: String,
    pub reply: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct RecapPoint {
    pub text: String,
    pub event_ids: Vec<String>,
    pub note_ids: Vec<String>,
}

#[derive(Debug, Clone, PartialEq)]
pub struct Recap {
    pub overview: String,
    pub did: Vec<RecapPoint>,
    pub unsure: Vec<RecapPoint>,
}

fn strings(value: Option<&Value>) -> Vec<String> {
    value.and_then(Value::as_array).map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()).unwrap_or_default()
}

impl Recap {
    pub fn to_json_dict(&self) -> Value {
        let point = |p: &RecapPoint| -> Value {
            let mut out = Map::new();
            out.insert("text".into(), json!(p.text));
            if !p.event_ids.is_empty() {
                out.insert("event_ids".into(), json!(p.event_ids));
            }
            if !p.note_ids.is_empty() {
                out.insert("note_ids".into(), json!(p.note_ids));
            }
            Value::Object(out)
        };
        let mut out = Map::new();
        out.insert("overview".into(), json!(self.overview));
        out.insert("did".into(), Value::Array(self.did.iter().map(point).collect()));
        out.insert("unsure".into(), Value::Array(self.unsure.iter().map(point).collect()));
        Value::Object(out)
    }

    /// As pydantic gives it: every field.
    pub fn to_json(&self) -> Value {
        let point = |p: &RecapPoint| json!({"text": p.text, "event_ids": p.event_ids, "note_ids": p.note_ids});
        json!({
            "overview": self.overview,
            "did": self.did.iter().map(point).collect::<Vec<_>>(),
            "unsure": self.unsure.iter().map(point).collect::<Vec<_>>(),
        })
    }

    pub fn from_json_dict(data: &Value) -> Recap {
        let points = |key: &str| -> Vec<RecapPoint> {
            data.get(key)
                .and_then(Value::as_array)
                .map(|items| {
                    items
                        .iter()
                        .map(|p| RecapPoint {
                            text: p["text"].as_str().unwrap_or_default().into(),
                            event_ids: strings(p.get("event_ids")),
                            note_ids: strings(p.get("note_ids")),
                        })
                        .collect()
                })
                .unwrap_or_default()
        };
        Recap { overview: data.get("overview").and_then(Value::as_str).unwrap_or_default().into(), did: points("did"), unsure: points("unsure") }
    }

    pub fn resolved(&self, refs: &[(String, String)]) -> Recap {
        let point = |p: &RecapPoint| RecapPoint {
            event_ids: p.event_ids.iter().map(|e| refs.iter().rev().find(|(k, _)| k == e).map(|(_, v)| v.clone()).unwrap_or(e.clone())).collect(),
            ..p.clone()
        };
        Recap { overview: self.overview.clone(), did: self.did.iter().map(point).collect(), unsure: self.unsure.iter().map(point).collect() }
    }

    pub fn problems(&self, events: &[String], notes: &[String]) -> Vec<String> {
        let mut problems = Vec::new();
        if self.overview.trim().is_empty() {
            problems.push("recap: the overview is empty".to_string());
        }
        if self.did.is_empty() {
            problems.push("recap: `did` is empty -- say what you did, even if it's recording every event as on schedule".into());
        }
        for (kind, points) in [("did", &self.did), ("unsure", &self.unsure)] {
            for (i, p) in points.iter().enumerate() {
                let place = format!("recap.{kind}[{i}]");
                if p.text.trim().is_empty() {
                    problems.push(format!("{place}: its text is empty"));
                }
                for e in &p.event_ids {
                    if !events.contains(e) {
                        problems.push(format!("{place}: {} isn't one of the proposal's events", py::repr(e)));
                    }
                }
                for n in &p.note_ids {
                    if !notes.contains(n) {
                        problems.push(format!("{place}: {} isn't one of the proposal's notes", py::repr(n)));
                    }
                }
            }
        }
        problems
    }
}

#[derive(Debug, Clone, Default)]
pub struct ProposalEvent {
    pub id: String,
    pub summary: Option<String>,
    pub start: Option<Moment>,
    pub end: Option<Moment>,
    pub status: String,
    pub planned_start: Option<Moment>,
    pub planned_end: Option<Moment>,
    pub description: Option<String>,
    pub location: Option<String>,
    pub priority: Option<i64>,
    pub action_ids: Option<Vec<String>>,
    pub facts: Option<Facts>,
    pub decided_by: Option<String>,
    pub history_until: Option<Moment>,
    pub is_end_of_day_sleep: bool,
    pub counts_against_follow_through: Option<bool>,
    pub follow_through: Vec<String>,
}

impl ProposalEvent {
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("id".into(), json!(self.id));
        out.insert("summary".into(), json!(self.summary));
        out.insert("start".into(), when(&self.start));
        out.insert("end".into(), when(&self.end));
        out.insert("status".into(), json!(self.status));
        out.insert("planned_start".into(), when(&self.planned_start));
        out.insert("planned_end".into(), when(&self.planned_end));
        out.insert("description".into(), json!(self.description));
        out.insert("location".into(), json!(self.location));
        out.insert("priority".into(), json!(self.priority));
        out.insert("action_ids".into(), json!(self.action_ids));
        out.insert("facts".into(), self.facts.as_ref().map(Facts::to_public).unwrap_or(Value::Null));
        out.insert("decided_by".into(), json!(self.decided_by));
        out.insert("history_until".into(), when(&self.history_until));
        out.insert("is_end_of_day_sleep".into(), json!(self.is_end_of_day_sleep));
        out.insert("counts_against_follow_through".into(), json!(self.counts_against_follow_through));
        out.insert("follow_through".into(), json!(self.follow_through));
        Value::Object(out)
    }
}

#[derive(Debug, Clone)]
pub struct ProposalNote {
    pub id: String,
    pub timestamp: Moment,
    pub description: Option<String>,
    pub use_: String,
    pub event_id: Option<String>,
    pub edge_of: Option<String>,
    pub decided_by: Option<String>,
}

impl ProposalNote {
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("id".into(), json!(self.id));
        out.insert("timestamp".into(), json!(py::pydantic_datetime(&self.timestamp)));
        out.insert("description".into(), json!(self.description));
        out.insert("use".into(), json!(self.use_));
        out.insert("event_id".into(), json!(self.event_id));
        out.insert("edge_of".into(), json!(self.edge_of));
        out.insert("decided_by".into(), json!(self.decided_by));
        Value::Object(out)
    }
}

/// A CompactionChange as pydantic gives it: each state with every field.
pub fn change_json(change: &CompactionChange) -> Value {
    let state = |s: &Option<super::plan::EventState>| -> Value {
        match s {
            None => Value::Null,
            Some(s) => {
                let mut out = Map::new();
                out.insert("summary".into(), json!(s.summary));
                out.insert("start".into(), when(&s.start));
                out.insert("end".into(), when(&s.end));
                out.insert("description".into(), json!(s.description));
                out.insert("location".into(), json!(s.location));
                out.insert("status".into(), json!(s.status));
                out.insert("priority".into(), json!(s.priority));
                out.insert("event_label_id".into(), json!(s.event_label_id));
                out.insert("action_ids".into(), json!(s.action_ids));
                out.insert("facts".into(), s.facts.clone().unwrap_or(Value::Null));
                Value::Object(out)
            }
        }
    };
    let mut out = Map::new();
    out.insert("action".into(), json!(change.action));
    out.insert("reason".into(), json!(change.reason));
    out.insert("event_id".into(), json!(change.event_id));
    out.insert("key".into(), json!(change.key));
    out.insert("before".into(), state(&change.before));
    out.insert("after".into(), state(&change.after));
    Value::Object(out)
}

#[derive(Debug, Clone)]
pub struct Proposal {
    pub id: String,
    pub revision: i64,
    pub state: String,
    pub window_start: Moment,
    pub through: Moment,
    pub by: String,
    pub reason: String,
    pub created: Moment,
    pub claude_through: Option<Moment>,
    pub events: Option<Vec<ProposalEvent>>,
    pub problem: Option<String>,
    pub notes: Vec<ProposalNote>,
    pub changes: Vec<CompactionChange>,
    pub warnings: Vec<String>,
    pub timeline: Option<Timeline>,
    pub additions: Option<Map<String, Value>>,
    pub settled_additions: Vec<AdditionSettled>,
    pub user_edits: Vec<UserEdit>,
    pub feedback: Vec<Feedback>,
    pub recap: Option<Recap>,
    pub recap_revision: Option<i64>,
    pub changed_since: Option<Vec<String>>,
    pub replaced: Option<Vec<String>>,
}

impl Proposal {
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("id".into(), json!(self.id));
        out.insert("revision".into(), json!(self.revision));
        out.insert("state".into(), json!(self.state));
        out.insert("window_start".into(), json!(py::pydantic_datetime(&self.window_start)));
        out.insert("through".into(), json!(py::pydantic_datetime(&self.through)));
        out.insert("by".into(), json!(self.by));
        out.insert("reason".into(), json!(self.reason));
        out.insert("created".into(), json!(py::pydantic_datetime(&self.created)));
        out.insert("claude_through".into(), when(&self.claude_through));
        out.insert(
            "events".into(),
            self.events.as_ref().map(|e| Value::Array(e.iter().map(ProposalEvent::to_json).collect())).unwrap_or(Value::Null),
        );
        out.insert("problem".into(), json!(self.problem));
        out.insert("notes".into(), Value::Array(self.notes.iter().map(ProposalNote::to_json).collect()));
        out.insert("changes".into(), Value::Array(self.changes.iter().map(change_json).collect()));
        out.insert("warnings".into(), json!(self.warnings));
        out.insert("timeline".into(), self.timeline.as_ref().map(Timeline::to_json).unwrap_or(Value::Null));
        out.insert("additions".into(), self.additions.clone().map(Value::Object).unwrap_or(Value::Null));
        out.insert("settled_additions".into(), Value::Array(self.settled_additions.iter().map(AdditionSettled::to_json).collect()));
        out.insert("user_edits".into(), Value::Array(self.user_edits.iter().map(UserEdit::to_json).collect()));
        out.insert("feedback".into(), Value::Array(self.feedback.iter().map(Feedback::to_json).collect()));
        out.insert("recap".into(), self.recap.as_ref().map(Recap::to_json).unwrap_or(Value::Null));
        out.insert("recap_revision".into(), json!(self.recap_revision));
        out.insert("changed_since".into(), json!(self.changed_since));
        out.insert("replaced".into(), json!(self.replaced));
        Value::Object(out)
    }
}

pub struct ProposalSummary {
    pub id: String,
    pub revision: i64,
    pub state: String,
    pub window_start: Moment,
    pub through: Moment,
    pub open_feedback: i64,
}

impl ProposalSummary {
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("id".into(), json!(self.id));
        out.insert("revision".into(), json!(self.revision));
        out.insert("state".into(), json!(self.state));
        out.insert("window_start".into(), json!(py::pydantic_datetime(&self.window_start)));
        out.insert("through".into(), json!(py::pydantic_datetime(&self.through)));
        out.insert("open_feedback".into(), json!(self.open_feedback));
        Value::Object(out)
    }
}

pub struct ProposalResult {
    pub status: String,
    pub message: String,
    pub proposal: Option<Proposal>,
}

impl ProposalResult {
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("status".into(), json!(self.status));
        out.insert("message".into(), json!(self.message));
        out.insert("proposal".into(), self.proposal.as_ref().map(Proposal::to_json).unwrap_or(Value::Null));
        Value::Object(out)
    }
}

pub struct ProposalContext {
    pub id: String,
    pub revision: i64,
    pub state: String,
    pub user_seq: i64,
    pub updates: Vec<Value>,
    pub creates: Vec<Value>,
    pub cancels: Vec<Value>,
    pub ignore_notes: Vec<String>,
    pub annotate_notes: Vec<Value>,
    pub new_actions: Vec<Value>,
    pub new_people: Vec<Value>,
    pub new_locations: Vec<Value>,
    pub settled_additions: Vec<AdditionSettled>,
    pub user_edits: Vec<UserEdit>,
    pub feedback: Vec<Feedback>,
    pub recap: Option<Recap>,
}

impl ProposalContext {
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("id".into(), json!(self.id));
        out.insert("revision".into(), json!(self.revision));
        out.insert("state".into(), json!(self.state));
        out.insert("user_seq".into(), json!(self.user_seq));
        out.insert("updates".into(), json!(self.updates));
        out.insert("creates".into(), json!(self.creates));
        out.insert("cancels".into(), json!(self.cancels));
        out.insert("ignore_notes".into(), json!(self.ignore_notes));
        out.insert("annotate_notes".into(), json!(self.annotate_notes));
        out.insert("new_actions".into(), json!(self.new_actions));
        out.insert("new_people".into(), json!(self.new_people));
        out.insert("new_locations".into(), json!(self.new_locations));
        out.insert("settled_additions".into(), Value::Array(self.settled_additions.iter().map(AdditionSettled::to_json).collect()));
        out.insert("user_edits".into(), Value::Array(self.user_edits.iter().map(UserEdit::to_json).collect()));
        out.insert("feedback".into(), Value::Array(self.feedback.iter().map(Feedback::to_json).collect()));
        out.insert("recap".into(), self.recap.as_ref().map(Recap::to_json).unwrap_or(Value::Null));
        Value::Object(out)
    }
}

/// An ordered dict, as Python's: assigning a key that's there keeps its
/// place; one that isn't goes at the end.
#[derive(Debug, Clone, Default)]
pub struct Dict<V> {
    pub items: Vec<(String, V)>,
}

impl<V: Clone> Dict<V> {
    pub fn new() -> Dict<V> {
        Dict { items: Vec::new() }
    }

    pub fn get(&self, key: &str) -> Option<&V> {
        self.items.iter().find(|(k, _)| k == key).map(|(_, v)| v)
    }

    pub fn contains(&self, key: &str) -> bool {
        self.items.iter().any(|(k, _)| k == key)
    }

    pub fn set(&mut self, key: &str, value: V) {
        match self.items.iter_mut().find(|(k, _)| k == key) {
            Some(slot) => slot.1 = value,
            None => self.items.push((key.to_string(), value)),
        }
    }

    pub fn pop(&mut self, key: &str) -> Option<V> {
        let index = self.items.iter().position(|(k, _)| k == key)?;
        Some(self.items.remove(index).1)
    }

    pub fn len(&self) -> usize {
        self.items.len()
    }

    pub fn is_empty(&self) -> bool {
        self.items.is_empty()
    }

    pub fn keys(&self) -> impl Iterator<Item = &String> {
        self.items.iter().map(|(k, _)| k)
    }

    pub fn values(&self) -> impl Iterator<Item = &V> {
        self.items.iter().map(|(_, v)| v)
    }
}

#[derive(Debug, Clone)]
pub struct Merged {
    pub decisions: Vec<EventDecision>,
    pub decided_by: Dict<String>,
    pub unknown: Vec<UserEdit>,
    pub ignore_notes: Vec<String>,
    pub note_targets: Dict<Option<String>>,
    pub notes_decided_by: Dict<String>,
    pub settled: Dict<AdditionSettled>,
}

/// `edit` as a decision (its `as_planned` as a bare keep).
pub fn edit_decision(edit: &UserEdit) -> EventDecision {
    let mut data = edit.edit.clone();
    let action = data.shift_remove("action").and_then(|a| a.as_str().map(str::to_string)).unwrap_or_default();
    data.insert("action".into(), json!(if action == "as_planned" { "keep" } else { action.as_str() }));
    let mut decision = EventDecision::from_json_dict(&Value::Object(data)).unwrap_or_else(|_| EventDecision::new("keep"));
    if action == "create" {
        decision.key = Some(edit.id.clone());
    }
    decision
}

#[derive(Default)]
pub struct MergeOptions<'a> {
    pub known_ids: Option<&'a [String]>,
    pub aliases: &'a [(String, String)],
    pub settled: &'a [String],
    pub claude_ignore: &'a [String],
    pub claude_targets: &'a [(String, String)],
    pub known_notes: Option<&'a [String]>,
}

fn alias(aliases: &[(String, String)], name: &str) -> String {
    aliases.iter().rev().find(|(k, _)| k == name).map(|(_, v)| v.clone()).unwrap_or_else(|| name.to_string())
}

/// `merge`: the decisions a revision is planned with -- Claude's, with
/// every active edit on top, in order -- and what it does with the notes.
pub fn merge(proposal: &str, claude: &[EventDecision], edits: &[UserEdit], options: MergeOptions) -> Merged {
    let aliases = options.aliases;
    let mut claude_targets: Dict<String> = Dict::new();
    for (n, e) in options.claude_targets {
        claude_targets.set(n, alias(aliases, e));
    }
    let mut ignore: Vec<String> = Vec::new();
    for n in options.claude_ignore {
        if !ignore.contains(n) {
            ignore.push(n.clone());
        }
    }
    let mut targets: Dict<Option<String>> = Dict::new();
    for (n, e) in &claude_targets.items {
        targets.set(n, Some(e.clone()));
    }
    let mut notes_by: Dict<String> = Dict::new();
    for n in ignore.iter().chain(targets.keys()) {
        notes_by.set(n, "claude".into());
    }
    let mut keyed: Dict<EventDecision> = Dict::new();
    let mut decided_by: Dict<String> = Dict::new();
    for decision in claude {
        let named = if decision.action == "create" { decision.key.clone() } else { decision.event_id.clone() };
        let mut name = named.filter(|n| !n.is_empty()).unwrap_or_else(|| format!("#{}", keyed.len()));
        if keyed.contains(&name) {
            name = format!("{name}#{}", keyed.len());
        }
        keyed.set(&name, decision.clone());
        decided_by.set(&name, "claude".into());
    }
    let mut unknown: Vec<UserEdit> = Vec::new();
    let mut note_edits: Vec<UserEdit> = Vec::new();
    let mut settled_refs: Dict<AdditionSettled> = Dict::new();
    let mut described: Dict<i64> = Dict::new();
    let unknown_target = |keyed: &Dict<EventDecision>, target: Option<&str>| -> bool {
        let creating = target.and_then(|t| keyed.get(t)).is_some_and(|d| d.action == "create");
        !creating && (is_key(proposal, target) || options.known_ids.is_some_and(|known| target.is_none_or(|t| !known.iter().any(|k| k == t))))
    };
    let mut active: Vec<&UserEdit> = edits.iter().filter(|e| e.status == "active").collect();
    active.sort_by_key(|e| e.seq);
    for edit in active {
        let action = edit.action().unwrap_or_default();
        if action == "note" || (action == "as_planned" && is_note_id(edit.event_id.as_deref())) {
            note_edits.push(edit.clone());
            continue;
        }
        if action == "addition" {
            let r = edit.event_id.clone().unwrap_or_default();
            settled_refs.set(
                &r,
                AdditionSettled {
                    ref_: r.clone(),
                    use_: edit.edit.get("use").and_then(Value::as_str).unwrap_or_default().into(),
                    id: edit.edit.get("id").and_then(Value::as_str).map(str::to_string),
                },
            );
            continue;
        }
        if action == "as_planned" && is_ref(edit.event_id.as_deref()) {
            settled_refs.pop(edit.event_id.as_deref().unwrap());
            continue;
        }
        if action == "create" {
            keyed.set(&edit.id, edit_decision(edit));
            decided_by.set(&edit.id, "user".into());
            continue;
        }
        let target = edit.event_id.as_deref().map(|e| alias(aliases, e));
        if target.as_ref().is_some_and(|t| options.settled.contains(t)) {
            continue;
        }
        let existing = target.as_deref().and_then(|t| keyed.get(t)).cloned();
        let creating = existing.as_ref().is_some_and(|d| d.action == "create");
        if !creating && unknown_target(&keyed, target.as_deref()) {
            unknown.push(edit.clone());
            continue;
        }
        let target = target.unwrap_or_default();
        if action == "as_planned" {
            keyed.pop(&target);
            decided_by.pop(&target);
            continue;
        }
        if action == "cancel" {
            if creating {
                keyed.pop(&target);
                decided_by.pop(&target);
            } else {
                keyed.set(
                    &target,
                    EventDecision {
                        event_id: Some(target.clone()),
                        counts_against_follow_through: edit.edit.get("counts_against_follow_through").and_then(Value::as_bool),
                        allow_history: edit.edit.get("allow_history").and_then(Value::as_bool),
                        ..EventDecision::new("cancel")
                    },
                );
                decided_by.set(&target, "user".into());
            }
            continue;
        }
        let given = edit_decision(edit);
        if given.description.is_some() {
            described.set(&target, edit.seq);
        }
        match existing.filter(|d| d.action == "keep" || d.action == "create") {
            Some(existing) => keyed.set(&target, overlay(&existing, &given)),
            None => keyed.set(&target, EventDecision { event_id: Some(target.clone()), ..given }),
        }
        decided_by.set(&target, "user".into());
    }

    for edit in note_edits {
        let note = edit.event_id.clone().unwrap_or_default();
        if options.known_notes.is_some_and(|known| !known.contains(&note)) {
            unknown.push(edit);
            continue;
        }
        if edit.action() == Some("as_planned") {
            ignore.retain(|n| *n != note);
            targets.pop(&note);
            notes_by.pop(&note);
            if options.claude_ignore.contains(&note) && !ignore.contains(&note) {
                ignore.push(note.clone());
            }
            if let Some(target) = claude_targets.get(&note) {
                targets.set(&note, Some(target.clone()));
            }
            if ignore.contains(&note) || targets.contains(&note) {
                notes_by.set(&note, "claude".into());
            }
            continue;
        }
        if edit.edit.get("use").and_then(Value::as_str) == Some("ignore") {
            if !ignore.contains(&note) {
                ignore.push(note.clone());
            }
            targets.pop(&note);
        } else {
            ignore.retain(|n| *n != note);
            for (name, decision) in keyed.items.iter_mut() {
                let dropped = decision.dropped_notes.clone().unwrap_or_default();
                if dropped.contains(&note) && described.get(name).copied().unwrap_or(edit.seq) < edit.seq {
                    decision.dropped_notes = Some(dropped.into_iter().filter(|n| *n != note).collect());
                }
            }
            match edit.edit.get("event_id").and_then(Value::as_str) {
                None => targets.set(&note, None),
                Some(target) => {
                    let target = alias(aliases, target);
                    if unknown_target(&keyed, Some(&target)) {
                        unknown.push(edit);
                        continue;
                    }
                    targets.set(&note, Some(target));
                }
            }
        }
        notes_by.set(&note, "user".into());
    }
    let creates: Vec<String> = keyed.items.iter().filter(|(_, d)| d.action == "create").map(|(k, _)| k.clone()).collect();
    targets.items.retain(|(_, target)| !(is_key(proposal, target.as_deref()) && !creates.contains(target.as_ref().unwrap())));
    let mut ignore_notes = ignore;
    ignore_notes.sort();
    Merged {
        decisions: keyed.items.into_iter().map(|(_, d)| d).collect(),
        decided_by,
        unknown,
        ignore_notes,
        note_targets: targets,
        notes_decided_by: notes_by,
        settled: settled_refs,
    }
}

/// `_overlay`: `under` with every field `over` sets set to its value.
pub fn overlay(under: &EventDecision, over: &EventDecision) -> EventDecision {
    let mut out = under.clone();
    if over.summary.is_some() {
        out.summary = over.summary.clone();
    }
    if over.start.is_some() {
        out.start = over.start;
    }
    if over.end.is_some() {
        out.end = over.end;
    }
    if over.start_note.is_some() {
        out.start_note = over.start_note.clone();
    }
    if over.end_note.is_some() {
        out.end_note = over.end_note.clone();
    }
    if over.annotate.is_some() {
        out.annotate = over.annotate.clone();
    }
    if over.action_ids.is_some() {
        out.action_ids = over.action_ids.clone();
    }
    if over.facts.is_some() {
        out.facts = over.facts.clone();
    }
    if over.description.is_some() {
        out.description = over.description.clone();
    }
    if over.location.is_some() {
        out.location = over.location.clone();
    }
    if over.priority.is_some() {
        out.priority = over.priority;
    }
    if over.start.is_some() && over.start_note.is_none() {
        out.start_note = None;
    }
    if over.start_note.is_some() && over.start.is_none() {
        out.start = None;
    }
    if over.end.is_some() && over.end_note.is_none() {
        out.end_note = None;
    }
    if over.end_note.is_some() && over.end.is_none() {
        out.end = None;
    }
    if over.allow_history == Some(true) {
        out.allow_history = Some(true);
    }
    if over.description.is_some() {
        out.annotate = None;
        out.dropped_notes = over.dropped_notes.clone();
    }
    out
}

/// `decision_shape`: which of compact_notes' lists `decision` goes in, and
/// as what.
pub fn decision_shape(decision: &EventDecision) -> (&'static str, Value) {
    let mut data = match decision.to_json_dict() {
        Value::Object(map) => map,
        _ => Map::new(),
    };
    let action = data.shift_remove("action").and_then(|a| a.as_str().map(str::to_string)).unwrap_or_default();
    data.shift_remove("allow_history");
    if action == "create" {
        return ("creates", Value::Object(data));
    }
    if action == "cancel" {
        let mut out = Map::new();
        out.insert("event_id".into(), json!(decision.event_id));
        out.insert("counts_against_follow_through".into(), json!(decision.counts_against_follow_through != Some(false)));
        return ("cancels", Value::Object(out));
    }
    data.shift_remove("into");
    ("updates", Value::Object(data))
}

/// `settle_refs`: `decision` with every ref the user settled replaced by
/// its id -- or, dropped, left out -- in its actions and facts.
pub fn settle_refs(decision: &EventDecision, settled: &Dict<AdditionSettled>) -> EventDecision {
    if settled.is_empty() {
        return decision.clone();
    }
    let id_of = |v: &str| -> Option<String> {
        match settled.get(v) {
            Some(choice) => choice.id.clone(),
            None => Some(v.to_string()),
        }
    };
    let named = |values: &[String]| -> Vec<String> {
        let mut out: Vec<String> = Vec::new();
        for v in values {
            if let Some(id) = id_of(v) {
                if !out.contains(&id) {
                    out.push(id);
                }
            }
        }
        out
    };
    let named_value = |value: &Option<Value>| -> Option<Value> {
        match value {
            None => None,
            Some(Value::Array(items)) => {
                let strings: Vec<String> = items.iter().filter_map(|i| i.as_str().map(str::to_string)).collect();
                Some(json!(named(&strings)))
            }
            Some(other) => Some(other.clone()),
        }
    };
    let mut out = decision.clone();
    if let Some(ids) = &decision.action_ids {
        out.action_ids = Some(named(ids));
    }
    if let Some(facts) = &decision.facts {
        let location = match &facts.location_id {
            Some(Value::String(l)) if !l.is_empty() => id_of(l).map(Value::String),
            other => other.clone(),
        };
        let notes = match &facts.notes {
            Some(Value::Object(map)) => {
                let mut kept = Map::new();
                for (person, note) in map {
                    if let Some(target) = id_of(person) {
                        if !kept.contains_key(&target) {
                            kept.insert(target.clone(), map.get(&target).cloned().unwrap_or(note.clone()));
                        }
                    }
                }
                Some(Value::Object(kept))
            }
            other => other.clone(),
        };
        out.facts = Some(Facts { location_id: location, with_ids: named_value(&facts.with_ids), for_ids: named_value(&facts.for_ids), notes });
    }
    out
}

#[cfg(test)]
#[path = "proposals_tests.rs"]
mod tests;
