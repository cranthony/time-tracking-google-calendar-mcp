//! Cancellations (utilities/cancellations.py): the events the user
//! cancelled that count against someone's follow-through, one row per
//! event per person (and engagement), in the Cancellations tab.

use crate::event::{parse_iso, Event, Moment};
use crate::facts::{Facts, SELF_ID};
use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::sheet::rows::{self, Kind, Row, RowSheet, RowType};
use crate::sheet::CANCELLATIONS;
use crate::stores::actions::{ActionTree, Actions};
use crate::stores::groups::{python_str, truthy};
use crate::stores::habits::{subject_id, Habits, SUBJECT_PREFIX};
use crate::stores::people::People;
use crate::stores::traits::{part_keys, part_problems, Trait, Traits};
use chrono::{DateTime, Duration, FixedOffset};
use serde_json::{json, Map, Value};

pub const KEEP_DAYS: i64 = 400 + 90;

pub const CANCELLATION_ROW: RowType = RowType {
    name: "Cancellation",
    fields: &[
        ("id", Kind::Str),
        ("event_id", Kind::Str),
        ("person_id", Kind::Str),
        ("engagement", Kind::Str),
        ("summary", Kind::Str),
        ("start", Kind::Str),
        ("end", Kind::Str),
        ("action_ids", Kind::Json),
        ("parts", Kind::Json),
        ("cancelled_at", Kind::Str),
        ("source", Kind::Str),
    ],
};

pub struct FollowThroughMatch {
    pub person_id: String,
    pub person_name: String,
    pub engagement: String,
    pub parts: Vec<String>,
    pub trait_names: Vec<String>,
}

/// A recorded cancellation as follow-through reads it: when it was
/// planned, its actions, and its person where their engagement looks.
pub fn to_event(row: &Row) -> anyhow::Result<Event> {
    let person = rows::text(row, "person_id").unwrap_or_default();
    let facts = if rows::text(row, "engagement") == Some("for") {
        Facts { for_ids: Some(json!([row.get("person_id").cloned().unwrap_or(Value::Null)])), ..Default::default() }
    } else if person == SELF_ID || person.starts_with(SUBJECT_PREFIX) {
        Facts::default()
    } else {
        Facts { with_ids: Some(json!([row.get("person_id").cloned().unwrap_or(Value::Null)])), ..Default::default() }
    };
    let action_ids = match row.get("action_ids") {
        Some(Value::Array(items)) => items.iter().map(|i| python_str(Some(i))).collect(),
        _ => Vec::new(),
    };
    Ok(Event {
        id: rows::text(row, "event_id").map(str::to_string),
        summary: rows::text(row, "summary").map(str::to_string),
        start: Some(parse_iso(rows::text(row, "start").unwrap_or_default(), None)?),
        end: Some(parse_iso(rows::text(row, "end").unwrap_or_default(), None)?),
        action_ids: Some(action_ids),
        facts: Some(facts),
        status: Some("cancelled".into()),
        ..Default::default()
    })
}

/// `datetime.fromisoformat(text)`, or None if it can't be read.
fn time(value: Option<&Value>) -> Option<Moment> {
    value.and_then(Value::as_str).filter(|t| !t.is_empty()).and_then(|t| parse_iso(t, None).ok())
}

/// A row's start, for sorting; one that can't be read sorts last.
fn start_of(row: &Row) -> DateTime<FixedOffset> {
    time(row.get("start")).unwrap_or_else(|| DateTime::<chrono::Utc>::MAX_UTC.fixed_offset())
}

#[derive(Clone)]
pub struct Cancellations {
    sheet: RowSheet,
    people: People,
    traits: Traits,
    actions: Actions,
    habits: Option<Habits>,
}

impl Cancellations {
    pub fn ensure(
        sheets: &SheetsClient,
        spreadsheet_id: &str,
        people: People,
        traits: Traits,
        actions: Actions,
        habits: Option<Habits>,
    ) -> anyhow::Result<Cancellations> {
        let sheet = RowSheet::ensure(sheets, spreadsheet_id, CANCELLATIONS.0, CANCELLATIONS.1, CANCELLATION_ROW, &["id", "person_id"])?;
        Ok(Cancellations { sheet, people, traits, actions, habits })
    }

    pub fn whole_tabs(&self) -> Vec<TabRange> {
        let mut tabs = vec![self.sheet.whole_tab(), self.traits.whole_tab()];
        tabs.extend(self.people.whole_tabs());
        tabs.extend(self.actions.whole_tabs());
        if let Some(habits) = &self.habits {
            tabs.push(habits.whole_tab());
        }
        tabs
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheet.prefetch(ranges)
    }

    pub fn all(&self) -> anyhow::Result<Vec<Row>> {
        self.sheet.read()
    }

    /// Each person's (and habit's) cancellations, newest first, as
    /// CancelledEvent JSON.
    pub fn by_person(&self) -> anyhow::Result<Vec<(String, Vec<Value>)>> {
        let mut rows = self.all()?;
        rows.sort_by(|a, b| start_of(b).cmp(&start_of(a)));
        let mut found: Vec<(String, Vec<Value>)> = Vec::new();
        for row in rows {
            let Some(person) = row.get("person_id").filter(|p| truthy(Some(p))).map(|p| python_str(Some(p))) else { continue };
            let when = |key: &str| time(row.get(key)).map(|m| json!(py::pydantic_datetime(&m))).unwrap_or(Value::Null);
            let item = json!({
                "event_id": row.get("event_id").cloned().unwrap_or(Value::Null),
                "summary": row.get("summary").cloned().unwrap_or(Value::Null),
                "start": when("start"),
                "end": when("end"),
                "action_ids": row.get("action_ids").cloned().unwrap_or(Value::Null),
                "engagement": row.get("engagement").cloned().unwrap_or(Value::Null),
                "parts": row.get("parts").cloned().unwrap_or(Value::Null),
                "cancelled_at": when("cancelled_at"),
                "source": row.get("source").cloned().unwrap_or(Value::Null),
            });
            match found.iter_mut().find(|(k, _)| *k == person) {
                Some(slot) => slot.1.push(item),
                None => found.push((person, vec![item])),
            }
        }
        Ok(found)
    }

    /// Who `event`, cancelled, counts against, and under which parts.
    pub fn matches(&self, event: &Event) -> anyhow::Result<Vec<FollowThroughMatch>> {
        let traits = self.traits.all()?;
        let tree = self.actions.tree()?;
        let facts = event.facts.clone().unwrap_or_default();
        let mut found = Vec::new();
        for person in self.people.get_people(None)? {
            let person_id = python_str(person.get("id"));
            let mut by_engagement: Vec<(String, Vec<String>, Vec<String>)> = Vec::new();
            for (t, parts) in traits_for(person.get("traits"), &traits) {
                for (part, key) in parts.iter().zip(part_keys(&parts)) {
                    if part.get("kind").and_then(Value::as_str) != Some("follow_through") || !part_problems(part).is_empty() {
                        continue;
                    }
                    let engagement = part.get("engagement_type").and_then(Value::as_str).unwrap_or("with").to_string();
                    let there = if engagement == "with" {
                        person_id == SELF_ID || facts.with().contains(&person_id)
                    } else {
                        facts.for_().contains(&person_id)
                    };
                    if !there || of_action(std::slice::from_ref(event), part.get("action"), Some(&tree)).is_empty() {
                        continue;
                    }
                    let index = match by_engagement.iter().position(|(e, _, _)| *e == engagement) {
                        Some(i) => i,
                        None => {
                            by_engagement.push((engagement.clone(), Vec::new(), Vec::new()));
                            by_engagement.len() - 1
                        }
                    };
                    by_engagement[index].1.push(format!("{}/{key}", python_str(t.id.as_ref())));
                    let name = trait_name(&t);
                    if !by_engagement[index].2.contains(&name) {
                        by_engagement[index].2.push(name);
                    }
                }
            }
            let person_name = person.get("name").filter(|n| truthy(Some(n))).map(|n| python_str(Some(n))).unwrap_or(person_id.clone());
            for (engagement, keys, names) in by_engagement {
                found.push(FollowThroughMatch { person_id: person_id.clone(), person_name: person_name.clone(), engagement, parts: keys, trait_names: names });
            }
        }
        if let Some(habits) = &self.habits {
            for habit in habits.all()? {
                let Some(id) = habit.get("id").filter(|i| truthy(Some(i))).map(|i| python_str(Some(i))) else { continue };
                if rows::text(&habit, "status") != Some("active")
                    || of_action(std::slice::from_ref(event), habit.get("action_id"), Some(&tree)).is_empty()
                {
                    continue;
                }
                let (mut keys, mut names) = (Vec::new(), Vec::new());
                for (t, parts) in traits_for(habit.get("traits"), &traits) {
                    for (part, key) in parts.iter().zip(part_keys(&parts)) {
                        if part.get("kind").and_then(Value::as_str) != Some("follow_through") || !part_problems(part).is_empty() {
                            continue;
                        }
                        if part.get("engagement_type").and_then(Value::as_str).unwrap_or("with") != "with"
                            || of_action(std::slice::from_ref(event), part.get("action"), Some(&tree)).is_empty()
                        {
                            continue;
                        }
                        keys.push(format!("{}/{key}", python_str(t.id.as_ref())));
                        let name = trait_name(&t);
                        if !names.contains(&name) {
                            names.push(name);
                        }
                    }
                }
                if !keys.is_empty() {
                    found.push(FollowThroughMatch {
                        person_id: subject_id(&id),
                        person_name: format!("the {} habit", python_str(habit.get("name"))),
                        engagement: "with".into(),
                        parts: keys,
                        trait_names: names,
                    });
                }
            }
        }
        Ok(found)
    }

    /// Record `event` (as planned) as cancelled for everyone it counts
    /// against; returns the rows written.
    pub fn record(&self, event: &Event, source: &str, at: Option<Moment>) -> anyhow::Result<Vec<Row>> {
        let at = at.unwrap_or_else(crate::determinism::now);
        let event_id = event.id.clone().unwrap_or_else(|| "None".into());
        let rows: Vec<Row> = self
            .matches(event)?
            .into_iter()
            .map(|m| {
                let mut row = CANCELLATION_ROW.empty();
                row.insert("id".into(), json!(format!("{event_id}/{}/{}", m.person_id, m.engagement)));
                row.insert("event_id".into(), json!(event.id));
                row.insert("person_id".into(), json!(m.person_id));
                row.insert("engagement".into(), json!(m.engagement));
                row.insert("summary".into(), json!(event.summary));
                row.insert("start".into(), json!(py::isoformat(&event.start())));
                row.insert("end".into(), json!(py::isoformat(&event.end())));
                row.insert("action_ids".into(), json!(event.action_ids.clone().unwrap_or_default()));
                row.insert("parts".into(), json!(m.parts));
                row.insert("cancelled_at".into(), json!(py::isoformat(&at)));
                row.insert("source".into(), json!(source));
                row
            })
            .collect();
        if rows.is_empty() {
            return Ok(Vec::new());
        }
        let written: Vec<&Value> = rows.iter().map(|r| &r["id"]).collect();
        let oldest = at - Duration::days(KEEP_DAYS);
        let mut kept: Vec<Row> =
            self.all()?.into_iter().filter(|r| !written.contains(&r.get("id").unwrap_or(&Value::Null)) && start_of(r) >= oldest).collect();
        kept.extend(rows.iter().cloned());
        kept.sort_by(|a, b| {
            let key = |r: &Row| (rows::text(r, "start").unwrap_or_default().to_string(), rows::text(r, "id").unwrap_or_default().to_string());
            key(a).cmp(&key(b))
        });
        self.sheet.write(&kept)?;
        Ok(rows)
    }
}

fn trait_name(t: &Trait) -> String {
    match t.name.as_ref().filter(|n| truthy(Some(n))) {
        Some(name) => python_str(Some(name)),
        None => python_str(t.id.as_ref()),
    }
}

/// The active traits that apply to someone with `spec` as their traits,
/// each with its parts for them.
pub fn traits_for(spec: Option<&Value>, traits: &[Trait]) -> Vec<(Trait, Vec<Value>)> {
    let active: Vec<&Trait> =
        traits.iter().filter(|t| t.status_str() == Some("active") && t.id.as_ref().is_some_and(|i| truthy(Some(i)))).collect();
    let empty = Map::new();
    let spec = spec.and_then(Value::as_object).unwrap_or(&empty);
    let select = spec.get("select").cloned().unwrap_or(json!("all"));
    let overrides = spec.get("parts").and_then(Value::as_object).cloned().unwrap_or_default();
    let chosen: Vec<&Trait> = if select == json!("all") {
        active
    } else {
        let listed: Vec<Value> = select.as_array().cloned().unwrap_or_default();
        active.into_iter().filter(|t| listed.contains(t.id.as_ref().unwrap())).collect()
    };
    chosen
        .into_iter()
        .map(|t| {
            let id = python_str(t.id.as_ref());
            let own = overrides.get(&id).filter(|p| truthy(Some(p))).cloned();
            let parts = own.or_else(|| t.parts.clone().filter(|p| truthy(Some(p)))).unwrap_or(json!([]));
            (t.clone(), parts.as_array().cloned().unwrap_or_default())
        })
        .collect()
}

/// `events` of the action `action_id` (or any action in the group it
/// names), or all of them without one.
pub fn of_action(events: &[Event], action_id: Option<&Value>, tree: Option<&ActionTree>) -> Vec<Event> {
    let Some(action_id) = action_id.filter(|a| truthy(Some(a))) else {
        return events.to_vec();
    };
    let matches = |candidate: &str| -> bool {
        if Some(candidate) == action_id.as_str() {
            return true;
        }
        let Some(tree) = tree else { return false };
        let Some(action) = tree.by_id(candidate) else { return false };
        tree.groups.chain(action).iter().skip(1).any(|g| g.get("id") == Some(action_id))
    };
    events.iter().filter(|e| e.action_ids.iter().flatten().any(|a| matches(a))).cloned().collect()
}
