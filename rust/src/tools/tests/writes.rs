//! Writes made once by what they name and the version they were made
//! to, and refused when what they change has changed since the version
//! the client last saw (see rows::Versioned).

use super::Server;
use serde_json::{json, Value};

impl Server {
    /// The whole tools/call result for `name`, sent with `meta`.
    fn raw(&self, name: &str, arguments: Value, meta: Value) -> Value {
        let mut timings = Vec::new();
        crate::mcp::call_tool(&self.t.app, &json!({"name": name, "arguments": arguments, "_meta": meta}), &mut timings)
    }

    /// Sheet writes made so far.
    fn sheet_writes(&self) -> usize {
        self.t.fake.requests().iter().filter(|r| r.method == "POST" && r.path.contains("values:batchUpdate")).count()
    }
}

#[test]
fn a_create_sent_again_gives_the_one_it_made() {
    let s = Server::new();
    let first = s.ok("create_location", json!({"location": {"id": "loc-abc123", "name": "Home", "hint": "the flat"}}));
    let writes = s.sheet_writes();

    let again = s.ok("create_location", json!({"location": {"id": "loc-abc123", "name": "Home", "hint": "the flat"}}));

    assert_eq!(first["created_id"], json!("loc-abc123"));
    assert_eq!(again, first);
    assert_eq!(s.sheet_writes(), writes, "made again");
    assert_eq!(s.ok("get_locations", json!({})).as_array().unwrap().len(), 1);
}

#[test]
fn a_create_naming_an_id_used_for_something_else_is_refused() {
    let s = Server::new();
    s.ok("create_location", json!({"location": {"id": "loc-abc123", "name": "Home"}}));

    let taken = s.raw("create_location", json!({"location": {"id": "loc-abc123", "name": "Gym"}}), json!({}));
    // Not a location's, but a group's: either can be named by it.
    let group = s.raw("create_location_group", json!({"group": {"id": "loc-abc123", "name": "Brooklyn"}}), json!({}));

    assert!(taken["content"][0]["text"].as_str().unwrap().contains("already used"));
    assert!(group["content"][0]["text"].as_str().unwrap().contains("already used"));
    // One not well formed is refused, not replaced: a create sent again
    // would make another.
    let bad = s.raw("create_location", json!({"location": {"id": "Not mine!", "name": "Gym"}}), json!({}));
    assert!(bad["content"][0]["text"].as_str().unwrap().contains("isn't"), "{bad}");
    let bad = s.raw("create_event", json!({"events": [{"id": "walk-1", "summary": "Walk", "start": "2026-03-02T11:00:00+00:00", "end": "2026-03-02T11:30:00+00:00"}]}), json!({}));
    assert!(bad["content"][0]["text"].as_str().unwrap().contains("isn't"), "{bad}");
}

#[test]
fn an_update_sent_again_is_given_as_it_is() {
    let s = Server::new();
    let id = s.ok("create_location", json!({"location": {"name": "Home"}}))["created_id"].clone();
    let first = s.ok("update_location", json!({"location": {"id": id, "hint": "upstairs", "version": 1}}));

    // Its answer lost: sent again from the version it was made to.
    let again = s.ok("update_location", json!({"location": {"id": id, "hint": "upstairs", "version": 1}}));

    assert_eq!((first["version"].clone(), again["version"].clone()), (json!(2), json!(2)));
    assert_eq!(again["hint"], json!("upstairs"));
    // Not when it's changed since in some other way.
    s.ok("update_location", json!({"location": {"id": id, "name": "Flat", "version": 2}}));
    let stale = s.raw("update_location", json!({"location": {"id": id, "hint": "upstairs", "version": 1}}), json!({}));
    assert_eq!(stale["_meta"]["conflict"]["current"]["version"], json!(3));
}

#[test]
fn a_deletion_sent_again_is_done() {
    let s = Server::new();
    let id = s.ok("create_circle", json!({"circle": {"name": "Family"}}))["created_id"].clone();
    s.ok("delete_circle", json!({"circle_id": id, "if_version": 1}));

    let again = s.ok("delete_circle", json!({"circle_id": id, "if_version": 1}));

    assert_eq!(again["deleted"]["id"], id);
    // Without a version, what isn't there is refused, as ever.
    let unknown = s.raw("delete_circle", json!({"circle_id": id}), json!({}));
    assert_eq!(unknown["isError"], json!(true));
}

#[test]
fn versions_go_up_as_it_changes() {
    let s = Server::new();
    let created = s.ok("create_location", json!({"location": {"name": "Home"}}));
    let id = created["created_id"].clone();

    let renamed = s.ok("update_location", json!({"location": {"id": id, "name": "Flat"}}));
    let same = s.ok("update_location", json!({"location": {"id": id, "name": "Flat"}}));

    assert_eq!(created["location"]["version"], json!(1));
    assert_eq!(renamed["version"], json!(2));
    // Nothing changed: no new version.
    assert_eq!(same["version"], json!(2));
    assert_eq!(s.ok("get_location", json!({"id_or_name": id}))["version"], json!(2));
}

#[test]
fn an_update_from_an_older_version_is_a_conflict_with_it_as_it_is_now() {
    let s = Server::new();
    let id = s.ok("create_location", json!({"location": {"name": "Home"}}))["created_id"].clone();
    s.ok("update_location", json!({"location": {"id": id, "hint": "upstairs", "version": 1}}));

    let stale = s.raw("update_location", json!({"location": {"id": id, "name": "Flat", "version": 1}}), json!({}));

    assert_eq!(stale["isError"], json!(true));
    let conflict = &stale["_meta"]["conflict"];
    assert_eq!((conflict["id"].clone(), conflict["expected"].clone()), (id.clone(), json!(1)));
    assert_eq!((conflict["current"]["hint"].clone(), conflict["current"]["version"].clone()), (json!("upstairs"), json!(2)));
    assert_eq!(s.ok("get_location", json!({"id_or_name": id}))["name"], json!("Home"));
    // From the version it is now, it's made.
    assert_eq!(s.ok("update_location", json!({"location": {"id": id, "name": "Flat", "version": 2}}))["version"], json!(3));
}

#[test]
fn an_update_to_one_deleted_since_is_a_conflict_with_nothing_as_it_is() {
    let s = Server::new();
    let id = s.ok("create_location", json!({"location": {"name": "Home"}}))["created_id"].clone();
    s.ok("delete_location", json!({"location_id": id}));

    let gone = s.raw("update_location", json!({"location": {"id": id, "name": "Flat", "version": 1}}), json!({}));

    assert_eq!(gone["_meta"]["conflict"]["current"], Value::Null);
    assert!(gone["content"][0]["text"].as_str().unwrap().contains("was deleted"));
}

#[test]
fn a_deletion_from_an_older_version_is_a_conflict() {
    let s = Server::new();
    let id = s.ok("create_circle", json!({"circle": {"name": "Family"}}))["created_id"].clone();
    s.ok("update_circle", json!({"circle": {"id": id, "note": "by blood or choice"}}));

    let stale = s.raw("delete_circle", json!({"circle_id": id, "if_version": 1}), json!({}));

    assert_eq!(stale["_meta"]["conflict"]["current"]["note"], json!("by blood or choice"));
    assert_eq!(s.ok("get_circles", json!({})).as_array().unwrap().len(), 1);
    s.ok("delete_circle", json!({"circle_id": id, "if_version": 2}));
    assert!(s.ok("get_circles", json!({})).as_array().unwrap().is_empty());
}

#[test]
fn every_versioned_kind_starts_at_one() {
    let s = Server::new();
    let made = [
        s.ok("create_person", json!({"person": {"name": "Sam"}}))["person"]["version"].clone(),
        s.ok("create_circle", json!({"circle": {"name": "Family"}}))["circle"]["version"].clone(),
        s.ok("create_location_group", json!({"group": {"name": "Brooklyn"}}))["group"]["version"].clone(),
        s.ok("create_location", json!({"location": {"name": "Home"}}))["location"]["version"].clone(),
        s.ok(
            "create_trait",
            json!({"trait": {"name": "Brave", "parts": [{"kind": "judgment", "rubric": "Was it brave?", "ratings": {"0": "no", "1": "yes"}, "facts": ["action"]}]}}),
        )["version"]
            .clone(),
    ];
    assert!(made.iter().all(|v| *v == json!(1)), "{made:?}");
}

#[test]
fn actions_and_their_groups_have_versions_and_conflicts() {
    let s = Server::new();
    let made = s.raw("create_action", json!({"action": {"id": "act-guitar", "name": "play guitar"}}), json!({}));
    let group = s.raw("create_action_group", json!({"group": {"id": "grp-creative", "name": "Creative"}}), json!({}));
    assert_eq!(made["structuredContent"]["created_id"], json!("act-guitar"));
    assert_eq!(made["structuredContent"]["changed"][0]["version"], json!(1));
    assert_eq!(group["structuredContent"]["created_id"], json!("grp-creative"));
    assert_eq!(group["structuredContent"]["changed"][0]["version"], json!(1));
    // An action's id can't be a group's: a trait's part may name either.
    let taken = s.raw("create_action", json!({"action": {"id": "grp-creative", "name": "draw"}}), json!({}));
    assert!(taken["content"][0]["text"].as_str().unwrap().contains("already used"));

    let moved = s.ok("update_action", json!({"action": {"id": "act-guitar", "group_id": "grp-creative", "version": 1}}));
    assert_eq!(moved["changed"][0]["version"], json!(2));
    let stale = s.raw("update_action", json!({"action": {"id": "act-guitar", "note": "x", "version": 1}}), json!({}));
    assert_eq!(stale["_meta"]["conflict"]["current"]["group_id"], json!("grp-creative"));
    assert_eq!(stale["_meta"]["conflict"]["current"]["version"], json!(2));

    s.ok("update_action_group", json!({"group": {"id": "grp-creative", "note": "making things", "version": 1}}));
    let stale = s.raw("delete_action_group", json!({"group_id": "grp-creative", "if_version": 1}), json!({}));
    assert_eq!(stale["_meta"]["conflict"]["current"]["note"], json!("making things"));
    s.ok("delete_action_group", json!({"group_id": "grp-creative", "if_version": 2}));
    let gone = s.raw("update_action_group", json!({"group": {"id": "grp-creative", "note": "y", "version": 2}}), json!({}));
    assert_eq!(gone["_meta"]["conflict"]["current"], Value::Null);
    // Moved up out of the group it was in: a change, so a new version.
    assert_eq!(s.ok("get_action", json!({"id_or_name": "act-guitar"}))["version"], json!(3));
}

#[test]
fn a_note_made_once_is_named_by_its_own_id_and_versioned() {
    let s = Server::new();
    let note = json!({"noted_time": {"timestamp": "2026-01-01T09:00:00+00:00", "description": "up", "id": "note-1"}});
    let made = s.raw("note", note.clone(), json!({}));
    assert_eq!(made["structuredContent"]["id"], json!("note-1"));
    assert_eq!(made["structuredContent"]["version"], json!(1));
    // Sent again: the one it made.
    assert_eq!(s.ok("note", note), made["structuredContent"]);
    assert_eq!(s.ok("get_notes", json!({})).as_array().unwrap().len(), 1);
    // Without one given, one is made for it.
    let plain = s.ok("note", json!({"noted_time": {"timestamp": "2026-01-01T10:00:00+00:00"}}));
    assert!(!plain["id"].as_str().unwrap().contains('#'), "{plain}");

    // Its id stays when its time changes.
    let moved = s.ok("edit_note", json!({"note_id": "note-1", "timestamp": "2026-01-01T09:05:00+00:00", "version": 1}));
    assert_eq!((moved["id"].clone(), moved["version"].clone()), (json!("note-1"), json!(2)));
    // Sent again: given as it is.
    assert_eq!(s.ok("edit_note", json!({"note_id": "note-1", "timestamp": "2026-01-01T09:05:00+00:00", "version": 1})), moved);
    let stale = s.raw("edit_note", json!({"note_id": "note-1", "description": "awake", "version": 1}), json!({}));
    assert_eq!(stale["isError"], json!(true));
    assert_eq!(stale["_meta"]["conflict"]["current"]["version"], json!(2));
    assert_eq!(stale["_meta"]["conflict"]["current"]["description"], json!("up"));
    let stale = s.raw("delete_note", json!({"note_id": "note-1", "if_version": 1}), json!({}));
    assert_eq!(stale["_meta"]["conflict"]["kind"], json!("note"));

    let deleted = s.ok("delete_note", json!({"note_id": "note-1", "if_version": 2}));
    assert_eq!(deleted["id"], json!("note-1"));
    let gone = s.raw("edit_note", json!({"note_id": "note-1", "description": "awake", "version": 2}), json!({}));
    assert_eq!(gone["_meta"]["conflict"]["current"], Value::Null);
    // Sent again: done.
    assert_eq!(s.ok("delete_note", json!({"note_id": "note-1", "if_version": 2}))["id"], json!("note-1"));
    let gone = s.raw("delete_note", json!({"note_id": "note-1"}), json!({}));
    assert!(gone["content"][0]["text"].as_str().unwrap().contains("there's no note 'note-1'"), "{gone}");
}

#[test]
fn an_event_changed_since_its_etag_was_read_is_a_conflict() {
    let s = Server::new();
    let made = s.ok("create_event", json!({"events": [{"summary": "Walk", "start": "2026-03-02T11:00:00+00:00", "end": "2026-03-02T11:30:00+00:00"}]}));
    let (id, etag) = (made["events"][0]["id"].clone(), made["events"][0]["etag"].clone());
    assert!(etag.is_string(), "{made}");
    let renamed = s.ok("update_event", json!({"updates": [{"event": {"id": id, "summary": "Stroll", "etag": etag}}]}));

    let stale = s.raw("update_event", json!({"updates": [{"event": {"id": id, "summary": "Run", "etag": etag}}]}), json!({}));

    assert_eq!(stale["isError"], json!(true));
    let conflict = &stale["_meta"]["conflict"];
    assert_eq!((conflict["kind"].clone(), conflict["expected"].clone()), (json!("event"), etag.clone()));
    assert_eq!(conflict["current"]["summary"], json!("Stroll"));
    assert_eq!(conflict["current"]["etag"], renamed["events"][0]["etag"]);
    assert!(stale["content"][0]["text"].as_str().unwrap().contains("changed after it was read"));
    let cancelled = s.raw("delete_event", json!({"cancels": [{"event_id": id, "counts_against_follow_through": false, "etag": etag}]}), json!({}));
    assert_eq!(cancelled["_meta"]["conflict"]["current"]["summary"], json!("Stroll"));
}

#[test]
fn an_event_change_sent_again_is_given_as_it_is() {
    let s = Server::new();
    let event = json!({"id": "abcd0000a1", "summary": "Walk", "start": "2026-03-02T11:00:00+00:00", "end": "2026-03-02T11:30:00+00:00"});
    let made = s.ok("create_event", json!({"events": [event]}));
    assert_eq!(made["events"][0]["id"], json!("abcd0000a1"));
    // Its own id: sent again, the one it made.
    let again = s.ok("create_event", json!({"events": [event]}));
    assert_eq!(again["events"][0]["etag"], made["events"][0]["etag"]);
    let listed = s.ok("list_events", json!({"min_time": "2026-03-02T00:00:00+00:00", "max_time": "2026-03-03T00:00:00+00:00"}));
    assert_eq!(listed.as_array().unwrap().len(), 1);
    let etag = made["events"][0]["etag"].clone();

    let renamed = s.ok("update_event", json!({"updates": [{"event": {"id": "abcd0000a1", "summary": "Stroll", "etag": etag}}]}));
    let again = s.ok("update_event", json!({"updates": [{"event": {"id": "abcd0000a1", "summary": "Stroll", "etag": etag}}]}));
    assert_eq!(again["events"][0]["etag"], renamed["events"][0]["etag"]);
    let etag = renamed["events"][0]["etag"].clone();
    s.ok("delete_event", json!({"cancels": [{"event_id": "abcd0000a1", "counts_against_follow_through": false, "etag": etag}]}));
    let again = s.ok("delete_event", json!({"cancels": [{"event_id": "abcd0000a1", "counts_against_follow_through": false, "etag": etag}]}));
    assert_eq!(again["events"][0]["is_cancelled"], json!(true));
    // An id used for something else is refused.
    let taken = s.raw(
        "create_event",
        json!({"events": [{"id": "abcd0000a1", "summary": "Run", "start": "2026-03-02T12:00:00+00:00", "end": "2026-03-02T12:30:00+00:00"}]}),
        json!({}),
    );
    assert!(taken["content"][0]["text"].as_str().unwrap().contains("already used"), "{taken}");
}

// -- versions required ------------------------------------------------------------------

/// [result]'s error text.
fn said(result: &Value) -> String {
    result["content"][0]["text"].as_str().unwrap_or_default().to_string()
}

#[test]
fn a_change_that_doesnt_say_which_version_it_read_is_refused() {
    let s = Server::new();
    let home = s.ok("create_location", json!({"location": {"name": "Home"}}))["created_id"].clone();
    let note = s.ok("note", json!({"noted_time": {"timestamp": "2026-01-01T09:00:00+00:00"}}))["id"].clone();
    s.seed(&[crate::testing::event_at("09:00-10:00").id("walk1").summary("Walk").build()]);

    for (tool, arguments) in [
        ("update_location", json!({"location": {"id": home, "hint": "upstairs"}})),
        ("delete_location", json!({"location_id": home})),
        ("edit_note", json!({"note_id": note, "description": "up"})),
        ("delete_note", json!({"note_id": note})),
        ("update_event", json!({"updates": [{"event": {"id": "walk1", "summary": "Stroll"}}]})),
        ("delete_event", json!({"cancels": [{"event_id": "walk1", "counts_against_follow_through": false}]})),
    ] {
        let refused = s.raw(tool, arguments, json!({}));
        assert_eq!(refused["isError"], json!(true), "{tool}");
        assert!(said(&refused).contains("Read it again") || said(&refused).contains("Read them again"), "{tool}: {}", said(&refused));
    }
    // Nothing changed.
    assert_eq!(s.ok("get_location", json!({"id_or_name": home}))["hint"], Value::Null);
    assert_eq!(s.event("walk1").summary.as_deref(), Some("Walk"));
}

#[test]
fn every_stale_item_of_a_batch_is_said_not_just_the_first() {
    let s = Server::new();
    let made = s.ok(
        "update_goals",
        json!({"creates": [
            {"id": "read-more", "title": "Read more", "deadline": "2026-12-01"},
            {"id": "run-more", "title": "Run more", "deadline": "2026-12-01"},
        ]}),
    );
    assert_eq!(made["goals"].as_array().unwrap().len(), 2);
    // Each changed elsewhere since version 1 was read.
    s.ok("update_goal", json!({"goal": {"id": "read-more", "description": "<p>books</p>", "version": 1}}));
    s.ok("update_goal", json!({"goal": {"id": "run-more", "description": "<p>miles</p>", "version": 1}}));

    let stale = s.raw(
        "update_goals",
        json!({"updates": [
            {"goal": {"id": "read-more", "title": "Read", "version": 1}},
            {"goal": {"id": "run-more", "title": "Run", "version": 1}},
        ]}),
        json!({}),
    );

    let ids: Vec<Value> = stale["_meta"]["conflicts"].as_array().unwrap().iter().map(|c| c["id"].clone()).collect();
    assert_eq!(ids, vec![json!("read-more"), json!("run-more")]);
    // The first, as an older client reads it, too.
    assert_eq!(stale["_meta"]["conflict"]["id"], json!("read-more"));
    assert!(said(&stale).contains("2 of the things it changes"), "{}", said(&stale));
}

#[test]
fn every_stale_event_of_a_batch_is_said() {
    let s = Server::new();
    s.seed(&[
        crate::testing::event_at("09:00-10:00").id("walk1").summary("Walk").build(),
        crate::testing::event_at("11:00-12:00").id("read1").summary("Read").build(),
    ]);
    let etags: Vec<Value> = ["walk1", "read1"].iter().map(|id| json!(s.event(id).etag.0.unwrap())).collect();
    s.ok("update_event", json!({"updates": [{"event": {"id": "walk1", "summary": "Stroll"}}, {"event": {"id": "read1", "summary": "Study"}}]}));

    let stale = s.raw(
        "update_event",
        json!({"updates": [
            {"event": {"id": "walk1", "summary": "Hike", "etag": etags[0]}},
            {"event": {"id": "read1", "summary": "Write", "etag": etags[1]}},
        ]}),
        json!({}),
    );

    let ids: Vec<Value> = stale["_meta"]["conflicts"].as_array().unwrap().iter().map(|c| c["id"].clone()).collect();
    assert_eq!(ids, vec![json!("walk1"), json!("read1")]);
    assert_eq!(stale["_meta"]["conflicts"][1]["current"]["summary"], json!("Study"));
}
