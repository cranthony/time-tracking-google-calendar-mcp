//! Traits: how the user wants to be with people, each rated from parts read
//! off events -- kept in the Traits tab, seeded with SEED_TRAITS the first
//! time.

use crate::contract::{wire_text, RowItem, Schema};
use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::sheet::rows::{self, Row, RowSheet, RowType};
use crate::sheet::TRAITS;
use crate::stores::groups::{python_repr, python_str, repr_or_none, str_or_none};
use crate::stores::scoring::{aggregation_problems, judgment_scoring_problems};
use anyhow::bail;
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::sync::LazyLock;

pub const TRAIT_STATUSES: [&str; 3] = ["active", "off", "archived"];
pub const DEFAULT_TRAIT_STATUSES: [&str; 2] = ["active", "off"];
pub const MAX_NAME_LENGTH: usize = 50;
const MAX_ID_LENGTH: usize = 30;
pub const ENGAGEMENT_TYPES: [&str; 2] = ["with", "for"];
const COMMON: [&str; 2] = ["id", "engagement_type"];
/// The facts any judgment can be made from; a trait's own facts are named
/// "fact:<id>" (TRAIT_FACT_PREFIX).
pub const FACTS: [&str; 5] = ["action", "action_history", "location", "location_history", "general_notes"];
pub const TRAIT_FACT_PREFIX: &str = "fact:";
/// The id of the seeded Thoughtful trait's fact.
const WHAT_MATTERS: &str = "what_matters";
const FACT_FIELDS: [&str; 4] = ["id", "name", "summary_prompt", "log_prompt"];
const MAX_FACT_ID_LENGTH: usize = 30;
pub const HISTORY_FACTS: [&str; 2] = ["action_history", "location_history"];
pub const DEFAULT_LOOKBACK_DAYS: i64 = 30;
pub const DEFAULT_TARGET: i64 = 1;
pub const DEFAULT_WITHIN_DAYS: i64 = 14;
pub const DEFAULT_WINDOW_DAYS: i64 = 30;
pub const CLEARABLE_TRAIT_FIELDS: [&str; 3] = ["definition", "scoring", "facts"];
const MAX_PART_ID_LENGTH: usize = 30;
static PART_ID: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^[A-Za-z0-9_#-]+$").unwrap());

/// Each part kind's (required, optional) fields.
pub fn part_kind(kind: &str) -> Option<(&'static [&'static str], &'static [&'static str])> {
    match kind {
        "judgment" => Some((&["rubric", "ratings", "facts"], &["window_days", "scoring"])),
        "continuity" => Some((&[], &["last_within_days", "next_within_days", "action"])),
        "count" => Some((&["target"], &["noun", "interval_days", "zero_at_days", "action"])),
        "duration" => Some((&["target_min"], &["interval_days", "zero_at_days", "action"])),
        "follow_through" => Some((&[], &["penalty", "recovery", "look_back_days", "action"])),
        "spacing" => Some((&["action", "max_gap_hours"], &["exceptions"])),
        _ => None,
    }
}

pub const PART_KIND_NAMES: [&str; 6] = ["judgment", "continuity", "count", "duration", "follow_through", "spacing"];

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum TraitStatus {
    #[serde(rename = "active")]
    Active,
    #[serde(rename = "off")]
    Off,
    #[serde(rename = "archived")]
    Archived,
}

/// One trait: how the user wants to be with people, and the parts it's
/// scored by (see get_traits).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema, RowItem)]
pub struct Trait {
    pub id: Option<String>,
    pub name: Option<String>,
    #[row(text)]
    pub status: Option<TraitStatus>,
    pub definition: Option<String>,
    pub parts: Option<Vec<Map<String, Value>>>,
    /// How its parts' scores combine into its own: an aggregation (see
    /// get_traits), its weights by part id. Without it, the plain mean.
    pub scoring: Option<Map<String, Value>>,
    /// What it keeps about each person it applies to, for its judgments
    /// (see get_traits): each {"id", "name", "summary_prompt",
    /// "log_prompt"?}.
    pub facts: Option<Vec<Map<String, Value>>>,
    /// Goes up by one each time it changes. Give it to an update (and to a delete, as if_version): it's the version you last read, required -- one saved before versions is version 0 -- changed since, the change is refused; on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK.
    pub version: Option<i64>,
}

impl Trait {
    /// Its parts as JSON values, none when it has none.
    pub fn part_values(&self) -> Vec<Value> {
        self.parts.iter().flatten().map(|p| Value::Object(p.clone())).collect()
    }

    /// Its facts' ids, in order.
    pub fn fact_ids(&self) -> Vec<String> {
        self.facts.iter().flatten().filter_map(|f| f.get("id").and_then(Value::as_str)).map(str::to_string).collect()
    }

    /// Its fact `id`'s definition, if it has one.
    pub fn fact(&self, id: &str) -> Option<&Map<String, Value>> {
        self.facts.iter().flatten().find(|f| f.get("id").and_then(Value::as_str) == Some(id))
    }
}

/// What the seeded Thoughtful trait keeps about what matters to each
/// person.
pub fn what_matters_fact() -> Map<String, Value> {
    json!({
        "id": WHAT_MATTERS,
        "name": "What matters",
        "summary_prompt": "What's important to them, so I can remember it and prepare for it: the people, interests, plans, hopes, worries and coming events in their life that they care about. A Markdown list, kept current -- add what's new, change what's changed, and drop what no longer holds.",
        "log_prompt": "What changed in what matters to them, and what in the event showed it."
    })
    .as_object()
    .unwrap()
    .clone()
}

/// A trait as get_traits lists it: plus anything wrong with it, as hand
/// edits can leave it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct ListedTrait {
    pub id: Option<String>,
    pub name: Option<String>,
    pub status: Option<TraitStatus>,
    pub definition: Option<String>,
    pub parts: Option<Vec<Map<String, Value>>>,
    pub scoring: Option<Map<String, Value>>,
    pub facts: Option<Vec<Map<String, Value>>>,
    pub problems: Option<Vec<String>>,
    pub version: Option<i64>,
}

pub const TRAIT_ROW: RowType = <Trait as rows::RowItem>::ROW_TYPE;

fn effort_ratings() -> Value {
    json!({
        "0": "Little effort, or my attention was elsewhere",
        "1": "Some effort or attention",
        "2": "Real effort, or my full attention",
        "3": "I went out of my way for them and was fully present"
    })
}

/// What a new Traits tab starts with.
pub fn seed_traits() -> Vec<Trait> {
    let t = |id: &str, name: &str, definition: &str, parts: Value| Trait {
        id: Some(id.into()),
        name: Some(name.into()),
        status: Some(TraitStatus::Active),
        definition: Some(definition.into()),
        parts: Some(parts.as_array().unwrap().iter().map(|p| p.as_object().unwrap().clone()).collect()),
        scoring: None,
        facts: None,
        version: None,
    };
    let mut thoughtful = t(
            "thoughtful",
            "Thoughtful",
            "Remember what matters to them, and prepare for it.",
            json!([
                {
                    "kind": "judgment",
                    "rubric": "Did this event show that I remembered what matters to them?",
                    "ratings": {
                        "0": "Nothing in it touched on what matters to them",
                        "1": "A small sign that I remembered",
                        "2": "It clearly reflected what matters to them",
                        "3": "It was shaped around what matters to them"
                    },
                    "facts": ["action", "general_notes", "fact:what_matters"]
                },
                {
                    "kind": "judgment",
                    "engagement_type": "for",
                    "rubric": "Was this preparation for them, built on what matters to them?",
                    "ratings": {
                        "0": "It wasn't really for them",
                        "1": "It was for them, but generic",
                        "2": "It was for them, and fit what matters to them",
                        "3": "It was carefully made around what matters to them"
                    },
                    "facts": ["action", "general_notes", "fact:what_matters"]
                }
            ]),
        );
    thoughtful.facts = Some(vec![what_matters_fact()]);
    vec![
        thoughtful,
        t(
            "reliable",
            "Reliable",
            "Do what I said I would, and keep contact going -- reliable to myself too.",
            json!([
                {"kind": "continuity", "last_within_days": 14, "next_within_days": 14},
                {"kind": "follow_through"},
                {"kind": "count", "target": 1, "interval_days": 14, "zero_at_days": 30, "noun": "meetings"}
            ]),
        ),
        t(
            "creative",
            "Creative",
            "Make something together with them (making something alone is taking care of myself).",
            json!([
                {
                    "kind": "judgment",
                    "rubric": "Did we make something together?",
                    "ratings": {
                        "0": "Nothing was made",
                        "1": "We shared ideas, but made nothing",
                        "2": "We made something small together",
                        "3": "We made something substantial together"
                    },
                    "facts": ["action", {"fact": "action_history", "lookback_days": 30}, "general_notes"]
                }
            ]),
        ),
        t(
            "adventurous",
            "Adventurous",
            "Share new experiences with them: new activities, new places.",
            json!([
                {
                    "kind": "judgment",
                    "rubric": "Was this activity or place new?",
                    "ratings": {
                        "0": "The activity and place were routine",
                        "1": "There was a twist on the activity or place",
                        "2": "The activity or the place were new",
                        "3": "Both the activity and place were new, or the event was otherwise adventurous"
                    },
                    "facts": [
                        "action",
                        {"fact": "action_history", "lookback_days": 90},
                        "location",
                        {"fact": "location_history", "lookback_days": 90}
                    ]
                }
            ]),
        ),
        t(
            "generous",
            "Generous",
            "Make an effort for them, including the attention I give them: the follow-through of thoughtfulness.",
            json!([
                {
                    "kind": "judgment",
                    "rubric": "How much effort and attention did I give them?",
                    "ratings": effort_ratings(),
                    "facts": ["action", "general_notes"]
                },
                {
                    "kind": "judgment",
                    "engagement_type": "for",
                    "rubric": "How much effort did I put into doing this for them?",
                    "ratings": effort_ratings(),
                    "facts": ["action", "general_notes"]
                }
            ]),
        ),
    ]
}

/// Python's `isinstance(v, (int, float)) and not isinstance(v, bool)`.
pub fn is_number(value: &Value) -> bool {
    value.is_number()
}

/// `isinstance(v, int)`, which a bool also is.
pub fn is_int(value: &Value) -> bool {
    value.is_i64() || value.is_u64() || value.is_boolean()
}

fn number(value: &Value) -> f64 {
    match value {
        Value::Bool(b) => *b as i64 as f64,
        other => other.as_f64().unwrap_or(f64::NAN),
    }
}

pub fn trait_problems(t: &Trait) -> Vec<String> {
    let mut problems = Vec::new();
    match t.name.as_deref().filter(|n| !n.trim().is_empty()) {
        None => problems.push("it needs a name".to_string()),
        Some(name) if name.chars().count() > MAX_NAME_LENGTH => {
            problems.push(format!("its name is longer than {MAX_NAME_LENGTH} characters"))
        }
        _ => {}
    }
    if t.status.is_none() {
        problems.push(format!("its status must be one of {}", TRAIT_STATUSES.join(", ")));
    }
    problems.extend(fact_definition_problems(t.facts.as_deref().unwrap_or_default()));
    let parts = t.part_values();
    if parts.is_empty() {
        problems.push("it needs parts: a list of at least one part, each an object with a \"kind\"".into());
        return problems;
    }
    let fact_ids = t.fact_ids();
    for (number, part) in parts.iter().enumerate() {
        for p in part_problems_among(part, Some(&fact_ids)) {
            problems.push(format!("part {}{} {p}", number + 1, kind_of(part)));
        }
    }
    problems.extend(repeated_part_ids(&parts));
    if let Some(scoring) = &t.scoring {
        for p in aggregation_problems(&Value::Object(scoring.clone()), Some(&part_keys(&parts))) {
            problems.push(format!("its scoring: {p}"));
        }
    }
    problems
}

/// The part ids that more than one of `parts` has.
fn repeated_part_ids(parts: &[Value]) -> Vec<String> {
    let ids: Vec<&str> = parts.iter().filter_map(|p| p.get("id").and_then(Value::as_str)).collect();
    let mut repeated: Vec<&str> = ids.iter().copied().filter(|i| ids.iter().filter(|j| *j == i).count() > 1).collect();
    repeated.sort();
    repeated.dedup();
    repeated.iter().map(|id| format!("its parts' id {} is used more than once", py::repr(id))).collect()
}

/// What's wrong with a trait's facts: each {"id", "name",
/// "summary_prompt", "log_prompt"?}, ids and names each used once.
fn fact_definition_problems(facts: &[Map<String, Value>]) -> Vec<String> {
    let mut problems = Vec::new();
    let (mut ids, mut names): (Vec<String>, Vec<String>) = (Vec::new(), Vec::new());
    for (number, fact) in facts.iter().enumerate() {
        let label = format!("fact {}", number + 1);
        let mut extra: Vec<&String> = fact.keys().filter(|k| !FACT_FIELDS.contains(&k.as_str())).collect();
        extra.sort();
        for name in extra {
            problems.push(format!("{label} has no field \"{name}\"; a fact takes \"id\", \"name\", \"summary_prompt\" and \"log_prompt\""));
        }
        match fact.get("id").and_then(Value::as_str) {
            Some(id) if id.chars().count() <= MAX_FACT_ID_LENGTH && PART_ID.is_match(id) => {
                if ids.iter().any(|i| i == id) {
                    problems.push(format!("its facts' id {} is used more than once", py::repr(id)));
                }
                ids.push(id.to_string());
            }
            _ => problems.push(format!(
                "{label}'s \"id\" must be text of at most {MAX_FACT_ID_LENGTH} letters, digits, \"_\", \"-\" or \"#\" (leave it out of a new fact, and one is made from its name)"
            )),
        }
        match fact.get("name").and_then(Value::as_str).map(str::trim).filter(|n| !n.is_empty()) {
            Some(name) if name.chars().count() > MAX_NAME_LENGTH => {
                problems.push(format!("{label}'s name is longer than {MAX_NAME_LENGTH} characters"))
            }
            Some(name) => {
                if names.iter().any(|n| n.to_lowercase() == name.to_lowercase()) {
                    problems.push(format!("its facts' name {} is used more than once", py::repr(name)));
                }
                names.push(name.to_string());
            }
            None => problems.push(format!("{label} needs a \"name\"")),
        }
        if !fact.get("summary_prompt").and_then(Value::as_str).is_some_and(|p| !p.trim().is_empty()) {
            problems.push(format!("{label} needs a \"summary_prompt\": what its summary of a person keeps, and how"));
        }
        if fact.get("log_prompt").is_some_and(|p| !p.is_null() && !p.as_str().is_some_and(|p| !p.trim().is_empty())) {
            problems.push(format!("{label}'s \"log_prompt\" must be non-empty text, or left out"));
        }
    }
    problems
}

/// `facts` with an id for each new one (one without): its name in
/// lowercase, words joined by "_", and none another of them has.
pub fn with_new_fact_ids(facts: &[Map<String, Value>]) -> Vec<Map<String, Value>> {
    let mut taken: Vec<String> = facts.iter().filter_map(|f| f.get("id").and_then(Value::as_str)).map(str::to_string).collect();
    facts
        .iter()
        .map(|fact| {
            if fact.contains_key("id") {
                return fact.clone();
            }
            let name = fact.get("name").and_then(Value::as_str).unwrap_or_default();
            let words = NOT_SLUG.replace_all(&name.to_lowercase(), "_").to_string();
            let base: String = words.trim_matches('_').chars().take(MAX_FACT_ID_LENGTH - 3).collect();
            let base = match base.trim_matches('_') {
                "" => "fact".to_string(),
                trimmed => trimmed.to_string(),
            };
            let mut id = base.clone();
            let mut n = 2;
            while taken.contains(&id) {
                id = format!("{base}_{n}");
                n += 1;
            }
            taken.push(id.clone());
            let mut out = Map::new();
            out.insert("id".into(), json!(id));
            out.extend(fact.clone());
            out
        })
        .collect()
}

/// What's wrong with a part, any "fact:<id>" it names taken to be one of
/// its trait's facts (see part_problems_among).
pub fn part_problems(part: &Value) -> Vec<String> {
    part_problems_among(part, None)
}

/// What's wrong with a part; with `fact_ids`, each "fact:<id>" it names
/// must be one of them, its trait's facts.
pub fn part_problems_among(part: &Value, fact_ids: Option<&[String]>) -> Vec<String> {
    let Some(object) = part.as_object().filter(|o| o.get("kind").is_some_and(Value::is_string)) else {
        return vec!["must be an object with a \"kind\"".into()];
    };
    let kind = object["kind"].as_str().unwrap();
    let Some((required, optional)) = part_kind(kind) else {
        return vec![format!("has kind {}; a part's kind is one of {}", py::repr(kind), PART_KIND_NAMES.join(", "))];
    };
    let mut missing: Vec<&str> = required.iter().copied().filter(|name| !object.contains_key(*name)).collect();
    missing.sort();
    let mut problems: Vec<String> = missing.iter().map(|name| format!("needs \"{name}\"")).collect();
    let mut extra: Vec<&String> = object
        .keys()
        .filter(|k| !required.contains(&k.as_str()) && !optional.contains(&k.as_str()) && !COMMON.contains(&k.as_str()) && k.as_str() != "kind")
        .collect();
    extra.sort();
    for name in extra {
        if name == "weight" {
            problems.push("has no field \"weight\": a part's weight is set in its trait's scoring, as \"weights\": {part id: weight}".into());
            continue;
        }
        let mut takes: Vec<&str> = required.iter().chain(optional.iter()).chain(COMMON.iter()).copied().collect();
        takes.sort();
        let takes: Vec<String> = takes.iter().map(|n| format!("\"{n}\"")).collect();
        problems.push(format!("has no field \"{name}\"; a {kind} part takes {}", takes.join(", ")));
    }
    if !problems.is_empty() {
        return problems;
    }
    if let Some(id) = object.get("id") {
        if !id.as_str().is_some_and(|i| i.chars().count() <= MAX_PART_ID_LENGTH && PART_ID.is_match(i)) {
            problems.push(format!(
                "\"id\" must be text of at most {MAX_PART_ID_LENGTH} letters, digits, \"_\", \"-\" or \"#\" (leave it out of a new part, and one is made)"
            ));
        }
    }
    if let Some(engagement) = object.get("engagement_type") {
        if !ENGAGEMENT_TYPES.iter().any(|e| engagement.as_str() == Some(*e)) {
            problems.push("\"engagement_type\" must be \"with\" or \"for\"".into());
        }
    }
    if let Some(action) = object.get("action") {
        if !action.as_str().is_some_and(|a| !a.trim().is_empty()) {
            problems.push("\"action\" must be an action or action group id".into());
        }
    }
    for name in ["max_gap_hours", "target", "target_min", "interval_days", "last_within_days", "next_within_days", "look_back_days", "window_days"] {
        if let Some(value) = object.get(name) {
            if !(is_number(value) && number(value) > 0.0) {
                problems.push(format!("\"{name}\" must be a number above 0"));
            }
        }
    }
    for name in ["penalty", "recovery"] {
        if let Some(value) = object.get(name) {
            if !(is_number(value) && (0.0..=100.0).contains(&number(value))) {
                problems.push(format!("\"{name}\" must be a number from 0 to 100"));
            }
        }
    }
    if let Some(zero) = object.get("zero_at_days") {
        let interval = object.get("interval_days").cloned().unwrap_or(json!(DEFAULT_WINDOW_DAYS));
        if !(is_number(zero) && is_number(&interval) && number(zero) > number(&interval)) {
            problems.push("\"zero_at_days\" must be a number above \"interval_days\"".into());
        }
    }
    if let Some(noun) = object.get("noun") {
        if !noun.as_str().is_some_and(|n| !n.trim().is_empty()) {
            problems.push("\"noun\" must be non-empty text".into());
        }
    }
    if kind == "judgment" {
        problems.extend(judgment_problems(object, fact_ids));
        if let Some(scoring) = object.get("scoring") {
            problems.extend(judgment_scoring_problems(scoring).into_iter().map(|p| format!("\"scoring\": {p}")));
        }
    }
    if let Some(exceptions) = object.get("exceptions") {
        problems.extend(exception_problems(exceptions));
    }
    problems
}

/// A spacing part's exceptions: each {"actions": [ids], "reset_hours":
/// n}, the clock stopped at their events and, after one, set to
/// reset_hours left (if given).
fn exception_problems(exceptions: &Value) -> Vec<String> {
    let Some(items) = exceptions.as_array() else {
        return vec!["\"exceptions\" must be a list of {\"actions\": [action ids], \"reset_hours\": hours}".into()];
    };
    let mut problems = Vec::new();
    for (i, item) in items.iter().enumerate() {
        let Some(object) = item.as_object() else {
            problems.push(format!("exception {i} must be an object with \"actions\""));
            continue;
        };
        let mut extra: Vec<&String> = object.keys().filter(|k| !["actions", "reset_hours"].contains(&k.as_str())).collect();
        extra.sort();
        for name in extra {
            problems.push(format!("exception {i} has no field \"{name}\"; an exception takes \"actions\", \"reset_hours\""));
        }
        match object.get("actions").and_then(Value::as_array) {
            Some(actions) if !actions.is_empty() && actions.iter().all(|a| a.as_str().is_some_and(|a| !a.trim().is_empty())) => {}
            _ => problems.push(format!("exception {i}'s \"actions\" must be a list of action or action group ids")),
        }
        if let Some(reset) = object.get("reset_hours") {
            if !(is_number(reset) && number(reset) >= 0.0) {
                problems.push(format!("exception {i}'s \"reset_hours\" must be a number, 0 or more"));
            }
        }
    }
    problems
}

/// The facts a judgment part can name, for its refusals.
fn fact_names() -> String {
    format!("{}, or one of its trait's facts as \"{TRAIT_FACT_PREFIX}<fact id>\"", FACTS.join(", "))
}

fn judgment_problems(part: &Map<String, Value>, fact_ids: Option<&[String]>) -> Vec<String> {
    let mut problems = Vec::new();
    if !part["rubric"].as_str().is_some_and(|r| !r.trim().is_empty()) {
        problems.push("\"rubric\" must be non-empty text: the question to judge each event by".into());
    }
    let ratings_ok = match &part["ratings"] {
        Value::Object(ratings) => {
            ratings.len() >= 2
                && ratings.keys().all(|k| !k.is_empty() && k.chars().all(|c| c.is_numeric()))
                && ratings.values().all(|v| v.as_str().is_some_and(|s| !s.trim().is_empty()))
        }
        _ => false,
    };
    if !ratings_ok {
        problems.push(
            "\"ratings\" must be an object of at least two ratings, each a whole number (as text, from \"0\") saying what it means: {\"0\": \"routine\", \"1\": \"a twist\", ...}".into(),
        );
    }
    let Value::Array(facts) = &part["facts"] else {
        problems.push(format!("\"facts\" must be a list of facts to judge by: {}", fact_names()));
        return problems;
    };
    let mut names: Vec<String> = Vec::new();
    for fact in facts {
        let name = match fact {
            Value::Object(o) => o.get("fact").cloned().unwrap_or(Value::Null),
            other => other.clone(),
        };
        let Some(name) = name.as_str().filter(|n| FACTS.contains(n) || trait_fact_id(n).is_some()) else {
            problems.push(format!("\"facts\" has {}; a fact is one of {}", python_repr(fact), fact_names()));
            continue;
        };
        if let (Some(id), Some(known)) = (trait_fact_id(name), fact_ids) {
            if !known.iter().any(|k| k == id) {
                problems.push(format!(
                    "\"facts\" names {}, which isn't one of its trait's facts{}",
                    py::repr(name),
                    if known.is_empty() { " (it has none)".to_string() } else { format!(" ({})", known.join(", ")) }
                ));
            }
        }
        names.push(name.to_string());
        if let Value::Object(o) = fact {
            let mut extra: Vec<&String> = o.keys().filter(|k| k.as_str() != "fact" && k.as_str() != "lookback_days").collect();
            extra.sort();
            if let Some(first) = extra.first() {
                problems.push(format!("\"facts\": {name} has no field {}; it takes \"fact\" and \"lookback_days\"", py::repr(first)));
            }
            if let Some(lookback) = o.get("lookback_days") {
                if !HISTORY_FACTS.contains(&name) {
                    problems.push(format!("\"facts\": only {} look back, not {name}", HISTORY_FACTS.join(" and ")));
                } else if !(is_int(lookback) && number(lookback) >= 1.0) {
                    problems.push(format!("\"facts\": {name}'s \"lookback_days\" must be a whole number, 1 or more"));
                }
            }
        }
    }
    let mut repeated: Vec<&String> = names.iter().filter(|n| names.iter().filter(|m| m == n).count() > 1).collect();
    repeated.sort();
    repeated.dedup();
    if !repeated.is_empty() {
        problems.push(format!("\"facts\" names {} more than once", repeated.iter().map(|s| s.as_str()).collect::<Vec<_>>().join(", ")));
    }
    problems
}

/// The trait fact's id a judgment fact names ("fact:<id>"), if it names one.
pub fn trait_fact_id(name: &str) -> Option<&str> {
    name.strip_prefix(TRAIT_FACT_PREFIX).filter(|id| !id.is_empty())
}

/// The highest rating a judgment part allows.
pub fn judgment_scale(part: &Value) -> i64 {
    part["ratings"].as_object().map(|r| r.keys().filter_map(|k| k.parse::<i64>().ok()).max().unwrap_or(0)).unwrap_or(0)
}

/// A judgment part's facts, each with its lookback in days.
pub fn fact_lookbacks(part: &Value) -> Vec<(String, i64)> {
    let mut out: Vec<(String, i64)> = Vec::new();
    for fact in part["facts"].as_array().into_iter().flatten() {
        let (name, lookback) = match fact {
            Value::Object(o) => {
                let name = python_str(o.get("fact"));
                let default = if HISTORY_FACTS.contains(&name.as_str()) { DEFAULT_LOOKBACK_DAYS } else { 0 };
                (name, o.get("lookback_days").and_then(Value::as_i64).unwrap_or(default))
            }
            other => {
                let name = python_str(Some(other));
                let default = if HISTORY_FACTS.contains(&name.as_str()) { DEFAULT_LOOKBACK_DAYS } else { 0 };
                (name, default)
            }
        };
        if let Some(slot) = out.iter_mut().find(|(n, _)| *n == name) {
            slot.1 = lookback;
        } else {
            out.push((name, lookback));
        }
    }
    out
}

/// What's wrong with a person's (or habit's) `traits`, as phrases
/// following "its traits".
const SPEC_FIELDS: [&str; 4] = ["select", "parts", "part_scoring", "scoring"];

pub fn person_traits_problems(spec: &Value, trait_ids: Option<&[String]>) -> Vec<String> {
    let Value::Object(spec) = spec else {
        return vec!["must be an object: {\"select\": \"all\" or [trait ids], \"parts\": {trait id: [parts]}, \"part_scoring\": {trait id: scoring}, \"scoring\": scoring}".into()];
    };
    let mut extra: Vec<&String> = spec.keys().filter(|k| !SPEC_FIELDS.contains(&k.as_str())).collect();
    extra.sort();
    let mut problems: Vec<String> =
        extra.iter().map(|n| format!("has no field \"{n}\"; it takes \"select\", \"parts\", \"part_scoring\" and \"scoring\"")).collect();
    if let Some(scoring) = spec.get("scoring") {
        for p in aggregation_problems(scoring, None) {
            problems.push(format!("scoring: {p}"));
        }
    }
    let mut select = spec.get("select").cloned().unwrap_or(json!("all"));
    let is_list = matches!(&select, Value::Array(items) if items.iter().all(Value::is_string));
    if select != json!("all") && !is_list {
        problems.push("\"select\" must be \"all\" or a list of trait ids".into());
        select = json!("all");
    }
    if let (Some(known), Value::Array(items)) = (trait_ids, &select) {
        for item in items {
            let id = item.as_str().unwrap_or_default();
            if !known.iter().any(|k| k == id) {
                problems.push(format!("\"select\" names {}, which isn't a trait", py::repr(id)));
            }
        }
    }
    let overrides = spec.get("parts").cloned().unwrap_or(json!({}));
    let Value::Object(overrides) = overrides else {
        problems.push("\"parts\" must be {trait id: [parts]}".into());
        return problems;
    };
    for (trait_id, parts) in &overrides {
        let selected = match &select {
            Value::Array(items) => items.iter().any(|i| i.as_str() == Some(trait_id)),
            _ => true,
        };
        if trait_ids.is_some_and(|known| !known.iter().any(|k| k == trait_id)) {
            problems.push(format!("\"parts\" names {}, which isn't a trait", py::repr(trait_id)));
        } else if !selected {
            problems.push(format!("\"parts\" names {}, which isn't selected", py::repr(trait_id)));
        } else {
            match parts {
                Value::Array(list) if !list.is_empty() => {
                    for (number, part) in list.iter().enumerate() {
                        for p in part_problems(part) {
                            problems.push(format!("\"parts\" for {}: part {}{} {p}", py::repr(trait_id), number + 1, kind_of(part)));
                        }
                    }
                    for p in repeated_part_ids(list) {
                        problems.push(format!("\"parts\" for {}: {p}", py::repr(trait_id)));
                    }
                }
                _ => problems.push(format!("\"parts\" for {} must be a list of at least one part", py::repr(trait_id))),
            }
        }
    }
    match spec.get("part_scoring") {
        None => {}
        Some(Value::Object(by_trait)) => {
            for (trait_id, scoring) in by_trait {
                if trait_ids.is_some_and(|known| !known.iter().any(|k| k == trait_id)) {
                    problems.push(format!("\"part_scoring\" names {}, which isn't a trait", py::repr(trait_id)));
                    continue;
                }
                // Checked against their own parts for it, if they have them.
                let own = overrides.get(trait_id).and_then(Value::as_array).map(|l| part_keys(l));
                for p in aggregation_problems(scoring, own.as_deref()) {
                    problems.push(format!("\"part_scoring\" for {}: {p}", py::repr(trait_id)));
                }
            }
        }
        Some(_) => problems.push("\"part_scoring\" must be {trait id: scoring}".into()),
    }
    problems
}

pub fn activity_label(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ").to_lowercase()
}

/// Each part's id, what its judgments and weight are kept under: its own
/// "id", or for a part without one -- as every part was before parts had
/// ids -- the name it had then: "kind" for the first of its kind, then
/// "kind#2"..., passing over any another part has as its id.
pub fn part_keys(parts: &[Value]) -> Vec<String> {
    let given: Vec<Option<String>> = parts.iter().map(part_id).collect();
    let mut taken: Vec<String> = given.iter().flatten().cloned().collect();
    let mut seen: Vec<(String, usize)> = Vec::new();
    parts
        .iter()
        .zip(given)
        .map(|(part, id)| {
            let kind = part.get("kind").and_then(Value::as_str).unwrap_or("part").to_string();
            let count = match seen.iter_mut().find(|(k, _)| *k == kind) {
                Some(slot) => {
                    slot.1 += 1;
                    slot.1
                }
                None => {
                    seen.push((kind.clone(), 1));
                    1
                }
            };
            if let Some(id) = id {
                return id;
            }
            let mut n = count;
            loop {
                let key = if n == 1 { kind.clone() } else { format!("{kind}#{n}") };
                if !taken.contains(&key) {
                    taken.push(key.clone());
                    return key;
                }
                n += 1;
            }
        })
        .collect()
}

fn part_id(part: &Value) -> Option<String> {
    part.get("id").and_then(Value::as_str).filter(|i| !i.is_empty()).map(str::to_string)
}

/// `parts` as kept since parts had ids: each with its id (see
/// part_keys), and without the weight a part had before weights moved to
/// its trait's scoring -- returned, by part id, for any but 1.
pub fn settled_parts(parts: &[Map<String, Value>]) -> (Vec<Map<String, Value>>, Map<String, Value>) {
    let values: Vec<Value> = parts.iter().map(|p| Value::Object(p.clone())).collect();
    let mut weights = Map::new();
    let settled = parts
        .iter()
        .zip(part_keys(&values))
        .map(|(part, id)| {
            let mut out = Map::new();
            out.insert("id".into(), json!(id));
            for (k, v) in part {
                match k.as_str() {
                    "id" => {}
                    "weight" => {
                        if v.as_f64() != Some(1.0) {
                            weights.insert(id.clone(), v.clone());
                        }
                    }
                    _ => {
                        out.insert(k.clone(), v.clone());
                    }
                }
            }
            out
        })
        .collect();
    (settled, weights)
}

/// `scoring` with `weights` (moved off parts) among its weights -- any it
/// already has kept.
pub fn with_weights(scoring: Option<Map<String, Value>>, weights: Map<String, Value>) -> Option<Map<String, Value>> {
    if weights.is_empty() {
        return scoring;
    }
    let mut scoring = scoring.unwrap_or_default();
    let mut merged = weights;
    if let Some(Value::Object(kept)) = scoring.get("weights") {
        for (k, v) in kept {
            merged.insert(k.clone(), v.clone());
        }
    }
    scoring.insert("weights".into(), Value::Object(merged));
    Some(scoring)
}

/// `parts` with an id for each new one (one without): short, random, and
/// none another of them has.
pub fn with_new_part_ids(parts: &[Map<String, Value>]) -> Vec<Map<String, Value>> {
    let mut taken: Vec<String> = parts.iter().filter_map(|p| part_id(&Value::Object(p.clone()))).collect();
    parts
        .iter()
        .map(|part| {
            if part.contains_key("id") {
                return part.clone();
            }
            let id = rows::new_id(taken.iter().map(|t| Some(t.as_str())));
            taken.push(id.clone());
            let mut out = Map::new();
            out.insert("id".into(), json!(id));
            out.extend(part.clone());
            out
        })
        .collect()
}

/// `t` as kept since parts had ids (see settled_parts).
pub fn settled(mut t: Trait) -> Trait {
    if let Some(parts) = &t.parts {
        let (parts, weights) = settled_parts(parts);
        t.parts = Some(parts);
        t.scoring = with_weights(t.scoring.take(), weights);
    }
    t
}

/// A person's or habit's own parts for each trait, as a list of objects,
/// or None if they aren't one.
fn own_parts(parts: &Value) -> Option<Vec<Map<String, Value>>> {
    parts.as_array().filter(|l| l.iter().all(Value::is_object)).map(|l| l.iter().filter_map(|p| p.as_object().cloned()).collect())
}

/// A person's or habit's traits as kept since parts had ids: their own
/// parts for each trait settled, and those parts' weights moved to their
/// part_scoring for it.
pub fn settled_spec(spec: Map<String, Value>) -> Map<String, Value> {
    let Some(Value::Object(overrides)) = spec.get("parts") else {
        return spec;
    };
    let mut part_scoring = match spec.get("part_scoring") {
        Some(Value::Object(m)) => m.clone(),
        _ => Map::new(),
    };
    let mut parts_out = Map::new();
    for (trait_id, parts) in overrides {
        let Some(maps) = own_parts(parts) else {
            parts_out.insert(trait_id.clone(), parts.clone());
            continue;
        };
        let (settled, weights) = settled_parts(&maps);
        parts_out.insert(trait_id.clone(), Value::Array(settled.into_iter().map(Value::Object).collect()));
        let existing = part_scoring.get(trait_id).and_then(Value::as_object).cloned();
        if let Some(merged) = with_weights(existing, weights) {
            part_scoring.insert(trait_id.clone(), Value::Object(merged));
        }
    }
    let mut out = spec.clone();
    out.insert("parts".into(), Value::Object(parts_out));
    if !part_scoring.is_empty() {
        out.insert("part_scoring".into(), Value::Object(part_scoring));
    }
    out
}

/// A person's or habit's traits with an id for each new part of their
/// own (see with_new_part_ids).
pub fn spec_with_new_part_ids(spec: &Map<String, Value>) -> Map<String, Value> {
    let Some(Value::Object(overrides)) = spec.get("parts") else {
        return spec.clone();
    };
    let parts: Map<String, Value> = overrides
        .iter()
        .map(|(trait_id, parts)| match own_parts(parts) {
            Some(maps) => (trait_id.clone(), Value::Array(with_new_part_ids(&maps).into_iter().map(Value::Object).collect())),
            None => (trait_id.clone(), parts.clone()),
        })
        .collect();
    let mut out = spec.clone();
    out.insert("parts".into(), Value::Object(parts));
    out
}

fn kind_of(part: &Value) -> String {
    match part.get("kind").and_then(Value::as_str) {
        Some(kind) if PART_KIND_NAMES.contains(&kind) => format!(" ({kind})"),
        _ => String::new(),
    }
}

static NOT_SLUG: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"[^a-z0-9]+").unwrap());

fn slug(name: &str) -> String {
    let replaced = NOT_SLUG.replace_all(&name.to_lowercase(), "-").to_string();
    let trimmed: String = replaced.trim_matches('-').chars().take(MAX_ID_LENGTH).collect();
    let trimmed = trimmed.trim_matches('-').to_string();
    if trimmed.is_empty() { "trait".into() } else { trimmed }
}

#[derive(Clone)]
pub struct Traits {
    sheet: RowSheet,
}

impl Traits {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<Traits> {
        let seed: Vec<Row> = seed_traits().iter().map(rows::RowItem::to_row).collect();
        Ok(Traits { sheet: RowSheet::ensure_seeded(sheets, spreadsheet_id, TRAITS.0, TRAITS.1, TRAIT_ROW, &["id", "name", "status", "parts"], &seed)? })
    }

    pub fn whole_tab(&self) -> TabRange {
        self.sheet.whole_tab()
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheet.prefetch(ranges)
    }

    /// Every trait, settled (see settled).
    pub fn all(&self) -> anyhow::Result<Vec<Trait>> {
        Ok(self.sheet.read_items::<Trait>()?.into_iter().map(settled).collect())
    }

    pub fn ids(&self) -> anyhow::Result<Vec<String>> {
        Ok(self.all()?.into_iter().filter_map(|t| t.id).collect())
    }

    pub fn by_id(&self) -> anyhow::Result<Vec<(String, Trait)>> {
        let mut out: Vec<(String, Trait)> = Vec::new();
        for t in self.all()? {
            if let Some(id) = t.id.clone().filter(|i| !i.is_empty()) {
                out.retain(|(k, _)| *k != id);
                out.push((id, t));
            }
        }
        Ok(out)
    }

    pub fn get_traits(&self, statuses: Option<&[String]>) -> anyhow::Result<Vec<ListedTrait>> {
        let statuses = check_statuses(statuses)?;
        Ok(self
            .all()?
            .into_iter()
            .filter(|t| statuses.iter().any(|s| t.status.as_ref().map(wire_text).as_ref() == Some(s)))
            .map(|t| {
                let problems = trait_problems(&t);
                ListedTrait {
                    id: t.id,
                    name: t.name,
                    status: t.status,
                    definition: t.definition,
                    parts: t.parts,
                    scoring: t.scoring,
                    facts: t.facts,
                    problems: if problems.is_empty() { None } else { Some(problems) },
                    version: t.version,
                }
            })
            .collect())
    }

    pub fn create_trait(&self, given: &Trait) -> anyhow::Result<Trait> {
        let traits = self.all()?;
        if given.id.as_deref().is_some_and(|i| !i.trim().is_empty()) {
            if let rows::Created::Again(made) = rows::created(given, &traits, &[])? {
                return Ok(made);
            }
        }
        let mut new = given.clone();
        if new.status.is_none() {
            new.status = Some(TraitStatus::Active);
        }
        new.parts = new.parts.as_deref().map(with_new_part_ids);
        new.facts = new.facts.as_deref().map(with_new_fact_ids);
        let problems = trait_problems(&new);
        if !problems.is_empty() {
            bail!("{}", said(&new, &problems));
        }
        let taken: Vec<Option<&str>> = traits.iter().map(|t| t.id.as_deref()).collect();
        let id = match given.id.as_deref().map(str::trim).filter(|i| !i.is_empty()) {
            // The client's own.
            Some(chosen) => rows::chosen_id(Some(chosen), taken.clone())?,
            _ => {
                let base = slug(new.name.as_deref().unwrap_or_default());
                let mut id = base.clone();
                let mut n = 2;
                while taken.contains(&Some(id.as_str())) {
                    let keep = MAX_ID_LENGTH.saturating_sub(n.to_string().len() + 1);
                    id = format!("{}-{n}", base.chars().take(keep).collect::<String>());
                    n += 1;
                }
                id
            }
        };
        new.id = Some(id);
        new.version = None;
        check_unique_name(&new, &traits)?;
        let mut all = traits;
        all.push(new.clone());
        self.sheet.write_items(&all)?;
        Ok(new)
    }

    pub fn update_trait(&self, given: &Trait, clear_fields: &[String]) -> anyhow::Result<Trait> {
        let Some(id) = given.id.clone().filter(|i| !i.is_empty()) else {
            bail!("update_trait needs the trait's id");
        };
        rows::check_clear_item(given, clear_fields, &CLEARABLE_TRAIT_FIELDS)?;
        let mut traits = self.all()?;
        let Some(index) = traits.iter().position(|t| t.id.as_deref() == Some(id.as_str())) else {
            rows::expect_item_version::<Trait>(&id, None, given.version)?;
            let known: Vec<String> =
                traits.iter().map(|t| format!("{} ({})", str_or_none(t.id.as_deref()), str_or_none(t.name.as_deref()))).collect();
            bail!("{} isn't a trait; the traits are {}", py::repr(&id), known.join(", "));
        };
        let mut updated = rows::updated_item(&traits[index], given, clear_fields, &["id"])?;
        if let Some(parts) = &given.parts {
            let parts = with_new_part_ids(parts);
            // A part let go takes its weight with it.
            if given.scoring.is_none() {
                updated.scoring = without_weights_of_gone(updated.scoring.take(), &parts);
            }
            updated.parts = Some(parts);
        }
        if let Some(facts) = &given.facts {
            updated.facts = Some(with_new_fact_ids(facts));
        }
        let problems = trait_problems(&updated);
        if !problems.is_empty() {
            bail!("{}", said(&updated, &problems));
        }
        check_unique_name(&updated, &traits)?;
        traits[index] = updated.clone();
        self.sheet.write_items(&traits)?;
        Ok(updated)
    }
}

/// `scoring` with only the weights of `parts` that are still there.
fn without_weights_of_gone(scoring: Option<Map<String, Value>>, parts: &[Map<String, Value>]) -> Option<Map<String, Value>> {
    let mut scoring = scoring?;
    let ids = part_keys(&parts.iter().map(|p| Value::Object(p.clone())).collect::<Vec<_>>());
    if let Some(Value::Object(weights)) = scoring.get_mut("weights") {
        weights.retain(|id, _| ids.contains(id));
    }
    Some(scoring)
}

fn check_unique_name(t: &Trait, traits: &[Trait]) -> anyhow::Result<()> {
    let name = t.name.as_deref().unwrap_or_default().to_lowercase();
    if let Some(clash) = traits.iter().find(|o| o.id != t.id && o.name.as_deref().unwrap_or_default().to_lowercase() == name) {
        bail!("There's already a trait named {} ({})", repr_or_none(clash.name.as_deref()), str_or_none(clash.id.as_deref()));
    }
    Ok(())
}

fn said(t: &Trait, problems: &[String]) -> String {
    let name = match t.name.as_deref().filter(|n| !n.trim().is_empty()) {
        Some(n) => format!("The trait {}", py::repr(n)),
        None => "The trait".into(),
    };
    format!("{name}: {}", problems.join("; "))
}

fn check_statuses(statuses: Option<&[String]>) -> anyhow::Result<Vec<String>> {
    match statuses {
        None => Ok(DEFAULT_TRAIT_STATUSES.iter().map(|s| s.to_string()).collect()),
        Some(given) => {
            let mut unknown: Vec<&String> = given.iter().filter(|s| !TRAIT_STATUSES.contains(&s.as_str())).collect();
            unknown.sort();
            unknown.dedup();
            if !unknown.is_empty() {
                bail!("Unknown trait status(es) {}; statuses are {}", py::repr_list(&unknown), TRAIT_STATUSES.join(", "));
            }
            Ok(given.to_vec())
        }
    }
}

// Written by the app: made once by their ids and versions.
impl rows::Versioned for Trait {}

#[cfg(test)]
#[path = "traits_tests.rs"]
mod tests;
