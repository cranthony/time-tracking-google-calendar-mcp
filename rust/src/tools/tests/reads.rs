//! How many Google Sheets read requests each tool makes. Sheets throttles
//! reads to 60 a minute per user, and a call that runs out waits for the
//! quota to roll over -- so every read counts. Every tool here reads the
//! spreadsheet in one request: a tool that reads more than one tab
//! prefetches them together. If a count goes up, find a way not to (usually
//! by prefetching whatever tab the tool newly reads); compaction's tools
//! are pinned in the compactor's tests.

use super::*;
use crate::testing::{event_at, facts};

struct Filled {
    s: Server,
    walk: String,
    outdoors: String,
    sam: String,
    family: String,
    home: String,
    brooklyn: String,
    outside: String,
}

fn id(created: Value) -> String {
    created["created_id"].as_str().unwrap().to_string()
}

/// An action in a group, a person in a circle, a location in a group, a habit, an
/// event with an action and facts, and a note, so every tab the tools read
/// has something in it.
fn filled() -> Filled {
    let s = Server::new();
    let outdoors = id(s.ok("create_action_group", json!({"group": {"name": "Outdoors"}})));
    let walk = id(s.ok("create_action", json!({"action": {"name": "Walk", "group_id": outdoors, "status": "active"}})));
    let family = id(s.ok("create_circle", json!({"circle": {"name": "Family"}})));
    let sam = id(s.ok("create_person", json!({"person": {"name": "Sam", "circles": [family]}})));
    let brooklyn = id(s.ok("create_location_group", json!({"group": {"name": "Brooklyn"}})));
    let home = id(s.ok("create_location", json!({"location": {"name": "Home", "group_id": brooklyn}})));
    let outside = id(s.ok("create_habit", json!({"habit": {"name": "Outside", "action_id": outdoors}})));
    s.seed(&[
        event_at("08:00-09:00").id("walked").summary("Walk").actions(&[&walk]).facts(facts(json!({"locations": [home], "with": [sam]}))).build(),
        event_at("20:00-22:00").id("evening").summary("Evening").priority(2).build(),
        event_at("22:00-06:00+1").id("sleep").summary("Sleep").priority(0).sleep().build(),
    ]);
    s.ok("note", json!({"noted_time": {"timestamp": "2026-01-01T09:30:00+00:00", "description": "walked"}}));
    // Finding a tab is a one-time read, whichever tool's first.
    s.ok("get_compaction_status", json!({}));
    Filled { s, walk, outdoors, sam, family, home, brooklyn, outside }
}

impl Filled {
    fn reads(&self) -> usize {
        self.s.t.fake.requests().iter().filter(|r| r.path.ends_with("values:batchGetByDataFilter")).count()
    }
}

#[test]
fn every_tool_reads_the_spreadsheet_in_one_request() {
    let f = filled();
    let note_id = f.s.ok("get_notes", json!({}))[0]["id"].clone();
    let calls: Vec<(&str, &str, Value)> = vec![
        // Actions and action groups, prefetched together.
        ("get_actions", "get_actions", json!({})),
        ("get_action", "get_action", json!({"id_or_name": "walk"})),
        ("create_action", "create_action", json!({"action": {"name": "Run"}})),
        ("update_action", "update_action", json!({"action": {"id": f.walk, "note": "outside"}})),
        ("get_action_groups", "get_action_groups", json!({})),
        ("get_action_group", "get_action_group", json!({"id_or_name": "outdoors"})),
        ("create_action_group", "create_action_group", json!({"group": {"name": "Indoors"}})),
        ("update_action_group", "update_action_group", json!({"group": {"id": f.outdoors, "priority": 1}})),
        // People and circles, prefetched together.
        ("get_people", "get_people", json!({})),
        ("get_person", "get_person", json!({"id_or_name": "sam"})),
        ("create_person", "create_person", json!({"person": {"name": "Alex"}})),
        ("update_person", "update_person", json!({"person": {"id": f.sam, "what_matters": "tea"}})),
        ("get_circles", "get_circles", json!({})),
        ("get_circle", "get_circle", json!({"id_or_name": "family"})),
        ("create_circle", "create_circle", json!({"circle": {"name": "Friends"}})),
        ("update_circle", "update_circle", json!({"circle": {"id": f.family, "note": "n"}})),
        // Locations and their groups, prefetched together.
        ("get_locations", "get_locations", json!({})),
        ("get_location", "get_location", json!({"id_or_name": "home"})),
        ("create_location", "create_location", json!({"location": {"name": "Studio"}})),
        ("update_location", "update_location", json!({"location": {"id": f.home, "hint": "the apartment"}})),
        ("get_location_groups", "get_location_groups", json!({})),
        ("get_location_group", "get_location_group", json!({"id_or_name": "brooklyn"})),
        ("create_location_group", "create_location_group", json!({"group": {"name": "New York"}})),
        ("update_location_group", "update_location_group", json!({"group": {"id": f.brooklyn, "note": "n"}})),
        // Travel times: those, with the actions' tabs for their modes.
        ("create_travel_time", "create_travel_time", json!({"travel_time": {"id": "x", "from_id": f.home, "to_id": f.brooklyn, "times": [{"mode_id": f.walk, "minutes": 10}]}})),
        ("get_travel_times", "get_travel_times", json!({})),
        ("estimate_travel_time", "estimate_travel_time", json!({"from_id": f.home, "to_id": f.brooklyn, "mode_id": f.walk})),
        // Habits, with the actions and traits they're checked against.
        ("get_habits", "get_habits", json!({})),
        ("get_habit", "get_habit", json!({"id_or_name": "outside"})),
        ("create_habit", "create_habit", json!({"habit": {"name": "Walks", "action_id": f.walk}})),
        ("update_habit", "update_habit", json!({"habit": {"id": f.outside, "traits": {"select": "all"}}})),
        // Events: the actions', people's and locations' tabs together.
        ("list_events", "list_events", json!({"min_time": "2026-01-01T00:00:00+00:00", "max_time": "2026-01-02T00:00:00+00:00"})),
        ("get_event", "get_event", json!({"id": "walked"})),
        ("update_event (facts)", "update_event", json!({"updates": [{"event": {"id": "walked", "facts": {"location_ids": [f.home], "notes": {"self": "nice"}}}}]})),
        ("update_event", "update_event", json!({"updates": [{"event": {"id": "evening", "start": "2026-01-01T20:15:00Z"}}]})),
        ("create_event", "create_event", json!({"events": [{"summary": "Stroll", "start": "2026-01-04T10:00:00Z", "end": "2026-01-04T11:00:00Z"}]})),
        ("delete_event", "delete_event", json!({"cancels": [{"event_id": "walked", "counts_against_follow_through": false}]})),
        // Notes, and the journal (the last compaction; any open one).
        ("get_compaction_status", "get_compaction_status", json!({})),
        ("edit_note", "edit_note", json!({"note_id": note_id, "description": "walked far"})),
        ("delete_note", "delete_note", json!({"note_id": note_id})),
        // Notes only.
        ("get_notes", "get_notes", json!({})),
        ("note", "note", json!({"noted_time": {"timestamp": "2026-01-01T12:00:00+00:00", "description": "washed up"}})),
        // Deleting tabs' rows: last, since they remove what others use.
        ("delete_circle", "delete_circle", json!({"circle_id": f.family})),
        ("delete_location", "delete_location", json!({"location_id": f.home})),
        ("delete_location_group", "delete_location_group", json!({"group_id": f.brooklyn})),
    ];

    let mut counts = Vec::new();
    for (label, tool, arguments) in calls {
        let before = f.reads();
        f.s.ok(tool, arguments);
        counts.push((label, f.reads() - before));
    }

    let ones: Vec<_> = counts.iter().map(|(label, _)| (*label, 1)).collect();
    assert_eq!(counts, ones);
}

#[test]
fn an_event_tool_reads_every_tab_it_needs_in_one_request() {
    let f = filled();
    f.s.t.fake.clear_requests();

    f.s.ok("list_events", json!({"min_time": "2026-01-01T00:00:00+00:00", "max_time": "2026-01-02T00:00:00+00:00"}));

    let reads: Vec<_> = f.s.t.fake.requests().into_iter().filter(|r| r.path.ends_with("values:batchGetByDataFilter")).collect();
    assert_eq!(reads.len(), 1);
    let sheet = f.s.t.spreadsheet();
    let tabs = f.s.t.fake.tab_ids(&sheet);
    let mut read: Vec<String> = reads[0].body["dataFilters"]
        .as_array()
        .unwrap()
        .iter()
        .map(|filter| {
            let tab = filter.pointer("/gridRange/sheetId").and_then(Value::as_i64).unwrap();
            tabs.iter().find(|(id, _)| *id == tab).unwrap().1.clone()
        })
        .collect();
    read.sort();
    assert_eq!(read, strings(&["Action Groups", "Actions", "Circles", "Location Groups", "Locations", "People"]));
}
