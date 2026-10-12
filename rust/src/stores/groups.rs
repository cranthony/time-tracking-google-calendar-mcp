//! Action groups and the priority palette: what actions inherit their color
//! and priority from.

use crate::google::calendar::EventLabel;
use crate::py;
use crate::sheet::rows::{self, Kind, Row, RowType};
use crate::stores::locations::dedup;
use anyhow::bail;
use regex::Regex;
use serde_json::{json, Map, Value};
use std::sync::LazyLock;

pub const GROUP_ROW: RowType = RowType {
    name: "ActionGroup",
    fields: &[
        ("id", Kind::Str),
        ("group_id", Kind::Str),
        ("name", Kind::Str),
        ("background_color", Kind::Str),
        ("priority", Kind::Int),
        ("note", Kind::Str),
    ],
};
pub const GROUP_CLEARABLE_FIELDS: [&str; 4] = ["group_id", "background_color", "priority", "note"];
pub const MAX_NAME_LENGTH: usize = 50;

// -- priority labels ---------------------------------------------------------

pub const PRIORITIES: [i64; 4] = [0, 1, 2, 3];
pub const DEFAULT_PRIORITY: i64 = 2;
const PRIORITY_NAMESPACE: &str = "3b8f6f1e-5d3a-4c8e-9a52-7e2f0c4d1b67";
static COLOR: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^#[0-9a-fA-F]{6}$").unwrap());

pub fn clamp(priority: Option<i64>) -> i64 {
    match priority {
        None => DEFAULT_PRIORITY,
        Some(p) => p.clamp(0, 3),
    }
}

pub fn priority_label_id(priority: Option<i64>) -> String {
    let namespace = uuid::Uuid::parse_str(PRIORITY_NAMESPACE).unwrap();
    uuid::Uuid::new_v5(&namespace, format!("priority-{}", clamp(priority)).as_bytes()).to_string()
}

/// The priority whose label `id` is, if it's one.
pub fn priority_of_label(id: &str) -> Option<i64> {
    PRIORITIES.into_iter().find(|p| priority_label_id(Some(*p)) == id)
}

/// The hex each priority's label starts out in (color_for_priority).
pub fn default_color(priority: Option<i64>) -> &'static str {
    match priority.unwrap_or(2).clamp(0, 3) {
        0 => "#e1e1e1",
        1 => "#fbd75b",
        2 => "#a4bdfc",
        _ => "#7ae7bf",
    }
}

pub type Palette = Vec<(i64, String)>;

/// Each priority's color: its label's, or its default.
pub fn palette(raw_labels: &[EventLabel]) -> Palette {
    PRIORITIES
        .into_iter()
        .map(|p| {
            let found = raw_labels
                .iter()
                .rev()
                .find(|l| l.id.as_deref().and_then(priority_of_label) == Some(p))
                .map(|l| l.background_color.clone())
                .filter(|c| !c.is_empty());
            (p, found.unwrap_or_else(|| default_color(Some(p)).to_string()))
        })
        .collect()
}

fn palette_get(palette: &Palette, priority: i64) -> Option<&str> {
    palette.iter().find(|(p, _)| *p == priority).map(|(_, c)| c.as_str()).filter(|c| !c.is_empty())
}

/// `raw_labels` with every priority label in them, colored as `colors`
/// says where it says.
pub fn with_priority_labels(raw_labels: &[EventLabel], colors: Option<&Palette>) -> Vec<EventLabel> {
    let mut merged = palette(raw_labels);
    if let Some(colors) = colors {
        for (p, c) in colors {
            if let Some(slot) = merged.iter_mut().find(|(q, _)| q == p) {
                slot.1 = c.clone();
            }
        }
    }
    let mut out: Vec<EventLabel> =
        raw_labels.iter().filter(|l| l.id.as_deref().and_then(priority_of_label).is_none()).cloned().collect();
    for p in PRIORITIES {
        out.push(EventLabel {
            id: Some(priority_label_id(Some(p))),
            name: Some(format!("Priority {p}")),
            background_color: merged.iter().find(|(q, _)| *q == p).unwrap().1.clone(),
        });
    }
    out
}

/// Why `color` can't be a label's, or `None`.
pub fn color_problem(color: &Value) -> Option<String> {
    match color {
        Value::String(text) if COLOR.is_match(text) => None,
        Value::String(text) => Some(format!("{} isn't a color: give one as #rrggbb, e.g. \"#a4bdfc\"", py::repr(text))),
        other => Some(format!("{} isn't a color: give one as #rrggbb, e.g. \"#a4bdfc\"", python_repr(other))),
    }
}

/// `repr()` of a JSON value as Python would print the object it parses to.
pub fn python_repr(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::Number(n) => py::python_number(n),
        Value::String(s) => py::repr(s),
        Value::Array(items) => format!("[{}]", items.iter().map(python_repr).collect::<Vec<_>>().join(", ")),
        Value::Object(map) => format!(
            "{{{}}}",
            map.iter().map(|(k, v)| format!("{}: {}", py::repr(k), python_repr(v))).collect::<Vec<_>>().join(", ")
        ),
    }
}

/// `str()` of a JSON value as Python would print it (None, numbers, text).
pub fn python_str(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => "None".into(),
        Some(Value::String(s)) => s.clone(),
        Some(other) => python_repr(other),
    }
}

// -- the group tree ----------------------------------------------------------

/// Python truthiness of a cell value.
pub fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Number(n)) => n.as_f64() != Some(0.0),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// Read-only lookups over one snapshot of the groups, and the palette
/// that colors what has no color of its own.
#[derive(Clone)]
pub struct GroupTree {
    pub groups: Vec<Row>,
    pub palette: Palette,
}

impl GroupTree {
    pub fn new(groups: Vec<Row>, palette: Palette) -> GroupTree {
        GroupTree { groups, palette }
    }

    /// `by_id`: the last group with this id (Python's dict keeps the last).
    pub fn by_id(&self, id: &Value) -> Option<&Row> {
        if !truthy(Some(id)) {
            return None;
        }
        self.groups.iter().rev().find(|g| truthy(g.get("id")) && g.get("id") == Some(id))
    }

    /// `item` and the groups it's inside, nearest first, stopping at a
    /// missing group or a cycle.
    pub fn chain<'a>(&'a self, item: &'a Row) -> Vec<&'a Row> {
        let mut chain: Vec<&Row> = vec![item];
        let mut next = item.get("group_id").filter(|g| truthy(Some(g))).and_then(|g| self.by_id(g));
        while let Some(group) = next {
            // `group not in chain`: dataclass equality, field by field.
            if chain.iter().any(|c| *c == group) {
                break;
            }
            chain.push(group);
            next = group.get("group_id").filter(|g| truthy(Some(g))).and_then(|g| self.by_id(g));
        }
        chain
    }

    pub fn path(&self, item: &Row) -> String {
        let chain = self.chain(item);
        chain.iter().rev().map(|i| rows::text(i, "name").unwrap_or("")).collect::<Vec<_>>().join(" › ")
    }

    pub fn priority(&self, item: &Row) -> Option<Value> {
        self.chain(item).iter().find_map(|i| i.get("priority").filter(|p| !p.is_null()).cloned())
    }

    pub fn priority_int(&self, item: &Row) -> Option<i64> {
        self.priority(item).and_then(|p| p.as_i64())
    }

    pub fn color(&self, item: &Row) -> Value {
        let chain = self.chain(item);
        if let Some(color) = chain.iter().find_map(|i| i.get("background_color").filter(|c| truthy(Some(c)))) {
            return color.clone();
        }
        let priority = self.priority_int(item);
        json!(palette_get(&self.palette, clamp(priority)).unwrap_or(default_color(priority)))
    }

    pub fn listed(&self, group: &Row) -> Value {
        let mut out: Map<String, Value> = group.clone();
        out.insert("path".into(), json!(self.path(group)));
        out.insert("effective_color".into(), self.color(group));
        out.insert("effective_priority".into(), self.priority(group).unwrap_or(Value::Null));
        Value::Object(out)
    }

    /// Enclosing groups before those inside them, siblings in sheet order;
    /// groups inside a missing one last.
    pub fn ordered(&self) -> Vec<&Row> {
        let parent_of = |g: &Row| -> Option<Value> {
            g.get("group_id").filter(|p| self.by_id(p).is_some()).cloned()
        };
        let mut ordered: Vec<usize> = Vec::new();
        fn visit(tree: &GroupTree, index: usize, ordered: &mut Vec<usize>, parent_of: &dyn Fn(&Row) -> Option<Value>) {
            if ordered.contains(&index) {
                return;
            }
            ordered.push(index);
            let id = tree.groups[index].get("id").cloned().unwrap_or(Value::Null);
            for (i, child) in tree.groups.iter().enumerate() {
                if parent_of(child).as_ref() == Some(&id) && truthy(Some(&id)) {
                    visit(tree, i, ordered, parent_of);
                }
            }
        }
        let roots: Vec<usize> = (0..self.groups.len()).filter(|i| parent_of(&self.groups[*i]).is_none()).collect();
        for index in roots.into_iter().chain(0..self.groups.len()) {
            visit(self, index, &mut ordered, &parent_of);
        }
        ordered.into_iter().map(|i| &self.groups[i]).collect()
    }
}

/// The group with this id, or else this name (ignoring case).
pub fn find_group(groups: &[Row], id_or_name: &str) -> anyhow::Result<Row> {
    let key = |row: &Row| rows::text(row, "name").unwrap_or_default().to_lowercase();
    let by_name = |name: &str| groups.iter().rev().find(|g| key(g) == name).cloned();
    if let Some(found) = groups.iter().find(|g| rows::text(g, "id") == Some(id_or_name)) {
        return Ok(found.clone());
    }
    if let Some(found) = by_name(&id_or_name.to_lowercase()) {
        return Ok(found);
    }
    let ids: Vec<String> = dedup(groups.iter().filter_map(|g| rows::text(g, "id").filter(|i| !i.is_empty()).map(str::to_string)));
    let names: Vec<String> = dedup(groups.iter().map(key));
    let mut close: Vec<Row> = Vec::new();
    for id in crate::difflib::get_close_matches(id_or_name, &ids, 3) {
        if let Some(g) = groups.iter().rev().find(|g| rows::text(g, "id") == Some(id.as_str())) {
            close.push(g.clone());
        }
    }
    for name in crate::difflib::get_close_matches(&id_or_name.to_lowercase(), &names, 3) {
        if let Some(g) = by_name(&name) {
            close.push(g);
        }
    }
    let hints = dedup(close.iter().map(|g| format!("{} ({})", python_str(g.get("id")), python_str(g.get("name")))));
    let hint = hints.join(", ");
    bail!(
        "There's no action group with the id or name {}{} (get_action_groups lists them)",
        py::repr(id_or_name),
        if hint.is_empty() { String::new() } else { format!("; did you mean {hint}?") }
    )
}

fn is_whole_number(value: &Value) -> bool {
    value.is_i64() || value.is_u64() || value.is_boolean()
}

/// Everything wrong with the groups as a whole, as phrases.
pub fn group_problems(groups: &[Row]) -> Vec<String> {
    let mut problems = Vec::new();
    let tree = GroupTree::new(groups.to_vec(), Vec::new());
    let ids: Vec<Option<&str>> = groups.iter().map(|g| rows::text(g, "id")).collect();
    let mut repeated: Vec<&str> = ids.iter().flatten().copied().filter(|i| !i.is_empty() && ids.iter().filter(|x| **x == Some(*i)).count() > 1).collect();
    repeated.sort();
    repeated.dedup();
    for id in repeated {
        problems.push(format!("action group id {} is used more than once", py::repr(id)));
    }
    let mut names: Vec<(String, Row)> = Vec::new();
    for group in groups {
        let id = group.get("id").filter(|i| truthy(Some(i)));
        let label = match id {
            Some(id) => format!("action group {}", python_str(Some(id))),
            None => format!("action group {}", python_repr(group.get("name").unwrap_or(&Value::Null))),
        };
        if id.is_none() {
            problems.push(format!("an action group named {} has no id", python_repr(group.get("name").unwrap_or(&Value::Null))));
        }
        match group.get("name").and_then(Value::as_str).filter(|n| !n.trim().is_empty()) {
            None => problems.push(format!("{label} needs a name")),
            Some(name) if name.chars().count() > MAX_NAME_LENGTH => {
                problems.push(format!("{label}'s name {} is longer than {MAX_NAME_LENGTH} characters", py::repr(name)))
            }
            Some(name) => {
                let key = name.trim().to_lowercase();
                if let Some((_, other)) = names.iter().find(|(k, _)| *k == key) {
                    problems.push(format!(
                        "there's already an action group named {} ({}); group names must be unique (an action may share one)",
                        python_repr(other.get("name").unwrap_or(&Value::Null)),
                        python_str(other.get("id"))
                    ));
                }
                names.retain(|(k, _)| *k != key);
                names.push((key, group.clone()));
            }
        }
        if let Some(parent) = group.get("group_id").filter(|p| !p.is_null()) {
            match tree.by_id(parent) {
                None => problems.push(format!("{label} is inside {}, which isn't an action group", python_repr(parent))),
                Some(parent_group) => {
                    if tree.chain(parent_group).iter().any(|g| g.get("id") == group.get("id")) {
                        problems.push(format!("{label} can't be inside itself, directly or through other groups"));
                    }
                }
            }
        }
        if let Some(priority) = group.get("priority").filter(|p| !p.is_null()) {
            if !is_whole_number(priority) {
                problems.push(format!("{label}'s priority must be a whole number"));
            }
        }
    }
    problems
}
