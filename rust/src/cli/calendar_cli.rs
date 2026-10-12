//! Command-line utilities for the Google Calendar API, over the server's
//! own clients and stores (calendar_cli.py, ported). A dev tool, not
//! something the deployed server needs: the calendar_cli binary runs it.
//!
//! Usage:
//!     calendar_cli list [from] [to]
//!     calendar_cli get <id>
//!     calendar_cli update_properties <id> key=value [key=value ...]
//!     calendar_cli update <id> key=value [key=value ...]
//!     calendar_cli create key=value [key=value ...]
//!     calendar_cli delete <id>
//!     calendar_cli list_raw_labels
//!     calendar_cli create_raw_label key=value [key=value ...]
//!     calendar_cli update_raw_label <label_id> key=value [key=value ...]
//!     calendar_cli delete_raw_label <label_id>
//!     calendar_cli list_actions [--status status ...] [--all]
//!     calendar_cli create_action key=value [key=value ...]
//!     calendar_cli update_action <action_id> [key=value ...] [--clear attribute ...]
//!     calendar_cli note <ago> [description]
//!     calendar_cli get_notes
//!     calendar_cli edit_note <note_id> [--ago <ago> | --at <time>] [--description <text>]
//!     calendar_cli delete_note <note_id>
//!
//! See ../README.md ("calendar_cli") for what each does. Durations (`list`'s
//! from/to, `note`'s ago) are pytimeparse's: "1h", "90m", "2d", "1:30".

use chrono::{DateTime, FixedOffset, Local, NaiveDateTime, TimeZone};
use crate::app::{App, Config};
use crate::compaction::compactor::{compaction_error, delete_note, edit_note};
use crate::event::{parse_iso, Event};
use crate::facts::Facts;
use crate::google::calendar::EventLabel;
use crate::py;
use crate::sheet::rows::Row;
use crate::stores::action_calendar::ActionCalendar;
use crate::stores::actions::{ACTION_ROW, ACTION_STATUSES, CLEARABLE_FIELDS};
use crate::stores::event_changes::EventChanges;
use crate::stores::groups::python_repr;
use crate::stores::notes::NotedTime;
use crate::write_lock::WRITE_LOCK;
use serde_json::{json, Value};
use std::io::Write;
use std::sync::Arc;

macro_rules! wln {
    ($out:expr, $($arg:tt)*) => {
        writeln!($out, $($arg)*).expect("writing the output")
    };
}

pub type Moment = DateTime<FixedOffset>;

const DEFAULT_WINDOW: &str = "1h";
const EVENT_ATTRIBUTES: [&str; 12] = [
    "action_ids", "description", "end", "event_label_id", "facts", "is_end_of_day_sleep", "judgments", "location", "priority", "start",
    "status", "summary",
];
const RAW_LABEL_ATTRIBUTES: [&str; 2] = ["background_color", "name"];
const ACTION_ATTRIBUTES: [&str; 6] = ["background_color", "group_id", "name", "note", "priority", "status"];

/// A mistake in how it was called: printed with the usage, exit status 2
/// (argparse's).
pub struct Usage(pub String);

pub fn main() {
    crate::dotenv::load(std::path::Path::new(".env"));
    let args: Vec<String> = std::env::args().skip(1).collect();
    match run(&args) {
        Ok(()) => {}
        Err(Failure::Usage(Usage(message))) => {
            eprintln!("usage: calendar_cli <command> [args] (see --help)\ncalendar_cli: error: {message}");
            std::process::exit(2);
        }
        Err(Failure::Exit(message)) => {
            eprintln!("{message}");
            std::process::exit(1);
        }
        Err(Failure::Error(error)) => {
            eprintln!("{error:#}");
            std::process::exit(1);
        }
    }
}

pub enum Failure {
    Usage(Usage),
    Exit(String),
    Error(anyhow::Error),
}

impl From<anyhow::Error> for Failure {
    fn from(error: anyhow::Error) -> Failure {
        Failure::Error(error)
    }
}

impl From<Usage> for Failure {
    fn from(usage: Usage) -> Failure {
        Failure::Usage(usage)
    }
}

fn usage(message: impl Into<String>) -> Failure {
    Failure::Usage(Usage(message.into()))
}

// -- parsing ------------------------------------------------------------------

/// `pytimeparse.parse`: a duration, in seconds -- "1h", "90m", "2d",
/// "1h30m", "2 hours", "1:30" (minutes and seconds), "1:30:00", "-1h".
/// A bare number isn't one (as in pytimeparse).
pub fn parse_duration(text: &str) -> Option<f64> {
    let text = text.trim();
    let (sign, rest) = match text.strip_prefix('-') {
        Some(rest) => (-1.0, rest.trim_start()),
        None => (1.0, text.strip_prefix('+').unwrap_or(text).trim_start()),
    };
    let clock = regex::Regex::new(r"^(\d+):(\d{2}(?:\.\d+)?)$|^(?:(\d+)\s*(?:d|dys?|days?),?\s*)?(\d+):(\d{2})(?::(\d{2}(?:\.\d+)?))?$").unwrap();
    if let Some(c) = clock.captures(rest) {
        let num = |i: usize| c.get(i).map(|m| m.as_str().parse::<f64>().unwrap_or(0.0));
        // Minutes and seconds; else hours, minutes and maybe seconds,
        // after any days.
        let seconds = if c.get(1).is_some() {
            num(1).unwrap() * 60.0 + num(2).unwrap()
        } else {
            num(3).unwrap_or(0.0) * 86400.0 + num(4).unwrap() * 3600.0 + num(5).unwrap() * 60.0 + num(6).unwrap_or(0.0)
        };
        return Some(sign * seconds);
    }
    let unit = regex::Regex::new(
        r"^\s*,?\s*(\d+(?:\.\d+)?|\.\d+)\s*(weeks?|wks?|w|days?|dys?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)",
    )
    .unwrap();
    let mut rest = rest;
    let mut total = 0.0;
    let mut matched = false;
    while !rest.trim().is_empty() {
        let c = unit.captures(rest)?;
        let amount: f64 = c[1].parse().ok()?;
        let scale = match c[2].chars().next().unwrap() {
            'w' => 604800.0,
            'd' => 86400.0,
            'h' => 3600.0,
            'm' => 60.0,
            _ => 1.0,
        };
        total += amount * scale;
        matched = true;
        rest = &rest[c.get(0).unwrap().end()..];
    }
    matched.then_some(sign * total)
}

fn duration(text: &str) -> Result<f64, Failure> {
    parse_duration(text).ok_or_else(|| usage(format!("could not parse duration: {}", py::repr(text))))
}

fn parse_bool(value: &str) -> Result<bool, String> {
    match value.to_lowercase().as_str() {
        "true" | "1" | "yes" => Ok(true),
        "false" | "0" | "no" => Ok(false),
        _ => Err(format!("could not parse boolean: {}", py::repr(value))),
    }
}

fn parse_iso_datetime(value: &str) -> Result<Moment, String> {
    parse_iso(value, None).map_err(|_| match parse_iso(value, Some("UTC")) {
        Ok(_) => format!("datetime {} must include a UTC offset/timezone", py::repr(value)),
        Err(e) => e.to_string(),
    })
}

/// An ISO 8601 time; one without a UTC offset is taken as local time.
fn parse_time(value: &str) -> Result<Moment, Failure> {
    if let Ok(moment) = parse_iso(value, None) {
        return Ok(moment);
    }
    let naive = NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M:%S%.f")
        .or_else(|_| NaiveDateTime::parse_from_str(value, "%Y-%m-%dT%H:%M"))
        .or_else(|_| NaiveDateTime::parse_from_str(value, "%Y-%m-%d %H:%M:%S%.f"))
        .map_err(|_| usage(format!("{} isn't an ISO 8601 time", py::repr(value))))?;
    Local.from_local_datetime(&naive).earliest().map(|t| t.fixed_offset()).ok_or_else(|| usage(format!("{} isn't a time here", py::repr(value))))
}

fn parse_facts(value: &str) -> Result<Facts, String> {
    Facts::from_json(value)
        .map(|f| f.normalized())
        .ok_or_else(|| format!("facts must be a JSON object, e.g. '{{\"with\":[\"p3x9aa\"]}}', not {}", py::repr(value)))
}

fn split_pair<'a>(value: &'a str, valid: &[&str]) -> Result<(&'a str, &'a str), Failure> {
    let (key, raw) = value.split_once('=').ok_or_else(|| usage(format!("expected key=value, got {}", py::repr(value))))?;
    if !valid.contains(&key) {
        let mut sorted = valid.to_vec();
        sorted.sort();
        return Err(usage(format!("unknown attribute {}; expected one of: {}", py::repr(key), sorted.join(", "))));
    }
    Ok((key, raw))
}

fn invalid(key: &str, message: String) -> Failure {
    usage(format!("invalid value for {}: {message}", py::repr(key)))
}

/// Set an Event attribute from its command-line text.
fn set_event_attribute(event: &mut Event, pair: &str) -> Result<String, Failure> {
    let (key, raw) = split_pair(pair, &EVENT_ATTRIBUTES)?;
    match key {
        "summary" => event.summary = Some(raw.into()),
        "start" => event.start = Some(parse_iso_datetime(raw).map_err(|e| invalid(key, e))?),
        "end" => event.end = Some(parse_iso_datetime(raw).map_err(|e| invalid(key, e))?),
        "description" => event.description = Some(raw.into()),
        "location" => event.location = Some(raw.into()),
        "status" => event.status = Some(raw.into()),
        "priority" => {
            event.priority =
                Some(raw.trim().parse().map_err(|_| invalid(key, format!("invalid literal for int() with base 10: {}", py::repr(raw))))?)
        }
        "is_end_of_day_sleep" => event.is_end_of_day_sleep = Some(parse_bool(raw).map_err(|e| invalid(key, e))?),
        "event_label_id" => event.event_label_id = Some(raw.into()),
        "action_ids" => event.action_ids = Some(raw.split(',').map(str::trim).filter(|a| !a.is_empty()).map(str::to_string).collect()),
        "facts" => event.facts = Some(parse_facts(raw).map_err(|e| invalid(key, e))?),
        _ => {
            let value: Value = serde_json::from_str(raw).map_err(|e| invalid(key, e.to_string()))?;
            event.judgments = Some(value.as_object().cloned().ok_or_else(|| invalid(key, "judgments must be a JSON object".into()))?);
        }
    }
    Ok(key.to_string())
}

fn set_action_attribute(row: &mut Row, pair: &str) -> Result<(), Failure> {
    let (key, raw) = split_pair(pair, &ACTION_ATTRIBUTES)?;
    let value = match key {
        "priority" => json!(raw.trim().parse::<i64>().map_err(|_| invalid(key, format!("invalid literal for int() with base 10: {}", py::repr(raw))))?),
        "status" if !ACTION_STATUSES.contains(&raw) => return Err(invalid(key, format!("expected one of {}", ACTION_STATUSES.join(", ")))),
        _ => json!(raw),
    };
    row.insert(key.into(), value);
    Ok(())
}

// -- formatting ---------------------------------------------------------------

/// `str()` of a datetime: its isoformat with a space.
fn str_datetime(moment: &Moment) -> String {
    py::isoformat(moment).replacen('T', " ", 1)
}

fn str_opt_list(items: &Option<Vec<String>>) -> Option<String> {
    items.as_ref().map(|i| py::repr_list(i))
}

fn facts_repr(facts: &Facts) -> String {
    let field = |v: &Option<Value>| v.as_ref().map(python_repr).unwrap_or_else(|| "None".into());
    format!(
        "Facts(location_id={}, with_ids={}, for_ids={}, notes={})",
        field(&facts.location_id),
        field(&facts.with_ids),
        field(&facts.for_ids),
        field(&facts.notes)
    )
}

/// `_format_event_details`: each field that's set, as `name: value`.
fn event_details(event: &Event) -> String {
    let mut lines: Vec<String> = Vec::new();
    let mut put = |name: &str, value: Option<String>| {
        if let Some(v) = value {
            lines.push(format!("{name}: {v}"));
        }
    };
    put("id", event.id.clone());
    put("summary", event.summary.clone());
    put("start", event.start.as_ref().map(str_datetime));
    put("end", event.end.as_ref().map(str_datetime));
    put("description", event.description.clone());
    put("location", event.location.clone());
    put("status", event.status.clone());
    put("recurring_event_id", event.recurring_event_id.clone());
    put("recurrence", str_opt_list(&event.recurrence));
    put("time_zone", event.time_zone.clone());
    put("original_start", event.original_start.as_ref().map(str_datetime));
    put("priority", event.priority.map(|p| p.to_string()));
    put("is_end_of_day_sleep", event.is_end_of_day_sleep.map(|b| if b { "True" } else { "False" }.to_string()));
    put("event_label_id", event.event_label_id.clone());
    put("action_ids", str_opt_list(&event.action_ids));
    put("facts", event.facts.as_ref().map(facts_repr));
    put("judgments", event.judgments.as_ref().map(|j| python_repr(&Value::Object(j.clone()))));
    put("action_priority", event.action_priority.map(|p| p.to_string()));
    put("actions_from_label", Some(if event.actions_from_label { "True" } else { "False" }.to_string()));
    let cleared: Vec<&String> = event.cleared.iter().collect();
    put("cleared", Some(if cleared.is_empty() { "frozenset()".into() } else { format!("frozenset({{{}}})", cleared.iter().map(|c| py::repr(c)).collect::<Vec<_>>().join(", ")) }));
    lines.join("\n")
}

fn label_details(label: &EventLabel) -> String {
    let mut lines = Vec::new();
    if let Some(id) = &label.id {
        lines.push(format!("id: {id}"));
    }
    lines.push(format!("background_color: {}", label.background_color));
    if let Some(name) = &label.name {
        lines.push(format!("name: {name}"));
    }
    lines.join("\n")
}

fn note_details(note: &NotedTime) -> String {
    let mut lines = vec![format!("timestamp: {}", str_datetime(&note.timestamp))];
    if let Some(d) = &note.description {
        lines.push(format!("description: {d}"));
    }
    if let Some(c) = &note.compaction_id {
        lines.push(format!("compaction_id: {c}"));
    }
    lines.join("\n")
}

fn print_actions(out: &mut dyn Write, listed: &Value, key: &str, none: &str) {
    let actions = listed[key].as_array().cloned().unwrap_or_default();
    if actions.is_empty() {
        wln!(out, "{none}");
    }
    for a in &actions {
        let text = |k: &str| crate::stores::groups::python_str(a.get(k));
        wln!(out, "{}\t{}\t{}", text("id"), text("status"), text("path"));
    }
    wln!(out, "({} of {} event labels in use)", listed["label_slots_used"], listed["label_slots_total"]);
}

// -- the commands -------------------------------------------------------------

fn positional<'a>(args: &'a [String], index: usize, name: &str) -> Result<&'a str, Failure> {
    args.get(index).map(String::as_str).ok_or_else(|| usage(format!("the following arguments are required: {name}")))
}

fn run(args: &[String]) -> Result<(), Failure> {
    if matches!(args.first().map(String::as_str), Some("-h" | "--help")) {
        println!("{}", include_str!("calendar_cli_help.txt"));
        return Ok(());
    }
    let app = Arc::new(App::new(Config::from_env()));
    app.set_self();
    run_with(args, &app, crate::determinism::now(), &mut std::io::stdout())
}

/// The command `args` names, on `app`, as of `now`, printing to `out`.
pub fn run_with(args: &[String], app: &Arc<App>, now: Moment, out: &mut dyn Write) -> Result<(), Failure> {
    let Some(command) = args.first() else { return Err(usage("the following arguments are required: command")) };
    let rest = &args[1..];
    let _held = WRITE_LOCK.hold();
    match command.as_str() {
        "list" => {
            let from = duration(rest.first().map(String::as_str).unwrap_or(DEFAULT_WINDOW))?;
            let to = duration(rest.get(1).map(String::as_str).unwrap_or(DEFAULT_WINDOW))?;
            let seconds = |s: f64| chrono::Duration::milliseconds((s * 1000.0).round() as i64);
            let events = app.calendar_client()?.list_events(&(now - seconds(from)), &(now + seconds(to)))?;
            if events.is_empty() {
                wln!(out, "No events found.");
            }
            for e in events {
                wln!(out, 
                    "{}\t{} - {}\t{}",
                    e.id.clone().unwrap_or_default(),
                    e.start.map(|s| py::isoformat(&s)).unwrap_or_default(),
                    e.end.map(|s| py::isoformat(&s)).unwrap_or_default(),
                    e.summary.clone().unwrap_or_else(|| "None".into())
                );
            }
        }
        "get" => wln!(out, "{}", event_details(&app.calendar_client()?.get_event(positional(rest, 0, "id")?)?)),
        "update_properties" => {
            let id = positional(rest, 0, "id")?;
            if rest.len() < 2 {
                return Err(usage("the following arguments are required: key=value"));
            }
            let mut event = Event { id: Some(id.into()), ..Default::default() };
            for pair in &rest[1..] {
                set_event_attribute(&mut event, pair)?;
            }
            // Setting actions also sets the label they imply.
            let updated = if event.action_ids.is_some() {
                ActionCalendar::new(app.calendar_client()?, app.actions()?).update_event(&event)?
            } else {
                app.calendar_client()?.update_event(&event)?
            };
            wln!(out, "{}", event_details(&updated));
        }
        "update" | "create" => {
            let mut event = Event::default();
            let pairs = if command == "update" {
                event.id = Some(positional(rest, 0, "id")?.into());
                &rest[1..]
            } else {
                rest
            };
            if pairs.is_empty() {
                return Err(usage("the following arguments are required: key=value"));
            }
            let mut given: Vec<String> = Vec::new();
            for pair in pairs {
                given.push(set_event_attribute(&mut event, pair)?);
            }
            let changes = EventChanges::new(ActionCalendar::new(app.calendar_client()?, app.actions()?), None);
            let checked = if command == "update" {
                if !given.iter().any(|g| g == "start" || g == "end") {
                    return Err(usage("update requires at least one of: end, start"));
                }
                changes.check(vec![event], Vec::new(), Vec::new(), Vec::new(), None, false)?
            } else {
                let mut missing: Vec<&str> = ["end", "start", "summary"].into_iter().filter(|r| !given.iter().any(|g| g == r)).collect();
                missing.sort();
                if !missing.is_empty() {
                    return Err(usage(format!("create requires: {}", missing.join(", "))));
                }
                changes.check(Vec::new(), vec![event], Vec::new(), Vec::new(), None, false)?
            };
            let batch = checked.map_err(|refusal| Failure::Exit(refusal.message))?;
            for written in changes.apply(&batch, "calendar_cli")? {
                wln!(out, "{}\n", event_details(&written));
            }
        }
        "delete" => {
            let id = positional(rest, 0, "id")?;
            app.calendar_client()?.delete_event(id)?;
            wln!(out, "Deleted event {id}.");
        }
        "list_raw_labels" => {
            let (labels, _etag) = app.calendar_client()?.list_event_labels()?;
            if labels.is_empty() {
                wln!(out, "No event labels found.");
            }
            for l in labels {
                wln!(out, "{}\t{}\t{}", l.id.clone().unwrap_or_else(|| "None".into()), l.background_color, l.name.clone().unwrap_or_default());
            }
        }
        "create_raw_label" | "update_raw_label" => {
            let (label_id, pairs) = if command == "update_raw_label" { (Some(positional(rest, 0, "label_id")?), &rest[1..]) } else { (None, rest) };
            if pairs.is_empty() {
                return Err(usage("the following arguments are required: key=value"));
            }
            let (mut color, mut name) = (None, None);
            for pair in pairs {
                let (key, raw) = split_pair(pair, &RAW_LABEL_ATTRIBUTES)?;
                if key == "name" {
                    name = Some(raw.to_string());
                } else {
                    color = Some(raw.to_string());
                }
            }
            let client = app.calendar_client()?;
            let label = match label_id {
                Some(id) => client.update_event_label(id, color.as_deref(), name.as_deref())?,
                None => {
                    let color = color.ok_or_else(|| usage("create_raw_label requires: background_color"))?;
                    client.create_event_label(&color, name.as_deref())?
                }
            };
            wln!(out, "{}", label_details(&label));
        }
        "delete_raw_label" => {
            let label = app.calendar_client()?.delete_event_label(positional(rest, 0, "label_id")?)?;
            wln!(out, "Deleted event label {}.", label.id.unwrap_or_default());
        }
        "list_actions" => {
            let mut statuses: Vec<String> = Vec::new();
            let mut all = false;
            let mut i = 0;
            while i < rest.len() {
                match rest[i].as_str() {
                    "--all" => all = true,
                    "--status" => {
                        let s = rest.get(i + 1).ok_or_else(|| usage("argument --status: expected one argument"))?;
                        if !ACTION_STATUSES.contains(&s.as_str()) {
                            return Err(usage(format!("argument --status: invalid choice: {} (choose from {})", py::repr(s), ACTION_STATUSES.map(py::repr).join(", "))));
                        }
                        statuses.push(s.clone());
                        i += 1;
                    }
                    other => return Err(usage(format!("unrecognized arguments: {other}"))),
                }
                i += 1;
            }
            let chosen: Option<Vec<String>> = if all { Some(ACTION_STATUSES.iter().map(|s| s.to_string()).collect()) } else if statuses.is_empty() { None } else { Some(statuses) };
            let listed = app.actions()?.get_actions(chosen.as_deref()).map_err(|e| Failure::Exit(format!("error: {e}")))?;
            print_actions(out, &listed, "actions", "No actions found.");
        }
        "create_action" | "update_action" => {
            let mut row: Row = ACTION_ROW.empty();
            let mut clear: Vec<String> = Vec::new();
            let pairs = if command == "update_action" {
                row.insert("id".into(), json!(positional(rest, 0, "action_id")?));
                &rest[1..]
            } else {
                rest
            };
            let mut i = 0;
            let mut any = false;
            while i < pairs.len() {
                if pairs[i] == "--clear" && command == "update_action" {
                    let field = pairs.get(i + 1).ok_or_else(|| usage("argument --clear: expected one argument"))?;
                    if !CLEARABLE_FIELDS.contains(&field.as_str()) {
                        let mut choices = CLEARABLE_FIELDS.to_vec();
                        choices.sort();
                        return Err(usage(format!("argument --clear: invalid choice: {} (choose from {})", py::repr(field), choices.into_iter().map(py::repr).collect::<Vec<_>>().join(", "))));
                    }
                    clear.push(field.clone());
                    i += 2;
                    continue;
                }
                set_action_attribute(&mut row, &pairs[i])?;
                any = true;
                i += 1;
            }
            let actions = app.actions()?;
            let changed = if command == "create_action" {
                if !any {
                    return Err(usage("the following arguments are required: key=value"));
                }
                actions.create_action(&row)
            } else {
                if !any && clear.is_empty() {
                    return Err(usage("update_action requires at least one key=value or --clear"));
                }
                actions.update_action(&row, &clear)
            };
            let changed = changed.map_err(|e| if crate::google::status_of(&e).is_some() { Failure::Error(e) } else { Failure::Exit(format!("error: {e}")) })?;
            print_actions(out, &changed, "changed", "No actions changed.");
        }
        "note" => {
            let ago = duration(positional(rest, 0, "ago")?)?;
            let timestamp = now - chrono::Duration::milliseconds((ago * 1000.0).round() as i64);
            let noted = NotedTime { timestamp, description: rest.get(1).cloned(), compaction_id: None };
            let appended = app.noted_time_sheet()?.append(&noted)?;
            wln!(out, "id: {}", appended.id());
            wln!(out, "{}", note_details(&noted));
        }
        "get_notes" => {
            let mut notes = app.noted_time_sheet()?.read_with_rows(false)?;
            notes.sort_by_key(|n| n.note.timestamp);
            if notes.is_empty() {
                wln!(out, "No notes found.");
            }
            for n in notes {
                wln!(out, "{}\t{}\t{}", n.id(), py::isoformat(&n.note.timestamp), n.note.description.clone().unwrap_or_default());
            }
        }
        "edit_note" => {
            let note_id = positional(rest, 0, "note_id")?;
            let (mut timestamp, mut description, mut i) = (None, None, 1);
            let mut by: Option<&str> = None;
            while i < rest.len() {
                let value = rest.get(i + 1).ok_or_else(|| usage(format!("argument {}: expected one argument", rest[i])))?;
                match rest[i].as_str() {
                    flag @ ("--ago" | "--at") => {
                        if let Some(other) = by {
                            return Err(usage(format!("argument {flag}: not allowed with argument {other}")));
                        }
                        by = Some(if flag == "--ago" { "--ago" } else { "--at" });
                        timestamp = Some(if flag == "--ago" {
                            now - chrono::Duration::milliseconds((duration(value)? * 1000.0).round() as i64)
                        } else {
                            parse_time(value)?
                        });
                    }
                    "--description" => description = Some(value.clone()),
                    other => return Err(usage(format!("unrecognized arguments: {other}"))),
                }
                i += 2;
            }
            if timestamp.is_none() && description.is_none() {
                return Err(usage("edit_note needs at least one of --ago, --at or --description"));
            }
            let edited = edit_note(&app.noted_time_sheet()?, &app.journal()?, note_id, timestamp, description.as_deref())
                .map_err(|e| refused(e))?;
            wln!(out, "id: {}", edited.id());
            wln!(out, "{}", note_details(&edited.note));
        }
        "delete_note" => {
            let deleted = delete_note(&app.noted_time_sheet()?, &app.journal()?, positional(rest, 0, "note_id")?).map_err(refused)?;
            wln!(out, "Deleted:");
            wln!(out, "{}", note_details(&deleted));
        }
        other => return Err(usage(format!("argument command: invalid choice: {}", py::repr(other)))),
    }
    Ok(())
}

/// A CompactionError, as `sys.exit(f"error: {exc}")`; anything else as is.
fn refused(error: anyhow::Error) -> Failure {
    match compaction_error(&error) {
        Some(c) => Failure::Exit(format!("error: {}", c.message)),
        None => Failure::Error(error),
    }
}

#[cfg(test)]
#[path = "calendar_cli_tests.rs"]
mod command_tests;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn durations_as_pytimeparse_reads_them() {
        for (text, seconds) in [
            ("1h", Some(3600.0)),
            ("90m", Some(5400.0)),
            ("2d", Some(172800.0)),
            ("1:30", Some(90.0)),
            ("1:30:00", Some(5400.0)),
            ("0s", Some(0.0)),
            ("45", None),
            ("1.5h", Some(5400.0)),
            ("1h30m", Some(5400.0)),
            ("1h 30m", Some(5400.0)),
            ("2 hours", Some(7200.0)),
            ("3 mins", Some(180.0)),
            ("1w", Some(604800.0)),
            ("1w2d", Some(777600.0)),
            ("-1h", Some(-3600.0)),
            ("1 day, 2:00:00", Some(93600.0)),
            ("1d2h3m4s", Some(93784.0)),
            ("2 hrs 5 mins", Some(7500.0)),
            ("x", None),
            ("1hr", Some(3600.0)),
            ("10sec", Some(10.0)),
            ("1.5", None),
            (".5h", Some(1800.0)),
            ("1:30.5", Some(90.5)),
            ("+1h", Some(3600.0)),
        ] {
            assert_eq!(parse_duration(text), seconds, "{text}");
        }
    }

    #[test]
    fn event_attributes_parsed_as_python_does() {
        let mut event = Event::default();
        set_event_attribute(&mut event, "action_ids= a, ,b").ok().unwrap();
        assert_eq!(event.action_ids, Some(vec!["a".to_string(), "b".to_string()]));
        set_event_attribute(&mut event, "is_end_of_day_sleep=Yes").ok().unwrap();
        assert_eq!(event.is_end_of_day_sleep, Some(true));
        set_event_attribute(&mut event, "start=2026-01-01T09:00:00Z").ok().unwrap();
        assert_eq!(str_datetime(&event.start.unwrap()), "2026-01-01 09:00:00+00:00");
        assert!(set_event_attribute(&mut event, "start=2026-01-01T09:00:00").is_err());
        assert!(set_event_attribute(&mut event, "colour=red").is_err());
        assert!(set_event_attribute(&mut event, "facts=[]").is_err());
        set_event_attribute(&mut event, r#"facts={"with":[" p1 ","p1"],"location":"l"}"#).ok().unwrap();
        assert_eq!(facts_repr(event.facts.as_ref().unwrap()), "Facts(location_id='l', with_ids=['p1'], for_ids=None, notes=None)");
    }

    #[test]
    fn details_as_python_prints_them() {
        let event = Event {
            id: Some("abc123".into()),
            summary: Some("Focus block".into()),
            start: Some(parse_iso("2026-01-01T09:00:00+00:00", None).unwrap()),
            priority: Some(2),
            action_ids: Some(vec!["x".into()]),
            ..Default::default()
        };
        assert_eq!(
            event_details(&event),
            "id: abc123\nsummary: Focus block\nstart: 2026-01-01 09:00:00+00:00\npriority: 2\naction_ids: ['x']\nactions_from_label: False\ncleared: frozenset()"
        );
    }
}
