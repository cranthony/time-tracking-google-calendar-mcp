//! A calendar event, decoupled from the API's raw resource shape -- the
//! `Event` of calendar_clients/google_calendar.py, read from and written
//! to the API exactly as it does.

use crate::facts::Facts;
use crate::py;
use anyhow::{anyhow, bail};
use chrono::{DateTime, FixedOffset, NaiveDateTime, TimeZone};
use chrono_tz::Tz;
use serde_json::{json, Map, Value};
use std::collections::BTreeSet;

pub const PREFIX: &str = "cascading-time-tracker-";
pub const MAX_DESCRIPTION_BYTES: usize = 8192;

pub const CLEARABLE_EVENT_FIELDS: [&str; 5] = ["description", "location", "priority", "facts", "judgments"];

/// The most extended properties each JSON field is split across.
pub fn chunks(name: &str) -> usize {
    match name {
        "facts" => 8,
        _ => 16,
    }
}

const CHUNK_CHARS: usize = 1024;

pub type Moment = DateTime<FixedOffset>;

#[derive(Debug, Clone, Default, PartialEq)]
pub struct Event {
    pub id: Option<String>,
    pub summary: Option<String>,
    pub start: Option<Moment>,
    pub end: Option<Moment>,
    pub description: Option<String>,
    pub location: Option<String>,
    pub status: Option<String>,
    pub recurring_event_id: Option<String>,
    pub recurrence: Option<Vec<String>>,
    pub time_zone: Option<String>,
    pub original_start: Option<Moment>,
    pub priority: Option<i64>,
    pub is_end_of_day_sleep: Option<bool>,
    pub event_label_id: Option<String>,
    pub action_ids: Option<Vec<String>>,
    pub facts: Option<Facts>,
    pub judgments: Option<Map<String, Value>>,
    pub action_priority: Option<i64>,
    pub actions_from_label: bool,
    pub cleared: BTreeSet<String>,
}

impl Event {
    /// `Event(...)`'s checks on `cleared`.
    pub fn check_cleared(&self) -> anyhow::Result<()> {
        let unknown: Vec<&String> = self.cleared.iter().filter(|c| !CLEARABLE_EVENT_FIELDS.contains(&c.as_str())).collect();
        if !unknown.is_empty() {
            let mut clearable: Vec<&str> = CLEARABLE_EVENT_FIELDS.to_vec();
            clearable.sort();
            bail!("Can't clear {}; clearable fields are {}", py::sorted_repr(unknown), py::repr_list(&clearable));
        }
        let both: Vec<&String> = self.cleared.iter().filter(|name| self.is_set(name)).collect();
        if !both.is_empty() {
            bail!("Can't both set and clear {}", py::sorted_repr(both));
        }
        Ok(())
    }

    fn is_set(&self, name: &str) -> bool {
        match name {
            "description" => self.description.is_some(),
            "location" => self.location.is_some(),
            "priority" => self.priority.is_some(),
            "facts" => self.facts.is_some(),
            "judgments" => self.judgments.is_some(),
            _ => false,
        }
    }

    pub fn effective_priority(&self) -> Option<i64> {
        self.priority.or(self.action_priority)
    }

    pub fn is_cancelled(&self) -> bool {
        self.status.as_deref() == Some("cancelled")
    }

    pub fn start(&self) -> Moment {
        self.start.expect("an event read from Calendar has a start")
    }

    pub fn end(&self) -> Moment {
        self.end.expect("an event read from Calendar has an end")
    }

    pub fn from_api(data: &Value) -> anyhow::Result<Event> {
        let empty = Map::new();
        let private = data
            .get("extendedProperties")
            .and_then(|e| e.get("private"))
            .and_then(Value::as_object)
            .unwrap_or(&empty);
        let prop = |name: &str| private.get(&format!("{PREFIX}{name}")).and_then(Value::as_str);
        let mut event = Event::default();
        if let Some(raw) = prop("priority") {
            event.priority = Some(python_int(raw).ok_or_else(|| anyhow!("invalid literal for int() with base 10: {}", py::repr(raw)))?);
        }
        if let Some(raw) = prop("is_end_of_day_sleep") {
            event.is_end_of_day_sleep = Some(raw.to_lowercase() == "true");
        }
        if let Some(raw) = prop("action_ids") {
            event.action_ids = Some(raw.split_whitespace().map(str::to_string).collect());
        }
        if let Some(text) = read_chunks(private, "facts") {
            event.facts = Facts::from_json(&text);
        }
        if let Some(text) = read_chunks(private, "judgments") {
            event.judgments = serde_json::from_str::<Value>(&text).ok().and_then(|v| v.as_object().cloned());
        }
        let start_data = data
            .get("start")
            .filter(|v| !v.is_null())
            .or_else(|| data.get("originalStartTime"))
            .ok_or_else(|| anyhow!("KeyError: 'originalStartTime'"))?;
        let start = parse_datetime(start_data)?;
        let end = match data.get("end") {
            Some(end) => parse_datetime(end)?,
            None => start,
        };
        let text = |key: &str| data.get(key).and_then(Value::as_str).map(str::to_string);
        event.id = text("id");
        event.summary = text("summary");
        event.start = Some(start);
        event.end = Some(end);
        event.description = text("description");
        event.location = text("location");
        event.status = text("status");
        event.recurring_event_id = text("recurringEventId");
        event.recurrence = data
            .get("recurrence")
            .and_then(Value::as_array)
            .map(|lines| lines.iter().filter_map(|l| l.as_str().map(str::to_string)).collect());
        event.time_zone = start_data.get("timeZone").and_then(Value::as_str).map(str::to_string);
        event.original_start = match data.get("originalStartTime") {
            Some(original) => Some(parse_datetime(original)?),
            None => None,
        };
        event.event_label_id = text("eventLabelId");
        Ok(event)
    }

    /// The API body for this event -- what `to_api_body` sends: only what's
    /// set, nulls for what's cleared, and never colorId.
    pub fn to_api_body(&self) -> anyhow::Result<Map<String, Value>> {
        let mut body = Map::new();
        if let Some(summary) = &self.summary {
            body.insert("summary".into(), json!(summary));
        }
        if let Some(start) = &self.start {
            body.insert("start".into(), format_datetime(start, self.time_zone.as_deref()));
        }
        if let Some(end) = &self.end {
            body.insert("end".into(), format_datetime(end, self.time_zone.as_deref()));
        }
        if let Some(recurrence) = &self.recurrence {
            body.insert("recurrence".into(), json!(recurrence));
        }
        if let Some(description) = &self.description {
            body.insert("description".into(), json!(description));
        }
        if let Some(location) = &self.location {
            body.insert("location".into(), json!(location));
        }
        if let Some(status) = &self.status {
            body.insert("status".into(), json!(status));
        }
        if let Some(label) = &self.event_label_id {
            body.insert("eventLabelId".into(), json!(label));
        }
        let mut private: Vec<(String, Option<String>)> = Vec::new();
        let set = |private: &mut Vec<(String, Option<String>)>, key: String, value: Option<String>| {
            if let Some(slot) = private.iter_mut().find(|(k, _)| *k == key) {
                slot.1 = value;
            } else {
                private.push((key, value));
            }
        };
        if let Some(priority) = self.priority {
            set(&mut private, format!("{PREFIX}priority"), Some(priority.to_string()));
        }
        if let Some(sleep) = self.is_end_of_day_sleep {
            set(&mut private, format!("{PREFIX}is_end_of_day_sleep"), Some(if sleep { "true" } else { "false" }.into()));
        }
        if let Some(ids) = &self.action_ids {
            set(&mut private, format!("{PREFIX}action_ids"), Some(ids.join(" ")));
        }
        if let Some(facts) = &self.facts {
            let text = if facts.is_empty() { None } else { Some(facts.to_json()) };
            for (key, value) in write_chunks("facts", text.as_deref())? {
                set(&mut private, key, value);
            }
        }
        if let Some(judgments) = &self.judgments {
            let text = if judgments.is_empty() { None } else { Some(py::json_compact(&Value::Object(judgments.clone()))) };
            for (key, value) in write_chunks("judgments", text.as_deref())? {
                set(&mut private, key, value);
            }
        }
        if self.actions_from_label {
            private.retain(|(k, _)| *k != format!("{PREFIX}action_ids"));
        }
        // Python iterates a frozenset here; the order doesn't reach Google
        // (a body is compared as JSON).
        for name in &self.cleared {
            match name.as_str() {
                "description" | "location" => {
                    body.insert(name.clone(), Value::Null);
                }
                "facts" | "judgments" => {
                    for (key, value) in write_chunks(name, None)? {
                        set(&mut private, key, value);
                    }
                }
                _ => set(&mut private, format!("{PREFIX}{name}"), None),
            }
        }
        if !private.is_empty() {
            let map: Map<String, Value> =
                private.into_iter().map(|(k, v)| (k, v.map(Value::String).unwrap_or(Value::Null))).collect();
            body.insert("extendedProperties".into(), json!({ "private": map }));
        }
        Ok(body)
    }

    /// The body for an insert or import: no null private properties (an
    /// insert refuses them, and there's nothing to remove yet).
    pub fn insert_body(&self) -> anyhow::Result<Map<String, Value>> {
        let mut body = self.to_api_body()?;
        if let Some(Value::Object(ext)) = body.get_mut("extendedProperties") {
            if let Some(Value::Object(private)) = ext.get_mut("private") {
                private.retain(|_, v| !v.is_null());
            }
        }
        Ok(body)
    }
}

/// `int(text)` as Python reads it: surrounding whitespace, a sign, and
/// underscores between digits allowed.
pub fn python_int(text: &str) -> Option<i64> {
    let trimmed = text.trim();
    let (sign, digits) = match trimmed.strip_prefix('-') {
        Some(rest) => (-1, rest),
        None => (1, trimmed.strip_prefix('+').unwrap_or(trimmed)),
    };
    if digits.is_empty() || digits.starts_with('_') || digits.ends_with('_') || digits.contains("__") {
        return None;
    }
    let cleaned: String = digits.chars().filter(|c| *c != '_').collect();
    if !cleaned.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    cleaned.parse::<i64>().ok().map(|n| sign * n)
}

pub fn parse_datetime(value: &Value) -> anyhow::Result<Moment> {
    let raw = value.get("dateTime").and_then(Value::as_str).ok_or_else(|| {
        anyhow!("Event is missing a dateTime with a UTC offset; all-day (date-only) events are not supported")
    })?;
    parse_iso(raw, value.get("timeZone").and_then(Value::as_str))
}

/// `datetime.fromisoformat`, with a "Z" read as UTC and a naive time put
/// in `time_zone`.
pub fn parse_iso(raw: &str, time_zone: Option<&str>) -> anyhow::Result<Moment> {
    let text = match raw.strip_suffix('Z') {
        Some(rest) => format!("{rest}+00:00"),
        None => raw.to_string(),
    };
    if let Ok(moment) = DateTime::parse_from_str(&text, "%Y-%m-%dT%H:%M:%S%.f%:z") {
        return Ok(moment);
    }
    if let Ok(moment) = DateTime::parse_from_str(&text, "%Y-%m-%dT%H:%M%:z") {
        return Ok(moment);
    }
    let naive = NaiveDateTime::parse_from_str(&text, "%Y-%m-%dT%H:%M:%S%.f")
        .or_else(|_| NaiveDateTime::parse_from_str(&text, "%Y-%m-%dT%H:%M"))
        .map_err(|_| anyhow!("Invalid isoformat string: {}", py::repr(raw)))?;
    let zone_name = time_zone.ok_or_else(|| anyhow!("datetime {} must include a UTC offset/timezone", py::repr(&text)))?;
    let zone: Tz = zone_name.parse().map_err(|_| anyhow!("No time zone found with key {zone_name}"))?;
    let local = zone
        .from_local_datetime(&naive)
        .earliest()
        .ok_or_else(|| anyhow!("{naive} doesn't exist in {zone_name}"))?;
    Ok(local.fixed_offset())
}

pub fn format_datetime(moment: &Moment, time_zone: Option<&str>) -> Value {
    let mut map = Map::new();
    map.insert("dateTime".into(), json!(py::isoformat(moment)));
    if let Some(zone) = time_zone {
        map.insert("timeZone".into(), json!(zone));
    }
    Value::Object(map)
}

fn chunk_key(name: &str, index: usize) -> String {
    if index == 0 {
        format!("{PREFIX}{name}")
    } else {
        format!("{PREFIX}{name}-{}", index + 1)
    }
}

fn read_chunks(private: &Map<String, Value>, name: &str) -> Option<String> {
    let first = private.get(&chunk_key(name, 0))?.as_str()?;
    let mut text = first.to_string();
    for index in 1..chunks(name) {
        match private.get(&chunk_key(name, index)).and_then(Value::as_str) {
            Some(part) => text.push_str(part),
            None => break,
        }
    }
    Some(text)
}

/// `text` split into chunks of at most 1024 characters, every chunk it
/// doesn't need set to null; `None` removes the field.
pub fn write_chunks(name: &str, text: Option<&str>) -> anyhow::Result<Vec<(String, Option<String>)>> {
    let most = chunks(name) * CHUNK_CHARS;
    let chars: Vec<char> = text.unwrap_or("").chars().collect();
    if text.is_some() && chars.len() > most {
        bail!("{name} are longer than {most} characters");
    }
    let parts: Vec<String> = chars.chunks(CHUNK_CHARS).map(|c| c.iter().collect()).collect();
    Ok((0..chunks(name)).map(|i| (chunk_key(name, i), parts.get(i).cloned())).collect())
}

#[cfg(test)]
#[path = "event_tests.rs"]
mod api_tests;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_through_the_api_shape() {
        let data = json!({
            "id": "abc",
            "summary": "Walk",
            "start": {"dateTime": "2026-10-07T09:00:00-04:00", "timeZone": "America/New_York"},
            "end": {"dateTime": "2026-10-07T10:00:00-04:00"},
            "extendedProperties": {"private": {
                "cascading-time-tracker-priority": "2",
                "cascading-time-tracker-action_ids": "a b",
                "cascading-time-tracker-facts": "{\"with\":[\"p1\"]}"
            }}
        });
        let event = Event::from_api(&data).unwrap();
        assert_eq!(event.priority, Some(2));
        assert_eq!(event.action_ids, Some(vec!["a".into(), "b".into()]));
        assert_eq!(event.facts.as_ref().unwrap().with(), vec!["p1".to_string()]);
        assert_eq!(event.time_zone.as_deref(), Some("America/New_York"));
        let body = event.to_api_body().unwrap();
        assert_eq!(body["start"]["dateTime"], "2026-10-07T09:00:00-04:00");
        assert_eq!(body["extendedProperties"]["private"]["cascading-time-tracker-facts-2"], Value::Null);
    }

    #[test]
    fn chunks_count_characters_not_bytes() {
        let text: String = std::iter::repeat('é').take(1500).collect();
        let parts = write_chunks("facts", Some(&text)).unwrap();
        assert_eq!(parts[0].1.as_ref().unwrap().chars().count(), 1024);
        assert_eq!(parts[1].1.as_ref().unwrap().chars().count(), 476);
        assert!(parts[2].1.is_none());
    }
}
