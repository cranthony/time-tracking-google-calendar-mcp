//! Scoring settings: how scores are combined -- a judgment part's
//! judgments into its score, and, as an aggregation, a trait's parts into
//! its score, a person's or habit's traits into theirs, a circle's people
//! into its own, and each layer of the habit graph. The server checks and
//! keeps them; the app does the scoring. The defaults, for whatever
//! doesn't set its own, are kept in the Scoring Defaults tab.

use crate::contract::{RowItem, Schema};
use crate::google::sheets::SheetsClient;
use crate::py;
use crate::sheet::rows::{self, RowSheet, RowType};
use crate::sheet::SCORING_DEFAULTS;
use crate::stores::traits::is_number;
use anyhow::bail;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

pub const STATISTICS: [&str; 2] = ["mean", "percentile"];
const AGGREGATION_FIELDS: [&str; 7] = ["statistic", "percentile", "weights", "limit", "limit_by", "cap_below", "missing"];
const AGGREGATION_LIMIT_BY: [&str; 2] = ["best", "worst"];
const JUDGMENT_FIELDS: [&str; 5] = ["weight_by", "statistic", "percentile", "limit", "limit_by"];
pub const WEIGHT_BY: [&str; 2] = ["event", "duration"];
const JUDGMENT_LIMIT_BY: [&str; 3] = ["best", "worst", "recent"];

/// The levels with a default, as the Scoring Defaults tab's ids.
pub const DEFAULT_LEVELS: [&str; 3] = ["traits", "circles", "habit_layers"];

fn quoted(names: &[&str]) -> String {
    names.iter().map(|n| format!("\"{n}\"")).collect::<Vec<_>>().join(", ")
}

fn either(names: &[&str]) -> String {
    match names {
        [] => String::new(),
        [one] => format!("\"{one}\""),
        [rest @ .., last] => format!("{} or \"{last}\"", quoted(rest)),
    }
}

fn number(value: &Value) -> f64 {
    value.as_f64().unwrap_or(f64::NAN)
}

fn is_score(value: &Value) -> bool {
    is_number(value) && (0.0..=100.0).contains(&number(value))
}

fn extra_fields(object: &Map<String, Value>, allowed: &[&str]) -> Vec<String> {
    let mut extra: Vec<&String> = object.keys().filter(|k| !allowed.contains(&k.as_str())).collect();
    extra.sort();
    extra.iter().map(|name| format!("has no field \"{name}\"; it takes {}", quoted(allowed))).collect()
}

fn statistic_problems(object: &Map<String, Value>) -> Vec<String> {
    let mut problems = Vec::new();
    let statistic = object.get("statistic");
    if let Some(s) = statistic {
        if !STATISTICS.iter().any(|known| s.as_str() == Some(*known)) {
            problems.push(format!("\"statistic\" must be {}", either(&STATISTICS)));
        }
    }
    let percentile = statistic.and_then(Value::as_str) == Some("percentile");
    match object.get("percentile") {
        Some(_) if !percentile => problems.push("\"percentile\" is only for \"statistic\": \"percentile\"".into()),
        Some(p) if !is_score(p) => problems.push("\"percentile\" must be a number from 0 to 100".into()),
        None if percentile => problems.push("\"statistic\": \"percentile\" needs \"percentile\", from 0 to 100".into()),
        _ => {}
    }
    problems
}

fn limit_problems(object: &Map<String, Value>, by: &[&str], whole: bool) -> Vec<String> {
    let mut problems = Vec::new();
    match (object.get("limit"), object.get("limit_by")) {
        (Some(limit), limit_by) => {
            let ok = is_number(limit) && number(limit) > 0.0 && (!whole || number(limit).fract() == 0.0);
            if !ok {
                problems.push(if whole { "\"limit\" must be a whole number, 1 or more".into() } else { "\"limit\" must be a number above 0".into() });
            }
            match limit_by {
                None => problems.push(format!("\"limit\" needs \"limit_by\": {}", either(by))),
                Some(b) if !by.iter().any(|known| b.as_str() == Some(*known)) => {
                    problems.push(format!("\"limit_by\" must be {}", either(by)))
                }
                _ => {}
            }
        }
        (None, Some(_)) => problems.push("\"limit_by\" is only for a \"limit\"".into()),
        (None, None) => {}
    }
    problems
}

/// What's wrong with an aggregation, as phrases following "its scoring:".
/// Its weights are checked against `part_ids` when given (a trait's own
/// parts); elsewhere, an id that names nothing is just never used.
pub fn aggregation_problems(value: &Value, part_ids: Option<&[String]>) -> Vec<String> {
    let Some(object) = value.as_object() else {
        return vec![format!("must be an object, with any of {}", quoted(&AGGREGATION_FIELDS))];
    };
    let mut problems = extra_fields(object, &AGGREGATION_FIELDS);
    problems.extend(statistic_problems(object));
    if let Some(weights) = object.get("weights") {
        match weights.as_object() {
            None => problems.push("\"weights\" must be an object of {id: weight}".into()),
            Some(weights) => {
                for (id, weight) in weights {
                    if !(is_number(weight) && number(weight) >= 0.0) {
                        problems.push(format!("\"weights\": {}'s weight must be a number, 0 or more", py::repr(id)));
                    }
                    if let Some(known) = part_ids {
                        if !known.iter().any(|k| k == id) {
                            problems.push(format!(
                                "\"weights\" names {}, which isn't one of its parts (by id: {})",
                                py::repr(id),
                                known.join(", ")
                            ));
                        }
                    }
                }
            }
        }
    }
    problems.extend(limit_problems(object, &AGGREGATION_LIMIT_BY, true));
    if let Some(cap) = object.get("cap_below") {
        let ok = cap.as_object().is_some_and(|c| {
            c.len() == 2 && c.get("threshold").is_some_and(is_score) && c.get("cap").is_some_and(is_score)
        });
        if !ok {
            problems.push("\"cap_below\" must be {\"threshold\": score, \"cap\": score}, each from 0 to 100".into());
        }
    }
    if let Some(missing) = object.get("missing") {
        if !(missing.as_str() == Some("skip") || is_score(missing)) {
            problems.push("\"missing\" must be \"skip\" or a score from 0 to 100".into());
        }
    }
    problems
}

/// What's wrong with a judgment part's scoring, as phrases following
/// "\"scoring\":".
pub fn judgment_scoring_problems(value: &Value) -> Vec<String> {
    let Some(object) = value.as_object() else {
        return vec![format!("must be an object, with any of {}", quoted(&JUDGMENT_FIELDS))];
    };
    let mut problems = extra_fields(object, &JUDGMENT_FIELDS);
    let weight_by = object.get("weight_by");
    if let Some(w) = weight_by {
        if !WEIGHT_BY.iter().any(|known| w.as_str() == Some(*known)) {
            problems.push(format!("\"weight_by\" must be {}", either(&WEIGHT_BY)));
        }
    }
    problems.extend(statistic_problems(object));
    let by_duration = weight_by.and_then(Value::as_str) == Some("duration");
    problems.extend(limit_problems(object, &JUDGMENT_LIMIT_BY, !by_duration));
    problems
}

/// A level's default aggregation, a row of the Scoring Defaults tab.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema, RowItem)]
pub struct ScoringDefault {
    pub id: Option<String>,
    pub scoring: Option<Map<String, Value>>,
}

pub const DEFAULT_ROW: RowType = <ScoringDefault as rows::RowItem>::ROW_TYPE;

/// How scores combine wherever nothing says otherwise: each an
/// aggregation (see get_scoring_defaults), or null for the plain mean --
/// for habit_layers, the lowest score.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema)]
pub struct ScoringDefaults {
    /// How a person's or habit's traits combine into their score, by
    /// trait id, unless their traits' own scoring says.
    pub traits: Option<Map<String, Value>>,
    /// How a circle's people combine into its score, by person id,
    /// unless the circle's own scoring says.
    pub circles: Option<Map<String, Value>>,
    /// How the habits and people in each layer of the habit graph
    /// combine into the layer's score: habits by "habit:<id>", people
    /// by id.
    pub habit_layers: Option<Map<String, Value>>,
}

impl ScoringDefaults {
    fn level(&self, name: &str) -> &Option<Map<String, Value>> {
        match name {
            "traits" => &self.traits,
            "circles" => &self.circles,
            _ => &self.habit_layers,
        }
    }

    fn level_mut(&mut self, name: &str) -> &mut Option<Map<String, Value>> {
        match name {
            "traits" => &mut self.traits,
            "circles" => &mut self.circles,
            _ => &mut self.habit_layers,
        }
    }
}

#[derive(Clone)]
pub struct ScoringDefaultsTab {
    sheet: RowSheet,
}

impl ScoringDefaultsTab {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<ScoringDefaultsTab> {
        Ok(ScoringDefaultsTab {
            sheet: RowSheet::ensure(sheets, spreadsheet_id, SCORING_DEFAULTS.0, SCORING_DEFAULTS.1, DEFAULT_ROW, &["id", "scoring"])?,
        })
    }

    /// The defaults; a level whose row is missing (or isn't one) has none.
    pub fn get(&self) -> anyhow::Result<ScoringDefaults> {
        let mut out = ScoringDefaults::default();
        for row in self.sheet.read_items::<ScoringDefault>()? {
            if let Some(level) = row.id.as_deref().filter(|id| DEFAULT_LEVELS.contains(id)) {
                *out.level_mut(level) = row.scoring;
            }
        }
        Ok(out)
    }

    /// Sets the levels `given` has, and clears those in `clear_fields`,
    /// keeping the rest; refused, saying why, if any isn't well formed.
    pub fn set(&self, given: &ScoringDefaults, clear_fields: &[String]) -> anyhow::Result<ScoringDefaults> {
        let mut defaults = self.get()?;
        let mut problems = Vec::new();
        for level in DEFAULT_LEVELS {
            let cleared = clear_fields.iter().any(|c| c == level);
            if cleared && given.level(level).is_some() {
                problems.push(format!("{level} is both given and cleared"));
            } else if cleared {
                *defaults.level_mut(level) = None;
            } else if let Some(scoring) = given.level(level) {
                for p in aggregation_problems(&Value::Object(scoring.clone()), None) {
                    problems.push(format!("{level}: {p}"));
                }
                *defaults.level_mut(level) = Some(scoring.clone());
            }
        }
        if !problems.is_empty() {
            bail!("The scoring defaults: {}", problems.join("; "));
        }
        let rows: Vec<ScoringDefault> = DEFAULT_LEVELS
            .iter()
            .filter_map(|level| defaults.level(level).clone().map(|s| ScoringDefault { id: Some(level.to_string()), scoring: Some(s) }))
            .collect();
        self.sheet.write_items(&rows)?;
        Ok(defaults)
    }
}

#[cfg(test)]
#[path = "scoring_tests.rs"]
mod tests;
