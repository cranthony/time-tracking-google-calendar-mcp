//! Event's tests. (Naive datetimes can't arise: a Moment always has an
//! offset.)

use super::*;
use crate::facts::Facts;
use crate::testing::facts;

fn api_event(id: &str, start: &str, end: &str) -> Value {
    json!({"id": id, "summary": "Busy", "start": {"dateTime": start}, "end": {"dateTime": end}})
}

fn plain() -> Value {
    api_event("abc123", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")
}

fn at(text: &str) -> Moment {
    DateTime::parse_from_rfc3339(text).unwrap()
}

fn with(mut data: Value, key: &str, value: Value) -> Value {
    data[key] = value;
    data
}

fn read(data: Value) -> Event {
    Event::from_api(&data).unwrap()
}

fn body(event: Event) -> Map<String, Value> {
    event.to_api_body().unwrap()
}

fn focus() -> Event {
    Event { summary: Some("Focus block".into()), start: Some(at("2026-01-01T09:00:00+00:00")), end: Some(at("2026-01-01T10:00:00+00:00")), ..Default::default() }
}

fn private(body: &Map<String, Value>) -> Value {
    body["extendedProperties"]["private"].clone()
}

fn cleared(names: &[&str]) -> BTreeSet<String> {
    names.iter().map(|n| n.to_string()).collect()
}

#[test]
fn action_ids_round_trip_through_a_private_extended_property() {
    // [] is stored, so clearing actions sticks.
    let split = |id: &str, minutes, percent, remaining| crate::stores::shares::Share::new(id.to_string(), minutes, percent, remaining);
    for (ids, stored) in [
        (crate::testing::shares(&["a1", "a2"]), "a1 a2"),
        (vec![], ""),
        (vec![split("a1", Some(30), None, true), split("a2", None, None, false), split("a3", None, Some(25), false)], "a1=30m+r a2 a3=25%"),
    ] {
        let sent = body(Event { action_ids: Some(ids.clone()), ..Default::default() });

        assert_eq!(private(&sent), json!({"cascading-time-tracker-action_ids": stored}));
        let data = with(plain(), "extendedProperties", sent["extendedProperties"].clone());
        assert_eq!(read(data).action_ids, Some(ids));
    }
}

#[test]
fn action_ids_are_none_when_never_set() {
    assert_eq!(read(plain()).action_ids, None);
    assert!(!body(Event::default()).contains_key("extendedProperties"));
}

#[test]
fn from_api_parses_fields() {
    let event = read(plain());

    assert_eq!(event.id.as_deref(), Some("abc123"));
    assert_eq!(event.summary.as_deref(), Some("Busy"));
    assert_eq!(event.start, Some(at("2026-01-01T09:00:00+00:00")));
    assert_eq!(event.end, Some(at("2026-01-01T10:00:00+00:00")));
    assert_eq!((event.description, event.location, event.status, event.recurring_event_id), (None, None, None, None));
    assert_eq!((event.priority, event.event_label_id), (None, None));
}

#[test]
fn from_api_parses_non_utc_offset() {
    let event = read(api_event("abc123", "2026-01-01T09:00:00-05:00", "2026-01-01T10:00:00-05:00"));

    assert_eq!(event.start.unwrap().offset().local_minus_utc(), -5 * 3600);
    assert_eq!(event.start, Some(at("2026-01-01T14:00:00+00:00")));
}

#[test]
fn from_api_parses_z_suffix_as_utc() {
    let event = read(api_event("abc123", "2026-01-01T09:00:00Z", "2026-01-01T10:00:00Z"));

    assert_eq!(event.start, Some(at("2026-01-01T09:00:00+00:00")));
    assert_eq!(event.end.unwrap().offset().local_minus_utc(), 0);
}

#[test]
fn from_api_extracts_location_status_recurring_id_and_label() {
    assert_eq!(read(with(plain(), "location", json!("Conference Room A"))).location.as_deref(), Some("Conference Room A"));
    assert_eq!(read(with(plain(), "status", json!("cancelled"))).status.as_deref(), Some("cancelled"));
    assert_eq!(read(with(plain(), "recurringEventId", json!("abc"))).recurring_event_id.as_deref(), Some("abc"));
    assert_eq!(read(with(plain(), "eventLabelId", json!("label-1"))).event_label_id.as_deref(), Some("label-1"));
}

#[test]
fn from_api_extracts_a_series_rules_and_time_zone() {
    let mut data = api_event("abc123", "2026-01-05T09:00:00-05:00", "2026-01-05T10:00:00-05:00");
    data["start"]["timeZone"] = json!("America/New_York");
    data["recurrence"] = json!(["RRULE:FREQ=WEEKLY;BYDAY=MO"]);

    let event = read(data);

    assert_eq!(event.recurrence, Some(vec!["RRULE:FREQ=WEEKLY;BYDAY=MO".to_string()]));
    assert_eq!(event.time_zone.as_deref(), Some("America/New_York"));
}

#[test]
fn from_api_extracts_an_instances_original_start() {
    let data = with(
        api_event("abc123_x", "2026-01-06T15:00:00+00:00", "2026-01-06T16:00:00+00:00"),
        "originalStartTime",
        json!({"dateTime": "2026-01-05T14:00:00+00:00"}),
    );

    assert_eq!(read(data).original_start, Some(at("2026-01-05T14:00:00+00:00")));
}

#[test]
fn to_api_body_sends_rules_and_time_zone_but_never_original_start() {
    let start = at("2026-01-05T09:00:00-05:00");
    let sent = body(Event {
        start: Some(start),
        end: Some(start + chrono::Duration::hours(1)),
        time_zone: Some("America/New_York".into()),
        recurrence: Some(vec!["RRULE:FREQ=DAILY".into()]),
        original_start: Some(start),
        ..Default::default()
    });

    assert_eq!(sent["recurrence"], json!(["RRULE:FREQ=DAILY"]));
    assert_eq!(sent["start"]["timeZone"], "America/New_York");
    assert_eq!(sent["end"]["timeZone"], "America/New_York");
    assert!(!sent.contains_key("originalStartTime"));
}

#[test]
fn from_api_uses_time_zone_when_date_time_is_naive() {
    let event = read(json!({
        "id": "abc123",
        "summary": "Busy",
        "start": {"dateTime": "2026-01-01T09:00:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-01-01T10:00:00", "timeZone": "America/New_York"},
    }));

    assert_eq!(event.start, Some(at("2026-01-01T09:00:00-05:00")));
    assert_eq!(event.end, Some(at("2026-01-01T10:00:00-05:00")));
}

#[test]
fn from_api_extracts_app_properties() {
    let data = with(
        plain(),
        "extendedProperties",
        json!({"private": {"cascading-time-tracker-priority": "2"}}),
    );

    let event = read(data);

    assert_eq!(event.priority, Some(2));
}

#[test]
fn from_api_ignores_the_old_end_of_day_sleep_mark() {
    // Once set by hand to end compaction's days; now inert on the events
    // that still have it.
    let data = with(plain(), "extendedProperties", json!({"private": {"cascading-time-tracker-is_end_of_day_sleep": "true"}}));

    assert_eq!(read(data), read(plain()));
}

#[test]
fn from_api_ignores_other_private_keys() {
    let event = read(with(plain(), "extendedProperties", json!({"private": {"someOtherApp-key": "value"}})));

    assert_eq!(event.priority, None);
}

#[test]
fn from_api_raises_when_date_time_is_missing_its_time_zone() {
    assert!(Event::from_api(&api_event("abc123", "2026-01-01T09:00:00", "2026-01-01T10:00:00")).is_err());
}

#[test]
fn from_api_raises_when_only_an_all_day_date_is_given() {
    let data = json!({"id": "abc123", "summary": "Busy", "start": {"date": "2026-01-01"}, "end": {"date": "2026-01-02"}});

    assert!(Event::from_api(&data).is_err());
}

#[test]
fn from_api_defaults_summary_to_none_when_absent() {
    let data = json!({"id": "abc123", "start": {"dateTime": "2026-01-01T09:00:00+00:00"}, "end": {"dateTime": "2026-01-01T10:00:00+00:00"}});

    assert_eq!(read(data).summary, None);
}

#[test]
fn to_api_body_omits_optional_fields_when_absent() {
    let sent = body(focus());

    for key in ["description", "location", "status", "recurringEventId", "extendedProperties", "eventLabelId", "colorId"] {
        assert!(!sent.contains_key(key), "{key}");
    }
    assert_eq!(sent["summary"], "Focus block");
    assert_eq!(sent["start"], json!({"dateTime": "2026-01-01T09:00:00+00:00"}));
    assert_eq!(sent["end"], json!({"dateTime": "2026-01-01T10:00:00+00:00"}));
}

#[test]
fn to_api_body_for_a_partial_update_payload_without_summary_start_end() {
    let sent = body(Event { id: Some("abc123".into()), priority: Some(1), ..Default::default() });

    assert!(!sent.contains_key("summary") && !sent.contains_key("start") && !sent.contains_key("end"));
    assert_eq!(sent["extendedProperties"], json!({"private": {"cascading-time-tracker-priority": "1"}}));
}

#[test]
fn to_api_body_includes_what_is_set() {
    let sent = body(Event {
        description: Some("Deep work".into()),
        location: Some("Conference Room A".into()),
        status: Some("tentative".into()),
        event_label_id: Some("label-1".into()),
        recurring_event_id: Some("abc123".into()),
        ..focus()
    });

    assert_eq!(sent["description"], "Deep work");
    assert_eq!(sent["location"], "Conference Room A");
    assert_eq!(sent["status"], "tentative");
    assert_eq!(sent["eventLabelId"], "label-1");
    // Never the series it's an instance of.
    assert!(!sent.contains_key("recurringEventId"));
}

#[test]
fn to_api_body_includes_app_properties_when_present() {
    let sent = body(Event { priority: Some(2), action_ids: Some(crate::testing::shares(&["a"])), ..focus() });

    assert_eq!(
        sent["extendedProperties"],
        json!({"private": {"cascading-time-tracker-priority": "2", "cascading-time-tracker-action_ids": "a"}})
    );
    assert_eq!(body(Event { priority: Some(1), ..focus() })["extendedProperties"], json!({"private": {"cascading-time-tracker-priority": "1"}}));
}

#[test]
fn to_api_body_never_writes_a_color_id() {
    // An event's own color overrides its label's, and writing one -- even
    // clearing it -- can drop the label.
    assert!(!body(Event { id: Some("abc123".into()), ..Default::default() }).contains_key("colorId"));
    for priority in [-1, 0, 1, 2, 3, 5] {
        assert!(!body(Event { id: Some("abc123".into()), priority: Some(priority), ..Default::default() }).contains_key("colorId"));
    }
    assert!(!body(Event { id: Some("abc123".into()), cleared: cleared(&["priority"]), ..Default::default() }).contains_key("colorId"));
}

#[test]
fn to_api_body_sends_cleared_fields_as_null() {
    let sent = body(Event { id: Some("abc123".into()), summary: Some("Focus".into()), cleared: cleared(&CLEARABLE_EVENT_FIELDS), ..Default::default() });

    let mut nulls = Map::new();
    nulls.insert("cascading-time-tracker-priority".into(), Value::Null);
    nulls.insert("cascading-time-tracker-description_format".into(), Value::Null);
    nulls.insert("cascading-time-tracker-facts".into(), Value::Null);
    for i in 2..9 {
        nulls.insert(format!("cascading-time-tracker-facts-{i}"), Value::Null);
    }
    nulls.insert("cascading-time-tracker-judgments".into(), Value::Null);
    for i in 2..17 {
        nulls.insert(format!("cascading-time-tracker-judgments-{i}"), Value::Null);
    }
    let sorted = |v: Value| -> std::collections::BTreeMap<String, Value> { v.as_object().unwrap().clone().into_iter().collect() };
    assert_eq!(sent["summary"], "Focus");
    assert_eq!((sent["description"].clone(), sent["location"].clone()), (Value::Null, Value::Null));
    assert_eq!(sorted(private(&sent)), sorted(Value::Object(nulls)));
    assert_eq!(sent.len(), 4);
}

#[test]
fn a_descriptions_format_is_a_private_property_cleared_with_it() {
    let sent = body(Event { id: Some("abc123".into()), description: Some("**Hi**".into()), description_format: Some(TextFormat::Markdown), ..Default::default() });
    assert_eq!(private(&sent)["cascading-time-tracker-description_format"], "markdown");
    let read = Event::from_api(&json!({"id": "abc123", "start": {"dateTime": "2026-10-29T09:00:00Z"}, "extendedProperties": {"private": {"cascading-time-tracker-description_format": "html"}}})).unwrap();
    assert_eq!(read.description_format, Some(TextFormat::Html));

    let cleared = body(Event { id: Some("abc123".into()), cleared: cleared(&["description"]), ..Default::default() });
    assert_eq!(cleared["description"], Value::Null);
    assert_eq!(private(&cleared)["cascading-time-tracker-description_format"], Value::Null);
}

#[test]
fn facts_round_trip_as_compact_json_under_short_keys() {
    let given = facts(json!({"locations": ["l1"], "with": ["p1"], "for": ["p2"], "notes": {"self": "tired", "p1": "glad"}}));
    let sent = body(Event { id: Some("abc123".into()), facts: Some(given.clone()), ..Default::default() });

    let stored = private(&sent);
    let parsed: Value = serde_json::from_str(stored["cascading-time-tracker-facts"].as_str().unwrap()).unwrap();
    assert_eq!(parsed, json!({"locations": ["l1"], "with": ["p1"], "for": ["p2"], "notes": {"self": "tired", "p1": "glad"}}));
    // Unused chunks are removed, so a shorter value leaves no tail.
    assert!((2..9).all(|i| stored[format!("cascading-time-tracker-facts-{i}")].is_null()));
    let mut data = Value::Object(sent);
    data["id"] = json!("abc123");
    data["start"] = json!({"dateTime": "2026-10-01T18:00:00-04:00"});
    data["end"] = json!({"dateTime": "2026-10-01T20:00:00-04:00"});
    assert_eq!(read(data).facts, Some(given));
}

#[test]
fn long_facts_are_split_across_properties_and_joined_on_read() {
    let given = facts(json!({"notes": {"self": "x".repeat(900), "p1": "y".repeat(900)}, "with": ["p1"]}));
    let sent = body(Event { id: Some("abc123".into()), facts: Some(given.clone()), ..Default::default() });

    let set: Map<String, Value> = private(&sent).as_object().unwrap().iter().filter(|(_, v)| !v.is_null()).map(|(k, v)| (k.clone(), v.clone())).collect();
    let mut keys: Vec<&String> = set.keys().collect();
    keys.sort();
    assert_eq!(keys, vec!["cascading-time-tracker-facts", "cascading-time-tracker-facts-2", "cascading-time-tracker-person-p1"]);
    assert!(set.values().all(|v| v.as_str().unwrap().chars().count() <= 1024));
    let data = json!({
        "id": "abc123",
        "start": {"dateTime": "2026-10-01T18:00:00-04:00"},
        "end": {"dateTime": "2026-10-01T20:00:00-04:00"},
        "extendedProperties": {"private": set},
    });
    assert_eq!(read(data).facts, Some(given));
}

#[test]
fn an_insert_sends_no_null_properties() {
    // An insert refuses a null private property, which a patch would take
    // to mean "remove it".
    let event = Event { facts: Some(facts(json!({"with": ["p1"]}))), ..focus() };

    let sent = event.insert_body().unwrap();

    assert_eq!(private(&sent), json!({"cascading-time-tracker-facts": "{\"with\":[\"p1\"]}", "cascading-time-tracker-person-p1": "1"}));
}

#[test]
fn facts_index_the_event_under_each_person_they_name_but_the_user() {
    let event = Event { facts: Some(facts(json!({"with": ["p1"], "for": ["p2"], "notes": {"self": "glad", "p3": "quiet"}}))), ..focus() };

    let sent = event.insert_body().unwrap();

    let mut keys: Vec<String> = private(&sent).as_object().unwrap().keys().filter(|k| is_person_key(k)).cloned().collect();
    keys.sort();
    assert_eq!(keys, vec![person_key("p1"), person_key("p2"), person_key("p3")]);
}

#[test]
fn facts_too_long_to_store_are_refused() {
    let notes: Map<String, Value> = (0..9).map(|i| (format!("p{i}"), json!("z".repeat(1000)))).collect();
    let event = Event { id: Some("abc123".into()), facts: Some(facts(json!({"notes": notes}))), ..Default::default() };

    let error = event.to_api_body().unwrap_err();

    assert!(error.to_string().contains("longer than 8192 characters"), "{error}");
}

#[test]
fn empty_facts_remove_them() {
    let sent = body(Event { id: Some("abc123".into()), facts: Some(Facts::default()), ..Default::default() });

    let stored = private(&sent);
    assert_eq!(stored.as_object().unwrap().len(), 8);
    assert!(stored.as_object().unwrap().values().all(Value::is_null));
    assert!(stored.get("cascading-time-tracker-facts").is_some());
}

#[test]
fn facts_that_arent_json_read_as_none() {
    let data = json!({
        "id": "abc123",
        "start": {"dateTime": "2026-10-01T18:00:00-04:00"},
        "end": {"dateTime": "2026-10-01T20:00:00-04:00"},
        "extendedProperties": {"private": {"cascading-time-tracker-facts": "not json"}},
    });

    assert_eq!(read(data).facts, None);
}

#[test]
fn to_api_body_clears_some_app_properties_while_setting_others() {
    let sent = body(Event { id: Some("abc123".into()), priority: Some(2), cleared: cleared(&["location"]), ..Default::default() });

    assert!(!sent.contains_key("colorId"));
    assert_eq!(sent["location"], Value::Null);
    assert_eq!(sent["extendedProperties"], json!({"private": {"cascading-time-tracker-priority": "2"}}));
}

#[test]
fn refuses_clearing_what_cant_be_cleared_or_is_also_set() {
    let cases = [
        (Event { cleared: cleared(&["summary"]), ..Default::default() }, "Can't clear ['summary']; clearable fields are"),
        (Event { cleared: cleared(&["action_ids"]), ..Default::default() }, "Can't clear ['action_ids']"),
        (Event { priority: Some(1), cleared: cleared(&["priority"]), ..Default::default() }, "Can't both set and clear ['priority']"),
    ];

    for (event, message) in cases {
        let error = event.check_cleared().unwrap_err();
        assert!(error.to_string().contains(message), "{error}");
    }
}

#[test]
fn to_api_body_indexes_an_event_under_the_habits_its_goals_are_from() {
    use crate::stores::goals::GoalShare;
    use crate::stores::shares::Share;
    let event = Event {
        goals: Some(vec![GoalShare::new("call_2026-10-25".into(), None, None, false), GoalShare::new("plain".into(), None, None, false)]),
        ..Event::default()
    };

    let body = event.to_api_body().unwrap();

    let private = &body["extendedProperties"]["private"];
    assert_eq!(private["cascading-time-tracker-habit-call"], "1");
    assert_eq!(private.as_object().unwrap().keys().filter(|k| is_habit_key(k)).count(), 1);
}
