//! Traits (utilities/traits.py): how the user wants to be with people,
//! each rated from parts read off events -- kept in the Traits tab, seeded
//! with SEED_TRAITS the first time.

use crate::google::sheets::{Rows, SheetsClient, TabRange};
use crate::py;
use crate::sheet::{create_tab, find_tab, TRAITS};
use crate::stores::groups::{python_repr, python_str};
use anyhow::bail;
use regex::Regex;
use serde_json::{json, Map, Value};
use std::sync::LazyLock;

pub const TRAIT_STATUSES: [&str; 3] = ["active", "off", "archived"];
pub const DEFAULT_TRAIT_STATUSES: [&str; 2] = ["active", "off"];
pub const MAX_NAME_LENGTH: usize = 50;
const MAX_ID_LENGTH: usize = 30;
pub const ENGAGEMENT_TYPES: [&str; 2] = ["with", "for"];
const COMMON: [&str; 2] = ["weight", "engagement_type"];
pub const FACTS: [&str; 7] =
    ["action", "action_history", "location", "location_history", "general_notes", "person_notes", "what_matters"];
pub const HISTORY_FACTS: [&str; 2] = ["action_history", "location_history"];
pub const DEFAULT_LOOKBACK_DAYS: i64 = 30;
pub const DEFAULT_TARGET: i64 = 1;
pub const DEFAULT_WITHIN_DAYS: i64 = 14;
pub const DEFAULT_WINDOW_DAYS: i64 = 30;
pub const HEADER_ROW: [&str; 5] = ["id", "name", "status", "definition", "parts"];
pub const CLEARABLE_TRAIT_FIELDS: [&str; 1] = ["definition"];
const HEADER_RANGE: &str = "A1:Z1";
const DATA_RANGE: &str = "A2:Z";
const WHOLE_RANGE: &str = "A1:Z";

/// Each part kind's (required, optional) fields.
pub fn part_kind(kind: &str) -> Option<(&'static [&'static str], &'static [&'static str])> {
    match kind {
        "judgment" => Some((&["rubric", "ratings", "facts"], &["window_days"])),
        "continuity" => Some((&[], &["last_within_days", "next_within_days", "action"])),
        "count" => Some((&["target"], &["noun", "interval_days", "zero_at_days", "action"])),
        "duration" => Some((&["target_min"], &["interval_days", "zero_at_days", "action"])),
        "follow_through" => Some((&[], &["penalty", "recovery", "look_back_days", "action"])),
        _ => None,
    }
}

pub const PART_KIND_NAMES: [&str; 5] = ["judgment", "continuity", "count", "duration", "follow_through"];

/// A trait: id, name, status, definition and parts, each a JSON value
/// (a hand edit can leave any of them any shape).
#[derive(Debug, Clone, PartialEq, Default)]
pub struct Trait {
    pub id: Option<Value>,
    pub name: Option<Value>,
    pub status: Option<Value>,
    pub definition: Option<Value>,
    pub parts: Option<Value>,
}

impl Trait {
    pub fn get(&self, field: &str) -> &Option<Value> {
        match field {
            "id" => &self.id,
            "name" => &self.name,
            "status" => &self.status,
            "definition" => &self.definition,
            _ => &self.parts,
        }
    }

    fn slot(&mut self, field: &str) -> &mut Option<Value> {
        match field {
            "id" => &mut self.id,
            "name" => &mut self.name,
            "status" => &mut self.status,
            "definition" => &mut self.definition,
            _ => &mut self.parts,
        }
    }

    pub fn id_str(&self) -> Option<&str> {
        self.id.as_ref().and_then(Value::as_str)
    }

    pub fn name_str(&self) -> Option<&str> {
        self.name.as_ref().and_then(Value::as_str)
    }

    pub fn status_str(&self) -> Option<&str> {
        self.status.as_ref().and_then(Value::as_str)
    }

    pub fn from_value(value: &Value) -> Trait {
        let mut t = Trait::default();
        for field in HEADER_ROW {
            *t.slot(field) = value.get(field).filter(|v| !v.is_null()).cloned();
        }
        t
    }

    pub fn to_json(&self) -> Map<String, Value> {
        HEADER_ROW.iter().map(|f| (f.to_string(), self.get(f).clone().unwrap_or(Value::Null))).collect()
    }

    pub fn from_row(header: &[String], data: &[String]) -> Trait {
        let cell = |name: &str| -> Option<String> {
            let i = header.iter().position(|h| h == name)?;
            let text = data.get(i).map(|c| c.trim()).unwrap_or("");
            if text.is_empty() { None } else { Some(text.to_string()) }
        };
        let parts = cell("parts").map(|text| serde_json::from_str::<Value>(&text).unwrap_or(Value::String(text)));
        Trait {
            id: cell("id").map(Value::String),
            name: cell("name").map(Value::String),
            status: cell("status").map(|s| s.to_lowercase()).filter(|s| !s.is_empty()).map(Value::String),
            definition: cell("definition").map(Value::String),
            parts,
        }
    }

    pub fn to_row(&self, header: &[String], original: Option<&Vec<String>>) -> Vec<String> {
        header
            .iter()
            .enumerate()
            .map(|(i, name)| {
                if HEADER_ROW.contains(&name.as_str()) {
                    match self.get(name) {
                        None | Some(Value::Null) => String::new(),
                        Some(Value::String(s)) => s.clone(),
                        Some(v) => py::json_compact_ascii(v),
                    }
                } else {
                    original.and_then(|o| o.get(i)).cloned().unwrap_or_default()
                }
            })
            .collect()
    }
}

fn effort_ratings() -> Value {
    json!({
        "0": "Little effort, or my attention was elsewhere",
        "1": "Some effort or attention",
        "2": "Real effort, or my full attention",
        "3": "I went out of my way for them and was fully present"
    })
}

/// What a new Traits tab starts with -- key for key as utilities/traits.py.
pub fn seed_traits() -> Vec<Trait> {
    let t = |id: &str, name: &str, definition: &str, parts: Value| Trait {
        id: Some(json!(id)),
        name: Some(json!(name)),
        status: Some(json!("active")),
        definition: Some(json!(definition)),
        parts: Some(parts),
    };
    vec![
        t(
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
                    "facts": ["action", "general_notes", "person_notes", "what_matters"]
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
                    "facts": ["action", "general_notes", "person_notes", "what_matters"]
                }
            ]),
        ),
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
                    "facts": ["action", "general_notes", "person_notes"]
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
    match t.name.as_ref().and_then(Value::as_str).filter(|n| !n.trim().is_empty()) {
        None => problems.push("it needs a name".to_string()),
        Some(name) if name.chars().count() > MAX_NAME_LENGTH => {
            problems.push(format!("its name is longer than {MAX_NAME_LENGTH} characters"))
        }
        _ => {}
    }
    if !TRAIT_STATUSES.iter().any(|s| t.status_str() == Some(*s)) {
        problems.push(format!("its status must be one of {}", TRAIT_STATUSES.join(", ")));
    }
    if let Some(definition) = &t.definition {
        if !definition.is_null() && !definition.is_string() {
            problems.push("its definition must be text".into());
        }
    }
    let parts = match &t.parts {
        Some(Value::Array(parts)) if !parts.is_empty() => parts,
        _ => {
            problems.push("it needs parts: a list of at least one part, each an object with a \"kind\"".into());
            return problems;
        }
    };
    for (number, part) in parts.iter().enumerate() {
        for p in part_problems(part) {
            problems.push(format!("part {}{} {p}", number + 1, kind_of(part)));
        }
    }
    problems
}

pub fn part_problems(part: &Value) -> Vec<String> {
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
        let mut takes: Vec<&str> = required.iter().chain(optional.iter()).chain(COMMON.iter()).copied().collect();
        takes.sort();
        let takes: Vec<String> = takes.iter().map(|n| format!("\"{n}\"")).collect();
        problems.push(format!("has no field \"{name}\"; a {kind} part takes {}", takes.join(", ")));
    }
    if !problems.is_empty() {
        return problems;
    }
    if let Some(weight) = object.get("weight") {
        if !(is_number(weight) && number(weight) >= 0.0) {
            problems.push("\"weight\" must be a number, 0 or more".into());
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
    for name in ["target", "target_min", "interval_days", "last_within_days", "next_within_days", "look_back_days", "window_days"] {
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
        problems.extend(judgment_problems(object));
    }
    problems
}

fn judgment_problems(part: &Map<String, Value>) -> Vec<String> {
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
        problems.push(format!("\"facts\" must be a list of facts to judge by: {}", FACTS.join(", ")));
        return problems;
    };
    let mut names: Vec<String> = Vec::new();
    for fact in facts {
        let name = match fact {
            Value::Object(o) => o.get("fact").cloned().unwrap_or(Value::Null),
            other => other.clone(),
        };
        let Some(name) = name.as_str().filter(|n| FACTS.contains(n)) else {
            problems.push(format!("\"facts\" has {}; a fact is one of {}", python_repr(fact), FACTS.join(", ")));
            continue;
        };
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
pub fn person_traits_problems(spec: &Value, trait_ids: Option<&[String]>) -> Vec<String> {
    let Value::Object(spec) = spec else {
        return vec!["must be an object: {\"select\": \"all\" or [trait ids], \"parts\": {trait id: [parts]}}".into()];
    };
    let mut extra: Vec<&String> = spec.keys().filter(|k| k.as_str() != "select" && k.as_str() != "parts").collect();
    extra.sort();
    let mut problems: Vec<String> = extra.iter().map(|n| format!("has no field \"{n}\"; it takes \"select\" and \"parts\"")).collect();
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
                }
                _ => problems.push(format!("\"parts\" for {} must be a list of at least one part", py::repr(trait_id))),
            }
        }
    }
    problems
}

pub fn activity_label(text: &str) -> String {
    text.split_whitespace().collect::<Vec<_>>().join(" ").to_lowercase()
}

/// A name for each part, unique within them: "kind", then "kind#2"...
pub fn part_keys(parts: &[Value]) -> Vec<String> {
    let mut seen: Vec<(String, usize)> = Vec::new();
    parts
        .iter()
        .map(|part| {
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
            if count == 1 { kind } else { format!("{kind}#{count}") }
        })
        .collect()
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
    sheets: SheetsClient,
    spreadsheet_id: String,
    sheet_id: i64,
}

impl Traits {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<Traits> {
        let sheet_id = match find_tab(sheets, spreadsheet_id, TRAITS.0)? {
            Some(id) => id,
            None => create_tab(sheets, spreadsheet_id, TRAITS.0, TRAITS.1, None, |new_id| {
                let header: Vec<String> = HEADER_ROW.iter().map(|s| s.to_string()).collect();
                sheets.write_rows_in_sheet(spreadsheet_id, new_id, HEADER_RANGE, &vec![header.clone()])?;
                let rows: Rows = seed_traits().iter().map(|t| t.to_row(&header, None)).collect();
                sheets.write_rows_in_sheet(spreadsheet_id, new_id, DATA_RANGE, &rows)
            })?,
        };
        Ok(Traits { sheets: sheets.clone(), spreadsheet_id: spreadsheet_id.into(), sheet_id })
    }

    pub fn whole_tab(&self) -> TabRange {
        TabRange::new(&self.spreadsheet_id, self.sheet_id, WHOLE_RANGE)
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheets.prefetch(ranges)
    }

    pub fn all(&self) -> anyhow::Result<Vec<Trait>> {
        let (header, rows) = self.read()?;
        Ok(rows.iter().filter(|r| r.iter().any(|c| !c.trim().is_empty())).map(|r| Trait::from_row(&header, r)).collect())
    }

    pub fn ids(&self) -> anyhow::Result<Vec<String>> {
        // `[t.id for t in all()]`: Nones included as "None" never match a
        // real id, so they're left out.
        Ok(self.all()?.into_iter().filter_map(|t| t.id_str().map(str::to_string)).collect())
    }

    pub fn by_id(&self) -> anyhow::Result<Vec<(String, Trait)>> {
        let mut out: Vec<(String, Trait)> = Vec::new();
        for t in self.all()? {
            if let Some(id) = t.id_str().filter(|i| !i.is_empty()).map(str::to_string) {
                out.retain(|(k, _)| *k != id);
                out.push((id, t));
            }
        }
        Ok(out)
    }

    pub fn get_traits(&self, statuses: Option<&[String]>) -> anyhow::Result<Value> {
        let statuses = check_statuses(statuses)?;
        let listed: Vec<Value> = self
            .all()?
            .into_iter()
            .filter(|t| statuses.iter().any(|s| t.status_str() == Some(s.as_str())))
            .map(|t| {
                let problems = trait_problems(&t);
                let mut out = t.to_json();
                out.insert("problems".into(), if problems.is_empty() { Value::Null } else { json!(problems) });
                Value::Object(out)
            })
            .collect();
        Ok(Value::Array(listed))
    }

    pub fn create_trait(&self, given: &Trait) -> anyhow::Result<Value> {
        let traits = self.all()?;
        let mut new = given.clone();
        if !new.status.as_ref().is_some_and(|s| crate::stores::groups::truthy(Some(s))) {
            new.status = Some(json!("active"));
        }
        let problems = trait_problems(&new);
        if !problems.is_empty() {
            bail!("{}", said(&new, &problems));
        }
        let taken: Vec<Option<&str>> = traits.iter().map(|t| t.id_str()).collect();
        let base = slug(new.name_str().unwrap_or_default());
        let mut id = base.clone();
        let mut n = 2;
        while taken.contains(&Some(id.as_str())) {
            let keep = MAX_ID_LENGTH.saturating_sub(n.to_string().len() + 1);
            id = format!("{}-{n}", base.chars().take(keep).collect::<String>());
            n += 1;
        }
        new.id = Some(json!(id));
        check_unique_name(&new, &traits)?;
        let mut all = traits;
        all.push(new.clone());
        self.write(&all)?;
        Ok(Value::Object(new.to_json()))
    }

    pub fn update_trait(&self, given: &Trait, clear_fields: &[String]) -> anyhow::Result<Value> {
        let Some(id) = given.id_str().filter(|i| !i.is_empty()).map(str::to_string) else {
            bail!("update_trait needs the trait's id");
        };
        let mut unknown: Vec<&String> = clear_fields.iter().filter(|c| !CLEARABLE_TRAIT_FIELDS.contains(&c.as_str())).collect();
        unknown.sort();
        unknown.dedup();
        if !unknown.is_empty() {
            bail!("Can't clear {}; clearable fields are ['definition']", py::repr_list(&unknown));
        }
        let mut both: Vec<&String> = clear_fields.iter().filter(|n| given.get(n).is_some()).collect();
        both.sort();
        if !both.is_empty() {
            bail!("Can't both set and clear {}", py::repr_list(&both));
        }
        let mut traits = self.all()?;
        let Some(index) = traits.iter().position(|t| t.id_str() == Some(id.as_str())) else {
            let known: Vec<String> =
                traits.iter().map(|t| format!("{} ({})", python_str(t.id.as_ref()), python_str(t.name.as_ref()))).collect();
            bail!("{} isn't a trait; the traits are {}", py::repr(&id), known.join(", "));
        };
        let mut updated = traits[index].clone();
        for field in HEADER_ROW {
            if field != "id" {
                if let Some(value) = given.get(field) {
                    *updated.slot(field) = Some(value.clone());
                }
            }
        }
        for name in clear_fields {
            *updated.slot(name) = None;
        }
        let problems = trait_problems(&updated);
        if !problems.is_empty() {
            bail!("{}", said(&updated, &problems));
        }
        check_unique_name(&updated, &traits)?;
        traits[index] = updated.clone();
        self.write(&traits)?;
        Ok(Value::Object(updated.to_json()))
    }

    fn read(&self) -> anyhow::Result<(Vec<String>, Rows)> {
        let mut found = self.sheets.read_ranges_in_sheet(&self.spreadsheet_id, self.sheet_id, &[HEADER_RANGE, DATA_RANGE])?;
        let rows = found.pop().unwrap_or_default();
        let header_rows = found.pop().unwrap_or_default();
        let header: Vec<String> = header_rows.first().map(|h| h.iter().map(|c| c.trim().to_string()).collect()).unwrap_or_default();
        let missing: Vec<&str> = ["id", "name", "status", "parts"].into_iter().filter(|c| !header.iter().any(|h| h == c)).collect();
        if !missing.is_empty() {
            bail!("The Traits tab's header row is missing columns: {}", py::repr_list(&missing));
        }
        Ok((header, rows))
    }

    fn write(&self, traits: &[Trait]) -> anyhow::Result<()> {
        let (header, previous) = self.read()?;
        let mut by_id: Vec<(Option<Value>, &Vec<String>)> = Vec::new();
        for row in &previous {
            if row.iter().any(|c| !c.trim().is_empty()) {
                let id = Trait::from_row(&header, row).id;
                by_id.retain(|(k, _)| *k != id);
                by_id.push((id, row));
            }
        }
        let mut rows: Rows = traits
            .iter()
            .map(|t| t.to_row(&header, by_id.iter().find(|(k, _)| *k == t.id).map(|(_, r)| *r)))
            .collect();
        for _ in traits.len()..previous.len() {
            rows.push(vec![String::new(); header.len()]);
        }
        self.sheets.write_rows_in_sheet(&self.spreadsheet_id, self.sheet_id, DATA_RANGE, &rows)
    }
}

fn check_unique_name(t: &Trait, traits: &[Trait]) -> anyhow::Result<()> {
    let name = t.name_str().unwrap_or_default().to_lowercase();
    if let Some(clash) = traits.iter().find(|o| o.id != t.id && o.name_str().unwrap_or_default().to_lowercase() == name) {
        bail!("There's already a trait named {} ({})", python_repr(clash.name.as_ref().unwrap_or(&Value::Null)), python_str(clash.id.as_ref()));
    }
    Ok(())
}

fn said(t: &Trait, problems: &[String]) -> String {
    let name = match t.name_str().filter(|n| !n.trim().is_empty()) {
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

#[cfg(test)]
#[path = "traits_tests.rs"]
mod tests;
