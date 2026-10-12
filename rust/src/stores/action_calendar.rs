//! Events tied to actions: what an event inherits from its actions is
//! filled in on read, and its label derived from them on write.

use crate::event::{Event, Moment};
use crate::google::calendar::{CalendarClient, EventLabel};
use crate::sheet::rows;
use crate::stores::actions::{ActionTree, Actions};
use crate::stores::groups::priority_label_id;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;

#[derive(Clone)]
pub struct ActionCalendar {
    client: CalendarClient,
    actions: Actions,
    /// Whether this calendar's priority labels have been made sure of --
    /// for as long as this ActionCalendar lives, as in Python.
    priority_labels_ready: Arc<AtomicBool>,
}

impl ActionCalendar {
    pub fn new(client: CalendarClient, actions: Actions) -> ActionCalendar {
        ActionCalendar { client, actions, priority_labels_ready: Arc::new(AtomicBool::new(false)) }
    }

    pub fn client(&self) -> &CalendarClient {
        &self.client
    }

    pub fn list_events(&self, time_min: &Moment, time_max: &Moment) -> anyhow::Result<Vec<Event>> {
        let events = self.client.list_events(time_min, time_max)?;
        Ok(fill_in_from_actions(&events, &self.actions.tree()?))
    }

    pub fn get_event(&self, event_id: &str) -> anyhow::Result<Event> {
        let event = self.client.get_event(event_id)?;
        Ok(fill_in_from_actions(&[event], &self.actions.tree()?).remove(0))
    }

    pub fn create_event(&self, event: &Event) -> anyhow::Result<Event> {
        let labelled = self.with_label(event, true)?;
        self.client.create_event(&labelled)
    }

    pub fn update_event(&self, event: &Event) -> anyhow::Result<Event> {
        let labelled = self.with_label(event, false)?;
        self.client.update_event(&labelled)
    }

    pub fn import_event(&self, event: &Event, ical_uid: &str) -> anyhow::Result<Event> {
        let labelled = self.with_label(event, true)?;
        self.client.import_event(&labelled, ical_uid)
    }

    pub fn list_event_labels(&self) -> anyhow::Result<(Vec<EventLabel>, Option<String>)> {
        self.client.list_event_labels()
    }

    fn with_label(&self, event: &Event, inserting: bool) -> anyhow::Result<Event> {
        let tree = self.actions.tree()?;
        let writes_actions = event.action_ids.is_some() && !event.actions_from_label;
        let writes_priority = event.priority.is_some() || event.cleared.contains("priority");
        if !(inserting || writes_actions || writes_priority) {
            return Ok(event.clone());
        }
        let current = if !inserting && !writes_actions {
            let id = event.id.as_deref().unwrap_or_default();
            Some(fill_in_from_actions(&[self.client.get_event(id)?], &tree).remove(0))
        } else {
            None
        };
        let doing = match &current {
            Some(current) => Event {
                action_ids: current.action_ids.clone(),
                event_label_id: current.event_label_id.clone(),
                actions_from_label: false,
                ..event.clone()
            },
            None => event.clone(),
        };
        let primary = doing.action_ids.as_ref().and_then(|ids| ids.first()).and_then(|id| tree.by_id(id));
        let mut held: Option<Vec<String>> = None;
        if let Some(primary) = primary {
            if rows::text(primary, "status") == Some("proposed") && crate::stores::groups::truthy(primary.get("label_id")) {
                held = Some(self.client.list_event_labels()?.0.into_iter().filter_map(|l| l.id).collect());
            }
        }
        let label = if doing.action_ids.is_none() {
            if inserting { event.event_label_id.clone() } else { None }
        } else {
            with_action_label(&doing, &tree, inserting, held.as_deref()).event_label_id
        };
        if let Some(label) = label.filter(|l| !l.is_empty()) {
            if current.as_ref().is_some_and(|c| c.event_label_id.as_deref() == Some(label.as_str())) {
                return Ok(event.clone());
            }
            return Ok(Event { event_label_id: Some(label), ..event.clone() });
        }
        let priority_label = self.priority_label(event, current.as_ref(), doing.action_ids.as_deref(), &tree)?;
        Ok(Event { event_label_id: Some(priority_label), ..event.clone() })
    }

    fn priority_label(&self, event: &Event, current: Option<&Event>, action_ids: Option<&[String]>, tree: &ActionTree) -> anyhow::Result<String> {
        if !self.priority_labels_ready.load(Ordering::SeqCst) {
            self.actions.ensure_priority_labels()?;
            self.priority_labels_ready.store(true, Ordering::SeqCst);
        }
        let mut priority = event.priority;
        if priority.is_none() && !event.cleared.contains("priority") {
            if let Some(current) = current {
                priority = current.priority;
            }
        }
        if priority.is_none() {
            priority = action_ids.into_iter().flatten().filter_map(|a| tree.priority(a)).min();
        }
        Ok(priority_label_id(priority))
    }
}

/// `events`, each with its action_ids (from its label, for one never
/// given actions) and action_priority filled in.
pub fn fill_in_from_actions(events: &[Event], tree: &ActionTree) -> Vec<Event> {
    events.iter().map(|e| fill_in(e, tree)).collect()
}

fn fill_in(event: &Event, tree: &ActionTree) -> Event {
    let mut event = event.clone();
    let action_ids = match &event.action_ids {
        Some(ids) => ids.clone(),
        None => {
            let Some(owner) = tree.action_for_label(event.event_label_id.as_deref()) else { return event };
            event.actions_from_label = true;
            vec![crate::stores::groups::python_str(owner.get("id"))]
        }
    };
    if action_ids.is_empty() {
        event.action_ids = Some(action_ids);
        return event;
    }
    event.action_priority = action_ids.iter().filter_map(|i| tree.priority(i)).min();
    event.action_ids = Some(action_ids);
    event
}

/// `event` with its label derived from its first action ("" for none on
/// an update, None on an insert), or unchanged if it isn't writing
/// action_ids.
pub fn with_action_label(event: &Event, tree: &ActionTree, inserting: bool, held: Option<&[String]>) -> Event {
    let Some(ids) = &event.action_ids else { return event.clone() };
    if event.actions_from_label {
        return event.clone();
    }
    let none = if inserting { None } else { Some(String::new()) };
    let primary = ids.first().and_then(|id| tree.by_id(id));
    let label_id = primary.and_then(|p| p.get("label_id")).filter(|l| crate::stores::groups::truthy(Some(l))).and_then(|l| l.as_str());
    let (Some(primary), Some(label_id)) = (primary, label_id) else {
        return Event { event_label_id: none, ..event.clone() };
    };
    if !inserting && event.event_label_id.as_deref() == Some(label_id) {
        return event.clone();
    }
    let holds = match held {
        Some(held) => held.iter().any(|h| h == label_id),
        None => rows::text(primary, "status") == Some("active"),
    };
    Event { event_label_id: if holds { Some(label_id.to_string()) } else { none }, ..event.clone() }
}

#[cfg(test)]
#[path = "action_calendar_tests.rs"]
mod tests;
