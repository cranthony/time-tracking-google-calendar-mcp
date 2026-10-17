//! The compaction tools: compacting notes into the calendar as proposals
//! the user confirms, judging the events it settles, and correcting notes.

use super::events::{when_given, EventCancel, Facts as PublicFacts, NoteWithId, NotedTime, PublicEvent};
use super::{from_store, texts, ToolError};
use crate::contract::{tool, wire_text, DateTimeText, Schema};
use crate::app::App;
use crate::compaction::additions::{Addition, Additions};
use crate::compaction::compactor::{compaction_error, NoteCompactor};
use crate::compaction::judgments::{self, Judging};
use crate::compaction::plan::EventDecision;
use crate::compaction::proposals;
use crate::facts::Facts;
use crate::py;
use crate::stores::action_calendar::ActionCalendar;
use indexmap::IndexMap;
use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

// -- the types -------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum ProposalSummaryState {
    #[serde(rename = "awaiting_review")]
    AwaitingReview,
    #[serde(rename = "awaiting_claude")]
    AwaitingClaude,
    #[serde(rename = "applying")]
    Applying,
    #[serde(rename = "applied")]
    Applied,
    #[serde(rename = "abandoned")]
    Abandoned,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum AdditionSettledUse {
    #[serde(rename = "create")]
    Create,
    #[serde(rename = "existing")]
    Existing,
    #[serde(rename = "drop")]
    Drop,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum FeedbackBy {
    #[serde(rename = "user")]
    User,
    #[serde(rename = "server")]
    Server,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum FeedbackStatus {
    #[serde(rename = "open")]
    Open,
    #[serde(rename = "answered")]
    Answered,
    #[serde(rename = "withdrawn")]
    Withdrawn,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum TimelineEventStatus {
    #[serde(rename = "planned")]
    Planned,
    #[serde(rename = "on_schedule")]
    OnSchedule,
    #[serde(rename = "adjusted")]
    Adjusted,
    #[serde(rename = "new")]
    New,
    #[serde(rename = "cancelled")]
    Cancelled,
    #[serde(rename = "merged")]
    Merged,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum TimelineEventMissing {
    #[serde(rename = "action")]
    Action,
    #[serde(rename = "location")]
    Location,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum UserEditStatus {
    #[serde(rename = "active")]
    Active,
    #[serde(rename = "inapplicable")]
    Inapplicable,
    #[serde(rename = "replaced")]
    Replaced,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum NewActionStatus {
    #[serde(rename = "proposed")]
    Proposed,
    #[serde(rename = "active")]
    Active,
    #[serde(rename = "archived")]
    Archived,
    #[serde(rename = "deleted")]
    Deleted,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum NewPersonStatus {
    #[serde(rename = "active")]
    Active,
    #[serde(rename = "archived")]
    Archived,
    #[serde(rename = "deleted")]
    Deleted,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum CompactionChangeAction {
    #[serde(rename = "update")]
    Update,
    #[serde(rename = "create")]
    Create,
    #[serde(rename = "cancel")]
    Cancel,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum CompactionResultStatus {
    #[serde(rename = "planned")]
    Planned,
    #[serde(rename = "proposed")]
    Proposed,
    #[serde(rename = "applied")]
    Applied,
    #[serde(rename = "already_compacted")]
    AlreadyCompacted,
    #[serde(rename = "abandoned")]
    Abandoned,
    #[serde(rename = "nothing_to_compact")]
    NothingToCompact,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum ProposalEventStatus {
    #[serde(rename = "on_schedule")]
    OnSchedule,
    #[serde(rename = "adjusted")]
    Adjusted,
    #[serde(rename = "new")]
    New,
    #[serde(rename = "cancelled")]
    Cancelled,
    #[serde(rename = "merged")]
    Merged,
    #[serde(rename = "planned")]
    Planned,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum ProposalEventDecidedBy {
    #[serde(rename = "claude")]
    Claude,
    #[serde(rename = "user")]
    User,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum ProposalNoteUse {
    #[serde(rename = "edge")]
    Edge,
    #[serde(rename = "annotates")]
    Annotates,
    #[serde(rename = "ignored")]
    Ignored,
    #[serde(rename = "unused")]
    Unused,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum ProposalBy {
    #[serde(rename = "claude")]
    Claude,
    #[serde(rename = "user")]
    User,
    #[serde(rename = "server")]
    Server,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum NoteEditUse {
    #[serde(rename = "annotate")]
    Annotate,
    #[serde(rename = "ignore")]
    Ignore,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum ProposalUpdateClearFields {
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

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum ProposalResultStatus {
    #[serde(rename = "applied")]
    Applied,
    #[serde(rename = "rechecked")]
    Rechecked,
    #[serde(rename = "needs_claude")]
    NeedsClaude,
    #[serde(rename = "rebuilt")]
    Rebuilt,
    #[serde(rename = "abandoned")]
    Abandoned,
}

/// The open proposal, for `get_compaction_status`.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ProposalSummary {
    pub id: String,
    pub revision: i64,
    pub state: ProposalSummaryState,
    pub window_start: DateTimeText,
    pub through: DateTimeText,
    pub open_feedback: i64,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct CompactionStatus {
    pub last_compaction: Option<DateTimeText>,
    pub latest_compacted_note: Option<NotedTime>,
    pub judgments_pending: Option<String>,
    pub proposal: Option<ProposalSummary>,
}

/// An addition the user settled, as a proposal shows it: its `id`, or
/// `None` if they dropped it.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct AdditionSettled {
    pub r#ref: String,
    pub r#use: AdditionSettledUse,
    pub id: Option<String>,
}

/// An action an event can be given (its groups left out).
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ContextAction {
    pub id: String,
    pub name: String,
    pub note: Option<String>,
}

/// One of several days compacted together.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ContextDay {
    pub label: String,
    pub compaction_window_start: DateTimeText,
    pub day_end: DateTimeText,
    pub note_ids: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ContextEvent {
    pub id: String,
    #[schema(required)]
    pub summary: Option<String>,
    pub start: DateTimeText,
    pub end: DateTimeText,
    pub description: Option<String>,
    pub action_ids: Option<Vec<String>>,
    pub action_names: Option<Vec<String>>,
    pub suggested_action_ids: Option<Vec<String>>,
    pub priority: Option<i64>,
    pub facts: Option<PublicFacts>,
    pub history_until: Option<DateTimeText>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ContextLocation {
    pub id: String,
    pub name: String,
    pub hint: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ContextNote {
    pub id: String,
    pub timestamp: DateTimeText,
    #[schema(required)]
    pub description: Option<String>,
    pub candidates: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ContextPerson {
    pub id: String,
    pub name: String,
    pub context: Option<String>,
    pub what_matters: Option<String>,
}

/// A note for Claude on a proposal: the user's, or the server's when it
/// needs Claude (a revision that no longer plans).
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct Feedback {
    pub id: String,
    pub seq: i64,
    pub text: String,
    pub event_id: Option<String>,
    pub note_id: Option<String>,
    pub at: Option<DateTimeText>,
    #[schema(default = "\"user\"")]
    pub by: FeedbackBy,
    pub created: DateTimeText,
    #[schema(default = "\"open\"")]
    pub status: FeedbackStatus,
    pub reply: Option<String>,
    pub answered_in: Option<i64>,
    pub superseded_by: Option<Vec<String>>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct PreviousNote {
    pub timestamp: DateTimeText,
    #[schema(required)]
    pub description: Option<String>,
}

/// The open proposal, as `prepare_compaction` hands it to Claude to
/// revise or extend: its own decisions, to send again, and what the
/// user said.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ProposalContext {
    pub id: String,
    pub revision: i64,
    pub state: ProposalSummaryState,
    pub user_seq: i64,
    #[schema(optional)]
    #[serde(default)]
    pub updates: Vec<Map<String, Value>>,
    #[schema(optional)]
    #[serde(default)]
    pub creates: Vec<Map<String, Value>>,
    #[schema(optional)]
    #[serde(default)]
    pub cancels: Vec<Map<String, Value>>,
    #[schema(optional)]
    #[serde(default)]
    pub ignore_notes: Vec<String>,
    #[schema(optional)]
    #[serde(default)]
    pub annotate_notes: Vec<Map<String, Value>>,
    #[schema(optional)]
    #[serde(default)]
    pub new_actions: Vec<Map<String, Value>>,
    #[schema(optional)]
    #[serde(default)]
    pub new_people: Vec<Map<String, Value>>,
    #[schema(optional)]
    #[serde(default)]
    pub new_locations: Vec<Map<String, Value>>,
    #[schema(optional)]
    #[serde(default)]
    pub settled_additions: Vec<AdditionSettled>,
    #[schema(optional)]
    #[serde(default)]
    pub user_edits: Vec<UserEdit>,
    #[schema(optional)]
    #[serde(default)]
    pub feedback: Vec<Feedback>,
    pub recap: Option<Recap>,
}

/// Claude's account of a revision, for the user reviewing it in the
/// app: a short `overview`, then each thing it `did` -- what it decided,
/// and from which notes -- and each thing it's `unsure` of, saying what
/// it assumed (empty if nothing: say so by sending it empty). One point
/// each, citing the events and notes it's about. Written afresh for every
/// revision: the proposal as it now stands, not what changed.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct Recap {
    pub overview: String,
    pub did: Vec<RecapPoint>,
    pub unsure: Vec<RecapPoint>,
}

/// One point of Claude's recap: `text` in Markdown (bold, italics,
/// code, bullet and numbered lists -- no headings, tables or images), and
/// the events (`event_ids`: ids, keys, or a create's `ref` from the same
/// call) and notes (`note_ids`) it's about, for the user to go to.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct RecapPoint {
    pub text: String,
    #[schema(optional)]
    #[serde(default)]
    pub event_ids: Vec<String>,
    #[schema(optional)]
    #[serde(default)]
    pub note_ids: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct Timeline {
    pub notes: Vec<TimelineNote>,
    pub events: Vec<TimelineEvent>,
    pub now: Option<DateTimeText>,
    pub last_compaction: Option<DateTimeText>,
    #[schema(default = "true")]
    pub decided: bool,
    #[schema(default = "\"\"")]
    pub text: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct TimelineEvent {
    pub summary: String,
    pub status: TimelineEventStatus,
    pub event_id: Option<String>,
    pub start: Option<DateTimeText>,
    pub end: Option<DateTimeText>,
    pub planned_start: Option<DateTimeText>,
    pub planned_end: Option<DateTimeText>,
    pub start_note: Option<String>,
    pub end_note: Option<String>,
    pub merged_into: Option<String>,
    #[schema(optional)]
    #[serde(default)]
    pub actions: Vec<String>,
    #[schema(optional)]
    #[serde(default)]
    pub new_actions: Vec<String>,
    #[schema(optional)]
    #[serde(default)]
    pub facts: Vec<String>,
    #[schema(default = "false")]
    pub new_facts: bool,
    #[schema(optional)]
    #[serde(default)]
    pub missing: Vec<TimelineEventMissing>,
    #[schema(optional)]
    #[serde(default)]
    pub follow_through: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct TimelineNote {
    pub id: String,
    pub time: DateTimeText,
    pub text: Option<String>,
    #[schema(optional)]
    #[serde(default)]
    pub anchors: Vec<String>,
    pub annotates: Option<String>,
    #[schema(default = "false")]
    pub ignored: bool,
    #[schema(default = "false")]
    pub compacted: bool,
}

/// One of the user's edits to a proposal, as `amend_proposal` made it.
/// Kept for the whole proposal, in the order made (`seq`).
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct UserEdit {
    pub id: String,
    pub seq: i64,
    #[schema(required)]
    pub event_id: Option<String>,
    pub edit: Map<String, Value>,
    #[schema(default = "\"active\"")]
    pub status: UserEditStatus,
    pub created: DateTimeText,
    pub base_revision: i64,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct CompactionContext {
    pub notes: Vec<ContextNote>,
    pub events: Vec<ContextEvent>,
    pub now: Option<DateTimeText>,
    pub day_end: Option<DateTimeText>,
    #[schema(default = "0")]
    pub remaining_note_count: i64,
    pub days: Option<Vec<ContextDay>>,
    pub open_compaction: Option<String>,
    pub judgments_pending: Option<String>,
    pub compaction_window_start: Option<DateTimeText>,
    pub timeline: Option<Timeline>,
    pub actions: Option<Vec<ContextAction>>,
    pub people: Option<Vec<ContextPerson>>,
    pub locations: Option<Vec<ContextLocation>>,
    pub previous_note: Option<PreviousNote>,
    pub proposal: Option<ProposalContext>,
    #[schema(default = "\"`timeline` shows the notes beside the planned events -- for every day of notes up to now, each under a heading with its date (`days` lists them when there's more than one). Each day is compacted on its own, but you decide them all in one list. Compare them and decide, event by event, what the notes show happened differently -- then call compact_notes with them, as `updates`, `creates` and `cancels`. SILENCE MEANS ON SCHEDULE: any past event you don't mention is recorded exactly as planned, so only mention the events the notes contradict. Notes are sparse -- the user doesn't note every event, so a missing start or end note never means an event didn't happen or ran into its neighbor. READ EACH NOTE'S TENSE to tell which edge it marks: most notes are written as something finishes, so a note in the past tense ('finished the report', 'had lunch', 'done with email', 'went for a walk') marks the END of that event -- it ran up to the note's time, not from it. Only a note in the future tense ('going to the gym', 'will start the report', 'about to eat') or one that explicitly says it's starting something ('starting email', 'heading out to', 'now working on') marks a START. Don't assume a note starts the event it names; when the tense is ambiguous (a bare 'email'), use the timing and the neighboring notes, and ask if it's still unclear. `updates` {event_id}: it happened; each edge stays as planned unless you move it -- {start_note}/{end_note} (a note id) sets that edge to the note's time and links the note to it, {start}/{end} sets an explicit time (add the note too when it gives a time relative to itself, e.g. 'leaving 15 minutes early'). An update also takes {summary} to rename an event and {annotate} to add text to its description. `creates` {summary, a start and an end (a time or note each), action_ids?, facts?}: something unplanned happened. `cancels` {event_id, counts_against_follow_through}: it didn't happen -- true if the user dropped it (skipped it, didn't get to it: it counts against the follow-through of whoever it was planned with), false if the plan changed for another reason (someone else called it off, it moved elsewhere). An event goes in only one of the lists. When the user tells you they don't remember where one event ended and the next began, grow one with an update (renamed after both) and cancel the other, not counting it -- only then, never just because the notes are sparse. NOTHING IS MOVED TO MAKE ROOM: every event you update or create must end after it starts and must not overlap any other event in `events` -- past or still to come, the others you change included. When a moved edge runs into a neighbor, move, shorten or cancel the neighbor in the same call (an overrun usually delays or shortens the next event; ask the user if the notes don't say which gives way). A call that breaks this changes nothing, and lists every problem and the day as it would leave it, so fix them all at once. Every note you don't use as a start_note/end_note has its text added to the description of the event it falls within; list any that shouldn't be in `ignore_notes`, and any that belong with another event in `annotate_notes` ({note_id, event_id} -- an event's id, or a create's `key` from the open proposal; for a new create, put the text in its `annotate`). Use only note ids from `notes` (only this round's -- a longer backlog than this takes another round, see `remaining_note_count`) and event ids from `events`. `events` may start with one that ended just before `compaction_window_start` (usually last night's sleep); if a note shows it actually ran later -- the user slept in -- move its end with an update, and say when whatever it now overlaps happened. An event with `history_until` was settled by an earlier compaction up to then -- usually one still going on when it ran: its start, and its lasting until then, are fact, so keep its start, end it no earlier (it may well have run later), and don't cancel it; compact_notes refuses otherwise. Likewise, what's still going on now is recorded up to now, and the next compaction says where it ended. When the last compaction ran in the evening, before the night, this round starts there, at `compaction_window_start`: notes written during the planned night mean the user stayed up -- move the night's start (bedtime) to when they went to bed -- and a first day with no notes of its own settles that evening and night before the morning. `previous_note`, if there is one, is the last note an earlier compaction already used, however long ago -- context only (it can't be used as a start_note/end_note or ignored): e.g. if it said 'starting the report' shortly before `compaction_window_start`, the report was already under way as this window began, and the first note may well be its end; the longer before the window it was written, the less it says about how the window began. The timeline shows it too (\\u2713), and when the last compaction ran, both as context. COMPACTION RECORDS THE PAST: `events` runs to the end of the day, but `timeline` stops at `now`, except for later events near a note (one may be what a note starts early) and, once proposed, later events the plan changes. An update that moves a future event reschedules it -- for 'move lunch later and adjust the afternoon': move each event it now runs into too, in the same call. A day's end-of-day sleep event works differently: moving its start moves bedtime (an earlier one needs whatever runs past it shortened or cancelled in the same call), and its end -- the wake-up time -- starts the next day. Give each night at most one decision. A note that marks waking up (early or late) is that night's end_note; a note in the night that doesn't end it ('can't sleep') is just added to it. If the user slept in, the morning events the night now runs over -- past or still to come, even past this round's now -- have to be moved, shortened or cancelled in the same call. Cancel a night only if the user didn't sleep. Compaction never adjusts what's past the last night otherwise, so move only its start to change only bedtime. COMPACTION ESTABLISHES THE FACTS of every past event, which traits are judged from later -- so for each past event in the timeline, settle: what the user was doing (its ACTIONS), where it was, who was there with them, who it was for, and a note on each person who was there. Settle them from the notes, the title and the description; don't ask the user what the notes already say, and leave out what doesn't apply (a solo event has no `with_ids`). ACTIONS: each event's `action_ids` are what the user was doing, each a verb from `actions` (\\\"play guitar\\\", \\\"eat a meal\\\"); an event can have several, the first setting its color. For every past event with `suggested_action_ids`, add an update {event_id, action_ids} applying them unless the notes say otherwise; give a create its action_ids too. When no action fits, add one (see NEW below) rather than force a poor match. An update without action_ids keeps them, and [] clears them. In the timeline, \\u25c6 marks an action an event already has and \\u25c7 one it's being given (or, before deciding, one suggested). FACTS: add `facts` to the event's update (or create): location_ids (from `locations`, matched by their hints -- usually one; several when it moved between places), with_ids (people from `people` who were there; never \\\"self\\\" -- the user is at every event), for_ids (people it was done for who weren't there: preparing a gift or a plan -- then they're not in with_ids), and notes: {person id: a subjective line on how it was for them} for each person who was there, \\\"self\\\" for the user -- whatever the notes say that might help judge it later (they were tired, it was their idea, we laughed a lot). The notes aren't judgments: you don't know the traits, so just record what happened. Facts replace an event's facts whole, so an event that has some (`facts`) keeps them unless you send new ones. In the timeline, \\u25b8 marks facts an event has and \\u25b9 ones it's being given. Once proposed, \\u26a0 marks a past event still missing its action or location (listed again under `Missing:` at the end of the day): settle it from the notes and propose again, or, if they don't say, ask the user -- or, with no one to ask (a scheduled run), leave it for the user to settle in review. NEW actions, people and locations: when an event's action, a person or a place isn't in the lists, add it with compact_notes' new_actions, new_people or new_locations -- each with a `ref` starting \\\"new:\\\" (\\\"new:ukulele\\\") that your updates and creates use wherever its id would go -- rather than by separate tools: they're created when the plan is applied, so the user confirms them with it. A new action is a verb phrase, its status active (the user approves it with the plan); a new person needs a context when their name is taken; a new location needs a hint. Check the lists first: don't add one that's already there under another name. In a conversation, confirm with the user anything you couldn't settle from the notes -- who was there, where it was -- in a short list before proposing; with no one to ask, propose what the notes do say. The user confirms the whole proposal, in the app. In a conversation, after every compact_notes show the user the result's `timeline.text` verbatim in a code block (it's laid out narrow enough for a phone, so don't reformat or widen it -- it shows each event's actions and facts compactly, under it), then the new actions, people and locations it adds, and the warnings. RECAP: every compact_notes call takes your `recap` of the proposal, which the user reads beside it in the app: a short `overview`; `did`, one point for each thing you decided (what, and from which notes); and `unsure`, one point for each thing you're not sure of, saying what you assumed and what would settle it (empty only if nothing is in doubt -- don't pad it). Each point is Markdown (bold, italics, code, bullet and numbered lists; no headings or tables), and cites the events (`event_ids`: ids, keys, or the `ref` you give a create) and notes (`note_ids`) it's about, so the user can go straight to them. Don't restate the timeline: say what a reader couldn't see from it. Write it afresh for every revision, about the proposal as it then stands; feedback answers go in `replies`. PROPOSALS: compact_notes doesn't apply anything -- it writes a proposal (or a new revision of the open one) for the user to review in the app, where they confirm it, edit it, or leave you feedback. With a proposal open (`proposal`), every compact_notes call revises it: pass its proposal_id and the revision you started from, and send ALL your decisions again -- `proposal.updates`, `creates` and `cancels` are your current ones; keep each create's `key` so the user's edits of it follow it -- changed as the notes and the feedback say, planned to now. The user's own edits (`proposal.user_edits`) are laid over yours by the server: never send them, and don't fight them -- that includes what they said a note is for (an edit with action `note`), and the additions they settled (`proposal.settled_additions`: created, found among those there, or dropped -- name each by its `id` from then on, or leave it out; send `proposal.new_actions`, `new_people` and `new_locations` again for the rest). Answer every open feedback item (`proposal.feedback`, status open) in `replies`, saying what you changed, or asking what they meant if you can't tell; to change an event (or a note) the user edited, the feedback you answer has to be about that event (`event_id`) or note (`note_id`). Nothing is applied until the user confirms it, in the app (confirm_proposal isn't yours to call). If they ask you for changes, make them with compact_notes again, passing proposal_id and the revision you started from.\"")]
    pub instructions: String,
}

/// Something unplanned that happened: a `create` of compact_notes.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct CompactionCreate {
    pub summary: String,
    pub start: Option<DateTimeText>,
    pub end: Option<DateTimeText>,
    pub start_note: Option<String>,
    pub end_note: Option<String>,
    pub annotate: Option<String>,
    pub action_ids: Option<Vec<String>>,
    pub facts: Option<PublicFacts>,
    pub key: Option<String>,
    pub r#ref: Option<String>,
}

/// A planned event that happened: an `update` of compact_notes -- see
/// `NoteCompactor`'s instructions. Edges stay as planned unless moved, to
/// a time or to a note's.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct CompactionUpdate {
    pub event_id: String,
    pub summary: Option<String>,
    pub start: Option<DateTimeText>,
    pub end: Option<DateTimeText>,
    pub start_note: Option<String>,
    pub end_note: Option<String>,
    pub annotate: Option<String>,
    pub action_ids: Option<Vec<String>>,
    pub facts: Option<PublicFacts>,
}

/// Claude's answer to one feedback item: what it changed, or a question
/// back if it couldn't tell what was meant.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct FeedbackReply {
    pub feedback_id: String,
    pub reply: String,
}

/// An action to add: `ref` and `name`, and any of an action's other
/// fields (its status is active unless given: the user approves it with
/// the plan). id and label_id are assigned when it's created.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct NewAction {
    pub id: Option<String>,
    pub group_id: Option<String>,
    pub name: Option<String>,
    pub status: Option<NewActionStatus>,
    pub label_id: Option<String>,
    pub background_color: Option<String>,
    pub priority: Option<i64>,
    pub note: Option<String>,
    pub r#ref: String,
}

/// A location to add: `ref`, `name` and a `hint`. id is assigned when
/// it's created.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
#[schema(deny_unknown)]
pub struct NewLocation {
    pub id: Option<String>,
    pub name: Option<String>,
    pub hint: Option<String>,
    pub r#ref: String,
}

/// A person to add: `ref` and `name`, and any of a person's other
/// fields. id is assigned when they're created.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct NewPerson {
    pub id: Option<String>,
    pub name: Option<String>,
    pub context: Option<String>,
    pub status: Option<NewPersonStatus>,
    pub circles: Option<Vec<String>>,
    pub what_matters: Option<String>,
    pub traits: Option<Map<String, Value>>,
    pub r#ref: String,
}

/// A note to add to a particular event, rather than the one it falls
/// within: an `annotate_notes` entry of compact_notes.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct NoteAnnotation {
    pub note_id: String,
    pub event_id: String,
}

/// One calendar change a plan makes.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct CompactionChange {
    pub action: CompactionChangeAction,
    pub reason: String,
    pub event_id: Option<String>,
    pub key: Option<String>,
    pub before: Option<EventState>,
    pub after: Option<EventState>,
}

/// The judgments one event calls for.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct EventJudgmentsDue {
    pub event_id: String,
    #[schema(required)]
    pub summary: Option<String>,
    pub start: DateTimeText,
    pub end: DateTimeText,
    pub people: IndexMap<String, Vec<String>>,
    pub current: Option<IndexMap<String, IndexMap<String, Map<String, Value>>>>,
}

/// An event's state as recorded in a plan (and in the journal):
/// the fields compaction reads or changes.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct EventState {
    pub summary: Option<String>,
    pub start: Option<DateTimeText>,
    pub end: Option<DateTimeText>,
    pub description: Option<String>,
    pub location: Option<String>,
    pub status: Option<String>,
    pub priority: Option<i64>,
    pub event_label_id: Option<String>,
    pub action_ids: Option<Vec<String>>,
    pub facts: Option<Map<String, Value>>,
}

/// A judgment part of a trait, given once for every judgment of it --
/// see `Judging.context`.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct JudgmentPart {
    pub key: String,
    pub trait_name: String,
    #[schema(required)]
    pub definition: Option<String>,
    pub engagement: String,
    pub rubric: String,
    pub ratings: IndexMap<String, String>,
    pub facts: Vec<String>,
}

/// The judgments a compaction calls for -- those not made yet, or all
/// of them when they're being redone -- each part, and each person's
/// history, given once.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct JudgmentsDue {
    pub compaction_id: String,
    pub events: Vec<EventJudgmentsDue>,
    pub parts: Vec<JudgmentPart>,
    pub history: IndexMap<String, PersonHistory>,
    #[schema(default = "\"JUDGE EACH ONE YOURSELF, without asking the user: they've already confirmed the facts, and the judgments are yours to make. `events` lists each event the compaction settled with facts, each person it was about, and the parts to judge for each of them (\\\"<trait id>/<part key>\\\"). Judge each event as the applied compaction's final timeline shows it -- its times, notes, actions and facts -- for that person alone, as it was for them. A part is in `parts` under \\\"<trait id>/<part key>@<person id>\\\" if there's one (that person's own), or else \\\"<trait id>/<part key>\\\": answer its rubric -- a \\\"with\\\" part for someone who was there, a \\\"for\\\" part for someone it was done for while they weren't -- from what the timeline, the people's what_matters and `history` (what each person did and where over the days before) show, choosing one of its ratings (the number), with one succinct line of reasoning that names the fact it rests on. A habit of the user's (\\\"habit:<id>\\\") is judged like a person there, for how the event went as practice of that habit, its note in what_matters. Where they don't say, rate what they do show rather than guess -- a judgment can be redone later. Record them all with record_judgments(compaction_id, judgments), each with request_id \\\"<event id>/<person id>/<trait id>/<part key>\\\"; the compaction isn't complete until every one is. Don't show the user the judgments unless they ask.\"")]
    pub instructions: String,
}

/// What one person did and where, over the days before a compaction.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct PersonHistory {
    pub days: i64,
    pub actions: IndexMap<String, i64>,
    pub locations: IndexMap<String, i64>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct CompactionResult {
    pub status: CompactionResultStatus,
    pub message: String,
    pub compaction_id: Option<String>,
    pub proposal_id: Option<String>,
    pub revision: Option<i64>,
    #[schema(default = "null")]
    pub changes: Vec<CompactionChange>,
    #[schema(default = "null")]
    pub warnings: Vec<String>,
    pub timeline: Option<Timeline>,
    pub additions: Option<IndexMap<String, Vec<Map<String, Value>>>>,
    pub judgments: Option<JudgmentsDue>,
}

/// One event as a revision leaves it.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ProposalEvent {
    pub id: String,
    #[schema(required)]
    pub summary: Option<String>,
    #[schema(required)]
    pub start: Option<DateTimeText>,
    #[schema(required)]
    pub end: Option<DateTimeText>,
    pub status: ProposalEventStatus,
    pub planned_start: Option<DateTimeText>,
    pub planned_end: Option<DateTimeText>,
    pub description: Option<String>,
    pub location: Option<String>,
    pub priority: Option<i64>,
    pub action_ids: Option<Vec<String>>,
    pub facts: Option<PublicFacts>,
    pub decided_by: Option<ProposalEventDecidedBy>,
    pub history_until: Option<DateTimeText>,
    #[schema(default = "false")]
    pub is_end_of_day_sleep: bool,
    pub counts_against_follow_through: Option<bool>,
    #[schema(optional)]
    #[serde(default)]
    pub follow_through: Vec<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ProposalNote {
    pub id: String,
    pub timestamp: DateTimeText,
    pub description: Option<String>,
    #[schema(default = "\"unused\"")]
    pub r#use: ProposalNoteUse,
    pub event_id: Option<String>,
    pub edge_of: Option<String>,
    pub decided_by: Option<ProposalEventDecidedBy>,
}

/// A proposal's current revision: the plan for compacting notes into
/// the calendar, for the user to review, edit and confirm.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct Proposal {
    pub id: String,
    pub revision: i64,
    pub state: ProposalSummaryState,
    pub window_start: DateTimeText,
    pub through: DateTimeText,
    pub by: ProposalBy,
    pub reason: String,
    pub created: DateTimeText,
    pub claude_through: Option<DateTimeText>,
    pub events: Option<Vec<ProposalEvent>>,
    pub problem: Option<String>,
    #[schema(optional)]
    #[serde(default)]
    pub notes: Vec<ProposalNote>,
    #[schema(optional)]
    #[serde(default)]
    pub changes: Vec<CompactionChange>,
    #[schema(optional)]
    #[serde(default)]
    pub warnings: Vec<String>,
    pub timeline: Option<Timeline>,
    pub additions: Option<IndexMap<String, Vec<Map<String, Value>>>>,
    #[schema(optional)]
    #[serde(default)]
    pub settled_additions: Vec<AdditionSettled>,
    #[schema(optional)]
    #[serde(default)]
    pub user_edits: Vec<UserEdit>,
    #[schema(optional)]
    #[serde(default)]
    pub feedback: Vec<Feedback>,
    pub recap: Option<Recap>,
    pub recap_revision: Option<i64>,
    pub changed_since: Option<Vec<String>>,
    pub replaced: Option<Vec<String>>,
}

/// The user's say on an action, person or location Claude's decisions
/// add (by its `ref`): `create` it now -- with any of `name`, `context`
/// (a person's) and `hint` (a location's) corrected -- say it's one
/// that's `existing` (its `id`), or `drop` it from the events.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct AdditionChoice {
    pub r#ref: String,
    pub r#use: AdditionSettledUse,
    pub id: Option<String>,
    pub name: Option<String>,
    pub context: Option<String>,
    pub hint: Option<String>,
}

/// The user's say on what a note is for: `annotate` -- add it to the
/// event it falls within, or to `event_id` (an event's id, or a created
/// one's key) -- or `ignore` it.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct NoteEdit {
    pub note_id: String,
    pub r#use: NoteEditUse,
    pub event_id: Option<String>,
}

/// An event to add to a proposal, as create_event takes one, plus
/// `start_note`/`end_note`: a note that sets that edge, at its time.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ProposalCreate {
    pub id: Option<String>,
    pub summary: Option<String>,
    pub start: Option<DateTimeText>,
    pub end: Option<DateTimeText>,
    pub description: Option<String>,
    pub location: Option<String>,
    pub priority: Option<i64>,
    pub action_ids: Option<Vec<String>>,
    pub action_names: Option<Vec<String>>,
    #[schema(default = "false")]
    pub actions_from_label: bool,
    pub event_label_id: Option<String>,
    #[schema(default = "false")]
    pub is_cancelled: bool,
    pub effective_priority: Option<i64>,
    pub is_end_of_day_sleep: Option<bool>,
    pub recurring_event_id: Option<String>,
    pub facts: Option<PublicFacts>,
    pub judgments: Option<Map<String, Value>>,
    pub start_note: Option<String>,
    pub end_note: Option<String>,
}

/// An edit of one of a proposal's events, as update_event takes one --
/// `event` (its id, or a new event's key, and the fields to set) and
/// `clear_fields` -- plus `start_note`/`end_note`: a note that sets that
/// edge, at its time.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ProposalUpdate {
    pub event: PublicEvent,
    pub clear_fields: Option<Vec<ProposalUpdateClearFields>>,
    pub start_note: Option<String>,
    pub end_note: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ProposalResult {
    pub status: ProposalResultStatus,
    pub message: String,
    pub proposal: Option<Proposal>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct HabitJudgmentsDue {
    pub backfill_id: String,
    pub habit_id: String,
    pub habit_name: String,
    pub since: DateTimeText,
    pub until: DateTimeText,
    pub events: Vec<EventJudgmentsDue>,
    pub parts: Vec<JudgmentPart>,
    pub history: IndexMap<String, PersonHistory>,
    #[schema(default = "\"JUDGE EACH ONE YOURSELF, without asking the user: this is a backfill of one of their habits, the judgments of its settled events -- already compacted -- since `since`. `events` lists each, with the parts to judge for the habit (\\\"<trait id>/<part key>\\\"); `parts` gives each part's rubric and ratings once (\\\"...@habit:<id>\\\" for the habit's own), and `history` what the user did and where in its scope over the days before. Judge each event as it is on the calendar -- its times, notes, actions and facts -- for how it went as practice of the habit, its note in what_matters, choosing one of the part's ratings (the number), with one succinct line of reasoning that names the fact it rests on. Record them all with record_judgments(backfill_id, judgments) -- the backfill_id where a compaction's id would go -- each with request_id \\\"<event id>/habit:<id>/<trait id>/<part key>\\\". With `current`, they're being redone: rate them afresh. Don't show the user the judgments unless they ask.\"")]
    pub instructions: String,
}

/// A judgment made: the request's id, a rating from its ratings, and
/// one succinct line of reasoning.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct Judgment {
    pub request_id: String,
    pub rating: i64,
    pub reasoning: String,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct JudgmentsResult {
    pub compaction_id: String,
    pub recorded: i64,
    pub remaining: Vec<String>,
    #[schema(default = "false")]
    pub complete: bool,
    #[schema(default = "\"\"")]
    pub message: String,
}

// -- the compactor ------------------------------------------------------------

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

/// A compact_notes update or create, as the decision it makes.
#[allow(clippy::too_many_arguments)]
fn decision(
    action: &str,
    event_id: Option<String>,
    summary: Option<String>,
    start: Option<&DateTimeText>,
    end: Option<&DateTimeText>,
    start_note: Option<String>,
    end_note: Option<String>,
    annotate: Option<String>,
    action_ids: Option<Vec<String>>,
    facts: Option<&PublicFacts>,
    key: Option<String>,
) -> Result<EventDecision, ToolError> {
    Ok(EventDecision {
        action: action.into(),
        event_id,
        summary,
        start: when_given(start)?,
        end: when_given(end)?,
        start_note,
        end_note,
        annotate,
        action_ids,
        facts: facts.map(Facts::from),
        key,
        ..Default::default()
    })
}

fn update_decision(u: &CompactionUpdate) -> Result<EventDecision, ToolError> {
    decision(
        "keep",
        Some(u.event_id.clone()),
        u.summary.clone(),
        u.start.as_ref(),
        u.end.as_ref(),
        u.start_note.clone(),
        u.end_note.clone(),
        u.annotate.clone(),
        u.action_ids.clone(),
        u.facts.as_ref(),
        None,
    )
}

fn create_decision(c: &CompactionCreate) -> Result<EventDecision, ToolError> {
    decision(
        "create",
        None,
        Some(c.summary.clone()),
        c.start.as_ref(),
        c.end.as_ref(),
        c.start_note.clone(),
        c.end_note.clone(),
        c.annotate.clone(),
        c.action_ids.clone(),
        c.facts.as_ref(),
        c.key.clone(),
    )
}

fn cancel_decision(c: &EventCancel) -> EventDecision {
    EventDecision {
        event_id: Some(c.event_id.clone()),
        counts_against_follow_through: Some(c.counts_against_follow_through),
        ..EventDecision::new("cancel")
    }
}

fn stored_recap(recap: &Recap) -> proposals::Recap {
    let points = |given: &[RecapPoint]| -> Vec<proposals::RecapPoint> {
        given
            .iter()
            .map(|p| proposals::RecapPoint { text: p.text.clone(), event_ids: p.event_ids.clone(), note_ids: p.note_ids.clone() })
            .collect()
    };
    proposals::Recap { overview: recap.overview.clone(), did: points(&recap.did), unsure: points(&recap.unsure) }
}

/// An addition as the compactor keeps it: the store's item, and its ref.
fn addition<T: Serialize, U: DeserializeOwned>(given: &T) -> Addition<U> {
    serde_json::from_value(serde_json::to_value(given).expect("an argument is JSON")).expect("a new item is its store's item")
}

/// What the client sees of a compactor's result (as the tool's type).
fn shown<T: DeserializeOwned>(tool: &str, result: anyhow::Result<Value>) -> Result<T, ToolError> {
    match result {
        Ok(value) => from_store(value),
        Err(e) => Err(rejected(tool, e)),
    }
}

/// An amend_proposal update or create, as the decision it makes -- or
/// what's wrong with it.
fn proposal_decision(
    event: &PublicEvent,
    action: &str,
    start_note: Option<String>,
    end_note: Option<String>,
    clear_fields: &[String],
) -> Result<EventDecision, anyhow::Error> {
    let mut problems: Vec<String> = Vec::new();
    if event.is_cancelled {
        problems.push("to cancel an event, put it in `cancels`".into());
    }
    if event.judgments.is_some() || clear_fields.iter().any(|c| c == "judgments") {
        problems.push("judgments are made once a proposal's applied: leave them out".into());
    }
    if clear_fields.iter().any(|c| c == "priority") {
        problems.push("a proposal can't clear an event's priority: set one".into());
    }
    let id = event.id.clone().filter(|i| !i.is_empty());
    if action == "keep" && id.is_none() {
        problems.push("an update needs the event's id (or a new event's key)".into());
    }
    let set = [("description", event.description.is_some()), ("location", event.location.is_some()), ("facts", event.facts.is_some())];
    let mut both: Vec<&str> = set.iter().filter(|(f, given)| *given && clear_fields.iter().any(|c| c == f)).map(|(f, _)| *f).collect();
    both.sort();
    if !both.is_empty() {
        problems.push(format!("{}: set or cleared, not both", both.join(", ")));
    }
    if !problems.is_empty() {
        let who = crate::compaction::plan::repr_opt(id.as_deref().or(event.summary.as_deref()));
        let issues: Vec<crate::compaction::plan::Issue> =
            problems.iter().map(|p| crate::compaction::plan::Issue::plain(format!("{who}: {p}"))).collect();
        return Err(crate::compaction::plan::error_of(&issues, "malformed_decision").into());
    }
    let cleared = |name: &str| clear_fields.iter().any(|c| c == name);
    Ok(EventDecision {
        action: action.into(),
        event_id: if action == "keep" { event.id.clone() } else { None },
        summary: event.summary.clone(),
        start: when_given(event.start.as_ref())?,
        end: when_given(event.end.as_ref())?,
        start_note,
        end_note,
        action_ids: if event.actions_from_label { None } else { event.action_ids.clone() },
        facts: if cleared("facts") { Some(Facts::default()) } else { event.facts.as_ref().map(Facts::from) },
        description: if cleared("description") { Some(String::new()) } else { event.description.clone() },
        location: if cleared("location") { Some(String::new()) } else { event.location.clone() },
        priority: event.priority,
        ..Default::default()
    })
}

/// An amend_proposal create, as the event it describes.
fn proposed_event(c: &ProposalCreate) -> PublicEvent {
    PublicEvent {
        id: c.id.clone(),
        summary: c.summary.clone(),
        start: c.start.clone(),
        end: c.end.clone(),
        description: c.description.clone(),
        location: c.location.clone(),
        priority: c.priority,
        action_ids: c.action_ids.clone(),
        action_names: c.action_names.clone(),
        actions_from_label: c.actions_from_label,
        event_label_id: c.event_label_id.clone(),
        is_cancelled: c.is_cancelled,
        effective_priority: c.effective_priority,
        is_end_of_day_sleep: c.is_end_of_day_sleep,
        recurring_event_id: c.recurring_event_id.clone(),
        facts: c.facts.clone(),
        judgments: c.judgments.clone(),
        ..Default::default()
    }
}

// -- the tools ------------------------------------------------------------------

/// When notes were last compacted into the calendar, the latest note
/// compacted, whether that compaction still has judgments to make, and
/// the open proposal, if any. Read-only.
#[tool]
pub fn get_compaction_status(app: &App) -> Result<CompactionStatus, ToolError> {
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
    from_store(Value::Object(out))
}

/// Correct an uncompacted note (by its id, from get_notes or
/// prepare_compaction): a new timestamp and/or description. Whichever is
/// left out keeps its current value; an empty description clears it.
/// Returns the edited note -- if the timestamp changed, so did its id.
/// Compacted notes can't be changed (edit the calendar event they became
/// instead). A proposal that included this note is planned again when
/// it's confirmed.
#[tool(writes)]
pub fn edit_note(
    app: &App,
    note_id: String,
    timestamp: Option<DateTimeText>,
    description: Option<String>,
    version: Option<i64>,
) -> Result<NoteWithId, ToolError> {
    let compactor = compactor(app)?;
    let timestamp = when_given(timestamp.as_ref())?;
    shown("edit_note", compactor.edit_note(&note_id, timestamp, description.as_deref(), version).map(|n| n.with_id()))
}

/// Delete an uncompacted note (by its id, from get_notes or
/// prepare_compaction), returning what it was. Other notes' ids are
/// unaffected. Compacted notes can't be deleted. A proposal that included
/// this note is planned again when it's confirmed.
#[tool(writes)]
pub fn delete_note(app: &App, note_id: String, if_version: Option<i64>) -> Result<NotedTime, ToolError> {
    let compactor = compactor(app)?;
    shown(
        "delete_note",
        compactor.delete_note(&note_id, if_version).map(|n| {
            let mut shown = n.note.to_json();
            shown["id"] = json!(n.own_id);
            shown
        }),
    )
}

/// Step 1 of compacting notes into the calendar: proposing what
/// happened since the last compaction, for the user to confirm. Returns
/// every day from the last compaction up to now (at most a week), notes
/// or not -- each uncompacted note with an id and the planned events
/// nearest it (`remaining_note_count` says how many are left for another
/// round) -- those days' planned events, a `timeline` showing the two
/// side by side, day by day, and instructions. Compare the notes to the
/// plan, decide which events the notes show happened differently, and
/// the facts of each past event -- its actions, where it was, who was
/// there, who it was for, and a note on each person there -- then call
/// compact_notes with those decisions. Also every action an event can be
/// given (`actions`, their groups left out), every person (`people`, the
/// user as "self") and every location (`locations`), to settle the facts
/// from. `proposal` is the open proposal, if there is one: revise or
/// extend it (see the instructions). judgments_pending names the last
/// compaction if its judgments aren't all made: make them first
/// (prepare_judgments, record_judgments). Read-only.
#[tool(writes)]
pub fn prepare_compaction(app: &App) -> Result<CompactionContext, ToolError> {
    from_store(compactor(app)?.prepare()?.to_json())
}

/// Step 2 of compacting notes: propose what happened -- each day's
/// events realigned to its notes, and the calendar changes that makes --
/// for the user to review and confirm in the app. Nothing on the calendar
/// changes, and you can't apply it: only the user's confirmation does.
/// Every day since the last compaction is planned together, as one span;
/// one call covers them all, to now.
///
/// Call with what the notes show happened differently from the plan, as
/// update_event takes a batch (see prepare_compaction's instructions):
/// `updates` (events that happened, with edges moved to a time or a
/// note's, renamed, annotated, their actions and facts set), `creates`
/// (something unplanned that happened) and `cancels` (what didn't happen,
/// each saying whether it counts against follow-through: the user
/// dropped it, or the plan changed). Any past event you don't mention is
/// recorded as on schedule. Notes that don't set an event edge are added
/// to the event they fall within, except those in `ignore_notes` -- and
/// those in `annotate_notes`, added to the event they name (an event's
/// id, a create's key from the open proposal, or its `ref`) instead.
/// new_actions, new_people and new_locations add what isn't there yet,
/// each with a `ref` ("new:ukulele") the updates and creates use in
/// place of its id; they're created when it's applied. You get the
/// proposal's id and revision, the proposed changes, the additions, and
/// a `timeline` of the notes beside the resulting events, each with its
/// actions and facts -- in a conversation, show its `text` to the user in
/// a code block.
///
/// `recap` is your account of it, for the user reviewing it in the app:
/// a short `overview`, then one point for each thing you `did` (what you
/// decided, and from which notes) and each thing you're `unsure` of
/// (saying what you assumed -- empty only if nothing is in doubt). Each
/// point is Markdown (bold, italics, code, lists; no headings) and cites
/// the events (`event_ids`: ids, keys, or a create's `ref`) and the notes
/// (`note_ids`) it's about, so the user can go straight to them. Give a
/// create a `ref` of your choosing to name it before it has a key, here
/// and in `annotate_notes`. Write it afresh every call, for the proposal
/// as it now stands.
///
/// With a proposal open (prepare_compaction's `proposal`), every call
/// revises or extends it: pass its `proposal_id` and the `revision` you
/// started from, send all your decisions again (each create with the
/// `key` it was given), and answer every open feedback item in
/// `replies`. The user's own edits -- of events, and of what notes are
/// for -- are laid over your decisions by the server; never send them.
///
/// NOTHING IS MOVED TO MAKE ROOM: every event you update or create must
/// end after it starts and must not overlap any other event -- past or
/// still to come, the others you change included -- so move, shorten or
/// cancel whatever's in the way in the same call. A call that breaks this
/// changes nothing, and the error lists every problem and the day as it
/// would leave it, to fix them all at once.
///
/// An update that moves a future event reschedules it. Moving the
/// end-of-day sleep event moves where the day ends: an earlier bedtime
/// needs whatever runs past it shortened or cancelled too, a later one
/// leaves the evening free. Its end is the wake-up time: a later one has
/// to move, shorten or cancel the morning events it now runs over, even
/// those after now, in the same call. Otherwise compaction never adjusts
/// what's past the last night -- so move only its start to change only
/// the bedtime, and if the wake-up time does change, the plan warns; tell
/// the user.
///
/// Once the user confirms it and it's applied, make its judgments with
/// prepare_judgments and record_judgments: the compaction isn't complete
/// until they're all recorded.
#[tool(writes)]
pub fn compact_notes(app: &App, recap: Recap, updates: Option<Vec<CompactionUpdate>>, creates: Option<Vec<CompactionCreate>>, cancels: Option<Vec<EventCancel>>, ignore_notes: Option<Vec<String>>, proposal_id: Option<String>, revision: Option<i64>, replies: Option<Vec<FeedbackReply>>, new_actions: Option<Vec<NewAction>>, new_people: Option<Vec<NewPerson>>, new_locations: Option<Vec<NewLocation>>, annotate_notes: Option<Vec<NoteAnnotation>>) -> Result<CompactionResult, ToolError> {
    let compactor = compactor(app)?;
    let (updates, creates, cancels) = (updates.unwrap_or_default(), creates.unwrap_or_default(), cancels.unwrap_or_default());
    let mut decisions: Vec<EventDecision> = Vec::new();
    for u in &updates {
        decisions.push(update_decision(u)?);
    }
    for c in &creates {
        decisions.push(create_decision(c)?);
    }
    decisions.extend(cancels.iter().map(cancel_decision));
    let additions = Additions {
        actions: new_actions.unwrap_or_default().iter().map(addition).collect(),
        people: new_people.unwrap_or_default().iter().map(addition).collect(),
        locations: new_locations.unwrap_or_default().iter().map(addition).collect(),
    };
    let replies: Vec<proposals::FeedbackReply> =
        replies.unwrap_or_default().into_iter().map(|r| proposals::FeedbackReply { feedback_id: r.feedback_id, reply: r.reply }).collect();
    let annotate: Vec<(String, String)> = annotate_notes.unwrap_or_default().into_iter().map(|a| (a.note_id, a.event_id)).collect();
    let create_refs: Vec<Option<String>> = creates.iter().map(|c| c.r#ref.clone()).collect();
    let result = compactor.dry_run(decisions, ignore_notes, additions, proposal_id, revision, replies, annotate, Some(stored_recap(&recap)), create_refs);
    shown("compact_notes", result.map(|r| r.to_json()))
}

/// The open proposal (or the one named) -- its current revision,
/// planned again on the calendar as it is now: its window (`window_start`
/// to `through`), `state`, every event of its days as it leaves them
/// (`events`, each with its id -- or a new event's key -- its times, what
/// it was planned as, and who decided it), the notes, the calendar writes
/// it makes, its `timeline`, the user's edits and the feedback with any
/// replies. With since_revision, `changed_since` lists the events whose
/// outcome changed after it. Read-only.
#[tool]
pub fn get_proposal(app: &App, proposal_id: Option<String>, since_revision: Option<i64>) -> Result<Proposal, ToolError> {
    let compactor = compactor(app)?;
    shown("get_proposal", compactor.get_proposal(proposal_id.as_deref(), since_revision).map(|p| p.to_json()))
}

/// The user's edits to the open proposal, from the app: `updates`,
/// `creates` and `cancels` as update_event takes them (an update's id may
/// be a new event's key), each update and create also taking a
/// `start_note`/`end_note` that sets that edge. A description given is
/// the event's whole description, notes and all -- as the proposal
/// shows it, edited: no notes are added to it. `notes`, what notes are for --
/// each annotating the event it falls within, or the one named
/// (`event_id`), or ignored; `additions`, settling an action, person or
/// location the proposal adds (by its ref) now, without confirming the
/// rest -- `create` it (with its name, a person's context or a
/// location's hint corrected), say it's one that's `existing` (its id),
/// or `drop` it from the events; and `as_planned` -- events (or keys)
/// whose decisions to clear, notes to leave as Claude had them, and refs
/// to leave unsettled. One created stays, whatever becomes of the
/// proposal. `through`, later than the proposal's, extends it there (no
/// later than now; it can't be cut short): each note it takes in is added
/// to the event it falls within, unless `notes` says otherwise, and the
/// proposal's `claude_through` still says where Claude's revision ran
/// to.
/// `revision` is the one the user was looking at. They're laid over the current revision as a new one,
/// which is returned, with `replaced`: the events whose newer change by
/// Claude they overrode. Refused, changing nothing, if one names an
/// event or a note that isn't in the proposal, adds a note to an event
/// of another day, or the result overlaps, or changes history -- moves
/// the start of, ends earlier, or cancels an event an earlier compaction
/// recorded -- unless allow_compacted_changes is set: ONLY set it when the
/// user has explicitly approved changing history. It's kept with these
/// edits, so later revisions keep to it. Nothing is moved to make room.
#[tool(writes)]
pub fn amend_proposal(app: &App, proposal_id: String, revision: i64, updates: Option<Vec<ProposalUpdate>>, creates: Option<Vec<ProposalCreate>>, cancels: Option<Vec<EventCancel>>, as_planned: Option<Vec<String>>, notes: Option<Vec<NoteEdit>>, additions: Option<Vec<AdditionChoice>>, through: Option<DateTimeText>, #[schema(default = "false")] allow_compacted_changes: bool) -> Result<Proposal, ToolError> {
    let compactor = compactor(app)?;
    let allow = allow_compacted_changes;
    let run = || -> anyhow::Result<Value> {
        let mut decisions: Vec<EventDecision> = Vec::new();
        for u in updates.unwrap_or_default() {
            let clear = texts(u.clear_fields.as_deref().unwrap_or_default());
            decisions.push(proposal_decision(&u.event, "keep", u.start_note, u.end_note, &clear)?);
        }
        for c in creates.unwrap_or_default() {
            decisions.push(proposal_decision(&proposed_event(&c), "create", c.start_note, c.end_note, &[])?);
        }
        decisions.extend(cancels.unwrap_or_default().iter().map(cancel_decision));
        if allow {
            for d in decisions.iter_mut() {
                d.allow_history = Some(true);
            }
        }
        let notes: Vec<proposals::NoteEdit> = notes
            .unwrap_or_default()
            .into_iter()
            .map(|n| proposals::NoteEdit { note_id: n.note_id, use_: wire_text(&n.r#use), event_id: n.event_id })
            .collect();
        let additions: Vec<proposals::AdditionChoice> = additions
            .unwrap_or_default()
            .into_iter()
            .map(|a| proposals::AdditionChoice { ref_: a.r#ref, use_: wire_text(&a.r#use), id: a.id, name: a.name, context: a.context, hint: a.hint })
            .collect();
        let through = when_given(through.as_ref())?;
        let proposal = compactor.amend(&proposal_id, revision, decisions, as_planned.unwrap_or_default(), notes, additions, through)?;
        Ok(proposal.to_json())
    };
    match run() {
        Ok(value) => from_store(value),
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

/// A note from the user for Claude on the open proposal, about an
/// event (or a new event's key), a time or a time note, if it's about
/// one. Claude answers it with a revised proposal -- and may then change
/// what the user edited of that event or note; until then, the proposal
/// can't be confirmed. Returns it, with its id.
#[tool(writes)]
pub fn add_proposal_note(app: &App, proposal_id: String, text: String, event_id: Option<String>, at: Option<DateTimeText>, note_id: Option<String>) -> Result<Feedback, ToolError> {
    let compactor = compactor(app)?;
    let at = when_given(at.as_ref())?;
    shown("add_proposal_note", compactor.add_note(&proposal_id, &text, event_id, at, note_id).map(|item| item.to_json()))
}

/// Take back an open note for Claude (by its id). One already
/// answered can't be.
#[tool(writes)]
pub fn withdraw_proposal_note(app: &App, feedback_id: String) -> Result<Feedback, ToolError> {
    let compactor = compactor(app)?;
    shown("withdraw_proposal_note", compactor.withdraw_note(&feedback_id).map(|item| item.to_json()))
}

/// For the app: the user confirms the open proposal's current
/// revision -- the one they reviewed -- as what happened, and it's
/// applied. Not for Claude: only the user confirms. Refused while
/// feedback is waiting for Claude, or if `revision` isn't the current
/// one. If the notes or calendar changed since -- through the
/// proposal's last night, past `through` -- so it would write something
/// else, it's planned again as a new revision to confirm instead
/// (`rechecked`); if it no longer plans at all, it's handed to Claude
/// (`needs_claude`). A change that can never be made while applying
/// proposes what's left again (`rebuilt`); any other failure leaves it
/// to finish_proposal.
#[tool(writes)]
pub fn confirm_proposal(app: &App, proposal_id: String, revision: i64) -> Result<ProposalResult, ToolError> {
    let compactor = compactor(app)?;
    shown("confirm_proposal", compactor.confirm(&proposal_id, revision).map(|r| r.to_json()))
}

/// Finish applying a confirmed proposal that stopped partway: what was
/// done stays done, and the rest is applied as confirmed, without planning
/// it again: over whatever the calendar says now. It was confirmed, so
/// this needs no new approval, nor a revision: only the confirmed one is
/// ever being applied. If a change can never be made (its event is
/// gone), what's left is proposed again as a new revision for the user
/// to confirm (`rebuilt`).
#[tool(writes)]
pub fn finish_proposal(app: &App, proposal_id: String) -> Result<ProposalResult, ToolError> {
    let compactor = compactor(app)?;
    shown("finish_proposal", compactor.finish(&proposal_id).map(|r| r.to_json()))
}

/// The judgments a compaction (by default the last one applied) calls
/// for, to make with record_judgments: each event with facts, with each
/// person it was about (the user, "self", and everyone there, for a
/// trait's "with" parts; everyone it was done for, for its "for" parts)
/// and the judgment parts of the traits that apply to them; then each of
/// those parts once -- its rubric and the ratings to choose from -- and
/// each person's history (what they did and where) over the days their
/// parts look back. Only those not made yet -- or with redo, all of
/// them, each event with the judgments already made (`current`), to
/// redo one with more context. Read-only.
#[tool]
pub fn prepare_judgments(app: &App, compaction_id: Option<String>, #[schema(default = "false")] redo: bool) -> Result<JudgmentsDue, ToolError> {
    let compactor = compactor(app)?;
    compactor.prefetch()?;
    let due = match compactor.judgments_due(compaction_id.as_deref(), redo) {
        Ok(due) => due,
        Err(e) => return Err(rejected("prepare_judgments", e)),
    };
    match due {
        Some(due) => from_store(due.to_json()),
        None => Err(ToolError::Message("there's no applied compaction to judge".into())),
    }
}

/// A backfill of one habit's judgments (by id or name): for a habit
/// just made, or given a new rubric, whose settled events -- already
/// compacted -- aren't judged for it yet. Only when the user asks for
/// one: it's never part of a compaction. Gives a backfill_id, then, as
/// prepare_judgments does, each of its events in scope since `since`
/// (by default as far back as its judgment parts average over, and a
/// week of scores) until where history ends, with the parts to judge
/// for it; each part once; and its history. Only those not judged yet --
/// or with redo, all of them, each with the judgment already made
/// (`current`), to judge again. Make them yourself and record them with
/// record_judgments(backfill_id, judgments). Read-only.
#[tool]
pub fn prepare_habit_judgments(app: &App, habit_id: String, since: Option<DateTimeText>, #[schema(default = "false")] redo: bool) -> Result<HabitJudgmentsDue, ToolError> {
    let compactor = compactor(app)?;
    compactor.prefetch()?;
    let since = when_given(since.as_ref())?;
    shown("prepare_habit_judgments", compactor.habit_judgments_due(&habit_id, since, redo).map(|due| due.to_json()))
}

/// Record judgments of a compaction's events (see prepare_judgments, and
/// the `judgments` an applied compaction returns) -- or, given a habit
/// backfill's backfill_id as compaction_id, of that habit's events (see
/// prepare_habit_judgments): each a request_id, a rating from that
/// request's ratings, and one succinct line of reasoning. Make them
/// yourself, never asking the user. They're kept on the events, by
/// person (or habit), trait and part; a judgment recorded again replaces
/// the earlier one. Returns how many were recorded, the ids of any
/// requests still to judge, and whether that completes the compaction
/// (or the backfill).
#[tool(writes)]
pub fn record_judgments(app: &App, compaction_id: String, judgments: Vec<Judgment>) -> Result<JudgmentsResult, ToolError> {
    let compactor = compactor(app)?;
    compactor.prefetch()?;
    let judgments: Vec<judgments::Judgment> =
        judgments.into_iter().map(|j| judgments::Judgment { request_id: j.request_id, rating: j.rating, reasoning: j.reasoning }).collect();
    shown("record_judgments", compactor.record_judgments(&compaction_id, &judgments).map(|r| r.to_json()))
}

/// Give up on a proposal (or a compaction from before proposals, by
/// its id). Changes it already applied stay applied; its notes stay
/// uncompacted (except those of days it had already finished), so a new
/// one can be proposed.
#[tool(writes)]
pub fn abandon_compaction(app: &App, proposal_id: String) -> Result<CompactionResult, ToolError> {
    let compactor = compactor(app)?;
    shown("abandon_compaction", compactor.abandon(&proposal_id).map(|r| r.to_json()))
}
