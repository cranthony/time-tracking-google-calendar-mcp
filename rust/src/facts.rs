//! An event's facts: what compaction established about it -- where (one
//! place or several), who with, who for, and a note on each person there.
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

static SPACES: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"\s+").unwrap());

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Facts {
    pub location_ids: Option<Value>,
    pub with_ids: Option<Value>,
    pub for_ids: Option<Value>,
    pub notes: Option<Value>,
}

/// The JSON key each field is stored under, in field order.
const FIELDS: [(&str, &str); 4] =
    [("location_ids", "locations"), ("with_ids", "with"), ("for_ids", "for"), ("notes", "notes")];

fn is_blank(value: &Option<Value>) -> bool {
    match value {
        None | Some(Value::Null) => true,
        Some(Value::Array(a)) => a.is_empty(),
        Some(Value::Object(o)) => o.is_empty(),
        _ => false,
    }
}

impl Facts {
    fn field(&self, name: &str) -> &Option<Value> {
        match name {
            "location_ids" => &self.location_ids,
            "with_ids" => &self.with_ids,
            "for_ids" => &self.for_ids,
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
                    let mut seen: Vec<String> = Vec::new();
                    for item in items {
                        if let Value::String(s) = item {
                            let trimmed = s.trim();
                            if !trimmed.is_empty() && !seen.iter().any(|x| x == trimmed) {
                                seen.push(trimmed.to_string());
                            }
                        }
                    }
                    Some(Value::Array(seen.into_iter().map(Value::String).collect()))
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
        Facts { location_ids: ids(&self.location_ids), with_ids: ids(&self.with_ids), for_ids: ids(&self.for_ids), notes }
    }

    fn strings(value: &Option<Value>) -> Vec<String> {
        match value {
            Some(Value::Array(items)) => items.iter().filter_map(|v| v.as_str().map(str::to_string)).collect(),
            _ => Vec::new(),
        }
    }

    pub fn with(&self) -> Vec<String> {
        Self::strings(&self.with_ids)
    }

    pub fn for_(&self) -> Vec<String> {
        Self::strings(&self.for_ids)
    }

    pub fn note_people(&self) -> Vec<String> {
        match &self.notes {
            Some(Value::Object(map)) => map.keys().cloned().collect(),
            _ => Vec::new(),
        }
    }

    /// Every person named: with, for, then noted.
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
        Facts { location_ids: get("location_ids"), with_ids: get("with_ids"), for_ids: get("for_ids"), notes: get("notes") }
    }
}

fn is_id_list(value: &Value) -> bool {
    match value {
        Value::Array(items) => items.iter().all(|i| matches!(i, Value::String(s) if !s.is_empty())),
        _ => false,
    }
}

/// Everything wrong with `facts`' shape, as phrases following "its facts".
pub fn fact_problems(facts: &Facts) -> Vec<String> {
    let mut problems = Vec::new();
    for (name, value, of) in
        [("location_ids", &facts.location_ids, "location"), ("with_ids", &facts.with_ids, "person"), ("for_ids", &facts.for_ids, "person")]
    {
        if let Some(v) = value {
            if !v.is_null() && !is_id_list(v) {
                problems.push(format!("\"{name}\" must be a list of {of} ids"));
            }
        }
    }
    if let Some(Value::Array(items)) = &facts.with_ids {
        if items.iter().any(|i| i.as_str() == Some(SELF_ID)) {
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
    }
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
                    let with = facts.with();
                    let mut absent: Vec<String> =
                        map.keys().filter(|k| !with.contains(k) && k.as_str() != SELF_ID).cloned().collect();
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
    if problems.is_empty() && facts.to_json().chars().count() > MAX_FACTS_CHARS {
        problems.push(format!("are longer than {MAX_FACTS_CHARS} characters as JSON"));
    }
    problems
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

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
            (json!({"with_ids": "sam"}), "\"with_ids\" must be a list of person ids"),
            (json!({"with_ids": ["self"]}), "\"with_ids\" never names \"self\""),
            (json!({"with_ids": ["sam"], "for_ids": ["sam"]}), "['sam'] can't be both \"with\" (there) and \"for\" (not there)"),
            (json!({"notes": ["x"]}), "\"notes\" must be {person id: a note}"),
            (json!({"notes": {"self": long}}), "\"notes\" for ['self'] must be at most"),
            (json!({"for_ids": ["mom"], "notes": {"mom": "x"}}), "\"notes\" are for the people who were there"),
        ] {
            let found = fact_problems(&facts(given.clone()));
            assert!(found.first().is_some_and(|f| f.starts_with(problem)), "{given}: {found:?}");
        }
    }

    #[test]
    fn well_formed_facts_have_no_problems() {
        let given = facts(json!({"location_ids": ["home", "park"], "with_ids": ["sam"], "for_ids": ["mom"], "notes": {"self": "a", "sam": "b"}}));
        assert!(fact_problems(&given).is_empty());

    }
}
