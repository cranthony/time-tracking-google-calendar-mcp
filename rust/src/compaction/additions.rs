//! The actions, people and locations a compaction adds as it goes each with
//! a `ref` ("new:ukulele") the plan's decisions name it by, created only
//! when the plan is applied.

use crate::event::Event;
use crate::facts::Facts;
use crate::sheet::rows::{Row, RowType};
use crate::stores::actions::{action_problems, Actions, ACTION_ROW};
use crate::stores::groups::truthy;
use crate::stores::locations::{location_problems, Locations, ROW as LOCATION_ROW};
use crate::stores::people::{person_problems, People, PERSON_ROW};
use crate::py;
use serde_json::{json, Map, Value};

pub const REF_PREFIX: &str = "new:";

/// An addition, as its dataclass (an Action, Person or Location with a
/// `ref`): every field, in order, null when unset.
fn item(row_type: &RowType, value: &Value) -> Row {
    let mut row = row_type.from_value(value);
    row.insert("ref".into(), value.get("ref").cloned().unwrap_or(Value::Null));
    row
}

pub fn ref_of(row: &Row) -> String {
    row.get("ref").and_then(Value::as_str).unwrap_or_default().to_string()
}

fn text<'a>(row: &'a Row, name: &str) -> Option<&'a str> {
    row.get(name).and_then(Value::as_str)
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Additions {
    pub actions: Vec<Row>,
    pub people: Vec<Row>,
    pub locations: Vec<Row>,
}

impl Additions {
    pub fn new(actions: &[Value], people: &[Value], locations: &[Value]) -> Additions {
        Additions {
            actions: actions.iter().map(|v| item(&ACTION_ROW, v)).collect(),
            people: people.iter().map(|v| item(&PERSON_ROW, v)).collect(),
            locations: locations.iter().map(|v| item(&LOCATION_ROW, v)).collect(),
        }
    }

    fn all(&self) -> impl Iterator<Item = &Row> {
        self.actions.iter().chain(&self.people).chain(&self.locations)
    }

    /// Every ref, with the name it stands for (a dict: the last of a ref
    /// wins, at the first one's place).
    pub fn refs(&self) -> Vec<(String, String)> {
        let mut out: Vec<(String, String)> = Vec::new();
        for row in self.all() {
            let name = text(row, "name").filter(|n| !n.is_empty()).map(str::to_string).unwrap_or_else(|| ref_of(row));
            match out.iter_mut().find(|(r, _)| *r == ref_of(row)) {
                Some(slot) => slot.1 = name,
                None => out.push((ref_of(row), name)),
            }
        }
        out
    }

    pub fn is_empty(&self) -> bool {
        self.actions.is_empty() && self.people.is_empty() && self.locations.is_empty()
    }

    pub fn to_json_dict(&self) -> Map<String, Value> {
        let plain = |row: &Row| -> Value {
            Value::Object(row.iter().filter(|(_, v)| !v.is_null()).map(|(k, v)| (k.clone(), v.clone())).collect())
        };
        let mut out = Map::new();
        for (kind, items) in [("actions", &self.actions), ("people", &self.people), ("locations", &self.locations)] {
            if !items.is_empty() {
                out.insert(kind.into(), Value::Array(items.iter().map(plain).collect()));
            }
        }
        out
    }

    pub fn from_json_dict(data: Option<&Value>) -> Additions {
        let list = |key: &str| -> Vec<Value> { data.and_then(|d| d.get(key)).and_then(Value::as_array).cloned().unwrap_or_default() };
        Additions::new(&list("actions"), &list("people"), &list("locations"))
    }
}

/// `item`'s fields that `base` has too, but its id and label.
fn base_fields(row: &Row) -> Row {
    row.iter().filter(|(k, _)| !["id", "label_id", "ref"].contains(&k.as_str())).map(|(k, v)| (k.clone(), v.clone())).collect()
}

fn with_fields(row_type: &RowType, fields: Row) -> Row {
    row_type.fields.iter().map(|(name, _)| (name.to_string(), fields.get(*name).cloned().unwrap_or(Value::Null))).collect()
}

fn status_or_active(row: &mut Row) {
    if !truthy(row.get("status")) {
        row.insert("status".into(), json!("active"));
    }
}

/// `check_additions`: everything wrong with `additions` as their stores
/// would refuse them once added to what's there, or as refs.
pub fn check_additions(
    additions: &Additions,
    actions: Option<&Actions>,
    people: Option<&People>,
    locations: Option<&Locations>,
) -> anyhow::Result<Vec<String>> {
    let mut problems: Vec<String> = Vec::new();
    let refs: Vec<String> = additions.all().map(ref_of).collect();
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
                    let mut row = with_fields(&ACTION_ROW, base_fields(a));
                    row.insert("id".into(), json!(ref_of(a)));
                    row.insert("label_id".into(), json!(ref_of(a)));
                    status_or_active(&mut row);
                    all.push(row);
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
                    let mut row = with_fields(&PERSON_ROW, base_fields(p));
                    row.insert("id".into(), json!(ref_of(p)));
                    status_or_active(&mut row);
                    all.push(row);
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
                    let mut row = with_fields(&LOCATION_ROW, base_fields(l));
                    row.insert("id".into(), json!(ref_of(l)));
                    all.push(row);
                }
                problems.extend(location_problems(&all).into_iter().map(|p| format!("new locations: {p}")));
            }
        }
    }
    Ok(problems)
}

fn folded(value: Option<&str>) -> String {
    value.unwrap_or_default().to_lowercase()
}

/// `create_additions`: create `additions` -- reusing any already there by
/// the same name (and context, for a person) -- and each ref's id.
pub fn create_additions(
    additions: &Additions,
    actions: Option<&Actions>,
    people: Option<&People>,
    locations: Option<&Locations>,
) -> anyhow::Result<Vec<(String, String)>> {
    let mut ids: Vec<(String, String)> = Vec::new();
    let put = |ids: &mut Vec<(String, String)>, r: String, id: String| match ids.iter_mut().find(|(k, _)| *k == r) {
        Some(slot) => slot.1 = id,
        None => ids.push((r, id)),
    };
    let created_id = |value: &Value| value["created_id"].as_str().unwrap_or_default().to_string();
    if let (false, Some(store)) = (additions.actions.is_empty(), actions) {
        for new in &additions.actions {
            let wanted = folded(text(new, "name").map(str::trim));
            let found = store.all()?.into_iter().find(|a| folded(text(a, "name")) == wanted);
            let id = match found {
                Some(found) => text(&found, "id").unwrap_or_default().to_string(),
                None => {
                    let mut row = with_fields(&ACTION_ROW, base_fields(new));
                    status_or_active(&mut row);
                    created_id(&store.create_action(&row)?)
                }
            };
            put(&mut ids, ref_of(new), id);
        }
    }
    if let (false, Some(store)) = (additions.people.is_empty(), people) {
        for new in &additions.people {
            let key = (folded(text(new, "name").map(str::trim)), folded(text(new, "context").map(str::trim)));
            let found = store.all()?.into_iter().find(|p| (folded(text(p, "name")), folded(text(p, "context"))) == key);
            let id = match found {
                Some(found) => text(&found, "id").unwrap_or_default().to_string(),
                None => created_id(&store.create_person(&with_fields(&PERSON_ROW, base_fields(new)))?),
            };
            put(&mut ids, ref_of(new), id);
        }
    }
    if let (false, Some(store)) = (additions.locations.is_empty(), locations) {
        for new in &additions.locations {
            let wanted = folded(text(new, "name").map(str::trim));
            let found = store.all()?.into_iter().find(|l| folded(text(l, "name")) == wanted);
            let id = match found {
                Some(found) => text(&found, "id").unwrap_or_default().to_string(),
                None => created_id(&store.create_location(&with_fields(&LOCATION_ROW, base_fields(new)))?),
            };
            put(&mut ids, ref_of(new), id);
        }
    }
    Ok(ids)
}

fn mapped(ids: &[(String, String)], value: &str) -> String {
    ids.iter().rev().find(|(k, _)| k == value).map(|(_, v)| v.clone()).unwrap_or_else(|| value.to_string())
}

fn mapped_value(ids: &[(String, String)], value: &Value) -> Value {
    match value {
        Value::String(s) => Value::String(mapped(ids, s)),
        other => other.clone(),
    }
}

/// `resolve`: `event` with every ref in its actions and facts replaced.
pub fn resolve(event: &Event, ids: &[(String, String)]) -> Event {
    if ids.is_empty() {
        return event.clone();
    }
    let mut out = event.clone();
    if let Some(action_ids) = event.action_ids.as_ref().filter(|a| !a.is_empty()) {
        out.action_ids = Some(action_ids.iter().map(|a| mapped(ids, a)).collect());
    }
    if let Some(facts) = &event.facts {
        let list = |value: &Option<Value>| -> Option<Value> {
            value.as_ref().map(|v| match v {
                Value::Array(items) => Value::Array(items.iter().map(|i| mapped_value(ids, i)).collect()),
                other => other.clone(),
            })
        };
        out.facts = Some(Facts {
            location_id: match &facts.location_id {
                Some(l) if truthy(Some(l)) => Some(mapped_value(ids, l)),
                other => other.clone(),
            },
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
        });
    }
    out
}
