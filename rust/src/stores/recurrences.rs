//! Recurring series as a whole: reading one, editing it whole or from an
//! event on, splitting it, deleting it -- and `Repeat`, its rules as a
//! client reads and writes them.

use crate::event::{Event, Moment};
use crate::google::status_of;
use crate::py;
use crate::stores::action_calendar::ActionCalendar;
use crate::zoned;
use anyhow::bail;
use chrono::{DateTime, Datelike, Duration, NaiveDate, NaiveDateTime, NaiveTime, TimeZone, Utc};
use chrono_tz::Tz;
use regex::Regex;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::sync::LazyLock;

static SPLIT_BY_GOOGLE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^(?P<base>.+)_R\d{8}T\d{6}$").unwrap());
static BYDAY_TOKEN: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^([+-]?\d+)?([A-Z]{2})$").unwrap());

pub use crate::google::expand::INSTANCE_CAP;
/// How long before a series runs out of instances `renew` splits it.
pub const RENEW_DAYS_AHEAD: i64 = 180;

const FREQS: [(&str, &str); 4] = [("day", "DAILY"), ("week", "WEEKLY"), ("month", "MONTHLY"), ("year", "YEARLY")];
const WEEKDAYS: [(&str, &str); 7] = [("mon", "MO"), ("tue", "TU"), ("wed", "WE"), ("thu", "TH"), ("fri", "FR"), ("sat", "SA"), ("sun", "SU")];
const NTHS: [(i64, &str); 6] = [(1, "first"), (2, "second"), (3, "third"), (4, "fourth"), (5, "fifth"), (-1, "last")];
const MONTHS: [&str; 12] = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const PARTS: [&str; 8] = ["FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYMONTH", "COUNT", "UNTIL", "WKST"];

fn code_of(day: &str) -> Option<&'static str> {
    WEEKDAYS.iter().find(|(d, _)| *d == day).map(|(_, c)| *c)
}

fn day_of(code: &str) -> Option<&'static str> {
    WEEKDAYS.iter().find(|(_, c)| *c == code).map(|(d, _)| *d)
}

/// A Python datetime that may or may not carry an offset.
#[derive(Debug, Clone, PartialEq)]
pub enum PyDt {
    Aware(Moment),
    Naive(NaiveDateTime),
}

impl PyDt {
    pub fn parse(text: &str) -> anyhow::Result<PyDt> {
        if let Ok(moment) = crate::event::parse_iso(text, None) {
            return Ok(PyDt::Aware(moment));
        }
        let naive = NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M:%S%.f")
            .or_else(|_| NaiveDateTime::parse_from_str(text, "%Y-%m-%dT%H:%M"))?;
        Ok(PyDt::Naive(naive))
    }

    /// `_in(zone, moment)`: in `zone`; a naive one taken to be there already.
    fn in_zone(&self, zone: &Tz) -> Moment {
        match self {
            PyDt::Aware(m) => zoned::in_zone(m, zone),
            PyDt::Naive(n) => zoned::resolve(zone, *n),
        }
    }

    fn to_json(&self) -> Value {
        match self {
            PyDt::Aware(m) => json!(py::pydantic_datetime(m)),
            PyDt::Naive(n) => json!(n.format("%Y-%m-%dT%H:%M:%S").to_string()),
        }
    }
}

#[derive(Debug, Clone, PartialEq)]
pub enum Until {
    Date(NaiveDate),
    DateTime(PyDt),
}

#[derive(Debug, Clone, PartialEq)]
pub struct Repeat {
    pub every: String,
    pub interval: i64,
    pub weekdays: Option<Vec<String>>,
    pub nth_weekdays: Option<Vec<(i64, String)>>,
    pub month_days: Option<Vec<i64>>,
    pub months: Option<Vec<i64>>,
    pub count: Option<i64>,
    pub until: Option<Until>,
    pub week_starts_on: Option<String>,
    pub skipped: Option<Vec<PyDt>>,
    pub added: Option<Vec<PyDt>>,
}

fn strings(value: Option<&Value>) -> Option<Vec<String>> {
    value.and_then(Value::as_array).map(|a| a.iter().filter_map(|i| i.as_str().map(str::to_string)).collect())
}

fn ints(value: Option<&Value>) -> Option<Vec<i64>> {
    value.and_then(Value::as_array).map(|a| a.iter().filter_map(Value::as_i64).collect())
}

impl Repeat {
    /// A Repeat from a client's JSON (already checked against the schema).
    pub fn from_value(value: &Value) -> anyhow::Result<Repeat> {
        let times = |key: &str| -> anyhow::Result<Option<Vec<PyDt>>> {
            match value.get(key).and_then(Value::as_array) {
                Some(items) => Ok(Some(items.iter().filter_map(Value::as_str).map(PyDt::parse).collect::<anyhow::Result<_>>()?)),
                None => Ok(None),
            }
        };
        let until = match value.get("until").and_then(Value::as_str) {
            Some(text) if text.chars().count() == 10 => Some(Until::Date(NaiveDate::parse_from_str(text, "%Y-%m-%d")?)),
            Some(text) => Some(Until::DateTime(PyDt::parse(text)?)),
            None => None,
        };
        Ok(Repeat {
            every: value.get("every").and_then(Value::as_str).unwrap_or_default().to_string(),
            interval: value.get("interval").and_then(Value::as_i64).unwrap_or(1),
            weekdays: strings(value.get("weekdays")),
            nth_weekdays: value.get("nth_weekdays").and_then(Value::as_array).map(|items| {
                items
                    .iter()
                    .map(|n| (n["nth"].as_i64().unwrap_or(0), n["weekday"].as_str().unwrap_or_default().to_string()))
                    .collect()
            }),
            month_days: ints(value.get("month_days")),
            months: ints(value.get("months")),
            count: value.get("count").and_then(Value::as_i64),
            until,
            week_starts_on: value.get("week_starts_on").and_then(Value::as_str).map(str::to_string),
            skipped: times("skipped")?,
            added: times("added")?,
        })
    }

    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("every".into(), json!(self.every));
        out.insert("interval".into(), json!(self.interval));
        out.insert("weekdays".into(), json!(self.weekdays));
        out.insert(
            "nth_weekdays".into(),
            match &self.nth_weekdays {
                Some(items) => Value::Array(items.iter().map(|(n, d)| json!({"nth": n, "weekday": d})).collect()),
                None => Value::Null,
            },
        );
        out.insert("month_days".into(), json!(self.month_days));
        out.insert("months".into(), json!(self.months));
        out.insert("count".into(), json!(self.count));
        out.insert(
            "until".into(),
            match &self.until {
                None => Value::Null,
                Some(Until::Date(d)) => json!(d.format("%Y-%m-%d").to_string()),
                Some(Until::DateTime(dt)) => dt.to_json(),
            },
        );
        out.insert("week_starts_on".into(), json!(self.week_starts_on));
        let times = |t: &Option<Vec<PyDt>>| match t {
            Some(items) => Value::Array(items.iter().map(PyDt::to_json).collect()),
            None => Value::Null,
        };
        out.insert("skipped".into(), times(&self.skipped));
        out.insert("added".into(), times(&self.added));
        Value::Object(out)
    }

    pub fn check(&self) -> anyhow::Result<()> {
        if !FREQS.iter().any(|(e, _)| *e == self.every) {
            bail!("every must be one of day, week, month, year, not {}", py::repr(&self.every));
        }
        if self.interval < 1 {
            bail!("interval must be at least 1");
        }
        if self.count.is_some() && self.until.is_some() {
            bail!("Give count or until, not both");
        }
        if self.count.is_some_and(|c| c < 1) {
            bail!("count must be at least 1");
        }
        let mut days: Vec<String> = self.weekdays.clone().unwrap_or_default();
        days.extend(self.nth_weekdays.iter().flatten().map(|(_, d)| d.clone()));
        days.extend(self.week_starts_on.clone());
        if let Some(bad) = days.iter().find(|d| code_of(d).is_none()) {
            bail!("Weekdays are mon, tue, wed, thu, fri, sat, sun, not {}", py::repr(bad));
        }
        if let Some(nths) = self.nth_weekdays.as_ref().filter(|n| !n.is_empty()) {
            if let Some((bad, _)) = nths.iter().find(|(n, _)| !NTHS.iter().any(|(k, _)| k == n)) {
                bail!("nth must be 1 to 5, or -1 for the last, not {bad}");
            }
            if self.every == "day" || self.every == "week" {
                bail!("nth_weekdays are for every \"month\" or \"year\", not \"{}\"", self.every);
            }
            if self.every == "year" && self.months.as_ref().is_none_or(|m| m.is_empty()) {
                bail!("nth_weekdays with every \"year\" need months, to say which month they're in");
            }
        }
        if let Some(days) = self.month_days.as_ref().filter(|d| !d.is_empty()) {
            if let Some(bad) = days.iter().find(|d| !((1..=31).contains(*d) || **d == -1)) {
                bail!("month_days must be 1 to 31, or -1 for the last, not {bad}");
            }
            if self.every == "week" {
                bail!("month_days are not for every \"week\"");
            }
        }
        if let Some(bad) = self.months.iter().flatten().find(|m| !(1..=12).contains(*m)) {
            bail!("months must be 1 to 12, not {bad}");
        }
        Ok(())
    }

    pub fn to_rules(&self, zone: &Tz) -> anyhow::Result<Vec<String>> {
        self.check()?;
        let mut parts: Vec<(&str, String)> = vec![("FREQ", FREQS.iter().find(|(e, _)| *e == self.every).unwrap().1.to_string())];
        if self.interval != 1 {
            parts.push(("INTERVAL", self.interval.to_string()));
        }
        if let Some(months) = self.months.as_ref().filter(|m| !m.is_empty()) {
            parts.push(("BYMONTH", months.iter().map(i64::to_string).collect::<Vec<_>>().join(",")));
        }
        if let Some(days) = self.month_days.as_ref().filter(|d| !d.is_empty()) {
            parts.push(("BYMONTHDAY", days.iter().map(i64::to_string).collect::<Vec<_>>().join(",")));
        }
        let mut byday: Vec<String> = self.weekdays.iter().flatten().map(|d| code_of(d).unwrap().to_string()).collect();
        byday.extend(self.nth_weekdays.iter().flatten().map(|(n, d)| format!("{n}{}", code_of(d).unwrap())));
        if !byday.is_empty() {
            parts.push(("BYDAY", byday.join(",")));
        }
        if let Some(count) = self.count {
            parts.push(("COUNT", count.to_string()));
        }
        if let Some(until) = &self.until {
            let moment = match until {
                Until::DateTime(dt) => dt.in_zone(zone),
                Until::Date(d) => zoned::resolve(zone, d.and_time(NaiveTime::from_hms_opt(23, 59, 59).unwrap())),
            };
            parts.push(("UNTIL", moment.with_timezone(&Utc).format("%Y%m%dT%H%M%SZ").to_string()));
        }
        if let Some(start) = &self.week_starts_on {
            parts.push(("WKST", code_of(start).unwrap().to_string()));
        }
        let mut rules = vec![format!("RRULE:{}", parts.iter().map(|(k, v)| format!("{k}={v}")).collect::<Vec<_>>().join(";"))];
        for (name, times) in [("EXDATE", &self.skipped), ("RDATE", &self.added)] {
            if let Some(times) = times.as_ref().filter(|t| !t.is_empty()) {
                let values: Vec<String> = times.iter().map(|t| t.in_zone(zone).format("%Y%m%dT%H%M%S").to_string()).collect();
                rules.push(format!("{name};TZID={}:{}", zone.name(), values.join(",")));
            }
        }
        Ok(rules)
    }

    /// A series' rule lines as a Repeat, its times in `zone`, or None if a
    /// Repeat can't say them.
    pub fn from_rules(rules: Option<&[String]>, zone: &Tz) -> Option<Repeat> {
        let rules = rules.filter(|r| !r.is_empty())?;
        let repeat = Repeat::parse(rules, zone).ok()?;
        repeat.check().ok()?;
        Some(repeat)
    }

    fn parse(rules: &[String], zone: &Tz) -> anyhow::Result<Repeat> {
        let rrules: Vec<&String> = rules.iter().filter(|r| r.starts_with("RRULE:")).collect();
        if rrules.len() != 1 {
            bail!("Not exactly one RRULE");
        }
        let mut exdates: Vec<PyDt> = Vec::new();
        let mut rdates: Vec<PyDt> = Vec::new();
        for rule in rules {
            if rule.starts_with("RRULE:") {
                continue;
            }
            let (head, values) = rule.split_once(':').unwrap_or((rule.as_str(), ""));
            let mut pieces = head.split(';');
            let name = pieces.next().unwrap_or_default();
            let params: Vec<&str> = pieces.collect();
            let times = parse_times(&params, values, zone)?;
            match name {
                "EXDATE" => exdates.extend(times.into_iter().map(PyDt::Aware)),
                "RDATE" => rdates.extend(times.into_iter().map(PyDt::Aware)),
                other => bail!("KeyError: {}", py::repr(other)),
            }
        }
        let parts = rule_parts(rrules[0]);
        if parts.iter().any(|(k, _)| !PARTS.contains(&k.as_str())) {
            bail!("Unsupported parts");
        }
        let get = |key: &str| parts.iter().rev().find(|(k, _)| k == key).map(|(_, v)| v.clone());
        let mut weekdays: Vec<String> = Vec::new();
        let mut nth_weekdays: Vec<(i64, String)> = Vec::new();
        for token in get("BYDAY").unwrap_or_default().split(',').filter(|t| !t.is_empty()) {
            let caps = BYDAY_TOKEN.captures(token).ok_or_else(|| anyhow::anyhow!("Bad BYDAY {}", py::repr(token)))?;
            let day = day_of(&caps[2]).ok_or_else(|| anyhow::anyhow!("KeyError"))?.to_string();
            match caps.get(1) {
                Some(n) => nth_weekdays.push((n.as_str().parse::<i64>()?, day)),
                None => weekdays.push(day),
            }
        }
        let list = |value: Option<String>| -> anyhow::Result<Option<Vec<i64>>> {
            match value.filter(|v| !v.is_empty()) {
                Some(v) => Ok(Some(v.split(',').map(|p| crate::event::python_int(p).ok_or_else(|| anyhow::anyhow!("invalid int"))).collect::<anyhow::Result<_>>()?)),
                None => Ok(None),
            }
        };
        let freq = get("FREQ").ok_or_else(|| anyhow::anyhow!("KeyError: 'FREQ'"))?;
        let every = FREQS.iter().find(|(_, c)| *c == freq).ok_or_else(|| anyhow::anyhow!("KeyError"))?.0.to_string();
        let interval = match get("INTERVAL").filter(|v| !v.is_empty()) {
            Some(v) => crate::event::python_int(&v).ok_or_else(|| anyhow::anyhow!("invalid int"))?,
            None => 1,
        };
        Ok(Repeat {
            every,
            interval,
            weekdays: if weekdays.is_empty() { None } else { Some(weekdays) },
            nth_weekdays: if nth_weekdays.is_empty() { None } else { Some(nth_weekdays) },
            month_days: list(get("BYMONTHDAY"))?,
            months: list(get("BYMONTH"))?,
            count: match get("COUNT") {
                Some(v) => Some(crate::event::python_int(&v).ok_or_else(|| anyhow::anyhow!("invalid int"))?),
                None => None,
            },
            until: match get("UNTIL") {
                Some(v) => Some(parse_until(&v, zone)?),
                None => None,
            },
            week_starts_on: match get("WKST") {
                Some(v) => Some(day_of(&v).ok_or_else(|| anyhow::anyhow!("KeyError"))?.to_string()),
                None => None,
            },
            skipped: if exdates.is_empty() { None } else { Some(exdates) },
            added: if rdates.is_empty() { None } else { Some(rdates) },
        })
    }

    /// "Every week on Mon, Wed, until Dec 31, 2026".
    pub fn describe(&self, zone: Option<&Tz>) -> String {
        let mut phrase = if self.interval == 1 { format!("Every {}", self.every) } else { format!("Every {} {}s", self.interval, self.every) };
        let mut days: Vec<String> = self.weekdays.iter().flatten().map(|d| capitalize(d)).collect();
        days.extend(
            self.nth_weekdays.iter().flatten().map(|(n, d)| format!("the {} {}", NTHS.iter().find(|(k, _)| k == n).unwrap().1, capitalize(d))),
        );
        if let Some(month_days) = self.month_days.as_ref().filter(|d| !d.is_empty()) {
            let numbered: Vec<String> = month_days.iter().filter(|d| **d > 0).map(i64::to_string).collect();
            let mut text = if numbered.is_empty() { String::new() } else { format!("day {}", numbered.join(", ")) };
            if month_days.contains(&-1) {
                if !text.is_empty() {
                    text.push_str(" and ");
                }
                text.push_str("the last day");
            }
            days.push(text);
        }
        if !days.is_empty() {
            phrase.push_str(&format!(" on {}", days.join(", ")));
        }
        if let Some(months) = self.months.as_ref().filter(|m| !m.is_empty()) {
            phrase.push_str(&format!(" in {}", months.iter().map(|m| MONTHS[(*m - 1) as usize]).collect::<Vec<_>>().join(", ")));
        }
        if let Some(count) = self.count {
            phrase.push_str(&format!(", {count} times"));
        }
        if let Some(until) = &self.until {
            let date = match until {
                Until::Date(d) => *d,
                Until::DateTime(dt) => {
                    let zone = zone.copied().unwrap_or(Tz::UTC);
                    dt.in_zone(&zone).date_naive()
                }
            };
            phrase.push_str(&format!(", until {} {}, {}", date.format("%b"), date.day(), date.year()));
        }
        if self.skipped.as_ref().is_some_and(|s| !s.is_empty()) {
            phrase.push_str(", with exceptions");
        }
        if let Some(added) = self.added.as_ref().filter(|a| !a.is_empty()) {
            phrase.push_str(&format!(", plus {} added date{}", added.len(), if added.len() > 1 { "s" } else { "" }));
        }
        phrase
    }
}

fn capitalize(word: &str) -> String {
    let mut chars = word.chars();
    match chars.next() {
        Some(first) => first.to_uppercase().collect::<String>() + &chars.as_str().to_lowercase(),
        None => String::new(),
    }
}

/// A series' rules in a phrase, or the rules themselves for what a Repeat
/// can't say.
pub fn describe_rules(rules: Option<&[String]>, zone: Option<&Tz>) -> Option<String> {
    let rules = rules.filter(|r| !r.is_empty())?;
    let repeat = Repeat::from_rules(Some(rules), zone.unwrap_or(&Tz::UTC));
    Some(match repeat {
        Some(repeat) => repeat.describe(zone),
        None => rules.join("; "),
    })
}

fn parse_until(value: &str, zone: &Tz) -> anyhow::Result<Until> {
    if let Some(stripped) = value.strip_suffix('Z') {
        let naive = NaiveDateTime::parse_from_str(stripped, "%Y%m%dT%H%M%S")?;
        return Ok(Until::DateTime(PyDt::Aware(zoned::in_zone(&Utc.from_utc_datetime(&naive).fixed_offset(), zone))));
    }
    if value.contains('T') {
        let naive = NaiveDateTime::parse_from_str(value, "%Y%m%dT%H%M%S")?;
        return Ok(Until::DateTime(PyDt::Aware(zoned::resolve(zone, naive))));
    }
    Ok(Until::Date(NaiveDate::parse_from_str(value, "%Y%m%d")?))
}

fn parse_times(params: &[&str], values: &str, zone: &Tz) -> anyhow::Result<Vec<Moment>> {
    let mut given: Vec<(String, String)> = Vec::new();
    for param in params {
        let (k, v) = param.split_once('=').ok_or_else(|| anyhow::anyhow!("bad parameter"))?;
        given.push((k.to_string(), v.to_string()));
    }
    let get = |key: &str| given.iter().rev().find(|(k, _)| k == key).map(|(_, v)| v.clone());
    if given.iter().any(|(k, _)| k != "TZID" && k != "VALUE") || get("VALUE").unwrap_or_else(|| "DATE-TIME".into()) != "DATE-TIME" {
        bail!("Unsupported parameters");
    }
    let named: Tz = match get("TZID") {
        Some(name) => name.parse().map_err(|_| anyhow::anyhow!("KeyError: unknown TZID"))?,
        None => *zone,
    };
    values
        .split(',')
        .map(|value| -> anyhow::Result<Moment> {
            let moment = match value.strip_suffix('Z') {
                Some(stripped) => Utc.from_utc_datetime(&NaiveDateTime::parse_from_str(stripped, "%Y%m%dT%H%M%S")?).fixed_offset(),
                None => zoned::resolve(&named, NaiveDateTime::parse_from_str(value, "%Y%m%dT%H%M%S")?),
            };
            Ok(zoned::in_zone(&moment, zone))
        })
        .collect()
}

pub fn rule_parts(rule: &str) -> Vec<(String, String)> {
    let body = rule.split_once(':').map(|(_, b)| b).unwrap_or("");
    let mut parts: Vec<(String, String)> = Vec::new();
    for part in body.split(';') {
        if let Some((k, v)) = part.split_once('=') {
            match parts.iter_mut().find(|(key, _)| key == k) {
                Some(slot) => slot.1 = v.to_string(),
                None => parts.push((k.to_string(), v.to_string())),
            }
        }
    }
    parts
}

fn with_rule(rules: &[String], index: usize, parts: &[(String, String)]) -> Vec<String> {
    rules
        .iter()
        .enumerate()
        .map(|(i, rule)| {
            if i == index {
                format!("RRULE:{}", parts.iter().map(|(k, v)| format!("{k}={v}")).collect::<Vec<_>>().join(";"))
            } else {
                rule.clone()
            }
        })
        .collect()
}

fn rrule(series: &Event) -> anyhow::Result<(Vec<String>, usize, Vec<(String, String)>)> {
    let rules = series.recurrence.clone().unwrap_or_default();
    let Some(index) = rules.iter().position(|r| r.starts_with("RRULE:")) else {
        bail!("Series {} has no RRULE, so it can't be ended early", series.id.as_deref().unwrap_or("None"));
    };
    let parts = rule_parts(&rules[index]);
    Ok((rules, index, parts))
}

fn ending_before(parts: &[(String, String)], at: &Moment) -> Vec<(String, String)> {
    let mut ended: Vec<(String, String)> = parts.iter().filter(|(k, _)| k != "COUNT" && k != "UNTIL").cloned().collect();
    ended.push(("UNTIL".into(), (*at - Duration::seconds(1)).with_timezone(&Utc).format("%Y%m%dT%H%M%SZ").to_string()));
    ended
}

fn ends_before(series: &Event, at: &Moment) -> bool {
    if series.is_cancelled() {
        return false;
    }
    let Ok((_, _, parts)) = rrule(series) else { return false };
    let Some((_, until)) = parts.iter().rev().find(|(k, _)| k == "UNTIL") else { return false };
    // `_parse_until(UNTIL, at.tzinfo)`: a floating time is in `at`'s offset.
    if let Some(stripped) = until.strip_suffix('Z') {
        return NaiveDateTime::parse_from_str(stripped, "%Y%m%dT%H%M%S").is_ok_and(|n| Utc.from_utc_datetime(&n) < *at);
    }
    if until.contains('T') {
        return NaiveDateTime::parse_from_str(until, "%Y%m%dT%H%M%S")
            .ok()
            .and_then(|n| at.offset().from_local_datetime(&n).single())
            .is_some_and(|u| u < *at);
    }
    NaiveDate::parse_from_str(until, "%Y%m%d").is_ok_and(|d| d < at.date_naive())
}

/// Whether `series` ends after a COUNT of instances Google expands them all.
fn counts_within_cap(series: &Event) -> bool {
    let Ok((_, _, parts)) = rrule(series) else { return false };
    parts.iter().rev().find(|(k, _)| k == "COUNT").and_then(|(_, count)| count.parse::<usize>().ok()).is_some_and(|c| c <= INSTANCE_CAP)
}

/// The id of the series split from `series_id` at `at`.
pub fn split_series_id(series_id: &str, at: &Moment) -> String {
    let utc = py::isoformat(&at.with_timezone(&Utc).fixed_offset());
    let digest = Sha256::digest(format!("{series_id}|{utc}").as_bytes());
    base32hex(&digest[..20]).to_lowercase()
}

/// `base64.b32hexencode` (RFC 4648's extended hex alphabet), for whole
/// 5-byte groups -- no padding needed.
fn base32hex(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 32] = b"0123456789ABCDEFGHIJKLMNOPQRSTUV";
    let mut out = String::new();
    let mut buffer: u64 = 0;
    let mut bits = 0;
    for byte in bytes {
        buffer = (buffer << 8) | *byte as u64;
        bits += 8;
        while bits >= 5 {
            bits -= 5;
            out.push(ALPHABET[((buffer >> bits) & 31) as usize] as char);
        }
    }
    if bits > 0 {
        out.push(ALPHABET[((buffer << (5 - bits)) & 31) as usize] as char);
    }
    out
}

/// A split that failed part way: says what was written and undone.
#[derive(Debug)]
pub struct SplitError(pub String);

impl std::fmt::Display for SplitError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for SplitError {}

/// A refused edit: a ValueError in Python, shown to the client.
#[derive(Debug)]
pub struct Refused(pub String);

impl std::fmt::Display for Refused {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for Refused {}

fn refused(message: impl Into<String>) -> anyhow::Error {
    Refused(message.into()).into()
}

#[derive(Clone)]
pub struct Recurrences {
    calendar: ActionCalendar,
}

const EDITABLE: [&str; 9] = ["summary", "start", "end", "description", "location", "priority", "action_ids", "goals", "facts"];

impl Recurrences {
    pub fn new(calendar: ActionCalendar) -> Recurrences {
        Recurrences { calendar }
    }

    fn time_zone(&self) -> anyhow::Result<Tz> {
        self.calendar.client().get_time_zone()
    }

    fn list_instances(&self, series_id: &str, before: &Moment) -> anyhow::Result<Vec<Event>> {
        self.calendar.client().list_instances(series_id, before)
    }

    pub fn series(&self, event_id: &str) -> anyhow::Result<Event> {
        let mut event = self.calendar.get_event(event_id)?;
        if let Some(parent) = event.recurring_event_id.clone().filter(|p| !p.is_empty()) {
            event = self.calendar.get_event(&parent)?;
        }
        if event.recurrence.as_ref().is_none_or(|r| r.is_empty()) {
            return Err(refused(format!("Event {event_id} isn't part of a recurring series")));
        }
        Ok(event)
    }

    pub fn update(&self, changes: &Event, starting_at: Option<&str>, repeat: Option<&Repeat>) -> anyhow::Result<Vec<Event>> {
        let Some(changes_id) = changes.id.clone().filter(|i| !i.is_empty()) else {
            return Err(refused("Say which series to update: its id, or one of its events'"));
        };
        if let Some(repeat) = repeat {
            repeat.check().map_err(|e| refused(e.to_string()))?;
        }
        let series = self.series(&changes_id)?;
        let mut earlier = None;
        let mut target = series.clone();
        if let Some(starting_at) = starting_at {
            if self.series(starting_at)?.id != series.id {
                return Err(refused(format!(
                    "Event {starting_at} isn't one of series {}'s events",
                    series.id.as_deref().unwrap_or("None")
                )));
            }
            let (before, later) = self.split(starting_at)?;
            earlier = before;
            target = later;
        }
        let mut patch = Event { id: target.id.clone(), cleared: changes.cleared.clone(), ..Default::default() };
        for name in EDITABLE {
            match name {
                "summary" => patch.summary = changes.summary.clone(),
                "start" => patch.start = changes.start,
                "end" => patch.end = changes.end,
                "description" => patch.description = changes.description.clone(),
                "location" => patch.location = changes.location.clone(),
                "priority" => patch.priority = changes.priority,
                "action_ids" => patch.action_ids = changes.action_ids.clone(),
                "goals" => patch.goals = changes.goals.clone(),
                _ => patch.facts = changes.facts.clone(),
            }
        }
        if target.id != series.id {
            if let Some(start) = patch.start {
                patch.start = Some(target.start() + (start - series.start()));
            }
            if let Some(end) = patch.end {
                patch.end = Some(target.end() + (end - series.end()));
            }
        }
        let target_id = target.id.clone().unwrap_or_default();
        if SPLIT_BY_GOOGLE.is_match(&target_id) {
            if repeat.is_some() {
                return Err(refused(format!(
                    "Series {target_id} was split from another in Google Calendar, which won't change its rules (it would delete the series instead): change them from one of its later events on"
                )));
            }
            if patch.start.is_some_and(|s| s != target.start()) {
                return Err(refused(format!(
                    "Series {target_id} was split from another in Google Calendar, which won't move its start: move it from one of its later events on"
                )));
            }
        }
        let zone_name = match &target.time_zone {
            Some(zone) if !zone.is_empty() => zone.clone(),
            _ => self.time_zone()?.name().to_string(),
        };
        if patch.start.is_some() || patch.end.is_some() {
            patch.time_zone = Some(zone_name.clone());
        }
        if let Some(repeat) = repeat {
            let zone: Tz = zone_name.parse().map_err(|_| anyhow::anyhow!("No time zone found with key {zone_name}"))?;
            patch.recurrence = Some(repeat.to_rules(&zone).map_err(|e| refused(e.to_string()))?);
        }
        let updated = self.calendar.update_event(&patch)?;
        let mut out = vec![updated];
        out.extend(earlier);
        Ok(out)
    }

    pub fn delete(&self, event_id: &str, starting_at: Option<&str>) -> anyhow::Result<Option<Event>> {
        let series = self.series(event_id)?;
        let mut at = None;
        if let Some(starting_at) = starting_at {
            let instance = self.calendar.get_event(starting_at)?;
            let parent = instance.recurring_event_id.clone().filter(|p| !p.is_empty()).or(instance.id.clone());
            if parent != series.id {
                return Err(refused(format!(
                    "Event {starting_at} isn't one of series {}'s events",
                    series.id.as_deref().unwrap_or("None")
                )));
            }
            at = Some(instance.original_start.unwrap_or(instance.start()));
        }
        match at {
            Some(at) if at > series.start() => Ok(Some(self.end_before(&series, &at)?)),
            _ => {
                self.calendar.update_event(&Event { id: series.id.clone(), status: Some("cancelled".into()), ..Default::default() })?;
                Ok(None)
            }
        }
    }

    pub fn split(&self, event_id: &str) -> anyhow::Result<(Option<Event>, Event)> {
        let instance = self.calendar.get_event(event_id)?;
        let Some(parent) = instance.recurring_event_id.clone().filter(|p| !p.is_empty()) else {
            if instance.recurrence.as_ref().is_some_and(|r| !r.is_empty()) {
                return Ok((None, instance));
            }
            return Err(refused(format!("Event {event_id} isn't part of a recurring series")));
        };
        let series = self.calendar.get_event(&parent)?;
        let at = instance.original_start.unwrap_or(instance.start());
        if at <= series.start() {
            return Ok((None, series));
        }
        let series_id = series.id.clone().unwrap_or_default();
        let (rules, index, parts) = rrule(&series).map_err(|e| refused(e.to_string()))?;
        let mut later_parts = parts.clone();
        if let Some((_, count)) = parts.iter().rev().find(|(k, _)| k == "COUNT") {
            if SPLIT_BY_GOOGLE.is_match(&series_id) {
                return Err(refused(format!(
                    "Series {series_id} was split from another in Google Calendar and ends after a count of events, so it can't be split here: split it there"
                )));
            }
            let before = self.list_instances(&series_id, &at)?.iter().filter(|e| e.original_start.unwrap_or(e.start()) < at).count() as i64;
            let remaining = crate::event::python_int(count).unwrap_or(0) - before;
            if remaining <= 0 {
                return Err(refused(format!("Event {event_id} is after the last of series {series_id}")));
            }
            if let Some(slot) = later_parts.iter_mut().find(|(k, _)| k == "COUNT") {
                slot.1 = remaining.to_string();
            }
        }
        let zone_name = match &series.time_zone {
            Some(zone) if !zone.is_empty() => zone.clone(),
            _ => self.time_zone()?.name().to_string(),
        };
        let zone: Tz = zone_name.parse().map_err(|_| anyhow::anyhow!("No time zone found with key {zone_name}"))?;
        let start = zoned::in_zone(&at, &zone);
        let later = Event {
            id: Some(split_series_id(&series_id, &at)),
            start: Some(start),
            end: Some(zoned::add(&start, &zone, series.end() - series.start())),
            time_zone: Some(zone_name.clone()),
            recurrence: Some(with_rule(&rules, index, &later_parts)),
            status: None,
            ..series.clone()
        };
        let created = match self.calendar.create_event(&later) {
            Ok(created) => created,
            Err(error) if status_of(&error) == Some(409) => {
                let mut existing = self.calendar.get_event(later.id.as_deref().unwrap())?;
                if existing.is_cancelled() {
                    existing = self.calendar.update_event(&Event { id: later.id.clone(), status: Some("confirmed".into()), ..Default::default() })?;
                }
                existing
            }
            Err(error) => return Err(error),
        };
        match self.end_before(&series, &at) {
            Ok(earlier) => Ok((Some(earlier), created)),
            Err(error) => {
                let undo = self.calendar.update_event(&Event { id: created.id.clone(), status: Some("cancelled".into()), ..Default::default() });
                let created_id = created.id.clone().unwrap_or_default();
                match undo {
                    Err(undo) => Err(SplitError(format!(
                        "Couldn't end series {series_id} before {} ({}), nor cancel its copy {created_id} from then on ({undo}): cancel that copy (delete_recurrence {created_id}), or its events will be there twice",
                        py::isoformat(&at),
                        error
                    ))
                    .into()),
                    Ok(_) => Err(SplitError(format!(
                        "Couldn't end series {series_id} before {}, so it wasn't split (its copy from then on was cancelled again): {error}",
                        py::isoformat(&at)
                    ))
                    .into()),
                }
            }
        }
    }

    /// Split each series whose instances run out within RENEW_DAYS_AHEAD
    /// of `now` -- Google expands only INSTANCE_CAP of them -- at its last
    /// one, so it goes on as a new series with instances of its own. The
    /// series that end by their own rules by then are left alone. Splitting
    /// at the last instance, not now, leaves the series' edited instances
    /// in place: only an edit to that last one is lost. Returns the new
    /// series; one that can't be split is logged and skipped.
    pub fn renew(&self, now: &Moment) -> anyhow::Result<Vec<Event>> {
        let client = self.calendar.client();
        let horizon = *now + Duration::days(RENEW_DAYS_AHEAD);
        let going_on: Vec<String> = client.list_series(&horizon, None)?.into_iter().filter_map(|s| s.id).collect();
        let mut renewed = Vec::new();
        for series in client.list_series(now, Some(&horizon))? {
            let Some(series_id) = series.id.clone().filter(|id| !going_on.contains(id)) else { continue };
            if ends_before(&series, &horizon) || counts_within_cap(&series) {
                continue;
            }
            let Some(last) = client.instances_from(&series_id, now)?.pop() else { continue };
            match self.split(last.id.as_deref().unwrap_or_default()) {
                Ok((_, later)) if later.id.as_deref() == Some(series_id.as_str()) => {}
                Ok((_, later)) => {
                    tracing::info!("renewed series {series_id} as {} from {}", later.id.as_deref().unwrap_or(""), py::isoformat(&later.start()));
                    renewed.push(later);
                }
                Err(error) => tracing::warn!("couldn't renew series {series_id}, which Google stops expanding soon: {error:#}"),
            }
        }
        Ok(renewed)
    }

    fn end_before(&self, series: &Event, at: &Moment) -> anyhow::Result<Event> {
        let series_id = series.id.clone().unwrap_or_default();
        let Some(caps) = SPLIT_BY_GOOGLE.captures(&series_id) else {
            let (rules, index, parts) = rrule(series).map_err(|e| refused(e.to_string()))?;
            return self.calendar.update_event(&Event {
                id: series.id.clone(),
                recurrence: Some(with_rule(&rules, index, &ending_before(&parts, at))),
                ..Default::default()
            });
        };
        let base = caps["base"].to_string();
        let zone_name = match &series.time_zone {
            Some(zone) if !zone.is_empty() => zone.clone(),
            _ => self.time_zone()?.name().to_string(),
        };
        let zone: Tz = zone_name.parse().map_err(|_| anyhow::anyhow!("No time zone found with key {zone_name}"))?;
        let start = zoned::in_zone(at, &zone);
        let rest = self.calendar.import_event(
            &Event {
                summary: series.summary.clone(),
                start: Some(start),
                end: Some(zoned::add(&start, &zone, series.end() - series.start())),
                time_zone: Some(zone_name.clone()),
                recurrence: series.recurrence.clone(),
                ..Default::default()
            },
            &format!("{base}_R{}@google.com", at.with_timezone(&Utc).format("%Y%m%dT%H%M%S")),
        )?;
        self.calendar.update_event(&Event { id: rest.id.clone(), status: Some("cancelled".into()), ..Default::default() })?;
        let ended = self.calendar.get_event(&series_id)?;
        if !ends_before(&ended, at) {
            let rules = match &ended.recurrence {
                Some(r) => py::repr_list(r),
                None => "None".into(),
            };
            return Err(SplitError(format!(
                "Importing {base}_R... didn't end series {series_id} before {} (its rules are now {rules}, and it's {})",
                py::isoformat(at),
                ended.status.clone().unwrap_or_else(|| "None".into())
            ))
            .into());
        }
        Ok(ended)
    }
}

/// Whether `error` is one the recurrence tools show the client (Python's
/// ValueError: a refusal, or a split that failed part way).
pub fn is_refusal(error: &anyhow::Error) -> bool {
    error.downcast_ref::<Refused>().is_some() || error.downcast_ref::<SplitError>().is_some()
}

#[allow(dead_code)]
fn _unused(_: DateTime<Utc>) {}

#[cfg(test)]
#[path = "recurrences_tests.rs"]
mod store_tests;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn split_ids_match_python() {
        // base64.b32hexencode(hashlib.sha256(b"abc|2026-10-09T13:00:00+00:00").digest()[:20]).decode().lower()
        let at = DateTime::parse_from_rfc3339("2026-10-09T09:00:00-04:00").unwrap();
        assert_eq!(split_series_id("abc", &at), "qs2bv68ro2u6npkda16ircho66ccdbmg");
    }
}
