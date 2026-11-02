//! The user's circadian rhythm: the time they woke naturally, a row per
//! revision in the Natural Wake Times tab, each applying from its date
//! on; and the windows derived from it -- their temperature minimum, a
//! dim window before it and a bright window after -- whose lengths are
//! kept in the Circadian Windows tab.

use crate::contract::{DateTimeText, RowItem, Schema};
use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::sheet::rows::{self, RowSheet, RowType};
use crate::sheet::{CIRCADIAN_WINDOWS, NATURAL_WAKE_TIMES};
use crate::stores::groups::python_repr;
use crate::stores::schedule::normalized_time;
use crate::zoned::{in_zone, resolve, Moment};
use anyhow::bail;
use chrono::{Duration, Months, NaiveDate, NaiveTime};
use chrono_tz::Tz;
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::sync::LazyLock;

pub const MAX_NOTE_LENGTH: usize = 500;
/// Rows dated more than this many days before the newest are trimmed.
pub const KEPT_DAYS: i64 = 366;
/// The most days get_circadian_rhythm derives in one call.
pub const MAX_RANGE_DAYS: i64 = 400;
pub const MAX_WINDOW_MINUTES: i64 = 1440;
static DATE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^\s*(\d{4}-\d{2}-\d{2})\s*$").unwrap());

/// The time the user woke naturally, from `date` on, as the Natural Wake
/// Times tab keeps it.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema, RowItem)]
pub struct WakeTime {
    pub id: Option<String>,
    pub date: Option<String>,
    pub time: Option<String>,
    pub note: Option<String>,
}

pub const WAKE_TIME_ROW: RowType = <WakeTime as rows::RowItem>::ROW_TYPE;

/// A window's length, a row of the Circadian Windows tab: id is the
/// setting's name.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema, RowItem)]
pub struct WindowSetting {
    pub id: Option<String>,
    pub minutes: Option<i64>,
}

pub const WINDOW_ROW: RowType = <WindowSetting as rows::RowItem>::ROW_TYPE;

/// How the temperature minimum and the windows around it are derived
/// from a wake time, each a whole number of minutes from 0 to 1440.
#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct Windows {
    /// How long before waking the temperature minimum is (default 120).
    pub minimum_before_waking_minutes: i64,
    /// How long the dim window, ending at the minimum, is (default 360).
    pub dim_before_minutes: i64,
    /// How long the bright window, starting at the minimum, is (default 240).
    pub bright_after_minutes: i64,
}

impl Default for Windows {
    fn default() -> Windows {
        Windows {
            minimum_before_waking_minutes: 120,
            dim_before_minutes: 360,
            bright_after_minutes: 240,
        }
    }
}

const WINDOW_NAMES: [&str; 3] = [
    "minimum_before_waking_minutes",
    "dim_before_minutes",
    "bright_after_minutes",
];

impl Windows {
    fn get(&self, name: &str) -> i64 {
        match name {
            "minimum_before_waking_minutes" => self.minimum_before_waking_minutes,
            "dim_before_minutes" => self.dim_before_minutes,
            _ => self.bright_after_minutes,
        }
    }

    fn get_mut(&mut self, name: &str) -> &mut i64 {
        match name {
            "minimum_before_waking_minutes" => &mut self.minimum_before_waking_minutes,
            "dim_before_minutes" => &mut self.dim_before_minutes,
            _ => &mut self.bright_after_minutes,
        }
    }
}

fn is_window_length(minutes: i64) -> bool {
    (0..=MAX_WINDOW_MINUTES).contains(&minutes)
}

/// A wake time as get_circadian_rhythm gives it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct Revision {
    pub id: String,
    /// The day it applies from ("YYYY-MM-DD"), until the next revision's.
    pub date: String,
    /// When the user woke naturally ("HH:MM", 24-hour, in the calendar's time zone).
    pub time: String,
    pub note: Option<String>,
    /// The clock time ("HH:MM") of the temperature minimum on its own date.
    pub temperature_minimum: String,
}

/// A window, from start to end.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct Window {
    pub start: DateTimeText,
    pub end: DateTimeText,
}

/// One day's temperature minimum and windows, from the revision that
/// applies to it.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct CircadianDay {
    /// "YYYY-MM-DD".
    pub date: String,
    /// The revision applying: the one with the latest date on or before it.
    pub revision_id: String,
    /// "HH:MM": the revision's wake time.
    pub wake_time: String,
    pub temperature_minimum: DateTimeText,
    /// Avoid bright light and hard exercise: they shift the schedule later.
    pub dim: Window,
    /// Seek bright light and exercise: they hold the schedule, or shift it earlier.
    pub bright: Window,
}

/// `text` as a date, if it's one ("YYYY-MM-DD").
pub fn parsed_date(text: &str) -> Option<NaiveDate> {
    let caps = DATE.captures(text)?;
    NaiveDate::parse_from_str(&caps[1], "%Y-%m-%d").ok()
}

fn date_problem(text: &str) -> String {
    format!(
        "{} isn't a date: give it as \"YYYY-MM-DD\" (e.g. \"2026-11-02\")",
        python_repr(&json!(text))
    )
}

/// `text` as a date, or refused saying what to send.
pub fn date_given(text: &str) -> anyhow::Result<NaiveDate> {
    match parsed_date(text) {
        Some(date) => Ok(date),
        None => bail!("{}", date_problem(text)),
    }
}

/// A row's date and wake time, if they're a date and a time of day.
fn when(row: &WakeTime) -> Option<(NaiveDate, NaiveTime)> {
    let date = parsed_date(row.date.as_deref()?)?;
    let time = normalized_time(row.time.as_deref())?;
    Some((date, NaiveTime::parse_from_str(&time, "%H:%M").ok()?))
}

/// The rows that are a date and a time of day, oldest first.
fn in_order(rows: Vec<WakeTime>) -> Vec<(NaiveDate, NaiveTime, WakeTime)> {
    let mut dated: Vec<_> = rows
        .into_iter()
        .filter_map(|row| when(&row).map(|(date, time)| (date, time, row)))
        .collect();
    dated.sort_by_key(|(date, _, _)| *date);
    dated
}

/// The temperature minimum for a wake time on `date`: that instant in
/// `zone`, less minimum_before_waking_minutes.
fn minimum_at(date: NaiveDate, wake: NaiveTime, windows: &Windows, zone: &Tz) -> Moment {
    let waking = resolve(zone, date.and_time(wake));
    in_zone(
        &(waking - Duration::minutes(windows.minimum_before_waking_minutes)),
        zone,
    )
}

fn revision(
    date: NaiveDate,
    wake: NaiveTime,
    row: &WakeTime,
    windows: &Windows,
    zone: Option<&Tz>,
) -> Revision {
    let minimum = match zone {
        Some(zone) => minimum_at(date, wake, windows, zone).time(),
        // Without a zone, the clock's arithmetic.
        None => {
            (date.and_time(wake) - Duration::minutes(windows.minimum_before_waking_minutes)).time()
        }
    };
    Revision {
        id: row.id.clone().unwrap_or_default(),
        date: date.format("%Y-%m-%d").to_string(),
        time: wake.format("%H:%M").to_string(),
        note: row.note.clone(),
        temperature_minimum: minimum.format("%H:%M").to_string(),
    }
}

fn instant(moment: &Moment) -> DateTimeText {
    DateTimeText(py::isoformat(moment))
}

/// Each day from `from` to `to` (inclusive) with a revision on or before
/// it: its temperature minimum and windows, in `zone`. Refused if `to` is
/// before `from`, or the range is over MAX_RANGE_DAYS.
pub fn days(
    revisions: &[Revision],
    windows: &Windows,
    zone: &Tz,
    from: NaiveDate,
    to: NaiveDate,
) -> anyhow::Result<Vec<CircadianDay>> {
    if to < from {
        bail!("to ({to}) is before from ({from}): give from first, then to, on or after it");
    }
    let count = (to - from).num_days() + 1;
    if count > MAX_RANGE_DAYS {
        bail!("from {from} to {to} is {count} days: give a range of at most {MAX_RANGE_DAYS}");
    }
    let mut out = Vec::new();
    for date in from.iter_days().take(count as usize) {
        let applying = revisions
            .iter()
            .rev()
            .find(|r| parsed_date(&r.date).is_some_and(|d| d <= date));
        let Some(applying) = applying else { continue };
        let Ok(wake) = NaiveTime::parse_from_str(&applying.time, "%H:%M") else {
            continue;
        };
        let minimum = minimum_at(date, wake, windows, zone);
        let dim_start = in_zone(
            &(minimum - Duration::minutes(windows.dim_before_minutes)),
            zone,
        );
        let bright_end = in_zone(
            &(minimum + Duration::minutes(windows.bright_after_minutes)),
            zone,
        );
        out.push(CircadianDay {
            date: date.format("%Y-%m-%d").to_string(),
            revision_id: applying.id.clone(),
            wake_time: applying.time.clone(),
            temperature_minimum: instant(&minimum),
            dim: Window {
                start: instant(&dim_start),
                end: instant(&minimum),
            },
            bright: Window {
                start: instant(&minimum),
                end: instant(&bright_end),
            },
        });
    }
    Ok(out)
}

#[derive(Clone)]
pub struct Circadian {
    wake_times: RowSheet,
    windows: RowSheet,
}

impl Circadian {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<Circadian> {
        Ok(Circadian {
            wake_times: RowSheet::ensure(
                sheets,
                spreadsheet_id,
                NATURAL_WAKE_TIMES.0,
                NATURAL_WAKE_TIMES.1,
                WAKE_TIME_ROW,
                &["id", "date", "time"],
            )?,
            windows: RowSheet::ensure(
                sheets,
                spreadsheet_id,
                CIRCADIAN_WINDOWS.0,
                CIRCADIAN_WINDOWS.1,
                WINDOW_ROW,
                &["id", "minutes"],
            )?,
        })
    }

    /// Both tabs, read together the first time.
    pub fn prefetch(&self) -> anyhow::Result<()> {
        self.wake_times
            .prefetch(&[self.wake_times.whole_tab(), self.windows.whole_tab()])
    }

    pub fn whole_tabs(&self) -> Vec<TabRange> {
        vec![self.wake_times.whole_tab(), self.windows.whole_tab()]
    }

    /// The windows' lengths: a default for any whose row is missing (or
    /// isn't a whole number of minutes from 0 to 1440).
    pub fn windows(&self) -> anyhow::Result<Windows> {
        let mut out = Windows::default();
        for row in self.windows.read_items::<WindowSetting>()? {
            let name = row.id.as_deref().filter(|id| WINDOW_NAMES.contains(id));
            if let (Some(name), Some(minutes)) =
                (name, row.minutes.filter(|m| is_window_length(*m)))
            {
                *out.get_mut(name) = minutes;
            }
        }
        Ok(out)
    }

    /// Sets all three windows; refused, saying why, if any isn't a whole
    /// number of minutes from 0 to 1440.
    pub fn set_windows(&self, windows: &Windows) -> anyhow::Result<Windows> {
        let problems: Vec<String> = WINDOW_NAMES
            .iter()
            .filter(|name| !is_window_length(windows.get(name)))
            .map(|name| {
                format!(
                    "{name} is {}: give a whole number of minutes from 0 to {MAX_WINDOW_MINUTES}",
                    windows.get(name)
                )
            })
            .collect();
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        let rows: Vec<WindowSetting> = WINDOW_NAMES
            .iter()
            .map(|name| WindowSetting {
                id: Some(name.to_string()),
                minutes: Some(windows.get(name)),
            })
            .collect();
        self.windows.write_items(&rows)?;
        Ok(*windows)
    }

    /// The wake times, oldest first, each with its temperature minimum's
    /// clock time (in `zone`, if the calendar has one). A row that isn't
    /// a date and a time of day is left out.
    pub fn revisions(&self, windows: &Windows, zone: Option<&Tz>) -> anyhow::Result<Vec<Revision>> {
        let rows = in_order(self.wake_times.read_items()?);
        Ok(rows
            .iter()
            .map(|(date, wake, row)| revision(*date, *wake, row, windows, zone))
            .collect())
    }

    /// Records that the user woke naturally at `time` on `date`, replacing
    /// that date's row if there is one (its note too: none given clears
    /// it), then trims rows dated more than KEPT_DAYS before the newest.
    /// Refused if the date or time isn't one, the date is more than a year
    /// after `today`, or the note is over MAX_NOTE_LENGTH characters.
    pub fn set_wake_time(
        &self,
        date: &str,
        time: &str,
        note: Option<&str>,
        today: NaiveDate,
    ) -> anyhow::Result<()> {
        let mut problems = Vec::new();
        let day = parsed_date(date);
        if day.is_none() {
            problems.push(date_problem(date));
        }
        let latest = today
            .checked_add_months(Months::new(12))
            .unwrap_or(NaiveDate::MAX);
        if let Some(day) = day.filter(|d| *d > latest) {
            problems.push(format!(
                "{day} is more than a year from today ({today}): give a date on or before {latest}"
            ));
        }
        let wake = normalized_time(Some(time));
        if wake.is_none() {
            problems.push(format!(
                "{} isn't a time of day: give it as \"HH:MM\", 24-hour (e.g. \"06:40\")",
                python_repr(&json!(time))
            ));
        }
        let note = note.map(str::trim).filter(|n| !n.is_empty());
        if note.is_some_and(|n| n.chars().count() > MAX_NOTE_LENGTH) {
            problems.push(format!(
                "the note is longer than {MAX_NOTE_LENGTH} characters"
            ));
        }
        let (Some(day), Some(wake), true) = (day, wake, problems.is_empty()) else {
            bail!("{}", problems.join("; "));
        };
        let mut rows = in_order(self.wake_times.read_items()?);
        let id = match rows.iter().position(|(d, _, _)| *d == day) {
            Some(i) => rows.remove(i).2.id.filter(|id| !id.is_empty()),
            None => None,
        };
        let id = id.unwrap_or_else(|| rows::new_id(rows.iter().map(|(_, _, r)| r.id.as_deref())));
        let parsed = NaiveTime::parse_from_str(&wake, "%H:%M")?;
        let saved = WakeTime {
            id: Some(id),
            date: Some(day.format("%Y-%m-%d").to_string()),
            time: Some(wake),
            note: note.map(str::to_string),
        };
        rows.push((day, parsed, saved));
        rows.sort_by_key(|(date, _, _)| *date);
        let newest = rows.last().map(|(date, _, _)| *date).unwrap_or(day);
        let kept: Vec<WakeTime> = rows
            .into_iter()
            .filter(|(date, _, _)| (newest - *date).num_days() <= KEPT_DAYS)
            .map(|(_, _, row)| row)
            .collect();
        self.wake_times.write_items(&kept)
    }
}

#[cfg(test)]
#[path = "circadian_tests.rs"]
mod tests;
