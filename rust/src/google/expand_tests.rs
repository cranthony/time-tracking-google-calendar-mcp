//! The expander's tests: what the local_expansion and
//! expansion_on_calendar probes found Google doing.

use super::*;

const NEW_YORK: Tz = chrono_tz::America::New_York;

fn utc(text: &str) -> DateTime<Utc> {
    DateTime::parse_from_rfc3339(text).unwrap().with_timezone(&Utc)
}

fn ids(events: &[Value]) -> Vec<String> {
    events.iter().map(|e| e["id"].as_str().unwrap().to_string()).collect()
}

/// A daily series at 07:00 New York time from Oct 30, 2026, `rule`'s way.
fn daily(id: &str, rule: &str) -> Value {
    json!({
        "id": id, "etag": "\"1\"", "status": "confirmed", "summary": "Run",
        "start": {"dateTime": "2026-10-30T07:00:00-04:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2026-10-30T07:30:00-04:00", "timeZone": "America/New_York"},
        "recurrence": [rule],
    })
}

fn listed(events: &[Value], from: &str, to: &str) -> Vec<Value> {
    expand(NEW_YORK, events, Some(utc(from)), Some(utc(to)), false, utc(to))
}

#[test]
fn an_instance_is_its_master_at_its_own_start_keeping_the_wall_clock_across_dst() {
    let events = vec![daily("run", "RRULE:FREQ=DAILY")];

    let found = listed(&events, "2026-10-31T00:00:00Z", "2026-11-03T00:00:00Z");

    assert_eq!(ids(&found), vec!["run_20261031T110000Z", "run_20261101T120000Z", "run_20261102T120000Z"]);
    let first = &found[0];
    assert_eq!((first["etag"].clone(), first["summary"].clone(), first["recurringEventId"].clone()), (json!("\"1\""), json!("Run"), json!("run")));
    assert_eq!(first["start"], json!({"dateTime": "2026-10-31T07:00:00-04:00", "timeZone": "America/New_York"}));
    assert_eq!(found[1]["end"], json!({"dateTime": "2026-11-01T07:30:00-05:00", "timeZone": "America/New_York"}));
    assert_eq!(found[1]["originalStartTime"], found[1]["start"]);
    assert!(first.get("recurrence").is_none());
}

#[test]
fn times_are_written_at_the_calendars_offset_whatever_zone_the_series_is_in() {
    let london = json!({
        "id": "tea", "etag": "\"1\"", "status": "confirmed",
        "start": {"dateTime": "2026-10-20T08:00:00", "timeZone": "Europe/London"},
        "end": {"dateTime": "2026-10-20T08:30:00", "timeZone": "Europe/London"},
        "recurrence": ["RRULE:FREQ=DAILY;COUNT=2"],
    });

    let found = listed(&[london], "2026-10-19T00:00:00Z", "2026-10-30T00:00:00Z");

    assert_eq!(found[0]["start"], json!({"dateTime": "2026-10-20T03:00:00-04:00", "timeZone": "Europe/London"}));
}

#[test]
fn exceptions_take_their_instances_places_and_cancelled_ones_are_left_out() {
    let renamed = json!({"id": "run_20261031T110000Z", "etag": "\"2\"", "status": "confirmed", "summary": "Long run", "recurringEventId": "run",
        "originalStartTime": {"dateTime": "2026-10-31T07:00:00-04:00"}, "start": {"dateTime": "2026-10-31T08:00:00-04:00"}, "end": {"dateTime": "2026-10-31T09:00:00-04:00"}});
    let cancelled = json!({"id": "run_20261101T120000Z", "status": "cancelled", "recurringEventId": "run",
        "originalStartTime": {"dateTime": "2026-11-01T07:00:00-05:00"}, "start": {"dateTime": "2026-11-01T07:00:00-05:00"}, "end": {"dateTime": "2026-11-01T07:30:00-05:00"}});
    let events = vec![daily("run", "RRULE:FREQ=DAILY"), renamed.clone(), cancelled];

    let found = listed(&events, "2026-10-31T00:00:00Z", "2026-11-03T00:00:00Z");

    assert_eq!(ids(&found), vec!["run_20261031T110000Z", "run_20261102T120000Z"]);
    assert_eq!(found[0], renamed);
    let with_deleted = expand(NEW_YORK, &events, Some(utc("2026-10-31T00:00:00Z")), Some(utc("2026-11-03T00:00:00Z")), true, utc("2026-11-03T00:00:00Z"));
    assert_eq!(with_deleted.len(), 3);
}

#[test]
fn a_series_google_split_names_its_instances_after_the_base_series() {
    let mut rest = daily("run_R20261101T120000", "RRULE:FREQ=DAILY");
    rest["start"] = json!({"dateTime": "2026-11-01T07:00:00-05:00", "timeZone": "America/New_York"});
    rest["end"] = json!({"dateTime": "2026-11-01T07:30:00-05:00", "timeZone": "America/New_York"});
    let events = vec![daily("run", "RRULE:FREQ=DAILY;UNTIL=20261101T035959Z"), rest];

    let found = listed(&events, "2026-10-31T00:00:00Z", "2026-11-03T00:00:00Z");

    assert_eq!(ids(&found), vec!["run_20261031T110000Z", "run_20261101T120000Z", "run_20261102T120000Z"]);
    assert_eq!(found[1]["recurringEventId"], json!("run_R20261101T120000"));
    assert_eq!((instance_base("run_R20261101T120000"), instance_base("run_Rx"), instance_base("run")), ("run", "run_Rx", "run"));
}

#[test]
fn google_expands_no_more_than_the_cap_counted_from_the_start() {
    let mut old = daily("old", "RRULE:FREQ=DAILY");
    old["start"] = json!({"dateTime": "2024-01-01T07:00:00-05:00", "timeZone": "America/New_York"});
    old["end"] = json!({"dateTime": "2024-01-01T07:30:00-05:00", "timeZone": "America/New_York"});

    assert_eq!(occurrences(NEW_YORK, &old, None).len(), INSTANCE_CAP);
    assert!(listed(&[old], "2026-10-31T00:00:00Z", "2026-11-03T00:00:00Z").is_empty());
}

#[test]
fn all_day_series_and_exdates_and_counts() {
    let all_day = json!({"id": "fast", "status": "confirmed", "start": {"date": "2026-10-30"}, "end": {"date": "2026-10-31"},
        "recurrence": ["RRULE:FREQ=DAILY;COUNT=4", "EXDATE;VALUE=DATE:20261101"]});

    let found = listed(&[all_day], "2026-10-29T00:00:00Z", "2026-11-10T00:00:00Z");

    assert_eq!(ids(&found), vec!["fast_20261030", "fast_20261031", "fast_20261102"]);
    assert_eq!(found[2]["end"], json!({"date": "2026-11-03"}));
}

#[test]
fn an_event_that_takes_no_time_is_listed_from_its_moment() {
    let mark = json!({"id": "mark", "status": "confirmed", "start": {"dateTime": "2026-10-31T09:00:00Z"}, "end": {"dateTime": "2026-10-31T09:00:00Z"}});

    assert_eq!(listed(std::slice::from_ref(&mark), "2026-10-31T09:00:00Z", "2026-11-01T00:00:00Z").len(), 1);
    assert!(listed(&[mark], "2026-10-31T09:00:01Z", "2026-11-01T00:00:00Z").is_empty());
}

#[test]
fn a_rule_it_cant_read_expands_to_nothing() {
    assert!(occurrences(NEW_YORK, &daily("odd", "RRULE:FREQ=SOMETIMES"), None).is_empty());
}
