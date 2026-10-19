//! An in-memory fake of the parts of Google Calendar v3 and Sheets v4 the
//! server uses, served over HTTP on a local port, for the unit tests:
//! conformance/fake_google.py, ported. The server's clients talk to it as
//! they talk to Google, through their own HTTP client.
//!
//! It's faithful where the server depends on Google's behaviour, as the
//! probes found it: recurring series expanded into instances, through
//! google::expand (which the local_expansion probes check against Google)
//! -- at most 730 of them, counted from the series' start (INSTANCE_CAP),
//! named after the base series for a series Google split -- instances
//! edited on their own as exceptions, series listed unexpanded
//! (singleEvents=false), what a patch to a series' master
//! does to them, a series ended early by UNTIL, series split by importing
//! "<id>_R<time>@google.com", extendedProperties.private merged by a patch
//! with null removing a key, calendar etags checked by If-Match, deleted
//! events answering 410, inserting an event with a label the calendar
//! hasn't got answering 400, and Sheets' grid that values.update grows but
//! batchUpdateByDataFilter doesn't. Every request is logged.

use crate::google::Google;
use chrono::{DateTime, Duration, NaiveDateTime, TimeZone, Utc};
use chrono_tz::Tz;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

const PAGE_CAP: usize = 7;
const HORIZON_DAYS: i64 = 800;

#[derive(Debug, Clone)]
pub struct LoggedRequest {
    pub method: String,
    pub path: String,
    pub query: Vec<(String, String)>,
    pub if_match: Option<String>,
    pub body: Value,
    /// The access token it was sent with.
    pub bearer: Option<String>,
    /// Whether it came inside a batch, rather than as a request of its own.
    pub batched: bool,
}

/// Which quota a request counts against: Sheets meters reads and writes
/// separately (60 a minute per user each); Calendar meters all queries.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Quota {
    SheetsRead,
    SheetsWrite,
    CalendarRead,
    CalendarWrite,
}

impl LoggedRequest {
    /// The quota this request counts against, if it's a Sheets or
    /// Calendar request (not a token or Secret Manager one).
    pub fn quota(&self) -> Option<Quota> {
        let reading = self.method == "GET";
        if self.path.starts_with("/v4/spreadsheets") {
            let searching = self.path.ends_with("values:batchGetByDataFilter") || self.path.ends_with("developerMetadata:search");
            Some(if reading || searching { Quota::SheetsRead } else { Quota::SheetsWrite })
        } else if self.path.starts_with("/calendar/v3/") {
            Some(if reading { Quota::CalendarRead } else { Quota::CalendarWrite })
        } else {
            None
        }
    }
}

struct GoogleError {
    status: u16,
    message: String,
}

fn err(status: u16, message: impl Into<String>) -> GoogleError {
    GoogleError { status, message: message.into() }
}

type Result<T> = std::result::Result<T, GoogleError>;

use crate::google::expand::{self, When};

fn zone_of(name: Option<&str>) -> Tz {
    expand::zone_of(name)
}

/// `expand::parse_moment`, of a time the fake has taken.
fn parse_moment(value: &Value, zone: Tz) -> When {
    expand::parse_moment(value, zone).expect("a time the fake took")
}

fn render_moment(when: When, zone: Tz, time_zone: Option<&str>) -> Value {
    expand::render_moment(when, zone, time_zone)
}

fn parse_rfc3339(text: &str) -> Result<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(&text.replace('Z', "+00:00"))
        .map(|m| m.with_timezone(&Utc))
        .map_err(|_| err(400, format!("Bad Request: time {text:?} has no offset")))
}

struct Calendar {
    resource: Map<String, Value>,
    events: BTreeMap<String, Value>,
    /// Insertion order of `events`, as Python's dict keeps it.
    order: Vec<String>,
    /// Each event changed, in order: a sync token is how many of these
    /// the listing it came with had seen.
    changes: Vec<String>,
    /// Events deleted, as a sync gives them: cancelled.
    gone: BTreeMap<String, Value>,
    /// While `Some`, how many of `changes` a sync can see: Calendar's
    /// feed lagging the writes it has answered (see `hold_feed`).
    feed_seen: Option<usize>,
}

impl Calendar {
    fn zone(&self) -> Tz {
        zone_of(self.resource.get("timeZone").and_then(Value::as_str))
    }

    fn put(&mut self, id: &str, event: Value) {
        if !self.events.contains_key(id) {
            self.order.push(id.to_string());
        }
        self.events.insert(id.to_string(), event);
        self.gone.remove(id);
        self.changes.push(id.to_string());
    }

    fn remove(&mut self, id: &str) {
        if let Some(mut event) = self.events.remove(id) {
            event["status"] = json!("cancelled");
            self.gone.insert(id.to_string(), event);
        }
        self.changes.push(id.to_string());
        self.order.retain(|o| o != id);
    }

    fn ordered(&self) -> Vec<Value> {
        self.order.iter().filter_map(|id| self.events.get(id).cloned()).collect()
    }
}

#[derive(Default)]
struct Tab {
    properties: Value,
    cells: BTreeMap<(usize, usize), String>,
    column_widths: BTreeMap<String, Value>,
}

struct Spreadsheet {
    properties: Value,
    sheets: Vec<Tab>,
    metadata: Vec<Value>,
}

#[derive(Default)]
pub struct State {
    calendars: BTreeMap<String, Calendar>,
    spreadsheets: BTreeMap<String, Spreadsheet>,
    pub requests: Vec<LoggedRequest>,
    /// Answer this many of the next Sheets requests with 429.
    pub rate_limit_next: usize,
    /// (method, a piece of the path, status, message): answer the next
    /// request that matches each with that error, once. A 404 to a write
    /// of an event deletes the event first, as if it was deleted
    /// meanwhile.
    pub fail_next: Vec<(String, String, u16, String)>,
    /// Import "<id>_R<time>" as a plain event, not splitting series <id>
    /// (it's undocumented, so it might stop).
    pub imports_dont_split: bool,
    /// Each version added to a Secret Manager secret: (secret, its data).
    pub secret_versions: Vec<(String, String)>,
    ids: u64,
}

fn new_tab(sheet_id: i64, title: &str) -> Tab {
    Tab {
        properties: json!({"sheetId": sheet_id, "title": title, "index": 0, "sheetType": "GRID", "gridProperties": {"rowCount": 1000, "columnCount": 26}}),
        ..Default::default()
    }
}

fn column_index(letters: &str) -> usize {
    letters.bytes().fold(0, |n, b| n * 26 + (b - b'A' + 1) as usize) - 1
}

fn cell_text(value: &Value) -> String {
    match value {
        Value::Bool(b) => if *b { "TRUE" } else { "FALSE" }.into(),
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        Value::Number(n) => {
            // str() of a Python number: 3, 3.5.
            if let Some(i) = n.as_i64() { i.to_string() } else { crate::py::python_number(n) }
        }
        other => other.to_string(),
    }
}

fn read(tab: &Tab, r0: usize, c0: usize, r1: Option<usize>, c1: Option<usize>) -> Vec<Vec<String>> {
    if tab.cells.is_empty() {
        return Vec::new();
    }
    let last_row = tab.cells.keys().map(|(r, _)| r).max().unwrap() + 1;
    let last_col = tab.cells.keys().map(|(_, c)| c).max().unwrap() + 1;
    let mut rows = Vec::new();
    for r in r0..r1.unwrap_or(last_row).min(last_row) {
        let mut row: Vec<String> = (c0..c1.unwrap_or(last_col).min(last_col)).map(|c| tab.cells.get(&(r, c)).cloned().unwrap_or_default()).collect();
        while row.last().is_some_and(String::is_empty) {
            row.pop();
        }
        rows.push(row);
    }
    while rows.last().is_some_and(Vec::is_empty) {
        rows.pop();
    }
    rows
}

fn write(tab: &mut Tab, r0: usize, c0: usize, rows: &[Value]) -> usize {
    let mut count = 0;
    for (i, row) in rows.iter().enumerate() {
        for (j, value) in row.as_array().into_iter().flatten().enumerate() {
            let text = cell_text(value);
            if text.is_empty() {
                tab.cells.remove(&(r0 + i, c0 + j));
            } else {
                tab.cells.insert((r0 + i, c0 + j), text);
            }
            count += 1;
        }
    }
    count
}

fn instance_id_parts(id: &str) -> Option<(&str, &str)> {
    let (master, when) = id.rsplit_once('_')?;
    let ok = (when.len() == 8 && when.chars().all(|c| c.is_ascii_digit()))
        || (when.len() == 16 && when.ends_with('Z') && when.as_bytes()[8] == b'T' && when[..8].chars().all(|c| c.is_ascii_digit()) && when[9..15].chars().all(|c| c.is_ascii_digit()));
    ok.then_some((master, when))
}

fn is_split_id(id: &str) -> bool {
    id.len() > 17 && {
        let tail = &id[id.len() - 17..];
        tail.starts_with("_R") && tail[2..10].chars().all(|c| c.is_ascii_digit()) && &tail[10..11] == "T" && tail[11..].chars().all(|c| c.is_ascii_digit())
    }
}

impl State {
    fn next_id(&mut self, prefix: &str) -> String {
        self.ids += 1;
        let digest = Sha256::digest(format!("{prefix}{}", self.ids).as_bytes());
        digest.iter().map(|b| format!("{b:02x}")).collect::<String>()[..26].to_string()
    }

    fn etag(&mut self) -> String {
        self.ids += 1;
        format!("\"{:016}\"", self.ids)
    }

    fn calendar(&mut self, id: &str) -> Result<&mut Calendar> {
        self.calendars.get_mut(id).ok_or_else(|| err(404, "Not Found"))
    }

    pub fn add_calendar(&mut self, summary: &str, time_zone: Option<&str>, description: Option<&str>) -> String {
        let id = format!("{}@group.calendar.google.com", self.next_id("cal"));
        let mut resource = Map::new();
        resource.insert("kind".into(), json!("calendar#calendar"));
        resource.insert("id".into(), json!(id));
        resource.insert("summary".into(), json!(summary));
        resource.insert("etag".into(), json!(self.etag()));
        if let Some(z) = time_zone {
            resource.insert("timeZone".into(), json!(z));
        }
        if let Some(d) = description {
            resource.insert("description".into(), json!(d));
        }
        self.calendars.insert(id.clone(), Calendar { resource, events: BTreeMap::new(), order: Vec::new(), changes: Vec::new(), gone: BTreeMap::new(), feed_seen: None });
        id
    }

    /// Change `fields` of event `event_id` behind the server's back, as
    /// someone in Google Calendar would: a new etag, and a change a sync
    /// brings.
    pub fn edit_event_by_hand(&mut self, calendar_id: &str, event_id: &str, fields: Value) {
        let etag = self.etag();
        let cal = self.calendars.get_mut(calendar_id).expect("a calendar");
        let mut event = cal.events[event_id].clone();
        for (k, v) in fields.as_object().expect("fields") {
            event[k] = v.clone();
        }
        event["etag"] = json!(etag);
        cal.put(event_id, event);
    }

    /// Hold Calendar's feed where it is (`true`), so a sync doesn't bring
    /// changes made from now on, writes the fake has answered included --
    /// as Calendar's feed can lag -- or let it catch up (`false`).
    pub fn hold_feed(&mut self, calendar_id: &str, held: bool) {
        let cal = self.calendars.get_mut(calendar_id).expect("a calendar");
        cal.feed_seen = held.then_some(cal.changes.len());
    }

    pub fn add_event(&mut self, calendar_id: &str, body: Value) -> String {
        let id = body.get("id").and_then(Value::as_str).map(str::to_string).unwrap_or_else(|| self.next_id("seed"));
        let event = self.new_event(calendar_id, body, &id).ok().expect("a valid event");
        self.calendars.get_mut(calendar_id).unwrap().put(&id, event);
        id
    }

    fn new_event(&mut self, calendar_id: &str, body: Value, id: &str) -> Result<Value> {
        let etag = self.etag();
        let zone = self.calendar(calendar_id)?.zone();
        let mut event = body;
        event["id"] = json!(id);
        if event.get("status").is_none() {
            event["status"] = json!("confirmed");
        }
        if event.get("iCalUID").is_none() {
            event["iCalUID"] = json!(format!("{id}@google.com"));
        }
        event["kind"] = json!("calendar#event");
        event["etag"] = json!(etag);
        if let Some(private) = event.pointer("/extendedProperties/private").and_then(Value::as_object).cloned() {
            let kept: Map<String, Value> = private.into_iter().filter(|(_, v)| !v.is_null()).collect();
            event["extendedProperties"]["private"] = Value::Object(kept);
        }
        check_event(&event, zone)?;
        Ok(event)
    }

    // -- series --

    fn occurrences(cal: &Calendar, master: &Value, until: Option<DateTime<Utc>>) -> Vec<When> {
        expand::occurrences(cal.zone(), master, until)
    }

    fn instance(cal: &Calendar, master: &Value, original: When) -> Value {
        let id = expand::instance_id(master["id"].as_str().unwrap_or_default(), original);
        expand::instance(cal.zone(), master, original, cal.events.get(&id))
    }

    fn exceptions(cal: &Calendar, master_id: &str) -> Vec<Value> {
        cal.ordered().into_iter().filter(|e| e["recurringEventId"] == json!(master_id)).collect()
    }

    fn expanded(cal: &Calendar, time_min: Option<DateTime<Utc>>, time_max: Option<DateTime<Utc>>, show_deleted: bool) -> Vec<Value> {
        let horizon = time_max.unwrap_or_else(Utc::now) + Duration::days(HORIZON_DAYS);
        expand::expand(cal.zone(), &cal.ordered(), time_min, time_max, show_deleted, horizon)
    }

    /// A singleEvents=false listing: events, and series (not their
    /// instances) with an instance in the range, and their instances edited
    /// on their own.
    fn unexpanded(cal: &Calendar, time_min: Option<DateTime<Utc>>, time_max: Option<DateTime<Utc>>, show_deleted: bool) -> Vec<Value> {
        let mut found = Vec::new();
        let mut series: Vec<String> = Vec::new();
        for item in Self::expanded(cal, time_min, time_max, show_deleted) {
            let Some(master_id) = item["recurringEventId"].as_str().map(str::to_string) else {
                found.push(item);
                continue;
            };
            if !series.contains(&master_id) {
                if let Some(master) = cal.events.get(&master_id) {
                    found.push(master.clone());
                }
                series.push(master_id);
            }
            if item["id"].as_str().is_some_and(|id| cal.events.contains_key(id)) {
                found.push(item);
            }
        }
        found
    }

    /// (event, its master if it's an instance) for `event_id`.
    fn find(cal: &Calendar, event_id: &str) -> Result<(Value, Option<Value>)> {
        if let Some(event) = cal.events.get(event_id) {
            let master = event["recurringEventId"].as_str().and_then(|m| cal.events.get(m)).cloned();
            return Ok((master.as_ref().map(|m| under(m, event)).unwrap_or_else(|| event.clone()), master));
        }
        if let Some((base, when)) = instance_id_parts(event_id) {
            let until = when.contains('T').then(|| {
                Utc.from_utc_datetime(&NaiveDateTime::parse_from_str(&when[..15], "%Y%m%dT%H%M%S").unwrap()) + Duration::seconds(1)
            });
            let parts = cal.ordered().into_iter().filter(|m| expand::is_master(m) && m["id"].as_str().map(expand::instance_base) == Some(base));
            for master in parts {
                for original in Self::occurrences(cal, &master, until) {
                    if original.suffix() == when {
                        return Ok((Self::instance(cal, &master, original), Some(master.clone())));
                    }
                }
            }
        }
        Err(err(404, "Not Found"))
    }

    fn present(cal: &Calendar, event: &Value) -> Value {
        let mut event = event.clone();
        let zone = cal.zone();
        for key in ["start", "end", "originalStartTime"] {
            if let Some(value) = event.get(key).cloned() {
                event[key] = render_moment(parse_moment(&value, zone), zone, value.get("timeZone").and_then(Value::as_str));
            }
        }
        // When it last changed, as Calendar gives it: a millisecond on for
        // each etag made, as etags count up.
        if let Some(n) = event["etag"].as_str().and_then(|e| e.trim_matches('"').parse::<i64>().ok()) {
            let at = Utc.with_ymd_and_hms(2026, 1, 1, 0, 0, 0).unwrap() + chrono::Duration::milliseconds(n);
            event["updated"] = json!(at.to_rfc3339_opts(chrono::SecondsFormat::Millis, true));
        }
        event
    }

    fn page(cal: &Calendar, items: &[Value], max_results: Option<&str>, token: Option<&str>) -> Value {
        let offset: usize = token.and_then(|t| t.parse().ok()).unwrap_or(0);
        let size = max_results.and_then(|m| m.parse::<usize>().ok()).unwrap_or(250).min(PAGE_CAP);
        let page: Vec<Value> = items.iter().skip(offset).take(size).map(|e| Self::present(cal, e)).collect();
        let mut out = json!({"kind": "calendar#events", "items": page});
        if offset + size < items.len() {
            out["nextPageToken"] = json!((offset + size).to_string());
        }
        out
    }

    // -- requests --

    /// An authenticated API request: logged, failed if a test asked for
    /// that, else answered.
    #[allow(clippy::too_many_arguments)]
    fn dispatch(&mut self, method: &str, path: &str, query: &[(String, String)], if_match: Option<&str>, body: Value, bearer: Option<String>, batched: bool) -> (u16, Option<Value>) {
        self.requests.push(LoggedRequest { method: method.into(), path: decode(path), query: query.to_vec(), if_match: if_match.map(str::to_string), body: body.clone(), bearer, batched });
        let failing = self.fail_next.iter().position(|(m, p, _, _)| m == method && decode(path).contains(p.as_str()));
        if let Some(index) = failing {
            let (_, _, code, message) = self.fail_next.remove(index);
            if code == 404 && method != "GET" && path.contains("/events/") {
                // Not found: deleted meanwhile, so gone from the calendar
                // too.
                let _ = self.handle("DELETE", path, &[], None, json!({}));
            }
            return (code, Some(error_body(code, &message)));
        }
        match self.handle(method, path, query, if_match, if body.is_null() { json!({}) } else { body }) {
            Ok(Some(value)) => (200, Some(value)),
            Ok(None) => (204, None),
            Err(e) => (e.status, Some(error_body(e.status, &e.message))),
        }
    }

    fn handle(&mut self, method: &str, path: &str, query: &[(String, String)], if_match: Option<&str>, body: Value) -> Result<Option<Value>> {
        let one = |name: &str| query.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str());
        let segments: Vec<String> = path.trim_start_matches('/').split('/').map(decode).collect();
        let s: Vec<&str> = segments.iter().map(String::as_str).collect();
        match (method, s.as_slice()) {
            ("POST", ["v1", "projects", project, "secrets", added]) if added.ends_with(":addVersion") => {
                use base64::Engine;
                let secret = format!("projects/{project}/secrets/{}", added.trim_end_matches(":addVersion"));
                let data = body["payload"]["data"].as_str().unwrap_or_default();
                let decoded = base64::engine::general_purpose::STANDARD.decode(data).map_err(|_| err(400, "payload.data isn't base64"))?;
                self.secret_versions.push((secret.clone(), String::from_utf8_lossy(&decoded).into_owned()));
                let number = self.secret_versions.iter().filter(|(s, _)| *s == secret).count();
                Ok(Some(json!({"name": format!("{secret}/versions/{number}"), "state": "ENABLED"})))
            }
            ("POST", ["calendar", "v3", "calendars"]) => {
                let id = self.add_calendar(
                    body["summary"].as_str().unwrap_or(""),
                    body["timeZone"].as_str(),
                    body["description"].as_str(),
                );
                Ok(Some(Value::Object(self.calendars[&id].resource.clone())))
            }
            ("GET", ["calendar", "v3", "calendars", cid]) => Ok(Some(Value::Object(self.calendar(cid)?.resource.clone()))),
            ("DELETE", ["calendar", "v3", "calendars", cid]) => {
                self.calendar(cid)?;
                self.calendars.remove(*cid);
                Ok(None)
            }
            ("PATCH", ["calendar", "v3", "calendars", cid]) => {
                let new_label_ids: Vec<String> = (0..body["labelProperties"]["eventLabels"].as_array().map(|a| a.len()).unwrap_or(0)).map(|_| self.next_id("label")).collect();
                let etag = self.etag();
                let cal = self.calendar(cid)?;
                if if_match.is_some_and(|m| Some(m) != cal.resource["etag"].as_str()) {
                    return Err(err(412, "Precondition Failed"));
                }
                for (key, value) in body.as_object().cloned().unwrap_or_default() {
                    if ["id", "etag", "kind"].contains(&key.as_str()) {
                        continue;
                    }
                    if key == "labelProperties" {
                        let labels: Vec<Value> = value["eventLabels"]
                            .as_array()
                            .cloned()
                            .unwrap_or_default()
                            .into_iter()
                            .zip(new_label_ids.iter())
                            .map(|(mut l, id)| {
                                if l.get("id").is_none() {
                                    l["id"] = json!(id);
                                }
                                l
                            })
                            .collect();
                        cal.resource.insert("labelProperties".into(), json!({"eventLabels": labels}));
                    } else if value.is_null() {
                        cal.resource.remove(&key);
                    } else {
                        cal.resource.insert(key, value);
                    }
                }
                cal.resource.insert("etag".into(), json!(etag));
                Ok(Some(Value::Object(cal.resource.clone())))
            }
            ("PATCH", ["calendar", "v3", "users", "me", "calendarList", cid]) => Ok(Some(json!({"id": cid, "hidden": true}))),
            ("GET", ["calendar", "v3", "calendars", cid, "events"]) => {
                let cal = self.calendar(cid)?;
                let time_min = one("timeMin").map(parse_rfc3339).transpose()?;
                let time_max = one("timeMax").map(parse_rfc3339).transpose()?;
                let show_deleted = one("showDeleted") == Some("true");
                let mut items = if let Some(token) = one("syncToken") {
                    // What changed since the token's listing: each event as
                    // it is now, a deleted one cancelled.
                    let seen: usize = token.parse().map_err(|_| err(400, "Invalid sync token value."))?;
                    if seen > cal.changes.len() {
                        return Err(err(410, "Sync token is no longer valid, a full sync is required."));
                    }
                    let mut ids: Vec<&String> = Vec::new();
                    for id in &cal.changes[seen..cal.feed_seen.unwrap_or(cal.changes.len()).max(seen)] {
                        if !ids.contains(&id) {
                            ids.push(id);
                        }
                    }
                    ids.into_iter().filter_map(|id| cal.events.get(id).or_else(|| cal.gone.get(id)).cloned()).collect()
                } else if one("singleEvents") == Some("true") {
                    Self::expanded(cal, time_min, time_max, show_deleted)
                } else if time_min.is_none() && time_max.is_none() {
                    // Every event, series and exception, as a full sync has them.
                    cal.ordered().into_iter().filter(|e| show_deleted || e["status"] != "cancelled").collect()
                } else {
                    Self::unexpanded(cal, time_min, time_max, show_deleted)
                };
                for (k, v) in query.iter().filter(|(k, _)| k == "privateExtendedProperty") {
                    let _ = k;
                    let (key, value) = v.split_once('=').unwrap_or((v, ""));
                    items.retain(|e| e.pointer("/extendedProperties/private").and_then(|p| p.get(key)) == Some(&json!(value)));
                }
                if let Some(uid) = one("iCalUID") {
                    items = cal.ordered().into_iter().filter(|e| e["iCalUID"] == json!(uid)).collect();
                }
                let mut out = Self::page(cal, &items, one("maxResults"), one("pageToken"));
                out["timeZone"] = cal.resource.get("timeZone").cloned().unwrap_or(json!("UTC"));
                if out.get("nextPageToken").is_none() {
                    let seen = one("syncToken").and_then(|t| t.parse::<usize>().ok()).unwrap_or(0);
                    out["nextSyncToken"] = json!(cal.feed_seen.unwrap_or(cal.changes.len()).max(seen).to_string());
                }
                Ok(Some(out))
            }
            ("POST", ["calendar", "v3", "calendars", cid, "events"]) => {
                let event_id = match body.get("id").and_then(Value::as_str) {
                    Some(id) => id.to_string(),
                    None => self.next_id("event"),
                };
                if !(event_id.len() >= 5 && event_id.chars().all(|c| c.is_ascii_digit() || ('a'..='v').contains(&c))) {
                    return Err(err(400, "Invalid resource id value."));
                }
                if self.calendar(cid)?.events.contains_key(&event_id) {
                    return Err(err(409, "The requested identifier already exists."));
                }
                // Inserting refuses a label the calendar doesn't have (an
                // update keeps one, as the label_lifecycle probe found).
                if let Some(label) = body.get("eventLabelId").and_then(Value::as_str) {
                    let labels = self.calendar(cid)?.resource.get("labelProperties").and_then(|p| p["eventLabels"].as_array()).cloned().unwrap_or_default();
                    if !labels.iter().any(|l| l["id"] == json!(label)) {
                        return Err(err(400, format!("Invalid event label id: {label}. An event label with the specified id does not exist.")));
                    }
                }
                let event = self.new_event(cid, body, &event_id)?;
                let cal = self.calendar(cid)?;
                cal.put(&event_id, event.clone());
                Ok(Some(Self::present(cal, &event)))
            }
            ("POST", ["calendar", "v3", "calendars", cid, "events", "import"]) => self.import_event(cid, body).map(Some),
            ("GET", ["calendar", "v3", "calendars", cid, "events", eid, "instances"]) => {
                let cal = self.calendar(cid)?;
                let master = cal.events.get(*eid).cloned().filter(|m| m.get("recurrence").is_some_and(|r| !r.is_null())).ok_or_else(|| err(404, "Not Found"))?;
                let time_min = one("timeMin").map(parse_rfc3339).transpose()?;
                let time_max = one("timeMax").map(parse_rfc3339).transpose()?;
                let show_deleted = one("showDeleted") == Some("true");
                let zone = cal.zone();
                let mut items = Vec::new();
                for original in Self::occurrences(cal, &master, Some(time_max.unwrap_or_else(|| Utc::now() + Duration::days(HORIZON_DAYS)))) {
                    let instance = Self::instance(cal, &master, original);
                    if instance["status"] == "cancelled" && !show_deleted {
                        continue;
                    }
                    if time_max.is_some_and(|m| parse_moment(&instance["start"], zone).key(zone) >= m) {
                        continue;
                    }
                    if time_min.is_some_and(|m| parse_moment(&instance["end"], zone).key(zone) <= m) {
                        continue;
                    }
                    items.push(instance);
                }
                Ok(Some(Self::page(cal, &items, one("maxResults"), one("pageToken"))))
            }
            ("GET", ["calendar", "v3", "calendars", cid, "events", eid]) => {
                let cal = self.calendar(cid)?;
                let (event, _) = Self::find(cal, eid)?;
                Ok(Some(Self::present(cal, &event)))
            }
            ("PUT", ["calendar", "v3", "calendars", cid, "events", eid]) => self.update_event(cid, eid, body).map(Some),
            ("PATCH", ["calendar", "v3", "calendars", cid, "events", eid]) => {
                if let Some(wanted) = if_match {
                    let (event, _) = Self::find(self.calendar(cid)?, eid)?;
                    if event["etag"].as_str() != Some(wanted) {
                        return Err(err(412, "Precondition Failed"));
                    }
                }
                self.patch_event(cid, eid, body).map(Some)
            }
            ("DELETE", ["calendar", "v3", "calendars", cid, "events", eid]) => {
                let etag = self.etag();
                let cal = self.calendar(cid)?;
                let (event, _) = Self::find(cal, eid)?;
                if event["status"] == "cancelled" {
                    return Err(err(410, "Resource has been deleted"));
                }
                if is_split_id(eid) && event.get("recurrence").is_some_and(|r| !r.is_null()) {
                    let first = Self::occurrences(cal, &event, None);
                    if first.first().is_some_and(|f| Self::instance(cal, &event, *f)["status"] == "cancelled") {
                        return Err(err(400, "Bad Request"));
                    }
                }
                let mut cancelled = event;
                cancelled["status"] = json!("cancelled");
                cancelled["etag"] = json!(etag);
                cal.put(eid, cancelled);
                Ok(None)
            }
            // -- sheets --
            ("POST", ["v4", "spreadsheets"]) => {
                self.sheets_request()?;
                let id = self.next_id("sheet") + &"x".repeat(18);
                let title = body.pointer("/properties/title").cloned().unwrap_or(json!("Untitled spreadsheet"));
                self.spreadsheets.insert(id.clone(), Spreadsheet { properties: json!({"title": title}), sheets: vec![new_tab(0, "Sheet1")], metadata: Vec::new() });
                Ok(Some(json!({"spreadsheetId": id})))
            }
            ("GET", ["v4", "spreadsheets", sid]) if !sid.contains(':') => {
                let sheet = self.spreadsheet(sid)?;
                Ok(Some(json!({"sheets": sheet.sheets.iter().map(|t| json!({"properties": t.properties})).collect::<Vec<_>>()})))
            }
            ("POST", ["v4", "spreadsheets", sid]) if sid.ends_with(":batchUpdate") => self.batch_update(sid.trim_end_matches(":batchUpdate"), &body).map(Some),
            ("POST", ["v4", "spreadsheets", sid, "developerMetadata:search"]) => {
                let sheet = self.spreadsheet(sid)?;
                let mut matched = Vec::new();
                for filter in body["dataFilters"].as_array().cloned().unwrap_or_default() {
                    let lookup = filter["developerMetadataLookup"].as_object().cloned().unwrap_or_default();
                    for m in &sheet.metadata {
                        if lookup.iter().all(|(k, v)| m.get(k) == Some(v)) {
                            matched.push(json!({"developerMetadata": m, "dataFilters": [filter.clone()]}));
                        }
                    }
                }
                Ok(Some(if matched.is_empty() { json!({}) } else { json!({"matchedDeveloperMetadata": matched}) }))
            }
            ("POST", ["v4", "spreadsheets", sid, "values:batchGetByDataFilter"]) => {
                let sheet = self.spreadsheet(sid)?;
                let mut ranges = Vec::new();
                for filter in body["dataFilters"].as_array().cloned().unwrap_or_default() {
                    let grid = &filter["gridRange"];
                    let tab = tab_by_id(sheet, grid["sheetId"].as_i64().unwrap_or(0))?;
                    let idx = |k: &str| grid.get(k).and_then(Value::as_u64).map(|v| v as usize);
                    let values = read(tab, idx("startRowIndex").unwrap_or(0), idx("startColumnIndex").unwrap_or(0), idx("endRowIndex"), idx("endColumnIndex"));
                    let mut range = json!({"range": tab.properties["title"], "majorDimension": "ROWS"});
                    if !values.is_empty() {
                        range["values"] = json!(values);
                    }
                    ranges.push(json!({"valueRange": range, "dataFilters": [filter.clone()]}));
                }
                Ok(Some(json!({"spreadsheetId": sid, "valueRanges": ranges})))
            }
            ("POST", ["v4", "spreadsheets", sid, "values:batchUpdateByDataFilter"]) => {
                let sheet = self.spreadsheet(sid)?;
                let mut total = 0;
                for data in body["data"].as_array().cloned().unwrap_or_default() {
                    let grid = &data["dataFilter"]["gridRange"];
                    let tab = tab_by_id_mut(sheet, grid["sheetId"].as_i64().unwrap_or(0))?;
                    let rows = data["values"].as_array().cloned().unwrap_or_default();
                    let r0 = grid.get("startRowIndex").and_then(Value::as_u64).unwrap_or(0) as usize;
                    let row_count = tab.properties["gridProperties"]["rowCount"].as_u64().unwrap() as usize;
                    let end = grid.get("endRowIndex").and_then(Value::as_u64).map(|e| e as usize).unwrap_or(row_count).min(row_count);
                    if r0 + rows.len() > end {
                        return Err(err(
                            400,
                            format!("Requested writing within range [{}], but tried writing to row [{}]", tab.properties["title"].as_str().unwrap(), r0 + rows.len()),
                        ));
                    }
                    let c0 = grid.get("startColumnIndex").and_then(Value::as_u64).unwrap_or(0) as usize;
                    total += write(tab, r0, c0, &rows);
                }
                Ok(Some(json!({"spreadsheetId": sid, "totalUpdatedCells": total})))
            }
            ("GET", ["v4", "spreadsheets", sid, "values", rng]) => {
                let sheet = self.spreadsheet(sid)?;
                let (tab, r0, c0, r1, c1) = a1(sheet, rng)?;
                let values = read(&sheet.sheets[tab], r0, c0, r1, c1);
                let mut out = json!({"range": rng, "majorDimension": "ROWS"});
                if !values.is_empty() {
                    out["values"] = json!(values);
                }
                Ok(Some(out))
            }
            ("PUT", ["v4", "spreadsheets", sid, "values", rng]) => {
                let sheet = self.spreadsheet(sid)?;
                let (tab, r0, c0, _, _) = a1(sheet, rng)?;
                let rows = body["values"].as_array().cloned().unwrap_or_default();
                let tab = &mut sheet.sheets[tab];
                let count = tab.properties["gridProperties"]["rowCount"].as_u64().unwrap() as usize;
                tab.properties["gridProperties"]["rowCount"] = json!(count.max(r0 + rows.len()));
                let cells = write(tab, r0, c0, &rows);
                Ok(Some(json!({"spreadsheetId": sid, "updatedRange": rng, "updatedRows": rows.len(), "updatedCells": cells})))
            }
            _ => Err(err(404, format!("fake: no route for {method} {path}"))),
        }
    }

    fn sheets_request(&mut self) -> Result<()> {
        if self.rate_limit_next > 0 {
            self.rate_limit_next -= 1;
            return Err(err(429, "Quota exceeded for quota metric 'Read requests'."));
        }
        Ok(())
    }

    fn spreadsheet(&mut self, id: &str) -> Result<&mut Spreadsheet> {
        self.sheets_request()?;
        self.spreadsheets.get_mut(id).ok_or_else(|| err(404, "Requested entity was not found."))
    }

    fn batch_update(&mut self, sid: &str, body: &Value) -> Result<Value> {
        let requests = body["requests"].as_array().cloned().unwrap_or_default();
        let mut replies = Vec::new();
        for request in requests {
            let (kind, args) = request.as_object().unwrap().iter().next().map(|(k, v)| (k.clone(), v.clone())).unwrap();
            match kind.as_str() {
                "addSheet" => {
                    self.ids += 1;
                    let properties = &args["properties"];
                    let sheet_id = properties["sheetId"].as_i64().unwrap_or(1_000_000 + self.ids as i64);
                    let sheet = self.spreadsheet(sid)?;
                    if sheet.sheets.iter().any(|t| t.properties["title"] == properties["title"]) {
                        return Err(err(400, format!("A sheet with the name \"{}\" already exists.", properties["title"].as_str().unwrap_or(""))));
                    }
                    let default_title = format!("Sheet{}", sheet.sheets.len() + 1);
                    let mut tab = new_tab(sheet_id, properties["title"].as_str().unwrap_or(&default_title));
                    if let Some(color) = properties.get("tabColor") {
                        tab.properties["tabColor"] = color.clone();
                    }
                    replies.push(json!({"addSheet": {"properties": tab.properties}}));
                    sheet.sheets.push(tab);
                }
                "updateSheetProperties" => {
                    let sheet = self.spreadsheet(sid)?;
                    let tab = tab_by_id_mut(sheet, args["properties"]["sheetId"].as_i64().unwrap())?;
                    for name in args["fields"].as_str().unwrap().split(',') {
                        tab.properties[name] = args["properties"][name].clone();
                    }
                    replies.push(json!({}));
                }
                "createDeveloperMetadata" => {
                    self.ids += 1;
                    let id = self.ids;
                    let sheet = self.spreadsheet(sid)?;
                    let mut metadata = args["developerMetadata"].clone();
                    metadata["metadataId"] = json!(id);
                    tab_by_id(sheet, metadata["location"]["sheetId"].as_i64().unwrap_or(-1))?;
                    sheet.metadata.push(metadata.clone());
                    replies.push(json!({"createDeveloperMetadata": {"developerMetadata": metadata}}));
                }
                "updateDimensionProperties" => {
                    let sheet = self.spreadsheet(sid)?;
                    let range = &args["range"];
                    let tab = tab_by_id_mut(sheet, range["sheetId"].as_i64().unwrap())?;
                    for column in range["startIndex"].as_u64().unwrap()..range["endIndex"].as_u64().unwrap() {
                        tab.column_widths.insert(column.to_string(), args["properties"]["pixelSize"].clone());
                    }
                    replies.push(json!({}));
                }
                "deleteDimension" => {
                    let sheet = self.spreadsheet(sid)?;
                    let range = &args["range"];
                    let tab = tab_by_id_mut(sheet, range["sheetId"].as_i64().unwrap())?;
                    if range["dimension"] != "ROWS" {
                        return Err(err(400, "fake: only ROWS"));
                    }
                    let (start, end) = (range["startIndex"].as_u64().unwrap() as usize, range["endIndex"].as_u64().unwrap() as usize);
                    let count = end - start;
                    tab.cells = std::mem::take(&mut tab.cells)
                        .into_iter()
                        .filter(|((r, _), _)| !(start..end).contains(r))
                        .map(|((r, c), v)| ((if r >= end { r - count } else { r }, c), v))
                        .collect();
                    let rows = tab.properties["gridProperties"]["rowCount"].as_u64().unwrap() as usize;
                    tab.properties["gridProperties"]["rowCount"] = json!(rows - count);
                    replies.push(json!({}));
                }
                "appendDimension" => {
                    let sheet = self.spreadsheet(sid)?;
                    let tab = tab_by_id_mut(sheet, args["sheetId"].as_i64().unwrap())?;
                    let rows = tab.properties["gridProperties"]["rowCount"].as_u64().unwrap();
                    tab.properties["gridProperties"]["rowCount"] = json!(rows + args["length"].as_u64().unwrap());
                    replies.push(json!({}));
                }
                "updateSpreadsheetProperties" => {
                    let sheet = self.spreadsheet(sid)?;
                    for name in args["fields"].as_str().unwrap().split(',') {
                        sheet.properties[name] = args["properties"][name].clone();
                    }
                    replies.push(json!({}));
                }
                other => return Err(err(400, format!("fake: unsupported request {other}"))),
            }
        }
        let sheet = self.spreadsheet(sid)?;
        let mut out = json!({"spreadsheetId": sid, "replies": replies});
        if body["includeSpreadsheetInResponse"] == true {
            out["updatedSpreadsheet"] = json!({"sheets": sheet.sheets.iter().map(|t| json!({"properties": t.properties})).collect::<Vec<_>>()});
        }
        Ok(out)
    }

    fn import_event(&mut self, cid: &str, body: Value) -> Result<Value> {
        let uid = body["iCalUID"].as_str().map(str::to_string).ok_or_else(|| err(400, "Missing iCalUID."))?;
        let rest_body = {
            let mut b = body.clone();
            b.as_object_mut().unwrap().remove("iCalUID");
            b
        };
        let split = uid.strip_suffix("@google.com").and_then(|u| {
            let (base, at) = u.rsplit_once("_R")?;
            (at.len() == 15 && at.as_bytes()[8] == b'T').then(|| (base.to_string(), at.to_string()))
        });
        let existing = {
            let cal = self.calendar(cid)?;
            cal.ordered().into_iter().find(|e| e["iCalUID"] == json!(uid) && e.get("recurringEventId").is_none_or(|r| r.is_null()))
        };
        if let Some(existing) = &existing {
            if split.is_none() || existing["status"] != "cancelled" {
                if split.is_none() {
                    return self.update_event(cid, existing["id"].as_str().unwrap(), rest_body);
                }
                return Ok(Self::present(self.calendar(cid)?, existing));
            }
        }
        let stem = split.as_ref().filter(|_| self.imports_dont_split).map(|_| uid.trim_end_matches("@google.com").to_string());
        let split = split.filter(|_| !self.imports_dont_split);
        let Some((base_id, at_text)) = split else {
            let id = match stem {
                Some(stem) => stem,
                None => self.next_id("import"),
            };
            let event = self.new_event(cid, body, &id)?;
            let cal = self.calendar(cid)?;
            cal.put(&id, event.clone());
            return Ok(Self::present(cal, &event));
        };
        let at = Utc.from_utc_datetime(&NaiveDateTime::parse_from_str(&at_text, "%Y%m%dT%H%M%S").unwrap());
        let etag = self.etag();
        let new_id = format!("{base_id}_R{at_text}");
        let mut rest = self.new_event(cid, rest_body, &new_id)?;
        rest["iCalUID"] = json!(uid);
        let cal = self.calendar(cid)?;
        let zone = cal.zone();
        let parts_of: Vec<Value> = cal
            .ordered()
            .into_iter()
            .filter(|e| e.get("recurringEventId").is_none_or(|r| r.is_null()))
            .filter(|e| e["id"].as_str().is_some_and(|i| i == base_id || i.starts_with(&format!("{base_id}_R"))))
            .collect();
        if parts_of.is_empty() {
            return Err(err(404, "Not Found"));
        }
        let earlier: Vec<&Value> = parts_of.iter().filter(|p| parse_moment(&p["start"], zone).key(zone) < at).collect();
        let current = earlier.iter().copied().max_by_key(|p| parse_moment(&p["start"], zone).key(zone)).ok_or_else(|| err(400, "Bad Request"))?.clone();
        let local_zone = current["start"].get("timeZone").and_then(Value::as_str).map(|z| zone_of(Some(z))).unwrap_or(zone);
        let day_before = at.with_timezone(&local_zone).date_naive() - Duration::days(1);
        let until = local_zone.from_local_datetime(&day_before.and_hms_opt(23, 59, 59).unwrap()).earliest().unwrap().with_timezone(&Utc);
        let current_id = current["id"].as_str().unwrap().to_string();
        let moved: Vec<Value> = Self::exceptions(cal, &current_id)
            .into_iter()
            .filter(|e| parse_moment(&e["originalStartTime"], zone).key(zone) >= at)
            .collect();
        let lines: Vec<Value> = current["recurrence"].as_array().cloned().unwrap_or_default();
        let new_lines: Vec<Value> = lines
            .iter()
            .map(|l| {
                let text = l.as_str().unwrap_or("");
                if let Some(rule) = text.strip_prefix("RRULE:") {
                    let mut kept: Vec<String> = rule.split(';').filter(|p| !p.starts_with("COUNT=") && !p.starts_with("UNTIL=") && !p.is_empty()).map(str::to_string).collect();
                    kept.push(format!("UNTIL={}", until.format("%Y%m%dT%H%M%SZ")));
                    json!(format!("RRULE:{}", kept.join(";")))
                } else {
                    l.clone()
                }
            })
            .collect();
        let mut updated = current.clone();
        updated["recurrence"] = json!(new_lines);
        updated["etag"] = json!(etag);
        cal.put(&current_id, updated);
        cal.put(&new_id, rest.clone());
        for mut exception in moved {
            cal.remove(exception["id"].as_str().unwrap());
            let original = parse_moment(&exception["originalStartTime"], zone);
            let id = expand::instance_id(&new_id, original);
            exception["id"] = json!(id);
            exception["recurringEventId"] = json!(new_id);
            cal.put(&id, exception);
        }
        Ok(Self::present(cal, &rest))
    }

    fn update_event(&mut self, cid: &str, eid: &str, body: Value) -> Result<Value> {
        let (current, _) = Self::find(self.calendar(cid)?, eid)?;
        let mut replacement = body;
        for kept in ["recurringEventId", "originalStartTime", "iCalUID"] {
            if current.get(kept).is_some() && replacement.get(kept).is_none() {
                replacement[kept] = current[kept].clone();
            }
        }
        if current.get("recurrence").is_some_and(|r| !r.is_null()) || replacement.get("recurrence").is_some() {
            if let Some(result) = self.check_rule_change(cid, &current, replacement.get("recurrence"))? {
                return Ok(result);
            }
        }
        let event = self.new_event(cid, replacement, eid)?;
        self.apply_master_change(cid, &current, &event, None)?;
        let cal = self.calendar(cid)?;
        cal.put(eid, event.clone());
        Ok(Self::present(cal, &event))
    }

    fn patch_event(&mut self, cid: &str, eid: &str, body: Value) -> Result<Value> {
        let (current, master) = Self::find(self.calendar(cid)?, eid)?;
        let mut patched = current.clone();
        for (key, value) in body.as_object().cloned().unwrap_or_default() {
            if ["id", "etag", "kind"].contains(&key.as_str()) {
                continue;
            }
            if key == "extendedProperties" {
                for (scope, props) in value.as_object().cloned().unwrap_or_default() {
                    if patched.get("extendedProperties").is_none() {
                        patched["extendedProperties"] = json!({});
                    }
                    if patched["extendedProperties"].get(&scope).is_none() {
                        patched["extendedProperties"][&scope] = json!({});
                    }
                    let existing = patched["extendedProperties"][&scope].as_object_mut().unwrap();
                    for (prop, prop_value) in props.as_object().cloned().unwrap_or_default() {
                        if prop_value.is_null() {
                            if !existing.contains_key(&prop) && master.is_some() {
                                return Err(err(400, "Required"));
                            }
                            existing.shift_remove(&prop);
                        } else {
                            existing.insert(prop, prop_value);
                        }
                    }
                }
            } else if value.is_null() {
                patched.as_object_mut().unwrap().shift_remove(&key);
            } else {
                patched[&key] = value;
            }
        }
        if let Some(rules) = body.get("recurrence") {
            if let Some(result) = self.check_rule_change(cid, &current, Some(rules))? {
                return Ok(result);
            }
        }
        let zone = self.calendar(cid)?.zone();
        check_event(&patched, zone)?;
        patched["etag"] = json!(self.etag());
        let times_changed = body.get("start").is_some() || body.get("end").is_some();
        self.apply_master_change(cid, &current, &patched, Some(times_changed))?;
        let cal = self.calendar(cid)?;
        cal.put(eid, patched.clone());
        Ok(Self::present(cal, &patched))
    }

    /// An "_R" series Google split itself can't have its rules changed: it's
    /// deleted instead (the answer is what's left of it), or the change
    /// fails if its first event is cancelled.
    fn check_rule_change(&mut self, cid: &str, current: &Value, rules: Option<&Value>) -> Result<Option<Value>> {
        let id = current["id"].as_str().unwrap_or("").to_string();
        if !is_split_id(&id) || rules == current.get("recurrence") {
            return Ok(None);
        }
        let etag = self.etag();
        let cal = self.calendar(cid)?;
        let first = Self::occurrences(cal, current, None);
        if first.first().is_some_and(|f| Self::instance(cal, current, *f)["status"] == "cancelled") {
            return Err(err(400, "Bad Request"));
        }
        let mut event = cal.events[&id].clone();
        event["status"] = json!("cancelled");
        event["recurrence"] = json!(["RRULE:FREQ=DAILY;COUNT=1"]);
        event["etag"] = json!(etag);
        for exception in Self::exceptions(cal, &id) {
            cal.remove(exception["id"].as_str().unwrap());
        }
        cal.put(&id, event.clone());
        Ok(Some(Self::present(cal, &event)))
    }

    fn apply_master_change(&mut self, cid: &str, before: &Value, after: &Value, times_changed: Option<bool>) -> Result<()> {
        if before.get("recurrence").is_none_or(|r| r.is_null() || r.as_array().is_some_and(|a| a.is_empty())) || before.get("recurringEventId").is_some_and(|r| !r.is_null()) {
            return Ok(());
        }
        let cal = self.calendar(cid)?;
        let zone = cal.zone();
        let master_id = before["id"].as_str().unwrap().to_string();
        let mut exceptions = Self::exceptions(cal, &master_id);
        let keys: Vec<String> = before.as_object().unwrap().keys().chain(after.as_object().unwrap().keys()).cloned().collect();
        let changed: Vec<&String> = keys.iter().filter(|k| *k != "etag" && before.get(k.as_str()) != after.get(k.as_str())).collect();
        if changed.iter().all(|k| *k == "status") {
            return Ok(());
        }
        let times_changed = times_changed.unwrap_or_else(|| before.get("start") != after.get("start") || before.get("end") != after.get("end"));
        if times_changed {
            let old = Self::occurrences(cal, before, None);
            let new = Self::occurrences(cal, after, None);
            for exception in exceptions {
                cal.remove(exception["id"].as_str().unwrap());
                if exception["status"] != "cancelled" {
                    continue;
                }
                let original = parse_moment(&exception["originalStartTime"], zone);
                if let Some(index) = old.iter().position(|o| *o == original) {
                    if index < new.len() {
                        let mut moved = Self::instance(cal, after, new[index]);
                        moved["status"] = json!("cancelled");
                        let id = moved["id"].as_str().unwrap().to_string();
                        cal.put(&id, moved);
                    }
                }
            }
            return Ok(());
        }
        if before.get("recurrence") != after.get("recurrence") {
            let kept: Vec<String> = Self::occurrences(cal, after, None).iter().map(When::suffix).collect();
            for exception in &exceptions {
                let original = parse_moment(&exception["originalStartTime"], zone);
                if !kept.contains(&original.suffix()) {
                    cal.remove(exception["id"].as_str().unwrap());
                }
            }
            exceptions = Self::exceptions(cal, &master_id);
        }
        for exception in exceptions {
            let mut reset = Map::new();
            for (k, v) in after.as_object().unwrap() {
                if !["recurrence", "id", "start", "end", "etag", "status"].contains(&k.as_str()) {
                    reset.insert(k.clone(), v.clone());
                }
            }
            for k in ["id", "start", "end", "recurringEventId", "originalStartTime", "status"] {
                if let Some(v) = exception.get(k) {
                    reset.insert(k.into(), v.clone());
                }
            }
            let id = exception["id"].as_str().unwrap().to_string();
            cal.put(&id, Value::Object(reset));
        }
        Ok(())
    }
}

fn under(master: &Value, exception: &Value) -> Value {
    expand::under(master, exception)
}

fn check_event(event: &Value, zone: Tz) -> Result<()> {
    if event.get("start").is_none() || event.get("end").is_none() {
        return Err(err(400, "Missing end time."));
    }
    if parse_moment(&event["end"], zone).key(zone) < parse_moment(&event["start"], zone).key(zone) {
        return Err(err(400, "The specified time range is empty."));
    }
    if event.get("colorId").is_some() {
        return Err(err(400, "fake: the server must never write colorId (it drops event labels)"));
    }
    Ok(())
}

fn tab_by_id(sheet: &Spreadsheet, id: i64) -> Result<&Tab> {
    sheet.sheets.iter().find(|t| t.properties["sheetId"].as_i64() == Some(id)).ok_or_else(|| err(400, format!("No grid with id: {id}")))
}

fn tab_by_id_mut(sheet: &mut Spreadsheet, id: i64) -> Result<&mut Tab> {
    sheet.sheets.iter_mut().find(|t| t.properties["sheetId"].as_i64() == Some(id)).ok_or_else(|| err(400, format!("No grid with id: {id}")))
}

/// (tab index, first row, first column, end row, end column) of an A1 range.
fn a1(sheet: &Spreadsheet, range: &str) -> Result<(usize, usize, usize, Option<usize>, Option<usize>)> {
    let (title, cells) = match range.rsplit_once('!') {
        Some((t, c)) => (t.to_string(), c.to_string()),
        None => (String::new(), range.to_string()),
    };
    let title = if title.starts_with('\'') && title.ends_with('\'') && title.len() >= 2 { title[1..title.len() - 1].replace("''", "'") } else { title };
    let tab = if title.is_empty() {
        0
    } else {
        sheet.sheets.iter().position(|t| t.properties["title"] == json!(title)).ok_or_else(|| err(400, format!("Unable to parse range: {title}")))?
    };
    let re = regex::Regex::new(r"^([A-Z]*)(\d*)(?::([A-Z]*)(\d*))?$").unwrap();
    let c = re.captures(&cells).ok_or_else(|| err(400, format!("Unable to parse range: {range}")))?;
    let (c1, r1) = (c.get(1).unwrap().as_str(), c.get(2).unwrap().as_str());
    let start_col = if c1.is_empty() { 0 } else { column_index(c1) };
    let start_row = if r1.is_empty() { 0 } else { r1.parse::<usize>().unwrap() - 1 };
    let (end_col, end_row) = if c.get(3).is_none() && c.get(4).is_none() {
        ((!c1.is_empty()).then_some(start_col + 1), (!r1.is_empty()).then_some(start_row + 1))
    } else {
        let (c2, r2) = (c.get(3).map(|m| m.as_str()).unwrap_or(""), c.get(4).map(|m| m.as_str()).unwrap_or(""));
        ((!c2.is_empty()).then(|| column_index(c2) + 1), (!r2.is_empty()).then(|| r2.parse().unwrap()))
    };
    Ok((tab, start_row, start_col, end_row, end_col))
}

fn decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            if let Ok(b) = u8::from_str_radix(std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or(""), 16) {
                out.push(b);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// The fake, serving on a local port for as long as it lives.
#[derive(Clone)]
pub struct FakeGoogle {
    pub state: Arc<Mutex<State>>,
    pub root: String,
}

impl FakeGoogle {
    pub fn start() -> FakeGoogle {
        let state = Arc::new(Mutex::new(State::default()));
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let serving = state.clone();
        std::thread::spawn(move || serve(listener, serving));
        FakeGoogle { state, root: format!("http://127.0.0.1:{port}/") }
    }

    /// A client of the fake, with a token that needs no refreshing.
    pub fn google(&self) -> Arc<Google> {
        let token = json!({"token": "fake", "refresh_token": "fake-refresh", "expiry": "2099-01-01T00:00:00Z"}).to_string();
        let path = std::env::temp_dir().join(format!("time-tracker-fake-token-{}.json", uuid::Uuid::new_v4()));
        Arc::new(Google::from_token_json_at(&token, &path, Some(self.root.clone())).unwrap())
    }

    pub fn with<T>(&self, f: impl FnOnce(&mut State) -> T) -> T {
        f(&mut self.state.lock().unwrap())
    }

    pub fn add_calendar(&self, time_zone: Option<&str>) -> String {
        self.with(|s| s.add_calendar("Time Tracking", time_zone, None))
    }

    /// Add an event behind the server's back (forgetting what it keeps of
    /// the calendar, as a restart would: see `forget_synced`).
    pub fn add_event(&self, calendar_id: &str, body: Value) -> String {
        self.forget_synced(calendar_id);
        self.with(|s| s.add_event(calendar_id, body))
    }

    /// Forget what the server keeps of `calendar_id`, so it syncs afresh:
    /// a test that changes the calendar behind the server's back, as
    /// setup, means the server to see it as changed.
    pub fn forget_synced(&self, calendar_id: &str) {
        crate::google::synced::forget(&format!("{}calendar/v3/", self.root), calendar_id);
    }

    pub fn requests(&self) -> Vec<LoggedRequest> {
        self.with(|s| s.requests.clone())
    }

    /// Each version added to a Secret Manager secret: (secret, its data).
    pub fn secret_versions(&self) -> Vec<(String, String)> {
        self.with(|s| s.secret_versions.clone())
    }

    /// The requests logged so far against each quota: [Sheets reads,
    /// Sheets writes, Calendar reads, Calendar writes].
    pub fn quota_counts(&self) -> [usize; 4] {
        let made: Vec<Option<Quota>> = self.requests().iter().map(LoggedRequest::quota).collect();
        [Quota::SheetsRead, Quota::SheetsWrite, Quota::CalendarRead, Quota::CalendarWrite].map(|quota| made.iter().filter(|q| **q == Some(quota)).count())
    }

    /// The HTTP requests made to Calendar so far: a batch is one.
    pub fn calendar_round_trips(&self) -> usize {
        self.requests().iter().filter(|r| (r.path.starts_with("/calendar/v3/") && !r.batched) || r.path == "/batch/calendar/v3").count()
    }

    pub fn clear_requests(&self) {
        self.with(|s| s.requests.clear())
    }

    /// A stored event, raw.
    pub fn event(&self, calendar_id: &str, event_id: &str) -> Option<Value> {
        self.with(|s| s.calendars.get(calendar_id).and_then(|c| c.events.get(event_id).cloned()))
    }

    pub fn calendar_resource(&self, calendar_id: &str) -> Value {
        self.with(|s| Value::Object(s.calendars[calendar_id].resource.clone()))
    }

    /// A tab's rows (by title), as Sheets would return them.
    pub fn rows(&self, spreadsheet_id: &str, title: &str) -> Vec<Vec<String>> {
        self.with(|s| {
            let sheet = &s.spreadsheets[spreadsheet_id];
            let tab = sheet.sheets.iter().find(|t| t.properties["title"] == json!(title)).expect("a tab with that title");
            read(tab, 0, 0, None, None)
        })
    }

    pub fn spreadsheet_title(&self, spreadsheet_id: &str) -> String {
        self.with(|s| s.spreadsheets[spreadsheet_id].properties["title"].as_str().unwrap_or("").to_string())
    }

    pub fn tab_titles(&self, spreadsheet_id: &str) -> Vec<String> {
        self.with(|s| s.spreadsheets[spreadsheet_id].sheets.iter().map(|t| t.properties["title"].as_str().unwrap_or("").to_string()).collect())
    }

    /// Each tab's sheetId, by title.
    pub fn tab_ids(&self, spreadsheet_id: &str) -> Vec<(i64, String)> {
        self.with(|s| {
            s.spreadsheets[spreadsheet_id]
                .sheets
                .iter()
                .map(|t| (t.properties["sheetId"].as_i64().unwrap_or(0), t.properties["title"].as_str().unwrap_or("").to_string()))
                .collect()
        })
    }

    /// A tab's grid size, in rows.
    pub fn row_count(&self, spreadsheet_id: &str, title: &str) -> u64 {
        self.with(|s| {
            let tab = s.spreadsheets[spreadsheet_id].sheets.iter().find(|t| t.properties["title"] == json!(title)).unwrap();
            tab.properties["gridProperties"]["rowCount"].as_u64().unwrap()
        })
    }

    pub fn set_row_count(&self, spreadsheet_id: &str, title: &str, rows: u64) {
        self.with(|s| {
            let sheet = s.spreadsheets.get_mut(spreadsheet_id).unwrap();
            let tab = sheet.sheets.iter_mut().find(|t| t.properties["title"] == json!(title)).unwrap();
            tab.properties["gridProperties"]["rowCount"] = json!(rows);
        })
    }

    /// Forget what the server keeps of `spreadsheet_id`, as a restart
    /// would: a test that edits it behind the server's back, as setup,
    /// means the server to read it as edited.
    pub fn forget_kept(&self, spreadsheet_id: &str) {
        crate::google::kept::forget_spreadsheet(&self.root, spreadsheet_id);
    }

    /// Replace everything below a tab's header row with `rows` (and forget
    /// what's kept of the spreadsheet: see `forget_kept`).
    pub fn replace_rows(&self, spreadsheet_id: &str, title: &str, rows: Vec<Vec<String>>) {
        self.forget_kept(spreadsheet_id);
        self.with(|s| {
            let sheet = s.spreadsheets.get_mut(spreadsheet_id).unwrap();
            let tab = sheet.sheets.iter_mut().find(|t| t.properties["title"] == json!(title)).unwrap();
            tab.cells.retain(|(r, _), _| *r == 0);
            for (r, row) in rows.into_iter().enumerate() {
                for (c, text) in row.into_iter().enumerate() {
                    if !text.is_empty() {
                        tab.cells.insert((r + 1, c), text);
                    }
                }
            }
        })
    }

    /// Set one cell by hand while the server runs: what it keeps isn't
    /// told (unlike `set_cell`).
    pub fn edit_by_hand(&self, spreadsheet_id: &str, title: &str, row: usize, column: usize, text: &str) {
        self.with(|s| {
            let sheet = s.spreadsheets.get_mut(spreadsheet_id).unwrap();
            let tab = sheet.sheets.iter_mut().find(|t| t.properties["title"] == json!(title)).unwrap();
            if text.is_empty() {
                tab.cells.remove(&(row, column));
            } else {
                tab.cells.insert((row, column), text.to_string());
            }
        })
    }

    /// Delete a tab, and its tags, by hand while the server runs.
    pub fn delete_tab_by_hand(&self, spreadsheet_id: &str, title: &str) {
        self.with(|s| {
            let sheet = s.spreadsheets.get_mut(spreadsheet_id).unwrap();
            let id = sheet.sheets.iter().find(|t| t.properties["title"] == json!(title)).unwrap().properties["sheetId"].clone();
            sheet.sheets.retain(|t| t.properties["sheetId"] != id);
            sheet.metadata.retain(|m| m["location"]["sheetId"] != id);
        })
    }

    /// Set one cell (and forget what's kept of the spreadsheet: see
    /// `forget_kept`).
    pub fn set_cell(&self, spreadsheet_id: &str, title: &str, row: usize, column: usize, text: &str) {
        self.forget_kept(spreadsheet_id);
        self.with(|s| {
            let sheet = s.spreadsheets.get_mut(spreadsheet_id).unwrap();
            let tab = sheet.sheets.iter_mut().find(|t| t.properties["title"] == json!(title)).unwrap();
            if text.is_empty() {
                tab.cells.remove(&(row, column));
            } else {
                tab.cells.insert((row, column), text.to_string());
            }
        })
    }
}

fn reason(status: u16) -> &'static str {
    match status {
        200 => "OK",
        204 => "No Content",
        _ => "Error",
    }
}

/// One request inside a batch.
struct BatchedRequest {
    method: String,
    path: String,
    query: Vec<(String, String)>,
    if_match: Option<String>,
    body: Value,
}

/// A multipart/mixed batch's requests, each with its Content-ID.
fn batch_parts(text: &str, boundary: &str) -> Vec<(String, BatchedRequest)> {
    let header = |lines: &str, name: &str| {
        lines.lines().find_map(|l| l.split_once(':').filter(|(k, _)| k.trim().eq_ignore_ascii_case(name)).map(|(_, v)| v.trim().to_string()))
    };
    let mut parts = Vec::new();
    for part in text.split(&format!("--{boundary}")).skip(1) {
        if part.starts_with("--") {
            break;
        }
        let Some((outer, http)) = part.trim_start_matches("\r\n").split_once("\r\n\r\n") else { continue };
        let content_id = header(outer, "content-id").unwrap_or_default().trim_matches(['<', '>']).to_string();
        let (head, body) = http.split_once("\r\n\r\n").unwrap_or((http, ""));
        let (request_line, headers) = head.split_once("\r\n").unwrap_or((head, ""));
        let mut request_line = request_line.split_whitespace();
        let (method, target) = (request_line.next().unwrap_or_default().to_string(), request_line.next().unwrap_or_default().to_string());
        let (path, query_text) = target.split_once('?').unwrap_or((&target, ""));
        let mut query: Vec<(String, String)> = query_text
            .split('&')
            .filter(|p| !p.is_empty())
            .map(|p| {
                let (k, v) = p.split_once('=').unwrap_or((p, ""));
                (decode(k), decode(v))
            })
            .collect();
        query.sort_by(|a, b| a.0.cmp(&b.0));
        let body = body.trim_end();
        let body = if body.is_empty() { Value::Null } else { serde_json::from_str(body).unwrap_or(Value::Null) };
        parts.push((content_id, BatchedRequest { method, path: path.to_string(), query, if_match: header(headers, "if-match"), body }));
    }
    parts
}

fn serve(listener: std::net::TcpListener, state: Arc<Mutex<State>>) {
    use std::io::{BufRead, BufReader, Read, Write};
    for stream in listener.incoming() {
        let Ok(stream) = stream else { continue };
        let state = state.clone();
        std::thread::spawn(move || {
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut stream = stream;
            loop {
                let mut line = String::new();
                if reader.read_line(&mut line).unwrap_or(0) == 0 {
                    return;
                }
                let mut parts = line.split_whitespace();
                let (method, target) = (parts.next().unwrap_or("").to_string(), parts.next().unwrap_or("").to_string());
                let mut length = 0usize;
                let mut if_match: Option<String> = None;
                let mut bearer: Option<String> = None;
                let mut metadata_flavor: Option<String> = None;
                let mut content_type = String::new();
                loop {
                    let mut header = String::new();
                    if reader.read_line(&mut header).unwrap_or(0) == 0 {
                        return;
                    }
                    let header = header.trim_end();
                    if header.is_empty() {
                        break;
                    }
                    if let Some((name, value)) = header.split_once(':') {
                        match name.trim().to_lowercase().as_str() {
                            "content-length" => length = value.trim().parse().unwrap_or(0),
                            "if-match" => if_match = Some(value.trim().to_string()),
                            "authorization" => bearer = value.trim().strip_prefix("Bearer ").map(str::to_string),
                            "metadata-flavor" => metadata_flavor = Some(value.trim().to_string()),
                            "content-type" => content_type = value.trim().to_string(),
                            _ => {}
                        }
                    }
                }
                let mut raw = vec![0u8; length];
                if reader.read_exact(&mut raw).is_err() {
                    return;
                }
                let mut body: Value = if raw.is_empty() { Value::Null } else { serde_json::from_slice(&raw).unwrap_or(Value::Null) };
                let (path, query_text) = target.split_once('?').unwrap_or((&target, ""));
                let mut query: Vec<(String, String)> = query_text
                    .split('&')
                    .filter(|p| !p.is_empty())
                    .map(|p| {
                        let (k, v) = p.split_once('=').unwrap_or((p, ""));
                        (decode(&k.replace('+', " ")), decode(&v.replace('+', " ")))
                    })
                    .filter(|(k, _)| !["alt", "prettyPrint", "key"].contains(&k.as_str()))
                    .collect();
                query.sort_by(|a, b| a.0.cmp(&b.0));
                let (status, answer) = if method == "POST" && path == "/token" {
                    // OAuth's token endpoint: a refresh, as a form.
                    let form: Map<String, Value> = String::from_utf8_lossy(&raw)
                        .split('&')
                        .filter_map(|p| p.split_once('='))
                        .map(|(k, v)| (decode(k), json!(decode(&v.replace('+', " ")))))
                        .collect();
                    body = Value::Object(form);
                    let mut s = state.lock().unwrap();
                    s.requests.push(LoggedRequest { method: method.clone(), path: decode(path), query: query.clone(), if_match: None, body: body.clone(), bearer: None, batched: false });
                    s.ids += 1;
                    let mut answer = json!({"access_token": format!("fresh-{}", s.ids), "expires_in": 3600, "token_type": "Bearer"});
                    // An authorization code's exchange: a refresh token too.
                    if body.get("grant_type") == Some(&json!("authorization_code")) {
                        answer["refresh_token"] = json!(format!("refresh-{}", s.ids));
                    }
                    (200, Some(answer))
                } else if method == "GET" && path == "/computeMetadata/v1/instance/service-accounts/default/token" {
                    // The metadata server: the service account's token, only
                    // for a request that says it's for the metadata server.
                    let mut s = state.lock().unwrap();
                    s.requests.push(LoggedRequest { method: method.clone(), path: decode(path), query: query.clone(), if_match: None, body: Value::Null, bearer: None, batched: false });
                    if metadata_flavor.as_deref() == Some("Google") {
                        (200, Some(json!({"access_token": "metadata-token", "expires_in": 3599, "token_type": "Bearer"})))
                    } else {
                        (403, Some(error_body(403, "Missing Metadata-Flavor:Google header.")))
                    }
                } else if bearer.is_none() {
                    (401, Some(error_body(401, "Request is missing required authentication credential.")))
                } else if method == "POST" && path == "/batch/calendar/v3" {
                    // A batch: logged itself, then each request in it is
                    // logged and answered as if sent on its own.
                    let mut s = state.lock().unwrap();
                    s.requests.push(LoggedRequest { method: method.clone(), path: path.to_string(), query: query.clone(), if_match: None, body: Value::Null, bearer: bearer.clone(), batched: false });
                    let boundary = content_type.split("boundary=").nth(1).unwrap_or_default().trim_matches('"').to_string();
                    let mut answers = String::new();
                    for (content_id, inner) in batch_parts(&String::from_utf8_lossy(&raw), &boundary) {
                        let (status, answer) = s.dispatch(&inner.method, &inner.path, &inner.query, inner.if_match.as_deref(), inner.body, bearer.clone(), true);
                        let text = answer.map(|a| a.to_string()).unwrap_or_default();
                        answers.push_str(&format!(
                            "--batch_fake\r\nContent-Type: application/http\r\nContent-ID: <response-{content_id}>\r\n\r\nHTTP/1.1 {status} {}\r\nContent-Type: application/json\r\n\r\n{text}\r\n",
                            reason(status)
                        ));
                    }
                    answers.push_str("--batch_fake--\r\n");
                    let response = format!("HTTP/1.1 200 OK\r\nContent-Type: multipart/mixed; boundary=batch_fake\r\nContent-Length: {}\r\n\r\n{answers}", answers.len());
                    if stream.write_all(response.as_bytes()).is_err() {
                        return;
                    }
                    continue;
                } else {
                    state.lock().unwrap().dispatch(&method, path, &query, if_match.as_deref(), body, bearer.clone(), false)
                };
                let text = answer.map(|a| a.to_string()).unwrap_or_default();
                let response = format!("HTTP/1.1 {status} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n\r\n{text}", reason(status), text.len());
                if stream.write_all(response.as_bytes()).is_err() {
                    return;
                }
            }
        });
    }
}

fn error_body(status: u16, message: &str) -> Value {
    let reason = match status {
        400 => "badRequest",
        404 => "notFound",
        409 => "duplicate",
        410 => "deleted",
        412 => "conditionNotMet",
        429 => "rateLimitExceeded",
        _ => "",
    };
    json!({"error": {"code": status, "message": message, "errors": [{"message": message, "reason": reason}]}})
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn serves_a_series_expanded_into_instances() {
        let fake = FakeGoogle::start();
        let cal = fake.add_calendar(Some("America/New_York"));
        fake.add_event(
            &cal,
            json!({"summary": "Standup", "start": {"dateTime": "2026-01-05T09:00:00", "timeZone": "America/New_York"},
                   "end": {"dateTime": "2026-01-05T09:15:00", "timeZone": "America/New_York"}, "recurrence": ["RRULE:FREQ=DAILY;COUNT=3"]}),
        );
        let google = fake.google();
        let url = google.calendar_url(&format!("calendars/{}/events", crate::google::segment(&cal)));
        let page = google
            .request("GET", &url, &[("singleEvents", "true".into()), ("timeMin", "2026-01-01T00:00:00Z".into()), ("timeMax", "2026-02-01T00:00:00Z".into())], None, None)
            .unwrap();
        let starts: Vec<&str> = page["items"].as_array().unwrap().iter().map(|i| i["start"]["dateTime"].as_str().unwrap()).collect();
        assert_eq!(starts, vec!["2026-01-05T09:00:00-05:00", "2026-01-06T09:00:00-05:00", "2026-01-07T09:00:00-05:00"]);
        assert_eq!(fake.requests().len(), 1);
    }
}
