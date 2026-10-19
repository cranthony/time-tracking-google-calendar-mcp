//! Keeping people's trait facts in compaction: each fact's summary of a
//! person, rewritten whole (compact_notes' `fact_summaries`), beside the
//! log of what changed on the event that showed it (its facts' `logs`).
//! Both are checked against each other when Claude proposes them, and
//! written when the proposal is applied.

use super::plan::EventDecision;
use super::proposals::{AdditionSettled, Dict};
use crate::facts::{Facts, SELF_ID};
use crate::py;
use crate::stores::cancellations::traits_for;
use crate::stores::people::Person;
use crate::stores::person_facts::{PersonFacts, SummariesByPerson, SummaryChange, MAX_SUMMARY_CHARS};
use crate::stores::traits::{Trait, TraitStatus, Traits};
use serde_json::{json, Map, Value};

/// What compaction keeps people's trait facts with.
#[derive(Clone)]
pub struct FactKeeping {
    pub traits: Traits,
    pub person_facts: PersonFacts,
}

/// A summary Claude proposes: `summary` for `person_id`'s `fact`
/// ("<trait id>/<fact id>").
#[derive(Clone, Debug, PartialEq)]
pub struct ProposedSummary {
    pub person_id: String,
    pub fact: String,
    pub summary: String,
}

impl ProposedSummary {
    pub fn to_json(&self) -> Value {
        json!({"person_id": self.person_id, "fact": self.fact, "summary": self.summary})
    }

    pub fn from_json(value: &Value) -> Option<ProposedSummary> {
        let text = |key: &str| value.get(key).and_then(Value::as_str).map(str::to_string);
        Some(ProposedSummary { person_id: text("person_id")?, fact: text("fact")?, summary: text("summary").unwrap_or_default() })
    }

    /// The summaries a revision keeps (its `claude_fact_summaries`).
    pub fn all_from(value: Option<&Value>) -> Vec<ProposedSummary> {
        value.and_then(Value::as_array).into_iter().flatten().filter_map(ProposedSummary::from_json).collect()
    }

    pub fn all_to_json(summaries: &[ProposedSummary]) -> Option<Value> {
        if summaries.is_empty() {
            None
        } else {
            Some(Value::Array(summaries.iter().map(ProposedSummary::to_json).collect()))
        }
    }

    /// As the store sets it, with its person's id as `rename` gives it
    /// (None when it gives none: dropped).
    pub fn change(&self, rename: &dyn Fn(&str) -> Option<String>) -> Option<SummaryChange> {
        let (trait_id, fact_id) = self.fact.split_once('/')?;
        Some(SummaryChange { person_id: rename(&self.person_id)?, trait_id: trait_id.into(), fact_id: fact_id.into(), summary: self.summary.clone() })
    }
}

/// A trait fact as compaction describes it to Claude, once.
pub fn described(t: &Trait, fact: &Map<String, Value>) -> Value {
    let text = |key: &str| fact.get(key).cloned().unwrap_or(Value::Null);
    json!({
        "fact": format!("{}/{}", t.id.as_deref().unwrap_or_default(), fact.get("id").and_then(Value::as_str).unwrap_or_default()),
        "trait_name": t.name.clone().filter(|n| !n.is_empty()).or(t.id.clone()),
        "name": text("name"),
        "summary_prompt": text("summary_prompt"),
        "log_prompt": text("log_prompt"),
    })
}

/// The active traits' facts, each described once.
pub fn trait_facts(traits: &[Trait]) -> Vec<Value> {
    traits
        .iter()
        .filter(|t| t.status == Some(TraitStatus::Active) && t.id.as_deref().is_some_and(|i| !i.is_empty()))
        .flat_map(|t| t.facts.iter().flatten().map(move |f| described(t, f)))
        .collect()
}

/// The facts ("<trait id>/<fact id>") of the active traits that apply to
/// someone with `spec` as their traits.
pub fn facts_for(spec: Option<&Value>, traits: &[Trait]) -> Vec<String> {
    traits_for(spec, traits)
        .into_iter()
        .flat_map(|(t, _)| {
            let id = t.id.clone().unwrap_or_default();
            t.fact_ids().into_iter().map(move |f| format!("{id}/{f}"))
        })
        .collect()
}

/// Every active trait's facts: what a person who's still to be added (by
/// ref) may have.
fn every_fact(traits: &[Trait]) -> Vec<String> {
    facts_for(None, traits)
}

/// Each of `person`'s facts, with their summary of it if they have one.
pub fn person_facts(person: &Person, traits: &[Trait], summaries: &SummariesByPerson) -> Vec<Value> {
    let id = person.id.clone().unwrap_or_default();
    let theirs = summaries.iter().find(|(p, _)| *p == id).map(|(_, s)| s.as_slice()).unwrap_or_default();
    facts_for(person.traits.clone().map(Value::Object).as_ref(), traits)
        .into_iter()
        .map(|key| {
            let summary = theirs.iter().find(|(t, f, _)| format!("{t}/{f}") == key).map(|(_, _, s)| s.clone());
            json!({"fact": key, "summary": summary})
        })
        .collect()
}

/// What's wrong with Claude's logs (in its decisions' facts) and
/// `summaries` together: each log and summary a fact of its person's --
/// anyone in `refs` (to be added) may have any active trait's -- and each
/// summary changed beside a log of it, each log beside its summary.
pub fn problems(decisions: &[EventDecision], summaries: &[ProposedSummary], people: &[Person], refs: &[String], traits: &[Trait]) -> Vec<String> {
    let mut problems = Vec::new();
    let facts_of = |person: &str| -> Option<Vec<String>> {
        if refs.iter().any(|r| r == person) {
            return Some(every_fact(traits));
        }
        people.iter().find(|p| p.id.as_deref() == Some(person)).map(|p| facts_for(p.traits.clone().map(Value::Object).as_ref(), traits))
    };
    let not_theirs = |person: &str, fact: &str, theirs: &[String]| {
        format!(
            "{} isn't one of {}'s facts ({})",
            py::repr(fact),
            py::repr(person),
            if theirs.is_empty() { "they have none".to_string() } else { theirs.join(", ") }
        )
    };
    let mut logged: Vec<(String, String)> = Vec::new();
    for (number, decision) in decisions.iter().enumerate() {
        let Some(facts) = &decision.facts else { continue };
        let label = format!(
            "decision {} ({}{})",
            number + 1,
            decision.action,
            decision.event_id.as_ref().filter(|e| !e.is_empty()).map(|e| format!(" {e}")).unwrap_or_default()
        );
        for (person, fact, _) in facts.log_entries() {
            if let Some(theirs) = facts_of(&person) {
                if !theirs.contains(&fact) {
                    problems.push(format!("{label}: its facts' logs: {}", not_theirs(&person, &fact, &theirs)));
                }
            }
            if !logged.contains(&(person.clone(), fact.clone())) {
                logged.push((person, fact));
            }
        }
    }
    let mut given: Vec<(String, String)> = Vec::new();
    for summary in summaries {
        let who = format!("fact_summaries: {}'s {}", py::repr(&summary.person_id), summary.fact);
        let pair = (summary.person_id.clone(), summary.fact.clone());
        if given.contains(&pair) {
            problems.push(format!("{who} is given more than once"));
            continue;
        }
        given.push(pair.clone());
        match facts_of(&summary.person_id) {
            None if summary.person_id != SELF_ID => problems.push(format!(
                "{who}: {} isn't a person (get_people lists them; add someone new with new_people)",
                py::repr(&summary.person_id)
            )),
            Some(theirs) if !theirs.contains(&summary.fact) => problems.push(format!("{who}: {}", not_theirs(&summary.person_id, &summary.fact, &theirs))),
            _ => {}
        }
        if summary.summary.chars().count() > MAX_SUMMARY_CHARS {
            problems.push(format!("{who} must be at most {MAX_SUMMARY_CHARS} characters"));
        }
        if !logged.contains(&pair) {
            problems.push(format!(
                "{who} has no log: say what changed in the logs of the event (or events) whose notes showed it -- its facts' logs, {{{}: {{{}: \"...\"}}}}",
                py::repr(&summary.person_id),
                py::repr(&summary.fact)
            ));
        }
    }
    for (person, fact) in logged {
        if !given.contains(&(person.clone(), fact.clone())) {
            problems.push(format!(
                "the log of {}'s {fact} has no summary: give the summary it rewrote in fact_summaries (the whole summary, as it now stands)",
                py::repr(&person)
            ));
        }
    }
    problems
}

/// The id a summary's person has once the proposal's additions are made:
/// one the user settled as `settled` says (dropped, none), one made as
/// `refs` says, and anyone else's own.
pub fn renamer<'a>(settled: &'a Dict<AdditionSettled>, refs: &'a [(String, String)]) -> impl Fn(&str) -> Option<String> + 'a {
    move |id: &str| match settled.get(id) {
        Some(choice) => choice.id.clone(),
        None => Some(refs.iter().rev().find(|(k, _)| k == id).map(|(_, v)| v.clone()).unwrap_or_else(|| id.to_string())),
    }
}

/// `facts`' logs with each person's id as `rename` gives it.
pub fn renamed_logs(facts: &Facts, rename: &dyn Fn(&str) -> Option<String>) -> Vec<(String, String, String)> {
    let renamed = Facts { logs: facts.renamed_logs(rename), ..Default::default() };
    renamed.log_entries()
}
