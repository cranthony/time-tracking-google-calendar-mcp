//! The people, circle, trait and habit tools.

use super::stores::{prefetch_habits, prefetch_people, prefetch_stores};
use super::{check_part_actions, refused, texts, ToolError};
use crate::app::App;
use crate::contract::{tool, Schema};
use crate::stores::habits::subject_id;
pub use crate::stores::habits::{CreatedHabit, Habit, ListedHabit};
pub use crate::stores::people::{Circle, CreatedCircle, CreatedPerson, DeletedCircle, ListedCircle, ListedPerson, Person, Status};
pub use crate::stores::person_facts::LoggedChange;
use crate::stores::person_facts::SummaryChange;
use indexmap::IndexMap;
pub use crate::stores::scoring::ScoringDefaults;
pub use crate::stores::traits::{ListedTrait, Trait, TraitStatus};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// Each habit (by its subject id) with the events the user cancelled
/// that count against its follow-through.
fn habits_with_cancellations(app: &App, mut habits: Vec<ListedHabit>) -> anyhow::Result<Vec<ListedHabit>> {
    let by_person = app.cancellations()?.by_person()?;
    for habit in habits.iter_mut() {
        let id = subject_id(habit.id.as_deref().unwrap_or("None"));
        habit.cancelled_events = Some(by_person.iter().find(|(k, _)| *k == id).map(|(_, v)| v.clone()).unwrap_or_default());
    }
    Ok(habits)
}

/// Each person with the events the user cancelled that count against
/// their follow-through, and their trait facts' summaries (of the facts
/// the traits still have).
fn people_with_cancellations(app: &App, mut people: Vec<ListedPerson>) -> anyhow::Result<Vec<ListedPerson>> {
    let by_person = app.cancellations()?.by_person()?;
    let summaries = app.person_facts()?.by_person()?;
    let traits = app.traits()?.all()?;
    for person in people.iter_mut() {
        let id = person.id.clone().unwrap_or_else(|| "None".into());
        person.cancelled_events = Some(by_person.iter().find(|(k, _)| *k == id).map(|(_, v)| v.clone()).unwrap_or_default());
        let mut facts: IndexMap<String, IndexMap<String, String>> = IndexMap::new();
        for (trait_id, fact, summary) in summaries.iter().find(|(k, _)| *k == id).map(|(_, v)| v.as_slice()).unwrap_or_default() {
            if traits.iter().any(|t| t.id.as_deref() == Some(trait_id.as_str()) && t.fact(fact).is_some()) {
                facts.entry(trait_id.clone()).or_default().insert(fact.clone(), summary.clone());
            }
        }
        person.facts = Some(facts);
    }
    Ok(people)
}

/// Set `person_id`'s summaries in `facts` ({trait id: {fact id:
/// summary}}; an empty one clears it), as the user's own.
fn set_summaries(app: &App, person_id: &str, facts: &IndexMap<String, IndexMap<String, String>>) -> anyhow::Result<()> {
    let changes: Vec<SummaryChange> = facts
        .iter()
        .flat_map(|(trait_id, by_fact)| {
            by_fact.iter().map(move |(fact, summary)| SummaryChange {
                person_id: person_id.to_string(),
                trait_id: trait_id.clone(),
                fact_id: fact.clone(),
                summary: summary.clone(),
            })
        })
        .collect();
    if changes.is_empty() {
        return Ok(());
    }
    app.person_facts()?.set_summaries(&changes, &app.traits()?.all()?, "user", crate::determinism::now())?;
    Ok(())
}

/// What's wrong with summaries to set ({trait id: {fact id: summary}}),
/// before anything's written.
fn check_summaries(app: &App, facts: Option<&IndexMap<String, IndexMap<String, String>>>) -> Result<(), ToolError> {
    let Some(facts) = facts else { return Ok(()) };
    let changes: Vec<SummaryChange> = facts
        .iter()
        .flat_map(|(trait_id, by_fact)| {
            by_fact.iter().map(move |(fact, summary)| SummaryChange {
                person_id: "the person".into(),
                trait_id: trait_id.clone(),
                fact_id: fact.clone(),
                summary: summary.clone(),
            })
        })
        .collect();
    let problems = crate::stores::person_facts::summary_problems(&changes, &app.traits()?.all()?);
    if problems.is_empty() {
        Ok(())
    } else {
        Err(refused(anyhow::anyhow!("{}", problems.join("; "))))
    }
}

/// A person's traits' parts, for checking their actions.
fn parts_of(traits: &Option<Map<String, Value>>) -> Option<&Value> {
    traits.as_ref().and_then(|t| t.get("parts"))
}


#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum TraitField {
    #[serde(rename = "definition")]
    Definition,
    #[serde(rename = "scoring")]
    Scoring,
    #[serde(rename = "facts")]
    Facts,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum PersonField {
    #[serde(rename = "context")]
    Context,
    #[serde(rename = "circles")]
    Circles,
    #[serde(rename = "traits")]
    Traits,
    #[serde(rename = "foundations")]
    Foundations,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum CircleField {
    #[serde(rename = "note")]
    Note,
    #[serde(rename = "scoring")]
    Scoring,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum ScoringLevel {
    #[serde(rename = "traits")]
    Traits,
    #[serde(rename = "circles")]
    Circles,
    #[serde(rename = "habit_layers")]
    HabitLayers,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum HabitField {
    #[serde(rename = "name")]
    Name,
    #[serde(rename = "note")]
    Note,
    #[serde(rename = "traits")]
    Traits,
    #[serde(rename = "foundations")]
    Foundations,
}

// -- the tools ------------------------------------------------------------------


/// The traits: how the user wants to be with people (and with
/// themselves), in order -- by default the active ones (rated) and those
/// turned off (kept, but not rated for now), not archived ones
/// (retired). Each has an id (fixed when it's created, and what a
/// person's traits name), name, definition, parts and scoring. Each part
/// is {"id": ..., "kind": ..., "engagement_type": "with", ...}: id is the
/// part's own, made when it's added and never changed -- its judgments and
/// weight are kept under it, so keep it on a part you send back, and leave
/// it out of a new one; engagement_type "with" (the default) reads the
/// events the person was at with the user, "for" those the user did for
/// them while they weren't there. A trait's score (0-100) combines its
/// parts' scores as its scoring says, leaving out any with nothing to rate
/// it by: without scoring, their plain mean. Scoring is an aggregation,
/// as are a person's and a circle's (see get_scoring_defaults for its
/// fields): a trait's weights are by part id, {"weights": {"<part id>":
/// 2}} counting that part twice. The kinds:
/// "judgment" -- each event judged by the assistant on its own, never by
/// asking the user, for one person or a named group, against a "rubric"
/// (the question: "Was this activity or place new?") on a scale of
/// "ratings" ({"0": "The activity and place were routine", "1": "There
/// was a twist on the activity or place", "2": "The activity or the
/// place were new", "3": "Both were new, or it was otherwise
/// adventurous"}), from the "facts" it names: "action" (what was done),
/// "action_history" (what's been done with them before), "location",
/// "location_history" (where they've been together before),
/// "general_notes" (the event's notes) and any of the trait's own facts
/// as "fact:<fact id>" (its summary of the person, as it is now), each a
/// name or {"fact": "action_history", "lookback_days": 90} (history facts
/// look back 30 days by default),
/// and scored over the person's events in its last "window_days" (30
/// by default) as its "scoring" says: {"weight_by": "event" (each
/// judgment counts once, the default) or "duration" (each by its event's
/// length), "statistic": "mean" (the default) or "percentile" (with
/// "percentile": 0-100 -- the lowest rating with at least that share of
/// the judgments, or of their time, at or below it; 0 the lowest, 100 the
/// highest), "limit" and "limit_by": only the judgments of the "best"
/// (highest rated), "worst" or most "recent" limit events, or -- weighted
/// by duration -- limit hours, the last event cut short to fit};
/// "continuity" (the last event ended within "last_within_days" of the
/// day's end and the next starts within "next_within_days" after it,
/// both default 14: 100 for both, 50 for one, 0 for neither); "count"
/// ({"kind": "count", "target": 1, "interval_days": 21, "zero_at_days":
/// 42, "noun": "visits"}), "duration" (with "target_min") and
/// "follow_through" ("penalty", "recovery", "look_back_days"), as the
/// measures of the same kind -- follow_through counting the events the
/// user cancelled: said in compaction didn't happen, or deleted with
/// delete_event's counts_against_follow_through, never a plan merely
/// changed; "spacing" ({"kind": "spacing", "action": "<meals>",
/// "max_gap_hours": 6, "exceptions": [{"actions": ["<sleep>"],
/// "reset_hours": 1}]}): a clock that each event of its action resets to
/// max_gap_hours left, stopped during an exception's events and, after
/// one, set to its reset_hours left (if given) -- each day scored as
/// the share of its time outside exceptions that the clock hadn't run
/// out. continuity, count, duration and follow_through can take an
/// "action" (an action or action group id) to count only its events. A
/// trait's facts are what it keeps about each person it applies to, for
/// its judgments: each {"id", "name", "summary_prompt", "log_prompt"?} --
/// id made from the name when it's added, and never changed; the
/// summary_prompt says what its summary of a person keeps, and how
/// (compaction rewrites a person's summary whole when an event's notes
/// show something new, logging what changed on the event, as the
/// log_prompt says). A person's summaries are in get_people's facts, and
/// the logs in get_fact_logs. A person's traits can select which traits
/// apply to them, give them their own parts for any, and say how they
/// combine (see create_person). problems lists anything wrong with a
/// trait edited by hand; a bad part isn't rated. Read-only.
#[tool]
pub fn get_traits(app: &App, statuses: Option<Vec<TraitStatus>>) -> Result<Vec<ListedTrait>, ToolError> {
    let statuses = statuses.map(|s| texts(&s));
    app.traits()?.get_traits(statuses.as_deref()).map_err(refused)
}

/// Add a trait: a name (unique, at most 50 characters), a definition
/// (what it means, in a sentence or two), parts (at least one; see
/// get_traits for each kind and its fields) and optionally facts (see
/// get_traits; leave out each new fact's id, and one is made from its
/// name). Its status is active unless
/// given ("off" to keep it unrated for now). Its id is made from its name
/// and never changes, so renaming it later doesn't touch the measures
/// naming it. A trait with a part that isn't well formed is refused,
/// saying what's wrong. It applies to every person whose traits don't
/// select others. Returns the trait as created.
#[tool(writes)]
pub fn create_trait(app: &App, r#trait: Trait) -> Result<Trait, ToolError> {
    check_part_actions(app, Some(&Value::Array(r#trait.part_values())))?;
    app.traits()?.create_trait(&r#trait).map_err(refused)
}

/// Change a trait, by id: rename it, reword its definition, change its
/// status or scoring, or give it new parts. Fields left out keep their
/// value; parts, if given, replace them whole, so send every part to keep,
/// each with its id (a part sent without one is new, and starts with no
/// judgments); a part left out takes its weight with it. facts, if given,
/// replace them whole too, each with its id (one sent without is new);
/// a fact left out keeps people's summaries of it, unused, and can't be
/// left out while a part names it. status is
/// active (rated), off (kept, but not rated for now) or archived (retired:
/// not rated, and get_traits leaves it out unless asked) -- archive a
/// trait rather than delete it, so its history stays readable. Checked as
/// for create_trait. Ratings already recorded aren't changed. Returns the
/// trait as updated.
#[tool(writes)]
pub fn update_trait(app: &App, r#trait: Trait, clear_fields: Option<Vec<TraitField>>) -> Result<Trait, ToolError> {
    check_part_actions(app, Some(&Value::Array(r#trait.part_values())))?;
    app.traits()?.update_trait(&r#trait, &texts(&clear_fields.unwrap_or_default())).map_err(refused)
}

/// The people the user spends time with: by default the active ones,
/// not archived (out of touch) or deleted ones. The user themself is
/// always first, with the id "self". Each has an id, name, context (what
/// tells them apart from others of the same name, e.g. "met at salsa"),
/// status, circles (the ids of the circles they're in) with their
/// circle_names, traits (which traits apply to them, and their own parts
/// for any; see create_person; without it, every active trait applies as
/// get_traits has it) and facts: their summary of each trait fact (see
/// get_traits), as {trait id: {fact id: summary}}, those they have
/// one of. Each also has cancelled_events: the events the user cancelled
/// that count against their follow-through, newest first -- when each was
/// planned, its actions, whether they were to be there ("with") or it was
/// for them ("for"), the follow-through parts it counted against, and
/// what cancelled it. Each also has foundations: the habits (each as
/// "habit:<id>") and people (by id) their relationship is built on, on
/// the user's habit graph -- a person with foundations, even none ([]),
/// is on it; without, they aren't. Read-only.
#[tool]
pub fn get_people(app: &App, statuses: Option<Vec<Status>>) -> Result<Vec<ListedPerson>, ToolError> {
    prefetch_people(app)?;
    let statuses = statuses.map(|s| texts(&s));
    let people = app.people()?.get_people(statuses.as_deref()).map_err(refused)?;
    Ok(people_with_cancellations(app, people)?)
}

/// One person, by id ("self" for the user) or else by name (ignoring
/// case), as get_people lists them. If several share the name, the error
/// lists them with their contexts; if there's none, it suggests close
/// matches. Read-only.
#[tool]
pub fn get_person(app: &App, id_or_name: String) -> Result<ListedPerson, ToolError> {
    prefetch_people(app)?;
    let person = app.people()?.get_person(&id_or_name).map_err(refused)?;
    Ok(people_with_cancellations(app, vec![person])?.remove(0))
}

/// Add a person: a name, and optionally a context (what tells them
/// apart, e.g. "met at salsa" -- a name and context together must be
/// unique, so give one when the name's taken), circles (circle ids, see
/// get_circles) and traits:
/// {"select": "all" or [trait ids], "parts": {trait id: [parts]},
/// "part_scoring": {trait id: scoring}, "scoring": scoring}, all
/// optional -- the traits that apply to them (by default every active
/// one); parts replacing a trait's for them alone, each checked as a
/// trait's own (see get_traits): a person's own cadence, say,
/// {"parts": {"reliable": [{"kind": "count", "target": 1,
/// "interval_days": 21, "noun": "visits"}]}}; how a trait's parts combine
/// for them, in place of the trait's scoring (weights by part id: their
/// own parts' ids, if they have them); and how their traits combine into
/// their score, in place of the scoring defaults' traits (weights by
/// trait id) -- each an aggregation (see get_scoring_defaults); and their
/// own prompts for a trait's facts (see get_traits), {"facts": {trait
/// id: {fact id: {"summary_prompt": {"text": ..., "how": "replace" or
/// "append"}, "log_prompt": {...}}}}}, each optional -- "replace" (the
/// default) keeps their summary by their text in place of the trait's,
/// "append" by the trait's with theirs after it -- kept for a trait that
/// doesn't apply to them too, and let go of once the trait no longer has
/// the fact (when they're next saved); and
/// foundations (see
/// get_people: habits as "habit:<id>", people by id; none of them
/// deleted, and never making a cycle). Active unless given a status. id
/// is assigned. facts sets their trait facts' summaries (see get_people).
/// Returns the person and their id as created_id.
#[tool(writes)]
pub fn create_person(app: &App, person: Person, facts: Option<IndexMap<String, IndexMap<String, String>>>) -> Result<CreatedPerson, ToolError> {
    prefetch_stores(app)?;
    check_part_actions(app, parts_of(&person.traits))?;
    check_summaries(app, facts.as_ref())?;
    let mut created = app.people()?.create_person(&person).map_err(refused)?;
    if let Some(facts) = &facts {
        set_summaries(app, &created.created_id, facts)?;
        created.person = people_with_cancellations(app, vec![created.person])?.remove(0);
    }
    Ok(created)
}

/// Update a person by id ("self" for the user): their name, context,
/// status (active; archived, out of touch; or deleted, shouldn't have
/// existed -- self is always active), circles (replaced whole, so send
/// every circle to keep), traits (replaced whole; see create_person) or
/// foundations (replaced whole; clear it to take them off the habit
/// graph). Omitted properties keep their value; list one in clear_fields
/// to blank it instead. facts sets their summaries of the trait facts it
/// names ({trait id: {fact id: summary}}, each the whole summary; "" to
/// clear one), as the user's own -- with no log -- and keeps the rest. A
/// person deleted leaves every habit's and person's foundations. Returns
/// the person as updated.
#[tool(writes)]
pub fn update_person(
    app: &App,
    person: Person,
    clear_fields: Option<Vec<PersonField>>,
    facts: Option<IndexMap<String, IndexMap<String, String>>>,
) -> Result<ListedPerson, ToolError> {
    prefetch_stores(app)?;
    check_part_actions(app, parts_of(&person.traits))?;
    check_summaries(app, facts.as_ref())?;
    let clear = texts(&clear_fields.unwrap_or_default());
    let updated = app.people()?.update_person(&person, &clear).map_err(refused)?;
    if updated.status == Some(Status::Deleted) {
        let id = updated.id.as_deref().unwrap_or("None");
        let habits = app.habits()?;
        habits.drop_foundation(id)?;
        drop_habits_about(app, &habits, id)?;
    }
    if let Some(facts) = &facts {
        set_summaries(app, updated.id.as_deref().unwrap_or("None"), facts)?;
        return Ok(people_with_cancellations(app, vec![updated])?.remove(0));
    }
    Ok(updated)
}

/// The logs of what compaction changed in a person's trait facts (by id,
/// "self" for the user) -- of one trait (trait_id) and fact (fact_id), if
/// given -- newest first: each the event that showed it (its id, summary
/// and start), the trait and fact, the log, and when it was logged.
/// Read-only.
#[tool]
pub fn get_fact_logs(app: &App, person_id: String, trait_id: Option<String>, fact_id: Option<String>) -> Result<Vec<LoggedChange>, ToolError> {
    let store = app.person_facts()?;
    store.prefetch(&store.whole_tabs())?;
    store.logs(&person_id, trait_id.as_deref(), fact_id.as_deref()).map_err(refused)
}

/// Every circle: a group people belong to ("Close friends",
/// "Family"), a person belonging to any number of them. Each has an id,
/// name, note, scoring (how its people's scores combine into its own;
/// see create_circle) and member_ids (the people in it). Read-only.
#[tool]
pub fn get_circles(app: &App) -> Result<Vec<ListedCircle>, ToolError> {
    prefetch_stores(app)?;
    app.people()?.get_circles().map_err(refused)
}

/// One circle, by id or else by name (ignoring case), as get_circles
/// lists it. Read-only.
#[tool]
pub fn get_circle(app: &App, id_or_name: String) -> Result<ListedCircle, ToolError> {
    prefetch_stores(app)?;
    app.people()?.get_circle(&id_or_name).map_err(refused)
}

/// Add a circle: a name (unique among circles, though a person may
/// share it) and optionally a note and scoring: how its people's scores
/// combine into its own, in place of the scoring defaults' circles -- an
/// aggregation (see get_scoring_defaults), its weights by person id. id
/// is assigned. Put people in it with update_person's circles. Returns
/// the circle and its id as created_id.
#[tool(writes)]
pub fn create_circle(app: &App, circle: Circle) -> Result<CreatedCircle, ToolError> {
    prefetch_stores(app)?;
    app.people()?.create_circle(&circle).map_err(refused)
}

/// Rename a circle or change its note or scoring, by id. Fields left
/// out keep their value; list one in clear_fields to blank it instead.
/// Returns it as updated.
#[tool(writes)]
pub fn update_circle(app: &App, circle: Circle, clear_fields: Option<Vec<CircleField>>) -> Result<ListedCircle, ToolError> {
    prefetch_stores(app)?;
    let clear = texts(&clear_fields.unwrap_or_default());
    app.people()?.update_circle(&circle, &clear).map_err(refused)
}

/// Delete a circle, by id. Its people aren't deleted: they just leave
/// it. Give if_version -- the version you last read: without it, it's refused; changed since, it's refused, with it as it is now -- on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK; gone already, it's done. Returns the circle as it was, and the ids of the people
/// who left.
#[tool(writes)]
pub fn delete_circle(app: &App, circle_id: String, if_version: Option<i64>) -> Result<DeletedCircle, ToolError> {
    prefetch_stores(app)?;
    let deleted = app.people()?.delete_circle(&circle_id, if_version).map_err(refused)?;
    drop_habits_about(app, &app.habits()?, &circle_id)?;
    Ok(deleted)
}

/// Deletes the habits about `target`, a person or circle deleted, taking
/// them out of every person's foundations too.
fn drop_habits_about(app: &App, habits: &crate::stores::habits::Habits, target: &str) -> anyhow::Result<()> {
    for subject in habits.delete_about(target)? {
        app.people()?.drop_foundation(&subject)?;
    }
    Ok(())
}

/// The user's habits: by default the active ones, not archived (set
/// aside) or deleted ones. Each is about the user's events with one
/// action, or with any action under one action group (action_id, with
/// its action_path) -- or is a person's or circle's habit (person_id or
/// circle_id): some of their traits, as they're scored for them, on the
/// habit graph (see create_habit). An action's habit is rated by traits as a person is: its traits
/// say which apply (by default every active one) and give it its own
/// parts -- its own rubrics -- for any (see create_habit). Each has an
/// id, name, status and note (what it's for, and what doing it well
/// looks like). Each also has cancelled_events: the events the user
/// cancelled that count against its follow-through, newest first, as a
/// person's (see get_people). Its events' judgments are kept under
/// "habit:<id>", as a person's are under their id. Each also has
/// foundations: the habits (each as "habit:<id>") and people (by id)
/// it's built on, on the user's habit graph. Read-only.
#[tool]
pub fn get_habits(app: &App, statuses: Option<Vec<Status>>) -> Result<Vec<ListedHabit>, ToolError> {
    prefetch_habits(app, true)?;
    let statuses = statuses.map(|s| texts(&s));
    let habits = app.habits()?.get_habits(statuses.as_deref()).map_err(refused)?;
    Ok(habits_with_cancellations(app, habits)?)
}

/// One habit, by id or else by name (ignoring case), as get_habits
/// lists it, whatever its status. If there's none, the error suggests
/// close matches. Read-only.
#[tool]
pub fn get_habit(app: &App, id_or_name: String) -> Result<ListedHabit, ToolError> {
    prefetch_habits(app, true)?;
    let habit = app.habits()?.get_habit(&id_or_name).map_err(refused)?;
    Ok(habits_with_cancellations(app, vec![habit])?.remove(0))
}

/// Add a habit: a name (unique among habits), the action or action
/// group whose events it's about (action_id: an action's or a group's
/// id; only events with that action, or any action under that group,
/// count toward it), and optionally a note (what it's for, and what
/// doing it well looks like -- context for judging its events) and
/// traits: {"select": "all" or [trait ids], "parts": {trait id:
/// [parts]}, "part_scoring": {trait id: scoring}, "scoring": scoring},
/// all optional, as a person's (see create_person) -- the traits that
/// apply to it (by default every active one), parts replacing a trait's
/// for it alone (a more specific rubric, say), and how they combine. It's
/// always "with": judgment parts for those an event was done "for"
/// don't apply to it. Also optionally foundations: the habits (each as
/// "habit:<id>") and people (by id) it's built on -- sleeping well under
/// exercising, say; none of them deleted, and never making a cycle.
/// A person's or circle's habit has a person_id or circle_id in place of
/// action_id: it's some of their traits, scored as they are for them (a
/// circle's, its people's combined as the circle's scoring says) -- a
/// layer of the relationship on the habit graph, being reliable for
/// Sam under being close to them, say. Its traits take only "select"
/// (the traits it's on; by default all that apply to them) and
/// "except" (traits left out, though selected: those added later are
/// on it), and its events aren't judged for it: they're theirs. Its
/// name is optional: without one it's named by them and its traits
/// ("Sam · Reliable", "Sam · except Reliable"), and it loses its own
/// when its traits or whose it is change, unless given another (given
/// its default, it has none of its own: default_name lists it).
/// Active unless given a status. id is assigned. Returns the habit and
/// its id as created_id.
#[tool(writes)]
pub fn create_habit(app: &App, habit: Habit) -> Result<CreatedHabit, ToolError> {
    prefetch_habits(app, false)?;
    check_part_actions(app, parts_of(&habit.traits))?;
    app.habits()?.create_habit(&habit).map_err(refused)
}

/// Update a habit by id: its name, action_id (an action's or a
/// group's), person_id or circle_id, status (active; archived, set aside; or deleted, shouldn't
/// have existed), note, traits or foundations (each replaced whole; see
/// create_habit). Omitted properties keep their value; list one in
/// clear_fields to blank it instead (a person's or circle's habit's name
/// to go back to its default). A habit deleted leaves every habit's and
/// person's foundations; a person's or circle's habits go with them.
/// Returns the habit as updated.
#[tool(writes)]
pub fn update_habit(app: &App, habit: Habit, clear_fields: Option<Vec<HabitField>>) -> Result<ListedHabit, ToolError> {
    prefetch_habits(app, false)?;
    check_part_actions(app, parts_of(&habit.traits))?;
    let clear = texts(&clear_fields.unwrap_or_default());
    let updated = app.habits()?.update_habit(&habit, &clear).map_err(refused)?;
    if updated.status == Some(Status::Deleted) {
        app.people()?.drop_foundation(&subject_id(updated.id.as_deref().unwrap_or("None")))?;
    }
    Ok(updated)
}

/// How scores combine wherever nothing says otherwise -- the defaults the
/// user's app scores by: traits (how a person's or habit's traits combine
/// into their score, unless their traits' own scoring says), circles (how
/// a circle's people combine into its score, unless its own scoring says)
/// and habit_layers (how the habits and people in each layer of the habit
/// graph combine into the layer's score). Each is an aggregation, or null
/// for the plain mean -- for habit_layers, the lowest score. An aggregation -- this, a trait's scoring, a
/// person's or habit's traits' scoring and part_scoring, and a circle's
/// scoring -- is an object with any of: "statistic": "mean" (weighted;
/// the default) or "percentile" (with "percentile": 0-100 -- the lowest
/// score with at least that share of the weight at or below it, so 0 is
/// the lowest score and 100 the highest); "weights": {id: weight, 0 or
/// more} (each 1 unless given; ids as the level says -- part ids, trait
/// ids, person ids, or "habit:<id>" for habits); "limit" (a whole number)
/// and "limit_by": "best" or "worst" -- only the limit highest or lowest
/// scores count; "cap_below": {"threshold": score, "cap": score} -- with
/// any score under threshold, the result is at most cap; and "missing":
/// "skip" (the default: what has nothing to rate it by is left out) or a
/// score to count it as. Scores are 0-100. Read-only.
#[tool]
pub fn get_scoring_defaults(app: &App) -> Result<ScoringDefaults, ToolError> {
    app.scoring_defaults()?.get().map_err(refused)
}

/// Set the scoring defaults (see get_scoring_defaults): traits, circles
/// and habit_layers, each an aggregation. Levels left out keep their
/// value; list one in clear_fields to go back to the plain mean. A level
/// that isn't well formed is refused, saying what's wrong. Returns them
/// as saved.
#[tool(writes)]
pub fn set_scoring_defaults(app: &App, defaults: ScoringDefaults, clear_fields: Option<Vec<ScoringLevel>>) -> Result<ScoringDefaults, ToolError> {
    app.scoring_defaults()?.set(&defaults, &texts(&clear_fields.unwrap_or_default())).map_err(refused)
}
