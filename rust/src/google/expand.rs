//! Recurring series expanded into their instances, as Google expands them
//! for a listing with singleEvents=true -- from what a listing with
//! singleEvents=false gives: each series' master, its instances edited on
//! their own (exceptions, cancelled ones included), and events on their
//! own. The calendar cache (`synced`) keeps that, and lists from it; the
//! fake Google lists through it too.
//!
//! As the probes found Google doing (see probe.rs: local_expansion,
//! instance_cap): an instance is its master -- the same etag, summary,
//! description, properties -- at its own start ("<master id>_<start in
//! UTC>", or "_<date>" for an all-day series), unless it was edited on its
//! own -- "<base>_<start>", where a series Google split itself
//! ("<base>_R<time>") names its instances after the series it was split
//! from; a series runs on the wall clock of its start's time zone; and
//! Google expands at most INSTANCE_CAP instances of a series, counted from
//! its start, so none past those exist.

use chrono::{DateTime, Duration, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Utc};
use chrono_tz::Tz;
use serde_json::{json, Map, Value};
use std::collections::HashMap;

/// How many instances of a series Google expands, counted from its start.
pub const INSTANCE_CAP: usize = 730;

/// When an event starts or ends: a moment, or (all day) a date.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum When {
    At(DateTime<Utc>),
    Day(NaiveDate),
}

impl When {
    /// The moment, a date taken as its midnight in `zone`.
    pub fn key(&self, zone: Tz) -> DateTime<Utc> {
        match self {
            When::At(m) => *m,
            When::Day(d) => zone.from_local_datetime(&d.and_time(NaiveTime::MIN)).earliest().map_or_else(|| Utc.from_utc_datetime(&d.and_time(NaiveTime::MIN)), |m| m.with_timezone(&Utc)),
        }
    }

    /// What an instance's id ends with, after the master's and "_".
    pub fn suffix(&self) -> String {
        match self {
            When::At(m) => m.format("%Y%m%dT%H%M%SZ").to_string(),
            When::Day(d) => d.format("%Y%m%d").to_string(),
        }
    }
}

pub fn zone_of(name: Option<&str>) -> Tz {
    name.and_then(|n| n.parse().ok()).unwrap_or(chrono_tz::UTC)
}

/// An API start or end ({"dateTime"} or {"date"}), a time without an
/// offset taken in its own "timeZone", else `zone`.
pub fn parse_moment(value: &Value, zone: Tz) -> Option<When> {
    if let (Some(day), None) = (value.get("date").and_then(Value::as_str), value.get("dateTime")) {
        return NaiveDate::parse_from_str(day, "%Y-%m-%d").ok().map(When::Day);
    }
    let raw = value.get("dateTime").and_then(Value::as_str)?.replace('Z', "+00:00");
    if let Ok(m) = DateTime::parse_from_rfc3339(&raw) {
        return Some(When::At(m.with_timezone(&Utc)));
    }
    let naive = NaiveDateTime::parse_from_str(&raw, "%Y-%m-%dT%H:%M:%S").ok()?;
    let zone = value.get("timeZone").and_then(Value::as_str).map(|z| zone_of(Some(z))).unwrap_or(zone);
    Some(When::At(zone.from_local_datetime(&naive).earliest()?.with_timezone(&Utc)))
}

/// `when` as the API gives it: a moment at `zone`'s offset (the
/// calendar's, whatever zone the event is in), with "timeZone" when it has
/// one.
pub fn render_moment(when: When, zone: Tz, time_zone: Option<&str>) -> Value {
    match when {
        When::At(m) => {
            let local = m.with_timezone(&zone);
            let mut text = local.format("%Y-%m-%dT%H:%M:%S%:z").to_string();
            if let Some(stripped) = text.strip_suffix("+00:00") {
                text = format!("{stripped}Z");
            }
            let mut out = json!({"dateTime": text});
            if let Some(z) = time_zone {
                out["timeZone"] = json!(z);
            }
            out
        }
        When::Day(d) => json!({"date": d.format("%Y-%m-%d").to_string()}),
    }
}

/// The id a series' instances are named after: its own, or for a series
/// Google split itself ("<base>_R<yyyymmdd>T<hhmmss>"), the base's.
pub fn instance_base(master_id: &str) -> &str {
    if let Some((base, split)) = master_id.rsplit_once("_R") {
        let digits = |t: &str| t.chars().all(|c| c.is_ascii_digit());
        if split.len() == 15 && split.as_bytes()[8] == b'T' && digits(&split[..8]) && digits(&split[9..]) {
            return base;
        }
    }
    master_id
}

/// The id of instance `original` of series `master_id`.
pub fn instance_id(master_id: &str, original: When) -> String {
    format!("{}_{}", instance_base(master_id), original.suffix())
}

/// Whether `event` is a series' master: it has a recurrence rule.
pub fn is_master(event: &Value) -> bool {
    event.get("recurrence").and_then(Value::as_array).is_some_and(|r| !r.is_empty())
}

/// Each instance of series `master` that Google has -- the first
/// INSTANCE_CAP -- by original start, up to (not at) `until`, in `zone`
/// (the calendar's). Empty for a rule it can't read.
pub fn occurrences(zone: Tz, master: &Value, until: Option<DateTime<Utc>>) -> Vec<When> {
    let Some(start) = master.get("start").and_then(|s| parse_moment(s, zone)) else { return Vec::new() };
    let lines: Vec<String> = master["recurrence"].as_array().map(|a| a.iter().filter_map(|l| l.as_str().map(str::to_string)).collect()).unwrap_or_default();
    let Some(rule) = lines.iter().find(|l| l.starts_with("RRULE:")) else { return vec![start] };
    let parts: Vec<(String, String)> =
        rule["RRULE:".len()..].split(';').filter(|p| !p.is_empty()).filter_map(|p| p.split_once('=').map(|(k, v)| (k.to_string(), v.to_string()))).collect();
    let all_day = matches!(start, When::Day(_));
    let local_zone = master["start"].get("timeZone").and_then(Value::as_str).map(|z| zone_of(Some(z))).unwrap_or(zone);
    let naive_start = match start {
        When::Day(d) => d.and_time(NaiveTime::MIN),
        When::At(m) => m.with_timezone(&local_zone).naive_local(),
    };
    let mut rule_until: Option<DateTime<Utc>> = None;
    for (k, v) in &parts {
        if k == "UNTIL" {
            rule_until = until_of(v, local_zone);
            if rule_until.is_none() {
                return Vec::new();
            }
        }
    }
    let rule_text: Vec<String> = parts.iter().filter(|(k, _)| k != "UNTIL").map(|(k, v)| format!("{k}={v}")).collect();
    // The expansion runs in UTC on the local wall clock; each start is then
    // put in the local zone, so a series keeps its hour across DST.
    let mut text = format!("DTSTART:{}Z\nRRULE:{}", naive_start.format("%Y%m%dT%H%M%S"), rule_text.join(";"));
    for line in lines.iter().filter(|l| l.starts_with("EXDATE")) {
        for value in line.split_once(':').map(|(_, v)| v).unwrap_or("").split(',') {
            let Some(naive) = exdate_of(value, local_zone) else { continue };
            text.push_str(&format!("\nEXDATE:{}Z", naive.format("%Y%m%dT%H%M%S")));
        }
    }
    let Ok(set) = text.parse::<rrule::RRuleSet>() else {
        tracing::warn!("couldn't expand the recurrence {text:?} of {}", master["id"]);
        return Vec::new();
    };
    let mut found = Vec::new();
    for naive in set.into_iter() {
        let naive = naive.naive_utc();
        let when = if all_day {
            When::Day(naive.date())
        } else {
            match local_zone.from_local_datetime(&naive).earliest() {
                Some(m) => When::At(m.with_timezone(&Utc)),
                None => continue,
            }
        };
        let key = when.key(zone);
        if rule_until.is_some_and(|u| key > u) || until.is_some_and(|u| key >= u) {
            break;
        }
        found.push(when);
        // Google expands no further, even for a range later on.
        if found.len() >= INSTANCE_CAP {
            break;
        }
    }
    found
}

fn until_of(value: &str, zone: Tz) -> Option<DateTime<Utc>> {
    if let Some(t) = value.strip_suffix('Z') {
        Some(Utc.from_utc_datetime(&NaiveDateTime::parse_from_str(t, "%Y%m%dT%H%M%S").ok()?))
    } else if value.contains('T') {
        Some(zone.from_local_datetime(&NaiveDateTime::parse_from_str(value, "%Y%m%dT%H%M%S").ok()?).earliest()?.with_timezone(&Utc))
    } else {
        let day = NaiveDate::parse_from_str(value, "%Y%m%d").ok()?;
        Some(zone.from_local_datetime(&day.and_hms_opt(23, 59, 59)?).earliest()?.with_timezone(&Utc))
    }
}

fn exdate_of(value: &str, zone: Tz) -> Option<NaiveDateTime> {
    if value.contains('T') {
        if let Some(t) = value.strip_suffix('Z') {
            Some(Utc.from_utc_datetime(&NaiveDateTime::parse_from_str(t, "%Y%m%dT%H%M%S").ok()?).with_timezone(&zone).naive_local())
        } else {
            NaiveDateTime::parse_from_str(value, "%Y%m%dT%H%M%S").ok()
        }
    } else {
        Some(NaiveDate::parse_from_str(value, "%Y%m%d").ok()?.and_time(NaiveTime::MIN))
    }
}

/// Instance `original` of series `master`: `exception` -- the instance as
/// edited on its own -- if there is one, else the master at that start
/// (cancelled, if the master is).
pub fn instance(zone: Tz, master: &Value, original: When, exception: Option<&Value>) -> Value {
    if let Some(exception) = exception {
        return under(master, exception);
    }
    let master_id = master["id"].as_str().unwrap_or_default();
    let start = master.get("start").and_then(|s| parse_moment(s, zone));
    let end = master.get("end").and_then(|e| parse_moment(e, zone));
    let time_zone = master["start"].get("timeZone").and_then(Value::as_str);
    let mut instance = Map::new();
    for (k, v) in master.as_object().into_iter().flatten() {
        if !["recurrence", "id", "start", "end"].contains(&k.as_str()) {
            instance.insert(k.clone(), v.clone());
        }
    }
    instance.insert("id".into(), json!(instance_id(master_id, original)));
    instance.insert("recurringEventId".into(), json!(master_id));
    instance.insert("originalStartTime".into(), render_moment(original, zone, time_zone));
    instance.insert("start".into(), render_moment(original, zone, time_zone));
    let end_when = match (original, start, end) {
        (When::At(o), Some(When::At(s)), Some(When::At(e))) => When::At(o + (e - s)),
        (When::Day(o), Some(When::Day(s)), Some(When::Day(e))) => When::Day(o + (e - s)),
        (_, _, Some(end)) => end,
        _ => original,
    };
    instance.insert("end".into(), render_moment(end_when, zone, master["end"].get("timeZone").and_then(Value::as_str)));
    if master["status"] == "cancelled" {
        instance.insert("status".into(), json!("cancelled"));
    }
    Value::Object(instance)
}

/// An exception as listed: cancelled with its master.
pub fn under(master: &Value, exception: &Value) -> Value {
    if master["status"] == "cancelled" && exception["status"] != "cancelled" {
        let mut e = exception.clone();
        e["status"] = json!("cancelled");
        return e;
    }
    exception.clone()
}

/// Whether `exception` is one of `master`'s instances Google has.
pub fn is_occurrence(zone: Tz, master: &Value, exception: &Value) -> bool {
    let Some(original) = exception.get("originalStartTime").and_then(|o| parse_moment(o, zone)) else { return false };
    occurrences(zone, master, Some(original.key(zone) + Duration::seconds(1))).contains(&original)
}

/// `events` -- a singleEvents=false listing's: events on their own, series'
/// masters and their exceptions -- as a singleEvents=true listing of
/// [`time_min`, `time_max`) gives them, in `zone` (the calendar's): each
/// series expanded, those cancelled left out unless `show_deleted`, sorted
/// by start (then id). An instance is in the range if it ends after
/// `time_min` and starts before `time_max`. A series is expanded no further
/// than `horizon` when `time_max` is open.
pub fn expand(
    zone: Tz,
    events: &[Value],
    time_min: Option<DateTime<Utc>>,
    time_max: Option<DateTime<Utc>>,
    show_deleted: bool,
    horizon: DateTime<Utc>,
) -> Vec<Value> {
    let by_id: HashMap<&str, &Value> = events.iter().filter_map(|e| Some((e["id"].as_str()?, e))).collect();
    let mut exceptions: HashMap<&str, Vec<&Value>> = HashMap::new();
    for event in events {
        if let Some(master) = event.get("recurringEventId").and_then(Value::as_str) {
            exceptions.entry(master).or_default().push(event);
        }
    }
    let mut found = Vec::new();
    for event in events {
        if event.get("recurringEventId").is_some_and(|r| !r.is_null()) {
            continue;
        }
        if !is_master(event) {
            found.push(event.clone());
            continue;
        }
        let master_id = event["id"].as_str().unwrap_or_default();
        let mut seen: Vec<String> = Vec::new();
        for original in occurrences(zone, event, Some(time_max.unwrap_or(horizon))) {
            let id = instance_id(master_id, original);
            let instance = instance(zone, event, original, by_id.get(id.as_str()).copied());
            seen.push(id);
            found.push(instance);
        }
        for exception in exceptions.get(master_id).into_iter().flatten() {
            let id = exception["id"].as_str().unwrap_or_default();
            if !seen.iter().any(|s| s == id) && is_occurrence(zone, event, exception) {
                found.push(under(event, exception));
            }
        }
    }
    let mut listed: Vec<(DateTime<Utc>, Value)> = found
        .into_iter()
        .filter(|e| show_deleted || e["status"] != "cancelled")
        .filter_map(|e| {
            let (start, end) = span(&e, zone)?;
            let outside = time_min.is_some_and(|m| end <= m && !(start == end && start == m)) || time_max.is_some_and(|m| start >= m);
            (!outside).then_some((start, e))
        })
        .collect();
    listed.sort_by(|(a_start, a), (b_start, b)| (a_start, a["id"].as_str().unwrap_or("")).cmp(&(b_start, b["id"].as_str().unwrap_or(""))));
    listed.into_iter().map(|(_, e)| e).collect()
}

/// An event's start and end, in `zone`; its end is its start if it has none.
fn span(e: &Value, zone: Tz) -> Option<(DateTime<Utc>, DateTime<Utc>)> {
    let start = parse_moment(e.get("start")?, zone)?.key(zone);
    let end = e.get("end").and_then(|v| parse_moment(v, zone)).map_or(start, |w| w.key(zone));
    Some((start, end))
}

/// Sort `events` as `expand` lists them: by start, then id.
pub fn sort_by_start(zone: Tz, events: &mut [Value]) {
    events.sort_by_cached_key(|e| (span(e, zone).map(|(start, _)| start), e["id"].as_str().unwrap_or("").to_string()));
}

#[cfg(test)]
#[path = "expand_tests.rs"]
mod tests;
