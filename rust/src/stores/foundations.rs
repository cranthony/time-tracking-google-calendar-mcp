//! Foundations: what a habit, or a person on the habit graph, is built
//! on -- other habits (by their subject id, "habit:<id>") and people (by
//! their id). Habits and people together form the graph, so each store
//! checks its own rows against the other's.

use crate::py;
use crate::sheet::rows::{self, Row};
use serde_json::Value;
use std::sync::Arc;

/// One node of the graph: a habit or a person, by subject id.
#[derive(Clone, Debug, PartialEq)]
pub struct Node {
    pub subject: String,
    pub deleted: bool,
    pub foundations: Vec<String>,
}

/// The other store's nodes.
pub type NodesFn = Arc<dyn Fn() -> anyhow::Result<Vec<Node>> + Send + Sync>;

/// `row`'s foundations, as far as they're a list of ids.
pub fn foundations_of(row: &Row) -> Vec<String> {
    match row.get("foundations") {
        Some(Value::Array(items)) => items.iter().filter_map(Value::as_str).map(str::to_string).collect(),
        _ => Vec::new(),
    }
}

pub fn node(subject: String, row: &Row) -> Node {
    Node { subject, deleted: rows::text(row, "status") == Some("deleted"), foundations: foundations_of(row) }
}

/// What's wrong with `subject`'s foundations, `value`, in `nodes` (every
/// habit and person, `subject` among them): not a list of ids, an id
/// given twice, itself, an id that's no habit or person (or a deleted
/// one), or a cycle back to it.
pub fn foundation_problems(subject: &str, value: &Value, nodes: &[Node]) -> Vec<String> {
    let Some(items) = value.as_array().filter(|items| items.iter().all(|i| i.as_str().is_some_and(|s| !s.trim().is_empty()))) else {
        return vec!["foundations must be a list of ids: a habit's as \"habit:<id>\", a person's as their id".into()];
    };
    let ids: Vec<&str> = items.iter().filter_map(Value::as_str).collect();
    let mut problems = Vec::new();
    let mut repeated: Vec<&str> = ids.iter().copied().filter(|i| ids.iter().filter(|x| *x == i).count() > 1).collect();
    repeated.sort();
    repeated.dedup();
    if !repeated.is_empty() {
        problems.push(format!("foundations name {} more than once", py::repr_list(&repeated)));
    }
    if ids.contains(&subject) {
        problems.push("foundations can't include itself".into());
    }
    let unknown: Vec<&str> = ids.iter().copied().filter(|i| *i != subject && !nodes.iter().any(|n| n.subject == *i && !n.deleted)).collect();
    if !unknown.is_empty() {
        problems.push(format!(
            "foundations {} aren't habits or people (get_habits and get_people list them; a habit is named \"habit:<id>\")",
            py::repr_list(&unknown)
        ));
    }
    if problems.is_empty() {
        if let Some(path) = cycle(subject, &ids, nodes) {
            problems.push(format!("foundations would make a cycle: {}", path.join(" → ")));
        }
    }
    problems
}

/// A path from `subject` back to itself through `foundations` (its new
/// ones) and the other nodes' foundations, if there's one.
fn cycle(subject: &str, foundations: &[&str], nodes: &[Node]) -> Option<Vec<String>> {
    let next = |at: &str| -> Vec<String> {
        if at == subject {
            return foundations.iter().map(|f| f.to_string()).collect();
        }
        nodes.iter().find(|n| n.subject == at).map(|n| n.foundations.clone()).unwrap_or_default()
    };
    let mut seen: Vec<String> = Vec::new();
    let mut path = vec![subject.to_string()];
    fn walk(at: &str, subject: &str, next: &dyn Fn(&str) -> Vec<String>, seen: &mut Vec<String>, path: &mut Vec<String>) -> bool {
        for f in next(at) {
            if f == subject {
                path.push(f);
                return true;
            }
            if seen.contains(&f) {
                continue;
            }
            seen.push(f.clone());
            path.push(f.clone());
            if walk(&f, subject, next, seen, path) {
                return true;
            }
            path.pop();
        }
        false
    }
    walk(subject, subject, &next, &mut seen, &mut path).then_some(path)
}

/// `rows` without `subject` in any row's foundations, and whether any
/// changed.
pub fn without(rows: &mut [Row], subject: &str) -> bool {
    let mut changed = false;
    for row in rows.iter_mut() {
        if let Some(Value::Array(items)) = row.get("foundations") {
            if items.iter().any(|i| i.as_str() == Some(subject)) {
                let kept: Vec<Value> = items.iter().filter(|i| i.as_str() != Some(subject)).cloned().collect();
                row.insert("foundations".into(), Value::Array(kept));
                changed = true;
            }
        }
    }
    changed
}

#[cfg(test)]
#[path = "foundations_tests.rs"]
mod tests;
