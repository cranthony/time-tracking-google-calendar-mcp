//! tests/test_google_calendar.py's client tests, against the fake Google.
//! (TestCalendar's Calendar class isn't in the port, and TestEventLabel's
//! type checks -- no priority field, a required color -- are the
//! compiler's.)

use super::*;
use crate::testing::fake_google::{FakeGoogle, LoggedRequest};
use crate::write_lock::WRITE_LOCK;
use chrono::DateTime;

struct Fixture {
    fake: FakeGoogle,
    client: CalendarClient,
    calendar_id: String,
}

fn fixture() -> Fixture {
    fixture_with(Some("UTC"), None)
}

fn fixture_with(time_zone: Option<&str>, description: Option<&str>) -> Fixture {
    let fake = FakeGoogle::start();
    let calendar_id = fake.with(|s| s.add_calendar("Time Tracking", time_zone, description));
    let client = CalendarClient::new(fake.google(), &calendar_id);
    Fixture { fake, client, calendar_id }
}

fn at(text: &str) -> Moment {
    DateTime::parse_from_rfc3339(text).unwrap()
}

fn hour(h: u32, m: u32) -> Moment {
    at(&format!("2026-01-01T{h:02}:{m:02}:00+00:00"))
}

fn event(id: Option<&str>, start: &str, end: &str) -> Event {
    Event { id: id.map(str::to_string), summary: Some("Busy".into()), start: Some(at(start)), end: Some(at(end)), ..Default::default() }
}

impl Fixture {
    fn add(&self, id: &str, start: &str, end: &str) {
        let mut body = event(Some(id), start, end).insert_body().unwrap();
        body.insert("id".into(), json!(id));
        self.fake.add_event(&self.calendar_id, Value::Object(body));
    }

    /// Three events, at 09, 11 and 13 o'clock on 1 January.
    fn day(&self) {
        self.add("ev1", "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00");
        self.add("ev2", "2026-01-01T11:00:00+00:00", "2026-01-01T12:00:00+00:00");
        self.add("ev3", "2026-01-01T13:00:00+00:00", "2026-01-01T14:00:00+00:00");
    }

    fn requests(&self, method: &str, ending: &str) -> Vec<LoggedRequest> {
        self.fake.requests().into_iter().filter(|r| r.method == method && r.path.ends_with(ending)).collect()
    }

    fn listings(&self) -> usize {
        self.requests("GET", "/events").len()
    }

    fn calendar_patches(&self) -> Vec<LoggedRequest> {
        self.requests("PATCH", &format!("/calendars/{}", self.calendar_id))
    }

    fn set_labels(&self, labels: Value) {
        let _held = WRITE_LOCK.hold();
        self.client.patch_calendar(None, json!({"labelProperties": {"eventLabels": labels}})).unwrap();
        self.fake.clear_requests();
    }

    fn set_description(&self, description: &str) {
        let _held = WRITE_LOCK.hold();
        self.client.patch_calendar(None, json!({"description": description})).unwrap();
        self.fake.clear_requests();
    }

    fn description(&self) -> Option<String> {
        self.fake.calendar_resource(&self.calendar_id).get("description").and_then(Value::as_str).map(str::to_string)
    }

    fn etag(&self) -> String {
        self.fake.calendar_resource(&self.calendar_id)["etag"].as_str().unwrap().to_string()
    }
}

fn ids(events: &[Event]) -> Vec<String> {
    events.iter().map(|e| e.id.clone().unwrap()).collect()
}

fn query<'a>(request: &'a LoggedRequest, name: &str) -> Option<&'a str> {
    request.query.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
}

// -- events -----------------------------------------------------------------------------

#[test]
fn list_events_maps_response_items() {
    let f = fixture();
    f.day();

    let events = f.client.list_events(&hour(0, 0), &at("2026-01-02T00:00:00+00:00")).unwrap();

    assert_eq!(ids(&events), vec!["ev1", "ev2", "ev3"]);
    let listing = &f.requests("GET", "/events")[0];
    assert_eq!(query(listing, "timeMin"), Some("2026-01-01T00:00:00+00:00"));
    assert_eq!(query(listing, "timeMax"), Some("2026-01-02T00:00:00+00:00"));
    assert_eq!((query(listing, "singleEvents"), query(listing, "orderBy"), query(listing, "maxResults")), (Some("true"), Some("startTime"), Some("2500")));
}

#[test]
fn list_events_follows_next_page_token_until_the_last_page() {
    // The fake pages every 7 events.
    let f = fixture();
    for i in 0..16 {
        f.add(&format!("ev{i:02}"), &format!("2026-01-01T{:02}:00:00+00:00", i + 1), &format!("2026-01-01T{:02}:30:00+00:00", i + 1));
    }

    let events = f.client.list_events(&hour(0, 0), &at("2026-01-02T00:00:00+00:00")).unwrap();

    assert_eq!(events.len(), 16);
    let tokens: Vec<Option<String>> = f.requests("GET", "/events").iter().map(|r| query(r, "pageToken").map(str::to_string)).collect();
    assert_eq!(tokens.len(), 3);
    assert!(tokens[0].is_none() && tokens[1].is_some() && tokens[2].is_some());
}

#[test]
fn list_instances_includes_cancelled_ones_and_follows_pages() {
    let f = fixture();
    let mut series = event(Some("series1"), "2026-01-05T09:00:00+00:00", "2026-01-05T10:00:00+00:00");
    series.recurrence = Some(vec!["RRULE:FREQ=DAILY".into()]);
    series.time_zone = Some("UTC".into());
    let mut body = series.insert_body().unwrap();
    body.insert("id".into(), json!("series1"));
    f.fake.add_event(&f.calendar_id, Value::Object(body));
    {
        let _held = WRITE_LOCK.hold();
        f.client.delete_event("series1_20260106T090000Z").unwrap();
    }

    let events = f.client.list_instances("series1", &at("2026-01-19T00:00:00+00:00")).unwrap();

    assert_eq!(events.len(), 14);
    assert!(events.iter().any(|e| e.id.as_deref() == Some("series1_20260106T090000Z") && e.is_cancelled()));
    let pages = f.requests("GET", "/instances");
    assert_eq!((query(&pages[0], "showDeleted"), query(&pages[0], "timeMax")), (Some("true"), Some("2026-01-19T00:00:00+00:00")));
    assert!(pages.len() > 1 && query(&pages[1], "pageToken").is_some());
}

#[test]
fn list_events_returns_empty_list_when_no_items() {
    let f = fixture();

    assert!(f.client.list_events(&hour(0, 0), &at("2026-01-02T00:00:00+00:00")).unwrap().is_empty());
}

#[test]
fn get_event_returns_parsed_event() {
    let f = fixture();
    f.day();

    assert_eq!(f.client.get_event("ev2").unwrap().start, Some(hour(11, 0)));
}

#[test]
fn create_event_sends_body_and_returns_parsed_event() {
    let f = fixture();
    let given = Event { summary: Some("New".into()), ..event(None, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00") };

    let created = {
        let _held = WRITE_LOCK.hold();
        f.client.create_event(&given).unwrap()
    };

    assert!(created.id.is_some());
    assert_eq!(created.summary.as_deref(), Some("New"));
    let sent = &f.requests("POST", "/events")[0];
    assert_eq!(sent.body, Value::Object(given.insert_body().unwrap()));
    // Without an id of its own, Calendar assigns one.
    assert!(sent.body.get("id").is_none());
    assert!(query(sent, "eventLabelVersion").is_none());
}

#[test]
fn create_event_sends_a_caller_chosen_id_so_a_retry_cannot_duplicate() {
    let f = fixture();
    let given = event(Some("cmpabc123s001"), "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00");
    let _held = WRITE_LOCK.hold();

    f.client.create_event(&given).unwrap();
    let again = f.client.create_event(&given);

    assert_eq!(f.requests("POST", "/events")[0].body["id"], "cmpabc123s001");
    assert_eq!(status_of(&again.unwrap_err()), Some(409));
}

#[test]
fn create_event_passes_event_label_version_when_setting_event_label_id() {
    let f = fixture();
    f.set_labels(json!([{"id": "label-1", "backgroundColor": "#8e24aa"}]));
    let given = Event { event_label_id: Some("label-1".into()), ..event(None, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00") };

    {
        let _held = WRITE_LOCK.hold();
        f.client.create_event(&given).unwrap();
    }

    assert_eq!(query(&f.requests("POST", "/events")[0], "eventLabelVersion"), Some("1"));
}

#[test]
fn update_event_requires_an_id() {
    let f = fixture();
    let _held = WRITE_LOCK.hold();

    assert!(f.client.update_event(&event(None, "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00")).is_err());
    assert!(f.fake.requests().is_empty());
}

#[test]
fn update_event_patches_and_returns_parsed_event() {
    let f = fixture();
    f.day();
    let patch = event(Some("ev1"), "2026-01-01T09:00:00+00:00", "2026-01-01T11:00:00+00:00");

    let updated = {
        let _held = WRITE_LOCK.hold();
        f.client.update_event(&patch).unwrap()
    };

    assert_eq!(updated.end, Some(hour(11, 0)));
    let sent = &f.requests("PATCH", "/events/ev1")[0];
    assert_eq!(sent.body, Value::Object(patch.to_api_body().unwrap()));
    // Not touching its label, so no label version.
    assert!(query(sent, "eventLabelVersion").is_none());
}

#[test]
fn update_event_passes_event_label_version_when_setting_event_label_id() {
    let f = fixture();
    f.day();
    f.set_labels(json!([{"id": "label-1", "backgroundColor": "#8e24aa"}]));

    {
        let _held = WRITE_LOCK.hold();
        f.client.update_event(&Event { id: Some("ev1".into()), event_label_id: Some("label-1".into()), ..Default::default() }).unwrap();
    }

    assert_eq!(query(&f.requests("PATCH", "/events/ev1")[0], "eventLabelVersion"), Some("1"));
}

#[test]
fn updating_an_event_nulls_only_the_properties_it_has() {
    // A recurring instance refuses a null for a property it doesn't have,
    // and shorter facts null every chunk they don't use.
    let f = fixture();
    let mut body = event(Some("ev1"), "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00").insert_body().unwrap();
    body.insert("id".into(), json!("ev1"));
    body.insert(
        "extendedProperties".into(),
        json!({"private": {"cascading-time-tracker-facts": "{}", "cascading-time-tracker-facts-2": "x", "cascading-time-tracker-priority": "1"}}),
    );
    f.fake.add_event(&f.calendar_id, Value::Object(body));
    let patch = Event {
        id: Some("ev1".into()),
        facts: Some(crate::testing::facts(json!({"with": ["p1"]}))),
        cleared: ["priority", "location"].iter().map(|s| s.to_string()).collect(),
        ..Default::default()
    };

    {
        let _held = WRITE_LOCK.hold();
        f.client.update_event(&patch).unwrap();
    }

    let sent = &f.requests("PATCH", "/events/ev1")[0].body;
    assert_eq!(
        sent["extendedProperties"]["private"],
        json!({"cascading-time-tracker-facts": "{\"with\":[\"p1\"]}", "cascading-time-tracker-facts-2": null, "cascading-time-tracker-priority": null})
    );
    assert_eq!(sent["location"], Value::Null);
}

#[test]
fn updating_with_nothing_to_remove_sends_no_properties() {
    let f = fixture();
    f.day();

    {
        let _held = WRITE_LOCK.hold();
        let patch = Event { id: Some("ev1".into()), summary: Some("Dinner".into()), cleared: ["facts".to_string()].into_iter().collect(), ..Default::default() };
        f.client.update_event(&patch).unwrap();
    }

    assert!(f.requests("PATCH", "/events/ev1")[0].body.get("extendedProperties").is_none());
}

#[test]
fn delete_event_deletes_it() {
    let f = fixture();
    f.day();

    {
        let _held = WRITE_LOCK.hold();
        f.client.delete_event("ev1").unwrap();
    }

    assert_eq!(f.requests("DELETE", "/events/ev1").len(), 1);
    assert!(f.client.get_event("ev1").unwrap().is_cancelled());
}

// -- event labels ------------------------------------------------------------------------

#[test]
fn list_event_labels_returns_parsed_labels_and_etag() {
    let f = fixture();
    f.set_labels(json!([{"id": "l1", "backgroundColor": "#8e24aa", "name": "Design Work"}, {"id": "l2", "backgroundColor": "#d50000"}]));

    let (labels, etag) = f.client.list_event_labels().unwrap();

    let label_ids: Vec<Option<String>> = labels.iter().map(|l| l.id.clone()).collect();
    assert_eq!(label_ids, vec![Some("l1".to_string()), Some("l2".to_string())]);
    assert_eq!((labels[0].name.as_deref(), labels[1].name.as_deref()), (Some("Design Work"), None));
    assert_eq!(etag, Some(f.etag()));
}

#[test]
fn list_event_labels_is_empty_when_none_are_defined() {
    let f = fixture();

    assert!(f.client.list_event_labels().unwrap().0.is_empty());
}

#[test]
fn create_event_label_appends_it_and_returns_it() {
    let f = fixture();
    f.set_labels(json!([{"id": "l1", "backgroundColor": "#d50000"}]));
    let etag = f.etag();

    let created = {
        let _held = WRITE_LOCK.hold();
        f.client.create_event_label("#8e24aa", Some("Design Work")).unwrap()
    };

    assert!(created.id.is_some() && created.id.as_deref() != Some("l1"));
    assert_eq!((created.background_color.as_str(), created.name.as_deref()), ("#8e24aa", Some("Design Work")));
    let patch = &f.calendar_patches()[0];
    assert_eq!(patch.body, json!({"labelProperties": {"eventLabels": [{"id": "l1", "backgroundColor": "#d50000"}, {"backgroundColor": "#8e24aa", "name": "Design Work"}]}}));
    // Guarded by the etag it read.
    assert_eq!(patch.if_match, Some(etag));
}

#[test]
fn create_event_label_creates_the_first_one() {
    let f = fixture();

    let created = {
        let _held = WRITE_LOCK.hold();
        f.client.create_event_label("#8e24aa", None).unwrap()
    };

    assert!(created.id.is_some());
    assert_eq!(created.name, None);
}

#[test]
fn updating_or_deleting_a_label_that_isnt_there_is_refused() {
    let f = fixture();
    let _held = WRITE_LOCK.hold();

    assert!(f.client.update_event_label("missing", Some("#000000"), None).is_err());
    assert!(f.client.delete_event_label("missing").is_err());
    assert!(f.calendar_patches().is_empty());
}

#[test]
fn update_event_label_changes_only_what_it_is_given() {
    let f = fixture();
    f.set_labels(json!([{"id": "l1", "backgroundColor": "#d50000", "name": "Old"}]));
    let _held = WRITE_LOCK.hold();

    let recolored = f.client.update_event_label("l1", Some("#8e24aa"), None).unwrap();
    assert_eq!((recolored.background_color.as_str(), recolored.name.as_deref()), ("#8e24aa", Some("Old")));
    assert_eq!(f.calendar_patches()[0].body, json!({"labelProperties": {"eventLabels": [{"id": "l1", "backgroundColor": "#8e24aa", "name": "Old"}]}}));

    // Renaming keeps its color.
    let renamed = f.client.update_event_label("l1", None, Some("New")).unwrap();
    assert_eq!((renamed.background_color.as_str(), renamed.name.as_deref()), ("#8e24aa", Some("New")));
    assert_eq!(f.calendar_patches()[1].body, json!({"labelProperties": {"eventLabels": [{"id": "l1", "backgroundColor": "#8e24aa", "name": "New"}]}}));
}

#[test]
fn delete_event_label_removes_it_and_returns_it() {
    let f = fixture();
    f.set_labels(json!([{"id": "l1", "backgroundColor": "#8e24aa", "name": "Design Work"}, {"id": "l2", "backgroundColor": "#d50000"}]));

    let removed = {
        let _held = WRITE_LOCK.hold();
        f.client.delete_event_label("l1").unwrap()
    };

    assert_eq!((removed.id.as_deref(), removed.background_color.as_str(), removed.name.as_deref()), (Some("l1"), "#8e24aa", Some("Design Work")));
    assert_eq!(f.calendar_patches()[0].body, json!({"labelProperties": {"eventLabels": [{"id": "l2", "backgroundColor": "#d50000"}]}}));
}

#[test]
fn replace_event_labels_creates_updates_and_deletes_in_one_call() {
    // It doesn't fetch its own etag: the caller supplies one, since Actions
    // does other work between reading it and writing.
    let f = fixture();
    f.set_labels(json!([{"id": "l1", "backgroundColor": "#d50000", "name": "Old"}, {"id": "l2", "backgroundColor": "#000000"}]));
    let etag = f.etag();

    let updated = {
        let _held = WRITE_LOCK.hold();
        f.client
            .replace_event_labels(
                &[
                    EventLabel { id: Some("l1".into()), background_color: "#d50000".into(), name: Some("Renamed".into()) },
                    EventLabel { id: None, background_color: "#8e24aa".into(), name: Some("New".into()) },
                ],
                Some(&etag),
            )
            .unwrap()
    };

    assert_eq!(updated.len(), 2);
    assert_eq!(updated[0].id.as_deref(), Some("l1"));
    assert!(updated[1].id.is_some());
    assert!(f.fake.requests().iter().all(|r| r.method != "GET"));
    let patch = &f.calendar_patches()[0];
    assert_eq!(patch.body, json!({"labelProperties": {"eventLabels": [{"id": "l1", "backgroundColor": "#d50000", "name": "Renamed"}, {"backgroundColor": "#8e24aa", "name": "New"}]}}));
    assert_eq!(patch.if_match, Some(etag));
}

#[test]
fn replacing_with_no_labels_deletes_every_one_and_needs_no_etag() {
    let f = fixture();
    f.set_labels(json!([{"id": "l1", "backgroundColor": "#d50000"}]));

    let updated = {
        let _held = WRITE_LOCK.hold();
        f.client.replace_event_labels(&[], None).unwrap()
    };

    assert!(updated.is_empty());
    let patch = &f.calendar_patches()[0];
    assert_eq!(patch.body, json!({"labelProperties": {"eventLabels": []}}));
    assert_eq!(patch.if_match, None);
}

#[test]
fn a_stale_etag_is_a_conflict() {
    let f = fixture();
    let _held = WRITE_LOCK.hold();

    let error = f.client.replace_event_labels(&[], Some("\"stale\"")).unwrap_err();

    assert!(error.downcast_ref::<EventLabelConflictError>().is_some(), "{error:#}");
    assert!(is_label_conflict(&error));
}

#[test]
fn a_label_write_after_the_calendar_changed_is_a_conflict() {
    // Another write lands between the read of the labels and their write.
    let f = fixture();
    f.fake.with(|s| s.fail_next.push(("PATCH".into(), format!("/calendars/{}", f.calendar_id), 412, "Precondition Failed".into())));
    let _held = WRITE_LOCK.hold();

    let error = f.client.create_event_label("#8e24aa", None).unwrap_err();

    assert!(error.downcast_ref::<EventLabelConflictError>().is_some(), "{error:#}");
}

#[test]
fn other_errors_are_not_treated_as_conflicts() {
    let f = fixture();
    f.fake.with(|s| s.fail_next.push(("PATCH".into(), format!("/calendars/{}", f.calendar_id), 500, "Internal error.".into())));
    let _held = WRITE_LOCK.hold();

    let error = f.client.create_event_label("#8e24aa", None).unwrap_err();

    assert!(error.downcast_ref::<EventLabelConflictError>().is_none());
    assert_eq!(status_of(&error), Some(500));
}

// -- calendar metadata ------------------------------------------------------------------

#[test]
fn get_calendar_metadata_reads_markers_alongside_human_text() {
    assert_eq!(fixture().client.get_calendar_metadata("event-label-sheet-id").unwrap(), None);
    let f = fixture_with(Some("UTC"), Some("A calendar for time tracking."));
    assert_eq!(f.client.get_calendar_metadata("event-label-sheet-id").unwrap(), None);
    let f = fixture_with(Some("UTC"), Some("A calendar for time tracking.\n[cascading-time-tracker:event-label-sheet-id=sheet-1]"));
    assert_eq!(f.client.get_calendar_metadata("event-label-sheet-id").unwrap().as_deref(), Some("sheet-1"));
    let f = fixture_with(Some("UTC"), Some("[cascading-time-tracker:some-other-key=other-value]"));
    assert_eq!(f.client.get_calendar_metadata("event-label-sheet-id").unwrap(), None);
}

fn set_metadata(f: &Fixture, value: Option<&str>) {
    let _held = WRITE_LOCK.hold();
    f.client.set_calendar_metadata("event-label-sheet-id", value).unwrap();
}

#[test]
fn set_calendar_metadata_appends_a_marker_to_the_human_description() {
    let f = fixture();
    f.set_description("A calendar for time tracking.");
    let etag = f.etag();

    set_metadata(&f, Some("sheet-1"));

    assert_eq!(f.description().as_deref(), Some("A calendar for time tracking.\n[cascading-time-tracker:event-label-sheet-id=sheet-1]"));
    let patch = &f.calendar_patches()[0];
    assert_eq!(patch.body, json!({"description": "A calendar for time tracking.\n[cascading-time-tracker:event-label-sheet-id=sheet-1]"}));
    assert_eq!(patch.if_match, Some(etag));
}

#[test]
fn set_calendar_metadata_replaces_the_marker_for_the_same_key_and_leaves_others() {
    let f = fixture();
    f.set_description("[cascading-time-tracker:event-label-sheet-id=old-sheet]");
    set_metadata(&f, Some("new-sheet"));
    assert_eq!(f.description().as_deref(), Some("[cascading-time-tracker:event-label-sheet-id=new-sheet]"));

    let f = fixture();
    f.set_description("[cascading-time-tracker:some-other-key=other-value]");
    set_metadata(&f, Some("sheet-1"));
    assert_eq!(
        f.description().as_deref(),
        Some("[cascading-time-tracker:some-other-key=other-value]\n[cascading-time-tracker:event-label-sheet-id=sheet-1]")
    );
}

#[test]
fn set_calendar_metadata_to_none_removes_the_marker() {
    let f = fixture();
    f.set_description("A calendar for time tracking.\n[cascading-time-tracker:event-label-sheet-id=sheet-1]");

    set_metadata(&f, None);

    assert_eq!(f.calendar_patches()[0].body, json!({"description": "A calendar for time tracking."}));
}

#[test]
fn set_calendar_metadata_on_a_changed_calendar_is_a_conflict() {
    let f = fixture();
    f.fake.with(|s| s.fail_next.push(("PATCH".into(), format!("/calendars/{}", f.calendar_id), 412, "Precondition Failed".into())));
    let _held = WRITE_LOCK.hold();

    let error = f.client.set_calendar_metadata("event-label-sheet-id", Some("sheet-1")).unwrap_err();

    assert!(error.downcast_ref::<EventLabelConflictError>().is_some(), "{error:#}");
}

// -- side calendars and time zones ---------------------------------------------------------

#[test]
fn lists_raw_events_by_private_property_across_pages() {
    let f = fixture();
    for i in 0..9 {
        let mut body = event(Some(&format!("ev{i}")), &format!("2026-09-0{}T09:00:00+00:00", i + 1), &format!("2026-09-0{}T10:00:00+00:00", i + 1)).insert_body().unwrap();
        body.insert("extendedProperties".into(), json!({"private": {"k": if i % 3 == 0 { "other" } else { "v" }}}));
        f.fake.add_event(&f.calendar_id, Value::Object(body));
    }

    let items = f.client.list_event_resources(&at("2026-09-01T00:00:00+00:00"), &at("2026-10-01T00:00:00+00:00"), Some("k=v")).unwrap();

    assert_eq!(items.len(), 6);
    let pages = f.requests("GET", "/events");
    assert!(pages.iter().all(|r| query(r, "privateExtendedProperty") == Some("k=v")));
}

#[test]
fn the_time_zone_is_fetched_once() {
    let f = fixture_with(Some("Europe/Paris"), None);

    assert_eq!(f.client.get_time_zone().unwrap(), chrono_tz::Europe::Paris);
    assert_eq!(f.client.get_time_zone().unwrap(), chrono_tz::Europe::Paris);
    assert_eq!(f.requests("GET", &format!("/calendars/{}", f.calendar_id)).len(), 1);
}

#[test]
fn a_calendar_without_a_time_zone_is_refused_until_one_is_set() {
    let f = fixture_with(None, None);

    assert!(is_time_zone_not_set(&f.client.get_time_zone().unwrap_err()));
    // Not remembered: one set in Google Calendar since is picked up.
    {
        let _held = WRITE_LOCK.hold();
        f.client.patch_calendar(None, json!({"timeZone": "Europe/Paris"})).unwrap();
    }
    assert_eq!(f.client.get_time_zone().unwrap(), chrono_tz::Europe::Paris);
}

#[test]
fn sets_the_time_zone_and_remembers_it() {
    let f = fixture();
    assert_eq!(f.client.get_time_zone().unwrap(), chrono_tz::UTC);

    let set = {
        let _held = WRITE_LOCK.hold();
        f.client.set_time_zone("America/New_York").unwrap()
    };

    assert_eq!(set, chrono_tz::America::New_York);
    assert_eq!(f.calendar_patches()[0].body, json!({"timeZone": "America/New_York"}));
    assert_eq!(f.client.get_time_zone().unwrap(), chrono_tz::America::New_York);
    assert_eq!(f.requests("GET", &format!("/calendars/{}", f.calendar_id)).len(), 1);
}

#[test]
fn refuses_a_name_that_isnt_a_time_zone() {
    let f = fixture();
    let _held = WRITE_LOCK.hold();

    for name in ["Mars/Olympus_Mons", "america/new_york", "", "../etc/passwd"] {
        let error = f.client.set_time_zone(name).unwrap_err();
        assert!(error.to_string().contains("isn't a time zone"), "{name}: {error}");
    }

    assert!(f.calendar_patches().is_empty());
}

#[test]
fn creates_a_calendar_in_a_time_zone() {
    let f = fixture();

    let id = {
        let _held = WRITE_LOCK.hold();
        f.client.create_calendar("Compactions", Some("d"), Some("Europe/Paris")).unwrap()
    };

    assert_eq!(f.requests("POST", "/calendars")[0].body, json!({"summary": "Compactions", "description": "d", "timeZone": "Europe/Paris"}));
    assert_eq!(f.fake.calendar_resource(&id)["timeZone"], "Europe/Paris");
}

#[test]
fn hiding_a_calendar_is_best_effort() {
    let f = fixture();
    f.fake.with(|s| s.fail_next.push(("PATCH".into(), "/calendarList/".into(), 403, "insufficient scope".into())));
    let _held = WRITE_LOCK.hold();

    assert!(!f.client.hide_calendar("cal").unwrap());
    assert!(f.client.hide_calendar("cal").unwrap());
}

#[test]
fn for_calendar_shares_the_credentials() {
    let f = fixture();

    let other = f.client.for_calendar("other-cal");

    assert_eq!(other.calendar_id(), "other-cal");
    assert!(Arc::ptr_eq(other.google(), f.client.google()));
}

// -- cached listings -----------------------------------------------------------------------

fn cached<T>(f: impl FnOnce() -> T) -> T {
    let _listings = CachedListings::start();
    f()
}

#[test]
fn a_listing_within_an_earlier_one_is_answered_from_it() {
    let f = fixture();
    f.day();

    let events = cached(|| {
        f.client.list_events(&hour(0, 0), &hour(23, 0)).unwrap();
        f.client.list_events(&hour(10, 30), &hour(13, 0)).unwrap()
    });

    // Calendar's own rule: ends after the start, starts before the end.
    assert_eq!(ids(&events), vec!["ev2"]);
    assert_eq!(f.listings(), 1);
}

#[test]
fn an_edge_touching_the_range_is_left_out_as_calendar_does() {
    let f = fixture();
    f.day();

    let events = cached(|| {
        f.client.list_events(&hour(0, 0), &hour(23, 0)).unwrap();
        f.client.list_events(&hour(10, 0), &hour(11, 0)).unwrap()
    });

    assert!(events.is_empty());
}

#[test]
fn a_listing_past_the_earlier_one_lists_again() {
    let f = fixture();
    f.day();

    cached(|| {
        f.client.list_events(&hour(8, 0), &hour(12, 0)).unwrap();
        f.client.list_events(&hour(8, 0), &hour(14, 0)).unwrap();
    });

    assert_eq!(f.listings(), 2);
}

#[test]
fn another_calendar_lists_its_own() {
    let f = fixture();
    f.day();
    let other = f.fake.add_calendar(Some("UTC"));

    cached(|| {
        f.client.list_events(&hour(0, 0), &hour(23, 0)).unwrap();
        f.client.for_calendar(&other).list_events(&hour(0, 0), &hour(23, 0)).unwrap();
    });

    assert_eq!(f.listings(), 2);
}

#[test]
fn what_a_cached_listing_answers_is_a_copy() {
    let f = fixture();
    f.day();

    let events = cached(|| {
        f.client.list_events(&hour(0, 0), &hour(23, 0)).unwrap()[0].summary = Some("changed".into());
        f.client.list_events(&hour(0, 0), &hour(23, 0)).unwrap()
    });

    assert!(events.iter().all(|e| e.summary.as_deref() == Some("Busy")));
}

#[test]
fn any_write_forgets_every_listing() {
    for delete_resource in [true, false] {
        let f = fixture();
        f.day();
        let _held = WRITE_LOCK.hold();

        cached(|| {
            f.client.list_events(&hour(0, 0), &hour(23, 0)).unwrap();
            if delete_resource {
                f.client.delete_event_resource("ev1").unwrap();
            } else {
                f.client.delete_event("ev1").unwrap();
            }
            f.client.list_events(&hour(0, 0), &hour(23, 0)).unwrap();
        });

        assert_eq!(f.listings(), 2);
    }
}

#[test]
fn nothing_is_kept_outside_the_block_or_after_it() {
    let f = fixture();
    f.day();

    f.client.list_events(&hour(0, 0), &hour(23, 0)).unwrap();
    cached(|| f.client.list_events(&hour(0, 0), &hour(23, 0)).unwrap());
    f.client.list_events(&hour(0, 0), &hour(23, 0)).unwrap();

    assert_eq!(f.listings(), 3);
}

#[test]
fn a_nested_block_shares_the_outer_ones() {
    let f = fixture();
    f.day();

    cached(|| {
        f.client.list_events(&hour(0, 0), &hour(23, 0)).unwrap();
        cached(|| f.client.list_events(&hour(9, 0), &hour(10, 0)).unwrap());
    });

    assert_eq!(f.listings(), 1);
}

// -- the write lock ---------------------------------------------------------------------------

#[test]
fn a_write_refuses_without_the_write_lock_and_sends_nothing() {
    let f = fixture();
    f.day();
    let label = EventLabel { id: None, background_color: "#000000".into(), name: None };
    let one = event(Some("evnew"), "2026-01-01T09:00:00+00:00", "2026-01-01T10:00:00+00:00");
    let writes: Vec<(&str, anyhow::Result<()>)> = vec![
        ("create_calendar", f.client.create_calendar("x", None, None).map(drop)),
        ("hide_calendar", f.client.hide_calendar("x").map(drop)),
        ("set_time_zone", f.client.set_time_zone("UTC").map(drop)),
        ("replace_event_resource", f.client.replace_event_resource("ev1", &Map::new()).map(drop)),
        ("delete_event_resource", f.client.delete_event_resource("ev1")),
        ("create_event", f.client.create_event(&one).map(drop)),
        ("import_event", f.client.import_event(&one, "uid@google.com").map(drop)),
        ("update_event", f.client.update_event(&one).map(drop)),
        ("delete_event", f.client.delete_event("ev1")),
        ("create_event_label", f.client.create_event_label("#000000", None).map(drop)),
        ("update_event_label", f.client.update_event_label("l1", None, None).map(drop)),
        ("delete_event_label", f.client.delete_event_label("l1").map(drop)),
        ("replace_event_labels", f.client.replace_event_labels(&[label], None).map(drop)),
        ("set_calendar_metadata", f.client.set_calendar_metadata("k", Some("v"))),
        ("patch_calendar", f.client.patch_calendar(None, json!({})).map(drop)),
    ];

    for (name, result) in writes {
        let error = result.expect_err(name);
        assert!(error.to_string().contains("must hold WRITE_LOCK"), "{name}: {error:#}");
    }
    assert!(f.fake.requests().is_empty());
}

#[test]
fn a_read_needs_no_lock() {
    let f = fixture();
    f.day();

    assert_eq!(f.client.get_event("ev1").unwrap().id.as_deref(), Some("ev1"));
}
