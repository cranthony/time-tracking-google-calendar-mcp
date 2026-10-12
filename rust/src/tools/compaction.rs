//! The compaction tools (server.py's): compacting notes into the calendar
//! as proposals the user confirms, judging the events it settles, and
//! correcting notes.

use super::events::moment;
use super::{ToolError, ToolResult};
use crate::app::App;
use crate::compaction::additions::Additions;
use crate::compaction::compactor::{compaction_error, NoteCompactor};
use crate::compaction::judgments::{Judging, Judgment};
use crate::compaction::plan::EventDecision;
use crate::compaction::proposals::{AdditionChoice, FeedbackReply, NoteEdit, Recap, RecapPoint};
use crate::facts::Facts;
use crate::py;
use crate::stores::action_calendar::ActionCalendar;
use serde_json::{json, Map, Value};

/// `get_note_compactor`: the compactor over the server's stores, built in
/// the order Python builds them.
fn compactor(app: &App) -> anyhow::Result<NoteCompactor> {
    let calendar = ActionCalendar::new(app.calendar_client()?, app.actions()?);
    let client = ActionCalendar::new(app.calendar_client()?, app.actions()?);
    let actions = app.actions()?;
    let people = app.people()?;
    let locations = app.locations()?;
    let notes = app.noted_time_sheet()?;
    let journal = app.journal()?;
    let judging = Judging {
        client: ActionCalendar::new(app.calendar_client()?, app.actions()?),
        actions: actions.clone(),
        people: people.clone(),
        locations: locations.clone(),
        traits: app.traits()?,
        habits: Some(app.habits()?),
    };
    let cancellations = app.cancellations()?;
    Ok(NoteCompactor {
        calendar,
        client,
        notes,
        journal,
        actions: Some(actions),
        people: Some(people),
        locations: Some(locations),
        judging: Some(judging),
        cancellations: Some(cancellations),
    })
}

/// `_rejected`: a CompactionError the client reads (logged by its
/// categories); anything else stays unexpected.
fn rejected(tool: &str, error: anyhow::Error) -> ToolError {
    match compaction_error(&error) {
        Some(refusal) => {
            tracing::warn!(
                "compaction rejected: tool={tool} categories={}: {}",
                refusal.categories.join(","),
                refusal.message.lines().collect::<Vec<_>>().join(" | ")
            );
            ToolError::Message(refusal.message.clone())
        }
        None => ToolError::Unexpected(error),
    }
}

fn text(value: &Value, key: &str) -> Option<String> {
    value.get(key).and_then(Value::as_str).map(str::to_string)
}

fn strings(value: Option<&Value>) -> Option<Vec<String>> {
    value.filter(|v| !v.is_null()).and_then(Value::as_array).map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect())
}

fn list<'a>(args: &'a Map<String, Value>, key: &str) -> Vec<&'a Value> {
    args.get(key).and_then(Value::as_array).map(|a| a.iter().collect()).unwrap_or_default()
}

fn facts_of(value: &Value) -> Option<Facts> {
    value.get("facts").filter(|f| !f.is_null()).map(Facts::from_public)
}

/// A compact_notes update or create, as the decision it makes.
fn decision_of(value: &Value, action: &str) -> Result<EventDecision, ToolError> {
    Ok(EventDecision {
        action: action.into(),
        event_id: if action == "keep" { text(value, "event_id") } else { None },
        summary: text(value, "summary"),
        start: moment(value.get("start"))?,
        end: moment(value.get("end"))?,
        start_note: text(value, "start_note"),
        end_note: text(value, "end_note"),
        annotate: text(value, "annotate"),
        action_ids: strings(value.get("action_ids")),
        facts: facts_of(value),
        key: if action == "create" { text(value, "key") } else { None },
        ..Default::default()
    })
}

fn cancel_of(value: &Value) -> EventDecision {
    EventDecision {
        event_id: text(value, "event_id"),
        counts_against_follow_through: value.get("counts_against_follow_through").and_then(Value::as_bool),
        ..EventDecision::new("cancel")
    }
}

fn recap_of(value: &Value) -> Recap {
    let points = |key: &str| -> Vec<RecapPoint> {
        value
            .get(key)
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .map(|p| RecapPoint {
                        text: text(p, "text").unwrap_or_default(),
                        event_ids: strings(p.get("event_ids")).unwrap_or_default(),
                        note_ids: strings(p.get("note_ids")).unwrap_or_default(),
                    })
                    .collect()
            })
            .unwrap_or_default()
    };
    Recap { overview: text(value, "overview").unwrap_or_default(), did: points("did"), unsure: points("unsure") }
}

pub fn get_compaction_status(app: &App, _args: &Map<String, Value>) -> ToolResult {
    let compactor = compactor(app)?;
    compactor.prefetch()?;
    let (_notes, latest) = app.noted_time_sheet()?.read_with_latest_compacted()?;
    let due = match compactor.judgments_due(None, false) {
        Ok(due) => due,
        Err(e) if compaction_error(&e).is_some() => None,
        Err(e) => return Err(e.into()),
    };
    let mut out = Map::new();
    out.insert("last_compaction".into(), json!(app.journal()?.last_stamped_now()?.map(|m| py::pydantic_datetime(&m))));
    out.insert("latest_compacted_note".into(), latest.map(|n| n.to_json()).unwrap_or(Value::Null));
    out.insert("judgments_pending".into(), json!(due.filter(|d| !d.events.is_empty()).map(|d| d.compaction_id)));
    out.insert("proposal".into(), compactor.proposal_summary()?.map(|p| p.to_json()).unwrap_or(Value::Null));
    Ok(Value::Object(out))
}

pub fn edit_note(app: &App, args: &Map<String, Value>) -> ToolResult {
    let compactor = compactor(app)?;
    let timestamp = moment(args.get("timestamp"))?;
    let description = args.get("description").and_then(Value::as_str);
    match compactor.edit_note(super::args_str(args, "note_id"), timestamp, description) {
        Ok(note) => Ok(note.with_id()),
        Err(e) => Err(rejected("edit_note", e)),
    }
}

pub fn delete_note(app: &App, args: &Map<String, Value>) -> ToolResult {
    let compactor = compactor(app)?;
    match compactor.delete_note(super::args_str(args, "note_id")) {
        Ok(note) => Ok(note.to_json()),
        Err(e) => Err(rejected("delete_note", e)),
    }
}

pub fn prepare_compaction(app: &App, _args: &Map<String, Value>) -> ToolResult {
    Ok(compactor(app)?.prepare()?.to_json())
}

pub fn compact_notes(app: &App, args: &Map<String, Value>) -> ToolResult {
    let compactor = compactor(app)?;
    let mut decisions: Vec<EventDecision> = Vec::new();
    for u in list(args, "updates") {
        decisions.push(decision_of(u, "keep")?);
    }
    for c in list(args, "creates") {
        decisions.push(decision_of(c, "create")?);
    }
    decisions.extend(list(args, "cancels").into_iter().map(cancel_of));
    let values = |key: &str| -> Vec<Value> { list(args, key).into_iter().cloned().collect() };
    let additions = Additions::new(&values("new_actions"), &values("new_people"), &values("new_locations"));
    let replies: Vec<FeedbackReply> = list(args, "replies")
        .into_iter()
        .map(|r| FeedbackReply { feedback_id: text(r, "feedback_id").unwrap_or_default(), reply: text(r, "reply").unwrap_or_default() })
        .collect();
    let annotate: Vec<(String, String)> = list(args, "annotate_notes")
        .into_iter()
        .map(|a| (text(a, "note_id").unwrap_or_default(), text(a, "event_id").unwrap_or_default()))
        .collect();
    let create_refs: Vec<Option<String>> = list(args, "creates").into_iter().map(|c| text(c, "ref")).collect();
    let result = compactor.dry_run(
        decisions,
        strings(args.get("ignore_notes")),
        additions,
        args.get("proposal_id").and_then(Value::as_str).map(str::to_string),
        args.get("revision").and_then(Value::as_i64),
        replies,
        annotate,
        args.get("recap").filter(|r| !r.is_null()).map(recap_of),
        create_refs,
    );
    match result {
        Ok(result) => Ok(result.to_json()),
        Err(e) => Err(rejected("compact_notes", e)),
    }
}

pub fn get_proposal(app: &App, args: &Map<String, Value>) -> ToolResult {
    let compactor = compactor(app)?;
    match compactor.get_proposal(args.get("proposal_id").and_then(Value::as_str), args.get("since_revision").and_then(Value::as_i64)) {
        Ok(p) => Ok(p.to_json()),
        Err(e) => Err(rejected("get_proposal", e)),
    }
}

/// `_proposal_decision`: an amend_proposal update or create, as the
/// decision it makes.
fn proposal_decision(
    event: &Value,
    action: &str,
    start_note: Option<String>,
    end_note: Option<String>,
    clear_fields: &[String],
) -> Result<EventDecision, anyhow::Error> {
    let mut problems: Vec<String> = Vec::new();
    if event.get("is_cancelled").and_then(Value::as_bool).unwrap_or(false) {
        problems.push("to cancel an event, put it in `cancels`".into());
    }
    if event.get("judgments").is_some_and(|j| !j.is_null()) || clear_fields.iter().any(|c| c == "judgments") {
        problems.push("judgments are made once a proposal's applied: leave them out".into());
    }
    if clear_fields.iter().any(|c| c == "priority") {
        problems.push("a proposal can't clear an event's priority: set one".into());
    }
    let id = text(event, "id").filter(|i| !i.is_empty());
    if action == "keep" && id.is_none() {
        problems.push("an update needs the event's id (or a new event's key)".into());
    }
    let mut both: Vec<&str> = ["description", "location", "facts"]
        .into_iter()
        .filter(|f| clear_fields.iter().any(|c| c == f) && event.get(*f).is_some_and(|v| !v.is_null()))
        .collect();
    both.sort();
    if !both.is_empty() {
        problems.push(format!("{}: set or cleared, not both", both.join(", ")));
    }
    if !problems.is_empty() {
        let who = crate::compaction::plan::repr_opt(id.as_deref().or(event.get("summary").and_then(Value::as_str)));
        let issues: Vec<crate::compaction::plan::Issue> =
            problems.iter().map(|p| crate::compaction::plan::Issue::plain(format!("{who}: {p}"))).collect();
        return Err(crate::compaction::plan::error_of(&issues, "malformed_decision").into());
    }
    let cleared = |name: &str| clear_fields.iter().any(|c| c == name);
    let from_label = event.get("actions_from_label").and_then(Value::as_bool).unwrap_or(false);
    Ok(EventDecision {
        action: action.into(),
        event_id: if action == "keep" { text(event, "id") } else { None },
        summary: text(event, "summary"),
        start: moment(event.get("start"))?,
        end: moment(event.get("end"))?,
        start_note,
        end_note,
        action_ids: if from_label { None } else { strings(event.get("action_ids")) },
        facts: if cleared("facts") { Some(Facts::default()) } else { facts_of(event) },
        description: if cleared("description") { Some(String::new()) } else { text(event, "description") },
        location: if cleared("location") { Some(String::new()) } else { text(event, "location") },
        priority: event.get("priority").and_then(Value::as_i64),
        ..Default::default()
    })
}

pub fn amend_proposal(app: &App, args: &Map<String, Value>) -> ToolResult {
    let compactor = compactor(app)?;
    let allow = args.get("allow_compacted_changes").and_then(Value::as_bool).unwrap_or(false);
    let run = || -> anyhow::Result<Value> {
        let mut decisions: Vec<EventDecision> = Vec::new();
        for u in list(args, "updates") {
            let clear: Vec<String> = strings(u.get("clear_fields")).unwrap_or_default();
            decisions.push(proposal_decision(&u["event"], "keep", text(u, "start_note"), text(u, "end_note"), &clear)?);
        }
        for c in list(args, "creates") {
            decisions.push(proposal_decision(c, "create", text(c, "start_note"), text(c, "end_note"), &[])?);
        }
        decisions.extend(list(args, "cancels").into_iter().map(cancel_of));
        if allow {
            for d in decisions.iter_mut() {
                d.allow_history = Some(true);
            }
        }
        let notes: Vec<NoteEdit> = list(args, "notes").into_iter().map(NoteEdit::from_value).collect();
        let additions: Vec<AdditionChoice> = list(args, "additions").into_iter().map(AdditionChoice::from_value).collect();
        let through = moment(args.get("through"))?;
        let proposal = compactor.amend(
            super::args_str(args, "proposal_id"),
            args.get("revision").and_then(Value::as_i64).unwrap_or(0),
            decisions,
            strings(args.get("as_planned")).unwrap_or_default(),
            notes,
            additions,
            through,
        )?;
        Ok(proposal.to_json())
    };
    match run() {
        Ok(value) => Ok(value),
        Err(e) => {
            if let Some(refusal) = compaction_error(&e) {
                if refusal.categories.iter().any(|c| c == "compacted") && !allow {
                    let mut wrapped = refusal.clone();
                    wrapped.message = format!(
                        "{}\n\nIt changes history, which needs the user's approval: send it again with allow_compacted_changes once they've given it.",
                        refusal.message
                    );
                    return Err(rejected("amend_proposal", wrapped.into()));
                }
            }
            Err(rejected("amend_proposal", e))
        }
    }
}

pub fn add_proposal_note(app: &App, args: &Map<String, Value>) -> ToolResult {
    let compactor = compactor(app)?;
    let at = moment(args.get("at"))?;
    match compactor.add_note(
        super::args_str(args, "proposal_id"),
        super::args_str(args, "text"),
        args.get("event_id").and_then(Value::as_str).map(str::to_string),
        at,
        args.get("note_id").and_then(Value::as_str).map(str::to_string),
    ) {
        Ok(item) => Ok(item.to_json()),
        Err(e) => Err(rejected("add_proposal_note", e)),
    }
}

pub fn withdraw_proposal_note(app: &App, args: &Map<String, Value>) -> ToolResult {
    let compactor = compactor(app)?;
    match compactor.withdraw_note(super::args_str(args, "feedback_id")) {
        Ok(item) => Ok(item.to_json()),
        Err(e) => Err(rejected("withdraw_proposal_note", e)),
    }
}

pub fn confirm_proposal(app: &App, args: &Map<String, Value>) -> ToolResult {
    let compactor = compactor(app)?;
    match compactor.confirm(super::args_str(args, "proposal_id"), args.get("revision").and_then(Value::as_i64).unwrap_or(0)) {
        Ok(result) => Ok(result.to_json()),
        Err(e) => Err(rejected("confirm_proposal", e)),
    }
}

pub fn finish_proposal(app: &App, args: &Map<String, Value>) -> ToolResult {
    let compactor = compactor(app)?;
    match compactor.finish(super::args_str(args, "proposal_id")) {
        Ok(result) => Ok(result.to_json()),
        Err(e) => Err(rejected("finish_proposal", e)),
    }
}

pub fn prepare_judgments(app: &App, args: &Map<String, Value>) -> ToolResult {
    let compactor = compactor(app)?;
    compactor.prefetch()?;
    let redo = args.get("redo").and_then(Value::as_bool).unwrap_or(false);
    let due = match compactor.judgments_due(args.get("compaction_id").and_then(Value::as_str), redo) {
        Ok(due) => due,
        Err(e) => return Err(rejected("prepare_judgments", e)),
    };
    match due {
        Some(due) => Ok(due.to_json()),
        None => Err(ToolError::Message("there's no applied compaction to judge".into())),
    }
}

pub fn prepare_habit_judgments(app: &App, args: &Map<String, Value>) -> ToolResult {
    let compactor = compactor(app)?;
    compactor.prefetch()?;
    let since = moment(args.get("since"))?;
    let redo = args.get("redo").and_then(Value::as_bool).unwrap_or(false);
    match compactor.habit_judgments_due(super::args_str(args, "habit_id"), since, redo) {
        Ok(due) => Ok(due.to_json()),
        Err(e) => Err(rejected("prepare_habit_judgments", e)),
    }
}

pub fn record_judgments(app: &App, args: &Map<String, Value>) -> ToolResult {
    let compactor = compactor(app)?;
    compactor.prefetch()?;
    let judgments: Vec<Judgment> = list(args, "judgments")
        .into_iter()
        .map(|j| Judgment {
            request_id: text(j, "request_id").unwrap_or_default(),
            rating: j.get("rating").and_then(Value::as_i64).unwrap_or(0),
            reasoning: text(j, "reasoning").unwrap_or_default(),
        })
        .collect();
    match compactor.record_judgments(super::args_str(args, "compaction_id"), &judgments) {
        Ok(result) => Ok(result.to_json()),
        Err(e) => Err(rejected("record_judgments", e)),
    }
}

pub fn abandon_compaction(app: &App, args: &Map<String, Value>) -> ToolResult {
    let compactor = compactor(app)?;
    match compactor.abandon(super::args_str(args, "proposal_id")) {
        Ok(result) => Ok(result.to_json()),
        Err(e) => Err(rejected("abandon_compaction", e)),
    }
}
