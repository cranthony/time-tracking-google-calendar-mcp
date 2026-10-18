//! The recurring-series tools (get_recurrence, update_recurrence,
//! split_recurrence, delete_recurrence).

use super::events::{check_action_ids, check_facts, check_goal_ids, goal_ids_for, shown_action_shares, shown_shares, when_given, EventField, Facts};
use super::stores::prefetch_stores;
use super::{from_store, texts, ToolError};
use crate::contract::wire_text;
use crate::app::App;
use crate::event::Event;
use crate::contract::{tool, DateOrDateTime, DateTimeText, Nth, Schema};
use crate::facts::Facts as EventFacts;
use crate::py;
use crate::stores::action_calendar::fill_in_from_actions;
use crate::stores::actions::{ActionShare, ActionTree};
use crate::stores::recurrences::{describe_rules, is_refusal, PyDt, Repeat as StoredRepeat, Until};
use chrono::NaiveDate;
use crate::zoned;
use chrono_tz::Tz;
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum Weekday {
    #[serde(rename = "mon")]
    Mon,
    #[serde(rename = "tue")]
    Tue,
    #[serde(rename = "wed")]
    Wed,
    #[serde(rename = "thu")]
    Thu,
    #[serde(rename = "fri")]
    Fri,
    #[serde(rename = "sat")]
    Sat,
    #[serde(rename = "sun")]
    Sun,
}

#[derive(Clone, Copy, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub enum RepeatEvery {
    #[serde(rename = "day")]
    Day,
    #[serde(rename = "week")]
    Week,
    #[serde(rename = "month")]
    Month,
    #[serde(rename = "year")]
    Year,
}

/// One weekday of a month: the first Monday, the last Friday.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct NthWeekday {
    /// Which of the month's `weekday`s: 1 for the first, 2 for the second,
    /// ... 5 for the fifth (months without one are skipped), or -1 for the
    /// last.
    pub nth: Nth,
    /// The day of the week, e.g. "mon".
    pub weekday: Weekday,
}

/// How a recurring series repeats, starting from its first event's
/// `start`: its events are at that event's time of day (in the series'
/// time zone), on the days this says.
///
/// Examples:
/// - every weekday: {"every": "week", "weekdays": ["mon", "tue", "wed", "thu", "fri"]}
/// - every other Tuesday: {"every": "week", "interval": 2, "weekdays": ["tue"]}
/// - the 1st and 15th of each month: {"every": "month", "month_days": [1, 15]}
/// - the last Friday of each month: {"every": "month", "nth_weekdays": [{"nth": -1, "weekday": "fri"}]}
/// - the 4th Thursday of November: {"every": "year", "months": [11], "nth_weekdays": [{"nth": 4, "weekday": "thu"}]}
/// - daily, 10 times: {"every": "day", "count": 10}
/// - weekly through 2026: {"every": "week", "until": "2026-12-31"}
///
/// Leaving out weekdays, nth_weekdays, month_days and months repeats on
/// the first event's own day: weekly on its weekday, monthly on its day
/// of the month, yearly on its date. Only what's shown here can be
/// written; a Repeat that breaks a rule given below (e.g. count with
/// until) is refused with the reason, and nothing is changed.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct Repeat {
    /// How often the series repeats: its events fall in every `interval`th
    /// day, week, month or year.
    pub every: RepeatEvery,
    /// 1 for every day/week/month/year, 2 for every other one, and so on.
    /// At least 1.
    #[schema(default = "1")]
    pub interval: i64,
    /// The days of the week events fall on, e.g. ["mon", "wed"]. Mostly
    /// for `every` "week". With "day" it keeps only those days (every
    /// weekday: "week" with all five is clearer); with "month" or "year"
    /// it's every one of those weekdays in the month or year -- use
    /// `nth_weekdays` for "the first Monday".
    pub weekdays: Option<Vec<Weekday>>,
    /// Particular weekdays of the month, like the first Monday or the last
    /// Friday. Only for `every` "month", or "year" with `months` set (each
    /// then counts within those months). Can be given with `weekdays`.
    pub nth_weekdays: Option<Vec<NthWeekday>>,
    /// The days of the month events fall on: 1 to 31, or -1 for the last
    /// day (months without a given day are skipped, so use -1 for "the end
    /// of the month", not 31). Not for `every` "week".
    pub month_days: Option<Vec<i64>>,
    /// Only these months, 1 (January) to 12 (December). Mostly for `every`
    /// "year", e.g. [11] with `nth_weekdays` for the 4th Thursday of
    /// November.
    pub months: Option<Vec<i64>>,
    /// End the series after this many events, counting from its first.
    /// Can't be given with `until`; give neither for a series that never
    /// ends.
    pub count: Option<i64>,
    /// End the series here: no event starts after it. A date ("2026-12-31")
    /// means through the end of that day, in the series' time zone; a
    /// datetime is exact. Can't be given with `count`.
    pub until: Option<DateOrDateTime>,
    /// The day weeks start on, which only matters for `every` "week" with
    /// an `interval` above 1 and several `weekdays`: it decides which days
    /// count as the same week. Monday when left out.
    pub week_starts_on: Option<Weekday>,
    /// Starts of events the series leaves out, e.g.
    /// ["2026-11-09T09:00:00-05:00"]. Each must be exactly the start the
    /// rules give that event (a time without an offset is taken to be in the
    /// series' time zone). (To cancel one event, cancelling it through
    /// update_event is usually simpler.)
    pub skipped: Option<Vec<DateTimeText>>,
    /// Starts of extra events, beyond those the rules make, e.g.
    /// ["2026-12-01T14:00:00-05:00"] (a time without an offset is taken to
    /// be in the series' time zone). Each lasts as long as the series'
    /// events do.
    pub added: Option<Vec<DateTimeText>>,
}

/// A recurring series of events, as a whole. id is the series' own
/// id, which is every one of its
/// events' recurring_event_id. start/end are when its first event starts
/// and ends; time_zone keeps its events at the same wall-clock time
/// across daylight saving changes. repeat says which days its events
/// fall on and when it ends (see Repeat), and schedule says that in
/// words. A series made elsewhere may repeat in ways repeat can't say
/// (e.g. "the last weekday of the month"): its repeat is then null and
/// its schedule shows its raw RFC 5545 rules; leave repeat out when
/// updating it to keep them. The other fields are as for PublicEvent,
/// and apply to every event in the series except where one was edited
/// on its own -- until the series is next edited, which resets them
/// (see update_recurrence). actions_from_label is as for PublicEvent.
/// facts are those of every event in it: usually who it's with or for,
/// and where (location_ids) -- set them to replace them whole.
///
/// schedule, action_names, event_label_id and effective_priority are
/// read-only: update_recurrence ignores them, as it does time_zone.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct PublicRecurrence {
    pub id: Option<String>,
    pub summary: Option<String>,
    pub start: Option<DateTimeText>,
    pub end: Option<DateTimeText>,
    pub time_zone: Option<String>,
    pub repeat: Option<Repeat>,
    pub schedule: Option<String>,
    pub description: Option<String>,
    pub location: Option<String>,
    pub priority: Option<i64>,
    pub action_ids: Option<Vec<ActionShare>>,
    pub action_names: Option<Vec<String>>,
    #[schema(default = "false")]
    pub actions_from_label: bool,
    pub goals: Option<Vec<crate::stores::goals::GoalShare>>,
    pub event_label_id: Option<String>,
    pub effective_priority: Option<i64>,
    pub facts: Option<Facts>,
}

/// A series as the recurrence tools return it (PublicRecurrence).
fn public_recurrence(event: &Event, tree: &ActionTree, goal_ids: Option<&[String]>) -> Value {
    let zone: Option<Tz> = event.time_zone.as_deref().filter(|z| !z.is_empty()).and_then(|z| z.parse().ok());
    let action_ids = shown_action_shares(event);
    let names = action_ids.as_ref().map(|ids| ids.iter().map(|a| tree.name(&a.action_id)).collect::<Vec<_>>());
    let shown = |m: Option<crate::event::Moment>| m.map(|m| match &zone {
        Some(zone) => py::pydantic_datetime(&zoned::in_zone(&m, zone)),
        None => py::pydantic_datetime(&m),
    });
    let repeat = StoredRepeat::from_rules(event.recurrence.as_deref(), zone.as_ref().unwrap_or(&Tz::UTC));
    let mut out = Map::new();
    out.insert("id".into(), json!(event.id));
    out.insert("summary".into(), json!(event.summary));
    out.insert("start".into(), json!(shown(event.start)));
    out.insert("end".into(), json!(shown(event.end)));
    out.insert("time_zone".into(), json!(event.time_zone));
    out.insert("repeat".into(), repeat.map(|r| r.to_json()).unwrap_or(Value::Null));
    out.insert("schedule".into(), json!(describe_rules(event.recurrence.as_deref(), zone.as_ref())));
    out.insert("description".into(), json!(event.description));
    out.insert("location".into(), json!(event.location));
    out.insert("priority".into(), json!(event.priority));
    out.insert("action_ids".into(), json!(action_ids));
    out.insert("action_names".into(), json!(names));
    out.insert("actions_from_label".into(), json!(event.actions_from_label));
    out.insert("goals".into(), json!(shown_shares(event, goal_ids)));
    out.insert("event_label_id".into(), json!(event.event_label_id));
    out.insert("effective_priority".into(), json!(event.effective_priority()));
    out.insert("facts".into(), event.facts.as_ref().map(EventFacts::to_public).unwrap_or(Value::Null));
    Value::Object(out)
}

fn public_recurrences(app: &App, events: &[Event]) -> anyhow::Result<Value> {
    let tree = app.actions()?.tree()?;
    let goal_ids = goal_ids_for(app, events)?;
    Ok(Value::Array(fill_in_from_actions(events, &tree).iter().map(|e| public_recurrence(e, &tree, goal_ids.as_deref())).collect()))
}

fn shown_refusal(error: anyhow::Error) -> ToolError {
    if is_refusal(&error) {
        ToolError::Message(error.to_string())
    } else {
        ToolError::Unexpected(error)
    }
}

/// `PublicRecurrence.to_event(clear_fields)`.
fn to_event(series: &PublicRecurrence, clear_fields: &[EventField]) -> Result<Event, ToolError> {
    let event = Event {
        id: series.id.clone(),
        summary: series.summary.clone(),
        start: when_given(series.start.as_ref())?,
        end: when_given(series.end.as_ref())?,
        description: series.description.clone(),
        location: series.location.clone(),
        priority: series.priority,
        action_ids: if series.actions_from_label { None } else { series.action_ids.clone() },
        goals: series.goals.clone(),
        facts: series.facts.as_ref().map(|f| EventFacts::from(f).normalized()),
        cleared: texts(clear_fields).into_iter().collect(),
        ..Default::default()
    };
    event.check_cleared().map_err(|e| ToolError::Message(e.to_string()))?;
    Ok(event)
}

/// How a client says a series repeats, as the store keeps it.
pub fn stored_repeat(repeat: &Repeat) -> anyhow::Result<StoredRepeat> {
    let times = |given: &Option<Vec<DateTimeText>>| -> anyhow::Result<Option<Vec<PyDt>>> {
        given.as_ref().map(|items| items.iter().map(|t| PyDt::parse(&t.0)).collect()).transpose()
    };
    let until = match &repeat.until {
        Some(DateOrDateTime(text)) if text.chars().count() == 10 => Some(Until::Date(NaiveDate::parse_from_str(text, "%Y-%m-%d")?)),
        Some(DateOrDateTime(text)) => Some(Until::DateTime(PyDt::parse(text)?)),
        None => None,
    };
    Ok(StoredRepeat {
        every: wire_text(&repeat.every),
        interval: repeat.interval,
        weekdays: repeat.weekdays.as_deref().map(texts),
        nth_weekdays: repeat.nth_weekdays.as_ref().map(|items| items.iter().map(|n| (n.nth.0, wire_text(&n.weekday))).collect()),
        month_days: repeat.month_days.clone(),
        months: repeat.months.clone(),
        count: repeat.count,
        until,
        week_starts_on: repeat.week_starts_on.as_ref().map(wire_text),
        skipped: times(&repeat.skipped)?,
        added: times(&repeat.added)?,
    })
}

// -- the tools ------------------------------------------------------------------

/// A recurring series, by its id or the id of any of its events (an
/// event's recurring_event_id is its series' id). See PublicRecurrence
/// for its fields.
#[tool]
pub fn get_recurrence(app: &App, id: String) -> Result<PublicRecurrence, ToolError> {
    prefetch_stores(app)?;
    let series = app.recurrences()?.series(&id).map_err(shown_refusal)?;
    from_store(public_recurrences(app, &[series])?.as_array().unwrap()[0].clone())
}

/// Edit a recurring series (recurrence.id is its id, or any of its
/// events'): every field given is set, those left out are kept, and
/// those in clear_fields are removed (as for update_event). Applies
/// to all its events -- or, with starting_at_event_id, to that event and
/// the ones after it only ("this and following"): the series is split
/// there (see split_recurrence) and only the later part is edited.
/// start/end are its first event's, as get_recurrence gave them; when
/// it's split, the later part moves by as much as they changed. repeat,
/// if given, replaces how it repeats whole (including count/until and
/// skipped/added), so send every part of it you want kept. A series' edits
/// aren't checked for overlaps. Returns the edited series, then the earlier part if it
/// was split.
///
/// Events edited on their own don't keep those edits (Google Calendar
/// works that way): any edit resets every field but their times
/// to the series' -- even fields it leaves out, so an event's own
/// priority, action_ids, facts or description are lost to an actions-only edit --
/// and an edit to start/end moves them back onto the series' times too.
/// Leave start/end out to keep each event's own time. A field left out
/// is kept on the series itself, priority and action_ids included.
///
/// A series split in Google Calendar itself (an id like
/// "abc123_R20260915T223000") can't have its repeat or start changed
/// whole -- Google refuses -- only from one of its later events on.
#[tool(writes)]
pub fn update_recurrence(app: &App, recurrence: PublicRecurrence, starting_at_event_id: Option<String>, clear_fields: Option<Vec<EventField>>) -> Result<Vec<PublicRecurrence>, ToolError> {
    prefetch_stores(app)?;
    check_action_ids(app, recurrence.id.as_deref(), recurrence.action_ids.as_deref(), recurrence.actions_from_label, true)?;
    check_goal_ids(app, recurrence.goals.as_deref())?;
    check_facts(app, recurrence.facts.as_ref())?;
    let changes = to_event(&recurrence, &clear_fields.unwrap_or_default())?;
    let repeat = recurrence.repeat.as_ref().map(stored_repeat).transpose()?;
    let updated = app.recurrences()?.update(&changes, starting_at_event_id.as_deref(), repeat.as_ref()).map_err(shown_refusal)?;
    from_store(public_recurrences(app, &updated)?)
}

/// Split the recurring series event_id is one of at that event: the
/// series ends just before it, and a copy starts at it, so the two can be
/// edited apart. A COUNT is shared between them; events after the split
/// that were edited on their own lose those edits, as in Google Calendar.
/// If the series can't be ended, the copy is cancelled again and the
/// error says so. Returns the series from the event on, then the one before it (none if
/// it was the series' first event, which leaves nothing to split).
#[tool(writes)]
pub fn split_recurrence(app: &App, event_id: String) -> Result<Vec<PublicRecurrence>, ToolError> {
    prefetch_stores(app)?;
    let (earlier, later) = app.recurrences()?.split(&event_id).map_err(shown_refusal)?;
    let mut events = vec![later];
    events.extend(earlier);
    from_store(public_recurrences(app, &events)?)
}

/// Delete a recurring series (id is its id, or any of its events'):
/// every one of its events, past ones and any edited on their own
/// included. Or, with starting_at_event_id, delete that event and the
/// ones after it only ("this and following"): the series is ended just
/// before it, and the events before it are kept as they are. To delete
/// one event of a series, use delete_event with that event's id instead.
/// Returns what's left of the series: nothing if it was deleted whole
/// (or from its first event on), else the series, now ending before the
/// event.
///
/// The two leave different things behind. Deleting a series whole
/// cancels each of its events, as delete_event does one: they stay on
/// the calendar as cancelled events, hidden from the user and from
/// list_events -- past events too, whose time is then no longer counted
/// as spent. Deleting this and following leaves no
/// cancelled events: the series' events from that one on are gone (one
/// edited on its own goes by where the series first put it, even if
/// moved earlier). Neither counts against follow-through: only an event
/// the user cancelled does (see delete_event). To stop
/// a series that's already begun -- one that won't happen any more,
/// rather than one that shouldn't have been -- delete from its next
/// event on.
#[tool(writes)]
pub fn delete_recurrence(app: &App, id: String, starting_at_event_id: Option<String>) -> Result<Vec<PublicRecurrence>, ToolError> {
    prefetch_stores(app)?;
    let left = app.recurrences()?.delete(&id, starting_at_event_id.as_deref()).map_err(shown_refusal)?;
    from_store(public_recurrences(app, &left.into_iter().collect::<Vec<_>>())?)
}
