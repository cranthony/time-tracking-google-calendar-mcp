//! Changing events several at once: a batch of updates, creates and
//! cancels, checked whole -- nothing moved to make room, nothing before
//! the last compaction changed unless asked
//! -- then written.

use crate::event::{Etag, Event, Moment};
use crate::google::status_of;
use crate::py;
use crate::sheet::rows;
use crate::stores::action_calendar::ActionCalendar;
use crate::stores::cancellations::Cancellations;
use crate::stores::shares::share_problems;
use crate::timeline::{build_timeline, Timeline, TimelineEvent};
use chrono::{Duration, FixedOffset, Offset};

pub const RESEND_RULE: &str = "Nothing was changed. Resend the whole batch so that, together, it is valid: every event it creates or updates must end after it starts, and must not overlap any other event -- neither one already on the calendar nor another in the batch. Fix each problem below by moving, shortening or cancelling events in the same call (each cancel saying whether it counts against follow-through), and check the result against every event listed here, not just the ones named.";

const APPROVAL: &str = "unless the user has explicitly approved changing history (allow_compacted_changes)";

/// One thing wrong, and the kind of mistake it is.
#[derive(Debug, Clone, PartialEq)]
pub struct Problem {
    pub category: String,
    pub message: String,
}

impl Problem {
    pub fn new(category: &str, message: impl Into<String>) -> Problem {
        Problem { category: category.into(), message: message.into() }
    }
}

/// Changes that can't be made as asked: the message says what to fix.
#[derive(Debug, Clone)]
pub struct ChangeError {
    pub message: String,
    pub categories: Vec<String>,
}

impl std::fmt::Display for ChangeError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for ChangeError {}

impl ChangeError {
    pub fn new(message: impl Into<String>, category: &str) -> ChangeError {
        ChangeError { message: message.into(), categories: vec![category.into()] }
    }

    pub fn of(problems: &[Problem], text: Option<String>) -> ChangeError {
        let message = text.unwrap_or_else(|| problems.iter().map(|p| p.message.as_str()).collect::<Vec<_>>().join("\n"));
        let mut categories: Vec<String> = problems.iter().map(|p| p.category.clone()).collect();
        categories.sort();
        categories.dedup();
        ChangeError { message, categories }
    }
}

pub struct Cancel {
    pub event_id: String,
    pub counts_against_follow_through: bool,
    /// The etag the client last saw: the cancel is refused if it's
    /// changed since.
    pub etag: Option<String>,
}

/// A change refused because the event isn't as the client last saw it
/// (its etag): changed since, or gone. `event` is it as it is now (None
/// if it's been deleted or cancelled).
#[derive(Debug, Clone)]
pub struct StaleEvent {
    pub event_id: String,
    pub expected: String,
    pub event: Option<Event>,
}

impl std::fmt::Display for StaleEvent {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self.event {
            None => write!(f, "event {} was deleted after it was read", py::repr(&self.event_id)),
            Some(_) => write!(f, "event {} changed after it was read", py::repr(&self.event_id)),
        }
    }
}

impl std::error::Error for StaleEvent {}

/// A batch refused because some of the events it changes aren't as the
/// client last saw them -- every one, so they're set right in one go --
/// and, if it stopped partway (Calendar found one changed after the batch
/// was checked), what it had written by then.
#[derive(Debug, Clone)]
pub struct StaleEvents {
    pub items: Vec<StaleEvent>,
    pub partly: Option<rows::Partly>,
}

impl std::fmt::Display for StaleEvents {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let said: Vec<String> = self.items.iter().map(StaleEvent::to_string).collect();
        f.write_str(&said.join("; "))
    }
}

impl std::error::Error for StaleEvents {}

#[derive(Debug, Clone)]
pub struct Placed {
    pub event: Event,
    pub key: String,
    pub moved: bool,
    pub asked: bool,
}

pub type Describe<'a> = &'a dyn Fn(&Placed) -> String;

/// Every overlapping pair where one is moved, and every moved event that
/// takes no time.
pub fn overlap_problems(placed: &[Placed], describe: Option<Describe>) -> Vec<Problem> {
    let default = |p: &Placed| describe_placed(p);
    let describe: Describe = describe.unwrap_or(&default);
    let mut problems = Vec::new();
    for p in placed {
        if p.moved && p.event.end() <= p.event.start() {
            problems.push(Problem::new(
                "nonpositive_length",
                format!("{} would end no later than it starts: give it some time, or cancel it.", describe(p)),
            ));
        }
    }
    let mut ordered: Vec<&Placed> = placed.iter().collect();
    ordered.sort_by_key(|p| (p.event.start(), p.event.end()));
    for (i, earlier) in ordered.iter().enumerate() {
        for later in &ordered[i + 1..] {
            if later.event.start() >= earlier.event.end() {
                break;
            }
            if (earlier.moved || later.moved)
                && earlier.event.end() > earlier.event.start()
                && later.event.end() > later.event.start()
            {
                problems.push(Problem::new("overlap", format!("{} overlaps {}.", describe(earlier), describe(later))));
            }
        }
    }
    problems
}

fn hm(moment: &Moment, tz: &FixedOffset) -> String {
    moment.with_timezone(tz).format("%H:%M").to_string()
}

/// The refusal of a batch: the rule, every problem, and the stretch of
/// calendar as the batch would leave it.
pub fn rejection(problems: &[Problem], listing: &[Placed], since: &Moment, until: &Moment, history_until: Option<&Moment>) -> ChangeError {
    let tz = since.offset().fix();
    let mut lines: Vec<String> = vec![RESEND_RULE.into(), String::new(), "Problems:".into()];
    lines.extend(problems.iter().map(|p| format!("- {}", p.message)));
    lines.push(String::new());
    lines.push(format!(
        "The calendar from {} to {}, as this batch would leave it (* = in this batch):",
        since.with_timezone(&tz).format("%a %d %b %H:%M"),
        until.with_timezone(&tz).format("%a %d %b %H:%M")
    ));
    let mut sorted: Vec<&Placed> = listing.iter().collect();
    sorted.sort_by_key(|p| (p.event.start(), p.event.end()));
    for p in sorted {
        let mut notes: Vec<String> = Vec::new();
        if let Some(history) = history_until {
            if p.event.start() < *history {
                notes.push(format!("history until {}", hm(&p.event.end().min(*history), &tz)));
            }
        }
        let summary = p.event.summary.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| "(no title)".into());
        let id = p.event.id.clone().filter(|i| !i.is_empty()).unwrap_or_else(|| p.key.clone());
        lines.push(format!(
            "  {}–{}  {summary}{}  {id}{}",
            hm(&p.event.start(), &tz),
            hm(&p.event.end(), &tz),
            if p.asked { " *" } else { "" },
            if notes.is_empty() { String::new() } else { format!("  ({})", notes.join(", ")) }
        ));
    }
    let mut error = ChangeError::of(problems, Some(lines.join("\n")));
    if problems.is_empty() {
        error.categories = vec!["overlap".into()];
    }
    error
}

fn describe_placed(p: &Placed) -> String {
    let tz = p.event.start().offset().fix();
    let span = format!("{}–{}", hm(&p.event.start(), &tz), hm(&p.event.end(), &tz));
    let name = match p.event.summary.as_deref().filter(|s| !s.is_empty()) {
        Some(summary) => py::repr(summary),
        None => p.event.id.clone().filter(|i| !i.is_empty()).unwrap_or_else(|| p.key.clone()),
    };
    format!("{name} ({span}{})", if p.moved { ", as asked" } else { "" })
}

#[derive(Debug, Clone)]
pub struct Change {
    pub action: String,
    pub before: Option<Event>,
    pub after: Option<Event>,
    pub patch: Option<Event>,
    pub counts_against_follow_through: bool,
    pub moved: bool,
}

#[derive(Debug, Clone, Default)]
pub struct Batch {
    pub changes: Vec<Change>,
    /// Events the batch's changes were made to already -- each this
    /// change, sent before -- as they are.
    pub again: Vec<Event>,
    pub listing: Vec<Placed>,
    pub since: Option<Moment>,
    pub until: Option<Moment>,
}

pub struct EventChanges {
    client: ActionCalendar,
    cancellations: Option<Cancellations>,
}

/// `summary!r`, as Python prints an optional string.
fn summary_repr(event: &Event) -> String {
    event.summary.as_deref().map(py::repr).unwrap_or_else(|| "None".into())
}

impl EventChanges {
    pub fn new(client: ActionCalendar, cancellations: Option<Cancellations>) -> EventChanges {
        EventChanges { client, cancellations }
    }

    #[allow(clippy::too_many_arguments)]
    pub fn check(
        &self,
        updates: Vec<Event>,
        creates: Vec<Event>,
        cancels: Vec<Cancel>,
        history_until: Option<Moment>,
        allow_compacted: bool,
    ) -> anyhow::Result<Result<Batch, ChangeError>> {
        let guarded = if allow_compacted { None } else { history_until };
        let mut problems: Vec<Problem> = Vec::new();
        let mut named: Vec<(String, String)> = Vec::new();
        let mut current: Vec<(String, Option<Event>)> = Vec::new();

        let claim = |problems: &mut Vec<Problem>, named: &mut Vec<(String, String)>, event_id: Option<&str>, how: &str| -> bool {
            let Some(event_id) = event_id.filter(|i| !i.is_empty()) else {
                problems.push(Problem::new("malformed_change", format!("{how} needs an event id")));
                return false;
            };
            if let Some((_, first)) = named.iter().find(|(id, _)| id == event_id) {
                problems.push(Problem::new(
                    "duplicate_change",
                    format!("event {event_id} is in the batch twice ({first} and {how}): give it one change"),
                ));
                return false;
            }
            named.push((event_id.to_string(), how.to_string()));
            true
        };

        let load = |problems: &mut Vec<Problem>, current: &mut Vec<(String, Option<Event>)>, event_id: &str| -> Option<Event> {
            if !current.iter().any(|(id, _)| id == event_id) {
                match self.client.get_event(event_id) {
                    Ok(event) => current.push((event_id.to_string(), Some(event))),
                    Err(_) => {
                        // Any failure to find it is reported as unknown -- and
                        // isn't remembered, so a second look tries again.
                        problems.push(Problem::new("unknown_event", format!("there's no event {}", py::repr(event_id))));
                        return None;
                    }
                }
            }
            let event = current.iter().find(|(id, _)| id == event_id).and_then(|(_, e)| e.clone())?;
            if event.is_cancelled() {
                problems.push(Problem::new(
                    "unknown_event",
                    format!("event {event_id} ({}) is cancelled already", summary_repr(&event)),
                ));
                return None;
            }
            Some(event)
        };

        // A change given the etag last seen (its version, as Calendar
        // gives it) is made once, by the rules every kind the app writes
        // keeps (see rows::Versioned): at that etag it's made; changed
        // since, but already as it'd leave it -- `done`, this change sent
        // before -- it's the same change again, and the event's given as
        // it is; else it's refused, whole batch and all, with the event as
        // it is now.
        let mut again: Vec<Event> = Vec::new();
        let fresh = |event_id: &str, expected: Option<&str>, done: &dyn Fn(&Event) -> bool| -> anyhow::Result<Option<Event>> {
            let Some(expected) = expected else { return Ok(None) };
            let at_expected = |now: &Event| !now.is_cancelled() && now.etag.0.as_deref() == Some(expected);
            if self.client.get_event(event_id).ok().is_some_and(|now| at_expected(&now)) {
                return Ok(None);
            }
            // What's kept may lag Calendar (its feed can be behind a write
            // already made): ask Calendar itself before refusing.
            match self.client.client().fresh_event(event_id).ok() {
                Some(now) if at_expected(&now) => Ok(None),
                Some(now) if done(&now) => Ok(Some(now)),
                now => Err(StaleEvent { event_id: event_id.into(), expected: expected.into(), event: now.filter(|e| !e.is_cancelled()) }.into()),
            }
        };

        // Each stale event, and each changed without saying which version
        // was read -- all of them, not just the first.
        let mut stale: Vec<StaleEvent> = Vec::new();
        let mut unversioned: Vec<String> = Vec::new();
        let found = |result: anyhow::Result<Option<Event>>, again: &mut Vec<Event>, stale: &mut Vec<StaleEvent>| -> anyhow::Result<bool> {
            match result {
                Ok(Some(now)) => {
                    again.push(now);
                    Ok(true)
                }
                Ok(None) => Ok(false),
                Err(error) => match error.downcast::<StaleEvent>() {
                    Ok(one) => {
                        stale.push(one);
                        Ok(true)
                    }
                    Err(error) => Err(error),
                },
            }
        };

        let mut changes: Vec<Change> = Vec::new();
        for mut patch in updates {
            if !claim(&mut problems, &mut named, patch.id.as_deref(), "an update") {
                continue;
            }
            if rows::versions_required() && patch.etag.0.is_none() {
                unversioned.push(patch.id.clone().unwrap_or_default());
                continue;
            }
            let as_patched = |now: &Event| !now.is_cancelled() && patched(now, &patch) == *now;
            if found(fresh(patch.id.as_deref().unwrap(), patch.etag.0.as_deref(), &as_patched), &mut again, &mut stale)? {
                continue;
            }
            let Some(before) = load(&mut problems, &mut current, patch.id.as_deref().unwrap()) else { continue };
            let event_id = patch.id.clone().unwrap_or_default();
            if before.recurrence.as_ref().is_some_and(|r| !r.is_empty()) {
                problems.push(Problem::new(
                    "recurring_series",
                    format!(
                        "event {event_id} ({}) is a recurring series, not one of its events: edit it with update_recurrence, or one of its events by that event's own id",
                        summary_repr(&before)
                    ),
                ));
                continue;
            }
            if patch.recurrence.is_some() {
                if let Some(series) = before.recurring_event_id.as_deref().filter(|s| !s.is_empty()) {
                    problems.push(Problem::new(
                        "recurring_series",
                        format!(
                            "event {event_id} ({}) is one of series {series}'s events already: change how it repeats with update_recurrence",
                            summary_repr(&before)
                        ),
                    ));
                    continue;
                }
                // Google needs a series' start and end in its time zone.
                patch.start = patch.start.or(before.start);
                patch.end = patch.end.or(before.end);
            }
            let after = patched(&before, &patch);
            if patch.goals.is_some() || patch.action_ids.is_some() || patch.facts.is_some() {
                if let Some(problem) = share_problem(&after, patch.goals.is_some(), patch.action_ids.is_some(), patch.facts.is_some(), &format!("event {event_id} ({})", summary_repr(&before))) {
                    problems.push(problem);
                    continue;
                }
            }
            if let Some(guard) = guarded {
                if before.start() < guard && !only_runs_on(&before, &after, &guard) {
                    problems.push(history_problem(&before, "changed", &guard));
                    continue;
                }
                if before.start() >= guard && after.start() < guard {
                    problems.push(into_history(&after, "moved", &guard));
                    continue;
                }
            }
            let moved = (after.start, after.end) != (before.start, before.end);
            changes.push(Change { action: "update".into(), before: Some(before), after: Some(after), patch: Some(patch), counts_against_follow_through: false, moved });
        }
        for cancel in &cancels {
            if !claim(&mut problems, &mut named, Some(&cancel.event_id), "a cancel") {
                continue;
            }
            if rows::versions_required() && cancel.etag.is_none() {
                unversioned.push(cancel.event_id.clone());
                continue;
            }
            if found(fresh(&cancel.event_id, cancel.etag.as_deref(), &Event::is_cancelled), &mut again, &mut stale)? {
                continue;
            }
            let Some(before) = load(&mut problems, &mut current, &cancel.event_id) else { continue };
            if let Some(guard) = guarded {
                if before.start() < guard {
                    problems.push(history_problem(&before, "cancelled", &guard));
                    continue;
                }
            }
            let patch = Event { id: before.id.clone(), status: Some("cancelled".into()), etag: Etag(cancel.etag.clone()), ..Default::default() };
            changes.push(Change {
                action: "cancel".into(),
                before: Some(before),
                after: None,
                patch: Some(patch),
                counts_against_follow_through: cancel.counts_against_follow_through,
                moved: false,
            });
        }
        for (number, event) in creates.into_iter().enumerate() {
            let (Some(start), Some(_)) = (event.start, event.end) else {
                problems.push(Problem::new(
                    "malformed_change",
                    format!("new event {} ({}) needs a start and an end", number + 1, summary_repr(&event)),
                ));
                continue;
            };
            // An id of the client's own names it, so a create sent again
            // finds the one it made. It's in Calendar's form (5 or more of
            // a-v and 0-9), or it's refused: replaced, a create sent again
            // would make another.
            let id = event.id.clone().map(|i| i.trim().to_string()).filter(|i| !i.is_empty());
            if let Some(bad) = id.as_ref().filter(|i| !own_event_id(i)) {
                problems.push(Problem::new(
                    "malformed_change",
                    format!("new event {} ({}): an event's id is 5 to 1024 of a-v and 0-9: {} isn't", number + 1, summary_repr(&event), py::repr(bad)),
                ));
                continue;
            }
            if let Some(id) = &id {
                if let Ok(made) = self.client.get_event(id) {
                    if !made.is_cancelled() && patched(&made, &Event { id: None, ..event.clone() }) == made {
                        again.push(made);
                    } else {
                        problems.push(Problem::new("malformed_change", format!("new event {} ({}): the id {} is already used", number + 1, summary_repr(&event), py::repr(id))));
                    }
                    continue;
                }
            }
            if let Some(problem) = share_problem(&event, true, true, true, &format!("new event {} ({})", number + 1, summary_repr(&event))) {
                problems.push(problem);
                continue;
            }
            if let Some(guard) = guarded {
                if start < guard {
                    problems.push(into_history(&event, "created", &guard));
                    continue;
                }
            }
            changes.push(Change {
                action: "create".into(),
                before: None,
                after: Some(Event { id, ..event }),
                patch: None,
                counts_against_follow_through: false,
                moved: false,
            });
        }
        if !unversioned.is_empty() {
            return Ok(Err(ChangeError::new(
                format!(
                    "Say which version of each event you change you last read: give the etag (list_events and get_event give it) for {}, so a change made since isn't overwritten. Read them again if you don't have it, then retry",
                    unversioned.iter().map(|i| py::repr(i)).collect::<Vec<_>>().join(", ")
                ),
                "unversioned",
            )));
        }
        if !stale.is_empty() {
            return Err(StaleEvents { items: stale, partly: None }.into());
        }
        if !problems.is_empty() {
            let mut error = ChangeError::of(&problems, None);
            if error.categories.is_empty() {
                error.categories = vec!["malformed_change".into()];
            }
            return Ok(Err(error));
        }
        let mut batch = Batch { changes: changes.clone(), again, ..Default::default() };
        let spans: Vec<(Moment, Moment)> = changes
            .iter()
            .filter(|c| c.action == "create" || c.moved)
            .flat_map(|c| [c.before.as_ref(), c.after.as_ref()])
            .flatten()
            .map(|e| (e.start(), e.end()))
            .collect();
        if spans.is_empty() {
            return Ok(Ok(batch));
        }
        let since = spans.iter().map(|(s, _)| *s).min().unwrap();
        // One listing, of the events from the first change through a day
        // past the latest.
        let latest = spans.iter().map(|(_, e)| *e).max().unwrap();
        let until = latest + Duration::hours(24);
        let listed: Vec<Event> = self.client.list_events(&since, &until)?.into_iter().filter(|e| !e.is_cancelled() && e.start() < until).collect();
        let mut placed: Vec<Placed> = Vec::new();
        for event in &listed {
            let change = changes.iter().rev().find(|c| c.before.as_ref().is_some_and(|b| b.id == event.id));
            match change {
                None => placed.push(Placed { event: event.clone(), key: event.id.clone().unwrap_or_default(), moved: false, asked: false }),
                Some(change) if change.action == "update" => placed.push(Placed {
                    event: change.after.clone().unwrap(),
                    key: event.id.clone().unwrap_or_default(),
                    moved: change.moved,
                    asked: true,
                }),
                Some(_) => {}
            }
        }
        for (number, change) in changes.iter().filter(|c| c.action == "create").enumerate() {
            placed.push(Placed { event: change.after.clone().unwrap(), key: format!("new {}", number + 1), moved: true, asked: true });
        }
        let listed_ids: Vec<&Option<String>> = listed.iter().map(|e| &e.id).collect();
        for change in &changes {
            if change.action == "update" {
                let before = change.before.as_ref().unwrap();
                let after = change.after.as_ref().unwrap();
                if !listed_ids.contains(&&before.id) && after.end() > since {
                    placed.push(Placed { event: after.clone(), key: before.id.clone().unwrap_or_default(), moved: change.moved, asked: true });
                }
            }
        }
        let problems = overlap_problems(&placed, None);
        if !problems.is_empty() {
            return Ok(Err(rejection(&problems, &placed, &since, &until, history_until.as_ref())));
        }
        batch.listing = placed;
        batch.since = Some(since);
        batch.until = Some(until);
        Ok(Ok(batch))
    }

    /// Write `batch`'s changes; the events as written. A patch Calendar
    /// refuses as made to an older version of the event (a 412, given its
    /// etag) is a `StaleEvent`, with the event as it is now.
    pub fn apply(&self, batch: &Batch, source: &str) -> anyhow::Result<Vec<Event>> {
        let patch = |patch: &Event| -> anyhow::Result<Event> {
            self.client.update_event(patch).map_err(|error| match (&patch.etag.0, &patch.id) {
                (Some(expected), Some(id)) if status_of(&error) == Some(412) => {
                    let now = self.client.client().fresh_event(id).ok().filter(|e| !e.is_cancelled());
                    StaleEvent { event_id: id.clone(), expected: expected.clone(), event: now }.into()
                }
                _ => error,
            })
        };
        let mut written = batch.again.clone();
        // What's been written, as said if it stops partway.
        let mut done: Vec<String> = Vec::new();
        let mut made_unnamed: Vec<String> = Vec::new();
        let stopped = |error: anyhow::Error, done: &[String], made_unnamed: &[String]| -> anyhow::Error {
            match error.downcast::<StaleEvent>() {
                Ok(one) => StaleEvents {
                    items: vec![one],
                    partly: Some(rows::Partly { written: done.to_vec(), of: batch.changes.len(), made_unnamed: made_unnamed.to_vec() }),
                }
                .into(),
                Err(error) => error,
            }
        };
        for change in &batch.changes {
            match change.action.as_str() {
                "update" => {
                    let event = patch(change.patch.as_ref().unwrap()).map_err(|e| stopped(e, &done, &made_unnamed))?;
                    done.push(format!("updated {}", event.id.clone().unwrap_or_default()));
                    written.push(event);
                }
                "create" => {
                    let asked = change.after.as_ref().unwrap();
                    let event = self.client.create_event(asked).map_err(|e| stopped(e, &done, &made_unnamed))?;
                    let id = event.id.clone().unwrap_or_default();
                    if asked.id.is_none() {
                        made_unnamed.push(id.clone());
                    }
                    done.push(format!("created {id}"));
                    written.push(event);
                }
                _ => {
                    let before = change.before.as_ref().unwrap();
                    let event = patch(change.patch.as_ref().unwrap()).map_err(|e| stopped(e, &done, &made_unnamed))?;
                    done.push(format!("cancelled {}", event.id.clone().unwrap_or_default()));
                    written.push(event);
                    if change.counts_against_follow_through {
                        if let Some(cancellations) = &self.cancellations {
                            cancellations.record(before, source, None)?;
                        }
                    }
                }
            }
        }
        Ok(written)
    }

}

/// Whether `id` is one a client may give a new event: Calendar's form,
/// 5 to 1024 of a-v and 0-9.
pub fn own_event_id(id: &str) -> bool {
    (5..=1024).contains(&id.len()) && id.chars().all(|c| matches!(c, 'a'..='v' | '0'..='9'))
}

/// The batch's changes beside the events around them, as compaction draws
/// its plans.
pub fn timeline(batch: &Batch) -> Option<Timeline> {
    let mut shown: Vec<TimelineEvent> = Vec::new();
    for p in &batch.listing {
        let change = batch.changes.iter().rev().find(|c| c.before.as_ref().is_some_and(|b| b.id == p.event.id && b.id.is_some()));
        let before = change.and_then(|c| c.before.clone()).unwrap_or_else(|| p.event.clone());
        let status = if p.event.id.is_none() {
            "new"
        } else if change.is_some_and(|c| c.moved) {
            "adjusted"
        } else {
            "planned"
        };
        shown.push(TimelineEvent {
            summary: p.event.summary.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| p.key.clone()),
            status: status.into(),
            event_id: p.event.id.clone(),
            start: p.event.start,
            end: p.event.end,
            planned_start: if p.event.id.is_none() { None } else { before.start },
            planned_end: if p.event.id.is_none() { None } else { before.end },
            ..Default::default()
        });
    }
    for change in &batch.changes {
        if change.action == "cancel" {
            let before = change.before.as_ref().unwrap();
            shown.push(TimelineEvent {
                summary: before.summary.clone().filter(|s| !s.is_empty()).unwrap_or_else(|| before.id.clone().unwrap_or_default()),
                status: "cancelled".into(),
                event_id: before.id.clone(),
                planned_start: before.start,
                planned_end: before.end,
                ..Default::default()
            });
        }
    }
    if shown.is_empty() { None } else { Some(build_timeline(Vec::new(), shown, None, true, None)) }
}

/// What's wrong with `event`'s goal shares (with `goals`), action
/// shares (with `actions`) and its people's and places' (with `facts`),
/// for its whole length, named as `name` -- or None.
fn share_problem(event: &Event, goals: bool, actions: bool, facts: bool, name: &str) -> Option<Problem> {
    let (Some(start), Some(end)) = (event.start, event.end) else { return None };
    let minutes = (end - start).num_minutes();
    let mut problems = if goals { share_problems(event.goals.as_deref().unwrap_or_default(), minutes) } else { Vec::new() };
    if let Some(actions) = event.action_ids.as_deref().filter(|a| actions && !a.is_empty() && !event.actions_from_label) {
        problems.extend(share_problems(actions, minutes));
    }
    if let Some(given) = event.facts.as_ref().filter(|_| facts) {
        problems.extend(given.share_problems(minutes).into_iter().map(|p| format!("its facts' {p}")));
    }
    if problems.is_empty() {
        return None;
    }
    Some(Problem::new("malformed_change", format!("{name}: {}", problems.join("; "))))
}

/// `before` with `patch`'s set fields, and those it clears removed.
pub fn patched(before: &Event, patch: &Event) -> Event {
    let mut after = before.clone();
    macro_rules! take {
        ($($field:ident),*) => { $( if patch.$field.is_some() { after.$field = patch.$field.clone(); } )* };
    }
    take!(summary, start, end, description, location, status, recurring_event_id, recurrence, time_zone, original_start,
          priority, event_label_id, action_ids, goals, facts, judgments, action_priority, goal_priority);
    if patch.actions_from_label {
        after.actions_from_label = true;
    }
    for name in &patch.cleared {
        match name.as_str() {
            "description" => after.description = None,
            "location" => after.location = None,
            "priority" => after.priority = None,
            "facts" => after.facts = None,
            "judgments" => after.judgments = None,
            _ => {}
        }
    }
    after
}

fn only_runs_on(before: &Event, after: &Event, history_until: &Moment) -> bool {
    if before.end() <= *history_until {
        return false;
    }
    let mut same = after.clone();
    same.end = before.end;
    same == *before && after.end() >= *history_until
}

fn history_problem(event: &Event, how: &str, history_until: &Moment) -> Problem {
    Problem::new(
        "compacted",
        format!(
            "{} ({}) is history -- it started before the last compaction, at {} -- so it can't be {how} {APPROVAL}. An event still going on then may run on: move only its end, to no earlier than that.",
            summary_repr(event),
            event.id.clone().unwrap_or_else(|| "None".into()),
            py::isoformat(history_until)
        ),
    )
}

fn into_history(event: &Event, how: &str, history_until: &Moment) -> Problem {
    Problem::new(
        "compacted",
        format!(
            "{} can't be {how} to start at {}: everything before the last compaction, at {}, is history, so it can't start before then {APPROVAL}.",
            summary_repr(event),
            py::isoformat(&event.start()),
            py::isoformat(history_until)
        ),
    )
}

#[cfg(test)]
#[path = "event_changes_tests.rs"]
mod tests;
