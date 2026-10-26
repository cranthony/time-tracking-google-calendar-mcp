//! Habits: what the user wants to do well, each scoped to one action or
//! action group and rated by traits as a person is, in the Habits tab --
//! or a person's or circle's habit: some of their traits, as they're
//! scored, on the habit graph -- or a recurring goal's: a goal for each
//! day, week or month (see habit_goals).

use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::contract::{wire_text, RowItem, Schema};
use crate::sheet::rows::{self, RowSheet, RowType};
use crate::sheet::HABITS;
use crate::stores::cancellations::CancelledEvent;
use crate::stores::foundations::{self, foundation_problems, Node, OnGraph};
use crate::stores::actions::{Action, ActionStatus};
use crate::stores::groups::{repr_or_none, set, str_or_none, GroupTree};
use crate::stores::habit_goals::{self, RecurringGoal, Template};
use crate::stores::locations::dedup;
use crate::stores::people::{Status, TraitIds};
use crate::stores::traits::{person_traits_problems, settled_spec, spec_with_new_part_ids, SpecOf};
use anyhow::bail;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::sync::Arc;

pub const HABIT_STATUSES: [&str; 3] = ["active", "archived", "deleted"];
pub const DEFAULT_STATUSES: [&str; 1] = ["active"];
pub const MAX_NAME_LENGTH: usize = 100;
pub const CLEARABLE_FIELDS: [&str; 4] = ["name", "note", "traits", "foundations"];
pub const SUBJECT_PREFIX: &str = "habit:";

// -- the types -------------------------------------------------------------------

/// A habit as the habit tools return it: plus its action's or
/// group's path.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct ListedHabit {
    pub id: Option<String>,
    pub name: Option<String>,
    pub action_id: Option<String>,
    pub person_id: Option<String>,
    pub circle_id: Option<String>,
    pub recurring_goal: Option<RecurringGoal>,
    pub status: Option<Status>,
    pub note: Option<String>,
    pub traits: Option<Map<String, Value>>,
    pub foundations: Option<Vec<String>>,
    pub action_path: Option<String>,
    /// For a person's or circle's habit, the name it has without its own
    /// (see create_habit): its name, unless it has its own.
    pub default_name: Option<String>,
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
    /// For a person's habit, in place of action_id: theirs.
    pub person_id: Option<String>,
    /// For a circle's habit, in place of action_id: its.
    pub circle_id: Option<String>,
    /// For a recurring goal's habit, in place of action_id: the goal made
    /// for each of its periods (see RecurringGoal).
    pub recurring_goal: Option<RecurringGoal>,
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

/// What a person's or circle's habit may be about, and is named by: each
/// person's, circle's and trait's id with its name (people and circles
/// not deleted).
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Targets {
    pub people: Vec<(String, String)>,
    pub circles: Vec<(String, String)>,
    pub traits: Vec<(String, String)>,
}
pub type TargetsFn = Arc<dyn Fn() -> anyhow::Result<Targets> + Send + Sync>;

/// The most traits a habit's name lists before "+N".
const NAMED_TRAITS: usize = 2;

/// Whose habit it is, if a person's or a circle's: ("person" or
/// "circle", their id).
pub fn target_of(habit: &Habit) -> Option<(&'static str, &str)> {
    set(habit.person_id.as_deref()).map(|p| ("person", p)).or_else(|| set(habit.circle_id.as_deref()).map(|c| ("circle", c)))
}

/// The name a person's or circle's habit has without its own: theirs,
/// then the traits it's on -- "Sam", "Sam · Reliable", "Sam · Reliable,
/// Kind", "Sam · Reliable +2", "Sam · except Reliable".
pub fn default_name(habit: &Habit, targets: &Targets) -> Option<String> {
    let (kind, id) = target_of(habit)?;
    let named = if kind == "person" { &targets.people } else { &targets.circles };
    let whose = named.iter().find(|(k, _)| k == id).map(|(_, n)| n.clone()).unwrap_or_else(|| id.to_string());
    let ids = |key: &str| -> Vec<String> {
        habit
            .traits
            .as_ref()
            .and_then(|t| t.get(key))
            .and_then(Value::as_array)
            .map(|l| l.iter().filter_map(Value::as_str).map(str::to_string).collect())
            .unwrap_or_default()
    };
    let trait_name = |id: &String| targets.traits.iter().find(|(k, _)| k == id).map(|(_, n)| n.clone()).unwrap_or_else(|| id.clone());
    let listed = |ids: Vec<String>| -> String {
        let mut out = ids.iter().take(NAMED_TRAITS).map(trait_name).collect::<Vec<_>>().join(", ");
        if ids.len() > NAMED_TRAITS {
            out.push_str(&format!(" +{}", ids.len() - NAMED_TRAITS));
        }
        out
    };
    let (select, except) = (ids("select"), ids("except"));
    let picked = habit.traits.as_ref().and_then(|t| t.get("select")).is_some_and(Value::is_array);
    Some(match (picked, except.is_empty()) {
        (true, _) => format!("{whose} · {}", listed(select.into_iter().filter(|t| !except.contains(t)).collect())),
        (false, false) => format!("{whose} · except {}", listed(except)),
        (false, true) => whose,
    })
}

/// `habit` with its name: its own, or for a person's or circle's habit
/// without one, its default.
fn named(habit: &Habit, targets: Option<&Targets>) -> Habit {
    let name = match (set(habit.name.as_deref()), targets) {
        (None, Some(targets)) => default_name(habit, targets).or_else(|| habit.name.clone()),
        _ => habit.name.clone(),
    };
    Habit { name, ..habit.clone() }
}

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
    targets: Option<TargetsFn>,
}

impl Habits {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str, scopes: Option<ScopesFn>, trait_ids: Option<TraitIds>) -> anyhow::Result<Habits> {
        Ok(Habits { sheet: RowSheet::ensure(sheets, spreadsheet_id, HABITS.0, HABITS.1, HABIT_ROW, &["id", "name"])?, scopes, trait_ids, targets: None })
    }

    /// It, checking a person's or circle's habit against `targets`, and
    /// naming one without a name of its own by them.
    pub fn with_targets(mut self, targets: TargetsFn) -> Habits {
        self.targets = Some(targets);
        self
    }

    /// Deletes every habit about `target` (a person's or circle's id,
    /// deleted), taking them out of every habit's foundations, and
    /// returns their subject ids, to take out of people's.
    pub fn delete_about(&self, target: &str) -> anyhow::Result<Vec<String>> {
        let mut habits = self.all()?;
        let mut gone = Vec::new();
        for h in habits.iter_mut() {
            if h.status != Some(Status::Deleted) && target_of(h).is_some_and(|(_, id)| id == target) {
                h.status = Some(Status::Deleted);
                gone.push(subject_id(h.id.as_deref().unwrap_or("None")));
            }
        }
        if gone.is_empty() {
            return Ok(gone);
        }
        for subject in &gone {
            foundations::without(&mut habits, subject);
        }
        self.sheet.write_items(&habits)?;
        Ok(gone)
    }

    /// Each active habit with a recurring goal, as its goals are made.
    pub fn templates(&self) -> anyhow::Result<Vec<Template>> {
        Ok(self
            .all()?
            .into_iter()
            .filter(|h| h.status == Some(Status::Active))
            .filter_map(|h| {
                let habit_id = set(h.id.as_deref())?.to_string();
                Some(Template { name: h.name.clone().filter(|n| !n.trim().is_empty()).unwrap_or_else(|| habit_id.clone()), habit_id, goal: h.recurring_goal? })
            })
            .collect())
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
        let habits = self.all()?;
        let targets = self.targets_if(&habits)?;
        Ok(habits
            .iter()
            .filter(|h| statuses.iter().any(|s| Some(s) == status_of(h).as_ref()))
            .map(|h| listed_with(h, &paths, targets.as_ref()))
            .collect())
    }

    pub fn get_habit(&self, id_or_name: &str) -> anyhow::Result<ListedHabit> {
        let habit = find(&self.named_all()?, id_or_name)?;
        self.shown(&habit)
    }

    /// Every habit with its name, as `named` gives it.
    pub fn named_all(&self) -> anyhow::Result<Vec<Habit>> {
        let habits = self.all()?;
        let targets = self.targets_if(&habits)?;
        Ok(habits.iter().map(|h| named(h, targets.as_ref())).collect())
    }

    /// The targets, if any of `habits` is a person's or circle's.
    fn targets_if(&self, habits: &[Habit]) -> anyhow::Result<Option<Targets>> {
        match &self.targets {
            Some(f) if habits.iter().any(|h| target_of(h).is_some()) => Ok(Some(f()?)),
            _ => Ok(None),
        }
    }

    /// `habit` without a name of its own if it's given its default: a
    /// person's or circle's habit is named as that says till it's given
    /// another.
    fn unless_default(&self, habit: &mut Habit) -> anyhow::Result<()> {
        if let Some(targets) = self.targets_if(std::slice::from_ref(habit))? {
            if habit.name.is_some() && habit.name.as_deref().map(str::trim) == default_name(habit, &targets).as_deref() {
                habit.name = None;
            }
        }
        Ok(())
    }

    fn shown(&self, habit: &Habit) -> anyhow::Result<ListedHabit> {
        let targets = self.targets_if(std::slice::from_ref(habit))?;
        Ok(listed_with(habit, &self.paths()?, targets.as_ref()))
    }

    pub fn create_habit(&self, habit: &Habit) -> anyhow::Result<CreatedHabit> {
        let habits = self.all()?;
        let id = match rows::created(habit, &habits, &[])? {
            rows::Created::New(id) => id,
            rows::Created::Again(made) => {
                return Ok(CreatedHabit { habit: self.shown(&made)?, created_id: made.id.clone().unwrap_or_default() })
            }
        };
        // A recurring goal's habit is scored by its goal rate alone,
        // unless it's given traits too.
        let traits = match (&habit.traits, &habit.recurring_goal) {
            (None, Some(_)) => Some(Map::from_iter([("select".to_string(), json!([]))])),
            (given, _) => given.as_ref().map(spec_with_new_part_ids),
        };
        let mut new =
            Habit { id: Some(id.clone()), status: Some(habit.status.unwrap_or(Status::Active)), traits, version: None, ..habit.clone() };
        self.unless_default(&mut new)?;
        let mut all = habits.clone();
        all.push(new.clone());
        self.write(&all, &id)?;
        Ok(CreatedHabit { habit: self.shown(&new)?, created_id: id })
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
        let before = habits[index].clone();
        habits[index] = rows::updated_item(&before, &given, clear_fields, &["id"])?;
        // A person's or circle's habit now about anything else takes the
        // name that says so, unless given one.
        let after = &habits[index];
        if target_of(after).is_some() && habit.name.is_none() && (target_of(&before) != target_of(after) || selection(&before) != selection(after)) {
            habits[index].name = None;
        }
        self.unless_default(&mut habits[index])?;
        if !was_deleted && habits[index].status == Some(Status::Deleted) {
            foundations::without(&mut habits, &subject_id(&id));
        }
        self.write(&habits, &id)?;
        self.shown(&habits[index])
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
            if let (Some((kind, id)), Some(targets)) = (target_of(habit), self.targets_if(std::slice::from_ref(habit))?) {
                let known = if kind == "person" { &targets.people } else { &targets.circles };
                if !known.iter().any(|(k, _)| k == id) {
                    let lists = if kind == "person" { "get_people" } else { "get_circles" };
                    problems.push(format!("habit {changed}'s {kind}_id {} isn't a {kind} ({lists} lists them)", py::repr(id)));
                }
            }
        }
        if let Some(habit) = habits.iter().find(|h| h.id.as_deref() == Some(changed)) {
            if let Some(given) = &habit.foundations {
                for p in foundation_problems(&subject_id(changed), &json!(given), &habit_nodes(habits)) {
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
            None if target_of(habit).is_some() => {}
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
        let about = [habit.action_id.as_deref(), habit.person_id.as_deref(), habit.circle_id.as_deref()];
        if about.iter().filter(|a| set(**a).is_some()).count() + usize::from(habit.recurring_goal.is_some()) > 1 {
            problems.push(format!("{label} is about one thing: an action_id, a person_id, a circle_id or a recurring_goal, not more"));
        }
        if let Some(goal) = &habit.recurring_goal {
            for p in habit_goals::problems(goal) {
                problems.push(format!("{label}'s recurring_goal {p}"));
            }
        }
        if target_of(habit).is_some() {
            if let Some(traits) = &habit.traits {
                let mut own: Vec<&String> = traits.keys().filter(|k| !["select", "except"].contains(&k.as_str())).collect();
                own.sort();
                for k in own {
                    problems.push(format!(
                        "{label}'s traits have no {}: a person's or circle's habit is scored as they are, so it takes only \"select\" and \"except\"",
                        py::repr(k)
                    ));
                }
            }
        }
        match set(habit.action_id.as_deref()) {
            None if target_of(habit).is_some() || habit.recurring_goal.is_some() => {}
            None => problems.push(format!(
                "{label} needs an action_id: the action, or the action group, it's about (or a person_id, circle_id or recurring_goal)"
            )),
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
        // Only the one changed: another's, wrong since a trait changed,
        // say, shouldn't keep this one from being saved.
        let checked = changed.is_none() || habit.id.as_deref() == changed;
        if let Some(traits) = habit.traits.as_ref().filter(|_| checked) {
            for p in person_traits_problems(&Value::Object(traits.clone()), trait_ids, SpecOf::Habit) {
                problems.push(format!("{label}'s traits {p}"));
            }
        }
    }
    problems
}

/// `habit` as listed, with its name and default name as `targets` give
/// them.
fn listed_with(habit: &Habit, paths: &Scopes, targets: Option<&Targets>) -> ListedHabit {
    ListedHabit { default_name: targets.and_then(|t| default_name(habit, t)), ..listed(&named(habit, targets), paths) }
}

pub fn listed(habit: &Habit, paths: &Scopes) -> ListedHabit {
    let action_path = set(habit.action_id.as_deref()).and_then(|a| paths.iter().find(|(k, _)| k == a).map(|(_, p)| p.clone()));
    ListedHabit {
        id: habit.id.clone(),
        name: habit.name.clone(),
        action_id: habit.action_id.clone(),
        person_id: habit.person_id.clone(),
        circle_id: habit.circle_id.clone(),
        recurring_goal: habit.recurring_goal.clone(),
        status: habit.status,
        note: habit.note.clone(),
        traits: habit.traits.clone(),
        foundations: habit.foundations.clone(),
        action_path,
        default_name: None,
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

/// The traits a habit is on, as its traits pick them.
fn selection(habit: &Habit) -> (Option<Value>, Option<Value>) {
    let get = |k: &str| habit.traits.as_ref().and_then(|t| t.get(k)).cloned();
    (get("select"), get("except"))
}

// Written by the app: made once by their ids and versions.
impl rows::Versioned for Habit {}

#[cfg(test)]
#[path = "habits_tests.rs"]
mod tests;
