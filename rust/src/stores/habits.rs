//! Habits (utilities/habits.py): what the user wants to do well, each
//! scoped to one action or action group and rated by traits as a person
//! is, in the Habits tab.

use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::sheet::rows::{self, Kind, Row, RowSheet, RowType};
use crate::sheet::HABITS;
use crate::stores::groups::{python_repr, python_str, truthy, GroupTree};
use crate::stores::locations::dedup;
use crate::stores::people::TraitIds;
use crate::stores::traits::person_traits_problems;
use anyhow::bail;
use serde_json::{json, Map, Value};
use std::sync::Arc;

pub const HABIT_STATUSES: [&str; 3] = ["active", "archived", "deleted"];
pub const DEFAULT_STATUSES: [&str; 1] = ["active"];
pub const MAX_NAME_LENGTH: usize = 100;
pub const CLEARABLE_FIELDS: [&str; 2] = ["note", "traits"];
pub const SUBJECT_PREFIX: &str = "habit:";

pub const HABIT_ROW: RowType = RowType {
    name: "Habit",
    fields: &[
        ("id", Kind::Str),
        ("name", Kind::Str),
        ("action_id", Kind::Str),
        ("status", Kind::Str),
        ("note", Kind::Str),
        ("traits", Kind::Json),
    ],
};

pub fn subject_id(habit_id: &str) -> String {
    format!("{SUBJECT_PREFIX}{habit_id}")
}

/// Every action's and group's id with its path: what a habit's action_id
/// may name (not a deleted action).
pub type Scopes = Vec<(String, String)>;
pub type ScopesFn = Arc<dyn Fn() -> anyhow::Result<Scopes> + Send + Sync>;

pub fn action_scopes(actions: &[Row], groups: &GroupTree) -> Scopes {
    let mut scopes: Scopes = Vec::new();
    let mut put = |id: String, path: String| {
        if let Some(slot) = scopes.iter_mut().find(|(k, _)| *k == id) {
            slot.1 = path;
        } else {
            scopes.push((id, path));
        }
    };
    for g in &groups.groups {
        if let Some(id) = g.get("id").filter(|i| truthy(Some(i))) {
            put(python_str(Some(id)), groups.path(g));
        }
    }
    for a in actions {
        if let Some(id) = a.get("id").filter(|i| truthy(Some(i))) {
            if rows::text(a, "status") != Some("deleted") {
                put(python_str(Some(id)), groups.path(a));
            }
        }
    }
    scopes
}

#[derive(Clone)]
pub struct Habits {
    sheet: RowSheet,
    scopes: Option<ScopesFn>,
    trait_ids: Option<TraitIds>,
}

impl Habits {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str, scopes: Option<ScopesFn>, trait_ids: Option<TraitIds>) -> anyhow::Result<Habits> {
        Ok(Habits { sheet: RowSheet::ensure(sheets, spreadsheet_id, HABITS.0, HABITS.1, HABIT_ROW, &["id", "name"])?, scopes, trait_ids })
    }

    pub fn whole_tab(&self) -> TabRange {
        self.sheet.whole_tab()
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheet.prefetch(ranges)
    }

    pub fn all(&self) -> anyhow::Result<Vec<Row>> {
        self.sheet.read()
    }

    pub fn get_habits(&self, statuses: Option<&[String]>) -> anyhow::Result<Vec<Map<String, Value>>> {
        let statuses = check_statuses(statuses)?;
        let paths = self.paths()?;
        Ok(self
            .all()?
            .iter()
            .filter(|h| statuses.iter().any(|s| rows::text(h, "status") == Some(s.as_str())))
            .map(|h| listed(h, &paths))
            .collect())
    }

    pub fn get_habit(&self, id_or_name: &str) -> anyhow::Result<Map<String, Value>> {
        let habit = find(&self.all()?, id_or_name)?;
        Ok(listed(&habit, &self.paths()?))
    }

    pub fn create_habit(&self, habit: &Row) -> anyhow::Result<Value> {
        let habits = self.all()?;
        let mut new = habit.clone();
        let id = rows::new_id(habits.iter().map(|h| rows::text(h, "id")).collect::<Vec<_>>());
        new.insert("id".into(), json!(id));
        if !truthy(habit.get("status")) {
            new.insert("status".into(), json!("active"));
        }
        let mut all = habits.clone();
        all.push(new.clone());
        self.write(&all, &id)?;
        Ok(json!({ "habit": Value::Object(listed(&new, &self.paths()?)), "created_id": id }))
    }

    pub fn update_habit(&self, habit: &Row, clear_fields: &[String]) -> anyhow::Result<Map<String, Value>> {
        let Some(id) = rows::text(habit, "id").filter(|i| !i.is_empty()).map(str::to_string) else {
            bail!("update_habit needs the habit's id");
        };
        rows::check_clear(habit, clear_fields, &CLEARABLE_FIELDS)?;
        let mut habits = self.all()?;
        let Some(index) = habits.iter().position(|h| rows::text(h, "id") == Some(id.as_str())) else {
            find(&habits, &id)?;
            unreachable!()
        };
        habits[index] = rows::updated(&habits[index], habit, clear_fields, &["id"]);
        self.write(&habits, &id)?;
        Ok(listed(&habits[index], &self.paths()?))
    }

    fn write(&self, habits: &[Row], changed: &str) -> anyhow::Result<()> {
        let scopes = match &self.scopes {
            Some(f) => Some(f()?),
            None => None,
        };
        let trait_ids = match &self.trait_ids {
            Some(f) if habits.iter().any(|h| rows::is_set(h, "traits")) => Some(f()?),
            _ => None,
        };
        let problems = habit_problems(habits, scopes.as_ref(), trait_ids.as_deref(), Some(changed));
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        self.sheet.write(habits)
    }

    fn paths(&self) -> anyhow::Result<Scopes> {
        match &self.scopes {
            Some(f) => f(),
            None => Ok(Vec::new()),
        }
    }
}

pub fn find(habits: &[Row], id_or_name: &str) -> anyhow::Result<Row> {
    if let Some(h) = habits.iter().find(|h| rows::text(h, "id") == Some(id_or_name)) {
        return Ok(h.clone());
    }
    let key = |row: &Row| rows::text(row, "name").unwrap_or_default().to_lowercase();
    let wanted = id_or_name.to_lowercase();
    if let Some(h) = habits.iter().rev().find(|h| key(h) == wanted) {
        return Ok(h.clone());
    }
    let ids = dedup(habits.iter().filter(|h| truthy(h.get("id"))).filter_map(|h| rows::text(h, "id").map(str::to_string)));
    let names = dedup(habits.iter().map(key));
    let mut close: Vec<&Row> = Vec::new();
    for id in crate::difflib::get_close_matches(id_or_name, &ids, 3) {
        if let Some(h) = habits.iter().rev().find(|h| rows::text(h, "id") == Some(id.as_str())) {
            close.push(h);
        }
    }
    for name in crate::difflib::get_close_matches(&wanted, &names, 3) {
        if let Some(h) = habits.iter().rev().find(|h| key(h) == name) {
            close.push(h);
        }
    }
    let hints = dedup(close.iter().map(|h| format!("{} ({})", python_str(h.get("id")), python_str(h.get("name")))));
    let hint = hints.join(", ");
    bail!(
        "There's no habit with the id or name {}{} (get_habits lists them)",
        py::repr(id_or_name),
        if hint.is_empty() { String::new() } else { format!("; did you mean {hint}?") }
    )
}

pub fn habit_problems(habits: &[Row], scopes: Option<&Scopes>, trait_ids: Option<&[String]>, changed: Option<&str>) -> Vec<String> {
    let mut problems = Vec::new();
    let ids: Vec<Option<&str>> = habits.iter().map(|h| rows::text(h, "id")).collect();
    let mut repeated: Vec<&str> =
        ids.iter().flatten().copied().filter(|i| !i.is_empty() && ids.iter().filter(|x| **x == Some(*i)).count() > 1).collect();
    repeated.sort();
    repeated.dedup();
    for id in repeated {
        problems.push(format!("habit id {} is used more than once", py::repr(id)));
    }
    let mut names: Vec<(String, Row)> = Vec::new();
    for habit in habits {
        let label = match habit.get("id").filter(|i| truthy(Some(i))) {
            Some(id) => format!("habit {}", python_str(Some(id))),
            None => format!("habit {}", python_repr(habit.get("name").unwrap_or(&Value::Null))),
        };
        if !truthy(habit.get("id")) {
            problems.push(format!("a habit named {} has no id", python_repr(habit.get("name").unwrap_or(&Value::Null))));
        }
        match habit.get("name").and_then(Value::as_str).filter(|n| !n.trim().is_empty()) {
            None => problems.push(format!("{label} needs a name")),
            Some(n) if n.chars().count() > MAX_NAME_LENGTH => problems.push(format!("{label}'s name is longer than {MAX_NAME_LENGTH} characters")),
            Some(n) => {
                let key = n.trim().to_lowercase();
                if let Some((_, other)) = names.iter().find(|(k, _)| *k == key) {
                    problems.push(format!(
                        "there's already a habit named {} ({}); habit names must be unique -- use that one, or pick another name",
                        python_repr(other.get("name").unwrap_or(&Value::Null)),
                        python_str(other.get("id"))
                    ));
                }
                names.retain(|(k, _)| *k != key);
                names.push((key, habit.clone()));
            }
        }
        match habit.get("action_id").filter(|a| truthy(Some(a))) {
            None => problems.push(format!("{label} needs an action_id: the action, or the action group, it's about")),
            Some(action_id) => {
                let applies = changed.is_none() || rows::text(habit, "id") == changed;
                if let Some(scopes) = scopes {
                    if applies && !scopes.iter().any(|(k, _)| Some(k.as_str()) == action_id.as_str()) {
                        problems.push(format!(
                            "{label}'s action_id {} isn't an action or an action group (get_actions and get_action_groups list them)",
                            python_repr(action_id)
                        ));
                    }
                }
            }
        }
        if !HABIT_STATUSES.iter().any(|s| rows::text(habit, "status") == Some(*s)) {
            problems.push(format!("{label}'s status must be one of {}", HABIT_STATUSES.join(", ")));
        }
        if let Some(traits) = habit.get("traits").filter(|t| !t.is_null()) {
            for p in person_traits_problems(traits, trait_ids) {
                problems.push(format!("{label}'s traits {p}"));
            }
        }
    }
    problems
}

pub fn listed(habit: &Row, paths: &Scopes) -> Map<String, Value> {
    let mut out: Map<String, Value> = Map::new();
    for (name, _) in HABIT_ROW.fields {
        out.insert(name.to_string(), habit.get(*name).cloned().unwrap_or(Value::Null));
    }
    let path = habit
        .get("action_id")
        .filter(|a| truthy(Some(a)))
        .and_then(|a| paths.iter().find(|(k, _)| Some(k.as_str()) == a.as_str()).map(|(_, p)| json!(p)));
    out.insert("action_path".into(), path.unwrap_or(Value::Null));
    out.insert("cancelled_events".into(), Value::Null);
    out
}

fn check_statuses(statuses: Option<&[String]>) -> anyhow::Result<Vec<String>> {
    match statuses {
        None => Ok(DEFAULT_STATUSES.iter().map(|s| s.to_string()).collect()),
        Some(given) => {
            let mut unknown: Vec<&String> = given.iter().filter(|s| !HABIT_STATUSES.contains(&s.as_str())).collect();
            unknown.sort();
            unknown.dedup();
            if !unknown.is_empty() {
                bail!("Unknown habit status(es) {}; statuses are {}", py::repr_list(&unknown), HABIT_STATUSES.join(", "));
            }
            Ok(given.to_vec())
        }
    }
}

#[cfg(test)]
#[path = "habits_tests.rs"]
mod tests;
