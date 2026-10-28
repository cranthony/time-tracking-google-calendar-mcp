//! The goal tools: get_goals, get_goal, create_goal and update_goals.

use super::{refused, texts, ToolError};
use crate::app::App;
use crate::contract::{tool, Schema};
pub use crate::stores::goals::{ActivateWhen, ChangedGoals, CreatedGoal, Deadline, Goal, GoalStatus, ListedGoal, StatusChange};
use crate::stores::goals::{GoalEdit, GoalRemoval};
use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum GoalField {
    #[serde(rename = "parent_id")]
    ParentId,
    #[serde(rename = "required")]
    Required,
    #[serde(rename = "priority")]
    Priority,
    #[serde(rename = "sort_order")]
    SortOrder,
    #[serde(rename = "expected_minutes")]
    ExpectedMinutes,
    #[serde(rename = "deadline")]
    Deadline,
    #[serde(rename = "activate_when")]
    ActivateWhen,
    #[serde(rename = "description")]
    Description,
    #[serde(rename = "definition_of_done")]
    DefinitionOfDone,
    #[serde(rename = "attribution_hints")]
    AttributionHints,
    #[serde(rename = "with_ids")]
    WithIds,
    #[serde(rename = "for_ids")]
    ForIds,
}

/// A goal as create_goal and update_goals take it (see get_goals).
#[derive(Clone, Debug, Default, Serialize, Deserialize, Schema)]
pub struct GoalInput {
    pub id: Option<String>,
    pub title: Option<String>,
    /// The goal it's a piece of; none for a top-level goal.
    pub parent_id: Option<String>,
    /// Whether its parent needs it done to be done.
    pub required: Option<bool>,
    /// How much it matters, 0 the most, as actions' priorities; none, it
    /// takes its nearest parent's that has one, and with none up its tree,
    /// it's treated as 3.
    pub priority: Option<i64>,
    /// Where it's listed among goals that otherwise list alike (the same
    /// priority, due the same day): the lowest first, those without one
    /// after them. The user's own order, set by dragging goals in the app.
    pub sort_order: Option<i64>,
    /// How long it's expected to take, in minutes of the user's time: a
    /// guide to compare the time spent with, never enforced.
    pub expected_minutes: Option<i64>,
    /// When it's due: a date given, or measured from something (see
    /// Deadline). Every open goal needs one, and an active one a due.
    pub deadline: Option<Deadline>,
    /// What it waits for before it's activated, while it's inactive (see
    /// ActivateWhen).
    pub activate_when: Option<ActivateWhen>,
    pub status: Option<GoalStatus>,
    /// Every change of its status, oldest first, the last its status now,
    /// each with the deadline in force at that change.
    pub history: Option<Vec<StatusChange>>,
    /// What it is, as HTML.
    pub description: Option<String>,
    /// What being done looks like, as HTML.
    pub definition_of_done: Option<String>,
    /// A guide for telling which events are for it.
    pub attribution_hints: Option<String>,
    /// The people it's done with: who the user works on it alongside
    /// (person ids; never "self", and never also in for_ids). A circle's
    /// id names a plain circle's members, or a community itself;
    /// {circle_id, except} a plain circle's but those in except.
    pub with_ids: Option<Vec<crate::attendees::Named>>,
    /// The people it's done for, who aren't part of doing it: a promise
    /// to them, a gift, a favor (person ids; never "self", and never also
    /// in with_ids, a plain circle's members included). Circles as in
    /// with_ids.
    pub for_ids: Option<Vec<crate::attendees::Named>>,
    /// The version you last read (see get_goals), to change one.
    pub version: Option<i64>,
}

impl From<GoalInput> for Goal {
    /// As the tab keeps it: a date given as its deadline, or else what
    /// it's measured from as its deadline_rule (its deadline worked out
    /// from that).
    fn from(input: GoalInput) -> Goal {
        let (deadline, deadline_rule) = match input.deadline {
            Some(Deadline { at: Some(at), before_parent: None, after_start: None, after_goal: None }) => (Some(at), None),
            Some(rule) => (None, Some(rule)),
            None => (None, None),
        };
        Goal {
            id: input.id,
            title: input.title,
            parent_id: input.parent_id,
            required: input.required,
            priority: input.priority,
            sort_order: input.sort_order,
            expected_minutes: input.expected_minutes,
            deadline,
            deadline_offset_minutes: None,
            status: input.status,
            history: input.history,
            description: input.description,
            definition_of_done: input.definition_of_done,
            attribution_hints: input.attribution_hints,
            with_ids: input.with_ids,
            for_ids: input.for_ids,
            deadline_rule,
            activate_when: input.activate_when,
            habit_id: None,
            version: input.version,
        }
    }
}

/// The fields `clear` names, as the tab keeps them: clearing the deadline
/// clears what it's measured from too.
fn cleared(clear: &[GoalField]) -> Vec<String> {
    let mut fields = texts(clear);
    if clear.contains(&GoalField::Deadline) {
        fields.push("deadline_rule".into());
    }
    fields
}

/// One goal's update: `goal` (its id and the fields to set) and the
/// fields to remove (`clear_fields`).
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct GoalUpdate {
    pub goal: GoalInput,
    pub clear_fields: Option<Vec<GoalField>>,
    /// A note for the status change this update records -- a status set
    /// without a history -- kept on that history entry: why the goal was
    /// marked done, say. With a history given, put the note on its entry
    /// instead.
    pub status_note: Option<String>,
}

/// A goal to delete. Give if_version -- the version you last read: without it, it's refused; changed since, it's refused -- on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct GoalDelete {
    pub goal_id: String,
    pub if_version: Option<i64>,
}

// -- the tools ------------------------------------------------------------------

/// The user's goals: outcomes they mean to reach and can mark done (not
/// ongoing practices -- those are habits). By default the inactive and
/// active ones, not done, missed or obsolete ones; every depth, each with its
/// parent_id and path (its parents' titles, outermost first). A goal
/// can sit under another to any depth: a sub-goal is a piece of reaching
/// its parent, and required says whether its parent needs it done.
/// A priority (0 the most, as actions'; optional) says how much it
/// matters: without one, a goal takes its nearest parent's that has one,
/// and with none up its tree it's treated as 3. sort_order (optional)
/// is the user's own order among goals that otherwise list alike (the
/// same priority, due the same day): the lowest first, those without one
/// after them; leave it as it is unless asked to reorder goals.
/// Each has a title, an optional expected_minutes (how long it's
/// expected to take: a guide for comparing with the time spent, never
/// enforced), a deadline -- {at: "2026-11-01"} (due at the end of that
/// day) or {at: "2026-11-01T17:00"}, in the calendar's time zone; or
/// measured from something, and kept so while it's open: {before_parent:
/// {days | minutes}} (that far before its parent's; moving it to the top
/// level unties it), {after_start: {days | minutes}} (after it becomes
/// active: days, the end of that many days after the day it started) or
/// {after_goal: {id, days | minutes}} (after another goal's) -- and due,
/// when that falls ("2026-11-01" or "2026-11-01T17:00"; none until one
/// measured from its start starts). Every open goal needs a deadline,
/// and an active one a due. An inactive goal can wait to be activated
/// (activate_when): {after: [{goal_closed: id} (done, missed or
/// obsolete) | {at: a date or a date and time}] -- every one -- then
/// delay: "next_day" (the start of the day after) or {minutes}, and
/// hold_while: [{active_at_least: n, priority?: {at_or_above | exactly:
/// a priority or "own"}}] (held back while that many other goals of that
/// priority are active, its own parents and sub-goals not counted)}.
/// Ready and not held, it's activated -- dated when it became ready --
/// with its inactive parents; activating it by hand ignores it, and
/// either way it's cleared -- as it is when the goal is closed (done,
/// missed or obsolete) while it waits. expected_start is when one waiting is
/// expected to be: when what it waits for is due, then its delay; none
/// while it's held, or what it waits for has no due yet. A status (inactive, active, done, missed -- its
/// deadline passed without it -- or obsolete) and its history (each change of
/// status and when, oldest first, each with the deadline in force at
/// that change, if it had one, and a note, if one was left with it --
/// why it was marked done, say; started_at is when it first became
/// active, closed_at when it was done, missed or made obsolete). A missed
/// goal can be reopened with a new deadline, and its history keeps each
/// deadline it missed, so a goal can be missed and retried any number
/// of times. Then a description and definition_of_done (HTML), and
/// attribution_hints (a guide for telling which events are for it).
/// with_ids are the people it's done with (who the user works on it
/// alongside) and for_ids those it's done for, who aren't part of doing
/// it (a promise to them, a gift, a favor): person ids from get_people,
/// never "self", and no one in both -- or circles from get_circles: a
/// plain circle's id for all its members, {circle_id, except: [person
/// ids]} for all but those, and a community's for the community itself.
/// A plain circle's member can't be in one while their circle is in the
/// other.
/// Events are tagged with goals, apart from their actions (see
/// list_events' goals): tag an event with a goal only when the goal was
/// open (inactive or active) when the event started -- open now, or
/// closed_at after it started -- and the event clearly matches its
/// attribution_hints, or with none its title -- never guess -- and
/// with the most specific goal that fits (a sub-goal rather than its
/// parent). Read-only.
#[tool]
pub fn get_goals(app: &App, statuses: Option<Vec<GoalStatus>>) -> Result<Vec<ListedGoal>, ToolError> {
    let statuses = statuses.map(|s| texts(&s));
    let zone = app.calendar_client()?.get_time_zone()?;
    app.goals()?.get_goals(statuses.as_deref(), &zone).map_err(refused)
}

/// One goal, by id or else by title (ignoring case), as get_goals lists
/// it, whatever its status. If there's none, the error suggests close
/// matches. Read-only.
#[tool]
pub fn get_goal(app: &App, id_or_title: String) -> Result<ListedGoal, ToolError> {
    let zone = app.calendar_client()?.get_time_zone()?;
    app.goals()?.get_goal(&id_or_title, &zone).map_err(refused)
}

/// Add a goal (see get_goals): a title, a deadline, and optionally
/// parent_id (the goal it's a piece of), required, priority,
/// expected_minutes, activate_when, description, definition_of_done,
/// attribution_hints, with_ids and for_ids. Active unless given a
/// status; activating a goal activates its inactive parents. Its history
/// starts with its status, now, unless given one (to backdate it, say).
/// A sub-goal can't be due after its parent, or be expected to take
/// longer, or be open (inactive or active) under a done, missed or
/// obsolete one. A goal can't wait, through others, for itself, or for a
/// parent to close, and a deadline can't be measured, through others,
/// from its own. id is assigned
/// unless given (a client's own, so it can name the goal before it's
/// made; the same create sent again gives the goal as it was made).
/// Returns the goal and its id as created_id.
#[tool(writes)]
pub fn create_goal(app: &App, goal: GoalInput) -> Result<CreatedGoal, ToolError> {
    let zone = app.calendar_client()?.get_time_zone()?;
    app.goals()?.create_goal(&goal.into(), &zone).map_err(refused)
}

/// Change goals, all at once, checked whole: `updates` (each a goal's id
/// and the fields to set -- left out, a field keeps its value; list it in
/// that update's clear_fields to remove it instead), `creates` (new goals,
/// as create_goal takes them; an update can name one by an id given
/// here) and `deletes`. A status set without a history adds the change,
/// now, with the goal's deadline after the change, and the update's
/// status_note, to the goal's history; a history given replaces it whole
/// (to backdate a change, or remove a
/// mistaken one), its entries' deadlines as given, and must end with the
/// goal's status. To retry a missed goal, set its new deadline and give
/// its history ending missed (with the deadline it missed) then active or
/// inactive (with the new one), both at the same moment -- how the app
/// records a retry.
///
/// The rules (see get_goals and create_goal) hold for the result, not
/// for each step: to mark a goal done, missed or obsolete, close or move its
/// open sub-goals in the same call; to delete one, delete or move its
/// sub-goals too; to make an earlier deadline fit, change its sub-goals'
/// in the same call. Nothing is moved or changed to make things fit: a
/// call that breaks a rule changes nothing, and the error names the goals
/// in the way. Deleting a goal can't be undone; events tagged with it
/// stop showing it. Give each update its version, and each delete its if_version -- the versions you last read (get_goals gives them): without, it's refused; changed since, the call is refused with every goal that's changed, as it is now (_meta.conflicts) -- on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK. Returns each goal made or
/// changed, as it is now, and the ids deleted.
#[tool(writes)]
pub fn update_goals(app: &App, updates: Option<Vec<GoalUpdate>>, creates: Option<Vec<GoalInput>>, deletes: Option<Vec<GoalDelete>>) -> Result<ChangedGoals, ToolError> {
    let zone = app.calendar_client()?.get_time_zone()?;
    let edits: Vec<GoalEdit> = updates
        .unwrap_or_default()
        .into_iter()
        .map(|u| GoalEdit { goal: u.goal.into(), clear_fields: cleared(&u.clear_fields.unwrap_or_default()), status_note: u.status_note })
        .collect();
    let creates: Vec<Goal> = creates.unwrap_or_default().into_iter().map(Goal::from).collect();
    let removals: Vec<GoalRemoval> =
        deletes.unwrap_or_default().into_iter().map(|d| GoalRemoval { goal_id: d.goal_id, if_version: d.if_version }).collect();
    app.goals()?.change_goals(&creates, &edits, &removals, &zone).map_err(refused)
}

/// Update one goal by id: the fields given are set, those left out kept,
/// and those in clear_fields removed, and status_note kept on the status
/// change it records -- update_goals for one goal (see there for status
/// and history). Give version -- the version you last read: without it, it's refused; changed since, it's refused -- on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK. Returns the goal as updated.
#[tool(writes)]
pub fn update_goal(app: &App, goal: GoalInput, clear_fields: Option<Vec<GoalField>>, status_note: Option<String>) -> Result<ListedGoal, ToolError> {
    let id = goal.id.clone().unwrap_or_default();
    let changed = update_goals(app, Some(vec![GoalUpdate { goal, clear_fields, status_note }]), None, None)?;
    changed.goals.into_iter().find(|g| g.id.as_deref() == Some(id.trim())).ok_or_else(|| ToolError::Message(format!("goal {id} wasn't updated")))
}

/// Delete one goal by id -- update_goals for one goal: refused while it
/// has sub-goals (delete or move them first). Can't be undone; events
/// tagged with it stop showing it. Given if_version -- the version last
/// seen -- it's refused if it's changed since; gone already, it's done.
/// Returns the ids deleted.
#[tool(writes)]
pub fn delete_goal(app: &App, goal_id: String, if_version: Option<i64>) -> Result<ChangedGoals, ToolError> {
    update_goals(app, None, None, Some(vec![GoalDelete { goal_id, if_version }]))
}
