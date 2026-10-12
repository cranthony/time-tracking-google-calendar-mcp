//! Probes: find out empirically, against Google for real, how its APIs
//! behave where their docs don't say -- and check the server's own writes
//! (probes/*.py, ported). Each makes a throwaway calendar (this app's
//! calendar.app.created scope allows that), runs its steps, prints what it
//! finds and deletes the calendar again; nothing else in your account is
//! touched. `cancelled_events` only reads the configured calendar.
//!
//! Usage:
//!     probe <name> [--keep] [--pause] [--raw] [--days N]
//!
//! where <name> is one of:
//!     cancelled_events  whether events.list shows cancelled events, and what they keep
//!     clear_fields      that clearing an event's fields (clear_fields) removes them
//!     label_lifecycle   what an event does when its label is removed, then added back
//!     metadata_tabs     the metadata spreadsheet's tabs end to end, and facts on events
//!     series_edits      which instances an edit to a recurring series reaches
//!     series_deletes    what deleting a series (whole, or from an event on) leaves
//!     series_splits     ending a series Google split itself ("_R" ids), and Recurrences on it
//!
//! Each probe's findings, from when it was last run, are in its Python
//! counterpart's docstring (probes/).

use anyhow::{anyhow, Context};
use chrono::{DateTime, Datelike, Duration, FixedOffset, TimeZone, Utc};
use chrono_tz::Tz;
use ctt::app::{App, Config};
use ctt::event::{parse_iso, Event, CLEARABLE_EVENT_FIELDS, PREFIX};
use ctt::facts::Facts;
use ctt::google::calendar::CalendarClient;
use ctt::google::{segment, status_of, Google};
use ctt::sheet::rows::Row;
use ctt::stores::action_calendar::ActionCalendar;
use ctt::stores::recurrences::{split_series_id, Recurrences, Repeat};
use ctt::write_lock::WRITE_LOCK;
use serde_json::{json, Map, Value};
use std::io::Write;
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;

type Moment = DateTime<FixedOffset>;

const TIME_ZONE: &str = "America/New_York";

struct Options {
    keep: bool,
    pause: bool,
    raw: bool,
    days: i64,
}

fn main() {
    ctt::dotenv::load(std::path::Path::new(".env"));
    if let Err(error) = run() {
        eprintln!("{error:#}");
        std::process::exit(1);
    }
}

fn run() -> anyhow::Result<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let usage = "usage: probe <cancelled_events|clear_fields|label_lifecycle|metadata_tabs|series_edits|series_deletes|series_splits> [--keep] [--pause] [--raw] [--days N]";
    let Some(name) = args.first().cloned() else { anyhow::bail!("{usage}") };
    let mut options = Options { keep: false, pause: false, raw: false, days: 14 };
    let mut i = 1;
    while i < args.len() {
        match args[i].as_str() {
            "--keep" => options.keep = true,
            "--pause" => options.pause = true,
            "--raw" => options.raw = true,
            "--days" => {
                i += 1;
                options.days = args.get(i).and_then(|d| d.parse().ok()).ok_or_else(|| anyhow!("--days takes a number"))?;
            }
            other => anyhow::bail!("{usage}\nunrecognized argument: {other}"),
        }
        i += 1;
    }
    let config = Config::from_env();
    let google = Arc::new(Google::load_or_consent(&config.token_path, &config.credentials_path)?);
    let _held = WRITE_LOCK.hold();
    if name == "cancelled_events" {
        return cancelled_events(&google, config.calendar_id()?, &options);
    }
    let (title, zone) = match name.as_str() {
        "clear_fields" => ("CTT clear-fields probe (safe to delete)", Some(TIME_ZONE)),
        "label_lifecycle" => ("CTT label probe (safe to delete)", None),
        "metadata_tabs" => ("CTT tabs probe (safe to delete)", None),
        "series_edits" => ("CTT series probe (safe to delete)", Some(TIME_ZONE)),
        "series_deletes" => ("CTT series delete probe (safe to delete)", Some(TIME_ZONE)),
        "series_splits" => ("CTT series split probe (safe to delete)", Some(TIME_ZONE)),
        _ => anyhow::bail!("{usage}"),
    };
    let calendar_id = CalendarClient::new(google.clone(), "").create_calendar(title, None, zone)?;
    println!("Scratch calendar: {calendar_id}");
    let raw = Raw { google: google.clone(), calendar_id: calendar_id.clone() };
    let outcome = match name.as_str() {
        "clear_fields" => clear_fields(&raw),
        "label_lifecycle" => label_lifecycle(&raw, &options),
        "metadata_tabs" => metadata_tabs(&raw, &config),
        "series_edits" => series_edits(&raw, &options),
        "series_deletes" => series_deletes(&raw, &options),
        _ => series_splits(&raw, &config),
    };
    if options.keep {
        println!("Kept scratch calendar {calendar_id}");
    } else {
        raw.delete_calendar()?;
        println!("Deleted scratch calendar");
    }
    outcome?;
    let failures = FAILURES.load(Ordering::SeqCst);
    if CHECKED.load(Ordering::SeqCst) > 0 {
        println!("\n{}", if failures == 0 { "All checks passed".to_string() } else { format!("{failures} check(s) FAILED") });
    }
    if failures > 0 {
        std::process::exit(1);
    }
    Ok(())
}

// -- shared ------------------------------------------------------------------

static FAILURES: AtomicUsize = AtomicUsize::new(0);
static CHECKED: AtomicUsize = AtomicUsize::new(0);

fn check(what: &str, ok: bool, detail: impl std::fmt::Debug) {
    CHECKED.fetch_add(1, Ordering::SeqCst);
    if !ok {
        FAILURES.fetch_add(1, Ordering::SeqCst);
    }
    println!("  {}  {what}{}", if ok { "PASS" } else { "FAIL" }, if ok { String::new() } else { format!("  ({detail:?})") });
}

fn pause(on: bool, prompt: &str) {
    if on {
        print!("  {prompt}, then press Enter... ");
        let _ = std::io::stdout().flush();
        let _ = std::io::stdin().read_line(&mut String::new());
    }
}

/// The Calendar API as it is, for what the probes ask it directly.
struct Raw {
    google: Arc<Google>,
    calendar_id: String,
}

impl Raw {
    fn call(&self, method: &str, path: &str, query: &[(&str, String)], body: Option<&Value>) -> anyhow::Result<Value> {
        let url = self.google.calendar_url(&format!("calendars/{}{path}", segment(&self.calendar_id)));
        self.google.request(method, &url, query, body, None)
    }

    fn get(&self, event_id: &str) -> anyhow::Result<Value> {
        self.call("GET", &format!("/events/{}", segment(event_id)), &[], None)
    }

    fn insert(&self, body: Value) -> anyhow::Result<Value> {
        self.call("POST", "/events", &[], Some(&body))
    }

    fn patch(&self, event_id: &str, body: Value) -> anyhow::Result<Value> {
        self.call("PATCH", &format!("/events/{}", segment(event_id)), &[], Some(&body))
    }

    fn update(&self, event_id: &str, body: Value) -> anyhow::Result<Value> {
        self.call("PUT", &format!("/events/{}", segment(event_id)), &[], Some(&body))
    }

    fn import(&self, body: Value) -> anyhow::Result<Value> {
        self.call("POST", "/events/import", &[], Some(&body))
    }

    fn list(&self, query: &[(&str, String)]) -> anyhow::Result<Vec<Value>> {
        let mut items = Vec::new();
        let mut token: Option<String> = None;
        loop {
            let mut q: Vec<(&str, String)> = query.to_vec();
            if let Some(t) = &token {
                q.push(("pageToken", t.clone()));
            }
            let page = self.call("GET", "/events", &q, None)?;
            items.extend(page["items"].as_array().cloned().unwrap_or_default());
            token = page["nextPageToken"].as_str().map(str::to_string);
            if token.is_none() {
                return Ok(items);
            }
        }
    }

    fn instances(&self, series_id: &str, deleted: bool) -> anyhow::Result<Vec<Value>> {
        let page = self.call("GET", &format!("/events/{}/instances", segment(series_id)), &[("showDeleted", deleted.to_string())], None)?;
        let mut items = page["items"].as_array().cloned().unwrap_or_default();
        items.sort_by_key(original_start);
        Ok(items)
    }

    fn delete_calendar(&self) -> anyhow::Result<Value> {
        let url = self.google.calendar_url(&format!("calendars/{}", segment(&self.calendar_id)));
        self.google.request("DELETE", &url, &[], None, None)
    }

    fn client(&self) -> CalendarClient {
        CalendarClient::new(self.google.clone(), &self.calendar_id)
    }
}

fn zone() -> Tz {
    TIME_ZONE.parse().unwrap()
}

fn when(moment: &Moment) -> Value {
    json!({"dateTime": ctt::py::isoformat(moment), "timeZone": TIME_ZONE})
}

/// 9:00 tomorrow, New York time (or `hour`, on the Monday after next week).
fn tomorrow_at(hour: u32) -> Moment {
    let today = Utc::now().with_timezone(&zone()).date_naive();
    let day = today + Duration::days(1);
    zone().with_ymd_and_hms(day.year(), day.month(), day.day(), hour, 0, 0).earliest().unwrap().fixed_offset()
}

fn private(resource: &Value) -> Map<String, Value> {
    resource.pointer("/extendedProperties/private").and_then(Value::as_object).cloned().unwrap_or_default()
}

fn key(name: &str) -> String {
    format!("{PREFIX}{name}")
}

fn moment_of(when: &Value) -> Option<Moment> {
    when.get("dateTime").and_then(Value::as_str).and_then(|t| parse_iso(t, None).ok())
}

fn original_start(event: &Value) -> String {
    let original = event.get("originalStartTime").or_else(|| event.get("start")).cloned().unwrap_or(json!({}));
    original.get("dateTime").or_else(|| original.get("date")).and_then(Value::as_str).unwrap_or("").to_string()
}

fn local(text: &str) -> String {
    parse_iso(text, None).map(|m| m.with_timezone(&zone()).format("%m-%d %H:%M").to_string()).unwrap_or_else(|_| "-".into())
}

fn indented(value: &Value, indent: &str) -> String {
    let text = serde_json::to_string_pretty(value).unwrap_or_default();
    format!("{indent}{}", text.replace('\n', &format!("\n{indent}")))
}

// -- cancelled_events -----------------------------------------------------------

fn cancelled_events(google: &Arc<Google>, calendar_id: &str, options: &Options) -> anyhow::Result<()> {
    let raw = Raw { google: google.clone(), calendar_id: calendar_id.into() };
    let end = Utc::now().fixed_offset();
    let start = end - Duration::days(options.days);
    println!("Calendar {calendar_id}, {} to {} UTC\n", start.format("%Y-%m-%d %H:%M"), end.format("%Y-%m-%d %H:%M"));
    let listing = |deleted: bool| {
        raw.list(&[
            ("timeMin", ctt::py::isoformat(&start)),
            ("timeMax", ctt::py::isoformat(&end)),
            ("singleEvents", "true".into()),
            ("orderBy", "startTime".into()),
            ("maxResults", "2500".into()),
            ("showDeleted", deleted.to_string()),
        ])
    };
    let plain = listing(false)?;
    let deleted = listing(true)?;
    let cancelled = |items: &[Value]| -> Vec<Value> { items.iter().filter(|e| e["status"] == "cancelled").cloned().collect() };
    let plain_cancelled = cancelled(&plain);
    let all_cancelled = cancelled(&deleted);
    println!("Without showDeleted: {} events, {} cancelled", plain.len(), plain_cancelled.len());
    println!("With showDeleted:    {} events, {} cancelled\n", deleted.len(), all_cancelled.len());
    let action_ids = key("action_ids");
    let mut series: Vec<(String, Option<Value>)> = Vec::new();
    for event in &all_cancelled {
        let props = private(event);
        let mut kept: Vec<String> = ["summary", "start", "end", "originalStartTime", "recurringEventId", "eventLabelId"]
            .into_iter()
            .filter(|f| event.get(*f).is_some())
            .map(str::to_string)
            .collect();
        if props.contains_key(&action_ids) {
            kept.push("action_ids".into());
        }
        let missing: Vec<&str> = ["summary", "start", "end", "action_ids"].into_iter().filter(|f| !kept.iter().any(|k| k == f)).collect();
        let at = event.get("start").or_else(|| event.get("originalStartTime")).and_then(|w| w.get("dateTime")).and_then(Value::as_str).unwrap_or("?");
        let also = if plain.iter().any(|e| e["id"] == event["id"]) { "also listed without showDeleted" } else { "" };
        println!("- {at}  {}  id={}  {also}", ctt::py::repr(event["summary"].as_str().unwrap_or("(no summary)")), event["id"].as_str().unwrap_or(""));
        println!("    kept: {}", if kept.is_empty() { "nothing but its id".into() } else { kept.join(", ") });
        if !missing.is_empty() {
            println!("    missing: {}", missing.join(", "));
        }
        if let Some(ids) = props.get(&action_ids).and_then(Value::as_str).filter(|s| !s.is_empty()) {
            println!("    action_ids: {ids}");
        } else if let Some(series_id) = event["recurringEventId"].as_str() {
            if !series.iter().any(|(k, _)| k == series_id) {
                series.push((series_id.into(), raw.get(series_id).ok()));
            }
            match &series.iter().find(|(k, _)| k == series_id).unwrap().1 {
                Some(master) => println!("    its series' action_ids: {}", private(master).get(&action_ids).and_then(Value::as_str).unwrap_or("(none)")),
                None => println!("    its series is gone"),
            }
        }
        if options.raw {
            println!("{}", indented(event, "    "));
        }
    }
    if all_cancelled.is_empty() {
        println!("No cancelled events in that range: try a longer --days, or cancel a test event first.");
    }
    Ok(())
}

// -- clear_fields ---------------------------------------------------------------

fn clear_fields(raw: &Raw) -> anyhow::Result<()> {
    let client = raw.client();
    let nine = tomorrow_at(9);
    let cleared = |names: &[&str]| names.iter().map(|n| n.to_string()).collect();

    println!("\n1. Clear every clearable field of an event, while renaming it");
    let full = client.create_event(&Event {
        summary: Some("Probe event".into()),
        start: Some(nine),
        end: Some(nine + Duration::hours(1)),
        time_zone: Some(TIME_ZONE.into()),
        description: Some("a description".into()),
        location: Some("a location".into()),
        priority: Some(1),
        action_ids: Some(vec!["actionA".into()]),
        ..Default::default()
    })?;
    let id = full.id.clone().unwrap();
    let before = raw.get(&id)?;
    check("set up with all the app's fields and no colorId", before.get("colorId").is_none() && private(&before).len() == 2, &before);
    let returned = client.update_event(&Event {
        id: Some(id.clone()),
        summary: Some("Probe event (renamed)".into()),
        cleared: cleared(&CLEARABLE_EVENT_FIELDS),
        ..Default::default()
    })?;
    let after = raw.get(&id)?;
    check("summary renamed", after["summary"] == "Probe event (renamed)", &after["summary"]);
    check("description removed", after.get("description").is_none_or(|d| d == ""), &after["description"]);
    check("location removed", after.get("location").is_none_or(|d| d == ""), &after["location"]);
    let left = private(&after);
    check("only action_ids left in extendedProperties.private", left.len() == 1 && left.get(&key("action_ids")) == Some(&json!("actionA")), &left);
    check("times kept", after["start"] == before["start"] && after["end"] == before["end"], ());
    check(
        "the patch's response parses with every cleared field None",
        returned.description.is_none() && returned.location.is_none() && returned.priority.is_none() && returned.facts.is_none() && returned.judgments.is_none(),
        &returned,
    );

    println!("\n2. Clear only an event's judgments");
    let partial = client.create_event(&Event {
        summary: Some("Probe partial".into()),
        start: Some(nine + Duration::hours(2)),
        end: Some(nine + Duration::hours(3)),
        time_zone: Some(TIME_ZONE.into()),
        judgments: json!({"self": {"t1": {"p1": {"rating": 1, "scale": 3, "reasoning": "Fine."}}}}).as_object().cloned(),
        priority: Some(3),
        ..Default::default()
    })?;
    let pid = partial.id.clone().unwrap();
    client.update_event(&Event { id: Some(pid.clone()), cleared: cleared(&["judgments"]), ..Default::default() })?;
    let after = private(&raw.get(&pid)?);
    check("judgments removed", !after.keys().any(|k| k.starts_with(&key("judgments"))), &after);
    check("priority kept", after.get(&key("priority")) == Some(&json!("3")), &after);

    let recurrences = recurrences_for(raw)?;
    let series = |summary: &str, hour: i64| -> anyhow::Result<Event> {
        let start = nine + Duration::hours(hour);
        client.create_event(&Event {
            summary: Some(summary.into()),
            start: Some(start),
            end: Some(start + Duration::minutes(30)),
            time_zone: Some(TIME_ZONE.into()),
            recurrence: Some(vec!["RRULE:FREQ=DAILY;COUNT=4".into()]),
            priority: Some(1),
            location: Some("Room 4".into()),
            ..Default::default()
        })
    };

    println!("\n3. Clear a series' priority");
    let whole = series("Probe series", 4)?.id.unwrap();
    recurrences.update(&Event { id: Some(whole.clone()), cleared: cleared(&["priority"]), ..Default::default() }, None, None)?;
    let master = raw.get(&whole)?;
    check("series' priority removed", !private(&master).contains_key(&key("priority")), private(&master));
    check("series' location kept", master["location"] == "Room 4", &master["location"]);
    let items = raw.instances(&whole, false)?;
    check(
        &format!("all {} instances lost their priority, and kept the location", items.len()),
        items.len() == 4 && items.iter().all(|i| !private(i).contains_key(&key("priority")) && i["location"] == "Room 4"),
        items.iter().map(|i| (private(i), i["location"].clone())).collect::<Vec<_>>(),
    );

    println!("\n4. Clear another series' priority from its third event on");
    let split = series("Probe split series", 6)?.id.unwrap();
    let third = raw.instances(&split, false)?[2]["id"].as_str().unwrap().to_string();
    let parts = recurrences.update(&Event { id: Some(split.clone()), cleared: cleared(&["priority"]), ..Default::default() }, Some(&third), None)?;
    let (later, earlier) = (parts[0].id.clone().unwrap(), parts[1].id.clone().unwrap());
    let (later_raw, earlier_raw) = (raw.get(&later)?, raw.get(&earlier)?);
    check("later part's priority removed", !private(&later_raw).contains_key(&key("priority")), private(&later_raw));
    check("earlier part kept its priority", private(&earlier_raw).get(&key("priority")) == Some(&json!("1")), private(&earlier_raw));
    let counts = (raw.instances(&earlier, false)?.len(), raw.instances(&later, false)?.len());
    check("2 events in each part", counts == (2, 2), counts);
    Ok(())
}

/// Recurrences over the scratch calendar, as the server makes it: through
/// an ActionCalendar (which makes the calendar's metadata spreadsheet).
fn recurrences_for(raw: &Raw) -> anyhow::Result<Recurrences> {
    let mut config = Config::from_env();
    config.calendar_id = Some(raw.calendar_id.clone());
    let app = Arc::new(App::new(config));
    app.set_self();
    Ok(Recurrences::new(ActionCalendar::new(raw.client(), app.actions()?)))
}

// -- label_lifecycle ------------------------------------------------------------

fn label_lifecycle(raw: &Raw, options: &Options) -> anyhow::Result<()> {
    let label_id = uuid::Uuid::new_v4().to_string();
    let label = json!({"id": label_id, "name": "Probe label", "backgroundColor": "#8e24aa"});
    let set_labels = |labels: Value| -> anyhow::Result<Value> {
        let url = raw.google.calendar_url(&format!("calendars/{}", segment(&raw.calendar_id)));
        let answer = raw.google.request("PATCH", &url, &[], Some(&json!({"labelProperties": {"eventLabels": labels}})), None)?;
        Ok(answer.pointer("/labelProperties/eventLabels").cloned().unwrap_or(json!([])))
    };
    let versioned = [("eventLabelVersion", "1".to_string())];
    let insert = |summary: &str, hour: i64, label: Option<&str>| -> anyhow::Result<Value> {
        let start = Utc::now().fixed_offset().with_timezone(&FixedOffset::east_opt(0).unwrap());
        let start = start - Duration::seconds(start.timestamp() % 3600) - Duration::nanoseconds(start.timestamp_subsec_nanos() as i64) + Duration::hours(hour);
        let mut body = json!({
            "summary": summary,
            "start": {"dateTime": ctt::py::isoformat(&start)},
            "end": {"dateTime": ctt::py::isoformat(&(start + Duration::minutes(30)))},
        });
        if let Some(l) = label {
            body["eventLabelId"] = json!(l);
        }
        raw.call("POST", "/events", &versioned, Some(&body))
    };
    let label_of = |id: &str| -> anyhow::Result<Value> { Ok(raw.get(id)?.get("eventLabelId").cloned().unwrap_or(Value::Null)) };
    let attempt = |step: &str, result: anyhow::Result<Value>| match result {
        Ok(v) => println!("  {step}: OK -> {v}"),
        Err(e) => println!("  {step}: HTTP {} {e}", status_of(&e).map(|s| s.to_string()).unwrap_or_default()),
    };

    println!("1. Create a label with a client-chosen UUID");
    let labels = set_labels(json!([label]))?;
    println!("  labels now: {labels}");
    println!("  client id kept: {}", labels.as_array().is_some_and(|l| l.iter().any(|x| x["id"] == json!(label_id))));
    let e1 = insert("Probe 1 (untouched)", 1, Some(&label_id))?["id"].as_str().unwrap().to_string();
    let e2 = insert("Probe 2 (patched while label gone)", 2, Some(&label_id))?["id"].as_str().unwrap().to_string();
    let e3 = insert("Probe 3 (patched with stale id)", 3, Some(&label_id))?["id"].as_str().unwrap().to_string();

    println!("2. Remove the label from the calendar");
    set_labels(json!([]))?;
    println!("  event 1 eventLabelId after removal: {}", label_of(&e1)?);
    pause(options.pause, "Check the three probe events in the Calendar UI");

    println!("3. Write events while the label is gone");
    attempt(
        "patch event 2's summary only (no eventLabelId in body)",
        raw.call("PATCH", &format!("/events/{}", segment(&e2)), &versioned, Some(&json!({"summary": "Probe 2 (patched)"}))).map(|v| v["eventLabelId"].clone()),
    );
    attempt(
        "patch event 3, re-sending the stale eventLabelId",
        raw.call("PATCH", &format!("/events/{}", segment(&e3)), &versioned, Some(&json!({"eventLabelId": label_id}))).map(|v| v["eventLabelId"].clone()),
    );
    attempt("insert a new event with the stale eventLabelId", insert("Probe 4", 4, Some(&label_id)).map(|v| v["eventLabelId"].clone()));

    println!("4. Add the label back with the same UUID (different color)");
    let mut green = label.clone();
    green["backgroundColor"] = json!("#0b8043");
    attempt("re-add", set_labels(json!([green])).map(|l| json!(l.as_array().map(|a| a.iter().map(|x| x["id"].clone()).collect::<Vec<_>>()))));
    for (name, id) in [("1", &e1), ("2", &e2), ("3", &e3)] {
        println!("  event {name} eventLabelId after re-adding: {}", label_of(id)?);
    }
    pause(options.pause, "Check whether the probe events show the label (now green) again");

    println!("5. Filter by a private extended property");
    raw.patch(&e1, json!({"extendedProperties": {"private": {"probe-goal": "g1"}}}))?;
    let found = raw.list(&[("privateExtendedProperty", "probe-goal=g1".into())])?;
    println!("  matched: {:?}", found.iter().map(|i| i["summary"].as_str().unwrap_or("").to_string()).collect::<Vec<_>>());

    println!("6. Store a long description (event notes live there)");
    for length in [8_000usize, 32_000, 128_000] {
        attempt(
            &format!("{length}-character description, length read back"),
            raw.patch(&e1, json!({"description": "x".repeat(length)})).map(|v| json!(v["description"].as_str().map(|d| d.len()).unwrap_or(0))),
        );
    }
    Ok(())
}

// -- metadata_tabs --------------------------------------------------------------

fn step(what: &str, ok: bool, detail: impl std::fmt::Debug) -> anyhow::Result<()> {
    println!("{} {what} {}", if ok { "ok  " } else { "FAIL" }, if ok { String::new() } else { format!("{detail:?}") });
    if !ok {
        anyhow::bail!("Step failed: {what}");
    }
    Ok(())
}

fn row(pairs: Value) -> Row {
    pairs.as_object().cloned().unwrap_or_default()
}

fn metadata_tabs(raw: &Raw, base: &Config) -> anyhow::Result<()> {
    let mut config = Config { calendar_id: Some(raw.calendar_id.clone()), token_path: base.token_path.clone(), credentials_path: base.credentials_path.clone() };
    config.calendar_id = Some(raw.calendar_id.clone());
    let app = Arc::new(App::new(config));
    app.set_self();
    let client = raw.client();
    let labels = || -> anyhow::Result<Vec<(String, String)>> {
        Ok(client.list_event_labels()?.0.into_iter().filter_map(|l| Some((l.id?, l.name?))).collect())
    };
    let label_name = |labels: &[(String, String)], id: &str| labels.iter().find(|(i, _)| i == id).map(|(_, n)| n.clone());
    let text = |v: &Value, k: &str| v.get(k).and_then(Value::as_str).unwrap_or("").to_string();

    println!("\nActions");
    let actions = app.actions()?;
    println!("Scratch spreadsheet: https://docs.google.com/spreadsheets/d/{}", actions.spreadsheet_id());
    let walk = text(&actions.create_action(&row(json!({"name": "Walk", "status": "active", "priority": 1})))?, "created_id");
    let idea = text(&actions.create_action(&row(json!({"name": "Juggle", "note": "proposed by the assistant"})))?, "created_id");
    let all = actions.all()?;
    let label_id = |id: &str| all.iter().find(|a| a["id"] == json!(id)).map(|a| text(&Value::Object(a.clone()), "label_id")).unwrap_or_default();
    step("created two actions", all.len() == 2, all.iter().map(|a| a["name"].clone()).collect::<Vec<_>>())?;
    let (walk_label, idea_label) = (label_id(&walk), label_id(&idea));
    let now = labels()?;
    step("both hold labels on the calendar", label_name(&now, &walk_label).as_deref() == Some("Walk") && label_name(&now, &idea_label).as_deref() == Some("Juggle"), &now)?;
    actions.update_action(&row(json!({"id": walk, "name": "Stroll"})), &[])?;
    step("renaming renames the label", label_name(&labels()?, &walk_label).as_deref() == Some("Stroll"), ())?;
    actions.update_action(&row(json!({"id": idea, "status": "archived"})), &[])?;
    step("archiving removes the label", label_name(&labels()?, &idea_label).is_none(), ())?;
    step("found by name", actions.get_action("stroll")?["id"] == json!(walk), ())?;
    match actions.create_action(&row(json!({"name": "stroll"}))) {
        Ok(_) => step("a duplicate name is refused", false, ())?,
        Err(e) => step("a duplicate name is refused", e.to_string().contains("already an action"), e.to_string())?,
    }

    println!("\nAction groups");
    let outdoors = text(&actions.create_action_group(&row(json!({"name": "Outdoors", "background_color": "#0b8043"})))?, "created_id");
    actions.update_action(&row(json!({"id": walk, "group_id": outdoors})), &[])?;
    let color = client.list_event_labels()?.0.into_iter().find(|l| l.id.as_deref() == Some(walk_label.as_str())).map(|l| l.background_color).unwrap_or_default();
    step("an action's label takes its group's color", color.to_lowercase() == "#0b8043", &color)?;
    actions.delete_action_group(&outdoors)?;
    step(
        "deleting the group moves the action to the top",
        actions.get_action(&walk)?["group_id"].is_null() && actions.get_action_groups()?.as_array().is_some_and(|g| g.is_empty()),
        (),
    )?;

    println!("\nPeople and circles");
    let people = app.people()?;
    step("self is there before any row", people.get_people(None)?.iter().map(|p| p["id"].clone()).collect::<Vec<_>>() == vec![json!("self")], ())?;
    let family = text(&people.create_circle(&row(json!({"name": "Family"})))?, "created_id");
    let sam = text(&people.create_person(&row(json!({"name": "Sam", "context": "cousin", "circles": [family]})))?, "created_id");
    people.update_person(&row(json!({"id": "self", "what_matters": "music"})), &[])?;
    let listed = people.get_people(None)?;
    let find = |id: &str| listed.iter().find(|p| p["id"] == json!(id)).cloned().unwrap_or_default();
    step("people and self's row read back", find(&sam)["circle_names"] == json!(["Family"]) && find("self")["what_matters"] == json!("music"), ())?;
    match people.create_person(&row(json!({"name": "sam", "context": "Cousin"}))) {
        Ok(_) => step("a duplicate name and context is refused", false, ())?,
        Err(e) => step("a duplicate name and context is refused", e.to_string().contains("already a person"), e.to_string())?,
    }
    people.delete_circle(&family)?;
    step("deleting a circle takes its people out", people.get_person(&sam)?["circles"].is_null(), ())?;

    println!("\nLocations");
    let locations = app.locations()?;
    let home = text(&locations.create_location(&row(json!({"name": "Home", "hint": "the apartment"})))?, "created_id");
    locations.update_location(&row(json!({"id": home, "hint": "the apartment; 'my place'"})), &[])?;
    step("a location reads back by name", locations.get_location("home")?["hint"] == json!("the apartment; 'my place'"), ())?;
    locations.delete_location(&home)?;
    step("deleting removes it", locations.all()?.is_empty(), ())?;

    println!("\nFacts on an event");
    let start = Utc::now().fixed_offset() - Duration::hours(2);
    let start = start - Duration::nanoseconds(start.timestamp_subsec_nanos() as i64);
    let long_facts = Facts { with_ids: Some(json!(["p1"])), notes: Some(json!({"self": "x".repeat(900), "p1": "y".repeat(900)})), ..Default::default() };
    let short_facts = Facts { with_ids: Some(json!(["p1"])), ..Default::default() };
    let created = client.create_event(&Event { summary: Some("Probe".into()), start: Some(start), end: Some(start + Duration::hours(1)), facts: Some(long_facts.clone()), ..Default::default() })?;
    let id = created.id.clone().unwrap();
    step("long facts are split across properties and read back whole", client.get_event(&id)?.facts == Some(long_facts.clone()), ())?;
    client.update_event(&Event { id: Some(id.clone()), facts: Some(short_facts.clone()), ..Default::default() })?;
    step("shorter facts leave no tail behind", client.get_event(&id)?.facts == Some(short_facts.clone()), ())?;
    let one = json!({"adventurous": {"judgment": {"rating": 2, "scale": 3, "reasoning": "z".repeat(250)}}});
    let mut judgments = Map::new();
    judgments.insert("self".into(), one.clone());
    for i in 0..8 {
        judgments.insert(format!("p{i}"), one.clone());
    }
    client.update_event(&Event { id: Some(id.clone()), judgments: Some(judgments.clone()), ..Default::default() })?;
    step("judgments round-trip too, across properties", client.get_event(&id)?.judgments == Some(judgments), ())?;
    client.update_event(&Event { id: Some(id.clone()), cleared: ["facts", "judgments"].iter().map(|s| s.to_string()).collect(), ..Default::default() })?;
    let read = client.get_event(&id)?;
    step("clearing removes them", read.facts.is_none() && read.judgments.is_none(), ())?;

    let series = client.create_event(&Event {
        summary: Some("Probe series".into()),
        start: Some(start),
        end: Some(start + Duration::hours(1)),
        time_zone: Some("UTC".into()),
        recurrence: Some(vec!["RRULE:FREQ=DAILY;COUNT=3".into()]),
        ..Default::default()
    })?;
    let instance = client.list_instances(series.id.as_deref().unwrap(), &(start + Duration::days(3)))?[1].id.clone().unwrap();
    client.update_event(&Event { id: Some(instance.clone()), facts: Some(long_facts.clone()), ..Default::default() })?;
    step("an instance takes long facts", client.get_event(&instance)?.facts == Some(long_facts), ())?;
    client.update_event(&Event { id: Some(instance.clone()), facts: Some(short_facts.clone()), ..Default::default() })?;
    step("and shorter ones", client.get_event(&instance)?.facts == Some(short_facts), ())?;
    client.update_event(&Event { id: Some(instance.clone()), cleared: ["facts", "judgments", "priority"].iter().map(|s| s.to_string()).collect(), ..Default::default() })?;
    step("and clearing them, and what it never had", client.get_event(&instance)?.facts.is_none(), ())?;
    println!("\nAll steps passed.");
    println!("(The scratch spreadsheet stays in Drive: this app can't trash it.)");
    Ok(())
}

// -- series_edits ---------------------------------------------------------------

const ROLES: [&str; 6] = ["untouched", "moved", "reprioritized", "regoaled", "described", "untouched"];

fn describe_series_event(event: &Value) -> String {
    let props = private(event);
    let get = |k: &str| props.get(&key(k)).and_then(Value::as_str).unwrap_or("-").to_string();
    let original = original_start(event);
    let mut fields = vec![
        format!("orig={}", if original.is_empty() { "-".into() } else { local(&original) }),
        format!("start={}", event.pointer("/start/dateTime").and_then(Value::as_str).map(local).unwrap_or_else(|| "-".into())),
        format!("status={}", event["status"].as_str().unwrap_or("?")),
        format!("priority={}", get("priority")),
        format!("goals={}", get("goal_ids")),
        format!("color={}", event["colorId"].as_str().unwrap_or("-")),
        format!("summary={}", ctt::py::repr(event["summary"].as_str().unwrap_or("-"))),
    ];
    if let Some(d) = event["description"].as_str() {
        fields.push(format!("description={}", ctt::py::repr(d)));
    }
    fields.join("  ")
}

fn series_edits(raw: &Raw, options: &Options) -> anyhow::Result<()> {
    let with_priority = |priority: i64, mut extra: Map<String, Value>| {
        extra.insert(key("priority"), json!(priority.to_string()));
        json!({"extendedProperties": {"private": extra}})
    };
    let goals = |g: &str| -> Map<String, Value> { [(key("goal_ids"), json!(g))].into_iter().collect() };
    let show = |step: &str, master_id: &str| -> anyhow::Result<Vec<Value>> {
        let master = raw.get(master_id)?;
        println!("\n{step}");
        println!("  {:<13} {}", "master", describe_series_event(&master));
        println!("    recurrence: {}", master["recurrence"]);
        let items = raw.instances(master_id, true)?;
        for (i, item) in items.iter().enumerate() {
            println!("  {:<13} {}", ROLES.get(i).copied().unwrap_or("?"), describe_series_event(item));
            if options.raw {
                println!("{}", indented(item, "    "));
            }
        }
        pause(options.pause, "Check the series in the Calendar UI");
        Ok(items)
    };
    let report_moves = |before: &[Value], after: &[Value]| {
        let mut any = false;
        for (i, (old, new)) in before.iter().zip(after).enumerate() {
            if old["start"] != new["start"] || old["end"] != new["end"] {
                any = true;
                let s = |e: &Value| e.pointer("/start/dateTime").and_then(Value::as_str).map(local).unwrap_or_else(|| "-".into());
                println!("  !! {} instance's times changed: {} -> {}", ROLES.get(i).copied().unwrap_or("?"), s(old), s(new));
            }
        }
        if !any {
            println!("  (no instance's times changed)");
        }
    };

    let start = tomorrow_at(9);
    let mut body = json!({
        "summary": "Probe series",
        "start": when(&start),
        "end": when(&(start + Duration::minutes(30))),
        "recurrence": [format!("RRULE:FREQ=DAILY;COUNT={}", ROLES.len())],
    });
    body["extendedProperties"] = with_priority(1, goals("goalA"))["extendedProperties"].clone();
    let master_id = raw.insert(body)?["id"].as_str().unwrap().to_string();
    println!("Series: {master_id}, daily from {}, priority 1, goals goalA", start.format("%Y-%m-%d %H:%M %:z"));

    println!("\n1. Edit four instances on their own");
    let items = raw.instances(&master_id, false)?;
    let moved_start = moment_of(&items[1]["start"]).context("no start")? + Duration::hours(1);
    raw.patch(items[1]["id"].as_str().unwrap(), json!({"start": when(&moved_start), "end": when(&(moved_start + Duration::minutes(30)))}))?;
    raw.patch(items[2]["id"].as_str().unwrap(), with_priority(5, Map::new()))?;
    raw.patch(items[3]["id"].as_str().unwrap(), json!({"extendedProperties": {"private": goals("goalX")}}))?;
    raw.patch(items[4]["id"].as_str().unwrap(), json!({"description": "edited on its own"}))?;
    let before = show("After step 1 (instance edits)", &master_id)?;

    println!("\n2. Patch the master's summary, priority (3) and goal_ids (goalB) -- no times");
    let mut body = with_priority(3, goals("goalB"));
    body["summary"] = json!("Probe series (edited)");
    raw.patch(&master_id, body)?;
    let after = show("After step 2 (series edit, no times)", &master_id)?;
    report_moves(&before, &after);

    println!("\n3. Patch the master's goal_ids (goalC) alone -- no priority in the body");
    raw.patch(&master_id, json!({"extendedProperties": {"private": goals("goalC")}}))?;
    let after_goals = show("After step 3 (goals-only series edit)", &master_id)?;
    report_moves(&after, &after_goals);

    println!("\n4. Patch the master's start and end 30 minutes later");
    let later = start + Duration::minutes(30);
    raw.patch(&master_id, json!({"start": when(&later), "end": when(&(later + Duration::minutes(30)))}))?;
    show("After step 4 (series times moved)", &master_id)?;
    Ok(())
}

// -- series_deletes -------------------------------------------------------------

const ROLES_A: [&str; 6] = ["untouched", "moved-1h", "cut-here", "described", "moved-before-cut", "moved-later"];
const ROLES_B: [&str; 6] = ["untouched", "described", "untouched", "untouched", "untouched", "untouched"];

fn describe_brief(event: &Value) -> String {
    let original = original_start(event);
    let mut fields = vec![
        format!("orig={}", if original.is_empty() { "-".into() } else { local(&original) }),
        format!("start={}", event.pointer("/start/dateTime").and_then(Value::as_str).map(local).unwrap_or_else(|| "-".into())),
        format!("status={}", event["status"].as_str().unwrap_or("?")),
        format!("summary={}", ctt::py::repr(event["summary"].as_str().unwrap_or("-"))),
    ];
    if let Some(d) = event["description"].as_str() {
        fields.push(format!("description={}", ctt::py::repr(d)));
    }
    fields.join("  ")
}

fn series_deletes(raw: &Raw, options: &Options) -> anyhow::Result<()> {
    let recurrences = recurrences_for(raw)?;
    let mv = |item: &Value, to: Moment| raw.patch(item["id"].as_str().unwrap(), json!({"start": when(&to), "end": when(&(to + Duration::minutes(30)))}));
    let insert = |summary: &str, start: Moment| -> anyhow::Result<String> {
        let id = raw
            .insert(json!({"summary": summary, "start": when(&start), "end": when(&(start + Duration::minutes(30))), "recurrence": ["RRULE:FREQ=DAILY;COUNT=6"]}))?["id"]
            .as_str()
            .unwrap()
            .to_string();
        println!("{summary}: {id}, daily from {}, 6 times", start.format("%Y-%m-%d %H:%M %:z"));
        Ok(id)
    };
    let show = |step: &str, master_id: &str, roles: &[&str]| -> anyhow::Result<()> {
        let master = raw.get(master_id)?;
        println!("\n{step}");
        println!("  {:<17} {}", "master", describe_brief(&master));
        println!("    recurrence: {}", master["recurrence"]);
        let items = raw.instances(master_id, true)?;
        println!("  instances (showDeleted):");
        let mut by_id: Vec<(String, &str)> = Vec::new();
        for (i, item) in items.iter().enumerate() {
            let role = roles.get(i).copied().unwrap_or("?");
            by_id.push((item["id"].as_str().unwrap_or("").to_string(), role));
            println!("    {role:<17} {}", describe_brief(item));
            if options.raw {
                println!("{}", indented(item, "      "));
            }
        }
        let listed = raw.list(&[("singleEvents", "true".into()), ("orderBy", "startTime".into())])?;
        let mine: Vec<&Value> = listed.iter().filter(|i| i["recurringEventId"] == json!(master_id)).collect();
        println!("  listed (singleEvents, no showDeleted -- what list_events sees):");
        for item in &mine {
            let role = by_id.iter().find(|(k, _)| Some(k.as_str()) == item["id"].as_str()).map(|(_, r)| *r).unwrap_or("?");
            println!("    {role:<17} {}", describe_brief(item));
        }
        if mine.is_empty() {
            println!("    (none)");
        }
        pause(options.pause, "Check the series in the Calendar UI");
        Ok(())
    };

    let start = tomorrow_at(9);
    let a = insert("Probe series A", start)?;
    let b = insert("Probe series B", start + Duration::hours(3))?;

    println!("\n1. Edit four of series A's instances on their own");
    let items = raw.instances(&a, false)?;
    mv(&items[1], start + Duration::days(1) + Duration::hours(1))?;
    raw.patch(items[3]["id"].as_str().unwrap(), json!({"description": "edited on its own"}))?;
    mv(&items[4], start + Duration::days(1) + Duration::hours(6))?;
    mv(&items[5], start + Duration::days(5) + Duration::hours(6))?;
    show("After step 1 (series A's instance edits)", &a, &ROLES_A)?;

    println!("\n2. Delete series A from its \"cut-here\" event on (Recurrences.delete)");
    let left = recurrences.delete(&a, Some(items[2]["id"].as_str().unwrap()))?;
    println!("  returned: recurrence={:?}", left.and_then(|e| e.recurrence));
    show("After step 2 (series A, this and following deleted)", &a, &ROLES_A)?;

    println!("\n3. Edit one of series B's instances on its own, then delete series B whole");
    raw.patch(raw.instances(&b, false)?[1]["id"].as_str().unwrap(), json!({"description": "edited on its own"}))?;
    let left = recurrences.delete(&b, None)?;
    println!("  returned: {}", if left.is_some() { "an event" } else { "None" });
    show("After step 3 (series B deleted)", &b, &ROLES_B)?;
    Ok(())
}

// -- series_splits --------------------------------------------------------------

const RULE: &str = "RRULE:FREQ=WEEKLY;WKST=SU;BYDAY=MO,TU";

fn stamp(at: &Moment) -> String {
    at.with_timezone(&Utc).format("%Y%m%dT%H%M%S").to_string()
}

fn series_splits(raw: &Raw, _config: &Config) -> anyhow::Result<()> {
    let client = raw.client();
    let recurrences = recurrences_for(raw)?;
    let today = Utc::now().with_timezone(&zone()).date_naive();
    let monday = today + Duration::days(7 - today.weekday().num_days_from_monday() as i64);
    let first = zone().with_ymd_and_hms(monday.year(), monday.month(), monday.day(), 18, 30, 0).earliest().unwrap().fixed_offset();
    let made = std::cell::Cell::new(0i64);
    let start_of = |event: &Value| -> Moment { moment_of(event.get("originalStartTime").unwrap_or(&event["start"])).unwrap() };
    let few = |series_id: &str, deleted: bool| -> anyhow::Result<Vec<Value>> {
        let page = raw.call(
            "GET",
            &format!("/events/{}/instances", segment(series_id)),
            &[("maxResults", "8".into()), ("showDeleted", deleted.to_string())],
            None,
        )?;
        Ok(page["items"].as_array().cloned().unwrap_or_default())
    };
    let import_split = |series_id: &str, at: Moment, fields: Value| -> anyhow::Result<Value> {
        let mut body = json!({"summary": "Probe", "start": when(&at), "end": when(&(at + Duration::hours(1))), "recurrence": [RULE]});
        for (k, v) in fields.as_object().cloned().unwrap_or_default() {
            body[k] = v;
        }
        body["iCalUID"] = json!(format!("{}_R{}@google.com", series_id.split("_R").next().unwrap(), stamp(&at)));
        raw.import(body)
    };
    let google_split = |edited: bool| -> anyhow::Result<(String, Value)> {
        made.set(made.get() + 1);
        let start = first + Duration::weeks(made.get() * 4);
        let master = raw.insert(json!({"summary": "Probe", "start": when(&start), "end": when(&(start + Duration::hours(1))), "recurrence": [RULE]}))?;
        let master_id = master["id"].as_str().unwrap().to_string();
        let r_id = import_split(&master_id, start_of(&few(&master_id, false)?[2]), json!({}))?["id"].as_str().unwrap().to_string();
        let items = few(&r_id, false)?;
        println!("\n  made {r_id} (its events like {});", items[0]["id"].as_str().unwrap_or(""));
        println!("  the series it was split from now {}", raw.get(&master_id)?["recurrence"]);
        if edited {
            raw.patch(items[0]["id"].as_str().unwrap(), json!({"status": "cancelled"}))?;
            let moved = start_of(&items[1]) + Duration::hours(1);
            raw.patch(items[1]["id"].as_str().unwrap(), json!({"start": when(&moved), "end": when(&(moved + Duration::hours(1)))}))?;
            raw.patch(items[4]["id"].as_str().unwrap(), json!({"description": "edited on its own"}))?;
            println!("  cancelled its 1st event, moved its 2nd, described its 5th");
        }
        Ok((r_id, items[2].clone()))
    };
    let show = |series_id: &str, label: &str| -> anyhow::Result<()> {
        let series = raw.get(series_id)?;
        println!("  {label} {series_id}: {}, {}", series["status"].as_str().unwrap_or(""), series["recurrence"]);
        for item in few(series_id, true)? {
            let mut fields = vec![
                format!("orig={}", start_of(&item).with_timezone(&zone()).format("%m-%d %H:%M")),
                format!("start={}", item.pointer("/start/dateTime").and_then(Value::as_str).map(local).unwrap_or_else(|| "-".into())),
                item["status"].as_str().unwrap_or("").to_string(),
            ];
            if let Some(d) = item["description"].as_str() {
                fields.push(ctt::py::repr(d));
            }
            println!("     {}", fields.join("  "));
        }
        Ok(())
    };
    let show_later = |series_id: &str, at: Moment| -> anyhow::Result<()> {
        let uid = format!("{}_R{}@google.com", series_id.split("_R").next().unwrap(), stamp(&at));
        for item in raw.list(&[("iCalUID", uid)])? {
            if item.get("recurrence").is_some() {
                show(item["id"].as_str().unwrap(), "rest")?;
            }
        }
        Ok(())
    };
    let until = |item: &Value| (start_of(item) - Duration::seconds(1)).with_timezone(&Utc).format("%Y%m%dT%H%M%SZ").to_string();
    let attempt = |label: &str, write: &dyn Fn(&str, &Value) -> anyhow::Result<()>, edited: bool| -> anyhow::Result<()> {
        let (r_id, cut) = google_split(edited)?;
        println!("{label}");
        match write(&r_id, &cut) {
            Ok(()) => println!("  -> OK"),
            Err(e) => println!("  -> FAILED {} {e}", status_of(&e).map(|s| s.to_string()).unwrap_or_default()),
        }
        show(&r_id, "series")?;
        show_later(&r_id, start_of(&cut))
    };

    println!("\n1. End an \"_R\" series before its 3rd event");
    for edited in [false, true] {
        println!("\n===== {} =====", if edited { "Edited" } else { "Unedited" });
        attempt("a. patch: recurrence with UNTIL", &|r, cut| raw.patch(r, json!({"recurrence": [format!("{RULE};UNTIL={}", until(cut))]})).map(drop), edited)?;
        attempt(
            "b. patch: recurrence with UNTIL, plus start/end",
            &|r, cut| {
                let current = raw.get(r)?;
                raw.patch(r, json!({"recurrence": [format!("{RULE};UNTIL={}", until(cut))], "start": current["start"], "end": current["end"]})).map(drop)
            },
            edited,
        )?;
        attempt("c. patch: recurrence with COUNT=2", &|r, _| raw.patch(r, json!({"recurrence": [format!("{RULE};COUNT=2")]})).map(drop), edited)?;
        attempt("d. patch: recurrence unchanged", &|r, _| raw.patch(r, json!({"recurrence": [RULE]})).map(drop), edited)?;
        attempt("e. patch: summary only", &|r, _| raw.patch(r, json!({"summary": "renamed"})).map(drop), edited)?;
        attempt(
            "f. update (PUT): the whole series, with UNTIL",
            &|r, cut| {
                let mut current = raw.get(r)?;
                current["recurrence"] = json!([format!("{RULE};UNTIL={}", until(cut))]);
                raw.update(r, current).map(drop)
            },
            edited,
        )?;
        attempt(
            "g. import <base id>_R<3rd event> (a split, as in the UI), then import it again",
            &|r, cut| {
                for _ in 0..2 {
                    import_split(r, start_of(cut), json!({"colorId": "5"}))?;
                }
                Ok(())
            },
            edited,
        )?;
        attempt(
            "h. import its own UID with other rules",
            &|r, _| import_split(r, start_of(&raw.get(r)?), json!({"recurrence": ["RRULE:FREQ=WEEKLY;BYDAY=MO"]})).map(drop),
            edited,
        )?;
        attempt(
            "i. import <base id>_R<3rd event>, then cancel that",
            &|r, cut| {
                let rest = import_split(r, start_of(cut), json!({}))?;
                raw.patch(rest["id"].as_str().unwrap(), json!({"status": "cancelled"})).map(drop)
            },
            edited,
        )?;
    }

    println!("\n2. Recurrences on \"_R\" series (1st event cancelled, etc.)");
    println!("\na. split at its 3rd event");
    let (r_id, cut) = google_split(true)?;
    let (earlier, later) = recurrences.split(cut["id"].as_str().unwrap())?;
    if let Some(earlier) = earlier {
        show(earlier.id.as_deref().unwrap(), "earlier")?;
    }
    show(later.id.as_deref().unwrap(), "later")?;
    show_later(&r_id, start_of(&cut))?;

    println!("\nb. delete from its 3rd event on");
    let (r_id, cut) = google_split(true)?;
    recurrences.delete(&r_id, Some(cut["id"].as_str().unwrap()))?;
    show(&r_id, "series")?;
    show_later(&r_id, start_of(&cut))?;

    println!("\nc. move the whole series' start 30 minutes later (no rules)");
    let (r_id, _cut) = google_split(true)?;
    let series = client.get_event(&r_id)?;
    for minutes in [30i64, 0] {
        let moved = Event {
            id: Some(r_id.clone()),
            start: Some(series.start() + Duration::minutes(minutes)),
            end: Some(series.end() + Duration::minutes(30)),
            ..Default::default()
        };
        match recurrences.update(&moved, None, None) {
            Ok(_) => println!("  start +{minutes}m, end +30m -> OK"),
            Err(e) => println!("  start +{minutes}m, end +30m -> FAILED: {e}"),
        }
    }
    show(&r_id, "series")?;

    println!("\nd. change the whole series' rules");
    let (r_id, _cut) = google_split(true)?;
    let weekly = Repeat::from_value(&json!({"every": "week", "weekdays": ["mon"]}))?;
    match recurrences.update(&Event { id: Some(r_id.clone()), ..Default::default() }, None, Some(&weekly)) {
        Ok(_) => println!("  -> OK"),
        Err(e) => println!("  -> refused: {e}"),
    }
    show(&r_id, "series")?;

    println!("\ne. a split undone (its copy cancelled), then retried");
    let (r_id, cut) = google_split(false)?;
    let at = start_of(&cut);
    let copy = client.create_event(&Event {
        id: Some(split_series_id(&r_id, &at)),
        summary: Some("Probe".into()),
        start: Some(at),
        end: Some(at + Duration::hours(1)),
        time_zone: Some(TIME_ZONE.into()),
        recurrence: Some(vec![RULE.into()]),
        ..Default::default()
    })?;
    let copy_id = copy.id.clone().unwrap();
    raw.patch(&copy_id, json!({"status": "cancelled"}))?;
    println!("  copy {copy_id} cancelled: {} instances listed", few(&copy_id, false)?.len());
    let (earlier, later) = recurrences.split(cut["id"].as_str().unwrap())?;
    if let Some(earlier) = earlier {
        show(earlier.id.as_deref().unwrap(), "earlier")?;
    }
    show(later.id.as_deref().unwrap(), "later")?;
    Ok(())
}
