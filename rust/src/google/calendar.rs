//! Google Calendar, behind a thin client (CalendarClient):
//! list/get/create/update/delete events, labels guarded by the calendar's
//! etag, and this app's metadata in the calendar's description. Listings
//! inside one tool call are cached (see `CachedListings`).

use super::{segment, status_of, Google};
use crate::event::{Event, Moment};
use crate::py;
use crate::write_lock;
use anyhow::{anyhow, bail};
use chrono_tz::Tz;
use regex::Regex;
use serde_json::{json, Map, Value};
use std::cell::RefCell;
use std::sync::{Arc, LazyLock, Mutex};

const LIST_PAGE_SIZE: usize = 2500;

static METADATA_MARKER: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?m)^\[cascading-time-tracker:([\w-]+)=([^\]]*)\]$").unwrap());

/// A write that lost a race: the calendar changed between this write's
/// read and its write (a stale etag).
#[derive(Debug)]
pub struct EventLabelConflictError;

impl std::fmt::Display for EventLabelConflictError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("This calendar changed while this update was being made; fetch its current value and try again.")
    }
}

impl std::error::Error for EventLabelConflictError {}

/// The calendar has no time zone of its own.
#[derive(Debug)]
pub struct TimeZoneNotSetError(pub String);

impl std::fmt::Display for TimeZoneNotSetError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Calendar {} has no time zone set", self.0)
    }
}

impl std::error::Error for TimeZoneNotSetError {}

/// A raw event label, as Calendar keeps one.
#[derive(Debug, Clone, PartialEq)]
pub struct EventLabel {
    pub id: Option<String>,
    pub background_color: String,
    pub name: Option<String>,
}

impl EventLabel {
    pub fn from_api(data: &Value) -> anyhow::Result<EventLabel> {
        Ok(EventLabel {
            id: data.get("id").and_then(Value::as_str).map(str::to_string),
            background_color: data
                .get("backgroundColor")
                .and_then(Value::as_str)
                .ok_or_else(|| anyhow!("KeyError: 'backgroundColor'"))?
                .to_string(),
            name: data.get("name").and_then(Value::as_str).map(str::to_string),
        })
    }

    pub fn to_api_body(&self) -> Value {
        let mut body = Map::new();
        body.insert("backgroundColor".into(), json!(self.background_color));
        if let Some(id) = &self.id {
            body.insert("id".into(), json!(id));
        }
        if let Some(name) = &self.name {
            body.insert("name".into(), json!(name));
        }
        Value::Object(body)
    }
}

struct Listing {
    calendar_id: String,
    time_min: Moment,
    time_max: Moment,
    events: Vec<Event>,
}

thread_local! {
    static LISTINGS: RefCell<Option<Vec<Listing>>> = const { RefCell::new(None) };
}

/// Within this guard's life, a listing inside one made earlier (same
/// calendar, range within it) is answered from memory; any write forgets
/// every listing first.
pub struct CachedListings {
    outer: bool,
}

impl CachedListings {
    pub fn start() -> CachedListings {
        let outer = LISTINGS.with(|l| {
            let mut listings = l.borrow_mut();
            if listings.is_none() {
                *listings = Some(Vec::new());
                true
            } else {
                false
            }
        });
        CachedListings { outer }
    }
}

impl Drop for CachedListings {
    fn drop(&mut self) {
        if self.outer {
            LISTINGS.with(|l| *l.borrow_mut() = None);
        }
    }
}

/// Whether Calendar lists `event` for the range: it ends after
/// `time_min` (or, taking no time, is at it) and starts before `time_max`.
fn overlaps(event: &Event, time_min: &Moment, time_max: &Moment) -> bool {
    let (start, end) = (event.start(), event.end());
    (end > *time_min || (start == end && start == *time_min)) && start < *time_max
}

fn cached_listing(calendar_id: &str, time_min: &Moment, time_max: &Moment) -> Option<Vec<Event>> {
    LISTINGS.with(|l| {
        let listings = l.borrow();
        for listing in listings.as_ref()? {
            if listing.calendar_id == calendar_id && listing.time_min <= *time_min && *time_max <= listing.time_max {
                return Some(listing.events.iter().filter(|e| overlaps(e, time_min, time_max)).cloned().collect());
            }
        }
        None
    })
}

fn remember_listing(listing: Listing) {
    LISTINGS.with(|l| {
        if let Some(listings) = l.borrow_mut().as_mut() {
            listings.push(listing);
        }
    });
}

/// `requires_write_lock`, and forget every cached listing before writing.
fn writing(what: &str) -> anyhow::Result<()> {
    write_lock::require(what)?;
    LISTINGS.with(|l| {
        if let Some(listings) = l.borrow_mut().as_mut() {
            listings.clear();
        }
    });
    Ok(())
}

#[derive(Clone)]
pub struct CalendarClient {
    google: Arc<Google>,
    calendar_id: String,
    time_zone: Arc<Mutex<Option<Tz>>>,
}

impl CalendarClient {
    pub fn new(google: Arc<Google>, calendar_id: &str) -> CalendarClient {
        CalendarClient { google, calendar_id: calendar_id.into(), time_zone: Arc::new(Mutex::new(None)) }
    }

    pub fn calendar_id(&self) -> &str {
        &self.calendar_id
    }

    pub fn google(&self) -> &Arc<Google> {
        &self.google
    }

    pub fn for_calendar(&self, calendar_id: &str) -> CalendarClient {
        CalendarClient::new(self.google.clone(), calendar_id)
    }

    fn calendar_path(&self) -> String {
        format!("calendars/{}", segment(&self.calendar_id))
    }

    fn events_path(&self) -> String {
        format!("calendars/{}/events", segment(&self.calendar_id))
    }

    fn event_path(&self, event_id: &str) -> String {
        format!("calendars/{}/events/{}", segment(&self.calendar_id), segment(event_id))
    }

    fn call(&self, method: &str, path: &str, query: &[(&str, String)], body: Option<&Value>) -> anyhow::Result<Value> {
        self.google.request(method, &self.google.calendar_url(path), query, body, None)
    }

    pub fn create_calendar(&self, summary: &str, description: Option<&str>, time_zone: Option<&str>) -> anyhow::Result<String> {
        writing("CalendarClient.create_calendar")?;
        let mut body = Map::new();
        body.insert("summary".into(), json!(summary));
        if let Some(d) = description {
            body.insert("description".into(), json!(d));
        }
        if let Some(zone) = time_zone {
            body.insert("timeZone".into(), json!(zone));
        }
        let response = self.call("POST", "calendars", &[], Some(&Value::Object(body)))?;
        Ok(response["id"].as_str().unwrap_or_default().to_string())
    }

    /// Best effort: hide `calendar_id` from the user's calendar list.
    pub fn hide_calendar(&self, calendar_id: &str) -> anyhow::Result<bool> {
        writing("CalendarClient.hide_calendar")?;
        let path = format!("users/me/calendarList/{}", segment(calendar_id));
        Ok(self.call("PATCH", &path, &[], Some(&json!({"hidden": true}))).is_ok())
    }

    pub fn get_calendar(&self) -> anyhow::Result<Value> {
        self.call("GET", &self.calendar_path(), &[], None)
    }

    /// The calendar's own time zone, fetched once then remembered.
    pub fn get_time_zone(&self) -> anyhow::Result<Tz> {
        if let Some(zone) = *self.time_zone.lock().unwrap() {
            return Ok(zone);
        }
        let calendar = self.get_calendar()?;
        let name = calendar.get("timeZone").and_then(Value::as_str).filter(|z| !z.is_empty());
        let name = name.ok_or_else(|| TimeZoneNotSetError(self.calendar_id.clone()))?;
        let zone: Tz = name.parse().map_err(|_| anyhow!("No time zone found with key {name}"))?;
        *self.time_zone.lock().unwrap() = Some(zone);
        Ok(zone)
    }

    pub fn set_time_zone(&self, time_zone: &str) -> anyhow::Result<Tz> {
        writing("CalendarClient.set_time_zone")?;
        let zone: Tz = time_zone.parse().map_err(|_| {
            anyhow!("{} isn't a time zone; use an IANA name like 'America/New_York'", py::repr(time_zone))
        })?;
        self.patch_calendar(None, json!({"timeZone": zone.name()}))?;
        *self.time_zone.lock().unwrap() = Some(zone);
        Ok(zone)
    }

    pub fn list_event_resources(&self, time_min: &Moment, time_max: &Moment, private_property: Option<&str>) -> anyhow::Result<Vec<Value>> {
        let mut items = Vec::new();
        let mut page_token: Option<String> = None;
        loop {
            let mut query = vec![
                ("timeMin", py::isoformat(time_min)),
                ("timeMax", py::isoformat(time_max)),
                ("singleEvents", "true".to_string()),
                ("maxResults", LIST_PAGE_SIZE.to_string()),
            ];
            if let Some(token) = &page_token {
                query.push(("pageToken", token.clone()));
            }
            if let Some(property) = private_property {
                query.push(("privateExtendedProperty", property.to_string()));
            }
            let response = self.call("GET", &self.events_path(), &query, None)?;
            items.extend(response.get("items").and_then(Value::as_array).cloned().unwrap_or_default());
            page_token = response.get("nextPageToken").and_then(Value::as_str).map(str::to_string);
            if page_token.is_none() {
                return Ok(items);
            }
        }
    }

    /// Create the event `event_id` from `body`, or replace it in full if
    /// it's already there (a 409 on insert).
    pub fn replace_event_resource(&self, event_id: &str, body: &Map<String, Value>) -> anyhow::Result<Value> {
        writing("CalendarClient.replace_event_resource")?;
        let mut full = body.clone();
        full.insert("id".into(), json!(event_id));
        let full = Value::Object(full);
        match self.call("POST", &self.events_path(), &[], Some(&full)) {
            Ok(created) => return Ok(created),
            Err(error) if status_of(&error) == Some(409) => {}
            Err(error) => return Err(error),
        }
        self.call("PUT", &self.event_path(event_id), &[], Some(&full))
    }

    /// Delete `event_id` if it's there: one already gone is fine.
    pub fn delete_event_resource(&self, event_id: &str) -> anyhow::Result<()> {
        writing("CalendarClient.delete_event_resource")?;
        match self.call("DELETE", &self.event_path(event_id), &[], None) {
            Ok(_) => Ok(()),
            Err(error) if matches!(status_of(&error), Some(404) | Some(410)) => Ok(()),
            Err(error) => Err(error),
        }
    }

    pub fn list_events(&self, time_min: &Moment, time_max: &Moment) -> anyhow::Result<Vec<Event>> {
        if let Some(cached) = cached_listing(&self.calendar_id, time_min, time_max) {
            return Ok(cached);
        }
        let mut events = Vec::new();
        let mut page_token: Option<String> = None;
        loop {
            let mut query = vec![
                ("timeMin", py::isoformat(time_min)),
                ("timeMax", py::isoformat(time_max)),
                ("singleEvents", "true".to_string()),
                ("orderBy", "startTime".to_string()),
                ("maxResults", LIST_PAGE_SIZE.to_string()),
            ];
            if let Some(token) = &page_token {
                query.push(("pageToken", token.clone()));
            }
            let response = self.call("GET", &self.events_path(), &query, None)?;
            for item in response.get("items").and_then(Value::as_array).into_iter().flatten() {
                events.push(Event::from_api(item)?);
            }
            page_token = response.get("nextPageToken").and_then(Value::as_str).map(str::to_string);
            if page_token.is_none() {
                remember_listing(Listing {
                    calendar_id: self.calendar_id.clone(),
                    time_min: *time_min,
                    time_max: *time_max,
                    events: events.clone(),
                });
                return Ok(events);
            }
        }
    }

    pub fn get_event(&self, event_id: &str) -> anyhow::Result<Event> {
        Event::from_api(&self.call("GET", &self.event_path(event_id), &[], None)?)
    }

    pub fn get_event_resource(&self, event_id: &str) -> anyhow::Result<Value> {
        self.call("GET", &self.event_path(event_id), &[], None)
    }

    fn label_version(body: &Map<String, Value>) -> Vec<(&'static str, String)> {
        if body.contains_key("eventLabelId") {
            vec![("eventLabelVersion", "1".to_string())]
        } else {
            vec![]
        }
    }

    /// Create `event`, with its own id if it has one (a retried create
    /// then fails with a 409 rather than making a second).
    pub fn create_event(&self, event: &Event) -> anyhow::Result<Event> {
        writing("CalendarClient.create_event")?;
        let mut body = event.insert_body()?;
        if let Some(id) = &event.id {
            body.insert("id".into(), json!(id));
        }
        let query = Self::label_version(&body);
        Event::from_api(&self.call("POST", &self.events_path(), &query, Some(&Value::Object(body)))?)
    }

    /// Import `event` with the iCalendar UID `ical_uid` (events.import),
    /// which Calendar gives an id of its own. Importing a UID again
    /// updates the event it made the first time rather than making
    /// another.
    ///
    /// A UID of the form "<series id>_R<time, UTC>@google.com" -- as in
    /// "abc123_R20261019T223000@google.com" -- makes Calendar split the
    /// series itself, leaving what its "this and following" edit leaves
    /// (undocumented, so check the result): the series
    /// `<series id>` (or its latest part, itself split that way, from
    /// before that time) is ended at the local midnight before that
    /// time's day, and the event made is the series' rest, with the id
    /// "<series id>_R<time>" -- events after it that were edited on their
    /// own move to it. Found with the series_splits probe; see
    /// stores::recurrences.
    pub fn import_event(&self, event: &Event, ical_uid: &str) -> anyhow::Result<Event> {
        writing("CalendarClient.import_event")?;
        let mut body = event.insert_body()?;
        body.insert("iCalUID".into(), json!(ical_uid));
        Event::from_api(&self.call("POST", &format!("{}/import", self.events_path()), &[], Some(&Value::Object(body)))?)
    }

    /// Patch the event `event.id` with `event`'s fields that are set;
    /// those left unset are kept, and extended properties not in the
    /// body are kept too (Google merges extendedProperties.private) --
    /// except those named in `event.cleared`, which are removed.
    ///
    /// `event.id` may be a recurring series' master: the patch then
    /// reaches every instance, including ones edited on their own
    /// (exceptions), whose fields other than their times are all reset
    /// to the master's -- even fields the patch doesn't set, such as an
    /// instance's own priority, action_ids or description. A patch with
    /// `start`/`end` resets the exceptions' times too, moving every
    /// instance onto the series' times. Found with
    /// the series_edits probe.
    pub fn update_event(&self, event: &Event) -> anyhow::Result<Event> {
        writing("CalendarClient.update_event")?;
        let id = event.id.as_deref().ok_or_else(|| anyhow!("event.id is required to update an event"))?;
        let mut body = event.to_api_body()?;
        let private_has_null = body
            .get("extendedProperties")
            .and_then(|e| e.get("private"))
            .and_then(Value::as_object)
            .is_some_and(|p| !p.is_empty() && p.values().any(Value::is_null));
        if private_has_null {
            // A null removes a property -- but Calendar refuses one an
            // instance doesn't have, and facts null every chunk they don't
            // use. Removing what isn't there is nothing, so drop those.
            let current = self.call("GET", &self.event_path(id), &[], None)?;
            let empty = Map::new();
            let current_private = current
                .get("extendedProperties")
                .and_then(|e| e.get("private"))
                .and_then(Value::as_object)
                .unwrap_or(&empty);
            let private = body["extendedProperties"]["private"].as_object().unwrap().clone();
            let kept: Map<String, Value> =
                private.into_iter().filter(|(k, v)| !v.is_null() || current_private.contains_key(k)).collect();
            if kept.is_empty() {
                body.shift_remove("extendedProperties");
            } else {
                body.insert("extendedProperties".into(), json!({ "private": kept }));
            }
        }
        let query = Self::label_version(&body);
        Event::from_api(&self.call("PATCH", &self.event_path(id), &query, Some(&Value::Object(body)))?)
    }

    /// The instances of series `event_id` starting before `time_max`,
    /// cancelled ones included.
    pub fn list_instances(&self, event_id: &str, time_max: &Moment) -> anyhow::Result<Vec<Event>> {
        let mut events = Vec::new();
        let mut page_token: Option<String> = None;
        loop {
            let mut query = vec![
                ("timeMax", py::isoformat(time_max)),
                ("showDeleted", "true".to_string()),
                ("maxResults", LIST_PAGE_SIZE.to_string()),
            ];
            if let Some(token) = &page_token {
                query.push(("pageToken", token.clone()));
            }
            let response = self.call("GET", &format!("{}/instances", self.event_path(event_id)), &query, None)?;
            for item in response.get("items").and_then(Value::as_array).into_iter().flatten() {
                events.push(Event::from_api(item)?);
            }
            page_token = response.get("nextPageToken").and_then(Value::as_str).map(str::to_string);
            if page_token.is_none() {
                return Ok(events);
            }
        }
    }

    pub fn delete_event(&self, event_id: &str) -> anyhow::Result<()> {
        writing("CalendarClient.delete_event")?;
        self.call("DELETE", &self.event_path(event_id), &[], None)?;
        Ok(())
    }

    /// Every custom label on this calendar, and the etag to write them back
    /// against.
    pub fn list_event_labels(&self) -> anyhow::Result<(Vec<EventLabel>, Option<String>)> {
        let (etag, labels) = self.raw_event_labels()?;
        Ok((labels.iter().map(EventLabel::from_api).collect::<anyhow::Result<_>>()?, etag))
    }

    pub fn create_event_label(&self, background_color: &str, name: Option<&str>) -> anyhow::Result<EventLabel> {
        writing("CalendarClient.create_event_label")?;
        let (etag, mut labels) = self.raw_event_labels()?;
        let existing: Vec<String> = labels.iter().filter_map(|l| l["id"].as_str().map(str::to_string)).collect();
        labels.push(EventLabel { id: None, background_color: background_color.into(), name: name.map(str::to_string) }.to_api_body());
        let updated = self.patch_event_labels(etag.as_deref(), labels)?;
        updated
            .into_iter()
            .find(|l| l.id.as_ref().is_some_and(|i| !existing.contains(i)))
            .ok_or_else(|| anyhow!("the new label wasn't returned"))
    }

    pub fn update_event_label(&self, label_id: &str, background_color: Option<&str>, name: Option<&str>) -> anyhow::Result<EventLabel> {
        writing("CalendarClient.update_event_label")?;
        let (etag, mut labels) = self.raw_event_labels()?;
        let index = labels
            .iter()
            .position(|l| l["id"].as_str() == Some(label_id))
            .ok_or_else(|| anyhow!("event label {} not found", py::repr(label_id)))?;
        let current = EventLabel::from_api(&labels[index])?;
        labels[index] = EventLabel {
            id: Some(label_id.into()),
            background_color: background_color.map(str::to_string).unwrap_or(current.background_color),
            name: name.map(str::to_string).or(current.name),
        }
        .to_api_body();
        let updated = self.patch_event_labels(etag.as_deref(), labels)?;
        updated.into_iter().find(|l| l.id.as_deref() == Some(label_id)).ok_or_else(|| anyhow!("label missing"))
    }

    pub fn delete_event_label(&self, label_id: &str) -> anyhow::Result<EventLabel> {
        writing("CalendarClient.delete_event_label")?;
        let (etag, labels) = self.raw_event_labels()?;
        let removed = labels
            .iter()
            .find(|l| l["id"].as_str() == Some(label_id))
            .ok_or_else(|| anyhow!("event label {} not found", py::repr(label_id)))?;
        let removed = EventLabel::from_api(removed)?;
        let remaining: Vec<Value> = labels.into_iter().filter(|l| l["id"].as_str() != Some(label_id)).collect();
        self.patch_event_labels(etag.as_deref(), remaining)?;
        Ok(removed)
    }

    /// Replace the calendar's whole set of labels, guarded by `etag`.
    pub fn replace_event_labels(&self, labels: &[EventLabel], etag: Option<&str>) -> anyhow::Result<Vec<EventLabel>> {
        writing("CalendarClient.replace_event_labels")?;
        self.patch_event_labels(etag, labels.iter().map(EventLabel::to_api_body).collect())
    }

    pub fn get_calendar_metadata(&self, key: &str) -> anyhow::Result<Option<String>> {
        let calendar = self.get_calendar()?;
        Ok(parse_calendar_metadata(calendar.get("description").and_then(Value::as_str))
            .into_iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v))
    }

    pub fn set_calendar_metadata(&self, key: &str, value: Option<&str>) -> anyhow::Result<()> {
        writing("CalendarClient.set_calendar_metadata")?;
        let calendar = self.get_calendar()?;
        let description = with_calendar_metadata(calendar.get("description").and_then(Value::as_str), key, value);
        self.patch_calendar(calendar.get("etag").and_then(Value::as_str), json!({"description": description}))?;
        Ok(())
    }

    fn raw_event_labels(&self) -> anyhow::Result<(Option<String>, Vec<Value>)> {
        let calendar = self.get_calendar()?;
        let etag = calendar.get("etag").and_then(Value::as_str).map(str::to_string);
        let labels = calendar.pointer("/labelProperties/eventLabels").and_then(Value::as_array).cloned().unwrap_or_default();
        Ok((etag, labels))
    }

    fn patch_event_labels(&self, etag: Option<&str>, labels: Vec<Value>) -> anyhow::Result<Vec<EventLabel>> {
        let response = self.patch_calendar(etag, json!({"labelProperties": {"eventLabels": labels}}))?;
        response
            .pointer("/labelProperties/eventLabels")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .map(EventLabel::from_api)
            .collect()
    }

    fn patch_calendar(&self, etag: Option<&str>, body: Value) -> anyhow::Result<Value> {
        writing("CalendarClient._patch_calendar")?;
        let url = self.google.calendar_url(&self.calendar_path());
        match self.google.request("PATCH", &url, &[], Some(&body), etag) {
            Err(error) if status_of(&error) == Some(412) => Err(EventLabelConflictError.into()),
            other => other,
        }
    }
}

pub fn parse_calendar_metadata(description: Option<&str>) -> Vec<(String, String)> {
    match description {
        None | Some("") => Vec::new(),
        Some(text) => METADATA_MARKER.captures_iter(text).map(|c| (c[1].to_string(), c[2].to_string())).collect(),
    }
}

/// `description` with `key`'s marker line set to `value` (or removed).
pub fn with_calendar_metadata(description: Option<&str>, key: &str, value: Option<&str>) -> String {
    let mut kept: Vec<String> = Vec::new();
    for line in python_splitlines(description.unwrap_or("")) {
        if let Some(caps) = METADATA_MARKER.captures(&line) {
            if caps.get(0).map(|m| m.as_str().len()) == Some(line.len()) && &caps[1] == key {
                continue;
            }
        }
        kept.push(line);
    }
    if let Some(value) = value {
        kept.push(format!("[cascading-time-tracker:{key}={value}]"));
    }
    kept.join("\n").trim_matches('\n').to_string()
}

/// `str.splitlines()`: splits on \n, \r\n, \r (and the other Unicode line
/// breaks), with no trailing empty line.
pub fn python_splitlines(text: &str) -> Vec<String> {
    let mut lines = Vec::new();
    let mut current = String::new();
    let mut chars = text.chars().peekable();
    while let Some(c) = chars.next() {
        match c {
            '\r' => {
                if chars.peek() == Some(&'\n') {
                    chars.next();
                }
                lines.push(std::mem::take(&mut current));
            }
            '\n' | '\u{0b}' | '\u{0c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}' => {
                lines.push(std::mem::take(&mut current));
            }
            c => current.push(c),
        }
    }
    if !current.is_empty() {
        lines.push(current);
    }
    lines
}

pub fn not_found(error: &anyhow::Error) -> bool {
    matches!(status_of(error), Some(404) | Some(410))
}

/// Raise `TimeZoneNotSetError`-like errors as they are; for callers that
/// need to tell one from others.
pub fn is_time_zone_not_set(error: &anyhow::Error) -> bool {
    error.downcast_ref::<TimeZoneNotSetError>().is_some()
}

pub fn is_label_conflict(error: &anyhow::Error) -> bool {
    error.downcast_ref::<EventLabelConflictError>().is_some()
}

#[allow(dead_code)]
fn _unused() -> anyhow::Result<()> {
    bail!("unused")
}

#[cfg(test)]
#[path = "calendar_tests.rs"]
mod tests;
