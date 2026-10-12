//! Compaction schedule hints (utilities/compaction_schedule.py): when the
//! user's scheduled routines run, as times of day, in the Compaction
//! Schedule tab.

use crate::google::sheets::{SheetsClient, TabRange};
use crate::sheet::rows::{self, Kind, Row, RowSheet, RowType};
use crate::sheet::COMPACTION_SCHEDULE;
use crate::stores::groups::{python_repr, truthy};
use anyhow::bail;
use regex::Regex;
use serde_json::{json, Value};
use std::sync::LazyLock;

pub const MAX_LABEL_LENGTH: usize = 100;
static TIME: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^\s*(\d{1,2}):(\d{2})\s*$").unwrap());

pub const HINT_ROW: RowType = RowType { name: "ScheduleHint", fields: &[("id", Kind::Str), ("time", Kind::Str), ("label", Kind::Str)] };

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

fn time_of(hint: &Row) -> Option<&str> {
    rows::text(hint, "time")
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

    pub fn all(&self) -> anyhow::Result<Vec<Row>> {
        Ok(in_order(self.sheet.read()?))
    }

    pub fn set_hints(&self, hints: &[Row]) -> anyhow::Result<Vec<Row>> {
        let problems = hint_problems(hints);
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        let mut saved: Vec<Row> = Vec::new();
        let mut taken: Vec<Option<String>> = Vec::new();
        for hint in hints {
            let given = hint.get("id").filter(|i| truthy(Some(i))).and_then(Value::as_str).map(str::to_string);
            let id = match given {
                Some(id) if !taken.contains(&Some(id.clone())) => id,
                _ => {
                    let mut all: Vec<Option<&str>> = taken.iter().map(|t| t.as_deref()).collect();
                    all.extend(hints.iter().map(|h| rows::text(h, "id")));
                    rows::new_id(all)
                }
            };
            taken.push(Some(id.clone()));
            let label = hint.get("label").and_then(Value::as_str).map(str::trim).filter(|l| !l.is_empty()).map(str::to_string);
            let mut out = hint.clone();
            out.insert("id".into(), json!(id));
            out.insert("time".into(), json!(normalized_time(time_of(hint))));
            out.insert("label".into(), json!(label));
            saved.push(out);
        }
        let saved = in_order(saved);
        self.sheet.write(&saved)?;
        Ok(saved)
    }
}

fn in_order(mut hints: Vec<Row>) -> Vec<Row> {
    hints.sort_by_key(|h| {
        let normalized = normalized_time(time_of(h));
        (normalized.is_none(), normalized.unwrap_or_else(|| time_of(h).unwrap_or("").to_string()))
    });
    hints
}

pub fn hint_problems(hints: &[Row]) -> Vec<String> {
    let mut problems = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for hint in hints {
        let Some(time) = normalized_time(time_of(hint)) else {
            problems.push(format!(
                "{} isn't a time of day: give it as \"HH:MM\", 24-hour (e.g. \"07:30\")",
                python_repr(hint.get("time").unwrap_or(&Value::Null))
            ));
            continue;
        };
        if seen.contains(&time) {
            problems.push(format!("{time} is given twice: each time is a hint once"));
        }
        seen.push(time.clone());
        if let Some(label) = hint.get("label").and_then(Value::as_str) {
            if !label.is_empty() && label.chars().count() > MAX_LABEL_LENGTH {
                problems.push(format!("the label at {time} is longer than {MAX_LABEL_LENGTH} characters"));
            }
        }
    }
    problems
}
