//! Action groups and the priority palette: what actions inherit their color
//! and priority from.

use crate::google::calendar::EventLabel;
use crate::py;
use crate::sheet::rows::{self, RowType};
use crate::stores::actions::{ActionGroup, ListedActionGroup};
use crate::stores::locations::dedup;
use anyhow::bail;
use regex::Regex;
use serde_json::Value;
use std::sync::LazyLock;

pub const GROUP_ROW: RowType = <ActionGroup as rows::RowItem>::ROW_TYPE;
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
pub fn color_problem(color: &str) -> Option<String> {
    if COLOR.is_match(color) {
        None
    } else {
        Some(format!("{} isn't a color: give one as #rrggbb, e.g. \"#a4bdfc\"", py::repr(color)))
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

/// What the group tree needs of an item placed in it: an action, or a
/// group.
pub trait Placed {
    fn id(&self) -> Option<&str>;
    fn group_id(&self) -> Option<&str>;
    fn name(&self) -> Option<&str>;
    fn background_color(&self) -> Option<&str>;
    fn priority(&self) -> Option<i64>;
    /// The item, if it's a group.
    fn as_group(&self) -> Option<&ActionGroup> {
        None
    }
}

impl Placed for ActionGroup {
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
    fn as_group(&self) -> Option<&ActionGroup> {
        Some(self)
    }
}

/// Text that's set: Python's truthiness of a cell.
pub fn set(text: Option<&str>) -> Option<&str> {
    text.filter(|t| !t.is_empty())
}

/// `str()` of an optional text, as Python prints it.
pub fn str_or_none(text: Option<&str>) -> String {
    text.unwrap_or("None").to_string()
}

/// `repr()` of an optional text, as Python prints it.
pub fn repr_or_none(text: Option<&str>) -> String {
    text.map(py::repr).unwrap_or_else(|| "None".into())
}

/// Read-only lookups over one snapshot of the groups, and the palette
/// that colors what has no color of its own.
#[derive(Clone)]
pub struct GroupTree {
    pub groups: Vec<ActionGroup>,
    pub palette: Palette,
}

impl GroupTree {
    pub fn new(groups: Vec<ActionGroup>, palette: Palette) -> GroupTree {
        GroupTree { groups, palette }
    }

    /// `by_id`: the last group with this id (Python's dict keeps the last).
    pub fn by_id(&self, id: &str) -> Option<&ActionGroup> {
        if id.is_empty() {
            return None;
        }
        self.groups.iter().rev().find(|g| set(g.id.as_deref()) == Some(id))
    }

    /// The groups `item` is inside, nearest first, stopping at a missing
    /// group or a cycle.
    pub fn enclosing<'a>(&'a self, item: &'a dyn Placed) -> Vec<&'a ActionGroup> {
        let mut found: Vec<&ActionGroup> = Vec::new();
        let mut next = set(item.group_id()).and_then(|g| self.by_id(g));
        while let Some(group) = next {
            // `group not in chain`: dataclass equality, field by field.
            if item.as_group() == Some(group) || found.contains(&group) {
                break;
            }
            found.push(group);
            next = set(group.group_id.as_deref()).and_then(|g| self.by_id(g));
        }
        found
    }

    pub fn path(&self, item: &dyn Placed) -> String {
        let mut names: Vec<&str> = vec![item.name().unwrap_or("")];
        names.extend(self.enclosing(item).iter().map(|g| g.name.as_deref().unwrap_or("")));
        names.reverse();
        names.join(" › ")
    }

    pub fn priority(&self, item: &dyn Placed) -> Option<i64> {
        item.priority().or_else(|| self.enclosing(item).iter().find_map(|g| g.priority))
    }

    pub fn color(&self, item: &dyn Placed) -> String {
        if let Some(color) = set(item.background_color()) {
            return color.to_string();
        }
        if let Some(color) = self.enclosing(item).iter().find_map(|g| set(g.background_color.as_deref())) {
            return color.to_string();
        }
        let priority = self.priority(item);
        palette_get(&self.palette, clamp(priority)).unwrap_or(default_color(priority)).to_string()
    }

    pub fn listed(&self, group: &ActionGroup) -> ListedActionGroup {
        ListedActionGroup {
            id: group.id.clone(),
            group_id: group.group_id.clone(),
            name: group.name.clone(),
            background_color: group.background_color.clone(),
            priority: group.priority,
            note: group.note.clone(),
            path: Some(self.path(group)),
            effective_color: Some(self.color(group)),
            effective_priority: self.priority(group),
            version: group.version,
        }
    }

    /// Enclosing groups before those inside them, siblings in sheet order;
    /// groups inside a missing one last.
    pub fn ordered(&self) -> Vec<&ActionGroup> {
        let parent_of = |g: &ActionGroup| -> Option<String> { g.group_id.clone().filter(|p| self.by_id(p).is_some()) };
        let mut ordered: Vec<usize> = Vec::new();
        fn visit(tree: &GroupTree, index: usize, ordered: &mut Vec<usize>, parent_of: &dyn Fn(&ActionGroup) -> Option<String>) {
            if ordered.contains(&index) {
                return;
            }
            ordered.push(index);
            let Some(id) = set(tree.groups[index].id.as_deref()) else { return };
            for (i, child) in tree.groups.iter().enumerate() {
                if parent_of(child).as_deref() == Some(id) {
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
pub fn find_group(groups: &[ActionGroup], id_or_name: &str) -> anyhow::Result<ActionGroup> {
    let key = |g: &ActionGroup| g.name.as_deref().unwrap_or_default().to_lowercase();
    let by_name = |name: &str| groups.iter().rev().find(|g| key(g) == name).cloned();
    if let Some(found) = groups.iter().find(|g| g.id.as_deref() == Some(id_or_name)) {
        return Ok(found.clone());
    }
    if let Some(found) = by_name(&id_or_name.to_lowercase()) {
        return Ok(found);
    }
    let ids: Vec<String> = dedup(groups.iter().filter_map(|g| set(g.id.as_deref()).map(str::to_string)));
    let names: Vec<String> = dedup(groups.iter().map(key));
    let mut close: Vec<ActionGroup> = Vec::new();
    for id in crate::difflib::get_close_matches(id_or_name, &ids, 3) {
        if let Some(g) = groups.iter().rev().find(|g| g.id.as_deref() == Some(id.as_str())) {
            close.push(g.clone());
        }
    }
    for name in crate::difflib::get_close_matches(&id_or_name.to_lowercase(), &names, 3) {
        if let Some(g) = by_name(&name) {
            close.push(g);
        }
    }
    let hints = dedup(close.iter().map(|g| format!("{} ({})", str_or_none(g.id.as_deref()), str_or_none(g.name.as_deref()))));
    let hint = hints.join(", ");
    bail!(
        "There's no action group with the id or name {}{} (get_action_groups lists them)",
        py::repr(id_or_name),
        if hint.is_empty() { String::new() } else { format!("; did you mean {hint}?") }
    )
}

/// Everything wrong with the groups as a whole, as phrases.
pub fn group_problems(groups: &[ActionGroup]) -> Vec<String> {
    let mut problems = Vec::new();
    let tree = GroupTree::new(groups.to_vec(), Vec::new());
    let ids: Vec<Option<&str>> = groups.iter().map(|g| g.id.as_deref()).collect();
    let mut repeated: Vec<&str> =
        ids.iter().flatten().copied().filter(|i| !i.is_empty() && ids.iter().filter(|x| **x == Some(*i)).count() > 1).collect();
    repeated.sort();
    repeated.dedup();
    for id in repeated {
        problems.push(format!("action group id {} is used more than once", py::repr(id)));
    }
    let mut names: Vec<(String, &ActionGroup)> = Vec::new();
    for group in groups {
        let id = set(group.id.as_deref());
        let label = match id {
            Some(id) => format!("action group {id}"),
            None => format!("action group {}", repr_or_none(group.name.as_deref())),
        };
        if id.is_none() {
            problems.push(format!("an action group named {} has no id", repr_or_none(group.name.as_deref())));
        }
        match group.name.as_deref().filter(|n| !n.trim().is_empty()) {
            None => problems.push(format!("{label} needs a name")),
            Some(name) if name.chars().count() > MAX_NAME_LENGTH => {
                problems.push(format!("{label}'s name {} is longer than {MAX_NAME_LENGTH} characters", py::repr(name)))
            }
            Some(name) => {
                let key = name.trim().to_lowercase();
                if let Some((_, other)) = names.iter().find(|(k, _)| *k == key) {
                    problems.push(format!(
                        "there's already an action group named {} ({}); group names must be unique (an action may share one)",
                        repr_or_none(other.name.as_deref()),
                        str_or_none(other.id.as_deref())
                    ));
                }
                names.retain(|(k, _)| *k != key);
                names.push((key, group));
            }
        }
        if let Some(parent) = group.group_id.as_deref() {
            match tree.by_id(parent) {
                None => problems.push(format!("{label} is inside {}, which isn't an action group", py::repr(parent))),
                Some(parent_group) => {
                    // Its parent, or a group its parent is inside, is it.
                    if parent_group.id == group.id || tree.enclosing(parent_group).iter().any(|g| g.id == group.id) {
                        problems.push(format!("{label} can't be inside itself, directly or through other groups"));
                    }
                }
            }
        }
    }
    problems
}
