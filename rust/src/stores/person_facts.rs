//! Trait facts: what each trait keeps about each person it applies to,
//! for its judgments -- each fact's summary of the person, rewritten whole
//! by compaction (or by the user) in the Person Facts tab, one row per
//! person and fact; and the log of each change compaction made, kept on
//! its event's facts and indexed in the Fact Logs tab, one row per event,
//! person and fact.

use crate::contract::{DateTimeText, RowItem, Schema};
use crate::event::{parse_iso, Moment};
use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::sheet::rows::{self, RowSheet, RowType};
use crate::sheet::{find_tab, FACT_LOGS, PEOPLE, PERSON_FACTS};
use crate::stores::groups::set;
use crate::stores::traits::{Trait, Traits, WHAT_MATTERS};
use anyhow::bail;
use serde::{Deserialize, Serialize};

/// Each person's summaries: (person id, [(trait id, fact id, summary)]).
pub type SummariesByPerson = Vec<(String, Vec<(String, String, String)>)>;

/// The most a summary can be: what a spreadsheet cell holds.
pub const MAX_SUMMARY_CHARS: usize = 50_000;

/// One row of the Person Facts tab: a trait fact's summary of a person.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, RowItem)]
pub struct PersonFact {
    pub id: Option<String>,
    pub person_id: Option<String>,
    pub trait_id: Option<String>,
    pub fact_id: Option<String>,
    pub summary: Option<String>,
    pub updated_at: Option<String>,
    pub source: Option<String>,
}

/// One row of the Fact Logs tab: what an event changed in a person's
/// trait fact.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, RowItem)]
pub struct FactLog {
    pub id: Option<String>,
    pub event_id: Option<String>,
    pub person_id: Option<String>,
    pub trait_id: Option<String>,
    pub fact_id: Option<String>,
    pub log: Option<String>,
    pub event_start: Option<String>,
    pub event_summary: Option<String>,
    pub logged_at: Option<String>,
    pub source: Option<String>,
}

/// A person's what_matters, as the People tab kept it before traits had
/// their own facts: read once, to move it into them.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, RowItem)]
#[row(name = "person")]
struct KeptWhatMatters {
    id: Option<String>,
    what_matters: Option<String>,
}

pub const PERSON_FACT_ROW: RowType = <PersonFact as rows::RowItem>::ROW_TYPE;
pub const FACT_LOG_ROW: RowType = <FactLog as rows::RowItem>::ROW_TYPE;
const KEPT_WHAT_MATTERS_ROW: RowType = <KeptWhatMatters as rows::RowItem>::ROW_TYPE;

/// A log as the fact tools return it: what an event changed in one of a
/// person's trait facts, and when.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct LoggedChange {
    pub event_id: Option<String>,
    pub event_summary: Option<String>,
    pub event_start: Option<DateTimeText>,
    pub trait_id: Option<String>,
    pub fact_id: Option<String>,
    pub log: Option<String>,
    pub logged_at: Option<DateTimeText>,
}

/// A summary to set: `summary` for `person_id`'s fact `fact_id` of the
/// trait `trait_id`.
#[derive(Clone, Debug, PartialEq)]
pub struct SummaryChange {
    pub person_id: String,
    pub trait_id: String,
    pub fact_id: String,
    pub summary: String,
}

fn summary_id(person: &str, trait_id: &str, fact: &str) -> String {
    format!("{person}/{trait_id}/{fact}")
}

fn time(text: Option<&str>) -> Option<Moment> {
    text.filter(|t| !t.is_empty()).and_then(|t| parse_iso(t, None).ok())
}

fn shown_time(text: Option<&str>) -> Option<DateTimeText> {
    time(text).map(|m| DateTimeText(py::pydantic_datetime(&m)))
}

#[derive(Clone)]
pub struct PersonFacts {
    sheet: RowSheet,
    log_sheet: RowSheet,
}

impl PersonFacts {
    /// The two tabs, made the first time -- the Person Facts tab with each
    /// person's what_matters, as the People tab kept it, as their summary
    /// for each trait's "what_matters" fact.
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str, traits: &Traits) -> anyhow::Result<PersonFacts> {
        let seed = match find_tab(sheets, spreadsheet_id, PERSON_FACTS.0, PERSON_FACTS.1)? {
            Some(_) => Vec::new(),
            None => kept_what_matters(sheets, spreadsheet_id, &traits.all()?)?.iter().map(rows::RowItem::to_row).collect(),
        };
        let sheet = RowSheet::ensure_seeded(
            sheets,
            spreadsheet_id,
            PERSON_FACTS.0,
            PERSON_FACTS.1,
            PERSON_FACT_ROW,
            &["id", "person_id", "trait_id", "fact_id"],
            &seed,
        )?;
        let log_sheet = RowSheet::ensure(sheets, spreadsheet_id, FACT_LOGS.0, FACT_LOGS.1, FACT_LOG_ROW, &["id", "event_id", "person_id"])?;
        Ok(PersonFacts { sheet, log_sheet })
    }

    pub fn whole_tabs(&self) -> Vec<TabRange> {
        vec![self.sheet.whole_tab(), self.log_sheet.whole_tab()]
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheet.prefetch(ranges)
    }

    /// Every summary, in sheet order.
    pub fn all(&self) -> anyhow::Result<Vec<PersonFact>> {
        self.sheet.read_items()
    }

    /// Each person's summaries that say anything: (person id, [(trait id,
    /// fact id, summary)]).
    pub fn by_person(&self) -> anyhow::Result<SummariesByPerson> {
        let mut found: SummariesByPerson = Vec::new();
        for row in self.all()? {
            let (Some(person), Some(trait_id), Some(fact), Some(summary)) = (
                set(row.person_id.as_deref()),
                set(row.trait_id.as_deref()),
                set(row.fact_id.as_deref()),
                row.summary.as_deref().filter(|s| !s.trim().is_empty()),
            ) else {
                continue;
            };
            let item = (trait_id.to_string(), fact.to_string(), summary.to_string());
            match found.iter_mut().find(|(k, _)| k == person) {
                Some(slot) => {
                    slot.1.retain(|(t, f, _)| !(*t == item.0 && *f == item.1));
                    slot.1.push(item);
                }
                None => found.push((person.to_string(), vec![item])),
            }
        }
        Ok(found)
    }

    /// `person`'s summary of the trait `trait_id`'s fact `fact`, if they
    /// have one that says anything.
    pub fn summary(&self, person: &str, trait_id: &str, fact: &str) -> anyhow::Result<Option<String>> {
        Ok(self
            .by_person()?
            .into_iter()
            .find(|(p, _)| p == person)
            .and_then(|(_, facts)| facts.into_iter().find(|(t, f, _)| t == trait_id && f == fact).map(|(_, _, s)| s)))
    }

    /// Set each of `changes`' summaries (an empty one clears it), each the
    /// fact of a trait in `traits`; returns how many changed.
    pub fn set_summaries(&self, changes: &[SummaryChange], traits: &[Trait], source: &str, at: Moment) -> anyhow::Result<usize> {
        let problems = summary_problems(changes, traits);
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        let mut rows = self.all()?;
        let mut changed = 0;
        for change in changes {
            let id = summary_id(&change.person_id, &change.trait_id, &change.fact_id);
            let summary = change.summary.trim().to_string();
            let row = PersonFact {
                id: Some(id.clone()),
                person_id: Some(change.person_id.clone()),
                trait_id: Some(change.trait_id.clone()),
                fact_id: Some(change.fact_id.clone()),
                summary: Some(summary.clone()).filter(|s| !s.is_empty()),
                updated_at: Some(py::isoformat(&at)),
                source: Some(source.to_string()),
            };
            match rows.iter_mut().find(|r| r.id.as_deref() == Some(id.as_str())) {
                Some(kept) if kept.summary.as_deref().unwrap_or_default() == summary => {}
                Some(kept) => {
                    *kept = row;
                    changed += 1;
                }
                None if summary.is_empty() => {}
                None => {
                    rows.push(row);
                    changed += 1;
                }
            }
        }
        if changed > 0 {
            self.sheet.write_items(&rows)?;
        }
        Ok(changed)
    }

    /// Every log kept, in sheet order.
    pub fn all_logs(&self) -> anyhow::Result<Vec<FactLog>> {
        self.log_sheet.read_items()
    }

    /// `person`'s logs -- of the trait `trait_id` and its fact `fact`, if
    /// given -- newest first.
    pub fn logs(&self, person: &str, trait_id: Option<&str>, fact: Option<&str>) -> anyhow::Result<Vec<LoggedChange>> {
        let mut rows: Vec<FactLog> = self
            .all_logs()?
            .into_iter()
            .filter(|r| {
                r.person_id.as_deref() == Some(person)
                    && trait_id.is_none_or(|t| r.trait_id.as_deref() == Some(t))
                    && fact.is_none_or(|f| r.fact_id.as_deref() == Some(f))
            })
            .collect();
        rows.sort_by(|a, b| {
            let key = |r: &FactLog| (time(r.event_start.as_deref()), r.id.clone());
            key(b).cmp(&key(a))
        });
        Ok(rows
            .into_iter()
            .map(|r| LoggedChange {
                event_id: r.event_id,
                event_summary: r.event_summary,
                event_start: shown_time(r.event_start.as_deref()),
                trait_id: r.trait_id,
                fact_id: r.fact_id,
                log: r.log,
                logged_at: shown_time(r.logged_at.as_deref()),
            })
            .collect())
    }

    /// Index `event`'s logs (each (person id, "<trait id>/<fact id>",
    /// log)), in place of any it had; nothing written when they're as
    /// kept.
    pub fn record_logs(
        &self,
        event_id: &str,
        start: Option<Moment>,
        summary: Option<&str>,
        logs: &[(String, String, String)],
        source: &str,
        at: Moment,
    ) -> anyhow::Result<()> {
        let all = self.all_logs()?;
        let kept: Vec<&FactLog> = all.iter().filter(|r| r.event_id.as_deref() == Some(event_id)).collect();
        let same = kept.len() == logs.len()
            && logs.iter().all(|(person, key, log)| {
                kept.iter().any(|r| {
                    r.person_id.as_deref() == Some(person.as_str())
                        && format!("{}/{}", r.trait_id.as_deref().unwrap_or_default(), r.fact_id.as_deref().unwrap_or_default()) == *key
                        && r.log.as_deref() == Some(log.as_str())
                })
            });
        if same {
            return Ok(());
        }
        let mut rows: Vec<FactLog> = all.iter().filter(|r| r.event_id.as_deref() != Some(event_id)).cloned().collect();
        for (person, key, log) in logs {
            let Some((trait_id, fact)) = key.split_once('/') else { continue };
            rows.push(FactLog {
                id: Some(format!("{event_id}/{person}/{key}")),
                event_id: Some(event_id.to_string()),
                person_id: Some(person.clone()),
                trait_id: Some(trait_id.to_string()),
                fact_id: Some(fact.to_string()),
                log: Some(log.clone()),
                event_start: start.map(|s| py::isoformat(&s)),
                event_summary: summary.map(str::to_string),
                logged_at: Some(py::isoformat(&at)),
                source: Some(source.to_string()),
            });
        }
        self.log_sheet.write_items(&rows)
    }
}

/// What's wrong with `changes`: each a fact of one of `traits`, at most
/// MAX_SUMMARY_CHARS, and each person's fact once.
pub fn summary_problems(changes: &[SummaryChange], traits: &[Trait]) -> Vec<String> {
    let mut problems = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for change in changes {
        let key = format!("{}/{}", change.trait_id, change.fact_id);
        let who = py::repr(&change.person_id);
        match traits.iter().find(|t| t.id.as_deref() == Some(change.trait_id.as_str())) {
            None => problems.push(format!("{who}'s summary of {key}: {} isn't a trait", py::repr(&change.trait_id))),
            Some(t) if t.fact(&change.fact_id).is_none() => problems.push(format!(
                "{who}'s summary of {key}: {} isn't one of {}'s facts{}",
                py::repr(&change.fact_id),
                change.trait_id,
                match t.fact_ids() {
                    ids if ids.is_empty() => " (it has none)".to_string(),
                    ids => format!(" ({})", ids.join(", ")),
                }
            )),
            Some(_) => {}
        }
        if change.summary.chars().count() > MAX_SUMMARY_CHARS {
            problems.push(format!("{who}'s summary of {key} must be at most {MAX_SUMMARY_CHARS} characters"));
        }
        let id = summary_id(&change.person_id, &change.trait_id, &change.fact_id);
        if seen.contains(&id) {
            problems.push(format!("{who}'s summary of {key} is given more than once"));
        }
        seen.push(id);
    }
    problems
}

/// Each person's what_matters, as the People tab kept it, as their
/// summary of each of `traits`' "what_matters" facts.
fn kept_what_matters(sheets: &SheetsClient, spreadsheet_id: &str, traits: &[Trait]) -> anyhow::Result<Vec<PersonFact>> {
    let with_fact: Vec<&str> = traits.iter().filter(|t| t.fact(WHAT_MATTERS).is_some()).filter_map(|t| t.id.as_deref()).collect();
    if with_fact.is_empty() || find_tab(sheets, spreadsheet_id, PEOPLE.0, PEOPLE.1)?.is_none() {
        return Ok(Vec::new());
    }
    let people = RowSheet::ensure(sheets, spreadsheet_id, PEOPLE.0, PEOPLE.1, KEPT_WHAT_MATTERS_ROW, &["id"])?;
    let mut rows = Vec::new();
    for person in people.read_items::<KeptWhatMatters>()? {
        let (Some(id), Some(text)) = (set(person.id.as_deref()), person.what_matters.as_deref().map(str::trim).filter(|t| !t.is_empty())) else {
            continue;
        };
        for trait_id in &with_fact {
            rows.push(PersonFact {
                id: Some(summary_id(id, trait_id, WHAT_MATTERS)),
                person_id: Some(id.to_string()),
                trait_id: Some(trait_id.to_string()),
                fact_id: Some(WHAT_MATTERS.to_string()),
                summary: Some(text.to_string()),
                updated_at: None,
                source: Some("what_matters".into()),
            });
        }
    }
    Ok(rows)
}

#[cfg(test)]
#[path = "person_facts_tests.rs"]
mod tests;
