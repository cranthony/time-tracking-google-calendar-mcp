//! How many requests each tool makes against each of Google's quotas.
//! Sheets throttles reads and writes to 60 a minute each per user, and a
//! call that runs out waits for the quota to roll over -- so every request
//! counts. Every tool here reads the spreadsheet in one request: a tool
//! that reads more than one tab prefetches them together. Calendar's quota
//! is looser, but each request is still a round trip. If a count goes
//! up, find a way not to (for a Sheets read, usually by prefetching
//! whatever tab the tool newly reads); compaction's tools are pinned in
//! the compactor's tests.

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

#[test]
fn the_requests_each_tool_makes() {
    let f = filled();
    let note_id = f.s.ok("get_notes", json!({}))[0]["id"].clone();
    // [Sheets reads, Sheets writes, Calendar reads, Calendar writes]
    let calls: Vec<(&str, &str, Value, [usize; 4])> = vec![
        // Actions and action groups, prefetched together.
        ("get_actions", "get_actions", json!({}), [1, 0, 1, 0]),
        ("get_action", "get_action", json!({"id_or_name": "walk"}), [1, 0, 1, 0]),
        ("create_action", "create_action", json!({"action": {"name": "Run"}}), [1, 1, 1, 1]),
        ("update_action", "update_action", json!({"action": {"id": f.walk, "note": "outside"}}), [1, 1, 1, 0]),
        ("get_action_groups", "get_action_groups", json!({}), [1, 0, 1, 0]),
        ("get_action_group", "get_action_group", json!({"id_or_name": "outdoors"}), [1, 0, 1, 0]),
        ("create_action_group", "create_action_group", json!({"group": {"name": "Indoors"}}), [1, 1, 1, 0]),
        ("update_action_group", "update_action_group", json!({"group": {"id": f.outdoors, "priority": 1}}), [1, 1, 1, 1]),
        // People and circles, prefetched together.
        ("get_people", "get_people", json!({}), [1, 0, 0, 0]),
        ("get_person", "get_person", json!({"id_or_name": "sam"}), [1, 0, 0, 0]),
        ("create_person", "create_person", json!({"person": {"name": "Alex"}}), [1, 1, 0, 0]),
        ("update_person", "update_person", json!({"person": {"id": f.sam, "what_matters": "tea"}}), [1, 1, 0, 0]),
        ("get_circles", "get_circles", json!({}), [1, 0, 0, 0]),
        ("get_circle", "get_circle", json!({"id_or_name": "family"}), [1, 0, 0, 0]),
        ("create_circle", "create_circle", json!({"circle": {"name": "Friends"}}), [1, 1, 0, 0]),
        ("update_circle", "update_circle", json!({"circle": {"id": f.family, "note": "n"}}), [1, 1, 0, 0]),
        // Locations and their groups, prefetched together.
        ("get_locations", "get_locations", json!({}), [1, 0, 0, 0]),
        ("get_location", "get_location", json!({"id_or_name": "home"}), [1, 0, 0, 0]),
        ("create_location", "create_location", json!({"location": {"name": "Studio"}}), [1, 1, 0, 0]),
        ("update_location", "update_location", json!({"location": {"id": f.home, "hint": "the apartment"}}), [1, 1, 0, 0]),
        ("get_location_groups", "get_location_groups", json!({}), [1, 0, 0, 0]),
        ("get_location_group", "get_location_group", json!({"id_or_name": "brooklyn"}), [1, 0, 0, 0]),
        ("create_location_group", "create_location_group", json!({"group": {"name": "New York"}}), [1, 1, 0, 0]),
        ("update_location_group", "update_location_group", json!({"group": {"id": f.brooklyn, "note": "n"}}), [1, 1, 0, 0]),
        // Travel times: those, with the actions' tabs for their modes.
        ("create_travel_time", "create_travel_time", json!({"travel_time": {"from_id": f.home, "to_id": f.brooklyn, "times": [{"mode_id": f.walk, "minutes": 10}]}}), [1, 1, 0, 0]),
        ("get_travel_times", "get_travel_times", json!({}), [1, 0, 0, 0]),
        ("estimate_travel_time", "estimate_travel_time", json!({"from_id": f.home, "to_id": f.brooklyn, "mode_id": f.walk}), [1, 0, 0, 0]),
        // Habits, with the actions and traits they're checked against.
        ("get_habits", "get_habits", json!({}), [1, 0, 0, 0]),
        ("get_habit", "get_habit", json!({"id_or_name": "outside"}), [1, 0, 0, 0]),
        ("create_habit", "create_habit", json!({"habit": {"name": "Walks", "action_id": f.walk}}), [1, 1, 0, 0]),
        ("update_habit", "update_habit", json!({"habit": {"id": f.outside, "traits": {"select": "all"}}}), [1, 1, 0, 0]),
        // Events: the actions', people's and locations' tabs together.
        ("list_events", "list_events", json!({"min_time": "2026-01-01T00:00:00+00:00", "max_time": "2026-01-02T00:00:00+00:00"}), [1, 0, 1, 0]),
        ("get_event", "get_event", json!({"id": "walked"}), [1, 0, 1, 0]),
        ("update_event (facts)", "update_event", json!({"updates": [{"event": {"id": "walked", "facts": {"location_ids": [f.home], "notes": {"self": "nice"}}}}]}), [1, 0, 1, 1]),
        ("update_event", "update_event", json!({"updates": [{"event": {"id": "evening", "start": "2026-01-01T20:15:00Z"}}]}), [1, 0, 2, 1]),
        ("create_event", "create_event", json!({"events": [{"summary": "Stroll", "start": "2026-01-04T10:00:00Z", "end": "2026-01-04T11:00:00Z"}]}), [1, 0, 2, 1]),
        // The priority labels are made sure of once, not every call.
        ("create_event (again)", "create_event", json!({"events": [{"summary": "Stroll", "start": "2026-01-05T10:00:00Z", "end": "2026-01-05T11:00:00Z"}]}), [1, 0, 1, 1]),
        ("delete_event", "delete_event", json!({"cancels": [{"event_id": "walked", "counts_against_follow_through": false}]}), [1, 0, 1, 1]),
        // Notes, and the journal (the last compaction; any open one).
        ("get_compaction_status", "get_compaction_status", json!({}), [1, 0, 0, 0]),
        ("edit_note", "edit_note", json!({"note_id": note_id, "description": "walked far"}), [1, 1, 0, 0]),
        ("delete_note", "delete_note", json!({"note_id": note_id}), [1, 1, 0, 0]),
        // Notes only.
        ("get_notes", "get_notes", json!({}), [1, 0, 0, 0]),
        ("note", "note", json!({"noted_time": {"timestamp": "2026-01-01T12:00:00+00:00", "description": "washed up"}}), [1, 1, 0, 0]),
        // Deleting tabs' rows: last, since they remove what others use.
        ("delete_circle", "delete_circle", json!({"circle_id": f.family}), [1, 1, 0, 0]),
        ("delete_location", "delete_location", json!({"location_id": f.home}), [1, 1, 0, 0]),
        ("delete_location_group", "delete_location_group", json!({"group_id": f.brooklyn}), [1, 1, 0, 0]),
    ];

    let mut made = Vec::new();
    let mut expected = Vec::new();
    for (label, tool, arguments, requests) in calls {
        // The versions it changes, read first, as a client does.
        let arguments = f.s.seen(tool, arguments);
        let before = f.s.t.fake.quota_counts();
        f.s.as_is(tool, arguments).unwrap_or_else(|e| panic!("{tool}: {e}"));
        let after = f.s.t.fake.quota_counts();
        made.push((label, [0, 1, 2, 3].map(|i| after[i] - before[i])));
        expected.push((label, requests));
    }

    assert_eq!(made, expected);
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


