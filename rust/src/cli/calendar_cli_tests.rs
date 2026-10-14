//! calendar_cli's parsing, and each command run on the fake Google.

use super::*;
use crate::sheet::ACTIONS;
use crate::testing::app::{test_app, TestApp};

fn now() -> Moment {
    parse_iso("2026-01-01T12:00:00+00:00", None).unwrap()
}

fn at(text: &str) -> Moment {
    parse_iso(text, None).unwrap()
}

/// What the command printed, or how it failed: ("usage", message),
/// ("exit", message) or ("error", message).
fn cli(t: &TestApp, args: &[&str]) -> Result<String, (&'static str, String)> {
    let args: Vec<String> = args.iter().map(|a| a.to_string()).collect();
    let mut out: Vec<u8> = Vec::new();
    match run_with(&args, &t.app, now(), &mut out) {
        Ok(()) => Ok(String::from_utf8(out).unwrap()),
        Err(Failure::Usage(Usage(message))) => Err(("usage", message)),
        Err(Failure::Exit(message)) => Err(("exit", message)),
        Err(Failure::Error(error)) => Err(("error", format!("{error:#}"))),
    }
}

fn ok(t: &TestApp, args: &[&str]) -> String {
    cli(t, args).unwrap_or_else(|(kind, message)| panic!("{kind}: {message}"))
}

fn failed(t: &TestApp, args: &[&str], kind: &str, text: &str) {
    let Err((got, message)) = cli(t, args) else { panic!("{args:?} succeeded") };
    assert_eq!(got, kind, "{message}");
    assert!(message.contains(text), "{text:?} isn't in: {message}");
}

fn usage_text(result: Result<String, Failure>) -> String {
    match result {
        Err(Failure::Usage(Usage(message))) => message,
        _ => panic!("not a usage error"),
    }
}

fn focus(id: &str, start: &str, end: &str) -> Event {
    Event { id: Some(id.into()), summary: Some("Focus block".into()), start: Some(at(start)), end: Some(at(end)), ..Default::default() }
}

fn writes(t: &TestApp) -> usize {
    t.fake.requests().iter().filter(|r| r.method != "GET" && r.path.contains("/events")).count()
}

// -- parsing ----------------------------------------------------------------------------

#[test]
fn parses_a_duration_or_says_it_cant() {
    assert_eq!(parse_duration("1h"), Some(3600.0));
    assert!(usage_text(duration("not a duration").map(|_| String::new())).contains("could not parse duration"));
}

#[test]
fn parses_booleans() {
    for value in ["true", "True", "1", "yes"] {
        assert_eq!(parse_bool(value), Ok(true), "{value}");
    }
    for value in ["false", "False", "0", "no"] {
        assert_eq!(parse_bool(value), Ok(false), "{value}");
    }
    assert!(parse_bool("maybe").is_err());
}

#[test]
fn parses_iso_datetimes_with_an_offset() {
    assert_eq!(parse_iso_datetime("2026-01-01T09:00:00-05:00").unwrap(), at("2026-01-01T09:00:00-05:00"));
    assert_eq!(parse_iso_datetime("2026-01-01T09:00:00Z").unwrap(), at("2026-01-01T09:00:00+00:00"));
    assert!(parse_iso_datetime("2026-01-01T09:00:00").unwrap_err().contains("must include a UTC offset"));
}

#[test]
fn parses_event_attributes() {
    let mut event = Event::default();

    assert_eq!(set_event_attribute(&mut event, "summary=New title").ok(), Some("summary".to_string()));
    set_event_attribute(&mut event, "priority=1").ok().unwrap();
    set_event_attribute(&mut event, "start=2026-01-01T09:00:00Z").ok().unwrap();

    assert_eq!((event.summary.as_deref(), event.priority, event.start), (Some("New title"), Some(1), Some(at("2026-01-01T09:00:00+00:00"))));
    for (pair, text) in [("priority", "expected key=value"), ("id=new-id", "unknown attribute 'id'"), ("priority=not-a-number", "invalid value for 'priority'")] {
        assert!(usage_text(set_event_attribute(&mut event, pair)).contains(text), "{pair}");
    }
}

#[test]
fn parses_action_attributes() {
    let mut row = ACTION_ROW.empty();

    set_action_attribute(&mut row, "name=Play guitar").ok().unwrap();
    set_action_attribute(&mut row, "status=archived").ok().unwrap();

    assert_eq!((row["name"].clone(), row["status"].clone()), (json!("Play guitar"), json!("archived")));
    let mut err = |pair: &str| usage_text(set_action_attribute(&mut row, pair).map(|_| String::new()));
    assert!(err("status=inactive").contains("expected one of proposed, active"));
    for key in ["id", "label_id"] {
        assert!(err(&format!("{key}=x")).contains("unknown attribute"), "{key}");
    }
}

#[test]
fn parses_raw_label_attributes() {
    assert_eq!(split_pair("background_color=#8e24aa", &RAW_LABEL_ATTRIBUTES).ok(), Some(("background_color", "#8e24aa")));
    assert_eq!(split_pair("name=Design Work", &RAW_LABEL_ATTRIBUTES).ok(), Some(("name", "Design Work")));
    for pair in ["background_color", "id=new-id", "priority=1"] {
        assert!(split_pair(pair, &RAW_LABEL_ATTRIBUTES).is_err(), "{pair}");
    }
}

#[test]
fn the_attributes_cover_every_writable_field() {
    // Not id, recurring_event_id, action_priority, actions_from_label, a
    // series' recurrence or time zone, original_start, or cleared.
    let mut events = EVENT_ATTRIBUTES.to_vec();
    events.sort();
    assert_eq!(events, vec!["action_ids", "description", "end", "event_label_id", "facts", "is_end_of_day_sleep", "judgments", "location", "priority", "start", "status", "summary"]);
    let mut actions: Vec<&str> = ACTION_ROW.fields.iter().map(|(n, _)| *n).filter(|n| !["id", "label_id"].contains(n)).collect();
    actions.sort();
    assert_eq!(actions, ACTION_ATTRIBUTES.to_vec());
    assert_eq!(RAW_LABEL_ATTRIBUTES, ["background_color", "name"]);
}

#[test]
fn formats_an_events_details_with_just_what_is_set() {
    let event = focus("abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00");

    let details = event_details(&event);

    for line in ["id: abc123", "summary: Focus block", "start: 2026-01-01 09:00:00+00:00", "end: 2026-01-01 10:00:00+00:00"] {
        assert!(details.contains(line), "{line}");
    }
    assert!(!details.contains("description") && !details.contains("location") && !details.contains("priority"));
    let fuller = event_details(&Event { description: Some("Details".into()), location: Some("Room".into()), priority: Some(1), ..event });
    for line in ["description: Details", "location: Room", "priority: 1"] {
        assert!(fuller.contains(line), "{line}");
    }
}

// -- events -----------------------------------------------------------------------------

#[test]
fn lists_events_in_the_window_around_now() {
    let t = test_app();
    t.seed_event(&focus("abc123", "2026-01-01T11:00:00+00:00", "2026-01-01T11:30:00+00:00"));
    t.seed_event(&focus("later1", "2026-01-01T15:00:00+00:00", "2026-01-01T16:00:00+00:00"));

    let out = ok(&t, &["list", "1h", "2h"]);

    assert!(out.contains("abc123\t2026-01-01T11:00:00+00:00 - 2026-01-01T11:30:00+00:00\tFocus block"), "{out}");
    assert!(!out.contains("later1"));
    // The default window: an hour either side.
    let listing = t.fake.requests().into_iter().rfind(|r| r.path.ends_with("/events")).unwrap();
    let query = |name: &str| listing.query.iter().find(|(k, _)| k == name).map(|(_, v)| v.clone()).unwrap();
    assert_eq!((query("timeMin"), query("timeMax")), ("2026-01-01T11:00:00+00:00".to_string(), "2026-01-01T14:00:00+00:00".to_string()));
    ok(&t, &["list"]);
    let listing = t.fake.requests().into_iter().rfind(|r| r.path.ends_with("/events")).unwrap();
    assert!(listing.query.contains(&("timeMax".to_string(), "2026-01-01T13:00:00+00:00".to_string())));
}

#[test]
fn says_when_there_are_no_events() {
    assert!(ok(&test_app(), &["list"]).contains("No events found."));
}

#[test]
fn gets_an_event() {
    let t = test_app();
    t.seed_event(&focus("abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"));

    assert!(ok(&t, &["get", "abc123"]).contains("id: abc123"));
}

#[test]
fn update_properties_patches_just_what_it_is_given_without_reading_first() {
    let t = test_app();
    t.seed_event(&focus("abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"));
    t.fake.clear_requests();

    let out = ok(&t, &["update_properties", "abc123", "priority=1", "location=Room"]);

    assert!(out.contains("priority: 1") && out.contains("location: Room"), "{out}");
    let requests = t.fake.requests();
    assert!(requests.iter().all(|r| !(r.method == "GET" && r.path.ends_with("/events/abc123"))));
    let patch = requests.iter().find(|r| r.method == "PATCH").unwrap();
    assert!(patch.body.get("summary").is_none() && patch.body.get("description").is_none());
    assert_eq!(patch.body["location"], "Room");
}

#[test]
fn update_properties_action_ids_also_set_the_label_they_imply() {
    let t = test_app();
    t.app.actions().unwrap();
    t.seed(ACTIONS.1, &[json!({"id": "g1", "name": "Cook", "status": "active", "label_id": "label-1"})]);
    t.seed_event(&focus("abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"));

    ok(&t, &["update_properties", "abc123", "action_ids=g1, g2"]);

    let event = t.app.calendar_client().unwrap().get_event("abc123").unwrap();
    assert_eq!((event.action_ids, event.event_label_id.as_deref()), (Some(vec!["g1".to_string(), "g2".to_string()]), Some("label-1")));
}

#[test]
fn update_moves_the_event_or_keeps_its_end() {
    let t = test_app();
    t.seed_event(&Event { summary: Some("Focus".into()), ..focus("abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00") });

    let out = ok(&t, &["update", "abc123", "start=2026-01-01T11:00:00Z", "end=2026-01-01T11:30:00Z", "priority=1"]);

    assert!(out.contains("summary: Focus"), "{out}");
    let event = t.app.calendar_client().unwrap().get_event("abc123").unwrap();
    assert_eq!((event.start, event.end, event.priority), (Some(at("2026-01-01T11:00:00+00:00")), Some(at("2026-01-01T11:30:00+00:00")), Some(1)));
    ok(&t, &["update", "abc123", "start=2026-01-01T08:00:00Z"]);
    assert_eq!(t.app.calendar_client().unwrap().get_event("abc123").unwrap().end, Some(at("2026-01-01T11:30:00+00:00")));
}

#[test]
fn update_refuses_an_overlap_or_no_new_times() {
    let t = test_app();
    t.seed_event(&focus("abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"));
    t.seed_event(&focus("def456", "2026-01-01T11:00:00+00:00", "2026-01-01T12:00:00+00:00"));
    t.fake.clear_requests();

    failed(&t, &["update", "abc123", "end=2026-01-01T11:30:00Z"], "exit", "Nothing was changed");
    failed(&t, &["update", "abc123", "priority=1"], "usage", "update requires at least one of: end, start");

    assert_eq!(writes(&t), 0);
}

#[test]
fn creates_an_event_given_its_summary_start_and_end() {
    let t = test_app();

    let out = ok(&t, &["create", "summary=New", "start=2026-01-01T09:00:00Z", "end=2026-01-01T09:30:00Z", "priority=1"]);

    assert!(out.contains("summary: New"), "{out}");
    let listed = t.app.calendar_client().unwrap().list_events(&at("2026-01-01T00:00:00+00:00"), &at("2026-01-02T00:00:00+00:00")).unwrap();
    assert_eq!((listed[0].summary.as_deref(), listed[0].priority), (Some("New"), Some(1)));
    for properties in [
        ["start=2026-01-01T09:00:00Z", "end=2026-01-01T09:30:00Z"],
        ["summary=New", "end=2026-01-01T09:30:00Z"],
        ["summary=New", "start=2026-01-01T09:00:00Z"],
    ] {
        let mut args = vec!["create"];
        args.extend(properties);
        failed(&t, &args, "usage", "create requires:");
    }
}

#[test]
fn deletes_an_event() {
    let t = test_app();
    t.seed_event(&focus("abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00"));

    assert!(ok(&t, &["delete", "abc123"]).contains("Deleted event abc123."));
    assert!(t.app.calendar_client().unwrap().get_event("abc123").unwrap().is_cancelled());
}

// -- raw labels -----------------------------------------------------------------------------

#[test]
fn creates_lists_updates_and_deletes_raw_labels() {
    let t = test_app();
    assert!(ok(&t, &["list_raw_labels"]).contains("No event labels found."));

    let created = ok(&t, &["create_raw_label", "background_color=#8e24aa", "name=Design Work"]);
    assert!(created.contains("Design Work"), "{created}");
    ok(&t, &["create_raw_label", "background_color=#111111"]);
    let listed = ok(&t, &["list_raw_labels"]);
    assert!(listed.contains("#8e24aa\tDesign Work"), "{listed}");
    let id = listed.lines().find(|l| l.contains("Design Work")).unwrap().split('\t').next().unwrap().to_string();

    assert!(ok(&t, &["update_raw_label", &id, "background_color=#d50000"]).contains("#d50000"));
    assert!(ok(&t, &["update_raw_label", &id, "name=New name"]).contains("New name"));
    assert!(ok(&t, &["delete_raw_label", &id]).contains(&id));
    assert!(!ok(&t, &["list_raw_labels"]).contains(&id));
}

#[test]
fn raw_label_commands_need_what_they_need() {
    let t = test_app();

    failed(&t, &["create_raw_label", "name=Design Work"], "usage", "create_raw_label requires: background_color");
    failed(&t, &["update_raw_label", "label-1"], "usage", "the following arguments are required: key=value");
    assert!(t.app.calendar_client().unwrap().list_event_labels().unwrap().0.is_empty());
}

// -- actions ------------------------------------------------------------------------------------

#[test]
fn list_actions_prints_each_action_and_the_label_count() {
    let t = test_app();
    assert!(ok(&t, &["list_actions"]).contains("No actions found."));
    ok(&t, &["create_action", "name=Play guitar", "status=active"]);
    ok(&t, &["create_action", "name=Juggle"]);
    ok(&t, &["create_action", "name=Old", "status=archived"]);

    let all = ok(&t, &["list_actions", "--all"]);
    let picked = ok(&t, &["list_actions", "--status", "archived"]);

    assert!(all.contains("\tactive\tPlay guitar") && all.contains("\tproposed\tJuggle") && all.contains("\tarchived\tOld"), "{all}");
    assert!(all.contains("event labels in use"));
    assert!(picked.contains("Old") && !picked.contains("Juggle"), "{picked}");
}

#[test]
fn create_and_update_action() {
    let t = test_app();

    let created = ok(&t, &["create_action", "name=Cook", "priority=2"]);
    let id = created.lines().next().unwrap().split('\t').next().unwrap().to_string();
    ok(&t, &["update_action", &id, "status=archived", "note=x"]);
    ok(&t, &["update_action", &id, "--clear", "note"]);

    let action = serde_json::to_value(t.app.actions().unwrap().get_action(&id).unwrap()).unwrap();
    assert_eq!((action["status"].clone(), action["note"].clone(), action["priority"].clone()), (json!("archived"), Value::Null, json!(2)));
    failed(&t, &["update_action", &id], "usage", "update_action requires at least one key=value or --clear");
    failed(&t, &["create_action", "priority=1"], "exit", "error: ");
}

// -- notes --------------------------------------------------------------------------------------

#[test]
fn records_a_note_ago_before_now() {
    let t = test_app();

    let out = ok(&t, &["note", "30m", "Started work"]);
    ok(&t, &["note", "0s"]);

    assert!(out.contains("2026-01-01 11:30:00+00:00") && out.contains("Started work"), "{out}");
    let notes: Vec<(Moment, Option<String>)> = t.app.noted_time_sheet().unwrap().read(false).unwrap().into_iter().map(|n| (n.timestamp, n.description)).collect();
    assert_eq!(notes, vec![(at("2026-01-01T11:30:00+00:00"), Some("Started work".to_string())), (now(), None)]);
    failed(&t, &["note", "not-a-duration"], "usage", "could not parse duration");
}

#[test]
fn lists_notes_by_time() {
    let t = test_app();
    assert!(ok(&t, &["get_notes"]).contains("No notes found."));
    ok(&t, &["note", "2h"]);
    ok(&t, &["note", "3h", "Started work"]);

    let lines: Vec<String> = ok(&t, &["get_notes"]).lines().map(str::to_string).collect();

    assert_eq!(lines, vec!["2026-01-01T09:00:00+00:00#3\t2026-01-01T09:00:00+00:00\tStarted work", "2026-01-01T10:00:00+00:00#2\t2026-01-01T10:00:00+00:00\t"]);
}

#[test]
fn edits_a_notes_description_or_time() {
    let t = test_app();
    ok(&t, &["note", "3h", "Started work"]);
    let id = "2026-01-01T09:00:00+00:00#2";

    let out = ok(&t, &["edit_note", id, "--description", "Standup"]);
    assert!(out.contains(&format!("id: {id}")) && out.contains("Standup"), "{out}");
    let moved = ok(&t, &["edit_note", id, "--ago", "30m"]);
    assert!(moved.contains("id: 2026-01-01T11:30:00+00:00#2"), "{moved}");
    let set = ok(&t, &["edit_note", "2026-01-01T11:30:00+00:00#2", "--at", "2026-01-01T09:30:00+00:00"]);
    assert!(set.contains("id: 2026-01-01T09:30:00+00:00#2"), "{set}");
    ok(&t, &["edit_note", "2026-01-01T09:30:00+00:00#2", "--description", ""]);
    assert_eq!(t.app.noted_time_sheet().unwrap().read(false).unwrap()[0].description, None);
}

#[test]
fn edit_note_rejects_bad_arguments_and_reports_a_refusal() {
    let t = test_app();
    ok(&t, &["note", "3h", "Started work"]);
    let id = "2026-01-01T09:00:00+00:00#2";

    failed(&t, &["edit_note", id], "usage", "needs at least one of");
    failed(&t, &["edit_note", id, "--ago", "1h", "--at", "2026-01-01T09:30:00+00:00"], "usage", "not allowed with");
    failed(&t, &["edit_note", id, "--at", "not-a-time"], "usage", "isn't an ISO 8601 time");
    failed(&t, &["edit_note", "2026-01-01T08:00:00+00:00#2", "--description", "x"], "exit", "error: ");
    assert_eq!(t.app.noted_time_sheet().unwrap().read(false).unwrap()[0].description.as_deref(), Some("Started work"));
}

#[test]
fn deletes_a_note_and_prints_what_it_was_or_reports_a_refusal() {
    let t = test_app();
    ok(&t, &["note", "3h", "oops"]);

    let out = ok(&t, &["delete_note", "2026-01-01T09:00:00+00:00#2"]);

    assert!(out.contains("Deleted:") && out.contains("oops"), "{out}");
    failed(&t, &["delete_note", "2026-01-01T09:00:00+00:00#2"], "exit", "no longer holds the note");
}

// -- migrate_locations ------------------------------------------------------------------

/// An event as Calendar keeps it, its facts' location stored the old way.
fn legacy(id: &str, start: &str, end: &str, rule: Option<&str>) -> Value {
    let mut body = json!({
        "id": id,
        "summary": id,
        "start": {"dateTime": start, "timeZone": "UTC"},
        "end": {"dateTime": end, "timeZone": "UTC"},
        "extendedProperties": {"private": {"cascading-time-tracker-facts": r#"{"location":"home","with":["sam"]}"#}},
    });
    if let Some(rule) = rule {
        body["recurrence"] = json!([rule]);
    }
    body
}

fn stored_facts(t: &TestApp, id: &str) -> Option<String> {
    let event = t.fake.event(&t.calendar_id, id)?;
    event.pointer("/extendedProperties/private/cascading-time-tracker-facts").and_then(Value::as_str).map(str::to_string)
}

#[test]
fn migrate_locations_says_what_it_would_rewrite_without_writing() {
    let t = test_app();
    t.fake.add_event(&t.calendar_id, legacy("lunch", "2025-12-30T12:00:00Z", "2025-12-30T13:00:00Z", None));
    t.fake.add_event(&t.calendar_id, legacy("standup", "2025-12-29T09:00:00Z", "2025-12-29T09:15:00Z", Some("RRULE:FREQ=DAILY")));
    t.fake.clear_requests();

    let out = ok(&t, &["migrate_locations"]);

    assert!(out.contains("Would rewrite 1 event(s) not in a series, and 4 past event(s) of a series."), "{out}");
    assert!(out.contains("Would split series standup (standup) from 2026-01-02T09:00:00+00:00 on"), "{out}");
    assert_eq!(writes(&t), 0);
}

#[test]
fn migrate_locations_rewrites_each_past_event_and_splits_a_series_at_its_next() {
    let t = test_app();
    t.fake.add_event(&t.calendar_id, legacy("lunch", "2025-12-30T12:00:00Z", "2025-12-30T13:00:00Z", None));
    t.fake.add_event(&t.calendar_id, legacy("standup", "2025-12-29T09:00:00Z", "2025-12-29T09:15:00Z", Some("RRULE:FREQ=DAILY")));

    ok(&t, &["migrate_locations", "--apply"]);

    let new = r#"{"locations":["home"],"with":["sam"]}"#;
    assert_eq!(stored_facts(&t, "lunch").as_deref(), Some(new));
    assert_eq!(stored_facts(&t, "standup").as_deref(), Some(new));
    let listed = t.app.calendar_client().unwrap().list_event_resources(&at("2025-12-01T00:00:00Z"), &at("2026-01-10T00:00:00Z"), None).unwrap();
    let series: Vec<&Value> = listed.iter().filter(|e| e.get("recurringEventId").is_some()).collect();
    assert!(series.len() > 4);
    for event in series {
        let raw = event.pointer("/extendedProperties/private/cascading-time-tracker-facts").and_then(Value::as_str);
        assert_eq!(raw, Some(new), "{event}");
    }
    // Nothing left to do.
    assert!(ok(&t, &["migrate_locations"]).starts_with("Would rewrite 0 event(s) not in a series, and 0 past"));
}
