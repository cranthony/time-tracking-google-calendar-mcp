//! An event's facts: what compaction established about it -- where (one
//! place or several), who with, who for, and a note on each person there
//! -- whether it's a commute, getting from one place to another, and the
//! log of each change it made to a person's trait facts (`logs`).
//!
//! Each place and person is an id, or, with a share of the event
//! (`LocationShare`, `PersonShare`), an object: {location_id or
//! person_id, minutes?, percent?, remaining?}. Ids stored before shares
//! were are read as they always were. With and for can name a circle too
//! -- its id, or {circle_id, except?, minutes?, percent?, remaining?} --
//! a plain one standing for its members, but those its except leaves out
//! (see crate::attendees, which says who an event's facts name).
//!
//! The fields hold JSON values rather than typed ones: facts read back
//! from an event someone edited by hand can be any shape, and the checks
//! below report those shapes exactly as the Python server does.

use crate::py;
use regex::Regex;
use serde_json::{Map, Value};
use std::sync::LazyLock;

pub const SELF_ID: &str = "self";
pub const MAX_FACTS_CHARS: usize = 8 * 1024;
pub const MAX_NOTE_CHARS: usize = 1000;
pub const MAX_LOG_CHARS: usize = 500;

static SPACES: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s+").unwrap());

/// A person or circle an event was with or for, and how much of the
/// event they take: minutes (a fixed share), percent (of what the
/// minutes leave), or neither -- the remaining time, what the minutes and
/// percents leave, all of it, as each person with neither takes it. A
/// plain circle's members each take its share whole, as a person does,
/// but those its `except` leaves out; a circle given by `circle_id` keeps
/// it (see crate::attendees).
#[derive(Clone, Debug, Default, PartialEq, Eq, Hash)]
pub struct PersonShare {
    /// The person's or circle's id.
    pub person_id: String,
    pub minutes: Option<i64>,
    pub percent: Option<i64>,
    pub remaining: bool,
    /// Whether it was given as a circle's: by circle_id.
    pub circle: bool,
    /// A plain circle's members who weren't.
    pub except: Vec<String>,
}

impl PersonShare {
    /// It with neither minutes nor percent.
    pub fn whole(id: impl Into<String>) -> PersonShare {
        PersonShare { person_id: id.into(), ..Default::default() }
    }
}

impl From<&str> for PersonShare {
    fn from(id: &str) -> PersonShare {
        PersonShare::whole(id)
    }
}

impl From<String> for PersonShare {
    fn from(id: String) -> PersonShare {
        PersonShare::whole(id)
    }
}

impl crate::stores::shares::Share for PersonShare {
    const KIND: &'static str = "person";
    const THEIRS: &'static str = "people's";
    const NEITHER: &'static str = "the remaining time";

    fn new(id: String, minutes: Option<i64>, percent: Option<i64>, remaining: bool) -> Self {
        PersonShare { person_id: id, minutes, percent, remaining: remaining && (minutes.is_some() || percent.is_some()), ..Default::default() }
    }
    fn id(&self) -> &str {
        &self.person_id
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

/// A person and how much of the event they take: minutes (a fixed
/// share), or percent (of what the minutes leave).
#[derive(serde::Serialize, serde::Deserialize, crate::contract::Schema)]
#[serde(deny_unknown_fields)]
#[schema(deny_unknown)]
pub struct PersonSplit {
    person_id: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    minutes: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    percent: Option<i64>,
    /// The remaining time too, what the minutes and percents
    /// leave, as one given as a bare id takes.
    #[schema(default = "false")]
    #[serde(default, skip_serializing_if = "crate::stores::shares::is_false")]
    remaining: bool,
}

/// A circle and how much of the event its members each take, as a person
/// takes it -- a plain circle's but those in except, who weren't there; a
/// community stands for itself, so takes no except.
#[derive(serde::Serialize, serde::Deserialize, crate::contract::Schema)]
#[serde(deny_unknown_fields)]
#[schema(deny_unknown)]
pub struct CircleSplit {
    circle_id: String,
    /// A plain circle's members who weren't, by person id.
    #[schema(optional)]
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    except: Vec<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    minutes: Option<i64>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    percent: Option<i64>,
    /// The remaining time too, what the minutes and percents
    /// leave, as one given as a bare id takes.
    #[schema(default = "false")]
    #[serde(default, skip_serializing_if = "crate::stores::shares::is_false")]
    remaining: bool,
}

impl serde::Serialize for PersonShare {
    fn serialize<S: serde::Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        if self.circle || !self.except.is_empty() {
            return CircleSplit {
                circle_id: self.person_id.clone(),
                except: self.except.clone(),
                minutes: self.minutes,
                percent: self.percent,
                remaining: self.remaining,
            }
            .serialize(serializer);
        }
        if self.minutes.is_none() && self.percent.is_none() {
            return serializer.serialize_str(&self.person_id);
        }
        PersonSplit { person_id: self.person_id.clone(), minutes: self.minutes, percent: self.percent, remaining: self.remaining }.serialize(serializer)
    }
}

impl<'de> serde::Deserialize<'de> for PersonShare {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        use crate::stores::shares::Share;
        #[derive(serde::Deserialize)]
        #[serde(untagged)]
        enum Given {
            Id(String),
            Split(PersonSplit),
            Circle(CircleSplit),
        }
        Ok(match Given::deserialize(deserializer)? {
            Given::Id(id) => PersonShare::whole(id),
            Given::Split(s) => PersonShare::new(s.person_id, s.minutes, s.percent, s.remaining),
            Given::Circle(c) => PersonShare { circle: true, except: c.except, ..PersonShare::new(c.circle_id, c.minutes, c.percent, c.remaining) },
        })
    }
}

impl crate::contract::Schema for PersonShare {
    fn schema(defs: &mut crate::contract::Defs) -> serde_json::Value {
        let split = <PersonSplit as crate::contract::Schema>::schema(defs);
        let circle = <CircleSplit as crate::contract::Schema>::schema(defs);
        serde_json::json!({"anyOf": [
            {"description": "A person's or circle's id: they take the remaining time, what the minutes and percents leave (a plain circle's members each, as a person).", "type": "string"},
            split,
            circle,
        ]})
    }
}

crate::stores::shares::id_share! {
    /// A place an event was at, and how much of the event it takes:
    /// minutes (a fixed share), percent (of what the minutes leave), or
    /// neither -- an even part of the remaining time, what the minutes and
    /// percents leave, with the other places that take it.
    LocationShare, LocationSplit, location_id, "location", "locations'", "an even part of the remaining time",
    "A location's id: it takes an even part of the remaining time, what the minutes and percents leave.",
    "A location and how much of the event it takes: minutes (a fixed\nshare), or percent (of what the minutes leave)."
}

/// The keys an id is under in a list's objects.
const ID_KEYS: [&str; 3] = ["person_id", "circle_id", "location_id"];

/// The id of an item of a facts list: the item, or its object's
/// person_id, circle_id or location_id.
pub fn item_id(item: &Value) -> Option<&str> {
    match item {
        Value::String(s) => Some(s),
        Value::Object(map) => ID_KEYS.iter().find_map(|k| map.get(*k)).and_then(Value::as_str),
        _ => None,
    }
}

/// `item` with its id as `id`.
fn with_item_id(item: &Value, id: String) -> Value {
    match item {
        Value::Object(map) => {
            let mut map = map.clone();
            if let Some(key) = ID_KEYS.iter().find(|k| map.contains_key(**k)) {
                map.insert(key.to_string(), Value::String(id));
            }
            Value::Object(map)
        }
        _ => Value::String(id),
    }
}

/// A facts list's items with each id as `rename` gives it -- those it
/// gives none dropped -- each id once.
pub fn renamed_items(items: &[Value], rename: &dyn Fn(&str) -> Option<String>) -> Vec<Value> {
    let mut seen: Vec<String> = Vec::new();
    let mut out = Vec::new();
    for item in items {
        let Some(id) = item_id(item).and_then(rename) else { continue };
        if seen.contains(&id) {
            continue;
        }
        seen.push(id.clone());
        out.push(with_item_id(item, id));
    }
    out
}

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Facts {
    pub location_ids: Option<Value>,
    pub with_ids: Option<Value>,
    pub for_ids: Option<Value>,
    pub notes: Option<Value>,
    /// True for a commute: getting from one place to another.
    pub commute: Option<Value>,
    /// What the event changed in each person's trait facts: {person id:
    /// {"<trait id>/<fact id>": a log of the change}}.
    pub logs: Option<Value>,
}

/// The JSON key each field is stored under, in field order.
const FIELDS: [(&str, &str); 6] = [
    ("location_ids", "locations"),
    ("with_ids", "with"),
    ("for_ids", "for"),
    ("notes", "notes"),
    ("commute", "commute"),
    ("logs", "logs"),
];

fn is_blank(value: &Option<Value>) -> bool {
    match value {
        None | Some(Value::Null) => true,
        Some(Value::Array(a)) => a.is_empty(),
        Some(Value::Object(o)) => o.is_empty(),
        // Not a commute is no fact.
        Some(Value::Bool(b)) => !b,
        _ => false,
    }
}

impl Facts {
    fn field(&self, name: &str) -> &Option<Value> {
        match name {
            "location_ids" => &self.location_ids,
            "with_ids" => &self.with_ids,
            "for_ids" => &self.for_ids,
            "commute" => &self.commute,
            "logs" => &self.logs,
            _ => &self.notes,
        }
    }

    pub fn is_empty(&self) -> bool {
        FIELDS.iter().all(|(name, _)| is_blank(self.field(name)))
    }

    /// Ids trimmed and deduplicated, blank notes dropped.
    pub fn normalized(&self) -> Facts {
        fn ids(value: &Option<Value>) -> Option<Value> {
            match value {
                Some(Value::Array(items)) => {
                    let trimmed = |id: &str| Some(id.trim().to_string()).filter(|t| !t.is_empty());
                    Some(Value::Array(renamed_items(items, &trimmed)))
                }
                other => other.clone(),
            }
        }
        let notes = match &self.notes {
            Some(Value::Object(map)) => {
                let mut kept = Map::new();
                for (key, value) in map {
                    if let Value::String(text) = value {
                        if !text.trim().is_empty() {
                            kept.insert(key.trim().to_string(), Value::String(SPACES.replace_all(text, " ").trim().to_string()));
                        }
                    }
                }
                Some(Value::Object(kept))
            }
            other => other.clone(),
        };
        let logs = match &self.logs {
            Some(Value::Object(by_person)) => {
                let mut kept = Map::new();
                for (person, logged) in by_person {
                    let Value::Object(logged) = logged else {
                        kept.insert(person.trim().to_string(), logged.clone());
                        continue;
                    };
                    let mut texts = Map::new();
                    for (key, value) in logged {
                        match value {
                            Value::String(text) if text.trim().is_empty() => {}
                            Value::String(text) => {
                                texts.insert(key.trim().to_string(), Value::String(SPACES.replace_all(text, " ").trim().to_string()));
                            }
                            other => {
                                texts.insert(key.trim().to_string(), other.clone());
                            }
                        }
                    }
                    if !texts.is_empty() {
                        kept.insert(person.trim().to_string(), Value::Object(texts));
                    }
                }
                Some(Value::Object(kept))
            }
            other => other.clone(),
        };
        Facts {
            location_ids: ids(&self.location_ids),
            with_ids: ids(&self.with_ids),
            for_ids: ids(&self.for_ids),
            notes,
            commute: self.commute.clone(),
            logs,
        }
    }

    /// Each log, in order: (person id, "<trait id>/<fact id>", the log).
    pub fn log_entries(&self) -> Vec<(String, String, String)> {
        let mut out = Vec::new();
        if let Some(Value::Object(by_person)) = &self.logs {
            for (person, logged) in by_person {
                for (key, log) in logged.as_object().into_iter().flatten() {
                    if let Value::String(log) = log {
                        out.push((person.clone(), key.clone(), log.clone()));
                    }
                }
            }
        }
        out
    }

    /// `logs` with each person's id as `rename` gives it -- those it gives
    /// none dropped.
    pub fn renamed_logs(&self, rename: &dyn Fn(&str) -> Option<String>) -> Option<Value> {
        match &self.logs {
            Some(Value::Object(by_person)) => {
                let mut kept = Map::new();
                for (person, logged) in by_person {
                    if let Some(target) = rename(person) {
                        if !kept.contains_key(&target) {
                            kept.insert(target.clone(), by_person.get(&target).cloned().unwrap_or(logged.clone()));
                        }
                    }
                }
                Some(Value::Object(kept))
            }
            other => other.clone(),
        }
    }

    /// Whether it's a commute.
    pub fn is_commute(&self) -> bool {
        self.commute == Some(Value::Bool(true))
    }

    /// A list's ids, whether each is an id or an object with a share.
    fn strings(value: &Option<Value>) -> Vec<String> {
        match value {
            Some(Value::Array(items)) => items.iter().filter_map(|v| item_id(v).map(str::to_string)).collect(),
            _ => Vec::new(),
        }
    }

    /// A list's items as shares; those that aren't one left out.
    fn shares<S: serde::de::DeserializeOwned>(value: &Option<Value>) -> Vec<S> {
        match value {
            Some(Value::Array(items)) => items.iter().filter_map(|v| serde_json::from_value(v.clone()).ok()).collect(),
            _ => Vec::new(),
        }
    }

    /// The people it was with, and how much of it each takes.
    pub fn with_shares(&self) -> Vec<PersonShare> {
        Self::shares(&self.with_ids)
    }

    /// The people it was for, and how much of it each takes.
    pub fn for_shares(&self) -> Vec<PersonShare> {
        Self::shares(&self.for_ids)
    }

    /// The places it was, and how much of it each takes.
    pub fn location_shares(&self) -> Vec<LocationShare> {
        Self::shares(&self.location_ids)
    }

    /// What's wrong with its people's and places' shares on an event
    /// `event_minutes` long, each after the list it's in.
    pub fn share_problems(&self, event_minutes: i64) -> Vec<String> {
        use crate::stores::shares::share_problems;
        let named = |name: &str, problems: Vec<String>| problems.into_iter().map(|p| format!("\"{name}\": {p}")).collect::<Vec<_>>();
        let mut problems = named("location_ids", share_problems(&self.location_shares(), event_minutes));
        problems.extend(named("with_ids", share_problems(&self.with_shares(), event_minutes)));
        problems.extend(named("for_ids", share_problems(&self.for_shares(), event_minutes)));
        problems
    }

    /// The ids it's with, as given: people's and circles'.
    pub fn with(&self) -> Vec<String> {
        Self::strings(&self.with_ids)
    }

    /// The ids it's for, as given: people's and circles'.
    pub fn for_(&self) -> Vec<String> {
        Self::strings(&self.for_ids)
    }

    /// Its with_ids' entries: who it was with, to tell who that is (see
    /// crate::attendees).
    pub fn with_entries(&self) -> Vec<crate::attendees::Entry> {
        crate::attendees::entries(self.with_ids.as_ref())
    }

    /// Its for_ids' entries.
    pub fn for_entries(&self) -> Vec<crate::attendees::Entry> {
        crate::attendees::entries(self.for_ids.as_ref())
    }

    pub fn note_people(&self) -> Vec<String> {
        match &self.notes {
            Some(Value::Object(map)) => map.keys().cloned().collect(),
            _ => Vec::new(),
        }
    }

    /// Every person (or circle) named: with, for, then noted.
    pub fn people(&self) -> Vec<String> {
        let mut all: Vec<String> = Vec::new();
        for id in self.with().into_iter().chain(self.for_()).chain(self.note_people()) {
            if !all.contains(&id) {
                all.push(id);
            }
        }
        all
    }

    /// Every place it was, in the order given.
    pub fn locations(&self) -> Vec<String> {
        Self::strings(&self.location_ids)
    }

    /// Compact JSON of what's set, under the stored keys.
    pub fn to_json(&self) -> String {
        let mut map = Map::new();
        for (name, key) in FIELDS {
            let value = self.field(name);
            if !is_blank(value) {
                map.insert(key.to_string(), value.clone().unwrap());
            }
        }
        py::json_compact(&Value::Object(map))
    }

    /// Facts from `to_json`'s text; `None` if it isn't an object.
    pub fn from_json(raw: &str) -> Option<Facts> {
        let data: Value = serde_json::from_str(raw).ok()?;
        let map = data.as_object()?;
        let mut facts = Facts::default();
        for (name, key) in FIELDS {
            if let Some(value) = map.get(key) {
                let slot = match name {
                    "location_ids" => &mut facts.location_ids,
                    "with_ids" => &mut facts.with_ids,
                    "for_ids" => &mut facts.for_ids,
                    "commute" => &mut facts.commute,
                    "logs" => &mut facts.logs,
                    _ => &mut facts.notes,
                };
                *slot = Some(value.clone());
            }
        }
        Some(facts)
    }

    /// The facts as a client sees them (PublicEvent's `facts`): every
    /// field, null when unset.
    pub fn to_public(&self) -> Value {
        let mut map = Map::new();
        for (name, _) in FIELDS {
            map.insert(name.to_string(), self.field(name).clone().unwrap_or(Value::Null));
        }
        Value::Object(map)
    }

    /// Facts from a client's JSON (already checked against the schema).
    pub fn from_public(value: &Value) -> Facts {
        let get = |key: &str| value.get(key).filter(|v| !v.is_null()).cloned();
        Facts {
            location_ids: get("location_ids"),
            with_ids: get("with_ids"),
            for_ids: get("for_ids"),
            notes: get("notes"),
            commute: get("commute"),
            logs: get("logs"),
        }
    }
}

/// Whether `value` is a list of ids, each a non-empty id, or an object
/// with one under `key` and a share (see `PersonShare`) -- or, for
/// people, under circle_id, with a share and an except.
fn is_id_list(value: &Value, key: &str) -> bool {
    let circles = key == "person_id";
    match value {
        Value::Array(items) => items.iter().all(|i| match i {
            Value::String(s) => !s.is_empty(),
            Value::Object(map) => {
                let id = if circles && map.contains_key("circle_id") { "circle_id" } else { key };
                map.get(id).and_then(Value::as_str).is_some_and(|s| !s.is_empty())
                    && map.iter().all(|(k, v)| match k.as_str() {
                        "minutes" | "percent" => v.is_null() || v.is_i64(),
                        "remaining" => v.is_boolean(),
                        "except" if id == "circle_id" => {
                            v.as_array().is_some_and(|ids| ids.iter().all(|e| e.as_str().is_some_and(|s| !s.is_empty())))
                        }
                        _ => k == id,
                    })
            }
            _ => false,
        }),
        _ => false,
    }
}

/// Everything wrong with `facts`' shape, as phrases following "its
/// facts", `circles` telling who a circle it names stands for.
pub fn fact_problems(facts: &Facts, circles: &crate::attendees::Circles) -> Vec<String> {
    let mut problems = Vec::new();
    for (name, value, of) in
        [("location_ids", &facts.location_ids, "location"), ("with_ids", &facts.with_ids, "person"), ("for_ids", &facts.for_ids, "person")]
    {
        if let Some(v) = value {
            if !v.is_null() && !is_id_list(v, &format!("{of}_id")) {
                problems.push(if of == "person" {
                    format!("\"{name}\" must be a list of person or circle ids, each an id, {{person_id, minutes?, percent?, remaining?}} or {{circle_id, except?, minutes?, percent?, remaining?}}")
                } else {
                    format!("\"{name}\" must be a list of {of} ids, each an id or {{{of}_id, minutes?, percent?, remaining?}}")
                });
            }
        }
    }
    if let Some(Value::Array(items)) = &facts.with_ids {
        if items.iter().any(|i| item_id(i) == Some(SELF_ID)) {
            problems.push("\"with_ids\" never names \"self\": the user is at every event".to_string());
        }
    }
    if problems.is_empty() {
        let with = facts.with();
        let mut both: Vec<String> = facts.for_().into_iter().filter(|i| with.contains(i)).collect();
        both.sort();
        both.dedup();
        if !both.is_empty() {
            problems.push(format!("{} can't be both \"with\" (there) and \"for\" (not there)", py::repr_list(&both)));
        }
        let through = circles.naming(&facts.with_entries(), &facts.for_entries(), &|_| true).both;
        if !through.is_empty() {
            problems.push(format!(
                "{} can't be both \"with\" (there) and \"for\" (not there): a plain circle stands for its members",
                py::repr_list(&through)
            ));
        }
    }
    // Who it was with, its plain circles' members among them.
    let there = circles.attendees(&facts.with_entries());
    if let Some(notes) = &facts.notes {
        if !notes.is_null() {
            match notes {
                Value::Object(map) if map.values().all(Value::is_string) => {
                    let mut long: Vec<String> = map
                        .iter()
                        .filter(|(_, v)| v.as_str().unwrap().chars().count() > MAX_NOTE_CHARS)
                        .map(|(k, _)| k.clone())
                        .collect();
                    long.sort();
                    if !long.is_empty() {
                        problems.push(format!(
                            "\"notes\" for {} must be at most {MAX_NOTE_CHARS} characters",
                            py::repr_list(&long)
                        ));
                    }
                    let mut absent: Vec<String> =
                        map.keys().filter(|k| !there.contains(k) && k.as_str() != SELF_ID).cloned().collect();
                    absent.sort();
                    if !absent.is_empty() {
                        problems.push(format!(
                            "\"notes\" are for the people who were there (\"self\" and \"with\"), not {}",
                            py::repr_list(&absent)
                        ));
                    }
                }
                _ => problems.push("\"notes\" must be {person id: a note}".to_string()),
            }
        }
    }
    if facts.commute.as_ref().is_some_and(|c| !c.is_null() && !c.is_boolean()) {
        problems.push("\"commute\" must be true or false".to_string());
    }
    if let Some(logs) = facts.logs.as_ref().filter(|l| !l.is_null()) {
        problems.extend(log_problems(logs, &there, &circles.attendees(&facts.for_entries())));
    }
    if problems.is_empty() && facts.to_json().chars().count() > MAX_FACTS_CHARS {
        problems.push(format!("are longer than {MAX_FACTS_CHARS} characters as JSON"));
    }
    problems
}

/// What's wrong with `logs`' shape: each for a person it was with or for
/// (or "self"), each of their facts "<trait id>/<fact id>", each log
/// non-empty and at most MAX_LOG_CHARS.
fn log_problems(logs: &Value, with: &[String], for_: &[String]) -> Vec<String> {
    const SHAPE: &str = "\"logs\" must be {person id: {\"<trait id>/<fact id>\": a log of what changed}}";
    let Value::Object(by_person) = logs else { return vec![SHAPE.to_string()] };
    let mut problems = Vec::new();
    let mut absent: Vec<String> = Vec::new();
    for (person, logged) in by_person {
        let Value::Object(logged) = logged else {
            problems.push(SHAPE.to_string());
            continue;
        };
        if person != SELF_ID && !with.contains(person) && !for_.contains(person) {
            absent.push(person.clone());
        }
        for (key, log) in logged {
            if !is_fact_key(key) {
                problems.push(format!("\"logs\" for {}: {} isn't \"<trait id>/<fact id>\"", py::repr(person), py::repr(key)));
            }
            match log.as_str() {
                Some(text) if text.chars().count() > MAX_LOG_CHARS => {
                    problems.push(format!("\"logs\" for {}: the log of {key} must be at most {MAX_LOG_CHARS} characters", py::repr(person)))
                }
                Some(text) if !text.trim().is_empty() => {}
                _ => problems.push(format!("\"logs\" for {}: the log of {} must be non-empty text", py::repr(person), py::repr(key))),
            }
        }
    }
    absent.sort();
    if !absent.is_empty() {
        problems.push(format!("\"logs\" are for the people it was with or for (or \"self\"), not {}", py::repr_list(&absent)));
    }
    problems
}

/// Whether `key` is "<trait id>/<fact id>", both non-empty.
pub fn is_fact_key(key: &str) -> bool {
    matches!(key.split_once('/'), Some((t, f)) if !t.trim().is_empty() && !f.trim().is_empty() && !f.contains('/'))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn logs_are_kept_and_checked() {
        let given = facts(json!({"with_ids": ["sam"], "logs": {"sam": {"thoughtful/what_matters": "  starts a  new job "}}}));
        assert_eq!(given.normalized().logs, Some(json!({"sam": {"thoughtful/what_matters": "starts a new job"}})));
        assert_eq!(given.to_json(), r#"{"with":["sam"],"logs":{"sam":{"thoughtful/what_matters":"  starts a  new job "}}}"#);
        assert_eq!(Facts::from_json(&given.to_json()), Some(given.clone()));
        assert!(fact_problems(&given, &Default::default()).is_empty());
        assert_eq!(given.log_entries(), vec![("sam".into(), "thoughtful/what_matters".into(), "  starts a  new job ".into())]);
        let long = "x".repeat(MAX_LOG_CHARS + 1);
        for (given, problem) in [
            (json!({"logs": ["x"]}), "\"logs\" must be {person id"),
            (json!({"logs": {"mom": {"a/b": "x"}}}), "\"logs\" are for the people it was with or for"),
            (json!({"logs": {"self": {"ab": "x"}}}), "\"logs\" for 'self': 'ab' isn't"),
            (json!({"logs": {"self": {"a/b": long}}}), "\"logs\" for 'self': the log of a/b must be at most"),
            (json!({"logs": {"self": {"a/b": 3}}}), "\"logs\" for 'self': the log of 'a/b' must be non-empty"),
        ] {
            let found = fact_problems(&facts(given.clone()), &Default::default());
            assert!(found.first().is_some_and(|f| f.starts_with(problem)), "{given}: {found:?}");
        }
        assert!(fact_problems(&facts(json!({"for_ids": ["mom"], "logs": {"mom": {"a/b": "x"}}})), &Default::default()).is_empty());
    }

    fn facts(value: Value) -> Facts {
        Facts::from_public(&value)
    }

    #[test]
    fn round_trip_as_compact_json_under_short_keys() {
        let given = facts(json!({"location_ids": ["home", "park"], "with_ids": ["sam"], "for_ids": ["mom"], "notes": {"self": "tired", "sam": "glad"}}));
        assert_eq!(given.to_json(), r#"{"locations":["home","park"],"with":["sam"],"for":["mom"],"notes":{"self":"tired","sam":"glad"}}"#);
        assert_eq!(Facts::from_json(&given.to_json()), Some(given));
    }

    #[test]
    fn a_commute_is_kept_and_checked() {
        let commute = facts(json!({"commute": true}));
        assert!(commute.is_commute() && !commute.is_empty());
        assert_eq!(commute.to_json(), r#"{"commute":true}"#);
        assert_eq!(Facts::from_json(&commute.to_json()), Some(commute));
        // Not a commute is no fact at all.
        assert!(facts(json!({"commute": false})).is_empty());
        assert_eq!(facts(json!({"commute": false, "with_ids": ["sam"]})).to_json(), r#"{"with":["sam"]}"#);
        assert_eq!(fact_problems(&facts(json!({"commute": "yes"})), &Default::default()), vec!["\"commute\" must be true or false"]);
    }

    #[test]
    fn a_single_location_id_is_neither_read_nor_sent() {
        assert_eq!(facts(json!({"location_id": "home", "with_ids": ["sam"]})), facts(json!({"with_ids": ["sam"]})));
        assert!(facts(json!({"location_ids": ["park"]})).to_public().get("location_id").is_none());
    }

    #[test]
    fn a_location_stored_the_old_way_is_no_longer_read() {
        assert_eq!(Facts::from_json(r#"{"location":"home","with":["sam"]}"#), Some(facts(json!({"with_ids": ["sam"]}))));
    }

    #[test]
    fn from_json_ignores_unknown_keys_and_refuses_what_isnt_facts() {
        assert_eq!(Facts::from_json(r#"{"with":["sam"],"mood":"good"}"#), Some(facts(json!({"with_ids": ["sam"]}))));
        assert_eq!(Facts::from_json("not json"), None);
        assert_eq!(Facts::from_json("[1]"), None);
    }

    #[test]
    fn normalized_trims_dedupes_and_drops_blank_notes() {
        let given = facts(json!({"location_ids": [" home ", "home", ""], "with_ids": [" sam", "sam", ""], "notes": {"sam": "  so  glad ", "self": "  "}}));
        assert_eq!(given.normalized(), facts(json!({"location_ids": ["home"], "with_ids": ["sam"], "notes": {"sam": "so glad"}})));
    }

    #[test]
    fn empty_and_people() {
        assert!(Facts::default().is_empty() && facts(json!({"with_ids": [], "notes": {}})).is_empty());
        assert_eq!(facts(json!({"with_ids": ["a"], "for_ids": ["b"], "notes": {"self": "x", "a": "y"}})).people(), vec!["a", "b", "self"]);
    }

    #[test]
    fn problems() {
        let long = "x".repeat(MAX_NOTE_CHARS + 1);
        for (given, problem) in [
            (json!({"location_ids": "home"}), "\"location_ids\" must be a list of location ids"),
            (json!({"with_ids": "sam"}), "\"with_ids\" must be a list of person or circle ids"),
            (json!({"with_ids": ["self"]}), "\"with_ids\" never names \"self\""),
            (json!({"with_ids": ["sam"], "for_ids": ["sam"]}), "['sam'] can't be both \"with\" (there) and \"for\" (not there)"),
            (json!({"notes": ["x"]}), "\"notes\" must be {person id: a note}"),
            (json!({"notes": {"self": long}}), "\"notes\" for ['self'] must be at most"),
            (json!({"for_ids": ["mom"], "notes": {"mom": "x"}}), "\"notes\" are for the people who were there"),
        ] {
            let found = fact_problems(&facts(given.clone()), &Default::default());
            assert!(found.first().is_some_and(|f| f.starts_with(problem)), "{given}: {found:?}");
        }
    }

    fn family() -> crate::attendees::Circles {
        use crate::attendees::{Circles, Members};
        use crate::stores::people::CircleKind;
        Circles(vec![Members { id: "fam".into(), kind: CircleKind::Circle, members: vec!["roger".into(), "cecile".into(), "ulyssie".into()] }])
    }

    #[test]
    fn a_circle_is_named_with_its_except_and_a_share_and_stands_for_its_members() {
        let given = facts(json!({
            "with_ids": [{"circle_id": "fam", "except": ["ulyssie"], "minutes": 30}],
            "notes": {"roger": "chatty"},
            "logs": {"cecile": {"kind/patience": "waited"}},
        }));
        assert_eq!(fact_problems(&given, &family()), Vec::<String>::new());
        let share = &given.with_shares()[0];
        assert_eq!((share.person_id.as_str(), share.circle, share.except.clone(), share.minutes), ("fam", true, vec!["ulyssie".to_string()], Some(30)));
        assert_eq!(serde_json::to_value(share).unwrap(), json!({"circle_id": "fam", "except": ["ulyssie"], "minutes": 30}));
        // Ulyssie wasn't there: no note for her.
        let absent = facts(json!({"with_ids": [{"circle_id": "fam", "except": ["ulyssie"]}], "notes": {"ulyssie": "x"}}));
        assert!(fact_problems(&absent, &family())[0].starts_with("\"notes\" are for the people who were there"));
    }

    #[test]
    fn a_member_cant_be_with_while_their_circle_is_for() {
        let found = fact_problems(&facts(json!({"with_ids": ["roger"], "for_ids": ["fam"]})), &family());
        assert_eq!(found, vec!["['roger'] can't be both \"with\" (there) and \"for\" (not there): a plain circle stands for its members"]);
        let except = fact_problems(&facts(json!({"with_ids": ["roger"], "for_ids": [{"circle_id": "fam", "except": ["roger"]}]})), &family());
        assert!(except.is_empty(), "{except:?}");
        assert!(!fact_problems(&facts(json!({"with_ids": [{"circle_id": "fam", "except": [""]}]})), &family()).is_empty());
    }

    #[test]
    fn people_and_places_can_take_shares_and_bare_ids_still_read() {
        let given = facts(json!({
            "location_ids": ["home", {"location_id": "park", "minutes": 20}],
            "with_ids": [{"person_id": "sam", "percent": 50, "remaining": true}, "kim"],
            "for_ids": ["mom"],
        }));

        assert_eq!(given.locations(), vec!["home", "park"]);
        assert_eq!(given.with(), vec!["sam", "kim"]);
        assert_eq!(given.location_shares(), vec![LocationShare::whole("home"), LocationShare { location_id: "park".into(), minutes: Some(20), ..Default::default() }]);
        assert_eq!(given.with_shares()[0], PersonShare { person_id: "sam".into(), percent: Some(50), remaining: true, ..Default::default() });
        assert!(fact_problems(&given, &Default::default()).is_empty());
        assert_eq!(Facts::from_json(&given.to_json()), Some(given));
    }

    #[test]
    fn normalized_trims_and_dedupes_shares_by_id() {
        let given = facts(json!({"with_ids": [{"person_id": " sam ", "minutes": 10}, "sam", "kim"]}));
        assert_eq!(given.normalized(), facts(json!({"with_ids": [{"person_id": "sam", "minutes": 10}, "kim"]})));
    }

    #[test]
    fn a_share_must_be_well_formed_and_fit_in_its_event() {
        for given in [
            json!({"with_ids": [{"location_id": "sam"}]}),
            json!({"with_ids": [{"person_id": "sam", "hours": 1}]}),
            json!({"location_ids": [{"location_id": "home", "minutes": "ten"}]}),
            json!({"with_ids": [{"person_id": "self", "minutes": 5}]}),
        ] {
            assert!(!fact_problems(&facts(given.clone()), &Default::default()).is_empty(), "{given}");
        }
        let long = facts(json!({"with_ids": [{"person_id": "sam", "minutes": 40}, {"person_id": "kim", "minutes": 30}], "for_ids": [{"person_id": "mom", "minutes": 60}]}));
        assert_eq!(long.share_problems(60), vec!["\"with_ids\": its people's minutes add up to 70, more than the event's 60"]);
    }

    #[test]
    fn renamed_items_keep_their_shares() {
        let items = vec![json!("new:sam"), json!({"person_id": "new:kim", "minutes": 5}), json!("gone")];
        let rename = |id: &str| match id {
            "gone" => None,
            other => Some(other.trim_start_matches("new:").to_string()),
        };
        assert_eq!(renamed_items(&items, &rename), vec![json!("sam"), json!({"person_id": "kim", "minutes": 5})]);
    }

    #[test]
    fn well_formed_facts_have_no_problems() {
        let given = facts(json!({"location_ids": ["home", "park"], "with_ids": ["sam"], "for_ids": ["mom"], "notes": {"self": "a", "sam": "b"}}));
        assert!(fact_problems(&given, &Default::default()).is_empty());

    }
}
