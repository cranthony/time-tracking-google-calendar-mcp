//! Writes made once by their write_id, and refused when what they change
//! has changed since the version the client last saw.

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
fn a_write_sent_again_is_made_once_and_answered_as_it_was() {
    let s = Server::new();
    let meta = json!({"write_id": "w-1"});

    let first = s.raw("create_location", json!({"location": {"name": "Home", "hint": "the flat"}}), meta.clone());
    let writes = s.sheet_writes();
    let again = s.raw("create_location", json!({"location": {"name": "Home", "hint": "the flat"}}), meta);

    assert_eq!(first["isError"], json!(false));
    assert_eq!(again["structuredContent"], first["structuredContent"]);
    assert_eq!(again["_meta"], json!({"replayed": true}));
    assert!(first.get("_meta").is_none());
    assert_eq!(s.sheet_writes(), writes, "made again");
    assert_eq!(s.ok("get_locations", json!({})).as_array().unwrap().len(), 1);
}

#[test]
fn a_write_that_failed_is_made_when_sent_again() {
    let s = Server::new();
    s.ok("create_location", json!({"location": {"name": "Home"}}));
    let meta = json!({"write_id": "w-2"});

    // A name already used: refused, and not kept as made.
    let refused = s.raw("create_location", json!({"location": {"name": "Home"}}), meta.clone());
    let made = s.raw("create_location", json!({"location": {"name": "Work"}}), meta);

    assert_eq!(refused["isError"], json!(true));
    assert_eq!(made["isError"], json!(false));
    assert!(made.get("_meta").is_none());
}

#[test]
fn a_write_id_is_one_tools() {
    let s = Server::new();
    let meta = json!({"write_id": "w-3"});
    s.raw("create_location", json!({"location": {"name": "Home"}}), meta.clone());

    let other = s.raw("create_location_group", json!({"group": {"name": "Brooklyn"}}), meta);

    assert_eq!(other["isError"], json!(true));
    assert!(other["content"][0]["text"].as_str().unwrap().contains("was used for create_location already"));
}

#[test]
fn a_write_made_once_may_name_what_it_makes() {
    let s = Server::new();

    let chosen = s.raw("create_location", json!({"location": {"id": "loc-abc123", "name": "Home"}}), json!({"write_id": "w-4"}));
    let assigned = s.ok("create_location", json!({"location": {"id": "loc-xyz", "name": "Work"}}));

    assert_eq!(chosen["structuredContent"]["created_id"], json!("loc-abc123"));
    // Without a write_id, as always: assigned.
    assert_ne!(assigned["created_id"], json!("loc-xyz"));
    let taken = s.raw("create_location", json!({"location": {"id": "loc-abc123", "name": "Gym"}}), json!({"write_id": "w-5"}));
    assert!(taken["content"][0]["text"].as_str().unwrap().contains("already used"));
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
