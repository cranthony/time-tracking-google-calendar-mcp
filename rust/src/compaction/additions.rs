//! The actions, people and locations a compaction adds as it goes each with
//! a `ref` ("new:ukulele") the plan's decisions name it by, created only
//! when the plan is applied -- and the goals it proposes from what the
//! notes say the user means to do, which name new people by their refs.

use crate::event::Event;
use crate::facts::Facts;
use crate::stores::actions::{action_problems, Action, ActionShare, ActionStatus, Actions};
use crate::stores::goals::{goal_problems, people_of, Goal, GoalStatus, Goals, StatusChange, TextFormat};
use crate::stores::locations::{location_problems, Location, Locations};
use crate::stores::people::{person_problems, People, Person, Status};
use crate::py;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use chrono_tz::Tz;
use crate::contract::DateTimeText;

pub const REF_PREFIX: &str = "new:";

/// An addition: an action, person or location with the `ref` the plan's
/// decisions name it by.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
pub struct Addition<T> {
    #[serde(flatten)]
    pub item: T,
    #[serde(rename = "ref", default)]
    pub ref_: String,
}

/// One addition of any kind.
#[derive(Debug, Clone, PartialEq)]
pub enum Added {
    Action(Addition<Action>),
    Person(Addition<Person>),
    Location(Addition<Location>),
    Goal(Addition<Goal>),
}

impl Added {
    pub fn kind(&self) -> &'static str {
        match self {
            Added::Action(_) => "action",
            Added::Person(_) => "person",
            Added::Location(_) => "location",
            Added::Goal(_) => "goal",
        }
    }

    pub fn ref_(&self) -> &str {
        match self {
            Added::Action(a) => &a.ref_,
            Added::Person(p) => &p.ref_,
            Added::Location(l) => &l.ref_,
            Added::Goal(g) => &g.ref_,
        }
    }

    fn name(&self) -> Option<&str> {
        match self {
            Added::Action(a) => a.item.name.as_deref(),
            Added::Person(p) => p.item.name.as_deref(),
            Added::Location(l) => l.item.name.as_deref(),
            Added::Goal(g) => g.item.title.as_deref(),
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Additions {
    pub actions: Vec<Addition<Action>>,
    pub people: Vec<Addition<Person>>,
    pub locations: Vec<Addition<Location>>,
    pub goals: Vec<Addition<Goal>>,
}

/// The stores additions go into, each if the calendar has it, and the
/// calendar's time zone (for goals' deadlines).
#[derive(Clone, Copy, Default)]
pub struct Stores<'a> {
    pub actions: Option<&'a Actions>,
    pub people: Option<&'a People>,
    pub locations: Option<&'a Locations>,
    pub goals: Option<&'a Goals>,
    pub zone: Option<Tz>,
}

/// `value`'s set fields: its nulls left out.
fn plain(value: Value) -> Value {
    match value {
        Value::Object(map) => Value::Object(map.into_iter().filter(|(_, v)| !v.is_null()).collect()),
        other => other,
    }
}

impl Additions {
    /// Every addition, actions first, then people, then locations, then
    /// goals.
    pub fn all(&self) -> Vec<Added> {
        let actions = self.actions.iter().cloned().map(Added::Action);
        let people = self.people.iter().cloned().map(Added::Person);
        let locations = self.locations.iter().cloned().map(Added::Location);
        actions.chain(people).chain(locations).chain(self.goals.iter().cloned().map(Added::Goal)).collect()
    }

    /// Just `added`.
    pub fn of(added: Added) -> Additions {
        let mut out = Additions::default();
        match added {
            Added::Action(a) => out.actions.push(a),
            Added::Person(p) => out.people.push(p),
            Added::Location(l) => out.locations.push(l),
            Added::Goal(g) => out.goals.push(g),
        }
        out
    }

    /// Every ref, with the name it stands for (a dict: the last of a ref
    /// wins, at the first one's place).
    pub fn refs(&self) -> Vec<(String, String)> {
        let mut out: Vec<(String, String)> = Vec::new();
        for added in self.all() {
            let r = added.ref_().to_string();
            let name = added.name().filter(|n| !n.is_empty()).map(str::to_string).unwrap_or_else(|| r.clone());
            match out.iter_mut().find(|(k, _)| *k == r) {
                Some(slot) => slot.1 = name,
                None => out.push((r, name)),
            }
        }
        out
    }

    pub fn is_empty(&self) -> bool {
        self.actions.is_empty() && self.people.is_empty() && self.locations.is_empty() && self.goals.is_empty()
    }

    /// Only those whose refs `keep` takes.
    pub fn filtered(&self, keep: impl Fn(&str) -> bool) -> Additions {
        Additions {
            actions: self.actions.iter().filter(|a| keep(&a.ref_)).cloned().collect(),
            people: self.people.iter().filter(|p| keep(&p.ref_)).cloned().collect(),
            locations: self.locations.iter().filter(|l| keep(&l.ref_)).cloned().collect(),
            goals: self.goals.iter().filter(|g| keep(&g.ref_)).cloned().collect(),
        }
    }

    pub fn to_json_dict(&self) -> Map<String, Value> {
        let json = |items: Vec<Value>| Value::Array(items.into_iter().map(plain).collect());
        let mut out = Map::new();
        if !self.actions.is_empty() {
            out.insert("actions".into(), json(self.actions.iter().map(to_value).collect()));
        }
        if !self.people.is_empty() {
            out.insert("people".into(), json(self.people.iter().map(to_value).collect()));
        }
        if !self.locations.is_empty() {
            out.insert("locations".into(), json(self.locations.iter().map(to_value).collect()));
        }
        if !self.goals.is_empty() {
            out.insert("goals".into(), json(self.goals.iter().map(to_value).collect()));
        }
        out
    }

    pub fn from_json_dict(data: Option<&Value>) -> anyhow::Result<Additions> {
        fn list<T: serde::de::DeserializeOwned>(data: Option<&Value>, key: &str) -> anyhow::Result<Vec<T>> {
            let items = data.and_then(|d| d.get(key)).and_then(Value::as_array).cloned().unwrap_or_default();
            items
                .into_iter()
                .map(|item| serde_json::from_value(item).map_err(|e| anyhow::anyhow!("the journal's {key} additions can't be read: {e}")))
                .collect()
        }
        Ok(Additions {
            actions: list(data, "actions")?,
            people: list(data, "people")?,
            locations: list(data, "locations")?,
            goals: list(data, "goals")?,
        })
    }
}

fn to_value<T: Serialize>(item: &T) -> Value {
    serde_json::to_value(item).expect("an addition is JSON")
}

/// A new action as its store takes it: active unless given, without the
/// id or label it'll be given.
fn new_action(a: &Addition<Action>) -> Action {
    Action { id: None, label_id: None, status: Some(a.item.status.unwrap_or(ActionStatus::Active)), ..a.item.clone() }
}

fn new_person(p: &Addition<Person>) -> Person {
    Person { id: None, ..p.item.clone() }
}

fn new_location(l: &Addition<Location>) -> Location {
    Location { id: None, ..l.item.clone() }
}

/// A new goal as its store takes it: inactive unless given (the user
/// activates it as they settle it), without the id
/// it'll be given, its parent and people named as `rename` gives them
/// (none, left out).
fn new_goal(g: &Addition<Goal>, rename: &dyn Fn(&str) -> Option<String>) -> Goal {
    let people = |named: &Option<Vec<crate::attendees::Named>>| -> Option<Vec<crate::attendees::Named>> {
        let kept: Vec<crate::attendees::Named> =
            named.iter().flatten().filter_map(|n| rename(&n.id).map(|id| crate::attendees::Named { id, except: n.except.clone() })).collect();
        (!kept.is_empty()).then_some(kept)
    };
    Goal {
        id: None,
        status: Some(g.item.status.unwrap_or(GoalStatus::Inactive)),
        parent_id: g.item.parent_id.as_deref().and_then(rename),
        with_ids: people(&g.item.with_ids),
        for_ids: people(&g.item.for_ids),
        // Written by Claude, in Markdown.
        description_format: g.item.description.as_ref().map(|_| TextFormat::Markdown),
        definition_of_done_format: g.item.definition_of_done.as_ref().map(|_| TextFormat::Markdown),
        history: None,
        version: None,
        ..g.item.clone()
    }
}

/// The id `value` stands for: a ref settled already, its id (none if
/// dropped); else one just made, its id; else itself.
fn renamed(settled: &[(String, Option<String>)], ids: &[(String, String)], value: &str) -> Option<String> {
    match settled.iter().find(|(r, _)| r == value) {
        Some((_, id)) => id.clone(),
        None => Some(mapped(ids, value)),
    }
}

/// `check_additions`: everything wrong with `additions` as their stores
/// would refuse them once added to what's there, or as refs.
/// `settled` are the refs the user has settled already (each with its id,
/// none if dropped), which goals may still name.
pub fn check_additions(additions: &Additions, stores: Stores, settled: &[(String, Option<String>)]) -> anyhow::Result<Vec<String>> {
    let Stores { actions, people, locations, goals, zone } = stores;
    let mut problems: Vec<String> = Vec::new();
    let refs: Vec<String> = additions.all().iter().map(|a| a.ref_().to_string()).collect();
    for r in &refs {
        if !(r.starts_with(REF_PREFIX) && r.chars().count() > REF_PREFIX.len()) {
            problems.push(format!("{} isn't a ref: refs start with {}, e.g. \"new:ukulele\"", py::repr(r), py::repr(REF_PREFIX)));
        }
    }
    let mut twice: Vec<&String> = refs.iter().filter(|r| refs.iter().filter(|x| x == r).count() > 1).collect();
    twice.sort();
    twice.dedup();
    for r in twice {
        problems.push(format!("the ref {} is used more than once", py::repr(r)));
    }
    if !additions.actions.is_empty() {
        match actions {
            None => problems.push("this calendar has no actions to add to".into()),
            Some(store) => {
                let mut all = store.all()?;
                for a in &additions.actions {
                    all.push(Action { id: Some(a.ref_.clone()), label_id: Some(a.ref_.clone()), ..new_action(a) });
                }
                problems.extend(action_problems(&all, &store.groups()?.groups).into_iter().map(|p| format!("new actions: {p}")));
            }
        }
    }
    if !additions.people.is_empty() {
        match people {
            None => problems.push("this calendar has no people to add to".into()),
            Some(store) => {
                let mut all = store.all()?;
                for p in &additions.people {
                    all.push(Person { id: Some(p.ref_.clone()), status: Some(p.item.status.unwrap_or(Status::Active)), ..new_person(p) });
                }
                problems.extend(person_problems(&all, &store.circles()?, None).into_iter().map(|p| format!("new people: {p}")));
            }
        }
    }
    if !additions.locations.is_empty() {
        match locations {
            None => problems.push("this calendar has no locations to add to".into()),
            Some(store) => {
                let mut all = store.all()?;
                for l in &additions.locations {
                    all.push(Location { id: Some(l.ref_.clone()), ..new_location(l) });
                }
                problems.extend(location_problems(&all).into_iter().map(|p| format!("new locations: {p}")));
            }
        }
    }
    if !additions.goals.is_empty() {
        match (goals, zone) {
            (Some(store), Some(zone)) => problems.extend(goal_addition_problems(additions, store, people, &zone, settled)?),
            _ => problems.push("this calendar has no goals to add to".into()),
        }
    }
    Ok(problems)
}

/// What's wrong with `additions`' goals, added to those there: as the
/// goal store would refuse them, and the people they name who are
/// neither people nor added (or settled) by ref.
fn goal_addition_problems(
    additions: &Additions,
    store: &Goals,
    people: Option<&People>,
    zone: &Tz,
    settled: &[(String, Option<String>)],
) -> anyhow::Result<Vec<String>> {
    let mut problems: Vec<String> = Vec::new();
    let mut all = store.all()?;
    let now = DateTimeText(py::pydantic_datetime(&crate::determinism::now()));
    let refs: Vec<String> = additions.goals.iter().map(|g| g.ref_.clone()).collect();
    for g in &additions.goals {
        let status = g.item.status.unwrap_or(GoalStatus::Inactive);
        all.push(Goal {
            id: Some(g.ref_.clone()),
            status: Some(status),
            history: Some(vec![StatusChange { status, at: now.clone(), deadline: g.item.deadline.clone(), note: None }]),
            version: None,
            ..g.item.clone()
        });
    }
    problems.extend(
        goal_problems(&all, zone).into_iter().filter(|(ids, _)| ids.iter().any(|i| refs.contains(i))).map(|(_, p)| format!("new goals: {p}")),
    );
    let (mut known, circles): (Vec<String>, crate::attendees::Circles) = match people {
        Some(p) => (p.all()?.into_iter().filter_map(|p| p.id).collect(), p.circle_index()?),
        None => Default::default(),
    };
    known.extend(additions.people.iter().map(|p| p.ref_.clone()));
    known.extend(settled.iter().map(|(r, _)| r.clone()));
    for g in &additions.goals {
        let is_person = |p: &str| known.iter().any(|k| k == p);
        let naming = circles.naming(
            &crate::attendees::named_entries(g.item.with_ids.as_deref()),
            &crate::attendees::named_entries(g.item.for_ids.as_deref()),
            &is_person,
        );
        let unknown: Vec<&String> = people_of(&g.item).into_iter().filter(|p| naming.unknown.contains(p)).collect();
        if !unknown.is_empty() {
            problems.push(format!(
                "new goals: {} names {}, who aren't people or circles (get_people and get_circles list them; add someone new with new_people)",
                py::repr(&g.ref_),
                py::repr_list(&unknown)
            ));
        }
        problems.extend(naming.other.iter().map(|p| format!("new goals: {}: {p}", py::repr(&g.ref_))));
        if !naming.both.is_empty() {
            problems.push(format!(
                "new goals: {} names {} both with and for: a plain circle stands for its members",
                py::repr(&g.ref_),
                py::repr_list(&naming.both)
            ));
        }
    }
    Ok(problems)
}

fn folded(value: Option<&str>) -> String {
    value.unwrap_or_default().to_lowercase()
}

/// `create_additions`: create `additions` -- reusing any already there by
/// the same name (and context, for a person) -- and each ref's id.
/// Goals come last, naming the people just made (or `settled`) by their
/// ids, and reuse an open goal with the same title.
pub fn create_additions(additions: &Additions, stores: Stores, settled: &[(String, Option<String>)]) -> anyhow::Result<Vec<(String, String)>> {
    let Stores { actions, people, locations, goals, zone } = stores;
    let mut ids: Vec<(String, String)> = Vec::new();
    let put = |ids: &mut Vec<(String, String)>, r: String, id: String| match ids.iter_mut().find(|(k, _)| *k == r) {
        Some(slot) => slot.1 = id,
        None => ids.push((r, id)),
    };
    if let (false, Some(store)) = (additions.actions.is_empty(), actions) {
        for new in &additions.actions {
            let wanted = folded(new.item.name.as_deref().map(str::trim));
            let found = store.all()?.into_iter().find(|a| folded(a.name.as_deref()) == wanted);
            let id = match found {
                Some(found) => found.id.unwrap_or_default(),
                None => store.create_action(&new_action(new))?.created_id,
            };
            put(&mut ids, new.ref_.clone(), id);
        }
    }
    if let (false, Some(store)) = (additions.people.is_empty(), people) {
        for new in &additions.people {
            let key = (folded(new.item.name.as_deref().map(str::trim)), folded(new.item.context.as_deref().map(str::trim)));
            let found = store.all()?.into_iter().find(|p| (folded(p.name.as_deref()), folded(p.context.as_deref())) == key);
            let id = match found {
                Some(found) => found.id.unwrap_or_default(),
                None => store.create_person(&new_person(new))?.created_id,
            };
            put(&mut ids, new.ref_.clone(), id);
        }
    }
    if let (false, Some(store)) = (additions.locations.is_empty(), locations) {
        for new in &additions.locations {
            let wanted = folded(new.item.name.as_deref().map(str::trim));
            let found = store.all()?.into_iter().find(|l| folded(l.name.as_deref()) == wanted);
            let id = match found {
                Some(found) => found.id.unwrap_or_default(),
                None => store.create_location(&new_location(new))?.created_id,
            };
            put(&mut ids, new.ref_.clone(), id);
        }
    }
    if let (false, Some(store), Some(zone)) = (additions.goals.is_empty(), goals, zone) {
        let open = |g: &Goal| g.status.is_some_and(|s| !s.is_closed());
        for new in &additions.goals {
            let wanted = folded(new.item.title.as_deref().map(str::trim));
            let found = store.all()?.into_iter().find(|g| open(g) && folded(g.title.as_deref().map(str::trim)) == wanted);
            let id = match found {
                Some(found) => found.id.unwrap_or_default(),
                None => store.create_goal(&new_goal(new, &|r| renamed(settled, &ids, r)), &zone)?.created_id,
            };
            put(&mut ids, new.ref_.clone(), id);
        }
    }
    Ok(ids)
}

fn mapped(ids: &[(String, String)], value: &str) -> String {
    ids.iter().rev().find(|(k, _)| k == value).map(|(_, v)| v.clone()).unwrap_or_else(|| value.to_string())
}

/// `resolve`: `event` with every ref in its actions and facts replaced.
pub fn resolve(event: &Event, ids: &[(String, String)]) -> Event {
    if ids.is_empty() {
        return event.clone();
    }
    let mut out = event.clone();
    if let Some(action_ids) = event.action_ids.as_ref().filter(|a| !a.is_empty()) {
        out.action_ids = Some(action_ids.iter().map(|a| ActionShare { action_id: mapped(ids, &a.action_id), ..a.clone() }).collect());
    }
    if let Some(facts) = &event.facts {
        let list = |value: &Option<Value>| -> Option<Value> {
            value.as_ref().map(|v| match v {
                Value::Array(items) => Value::Array(crate::facts::renamed_items(items, &|id| Some(mapped(ids, id)))),
                other => other.clone(),
            })
        };
        out.facts = Some(Facts {
            location_ids: list(&facts.location_ids),
            with_ids: list(&facts.with_ids),
            for_ids: list(&facts.for_ids),
            notes: facts.notes.as_ref().map(|n| match n {
                Value::Object(map) => {
                    let mut kept = Map::new();
                    for (k, v) in map {
                        kept.insert(mapped(ids, k), v.clone());
                    }
                    Value::Object(kept)
                }
                other => other.clone(),
            }),
            commute: facts.commute.clone(),
            logs: facts.renamed_logs(&|id| Some(mapped(ids, id))),
        });
    }
    out
}
