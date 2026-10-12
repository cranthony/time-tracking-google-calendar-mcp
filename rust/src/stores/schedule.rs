//! Compaction schedule hints: when the user's scheduled routines run, as
//! times of day, in the Compaction Schedule tab.

use crate::google::sheets::{SheetsClient, TabRange};
use crate::contract::{RowItem, Schema};
use crate::sheet::rows::{self, RowSheet, RowType};
use crate::sheet::COMPACTION_SCHEDULE;
use crate::stores::groups::python_repr;
use anyhow::bail;
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::sync::LazyLock;

pub const MAX_LABEL_LENGTH: usize = 100;
static TIME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\s*(\d{1,2}):(\d{2})\s*$").unwrap());

/// One time a routine runs: when (time) and what it's called (label).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema, RowItem)]
pub struct ScheduleHint {
    pub id: Option<String>,
    pub time: Option<String>,
    pub label: Option<String>,
}

pub const HINT_ROW: RowType = <ScheduleHint as rows::RowItem>::ROW_TYPE;

/// `text` as "HH:MM", if it's a time of day.
pub fn normalized_time(text: Option<&str>) -> Option<String> {
    let caps = TIME.captures(text.unwrap_or(""))?;
    let digits = |s: &str| -> Option<u32> { s.chars().map(|c| c.to_digit(10)).try_fold(0u32, |n, d| Some(n * 10 + d?)) };
    let (hours, minutes) = (digits(&caps[1])?, digits(&caps[2])?);
    if hours > 23 || minutes > 59 {
        return None;
    }
    Some(format!("{hours:02}:{minutes:02}"))
}

fn time_of(hint: &ScheduleHint) -> Option<&str> {
    hint.time.as_deref()
}

#[derive(Clone)]
pub struct CompactionSchedule {
    sheet: RowSheet,
}

impl CompactionSchedule {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<CompactionSchedule> {
        Ok(CompactionSchedule {
            sheet: RowSheet::ensure(sheets, spreadsheet_id, COMPACTION_SCHEDULE.0, COMPACTION_SCHEDULE.1, HINT_ROW, &["id", "time"])?,
        })
    }

    pub fn whole_tab(&self) -> TabRange {
        self.sheet.whole_tab()
    }

    pub fn all(&self) -> anyhow::Result<Vec<ScheduleHint>> {
        Ok(in_order(self.sheet.read_items()?))
    }

    pub fn set_hints(&self, hints: &[ScheduleHint]) -> anyhow::Result<Vec<ScheduleHint>> {
        let problems = hint_problems(hints);
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        let mut saved: Vec<ScheduleHint> = Vec::new();
        let mut taken: Vec<Option<String>> = Vec::new();
        for hint in hints {
            let given = hint.id.clone().filter(|i| !i.is_empty());
            let id = match given {
                Some(id) if !taken.contains(&Some(id.clone())) => id,
                _ => {
                    let mut all: Vec<Option<&str>> = taken.iter().map(|t| t.as_deref()).collect();
                    all.extend(hints.iter().map(|h| h.id.as_deref()));
                    rows::new_id(all)
                }
            };
            taken.push(Some(id.clone()));
            let label = hint.label.as_deref().map(str::trim).filter(|l| !l.is_empty()).map(str::to_string);
            saved.push(ScheduleHint { id: Some(id), time: normalized_time(time_of(hint)), label });
        }
        let saved = in_order(saved);
        self.sheet.write_items(&saved)?;
        Ok(saved)
    }
}

#[cfg(test)]
#[path = "schedule_tests.rs"]
mod tests;

fn in_order(mut hints: Vec<ScheduleHint>) -> Vec<ScheduleHint> {
    hints.sort_by_key(|h| {
        let normalized = normalized_time(time_of(h));
        (normalized.is_none(), normalized.unwrap_or_else(|| time_of(h).unwrap_or("").to_string()))
    });
    hints
}

pub fn hint_problems(hints: &[ScheduleHint]) -> Vec<String> {
    let mut problems = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for hint in hints {
        let Some(time) = normalized_time(time_of(hint)) else {
            problems.push(format!(
                "{} isn't a time of day: give it as \"HH:MM\", 24-hour (e.g. \"07:30\")",
                python_repr(&json!(hint.time))
            ));
            continue;
        };
        if seen.contains(&time) {
            problems.push(format!("{time} is given twice: each time is a hint once"));
        }
        seen.push(time.clone());
        if let Some(label) = hint.label.as_deref() {
            if !label.is_empty() && label.chars().count() > MAX_LABEL_LENGTH {
                problems.push(format!("the label at {time} is longer than {MAX_LABEL_LENGTH} characters"));
            }
        }
    }
    problems
}
