//! The write-ahead journal of note compactions, one tab of the metadata
//! spreadsheet: one row per fact, in eight columns --
//!
//! ```text
//! compaction_id | step | kind | event_id | before | after | status | detail
//! ```
//!
//! Everything here reads the whole tab, in one range: inside a tool call's
//! cached reads, every later read until it's next written is served from
//! memory.

use crate::compaction::plan::{error, CompactionChange, CompactionPlan, EventDecision, EventState};
use crate::compaction::proposals::{Feedback, UserEdit};
use crate::event::{parse_iso, Moment};
use crate::google::sheets::{Rows, SheetsClient, TabRange, TabWrite};
use crate::py;
use crate::sheet::{ensure_tab, COMPACTIONS, MIN_TAB_ROWS};
use serde_json::{json, Map, Value};

pub const PLANNED: &str = "planned";
pub const PROPOSED: &str = "proposed";
pub const SUPERSEDED: &str = "superseded";
pub const APPLYING: &str = "applying";
pub const APPLIED: &str = "applied";
pub const STAMPED: &str = "stamped";
pub const FAILED: &str = "failed";
pub const ABANDONED: &str = "abandoned";
pub const OPEN_STATUSES: [&str; 2] = [APPLYING, APPLIED];
pub const USER_EDIT: &str = "user_decision";
pub const FEEDBACK: &str = "feedback";
pub const CLAUDE_DECISION: &str = "claude_decision";

pub const HEADER: [&str; 8] = ["compaction_id", "step", "kind", "event_id", "before", "after", "status", "detail"];
pub const HEADER_RANGE: &str = "A1:H1";
pub const DATA_RANGE: &str = "A2:H";
pub const FIRST_DATA_ROW: i64 = 2;
const STATUS_COLUMN: &str = "G";
const MAX_ROWS: usize = 100;
const TRIM_TO_ROWS: usize = 50;
const WHOLE_SUPERSEDED: usize = 3;

fn moment(text: Option<&str>) -> anyhow::Result<Moment> {
    parse_iso(text.unwrap_or_default(), None)
}

fn strings(value: Option<&Value>) -> Vec<String> {
    value.and_then(Value::as_array).map(|a| a.iter().filter_map(|v| v.as_str().map(str::to_string)).collect()).unwrap_or_default()
}

fn string_pairs(value: Option<&Value>) -> Vec<(String, String)> {
    value
        .and_then(Value::as_object)
        .map(|m| m.iter().filter_map(|(k, v)| v.as_str().map(|s| (k.clone(), s.to_string()))).collect())
        .unwrap_or_default()
}

fn pairs_json(pairs: &[(String, String)]) -> Value {
    Value::Object(pairs.iter().map(|(k, v)| (k.clone(), json!(v))).collect())
}

fn parse(text: &str) -> anyhow::Result<Value> {
    Ok(serde_json::from_str(text)?)
}

fn detail_of(row: &[String]) -> Value {
    if row[7].is_empty() {
        json!({})
    } else {
        serde_json::from_str(&row[7]).unwrap_or(json!({}))
    }
}

#[derive(Debug, Clone)]
pub struct JournalStep {
    pub step: i64,
    pub action: String,
    pub event_id: Option<String>,
    pub before: Option<EventState>,
    pub after: Option<EventState>,
    pub status: String,
    pub reason: String,
    pub row: i64,
    pub key: Option<String>,
}

#[derive(Debug, Clone, Default)]
pub struct RevisionMeta {
    pub proposal: String,
    pub revision: i64,
    pub window_start: Option<Moment>,
    pub created: Option<Moment>,
    pub base: Option<i64>,
    pub user_seq: i64,
    pub by: String,
    pub reason: String,
    pub changed: Vec<String>,
    pub outcomes: Vec<(String, String)>,
    pub key_seq: i64,
    pub claude_decisions: Vec<EventDecision>,
    pub claude_ignore_notes: Option<Vec<String>>,
    pub claude_note_targets: Vec<(String, String)>,
    pub claude_additions: Option<Map<String, Value>>,
    pub aliases: Vec<(String, String)>,
    pub settled: Vec<String>,
    pub judge_also: Vec<Value>,
    pub claude_through: Option<Moment>,
    pub claude_recap: Option<Value>,
    pub recap_revision: Option<i64>,
}

impl RevisionMeta {
    pub fn window_start(&self) -> Moment {
        self.window_start.expect("a revision has a window start")
    }

    pub fn created(&self) -> Moment {
        self.created.expect("a revision has a creation time")
    }

    pub fn to_detail(&self) -> Map<String, Value> {
        let mut out = Map::new();
        out.insert("proposal".into(), json!(self.proposal));
        out.insert("revision".into(), json!(self.revision));
        out.insert("from".into(), json!(py::isoformat(&self.window_start())));
        out.insert("created".into(), json!(py::isoformat(&self.created())));
        out.insert("base".into(), json!(self.base));
        out.insert("user_seq".into(), json!(self.user_seq));
        out.insert("by".into(), json!(self.by));
        out.insert("reason".into(), json!(self.reason));
        out.insert("changed".into(), json!(self.changed));
        out.insert("outcomes".into(), pairs_json(&self.outcomes));
        out.insert("key_seq".into(), json!(self.key_seq));
        if let Some(ignore) = &self.claude_ignore_notes {
            out.insert("claude_ignore_notes".into(), json!(ignore));
        }
        if !self.claude_note_targets.is_empty() {
            out.insert("claude_note_targets".into(), pairs_json(&self.claude_note_targets));
        }
        if let Some(additions) = &self.claude_additions {
            out.insert("claude_additions".into(), Value::Object(additions.clone()));
        }
        if !self.aliases.is_empty() {
            out.insert("aliases".into(), pairs_json(&self.aliases));
        }
        if !self.settled.is_empty() {
            out.insert("settled".into(), json!(self.settled));
        }
        if !self.judge_also.is_empty() {
            out.insert("judge_also".into(), json!(self.judge_also));
        }
        if let Some(through) = &self.claude_through {
            out.insert("claude_through".into(), json!(py::isoformat(through)));
        }
        if let Some(recap) = &self.claude_recap {
            out.insert("claude_recap".into(), recap.clone());
        }
        if let Some(revision) = self.recap_revision {
            out.insert("recap_revision".into(), json!(revision));
        }
        out
    }

    pub fn from_detail(detail: &Value, claude_decisions: Vec<EventDecision>) -> anyhow::Result<RevisionMeta> {
        let text = |key: &str| detail.get(key).and_then(Value::as_str);
        Ok(RevisionMeta {
            proposal: text("proposal").unwrap_or_default().into(),
            revision: detail["revision"].as_i64().unwrap_or(0),
            window_start: Some(moment(text("from"))?),
            created: Some(moment(text("created"))?),
            base: detail.get("base").and_then(Value::as_i64),
            user_seq: detail.get("user_seq").and_then(Value::as_i64).unwrap_or(0),
            by: text("by").unwrap_or("claude").into(),
            reason: text("reason").unwrap_or("").into(),
            changed: strings(detail.get("changed")),
            outcomes: string_pairs(detail.get("outcomes")),
            key_seq: detail.get("key_seq").and_then(Value::as_i64).unwrap_or(0),
            claude_decisions,
            claude_ignore_notes: detail.get("claude_ignore_notes").filter(|v| !v.is_null()).map(|v| strings(Some(v))),
            claude_note_targets: string_pairs(detail.get("claude_note_targets")),
            claude_additions: detail.get("claude_additions").and_then(Value::as_object).cloned(),
            aliases: string_pairs(detail.get("aliases")),
            settled: strings(detail.get("settled")),
            judge_also: detail.get("judge_also").and_then(Value::as_array).cloned().unwrap_or_default(),
            claude_through: match text("claude_through").filter(|t| !t.is_empty()) {
                Some(t) => Some(parse_iso(t, None)?),
                None => None,
            },
            claude_recap: detail.get("claude_recap").filter(|v| !v.is_null()).cloned(),
            recap_revision: detail.get("recap_revision").and_then(Value::as_i64),
        })
    }
}

#[derive(Debug, Clone)]
pub struct JournalCompaction {
    pub id: String,
    pub status: String,
    pub now: Moment,
    pub note_ids: Vec<String>,
    pub warnings: Vec<String>,
    pub decisions: Vec<EventDecision>,
    pub ignore_notes: Vec<String>,
    pub steps: Vec<JournalStep>,
    pub note_targets: Vec<(String, Option<String>)>,
    pub row: i64,
    pub batch: Option<String>,
    pub day: i64,
    pub additions: Map<String, Value>,
    pub proposal: Option<String>,
    pub revision: i64,
    pub meta: Option<RevisionMeta>,
}

impl JournalCompaction {
    pub fn batch_id(&self) -> String {
        self.batch.clone().unwrap_or_else(|| self.id.clone())
    }

    pub fn meta(&self) -> &RevisionMeta {
        self.meta.as_ref().expect("a revision's first day has its meta")
    }

    pub fn changes(&self) -> Vec<CompactionChange> {
        self.steps
            .iter()
            .map(|s| CompactionChange {
                action: s.action.clone(),
                reason: s.reason.clone(),
                event_id: s.event_id.clone(),
                key: s.key.clone(),
                before: s.before.clone(),
                after: s.after.clone(),
            })
            .collect()
    }
}

pub struct PlannedDay<'a> {
    pub compaction_id: String,
    pub now: Moment,
    pub note_ids: Vec<String>,
    pub decisions: &'a [EventDecision],
    pub plan: &'a CompactionPlan,
    pub ignore_notes: Vec<String>,
    pub additions: Map<String, Value>,
    pub note_targets: Vec<(String, Option<String>)>,
}

#[derive(Clone)]
pub struct CompactionJournal {
    pub(crate) sheets: SheetsClient,
    pub(crate) spreadsheet_id: String,
    pub(crate) sheet_id: i64,
}

fn edit_row(edit: &UserEdit) -> Vec<String> {
    let suffix = format!("u{}", edit.seq);
    let proposal = edit.id.strip_suffix(&suffix).unwrap_or(&edit.id).to_string();
    vec![
        proposal,
        edit.seq.to_string(),
        USER_EDIT.into(),
        edit.event_id.clone().unwrap_or_default(),
        py::json_default(&Value::Object(edit.edit.clone())),
        String::new(),
        edit.status.clone(),
        py::json_default(&json!({"created": py::isoformat(&edit.created), "base": edit.base_revision})),
    ]
}

fn feedback_row(feedback: &Feedback, proposal: &str) -> Vec<String> {
    let mut note = Map::new();
    note.insert("text".into(), json!(feedback.text));
    note.insert("by".into(), json!(feedback.by));
    note.insert("created".into(), json!(py::isoformat(&feedback.created)));
    if let Some(at) = &feedback.at {
        note.insert("at".into(), json!(py::isoformat(at)));
    }
    if let Some(note_id) = &feedback.note_id {
        note.insert("note_id".into(), json!(note_id));
    }
    vec![
        proposal.into(),
        feedback.seq.to_string(),
        FEEDBACK.into(),
        feedback.event_id.clone().unwrap_or_default(),
        py::json_default(&Value::Object(note)),
        String::new(),
        feedback.status.clone(),
        String::new(),
    ]
}

impl CompactionJournal {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<CompactionJournal> {
        let (sheet_id, created) = ensure_tab(sheets, spreadsheet_id, COMPACTIONS.0, COMPACTIONS.1)?;
        if created {
            let header: Vec<String> = HEADER.iter().map(|s| s.to_string()).collect();
            sheets.write_rows_in_sheet(spreadsheet_id, sheet_id, HEADER_RANGE, &vec![header])?;
        }
        Ok(CompactionJournal { sheets: sheets.clone(), spreadsheet_id: spreadsheet_id.into(), sheet_id })
    }

    pub fn whole_tab(&self) -> TabRange {
        TabRange::new(&self.spreadsheet_id, self.sheet_id, "A1:H")
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheets.prefetch(ranges)
    }

    /// `start_batch`: record a new batch -- each day's compaction row, its
    /// decisions and a `pending` row per step -- in one write.
    pub fn start_batch(&self, days: &[PlannedDay], meta: Option<&RevisionMeta>, edits: &[UserEdit]) -> anyhow::Result<()> {
        let batch = days[0].compaction_id.clone();
        let status = if meta.is_some() { PROPOSED } else { PLANNED };
        let mut rows: Rows = edits.iter().map(edit_row).collect();
        for (index, day) in days.iter().enumerate() {
            let number = index + 1;
            let mut detail = Map::new();
            detail.insert("now".into(), json!(py::isoformat(&day.now)));
            detail.insert("note_ids".into(), json!(day.note_ids));
            detail.insert("warnings".into(), json!(day.plan.warnings));
            detail.insert("ignore_notes".into(), json!(day.ignore_notes));
            if number > 1 {
                detail.insert("batch".into(), json!(batch));
                detail.insert("day".into(), json!(number));
            }
            if !day.additions.is_empty() {
                detail.insert("additions".into(), Value::Object(day.additions.clone()));
            }
            if !day.note_targets.is_empty() {
                detail.insert(
                    "note_targets".into(),
                    Value::Object(day.note_targets.iter().map(|(k, v)| (k.clone(), json!(v))).collect()),
                );
            }
            if let Some(meta) = meta {
                if number == 1 {
                    for (k, v) in meta.to_detail() {
                        detail.insert(k, v);
                    }
                } else {
                    detail.insert("proposal".into(), json!(meta.proposal));
                    detail.insert("revision".into(), json!(meta.revision));
                }
            }
            let row = |kind: &str, step: String, event_id: String, before: String, after: String, status: &str, detail: String| {
                vec![day.compaction_id.clone(), step, kind.to_string(), event_id, before, after, status.to_string(), detail]
            };
            rows.push(row("compaction", "0".into(), String::new(), String::new(), String::new(), status, py::json_default(&Value::Object(detail))));
            if let (Some(meta), 1) = (meta, number) {
                for d in &meta.claude_decisions {
                    let name = d.event_id.clone().or(d.key.clone()).unwrap_or_default();
                    rows.push(row(CLAUDE_DECISION, "0".into(), name, py::json_default(&d.to_json_dict()), String::new(), "", String::new()));
                }
            }
            for decision in day.decisions {
                rows.push(row(
                    "decision",
                    "0".into(),
                    decision.event_id.clone().unwrap_or_default(),
                    py::json_default(&decision.to_json_dict()),
                    String::new(),
                    "",
                    String::new(),
                ));
            }
            for (step, change) in day.plan.changes.iter().enumerate() {
                rows.push(row(
                    &change.action,
                    (step + 1).to_string(),
                    change.event_id.clone().or(change.key.clone()).unwrap_or_default(),
                    change.before.as_ref().map(|b| py::json_default(&b.to_json_dict())).unwrap_or_default(),
                    change.after.as_ref().map(|a| py::json_default(&a.to_json_dict())).unwrap_or_default(),
                    "pending",
                    change.reason.clone(),
                ));
            }
        }
        self.append(rows)
    }

    fn append(&self, rows: Rows) -> anyhow::Result<()> {
        if rows.is_empty() {
            return Ok(());
        }
        let first_row = FIRST_DATA_ROW + self.read_rows()?.len() as i64;
        self.sheets.write_rows_in_sheet(&self.spreadsheet_id, self.sheet_id, &format!("A{first_row}:H"), &rows)
    }

    pub fn add_user_edits(&self, edits: &[UserEdit]) -> anyhow::Result<()> {
        self.append(edits.iter().map(edit_row).collect())
    }

    pub fn user_edits(&self, proposal: &str) -> anyhow::Result<Vec<(i64, UserEdit)>> {
        let mut found: Vec<(i64, UserEdit)> = Vec::new();
        for (offset, row) in self.read_rows()?.iter().enumerate() {
            if row[0] == proposal && row[2] == USER_EDIT {
                let detail = if row[7].is_empty() { json!({}) } else { parse(&row[7])? };
                let seq: i64 = row[1].trim().parse()?;
                found.push((
                    FIRST_DATA_ROW + offset as i64,
                    UserEdit {
                        id: format!("{proposal}u{}", row[1]),
                        seq,
                        event_id: Some(row[3].clone()).filter(|e| !e.is_empty()),
                        edit: parse(&row[4])?.as_object().cloned().unwrap_or_default(),
                        status: if row[6].is_empty() { "active".into() } else { row[6].clone() },
                        created: moment(detail.get("created").and_then(Value::as_str))?,
                        base_revision: detail.get("base").and_then(Value::as_i64).unwrap_or(0),
                    },
                ));
            }
        }
        found.sort_by_key(|(_, e)| e.seq);
        Ok(found)
    }

    pub fn set_user_edit_status(&self, row: i64, status: &str) -> anyhow::Result<()> {
        self.write_status(row, status)
    }

    pub fn add_feedback(&self, feedback: &Feedback, proposal: &str) -> anyhow::Result<()> {
        self.append(vec![feedback_row(feedback, proposal)])
    }

    pub fn feedback(&self, proposal: &str) -> anyhow::Result<Vec<(i64, Feedback)>> {
        let mut found: Vec<(i64, Feedback)> = Vec::new();
        for (offset, row) in self.read_rows()?.iter().enumerate() {
            if row[0] == proposal && row[2] == FEEDBACK {
                let note = parse(&row[4])?;
                let answer = if row[5].is_empty() { json!({}) } else { parse(&row[5])? };
                let seq: i64 = row[1].trim().parse()?;
                found.push((
                    FIRST_DATA_ROW + offset as i64,
                    Feedback {
                        id: format!("{proposal}f{}", row[1]),
                        seq,
                        text: note["text"].as_str().unwrap_or_default().into(),
                        event_id: Some(row[3].clone()).filter(|e| !e.is_empty()),
                        note_id: note.get("note_id").and_then(Value::as_str).map(str::to_string),
                        at: match note.get("at").and_then(Value::as_str).filter(|a| !a.is_empty()) {
                            Some(at) => Some(parse_iso(at, None)?),
                            None => None,
                        },
                        by: note.get("by").and_then(Value::as_str).unwrap_or("user").into(),
                        created: moment(note.get("created").and_then(Value::as_str))?,
                        status: if row[6].is_empty() { "open".into() } else { row[6].clone() },
                        reply: answer.get("reply").and_then(Value::as_str).map(str::to_string),
                        answered_in: answer.get("revision").and_then(Value::as_i64),
                        superseded_by: answer.get("superseded_by").filter(|v| !v.is_null()).map(|v| strings(Some(v))),
                    },
                ));
            }
        }
        found.sort_by_key(|(_, f)| f.seq);
        Ok(found)
    }

    pub fn update_feedback(&self, row: i64, feedback: &Feedback) -> anyhow::Result<()> {
        let mut answer = Map::new();
        answer.insert("reply".into(), json!(feedback.reply));
        answer.insert("revision".into(), json!(feedback.answered_in));
        if let Some(by) = feedback.superseded_by.as_ref().filter(|s| !s.is_empty()) {
            answer.insert("superseded_by".into(), json!(by));
        }
        let cell = if feedback.reply.is_some() { py::json_default(&Value::Object(answer)) } else { String::new() };
        self.sheets.write_rows_in_sheet(
            &self.spreadsheet_id,
            self.sheet_id,
            &format!("F{row}:G{row}"),
            &vec![vec![cell, feedback.status.clone()]],
        )
    }

    /// (revision, its first day's id, that day's status) of each of
    /// `proposal`'s revisions, oldest first.
    pub fn revisions(&self, proposal: &str) -> anyhow::Result<Vec<(i64, String, String)>> {
        let mut found: Vec<(i64, String, String)> = Vec::new();
        for row in self.read_rows()? {
            if row[2] != "compaction" || row[7].is_empty() {
                continue;
            }
            let detail = parse(&row[7])?;
            if detail.get("proposal").and_then(Value::as_str) == Some(proposal) && !crate::stores::groups::truthy(detail.get("batch")) {
                found.push((detail["revision"].as_i64().unwrap_or(0), row[0].clone(), row[6].clone()));
            }
        }
        found.sort();
        Ok(found)
    }

    pub fn revision_meta(&self, proposal: &str, revision: i64) -> anyhow::Result<Option<RevisionMeta>> {
        for row in self.read_rows()? {
            if row[2] != "compaction" || row[7].is_empty() {
                continue;
            }
            let detail = parse(&row[7])?;
            if detail.get("proposal").and_then(Value::as_str) == Some(proposal)
                && detail.get("revision").and_then(Value::as_i64) == Some(revision)
                && !crate::stores::groups::truthy(detail.get("batch"))
            {
                return Ok(Some(RevisionMeta::from_detail(&detail, Vec::new())?));
            }
        }
        Ok(None)
    }

    /// The id of the proposal still open -- some day of its newest revision
    /// neither stamped nor abandoned -- if there is one.
    pub fn open_proposal(&self) -> anyhow::Result<Option<String>> {
        Ok(open_proposal_of(&self.read_rows()?))
    }

    /// The compaction `compaction_id`; a CompactionError if there isn't one.
    pub fn load(&self, compaction_id: &str) -> anyhow::Result<JournalCompaction> {
        let rows = self.read_rows()?;
        load_from(&rows, compaction_id)
    }

    pub fn load_batch(&self, batch_id: &str) -> anyhow::Result<Vec<JournalCompaction>> {
        let rows = self.read_rows()?;
        let first = load_from(&rows, batch_id)?;
        let mut later: Vec<JournalCompaction> = Vec::new();
        for row in &rows {
            if row[2] == "compaction" && detail_of(row).get("batch").and_then(Value::as_str) == Some(batch_id) {
                later.push(load_from(&rows, &row[0])?);
            }
        }
        later.sort_by_key(|c| c.day);
        let mut all = vec![first];
        all.extend(later);
        Ok(all)
    }

    pub fn compactions_with_status(&self, statuses: &[&str]) -> anyhow::Result<Vec<(String, String)>> {
        Ok(self
            .read_rows()?
            .into_iter()
            .filter(|row| row[2] == "compaction" && statuses.contains(&row[6].as_str()))
            .map(|row| (row[0].clone(), row[6].clone()))
            .collect())
    }

    pub fn open_compactions(&self) -> anyhow::Result<Vec<(String, String)>> {
        self.compactions_with_status(&OPEN_STATUSES)
    }

    /// The `now` of the most recently stamped compaction, if any: where
    /// history ends.
    pub fn last_stamped_now(&self) -> anyhow::Result<Option<Moment>> {
        let mut latest: Option<Moment> = None;
        for row in self.read_rows()? {
            if row[2] != "compaction" || row[6] != STAMPED {
                continue;
            }
            let detail: Value = serde_json::from_str(&row[7])?;
            let now = parse_iso(detail["now"].as_str().unwrap_or_default(), None)?;
            if latest.is_none_or(|l| now > l) {
                latest = Some(now);
            }
        }
        Ok(latest)
    }

    pub fn last_stamped_batch(&self) -> anyhow::Result<Option<String>> {
        let mut latest: Option<(Moment, String)> = None;
        for row in self.read_rows()? {
            if row[2] != "compaction" || row[6] != STAMPED {
                continue;
            }
            let detail: Value = serde_json::from_str(&row[7])?;
            let now = parse_iso(detail["now"].as_str().unwrap_or_default(), None)?;
            if latest.as_ref().is_none_or(|(l, _)| now > *l) {
                let batch = detail.get("batch").and_then(Value::as_str).filter(|b| !b.is_empty()).map(str::to_string).unwrap_or(row[0].clone());
                latest = Some((now, batch));
            }
        }
        Ok(latest.map(|(_, b)| b))
    }

    pub fn set_status(&self, compaction: &mut JournalCompaction, status: &str) -> anyhow::Result<()> {
        self.write_status(compaction.row, status)?;
        compaction.status = status.into();
        Ok(())
    }

    /// Delete rows nothing needs any more, anywhere in the tab, bottom
    /// first:
    ///
    /// - Of an open proposal's superseded revisions, all but the newest
    ///   `WHOLE_SUPERSEDED` lose every row but their `compaction` ones
    ///   (whose `changed` lists still say what each revision changed).
    /// - Once the tab has more than `MAX_ROWS` data rows, whole
    ///   finished blocks go, oldest first, until it's down to
    ///   `TRIM_TO_ROWS`: a stamped, abandoned or superseded compaction
    ///   (or a failed one, its proposal finished), and a finished
    ///   proposal's edits and feedback. Never the most recently stamped
    ///   batch, which `last_stamped_now` depends on, nor anything of an
    ///   open proposal or a compaction still planned or open.
    ///
    /// Superseded revisions were never applied, and nothing reads them
    /// once a newer one exists, so they're safe to delete.
    pub fn garbage_collect(&self) -> anyhow::Result<()> {
        let rows = self.read_rows()?;
        let details: Vec<Value> =
            rows.iter().map(|row| if row[2] == "compaction" && !row[7].is_empty() { parse(&row[7]).unwrap_or(json!({})) } else { json!({}) }).collect();
        let mut batch_of: Vec<(String, String)> = Vec::new();
        let mut status_of: Vec<(String, String)> = Vec::new();
        let mut proposal_of: Vec<(String, Option<String>)> = Vec::new();
        fn put<V>(map: &mut Vec<(String, V)>, key: &str, value: V) {
            match map.iter_mut().find(|(k, _)| k == key) {
                Some(slot) => slot.1 = value,
                None => map.push((key.to_string(), value)),
            }
        }
        for (i, row) in rows.iter().enumerate() {
            if row[2] == "compaction" {
                let batch = details[i].get("batch").and_then(Value::as_str).filter(|b| !b.is_empty()).map(str::to_string).unwrap_or(row[0].clone());
                put(&mut batch_of, &row[0], batch);
                put(&mut status_of, &row[0], row[6].clone());
                put(&mut proposal_of, &row[0], details[i].get("proposal").and_then(Value::as_str).map(str::to_string));
            }
        }
        let get = |map: &Vec<(String, String)>, key: &str| map.iter().find(|(k, _)| k == key).map(|(_, v)| v.clone());
        let proposal = |key: &str| proposal_of.iter().find(|(k, _)| k == key).and_then(|(_, v)| v.clone());
        let open_proposal = open_proposal_of(&rows);
        let last_stamped = rows.iter().rev().find(|row| row[2] == "compaction" && row[6] == STAMPED).and_then(|row| get(&batch_of, &row[0]));

        let mut doomed: std::collections::BTreeSet<usize> = std::collections::BTreeSet::new();
        if let Some(open) = &open_proposal {
            let mut superseded: Vec<(i64, String)> = Vec::new();
            for (i, row) in rows.iter().enumerate() {
                if row[2] == "compaction"
                    && proposal(&row[0]).as_deref() == Some(open.as_str())
                    && get(&status_of, &row[0]).as_deref() == Some(SUPERSEDED)
                {
                    let entry = (details[i]["revision"].as_i64().unwrap_or(0), get(&batch_of, &row[0]).unwrap());
                    if !superseded.contains(&entry) {
                        superseded.push(entry);
                    }
                }
            }
            superseded.sort();
            superseded.reverse();
            let trimmed: Vec<String> = superseded.iter().skip(WHOLE_SUPERSEDED).map(|(_, b)| b.clone()).collect();
            for (i, row) in rows.iter().enumerate() {
                if row[2] != "compaction" && get(&batch_of, &row[0]).is_some_and(|b| trimmed.contains(&b)) {
                    doomed.insert(i);
                }
            }
        }

        if rows.len() - doomed.len() > MAX_ROWS {
            let excess = rows.len() - doomed.len() - TRIM_TO_ROWS;
            let finished = |compaction_id: &str| -> bool {
                if get(&batch_of, compaction_id) == last_stamped {
                    return false;
                }
                let p = proposal(compaction_id);
                if p.is_some() && p == open_proposal {
                    return false;
                }
                let status = get(&status_of, compaction_id).unwrap_or_default();
                [STAMPED, ABANDONED, SUPERSEDED].contains(&status.as_str()) || (status == FAILED && p.is_some())
            };
            let mut units: Vec<(String, Vec<usize>)> = Vec::new();
            for (i, row) in rows.iter().enumerate() {
                if row[0].is_empty() {
                    continue;
                }
                let unit = if row[2] == USER_EDIT || row[2] == FEEDBACK { format!("ledger:{}", row[0]) } else { row[0].clone() };
                match units.iter_mut().find(|(u, _)| *u == unit) {
                    Some(slot) => slot.1.push(i),
                    None => units.push((unit, vec![i])),
                }
            }
            let proposals_finished: Vec<String> =
                proposal_of.iter().filter_map(|(_, p)| p.clone()).filter(|p| Some(p) != open_proposal.as_ref()).collect();
            units.sort_by_key(|(_, indices)| indices[0]);
            let mut freed = 0;
            for (unit, indices) in &units {
                if freed >= excess {
                    break;
                }
                if let Some(p) = unit.strip_prefix("ledger:") {
                    if !proposals_finished.iter().any(|f| f == p) {
                        continue;
                    }
                } else if get(&status_of, unit).is_none() || !finished(unit) {
                    continue;
                }
                let fresh: Vec<usize> = indices.iter().copied().filter(|i| !doomed.contains(i)).collect();
                freed += fresh.len();
                doomed.extend(fresh);
            }
        }

        if doomed.is_empty() {
            return Ok(());
        }
        let mut runs: Vec<(usize, usize)> = Vec::new();
        for i in doomed {
            match runs.last_mut() {
                Some(run) if run.1 + 1 == i => run.1 = i,
                _ => runs.push((i, i)),
            }
        }
        for (start, end) in runs.into_iter().rev() {
            self.sheets.delete_rows(
                &self.spreadsheet_id,
                self.sheet_id,
                FIRST_DATA_ROW + start as i64,
                FIRST_DATA_ROW + end as i64,
                MIN_TAB_ROWS,
            )?;
        }
        Ok(())
    }

    /// Write each (row, status) of `statuses` -- steps' and compactions'
    /// alike -- in one request.
    pub fn write_statuses(&self, statuses: &[(i64, &str)]) -> anyhow::Result<()> {
        let writes: Vec<TabWrite> = statuses
            .iter()
            .map(|(row, status)| TabWrite { sheet_id: self.sheet_id, range: format!("{STATUS_COLUMN}{row}:{STATUS_COLUMN}{row}"), rows: vec![vec![status.to_string()]] })
            .collect();
        self.sheets.write_tabs(&self.spreadsheet_id, &writes)
    }

    fn write_status(&self, row: i64, status: &str) -> anyhow::Result<()> {
        self.sheets.write_rows_in_sheet(
            &self.spreadsheet_id,
            self.sheet_id,
            &format!("{STATUS_COLUMN}{row}:{STATUS_COLUMN}{row}"),
            &vec![vec![status.to_string()]],
        )
    }

    /// Every data row through the last non-blank one, padded to the full
    /// width.
    pub fn read_rows(&self) -> anyhow::Result<Rows> {
        let rows = self.sheets.read_rows_in_sheet(&self.spreadsheet_id, self.sheet_id, DATA_RANGE)?;
        Ok(rows
            .into_iter()
            .map(|mut row| {
                while row.len() < HEADER.len() {
                    row.push(String::new());
                }
                row
            })
            .collect())
    }
}

#[cfg(test)]
#[path = "journal_tests.rs"]
mod tests;

fn open_proposal_of(rows: &Rows) -> Option<String> {
    let mut statuses: Vec<((String, i64), Vec<String>)> = Vec::new();
    for row in rows {
        if row[2] != "compaction" || row[7].is_empty() {
            continue;
        }
        let detail = detail_of(row);
        if let Some(proposal) = detail.get("proposal").filter(|p| !p.is_null()) {
            let key = (proposal.as_str().unwrap_or_default().to_string(), detail["revision"].as_i64().unwrap_or(0));
            match statuses.iter_mut().find(|(k, _)| *k == key) {
                Some(slot) => slot.1.push(row[6].clone()),
                None => statuses.push((key, vec![row[6].clone()])),
            }
        }
    }
    let mut latest: Vec<(String, i64)> = Vec::new();
    for ((proposal, revision), _) in &statuses {
        match latest.iter_mut().find(|(p, _)| p == proposal) {
            Some(slot) => slot.1 = slot.1.max(*revision),
            None => latest.push((proposal.clone(), (*revision).max(0))),
        }
    }
    latest
        .into_iter()
        .find(|(proposal, revision)| {
            statuses
                .iter()
                .find(|((p, r), _)| p == proposal && r == revision)
                .is_some_and(|(_, s)| s.iter().any(|status| ![STAMPED, ABANDONED, SUPERSEDED].contains(&status.as_str())))
        })
        .map(|(p, _)| p)
}

fn load_from(rows: &Rows, compaction_id: &str) -> anyhow::Result<JournalCompaction> {
    let inner = || -> anyhow::Result<Option<JournalCompaction>> {
        let mut compaction: Option<JournalCompaction> = None;
        let mut decisions: Vec<EventDecision> = Vec::new();
        let mut claude: Vec<EventDecision> = Vec::new();
        let mut steps: Vec<JournalStep> = Vec::new();
        let mut detail = json!({});
        for (offset, row) in rows.iter().enumerate() {
            if row[0] != compaction_id {
                continue;
            }
            let sheet_row = FIRST_DATA_ROW + offset as i64;
            let kind = row[2].as_str();
            if kind == "compaction" {
                detail = parse(&row[7])?;
                compaction = Some(JournalCompaction {
                    id: compaction_id.into(),
                    status: row[6].clone(),
                    now: moment(detail["now"].as_str())?,
                    note_ids: strings(detail.get("note_ids")),
                    warnings: strings(detail.get("warnings")),
                    decisions: Vec::new(),
                    ignore_notes: strings(detail.get("ignore_notes")),
                    steps: Vec::new(),
                    note_targets: detail
                        .get("note_targets")
                        .and_then(Value::as_object)
                        .map(|m| m.iter().map(|(k, v)| (k.clone(), v.as_str().map(str::to_string))).collect())
                        .unwrap_or_default(),
                    row: sheet_row,
                    batch: detail.get("batch").and_then(Value::as_str).map(str::to_string),
                    day: detail.get("day").and_then(Value::as_i64).unwrap_or(1),
                    additions: detail.get("additions").and_then(Value::as_object).cloned().unwrap_or_default(),
                    proposal: detail.get("proposal").and_then(Value::as_str).map(str::to_string),
                    revision: detail.get("revision").and_then(Value::as_i64).unwrap_or(0),
                    meta: None,
                });
            } else if kind == "decision" {
                decisions.push(EventDecision::from_json_dict(&parse(&row[4])?)?);
            } else if kind == CLAUDE_DECISION {
                claude.push(EventDecision::from_json_dict(&parse(&row[4])?)?);
            } else if ["disposition", USER_EDIT, FEEDBACK].contains(&kind) {
                continue;
            } else {
                let create = kind == "create";
                steps.push(JournalStep {
                    step: row[1].trim().parse()?,
                    action: kind.into(),
                    event_id: if create { None } else { Some(row[3].clone()).filter(|e| !e.is_empty()) },
                    before: if row[4].is_empty() { None } else { Some(EventState::from_json_dict(&parse(&row[4])?)?) },
                    after: if row[5].is_empty() { None } else { Some(EventState::from_json_dict(&parse(&row[5])?)?) },
                    status: row[6].clone(),
                    reason: row[7].clone(),
                    row: sheet_row,
                    key: if create { Some(row[3].clone()).filter(|e| !e.is_empty()) } else { None },
                });
            }
        }
        let Some(mut compaction) = compaction else { return Ok(None) };
        compaction.decisions = decisions;
        steps.sort_by_key(|s| s.step);
        compaction.steps = steps;
        if compaction.proposal.is_some() && compaction.day == 1 {
            compaction.meta = Some(RevisionMeta::from_detail(&detail, claude)?);
        }
        Ok(Some(compaction))
    };
    match inner()? {
        Some(compaction) => Ok(compaction),
        None => Err(error(format!("there's no compaction with id {}", py::repr(compaction_id)), "unknown_compaction").into()),
    }
}
