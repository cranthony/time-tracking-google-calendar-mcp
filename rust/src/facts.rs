//! An event's facts: what compaction established about it -- where (one
//! place or several), who with, who for, and a note on each person there
//! -- and whether it's a commute, getting from one place to another.
//!
//! Each place and person is an id, or, with a share of the event
//! (`LocationShare`, `PersonShare`), an object: {location_id or
//! person_id, minutes?, percent?, remaining?}. Ids stored before shares
//! were are read as they always were.
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

crate::stores::shares::id_share! {
    /// A person an event was with or for, and how much of the event they
    /// take: minutes (a fixed share), percent (of what the minutes leave),
    /// or neither -- the remaining time, what the minutes and percents
    /// leave, all of it, as each person with neither takes it.
    PersonShare, PersonSplit, person_id, "person", "people's", "the remaining time",
    "A person's id: they take the remaining time, what the minutes and percents leave.",
    "A person and how much of the event they take: minutes (a fixed\nshare), or percent (of what the minutes leave)."
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
const ID_KEYS: [&str; 2] = ["person_id", "location_id"];

/// The id of an item of a facts list: the item, or its object's
/// person_id or location_id.
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
}

/// The JSON key each field is stored under, in field order.
const FIELDS: [(&str, &str); 5] =
    [("location_ids", "locations"), ("with_ids", "with"), ("for_ids", "for"), ("notes", "notes"), ("commute", "commute")];

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
        Facts {
            location_ids: ids(&self.location_ids),
            with_ids: ids(&self.with_ids),
            for_ids: ids(&self.for_ids),
            notes,
            commute: self.commute.clone(),
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
                    "commute" => &mut facts.commute,
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
        Facts { location_ids: get("location_ids"), with_ids: get("with_ids"), for_ids: get("for_ids"), notes: get("notes"), commute: get("commute") }
    }
}

/// Whether `value` is a list of ids, each a non-empty id, or an object
/// with one under `key` and a share (see `PersonShare`).
fn is_id_list(value: &Value, key: &str) -> bool {
    match value {
        Value::Array(items) => items.iter().all(|i| match i {
            Value::String(s) => !s.is_empty(),
            Value::Object(map) => {
                map.get(key).and_then(Value::as_str).is_some_and(|s| !s.is_empty())
                    && map.iter().all(|(k, v)| match k.as_str() {
                        "minutes" | "percent" => v.is_null() || v.is_i64(),
                        "remaining" => v.is_boolean(),
                        _ => k == key,
                    })
            }
            _ => false,
        }),
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
            if !v.is_null() && !is_id_list(v, &format!("{of}_id")) {
                problems.push(format!("\"{name}\" must be a list of {of} ids, each an id or {{{of}_id, minutes?, percent?, remaining?}}"));
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
    if facts.commute.as_ref().is_some_and(|c| !c.is_null() && !c.is_boolean()) {
        problems.push("\"commute\" must be true or false".to_string());
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
    fn a_commute_is_kept_and_checked() {
        let commute = facts(json!({"commute": true}));
        assert!(commute.is_commute() && !commute.is_empty());
        assert_eq!(commute.to_json(), r#"{"commute":true}"#);
        assert_eq!(Facts::from_json(&commute.to_json()), Some(commute));
        // Not a commute is no fact at all.
        assert!(facts(json!({"commute": false})).is_empty());
        assert_eq!(facts(json!({"commute": false, "with_ids": ["sam"]})).to_json(), r#"{"with":["sam"]}"#);
        assert_eq!(fact_problems(&facts(json!({"commute": "yes"}))), vec!["\"commute\" must be true or false"]);
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
        assert!(fact_problems(&given).is_empty());
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
            assert!(!fact_problems(&facts(given.clone())).is_empty(), "{given}");
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
        assert!(fact_problems(&given).is_empty());

    }
}
