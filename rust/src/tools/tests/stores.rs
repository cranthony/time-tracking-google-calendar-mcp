//! TestTraitTools, TestPartActions, TestActionTools, TestPeopleTools,
//! TestCompactionScheduleTools, TestLocationTools, TestPublicRecurrence,
//! TestDeleteRecurrence, TestSetTimeZone and TestWriteLock.

use super::*;
use crate::sheet::{ACTIONS, ACTION_GROUPS, PEOPLE};

fn part(action: &str) -> Value {
    json!({"kind": "count", "target": 1, "interval_days": 21, "noun": "visits", "action": action})
}

/// An action, a deleted one, and a group.
fn with_scopes(server: &Server) {
    server.t.app.actions().unwrap();
    server.t.seed(ACTIONS.1, &[
        json!({"id": "act", "name": "Focus", "status": "active", "label_id": "label-act"}),
        json!({"id": "gone", "name": "Old", "status": "deleted", "label_id": "label-gone"}),
    ]);
    server.t.seed(ACTION_GROUPS.1, &[json!({"id": "grp", "name": "Music"})]);
}

// -- traits and their parts' actions ------------------------------------------------------

#[test]
fn trait_tools_list_create_and_update_and_say_what_went_wrong() {
    let server = Server::new();

    let names: Vec<Value> = server.ok("get_traits", json!({"statuses": ["active"]})).as_array().unwrap().iter().map(|t| t["id"].clone()).collect();
    assert_eq!(names.len(), 5);
    server.refused("create_trait", json!({"trait": {"name": "X", "parts": [{"kind": "count", "target": 0}]}}), "must be a number above 0");
    server.refused("update_trait", json!({"trait": {"id": "x"}, "clear_fields": ["definition"]}), "isn't a trait");
    assert_eq!(server.ok("update_trait", json!({"trait": {"id": "creative"}, "clear_fields": ["definition"]}))["definition"], Value::Null);
}

#[test]
fn a_parts_action_or_group_is_accepted() {
    let server = Server::new();
    with_scopes(&server);

    server.ok("create_trait", json!({"trait": {"name": "X", "parts": [part("act"), part("grp")]}}));
    server.ok("update_person", json!({"person": {"id": "self", "traits": {"parts": {"reliable": [part("grp")]}}}}));
    server.ok("create_habit", json!({"habit": {"name": "H", "action_id": "act", "traits": {"parts": {"reliable": [part("act")]}}}}));
}

#[test]
fn anything_else_is_refused_by_every_tool_taking_parts() {
    let server = Server::new();
    with_scopes(&server);
    let habit = server.ok("create_habit", json!({"habit": {"name": "H", "action_id": "act"}}))["created_id"].clone();

    for action in ["nosuch", "gone"] {
        let parts = json!({"reliable": [part(action)]});
        for (tool, args) in [
            ("create_trait", json!({"trait": {"name": "X", "parts": [part(action)]}})),
            ("update_trait", json!({"trait": {"id": "reliable", "parts": [part(action)]}})),
            ("create_person", json!({"person": {"name": "Sam", "traits": {"parts": parts}}})),
            ("update_person", json!({"person": {"id": "self", "traits": {"parts": parts}}})),
            ("create_habit", json!({"habit": {"name": "H2", "action_id": "act", "traits": {"parts": parts}}})),
            ("update_habit", json!({"habit": {"id": habit, "traits": {"parts": parts}}})),
        ] {
            server.refused(tool, args, &format!("A part's action '{action}' isn't an action or action group"));
        }
    }
    let people = server.ok("get_people", json!({}));
    assert_eq!(people.as_array().unwrap().len(), 1);
}

// -- actions, people, locations -------------------------------------------------------------

#[test]
fn action_tools_run_the_store() {
    let server = Server::new();

    let walk = server.ok("create_action", json!({"action": {"name": "Walk", "note": "outside"}}))["created_id"].clone();
    server.ok("update_action", json!({"action": {"id": walk}, "clear_fields": ["note"]}));
    let group = server.ok("create_action_group", json!({"group": {"name": "Creative", "note": "n"}}))["created_id"].clone();
    server.ok("update_action_group", json!({"group": {"id": group}, "clear_fields": ["note"]}));
    server.ok("update_priority_color", json!({"priority": 1, "color": "#123456"}));

    assert_eq!(server.ok("get_action", json!({"id_or_name": "Walk"}))["note"], Value::Null);
    assert_eq!(server.ok("get_actions", json!({"statuses": ["proposed"]}))["actions"].as_array().unwrap().len(), 1);
    assert_eq!(server.ok("get_action_group", json!({"id_or_name": "Creative"}))["note"], Value::Null);
    assert_eq!(server.ok("get_action_groups", json!({})).as_array().unwrap().len(), 1);
    let colors = server.ok("get_priority_colors", json!({}));
    assert!(colors.as_array().unwrap().iter().any(|c| c["priority"] == 1 && c["color"] == "#123456"));
    assert_eq!(server.ok("delete_action_group", json!({"group_id": group}))["deleted"]["name"], "Creative");
}

#[test]
fn action_tool_errors_are_tool_errors() {
    let server = Server::new();
    server.ok("create_action", json!({"action": {"name": "Walk"}}));

    server.refused("get_action", json!({"id_or_name": "x"}), "no action");
    server.refused("create_action", json!({"action": {"name": "walk"}}), "already an action");
}

#[test]
fn people_tools_run_the_store() {
    let server = Server::new();

    let sam = server.ok("create_person", json!({"person": {"name": "Sam", "context": "salsa"}}))["created_id"].clone();
    server.ok("update_person", json!({"person": {"id": sam}, "clear_fields": ["context"]}));
    let family = server.ok("create_circle", json!({"circle": {"name": "Family", "note": "n"}}))["created_id"].clone();
    server.ok("update_circle", json!({"circle": {"id": family}, "clear_fields": ["note"]}));

    assert_eq!(server.ok("get_person", json!({"id_or_name": "Sam"}))["context"], Value::Null);
    assert_eq!(server.ok("get_people", json!({"statuses": ["active"]})).as_array().unwrap().len(), 2);
    assert_eq!(server.ok("get_circle", json!({"id_or_name": "Family"}))["note"], Value::Null);
    assert_eq!(server.ok("get_circles", json!({})).as_array().unwrap().len(), 1);
    assert_eq!(server.ok("delete_circle", json!({"circle_id": family}))["deleted"]["name"], "Family");
    server.refused("create_person", json!({"person": {"name": "sam"}}), "already a person");
}

#[test]
fn each_person_comes_with_the_events_the_user_cancelled_that_count_against_them() {
    let server = Server::new();
    server.t.app.people().unwrap();
    server.t.seed(PEOPLE.1, &[json!({"id": "sam", "name": "Sam", "status": "active"})]);
    server.seed(&[Event { summary: Some("Lunch".into()), facts: Some(crate::testing::facts(json!({"with": ["sam"]}))), ..focus("lunch1", at(12, 0), at(13, 0)) }]);
    server.ok("delete_event", json!({"cancels": [{"event_id": "lunch1", "counts_against_follow_through": true}]}));

    let listed = server.ok("get_people", json!({}));
    let one = server.ok("get_person", json!({"id_or_name": "Sam"}));

    let cancelled = |person: &Value| -> Vec<Value> { person["cancelled_events"].as_array().unwrap().iter().map(|c| c["event_id"].clone()).collect() };
    let sam = listed.as_array().unwrap().iter().find(|p| p["id"] == "sam").unwrap();
    assert_eq!(cancelled(sam), vec![json!("lunch1")]);
    assert_eq!(cancelled(&one), vec![json!("lunch1")]);
    assert_eq!((one["cancelled_events"][0]["summary"].clone(), one["cancelled_events"][0]["source"].clone()), (json!("Lunch"), json!("delete_event")));
}

#[test]
fn location_tools_run_the_store() {
    let server = Server::new();

    let home = server.ok("create_location", json!({"location": {"name": "Home", "hint": "h"}}))["created_id"].clone();
    server.ok("update_location", json!({"location": {"id": home}, "clear_fields": ["hint"]}));

    assert_eq!(server.ok("get_location", json!({"id_or_name": "Home"}))["hint"], Value::Null);
    assert_eq!(server.ok("get_locations", json!({})).as_array().unwrap().len(), 1);
    assert_eq!(server.ok("delete_location", json!({"location_id": home}))["name"], "Home");
    server.refused("get_location", json!({"id_or_name": "x"}), "no location");
}

// -- schedule hints and the time zone -----------------------------------------------------------

#[test]
fn schedule_hints_come_with_the_calendars_time_zone() {
    let server = Server::new();
    server.ok("set_time_zone", json!({"time_zone": "America/New_York"}));

    let saved = server.ok("set_compaction_schedule_hints", json!({"hints": [{"time": "7:30", "label": "Morning compaction"}]}));
    let got = server.ok("get_compaction_schedule_hints", json!({}));

    assert_eq!(saved, got);
    assert_eq!(got["time_zone"], "America/New_York");
    assert_eq!((got["hints"][0]["time"].clone(), got["hints"][0]["label"].clone()), (json!("07:30"), json!("Morning compaction")));
    server.refused("set_compaction_schedule_hints", json!({"hints": [{"time": "7"}]}), "isn't a time of day");
}

#[test]
fn schedule_hints_without_a_time_zone_say_so() {
    let server = Server { t: crate::testing::app::test_app_in(None) };

    assert_eq!(server.ok("get_compaction_schedule_hints", json!({}))["time_zone"], Value::Null);
}

#[test]
fn sets_the_calendars_time_zone_or_says_why_not() {
    let server = Server::new();

    assert_eq!(server.ok("set_time_zone", json!({"time_zone": "America/New_York"})), json!("America/New_York"));
    server.refused("set_time_zone", json!({"time_zone": "Nowhere"}), "isn't a time zone");
}

#[test]
fn any_tool_without_a_time_zone_asks_for_it_to_be_set_then_retried() {
    let server = Server { t: crate::testing::app::test_app_in(None) };
    // A series with no zone of its own: splitting it reads the calendar's.
    server.seed(&[Event { time_zone: None, ..standup("RRULE:FREQ=WEEKLY;BYDAY=MO") }]);

    let text = server.refused("split_recurrence", json!({"event_id": "series1_20261012T130000Z"}), "Call set_time_zone .* then call split_recurrence again");
    assert!(text.contains("IANA"));
}

// -- recurrences -----------------------------------------------------------------------------------

fn standup(rule: &str) -> Event {
    Event {
        summary: Some("Standup".into()),
        time_zone: Some("America/New_York".into()),
        recurrence: Some(strings(&[rule])),
        ..focus("series1", chrono::DateTime::parse_from_rfc3339("2026-10-05T13:00:00+00:00").unwrap(), chrono::DateTime::parse_from_rfc3339("2026-10-05T14:00:00+00:00").unwrap())
    }
}

#[test]
fn shows_a_series_in_its_own_time_zone_with_its_schedule_in_words() {
    let server = Server::new();
    server.t.app.actions().unwrap();
    server.t.seed(ACTIONS.1, &[json!({"id": "g1", "name": "Work", "status": "active", "label_id": "l1"})]);
    server.seed(&[Event { action_ids: Some(strings(&["g1"])), facts: Some(crate::testing::facts(json!({"with": ["sam"], "location": "home"}))), ..standup("RRULE:FREQ=WEEKLY;BYDAY=MO") }]);

    let public = server.ok("get_recurrence", json!({"id": "series1"}));

    assert_eq!(public["start"], "2026-10-05T09:00:00-04:00");
    assert_eq!(public["schedule"], "Every week on Mon");
    assert_eq!(public["action_names"], json!(["Work"]));
    assert_eq!((public["repeat"]["every"].clone(), public["repeat"]["weekdays"].clone()), (json!("week"), json!(["mon"])));
    assert_eq!((public["facts"]["with_ids"].clone(), public["facts"]["location_id"].clone()), (json!(["sam"]), json!("home")));
}

#[test]
fn a_series_repeating_in_ways_repeat_cant_say_shows_its_rules_in_schedule() {
    let server = Server::new();
    let rule = "RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1";
    server.seed(&[standup(rule)]);

    let public = server.ok("get_recurrence", json!({"id": "series1"}));

    assert!(public["repeat"].is_null());
    assert_eq!(public["schedule"], rule);
}

#[test]
fn update_recurrence_writes_the_repeat_and_clears_what_it_names() {
    let server = Server::new();
    server.seed(&[Event { priority: Some(1), ..standup("RRULE:FREQ=WEEKLY;BYDAY=MO") }]);

    let updated = server.ok("update_recurrence", json!({"recurrence": {"id": "series1", "repeat": {"every": "day", "count": 5}}, "clear_fields": ["priority"]}));

    assert_eq!(updated[0]["schedule"], "Every day, 5 times");
    assert_eq!(server.event("series1").priority, None);
}

#[test]
fn update_recurrence_refuses_a_bad_repeat_facts_or_clear_before_writing() {
    let server = Server::new();
    server.seed(&[standup("RRULE:FREQ=WEEKLY;BYDAY=MO")]);

    server.refused("update_recurrence", json!({"recurrence": {"id": "series1", "repeat": {"every": "day", "count": 2, "until": "2026-12-31"}}}), "count or until");
    server.refused("update_recurrence", json!({"recurrence": {"id": "series1", "facts": {"for_ids": ["p9"]}}, "starting_at_event_id": "series1_20261012T130000Z"}), r"Its facts name \['p9'\], who aren't people");
    server.refused("update_recurrence", json!({"recurrence": {"id": "series1", "priority": 1}, "starting_at_event_id": "series1_20261012T130000Z", "clear_fields": ["priority"]}), "Can't both set and clear");

    assert_eq!(server.event_writes(), 0);
}

#[test]
fn update_recurrence_sets_facts_normalized() {
    let server = Server::new();
    server.t.app.people().unwrap();
    server.t.app.locations().unwrap();
    server.t.seed(PEOPLE.1, &[json!({"id": "sam", "name": "Sam", "status": "active"})]);
    server.t.seed(crate::sheet::LOCATIONS.1, &[json!({"id": "home", "name": "Home"})]);
    server.seed(&[standup("RRULE:FREQ=WEEKLY;BYDAY=MO")]);

    server.ok("update_recurrence", json!({"recurrence": {"id": "series1", "facts": {"with_ids": [" sam"], "location_id": "home"}}}));

    let facts = server.event("series1").facts.unwrap();
    assert_eq!((facts.with(), facts.location()), (strings(&["sam"]), Some("home".to_string())));
}

#[test]
fn an_until_reads_as_a_date_or_a_datetime() {
    let server = Server::new();
    server.seed(&[standup("RRULE:FREQ=WEEKLY;BYDAY=MO")]);

    // A date: through the end of that day in the series' zone.
    server.ok("update_recurrence", json!({"recurrence": {"id": "series1", "repeat": {"every": "week", "until": "2026-12-31"}}}));
    assert_eq!(server.event("series1").recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;UNTIL=20270101T045959Z"])));

    let timed = server.ok("update_recurrence", json!({"recurrence": {"id": "series1", "repeat": {"every": "week", "until": "2026-12-31T00:00:00-05:00"}}}));
    assert_eq!(server.event("series1").recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;UNTIL=20261231T050000Z"])));
    assert!(timed[0]["repeat"]["until"].as_str().unwrap().starts_with("2026-12-31T00:00:00"));
}

#[test]
fn delete_recurrence_returns_what_is_left_of_the_series() {
    let server = Server::new();
    server.seed(&[standup("RRULE:FREQ=WEEKLY;BYDAY=MO"), Event { id: Some("other".into()), ..standup("RRULE:FREQ=WEEKLY;BYDAY=MO") }]);

    let left = server.ok("delete_recurrence", json!({"id": "series1", "starting_at_event_id": "series1_20261019T130000Z"}));
    assert_eq!(left[0]["schedule"], "Every week on Mon, until Oct 19, 2026");

    server.refused("delete_recurrence", json!({"id": "series1", "starting_at_event_id": "other_20261012T130000Z"}), "isn't one of series series1's events");
    assert_eq!(server.ok("delete_recurrence", json!({"id": "series1"})), json!([]));
}

// -- the write lock --------------------------------------------------------------------------------

const READ_ONLY_TOOLS: [&str; 24] = [
    "list_events", "get_event", "get_recurrence", "get_compaction_status", "get_proposal", "get_notes", "get_traits", "get_actions",
    "get_action", "get_action_groups", "get_action_group", "get_priority_colors", "get_people", "get_person", "get_circles",
    "get_circle", "get_locations", "get_location", "get_habits", "get_habit", "get_compaction_schedule_hints", "prepare_judgments",
    "prepare_habit_judgments", "get_health",
];

#[test]
fn every_tool_but_the_read_only_ones_holds_the_write_lock() {
    // A new tool has to be put on one side or the other: a read-only one
    // that reaches a write fails, so only list one here that never writes.
    let mut read_only: Vec<&str> = crate::tools::listed().into_iter().filter(|(_, writes)| !writes).map(|(name, _)| name).collect();
    read_only.sort();
    let mut expected = READ_ONLY_TOOLS.to_vec();
    expected.sort();
    assert_eq!(read_only, expected);
    // And every tool the contract lists is registered.
    let contract = crate::mcp::CONTRACT.iter().filter_map(|t| t["name"].as_str());
    for name in contract {
        assert!(crate::tools::listed().iter().any(|(n, _)| *n == name), "{name}");
    }
}

#[test]
fn a_read_only_tool_runs_while_another_thread_holds_the_lock() {
    // The notes tab built first: building it takes the lock.
    let server = Server::new();
    server.ok("get_notes", json!({}));
    let app = server.t.app.clone();
    let _held = crate::write_lock::WRITE_LOCK.hold();

    let done = std::thread::spawn(move || crate::tools::run(&app, "get_notes", json!({})).is_ok());

    assert!(done.join().unwrap());
}
