//! Actions: what the user does with their time, each holding one of the
//! calendar's event labels while it's in play, kept with the action groups
//! in the Actions and Action Groups tabs.

use crate::google::calendar::{CalendarClient, EventLabel};
use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::sheet::rows::{self, Kind, Row, RowSheet, RowType};
use crate::sheet::{ACTIONS, ACTION_GROUPS};
use crate::stores::groups::{
    color_problem, find_group, group_problems, palette, python_repr, python_str, priority_label_id, truthy,
    with_priority_labels, GroupTree, Palette, GROUP_CLEARABLE_FIELDS, GROUP_ROW, PRIORITIES,
};
use crate::stores::locations::dedup;
use anyhow::bail;
use serde_json::{json, Map, Value};

pub const ACTION_ROW: RowType = RowType {
    name: "Action",
    fields: &[
        ("id", Kind::Str),
        ("group_id", Kind::Str),
        ("name", Kind::Str),
        ("status", Kind::Str),
        ("label_id", Kind::Str),
        ("background_color", Kind::Str),
        ("priority", Kind::Int),
        ("note", Kind::Str),
    ],
};

pub const ACTION_STATUSES: [&str; 4] = ["proposed", "active", "archived", "deleted"];
pub const DEFAULT_STATUSES: [&str; 2] = ["proposed", "active"];
pub const MAX_LABELS: usize = 200;
pub const MAX_NAME_LENGTH: usize = 50;
const LABEL_ID_NAMESPACE: &str = "8f0d3c52-41b7-4a8e-9d0a-6c1e2b7f4a93";
pub const CLEARABLE_FIELDS: [&str; 4] = ["group_id", "background_color", "priority", "note"];
const READ_ONLY_FIELDS: [&str; 2] = ["id", "label_id"];

fn id_of(row: &Row) -> Option<&Value> {
    row.get("id").filter(|i| truthy(Some(i)))
}

/// Read-only lookups over one snapshot of the actions and their groups.
#[derive(Clone)]
pub struct ActionTree {
    pub actions: Vec<Row>,
    pub groups: GroupTree,
}

impl ActionTree {
    pub fn new(actions: Vec<Row>, groups: GroupTree) -> ActionTree {
        ActionTree { actions, groups }
    }

    pub fn by_id(&self, id: &str) -> Option<&Row> {
        self.actions.iter().rev().find(|a| id_of(a).and_then(Value::as_str) == Some(id))
    }

    pub fn priority(&self, action_id: &str) -> Option<i64> {
        self.by_id(action_id).and_then(|a| self.groups.priority_int(a))
    }

    pub fn name(&self, action_id: &str) -> String {
        match self.by_id(action_id) {
            Some(action) => match action.get("name").filter(|n| truthy(Some(n))) {
                Some(name) => python_str(Some(name)),
                None => action_id.to_string(),
            },
            None => format!("(unknown action {action_id})"),
        }
    }

    pub fn action_for_label(&self, label_id: Option<&str>) -> Option<&Row> {
        let label_id = label_id.filter(|l| !l.is_empty())?;
        self.actions.iter().rev().find(|a| truthy(a.get("label_id")) && rows::text(a, "label_id") == Some(label_id))
    }

    /// Refuse ids that aren't actions (suggesting close ones), or that
    /// give an event a deleted action it doesn't already have.
    pub fn check_action_ids(&self, action_ids: &[String], already: &[String]) -> anyhow::Result<()> {
        let deleted: Vec<&String> = action_ids
            .iter()
            .filter(|i| self.by_id(i).is_some_and(|a| rows::text(a, "status") == Some("deleted")) && !already.contains(i))
            .collect();
        if !deleted.is_empty() {
            let names: Vec<String> =
                deleted.iter().map(|i| format!("{i} ({})", python_str(self.by_id(i).unwrap().get("name")))).collect();
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
}

/// What `_commit` reports: the groups changed, the actions affected.
pub struct Changes {
    pub changed: Vec<Value>,
    pub affected_actions: Vec<Value>,
    pub label_slots_used: usize,
}

impl Changes {
    pub fn to_json(&self) -> Value {
        json!({
            "changed": self.changed,
            "affected_actions": self.affected_actions,
            "label_slots_used": self.label_slots_used,
            "label_slots_total": MAX_LABELS,
        })
    }
}

impl Actions {
    pub fn ensure(calendar: &CalendarClient, sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<Actions> {
        let sheet = RowSheet::ensure(sheets, spreadsheet_id, ACTIONS.0, ACTIONS.1, ACTION_ROW, &["id", "name"])?;
        let group_sheet = RowSheet::ensure(sheets, spreadsheet_id, ACTION_GROUPS.0, ACTION_GROUPS.1, GROUP_ROW, &["id", "name"])?;
        Ok(Actions { calendar: calendar.clone(), sheet, group_sheet })
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

    pub fn all(&self) -> anyhow::Result<Vec<Row>> {
        self.prefetch(&self.whole_tabs())?;
        self.sheet.read()
    }

    pub fn groups(&self) -> anyhow::Result<GroupTree> {
        self.prefetch(&self.whole_tabs())?;
        Ok(GroupTree::new(self.group_sheet.read()?, Vec::new()))
    }

    pub fn tree(&self) -> anyhow::Result<ActionTree> {
        Ok(ActionTree::new(self.all()?, self.groups()?))
    }

    pub fn get_actions(&self, statuses: Option<&[String]>) -> anyhow::Result<Value> {
        let statuses = check_statuses(statuses)?;
        let actions = self.all()?;
        let (raw_labels, _) = self.calendar.list_event_labels()?;
        let tree = GroupTree::new(self.groups()?.groups, palette(&raw_labels));
        let holders = label_holders(&actions, &raw_labels);
        let listed: Vec<Value> = actions
            .iter()
            .filter(|a| statuses.iter().any(|s| Some(s.as_str()) == rows::text(a, "status")))
            .map(|a| listed(a, &tree, &holders))
            .collect();
        Ok(json!({
            "actions": listed,
            "label_slots_used": slots_used(&actions, &raw_labels, &holders),
            "label_slots_total": MAX_LABELS,
        }))
    }

    pub fn get_action(&self, id_or_name: &str) -> anyhow::Result<Value> {
        let actions = self.all()?;
        let action = find(&actions, id_or_name)?;
        let (raw_labels, _) = self.calendar.list_event_labels()?;
        let tree = GroupTree::new(self.groups()?.groups, palette(&raw_labels));
        Ok(listed(&action, &tree, &label_holders(&actions, &raw_labels)))
    }

    pub fn get_action_groups(&self) -> anyhow::Result<Value> {
        let tree = self.painted_groups()?;
        Ok(Value::Array(tree.ordered().into_iter().map(|g| tree.listed(g)).collect()))
    }

    pub fn get_action_group(&self, id_or_name: &str) -> anyhow::Result<Value> {
        let tree = self.painted_groups()?;
        Ok(tree.listed(&find_group(&tree.groups, id_or_name)?))
    }

    fn painted_groups(&self) -> anyhow::Result<GroupTree> {
        let (raw_labels, _) = self.calendar.list_event_labels()?;
        Ok(GroupTree::new(self.groups()?.groups, palette(&raw_labels)))
    }

    pub fn priority_colors(&self) -> anyhow::Result<Value> {
        let (raw_labels, _) = self.calendar.list_event_labels()?;
        Ok(priority_colors_json(&palette(&raw_labels)))
    }

    pub fn set_priority_color(&self, priority: &Value, color: &Value) -> anyhow::Result<Value> {
        let p = priority.as_i64().filter(|p| PRIORITIES.contains(p));
        let Some(p) = p else {
            bail!("There's no priority {}: priorities are 0, 1, 2, 3", python_repr(priority));
        };
        if let Some(problem) = color_problem(color) {
            bail!("{problem}");
        }
        let (actions, groups) = (self.all()?, self.groups()?.groups);
        let changes = self.commit(
            &actions,
            &actions,
            &groups,
            &groups,
            CommitOptions { write_actions: false, write_groups: false, priority_colors: Some(vec![(p, color.as_str().unwrap().to_string())]), ..Default::default() },
        )?;
        Ok(json!({
            "colors": self.priority_colors()?,
            "affected_actions": changes.affected_actions,
            "label_slots_used": changes.label_slots_used,
            "label_slots_total": MAX_LABELS,
        }))
    }

    /// Add any priority label the calendar doesn't have yet.
    pub fn ensure_priority_labels(&self) -> anyhow::Result<()> {
        let (raw_labels, etag) = self.calendar.list_event_labels()?;
        let held: Vec<&str> = raw_labels.iter().filter_map(|l| l.id.as_deref()).collect();
        if !PRIORITIES.iter().all(|p| held.contains(&priority_label_id(Some(*p)).as_str())) {
            self.calendar.replace_event_labels(&with_priority_labels(&raw_labels, None), etag.as_deref())?;
        }
        Ok(())
    }

    pub fn create_action(&self, action: &Row) -> anyhow::Result<Value> {
        check_color(action.get("background_color"))?;
        let actions = self.all()?;
        let mut new = action.clone();
        for name in READ_ONLY_FIELDS {
            new.insert(name.into(), Value::Null);
        }
        if !truthy(action.get("status")) {
            new.insert("status".into(), json!("proposed"));
        }
        let id = rows::new_id(actions.iter().map(|a| rows::text(a, "id")).collect::<Vec<_>>());
        let namespace = uuid::Uuid::parse_str(LABEL_ID_NAMESPACE).unwrap();
        new.insert("id".into(), json!(id));
        new.insert("label_id".into(), json!(uuid::Uuid::new_v5(&namespace, id.as_bytes()).to_string()));
        let groups = self.groups()?.groups;
        let mut after = actions.clone();
        after.push(new);
        let changes = self.commit(&actions, &after, &groups, &groups, CommitOptions { write_groups: false, ..Default::default() })?;
        let (mine, others): (Vec<Value>, Vec<Value>) =
            changes.affected_actions.into_iter().partition(|a| a["id"].as_str() == Some(id.as_str()));
        Ok(json!({
            "changed": mine.into_iter().chain(others).collect::<Vec<_>>(),
            "label_slots_used": changes.label_slots_used,
            "label_slots_total": MAX_LABELS,
            "created_id": id,
        }))
    }

    pub fn update_action(&self, action: &Row, clear_fields: &[String]) -> anyhow::Result<Value> {
        let Some(id) = rows::text(action, "id").filter(|i| !i.is_empty()).map(str::to_string) else {
            bail!("update_action needs the action's id");
        };
        rows::check_clear(action, clear_fields, &CLEARABLE_FIELDS)?;
        check_color(action.get("background_color"))?;
        let actions = self.all()?;
        let Some(index) = actions.iter().position(|a| rows::text(a, "id") == Some(id.as_str())) else {
            bail!("{}", unknown_message(&actions, &id));
        };
        let mut after = actions.clone();
        after[index] = rows::updated(&actions[index], action, clear_fields, &READ_ONLY_FIELDS);
        let groups = self.groups()?.groups;
        let changes = self.commit(
            &actions,
            &after,
            &groups,
            &groups,
            CommitOptions { write_groups: false, changed_actions: vec![id.clone()], ..Default::default() },
        )?;
        let (mine, others): (Vec<Value>, Vec<Value>) =
            changes.affected_actions.into_iter().partition(|a| a["id"].as_str() == Some(id.as_str()));
        Ok(json!({
            "changed": mine.into_iter().chain(others).collect::<Vec<_>>(),
            "label_slots_used": changes.label_slots_used,
            "label_slots_total": MAX_LABELS,
        }))
    }

    pub fn create_action_group(&self, group: &Row) -> anyhow::Result<Value> {
        check_color(group.get("background_color"))?;
        let groups = self.groups()?.groups;
        let mut new = group.clone();
        let id = rows::new_id(groups.iter().map(|g| rows::text(g, "id")).collect::<Vec<_>>());
        new.insert("id".into(), json!(id));
        let actions = self.all()?;
        let mut after = groups.clone();
        after.push(new);
        let changes = self.commit(&actions, &actions, &groups, &after, CommitOptions { write_actions: false, ..Default::default() })?;
        let mut out = changes.to_json();
        out.as_object_mut().unwrap().insert("created_id".into(), json!(id));
        Ok(out)
    }

    pub fn update_action_group(&self, group: &Row, clear_fields: &[String]) -> anyhow::Result<Value> {
        let Some(id) = rows::text(group, "id").filter(|i| !i.is_empty()).map(str::to_string) else {
            bail!("update_action_group needs the group's id");
        };
        rows::check_clear(group, clear_fields, &GROUP_CLEARABLE_FIELDS)?;
        check_color(group.get("background_color"))?;
        let groups = self.groups()?.groups;
        let Some(index) = groups.iter().position(|g| rows::text(g, "id") == Some(id.as_str())) else {
            find_group(&groups, &id)?;
            unreachable!()
        };
        let mut after = groups.clone();
        after[index] = rows::updated(&groups[index], group, clear_fields, &["id"]);
        let actions = self.all()?;
        Ok(self
            .commit(&actions, &actions, &groups, &after, CommitOptions { write_actions: false, changed_groups: vec![id], ..Default::default() })?
            .to_json())
    }

    pub fn delete_action_group(&self, group_id: &str) -> anyhow::Result<Value> {
        let groups = self.groups()?.groups;
        let deleted = match groups.iter().find(|g| rows::text(g, "id") == Some(group_id)) {
            Some(found) => found.clone(),
            None => find_group(&groups, group_id)?,
        };
        if rows::text(&deleted, "id") != Some(group_id) {
            bail!(
                "Delete a group by its id: {} is {}",
                python_repr(deleted.get("name").unwrap_or(&Value::Null)),
                python_str(deleted.get("id"))
            );
        }
        let parent = deleted.get("group_id").cloned().unwrap_or(Value::Null);
        let moved = |row: &Row| -> Row {
            let mut row = row.clone();
            if rows::text(&row, "group_id") == Some(group_id) {
                row.insert("group_id".into(), parent.clone());
            }
            row
        };
        let groups_after: Vec<Row> = groups.iter().filter(|g| rows::text(g, "id") != Some(group_id)).map(moved).collect();
        let actions = self.all()?;
        let actions_after: Vec<Row> = actions.iter().map(moved).collect();
        let changed_groups: Vec<String> = groups
            .iter()
            .filter(|g| rows::text(g, "group_id") == Some(group_id))
            .filter_map(|g| rows::text(g, "id").map(str::to_string))
            .collect();
        let changes =
            self.commit(&actions, &actions_after, &groups, &groups_after, CommitOptions { changed_groups, ..Default::default() })?;
        let mut out = changes.to_json();
        out.as_object_mut().unwrap().insert("deleted".into(), Value::Object(deleted));
        Ok(out)
    }

    /// Check the actions and groups after a change, write them, then make
    /// the calendar's labels match -- everything that can be refused
    /// checked before anything is written.
    fn commit(
        &self,
        actions_before: &[Row],
        actions_after: &[Row],
        groups_before: &[Row],
        groups_after: &[Row],
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
            self.group_sheet.write(groups_after)?;
        }
        if options.write_actions {
            self.sheet.write(actions_after)?;
        }
        if !same_labels(&desired, &raw_labels) {
            raw_labels = self.calendar.replace_event_labels(&desired, etag.as_deref())?;
        }
        let old_groups: Vec<Option<&Value>> = groups_before.iter().map(|g| g.get("id")).collect();
        let placement = |action: &Row, tree: &GroupTree, held: &[Value]| -> (Value, String, Value, Option<Value>, bool) {
            (
                action.get("group_id").cloned().unwrap_or(Value::Null),
                tree.path(action),
                tree.color(action),
                tree.priority(action),
                held.contains(action.get("id").unwrap_or(&Value::Null)),
            )
        };
        let changed: Vec<Value> = tree_after
            .ordered()
            .into_iter()
            .filter(|g| {
                let id = g.get("id");
                options.changed_groups.iter().any(|c| id.and_then(Value::as_str) == Some(c.as_str())) || !old_groups.contains(&id)
            })
            .map(|g| tree_after.listed(g))
            .collect();
        let affected: Vec<Value> = actions_after
            .iter()
            .filter(|a| {
                let id = a.get("id").unwrap_or(&Value::Null);
                let before = actions_before.iter().rev().find(|b| b.get("id").unwrap_or(&Value::Null) == id);
                options.changed_actions.iter().any(|c| id.as_str() == Some(c.as_str()))
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

pub fn find(actions: &[Row], id_or_name: &str) -> anyhow::Result<Row> {
    if let Some(found) = actions.iter().find(|a| rows::text(a, "id") == Some(id_or_name)) {
        return Ok(found.clone());
    }
    let wanted = id_or_name.to_lowercase();
    if let Some(found) = actions.iter().rev().find(|a| rows::text(a, "name").unwrap_or_default().to_lowercase() == wanted) {
        return Ok(found.clone());
    }
    bail!("{}", unknown_message(actions, id_or_name))
}

pub fn unknown_message(actions: &[Row], id_or_name: &str) -> String {
    let with_ids: Vec<&Row> = actions.iter().filter(|a| id_of(a).is_some()).collect();
    let key = |row: &Row| rows::text(row, "name").unwrap_or_default().to_lowercase();
    let ids = dedup(with_ids.iter().filter_map(|a| rows::text(a, "id").map(str::to_string)));
    let names = dedup(with_ids.iter().map(|a| key(a)));
    let mut close: Vec<&Row> = Vec::new();
    for id in crate::difflib::get_close_matches(id_or_name, &ids, 3) {
        if let Some(a) = with_ids.iter().rev().find(|a| rows::text(a, "id") == Some(id.as_str())) {
            close.push(a);
        }
    }
    for name in crate::difflib::get_close_matches(&id_or_name.to_lowercase(), &names, 3) {
        if let Some(a) = with_ids.iter().rev().find(|a| key(a) == name) {
            close.push(a);
        }
    }
    let hints = dedup(close.iter().map(|a| format!("{} ({})", python_str(a.get("id")), python_str(a.get("name")))));
    let hint = hints.join(", ");
    format!(
        "There's no action with the id or name {}{} (get_actions lists them)",
        py::repr(id_or_name),
        if hint.is_empty() { String::new() } else { format!("; did you mean {hint}?") }
    )
}

fn check_color(color: Option<&Value>) -> anyhow::Result<()> {
    if let Some(color) = color.filter(|c| !c.is_null()) {
        if let Some(problem) = color_problem(color) {
            bail!("{problem}");
        }
    }
    Ok(())
}

pub fn action_problems(actions: &[Row], groups: &[Row]) -> Vec<String> {
    let mut problems = Vec::new();
    let ids: Vec<Option<&str>> = actions.iter().map(|a| rows::text(a, "id")).collect();
    let mut repeated: Vec<&str> =
        ids.iter().flatten().copied().filter(|i| !i.is_empty() && ids.iter().filter(|x| **x == Some(*i)).count() > 1).collect();
    repeated.sort();
    repeated.dedup();
    for id in repeated {
        problems.push(format!("action id {} is used more than once", py::repr(id)));
    }
    let group_ids: Vec<&Value> = groups.iter().map(|g| g.get("id").unwrap_or(&Value::Null)).collect();
    let mut names: Vec<(String, Row)> = Vec::new();
    for action in actions {
        let name_repr = python_repr(action.get("name").unwrap_or(&Value::Null));
        let label = match id_of(action) {
            Some(id) => format!("action {}", python_str(Some(id))),
            None => format!("action {name_repr}"),
        };
        if id_of(action).is_none() {
            problems.push(format!("an action named {name_repr} has no id"));
        }
        if !truthy(action.get("label_id")) {
            problems.push(format!("{label} has no label_id"));
        }
        match action.get("name").and_then(Value::as_str).filter(|n| !n.trim().is_empty()) {
            None => problems.push(format!("{label} needs a name")),
            Some(name) if name.chars().count() > MAX_NAME_LENGTH => {
                problems.push(format!("{label}'s name {} is longer than {MAX_NAME_LENGTH} characters", py::repr(name)))
            }
            Some(name) => {
                let key = name.trim().to_lowercase();
                if let Some((_, other)) = names.iter().find(|(k, _)| *k == key) {
                    problems.push(format!(
                        "there's already an action named {} ({}, {}); action names must be unique -- use that one, or pick another name",
                        python_repr(other.get("name").unwrap_or(&Value::Null)),
                        python_str(other.get("id")),
                        python_str(other.get("status"))
                    ));
                }
                names.retain(|(k, _)| *k != key);
                names.push((key, action.clone()));
            }
        }
        if !ACTION_STATUSES.iter().any(|s| rows::text(action, "status") == Some(*s)) {
            problems.push(format!("{label}'s status must be one of {}", ACTION_STATUSES.join(", ")));
        }
        if let Some(priority) = action.get("priority").filter(|p| !p.is_null()) {
            if !(priority.is_i64() || priority.is_u64() || priority.is_boolean()) {
                problems.push(format!("{label}'s priority must be a whole number"));
            }
        }
        if let Some(group_id) = action.get("group_id").filter(|g| !g.is_null()) {
            if !group_ids.contains(&group_id) {
                problems.push(format!(
                    "{label}'s group_id {} isn't an action group (get_action_groups lists them)",
                    python_repr(group_id)
                ));
            }
        }
    }
    problems
}

pub fn listed(action: &Row, tree: &GroupTree, holders: &[Value]) -> Value {
    let mut out: Map<String, Value> = Map::new();
    for (name, _) in ACTION_ROW.fields {
        out.insert(name.to_string(), action.get(*name).cloned().unwrap_or(Value::Null));
    }
    out.insert("path".into(), json!(tree.path(action)));
    out.insert("effective_color".into(), tree.color(action));
    out.insert("effective_priority".into(), tree.priority(action).unwrap_or(Value::Null));
    out.insert("holds_label".into(), json!(holders.contains(action.get("id").unwrap_or(&Value::Null))));
    Value::Object(out)
}

/// The calendar's labels that aren't any action's -- plus every priority
/// label, recolored as `priority_colors` says.
fn foreign(actions: &[Row], raw_labels: &[EventLabel], priority_colors: Option<&Palette>) -> Vec<EventLabel> {
    let ours: Vec<&str> = actions.iter().filter(|a| truthy(a.get("label_id"))).filter_map(|a| rows::text(a, "label_id")).collect();
    let others: Vec<EventLabel> =
        raw_labels.iter().filter(|l| !l.id.as_deref().is_some_and(|i| ours.contains(&i))).cloned().collect();
    with_priority_labels(&others, priority_colors)
}

/// The ids of the actions holding a label: every active one, then
/// proposed ones in sheet order while there's room.
pub fn label_holders(actions: &[Row], raw_labels: &[EventLabel]) -> Vec<Value> {
    let mut room = MAX_LABELS as i64 - foreign(actions, raw_labels, None).len() as i64;
    let mut holders: Vec<Value> = Vec::new();
    for action in actions {
        if rows::text(action, "status") == Some("active") {
            let id = action.get("id").cloned().unwrap_or(Value::Null);
            if !holders.contains(&id) {
                holders.push(id);
            }
        }
    }
    room -= holders.len() as i64;
    for action in actions {
        if rows::text(action, "status") == Some("proposed") && room > 0 {
            let id = action.get("id").cloned().unwrap_or(Value::Null);
            if !holders.contains(&id) {
                holders.push(id);
            }
            room -= 1;
        }
    }
    holders
}

fn desired_labels(
    actions: &[Row],
    tree: &GroupTree,
    raw_labels: &[EventLabel],
    holders: &[Value],
    priority_colors: Option<&Palette>,
) -> anyhow::Result<Vec<EventLabel>> {
    let mut labels = foreign(actions, raw_labels, priority_colors);
    let active = actions.iter().filter(|a| rows::text(a, "status") == Some("active")).count();
    if labels.len() + active > MAX_LABELS {
        bail!(
            "That would need {} event labels, but a calendar holds {MAX_LABELS} ({} aren't actions'), so at most {} actions can be active at once. Archive one that isn't done any more.",
            labels.len() + active,
            labels.len(),
            MAX_LABELS - labels.len()
        );
    }
    for action in actions {
        if holders.contains(action.get("id").unwrap_or(&Value::Null)) {
            labels.push(EventLabel {
                id: action.get("label_id").and_then(Value::as_str).map(str::to_string),
                name: action.get("name").and_then(Value::as_str).map(str::to_string),
                background_color: python_str(Some(&tree.color(action))),
            });
        }
    }
    Ok(labels)
}

fn slots_used(actions: &[Row], raw_labels: &[EventLabel], holders: &[Value]) -> usize {
    foreign(actions, raw_labels, None).len() + holders.len()
}

pub fn priority_colors_json(palette: &Palette) -> Value {
    Value::Array(
        PRIORITIES
            .iter()
            .map(|p| {
                json!({
                    "priority": p,
                    "color": palette.iter().find(|(q, _)| q == p).map(|(_, c)| c.clone()).unwrap_or_default(),
                    "label_id": priority_label_id(Some(*p)),
                })
            })
            .collect(),
    )
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

#[cfg(test)]
#[path = "actions_tests.rs"]
mod tests;
