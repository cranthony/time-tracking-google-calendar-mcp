//! Trait examples: stories of the user or someone else embodying a trait
//! well -- when it happened, who did it, what they did and why it
//! embodies the trait -- kept in the Trait Examples tab, one row per
//! example. They show what doing a trait well looks like: the app marks
//! the events they happened at, and judging is shown them to calibrate by.

use crate::contract::{RowItem, Schema};
use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::sheet::rows::{self, RowSheet, RowType};
use crate::sheet::TRAIT_EXAMPLES;
use crate::stores::circadian::parsed_date;
use anyhow::bail;
use serde::{Deserialize, Serialize};

/// The most an example's what and why can each be, in characters.
pub const MAX_STORY_CHARS: usize = 2000;

/// One example of a trait done well (see get_trait_examples).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema, RowItem)]
#[schema(deny_unknown)]
pub struct TraitExample {
    /// Made by the server.
    pub id: Option<String>,
    /// The trait it's an example of.
    pub trait_id: Option<String>,
    /// The day it happened, "YYYY-MM-DD".
    pub when: Option<String>,
    /// Who did it, if they're in People: a person (the user is "self") or
    /// a community.
    pub person_id: Option<String>,
    /// Who did it, in words, for someone not in People ("a stranger on
    /// the train"). It or person_id is needed; both can be given.
    pub who: Option<String>,
    /// What they did (at most 2000 characters).
    pub what: Option<String>,
    /// Why it embodies the trait (at most 2000 characters).
    pub why: Option<String>,
    /// The calendar event it happened at, if there's one.
    pub event_id: Option<String>,
    /// Goes up by one each time it changes. Give it to an update (and to a delete, as if_version): it's the version you last read, required -- one saved before versions is version 0 -- changed since, the change is refused; on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK.
    pub version: Option<i64>,
}

/// A trait example's fields that clear_fields can blank.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum ClearableField {
    #[serde(rename = "person_id")]
    PersonId,
    #[serde(rename = "who")]
    Who,
    #[serde(rename = "event_id")]
    EventId,
}

pub const ROW: RowType = <TraitExample as rows::RowItem>::ROW_TYPE;
pub const CLEARABLE_FIELDS: [&str; 3] = ["person_id", "who", "event_id"];

/// What an example can name: the traits' ids, and everyone's (people's,
/// "self" among them, and communities').
pub struct Known<'a> {
    pub trait_ids: &'a [String],
    pub person_ids: &'a [String],
}

/// What's wrong with `example`, as phrases; nothing if it's well formed.
pub fn example_problems(example: &TraitExample, known: &Known) -> Vec<String> {
    let mut problems = Vec::new();
    let given = |v: &Option<String>| v.as_deref().map(str::trim).filter(|s| !s.is_empty()).map(str::to_string);
    match given(&example.trait_id) {
        None => problems.push("it needs a trait_id: the trait it's an example of".to_string()),
        Some(id) if !known.trait_ids.contains(&id) => {
            problems.push(format!("its trait_id {} isn't a trait (get_traits lists them)", py::repr(&id)))
        }
        Some(_) => {}
    }
    match example.when.as_deref() {
        None => problems.push("it needs a when: the day it happened, \"YYYY-MM-DD\"".to_string()),
        Some(when) if parsed_date(when).is_none() => {
            problems.push(format!("its when {} isn't a date: give it as \"YYYY-MM-DD\" (e.g. \"2026-11-02\")", py::repr(when)))
        }
        Some(_) => {}
    }
    let (person, who) = (given(&example.person_id), given(&example.who));
    if example.person_id.is_some() && person.is_none() {
        problems.push("its person_id must be a person's or community's id, or left out".to_string());
    }
    if example.who.is_some() && who.is_none() {
        problems.push("its who must be non-empty text, or left out".to_string());
    }
    if let Some(id) = &person {
        if !known.person_ids.contains(id) {
            problems.push(format!("its person_id {} isn't a person or a community (get_people and get_circles list them)", py::repr(id)));
        }
    } else if who.is_none() && example.who.is_none() && example.person_id.is_none() {
        problems.push("it needs who did it: a person_id (\"self\" for the user), or who, in words, for someone not in People".to_string());
    }
    for (name, value, says) in [("what", &example.what, "what they did"), ("why", &example.why, "why it embodies the trait")] {
        match given(value) {
            None => problems.push(format!("it needs a {name}: {says}")),
            Some(_) => {
                let length = value.as_deref().unwrap_or_default().chars().count();
                if length > MAX_STORY_CHARS {
                    problems.push(format!("its {name} is {length} characters; it can be at most {MAX_STORY_CHARS}"));
                }
            }
        }
    }
    if example.event_id.is_some() && given(&example.event_id).is_none() {
        problems.push("its event_id must be an event's id, or left out".to_string());
    }
    problems
}

/// `problems` as a refusal.
fn said(problems: &[String]) -> String {
    format!("The trait example: {}", problems.join("; "))
}

/// `examples` newest first: by when, latest first, and of a day, the one
/// added last first.
pub fn newest_first(mut examples: Vec<TraitExample>) -> Vec<TraitExample> {
    examples.reverse();
    examples.sort_by(|a, b| b.when.cmp(&a.when));
    examples
}

#[derive(Clone)]
pub struct TraitExamples {
    sheet: RowSheet,
}

impl TraitExamples {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<TraitExamples> {
        let sheet = RowSheet::ensure(sheets, spreadsheet_id, TRAIT_EXAMPLES.0, TRAIT_EXAMPLES.1, ROW, &["id", "trait_id"])?;
        Ok(TraitExamples { sheet })
    }

    pub fn whole_tab(&self) -> TabRange {
        self.sheet.whole_tab()
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheet.prefetch(ranges)
    }

    /// Every example, in the tab's order.
    pub fn all(&self) -> anyhow::Result<Vec<TraitExample>> {
        self.sheet.read_items()
    }

    /// The examples, newest first (see newest_first): with `trait_id`,
    /// only its; with `event_ids`, only those at one of them.
    pub fn get_trait_examples(&self, trait_id: Option<&str>, event_ids: Option<&[String]>) -> anyhow::Result<Vec<TraitExample>> {
        let all = self.all()?;
        let kept = all.into_iter().filter(|e| {
            trait_id.is_none_or(|t| e.trait_id.as_deref() == Some(t))
                && event_ids.is_none_or(|ids| e.event_id.as_ref().is_some_and(|id| ids.contains(id)))
        });
        Ok(newest_first(kept.collect()))
    }

    pub fn create_trait_example(&self, given: &TraitExample, known: &Known) -> anyhow::Result<TraitExample> {
        let mut all = self.all()?;
        let id = match rows::created(given, &all, &[])? {
            rows::Created::New(id) => id,
            rows::Created::Again(made) => return Ok(made),
        };
        let new = TraitExample { id: Some(id), version: None, ..trimmed(given) };
        let problems = example_problems(&new, known);
        if !problems.is_empty() {
            bail!("{}", said(&problems));
        }
        all.push(new.clone());
        self.sheet.write_items(&all)?;
        Ok(new)
    }

    pub fn update_trait_example(&self, given: &TraitExample, clear_fields: &[String], known: &Known) -> anyhow::Result<TraitExample> {
        let Some(id) = given.id.clone().filter(|i| !i.is_empty()) else {
            bail!("update_trait_example needs the example's id");
        };
        rows::check_clear_item(given, clear_fields, &CLEARABLE_FIELDS)?;
        let mut all = self.all()?;
        let Some(index) = all.iter().position(|e| e.id.as_deref() == Some(id.as_str())) else {
            rows::expect_item_version::<TraitExample>(&id, None, given.version)?;
            bail!("There's no trait example with the id {} (get_trait_examples lists them)", py::repr(&id));
        };
        let updated = rows::updated_item(&all[index], &trimmed(given), clear_fields, &["id"])?;
        let problems = example_problems(&updated, known);
        if !problems.is_empty() {
            bail!("{}", said(&problems));
        }
        all[index] = updated;
        self.sheet.write_items(&all)?;
        Ok(all[index].clone())
    }

    /// Deletes an example -- unless it's changed since version
    /// `if_version`, if given.
    pub fn delete_trait_example(&self, example_id: &str, if_version: Option<i64>) -> anyhow::Result<TraitExample> {
        let all = self.all()?;
        let found = all.iter().find(|e| e.id.as_deref() == Some(example_id)).cloned();
        if !rows::deleting(example_id, found.as_ref(), if_version)? && if_version.is_some() {
            return Ok(rows::gone(example_id));
        }
        let Some(deleted) = found else {
            bail!("There's no trait example with the id {} (get_trait_examples lists them)", py::repr(example_id));
        };
        let kept: Vec<TraitExample> = all.into_iter().filter(|e| e.id.as_deref() != Some(example_id)).collect();
        self.sheet.write_items(&kept)?;
        Ok(deleted)
    }
}

/// `example` with its date and ids trimmed of spaces around them.
fn trimmed(example: &TraitExample) -> TraitExample {
    let trim = |v: &Option<String>| v.as_ref().map(|s| s.trim().to_string());
    TraitExample {
        trait_id: trim(&example.trait_id),
        when: trim(&example.when),
        person_id: trim(&example.person_id),
        event_id: trim(&example.event_id),
        ..example.clone()
    }
}

// Written by the app: made once by their ids and versions.
impl rows::Versioned for TraitExample {}

#[cfg(test)]
#[path = "trait_examples_tests.rs"]
mod tests;
