//! Cancellations: the events the user cancelled that count against
//! someone's follow-through, one row per event per person (and engagement),
//! in the Cancellations tab.

use crate::event::{parse_iso, Event, Moment};
use crate::facts::{Facts, SELF_ID};
use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::stores::actions::{share_ids, ActionShare};
use crate::contract::{DateTimeText, RowItem, Schema};
use crate::sheet::rows::{self, RowSheet, RowType};
use crate::sheet::CANCELLATIONS;
use crate::stores::actions::{ActionTree, Actions};
use crate::stores::groups::{set, str_or_none, truthy};
use crate::stores::habits::{subject_id, Habits, SUBJECT_PREFIX};
use crate::stores::people::People;
use crate::stores::traits::{part_keys, part_problems, Trait, TraitStatus, Traits};
use chrono::{DateTime, Duration, FixedOffset};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

pub const KEEP_DAYS: i64 = 400 + 90;

/// An event the user cancelled, as it counts against one person's
/// follow-through -- one row of the Cancellations tab.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct CancelledEvent {
    pub event_id: Option<String>,
    pub summary: Option<String>,
    pub start: Option<DateTimeText>,
    pub end: Option<DateTimeText>,
    pub action_ids: Option<Vec<String>>,
    pub engagement: Option<String>,
    pub parts: Option<Vec<String>>,
    pub cancelled_at: Option<DateTimeText>,
    pub source: Option<String>,
}

/// One row of the Cancellations tab: an event the user cancelled, for one
/// person (or habit) and engagement.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, RowItem)]
pub struct Cancellation {
    pub id: Option<String>,
    pub event_id: Option<String>,
    pub person_id: Option<String>,
    pub engagement: Option<String>,
    pub summary: Option<String>,
    pub start: Option<String>,
    pub end: Option<String>,
    pub action_ids: Option<Vec<String>>,
    pub parts: Option<Vec<String>>,
    pub cancelled_at: Option<String>,
    pub source: Option<String>,
}

pub const CANCELLATION_ROW: RowType = <Cancellation as rows::RowItem>::ROW_TYPE;

pub struct FollowThroughMatch {
    pub person_id: String,
    pub person_name: String,
    pub engagement: String,
    pub parts: Vec<String>,
    pub trait_names: Vec<String>,
}

/// A recorded cancellation as follow-through reads it: when it was
/// planned, its actions, and its person where their engagement looks.
pub fn to_event(row: &Cancellation) -> anyhow::Result<Event> {
    let person = row.person_id.as_deref().unwrap_or_default();
    let facts = if row.engagement.as_deref() == Some("for") {
        Facts { for_ids: Some(json!([row.person_id])), ..Default::default() }
    } else if person == SELF_ID || person.starts_with(SUBJECT_PREFIX) {
        Facts::default()
    } else {
        Facts { with_ids: Some(json!([row.person_id])), ..Default::default() }
    };
    Ok(Event {
        id: row.event_id.clone(),
        summary: row.summary.clone(),
        start: Some(parse_iso(row.start.as_deref().unwrap_or_default(), None)?),
        end: Some(parse_iso(row.end.as_deref().unwrap_or_default(), None)?),
        action_ids: Some(row.action_ids.iter().flatten().map(|id| ActionShare::whole(id.clone())).collect()),
        facts: Some(facts),
        status: Some("cancelled".into()),
        ..Default::default()
    })
}

/// `datetime.fromisoformat(text)`, or None if it can't be read.
fn time(text: Option<&str>) -> Option<Moment> {
    text.filter(|t| !t.is_empty()).and_then(|t| parse_iso(t, None).ok())
}

/// A row's start, for sorting; one that can't be read sorts last.
fn start_of(row: &Cancellation) -> DateTime<FixedOffset> {
    time(row.start.as_deref()).unwrap_or_else(|| DateTime::<chrono::Utc>::MAX_UTC.fixed_offset())
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

    pub fn all(&self) -> anyhow::Result<Vec<Cancellation>> {
        self.sheet.read_items()
    }

    /// Each person's (and habit's) cancellations, newest first.
    pub fn by_person(&self) -> anyhow::Result<Vec<(String, Vec<CancelledEvent>)>> {
        let mut rows = self.all()?;
        rows.sort_by(|a, b| start_of(b).cmp(&start_of(a)));
        let mut found: Vec<(String, Vec<CancelledEvent>)> = Vec::new();
        for row in rows {
            let Some(person) = set(row.person_id.as_deref()).map(str::to_string) else { continue };
            let when = |text: &Option<String>| time(text.as_deref()).map(|m| DateTimeText(py::pydantic_datetime(&m)));
            let item = CancelledEvent {
                event_id: row.event_id.clone(),
                summary: row.summary.clone(),
                start: when(&row.start),
                end: when(&row.end),
                action_ids: row.action_ids.clone(),
                engagement: row.engagement.clone(),
                parts: row.parts.clone(),
                cancelled_at: when(&row.cancelled_at),
                source: row.source.clone(),
            };
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
            let person_id = str_or_none(person.id.as_deref());
            let mut by_engagement: Vec<(String, Vec<String>, Vec<String>)> = Vec::new();
            let spec = person.traits.clone().map(Value::Object);
            for (t, parts) in traits_for(spec.as_ref(), &traits) {
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
                    by_engagement[index].1.push(format!("{}/{key}", str_or_none(t.id.as_deref())));
                    let name = trait_name(&t);
                    if !by_engagement[index].2.contains(&name) {
                        by_engagement[index].2.push(name);
                    }
                }
            }
            let person_name = set(person.name.as_deref()).map(str::to_string).unwrap_or(person_id.clone());
            for (engagement, keys, names) in by_engagement {
                found.push(FollowThroughMatch { person_id: person_id.clone(), person_name: person_name.clone(), engagement, parts: keys, trait_names: names });
            }
        }
        if let Some(habits) = &self.habits {
            for habit in habits.all()? {
                let Some(id) = set(habit.id.as_deref()).map(str::to_string) else { continue };
                let action_id = habit.action_id.clone().map(Value::String);
                if habit.status != Some(crate::stores::people::Status::Active)
                    || of_action(std::slice::from_ref(event), action_id.as_ref(), Some(&tree)).is_empty()
                {
                    continue;
                }
                let (mut keys, mut names) = (Vec::new(), Vec::new());
                let spec = habit.traits.clone().map(Value::Object);
                for (t, parts) in traits_for(spec.as_ref(), &traits) {
                    for (part, key) in parts.iter().zip(part_keys(&parts)) {
                        if part.get("kind").and_then(Value::as_str) != Some("follow_through") || !part_problems(part).is_empty() {
                            continue;
                        }
                        if part.get("engagement_type").and_then(Value::as_str).unwrap_or("with") != "with"
                            || of_action(std::slice::from_ref(event), part.get("action"), Some(&tree)).is_empty()
                        {
                            continue;
                        }
                        keys.push(format!("{}/{key}", str_or_none(t.id.as_deref())));
                        let name = trait_name(&t);
                        if !names.contains(&name) {
                            names.push(name);
                        }
                    }
                }
                if !keys.is_empty() {
                    found.push(FollowThroughMatch {
                        person_id: subject_id(&id),
                        person_name: format!("the {} habit", str_or_none(habit.name.as_deref())),
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
    pub fn record(&self, event: &Event, source: &str, at: Option<Moment>) -> anyhow::Result<Vec<Cancellation>> {
        let at = at.unwrap_or_else(crate::determinism::now);
        let event_id = event.id.clone().unwrap_or_else(|| "None".into());
        let rows: Vec<Cancellation> = self
            .matches(event)?
            .into_iter()
            .map(|m| Cancellation {
                id: Some(format!("{event_id}/{}/{}", m.person_id, m.engagement)),
                event_id: event.id.clone(),
                person_id: Some(m.person_id),
                engagement: Some(m.engagement),
                summary: event.summary.clone(),
                start: Some(py::isoformat(&event.start())),
                end: Some(py::isoformat(&event.end())),
                action_ids: Some(share_ids(event.action_ids.as_deref().unwrap_or_default())),
                parts: Some(m.parts),
                cancelled_at: Some(py::isoformat(&at)),
                source: Some(source.to_string()),
            })
            .collect();
        if rows.is_empty() {
            return Ok(Vec::new());
        }
        let written: Vec<&Option<String>> = rows.iter().map(|r| &r.id).collect();
        let oldest = at - Duration::days(KEEP_DAYS);
        let mut kept: Vec<Cancellation> = self.all()?.into_iter().filter(|r| !written.contains(&&r.id) && start_of(r) >= oldest).collect();
        kept.extend(rows.iter().cloned());
        kept.sort_by(|a, b| {
            let key = |r: &Cancellation| (r.start.clone().unwrap_or_default(), r.id.clone().unwrap_or_default());
            key(a).cmp(&key(b))
        });
        self.sheet.write_items(&kept)?;
        Ok(rows)
    }
}

fn trait_name(t: &Trait) -> String {
    match t.name.as_deref().filter(|n| !n.is_empty()) {
        Some(name) => name.to_string(),
        None => str_or_none(t.id.as_deref()),
    }
}

/// The active traits that apply to someone with `spec` as their traits,
/// each with its parts for them.
pub fn traits_for(spec: Option<&Value>, traits: &[Trait]) -> Vec<(Trait, Vec<Value>)> {
    let active: Vec<&Trait> =
        traits.iter().filter(|t| t.status == Some(TraitStatus::Active) && t.id.as_deref().is_some_and(|i| !i.is_empty())).collect();
    let empty = Map::new();
    let spec = spec.and_then(Value::as_object).unwrap_or(&empty);
    let select = spec.get("select").cloned().unwrap_or(json!("all"));
    let overrides = spec.get("parts").and_then(Value::as_object).cloned().unwrap_or_default();
    let chosen: Vec<&Trait> = if select == json!("all") {
        active
    } else {
        let listed: Vec<Value> = select.as_array().cloned().unwrap_or_default();
        active.into_iter().filter(|t| listed.contains(&json!(t.id))).collect()
    };
    chosen
        .into_iter()
        .map(|t| {
            let id = str_or_none(t.id.as_deref());
            let own = overrides.get(&id).filter(|p| truthy(Some(p))).and_then(Value::as_array).cloned();
            (t.clone(), own.unwrap_or_else(|| t.part_values()))
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
        tree.groups.enclosing(action).iter().any(|g| g.id.as_deref().is_some() && g.id.as_deref() == action_id.as_str())
    };
    events.iter().filter(|e| e.action_ids.iter().flatten().any(|a| matches(&a.action_id))).cloned().collect()
}

#[cfg(test)]
#[path = "cancellations_tests.rs"]
mod tests;
