//! Judging traits: the last step of a compaction, once its events' facts
//! are written -- each event with facts judged for each person (and habit)
//! it was about, against the judgment parts of the traits that apply to
//! them.

use crate::event::{Event, Moment};
use crate::facts::SELF_ID;
use crate::py;

use crate::stores::action_calendar::ActionCalendar;
use crate::stores::actions::{ActionTree, Actions};
use crate::stores::cancellations::{of_action, traits_for};
use crate::stores::groups::{python_str, repr_or_none, str_or_none, truthy};
use crate::stores::habits::{self, subject_id, Habit, Habits};
use crate::stores::locations::Locations;
use crate::stores::people::{People, Person};
use crate::stores::person_facts::PersonFacts;
use crate::stores::traits::{fact_lookbacks, part_keys, part_problems, trait_fact_id, Trait, TraitStatus, Traits};
use chrono::Duration;
use serde_json::{json, Map, Value};

pub const MAX_REASONING_CHARS: usize = 300;
pub const SCORE_DAYS: i64 = 7;
pub const DEFAULT_WINDOW_DAYS: i64 = 30;

pub use super::guide::{BACKFILL_INSTRUCTIONS, JUDGMENT_INSTRUCTIONS as INSTRUCTIONS};

fn when(moment: &Moment) -> Value {
    json!(py::pydantic_datetime(moment))
}

/// Someone (or a habit) an event can be judged for.
#[derive(Debug, Clone)]
pub struct Subject {
    pub id: String,
    pub name: Option<Value>,
    pub context: Option<Value>,
    /// A habit's note: what it's judged as practice of.
    pub note: Option<Value>,
    pub traits: Option<Value>,
}

impl Subject {
    fn of(person: &Person) -> Subject {
        let text = |t: &Option<String>| t.clone().map(Value::String);
        Subject {
            id: person.id.clone().unwrap_or_default(),
            name: text(&person.name),
            context: text(&person.context),
            note: None,
            traits: person.traits.clone().map(Value::Object),
        }
    }

    fn bare(id: &str) -> Subject {
        Subject { id: id.into(), name: Some(json!(id)), context: None, note: None, traits: None }
    }
}

#[derive(Debug, Clone)]
pub struct JudgmentPart {
    pub key: String,
    pub trait_name: String,
    pub definition: Option<Value>,
    pub engagement: String,
    pub rubric: Value,
    pub ratings: Value,
    pub facts: Vec<String>,
}

impl JudgmentPart {
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("key".into(), json!(self.key));
        out.insert("trait_name".into(), json!(self.trait_name));
        out.insert("definition".into(), self.definition.clone().unwrap_or(Value::Null));
        out.insert("engagement".into(), json!(self.engagement));
        out.insert("rubric".into(), self.rubric.clone());
        out.insert("ratings".into(), self.ratings.clone());
        out.insert("facts".into(), json!(self.facts));
        Value::Object(out)
    }
}

#[derive(Debug, Clone)]
pub struct PersonHistory {
    pub days: i64,
    pub actions: Vec<(String, i64)>,
    pub locations: Vec<(String, i64)>,
}

fn counts_json(counts: &[(String, i64)]) -> Value {
    Value::Object(counts.iter().map(|(k, v)| (k.clone(), json!(v))).collect())
}

impl PersonHistory {
    pub fn to_json(&self) -> Value {
        json!({"days": self.days, "actions": counts_json(&self.actions), "locations": counts_json(&self.locations)})
    }
}

#[derive(Debug, Clone)]
pub struct EventJudgmentsDue {
    pub event_id: String,
    pub summary: Option<String>,
    pub start: Moment,
    pub end: Moment,
    pub people: Vec<(String, Vec<String>)>,
    pub current: Option<Map<String, Value>>,
}

impl EventJudgmentsDue {
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("event_id".into(), json!(self.event_id));
        out.insert("summary".into(), json!(self.summary));
        out.insert("start".into(), when(&self.start));
        out.insert("end".into(), when(&self.end));
        out.insert("people".into(), Value::Object(self.people.iter().map(|(k, v)| (k.clone(), json!(v))).collect()));
        out.insert("current".into(), self.current.clone().map(Value::Object).unwrap_or(Value::Null));
        Value::Object(out)
    }
}

#[derive(Debug, Clone)]
pub struct JudgmentRequest {
    pub id: String,
    pub event_id: String,
    pub summary: Option<String>,
    pub start: Moment,
    pub end: Moment,
    pub person_id: String,
    pub trait_id: String,
    pub part: String,
    pub ratings: Value,
    pub current: Option<Value>,
}

#[derive(Debug, Clone)]
pub struct Judgment {
    pub request_id: String,
    pub rating: i64,
    pub reasoning: String,
}

#[derive(Debug, Clone, Default)]
pub struct JudgmentsDue {
    pub compaction_id: String,
    pub events: Vec<EventJudgmentsDue>,
    pub parts: Vec<JudgmentPart>,
    pub history: Vec<(String, PersonHistory)>,
    /// Each person's summaries of the trait facts their parts name: (person
    /// id, [("<trait id>/<fact id>", summary)]).
    pub facts: Vec<(String, Vec<(String, String)>)>,
    /// Each habit's note: (its subject id, note).
    pub habit_notes: Vec<(String, String)>,
}

fn pairs_json(pairs: &[(String, String)]) -> Value {
    Value::Object(pairs.iter().map(|(k, v)| (k.clone(), json!(v))).collect())
}

impl JudgmentsDue {
    pub fn count(&self) -> usize {
        self.events.iter().map(|e| e.people.iter().map(|(_, p)| p.len()).sum::<usize>()).sum()
    }

    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("compaction_id".into(), json!(self.compaction_id));
        out.insert("events".into(), Value::Array(self.events.iter().map(EventJudgmentsDue::to_json).collect()));
        out.insert("parts".into(), Value::Array(self.parts.iter().map(JudgmentPart::to_json).collect()));
        out.insert("history".into(), Value::Object(self.history.iter().map(|(k, h)| (k.clone(), h.to_json())).collect()));
        out.insert("facts".into(), Value::Object(self.facts.iter().map(|(k, f)| (k.clone(), pairs_json(f))).collect()));
        out.insert("habit_notes".into(), pairs_json(&self.habit_notes));
        out.insert("instructions".into(), json!(INSTRUCTIONS));
        Value::Object(out)
    }
}

pub struct HabitJudgmentsDue {
    pub backfill_id: String,
    pub habit_id: String,
    pub habit_name: String,
    pub since: Moment,
    pub until: Moment,
    pub due: JudgmentsDue,
}

impl HabitJudgmentsDue {
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("backfill_id".into(), json!(self.backfill_id));
        out.insert("habit_id".into(), json!(self.habit_id));
        out.insert("habit_name".into(), json!(self.habit_name));
        out.insert("since".into(), when(&self.since));
        out.insert("until".into(), when(&self.until));
        out.insert("events".into(), Value::Array(self.due.events.iter().map(EventJudgmentsDue::to_json).collect()));
        out.insert("parts".into(), Value::Array(self.due.parts.iter().map(JudgmentPart::to_json).collect()));
        out.insert("history".into(), Value::Object(self.due.history.iter().map(|(k, h)| (k.clone(), h.to_json())).collect()));
        out.insert("habit_notes".into(), pairs_json(&self.due.habit_notes));
        out.insert("instructions".into(), json!(BACKFILL_INSTRUCTIONS));
        Value::Object(out)
    }
}

pub struct JudgmentsResult {
    pub compaction_id: String,
    pub recorded: usize,
    pub remaining: Vec<String>,
    pub complete: bool,
    pub message: String,
}

impl JudgmentsResult {
    pub fn to_json(&self) -> Value {
        let mut out = Map::new();
        out.insert("compaction_id".into(), json!(self.compaction_id));
        out.insert("recorded".into(), json!(self.recorded));
        out.insert("remaining".into(), json!(self.remaining));
        out.insert("complete".into(), json!(self.complete));
        out.insert("message".into(), json!(self.message));
        Value::Object(out)
    }
}

/// `Counter(...).most_common()`: by count, most first, ties in the order
/// first seen.
fn most_common(items: impl IntoIterator<Item = String>) -> Vec<(String, i64)> {
    let mut counts: Vec<(String, i64)> = Vec::new();
    for item in items {
        match counts.iter_mut().find(|(k, _)| *k == item) {
            Some(slot) => slot.1 += 1,
            None => counts.push((item, 1)),
        }
    }
    counts.sort_by(|a, b| b.1.cmp(&a.1));
    counts
}

fn is_judgment(part: &Value) -> bool {
    part.is_object() && part.get("kind").and_then(Value::as_str) == Some("judgment") && part_problems(part).is_empty()
}

fn engagement_of(part: &Value) -> String {
    part.get("engagement_type").map(|v| python_str(Some(v))).unwrap_or_else(|| "with".into())
}

fn facts_locations(event: &Event) -> Vec<String> {
    event.facts.as_ref().map(|f| f.locations()).unwrap_or_default()
}

/// `_with`: whether `person_id` took part in `event` -- the user always
/// did; anyone else if it was with or for them.
fn with(event: &Event, person_id: &str) -> bool {
    if person_id == SELF_ID {
        return true;
    }
    let Some(facts) = &event.facts else { return false };
    facts.with().iter().any(|p| p == person_id) || facts.for_().iter().any(|p| p == person_id)
}

fn judgeable(event: &Event) -> bool {
    !event.is_cancelled() && event.facts.as_ref().is_some_and(|f| !f.is_empty())
}

fn lookup(names: &[(String, String)], id: &str) -> String {
    names.iter().rev().find(|(k, _)| k == id).map(|(_, v)| v.clone()).unwrap_or_else(|| id.to_string())
}

#[derive(Clone)]
pub struct Judging {
    pub client: ActionCalendar,
    pub actions: Actions,
    pub people: People,
    pub locations: Locations,
    pub traits: Traits,
    pub habits: Option<Habits>,
    /// Where people's trait fact summaries are kept, for the parts that
    /// name them.
    pub person_facts: Option<PersonFacts>,
}

struct Subjects {
    people: Vec<(String, Subject)>,
    scopes: Vec<(String, Value)>,
}

impl Subjects {
    fn get(&self, id: &str) -> Option<&Subject> {
        self.people.iter().rev().find(|(k, _)| k == id).map(|(_, s)| s)
    }

    fn scope(&self, id: &str) -> Option<&Value> {
        self.scopes.iter().rev().find(|(k, _)| k == id).map(|(_, s)| s)
    }

    fn part_of(&self, tree: Option<&ActionTree>, event: &Event, subject: &str) -> bool {
        match self.scope(subject) {
            Some(scope) => !of_action(std::slice::from_ref(event), Some(scope), tree).is_empty(),
            None => with(event, subject),
        }
    }
}

impl Judging {
    pub fn whole_tabs(&self) -> Vec<crate::google::sheets::TabRange> {
        let mut tabs = vec![self.traits.whole_tab()];
        if let Some(habits) = &self.habits {
            tabs.push(habits.whole_tab());
        }
        if let Some(facts) = &self.person_facts {
            tabs.extend(facts.whole_tabs());
        }
        tabs
    }

    fn subjects(&self) -> anyhow::Result<Subjects> {
        let mut people: Vec<(String, Subject)> = Vec::new();
        let set = |people: &mut Vec<(String, Subject)>, key: &str, subject: Subject| match people.iter_mut().find(|(k, _)| k == key) {
            Some(slot) => slot.1 = subject,
            None => people.push((key.to_string(), subject)),
        };
        for person in self.people.all()? {
            if person.id.as_deref().is_some_and(|i| !i.is_empty()) {
                let subject = Subject::of(&person);
                set(&mut people, &subject.id.clone(), subject);
            }
        }
        if !people.iter().any(|(k, _)| k == SELF_ID) {
            people.push((SELF_ID.into(), Subject { id: SELF_ID.into(), name: Some(json!("Me")), context: None, note: None, traits: None }));
        }
        let mut scopes: Vec<(String, Value)> = Vec::new();
        if let Some(store) = &self.habits {
            for habit in store.all()? {
                let (Some(id), Some(action_id)) = (habit.id.clone().filter(|i| !i.is_empty()), habit.action_id.clone().filter(|a| !a.is_empty())) else {
                    continue;
                };
                if habit.status != Some(crate::stores::people::Status::Active) {
                    continue;
                }
                let key = subject_id(&id);
                let name = Some(Value::String(habit.name.clone().filter(|n| !n.is_empty()).unwrap_or(id)));
                set(
                    &mut people,
                    &key,
                    Subject {
                        id: key.clone(),
                        name,
                        context: None,
                        note: habit.note.clone().map(Value::String),
                        traits: habit.traits.clone().map(Value::Object),
                    },
                );
                match scopes.iter_mut().find(|(k, _)| *k == key) {
                    Some(slot) => slot.1 = Value::String(action_id),
                    None => scopes.push((key, Value::String(action_id))),
                }
            }
        }
        Ok(Subjects { people, scopes })
    }

    fn active_traits(&self) -> anyhow::Result<Vec<Trait>> {
        Ok(self.traits.all()?.into_iter().filter(|t| t.status == Some(TraitStatus::Active) && t.id.as_deref().is_some_and(|i| !i.is_empty())).collect())
    }

    fn names(&self, subjects: &Subjects) -> anyhow::Result<Vec<(String, String)>> {
        let mut names: Vec<(String, String)> = subjects.people.iter().map(|(i, p)| (i.clone(), if truthy(p.name.as_ref()) { python_str(p.name.as_ref()) } else { i.clone() })).collect();
        for a in self.actions.all()? {
            if let Some(id) = a.id.filter(|i| !i.is_empty()) {
                names.push((id.clone(), a.name.filter(|n| !n.is_empty()).unwrap_or(id)));
            }
        }
        for l in self.locations.all()? {
            if let Some(id) = l.id.filter(|i| !i.is_empty()) {
                names.push((id.clone(), l.name.filter(|n| !n.is_empty()).unwrap_or(id)));
            }
        }
        Ok(names)
    }

    fn list_events(&self, time_min: &Moment, time_max: &Moment) -> anyhow::Result<Vec<Event>> {
        self.client.list_events(time_min, time_max)
    }

    /// `requests`: every judgment `event_ids`' events call for, in event
    /// order; only those not made yet unless `include_judged`.
    pub fn requests(&self, event_ids: &[String], span: (Moment, Moment), include_judged: bool) -> anyhow::Result<Vec<JudgmentRequest>> {
        if event_ids.is_empty() {
            return Ok(Vec::new());
        }
        let listed: Vec<Event> = self
            .list_events(&(span.0 - Duration::days(self.lookback()?)), &(span.1 + Duration::seconds(1)))?
            .into_iter()
            .filter(|e| !e.is_cancelled())
            .collect();
        let events: Vec<Event> = listed.iter().filter(|e| e.id.as_ref().is_some_and(|i| event_ids.contains(i))).cloned().collect();
        let history: Vec<Event> = listed.iter().filter(|e| e.facts.is_some()).cloned().collect();
        self.requests_for(&events, &history, include_judged, None)
    }

    /// `habit`: the active habit with this id, or else this name.
    pub fn habit(&self, id_or_name: &str) -> anyhow::Result<Habit> {
        let all = match &self.habits {
            Some(h) => h.all()?,
            None => Vec::new(),
        };
        let habit = habits::find(&all, id_or_name)?;
        if habit.status != Some(crate::stores::people::Status::Active) {
            anyhow::bail!(
                "{} ({}) is {}: only an active habit is judged",
                repr_or_none(habit.name.as_deref()),
                str_or_none(habit.id.as_deref()),
                str_or_none(habit.status.as_ref().map(crate::contract::wire_text).as_deref())
            );
        }
        Ok(habit)
    }

    pub fn backfill_days(&self, habit: &Habit) -> anyhow::Result<i64> {
        let traits = self.active_traits()?;
        let spec = habit.traits.clone().map(Value::Object);
        let windows: Vec<i64> = traits_for(spec.as_ref(), &traits)
            .into_iter()
            .flat_map(|(_, parts)| parts)
            .filter(is_judgment)
            .map(|part| {
                let window = part.get("window_days").filter(|w| truthy(Some(w)));
                window.and_then(Value::as_i64).unwrap_or(DEFAULT_WINDOW_DAYS)
            })
            .collect();
        Ok(windows.into_iter().max().unwrap_or(DEFAULT_WINDOW_DAYS) + SCORE_DAYS)
    }

    pub fn habit_requests(&self, habit: &Habit, since: Moment, until: Moment, include_judged: bool) -> anyhow::Result<Vec<JudgmentRequest>> {
        let listed: Vec<Event> = self
            .list_events(&(since - Duration::days(self.lookback()?)), &until)?
            .into_iter()
            .filter(|e| !e.is_cancelled())
            .collect();
        let events: Vec<Event> = listed.iter().filter(|e| since <= e.start() && e.start() < until).cloned().collect();
        let history: Vec<Event> = listed.iter().filter(|e| e.facts.is_some()).cloned().collect();
        let only = subject_id(habit.id.as_deref().unwrap_or("None"));
        self.requests_for(&events, &history, include_judged, Some(&only))
    }

    pub fn requests_for(&self, events: &[Event], _history: &[Event], include_judged: bool, only: Option<&str>) -> anyhow::Result<Vec<JudgmentRequest>> {
        let traits = self.active_traits()?;
        let mut events: Vec<&Event> = events.iter().filter(|e| judgeable(e)).collect();
        if events.is_empty() {
            return Ok(Vec::new());
        }
        let subjects = self.subjects()?;
        let tree = if subjects.scopes.is_empty() { None } else { Some(self.actions.tree()?) };
        let mut requests: Vec<JudgmentRequest> = Vec::new();
        events.sort_by_key(|e| e.start());
        for event in events {
            let facts = event.facts.as_ref().unwrap();
            let mut about: Vec<(String, &str)> = vec![(SELF_ID.into(), "with")];
            about.extend(facts.with().into_iter().map(|p| (p, "with")));
            about.extend(facts.for_().into_iter().map(|p| (p, "for")));
            for (h, _) in &subjects.scopes {
                if subjects.part_of(tree.as_ref(), event, h) {
                    about.push((h.clone(), "with"));
                }
            }
            for (person_id, engagement) in about {
                if only.is_some_and(|o| o != person_id) {
                    continue;
                }
                let person = subjects.get(&person_id).cloned().unwrap_or_else(|| Subject::bare(&person_id));
                for (t, parts) in traits_for(person.traits.as_ref(), &traits) {
                    let trait_id = str_or_none(t.id.as_deref());
                    for (part, key) in parts.iter().zip(part_keys(&parts)) {
                        if !is_judgment(part) || engagement_of(part) != engagement {
                            continue;
                        }
                        let current = event
                            .judgments
                            .as_ref()
                            .and_then(|j| j.get(&person_id))
                            .filter(|v| truthy(Some(v)))
                            .and_then(|p| p.get(&trait_id))
                            .and_then(|t| t.get(&key))
                            .cloned();
                        if current.is_some() && !include_judged {
                            continue;
                        }
                        requests.push(JudgmentRequest {
                            id: format!("{}/{}/{}/{}", event.id.clone().unwrap_or_default(), person.id, trait_id, key),
                            event_id: event.id.clone().unwrap_or_default(),
                            summary: event.summary.clone(),
                            start: event.start(),
                            end: event.end(),
                            person_id: person.id.clone(),
                            trait_id: trait_id.clone(),
                            part: key.clone(),
                            ratings: part.get("ratings").cloned().unwrap_or(json!({})),
                            current,
                        });
                    }
                }
            }
        }
        Ok(requests)
    }

    /// `record`: write `judgments` onto their events, beside any already
    /// made. ValueError (a plain error), before writing anything, for any
    /// that doesn't answer a request or is off its scale.
    pub fn record(&self, requests: &[JudgmentRequest], judgments: &[Judgment]) -> anyhow::Result<usize> {
        let problems = check(requests, judgments);
        if !problems.is_empty() {
            anyhow::bail!("{}", problems.join("; "));
        }
        let by_id = |id: &str| requests.iter().rev().find(|r| r.id == id).unwrap();
        let mut events: Vec<String> = Vec::new();
        for j in judgments {
            let event_id = by_id(&j.request_id).event_id.clone();
            if !events.contains(&event_id) {
                events.push(event_id);
            }
        }
        for event_id in events {
            // Merged into the judgments as read, and written onto that
            // version, so none made since -- what's kept lagging -- is lost.
            crate::google::calendar::retry_stale(|| {
                let read = self.client.get_event(&event_id)?;
                let patch = Event {
                    id: Some(event_id.clone()),
                    judgments: Some(merged(read.judgments.as_ref(), requests, judgments, &event_id)),
                    etag: read.etag.clone(),
                    ..Default::default()
                };
                self.client.update_event(&patch)
            })?;
        }
        Ok(judgments.len())
    }

    pub fn lookback(&self) -> anyhow::Result<i64> {
        let traits = self.active_traits()?;
        let mut longest = 0;
        for (_, person) in self.subjects()?.people {
            for (_, parts) in traits_for(person.traits.as_ref(), &traits) {
                for part in parts.iter().filter(|p| is_judgment(p)) {
                    for (_, days) in fact_lookbacks(part) {
                        longest = longest.max(days);
                    }
                }
            }
        }
        Ok(longest)
    }

    pub fn due(&self, compaction_id: &str, requests: &[JudgmentRequest]) -> anyhow::Result<JudgmentsDue> {
        if requests.is_empty() {
            return Ok(JudgmentsDue { compaction_id: compaction_id.into(), ..Default::default() });
        }
        let first = requests.iter().map(|r| r.start).min().unwrap();
        let (parts, history) = self.parts_and_history(first)?;
        let people: Vec<&String> = requests.iter().map(|r| &r.person_id).collect();
        let named: Vec<(String, String)> = requests.iter().map(|r| (r.person_id.clone(), format!("{}/{}", r.trait_id, r.part))).collect();
        let parts: Vec<JudgmentPart> =
            parts.into_iter().filter(|p| named.iter().any(|(person, key)| *key == p.key || format!("{key}@{person}") == p.key)).collect();
        let (facts, habit_notes) = self.facts_of(requests, &parts)?;
        Ok(JudgmentsDue {
            compaction_id: compaction_id.into(),
            events: due_by_event(requests),
            parts,
            history: history.into_iter().filter(|(person, _)| people.contains(&person)).collect(),
            facts,
            habit_notes,
        })
    }

    /// The summaries of the trait facts `requests`' parts name, by person
    /// -- each fact once -- and the notes of the habits among them.
    #[allow(clippy::type_complexity)]
    fn facts_of(&self, requests: &[JudgmentRequest], parts: &[JudgmentPart]) -> anyhow::Result<(Vec<(String, Vec<(String, String)>)>, Vec<(String, String)>)> {
        let subjects = self.subjects()?;
        let kept = match &self.person_facts {
            Some(store) => store.by_person()?,
            None => Vec::new(),
        };
        let mut facts: Vec<(String, Vec<(String, String)>)> = Vec::new();
        let mut notes: Vec<(String, String)> = Vec::new();
        for request in requests {
            let person = &request.person_id;
            if let Some(note) = subjects.get(person).and_then(|s| s.note.as_ref()).and_then(Value::as_str) {
                if !notes.iter().any(|(k, _)| k == person) {
                    notes.push((person.clone(), note.to_string()));
                }
            }
            let key = format!("{}/{}", request.trait_id, request.part);
            let own = format!("{key}@{person}");
            let Some(part) = parts.iter().find(|p| p.key == own).or_else(|| parts.iter().find(|p| p.key == key)) else { continue };
            let theirs = kept.iter().find(|(p, _)| p == person).map(|(_, f)| f.as_slice()).unwrap_or_default();
            for name in &part.facts {
                let Some(fact) = trait_fact_id(name) else { continue };
                let Some((_, _, summary)) = theirs.iter().find(|(t, f, _)| *t == request.trait_id && f == fact) else { continue };
                let fact_key = format!("{}/{fact}", request.trait_id);
                let slot = match facts.iter().position(|(p, _)| p == person) {
                    Some(i) => i,
                    None => {
                        facts.push((person.clone(), Vec::new()));
                        facts.len() - 1
                    }
                };
                if !facts[slot].1.iter().any(|(k, _)| *k == fact_key) {
                    facts[slot].1.push((fact_key, summary.clone()));
                }
            }
        }
        Ok((facts, notes))
    }

    fn parts_and_history(&self, before: Moment) -> anyhow::Result<(Vec<JudgmentPart>, Vec<(String, PersonHistory)>)> {
        let traits = self.active_traits()?;
        let subjects = self.subjects()?;
        let tree = if subjects.scopes.is_empty() { None } else { Some(self.actions.tree()?) };
        let names = self.names(&subjects)?;
        let mut parts: Vec<JudgmentPart> = Vec::new();
        let mut looks: Vec<(String, i64)> = Vec::new();
        for (_, person) in &subjects.people {
            let overrides = person.traits.as_ref().and_then(|t| t.get("parts")).and_then(Value::as_object).cloned().unwrap_or_default();
            for (t, trait_parts) in traits_for(person.traits.as_ref(), &traits) {
                let trait_id = str_or_none(t.id.as_deref());
                let own = truthy(overrides.get(&trait_id));
                for (part, key) in trait_parts.iter().zip(part_keys(&trait_parts)) {
                    if !is_judgment(part) {
                        continue;
                    }
                    let lookbacks = fact_lookbacks(part);
                    let longest = lookbacks.iter().map(|(_, d)| *d).fold(looks.iter().find(|(k, _)| *k == person.id).map(|(_, v)| *v).unwrap_or(0), i64::max);
                    match looks.iter_mut().find(|(k, _)| *k == person.id) {
                        Some(slot) => slot.1 = longest,
                        None => looks.push((person.id.clone(), longest)),
                    }
                    let name = format!("{trait_id}/{key}{}", if own { format!("@{}", person.id) } else { String::new() });
                    if !parts.iter().any(|p| p.key == name) {
                        parts.push(JudgmentPart {
                            key: name,
                            trait_name: t.name.clone().filter(|n| !n.is_empty()).unwrap_or_else(|| trait_id.clone()),
                            definition: t.definition.clone().map(Value::String),
                            engagement: engagement_of(part),
                            rubric: part.get("rubric").cloned().unwrap_or(Value::Null),
                            ratings: part.get("ratings").cloned().unwrap_or(json!({})),
                            facts: lookbacks.iter().map(|(f, _)| f.clone()).collect(),
                        });
                    }
                }
            }
        }
        let mut history: Vec<(String, PersonHistory)> = Vec::new();
        let longest = looks.iter().map(|(_, d)| *d).max().unwrap_or(0);
        if longest > 0 {
            let listed: Vec<Event> = self
                .list_events(&(before - Duration::days(longest)), &before)?
                .into_iter()
                .filter(|e| !e.is_cancelled() && e.facts.is_some() && e.start() < before)
                .collect();
            for (person_id, days) in &looks {
                if *days == 0 {
                    continue;
                }
                let past: Vec<&Event> = listed
                    .iter()
                    .filter(|e| e.start() >= before - Duration::days(*days) && subjects.part_of(tree.as_ref(), e, person_id))
                    .collect();
                let actions = most_common(past.iter().flat_map(|e| e.action_ids.iter().flatten().map(|a| lookup(&names, &a.action_id))));
                let locations = most_common(past.iter().flat_map(|e| facts_locations(e).into_iter().map(|l| lookup(&names, &l))));
                history.push((person_id.clone(), PersonHistory { days: *days, actions, locations }));
            }
        }
        Ok((parts, history))
    }
}

#[cfg(test)]
#[path = "judgments_tests.rs"]
mod tests;

/// `due_by_event`: `requests`, grouped by event and then person, each as
/// just its part.
pub fn due_by_event(requests: &[JudgmentRequest]) -> Vec<EventJudgmentsDue> {
    let mut due: Vec<EventJudgmentsDue> = Vec::new();
    for request in requests {
        let index = match due.iter().position(|e| e.event_id == request.event_id) {
            Some(i) => i,
            None => {
                due.push(EventJudgmentsDue {
                    event_id: request.event_id.clone(),
                    summary: request.summary.clone(),
                    start: request.start,
                    end: request.end,
                    people: Vec::new(),
                    current: None,
                });
                due.len() - 1
            }
        };
        let event = &mut due[index];
        let key = format!("{}/{}", request.trait_id, request.part);
        match event.people.iter_mut().find(|(p, _)| *p == request.person_id) {
            Some(slot) => slot.1.push(key.clone()),
            None => event.people.push((request.person_id.clone(), vec![key.clone()])),
        }
        if let Some(current) = &request.current {
            let mut map = event.current.take().unwrap_or_default();
            let person = map.entry(request.person_id.clone()).or_insert_with(|| json!({}));
            let kept: Map<String, Value> = current
                .as_object()
                .map(|c| c.iter().filter(|(k, _)| *k == "rating" || *k == "reasoning").map(|(k, v)| (k.clone(), v.clone())).collect())
                .unwrap_or_default();
            if let Some(obj) = person.as_object_mut() {
                obj.insert(key, Value::Object(kept));
            }
            event.current = Some(map);
        }
    }
    due
}

/// `check`: everything wrong with `judgments` as answers to `requests`.
pub fn check(requests: &[JudgmentRequest], judgments: &[Judgment]) -> Vec<String> {
    let mut problems = Vec::new();
    let mut answered: Vec<String> = Vec::new();
    for judgment in judgments {
        let request = requests.iter().rev().find(|r| r.id == judgment.request_id);
        match request {
            None => problems.push(format!("{} isn't one of the compaction's judgment requests", py::repr(&judgment.request_id))),
            Some(_) if answered.contains(&judgment.request_id) => problems.push(format!("{}: judged more than once", judgment.request_id)),
            Some(request) => {
                let ratings: Vec<String> = request.ratings.as_object().map(|r| r.keys().cloned().collect()).unwrap_or_default();
                if !ratings.contains(&judgment.rating.to_string()) {
                    problems.push(format!(
                        "{}: the rating {} isn't one of its ratings ({})",
                        judgment.request_id,
                        judgment.rating,
                        ratings.join(", ")
                    ));
                } else if judgment.reasoning.trim().is_empty() {
                    problems.push(format!("{}: give a line of reasoning", judgment.request_id));
                } else if judgment.reasoning.chars().count() > MAX_REASONING_CHARS {
                    problems.push(format!("{}: keep the reasoning to one line, {MAX_REASONING_CHARS} characters", judgment.request_id));
                }
            }
        }
        answered.push(judgment.request_id.clone());
    }
    problems
}

/// `merged`: an event's judgments with those of `judgments` about it added.
pub fn merged(existing: Option<&Map<String, Value>>, requests: &[JudgmentRequest], judgments: &[Judgment], event_id: &str) -> Map<String, Value> {
    let mut result = existing.cloned().unwrap_or_default();
    for judgment in judgments {
        let request = requests.iter().rev().find(|r| r.id == judgment.request_id).unwrap();
        if request.event_id != event_id {
            continue;
        }
        let mut person = result.get(&request.person_id).and_then(Value::as_object).cloned().unwrap_or_default();
        let mut t = person.get(&request.trait_id).and_then(Value::as_object).cloned().unwrap_or_default();
        let scale = request.ratings.as_object().map(|r| r.keys().filter_map(|k| k.trim().parse::<i64>().ok()).max().unwrap_or(0)).unwrap_or(0);
        t.insert(
            request.part.clone(),
            json!({
                "rating": judgment.rating,
                "scale": scale,
                "reasoning": judgment.reasoning.split_whitespace().collect::<Vec<_>>().join(" "),
            }),
        );
        person.insert(request.trait_id.clone(), Value::Object(t));
        result.insert(request.person_id.clone(), Value::Object(person));
    }
    result
}
