//! What an event takes from its actions, and the label it's written with --
//! the ActionCalendar against the fake Google.

use super::*;
use crate::sheet::{ACTIONS, ACTION_GROUPS};
use crate::stores::actions::ACTION_ROW;
use crate::stores::groups::{palette, GroupTree, GROUP_ROW};
use crate::testing::app::{test_app, TestApp};
use crate::write_lock::WRITE_LOCK;
use serde_json::{json, Value};

fn start() -> Moment {
    chrono::DateTime::parse_from_rfc3339("2026-10-05T09:00:00+00:00").unwrap()
}

fn action_rows() -> Vec<Value> {
    vec![
        json!({"id": "cook", "name": "Cook", "status": "active", "label_id": "l-cook", "group_id": "food"}),
        json!({"id": "idea", "name": "Juggle", "status": "proposed", "label_id": "l-idea", "priority": 3}),
        json!({"id": "old", "name": "Old", "status": "archived", "label_id": "l-old"}),
    ]
}

fn group_rows() -> Vec<Value> {
    vec![json!({"id": "food", "name": "Food", "priority": 1})]
}

fn tree() -> ActionTree {
    ActionTree::new(
        action_rows().iter().map(|a| crate::sheet::rows::item_of(ACTION_ROW.from_value(a))).collect(),
        GroupTree::new(group_rows().iter().map(|g| crate::sheet::rows::item_of(GROUP_ROW.from_value(g))).collect(), palette(&[])),
    )
}

fn event() -> Event {
    Event { id: Some("e1".into()), summary: Some("x".into()), start: Some(start()), end: Some(start() + chrono::Duration::hours(1)), ..Default::default() }
}

fn doing(ids: &[&str]) -> Event {
    Event { action_ids: Some(ids.iter().map(|s| s.to_string()).collect()), ..event() }
}

fn labelled(label: &str) -> Event {
    Event { event_label_id: Some(label.into()), ..event() }
}

// -- filling in ---------------------------------------------------------------------------

#[test]
fn takes_the_highest_priority_among_the_actions_and_their_groups() {
    let filled = fill_in_from_actions(&[doing(&["idea", "cook"])], &tree()).remove(0);

    // Cook's group's.
    assert_eq!((filled.action_priority, filled.effective_priority()), (Some(1), Some(1)));
}

#[test]
fn an_event_without_actions_is_read_as_doing_its_labels() {
    let filled = fill_in_from_actions(&[labelled("l-cook")], &tree()).remove(0);

    assert_eq!((filled.action_ids, filled.actions_from_label), (Some(vec!["cook".to_string()]), true));
}

#[test]
fn leaves_alone_an_event_with_none_or_an_unknown_label() {
    let bare = labelled("someone-elses");

    assert_eq!(fill_in_from_actions(&[bare.clone()], &tree()), vec![bare]);
    assert_eq!(fill_in_from_actions(&[doing(&[])], &tree())[0].action_ids, Some(vec![]));
}

// -- the action's label -------------------------------------------------------------------------

#[test]
fn an_active_first_action_sets_the_label() {
    assert_eq!(with_action_label(&doing(&["cook", "idea"]), &tree(), true, None).event_label_id.as_deref(), Some("l-cook"));
}

#[test]
fn an_archived_action_gives_no_label() {
    assert_eq!(with_action_label(&doing(&["old"]), &tree(), true, None).event_label_id, None);
    assert_eq!(with_action_label(&doing(&["old"]), &tree(), false, None).event_label_id.as_deref(), Some(""));
}

#[test]
fn an_update_keeps_the_label_the_event_already_has_from_its_action() {
    let given = Event { event_label_id: Some("l-old".into()), ..doing(&["old"]) };

    assert_eq!(with_action_label(&given, &tree(), false, None).event_label_id.as_deref(), Some("l-old"));
}

#[test]
fn a_proposed_action_labels_it_only_if_the_calendar_holds_the_label() {
    let given = doing(&["idea"]);

    assert_eq!(with_action_label(&given, &tree(), true, Some(&["l-idea".to_string()])).event_label_id.as_deref(), Some("l-idea"));
    assert_eq!(with_action_label(&given, &tree(), true, Some(&[])).event_label_id, None);
}

#[test]
fn untouched_when_not_writing_actions_or_inferring_them() {
    assert_eq!(with_action_label(&labelled("x"), &tree(), false, None).event_label_id.as_deref(), Some("x"));
    let inferred = Event { actions_from_label: true, ..doing(&["cook"]) };
    assert_eq!(with_action_label(&inferred, &tree(), false, None), inferred);
}

// -- the calendar -----------------------------------------------------------------------------------

struct Fixture {
    t: TestApp,
    calendar: ActionCalendar,
}

fn fixture(labels: &[Value], current: Option<Event>) -> Fixture {
    let t = test_app();
    let actions = t.app.actions().unwrap();
    t.seed(ACTIONS.1, &action_rows());
    t.seed(ACTION_GROUPS.1, &group_rows());
    if !labels.is_empty() {
        let _held = WRITE_LOCK.hold();
        let client = t.app.calendar_client().unwrap();
        let labels: Vec<EventLabel> = labels.iter().map(|l| EventLabel::from_api(l).unwrap()).collect();
        client.replace_event_labels(&labels, None).unwrap();
    }
    if let Some(current) = current {
        t.seed_event(&current);
    }
    let calendar = ActionCalendar::new(t.app.calendar_client().unwrap(), actions);
    t.fake.clear_requests();
    Fixture { t, calendar }
}

impl Fixture {
    fn create(&self, given: Event) -> Value {
        let _held = WRITE_LOCK.hold();
        self.calendar.create_event(&Event { id: Some("event1".into()), ..given }).unwrap();
        self.sent("POST")
    }

    fn update(&self, given: Event) -> Value {
        let _held = WRITE_LOCK.hold();
        self.calendar.update_event(&given).unwrap();
        self.sent("PATCH")
    }

    /// The body of the last event write.
    fn sent(&self, method: &str) -> Value {
        self.t.fake.requests().into_iter().rfind(|r| r.method == method && r.path.contains("/events")).unwrap().body
    }

    fn calendar_reads(&self) -> usize {
        self.t.fake.requests().iter().filter(|r| r.method == "GET" && r.path.ends_with(&format!("/calendars/{}", self.t.calendar_id))).count()
    }

    fn event_reads(&self) -> usize {
        self.t.fake.requests().iter().filter(|r| r.method == "GET" && r.path.ends_with("/events/e1")).count()
    }

    fn label_writes(&self) -> usize {
        self.t.fake.requests().iter().filter(|r| r.method == "PATCH" && r.path.ends_with(&format!("/calendars/{}", self.t.calendar_id))).count()
    }
}

#[test]
fn reads_fill_in_and_writes_derive_the_label() {
    // The action's label is on the calendar, as an active action's is.
    let f = fixture(&[json!({"id": "l-cook", "name": "Cook", "backgroundColor": "#123456"})], Some(doing(&["cook"])));

    assert_eq!(f.calendar.list_events(&start(), &(start() + chrono::Duration::hours(1))).unwrap()[0].action_priority, Some(1));
    assert_eq!(f.create(doing(&["cook"]))["eventLabelId"], "l-cook");
    // An active action always holds its label.
    assert_eq!(f.calendar_reads(), 0);
}

#[test]
fn a_proposed_actions_label_is_checked_against_the_calendar() {
    let f = fixture(&[json!({"id": "l-idea", "name": "Juggle", "backgroundColor": "#123456"})], None);

    assert_eq!(f.create(doing(&["idea"]))["eventLabelId"], "l-idea");
    assert_eq!(f.calendar_reads(), 1);
}

// -- priority labels --------------------------------------------------------------------------------
// An event with no action's label takes its priority's.

#[test]
fn a_priority_label_removed_since_it_was_made_sure_of_is_added_back() {
    let f = fixture(&[], None);
    f.create(Event { priority: Some(1), ..event() });
    // Removed behind the app's back, after it made sure of them.
    let client = f.t.app.calendar_client().unwrap();
    {
        let _held = WRITE_LOCK.hold();
        client.replace_event_labels(&[], None).unwrap();
    }

    let _held = WRITE_LOCK.hold();
    let created = f.calendar.create_event(&Event { id: Some("event2".into()), priority: Some(1), ..event() }).unwrap();

    assert_eq!(created.event_label_id, Some(priority_label_id(Some(1))));
    let labels = client.list_event_labels().unwrap().0;
    assert!(labels.iter().any(|l| l.id.as_deref() == Some(priority_label_id(Some(1)).as_str())));
}

#[test]
fn an_event_without_actions_takes_its_priority_label() {
    let f = fixture(&[], None);

    assert_eq!(f.create(Event { priority: Some(1), ..event() })["eventLabelId"], json!(priority_label_id(Some(1))));
    // Made before any is used.
    let labels = f.t.app.calendar_client().unwrap().list_event_labels().unwrap().0;
    assert!(labels.iter().any(|l| l.id.as_deref() == Some(priority_label_id(Some(1)).as_str())));
}

#[test]
fn without_a_priority_it_takes_the_default_ones() {
    assert_eq!(fixture(&[], None).create(event())["eventLabelId"], json!(priority_label_id(Some(2))));
}

#[test]
fn an_action_that_doesnt_hold_a_label_gives_its_priority() {
    // No room for Juggle's.
    assert_eq!(fixture(&[], None).create(doing(&["idea"]))["eventLabelId"], json!(priority_label_id(Some(3))));
}

#[test]
fn clearing_its_actions_gives_it_its_priority_label() {
    let f = fixture(&[], Some(event()));

    assert_eq!(f.update(Event { priority: Some(0), ..doing(&[]) })["eventLabelId"], json!(priority_label_id(Some(0))));
}

#[test]
fn a_new_priority_relabels_an_event_without_an_actions_label() {
    let f = fixture(&[], Some(labelled(&priority_label_id(Some(2)))));

    let sent = f.update(Event { id: Some("e1".into()), priority: Some(0), ..Default::default() });

    assert_eq!(sent["eventLabelId"], json!(priority_label_id(Some(0))));
    assert_eq!(f.event_reads(), 1);
}

#[test]
fn a_new_priority_keeps_an_actions_label() {
    let f = fixture(&[], Some(Event { event_label_id: Some("l-cook".into()), ..doing(&["cook"]) }));

    let sent = f.update(Event { id: Some("e1".into()), priority: Some(0), ..Default::default() });

    // Not written: it keeps l-cook.
    assert!(sent.get("eventLabelId").is_none(), "{sent}");
}

#[test]
fn one_inferred_from_its_label_keeps_it_too() {
    let f = fixture(&[], Some(labelled("l-cook")));

    assert!(f.update(Event { id: Some("e1".into()), priority: Some(0), ..Default::default() }).get("eventLabelId").is_none());
}

#[test]
fn an_update_writing_neither_actions_nor_a_priority_is_untouched() {
    let f = fixture(&[], Some(event()));

    let sent = f.update(Event { id: Some("e1".into()), summary: Some("Renamed".into()), ..Default::default() });

    assert!(sent.get("eventLabelId").is_none());
    assert_eq!(f.event_reads(), 0);
    assert_eq!(f.label_writes(), 0);
}
