//! Compacting notes: the application-level policy that ties the notes tab
//! (stores::notes), the calendar, the planner (compaction::plan) and the
//! write-ahead journal (stores::journal) together.
//!
//! A compaction is a *proposal* the user confirms (see
//! compaction::proposals, and docs/compaction-proposals.md for the
//! contract). The flow, as the MCP tools expose it:
//!
//! 1. `prepare` -- read-only. Hands the client every day from the last
//!    compaction up to now, notes or not (each uncompacted note with a
//!    stable id and a shortlist of nearby planned events), those days'
//!    planned events, the two side by side as a `Timeline` (see
//!    timeline.rs), and the open proposal, if there is one. The
//!    client compares them and decides, event by event, what the notes show
//!    happened differently.
//! 2. `dry_run` (compact_notes) -- validates the client's decisions, lays
//!    the user's edits over them, and writes the plan to the journal as a
//!    proposal's revision, returning it (and the resulting timeline).
//!    Nothing on the calendar changes. The user reviews it in the app:
//!    `amend` (their edits), `add_note`/`withdraw_note` (feedback for the
//!    client, answered by its next revision), `get_proposal`.
//! 3. `confirm` -- the user's alone: plans the revision again, and if
//!    nothing changed, applies it (`commit`), journaling each step as it
//!    goes, and stamping each day's notes as compacted once that day's steps
//!    are done. If it dies partway, `finish` resumes it: the journal
//!    remembers exactly what was confirmed and how far it got. A write that
//!    can never succeed proposes what's left of it again (`rebuild`). A
//!    proposal can be `abandon`ed.
//! 4. `record_judgments` -- the facts written, the client judges the traits
//!    of each event's people (see compaction::judgments). Nothing about
//!    judging is sent before the user approves the plan: the commit hands
//!    back the plan's final timeline and, beside it, the judgments due, in
//!    one compact list (each event's people and their parts, then each part
//!    and each person's history once). A compaction isn't complete until
//!    they're all judged. `judgments_due` hands them over again -- to
//!    finish, or redo.
//!
//! One span, all at once: one compaction takes on every day from the last
//! compaction up to now (at most `MAX_DAYS` of them), notes or not, and
//! plans them together, as one span, which the user reviews whole. It
//! starts in the day the last compaction ran in (before there's been one,
//! the one the oldest note falls in, or today): when the last end-of-day
//! sleep event that began before then ends (or at the note, if it's
//! earlier -- a note written before the planned wake-up time). It runs
//! through its last night: the end-of-day sleep event that ends after now
//! (or 24 hours past its last day's start, if there isn't one). Its other
//! nights are events like any other, each decided once; nothing about
//! them moves where a day starts, which is only how the timeline heads
//! its days. A cancelled last night -- no sleep -- runs it on to the next
//! night. One that ends later than planned -- slept in -- takes in the
//! events it now runs into, up to its new end, for the decisions to
//! settle (see `cut`). Before, each day was a compaction of its own,
//! planned after the one before it: a proposal journaled then, as a batch
//! of days, is planned as one span too. In the journal, it's one
//! compaction (see stores::journal) -- the compaction id
//! the MCP tools use -- applied, then stamped, whole. Notes written after
//! now wait for a later compaction.
//!
//! A day can take several compactions, so the events offered -- the
//! *compaction window* -- start at the later of the span's start and the
//! last *stamped* compaction's `now`: whatever an earlier compaction already
//! settled isn't offered again. Nor is anything after it skipped: a day
//! with no notes is confirmed as planned. The one event that ended within `LOOKBACK_MINUTES`
//! before the compaction window starts is offered too, so an event the last
//! compaction closed off at "now" (or the night's sleep) can still be
//! stretched. The latest compacted note, however long ago it was written,
//! is offered as `previous_note`: what the user last said before the window
//! often says what was going on as it began. The timeline shows it too, and
//! when the last compaction ran, as context, above the span's first day.
//! Every past event offered is recorded as on schedule unless the client's
//! decisions say otherwise (see compaction::plan).
//!
//! The events offered run on past `now`, through the span's last night,
//! because an event that ran long runs into what follows it, and nothing is
//! moved to make room: the decisions have to move what follows too, and
//! nothing may overlap any of the span's events, past or still to come (see
//! compaction::plan). So they're part of the plan -- and of the
//! proposal's `events`, as `planned` -- and confirming checks them too: one
//! the plan writes that has changed since makes a new revision, and one that
//! now overlaps what the plan keeps hands it to Claude (see `confirm`).
//! Events after the last night aren't read. But compaction is about recording the past,
//! so the timeline shown to the user stops at `now`: a later event appears
//! in it only if it's near enough to a note to be one of its candidates
//! (lunch at noon, for an 11:45 "starting lunch") or -- after a dry run --
//! if the plan changes it.
//!
//! `prepare` also garbage-collects the journal (`CompactionJournal::
//! garbage_collect`) before doing anything else -- see there, and
//! `NotedTimeSheet::garbage_collect` (called from `note`/`append` instead,
//! since that's where the notes tab grows), for what that means for row
//! numbers and ids.

use super::additions::{check_additions, create_additions, resolve, Added, Additions, Stores, REF_PREFIX};
use super::fact_changes::{self, FactKeeping, ProposedSummary};
use super::guide::{APPROVAL_RULE, DECISION_GUIDE};
use super::judgments::{HabitJudgmentsDue, Judging, Judgment, JudgmentRequest, JudgmentsDue, JudgmentsResult};
use super::plan::{
    error, error_of, facts_dict, facts_from_dict, plan_compaction, planned_timeline, repr_opt, CompactionChange, CompactionPlan,
    EventDecision, EventState, Issue, PlanNote, PlanOptions,
};
use super::proposals::{
    change_json, claude_key, claude_key_number, decision_shape, edit_id, feedback_id, merge, new_proposal_id, revision_id, settle_refs,
    split_feedback_id, AdditionChoice, AdditionSettled, Dict, Feedback, FeedbackReply, MergeOptions, Merged, NoteEdit, Proposal, ProposalContext,
    ProposalEvent, ProposalNote, ProposalResult, ProposalSummary, Recap, UserEdit,
};
use crate::determinism;
use crate::event::{Event, Moment};
use crate::facts::{fact_problems, Facts, SELF_ID};
use crate::google::calendar::BATCH_SIZE;
use crate::google::{BatchRequest, GoogleError};
use crate::py;
use crate::stores::action_calendar::ActionCalendar;
use crate::stores::actions::{share_ids, ActionShare, ActionTree, Actions};
use crate::stores::cancellations::Cancellations;
use crate::stores::event_changes::{ChangeError, Problem};
use crate::stores::goals::{self, share_problems, Goals};
use crate::stores::groups::{python_repr, python_str, truthy};
use crate::stores::habits::{subject_id, SUBJECT_PREFIX};
use crate::stores::journal::{
    CompactionJournal, JournalCompaction, JournalStep, PlannedDay, RevisionMeta, ABANDONED, APPLIED, APPLYING, FAILED, OPEN_STATUSES,
    PLANNED, PROPOSED, STAMPED, SUPERSEDED,
};
use crate::stores::locations::{listed, Locations};
use crate::stores::notes::{NotedTime, NotedTimeSheet, SheetNote};
use crate::stores::people::People;
use crate::timeline::{join_days, render, split_days, Timeline};
use chrono::Duration;
use serde_json::{json, Map, Value};
use sha1::{Digest, Sha1};

const CANDIDATE_MINUTES: i64 = 60;
const HINT_HISTORY_DAYS: i64 = 28;
const MAX_DAYS: i64 = 7;
const LOOKBACK_MINUTES: i64 = 15;

fn prefetch_before() -> Duration {
    Duration::hours(24) + Duration::minutes(LOOKBACK_MINUTES)
}

fn prefetch_after() -> Duration {
    Duration::hours(48)
}

/// A CompactionError, as an error to return.
fn ce(message: impl Into<String>, category: &str) -> anyhow::Error {
    error(message, category).into()
}

fn ce_of(problems: &[String], category: &str) -> anyhow::Error {
    let issues: Vec<Issue> = problems.iter().map(|p| Issue::plain(p.clone())).collect();
    error_of(&issues, category).into()
}

/// The action shares and the people's and places' shares `plan` gives
/// events must fit in their events.
fn check_shares(plan: &CompactionPlan) -> anyhow::Result<()> {
    let mut problems: Vec<String> = Vec::new();
    for change in &plan.changes {
        let Some(after) = &change.after else { continue };
        let before = change.before.as_ref();
        let minutes = match (after.start, after.end) {
            (Some(start), Some(end)) => (end - start).num_minutes(),
            _ => continue,
        };
        let who = format!("{} {}", change.action, repr_opt(change.event_id.as_deref().or(after.summary.as_deref())));
        if after.action_ids.is_some() && after.action_ids.as_ref() != before.and_then(|b| b.action_ids.as_ref()) {
            let shares = after.action_ids.as_deref().unwrap_or_default();
            problems.extend(crate::stores::shares::share_problems(shares, minutes).into_iter().map(|p| format!("{who}: {p}")));
        }
        if after.facts.is_some() && after.facts.as_ref() != before.and_then(|b| b.facts.as_ref()) {
            let facts = super::plan::facts_from_dict(after.facts.as_ref());
            problems.extend(facts.share_problems(minutes).into_iter().map(|p| format!("{who}: its facts' {p}")));
        }
    }
    if !problems.is_empty() {
        return Err(ce_of(&problems, "shares"));
    }
    Ok(())
}

/// The CompactionError `error` is, if it's one.
pub fn compaction_error(error: &anyhow::Error) -> Option<&ChangeError> {
    error.downcast_ref::<ChangeError>()
}

fn google_status(error: &anyhow::Error) -> Option<u16> {
    error.downcast_ref::<GoogleError>().map(|e| e.status)
}

/// Python's max() of two: the first of equals.
fn pmax(a: Moment, b: Moment) -> Moment {
    if b > a { b } else { a }
}

fn when(moment: &Option<Moment>) -> Value {
    moment.map(|m| json!(py::pydantic_datetime(&m))).unwrap_or(Value::Null)
}

fn iso(moment: &Moment) -> String {
    py::isoformat(moment)
}

fn lookup(map: &[(String, String)], key: &str) -> Option<String> {
    map.iter().rev().find(|(k, _)| k == key).map(|(_, v)| v.clone())
}

// -- what the tools hand back -------------------------------------------------

pub struct ContextNote {
    pub id: String,
    pub timestamp: Moment,
    pub description: Option<String>,
    pub candidates: Vec<String>,
}

pub struct ContextEvent {
    pub id: String,
    pub summary: Option<String>,
    pub start: Moment,
    pub end: Moment,
    pub description: Option<String>,
    pub action_ids: Option<Vec<ActionShare>>,
    pub action_names: Option<Vec<String>>,
    pub suggested_action_ids: Option<Vec<String>>,
    pub priority: Option<i64>,
    pub facts: Option<Facts>,
    pub history_until: Option<Moment>,
}

pub struct ContextDay {
    pub label: String,
    pub compaction_window_start: Moment,
    pub day_end: Moment,
    pub note_ids: Vec<String>,
}

#[derive(Default)]
pub struct CompactionContext {
    pub notes: Vec<ContextNote>,
    pub events: Vec<ContextEvent>,
    pub now: Option<Moment>,
    pub day_end: Option<Moment>,
    pub remaining_note_count: usize,
    pub days: Option<Vec<ContextDay>>,
    pub open_compaction: Option<String>,
    pub judgments_pending: Option<String>,
    pub compaction_window_start: Option<Moment>,
    pub timeline: Option<Timeline>,
    pub actions: Option<Vec<Value>>,
    pub people: Option<Vec<Value>>,
    pub locations: Option<Vec<Value>>,
    pub goals: Option<Vec<Value>>,
    pub previous_note: Option<NotedTime>,
    pub proposal: Option<ProposalContext>,
    pub trait_facts: Option<Vec<Value>>,
}

impl CompactionContext {
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert(
            "notes".into(),
            Value::Array(
                self.notes
                    .iter()
                    .map(|n| {
                        json!({
                            "id": n.id,
                            "timestamp": py::pydantic_datetime(&n.timestamp),
                            "description": n.description,
                            "candidates": n.candidates,
                        })
                    })
                    .collect(),
            ),
        );
        out.insert(
            "events".into(),
            Value::Array(
                self.events
                    .iter()
                    .map(|e| {
                        let mut m = Map::new();
                        m.insert("id".into(), json!(e.id));
                        m.insert("summary".into(), json!(e.summary));
                        m.insert("start".into(), json!(py::pydantic_datetime(&e.start)));
                        m.insert("end".into(), json!(py::pydantic_datetime(&e.end)));
                        m.insert("description".into(), json!(e.description));
                        m.insert("action_ids".into(), json!(e.action_ids));
                        m.insert("action_names".into(), json!(e.action_names));
                        m.insert("suggested_action_ids".into(), json!(e.suggested_action_ids));
                        m.insert("priority".into(), json!(e.priority));
                        m.insert("facts".into(), e.facts.as_ref().map(Facts::to_public).unwrap_or(Value::Null));
                        m.insert("history_until".into(), when(&e.history_until));
                        Value::Object(m)
                    })
                    .collect(),
            ),
        );
        out.insert("now".into(), when(&self.now));
        out.insert("day_end".into(), when(&self.day_end));
        out.insert("remaining_note_count".into(), json!(self.remaining_note_count));
        out.insert(
            "days".into(),
            match &self.days {
                None => Value::Null,
                Some(days) => Value::Array(
                    days.iter()
                        .map(|d| {
                            json!({
                                "label": d.label,
                                "compaction_window_start": py::pydantic_datetime(&d.compaction_window_start),
                                "day_end": py::pydantic_datetime(&d.day_end),
                                "note_ids": d.note_ids,
                            })
                        })
                        .collect(),
                ),
            },
        );
        out.insert("open_compaction".into(), json!(self.open_compaction));
        out.insert("judgments_pending".into(), json!(self.judgments_pending));
        out.insert("compaction_window_start".into(), when(&self.compaction_window_start));
        out.insert("timeline".into(), self.timeline.as_ref().map(Timeline::to_json).unwrap_or(Value::Null));
        out.insert("actions".into(), json!(self.actions));
        out.insert("people".into(), json!(self.people));
        out.insert("locations".into(), json!(self.locations));
        out.insert("goals".into(), json!(self.goals));
        out.insert("trait_facts".into(), json!(self.trait_facts));
        out.insert(
            "previous_note".into(),
            self.previous_note
                .as_ref()
                .map(|n| json!({"timestamp": py::pydantic_datetime(&n.timestamp), "description": n.description}))
                .unwrap_or(Value::Null),
        );
        out.insert("proposal".into(), self.proposal.as_ref().map(ProposalContext::to_json).unwrap_or(Value::Null));
        out.insert("instructions".into(), json!(DECISION_GUIDE));
        Value::Object(out)
    }
}

#[derive(Default)]
pub struct CompactionResult {
    pub status: String,
    pub message: String,
    pub compaction_id: Option<String>,
    pub proposal_id: Option<String>,
    pub revision: Option<i64>,
    pub changes: Vec<CompactionChange>,
    pub warnings: Vec<String>,
    pub timeline: Option<Timeline>,
    pub additions: Option<Map<String, Value>>,
    pub judgments: Option<JudgmentsDue>,
}

impl CompactionResult {
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("status".into(), json!(self.status));
        out.insert("message".into(), json!(self.message));
        out.insert("compaction_id".into(), json!(self.compaction_id));
        out.insert("proposal_id".into(), json!(self.proposal_id));
        out.insert("revision".into(), json!(self.revision));
        out.insert("changes".into(), Value::Array(self.changes.iter().map(change_json).collect()));
        out.insert("warnings".into(), json!(self.warnings));
        out.insert("timeline".into(), self.timeline.as_ref().map(Timeline::to_json).unwrap_or(Value::Null));
        out.insert("additions".into(), self.additions.clone().map(Value::Object).unwrap_or(Value::Null));
        out.insert("judgments".into(), self.judgments.as_ref().map(JudgmentsDue::to_json).unwrap_or(Value::Null));
        Value::Object(out)
    }
}

// -- the span -----------------------------------------------------------------

#[derive(Debug, Clone)]
pub struct Day {
    pub notes: Vec<SheetNote>,
    pub events: Vec<Event>,
    pub day_start: Moment,
    pub compaction_window_start: Moment,
    pub day_end: Moment,
    pub now: Moment,
    pub previous: Option<Event>,
    pub latest_compacted: Option<NotedTime>,
    pub last_compaction: Option<Moment>,
    pub history_until: Option<Moment>,
    pub closing: Option<Event>,
    pub nights: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct Walked {
    pub day: Day,
    pub plan: CompactionPlan,
    pub decisions: Vec<EventDecision>,
    pub ignore_notes: Vec<String>,
    pub note_targets: Vec<(String, Option<String>)>,
}

/// A revision planned again, or why it can't be.
pub enum Replanned {
    Walked(Vec<Walked>),
    Failed(ChangeError),
}

#[derive(Clone)]
pub struct NoteCompactor {
    pub calendar: ActionCalendar,
    pub client: ActionCalendar,
    pub notes: NotedTimeSheet,
    pub journal: CompactionJournal,
    pub actions: Option<Actions>,
    pub people: Option<People>,
    pub locations: Option<Locations>,
    pub goals: Option<Goals>,
    pub judging: Option<Judging>,
    pub cancellations: Option<Cancellations>,
    /// What people's trait facts are kept with: without it, compaction
    /// neither shows nor changes them.
    pub facts: Option<FactKeeping>,
}

impl NoteCompactor {
    fn clock(&self) -> Moment {
        determinism::now()
    }

    fn tree(&self) -> anyhow::Result<Option<ActionTree>> {
        match &self.actions {
            Some(a) => Ok(Some(a.tree()?)),
            None => Ok(None),
        }
    }

    /// The span's calendar: what the plan's days are read from (which
    /// nothing applies changes to any more), only what overlaps the window.
    fn span_events(&self, time_min: Moment, time_max: Moment) -> anyhow::Result<Vec<Event>> {
        Ok(self
            .calendar
            .list_events(&time_min, &time_max)?
            .into_iter()
            .filter(|e| e.end() > time_min && e.start() < time_max)
            .collect())
    }

    pub fn prepare(&self) -> anyhow::Result<CompactionContext> {
        self.prefetch_tabs(true)?;
        let pending = self.judgments_pending()?;
        self.journal.garbage_collect()?;
        let open_ids = self.journal.open_compactions()?;
        let open_id = match open_ids.first() {
            Some((id, _)) => Some(self.journal.load(id)?.batch_id()),
            None => None,
        };
        let proposal = self.proposal_context()?;
        let (day, remaining) = self.walk(self.clock(), None, MAX_DAYS)?;
        let Some(day) = day else {
            return Ok(CompactionContext { open_compaction: open_id, judgments_pending: pending, proposal, ..Default::default() });
        };
        let tree = self.tree()?;
        let names = self.names(None)?;
        let suggested = match &tree {
            Some(tree) => self.suggestions(std::slice::from_ref(&day), tree)?,
            None => Vec::new(),
        };
        let mut notes: Vec<ContextNote> = Vec::new();
        let mut events: Vec<ContextEvent> = Vec::new();
        let mut timelines: Vec<(Moment, Timeline)> = Vec::new();
        {
            let befores = befores(&day);
            let candidates: Vec<(String, Vec<String>)> = day
                .notes
                .iter()
                .map(|n| (n.id(), candidates(n.note.timestamp, &day.events, lookup_event(&befores, &n.id()))))
                .collect();
            for n in &day.notes {
                notes.push(ContextNote {
                    id: n.id(),
                    timestamp: n.note.timestamp,
                    description: n.note.description.clone(),
                    candidates: candidates.iter().rev().find(|(k, _)| *k == n.id()).map(|(_, v)| v.clone()).unwrap_or_default(),
                });
            }
            for e in &day.events {
                let Some(id) = e.id.clone().filter(|i| !i.is_empty()) else { continue };
                if events.iter().any(|c| c.id == id) {
                    continue;
                }
                events.push(ContextEvent {
                    id: id.clone(),
                    summary: e.summary.clone(),
                    start: e.start(),
                    end: e.end(),
                    description: e.description.clone(),
                    action_ids: e.action_ids.clone(),
                    action_names: e.action_ids.as_ref().map(|ids| ids.iter().map(|a| lookup(&names, &a.action_id).unwrap_or(a.action_id.clone())).collect()),
                    suggested_action_ids: suggested.iter().rev().find(|(k, _)| *k == id).map(|(_, v)| v.clone()),
                    priority: e.effective_priority(),
                    facts: e.facts.clone(),
                    history_until: day.history_until.filter(|h| e.start() < *h).map(|h| e.end().min(h)),
                });
            }
            let shown = shown(&day, &candidates);
            timelines.extend(split_days(
                &planned_timeline(
                    &plan_notes(&day),
                    &shown,
                    day.now,
                    &names,
                    &suggested,
                    latest_compacted(&day).as_ref(),
                    day.last_compaction,
                ),
                &day_starts(&day, &[]),
            ));
        }
        let previous_note = day.latest_compacted.clone();
        Ok(CompactionContext {
            notes,
            events,
            now: Some(day.now),
            day_end: Some(day.day_end),
            remaining_note_count: remaining,
            open_compaction: open_id,
            compaction_window_start: Some(day.compaction_window_start),
            days: context_days(&day),
            timeline: Some(join_days(timelines)),
            actions: tree.as_ref().map(|t| {
                t.actions
                    .iter()
                    .filter(|a| matches!(a.status, Some(crate::stores::actions::ActionStatus::Active | crate::stores::actions::ActionStatus::Proposed)))
                    .map(|a| {
                        let mut listed = json!({"id": a.id, "name": a.name, "note": a.note});
                        // Only a commute says so: the rest are taken not to be.
                        if a.commute == Some(true) {
                            listed["commute"] = json!(true);
                        }
                        listed
                    })
                    .collect()
            }),
            people: match &self.people {
                Some(p) => {
                    let kept = match &self.facts {
                        Some(f) => Some((f.traits.all()?, f.person_facts.by_person()?)),
                        None => None,
                    };
                    let active: Vec<String> = p.get_people(None)?.into_iter().filter_map(|p| p.id).collect();
                    Some(
                        p.all()?
                            .iter()
                            .filter(|p| p.id.as_ref().is_some_and(|i| active.contains(i)))
                            .map(|p| {
                                let mut listed = json!({"id": p.id, "name": p.name, "context": p.context});
                                if let Some((traits, summaries)) = &kept {
                                    listed["facts"] = json!(fact_changes::person_facts(p, traits, summaries));
                                }
                                listed
                            })
                            .collect(),
                    )
                }
                None => None,
            },
            trait_facts: match &self.facts {
                Some(f) => Some(fact_changes::trait_facts(&f.traits.all()?)),
                None => None,
            },
            locations: match &self.locations {
                Some(l) => {
                    let groups = l.groups()?;
                    Some(
                        l.all()?
                            .iter()
                            .map(|l| {
                                let mut context = json!({"id": l.id, "name": l.name, "hint": l.hint});
                                // Where it is among the groups, if it's in one.
                                if l.group_id.as_deref().is_some_and(|g| !g.is_empty()) {
                                    context["path"] = json!(listed(l, &groups).path);
                                }
                                context
                            })
                            .collect(),
                    )
                }
                None => None,
            },
            goals: match &self.goals {
                Some(g) => Some(open_goals(&g.all()?)),
                None => None,
            },
            previous_note,
            judgments_pending: pending,
            proposal,
        })
    }

    pub fn prefetch(&self) -> anyhow::Result<()> {
        self.prefetch_tabs(true)
    }

    /// The stores additions go into: with the calendar's time zone if
    /// `additions` adds goals.
    fn stores(&self, additions: &Additions) -> anyhow::Result<Stores<'_>> {
        let zone = if additions.goals.is_empty() { None } else { Some(self.calendar.client().get_time_zone()?) };
        Ok(Stores { actions: self.actions.as_ref(), people: self.people.as_ref(), locations: self.locations.as_ref(), goals: self.goals.as_ref(), zone })
    }

    fn prefetch_tabs(&self, facts: bool) -> anyhow::Result<()> {
        let mut tabs = vec![self.notes.whole_tab(), self.journal.whole_tab()];
        if facts {
            if let Some(a) = &self.actions {
                tabs.extend(a.whole_tabs());
            }
            if let Some(p) = &self.people {
                tabs.extend(p.whole_tabs());
            }
            if let Some(l) = &self.locations {
                tabs.extend(l.whole_tabs());
            }
            if let Some(g) = &self.goals {
                tabs.push(g.whole_tab());
            }
            if let Some(j) = &self.judging {
                tabs.extend(j.whole_tabs());
            }
            if let Some(c) = &self.cancellations {
                tabs.extend(c.whole_tabs());
            }
            if let Some(f) = &self.facts {
                tabs.push(f.traits.whole_tab());
                tabs.extend(f.person_facts.whole_tabs());
            }
        }
        self.notes.prefetch(&tabs)
    }

    /// Every action's, person's and location's id (and every ref in
    /// `additions`), with its name.
    fn names(&self, additions: Option<&Additions>) -> anyhow::Result<Vec<(String, String)>> {
        let mut names: Vec<(String, String)> = Vec::new();
        if let Some(a) = &self.actions {
            names.extend(a.all()?.into_iter().filter_map(|a| {
                let id = a.id.filter(|i| !i.is_empty())?;
                Some((id.clone(), a.name.filter(|n| !n.is_empty()).unwrap_or(id)))
            }));
        }
        if let Some(p) = &self.people {
            names.extend(p.all()?.into_iter().filter_map(|p| {
                let id = p.id.filter(|i| !i.is_empty())?;
                Some((id.clone(), p.name.filter(|n| !n.is_empty()).unwrap_or(id)))
            }));
        }
        if let Some(l) = &self.locations {
            names.extend(l.all()?.into_iter().filter_map(|l| {
                let id = l.id.filter(|i| !i.is_empty())?;
                Some((id.clone(), l.name.filter(|n| !n.is_empty()).unwrap_or(id)))
            }));
        }
        if let Some(additions) = additions {
            names.extend(additions.refs().into_iter().map(|(r, name)| (r, format!("{name} (new)"))));
        }
        Ok(names)
    }

    fn suggestions(&self, days: &[Day], tree: &ActionTree) -> anyhow::Result<Vec<(String, Vec<String>)>> {
        let bare: Vec<&Event> = days
            .iter()
            .flat_map(|day| {
                day.events.iter().filter(move |e| {
                    e.id.as_deref().is_some_and(|i| !i.is_empty())
                        && e.action_ids.as_ref().is_none_or(|a| a.is_empty())
                        && e.end() <= day.now
                        && e.summary.as_deref().is_some_and(|s| !s.is_empty())
                })
            })
            .collect();
        if bare.is_empty() {
            return Ok(Vec::new());
        }
        let start = days[0].compaction_window_start;
        let mut history = self.calendar.list_events(&(start - Duration::days(HINT_HISTORY_DAYS)), &start)?;
        history.sort_by_key(|e| e.start());
        let mut hints: Vec<(String, Vec<String>)> = Vec::new();
        for event in &history {
            let usable: Vec<String> = event
                .action_ids
                .iter()
                .flatten()
                .filter(|a| tree.by_id(&a.action_id).is_some_and(|action| action.status != Some(crate::stores::actions::ActionStatus::Deleted)))
                .map(|a| a.action_id.clone())
                .collect();
            if let Some(summary) = event.summary.as_deref().filter(|s| !s.is_empty()) {
                if !usable.is_empty() && !event.is_cancelled() {
                    let key = title_key(summary);
                    match hints.iter_mut().find(|(k, _)| *k == key) {
                        Some(slot) => slot.1 = usable,
                        None => hints.push((key, usable)),
                    }
                }
            }
        }
        let mut out: Vec<(String, Vec<String>)> = Vec::new();
        for e in bare {
            if let Some((_, hint)) = hints.iter().find(|(k, _)| *k == title_key(e.summary.as_deref().unwrap())) {
                let id = e.id.clone().unwrap();
                match out.iter_mut().find(|(k, _)| *k == id) {
                    Some(slot) => slot.1 = hint.clone(),
                    None => out.push((id, hint.clone())),
                }
            }
        }
        Ok(out)
    }

    /// compact_notes: propose what happened, or revise the open proposal.
    #[allow(clippy::too_many_arguments)]
    pub fn dry_run(
        &self,
        decisions: Vec<EventDecision>,
        ignore_notes: Option<Vec<String>>,
        claude_additions: Additions,
        proposal_id: Option<String>,
        revision: Option<i64>,
        replies: Vec<FeedbackReply>,
        annotate_notes: Vec<(String, String)>,
        recap: Option<Recap>,
        create_refs: Vec<Option<String>>,
        fact_summaries: Vec<ProposedSummary>,
    ) -> anyhow::Result<CompactionResult> {
        self.prefetch_tabs(true)?;
        self.journal.garbage_collect()?;
        self.require_no_open_compaction()?;
        let open_id = self.journal.open_proposal()?;
        if let (Some(open), None) = (&open_id, &proposal_id) {
            return Err(ce(
                format!(
                    "proposal {open} is open: revise or extend it -- compact_notes with proposal_id={} and the revision you started from (prepare_compaction's `proposal` shows it)",
                    py::repr(open)
                ),
                "open_proposal",
            ));
        }
        if let Some(given) = &proposal_id {
            if Some(given) != open_id.as_ref() {
                return Err(ce(
                    format!(
                        "there's no open proposal {}{}",
                        py::repr(given),
                        open_id.as_ref().map(|o| format!("; the open one is {o}")).unwrap_or_default()
                    ),
                    "unknown_proposal",
                ));
            }
        }
        let claude_targets = note_targets(&annotate_notes)?;
        let now = self.clock();
        let mut current: Option<Vec<JournalCompaction>> = None;
        let mut edit_rows: Vec<(i64, UserEdit)> = Vec::new();
        let mut feedback_rows: Vec<(i64, Feedback)> = Vec::new();
        let mut base_meta: Option<RevisionMeta> = None;
        let proposal: String;
        let meta: Option<RevisionMeta>;
        match &open_id {
            None => {
                proposal = new_proposal_id(&determinism::random_token("0123456789abcdef", 32));
                meta = None;
            }
            Some(open) => {
                proposal = open.clone();
                let days = self.current(&proposal)?;
                meta = days[0].meta.clone();
                base_meta = Some(self.base(&proposal, meta.as_ref().unwrap(), revision)?);
                current = Some(days);
                edit_rows = self.journal.user_edits(&proposal)?;
                feedback_rows = self.journal.feedback(&proposal)?;
            }
        }
        let (answered, replaced) = self.answers(&proposal, &replies, &feedback_rows, &edit_rows, base_meta.as_ref())?;
        let edits: Vec<UserEdit> = edit_rows.iter().filter(|(row, _)| !replaced.contains(row)).map(|(_, e)| e.clone()).collect();
        let settled_additions = merge(&proposal, &[], &edits, MergeOptions::default()).settled;
        let additions = unsettled(&claude_additions, &settled_additions);
        let problems = check_additions(&additions, self.stores(&additions)?, &settled_ids(&settled_additions))?;
        if !problems.is_empty() {
            return Err(ce_of(&problems, "additions"));
        }
        let settled_refs: Vec<String> = settled_additions.keys().cloned().collect();
        let decisions = self.checked_facts(decisions, &claude_additions, &settled_refs)?;
        self.check_fact_changes(&decisions, &fact_summaries, &claude_additions, &settled_refs)?;
        let (decisions, key_seq) = keyed(&proposal, decisions, meta.as_ref())?;
        let refs = create_refs_of(&decisions, &create_refs)?;
        let claude_targets: Vec<(String, String)> =
            claude_targets.into_iter().map(|(note, event)| (note, lookup(&refs, &event).unwrap_or(event))).collect();
        let aliases: Vec<(String, String)> = meta.as_ref().map(|m| m.aliases.clone()).unwrap_or_default();
        let settled: Vec<String> = meta.as_ref().map(|m| m.settled.clone()).unwrap_or_default();
        let ignore: Vec<String> = ignore_notes.clone().unwrap_or_default();
        let known_ids = self.known_ids(now)?;
        let known_notes = self.uncompacted_notes(now)?;
        let merged = merge(
            &proposal,
            &decisions,
            &edits,
            MergeOptions {
                known_ids: Some(&known_ids),
                aliases: &aliases,
                settled: &settled,
                claude_ignore: &ignore,
                claude_targets: &claude_targets,
                known_notes: Some(&known_notes),
            },
        );
        let (walked, remaining) = self.plan_walk(&settled_decisions(&merged), &merged.ignore_notes, &additions, now, &merged.note_targets.items)?;
        if walked.is_empty() || (meta.is_none() && !has_anything(&walked)) {
            return Ok(CompactionResult {
                status: "nothing_to_compact".into(),
                message: "nothing has happened to compact since the last compaction".into(),
                ..Default::default()
            });
        }
        let recap = match recap {
            Some(recap) => {
                let recap = recap.resolved(&refs);
                let events: Vec<String> = proposal_events(&walked, &merged.decided_by).into_iter().map(|e| e.id).collect();
                let notes: Vec<String> = walked.iter().flat_map(|w| w.day.notes.iter().map(|n| n.id())).collect();
                let problems = recap.problems(&events, &notes);
                if !problems.is_empty() {
                    return Err(ce_of(&problems, "recap"));
                }
                Some(recap)
            }
            None => None,
        };
        let superseded = self.supersede_planned()?;
        let revision_number = meta.as_ref().map(|m| m.revision + 1).unwrap_or(1);
        let outcomes = outcomes_of(&walked);
        let mut claude_ignore: Vec<String> = ignore.clone();
        claude_ignore.sort();
        claude_ignore.dedup();
        let mut settled_sorted = settled.clone();
        settled_sorted.sort();
        settled_sorted.dedup();
        let new_meta = RevisionMeta {
            proposal: proposal.clone(),
            revision: revision_number,
            window_start: Some(walked[0].day.compaction_window_start),
            created: Some(now),
            base: revision,
            user_seq: edit_rows.iter().map(|(_, e)| e.seq).max().unwrap_or(0),
            by: "claude".into(),
            reason: if meta.is_none() {
                "proposed".into()
            } else if !answered.is_empty() {
                "revised for notes".into()
            } else {
                "extended".into()
            },
            changed: changed(meta.as_ref().map(|m| m.outcomes.as_slice()), &outcomes),
            outcomes,
            key_seq,
            claude_decisions: decisions.clone(),
            claude_ignore_notes: Some(claude_ignore),
            claude_note_targets: claude_targets.clone(),
            claude_additions: Some(claude_additions.to_json_dict()),
            aliases: aliases.clone(),
            settled: settled_sorted,
            judge_also: meta.as_ref().map(|m| m.judge_also.clone()).unwrap_or_default(),
            claude_through: Some(walked.last().unwrap().day.now),
            claude_recap: recap.as_ref().map(Recap::to_json_dict),
            recap_revision: recap.as_ref().map(|_| revision_number),
            claude_fact_summaries: ProposedSummary::all_to_json(&fact_summaries),
        };
        self.write_revision(&walked, &new_meta, &additions, current, &[])?;
        let newer: Vec<(String, String)> = edit_rows
            .iter()
            .filter(|(_, e)| base_meta.as_ref().is_some_and(|b| e.seq > b.user_seq))
            .map(|(_, e)| (e.event_id.clone().unwrap_or_default(), e.id.clone()))
            .collect();
        for (row, item) in &answered {
            let mut item = item.clone();
            if let Some(newer) = item.event_id.as_ref().and_then(|e| lookup(&newer, e)) {
                item.superseded_by = Some(vec![newer]);
            }
            item.answered_in = Some(revision_number);
            self.journal.update_feedback(*row, &item)?;
        }
        for row in &replaced {
            self.journal.set_user_edit_status(*row, "replaced")?;
        }
        let unknown: Vec<&String> = merged.unknown.iter().map(|e| &e.id).collect();
        for (row, edit) in &edit_rows {
            if unknown.contains(&&edit.id) && !replaced.contains(row) {
                self.journal.set_user_edit_status(*row, "inapplicable")?;
            }
        }
        let changes: Vec<CompactionChange> = walked.iter().flat_map(|w| w.plan.changes.clone()).collect();
        let note_count: usize = walked.iter().map(|w| w.day.notes.len()).sum();
        let day_count: usize = walked.iter().map(|w| day_starts(&w.day, &w.decisions).len()).sum();
        let days = if day_count > 1 { format!(" over {day_count} days") } else { String::new() };
        let answered_ids: Vec<&String> = answered.iter().map(|(_, i)| &i.id).collect();
        let still_open = feedback_rows.iter().filter(|(_, f)| f.status == "open" && !answered_ids.contains(&&f.id)).count();
        let mut message = format!(
            "proposal {proposal} revision {revision_number}: {} calendar change(s) for {note_count} note(s){days}, through {}; nothing has been changed yet. In a conversation, show the user `timeline.text` in a code block (see the instructions from prepare_compaction) and the warnings. {APPROVAL_RULE}",
            changes.len(),
            iso(&walked.last().unwrap().day.now)
        );
        if superseded > 0 {
            message.push_str(&format!(" (Replaced {superseded} unapplied plan(s) from before proposals.)"));
        }
        if still_open > 0 {
            message.push_str(&format!(
                " {still_open} feedback item(s) came in after your revision began and are still open: prepare_compaction again and answer them."
            ));
        }
        if remaining > 0 {
            message.push_str(&format!(" {remaining} later note(s) aren't part of it: they'll be in the next proposal."));
        }
        let additions_json = additions.to_json_dict();
        Ok(CompactionResult {
            status: "proposed".into(),
            compaction_id: Some(revision_id(&proposal, revision_number)),
            proposal_id: Some(proposal.clone()),
            revision: Some(revision_number),
            warnings: walked.iter().flat_map(|w| w.plan.warnings.clone()).collect(),
            timeline: Some(joined(&walked)),
            additions: if additions_json.is_empty() { None } else { Some(additions_json) },
            changes,
            message,
            judgments: None,
        })
    }

    /// `_plan_walk`: walk the span up to `now`, planning it with
    /// `decisions`, the notes to ignore and those added to a particular
    /// event.
    fn plan_walk(
        &self,
        decisions: &[EventDecision],
        ignore_notes: &[String],
        additions: &Additions,
        now: Moment,
        note_targets: &[(String, Option<String>)],
    ) -> anyhow::Result<(Vec<Walked>, usize)> {
        let night = |event_id: &str| decisions.iter().find(|d| d.event_id.as_deref() == Some(event_id)).cloned();
        let (day, remaining) = self.walk(now, Some(&night), MAX_DAYS)?;
        let Some(day) = day else { return Ok((Vec::new(), remaining)) };
        let plan = self.plan(&day, decisions, ignore_notes, additions, note_targets)?;
        Ok((
            vec![Walked { day, plan, decisions: decisions.to_vec(), ignore_notes: ignore_notes.to_vec(), note_targets: note_targets.to_vec() }],
            remaining,
        ))
    }

    fn write_revision(
        &self,
        walked: &[Walked],
        meta: &RevisionMeta,
        additions: &Additions,
        previous: Option<Vec<JournalCompaction>>,
        edits: &[UserEdit],
    ) -> anyhow::Result<()> {
        let batch = revision_id(&meta.proposal, meta.revision);
        let days: Vec<PlannedDay> = walked
            .iter()
            .enumerate()
            .map(|(i, w)| PlannedDay {
                compaction_id: day_id(&batch, i + 1),
                now: w.day.now,
                note_ids: w.day.notes.iter().map(SheetNote::id).collect(),
                decisions: &w.decisions,
                plan: &w.plan,
                ignore_notes: w.ignore_notes.clone(),
                additions: if i == 0 { additions.to_json_dict() } else { Map::new() },
                note_targets: w.note_targets.clone(),
            })
            .collect();
        self.journal.start_batch(&days, Some(meta), edits)?;
        for mut day in previous.unwrap_or_default() {
            if day.status == PROPOSED || day.status == PLANNED {
                self.journal.set_status(&mut day, SUPERSEDED)?;
            }
        }
        Ok(())
    }

    fn current(&self, proposal: &str) -> anyhow::Result<Vec<JournalCompaction>> {
        let revisions = self.journal.revisions(proposal)?;
        let Some((_, first, _)) = revisions.last() else {
            return Err(ce(format!("there's no proposal {}", py::repr(proposal)), "unknown_proposal"));
        };
        self.journal.load_batch(first)
    }

    fn open(&self, proposal_id: &str) -> anyhow::Result<Vec<JournalCompaction>> {
        let open_id = self.journal.open_proposal()?;
        if Some(proposal_id) != open_id.as_deref() {
            return Err(ce(
                format!(
                    "proposal {} isn't open{}",
                    py::repr(proposal_id),
                    match &open_id {
                        Some(o) => format!("; the open one is {o}"),
                        None => "; there's no open proposal".into(),
                    }
                ),
                "unknown_proposal",
            ));
        }
        self.current(proposal_id)
    }

    fn base(&self, proposal: &str, meta: &RevisionMeta, revision: Option<i64>) -> anyhow::Result<RevisionMeta> {
        let Some(revision) = revision.filter(|r| (1..=meta.revision).contains(r)) else {
            return Err(ce(
                format!(
                    "say which revision of proposal {proposal} you started from (revision=, 1 to {}; the current one is {})",
                    meta.revision, meta.revision
                ),
                "stale_revision",
            ));
        };
        let base = if revision == meta.revision { Some(meta.clone()) } else { self.journal.revision_meta(proposal, revision)? };
        base.ok_or_else(|| ce(format!("revision {revision} of proposal {proposal} is gone: prepare again"), "stale_revision"))
    }

    #[allow(clippy::type_complexity)]
    fn answers(
        &self,
        proposal: &str,
        replies: &[FeedbackReply],
        feedback_rows: &[(i64, Feedback)],
        edit_rows: &[(i64, UserEdit)],
        base: Option<&RevisionMeta>,
    ) -> anyhow::Result<(Vec<(i64, Feedback)>, Vec<i64>)> {
        let mut problems: Vec<String> = Vec::new();
        let mut answered: Vec<(i64, Feedback)> = Vec::new();
        for reply in replies {
            let Some((row, item)) = feedback_rows.iter().rev().find(|(_, f)| f.id == reply.feedback_id) else {
                problems.push(format!("replies: {} isn't feedback on proposal {proposal}", py::repr(&reply.feedback_id)));
                continue;
            };
            if item.status == "withdrawn" {
                continue;
            }
            if item.status != "open" {
                problems.push(format!("replies: {} is already {}", item.id, item.status));
                continue;
            }
            if reply.reply.trim().is_empty() {
                problems.push(format!("replies: the reply to {} is empty", item.id));
                continue;
            }
            let mut item = item.clone();
            item.status = "answered".into();
            item.reply = Some(reply.reply.trim().to_string());
            answered.push((*row, item));
        }
        if let Some(base) = base {
            let done: Vec<&String> = answered.iter().map(|(_, i)| &i.id).collect();
            let missed: Vec<String> = feedback_rows
                .iter()
                .filter(|(_, f)| f.status == "open" && f.created < base.created() && !done.contains(&&f.id))
                .map(|(_, f)| f.id.clone())
                .collect();
            if !missed.is_empty() {
                problems.push(format!("answer every open feedback item in `replies` -- these aren't: {}", missed.join(", ")));
            }
        }
        if !problems.is_empty() {
            return Err(ce_of(&problems, "feedback"));
        }
        let about: Vec<String> =
            answered.iter().flat_map(|(_, i)| [i.event_id.clone(), i.note_id.clone()]).flatten().filter(|n| !n.is_empty()).collect();
        let mut replaced: Vec<i64> = edit_rows
            .iter()
            .filter(|(_, e)| {
                e.status == "active"
                    && e.event_id.as_ref().is_some_and(|id| about.contains(id))
                    && base.is_some_and(|b| e.seq <= b.user_seq)
            })
            .map(|(row, _)| *row)
            .collect();
        replaced.sort();
        Ok((answered, replaced))
    }

    fn known_ids(&self, now: Moment) -> anyhow::Result<Vec<String>> {
        let (sheet_notes, _latest) = self.notes.read_with_latest_compacted()?;
        let times: Vec<Moment> = sheet_notes.iter().map(|n| n.note.timestamp).filter(|t| *t <= now).collect();
        let last_stamped = self.journal.last_stamped_now()?;
        let anchor = last_stamped.or(if times.is_empty() { Some(now) } else { None });
        let oldest = anchor.into_iter().chain(times.iter().copied()).min().unwrap();
        let mut ids: Vec<String> = Vec::new();
        for e in self.calendar.list_events(&(oldest - prefetch_before()), &(now + prefetch_after()))? {
            if let Some(id) = e.id.clone().filter(|i| !i.is_empty()) {
                if !e.is_cancelled() && !ids.contains(&id) {
                    ids.push(id);
                }
            }
        }
        Ok(ids)
    }

    fn proposal_context(&self) -> anyhow::Result<Option<ProposalContext>> {
        let Some(proposal) = self.journal.open_proposal()? else { return Ok(None) };
        let current = self.current(&proposal)?;
        let meta = current[0].meta().clone();
        let feedback: Vec<Feedback> = self.journal.feedback(&proposal)?.into_iter().map(|(_, f)| f).collect();
        let edits: Vec<UserEdit> = self.journal.user_edits(&proposal)?.into_iter().map(|(_, e)| e).collect();
        let claude_additions = claude_additions_of(&current);
        let (mut updates, mut creates, mut cancels) = (Vec::new(), Vec::new(), Vec::new());
        for decision in &meta.claude_decisions {
            let (kind, shape) = decision_shape(decision);
            match kind {
                "creates" => creates.push(shape),
                "cancels" => cancels.push(shape),
                _ => updates.push(shape),
            }
        }
        let list = |key: &str| -> Vec<Value> { claude_additions.get(key).and_then(Value::as_array).cloned().unwrap_or_default() };
        Ok(Some(ProposalContext {
            id: proposal.clone(),
            revision: meta.revision,
            state: state_of(&current, &feedback),
            user_seq: meta.user_seq,
            updates,
            creates,
            cancels,
            ignore_notes: claude_ignore(&current),
            annotate_notes: meta.claude_note_targets.iter().map(|(n, e)| json!({"note_id": n, "event_id": e})).collect(),
            new_actions: list("actions"),
            new_people: list("people"),
            new_locations: list("locations"),
            new_goals: list("goals"),
            settled_additions: merge(&proposal, &[], &edits, MergeOptions::default()).settled.values().cloned().collect(),
            user_edits: edits,
            feedback,
            recap: meta.claude_recap.as_ref().map(Recap::from_json_dict),
            fact_summaries: ProposedSummary::all_from(meta.claude_fact_summaries.as_ref()).iter().map(ProposedSummary::to_json).collect(),
        }))
    }

    pub fn proposal_summary(&self) -> anyhow::Result<Option<ProposalSummary>> {
        let Some(proposal) = self.journal.open_proposal()? else { return Ok(None) };
        let current = self.current(&proposal)?;
        let meta = current[0].meta();
        let feedback: Vec<Feedback> = self.journal.feedback(&proposal)?.into_iter().map(|(_, f)| f).collect();
        Ok(Some(ProposalSummary {
            id: proposal.clone(),
            revision: meta.revision,
            state: state_of(&current, &feedback),
            window_start: meta.window_start(),
            through: current.last().unwrap().now,
            open_feedback: feedback.iter().filter(|f| f.status == "open").count() as i64,
        }))
    }

    pub fn get_proposal(&self, proposal_id: Option<&str>, since_revision: Option<i64>) -> anyhow::Result<Proposal> {
        self.prefetch_tabs(true)?;
        let proposal = match proposal_id.filter(|p| !p.is_empty()) {
            Some(p) => Some(p.to_string()),
            None => self.journal.open_proposal()?,
        };
        let Some(proposal) = proposal else { return Err(ce("there's no open proposal", "unknown_proposal")) };
        let current = self.current(&proposal)?;
        let replanned = self.replanned(&current)?;
        self.view(&current, replanned, since_revision, None, None, None)
    }

    fn replanned(&self, current: &[JournalCompaction]) -> anyhow::Result<Replanned> {
        if current.iter().any(|d| [STAMPED, ABANDONED, APPLYING, APPLIED].contains(&d.status.as_str())) {
            return Ok(Replanned::Failed(error("it's been applied or abandoned", "finished")));
        }
        match self.replan(current, false) {
            Ok(walked) => Ok(Replanned::Walked(walked)),
            Err(e) => match compaction_error(&e) {
                Some(c) => Ok(Replanned::Failed(c.clone())),
                None => Err(e),
            },
        }
    }

    fn view(
        &self,
        current: &[JournalCompaction],
        walked: Replanned,
        since_revision: Option<i64>,
        replaced: Option<Vec<String>>,
        edits: Option<Vec<UserEdit>>,
        feedback: Option<Vec<Feedback>>,
    ) -> anyhow::Result<Proposal> {
        let meta = current[0].meta().clone();
        let proposal = meta.proposal.clone();
        let edits = match edits {
            Some(e) => e,
            None => self.journal.user_edits(&proposal)?.into_iter().map(|(_, e)| e).collect(),
        };
        let feedback = match feedback {
            Some(f) => f,
            None => self.journal.feedback(&proposal)?.into_iter().map(|(_, f)| f).collect(),
        };
        let state = state_of(current, &feedback);
        let mut view = Proposal {
            id: proposal.clone(),
            revision: meta.revision,
            state: state.clone(),
            window_start: meta.window_start(),
            through: current.last().unwrap().now,
            claude_through: Some(meta.claude_through.unwrap_or(current.last().unwrap().now)),
            by: meta.by.clone(),
            reason: meta.reason.clone(),
            created: meta.created(),
            events: None,
            problem: None,
            notes: Vec::new(),
            changes: Vec::new(),
            warnings: current.iter().flat_map(|d| d.warnings.clone()).collect(),
            timeline: None,
            additions: Some(current[0].additions.clone()).filter(|a| !a.is_empty()),
            settled_additions: Vec::new(),
            user_edits: edits.clone(),
            feedback,
            recap: meta.claude_recap.as_ref().map(Recap::from_json_dict),
            recap_revision: meta.recap_revision,
            changed_since: None,
            replaced,
            fact_summaries: self.proposed_summaries(&meta)?,
        };
        match walked {
            Replanned::Failed(problem) => {
                view.changes = current.iter().flat_map(|d| d.changes()).collect();
                if state == "awaiting_review" || state == "awaiting_claude" {
                    view.problem = Some(format!("it no longer plans against the calendar: {}", problem.message));
                }
            }
            Replanned::Walked(walked) => {
                let included: Vec<UserEdit> = edits.iter().filter(|e| e.seq <= meta.user_seq).cloned().collect();
                let ignore = claude_ignore(current);
                let merged = merge(
                    &proposal,
                    &meta.claude_decisions,
                    &included,
                    MergeOptions { aliases: &meta.aliases, claude_ignore: &ignore, claude_targets: &meta.claude_note_targets, ..Default::default() },
                );
                view.events = Some(proposal_events(&walked, &merged.decided_by));
                view.notes = proposal_notes(&walked, &merged);
                view.settled_additions = merged.settled.values().cloned().collect();
                view.changes = walked.iter().flat_map(|w| w.plan.changes.clone()).collect();
                view.timeline = Some(joined(&walked));
            }
        }
        if let Some(since) = since_revision {
            let mut changed: Vec<String> = Vec::new();
            for number in (since + 1)..=meta.revision {
                let later = if number == meta.revision { Some(meta.clone()) } else { self.journal.revision_meta(&proposal, number)? };
                if let Some(later) = later {
                    for name in later.changed {
                        if !changed.contains(&name) {
                            changed.push(name);
                        }
                    }
                }
            }
            changed.sort();
            view.changed_since = Some(changed);
        }
        Ok(view)
    }

    /// amend_proposal: the user's edits laid over the open proposal as a
    /// new revision.
    #[allow(clippy::too_many_arguments)]
    pub fn amend(
        &self,
        proposal_id: &str,
        revision: i64,
        decisions: Vec<EventDecision>,
        as_planned: Vec<String>,
        notes: Vec<NoteEdit>,
        additions: Vec<AdditionChoice>,
        through: Option<Moment>,
    ) -> anyhow::Result<Proposal> {
        self.prefetch_tabs(true)?;
        self.journal.garbage_collect()?;
        let current = self.open(proposal_id)?;
        let meta = current[0].meta().clone();
        if current.iter().any(|d| OPEN_STATUSES.contains(&d.status.as_str())) {
            return Err(ce(format!("proposal {proposal_id} is being applied; it can't be changed now"), "applying"));
        }
        self.base(proposal_id, &meta, Some(revision))?;
        let claude_additions = Additions::from_json_dict(Some(&Value::Object(claude_additions_of(&current))))?;
        let edit_rows = self.journal.user_edits(proposal_id)?;
        let all_edits: Vec<UserEdit> = edit_rows.iter().map(|(_, e)| e.clone()).collect();
        let known_settled: Vec<String> = merge(proposal_id, &[], &all_edits, MergeOptions::default()).settled.keys().cloned().collect();
        let decisions = self.checked_facts(decisions, &claude_additions, &known_settled)?;
        let mut seq = edit_rows.iter().map(|(_, e)| e.seq).max().unwrap_or(0);
        let now = self.clock();
        let mut new_edits: Vec<UserEdit> = Vec::new();
        let add = |new_edits: &mut Vec<UserEdit>, seq: &mut i64, event_id: Option<String>, edit: Map<String, Value>| {
            *seq += 1;
            new_edits.push(UserEdit {
                id: edit_id(proposal_id, *seq),
                seq: *seq,
                event_id,
                edit,
                status: "active".into(),
                created: now,
                base_revision: revision,
            });
        };
        for decision in &decisions {
            let edit = decision.to_json_dict().as_object().cloned().unwrap_or_default();
            add(&mut new_edits, &mut seq, if decision.action != "create" { decision.event_id.clone() } else { None }, edit);
        }
        for note in &notes {
            add(&mut new_edits, &mut seq, Some(note.note_id.clone()), note.edit());
        }
        let choices = self.addition_choices(&claude_additions, &additions)?;
        for (choice, _) in &choices {
            let mut edit = Map::new();
            edit.insert("action".into(), json!("addition"));
            edit.insert("use".into(), json!(choice.use_));
            if let Some(id) = choice.id.as_ref().filter(|i| !i.is_empty()) {
                edit.insert("id".into(), json!(id));
            }
            add(&mut new_edits, &mut seq, Some(choice.ref_.clone()), edit);
        }
        // Which of them are notes, said in the edit: a note's own id looks
        // like any other.
        let notes_named = if as_planned.is_empty() { Vec::new() } else { self.uncompacted_notes(now)? };
        for name in &as_planned {
            let mut edit = Map::new();
            edit.insert("action".into(), json!("as_planned"));
            if notes_named.contains(name) {
                edit.insert("note".into(), json!(true));
            }
            add(&mut new_edits, &mut seq, Some(name.clone()), edit);
        }
        // The revision after the one it was made from is this amend's own,
        // made when it was sent before (its answer lost): the same edits,
        // made to `revision`. It's given as it is, not made again -- a
        // proposal's revision is its version (see rows::Versioned).
        let made_from: Vec<(Option<String>, Map<String, Value>)> =
            edit_rows.iter().filter(|(_, e)| e.base_revision == revision).map(|(_, e)| (e.event_id.clone(), e.edit.clone())).collect();
        let these: Vec<(Option<String>, Map<String, Value>)> = new_edits.iter().map(|e| (e.event_id.clone(), e.edit.clone())).collect();
        if meta.revision == revision + 1 && meta.by == "user" && !these.is_empty() && made_from == these && through.is_none_or(|t| t <= current.last().unwrap().now) {
            return self.get_proposal(Some(proposal_id), None);
        }
        let before = current.last().unwrap().now;
        if let Some(through) = through {
            if through < before {
                return Err(ce(format!("proposal {proposal_id} runs to {}: it can only be extended, not cut short", iso(&before)), "not_extended"));
            }
            if through > now {
                return Err(ce(format!("proposal {proposal_id} can't be extended past now ({})", iso(&now)), "future"));
            }
        }
        let extended = through.is_some_and(|t| t > before);
        let mut taken_in: Vec<String> = Vec::new();
        if extended {
            let mut said: Vec<String> = notes.iter().map(|n| n.note_id.clone()).collect();
            said.extend(edit_rows.iter().filter(|(_, e)| e.action() == Some("note")).filter_map(|(_, e)| e.event_id.clone()));
            let (sheet_notes, _latest) = self.notes.read_with_latest_compacted()?;
            taken_in = sheet_notes.iter().filter(|n| before < n.note.timestamp && n.note.timestamp <= through.unwrap()).map(SheetNote::id).collect();
            for note_id in &taken_in {
                if said.contains(note_id) {
                    continue;
                }
                let edit = NoteEdit { note_id: note_id.clone(), use_: "annotate".into(), event_id: None }.edit();
                add(&mut new_edits, &mut seq, Some(note_id.clone()), edit);
            }
        }
        if new_edits.is_empty() && !extended {
            return Err(ce("there's nothing to amend", "empty"));
        }
        let through = if extended { through.unwrap() } else { before };
        let mut known_notes: Vec<String> = current.iter().flat_map(|d| d.note_ids.clone()).collect();
        known_notes.extend(taken_in.iter().cloned());
        let ignore = claude_ignore(&current);
        let planned = |edits: &[UserEdit]| -> anyhow::Result<(Merged, Vec<Walked>)> {
            let mut all = all_edits.clone();
            all.extend(edits.iter().cloned());
            let known_ids = self.known_ids(through)?;
            let merged = merge(
                proposal_id,
                &meta.claude_decisions,
                &all,
                MergeOptions {
                    known_ids: Some(&known_ids),
                    aliases: &meta.aliases,
                    settled: &meta.settled,
                    claude_ignore: &ignore,
                    claude_targets: &meta.claude_note_targets,
                    known_notes: Some(&known_notes),
                },
            );
            let unknown_new: Vec<&UserEdit> = merged.unknown.iter().filter(|e| edits.iter().any(|n| n == *e)).collect();
            if !unknown_new.is_empty() {
                let named: Vec<String> = unknown_new
                    .iter()
                    .map(|e| match e.event_id.clone().filter(|i| !i.is_empty()) {
                        Some(id) => py::repr(&id),
                        None => match e.edit.get("event_id") {
                            Some(v) => python_repr(v),
                            None => "None".into(),
                        },
                    })
                    .collect();
                return Err(ce(format!("these edits name events or notes that aren't in the proposal: {}", named.join(", ")), "unknown_event"));
            }
            let (walked, _remaining) = self.plan_walk(
                &settled_decisions(&merged),
                &merged.ignore_notes,
                &unsettled(&claude_additions, &merged.settled),
                through,
                &merged.note_targets.items,
            )?;
            Ok((merged, walked))
        };
        let creating: Vec<String> = new_edits
            .iter()
            .filter(|e| e.action() == Some("addition") && e.edit.get("use").and_then(Value::as_str) == Some("create"))
            .filter_map(|e| e.event_id.clone())
            .collect();
        if !creating.is_empty() {
            let without: Vec<UserEdit> = new_edits.iter().filter(|e| e.event_id.as_ref().is_none_or(|id| !creating.contains(id))).cloned().collect();
            planned(&without)?;
            // Goals last, so the people they name are made first.
            let mut ordered: Vec<&(AdditionChoice, Added)> = choices.iter().collect();
            ordered.sort_by_key(|(_, item)| matches!(item, Added::Goal(_)));
            for (choice, item) in ordered {
                if choice.use_ == "create" {
                    // Settled so far: a create not made yet isn't.
                    let made: Vec<UserEdit> = all_edits
                        .iter()
                        .chain(new_edits.iter())
                        .filter(|e| !(e.action() == Some("addition") && e.edit.get("use").and_then(Value::as_str) == Some("create") && e.edit.get("id").is_none()))
                        .cloned()
                        .collect();
                    let settled = merge(proposal_id, &[], &made, MergeOptions::default()).settled;
                    let id = self.create_addition(item, choice, &settled, &claude_additions)?;
                    // The last edit for that ref, as Python's dict keeps it.
                    if let Some(edit) = new_edits.iter_mut().rev().find(|e| {
                        e.action() == Some("addition")
                            && e.edit.get("use").and_then(Value::as_str) == Some("create")
                            && e.event_id.as_deref() == Some(choice.ref_.as_str())
                    }) {
                        edit.edit.insert("id".into(), json!(id));
                    }
                }
            }
        }
        let (merged, mut walked) = planned(&new_edits)?;
        let additions = unsettled(&claude_additions, &merged.settled);
        let mut kept_out: Vec<(String, String)> = Vec::new();
        for w in &walked {
            for (note, key) in &w.plan.kept_out {
                match kept_out.iter_mut().find(|(n, _)| n == note) {
                    Some(slot) => slot.1 = key.clone(),
                    None => kept_out.push((note.clone(), key.clone())),
                }
            }
        }
        let mut left_out: Vec<(String, Vec<String>)> = Vec::new();
        for edit in new_edits.iter_mut() {
            let action = edit.action().unwrap_or_default().to_string();
            if edit.edit.get("description").is_some_and(|d| !d.is_null()) && (action == "keep" || action == "create") {
                let described = if action == "create" { edit.id.clone() } else { edit.event_id.clone().unwrap_or_default() };
                let mut notes: Vec<String> = kept_out.iter().filter(|(_, k)| *k == described).map(|(n, _)| n.clone()).collect();
                notes.sort();
                match left_out.iter_mut().find(|(k, _)| *k == described) {
                    Some(slot) => slot.1 = notes.clone(),
                    None => left_out.push((described.clone(), notes.clone())),
                }
                edit.edit.insert("dropped_notes".into(), json!(notes));
            }
        }
        for w in walked.iter_mut() {
            for d in w.decisions.iter_mut() {
                let name = d.key.clone().or(d.event_id.clone()).unwrap_or_default();
                if d.description.is_some() && d.dropped_notes.is_none() {
                    if let Some((_, notes)) = left_out.iter().find(|(k, _)| *k == name) {
                        d.dropped_notes = Some(notes.clone());
                    }
                }
            }
        }
        let outcomes = outcomes_of(&walked);
        let mut new_meta = meta.clone();
        new_meta.revision = meta.revision + 1;
        new_meta.window_start = Some(walked[0].day.compaction_window_start);
        new_meta.created = Some(now);
        new_meta.base = Some(revision);
        new_meta.user_seq = seq;
        new_meta.by = "user".into();
        new_meta.reason = "user edit".into();
        new_meta.changed = changed(Some(&meta.outcomes), &outcomes);
        new_meta.outcomes = outcomes;
        new_meta.claude_additions = Some(claude_additions.to_json_dict());
        let replaced = self.replaced(proposal_id, &meta, revision, &new_edits)?;
        self.write_revision(&walked, &new_meta, &additions, Some(current), &new_edits)?;
        let unknown: Vec<&String> = merged.unknown.iter().map(|e| &e.id).collect();
        for (row, edit) in &edit_rows {
            if unknown.contains(&&edit.id) {
                self.journal.set_user_edit_status(*row, "inapplicable")?;
            }
        }
        let current = self.current(proposal_id)?;
        self.view(&current, Replanned::Walked(walked), None, Some(replaced), None, None)
    }

    fn addition_choices(&self, additions: &Additions, choices: &[AdditionChoice]) -> anyhow::Result<Vec<(AdditionChoice, Added)>> {
        let mut added: Vec<(String, Added)> = Vec::new();
        for item in additions.all() {
            let r = item.ref_().to_string();
            match added.iter_mut().find(|(k, _)| *k == r) {
                Some(slot) => slot.1 = item,
                None => added.push((r, item)),
            }
        }
        let there_actions = match &self.actions {
            Some(a) => a.all()?.into_iter().map(|a| a.id.unwrap_or_else(|| "None".into())).collect(),
            None => Vec::new(),
        };
        let there_people = match &self.people {
            Some(p) => p.all()?.into_iter().map(|p| p.id.unwrap_or_else(|| "None".into())).collect(),
            None => Vec::new(),
        };
        let there_locations = match &self.locations {
            Some(l) => l.all()?.into_iter().map(|l| l.id.unwrap_or_else(|| "None".into())).collect(),
            None => Vec::new(),
        };
        let there_goals = match &self.goals {
            Some(g) => g.ids()?,
            None => Vec::new(),
        };
        let mut problems: Vec<String> = Vec::new();
        let mut found = Vec::new();
        for choice in choices {
            let Some((_, item)) = added.iter().find(|(k, _)| *k == choice.ref_) else {
                problems.push(format!("additions: {} isn't something the proposal adds", py::repr(&choice.ref_)));
                continue;
            };
            let kind = item.kind();
            let there = match item {
                Added::Action(_) => &there_actions,
                Added::Person(_) => &there_people,
                Added::Location(_) => &there_locations,
                Added::Goal(_) => &there_goals,
            };
            if choice.use_ == "existing" && !choice.id.as_ref().is_some_and(|i| there.contains(i)) {
                problems.push(format!("additions: {} -- there's no {kind} {}", choice.ref_, repr_opt(choice.id.as_deref())));
                continue;
            }
            if choice.use_ != "existing" && choice.id.is_some() {
                problems.push(format!("additions: {} -- only 'existing' takes an id", choice.ref_));
                continue;
            }
            if (choice.status.is_some() || choice.deadline.is_some()) && !(choice.use_ == "create" && matches!(item, Added::Goal(_))) {
                problems.push(format!("additions: {} -- only a goal made now takes a status or deadline", choice.ref_));
                continue;
            }
            found.push((choice.clone(), item.clone()));
        }
        if !problems.is_empty() {
            return Err(ce_of(&problems, "additions"));
        }
        Ok(found)
    }

    /// Create `item` now, as `choice` corrects it. A goal can't be
    /// created while it names a person the proposal adds who isn't
    /// settled yet (one dropped is left out of it).
    fn create_addition(&self, item: &Added, choice: &AdditionChoice, settled: &Dict<AdditionSettled>, additions: &Additions) -> anyhow::Result<String> {
        let mut item = item.clone();
        match &mut item {
            Added::Action(a) => {
                if let Some(name) = &choice.name {
                    a.item.name = Some(name.clone());
                }
                a.item.status = Some(crate::stores::actions::ActionStatus::Active);
            }
            Added::Person(p) => {
                if let Some(name) = &choice.name {
                    p.item.name = Some(name.clone());
                }
                if let Some(context) = &choice.context {
                    p.item.context = Some(context.clone());
                }
            }
            Added::Location(l) => {
                if let Some(name) = &choice.name {
                    l.item.name = Some(name.clone());
                }
                if let Some(hint) = &choice.hint {
                    l.item.hint = Some(hint.clone());
                }
            }
            Added::Goal(g) => {
                if let Some(name) = &choice.name {
                    g.item.title = Some(name.clone());
                }
                if let Some(deadline) = &choice.deadline {
                    g.item.deadline = Some(deadline.clone());
                }
                if let Some(status) = choice.status {
                    g.item.status = Some(status);
                }
                let new_people: Vec<&String> = additions.people.iter().map(|p| &p.ref_).collect();
                let waiting: Vec<&String> = goals::people_of(&g.item).into_iter().filter(|p| new_people.contains(p) && settled.get(p.as_str()).is_none()).collect();
                if !waiting.is_empty() {
                    return Err(ce(format!("additions: {} names {}: settle them first", g.ref_, py::repr_list(&waiting)), "additions"));
                }
            }
        }
        let r = item.ref_().to_string();
        let added = Additions::of(item);
        let ids = create_additions(&added, self.stores(&added)?, &settled_ids(settled))?;
        Ok(lookup(&ids, &r).unwrap_or_default())
    }

    fn replaced(&self, proposal: &str, meta: &RevisionMeta, base: i64, edits: &[UserEdit]) -> anyhow::Result<Vec<String>> {
        if base == meta.revision {
            return Ok(Vec::new());
        }
        let then = match self.journal.load(&revision_id(proposal, base)) {
            Ok(then) => then,
            Err(e) if compaction_error(&e).is_some() => return Ok(Vec::new()),
            Err(e) => return Err(e),
        };
        let claude = then.meta.as_ref().map(|m| m.claude_decisions.clone()).unwrap_or_default();
        if then.decisions.is_empty() && then.steps.is_empty() && claude.is_empty() {
            return Ok(Vec::new());
        }
        let by_event = |decisions: &[EventDecision]| -> Vec<(String, Value)> {
            let mut out: Vec<(String, Value)> = Vec::new();
            for d in decisions {
                let name = d.key.clone().or(d.event_id.clone()).unwrap_or_default();
                match out.iter_mut().find(|(k, _)| *k == name) {
                    Some(slot) => slot.1 = d.to_json_dict(),
                    None => out.push((name, d.to_json_dict())),
                }
            }
            out
        };
        let before = by_event(&claude);
        let now = by_event(&meta.claude_decisions);
        let get = |map: &[(String, Value)], key: &str| map.iter().find(|(k, _)| k == key).map(|(_, v)| v.clone());
        let mut names: Vec<String> = edits
            .iter()
            .filter_map(|e| e.event_id.clone().filter(|i| !i.is_empty()))
            .filter(|id| get(&before, id) != get(&now, id))
            .collect();
        names.sort();
        names.dedup();
        Ok(names)
    }

    pub fn add_note(&self, proposal_id: &str, text: &str, event_id: Option<String>, at: Option<Moment>, note_id: Option<String>) -> anyhow::Result<Feedback> {
        self.prefetch_tabs(false)?;
        if self.open(proposal_id)?.iter().any(|d| OPEN_STATUSES.contains(&d.status.as_str())) {
            return Err(ce(format!("proposal {proposal_id} is being applied"), "applying"));
        }
        if text.trim().is_empty() {
            return Err(ce("the note is empty", "empty"));
        }
        let feedback = self.journal.feedback(proposal_id)?;
        // The same note, still open: this one sent again, its answer lost.
        if let Some((_, made)) = feedback
            .iter()
            .find(|(_, f)| f.status == "open" && f.by == "user" && f.text == text.trim() && f.event_id == event_id && f.note_id == note_id && f.at == at)
        {
            return Ok(made.clone());
        }
        let seq = feedback.iter().map(|(_, f)| f.seq).max().unwrap_or(0) + 1;
        let item = Feedback {
            id: feedback_id(proposal_id, seq),
            seq,
            text: text.trim().into(),
            event_id,
            note_id,
            at,
            by: "user".into(),
            created: self.clock(),
            status: "open".into(),
            reply: None,
            answered_in: None,
            superseded_by: None,
        };
        self.journal.add_feedback(&item, proposal_id)?;
        Ok(item)
    }

    fn uncompacted_notes(&self, now: Moment) -> anyhow::Result<Vec<String>> {
        let (sheet_notes, _latest) = self.notes.read_with_latest_compacted()?;
        Ok(sheet_notes.iter().filter(|n| n.note.timestamp <= now).map(SheetNote::id).collect())
    }

    pub fn withdraw_note(&self, feedback: &str) -> anyhow::Result<Feedback> {
        self.prefetch_tabs(false)?;
        let Some((proposal, _seq)) = split_feedback_id(feedback) else {
            return Err(ce(format!("{} isn't a feedback id", py::repr(feedback)), "unknown_feedback"));
        };
        let Some((row, item)) = self.journal.feedback(&proposal)?.into_iter().find(|(_, f)| f.id == feedback) else {
            return Err(ce(format!("there's no feedback {}", py::repr(feedback)), "unknown_feedback"));
        };
        if item.status == "withdrawn" {
            return Ok(item);
        }
        if item.status == "answered" {
            return Err(ce(
                format!(
                    "{feedback} was already answered, in revision {}: see the reply",
                    item.answered_in.map(|r| r.to_string()).unwrap_or_else(|| "None".into())
                ),
                "answered",
            ));
        }
        self.open(&proposal)?;
        let mut item = item;
        item.status = "withdrawn".into();
        self.journal.update_feedback(row, &item)?;
        Ok(item)
    }

    pub fn confirm(&self, proposal_id: &str, revision: i64) -> anyhow::Result<ProposalResult> {
        self.prefetch_tabs(true)?;
        // Confirmed already, at this revision -- this confirm sent before,
        // its answer lost: it's applied, or finished applying, not refused.
        // Or planned again from it, by that confirm: given as it is.
        if let Ok(days) = self.current(proposal_id) {
            let meta = days[0].meta();
            let applying = days.iter().any(|d| d.status == STAMPED || OPEN_STATUSES.contains(&d.status.as_str()));
            if meta.revision == revision && applying {
                return self.finish(proposal_id);
            }
            if meta.revision == revision + 1 && meta.base == Some(revision) && meta.reason == "recheck" && !applying {
                return Ok(ProposalResult {
                    status: "rechecked".into(),
                    message: format!(
                        "the notes or calendar changed since revision {revision}, so it was planned again as revision {}: review what changed, and confirm that",
                        meta.revision
                    ),
                    proposal: Some(self.get_proposal(Some(proposal_id), Some(revision))?),
                });
            }
        }
        let current = self.open(proposal_id)?;
        let meta = current[0].meta().clone();
        if current.iter().any(|d| OPEN_STATUSES.contains(&d.status.as_str())) {
            return Err(ce(format!("proposal {proposal_id} is already being applied: finish it with finish_proposal"), "applying"));
        }
        if revision != meta.revision {
            return Err(ce(
                format!("revision {revision} isn't proposal {proposal_id}'s current one ({}): review that, and confirm it", meta.revision),
                "stale_revision",
            ));
        }
        let feedback: Vec<Feedback> = self.journal.feedback(proposal_id)?.into_iter().map(|(_, f)| f).collect();
        let waiting: Vec<String> = feedback.iter().filter(|f| f.status == "open").map(|f| f.id.clone()).collect();
        if !waiting.is_empty() {
            return Err(ce(
                format!("feedback is waiting for Claude ({}): wait for the revision that answers it, or withdraw it", waiting.join(", ")),
                "feedback_open",
            ));
        }
        if current[0].status != PROPOSED {
            return Err(ce(format!("proposal {proposal_id} needs Claude first"), "needs_claude"));
        }
        self.require_no_open_compaction()?;
        self.journal.garbage_collect()?;
        let current = self.current(proposal_id)?;
        if let Err(e) = self.replan(&current, true) {
            let Some(problem) = compaction_error(&e) else { return Err(e) };
            if !problem.categories.iter().any(|c| c == "stale") {
                return self.needs_claude(proposal_id, &current, &format!("it no longer plans: {}", problem.message));
            }
            let walked = match self.replan(&current, false) {
                Ok(walked) => walked,
                Err(e) => match compaction_error(&e) {
                    Some(replanned) => return self.needs_claude(proposal_id, &current, &format!("it no longer plans: {}", replanned.message)),
                    None => return Err(e),
                },
            };
            let outcomes = outcomes_of(&walked);
            let mut new_meta = meta.clone();
            new_meta.revision = meta.revision + 1;
            new_meta.window_start = Some(walked[0].day.compaction_window_start);
            new_meta.created = Some(self.clock());
            new_meta.base = Some(meta.revision);
            new_meta.by = "server".into();
            new_meta.reason = "recheck".into();
            new_meta.changed = changed(Some(&meta.outcomes), &outcomes);
            new_meta.outcomes = outcomes;
            let additions = Additions::from_json_dict(Some(&Value::Object(current[0].additions.clone())))?;
            self.write_revision(&walked, &new_meta, &additions, Some(current), &[])?;
            let refreshed = self.current(proposal_id)?;
            return Ok(ProposalResult {
                status: "rechecked".into(),
                message: format!(
                    "the notes or calendar changed since revision {revision}, so it was planned again as revision {}: review what changed, and confirm that",
                    new_meta.revision
                ),
                proposal: Some(self.view(&refreshed, Replanned::Walked(walked), Some(revision), None, None, None)?),
            });
        }
        self.apply(proposal_id, current, Some(feedback))
    }

    pub fn finish(&self, proposal_id: &str) -> anyhow::Result<ProposalResult> {
        self.prefetch_tabs(true)?;
        let current = self.current(proposal_id)?;
        let statuses: Vec<&str> = current.iter().map(|d| d.status.as_str()).collect();
        if statuses.iter().all(|s| *s == STAMPED) {
            return Ok(ProposalResult {
                status: "applied".into(),
                message: format!("proposal {proposal_id} was already applied"),
                proposal: Some(self.view(&current, Replanned::Failed(error("applied", "finished")), None, None, None, None)?),
            });
        }
        if !statuses.iter().any(|s| OPEN_STATUSES.contains(s)) {
            return Err(ce(format!("proposal {proposal_id} hasn't been confirmed, so there's nothing to finish"), "not_confirmed"));
        }
        self.apply(proposal_id, current, None)
    }

    fn apply(&self, proposal: &str, mut current: Vec<JournalCompaction>, feedback: Option<Vec<Feedback>>) -> anyhow::Result<ProposalResult> {
        let edits: Vec<UserEdit> = self.journal.user_edits(proposal)?.into_iter().map(|(_, e)| e).collect();
        let feedback = match feedback {
            Some(f) => f,
            None => self.journal.feedback(proposal)?.into_iter().map(|(_, f)| f).collect(),
        };
        let result = match self.commit(&current[0].batch_id(), false) {
            Ok(result) => result,
            Err(e) => match google_status(&e) {
                Some(404) | Some(410) => return self.rebuild(proposal, &current[0].batch_id(), &e),
                Some(_) => {
                    return Err(ce(
                        format!("applying proposal {proposal} stopped partway ({e}); what's done stays done -- finish it with finish_proposal"),
                        "apply_stopped",
                    ))
                }
                None => return Err(e),
            },
        };
        let judging = if result.judgments.is_some() { " Claude makes its judgments next (prepare_judgments)." } else { "" };
        for day in current.iter_mut() {
            day.status = STAMPED.into();
        }
        let message = result.message.split(". The compaction isn't complete yet").next().unwrap_or_default().to_string() + "." + judging;
        Ok(ProposalResult {
            status: "applied".into(),
            message,
            proposal: Some(self.view(&current, Replanned::Failed(error("applied", "finished")), None, None, Some(edits), Some(feedback))?),
        })
    }

    fn rebuild(&self, proposal: &str, batch: &str, failure: &anyhow::Error) -> anyhow::Result<ProposalResult> {
        let mut days = self.journal.load_batch(batch)?;
        let meta = days[0].meta().clone();
        let failed_index = days.iter().position(|d| d.status != STAMPED).unwrap();
        self.record_cancellations(&days[failed_index], true)?;
        let mut aliases = meta.aliases.clone();
        let mut settled: Vec<String> = meta.settled.clone();
        let mut judge_also = meta.judge_also.clone();
        for day in &days {
            if day.status == STAMPED {
                let (ids, _) = judged_events(std::slice::from_ref(day));
                judge_also.extend(ids.into_iter().map(|i| json!([i, null, null])));
            }
        }
        let failed = days[failed_index].clone();
        for step in &failed.steps {
            if step.status != "done" {
                continue;
            }
            if step.action == "create" {
                if let Some(key) = &step.key {
                    let id = new_event_id(&failed.id, step.step);
                    match aliases.iter_mut().find(|(k, _)| k == key) {
                        Some(slot) => slot.1 = id,
                        None => aliases.push((key.clone(), id)),
                    }
                }
            } else if step.action == "cancel" {
                if let Some(id) = &step.event_id {
                    if !settled.contains(id) {
                        settled.push(id.clone());
                    }
                }
            }
            if step.action != "cancel" && step.after.as_ref().is_some_and(|a| a.facts.as_ref().is_some_and(|f| truthy(Some(f)))) {
                let id = if step.action == "update" { step.event_id.clone().unwrap_or_default() } else { new_event_id(&failed.id, step.step) };
                judge_also.push(json!([id, null, null]));
            }
        }
        for day in days.iter_mut() {
            if day.status != STAMPED {
                self.journal.set_status(day, FAILED)?;
            }
        }
        let mut history: Vec<String> = Vec::new();
        for day in &days {
            if day.status == STAMPED {
                for d in &day.decisions {
                    for name in [&d.event_id, &d.key].into_iter().flatten() {
                        if !name.is_empty() && !history.contains(name) {
                            history.push(name.clone());
                        }
                    }
                }
            }
        }
        let through = days.last().unwrap().now;
        let known = self.known_ids(through)?;
        let mut carried: Vec<EventDecision> = Vec::new();
        let mut dropped: Vec<String> = Vec::new();
        for decision in &meta.claude_decisions {
            if decision.action == "create" {
                if decision.key.as_ref().is_none_or(|k| lookup(&aliases, k).is_none()) {
                    carried.push(decision.clone());
                }
            } else if decision.event_id.as_ref().is_some_and(|e| settled.contains(e) || history.contains(e)) {
                continue;
            } else if !decision.event_id.as_ref().is_some_and(|e| known.contains(e)) {
                dropped.push(decision.event_id.clone().unwrap_or_default());
            } else {
                carried.push(decision.clone());
            }
        }
        let edit_rows = self.journal.user_edits(proposal)?;
        let all_edits: Vec<UserEdit> = edit_rows.iter().map(|(_, e)| e.clone()).collect();
        let mut settled_all = settled.clone();
        for h in &history {
            if !settled_all.contains(h) {
                settled_all.push(h.clone());
            }
        }
        let ignore = claude_ignore(&days);
        let known_notes = self.uncompacted_notes(through)?;
        let merged = merge(
            proposal,
            &carried,
            &all_edits,
            MergeOptions {
                known_ids: Some(&known),
                aliases: &aliases,
                settled: &settled_all,
                claude_ignore: &ignore,
                claude_targets: &meta.claude_note_targets,
                known_notes: Some(&known_notes),
            },
        );
        let inapplicable: Vec<&String> = merged.unknown.iter().map(|e| &e.id).collect();
        for (row, edit) in &edit_rows {
            if inapplicable.contains(&&edit.id) {
                self.journal.set_user_edit_status(*row, "inapplicable")?;
            }
        }
        let stopped = format!("applying revision {} stopped: {failure}", meta.revision);
        let additions = unsettled(&Additions::from_json_dict(Some(&Value::Object(claude_additions_of(&days))))?, &merged.settled);
        let mut walked = match self.plan_walk(&settled_decisions(&merged), &merged.ignore_notes, &additions, through, &merged.note_targets.items) {
            Ok((walked, _)) => walked,
            Err(e) => match compaction_error(&e) {
                Some(problem) => {
                    return self.needs_claude(proposal, &days, &format!("{stopped}. What's left of it couldn't be planned again: {}", problem.message))
                }
                None => return Err(e),
            },
        };
        let mut left_out: Vec<String> = dropped.clone();
        left_out.extend(merged.unknown.iter().filter_map(|e| e.event_id.clone()).filter(|e| !e.is_empty()));
        left_out.sort();
        left_out.dedup();
        walked[0].plan.warnings.insert(
            0,
            format!(
                "{stopped}. What it wrote stays; this is what's left of it{}.",
                if left_out.is_empty() { String::new() } else { format!(" -- leaving out what was about {}, which is gone", left_out.join(", ")) }
            ),
        );
        let outcomes = outcomes_of(&walked);
        let mut new_meta = meta.clone();
        new_meta.revision = meta.revision + 1;
        new_meta.window_start = Some(walked[0].day.compaction_window_start);
        new_meta.created = Some(self.clock());
        new_meta.base = Some(meta.revision);
        new_meta.by = "server".into();
        new_meta.reason = "apply failed".into();
        new_meta.changed = changed(Some(&meta.outcomes), &outcomes);
        new_meta.outcomes = outcomes;
        new_meta.claude_decisions = carried;
        new_meta.aliases = aliases;
        settled_all.sort();
        new_meta.settled = settled_all;
        new_meta.judge_also = judge_also;
        self.write_revision(&walked, &new_meta, &additions, None, &[])?;
        let refreshed = self.current(proposal)?;
        Ok(ProposalResult {
            status: "rebuilt".into(),
            message: format!("{stopped}; what's left of it is revision {}, to review and confirm", new_meta.revision),
            proposal: Some(self.view(&refreshed, Replanned::Walked(walked), None, None, None, None)?),
        })
    }

    fn needs_claude(&self, proposal: &str, current: &[JournalCompaction], why: &str) -> anyhow::Result<ProposalResult> {
        let rows = self.journal.feedback(proposal)?;
        let seq = rows.iter().map(|(_, f)| f.seq).max().unwrap_or(0) + 1;
        self.journal.add_feedback(
            &Feedback {
                id: feedback_id(proposal, seq),
                seq,
                text: format!("Revision {} {why}", current[0].meta().revision),
                event_id: None,
                note_id: None,
                at: None,
                by: "server".into(),
                created: self.clock(),
                status: "open".into(),
                reply: None,
                answered_in: None,
                superseded_by: None,
            },
            proposal,
        )?;
        let refreshed = self.current(proposal)?;
        Ok(ProposalResult {
            status: "needs_claude".into(),
            message: format!("proposal {proposal} {why}. It's waiting for Claude to revise it."),
            proposal: Some(self.view(&refreshed, Replanned::Failed(error(why, "needs_claude")), None, None, None, None)?),
        })
    }

    /// `commit`: apply the batch a day at a time -- each day's steps, then
    /// its notes stamped.
    pub fn commit(&self, compaction_id: &str, verify: bool) -> anyhow::Result<CompactionResult> {
        self.prefetch_tabs(true)?;
        let mut days = self.journal.load_batch(compaction_id)?;
        let changes: Vec<CompactionChange> = days.iter().flat_map(|d| d.changes()).collect();
        if days.iter().all(|d| d.status == STAMPED) {
            return Ok(CompactionResult {
                status: "already_compacted".into(),
                compaction_id: Some(compaction_id.into()),
                changes,
                message: format!("compaction {compaction_id} was already applied and its notes stamped"),
                ..Default::default()
            });
        }
        if days.iter().any(|d| d.status == ABANDONED) {
            return Err(ce(format!("compaction {compaction_id} was abandoned; run a new dry run"), "abandoned"));
        }
        if days.iter().any(|d| d.status == SUPERSEDED || d.status == FAILED) {
            return Err(ce(
                format!(
                    "compaction {compaction_id} was {}",
                    if days[0].status == SUPERSEDED { "superseded by a newer revision" } else { "stopped by a write that failed" }
                ),
                "superseded",
            ));
        }
        let pending: Vec<usize> = (0..days.len()).filter(|i| days[*i].status != STAMPED).collect();
        let mut timeline = None;
        if [PLANNED, PROPOSED].contains(&days[pending[0]].status.as_str()) {
            self.require_no_open_compaction()?;
            if verify {
                let unapplied: Vec<JournalCompaction> = pending.iter().map(|i| days[*i].clone()).collect();
                let walked = self.replan(&unapplied, true)?;
                timeline = Some(joined(&walked));
            }
        }
        let mut refs: Option<Vec<(String, String)>> = None;
        for &i in &pending {
            if [PLANNED, PROPOSED].contains(&days[i].status.as_str()) {
                self.journal.set_status(&mut days[i], APPLYING)?;
            }
            if refs.is_none() {
                let additions = Additions::from_json_dict(Some(&Value::Object(days[0].additions.clone())))?;
                let settled = match &days[0].meta {
                    Some(meta) => settled_ids(&self.settled_of(&meta.proposal)?),
                    None => Vec::new(),
                };
                refs = Some(create_additions(&additions, self.stores(&additions)?, &settled)?);
            }
            let id = days[i].id.clone();
            // The steps made are marked done together, once they're made
            // (or one fails): a step made but not marked, if this stops in
            // between, is made again on resuming, which changes nothing.
            let (done, failure) = self.apply_steps(&id, &days[i].steps, refs.as_ref().unwrap())?;
            let mut statuses: Vec<(i64, &str)> = Vec::new();
            for &s in &done {
                days[i].steps[s].status = "done".into();
                statuses.push((days[i].steps[s].row, "done"));
            }
            let last = Some(&i) == pending.last();
            let failure = failure
                .or_else(|| self.record_cancellations(&days[i], false).err())
                .or_else(|| self.record_fact_logs(&days[i], refs.as_ref().unwrap()).err())
                .or_else(|| if last { self.record_summaries(&days[0], refs.as_ref().unwrap()).err() } else { None });
            if let Some(failure) = failure {
                self.journal.write_statuses(&statuses)?;
                return Err(failure);
            }
            statuses.push((days[i].row, APPLIED));
            self.journal.write_statuses(&statuses)?;
            days[i].status = APPLIED.into();
            self.notes.mark_compacted(&days[i].note_ids, &id)?;
            self.journal.set_status(&mut days[i], STAMPED)?;
        }
        let steps: usize = days.iter().map(|d| d.steps.len()).sum();
        let notes: usize = days.iter().map(|d| d.note_ids.len()).sum();
        let warnings: Vec<String> = days.iter().flat_map(|d| d.warnings.clone()).collect();
        let due = if self.judging.is_some() { Some(self.due(compaction_id, &days, false)?) } else { None };
        let mut message = format!("applied {steps} change(s) and marked {notes} note(s) compacted");
        if let Some(due) = due.as_ref().filter(|d| !d.events.is_empty()) {
            message.push_str(&format!(
                ". The compaction isn't complete yet: make the {} judgment(s) in `judgments` now, yourself, of the events as `timeline` shows them, and record them with record_judgments (see `judgments.instructions`)",
                due.count()
            ));
        }
        Ok(CompactionResult {
            status: "applied".into(),
            compaction_id: Some(compaction_id.into()),
            changes,
            warnings,
            message,
            judgments: due.filter(|d| !d.events.is_empty()),
            timeline,
            ..Default::default()
        })
    }

    // -- judgments ------------------------------------------------------------

    pub fn judgments_due(&self, compaction_id: Option<&str>, redo: bool) -> anyhow::Result<Option<JudgmentsDue>> {
        if self.judging.is_none() {
            return Ok(None);
        }
        let compaction_id = match compaction_id.filter(|c| !c.is_empty()) {
            Some(c) => c.to_string(),
            None => match self.journal.last_stamped_batch()? {
                Some(c) => c,
                None => return Ok(None),
            },
        };
        let days = self.journal.load_batch(&compaction_id)?;
        Ok(Some(self.due(&compaction_id, &days, redo)?))
    }

    fn due(&self, compaction_id: &str, days: &[JournalCompaction], redo: bool) -> anyhow::Result<JudgmentsDue> {
        let requests = self.requests(compaction_id, days, redo)?;
        self.judging.as_ref().unwrap().due(compaction_id, &requests)
    }

    fn requests(&self, compaction_id: &str, days: &[JournalCompaction], redo: bool) -> anyhow::Result<Vec<JudgmentRequest>> {
        if !days.iter().all(|d| d.status == STAMPED) {
            return Err(ce(format!("compaction {compaction_id} hasn't been applied, so its events have nothing to judge"), "not_applied"));
        }
        let (ids, span) = judged_events(days);
        self.judging.as_ref().unwrap().requests(&ids, span, redo)
    }

    pub fn habit_judgments_due(&self, habit_id: &str, since: Option<Moment>, redo: bool) -> anyhow::Result<HabitJudgmentsDue> {
        let (habit, since, until) = self.backfill(habit_id, since)?;
        let judging = self.judging.as_ref().unwrap();
        let id = habit.id.clone().unwrap_or_else(|| "None".into());
        let backfill_id = format!("{}@{}", subject_id(&id), iso(&since));
        let requests = judging.habit_requests(&habit, since, until, redo)?;
        let due = judging.due(&backfill_id, &requests)?;
        Ok(HabitJudgmentsDue {
            backfill_id,
            habit_name: habit.name.clone().filter(|n| !n.is_empty()).unwrap_or_else(|| id.clone()),
            habit_id: id,
            since,
            until,
            due,
        })
    }

    fn backfill(&self, habit_id: &str, since: Option<Moment>) -> anyhow::Result<(crate::stores::habits::Habit, Moment, Moment)> {
        let Some(judging) = &self.judging else { return Err(ce("this calendar has no traits to judge", "no_traits")) };
        let habit = match judging.habit(habit_id) {
            Ok(h) => h,
            Err(e) if google_status(&e).is_some() => return Err(e),
            Err(e) => return Err(ce(e.to_string(), "unknown_habit")),
        };
        let Some(until) = self.journal.last_stamped_now()? else {
            return Err(ce("nothing's been compacted yet, so there's nothing settled to judge", "not_applied"));
        };
        let since = match since {
            Some(s) => s,
            None => until - Duration::days(judging.backfill_days(&habit)?),
        };
        Ok((habit, since, until))
    }

    fn record_backfill(&self, backfill_id: &str, judgments: &[Judgment]) -> anyhow::Result<JudgmentsResult> {
        let (habit_part, since_part) = backfill_id.split_once('@').unwrap_or((backfill_id, ""));
        let since = match crate::event::parse_iso(since_part, None) {
            Ok(s) => s,
            Err(_) => {
                return Err(ce(format!("{} isn't a backfill_id (prepare_habit_judgments gives one)", py::repr(backfill_id)), "judgment"))
            }
        };
        let (habit, since, until) = self.backfill(habit_part.strip_prefix(SUBJECT_PREFIX).unwrap_or(habit_part), Some(since))?;
        let judging = self.judging.as_ref().unwrap();
        let recorded = match judging.habit_requests(&habit, since, until, true).and_then(|r| judging.record(&r, judgments)) {
            Ok(n) => n,
            Err(e) if google_status(&e).is_some() => return Err(e),
            Err(e) => return Err(ce(e.to_string(), "judgment")),
        };
        let remaining: Vec<String> = judging.habit_requests(&habit, since, until, false)?.into_iter().map(|r| r.id).collect();
        let name = crate::stores::groups::repr_or_none(habit.name.as_deref());
        Ok(JudgmentsResult {
            compaction_id: backfill_id.into(),
            recorded,
            complete: remaining.is_empty(),
            message: if remaining.is_empty() {
                format!("recorded {recorded} judgment(s); the backfill of {name} is complete")
            } else {
                format!("recorded {recorded} judgment(s); {} still to make for {name} (see `remaining`)", remaining.len())
            },
            remaining,
        })
    }

    pub fn record_judgments(&self, compaction_id: &str, judgments: &[Judgment]) -> anyhow::Result<JudgmentsResult> {
        if compaction_id.starts_with(SUBJECT_PREFIX) {
            return self.record_backfill(compaction_id, judgments);
        }
        let Some(judging) = &self.judging else { return Err(ce("this calendar has no traits to judge", "no_traits")) };
        let days = self.journal.load_batch(compaction_id)?;
        let recorded = match self.requests(compaction_id, &days, true).and_then(|r| judging.record(&r, judgments)) {
            Ok(n) => n,
            Err(e) if google_status(&e).is_some() => return Err(e),
            Err(e) => return Err(ce(e.to_string(), "judgment")),
        };
        let remaining: Vec<String> = self.requests(compaction_id, &days, false)?.into_iter().map(|r| r.id).collect();
        Ok(JudgmentsResult {
            compaction_id: compaction_id.into(),
            recorded,
            complete: remaining.is_empty(),
            message: if remaining.is_empty() {
                format!("recorded {recorded} judgment(s); compaction {compaction_id} is complete")
            } else {
                format!(
                    "recorded {recorded} judgment(s); {} still to make before compaction {compaction_id} is complete (see `remaining`)",
                    remaining.len()
                )
            },
            remaining,
        })
    }

    fn judgments_pending(&self) -> anyhow::Result<Option<String>> {
        if self.judging.is_none() {
            return Ok(None);
        }
        let Some(compaction_id) = self.journal.last_stamped_batch()? else { return Ok(None) };
        let due = match self.journal.load_batch(&compaction_id).and_then(|days| self.requests(&compaction_id, &days, false)) {
            Ok(due) => due,
            Err(e) if compaction_error(&e).is_some() => return Ok(None),
            Err(e) => return Err(e),
        };
        Ok(if due.is_empty() { None } else { Some(compaction_id) })
    }

    /// Claude's logs and summaries of people's trait facts checked against
    /// each other (see fact_changes::problems): a CompactionError ("facts")
    /// listing everything wrong.
    fn check_fact_changes(&self, decisions: &[EventDecision], summaries: &[ProposedSummary], additions: &Additions, settled: &[String]) -> anyhow::Result<()> {
        let logged = decisions.iter().any(|d| d.facts.as_ref().is_some_and(|f| !f.log_entries().is_empty()));
        if !logged && summaries.is_empty() {
            return Ok(());
        }
        let (Some(keeping), Some(people)) = (&self.facts, &self.people) else {
            return Err(ce("this server keeps no trait facts: leave out fact_summaries and logs", "facts"));
        };
        let mut refs: Vec<String> = additions.people.iter().map(|p| p.ref_.clone()).collect();
        refs.extend(settled.iter().cloned());
        let problems = fact_changes::problems(decisions, summaries, &people.all()?, &refs, &keeping.traits.all()?);
        if !problems.is_empty() {
            return Err(ce_of(&problems, "facts"));
        }
        Ok(())
    }

    /// The summaries a revision proposes, each with the person's summary
    /// as it is now (`previous`), for the user to compare.
    fn proposed_summaries(&self, meta: &RevisionMeta) -> anyhow::Result<Vec<Value>> {
        let proposed = ProposedSummary::all_from(meta.claude_fact_summaries.as_ref());
        if proposed.is_empty() {
            return Ok(Vec::new());
        }
        let kept = match &self.facts {
            Some(f) => f.person_facts.by_person()?,
            None => Vec::new(),
        };
        Ok(proposed
            .iter()
            .map(|s| {
                let previous = kept
                    .iter()
                    .find(|(p, _)| *p == s.person_id)
                    .and_then(|(_, facts)| facts.iter().find(|(t, f, _)| format!("{t}/{f}") == s.fact))
                    .map(|(_, _, summary)| summary.clone());
                let mut shown = s.to_json();
                shown["previous"] = json!(previous);
                shown
            })
            .collect())
    }

    /// The settled additions of `proposal`'s user edits, by ref.
    fn settled_of(&self, proposal: &str) -> anyhow::Result<Dict<AdditionSettled>> {
        let edits: Vec<UserEdit> = self.journal.user_edits(proposal)?.into_iter().map(|(_, e)| e).collect();
        Ok(merge(proposal, &[], &edits, MergeOptions::default()).settled)
    }

    /// Index the logs of `day`'s events, as applied (refs made into ids).
    fn record_fact_logs(&self, day: &JournalCompaction, refs: &[(String, String)]) -> anyhow::Result<()> {
        let Some(keeping) = &self.facts else { return Ok(()) };
        let rename = |id: &str| Some(refs.iter().rev().find(|(k, _)| k == id).map(|(_, v)| v.clone()).unwrap_or_else(|| id.to_string()));
        for step in &day.steps {
            if step.action == "cancel" {
                continue;
            }
            let Some(after) = &step.after else { continue };
            let facts = facts_from_dict(after.facts.as_ref());
            let had = step.before.as_ref().map(|b| facts_from_dict(b.facts.as_ref())).unwrap_or_default();
            if facts.log_entries().is_empty() && had.log_entries().is_empty() {
                continue;
            }
            let event_id = match step.action.as_str() {
                "create" => new_event_id(&day.id, step.step),
                _ => step.event_id.clone().unwrap_or_default(),
            };
            let logs = fact_changes::renamed_logs(&facts, &rename);
            keeping.person_facts.record_logs(&event_id, after.start, after.summary.as_deref(), &logs, &format!("compaction {}", day.batch_id()), self.clock())?;
        }
        Ok(())
    }

    /// Write the summaries `day`'s revision proposes (the first day's meta
    /// holds them), people added by ref by their ids.
    fn record_summaries(&self, day: &JournalCompaction, refs: &[(String, String)]) -> anyhow::Result<()> {
        let Some(keeping) = &self.facts else { return Ok(()) };
        let Some(meta) = &day.meta else { return Ok(()) };
        let proposed = ProposedSummary::all_from(meta.claude_fact_summaries.as_ref());
        if proposed.is_empty() {
            return Ok(());
        }
        let settled = self.settled_of(&meta.proposal)?;
        let rename = fact_changes::renamer(&settled, refs);
        let traits = keeping.traits.all()?;
        // A fact let go of since it was proposed is left out.
        let changes: Vec<_> = proposed
            .iter()
            .filter_map(|s| s.change(&rename))
            .filter(|c| traits.iter().any(|t| t.id.as_deref() == Some(c.trait_id.as_str()) && t.fact(&c.fact_id).is_some()))
            .collect();
        keeping.person_facts.set_summaries(&changes, &traits, &format!("compaction {}", day.batch_id()), self.clock())?;
        Ok(())
    }

    fn checked_facts(&self, decisions: Vec<EventDecision>, additions: &Additions, settled: &[String]) -> anyhow::Result<Vec<EventDecision>> {
        let people: Option<Vec<String>> = match &self.people {
            Some(p) => Some(p.all()?.into_iter().map(|p| p.id.unwrap_or_else(|| "None".into())).collect()),
            None => None,
        };
        let locations: Option<Vec<String>> = match &self.locations {
            Some(l) => Some(l.all()?.into_iter().map(|l| l.id.unwrap_or_else(|| "None".into())).collect()),
            None => None,
        };
        let mut new_people: Vec<String> = additions.people.iter().map(|p| p.ref_.clone()).collect();
        new_people.extend(settled.iter().cloned());
        let mut new_locations: Vec<String> = additions.locations.iter().map(|l| l.ref_.clone()).collect();
        new_locations.extend(settled.iter().cloned());
        let mut checked = Vec::new();
        let mut problems: Vec<String> = Vec::new();
        for (number, mut decision) in decisions.into_iter().enumerate() {
            if let Some(facts) = &decision.facts {
                let facts = facts.normalized();
                let label = format!(
                    "decision {} ({}{})",
                    number + 1,
                    decision.action,
                    decision.event_id.as_ref().filter(|e| !e.is_empty()).map(|e| format!(" {e}")).unwrap_or_default()
                );
                problems.extend(fact_problems(&facts).into_iter().map(|p| format!("{label}: its facts {p}")));
                if let Some(people) = &people {
                    let unknown: Vec<String> = facts
                        .people()
                        .into_iter()
                        .filter(|i| !people.contains(i) && !new_people.contains(i) && i != SELF_ID)
                        .collect();
                    if !unknown.is_empty() {
                        problems.push(format!(
                            "{label}: its facts name {}, who aren't people (get_people lists them; add someone new with new_people)",
                            py::repr_list(&unknown)
                        ));
                    }
                }
                if let Some(locations) = &locations {
                    for location in facts.locations() {
                        if !locations.contains(&location) && !new_locations.contains(&location) {
                            problems.push(format!(
                                "{label}: its facts' location {} isn't a location (add a new one with new_locations)",
                                py::repr(&location)
                            ));
                        }
                    }
                }
                decision.facts = Some(facts);
            }
            checked.push(decision);
        }
        if !problems.is_empty() {
            return Err(ce_of(&problems, "facts"));
        }
        Ok(checked)
    }

    pub fn edit_note(&self, note_id: &str, timestamp: Option<Moment>, description: Option<&str>, if_version: Option<i64>) -> anyhow::Result<SheetNote> {
        self.prefetch_tabs(false)?;
        edit_note(&self.notes, &self.journal, note_id, timestamp, description, if_version)
    }

    pub fn delete_note(&self, note_id: &str, if_version: Option<i64>) -> anyhow::Result<Option<SheetNote>> {
        self.prefetch_tabs(false)?;
        delete_note(&self.notes, &self.journal, note_id, if_version)
    }

    pub fn abandon(&self, compaction_id: &str) -> anyhow::Result<CompactionResult> {
        let mut days =
            if !self.journal.revisions(compaction_id)?.is_empty() { self.current(compaction_id)? } else { self.journal.load_batch(compaction_id)? };
        if days.iter().all(|d| d.status == STAMPED) {
            return Err(ce(format!("compaction {compaction_id} is already complete; there's nothing to abandon"), "already_complete"));
        }
        let finished = days.iter().filter(|d| d.status == STAMPED).count();
        let total = days.len();
        for day in days.iter_mut() {
            if day.status != STAMPED && day.status != ABANDONED {
                self.journal.set_status(day, ABANDONED)?;
            }
        }
        let mut message = format!(
            "compaction {compaction_id} abandoned. Any steps it had already applied stay applied (the journal has each one's before-state); its notes are still uncompacted."
        );
        if finished > 0 {
            message.push_str(&format!(
                " ({finished} of its {total} days had already been applied in full, so those days' notes stay compacted.)"
            ));
        }
        Ok(CompactionResult { status: "abandoned".into(), compaction_id: Some(compaction_id.into()), message, ..Default::default() })
    }

    /// `_walk`: the span from the last compaction (or the oldest note's
    /// day) up to `now`, and how many uncompacted notes are left after it.
    fn walk(&self, now: Moment, night: Option<&dyn Fn(&str) -> Option<EventDecision>>, max_days: i64) -> anyhow::Result<(Option<Day>, usize)> {
        let (sheet_notes, latest) = self.notes.read_with_latest_compacted()?;
        let notes: Vec<SheetNote> = sheet_notes.iter().filter(|n| n.note.timestamp <= now).cloned().collect();
        let times: Vec<(String, Moment)> = notes.iter().map(|n| (n.id(), n.note.timestamp)).collect();
        let last_stamped = self.journal.last_stamped_now()?;
        let anchor = last_stamped.or(if notes.is_empty() { Some(now) } else { None });
        if anchor.is_some_and(|a| a >= now) {
            return Ok((None, sheet_notes.len()));
        }
        let oldest = anchor.into_iter().chain(times.iter().map(|(_, t)| *t)).min().unwrap();
        self.calendar.list_events(&(oldest - prefetch_before()), &(now + prefetch_after()))?;
        let day_for = |sleepless: &[String], overrun: Option<Moment>| {
            self.day(now, &notes, last_stamped, latest.clone(), sleepless, overrun, anchor, max_days)
        };
        let mut day = cut(&day_for, night, &times)?;
        day.history_until = last_stamped;
        let remaining = sheet_notes.len() - day.notes.len();
        Ok((Some(day), remaining))
    }

    /// The span from the day the oldest of `notes` falls in -- or, with
    /// none, the one `window_start` does, or with an `anchor` (see
    /// `walk`), the one that does -- up to `now`, with its notes. It
    /// starts when the night before that ends (or at the note, if it was
    /// written before the wake-up time) and runs through its last night:
    /// the end-of-day sleep that ends after `now` -- or, with more than
    /// `max_days` days to `now`, the last that starts within them, its
    /// end the span's `now`. Without one, it runs 24 hours past its
    /// start, or to `now`. Its compaction window starts at
    /// `window_start`, if given, or else no earlier than `last_stamped`.
    /// `first`: whether it's shown the latest compacted note and the last
    /// compaction, as context. `sleepless`: nights that don't end it, and
    /// `overrun`: where its last night now ends, if that's later -- the
    /// events up to then are its too (see `cut`).
    #[allow(clippy::too_many_arguments)]
    fn day(
        &self,
        now: Moment,
        notes: &[SheetNote],
        last_stamped: Option<Moment>,
        latest_compacted: Option<NotedTime>,
        sleepless: &[String],
        overrun: Option<Moment>,
        anchor: Option<Moment>,
        max_days: i64,
    ) -> anyhow::Result<Day> {
        let oldest = anchor.unwrap_or_else(|| notes.iter().map(|n| n.note.timestamp).min().unwrap());
        let recent = self.span_events(oldest - Duration::hours(24), oldest + Duration::seconds(1))?;
        let mut opening: Option<&Event> = None;
        for e in recent.iter().filter(|e| e.is_end_of_day_sleep == Some(true) && !e.is_cancelled() && e.start() <= oldest) {
            if opening.is_none_or(|o| e.start() > o.start()) {
                opening = Some(e);
            }
        }
        let day_start = match opening {
            Some(o) => o.end().min(oldest),
            None => oldest,
        };
        let compaction_window_start = match last_stamped {
            Some(l) => pmax(day_start, l),
            None => day_start,
        };
        let limit = day_start + Duration::hours(24) * max_days as i32;
        let capped = now > limit;
        let until = if capped { limit } else { now };
        let reach = Duration::hours(24) * (1 + sleepless.len() as i32);
        let reach_end = pmax(pmax(day_start, until), overrun.unwrap_or(until)) + reach;
        let mut fetched: Vec<Event> = self
            .span_events(compaction_window_start - Duration::minutes(LOOKBACK_MINUTES), reach_end)?
            .into_iter()
            .filter(|e| !e.is_cancelled())
            .collect();
        fetched.sort_by_key(|e| e.start());
        let earlier: Vec<Event> = fetched.iter().filter(|e| e.end() <= compaction_window_start).cloned().collect();
        let mut events: Vec<Event> = fetched.iter().filter(|e| e.end() > compaction_window_start).cloned().collect();
        let is_night = |e: &Event| e.is_end_of_day_sleep == Some(true) && e.start() > day_start;
        let nights: Vec<usize> = (0..events.len())
            .filter(|i| is_night(&events[*i]) && !events[*i].id.as_ref().is_some_and(|id| sleepless.contains(id)))
            .collect();
        let closing = if capped {
            nights.iter().rev().copied().find(|i| events[*i].start() < limit)
        } else {
            nights.iter().copied().find(|i| events[*i].end() > now)
        };
        if let Some(closing) = closing {
            let runs_into: Vec<Event> = events[closing + 1..].iter().filter(|e| overrun.is_some_and(|o| e.start() < o)).cloned().collect();
            events.truncate(closing + 1);
            events.extend(runs_into);
        }
        let mut previous: Option<Event> = None;
        for e in &earlier {
            if previous.as_ref().is_none_or(|p| e.end() > p.end()) {
                previous = Some(e.clone());
            }
        }
        if let Some(p) = &previous {
            events.insert(0, p.clone());
        }
        let shift = if previous.is_some() { 1 } else { 0 };
        let day_end = match closing {
            Some(closing) => events[closing + shift].end(),
            None => {
                // The night indices from before `previous` went in front,
                // as Python reads them.
                let mut last_start = day_start;
                for i in &nights {
                    let end = events[*i].end();
                    if end <= until && end > last_start {
                        last_start = end;
                    }
                }
                pmax(last_start + reach, until)
            }
        };
        let span_now = now.min(day_end);
        let mut night_ids: Vec<String> = Vec::new();
        for e in &events {
            if is_night(e) {
                if let Some(id) = e.id.clone().filter(|i| !i.is_empty()) {
                    if !night_ids.contains(&id) {
                        night_ids.push(id);
                    }
                }
            }
        }
        Ok(Day {
            notes: notes.iter().filter(|n| n.note.timestamp <= span_now).cloned().collect(),
            closing: closing.map(|c| events[c + shift].clone()),
            events,
            day_start,
            compaction_window_start,
            day_end,
            now: span_now,
            previous,
            latest_compacted,
            last_compaction: last_stamped,
            history_until: None,
            nights: night_ids,
        })
    }

    fn supersede_planned(&self) -> anyhow::Result<usize> {
        let mut replaced: Vec<JournalCompaction> = Vec::new();
        for (id, _status) in self.journal.compactions_with_status(&[PLANNED])? {
            replaced.push(self.journal.load(&id)?);
        }
        let mut batches: Vec<String> = Vec::new();
        for compaction in replaced.iter_mut() {
            self.journal.set_status(compaction, ABANDONED)?;
            if !batches.contains(&compaction.batch_id()) {
                batches.push(compaction.batch_id());
            }
        }
        Ok(batches.len())
    }

    fn require_no_open_compaction(&self) -> anyhow::Result<()> {
        if let Some((compaction_id, status)) = self.journal.open_compactions()?.into_iter().next() {
            let batch_id = self.journal.load(&compaction_id)?.batch_id();
            return Err(ce(
                format!(
                    "compaction {batch_id} is {status} -- finish it with finish_proposal, or abandon it with abandon_compaction, before starting another"
                ),
                "open_compaction",
            ));
        }
        Ok(())
    }

    /// `_replan`: plan a batch's unapplied days again, as one span.
    /// `strict`: check it comes out as it did ("stale" if not).
    fn replan(&self, days: &[JournalCompaction], strict: bool) -> anyhow::Result<Vec<Walked>> {
        let stale = || ce(format!("the notes or calendar changed since compaction {} was previewed; run a new dry run", days[0].batch_id()), "stale");
        let decisions: Vec<EventDecision> = days.iter().flat_map(|d| d.decisions.clone()).collect();
        let ignore_notes: Vec<String> = days.iter().flat_map(|d| d.ignore_notes.clone()).collect();
        let mut note_targets: Vec<(String, Option<String>)> = Vec::new();
        for day in days {
            for (n, e) in &day.note_targets {
                match note_targets.iter_mut().find(|(k, _)| k == n) {
                    Some(slot) => slot.1 = e.clone(),
                    None => note_targets.push((n.clone(), e.clone())),
                }
            }
        }
        let mut note_ids: Vec<String> = days.iter().flat_map(|d| d.note_ids.clone()).collect();
        note_ids.sort();
        note_ids.dedup();
        let changes: Vec<CompactionChange> = days.iter().flat_map(|d| d.changes()).collect();
        let night = |event_id: &str| decisions.iter().find(|d| d.event_id.as_deref() == Some(event_id)).cloned();
        let (day, _remaining) = self.walk(days.last().unwrap().now, Some(&night), MAX_DAYS)?;
        let Some(day) = day else { return Err(anyhow::anyhow!("there's nothing to plan up to {}", iso(&days.last().unwrap().now))) };
        if strict {
            let mut ids: Vec<String> = day.notes.iter().map(SheetNote::id).collect();
            ids.sort();
            ids.dedup();
            if ids != note_ids {
                return Err(stale());
            }
        }
        let additions = Additions::from_json_dict(Some(&Value::Object(days[0].additions.clone())))?;
        let plan = self.plan(&day, &decisions, &ignore_notes, &additions, &note_targets)?;
        if strict && comparable(&plan.changes) != comparable(&changes) {
            return Err(stale());
        }
        Ok(vec![Walked { day, plan, decisions, ignore_notes, note_targets }])
    }

    /// `_plan`: `plan_compaction` for `day`, with the decisions' actions
    /// checked and created events' labels checked against the calendar's.
    fn plan(
        &self,
        day: &Day,
        decisions: &[EventDecision],
        ignore_notes: &[String],
        additions: &Additions,
        note_targets: &[(String, Option<String>)],
    ) -> anyhow::Result<CompactionPlan> {
        let tree = self.tree()?;
        if let Some(tree) = &tree {
            let current = |id: Option<&str>| -> Vec<String> {
                day.events
                    .iter()
                    .rev()
                    .find(|e| e.id.as_deref().is_some_and(|i| !i.is_empty()) && e.id.as_deref() == id)
                    .map(|e| share_ids(e.action_ids.as_deref().unwrap_or_default()))
                    .unwrap_or_default()
            };
            let new: Vec<String> = additions.actions.iter().map(|a| a.ref_.clone()).collect();
            let mut problems: Vec<String> = Vec::new();
            for d in decisions {
                let ids = share_ids(d.action_ids.as_deref().unwrap_or_default());
                let named: Vec<String> = ids.iter().filter(|a| !new.contains(a)).cloned().collect();
                let who = repr_opt(d.event_id.as_deref().filter(|e| !e.is_empty()).or(d.summary.as_deref()));
                if !ids.is_empty() && ids.iter().any(|a| a.starts_with(REF_PREFIX) && !new.contains(a)) {
                    problems.push(format!("{} {who}: a ref that isn't in new_actions", d.action));
                    continue;
                }
                if !named.is_empty() {
                    if let Err(e) = tree.check_action_ids(&named, &current(d.event_id.as_deref())) {
                        problems.push(format!("{} {who}: {e}", d.action));
                    }
                }
            }
            if !problems.is_empty() {
                return Err(ce_of(&problems, "actions"));
            }
        }
        let names = self.names(Some(additions))?;
        let notes = plan_notes(day);
        let previous = latest_compacted(day);
        let closing_id = day.closing.as_ref().and_then(|c| c.id.clone());
        let mut plan = plan_compaction(
            &notes,
            decisions,
            &day.events,
            day.now,
            PlanOptions {
                ignore_notes: Some(ignore_notes),
                day_start: Some(day.day_start),
                names: Some(&names),
                previous_note: previous.as_ref(),
                last_compaction: day.last_compaction,
                next_day_follows: false,
                history_until: day.history_until,
                note_targets: Some(note_targets),
                closing_id: closing_id.as_deref(),
                nights: Some(&day.nights),
            },
        )
        .map_err(anyhow::Error::from)?;
        self.check_goals(&plan)?;
        check_shares(&plan)?;
        self.check_before_window(day, &plan)?;
        self.show_follow_through(day, decisions, &mut plan)?;
        if let Some(tree) = &tree {
            keep_priorities(&mut plan, tree, day);
        }
        let labelled: Vec<usize> = (0..plan.changes.len())
            .filter(|i| plan.changes[*i].action == "create" && plan.changes[*i].after.as_ref().is_some_and(|a| a.event_label_id.is_some()))
            .collect();
        if labelled.is_empty() {
            return Ok(plan);
        }
        let (labels, _etag) = self.client.list_event_labels()?;
        let label_ids: Vec<String> = labels.iter().filter_map(|l| l.id.clone()).collect();
        for i in labelled {
            let after = plan.changes[i].after.as_mut().unwrap();
            if !after.event_label_id.as_ref().is_some_and(|l| label_ids.contains(l)) {
                after.event_label_id = None;
            }
        }
        Ok(plan)
    }

    /// The goal shares `plan` gives events must name goals there are
    /// (read only if it gives any), and fit in their events.
    fn check_goals(&self, plan: &CompactionPlan) -> anyhow::Result<()> {
        let tagged: Vec<&CompactionChange> = plan
            .changes
            .iter()
            .filter(|c| {
                let goals = c.after.as_ref().and_then(|a| a.goals.as_ref()).filter(|g| !g.is_empty());
                goals.is_some() && goals != c.before.as_ref().and_then(|b| b.goals.as_ref())
            })
            .collect();
        if tagged.is_empty() {
            return Ok(());
        }
        let known = match &self.goals {
            Some(g) => Some(g.ids()?),
            None => None,
        };
        let mut problems: Vec<String> = Vec::new();
        for change in tagged {
            let after = change.after.as_ref().unwrap();
            let shares = after.goals.as_deref().unwrap_or_default();
            let who = format!("{} {}", change.action, repr_opt(change.event_id.as_deref().or(after.summary.as_deref())));
            let minutes = match (after.start, after.end) {
                (Some(start), Some(end)) => (end - start).num_minutes(),
                _ => i64::MAX,
            };
            problems.extend(share_problems(shares, minutes).into_iter().map(|p| format!("{who}: {p}")));
            if let Some(known) = &known {
                let unknown: Vec<String> =
                    shares.iter().map(|s| s.goal_id.trim().to_string()).filter(|id| !id.is_empty() && !known.contains(id)).collect();
                if !unknown.is_empty() {
                    problems.push(format!("{who}: its goals name {}, which aren't goals (`goals` lists those to tag events with)", py::repr_list(&unknown)));
                }
            }
        }
        if !problems.is_empty() {
            return Err(ce_of(&problems, "goals"));
        }
        Ok(())
    }

    fn show_follow_through(&self, day: &Day, decisions: &[EventDecision], plan: &mut CompactionPlan) -> anyhow::Result<()> {
        let (Some(cancellations), Some(timeline)) = (&self.cancellations, plan.timeline.as_mut()) else { return Ok(()) };
        let cancelled: Vec<String> = decisions
            .iter()
            .filter(|d| d.action == "cancel" && d.counts_against_follow_through != Some(false))
            .filter_map(|d| d.event_id.clone())
            .collect();
        let mut marked = false;
        for shown in timeline.events.iter_mut() {
            let Some(event_id) = &shown.event_id else { continue };
            if !cancelled.contains(event_id) {
                continue;
            }
            let Some(event) = day.events.iter().rev().find(|e| e.id.as_ref() == Some(event_id)) else { continue };
            shown.follow_through =
                cancellations.matches(event)?.iter().map(|m| format!("{} ({})", m.person_name, m.trait_names.join(", "))).collect();
            marked = marked || !shown.follow_through.is_empty();
        }
        if marked {
            timeline.text = render(timeline, true);
        }
        Ok(())
    }

    fn record_cancellations(&self, journal: &JournalCompaction, only_done: bool) -> anyhow::Result<()> {
        let Some(cancellations) = &self.cancellations else { return Ok(()) };
        let cancelled: Vec<String> = journal
            .decisions
            .iter()
            .filter(|d| d.action == "cancel" && d.counts_against_follow_through != Some(false))
            .filter_map(|d| d.event_id.clone())
            .collect();
        for step in &journal.steps {
            if only_done && step.status != "done" {
                continue;
            }
            if step.action == "cancel" && step.event_id.as_ref().is_some_and(|e| cancelled.contains(e)) {
                if let Some(before) = &step.before {
                    cancellations.record(&before.to_event(step.event_id.as_deref()), &format!("compaction {}", journal.batch_id()), Some(self.clock()))?;
                }
            }
        }
        Ok(())
    }

    fn check_before_window(&self, day: &Day, plan: &CompactionPlan) -> anyhow::Result<()> {
        let placed: Vec<&CompactionChange> = plan.changes.iter().filter(|c| c.action != "cancel" && c.after.is_some()).collect();
        let Some(reach) = placed.iter().map(|c| c.after.as_ref().unwrap().start()).min() else { return Ok(()) };
        if reach >= day.compaction_window_start {
            return Ok(());
        }
        let offered: Vec<Option<&String>> = day.events.iter().map(|e| e.id.as_ref()).collect();
        let before: Vec<Event> = self
            .span_events(reach, day.compaction_window_start)?
            .into_iter()
            .filter(|e| !e.is_cancelled() && !offered.contains(&e.id.as_ref()))
            .collect();
        let mut problems: Vec<Issue> = Vec::new();
        for c in &placed {
            let after = c.after.as_ref().unwrap();
            for e in &before {
                if e.id != c.event_id && e.start() < after.end.unwrap() && e.end() > after.start() {
                    problems.push(Issue::tagged(
                        "overlap",
                        format!(
                            "{} ({} to {}) runs back over {} ({} to {}), from before this round's events begin ({}) -- an earlier compaction settled it. Start it no earlier than {}, or change {} with update_event first.",
                            repr_opt(after.summary.as_deref()),
                            iso(&after.start()),
                            iso(&after.end.unwrap()),
                            repr_opt(e.summary.as_deref()),
                            iso(&e.start()),
                            iso(&e.end()),
                            iso(&day.compaction_window_start),
                            iso(&e.end()),
                            repr_opt(e.summary.as_deref()),
                        ),
                    ));
                }
            }
        }
        if !problems.is_empty() {
            return Err(error_of(&problems, "overlap").into());
        }
        Ok(())
    }

    /// Make the changes of `steps` not done yet. Their requests are made
    /// first -- the events they update fetched beforehand in a batch,
    /// unless a listing already has them -- and then sent in batches. Calendar makes a batch's
    /// changes in any order, so steps to the same event (or to a series
    /// and its instances) go in batches of their own, in step order.
    /// Returns the steps made, by index, and the first failure, after which
    /// no later batch is sent. A create Calendar already has (a 409) was
    /// made.
    fn apply_steps(&self, compaction_id: &str, steps: &[JournalStep], refs: &[(String, String)]) -> anyhow::Result<(Vec<usize>, Option<anyhow::Error>)> {
        let target = |step: &JournalStep| match step.action.as_str() {
            "create" => new_event_id(compaction_id, step.step),
            _ => step.event_id.clone().unwrap_or_default(),
        };
        let pending: Vec<usize> = (0..steps.len()).filter(|s| steps[*s].status != "done").collect();
        let updated: Vec<String> = pending.iter().filter(|s| steps[**s].action != "create").map(|s| target(&steps[*s])).collect();
        self.client.client().prefetch_events(&updated)?;
        let related = |a: &str, b: &str| a == b || a.starts_with(&format!("{b}_")) || b.starts_with(&format!("{a}_"));
        let changed_since = |step: &JournalStep, event_id: &str| {
            ce(
                format!(
                    "event {event_id} was changed in the calendar since this compaction was planned, so its change wasn't made, and wasn't overwritten (the changes before it were made): abandon this compaction (abandon_compaction), which keeps what was made, and propose again on the calendar as it is now{}",
                    step.before.as_ref().and_then(|b| b.summary.as_deref()).map(|s| format!(" ({})", py::repr(s))).unwrap_or_default()
                ),
                "stale",
            )
        };
        let mut batches: Vec<Vec<(usize, BatchRequest)>> = Vec::new();
        // A step changed since stops the steps from it on; those before it
        // are still made.
        let mut blocked: Option<anyhow::Error> = None;
        for s in pending {
            let step = &steps[s];
            let request = if step.action == "create" {
                self.client.create_event_request(&resolve(&step.after.as_ref().unwrap().to_event(Some(&target(step))), refs))?
            } else {
                // Onto the version the plan was checked against (just
                // replanned on what's kept): one changed since -- what's
                // kept lagging Calendar's feed -- is refused, not overwritten.
                // A field it changes that's now neither as planned from nor
                // as planned is a change made since, which it mustn't undo.
                let mut patch = resolve(&patch_for(step), refs);
                if let Some(id) = patch.id.clone() {
                    let now = self.client.get_event(&id)?;
                    if step.action != "cancel" && !as_planned(step, &EventState::from_event(&now)) {
                        blocked = Some(changed_since(step, &id));
                        break;
                    }
                    patch.etag = now.etag;
                }
                self.client.update_event_request(&patch)?
            };
            match batches.last_mut() {
                Some(batch) if batch.len() < BATCH_SIZE && !batch.iter().any(|(o, _)| related(&target(&steps[*o]), &target(step))) => batch.push((s, request)),
                _ => batches.push(vec![(s, request)]),
            }
        }
        let mut done = Vec::new();
        for batch in batches {
            let requests: Vec<BatchRequest> = batch.iter().map(|(_, r)| r.clone()).collect();
            let mut answers = match self.client.client().send_writes(&requests) {
                Ok(answers) => answers,
                Err(e) => return Ok((done, Some(e))),
            };
            // A priority label refused may have been removed since the
            // labels were made sure of: if it was, it's back, so those are
            // sent again.
            let label = |i: usize| requests[i].body.as_ref().and_then(|b| b["eventLabelId"].as_str());
            let refused: Vec<usize> = (0..answers.len()).filter(|&i| answers[i].is_err() && label(i).is_some()).collect();
            if let Some(&first) = refused.first() {
                let error = answers[first].as_ref().err().unwrap();
                if self.client.label_restored(label(first), error)? {
                    let again: Vec<BatchRequest> = refused.iter().map(|&i| requests[i].clone()).collect();
                    let resent = match self.client.client().send_writes(&again) {
                        Ok(resent) => resent,
                        Err(e) => return Ok((done, Some(e))),
                    };
                    for (i, answer) in refused.into_iter().zip(resent) {
                        answers[i] = answer;
                    }
                }
            }
            let mut failure = None;
            for ((s, _), answer) in batch.iter().zip(answers) {
                match answer {
                    Ok(_) => done.push(*s),
                    Err(e) if steps[*s].action == "create" && google_status(&e) == Some(409) => done.push(*s),
                    Err(e) if google_status(&e) == Some(412) => failure = failure.or(Some(changed_since(&steps[*s], &target(&steps[*s])))),
                    Err(e) => failure = failure.or(Some(e)),
                }
            }
            if failure.is_some() {
                return Ok((done, failure));
            }
        }
        Ok((done, blocked))
    }
}

/// `edit_note`: `NotedTimeSheet::edit`, refused (a CompactionError) for a
/// note a compaction is partway through applying -- it stamps its notes
/// last, by their ids. A note changed since the version given is a
/// `Conflict`, as it is.
pub fn edit_note(
    notes: &NotedTimeSheet,
    journal: &CompactionJournal,
    note_id: &str,
    timestamp: Option<Moment>,
    description: Option<&str>,
    if_version: Option<i64>,
) -> anyhow::Result<SheetNote> {
    require_not_being_applied(journal, note_id)?;
    match notes.edit(note_id, timestamp, description, if_version) {
        Ok(note) => Ok(note),
        Err(e) if google_status(&e).is_some() || crate::sheet::rows::conflicts_of(&e).is_some() => Err(e),
        Err(e) => Err(ce(e.to_string(), "note_edit")),
    }
}

/// `delete_note`: `NotedTimeSheet::delete`, refused like `edit_note`.
pub fn delete_note(notes: &NotedTimeSheet, journal: &CompactionJournal, note_id: &str, if_version: Option<i64>) -> anyhow::Result<Option<SheetNote>> {
    require_not_being_applied(journal, note_id)?;
    match notes.delete(note_id, if_version) {
        Ok(note) => Ok(note),
        Err(e) if google_status(&e).is_some() || crate::sheet::rows::conflicts_of(&e).is_some() => Err(e),
        Err(e) => Err(ce(e.to_string(), "note_edit")),
    }
}

fn require_not_being_applied(journal: &CompactionJournal, note_id: &str) -> anyhow::Result<()> {
    for (compaction_id, status) in journal.open_compactions()? {
        let compaction = journal.load(&compaction_id)?;
        if compaction.note_ids.iter().any(|n| n == note_id) {
            return Err(ce(
                format!(
                    "note {} is part of compaction {}, which is {status} -- finish it with finish_proposal, or abandon it with abandon_compaction, first",
                    py::repr(note_id),
                    compaction.batch_id()
                ),
                "note_in_compaction",
            ));
        }
    }
    Ok(())
}

/// `_cut`: the span `day_for` reads, ended where the decision on its last
/// night puts it.
fn cut(
    day_for: &dyn Fn(&[String], Option<Moment>) -> anyhow::Result<Day>,
    night: Option<&dyn Fn(&str) -> Option<EventDecision>>,
    times: &[(String, Moment)],
) -> anyhow::Result<Day> {
    let mut sleepless: Vec<String> = Vec::new();
    let mut overrun: Option<Moment> = None;
    let mut day = day_for(&sleepless, overrun)?;
    while let (Some(night), Some(closing)) = (night, day.closing.clone()) {
        let closing_id = closing.id.clone().unwrap_or_default();
        let Some(decision) = night(&closing_id) else { break };
        if decision.action == "cancel" && !sleepless.contains(&closing_id) {
            sleepless.push(closing_id);
            overrun = None;
        } else if decision.action == "keep" && overrun.is_none() {
            let end = decision.end.or_else(|| decision.end_note.as_ref().and_then(|n| times.iter().rev().find(|(k, _)| k == n).map(|(_, t)| *t)));
            match end {
                Some(end) if end > closing.end() => overrun = Some(end),
                _ => break,
            }
        } else {
            break;
        }
        day = day_for(&sleepless, overrun)?;
    }
    Ok(day)
}

fn lookup_event<'a>(befores: &'a [(String, Event)], id: &str) -> Option<&'a Event> {
    befores.iter().rev().find(|(k, _)| k == id).map(|(_, e)| e)
}

/// The changes, comparable as Python compares them (sorted reprs).
fn comparable(changes: &[CompactionChange]) -> Vec<String> {
    let mut out: Vec<String> = changes
        .iter()
        .map(|c| {
            py::json_default(&json!([
                c.action,
                c.event_id,
                c.key,
                c.before.as_ref().map(EventState::to_json_dict),
                c.after.as_ref().map(EventState::to_json_dict)
            ]))
        })
        .collect();
    out.sort();
    out
}

/// `_judged_events`: the ids of a batch's events whose facts it set, and
/// the span of time they're in.
pub fn judged_events(days: &[JournalCompaction]) -> (Vec<String>, (Moment, Moment)) {
    let mut ids: Vec<String> = Vec::new();
    let mut spans: Vec<(Moment, Moment)> = Vec::new();
    for day in days {
        for step in &day.steps {
            let Some(after) = step.after.as_ref().filter(|a| a.facts.as_ref().is_some_and(|f| truthy(Some(f)))) else { continue };
            if step.action == "cancel" {
                continue;
            }
            ids.push(if step.action == "update" { step.event_id.clone().unwrap_or_default() } else { new_event_id(&day.id, step.step) });
            spans.push((after.start(), after.end.unwrap()));
        }
    }
    if let Some(meta) = &days[0].meta {
        for entry in &meta.judge_also {
            if let Some(id) = entry.get(0).map(|v| python_str(Some(v))) {
                if !ids.contains(&id) {
                    ids.push(id);
                }
            }
        }
    }
    if spans.is_empty() {
        return (ids, (days[0].now, days[0].now));
    }
    let start = spans.iter().map(|(s, _)| *s).min().unwrap();
    let end = spans.iter().map(|(_, e)| *e).reduce(pmax).unwrap();
    (ids, (start, end))
}

/// `_keyed`: `decisions` with a key on every create.
fn keyed(proposal: &str, decisions: Vec<EventDecision>, meta: Option<&RevisionMeta>) -> anyhow::Result<(Vec<EventDecision>, i64)> {
    let mut key_seq = meta.map(|m| m.key_seq).unwrap_or(0);
    let mut problems: Vec<String> = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    let mut out: Vec<EventDecision> = Vec::new();
    for mut decision in decisions {
        if decision.action != "create" {
            decision.key = None;
            out.push(decision);
            continue;
        }
        if let Some(key) = decision.key.clone() {
            let number = claude_key_number(proposal, Some(&key));
            if number.is_none_or(|n| n > key_seq) || seen.contains(&key) {
                problems.push(format!(
                    "create {}: {} isn't one of proposal {proposal}'s keys (keep only those prepare_compaction gave; leave it out for something new)",
                    repr_opt(decision.summary.as_deref()),
                    py::repr(&key)
                ));
                continue;
            }
            seen.push(key);
            out.push(decision);
            continue;
        }
        key_seq += 1;
        decision.key = Some(claude_key(proposal, key_seq));
        out.push(decision);
    }
    if !problems.is_empty() {
        return Err(ce_of(&problems, "unknown_key"));
    }
    Ok((out, key_seq))
}

/// `_create_refs`: each create's `ref` -> the key it was given.
fn create_refs_of(decisions: &[EventDecision], refs: &[Option<String>]) -> anyhow::Result<Vec<(String, String)>> {
    let creates: Vec<&EventDecision> = decisions.iter().filter(|d| d.action == "create").collect();
    let mut out: Vec<(String, String)> = Vec::new();
    let mut problems: Vec<String> = Vec::new();
    for (decision, r) in creates.iter().zip(refs) {
        let Some(r) = r else { continue };
        if out.iter().any(|(k, _)| k == r) {
            problems.push(format!("create {}: the ref {} is another create's too", repr_opt(decision.summary.as_deref()), py::repr(r)));
            continue;
        }
        out.push((r.clone(), decision.key.clone().unwrap_or_default()));
    }
    if !problems.is_empty() {
        return Err(ce_of(&problems, "duplicate_ref"));
    }
    Ok(out)
}

/// `_note_targets`: `annotate_notes` as note -> event.
fn note_targets(annotations: &[(String, String)]) -> anyhow::Result<Vec<(String, String)>> {
    let mut targets: Vec<(String, String)> = Vec::new();
    let mut twice: Vec<String> = Vec::new();
    for (note, event) in annotations {
        match targets.iter_mut().find(|(k, _)| k == note) {
            Some(slot) => {
                twice.push(note.clone());
                slot.1 = event.clone();
            }
            None => targets.push((note.clone(), event.clone())),
        }
    }
    if !twice.is_empty() {
        return Err(ce(
            format!("annotate_notes: each note goes with one event -- {} are given more than once", twice.join(", ")),
            "malformed_decision",
        ));
    }
    Ok(targets)
}

/// Each settled ref with its id: none if dropped.
fn settled_ids(settled: &Dict<AdditionSettled>) -> Vec<(String, Option<String>)> {
    settled.values().map(|s| (s.ref_.clone(), s.id.clone())).collect()
}

fn claude_additions_of(days: &[JournalCompaction]) -> Map<String, Value> {
    if let Some(additions) = days[0].meta.as_ref().and_then(|m| m.claude_additions.clone()) {
        return additions;
    }
    days[0].additions.clone()
}

fn unsettled<V>(additions: &Additions, settled: &Dict<V>) -> Additions
where
    V: Clone,
{
    additions.filtered(|r| !settled.contains(&r.to_string()))
}

fn settled_decisions(merged: &Merged) -> Vec<EventDecision> {
    merged.decisions.iter().map(|d| settle_refs(d, &merged.settled)).collect()
}

fn claude_ignore(days: &[JournalCompaction]) -> Vec<String> {
    if let Some(ignore) = days[0].meta.as_ref().and_then(|m| m.claude_ignore_notes.clone()) {
        return ignore;
    }
    let mut all: Vec<String> = days.iter().flat_map(|d| d.ignore_notes.clone()).collect();
    all.sort();
    all.dedup();
    all
}

fn proposal_notes(walked: &[Walked], merged: &Merged) -> Vec<ProposalNote> {
    let mut notes = Vec::new();
    for w in walked {
        for note in &w.day.notes {
            let id = note.id();
            let (use_, event) = w.plan.note_uses.iter().find(|(k, _)| *k == id).map(|(_, v)| v.clone()).unwrap_or(("unused".into(), None));
            let edge = w.plan.note_edges.iter().find(|(k, _)| *k == id).map(|(_, v)| v.clone());
            let mut decided_by = merged.notes_decided_by.get(&id).cloned();
            if decided_by.is_none() && use_ == "edge" {
                decided_by = event.as_ref().and_then(|e| merged.decided_by.get(e).cloned());
            }
            notes.push(ProposalNote {
                id,
                timestamp: note.note.timestamp,
                description: note.note.description.clone(),
                use_,
                event_id: event.filter(|e| !e.starts_with("new:")),
                edge_of: edge.filter(|e| !e.starts_with("new:")),
                decided_by,
            });
        }
    }
    notes
}

fn has_anything(walked: &[Walked]) -> bool {
    walked.iter().any(|w| !w.day.notes.is_empty())
        || walked.iter().any(|w| w.day.events.iter().any(|e| e.start() < w.day.now && e.end() > w.day.compaction_window_start))
}

/// `json.dumps(..., sort_keys=True)`.
fn sorted_keys(value: &Value) -> Value {
    match value {
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort();
            Value::Object(keys.into_iter().map(|k| (k.clone(), sorted_keys(&map[k]))).collect())
        }
        Value::Array(items) => Value::Array(items.iter().map(sorted_keys).collect()),
        other => other.clone(),
    }
}

fn outcomes_of(walked: &[Walked]) -> Vec<(String, String)> {
    let mut outcomes: Vec<(String, String)> = Vec::new();
    for w in walked {
        for change in &w.plan.changes {
            let Some(name) = change.event_id.clone().or(change.key.clone()) else { continue };
            let digest = py::json_default(&sorted_keys(&json!([change.action, change.after.as_ref().map(EventState::to_json_dict)])));
            let hash = Sha1::digest(digest.as_bytes());
            let hex: String = hash.iter().map(|b| format!("{b:02x}")).collect::<String>()[..10].to_string();
            match outcomes.iter_mut().find(|(k, _)| *k == name) {
                Some(slot) => slot.1 = hex,
                None => outcomes.push((name, hex)),
            }
        }
    }
    outcomes
}

fn changed(before: Option<&[(String, String)]>, after: &[(String, String)]) -> Vec<String> {
    let Some(before) = before else { return Vec::new() };
    let mut names: Vec<String> = before.iter().chain(after).map(|(k, _)| k.clone()).collect();
    names.sort();
    names.dedup();
    names.into_iter().filter(|n| lookup(before, n) != lookup(after, n)).collect()
}

pub fn state_of(days: &[JournalCompaction], feedback: &[Feedback]) -> String {
    let statuses: Vec<&str> = days.iter().map(|d| d.status.as_str()).collect();
    if statuses.iter().all(|s| *s == STAMPED) {
        return "applied".into();
    }
    if statuses.iter().any(|s| OPEN_STATUSES.contains(s)) {
        return "applying".into();
    }
    if statuses.contains(&ABANDONED) {
        return "abandoned".into();
    }
    if statuses.contains(&FAILED) || feedback.iter().any(|f| f.status == "open") {
        return "awaiting_claude".into();
    }
    "awaiting_review".into()
}

fn proposal_events(walked: &[Walked], decided_by: &Dict<String>) -> Vec<ProposalEvent> {
    let mut out: Vec<(String, ProposalEvent)> = Vec::new();
    for w in walked {
        let day = &w.day;
        let change_of = |id: &str| w.plan.changes.iter().rev().find(|c| c.event_id.as_deref() == Some(id));
        let merged: Vec<&String> = w.decisions.iter().filter(|d| d.action == "merge").filter_map(|d| d.event_id.as_ref()).collect();
        let cancel_of = |id: &str| w.decisions.iter().rev().find(|d| d.action == "cancel" && d.event_id.as_deref() == Some(id));
        let against = |id: &str| -> Vec<String> {
            w.plan
                .timeline
                .as_ref()
                .and_then(|t| t.events.iter().rev().find(|e| e.event_id.as_deref() == Some(id)))
                .map(|e| e.follow_through.clone())
                .unwrap_or_default()
        };
        for event in &day.events {
            let Some(id) = event.id.clone().filter(|i| !i.is_empty() && !i.starts_with("planned")) else { continue };
            let change = change_of(&id);
            if out.iter().any(|(k, _)| *k == id) && change.is_none() {
                continue;
            }
            let state = match change {
                Some(c) if c.action == "update" => c.after.clone().unwrap(),
                _ => EventState::from_event(event),
            };
            let status = if change.is_some_and(|c| c.action == "cancel") {
                if merged.contains(&&id) { "merged" } else { "cancelled" }
            } else if (state.start, state.end) != (event.start, event.end) {
                "adjusted"
            } else if event.start() < day.now {
                "on_schedule"
            } else {
                "planned"
            };
            let counts = if status == "cancelled" {
                Some(cancel_of(&id).is_none_or(|d| d.counts_against_follow_through != Some(false)))
            } else {
                None
            };
            let shown = ProposalEvent {
                counts_against_follow_through: counts,
                follow_through: if counts == Some(true) { against(&id) } else { Vec::new() },
                id: id.clone(),
                summary: state.summary.clone(),
                start: state.start,
                end: state.end,
                status: status.into(),
                planned_start: event.start,
                planned_end: event.end,
                description: state.description.clone(),
                location: state.location.clone(),
                priority: state.priority,
                action_ids: state.action_ids.clone(),
                goals: state.goals.clone(),
                facts: state.facts.as_ref().map(|f| facts_from_dict(Some(f))),
                decided_by: decided_by.get(&id).cloned(),
                history_until: day.history_until.filter(|h| event.start() < *h).map(|h| event.end().min(h)),
                is_end_of_day_sleep: event.is_end_of_day_sleep == Some(true),
            };
            match out.iter_mut().find(|(k, _)| *k == id) {
                Some(slot) => slot.1 = shown,
                None => out.push((id, shown)),
            }
        }
        for change in &w.plan.changes {
            let Some(after) = change.after.as_ref().filter(|_| change.action == "create") else { continue };
            let name = change.key.clone().unwrap_or_else(|| format!("new{}", out.len() + 1));
            let shown = ProposalEvent {
                id: name.clone(),
                summary: after.summary.clone(),
                start: after.start,
                end: after.end,
                status: "new".into(),
                description: after.description.clone(),
                location: after.location.clone(),
                priority: after.priority,
                action_ids: after.action_ids.clone(),
                goals: after.goals.clone(),
                facts: after.facts.as_ref().map(|f| facts_from_dict(Some(f))),
                decided_by: change.key.as_ref().and_then(|k| decided_by.get(k).cloned()),
                ..Default::default()
            };
            match out.iter_mut().find(|(k, _)| *k == name) {
                Some(slot) => slot.1 = shown,
                None => out.push((name, shown)),
            }
        }
    }
    let mut events: Vec<ProposalEvent> = out.into_iter().map(|(_, e)| e).collect();
    events.sort_by(|a, b| (a.start.or(a.planned_start), &a.id).cmp(&(b.start.or(b.planned_start), &b.id)));
    events
}

/// `_day_starts`: where each of the days a span takes on starts.
fn day_starts(day: &Day, decisions: &[EventDecision]) -> Vec<Moment> {
    let sleepless: Vec<&String> = decisions.iter().filter(|d| d.action == "cancel").filter_map(|d| d.event_id.as_ref()).collect();
    let mut ends: Vec<Moment> = day
        .events
        .iter()
        .filter(|e| {
            e.id.as_ref().is_some_and(|id| day.nights.contains(id) && !sleepless.contains(&id)) && day.day_start < e.end() && e.end() < day.now
        })
        .map(|e| e.end())
        .collect();
    ends.sort();
    let mut starts = vec![day.day_start];
    starts.extend(ends);
    starts
}

fn joined(walked: &[Walked]) -> Timeline {
    join_days(walked.iter().flat_map(|w| split_days(w.plan.timeline.as_ref().unwrap(), &day_starts(&w.day, &w.decisions))).collect())
}

/// `_befores`: the first note of each day a span takes on, with the event
/// that ended just before that day.
fn befores(day: &Day) -> Vec<(String, Event)> {
    let starts = day_starts(day, &[]);
    let mut nights: Vec<(Moment, Event)> = Vec::new();
    for e in &day.events {
        if e.id.as_ref().is_some_and(|id| day.nights.contains(id)) {
            match nights.iter_mut().find(|(end, _)| *end == e.end()) {
                Some(slot) => slot.1 = e.clone(),
                None => nights.push((e.end(), e.clone())),
            }
        }
    }
    let mut out: Vec<(String, Event)> = Vec::new();
    for (i, start) in starts.iter().enumerate() {
        let end = starts.get(i + 1);
        let mut first: Option<&SheetNote> = None;
        for n in &day.notes {
            let t = n.note.timestamp;
            let from = if i > 0 { *start } else { t };
            if t >= from && end.is_none_or(|e| t < *e) && first.is_none_or(|f| t < f.note.timestamp) {
                first = Some(n);
            }
        }
        let before = if i == 0 { day.previous.clone() } else { nights.iter().find(|(e, _)| e == start).map(|(_, ev)| ev.clone()) };
        if let (Some(first), Some(before)) = (first, before) {
            let id = first.id();
            match out.iter_mut().find(|(k, _)| *k == id) {
                Some(slot) => slot.1 = before,
                None => out.push((id, before)),
            }
        }
    }
    out
}

fn display_offset(day: &Day) -> chrono::FixedOffset {
    match day.notes.first() {
        Some(n) => *n.note.timestamp.offset(),
        None => *day.day_start.offset(),
    }
}

fn context_days(day: &Day) -> Option<Vec<ContextDay>> {
    let starts = day_starts(day, &[]);
    if starts.len() < 2 {
        return None;
    }
    let tz = display_offset(day);
    let mut ends: Vec<Moment> = starts[1..].to_vec();
    ends.push(day.day_end);
    Some(
        starts
            .iter()
            .zip(&ends)
            .enumerate()
            .map(|(i, (start, end))| ContextDay {
                label: start.with_timezone(&tz).format("%a %d %b").to_string(),
                compaction_window_start: pmax(*start, day.compaction_window_start),
                day_end: *end,
                note_ids: day
                    .notes
                    .iter()
                    .filter(|n| (i == 0 || n.note.timestamp >= *start) && (*end == day.day_end || n.note.timestamp < *end))
                    .map(SheetNote::id)
                    .collect(),
            })
            .collect(),
    )
}

fn day_id(batch_id: &str, number: usize) -> String {
    if number == 1 { batch_id.to_string() } else { format!("{batch_id}d{number}") }
}

fn plan_notes(day: &Day) -> Vec<PlanNote> {
    day.notes.iter().map(|n| PlanNote { id: n.id(), timestamp: n.note.timestamp, description: n.note.description.clone() }).collect()
}

fn latest_compacted(day: &Day) -> Option<PlanNote> {
    day.latest_compacted.as_ref().map(|l| PlanNote { id: "previous_note".into(), timestamp: l.timestamp, description: l.description.clone() })
}

fn shown(day: &Day, candidates: &[(String, Vec<String>)]) -> Vec<Event> {
    let near: Vec<&String> = candidates.iter().flat_map(|(_, ids)| ids).collect();
    day.events
        .iter()
        .filter(|e| e.id.as_ref().is_some_and(|id| !id.is_empty() && (e.start() < day.now || near.contains(&id))))
        .cloned()
        .collect()
}

fn candidates(timestamp: Moment, events: &[Event], previous: Option<&Event>) -> Vec<String> {
    let window = Duration::minutes(CANDIDATE_MINUTES);
    let mut near: Vec<&Event> = events
        .iter()
        .filter(|e| e.id.as_ref().is_some_and(|i| !i.is_empty()) && e.end() > timestamp - window && e.start() < timestamp + window)
        .collect();
    near.sort_by_key(|e| (e.start() - timestamp).abs());
    let mut ids: Vec<String> = near.iter().map(|e| e.id.clone().unwrap()).collect();
    if let Some(id) = previous.and_then(|p| p.id.clone()).filter(|i| !i.is_empty()) {
        if !ids.contains(&id) {
            ids.push(id);
        }
    }
    ids
}

fn title_key(summary: &str) -> String {
    summary.to_lowercase().split_whitespace().collect::<Vec<_>>().join(" ")
}

/// `_keep_priorities`: give each event the plan settles the priority its
/// actions give it now, as its own, unless it has one.
fn keep_priorities(plan: &mut CompactionPlan, tree: &ActionTree, day: &Day) {
    let kept = |state: &EventState| -> Option<i64> {
        let start = state.start?;
        if start >= day.now || state.priority.is_some() {
            return None;
        }
        state.action_ids.iter().flatten().filter_map(|a| tree.priority(&a.action_id)).min()
    };
    let mut changed: Vec<Option<String>> = Vec::new();
    for change in plan.changes.iter_mut() {
        changed.push(change.event_id.clone());
        if change.action != "cancel" {
            if let Some(after) = change.after.as_mut() {
                if let Some(priority) = kept(after) {
                    after.priority = Some(priority);
                }
            }
        }
    }
    for event in &day.events {
        let Some(id) = event.id.clone().filter(|i| !i.is_empty()) else { continue };
        if changed.contains(&Some(id.clone())) || event.is_cancelled() {
            continue;
        }
        let before = EventState::from_event(event);
        if let Some(priority) = kept(&before) {
            let mut after = before.clone();
            after.priority = Some(priority);
            plan.changes.push(CompactionChange {
                action: "update".into(),
                event_id: Some(id),
                key: None,
                reason: "kept the priority its actions give it, as its own".into(),
                before: Some(before),
                after: Some(after),
            });
        }
    }
}

pub fn new_event_id(compaction_id: &str, step: i64) -> String {
    format!("cmp{compaction_id}s{step:03}")
}

/// The open goals (inactive or active), as `prepare` lists them: those
/// with attribution hints are the ones compaction may tag events with;
/// all of them, the ones a new goal mustn't repeat.
pub fn open_goals(all: &[goals::Goal]) -> Vec<Value> {
    all.iter()
        .filter(|g| g.status.is_some_and(|s| !s.is_closed()))
        .filter(|g| g.id.as_deref().is_some_and(|i| !i.is_empty()))
        .map(|g| {
            let hints = g.attribution_hints.as_deref().map(str::trim).filter(|h| !h.is_empty());
            let mut out = json!({"id": g.id, "title": g.title, "path": goals::path(g, all), "status": g.status, "deadline": g.deadline});
            if let Some(hints) = hints {
                out["attribution_hints"] = json!(hints);
            }
            for (key, ids) in [("with_ids", &g.with_ids), ("for_ids", &g.for_ids)] {
                if let Some(ids) = ids.as_ref().filter(|i| !i.is_empty()) {
                    out[key] = json!(ids);
                }
            }
            out
        })
        .collect()
}

/// Whether each field `step` changes is, `now`, as it was planned from or
/// as planned: not changed since by anything else.
fn as_planned(step: &JournalStep, now: &EventState) -> bool {
    let before = step.before.clone().unwrap_or_default();
    let after = step.after.clone().unwrap_or_default();
    let patch = patch_for(step);
    macro_rules! held {
        ($($field:ident),*) => { true $(&& (patch.$field.is_none() || now.$field == before.$field || now.$field == after.$field))* };
    }
    held!(summary, start, end, description, location, priority, event_label_id, action_ids, goals) && (patch.facts.is_none() || now.facts == before.facts || now.facts == after.facts)
}

fn patch_for(step: &JournalStep) -> Event {
    if step.action == "cancel" {
        return Event { id: step.event_id.clone(), status: Some("cancelled".into()), ..Default::default() };
    }
    let before = step.before.clone().unwrap_or_default();
    let after = step.after.clone().unwrap_or_default();
    let mut patch = Event { id: step.event_id.clone(), ..Default::default() };
    if after.summary.is_some() && after.summary != before.summary {
        patch.summary = after.summary.clone();
    }
    if after.start.is_some() && after.start != before.start {
        patch.start = after.start;
    }
    if after.end.is_some() && after.end != before.end {
        patch.end = after.end;
    }
    if after.description.is_some() && after.description != before.description {
        patch.description = after.description.clone();
    }
    if after.location.is_some() && after.location != before.location {
        patch.location = after.location.clone();
    }
    if after.priority.is_some() && after.priority != before.priority {
        patch.priority = after.priority;
    }
    if after.event_label_id.is_some() && after.event_label_id != before.event_label_id {
        patch.event_label_id = after.event_label_id.clone();
    }
    if after.action_ids.is_some() && after.action_ids != before.action_ids {
        patch.action_ids = after.action_ids.clone();
    }
    if after.goals.is_some() && after.goals != before.goals {
        patch.goals = after.goals.clone();
    }
    if after.facts != before.facts {
        patch.facts = Some(facts_from_dict(after.facts.as_ref()));
    }
    patch
}

#[allow(dead_code)]
fn unused(_: Problem, _: Option<Value>) -> Option<Value> {
    facts_dict(None)
}

#[cfg(test)]
#[path = "compactor_tests/mod.rs"]
mod tests;
