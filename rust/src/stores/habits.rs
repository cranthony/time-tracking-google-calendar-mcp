//! Habits: what the user wants to do well, each scoped to one action or
//! action group and rated by traits as a person is, in the Habits tab.

use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::contract::{wire_text, RowItem, Schema};
use crate::sheet::rows::{self, RowSheet, RowType};
use crate::sheet::HABITS;
use crate::stores::cancellations::CancelledEvent;
use crate::stores::foundations::{self, foundation_problems, Node, NodesFn, OnGraph};
use crate::stores::actions::{Action, ActionStatus};
use crate::stores::groups::{repr_or_none, set, str_or_none, GroupTree};
use crate::stores::locations::dedup;
use crate::stores::people::{Status, TraitIds};
use crate::stores::traits::{person_traits_problems, settled_spec, spec_with_new_part_ids};
use anyhow::bail;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::sync::Arc;

pub const HABIT_STATUSES: [&str; 3] = ["active", "archived", "deleted"];
pub const DEFAULT_STATUSES: [&str; 1] = ["active"];
pub const MAX_NAME_LENGTH: usize = 100;
pub const CLEARABLE_FIELDS: [&str; 3] = ["note", "traits", "foundations"];
pub const SUBJECT_PREFIX: &str = "habit:";

// -- the types -------------------------------------------------------------------

/// A habit as the habit tools return it: plus its action's or
/// group's path.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct ListedHabit {
    pub id: Option<String>,
    pub name: Option<String>,
    pub action_id: Option<String>,
    pub status: Option<Status>,
    pub note: Option<String>,
    pub traits: Option<Map<String, Value>>,
    pub foundations: Option<Vec<String>>,
    pub action_path: Option<String>,
    pub cancelled_events: Option<Vec<CancelledEvent>>,
    pub version: Option<i64>,
}

/// One habit: what the user wants to do well, tied to an action or
/// action group (see get_habits).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema, RowItem)]
pub struct Habit {
    pub id: Option<String>,
    pub name: Option<String>,
    pub action_id: Option<String>,
    #[row(text)]
    pub status: Option<Status>,
    pub note: Option<String>,
    pub traits: Option<Map<String, Value>>,
    pub foundations: Option<Vec<String>>,
    /// Goes up by one each time it changes. Give it to an update (and to a delete, as if_version): it's the version you last read, required -- one saved before versions is version 0 -- changed since, the change is refused; on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK.
    pub version: Option<i64>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct CreatedHabit {
    pub habit: ListedHabit,
    pub created_id: String,
}

impl OnGraph for Habit {
    fn foundations_mut(&mut self) -> &mut Option<Vec<String>> {
        &mut self.foundations
    }
}

pub const HABIT_ROW: RowType = <Habit as rows::RowItem>::ROW_TYPE;

/// Every action's and group's id with its path: what a habit's action_id
/// may name (not a deleted action).
pub type Scopes = Vec<(String, String)>;
pub type ScopesFn = Arc<dyn Fn() -> anyhow::Result<Scopes> + Send + Sync>;

pub fn action_scopes(actions: &[Action], groups: &GroupTree) -> Scopes {
    let mut scopes: Scopes = Vec::new();
    let mut put = |id: String, path: String| {
        if let Some(slot) = scopes.iter_mut().find(|(k, _)| *k == id) {
            slot.1 = path;
        } else {
            scopes.push((id, path));
        }
    };
    for g in &groups.groups {
        if let Some(id) = set(g.id.as_deref()) {
            put(id.to_string(), groups.path(g));
        }
    }
    for a in actions {
        if let Some(id) = set(a.id.as_deref()) {
            if a.status != Some(ActionStatus::Deleted) {
                put(id.to_string(), groups.path(a));
            }
        }
    }
    scopes
}

pub fn subject_id(habit_id: &str) -> String {
    format!("{SUBJECT_PREFIX}{habit_id}")
}

/// A habit's status as its text ("active"), if it has one.
fn status_of(habit: &Habit) -> Option<String> {
    habit.status.as_ref().map(wire_text)
}

#[derive(Clone)]
pub struct Habits {
    sheet: RowSheet,
    scopes: Option<ScopesFn>,
    trait_ids: Option<TraitIds>,
    people: Option<NodesFn>,
}

impl Habits {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str, scopes: Option<ScopesFn>, trait_ids: Option<TraitIds>) -> anyhow::Result<Habits> {
        Ok(Habits { sheet: RowSheet::ensure(sheets, spreadsheet_id, HABITS.0, HABITS.1, HABIT_ROW, &["id", "name"])?, scopes, trait_ids, people: None })
    }

    /// It, checking foundations against `people`'s nodes too: the people
    /// on the habit graph.
    pub fn with_people(mut self, people: NodesFn) -> Habits {
        self.people = Some(people);
        self
    }

    /// Every habit as a node of the habit graph.
    pub fn nodes(&self) -> anyhow::Result<Vec<Node>> {
        Ok(habit_nodes(&self.all()?))
    }

    /// Takes `subject` (a habit's or a person's, deleted) out of every
    /// habit's foundations.
    pub fn drop_foundation(&self, subject: &str) -> anyhow::Result<()> {
        let mut habits = self.all()?;
        if foundations::without(&mut habits, subject) {
            self.sheet.write_items(&habits)?;
        }
        Ok(())
    }

    pub fn whole_tab(&self) -> TabRange {
        self.sheet.whole_tab()
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheet.prefetch(ranges)
    }

    /// Every habit, its traits settled (see traits::settled_spec).
    pub fn all(&self) -> anyhow::Result<Vec<Habit>> {
        Ok(self.sheet.read_items::<Habit>()?.into_iter().map(|h| Habit { traits: h.traits.clone().map(settled_spec), ..h }).collect())
    }

    pub fn get_habits(&self, statuses: Option<&[String]>) -> anyhow::Result<Vec<ListedHabit>> {
        let statuses = check_statuses(statuses)?;
        let paths = self.paths()?;
        Ok(self.all()?.iter().filter(|h| statuses.iter().any(|s| Some(s) == status_of(h).as_ref())).map(|h| listed(h, &paths)).collect())
    }

    pub fn get_habit(&self, id_or_name: &str) -> anyhow::Result<ListedHabit> {
        let habit = find(&self.all()?, id_or_name)?;
        Ok(listed(&habit, &self.paths()?))
    }

    pub fn create_habit(&self, habit: &Habit) -> anyhow::Result<CreatedHabit> {
        let habits = self.all()?;
        let id = match rows::created(habit, &habits, &[])? {
            rows::Created::New(id) => id,
            rows::Created::Again(made) => {
                return Ok(CreatedHabit { habit: listed(&made, &self.paths()?), created_id: made.id.clone().unwrap_or_default() })
            }
        };
        let traits = habit.traits.as_ref().map(spec_with_new_part_ids);
        let new =
            Habit { id: Some(id.clone()), status: Some(habit.status.unwrap_or(Status::Active)), traits, version: None, ..habit.clone() };
        let mut all = habits.clone();
        all.push(new.clone());
        self.write(&all, &id)?;
        Ok(CreatedHabit { habit: listed(&new, &self.paths()?), created_id: id })
    }

    pub fn update_habit(&self, habit: &Habit, clear_fields: &[String]) -> anyhow::Result<ListedHabit> {
        let Some(id) = set(habit.id.as_deref()).map(str::to_string) else {
            bail!("update_habit needs the habit's id");
        };
        rows::check_clear_item(habit, clear_fields, &CLEARABLE_FIELDS)?;
        let mut habits = self.all()?;
        let Some(index) = habits.iter().position(|h| h.id.as_deref() == Some(id.as_str())) else {
            rows::expect_item_version::<Habit>(&id, None, habit.version)?;
            find(&habits, &id)?;
            unreachable!()
        };
        let was_deleted = habits[index].status == Some(Status::Deleted);
        let given = Habit { traits: habit.traits.as_ref().map(spec_with_new_part_ids), ..habit.clone() };
        habits[index] = rows::updated_item(&habits[index], &given, clear_fields, &["id"])?;
        if !was_deleted && habits[index].status == Some(Status::Deleted) {
            foundations::without(&mut habits, &subject_id(&id));
        }
        self.write(&habits, &id)?;
        Ok(listed(&habits[index], &self.paths()?))
    }

    fn write(&self, habits: &[Habit], changed: &str) -> anyhow::Result<()> {
        let scopes = match &self.scopes {
            Some(f) => Some(f()?),
            None => None,
        };
        let trait_ids = match &self.trait_ids {
            Some(f) if habits.iter().any(|h| h.traits.is_some()) => Some(f()?),
            _ => None,
        };
        let mut problems = habit_problems(habits, scopes.as_ref(), trait_ids.as_deref(), Some(changed));
        if let Some(habit) = habits.iter().find(|h| h.id.as_deref() == Some(changed)) {
            if let Some(given) = &habit.foundations {
                let mut nodes = habit_nodes(habits);
                if let Some(people) = &self.people {
                    nodes.extend(people()?);
                }
                for p in foundation_problems(&subject_id(changed), &json!(given), &nodes) {
                    problems.push(format!("habit {changed}'s {p}"));
                }
            }
        }
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        self.sheet.write_items(habits)
    }

    fn paths(&self) -> anyhow::Result<Scopes> {
        match &self.scopes {
            Some(f) => f(),
            None => Ok(Vec::new()),
        }
    }
}

fn habit_nodes(habits: &[Habit]) -> Vec<Node> {
    habits
        .iter()
        .filter_map(|h| {
            let id = set(h.id.as_deref())?;
            Some(Node { subject: subject_id(id), deleted: h.status == Some(Status::Deleted), foundations: h.foundations.clone().unwrap_or_default() })
        })
        .collect()
}


pub fn find(habits: &[Habit], id_or_name: &str) -> anyhow::Result<Habit> {
    if let Some(h) = habits.iter().find(|h| h.id.as_deref() == Some(id_or_name)) {
        return Ok(h.clone());
    }
    let key = |h: &Habit| h.name.as_deref().unwrap_or_default().to_lowercase();
    let wanted = id_or_name.to_lowercase();
    if let Some(h) = habits.iter().rev().find(|h| key(h) == wanted) {
        return Ok(h.clone());
    }
    let ids = dedup(habits.iter().filter_map(|h| set(h.id.as_deref()).map(str::to_string)));
    let names = dedup(habits.iter().map(key));
    let mut close: Vec<&Habit> = Vec::new();
    for id in crate::difflib::get_close_matches(id_or_name, &ids, 3) {
        if let Some(h) = habits.iter().rev().find(|h| h.id.as_deref() == Some(id.as_str())) {
            close.push(h);
        }
    }
    for name in crate::difflib::get_close_matches(&wanted, &names, 3) {
        if let Some(h) = habits.iter().rev().find(|h| key(h) == name) {
            close.push(h);
        }
    }
    let hints = dedup(close.iter().map(|h| format!("{} ({})", str_or_none(h.id.as_deref()), str_or_none(h.name.as_deref()))));
    let hint = hints.join(", ");
    bail!(
        "There's no habit with the id or name {}{} (get_habits lists them)",
        py::repr(id_or_name),
        if hint.is_empty() { String::new() } else { format!("; did you mean {hint}?") }
    )
}

pub fn habit_problems(habits: &[Habit], scopes: Option<&Scopes>, trait_ids: Option<&[String]>, changed: Option<&str>) -> Vec<String> {
    let mut problems = Vec::new();
    let ids: Vec<Option<&str>> = habits.iter().map(|h| h.id.as_deref()).collect();
    let mut repeated: Vec<&str> =
        ids.iter().flatten().copied().filter(|i| !i.is_empty() && ids.iter().filter(|x| **x == Some(*i)).count() > 1).collect();
    repeated.sort();
    repeated.dedup();
    for id in repeated {
        problems.push(format!("habit id {} is used more than once", py::repr(id)));
    }
    let mut names: Vec<(String, &Habit)> = Vec::new();
    for habit in habits {
        let label = match set(habit.id.as_deref()) {
            Some(id) => format!("habit {id}"),
            None => format!("habit {}", repr_or_none(habit.name.as_deref())),
        };
        if set(habit.id.as_deref()).is_none() {
            problems.push(format!("a habit named {} has no id", repr_or_none(habit.name.as_deref())));
        }
        match habit.name.as_deref().filter(|n| !n.trim().is_empty()) {
            None => problems.push(format!("{label} needs a name")),
            Some(n) if n.chars().count() > MAX_NAME_LENGTH => problems.push(format!("{label}'s name is longer than {MAX_NAME_LENGTH} characters")),
            Some(n) => {
                let key = n.trim().to_lowercase();
                if let Some((_, other)) = names.iter().find(|(k, _)| *k == key) {
                    problems.push(format!(
                        "there's already a habit named {} ({}); habit names must be unique -- use that one, or pick another name",
                        repr_or_none(other.name.as_deref()),
                        str_or_none(other.id.as_deref())
                    ));
                }
                names.retain(|(k, _)| *k != key);
                names.push((key, habit));
            }
        }
        match set(habit.action_id.as_deref()) {
            None => problems.push(format!("{label} needs an action_id: the action, or the action group, it's about")),
            Some(action_id) => {
                let applies = changed.is_none() || habit.id.as_deref() == changed;
                if let Some(scopes) = scopes {
                    if applies && !scopes.iter().any(|(k, _)| k == action_id) {
                        problems.push(format!(
                            "{label}'s action_id {} isn't an action or an action group (get_actions and get_action_groups list them)",
                            py::repr(action_id)
                        ));
                    }
                }
            }
        }
        if habit.status.is_none() {
            problems.push(format!("{label}'s status must be one of {}", HABIT_STATUSES.join(", ")));
        }
        if let Some(traits) = &habit.traits {
            for p in person_traits_problems(&Value::Object(traits.clone()), trait_ids) {
                problems.push(format!("{label}'s traits {p}"));
            }
        }
    }
    problems
}

pub fn listed(habit: &Habit, paths: &Scopes) -> ListedHabit {
    let action_path = set(habit.action_id.as_deref()).and_then(|a| paths.iter().find(|(k, _)| k == a).map(|(_, p)| p.clone()));
    ListedHabit {
        id: habit.id.clone(),
        name: habit.name.clone(),
        action_id: habit.action_id.clone(),
        status: habit.status,
        note: habit.note.clone(),
        traits: habit.traits.clone(),
        foundations: habit.foundations.clone(),
        action_path,
        cancelled_events: None,
        version: habit.version,
    }
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

// Written by the app: made once by their ids and versions.
impl rows::Versioned for Habit {}

#[cfg(test)]
#[path = "habits_tests.rs"]
mod tests;
