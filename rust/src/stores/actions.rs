//! Actions: what the user does with their time, each holding one of the
//! calendar's event labels while it's in play, kept with the action groups
//! in the Actions and Action Groups tabs.

use crate::google::calendar::{CalendarClient, EventLabel};
use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::contract::{wire_text, RowItem, Schema};
use crate::sheet::rows::{self, RowSheet, RowType};
use crate::sheet::{ACTIONS, ACTION_GROUPS};
use crate::stores::groups::{
    color_problem, find_group, group_problems, palette, priority_label_id, repr_or_none, set, str_or_none, with_priority_labels,
    GroupTree, Palette, Placed, GROUP_CLEARABLE_FIELDS, GROUP_ROW, PRIORITIES,
};
use crate::stores::locations::dedup;
use crate::stores::shares::{is_false, Share};
use anyhow::bail;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use serde::{Deserialize, Serialize};

// -- the types -------------------------------------------------------------------

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum ActionStatus {
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
pub enum ClearableField {
    #[serde(rename = "group_id")]
    GroupId,
    #[serde(rename = "background_color")]
    BackgroundColor,
    #[serde(rename = "priority")]
    Priority,
    #[serde(rename = "note")]
    Note,
}

/// An action as the action tools return it: plus where it sits, what
/// it inherits from its groups, and whether it holds its label.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct ListedAction {
    pub id: Option<String>,
    pub group_id: Option<String>,
    pub name: Option<String>,
    pub status: Option<ActionStatus>,
    pub label_id: Option<String>,
    pub background_color: Option<String>,
    pub priority: Option<i64>,
    pub note: Option<String>,
    pub commute: Option<bool>,
    pub path: Option<String>,
    pub effective_color: Option<String>,
    pub effective_priority: Option<i64>,
    pub holds_label: Option<bool>,
    pub version: Option<i64>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct ActionList {
    pub actions: Vec<ListedAction>,
    pub label_slots_used: i64,
    #[schema(default = "200")]
    pub label_slots_total: i64,
}

/// One action: what the user does with their time, a verb phrase (see
/// get_actions).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema, RowItem)]
pub struct Action {
    pub id: Option<String>,
    pub group_id: Option<String>,
    pub name: Option<String>,
    #[row(text)]
    pub status: Option<ActionStatus>,
    pub label_id: Option<String>,
    pub background_color: Option<String>,
    pub priority: Option<i64>,
    pub note: Option<String>,
    /// Whether it's a way of getting from one place to another ("drive",
    /// "walk"), which a travel time can be by.
    pub commute: Option<bool>,
    /// Goes up by one each time it changes. Give it to an update (and to a delete, as if_version): it's the version you last read, required -- one saved before versions is version 0 -- changed since, the change is refused; on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK.
    pub version: Option<i64>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct CreatedAction {
    pub changed: Vec<ListedAction>,
    pub label_slots_used: i64,
    #[schema(default = "200")]
    pub label_slots_total: i64,
    pub created_id: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct ActionChanges {
    pub changed: Vec<ListedAction>,
    pub label_slots_used: i64,
    #[schema(default = "200")]
    pub label_slots_total: i64,
}

/// A group as the group tools return it: plus where it sits, and what
/// it inherits.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct ListedActionGroup {
    pub id: Option<String>,
    pub group_id: Option<String>,
    pub name: Option<String>,
    pub background_color: Option<String>,
    pub priority: Option<i64>,
    pub note: Option<String>,
    pub path: Option<String>,
    pub effective_color: Option<String>,
    pub effective_priority: Option<i64>,
    pub version: Option<i64>,
}

/// One action group: a name that rolls actions up (see
/// get_action_groups).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema, RowItem)]
pub struct ActionGroup {
    pub id: Option<String>,
    pub group_id: Option<String>,
    pub name: Option<String>,
    pub background_color: Option<String>,
    pub priority: Option<i64>,
    pub note: Option<String>,
    /// Goes up by one each time it changes. Give it to an update (and to a delete, as if_version): it's the version you last read, required -- one saved before versions is version 0 -- changed since, the change is refused; on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK.
    pub version: Option<i64>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct CreatedActionGroup {
    pub changed: Vec<ListedActionGroup>,
    pub affected_actions: Vec<ListedAction>,
    pub label_slots_used: i64,
    #[schema(default = "200")]
    pub label_slots_total: i64,
    pub created_id: String,
}

/// One priority's color: its label's.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct PriorityColor {
    pub priority: i64,
    pub color: String,
    pub label_id: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct PriorityColorChange {
    pub colors: Vec<PriorityColor>,
    pub affected_actions: Vec<ListedAction>,
    pub label_slots_used: i64,
    #[schema(default = "200")]
    pub label_slots_total: i64,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct ActionGroupChanges {
    pub changed: Vec<ListedActionGroup>,
    pub affected_actions: Vec<ListedAction>,
    pub label_slots_used: i64,
    #[schema(default = "200")]
    pub label_slots_total: i64,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct DeletedActionGroup {
    pub changed: Vec<ListedActionGroup>,
    pub affected_actions: Vec<ListedAction>,
    pub label_slots_used: i64,
    #[schema(default = "200")]
    pub label_slots_total: i64,
    pub deleted: ActionGroup,
}

pub const ACTION_ROW: RowType = <Action as rows::RowItem>::ROW_TYPE;

pub const ACTION_STATUSES: [&str; 4] = ["proposed", "active", "archived", "deleted"];
pub const DEFAULT_STATUSES: [&str; 2] = ["proposed", "active"];
pub const MAX_LABELS: i64 = 200;
pub const MAX_NAME_LENGTH: usize = 50;
const LABEL_ID_NAMESPACE: &str = "8f0d3c52-41b7-4a8e-9d0a-6c1e2b7f4a93";
pub const CLEARABLE_FIELDS: [&str; 4] = ["group_id", "background_color", "priority", "note"];
const READ_ONLY_FIELDS: [&str; 2] = ["id", "label_id"];

impl Placed for Action {
    fn id(&self) -> Option<&str> {
        self.id.as_deref()
    }
    fn group_id(&self) -> Option<&str> {
        self.group_id.as_deref()
    }
    fn name(&self) -> Option<&str> {
        self.name.as_deref()
    }
    fn background_color(&self) -> Option<&str> {
        self.background_color.as_deref()
    }
    fn priority(&self) -> Option<i64> {
        self.priority
    }
}

/// An action's status as its text ("active"), if it has one.
pub fn status_of(action: &Action) -> Option<String> {
    action.status.as_ref().map(wire_text)
}

// -- events' action shares -----------------------------------------------------

/// One action an event is of, and how much of the event it takes:
/// minutes (a fixed share), percent (of what the fixed shares leave), or
/// neither -- an even part of the remaining time, what the minutes and
/// percents leave, with the others that take it. One with minutes or
/// percent can take its part of the remaining time too (`remaining`).
/// One with neither is sent and shown as its bare id, as every action was
/// before shares; one with a share as an object.
#[derive(Clone, Debug, Default, PartialEq, Eq, Hash)]
pub struct ActionShare {
    pub action_id: String,
    pub minutes: Option<i64>,
    pub percent: Option<i64>,
    pub remaining: bool,
}

impl ActionShare {
    /// The action, with neither minutes nor percent.
    pub fn whole(action_id: impl Into<String>) -> ActionShare {
        ActionShare { action_id: action_id.into(), ..Default::default() }
    }
}

impl From<&str> for ActionShare {
    fn from(id: &str) -> ActionShare {
        ActionShare::whole(id)
    }
}

impl From<String> for ActionShare {
    fn from(id: String) -> ActionShare {
        ActionShare::whole(id)
    }
}

impl Share for ActionShare {
    const KIND: &'static str = "action";
    const NEITHER: &'static str = "an even part of the remaining time";

    fn new(action_id: String, minutes: Option<i64>, percent: Option<i64>, remaining: bool) -> Self {
        ActionShare { action_id, minutes, percent, remaining: remaining && (minutes.is_some() || percent.is_some()) }
    }
    fn id(&self) -> &str {
        &self.action_id
    }
    fn minutes(&self) -> Option<i64> {
        self.minutes
    }
    fn percent(&self) -> Option<i64> {
        self.percent
    }
    fn remaining(&self) -> bool {
        self.remaining
    }
}

/// An action and how much of the event it takes: minutes (a fixed
/// share), or percent (of what the minutes leave).
#[derive(Serialize, Deserialize, Schema)]
#[serde(deny_unknown_fields)]
#[schema(deny_unknown)]
pub struct ActionSplit {
    action_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    minutes: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    percent: Option<i64>,
    /// An even part of the remaining time too, what the minutes and
    /// percents leave, with the actions given as bare ids.
    #[schema(default = "false")]
    #[serde(default, skip_serializing_if = "is_false")]
    remaining: bool,
}

#[derive(Deserialize)]
#[serde(untagged)]
enum GivenShare {
    Id(String),
    Split(ActionSplit),
}

impl Serialize for ActionShare {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        if self.minutes.is_none() && self.percent.is_none() {
            return serializer.serialize_str(&self.action_id);
        }
        ActionSplit { action_id: self.action_id.clone(), minutes: self.minutes, percent: self.percent, remaining: self.remaining }.serialize(serializer)
    }
}

impl<'de> Deserialize<'de> for ActionShare {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        Ok(match GivenShare::deserialize(deserializer)? {
            GivenShare::Id(id) => ActionShare::whole(id),
            GivenShare::Split(s) => ActionShare::new(s.action_id, s.minutes, s.percent, s.remaining),
        })
    }
}

impl Schema for ActionShare {
    fn schema(defs: &mut crate::contract::Defs) -> serde_json::Value {
        let split = ActionSplit::schema(defs);
        serde_json::json!({"anyOf": [
            {"description": "An action's id: it takes an even part of the remaining time, what the minutes and percents leave.", "type": "string"},
            split
        ]})
    }
}

/// The ids of `shares`' actions, in order.
pub fn share_ids(shares: &[ActionShare]) -> Vec<String> {
    shares.iter().map(|s| s.action_id.clone()).collect()
}

/// Read-only lookups over one snapshot of the actions and their groups.
#[derive(Clone)]
pub struct ActionTree {
    pub actions: Vec<Action>,
    pub groups: GroupTree,
}

impl ActionTree {
    pub fn new(actions: Vec<Action>, groups: GroupTree) -> ActionTree {
        ActionTree { actions, groups }
    }

    pub fn by_id(&self, id: &str) -> Option<&Action> {
        self.actions.iter().rev().find(|a| set(a.id.as_deref()) == Some(id))
    }

    pub fn priority(&self, action_id: &str) -> Option<i64> {
        self.by_id(action_id).and_then(|a| self.groups.priority(a))
    }

    pub fn name(&self, action_id: &str) -> String {
        match self.by_id(action_id) {
            Some(action) => match set(action.name.as_deref()) {
                Some(name) => name.to_string(),
                None => action_id.to_string(),
            },
            None => format!("(unknown action {action_id})"),
        }
    }

    pub fn action_for_label(&self, label_id: Option<&str>) -> Option<&Action> {
        let label_id = set(label_id)?;
        self.actions.iter().rev().find(|a| set(a.label_id.as_deref()) == Some(label_id))
    }

    /// Refuse ids that aren't actions (suggesting close ones), or that
    /// give an event a deleted action it doesn't already have.
    pub fn check_action_ids(&self, action_ids: &[String], already: &[String]) -> anyhow::Result<()> {
        let deleted: Vec<&String> = action_ids
            .iter()
            .filter(|i| self.by_id(i).is_some_and(|a| a.status == Some(ActionStatus::Deleted)) && !already.contains(i))
            .collect();
        if !deleted.is_empty() {
            let names: Vec<String> = deleted.iter().map(|i| format!("{i} ({})", str_or_none(self.by_id(i).unwrap().name.as_deref()))).collect();
            bail!("Deleted actions can't be given to an event: {}", names.join(", "));
        }
        let unknown: Vec<&String> = action_ids.iter().filter(|i| self.by_id(i).is_none()).collect();
        if !unknown.is_empty() {
            let messages: Vec<String> = unknown.iter().map(|i| unknown_message(&self.actions, i)).collect();
            bail!("{}", messages.join("; "));
        }
        Ok(())
    }
}

#[derive(Clone)]
pub struct Actions {
    calendar: CalendarClient,
    sheet: RowSheet,
    group_sheet: RowSheet,
    /// Whether this calendar's priority labels have been made sure of --
    /// for as long as this Actions (the app's) lives.
    priority_labels_ready: Arc<AtomicBool>,
}

/// What `_commit` reports: the groups changed, the actions affected.
pub struct Changes {
    pub changed: Vec<ListedActionGroup>,
    pub affected_actions: Vec<ListedAction>,
    pub label_slots_used: i64,
}

impl Actions {
    pub fn ensure(calendar: &CalendarClient, sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<Actions> {
        let sheet = RowSheet::ensure(sheets, spreadsheet_id, ACTIONS.0, ACTIONS.1, ACTION_ROW, &["id", "name"])?;
        let group_sheet = RowSheet::ensure(sheets, spreadsheet_id, ACTION_GROUPS.0, ACTION_GROUPS.1, GROUP_ROW, &["id", "name"])?;
        Ok(Actions { calendar: calendar.clone(), sheet, group_sheet, priority_labels_ready: Arc::new(AtomicBool::new(false)) })
    }

    pub fn spreadsheet_id(&self) -> &str {
        self.sheet.spreadsheet_id()
    }

    pub fn whole_tabs(&self) -> Vec<TabRange> {
        vec![self.sheet.whole_tab(), self.group_sheet.whole_tab()]
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheet.prefetch(ranges)
    }

    pub fn all(&self) -> anyhow::Result<Vec<Action>> {
        self.prefetch(&self.whole_tabs())?;
        self.sheet.read_items()
    }

    pub fn groups(&self) -> anyhow::Result<GroupTree> {
        self.prefetch(&self.whole_tabs())?;
        Ok(GroupTree::new(self.group_sheet.read_items()?, Vec::new()))
    }

    pub fn tree(&self) -> anyhow::Result<ActionTree> {
        Ok(ActionTree::new(self.all()?, self.groups()?))
    }

    pub fn get_actions(&self, statuses: Option<&[String]>) -> anyhow::Result<ActionList> {
        let statuses = check_statuses(statuses)?;
        let actions = self.all()?;
        let (raw_labels, _) = self.calendar.list_event_labels()?;
        let tree = GroupTree::new(self.groups()?.groups, palette(&raw_labels));
        let holders = label_holders(&actions, &raw_labels);
        let listed: Vec<ListedAction> =
            actions.iter().filter(|a| statuses.iter().any(|s| Some(s) == status_of(a).as_ref())).map(|a| listed(a, &tree, &holders)).collect();
        Ok(ActionList { actions: listed, label_slots_used: slots_used(&actions, &raw_labels, &holders), label_slots_total: MAX_LABELS })
    }

    pub fn get_action(&self, id_or_name: &str) -> anyhow::Result<ListedAction> {
        let actions = self.all()?;
        let action = find(&actions, id_or_name)?;
        let (raw_labels, _) = self.calendar.list_event_labels()?;
        let tree = GroupTree::new(self.groups()?.groups, palette(&raw_labels));
        Ok(listed(&action, &tree, &label_holders(&actions, &raw_labels)))
    }

    pub fn get_action_groups(&self) -> anyhow::Result<Vec<ListedActionGroup>> {
        let tree = self.painted_groups()?;
        Ok(tree.ordered().into_iter().map(|g| tree.listed(g)).collect())
    }

    pub fn get_action_group(&self, id_or_name: &str) -> anyhow::Result<ListedActionGroup> {
        let tree = self.painted_groups()?;
        Ok(tree.listed(&find_group(&tree.groups, id_or_name)?))
    }

    fn painted_groups(&self) -> anyhow::Result<GroupTree> {
        let (raw_labels, _) = self.calendar.list_event_labels()?;
        Ok(GroupTree::new(self.groups()?.groups, palette(&raw_labels)))
    }

    pub fn priority_colors(&self) -> anyhow::Result<Vec<PriorityColor>> {
        let (raw_labels, _) = self.calendar.list_event_labels()?;
        Ok(priority_colors(&palette(&raw_labels)))
    }

    pub fn set_priority_color(&self, priority: i64, color: &str) -> anyhow::Result<PriorityColorChange> {
        if !PRIORITIES.contains(&priority) {
            bail!("There's no priority {priority}: priorities are 0, 1, 2, 3");
        }
        if let Some(problem) = color_problem(color) {
            bail!("{problem}");
        }
        let (actions, groups) = (self.all()?, self.groups()?.groups);
        let changes = self.commit(
            &actions,
            &actions,
            &groups,
            &groups,
            CommitOptions { write_actions: false, write_groups: false, priority_colors: Some(vec![(priority, color.to_string())]), ..Default::default() },
        )?;
        Ok(PriorityColorChange {
            colors: self.priority_colors()?,
            affected_actions: changes.affected_actions,
            label_slots_used: changes.label_slots_used,
            label_slots_total: MAX_LABELS,
        })
    }

    /// Add any priority label the calendar doesn't have yet.
    /// Make sure the calendar has every priority's label; whether any had
    /// to be added back.
    pub fn ensure_priority_labels(&self) -> anyhow::Result<bool> {
        let (raw_labels, etag) = self.calendar.list_event_labels()?;
        let held: Vec<&str> = raw_labels.iter().filter_map(|l| l.id.as_deref()).collect();
        let missing = !PRIORITIES.iter().all(|p| held.contains(&priority_label_id(Some(*p)).as_str()));
        if missing {
            self.calendar.replace_event_labels(&with_priority_labels(&raw_labels, None), etag.as_deref())?;
        }
        self.priority_labels_ready.store(true, Ordering::SeqCst);
        Ok(missing)
    }

    /// `ensure_priority_labels`, unless that's been done already.
    pub fn ensure_priority_labels_once(&self) -> anyhow::Result<()> {
        if !self.priority_labels_ready.load(Ordering::SeqCst) {
            self.ensure_priority_labels()?;
        }
        Ok(())
    }

    pub fn create_action(&self, action: &Action) -> anyhow::Result<CreatedAction> {
        check_color(action.background_color.as_deref())?;
        let actions = self.all()?;
        let groups = self.groups()?.groups;
        // Not a group's either: a trait's part can name either.
        let taken: Vec<Option<&str>> = groups.iter().map(|g| g.id.as_deref()).collect();
        let id = match rows::created(action, &actions, &taken)? {
            rows::Created::New(id) => id,
            rows::Created::Again(made) => {
                let id = made.id.clone().unwrap_or_default();
                let list = self.get_actions(Some(ACTION_STATUSES.map(String::from).as_slice()))?;
                return Ok(CreatedAction {
                    changed: list.actions.into_iter().filter(|a| a.id.as_deref() == Some(id.as_str())).collect(),
                    label_slots_used: list.label_slots_used,
                    label_slots_total: MAX_LABELS,
                    created_id: id,
                });
            }
        };
        let namespace = uuid::Uuid::parse_str(LABEL_ID_NAMESPACE).unwrap();
        let new = Action {
            id: Some(id.clone()),
            label_id: Some(uuid::Uuid::new_v5(&namespace, id.as_bytes()).to_string()),
            status: Some(action.status.unwrap_or(ActionStatus::Proposed)),
            version: None,
            ..action.clone()
        };
        let mut after = actions.clone();
        after.push(new);
        let changes = self.commit(&actions, &after, &groups, &groups, CommitOptions { write_groups: false, ..Default::default() })?;
        let (mine, others): (Vec<ListedAction>, Vec<ListedAction>) =
            changes.affected_actions.into_iter().partition(|a| a.id.as_deref() == Some(id.as_str()));
        Ok(CreatedAction {
            changed: mine.into_iter().chain(others).collect(),
            label_slots_used: changes.label_slots_used,
            label_slots_total: MAX_LABELS,
            created_id: id,
        })
    }

    pub fn update_action(&self, action: &Action, clear_fields: &[String]) -> anyhow::Result<ActionChanges> {
        let Some(id) = set(action.id.as_deref()).map(str::to_string) else {
            bail!("update_action needs the action's id");
        };
        rows::check_clear_item(action, clear_fields, &CLEARABLE_FIELDS)?;
        check_color(action.background_color.as_deref())?;
        let actions = self.all()?;
        let Some(index) = actions.iter().position(|a| a.id.as_deref() == Some(id.as_str())) else {
            rows::expect_item_version::<Action>(&id, None, action.version)?;
            bail!("{}", unknown_message(&actions, &id));
        };
        let mut after = actions.clone();
        after[index] = rows::updated_item(&actions[index], action, clear_fields, &READ_ONLY_FIELDS)?;
        let groups = self.groups()?.groups;
        let changes = self.commit(
            &actions,
            &after,
            &groups,
            &groups,
            CommitOptions { write_groups: false, changed_actions: vec![id.clone()], ..Default::default() },
        )?;
        let (mine, others): (Vec<ListedAction>, Vec<ListedAction>) =
            changes.affected_actions.into_iter().partition(|a| a.id.as_deref() == Some(id.as_str()));
        Ok(ActionChanges { changed: mine.into_iter().chain(others).collect(), label_slots_used: changes.label_slots_used, label_slots_total: MAX_LABELS })
    }

    pub fn create_action_group(&self, group: &ActionGroup) -> anyhow::Result<CreatedActionGroup> {
        check_color(group.background_color.as_deref())?;
        let groups = self.groups()?.groups;
        let actions = self.all()?;
        let taken: Vec<Option<&str>> = actions.iter().map(|a| a.id.as_deref()).collect();
        let id = match rows::created(group, &groups, &taken)? {
            rows::Created::New(id) => id,
            rows::Created::Again(made) => {
                let id = made.id.clone().unwrap_or_default();
                return Ok(CreatedActionGroup {
                    changed: vec![self.get_action_group(&id)?],
                    affected_actions: Vec::new(),
                    label_slots_used: self.get_actions(None)?.label_slots_used,
                    label_slots_total: MAX_LABELS,
                    created_id: id,
                });
            }
        };
        let new = ActionGroup { id: Some(id.clone()), version: None, ..group.clone() };
        let mut after = groups.clone();
        after.push(new);
        let changes = self.commit(&actions, &actions, &groups, &after, CommitOptions { write_actions: false, ..Default::default() })?;
        Ok(CreatedActionGroup {
            changed: changes.changed,
            affected_actions: changes.affected_actions,
            label_slots_used: changes.label_slots_used,
            label_slots_total: MAX_LABELS,
            created_id: id,
        })
    }

    pub fn update_action_group(&self, group: &ActionGroup, clear_fields: &[String]) -> anyhow::Result<ActionGroupChanges> {
        let Some(id) = set(group.id.as_deref()).map(str::to_string) else {
            bail!("update_action_group needs the group's id");
        };
        rows::check_clear_item(group, clear_fields, &GROUP_CLEARABLE_FIELDS)?;
        check_color(group.background_color.as_deref())?;
        let groups = self.groups()?.groups;
        let Some(index) = groups.iter().position(|g| g.id.as_deref() == Some(id.as_str())) else {
            rows::expect_item_version::<ActionGroup>(&id, None, group.version)?;
            find_group(&groups, &id)?;
            unreachable!()
        };
        let mut after = groups.clone();
        after[index] = rows::updated_item(&groups[index], group, clear_fields, &["id"])?;
        let actions = self.all()?;
        let changes = self.commit(&actions, &actions, &groups, &after, CommitOptions { write_actions: false, changed_groups: vec![id], ..Default::default() })?;
        Ok(ActionGroupChanges {
            changed: changes.changed,
            affected_actions: changes.affected_actions,
            label_slots_used: changes.label_slots_used,
            label_slots_total: MAX_LABELS,
        })
    }

    /// Deletes a group -- unless it's changed since version `if_version`,
    /// if given.
    pub fn delete_action_group(&self, group_id: &str, if_version: Option<i64>) -> anyhow::Result<DeletedActionGroup> {
        let groups = self.groups()?.groups;
        let deleted = match groups.iter().find(|g| g.id.as_deref() == Some(group_id)) {
            Some(found) => found.clone(),
            None => {
                if !rows::deleting::<ActionGroup>(group_id, None, if_version)? {
                    return Ok(DeletedActionGroup {
                        changed: Vec::new(),
                        affected_actions: Vec::new(),
                        label_slots_used: self.get_actions(None)?.label_slots_used,
                        label_slots_total: MAX_LABELS,
                        deleted: rows::gone(group_id),
                    });
                }
                find_group(&groups, group_id)?
            }
        };
        rows::expect_item_version(group_id, Some(&deleted), if_version)?;
        if deleted.id.as_deref() != Some(group_id) {
            bail!("Delete a group by its id: {} is {}", repr_or_none(deleted.name.as_deref()), str_or_none(deleted.id.as_deref()));
        }
        let parent = deleted.group_id.clone();
        let moved = |current: &Option<String>| -> Option<String> {
            if current.as_deref() == Some(group_id) {
                parent.clone()
            } else {
                current.clone()
            }
        };
        let groups_after: Vec<ActionGroup> = groups
            .iter()
            .filter(|g| g.id.as_deref() != Some(group_id))
            .map(|g| ActionGroup { group_id: moved(&g.group_id), ..g.clone() })
            .collect();
        let actions = self.all()?;
        let actions_after: Vec<Action> = actions.iter().map(|a| Action { group_id: moved(&a.group_id), ..a.clone() }).collect();
        let changed_groups: Vec<String> =
            groups.iter().filter(|g| g.group_id.as_deref() == Some(group_id)).filter_map(|g| g.id.clone()).collect();
        let changes = self.commit(&actions, &actions_after, &groups, &groups_after, CommitOptions { changed_groups, ..Default::default() })?;
        Ok(DeletedActionGroup {
            changed: changes.changed,
            affected_actions: changes.affected_actions,
            label_slots_used: changes.label_slots_used,
            label_slots_total: MAX_LABELS,
            deleted,
        })
    }

    /// Check the actions and groups after a change, write them, then make
    /// the calendar's labels match -- everything that can be refused
    /// checked before anything is written.
    fn commit(
        &self,
        actions_before: &[Action],
        actions_after: &[Action],
        groups_before: &[ActionGroup],
        groups_after: &[ActionGroup],
        options: CommitOptions,
    ) -> anyhow::Result<Changes> {
        let mut problems = group_problems(groups_after);
        problems.extend(action_problems(actions_after, groups_after));
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        let (mut raw_labels, etag) = self.calendar.list_event_labels()?;
        let palette_before = palette(&raw_labels);
        let tree_before = GroupTree::new(groups_before.to_vec(), palette_before.clone());
        let mut palette_after = palette_before.clone();
        for (p, c) in options.priority_colors.iter().flatten() {
            if let Some(slot) = palette_after.iter_mut().find(|(q, _)| q == p) {
                slot.1 = c.clone();
            }
        }
        let tree_after = GroupTree::new(groups_after.to_vec(), palette_after);
        let held_before = label_holders(actions_before, &raw_labels);
        let holders = label_holders(actions_after, &raw_labels);
        let desired = desired_labels(actions_after, &tree_after, &raw_labels, &holders, options.priority_colors.as_ref())?;
        if options.write_groups {
            self.group_sheet.write_items(groups_after)?;
        }
        if options.write_actions {
            self.sheet.write_items(actions_after)?;
        }
        if !same_labels(&desired, &raw_labels) {
            raw_labels = self.calendar.replace_event_labels(&desired, etag.as_deref())?;
        }
        let old_groups: Vec<Option<&str>> = groups_before.iter().map(|g| g.id.as_deref()).collect();
        let placement = |action: &Action, tree: &GroupTree, held: &[Option<String>]| {
            (action.group_id.clone(), tree.path(action), tree.color(action), tree.priority(action), held.contains(&action.id))
        };
        let changed: Vec<ListedActionGroup> = tree_after
            .ordered()
            .into_iter()
            .filter(|g| {
                let id = g.id.as_deref();
                options.changed_groups.iter().any(|c| id == Some(c.as_str())) || !old_groups.contains(&id)
            })
            .map(|g| tree_after.listed(g))
            .collect();
        let affected: Vec<ListedAction> = actions_after
            .iter()
            .filter(|a| {
                let before = actions_before.iter().rev().find(|b| b.id == a.id);
                options.changed_actions.iter().any(|c| a.id.as_deref() == Some(c.as_str()))
                    || before.is_none()
                    || placement(before.unwrap(), &tree_before, &held_before) != placement(a, &tree_after, &holders)
            })
            .map(|a| listed(a, &tree_after, &holders))
            .collect();
        Ok(Changes { changed, affected_actions: affected, label_slots_used: slots_used(actions_after, &raw_labels, &holders) })
    }
}

struct CommitOptions {
    write_actions: bool,
    write_groups: bool,
    changed_actions: Vec<String>,
    changed_groups: Vec<String>,
    priority_colors: Option<Palette>,
}

impl Default for CommitOptions {
    /// `_commit`'s defaults: write both tabs, report nothing extra.
    fn default() -> Self {
        CommitOptions { write_actions: true, write_groups: true, changed_actions: Vec::new(), changed_groups: Vec::new(), priority_colors: None }
    }
}

/// The set of labels, compared as Python compares `{astuple(label)}`.
fn same_labels(a: &[EventLabel], b: &[EventLabel]) -> bool {
    let key = |l: &EventLabel| (l.id.clone(), l.background_color.clone(), l.name.clone());
    let mut x: Vec<_> = a.iter().map(key).collect();
    let mut y: Vec<_> = b.iter().map(key).collect();
    x.sort();
    x.dedup();
    y.sort();
    y.dedup();
    x == y
}

pub fn find(actions: &[Action], id_or_name: &str) -> anyhow::Result<Action> {
    if let Some(found) = actions.iter().find(|a| a.id.as_deref() == Some(id_or_name)) {
        return Ok(found.clone());
    }
    let wanted = id_or_name.to_lowercase();
    if let Some(found) = actions.iter().rev().find(|a| a.name.as_deref().unwrap_or_default().to_lowercase() == wanted) {
        return Ok(found.clone());
    }
    bail!("{}", unknown_message(actions, id_or_name))
}

pub fn unknown_message(actions: &[Action], id_or_name: &str) -> String {
    let with_ids: Vec<&Action> = actions.iter().filter(|a| set(a.id.as_deref()).is_some()).collect();
    let key = |a: &Action| a.name.as_deref().unwrap_or_default().to_lowercase();
    let ids = dedup(with_ids.iter().filter_map(|a| a.id.clone()));
    let names = dedup(with_ids.iter().map(|a| key(a)));
    let mut close: Vec<&Action> = Vec::new();
    for id in crate::difflib::get_close_matches(id_or_name, &ids, 3) {
        if let Some(a) = with_ids.iter().rev().find(|a| a.id.as_deref() == Some(id.as_str())) {
            close.push(a);
        }
    }
    for name in crate::difflib::get_close_matches(&id_or_name.to_lowercase(), &names, 3) {
        if let Some(a) = with_ids.iter().rev().find(|a| key(a) == name) {
            close.push(a);
        }
    }
    let hints = dedup(close.iter().map(|a| format!("{} ({})", str_or_none(a.id.as_deref()), str_or_none(a.name.as_deref()))));
    let hint = hints.join(", ");
    format!(
        "There's no action with the id or name {}{} (get_actions lists them)",
        py::repr(id_or_name),
        if hint.is_empty() { String::new() } else { format!("; did you mean {hint}?") }
    )
}

fn check_color(color: Option<&str>) -> anyhow::Result<()> {
    if let Some(problem) = color.and_then(color_problem) {
        bail!("{problem}");
    }
    Ok(())
}

pub fn action_problems(actions: &[Action], groups: &[ActionGroup]) -> Vec<String> {
    let mut problems = Vec::new();
    let ids: Vec<Option<&str>> = actions.iter().map(|a| a.id.as_deref()).collect();
    let mut repeated: Vec<&str> =
        ids.iter().flatten().copied().filter(|i| !i.is_empty() && ids.iter().filter(|x| **x == Some(*i)).count() > 1).collect();
    repeated.sort();
    repeated.dedup();
    for id in repeated {
        problems.push(format!("action id {} is used more than once", py::repr(id)));
    }
    let group_ids: Vec<Option<&str>> = groups.iter().map(|g| g.id.as_deref()).collect();
    let mut names: Vec<(String, &Action)> = Vec::new();
    for action in actions {
        let name_repr = repr_or_none(action.name.as_deref());
        let label = match set(action.id.as_deref()) {
            Some(id) => format!("action {id}"),
            None => format!("action {name_repr}"),
        };
        if set(action.id.as_deref()).is_none() {
            problems.push(format!("an action named {name_repr} has no id"));
        }
        if set(action.label_id.as_deref()).is_none() {
            problems.push(format!("{label} has no label_id"));
        }
        match action.name.as_deref().filter(|n| !n.trim().is_empty()) {
            None => problems.push(format!("{label} needs a name")),
            Some(name) if name.chars().count() > MAX_NAME_LENGTH => {
                problems.push(format!("{label}'s name {} is longer than {MAX_NAME_LENGTH} characters", py::repr(name)))
            }
            Some(name) => {
                let key = name.trim().to_lowercase();
                if let Some((_, other)) = names.iter().find(|(k, _)| *k == key) {
                    problems.push(format!(
                        "there's already an action named {} ({}, {}); action names must be unique -- use that one, or pick another name",
                        repr_or_none(other.name.as_deref()),
                        str_or_none(other.id.as_deref()),
                        str_or_none(status_of(other).as_deref())
                    ));
                }
                names.retain(|(k, _)| *k != key);
                names.push((key, action));
            }
        }
        if action.status.is_none() {
            problems.push(format!("{label}'s status must be one of {}", ACTION_STATUSES.join(", ")));
        }
        if let Some(group_id) = action.group_id.as_deref() {
            if !group_ids.contains(&Some(group_id)) {
                problems.push(format!("{label}'s group_id {} isn't an action group (get_action_groups lists them)", py::repr(group_id)));
            }
        }
    }
    problems
}

pub fn listed(action: &Action, tree: &GroupTree, holders: &[Option<String>]) -> ListedAction {
    ListedAction {
        id: action.id.clone(),
        group_id: action.group_id.clone(),
        name: action.name.clone(),
        status: action.status,
        label_id: action.label_id.clone(),
        background_color: action.background_color.clone(),
        priority: action.priority,
        note: action.note.clone(),
        commute: action.commute,
        path: Some(tree.path(action)),
        effective_color: Some(tree.color(action)),
        effective_priority: tree.priority(action),
        holds_label: Some(holders.contains(&action.id)),
        version: action.version,
    }
}

/// The calendar's labels that aren't any action's -- plus every priority
/// label, recolored as `priority_colors` says.
fn foreign(actions: &[Action], raw_labels: &[EventLabel], priority_colors: Option<&Palette>) -> Vec<EventLabel> {
    let ours: Vec<&str> = actions.iter().filter_map(|a| set(a.label_id.as_deref())).collect();
    let others: Vec<EventLabel> = raw_labels.iter().filter(|l| !l.id.as_deref().is_some_and(|i| ours.contains(&i))).cloned().collect();
    with_priority_labels(&others, priority_colors)
}

/// The ids of the actions holding a label: every active one, then
/// proposed ones in sheet order while there's room.
pub fn label_holders(actions: &[Action], raw_labels: &[EventLabel]) -> Vec<Option<String>> {
    let mut room = MAX_LABELS - foreign(actions, raw_labels, None).len() as i64;
    let mut holders: Vec<Option<String>> = Vec::new();
    for action in actions {
        if action.status == Some(ActionStatus::Active) && !holders.contains(&action.id) {
            holders.push(action.id.clone());
        }
    }
    room -= holders.len() as i64;
    for action in actions {
        if action.status == Some(ActionStatus::Proposed) && room > 0 {
            if !holders.contains(&action.id) {
                holders.push(action.id.clone());
            }
            room -= 1;
        }
    }
    holders
}

fn desired_labels(
    actions: &[Action],
    tree: &GroupTree,
    raw_labels: &[EventLabel],
    holders: &[Option<String>],
    priority_colors: Option<&Palette>,
) -> anyhow::Result<Vec<EventLabel>> {
    let mut labels = foreign(actions, raw_labels, priority_colors);
    let active = actions.iter().filter(|a| a.status == Some(ActionStatus::Active)).count();
    if (labels.len() + active) as i64 > MAX_LABELS {
        bail!(
            "That would need {} event labels, but a calendar holds {MAX_LABELS} ({} aren't actions'), so at most {} actions can be active at once. Archive one that isn't done any more.",
            labels.len() + active,
            labels.len(),
            MAX_LABELS - labels.len() as i64
        );
    }
    for action in actions {
        if holders.contains(&action.id) {
            labels.push(EventLabel { id: action.label_id.clone(), name: action.name.clone(), background_color: tree.color(action) });
        }
    }
    Ok(labels)
}

fn slots_used(actions: &[Action], raw_labels: &[EventLabel], holders: &[Option<String>]) -> i64 {
    (foreign(actions, raw_labels, None).len() + holders.len()) as i64
}

pub fn priority_colors(palette: &Palette) -> Vec<PriorityColor> {
    PRIORITIES
        .iter()
        .map(|p| PriorityColor {
            priority: *p,
            color: palette.iter().find(|(q, _)| q == p).map(|(_, c)| c.clone()).unwrap_or_default(),
            label_id: priority_label_id(Some(*p)),
        })
        .collect()
}

fn check_statuses(statuses: Option<&[String]>) -> anyhow::Result<Vec<String>> {
    match statuses {
        None => Ok(DEFAULT_STATUSES.iter().map(|s| s.to_string()).collect()),
        Some(given) => {
            let mut unknown: Vec<&String> = given.iter().filter(|s| !ACTION_STATUSES.contains(&s.as_str())).collect();
            unknown.sort();
            unknown.dedup();
            if !unknown.is_empty() {
                bail!("Unknown action status(es) {}; statuses are {}", py::repr_list(&unknown), ACTION_STATUSES.join(", "));
            }
            Ok(given.to_vec())
        }
    }
}

// Written by the app: made once by their ids and versions.
impl rows::Versioned for Action {}
impl rows::Versioned for ActionGroup {}

#[cfg(test)]
#[path = "actions_tests.rs"]
mod tests;
