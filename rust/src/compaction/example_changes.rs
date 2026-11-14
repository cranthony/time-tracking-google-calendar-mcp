//! Trait examples in compaction: stories in the notes of the user or
//! someone else embodying a trait well, which Claude proposes with
//! compact_notes' `trait_examples` (each as create_trait_example takes
//! it, its `event_id` the plan's event it happened at). A proposal keeps
//! them through its revisions, each by its id (made by the server when
//! Claude gives none, and sent back to Claude to send again); the user
//! drops or changes one in review; the rest are created when the proposal
//! is applied.

use super::proposals::Dict;
use crate::py;
use crate::stores::people::Subject;
use crate::stores::trait_examples::{example_problems, Known, TraitExample, CLEARABLE_FIELDS};
use crate::stores::traits::Trait;
use crate::sheet::rows;
use serde_json::{json, Map, Value};

pub const USES: [&str; 2] = ["drop", "change"];

/// The fields of an example the user can change.
const FIELDS: [&str; 7] = ["trait_id", "when", "person_id", "who", "what", "why", "event_id"];

/// The examples a revision keeps (its `claude_trait_examples`).
pub fn all_from(value: Option<&Value>) -> Vec<TraitExample> {
    value.and_then(Value::as_array).into_iter().flatten().filter_map(|v| serde_json::from_value(v.clone()).ok()).collect()
}

pub fn all_to_json(examples: &[TraitExample]) -> Option<Value> {
    if examples.is_empty() {
        None
    } else {
        Some(Value::Array(examples.iter().map(to_json).collect()))
    }
}

/// An example as a proposal keeps it: its fields given, without a version.
pub fn to_json(example: &TraitExample) -> Value {
    let mut value = serde_json::to_value(TraitExample { version: None, ..example.clone() }).expect("an example is JSON");
    if let Value::Object(fields) = &mut value {
        fields.retain(|_, v| !v.is_null());
    }
    value
}

/// `examples` with an id each: one without gets a new one, none another
/// has or `taken` holds.
pub fn with_ids(examples: Vec<TraitExample>, taken: &[String]) -> Vec<TraitExample> {
    let mut ids: Vec<String> = taken.to_vec();
    ids.extend(examples.iter().filter_map(|e| e.id.clone()));
    examples
        .into_iter()
        .map(|mut e| {
            if e.id.as_deref().is_none_or(|i| i.trim().is_empty()) {
                let id = rows::new_id(ids.iter().map(|i| Some(i.as_str())));
                ids.push(id.clone());
                e.id = Some(id);
            }
            e
        })
        .collect()
}

/// The user's say on an example (amend_proposal's `trait_examples`), as
/// the ledger keeps it: `drop` it, or `change` it -- their fields over
/// Claude's (or an example of their own, by an id the proposal doesn't
/// have), and those they clear.
#[derive(Debug, Clone, PartialEq)]
pub struct ExampleSettled {
    pub id: String,
    pub use_: String,
    pub fields: TraitExample,
    pub clear_fields: Vec<String>,
}

impl ExampleSettled {
    /// From a `trait_example` ledger entry (the example's id from its
    /// event_id column).
    pub fn from_edit(id: &str, edit: &Map<String, Value>) -> ExampleSettled {
        let fields: Map<String, Value> = edit.iter().filter(|(k, _)| FIELDS.contains(&k.as_str())).map(|(k, v)| (k.clone(), v.clone())).collect();
        ExampleSettled {
            id: id.to_string(),
            use_: edit.get("use").and_then(Value::as_str).unwrap_or_default().into(),
            fields: serde_json::from_value(Value::Object(fields)).unwrap_or_default(),
            clear_fields: edit
                .get("clear_fields")
                .and_then(Value::as_array)
                .map(|a| a.iter().filter_map(Value::as_str).map(str::to_string).collect())
                .unwrap_or_default(),
        }
    }

    /// The ledger entry recording it.
    pub fn edit(&self) -> Map<String, Value> {
        let mut out = Map::new();
        out.insert("action".into(), json!("trait_example"));
        out.insert("use".into(), json!(self.use_));
        if let Value::Object(fields) = to_json(&TraitExample { id: None, ..self.fields.clone() }) {
            out.extend(fields);
        }
        if !self.clear_fields.is_empty() {
            out.insert("clear_fields".into(), json!(self.clear_fields));
        }
        out
    }

    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("id".into(), json!(self.id));
        out.insert("use".into(), json!(self.use_));
        if let Value::Object(fields) = to_json(&TraitExample { id: None, ..self.fields.clone() }) {
            out.extend(fields);
        }
        out.insert("clear_fields".into(), json!(self.clear_fields));
        Value::Object(out)
    }

    /// The example it keeps in the proposal (`change`): the user's fields
    /// over `under`'s, Claude's, if there is one, less those they cleared.
    pub fn changed(&self, under: Option<&TraitExample>) -> TraitExample {
        let base = under.cloned().unwrap_or_default();
        let theirs = &self.fields;
        let pick = |mine: &Option<String>, was: &Option<String>, name: &str| {
            if self.clear_fields.iter().any(|c| c == name) {
                None
            } else {
                mine.clone().or_else(|| was.clone())
            }
        };
        TraitExample {
            id: Some(self.id.clone()),
            trait_id: pick(&theirs.trait_id, &base.trait_id, "trait_id"),
            when: pick(&theirs.when, &base.when, "when"),
            person_id: pick(&theirs.person_id, &base.person_id, "person_id"),
            who: pick(&theirs.who, &base.who, "who"),
            what: pick(&theirs.what, &base.what, "what"),
            why: pick(&theirs.why, &base.why, "why"),
            event_id: pick(&theirs.event_id, &base.event_id, "event_id"),
            version: None,
        }
    }
}

/// Claude's examples, less those the user settled.
pub fn unsettled(claude: &[TraitExample], settled: &Dict<ExampleSettled>) -> Vec<TraitExample> {
    claude.iter().filter(|e| !e.id.as_deref().is_some_and(|i| settled.contains(i))).cloned().collect()
}

/// The examples a revision makes, each with who decided it: Claude's,
/// less those the user dropped, with the user's fields over Claude's
/// where they changed one, then the user's own.
pub fn effective(claude: &[TraitExample], settled: &Dict<ExampleSettled>) -> Vec<(TraitExample, &'static str)> {
    let mut out: Vec<(TraitExample, &'static str)> = Vec::new();
    for example in claude {
        match example.id.as_deref().and_then(|i| settled.get(i)) {
            None => out.push((example.clone(), "claude")),
            Some(theirs) if theirs.use_ == "change" => out.push((theirs.changed(Some(example)), "user")),
            Some(_) => {}
        }
    }
    for theirs in settled.values() {
        if theirs.use_ == "change" && !claude.iter().any(|e| e.id.as_deref() == Some(theirs.id.as_str())) {
            out.push((theirs.changed(None), "user"));
        }
    }
    out
}

/// Everything wrong with `examples`, each said with its id: an id given
/// twice, anything create_trait_example would refuse (`known` names the
/// traits, and everyone -- people added by ref among them), and an
/// event_id that isn't one of `events`, the proposal's (ids and keys).
pub fn problems(examples: &[TraitExample], known: &Known, events: &[String]) -> Vec<String> {
    let mut problems = Vec::new();
    let mut seen: Vec<&str> = Vec::new();
    for example in examples {
        let id = example.id.as_deref().unwrap_or_default();
        let label = format!("trait_examples: example {}", py::repr(id));
        if seen.contains(&id) {
            problems.push(format!("{label} is given more than once"));
            continue;
        }
        seen.push(id);
        if !id.is_empty() && !rows::chosen_id_shape(id) {
            problems.push(format!("{label}: its id must be 2 to 40 lowercase letters, digits and dashes (or left out, and one is made)"));
        }
        for p in example_problems(example, known) {
            problems.push(format!("{label}: {p}"));
        }
        if let Some(event) = example.event_id.as_deref().map(str::trim).filter(|e| !e.is_empty()) {
            if !events.iter().any(|e| e == event) {
                problems.push(format!("{label}: its event_id {} isn't one of the proposal's events", py::repr(event)));
            }
        }
    }
    problems
}

/// What `settled`'s choices are wrong in, given `claude`'s examples: a
/// use that isn't drop or change, a drop of one the proposal doesn't
/// make, a field that can't be cleared, an id given twice.
pub fn choice_problems(choices: &[ExampleSettled], claude: &[TraitExample]) -> Vec<String> {
    let mut problems = Vec::new();
    let mut seen: Vec<&str> = Vec::new();
    for choice in choices {
        let label = format!("trait_examples: example {}", py::repr(&choice.id));
        if choice.id.trim().is_empty() {
            problems.push("trait_examples: an example's choice needs its id".into());
            continue;
        }
        if seen.contains(&choice.id.as_str()) {
            problems.push(format!("{label} is given more than once"));
        }
        seen.push(&choice.id);
        if !USES.contains(&choice.use_.as_str()) {
            problems.push(format!("{label}: use is one of {}, not {}", USES.join(", "), py::repr(&choice.use_)));
        }
        let proposed = claude.iter().any(|e| e.id.as_deref() == Some(choice.id.as_str()));
        if choice.use_ == "drop" && !proposed {
            problems.push(format!("{label} isn't one the proposal makes, so there's nothing to drop"));
        }
        for field in &choice.clear_fields {
            if !CLEARABLE_FIELDS.contains(&field.as_str()) {
                problems.push(format!("{label}: {} can't be cleared; only {} can", py::repr(field), CLEARABLE_FIELDS.join(", ")));
            }
        }
    }
    problems
}

/// The examples as a proposal shows them: each with its trait's name,
/// the person's name, and who decided it.
pub fn shown(examples: &[(TraitExample, &str)], traits: &[Trait], subjects: &[Subject], added: &[(String, String)]) -> Vec<Value> {
    examples
        .iter()
        .map(|(example, by)| {
            let mut out = to_json(example);
            let trait_name = traits.iter().find(|t| t.id == example.trait_id).and_then(|t| t.name.clone());
            let person_name = example.person_id.as_deref().and_then(|p| {
                subjects
                    .iter()
                    .find(|s| s.id == p)
                    .and_then(|s| s.name.clone())
                    .or_else(|| added.iter().find(|(r, _)| r == p).map(|(_, n)| n.clone()))
            });
            out["trait_name"] = json!(trait_name);
            out["person_name"] = json!(person_name);
            out["decided_by"] = json!(by);
            out
        })
        .collect()
}

#[cfg(test)]
#[path = "example_changes_tests.rs"]
mod tests;
