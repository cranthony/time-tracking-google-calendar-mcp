//! TestPublicEvent, TestListEvents, TestGetEvent, TestUpdateEvent,
//! TestCreateEvent, TestDeleteEvent, TestNote and TestGetNotes.

use super::*;
use crate::facts::Facts;
use crate::sheet::{ACTIONS, LOCATIONS, PEOPLE};
use crate::stores::actions::{ActionTree, ACTION_ROW};
use crate::stores::groups::{palette, GroupTree};
use crate::tools::events::{public_event, to_event};

fn action(id: &str, name: &str, priority: Option<i64>) -> Value {
    json!({"id": id, "name": name, "status": "active", "label_id": format!("label-{id}"), "priority": priority})
}

fn tree(actions: &[Value]) -> ActionTree {
    ActionTree::new(actions.iter().map(|a| ACTION_ROW.from_value(a)).collect(), GroupTree::new(vec![], palette(&[])))
}

fn with_actions(server: &Server, actions: &[Value]) {
    server.t.app.actions().unwrap();
    server.t.seed(ACTIONS.1, actions);
}

fn day_at(hour: u32) -> Moment {
    at(hour, 0)
}

// -- PublicEvent ------------------------------------------------------------------------

#[test]
fn actions_inferred_from_a_label_are_marked_and_not_written_back() {
    let public = public_event(&Event { id: Some("e1".into()), action_ids: Some(strings(&["g1"])), actions_from_label: true, ..Default::default() }, None);

    assert_eq!(public["actions_from_label"], true);
    assert_eq!(to_event(&public, &[]).ok().unwrap().action_ids, None);
    let mut confirmed = public.clone();
    confirmed["actions_from_label"] = json!(false); // Confirmed: stored.
    assert_eq!(to_event(&confirmed, &[]).ok().unwrap().action_ids, Some(strings(&["g1"])));
}

#[test]
fn a_public_event_shows_just_its_public_fields() {
    let public = public_event(&focus("e1", day_at(9), day_at(10)), None);

    let fields: Vec<&String> = public.as_object().unwrap().keys().collect();
    assert_eq!(
        fields,
        vec![
            "id", "summary", "start", "end", "description", "location", "priority", "action_ids", "action_names", "actions_from_label",
            "event_label_id", "is_cancelled", "effective_priority", "is_end_of_day_sleep", "recurring_event_id", "facts", "judgments",
        ]
    );
}

#[test]
fn shows_but_never_writes_back_sleep_series_label_and_effective_priority() {
    let event = Event {
        is_end_of_day_sleep: Some(true),
        recurring_event_id: Some("series-1".into()),
        event_label_id: Some("label-1".into()),
        priority: Some(1),
        ..focus("abc123", day_at(9), day_at(10))
    };

    let public = public_event(&event, None);

    assert_eq!((public["is_end_of_day_sleep"].clone(), public["recurring_event_id"].clone(), public["event_label_id"].clone()), (json!(true), json!("series-1"), json!("label-1")));
    assert_eq!(public["effective_priority"], 1);
    let mut sent = public.clone();
    sent["effective_priority"] = json!(0);
    let back = to_event(&sent, &[]).ok().unwrap();
    assert_eq!((back.id.as_deref(), back.priority), (Some("abc123"), Some(1)));
    assert_eq!((back.is_end_of_day_sleep, back.recurring_event_id, back.event_label_id), (None, None, None));
}

#[test]
fn effective_priority_comes_from_the_events_actions_when_it_has_none() {
    let public = public_event(&Event { action_ids: Some(strings(&["g1"])), action_priority: Some(0), ..focus("abc123", day_at(9), day_at(10)) }, None);

    assert_eq!((public["priority"].clone(), public["effective_priority"].clone()), (Value::Null, json!(0)));
    assert_eq!(to_event(&json!({"id": "abc123", "effective_priority": 0}), &[]).ok().unwrap().priority, None);
}

#[test]
fn names_the_events_actions_and_writes_back_only_their_ids() {
    let event = Event { action_ids: Some(strings(&["g2", "g1", "gone"])), ..focus("abc123", day_at(9), day_at(10)) };

    let public = public_event(&event, Some(&tree(&[action("g1", "Cooking", None), action("g2", "Hosting", None)])));

    assert_eq!(public["action_ids"], json!(["g2", "g1", "gone"]));
    assert_eq!(public["action_names"], json!(["Hosting", "Cooking", "(unknown action gone)"]));
    assert_eq!(to_event(&json!({"id": "abc123", "action_ids": ["g1", "g2"], "action_names": ["A", "B"]}), &[]).ok().unwrap().action_ids, Some(strings(&["g1", "g2"])));
}

#[test]
fn shows_a_cancellation_alongside_its_other_fields() {
    let event = Event { status: Some("cancelled".into()), priority: Some(1), location: Some("Room".into()), ..focus("abc123", day_at(9), day_at(10)) };

    let public = public_event(&event, None);

    assert_eq!(
        (public["id"].clone(), public["is_cancelled"].clone(), public["summary"].clone(), public["priority"].clone(), public["location"].clone()),
        (json!("abc123"), json!(true), json!("Focus block"), json!(1), json!("Room"))
    );
    assert_eq!(public["start"], when(day_at(9)));
    assert_eq!(public_event(&Event { status: Some("confirmed".into()), ..focus("abc123", day_at(9), day_at(10)) }, None)["is_cancelled"], false);
}

#[test]
fn is_cancelled_true_writes_a_cancellation_and_false_nothing() {
    assert_eq!(to_event(&json!({"id": "abc123", "is_cancelled": true}), &[]).ok().unwrap().status.as_deref(), Some("cancelled"));
    assert_eq!(to_event(&json!({"id": "abc123", "is_cancelled": false}), &[]).ok().unwrap().status, None);
}

// -- listing and getting -----------------------------------------------------------------

fn listed(server: &Server) -> Vec<Value> {
    server.ok("list_events", json!({"min_time": "2026-01-01T00:00:00+00:00", "max_time": "2026-01-02T00:00:00+00:00"})).as_array().unwrap().clone()
}

#[test]
fn lists_the_calendars_events_with_sleep_and_series() {
    let server = Server::new();
    server.seed(&[
        Event { is_end_of_day_sleep: Some(true), ..focus("abc123", day_at(9), day_at(10)) },
        Event { recurrence: Some(strings(&["RRULE:FREQ=DAILY"])), time_zone: Some("UTC".into()), ..focus("series1", day_at(12), day_at(13)) },
    ]);

    let events = listed(&server);

    assert_eq!(events.len(), 2);
    assert_eq!(events[0], public_event(&server.event("abc123"), Some(&server.t.app.actions().unwrap().tree().unwrap())));
    assert_eq!(events[0]["is_end_of_day_sleep"], true);
    assert_eq!(events[1]["recurring_event_id"], "series1");
}

#[test]
fn listing_omits_cancelled_events() {
    let server = Server::new();
    server.seed(&[focus("abc123", day_at(9), day_at(10)), Event { status: Some("cancelled".into()), ..focus("def456", day_at(10), day_at(11)) }]);

    let ids: Vec<Value> = listed(&server).iter().map(|e| e["id"].clone()).collect();

    assert_eq!(ids, vec![json!("abc123")]);
}

#[test]
fn fills_in_actions_and_effective_priority_from_the_event_label() {
    // An event never given actions has only a label: it's read as doing the
    // action that holds it.
    let server = Server::new();
    with_actions(&server, &[json!({"id": "g1", "name": "Focus", "status": "active", "label_id": "label-1", "priority": 0})]);
    server.seed(&[Event { event_label_id: Some("label-1".into()), ..focus("abc123", day_at(9), day_at(10)) }]);

    let event = &listed(&server)[0];

    assert_eq!((event["action_ids"].clone(), event["action_names"].clone(), event["effective_priority"].clone()), (json!(["g1"]), json!(["Focus"]), json!(0)));
    // The event's own priority stays unset, so sending it back to
    // update_event doesn't copy the action's onto it.
    assert!(event["priority"].is_null());
}

#[test]
fn an_events_own_priority_wins_over_its_actions() {
    let server = Server::new();
    with_actions(&server, &[action("g1", "Focus", Some(0))]);
    server.seed(&[Event { action_ids: Some(strings(&["g1"])), priority: Some(3), ..focus("abc123", day_at(9), day_at(10)) }]);

    assert_eq!(listed(&server)[0]["effective_priority"], 3);
}

#[test]
fn gets_an_event_filled_in_from_its_actions() {
    let server = Server::new();
    with_actions(&server, &[action("g1", "Focus", Some(0))]);
    server.seed(&[Event { action_ids: Some(strings(&["g1"])), is_end_of_day_sleep: Some(true), ..focus("abc123", day_at(9), day_at(10)) }]);

    let event = server.ok("get_event", json!({"id": "abc123"}));

    assert_eq!((event["is_end_of_day_sleep"].clone(), event["effective_priority"].clone(), event["priority"].clone()), (json!(true), json!(0), Value::Null));
}

#[test]
fn a_cancelled_event_isnt_got() {
    let server = Server::new();
    server.seed(&[Event { status: Some("cancelled".into()), ..focus("abc123", day_at(9), day_at(10)) }]);

    server.refused("get_event", json!({"id": "abc123"}), "has been cancelled");
}

// -- changing events -----------------------------------------------------------------------

/// Two events, 09:00-10:00 and 10:00-11:00.
fn two() -> Server {
    let server = Server::new();
    server.seed(&[focus("abc123", day_at(9), day_at(10)), focus("def456", day_at(10), day_at(11))]);
    server
}

fn update(id: &str, fields: Value) -> Value {
    let mut event = fields;
    event["id"] = json!(id);
    json!({"event": event})
}

#[test]
fn updates_several_events_at_once() {
    let server = two();

    let result = server.ok("update_event", json!({"updates": [update("abc123", json!({"start": when(at(8, 0)), "end": when(at(9, 0))})), update("def456", json!({"summary": "Renamed"}))]}));

    let ids: Vec<Value> = result["events"].as_array().unwrap().iter().map(|e| e["id"].clone()).collect();
    assert_eq!(ids, vec![json!("abc123"), json!("def456")]);
    assert_eq!(server.event("abc123").start, Some(at(8, 0)));
    assert_eq!(server.event("def456").summary.as_deref(), Some("Renamed"));
    assert_eq!(result["dry_run"], false);
}

#[test]
fn an_overlap_is_refused_changing_nothing() {
    let server = two();

    server.refused("update_event", json!({"updates": [update("abc123", json!({"start": when(at(9, 0)), "end": when(at(10, 30))}))]}), "Nothing was changed");

    assert_eq!(server.event_writes(), 0);
}

#[test]
fn moves_cancels_creates_and_shifts_together() {
    let server = two();

    let result = server.ok(
        "update_event",
        json!({
            "updates": [update("abc123", json!({"start": when(at(9, 0)), "end": when(at(10, 30))}))],
            "cancels": [{"event_id": "def456", "counts_against_follow_through": true}],
            "creates": [{"summary": "Walk", "start": when(at(10, 30)), "end": when(at(11, 0))}],
        }),
    );

    assert!(server.event("def456").is_cancelled());
    assert!(!server.t.app.cancellations().unwrap().all().unwrap().is_empty());
    let listed = server.t.app.calendar_client().unwrap().list_events(&at(0, 0), &at(23, 0)).unwrap();
    assert!(listed.iter().any(|e| e.summary.as_deref() == Some("Walk")));
    assert!(result["timeline"].as_str().unwrap().contains("✕"));
}

#[test]
fn a_shift_moves_its_events() {
    let server = two();

    server.ok("update_event", json!({"shifts": [{"event_ids": ["abc123", "def456"], "minutes": 60}]}));

    assert_eq!((server.event("abc123").start, server.event("def456").start), (Some(at(10, 0)), Some(at(11, 0))));
}

#[test]
fn a_dry_run_changes_nothing() {
    let server = two();

    let result = server.ok("update_event", json!({"updates": [update("abc123", json!({"start": when(at(8, 0)), "end": when(at(9, 0))}))], "dry_run": true}));

    assert_eq!(result["dry_run"], true);
    assert_eq!(server.event_writes(), 0);
    assert!(result["timeline"].as_str().unwrap().contains("Focus block · ⇠1h00m"), "{}", result["timeline"]);
}

#[test]
fn an_update_cant_cancel() {
    let server = two();

    server.refused("update_event", json!({"updates": [update("abc123", json!({"is_cancelled": true}))]}), "put it in cancels");

    assert_eq!(server.event_writes(), 0);
}

#[test]
fn history_is_changed_only_when_allowed() {
    let server = Server::new();
    server.seed(&[focus("abc123", day_at(9), day_at(10))]);
    let journal = server.t.app.journal().unwrap();
    {
        let _held = crate::write_lock::WRITE_LOCK.hold();
        let plan = crate::compaction::plan::CompactionPlan::default();
        let day = crate::stores::journal::PlannedDay {
            compaction_id: "prev".into(),
            now: at(10, 0),
            note_ids: vec![],
            decisions: &[],
            plan: &plan,
            ignore_notes: vec![],
            additions: Default::default(),
            note_targets: vec![],
        };
        journal.start_batch(&[day], None, &[]).unwrap();
        let mut prev = journal.load("prev").unwrap();
        journal.set_status(&mut prev, crate::stores::journal::STAMPED).unwrap();
    }
    let rename = json!({"updates": [update("abc123", json!({"summary": "Renamed"}))]});

    server.refused("update_event", rename.clone(), "is history");
    let mut allowed = rename;
    allowed["allow_compacted_changes"] = json!(true);
    server.ok("update_event", allowed);

    assert_eq!(server.event("abc123").summary.as_deref(), Some("Renamed"));
}

#[test]
fn clears_the_fields_it_names_and_refuses_to_both_set_and_clear_one() {
    let server = Server::new();
    server.seed(&[Event { priority: Some(2), ..focus("abc123", day_at(9), day_at(10)) }]);

    server.ok("update_event", json!({"updates": [{"event": {"id": "abc123", "summary": "Renamed"}, "clear_fields": ["priority"]}]}));

    let event = server.event("abc123");
    assert_eq!((event.summary.as_deref(), event.priority), (Some("Renamed"), None));
    server.refused("update_event", json!({"updates": [{"event": {"id": "abc123", "priority": 1}, "clear_fields": ["priority"]}]}), r"Can't both set and clear \['priority'\]");
}

#[test]
fn the_clearable_fields_are_the_events() {
    let schema = crate::mcp::contract_tool("update_event").unwrap()["inputSchema"].to_string();
    for field in crate::event::CLEARABLE_EVENT_FIELDS {
        assert!(schema.contains(&format!("\"{field}\"")), "{field}");
    }
}

#[test]
fn refuses_action_ids_that_arent_actions() {
    let server = two();
    with_actions(&server, &[action("g7k2qp", "Cooking", None)]);

    server.refused("update_event", json!({"updates": [update("abc123", json!({"action_ids": ["g7k2qq"]}))]}), r"no action with the id or name 'g7k2qq'; did you mean g7k2qp \(Cooking\)");

    assert_eq!(server.event_writes(), 0);
}

fn with_people_and_places(server: &Server) {
    server.t.app.people().unwrap();
    server.t.app.locations().unwrap();
    server.t.seed(PEOPLE.1, &[json!({"id": "sam", "name": "Sam", "status": "active"})]);
    server.t.seed(LOCATIONS.1, &[json!({"id": "home", "name": "Home"})]);
}

#[test]
fn sets_facts_normalized() {
    let server = two();
    with_people_and_places(&server);

    server.ok("update_event", json!({"updates": [update("abc123", json!({"facts": {"location_id": "home", "with_ids": [" sam"], "notes": {"sam": " glad  "}}}))]}));

    let expected = Facts::from_public(&json!({"location_id": "home", "with_ids": ["sam"], "notes": {"sam": "glad"}}));
    assert_eq!(server.event("abc123").facts.map(|f| f.to_public()), Some(expected.to_public()));
}

#[test]
fn refuses_facts_that_arent_well_formed() {
    let server = two();
    with_people_and_places(&server);

    for (facts, message) in [
        (json!({"with_ids": ["self"]}), r#"Its facts "with_ids" never names "self""#),
        (json!({"for_ids": ["p9"]}), r"Its facts name \['p9'\], who aren't people"),
        (json!({"location_id": "nowhere"}), r"Its facts' location 'nowhere' isn't a location"),
    ] {
        server.refused("update_event", json!({"updates": [update("abc123", json!({"facts": facts}))]}), message);
    }

    assert_eq!(server.event_writes(), 0);
}

#[test]
fn creates_several_at_once() {
    let server = two();

    let result = server.ok(
        "create_event",
        json!({"events": [{"summary": "Walk", "start": when(at(11, 0)), "end": when(at(11, 30))}, {"summary": "Read", "start": when(at(11, 30)), "end": when(at(12, 0))}]}),
    );

    let summaries: Vec<Value> = result["events"].as_array().unwrap().iter().map(|e| e["summary"].clone()).collect();
    assert_eq!(summaries, vec![json!("Walk"), json!("Read")]);
    assert_eq!(server.t.app.calendar_client().unwrap().list_events(&at(0, 0), &at(23, 0)).unwrap().len(), 4);
}

#[test]
fn a_create_that_overlaps_is_refused() {
    let server = two();

    server.refused("create_event", json!({"events": [{"summary": "Walk", "start": when(at(9, 30)), "end": when(at(10, 15))}]}), "overlaps 'Walk'");

    assert_eq!(server.event_writes(), 0);
}

#[test]
fn cancels_several_saying_which_count() {
    let server = two();

    let result = server.ok(
        "delete_event",
        json!({"cancels": [{"event_id": "abc123", "counts_against_follow_through": false}, {"event_id": "def456", "counts_against_follow_through": true}]}),
    );

    let cancelled: Vec<(Value, Value)> = result["events"].as_array().unwrap().iter().map(|e| (e["id"].clone(), e["is_cancelled"].clone())).collect();
    assert_eq!(cancelled, vec![(json!("abc123"), json!(true)), (json!("def456"), json!(true))]);
    let rows = server.t.app.cancellations().unwrap().all().unwrap();
    assert!(!rows.is_empty() && rows.iter().all(|r| r["event_id"] == "def456" && r["source"] == "delete_event"), "{rows:?}");
}

// -- notes -------------------------------------------------------------------------------------

#[test]
fn a_note_is_appended_and_returned_with_its_id() {
    let server = Server::new();

    let result = server.ok("note", json!({"noted_time": {"timestamp": "2026-01-01T09:00:00+00:00", "description": "Started work"}}));

    assert_eq!(result["id"], "2026-01-01T09:00:00+00:00#2");
    assert_eq!((result["timestamp"].clone(), result["description"].clone(), result["compaction_id"].clone()), (when(at(9, 0)), json!("Started work"), Value::Null));
}

#[test]
fn a_caller_never_sets_a_notes_compaction_id() {
    let server = Server::new();

    let result = server.call("note", json!({"noted_time": {"timestamp": "2026-01-01T09:00:00+00:00", "compaction_id": "sneaky"}}));

    // The schema has no such field: refused, or ignored -- never stored.
    if let Ok(note) = result {
        assert!(note["compaction_id"].is_null());
    }
    assert!(server.t.app.noted_time_sheet().unwrap().read_with_rows(true).unwrap().iter().all(|n| n.note.compaction_id.is_none()));
}

#[test]
fn notes_are_listed_uncompacted_sorted_by_timestamp_or_with_the_compacted_ones() {
    let server = Server::new();
    server.ok("note", json!({"noted_time": {"timestamp": "2026-01-01T10:00:00+00:00"}}));
    server.ok("note", json!({"noted_time": {"timestamp": "2026-01-01T09:00:00+00:00", "description": "Started work"}}));
    {
        let _held = crate::write_lock::WRITE_LOCK.hold();
        server.t.app.noted_time_sheet().unwrap().mark_compacted(&["2026-01-01T10:00:00+00:00#2".to_string()], "cmp1").unwrap();
    }
    server.ok("note", json!({"noted_time": {"timestamp": "2026-01-01T09:30:00+00:00"}}));

    let ids = |args: Value| -> Vec<Value> { server.ok("get_notes", args).as_array().unwrap().iter().map(|n| n["id"].clone()).collect() };
    assert_eq!(ids(json!({})), vec![json!("2026-01-01T09:00:00+00:00#3"), json!("2026-01-01T09:30:00+00:00#4")]);
    assert_eq!(ids(json!({"include_compacted": true})).len(), 3);
    assert_eq!(server.ok("get_notes", json!({}))[0]["description"], "Started work");
}
