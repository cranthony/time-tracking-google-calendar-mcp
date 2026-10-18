//! Google Calendar, behind a thin client (CalendarClient):
//! list/get/create/update/delete events, labels guarded by the calendar's
//! etag, and this app's metadata in the calendar's description. Events are
//! listed and got from what the client keeps of the calendar, synced with
//! Calendar's incremental sync (see `synced`), not asked of Calendar each
//! time.

use super::expand::{self, instance_base, When};
use super::synced;
use super::{segment, status_of, BatchRequest, Google};
use crate::event::{Event, Moment};
use crate::py;
use crate::write_lock;
use anyhow::{anyhow, bail};
use chrono_tz::Tz;
use regex::Regex;
use serde_json::{json, Map, Value};
use std::sync::{Arc, LazyLock, Mutex};

const LIST_PAGE_SIZE: usize = 2500;

/// A write under way: as it ends, it moves what's kept on again (see
/// `synced`).
pub struct Writing {
    client: CalendarClient,
}

impl Drop for Writing {
    fn drop(&mut self) {
        self.client.store(|store| store.generation += 1);
    }
}

/// What a sync brought: the events changed, where the next starts, and
/// the calendar's time zone.
struct Changes {
    items: Vec<Value>,
    token: String,
    zone: Option<Tz>,
}

/// The most requests Calendar takes in one batch.
pub const BATCH_SIZE: usize = 50;

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

/// Fail loudly when a write is attempted without the write lock.
fn writing(what: &str) -> anyhow::Result<()> {
    write_lock::require(what)
}

/// The event a request's path names: its last segment, after "/events/".
fn event_of(path: &str) -> Option<&str> {
    let (_, rest) = path.split_once("/events/")?;
    Some(rest.split('/').next().unwrap_or(rest)).filter(|id| !id.is_empty() && *id != "import")
}

/// Whether `error` is Calendar asking for requests to slow down.
fn rate_limited(error: &anyhow::Error) -> bool {
    match error.downcast_ref::<super::GoogleError>() {
        Some(e) => e.status == 429 || (e.status == 403 && e.message.to_lowercase().contains("rate limit")),
        None => false,
    }
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
        // Every event's times are written at the calendar's offset.
        synced::forget(&self.root(), &self.calendar_id);
        let zone: Tz = time_zone.parse().map_err(|_| {
            anyhow!("{} isn't a time zone; use an IANA name like 'America/New_York'", py::repr(time_zone))
        })?;
        self.patch_calendar(None, json!({"timeZone": zone.name()}))?;
        *self.time_zone.lock().unwrap() = Some(zone);
        Ok(zone)
    }

    pub fn list_event_resources(&self, time_min: &Moment, time_max: &Moment, private_property: Option<&str>) -> anyhow::Result<Vec<Value>> {
        self.listed(time_min, time_max, private_property)
    }

    /// Create the event `event_id` from `body`, or replace it in full if
    /// it's already there (a 409 on insert).
    pub fn replace_event_resource(&self, event_id: &str, body: &Map<String, Value>) -> anyhow::Result<Value> {
        let _writing = self.writing_event("CalendarClient.replace_event_resource", Some(event_id))?;
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
        let _writing = self.writing_event("CalendarClient.delete_event_resource", Some(event_id))?;
        match self.call("DELETE", &self.event_path(event_id), &[], None) {
            Ok(_) => Ok(()),
            Err(error) if matches!(status_of(&error), Some(404) | Some(410)) => Ok(()),
            Err(error) => Err(error),
        }
    }

    pub fn list_events(&self, time_min: &Moment, time_max: &Moment) -> anyhow::Result<Vec<Event>> {
        self.listed(time_min, time_max, None)?.iter().map(Event::from_api).collect()
    }

    pub fn get_event(&self, event_id: &str) -> anyhow::Result<Event> {
        Event::from_api(&self.get_event_resource(event_id)?)
    }

    /// The event `event_id` as Calendar has it now: asked of Calendar.
    pub fn fresh_event(&self, event_id: &str) -> anyhow::Result<Event> {
        Event::from_api(&self.call("GET", &self.event_path(event_id), &[], None)?)
    }

    /// Event `event_id` as kept -- an instance of a series made from its
    /// master, as Google makes it -- or, one not kept (or cancelled), as
    /// Calendar answers for it.
    pub fn get_event_resource(&self, event_id: &str) -> anyhow::Result<Value> {
        self.ensure_synced()?;
        if let Some(event) = self.kept_event(event_id) {
            return Ok(event);
        }
        self.call("GET", &self.event_path(event_id), &[], None)
    }

    /// Bring what's kept up to date first, if it's stale, so getting each of
    /// `event_ids` comes from memory.
    pub fn prefetch_events(&self, _event_ids: &[String]) -> anyhow::Result<()> {
        self.ensure_synced()
    }

    // -- what's kept: see `synced` --

    fn root(&self) -> String {
        self.google.calendar_url("")
    }

    fn store<T>(&self, f: impl FnOnce(&mut synced::Store) -> T) -> T {
        synced::with(&self.root(), &self.calendar_id, f)
    }

    /// `writing`, noting the write to event `event_id` (or, `None`, to
    /// anything) in what's kept, so the next read syncs first; keep the
    /// guard until the write is made, so a sync under way meanwhile isn't
    /// taken as up to date.
    fn writing_event(&self, what: &str, event_id: Option<&str>) -> anyhow::Result<Writing> {
        writing(what)?;
        self.store(|store| store.writing(event_id));
        Ok(Writing { client: self.clone() })
    }

    /// The event a write is to, for its path or, for one made, its body.
    fn written_by(request: &BatchRequest) -> Option<String> {
        event_of(&request.path).map(str::to_string).or_else(|| request.body.as_ref()?.get("id")?.as_str().map(str::to_string))
    }

    /// Have the next read sync first.
    pub fn mark_stale(&self) {
        self.store(|store| store.stale = true);
    }

    /// Sync, if nothing's kept yet or a write has made it stale.
    pub fn ensure_synced(&self) -> anyhow::Result<()> {
        if self.store(|store| store.token.is_none() || store.stale) {
            self.sync()?;
        }
        Ok(())
    }

    /// Bring what's kept up to date with Calendar's incremental sync -- or a
    /// full listing, the first time or once Calendar says the sync token
    /// is too old (410). Keeps what changed that the server didn't write
    /// itself (`take_outside`).
    pub fn sync(&self) -> anyhow::Result<()> {
        let lock = synced::sync_lock(&self.root(), &self.calendar_id);
        let _syncing = lock.lock().unwrap_or_else(|e| e.into_inner());
        let (token, generation) = self.store(|store| (store.token.clone(), store.generation));
        let (changes, full) = match token {
            Some(token) => match self.changes(Some(&token)) {
                Ok(changes) => (changes, false),
                Err(error) if status_of(&error) == Some(410) => (self.changes(None)?, true),
                Err(error) => return Err(error),
            },
            None => (self.changes(None)?, true),
        };
        self.store(|store| {
            if full {
                store.events.clear();
            }
            for item in changes.items {
                if !full && !store.is_ours(&item) {
                    store.outside.push(item.clone());
                }
                if let Some(id) = item["id"].as_str() {
                    store.events.insert(id.to_string(), item);
                }
            }
            store.token = Some(changes.token);
            store.zone = changes.zone.or(store.zone);
            // Up to date only if no write began or ended while this ran.
            if store.generation == generation {
                store.stale = false;
                store.written.clear();
                store.written_all = false;
            }
        });
        Ok(())
    }

    /// The events a sync brought that the server didn't write itself --
    /// changed elsewhere -- since this was last asked.
    pub fn take_outside(&self) -> Vec<Value> {
        self.store(|store| std::mem::take(&mut store.outside))
    }

    /// About how many bytes the events kept take (see synced::size).
    pub fn kept_size(&self) -> usize {
        synced::size(&self.root())
    }

    /// What changed since `token` (everything, for `None`): every event,
    /// series master and exception, cancelled ones included, unexpanded.
    fn changes(&self, token: Option<&str>) -> anyhow::Result<Changes> {
        let mut items = Vec::new();
        let mut page_token: Option<String> = None;
        loop {
            let mut query = vec![("singleEvents", "false".to_string()), ("showDeleted", "true".to_string()), ("maxResults", LIST_PAGE_SIZE.to_string())];
            if let Some(token) = token {
                query.push(("syncToken", token.to_string()));
            }
            if let Some(page) = &page_token {
                query.push(("pageToken", page.clone()));
            }
            let response = self.call("GET", &self.events_path(), &query, None)?;
            items.extend(response.get("items").and_then(Value::as_array).cloned().unwrap_or_default());
            page_token = response.get("nextPageToken").and_then(Value::as_str).map(str::to_string);
            if page_token.is_none() {
                let token = response.get("nextSyncToken").and_then(Value::as_str).ok_or_else(|| anyhow!("Calendar gave no nextSyncToken"))?;
                let zone = response.get("timeZone").and_then(Value::as_str).and_then(|z| z.parse().ok());
                return Ok(Changes { items, token: token.to_string(), zone });
            }
        }
    }

    /// The events in [`time_min`, `time_max`) as a singleEvents=true listing
    /// gives them (cancelled ones left out), from what's kept; with
    /// `private_property` ("key=value"), only those with that private
    /// extended property.
    fn listed(&self, time_min: &Moment, time_max: &Moment, private_property: Option<&str>) -> anyhow::Result<Vec<Value>> {
        self.ensure_synced()?;
        let (events, zone) = self.store(|store| (store.events.values().cloned().collect::<Vec<Value>>(), store.zone));
        let (min, max) = (time_min.with_timezone(&chrono::Utc), time_max.with_timezone(&chrono::Utc));
        let mut listed = expand::expand(zone.unwrap_or(chrono_tz::UTC), &events, Some(min), Some(max), false, max);
        if let Some((key, value)) = private_property.map(|p| p.split_once('=').unwrap_or((p, ""))) {
            listed.retain(|e| e.pointer("/extendedProperties/private").and_then(|p| p.get(key)).and_then(Value::as_str) == Some(value));
        }
        Ok(listed)
    }

    /// Event `event_id` as kept: stored, or an instance made from its
    /// series' master. `None` for one not kept, or cancelled.
    fn kept_event(&self, event_id: &str) -> Option<Value> {
        self.store(|store| {
            let zone = store.zone.unwrap_or(chrono_tz::UTC);
            let live = |e: Value| (e["status"] != "cancelled").then_some(e);
            if let Some(event) = store.events.get(event_id) {
                let master = event["recurringEventId"].as_str().and_then(|m| store.events.get(m));
                return live(master.map_or_else(|| event.clone(), |m| expand::under(m, event)));
            }
            let (base, when) = event_id.rsplit_once('_')?;
            let until = chrono::NaiveDateTime::parse_from_str(when.trim_end_matches('Z'), "%Y%m%dT%H%M%S")
                .ok()
                .map(|t| chrono::TimeZone::from_utc_datetime(&chrono::Utc, &t) + chrono::Duration::seconds(1));
            let parts = store.events.values().filter(|m| expand::is_master(m) && m["id"].as_str().map(instance_base) == Some(base));
            for master in parts {
                let original: Option<When> = expand::occurrences(zone, master, until).into_iter().find(|o| o.suffix() == when);
                if let Some(original) = original {
                    return live(expand::instance(zone, master, original, None));
                }
            }
            None
        })
    }

    /// Send `writes` (made by `create_event_request` and
    /// `update_event_request`), in batches when there's more than one, and
    /// answer each as it alone would be answered. Calendar may apply a
    /// batch's writes in any order, so no two may be to the same event.
    /// Any Calendar asks to slow down are sent again, one at a time.
    pub fn send_writes(&self, writes: &[BatchRequest]) -> anyhow::Result<Vec<anyhow::Result<Value>>> {
        writing("CalendarClient.send_writes")?;
        let _writing: Vec<Writing> =
            writes.iter().map(|write| self.writing_event("CalendarClient.send_writes", Self::written_by(write).as_deref())).collect::<anyhow::Result<_>>()?;
        if writes.len() == 1 {
            return Ok(vec![self.send(&writes[0])]);
        }
        let mut answers = Vec::new();
        for chunk in writes.chunks(BATCH_SIZE) {
            answers.extend(self.google.calendar_batch(chunk)?);
        }
        for (write, answer) in writes.iter().zip(answers.iter_mut()) {
            if answer.as_ref().is_err_and(rate_limited) {
                *answer = self.send(write);
            }
        }
        Ok(answers)
    }

    fn send(&self, request: &BatchRequest) -> anyhow::Result<Value> {
        let url = self.google.calendar_url(&request.path);
        self.google.request(request.method, &url, &request.query, request.body.as_ref(), request.if_match.as_deref())
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
        let request = self.create_event_request(event)?;
        let _writing = self.writing_event("CalendarClient.create_event", Self::written_by(&request).as_deref())?;
        let created = self.send(&request)?;
        let _made = self.writing_event("CalendarClient.create_event", created["id"].as_str())?;
        Event::from_api(&created)
    }

    /// The request `create_event` sends, for `send_writes`.
    pub fn create_event_request(&self, event: &Event) -> anyhow::Result<BatchRequest> {
        let mut body = event.insert_body()?;
        if let Some(id) = &event.id {
            body.insert("id".into(), json!(id));
        }
        let query = Self::label_version(&body);
        Ok(BatchRequest { method: "POST", path: self.events_path(), query, body: Some(Value::Object(body)), if_match: None })
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
        // It may split the series the UID names, moving its exceptions.
        let _writing = self.writing_event("CalendarClient.import_event", Some(instance_base(ical_uid.trim_end_matches("@google.com"))))?;
        let mut body = event.insert_body()?;
        body.insert("iCalUID".into(), json!(ical_uid));
        let imported = self.call("POST", &format!("{}/import", self.events_path()), &[], Some(&Value::Object(body)))?;
        let _made = self.writing_event("CalendarClient.import_event", imported["id"].as_str())?;
        Event::from_api(&imported)
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
        // Built first: building it may read the event, perhaps from memory.
        let request = self.update_event_request(event)?;
        let _writing = self.writing_event("CalendarClient.update_event", Self::written_by(&request).as_deref())?;
        Event::from_api(&self.send(&request)?)
    }

    /// The request `update_event` sends, for `send_writes`.
    pub fn update_event_request(&self, event: &Event) -> anyhow::Result<BatchRequest> {
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
            let current = self.get_event_resource(id)?;
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
        Ok(BatchRequest { method: "PATCH", path: self.event_path(id), query, body: Some(Value::Object(body)), if_match: event.etag.0.clone() })
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

    /// The instances of series `event_id` from `time_min` on (ending after
    /// it), cancelled ones left out -- as far as Google expands the series.
    pub fn instances_from(&self, event_id: &str, time_min: &Moment) -> anyhow::Result<Vec<Event>> {
        let mut events = Vec::new();
        let mut page_token: Option<String> = None;
        loop {
            let mut query = vec![("timeMin", py::isoformat(time_min)), ("maxResults", LIST_PAGE_SIZE.to_string())];
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

    /// The recurring series with an instance from `time_min` on (and
    /// before `time_max`, if given): their masters, unexpanded. Google only
    /// counts the instances it expands, so a series past its last one
    /// (see Recurrences::renew) isn't listed.
    pub fn list_series(&self, time_min: &Moment, time_max: Option<&Moment>) -> anyhow::Result<Vec<Event>> {
        let mut series = Vec::new();
        let mut page_token: Option<String> = None;
        loop {
            let mut query = vec![("timeMin", py::isoformat(time_min)), ("singleEvents", "false".to_string()), ("maxResults", LIST_PAGE_SIZE.to_string())];
            if let Some(time_max) = time_max {
                query.push(("timeMax", py::isoformat(time_max)));
            }
            if let Some(token) = &page_token {
                query.push(("pageToken", token.clone()));
            }
            let response = self.call("GET", &self.events_path(), &query, None)?;
            for item in response.get("items").and_then(Value::as_array).into_iter().flatten() {
                if item.get("recurrence").and_then(Value::as_array).is_some_and(|r| !r.is_empty()) {
                    series.push(Event::from_api(item)?);
                }
            }
            page_token = response.get("nextPageToken").and_then(Value::as_str).map(str::to_string);
            if page_token.is_none() {
                return Ok(series);
            }
        }
    }

    pub fn delete_event(&self, event_id: &str) -> anyhow::Result<()> {
        let _writing = self.writing_event("CalendarClient.delete_event", Some(event_id))?;
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
        let _writing = self.writing_event("CalendarClient.create_event_label", None)?;
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
        let _writing = self.writing_event("CalendarClient.update_event_label", None)?;
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
        let _writing = self.writing_event("CalendarClient.delete_event_label", None)?;
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
        let _writing = self.writing_event("CalendarClient.replace_event_labels", None)?;
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
