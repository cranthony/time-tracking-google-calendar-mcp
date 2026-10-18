//! The module's tests, against the fake Google's series.

use super::*;
use crate::testing::app::{test_app, TestApp};
use crate::testing::fake_google::LoggedRequest;
use crate::write_lock::WRITE_LOCK;
use chrono::TimeZone;

const NY: Tz = chrono_tz::America::New_York;

fn at(day: u32, hour: u32) -> Moment {
    at_in(10, day, hour)
}

fn at_in(month: u32, day: u32, hour: u32) -> Moment {
    NY.with_ymd_and_hms(2026, month, day, hour, 0, 0).unwrap().fixed_offset()
}

/// An instance of `series` at 09:00 New York (13:00 UTC) on October `day`.
fn instance(series: &str, day: u32) -> String {
    format!("{series}_202610{day:02}T130000Z")
}

fn strings(items: &[&str]) -> Vec<String> {
    crate::testing::strings(items)
}

/// Every Monday 09:00-10:00 from Mon Oct 5, 2026.
fn weekly(rule: &str) -> Event {
    Event {
        id: Some("series1".into()),
        summary: Some("Standup".into()),
        start: Some(at(5, 9)),
        end: Some(at(5, 10)),
        time_zone: Some("America/New_York".into()),
        recurrence: Some(vec![rule.into()]),
        action_ids: Some(strings(&["work"])),
        ..Default::default()
    }
}

const MONDAYS: &str = "RRULE:FREQ=WEEKLY;BYDAY=MO";

struct Fixture {
    t: TestApp,
    recurrences: Recurrences,
}

fn fixture(series: &[Event]) -> Fixture {
    let t = test_app();
    for event in series {
        t.seed_event(event);
    }
    let recurrences = Recurrences::new(ActionCalendar::new(t.app.calendar_client().unwrap(), t.app.actions().unwrap()));
    t.fake.clear_requests();
    Fixture { t, recurrences }
}

impl Fixture {
    fn held<T>(&self, f: impl FnOnce(&Recurrences) -> T) -> T {
        let _held = WRITE_LOCK.hold();
        f(&self.recurrences)
    }

    fn event(&self, id: &str) -> Event {
        self.t.app.calendar_client().unwrap().get_event(id).unwrap()
    }

    fn writes(&self) -> Vec<LoggedRequest> {
        self.t.fake.requests().into_iter().filter(|r| r.method != "GET" && r.path.contains("/events")).collect()
    }

    /// Move an instance, as the user might in Calendar.
    fn move_instance(&self, id: &str, to: Moment) {
        let _held = WRITE_LOCK.hold();
        let client = self.t.app.calendar_client().unwrap();
        client.update_event(&Event { id: Some(id.into()), start: Some(to), end: Some(to + chrono::Duration::hours(1)), ..Default::default() }).unwrap();
        self.t.fake.clear_requests();
    }

    fn fail_updates_of(&self, id: &str, times: usize) {
        self.t.fake.with(|s| {
            for _ in 0..times {
                s.fail_next.push(("PATCH".into(), format!("/events/{id}"), 400, "Bad Request".into()));
            }
        });
    }
}

fn refused_with<T>(result: anyhow::Result<T>, text: &str) -> anyhow::Error {
    let Err(error) = result else { panic!("{text:?} wasn't refused") };
    assert!(error.to_string().contains(text) || regex::Regex::new(text).is_ok_and(|r| r.is_match(&error.to_string())), "{text:?} isn't in: {error}");
    error
}

fn repeat(every: &str) -> Repeat {
    Repeat {
        every: every.into(),
        interval: 1,
        weekdays: None,
        nth_weekdays: None,
        month_days: None,
        months: None,
        count: None,
        until: None,
        week_starts_on: None,
        skipped: None,
        added: None,
    }
}

fn days(names: &[&str]) -> Option<Vec<String>> {
    Some(strings(names))
}

fn date(y: i32, m: u32, d: u32) -> Option<Until> {
    Some(Until::Date(NaiveDate::from_ymd_opt(y, m, d).unwrap()))
}

// -- finding a series -----------------------------------------------------------------

#[test]
fn finds_the_series_from_any_of_its_events() {
    let f = fixture(&[weekly(MONDAYS)]);

    assert_eq!(f.recurrences.series(&instance("series1", 12)).unwrap().id.as_deref(), Some("series1"));
    assert_eq!(f.recurrences.series("series1").unwrap().id.as_deref(), Some("series1"));
}

#[test]
fn refuses_a_single_event() {
    let lone = Event { id: Some("lone".into()), summary: Some("Lunch".into()), start: Some(at(5, 12)), end: Some(at(5, 13)), ..Default::default() };
    let f = fixture(&[lone]);

    let error = refused_with(f.recurrences.series("lone"), "isn't part of a recurring series");

    assert!(is_refusal(&error));
}

// -- updating --------------------------------------------------------------------------

#[test]
fn edits_the_whole_series_through_one_of_its_events() {
    let f = fixture(&[weekly(MONDAYS)]);

    let updated = f.held(|r| r.update(&Event { id: Some(instance("series1", 12)), summary: Some("Team standup".into()), ..Default::default() }, None, None)).unwrap();

    assert_eq!(updated.len(), 1);
    assert_eq!(updated[0].id.as_deref(), Some("series1"));
    let series = f.event("series1");
    assert_eq!(series.summary.as_deref(), Some("Team standup"));
    assert_eq!(series.recurrence, Some(strings(&[MONDAYS])));
}

#[test]
fn new_times_carry_the_series_time_zone() {
    let f = fixture(&[weekly(MONDAYS)]);

    f.held(|r| r.update(&Event { id: Some("series1".into()), start: Some(at(5, 10)), end: Some(at(5, 11)), ..Default::default() }, None, None)).unwrap();

    let writes = f.writes();
    assert_eq!(writes.len(), 1);
    assert_eq!(writes[0].body["start"]["timeZone"], "America/New_York");
}

#[test]
fn this_and_following_splits_then_edits_only_the_later_part() {
    let f = fixture(&[weekly(MONDAYS)]);

    let parts = f
        .held(|r| r.update(&Event { id: Some("series1".into()), summary: Some("Team standup".into()), start: Some(at(5, 10)), end: Some(at(5, 11)), ..Default::default() }, Some(&instance("series1", 19)), None))
        .unwrap();

    let (later, earlier) = (&parts[0], &parts[1]);
    assert_eq!(earlier.id.as_deref(), Some("series1"));
    assert_eq!(earlier.summary.as_deref(), Some("Standup"));
    assert_eq!(earlier.recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z"])));
    assert_eq!(later.summary.as_deref(), Some("Team standup"));
    // Moved an hour later, from the event it was split at.
    assert_eq!((later.start, later.end), (Some(at(19, 10)), Some(at(19, 11))));
    assert_eq!(later.action_ids, Some(strings(&["work"])));
}

#[test]
fn clears_fields_on_the_series_keeping_the_rest() {
    let f = fixture(&[Event { priority: Some(1), location: Some("Room 4".into()), ..weekly(MONDAYS) }]);

    let updated = f
        .held(|r| r.update(&Event { id: Some("series1".into()), summary: Some("Team standup".into()), cleared: ["priority".to_string()].into_iter().collect(), ..Default::default() }, None, None))
        .unwrap();

    assert_eq!((updated[0].summary.as_deref(), updated[0].priority, updated[0].location.as_deref()), (Some("Team standup"), None, Some("Room 4")));
}

#[test]
fn writes_facts_to_the_series() {
    let f = fixture(&[weekly(MONDAYS)]);
    let facts = crate::testing::facts(json!({"with": ["sam"]}));

    let updated = f.held(|r| r.update(&Event { id: Some("series1".into()), facts: Some(facts.clone()), ..Default::default() }, None, None)).unwrap();

    assert_eq!(updated[0].facts.as_ref().map(|f| f.to_public()), Some(facts.to_public()));
}

#[test]
fn this_and_following_clears_fields_on_the_later_part_only() {
    let f = fixture(&[Event { priority: Some(1), ..weekly(MONDAYS) }]);

    let parts = f
        .held(|r| r.update(&Event { id: Some("series1".into()), cleared: ["priority".to_string()].into_iter().collect(), ..Default::default() }, Some(&instance("series1", 19)), None))
        .unwrap();

    assert_eq!((parts[0].priority, parts[1].priority), (None, Some(1)));
}

#[test]
fn this_and_following_refuses_an_event_from_another_series() {
    let f = fixture(&[weekly(MONDAYS), Event { id: Some("other".into()), ..weekly(MONDAYS) }]);

    refused_with(
        f.held(|r| r.update(&Event { id: Some("series1".into()), summary: Some("x".into()), ..Default::default() }, Some(&instance("other", 12)), None)),
        "isn't one of series series1's events",
    );
    refused_with(f.held(|r| r.delete("series1", Some(&instance("other", 12)))), "isn't one of series series1's events");

    assert!(f.writes().is_empty());
}

#[test]
fn writes_a_repeat_as_rules_in_the_series_time_zone() {
    let f = fixture(&[weekly(MONDAYS)]);
    let given = Repeat { weekdays: days(&["mon", "wed"]), until: date(2026, 12, 31), ..repeat("week") };

    f.held(|r| r.update(&Event { id: Some("series1".into()), ..Default::default() }, None, Some(&given))).unwrap();

    // Through the end of Dec 31 in New York, which is Jan 1 in UTC.
    assert_eq!(f.event("series1").recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20270101T045959Z"])));
}

#[test]
fn this_and_following_writes_the_repeat_to_the_later_part() {
    let f = fixture(&[weekly(MONDAYS)]);
    let given = Repeat { weekdays: days(&["tue"]), ..repeat("week") };

    let parts = f.held(|r| r.update(&Event { id: Some("series1".into()), ..Default::default() }, Some(&instance("series1", 19)), Some(&given))).unwrap();

    assert_eq!(parts[0].recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=TU"])));
    assert_eq!(parts[1].recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z"])));
}

#[test]
fn refuses_a_bad_repeat_without_writing() {
    let f = fixture(&[weekly(MONDAYS)]);
    let given = Repeat { count: Some(3), until: date(2026, 12, 31), ..repeat("week") };

    refused_with(f.held(|r| r.update(&Event { id: Some("series1".into()), ..Default::default() }, Some(&instance("series1", 19)), Some(&given))), "count or until");

    assert!(f.writes().is_empty());
}

// -- deleting ------------------------------------------------------------------------------

#[test]
fn cancels_the_whole_series_through_one_of_its_events() {
    let f = fixture(&[weekly(MONDAYS)]);

    assert!(f.held(|r| r.delete(&instance("series1", 12), None)).unwrap().is_none());

    let writes = f.writes();
    assert_eq!(writes.len(), 1);
    assert!(writes[0].path.ends_with("/events/series1"));
    assert_eq!(writes[0].body, json!({"status": "cancelled"}));
}

#[test]
fn this_and_following_ends_the_series_before_the_event() {
    let f = fixture(&[weekly(MONDAYS)]);

    let left = f.held(|r| r.delete("series1", Some(&instance("series1", 19)))).unwrap().unwrap();

    assert_eq!(left.recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z"])));
    assert_ne!(left.status.as_deref(), Some("cancelled"));
    // No copy is made.
    assert!(f.writes().iter().all(|w| w.method == "PATCH"));
}

#[test]
fn this_and_following_replaces_a_count_and_keeps_other_rules() {
    let series = Event {
        recurrence: Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=10", "EXDATE;TZID=America/New_York:20261012T090000"])),
        ..weekly(MONDAYS)
    };
    let f = fixture(&[series]);

    let left = f.held(|r| r.delete("series1", Some(&instance("series1", 19)))).unwrap().unwrap();

    assert_eq!(left.recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z", "EXDATE;TZID=America/New_York:20261012T090000"])));
}

#[test]
fn this_and_following_ends_a_moved_event_where_the_series_put_it() {
    let f = fixture(&[weekly(MONDAYS)]);
    f.move_instance(&instance("series1", 19), at(18, 15));

    let left = f.held(|r| r.delete("series1", Some(&instance("series1", 19)))).unwrap().unwrap();

    assert_eq!(left.recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z"])));
}

#[test]
fn this_and_following_from_the_first_event_cancels_the_series() {
    let f = fixture(&[weekly(MONDAYS)]);

    assert!(f.held(|r| r.delete("series1", Some(&instance("series1", 5)))).unwrap().is_none());

    assert_eq!(f.event("series1").status.as_deref(), Some("cancelled"));
}

// -- splitting --------------------------------------------------------------------------------

#[test]
fn split_ends_the_series_before_the_event_and_starts_a_copy_at_it() {
    let f = fixture(&[weekly(MONDAYS)]);

    let (earlier, later) = f.held(|r| r.split(&instance("series1", 19))).unwrap();

    assert_eq!(earlier.unwrap().recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z"])));
    assert_eq!(later.id, Some(split_series_id("series1", &at(19, 9))));
    assert_eq!((later.start, later.end, later.time_zone.as_deref()), (Some(at(19, 9)), Some(at(19, 10)), Some("America/New_York")));
    assert_eq!(later.recurrence, Some(strings(&[MONDAYS])));
    assert_eq!(later.summary.as_deref(), Some("Standup"));
}

#[test]
fn a_moved_event_splits_where_the_series_put_it() {
    let f = fixture(&[weekly(MONDAYS)]);
    f.move_instance(&instance("series1", 19), at(20, 15));

    let (_, later) = f.held(|r| r.split(&instance("series1", 19))).unwrap();

    assert_eq!(later.start, Some(at(19, 9)));
}

#[test]
fn shares_a_count_between_the_two() {
    let f = fixture(&[weekly("RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=10")]);

    let (earlier, later) = f.held(|r| r.split(&instance("series1", 19))).unwrap();

    assert_eq!(earlier.unwrap().recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z"])));
    assert_eq!(later.recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=8"])));
}

#[test]
fn keeps_an_until_and_copies_exceptions() {
    let rules = strings(&["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261231T235959Z", "EXDATE;TZID=America/New_York:20261109T090000"]);
    let f = fixture(&[Event { recurrence: Some(rules.clone()), ..weekly(MONDAYS) }]);

    let (earlier, later) = f.held(|r| r.split(&instance("series1", 19))).unwrap();

    assert_eq!(later.recurrence, Some(rules));
    assert_eq!(earlier.unwrap().recurrence.unwrap()[1], "EXDATE;TZID=America/New_York:20261109T090000");
}

#[test]
fn the_first_event_leaves_nothing_to_split() {
    let f = fixture(&[weekly(MONDAYS)]);

    let (earlier, later) = f.held(|r| r.split(&instance("series1", 5))).unwrap();

    assert!(earlier.is_none());
    assert_eq!(later.id.as_deref(), Some("series1"));
    assert!(f.writes().is_empty());
}

#[test]
fn a_retried_split_finds_the_series_it_already_made() {
    let f = fixture(&[weekly(MONDAYS)]);
    let (_, first) = f.held(|r| r.split(&instance("series1", 19))).unwrap();
    // As if the trim had failed: the old series is whole again.
    {
        let _held = WRITE_LOCK.hold();
        f.t.app.calendar_client().unwrap().update_event(&Event { id: Some("series1".into()), recurrence: Some(strings(&[MONDAYS])), ..Default::default() }).unwrap();
    }
    f.t.fake.clear_requests();

    let (earlier, again) = f.held(|r| r.split(&instance("series1", 19))).unwrap();

    assert_eq!(again.id, first.id);
    assert_eq!(f.writes().iter().filter(|w| w.method == "POST").count(), 1); // the retry, refused as there
    assert_eq!(earlier.unwrap().recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z"])));
}

#[test]
fn a_series_that_cant_be_ended_cancels_its_copy_again() {
    let f = fixture(&[weekly(MONDAYS)]);
    f.fail_updates_of("series1", 1);

    let error = refused_with(f.held(|r| r.split(&instance("series1", 19))), "wasn't split .*Bad Request");

    assert!(error.downcast_ref::<SplitError>().is_some());
    assert_eq!(f.event(&split_series_id("series1", &at(19, 9))).status.as_deref(), Some("cancelled"));
    assert_eq!(f.event("series1").recurrence, Some(strings(&[MONDAYS])));
}

#[test]
fn a_copy_that_cant_be_cancelled_either_is_named_to_cancel_by_hand() {
    let f = fixture(&[weekly(MONDAYS)]);
    let copy = split_series_id("series1", &at(19, 9));
    f.fail_updates_of("series1", 1);
    f.fail_updates_of(&copy, 1);

    refused_with(f.held(|r| r.split(&instance("series1", 19))), &format!("cancel that copy (delete_recurrence {copy})"));
}

#[test]
fn a_split_retried_after_undoing_itself_restores_its_copy() {
    let f = fixture(&[weekly(MONDAYS)]);
    f.fail_updates_of("series1", 1);
    assert!(f.held(|r| r.split(&instance("series1", 19))).is_err());

    let (earlier, later) = f.held(|r| r.split(&instance("series1", 19))).unwrap();

    assert_eq!(later.status.as_deref(), Some("confirmed"));
    assert_eq!(f.event(later.id.as_deref().unwrap()).status.as_deref(), Some("confirmed"));
    assert_eq!(earlier.unwrap().recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261019T125959Z"])));
}

#[test]
fn this_and_following_edits_nothing_when_the_split_fails() {
    let f = fixture(&[weekly(MONDAYS)]);
    f.fail_updates_of("series1", 1);

    let error = f.held(|r| r.update(&Event { id: Some("series1".into()), summary: Some("Team standup".into()), ..Default::default() }, Some(&instance("series1", 19)), None)).unwrap_err();

    assert!(error.downcast_ref::<SplitError>().is_some(), "{error}");
    let copy = f.event(&split_series_id("series1", &at(19, 9)));
    assert_eq!((copy.status.as_deref(), copy.summary.as_deref()), (Some("cancelled"), Some("Standup")));
    assert_eq!(f.event("series1").summary.as_deref(), Some("Standup"));
}

// -- series Google Calendar split itself ---------------------------------------------------------
// Google won't change their rules -- see the module docstring.

const GOOGLE_SPLIT: &str = "abc_R20261012T130000";

/// Every Monday 09:00-10:00 from Mon Oct 12, 2026: split by Google
/// Calendar from series abc, which ran until then.
fn split_by_google(rule: &str) -> Vec<Event> {
    vec![
        Event { id: Some("abc".into()), recurrence: Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261012T035959Z"])), ..weekly(MONDAYS) },
        Event { id: Some(GOOGLE_SPLIT.into()), start: Some(at(12, 9)), end: Some(at(12, 10)), ..weekly(rule) },
    ]
}

#[test]
fn split_ends_a_google_split_series_as_google_would_and_cancels_the_rest() {
    let f = fixture(&split_by_google(MONDAYS));

    let (earlier, later) = f.held(|r| r.split(&instance(GOOGLE_SPLIT, 26))).unwrap();

    // At the local midnight before Oct 26.
    let earlier = earlier.unwrap();
    assert_eq!(earlier.recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261026T035959Z"])));
    assert_eq!(later.id, Some(split_series_id(GOOGLE_SPLIT, &at(26, 9))));
    assert_eq!((later.start, later.recurrence.clone()), (Some(at(26, 9)), Some(strings(&[MONDAYS]))));
    assert_eq!(f.event("abc_R20261026T130000").status.as_deref(), Some("cancelled"));
    assert!(!f.writes().iter().any(|w| w.method == "PATCH" && w.path.ends_with(&format!("/events/{GOOGLE_SPLIT}")) && w.body.get("recurrence").is_some()));
}

#[test]
fn this_and_following_delete_ends_a_google_split_series_as_google_would() {
    let f = fixture(&split_by_google(MONDAYS));

    let left = f.held(|r| r.delete(GOOGLE_SPLIT, Some(&instance(GOOGLE_SPLIT, 26)))).unwrap().unwrap();

    assert_eq!(left.recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20261026T035959Z"])));
    assert_eq!(f.event("abc_R20261026T130000").status.as_deref(), Some("cancelled"));
}

#[test]
fn a_failed_end_cancels_the_copy_again() {
    let f = fixture(&split_by_google(MONDAYS));
    f.fail_updates_of("abc_R20261026T130000", 1); // cancelling the rest

    refused_with(f.held(|r| r.split(&instance(GOOGLE_SPLIT, 26))), "wasn't split");

    assert_eq!(f.event(&split_series_id(GOOGLE_SPLIT, &at(26, 9))).status.as_deref(), Some("cancelled"));
}

#[test]
fn an_import_that_doesnt_end_it_undoes_the_split() {
    let f = fixture(&split_by_google(MONDAYS));
    f.t.fake.with(|s| s.imports_dont_split = true);

    refused_with(f.held(|r| r.split(&instance(GOOGLE_SPLIT, 26))), &format!("wasn't split .*didn't end series {GOOGLE_SPLIT}"));

    assert_eq!(f.event(&split_series_id(GOOGLE_SPLIT, &at(26, 9))).status.as_deref(), Some("cancelled"));
    assert_eq!(f.event("abc_R20261026T130000").status.as_deref(), Some("cancelled"));
    assert_eq!(f.event(GOOGLE_SPLIT).recurrence, Some(strings(&[MONDAYS])));
}

#[test]
fn a_delete_whose_import_doesnt_end_it_says_so() {
    let f = fixture(&split_by_google(MONDAYS));
    f.t.fake.with(|s| s.imports_dont_split = true);

    refused_with(f.held(|r| r.delete(GOOGLE_SPLIT, Some(&instance(GOOGLE_SPLIT, 26)))), "didn't end series");
}

#[test]
fn refuses_to_change_a_google_split_series_rules_whole_or_move_its_start() {
    let f = fixture(&split_by_google(MONDAYS));

    refused_with(f.held(|r| r.update(&Event { id: Some(GOOGLE_SPLIT.into()), ..Default::default() }, None, Some(&repeat("week")))), "won't change its rules");
    refused_with(
        f.held(|r| r.update(&Event { id: Some(GOOGLE_SPLIT.into()), start: Some(at(12, 10)), end: Some(at(12, 11)), ..Default::default() }, None, None)),
        "won't move its start",
    );

    assert!(f.writes().is_empty());
}

#[test]
fn a_google_split_series_other_fields_and_end_can_still_be_edited() {
    let f = fixture(&split_by_google(MONDAYS));

    f.held(|r| r.update(&Event { id: Some(GOOGLE_SPLIT.into()), summary: Some("Sync".into()), start: Some(at(12, 9)), end: Some(at(12, 11)), ..Default::default() }, None, None)).unwrap();

    let series = f.event(GOOGLE_SPLIT);
    assert_eq!((series.summary.as_deref(), series.end), (Some("Sync"), Some(at(12, 11))));
}

#[test]
fn a_google_split_series_rules_can_change_from_a_later_event_on() {
    let f = fixture(&split_by_google(MONDAYS));
    let given = Repeat { weekdays: days(&["tue"]), ..repeat("week") };

    let parts = f.held(|r| r.update(&Event { id: Some(GOOGLE_SPLIT.into()), ..Default::default() }, Some(&instance(GOOGLE_SPLIT, 26)), Some(&given))).unwrap();

    assert_eq!(parts[0].recurrence, Some(strings(&["RRULE:FREQ=WEEKLY;BYDAY=TU"])));
}

#[test]
fn refuses_to_split_a_google_split_series_with_a_count() {
    let f = fixture(&split_by_google("RRULE:FREQ=WEEKLY;BYDAY=MO;COUNT=10"));

    refused_with(f.held(|r| r.split(&instance(GOOGLE_SPLIT, 26))), "ends after a count");

    assert!(f.writes().is_empty());
}

// -- renewing series before Google stops expanding them -------------------------------------------

/// 08:00 New York on Sunday, October 18, 2026.
fn renewal_now() -> Moment {
    at(18, 8)
}

/// 09:00 New York, `days` days before October 18, 2026.
fn nine_days_before(days: i64) -> Moment {
    let day = NaiveDate::from_ymd_opt(2026, 10, 18).unwrap() - Duration::days(days);
    NY.from_local_datetime(&day.and_hms_opt(9, 0, 0).unwrap()).unwrap().fixed_offset()
}

/// A 09:00-10:00 series `id` begun `days` days ago, by `rule`.
fn begun(id: &str, days: i64, rule: &str) -> Event {
    let start = nine_days_before(days);
    Event { id: Some(id.into()), start: Some(start), end: Some(start + Duration::hours(1)), recurrence: Some(vec![rule.into()]), ..weekly(rule) }
}

/// The events listed over `days` days from `from`, in series `series`.
fn listed_in(f: &Fixture, series: &str, from: Moment, days: i64) -> Vec<Event> {
    let client = f.t.app.calendar_client().unwrap();
    client.list_events(&from, &(from + Duration::days(days))).unwrap().into_iter().filter(|e| e.recurring_event_id.as_deref() == Some(series)).collect()
}

#[test]
fn google_lists_no_instances_of_a_series_past_its_730th() {
    let f = fixture(&[begun("old", 1000, "RRULE:FREQ=DAILY")]);
    let client = f.t.app.calendar_client().unwrap();

    assert_eq!(listed_in(&f, "old", renewal_now(), 30).len(), 0);
    assert_eq!(client.instances_from("old", &renewal_now()).unwrap().len(), 0);
    assert!(client.get_event("old_20261019T130000Z").is_err());
    let all = client.list_instances("old", &renewal_now()).unwrap();
    assert_eq!((all.len(), all.last().unwrap().start()), (INSTANCE_CAP, nine_days_before(1000 - 729)));
    assert_eq!(client.list_series(&renewal_now(), None).unwrap().len(), 0);
}

#[test]
fn renew_splits_a_series_at_its_730th_instance_so_it_goes_on() {
    // Its 730th instance is 129 days from now.
    let f = fixture(&[begun("daily", 600, "RRULE:FREQ=DAILY")]);
    let last = nine_days_before(600 - 729);

    let renewed = f.held(|r| r.renew(&renewal_now())).unwrap();

    assert_eq!(renewed.len(), 1);
    let later = &renewed[0];
    assert_eq!((later.id.clone(), later.start), (Some(split_series_id("daily", &last)), Some(last)));
    assert_eq!((later.recurrence.clone(), later.action_ids.clone()), (Some(strings(&["RRULE:FREQ=DAILY"])), Some(strings(&["work"]))));
    assert!(ends_before(&f.event("daily"), &last));
    let later_id = later.id.clone().unwrap();
    assert_eq!(listed_in(&f, "daily", renewal_now(), 7).len(), 7);
    // Past what Google expands of the series it was, it goes on.
    let past = renewal_now() + Duration::days(400);
    assert_eq!(listed_in(&f, &later_id, past, 7).len(), 7);
    assert_eq!(listed_in(&f, "daily", past, 7).len(), 0);
    // Renewed, it's left alone.
    f.t.fake.clear_requests();
    assert_eq!(f.held(|r| r.renew(&renewal_now())).unwrap().len(), 0);
    assert!(f.writes().is_empty());
}

#[test]
fn renew_leaves_series_that_go_on_or_end_by_their_own_rules() {
    let f = fixture(&[
        begun("young", 10, "RRULE:FREQ=DAILY"),
        begun("weekly", 1000, MONDAYS),
        begun("until", 600, "RRULE:FREQ=DAILY;UNTIL=20261117T235959Z"),
        begun("counted", 600, "RRULE:FREQ=DAILY;COUNT=700"),
    ]);

    let renewed = f.held(|r| r.renew(&renewal_now())).unwrap();

    assert!(renewed.is_empty());
    assert!(f.writes().is_empty());
    // A listing of the series alive now, and of those alive past the horizon.
    assert_eq!(f.t.fake.quota_counts(), [0, 0, 2, 0]);
}

// -- ids, words and rules -----------------------------------------------------------------------

#[test]
fn split_series_ids_are_valid_calendar_ids() {
    let id = split_series_id("series1", &at(19, 9));

    assert_eq!(id, split_series_id("series1", &at(19, 9)));
    assert_ne!(id, split_series_id("series1", &at(26, 9)));
    assert!((5..=1024).contains(&id.len()) && id.chars().all(|c| "0123456789abcdefghijklmnopqrstuv".contains(c)));
}

#[test]
fn describes_rules_in_words() {
    let cases: [(&[&str], &str); 10] = [
        (&["RRULE:FREQ=WEEKLY;BYDAY=MO,WE"], "Every week on Mon, Wed"),
        (&["RRULE:FREQ=DAILY;INTERVAL=2;COUNT=10"], "Every 2 days, 10 times"),
        (&["RRULE:FREQ=MONTHLY;BYDAY=-1FR"], "Every month on the last Fri"),
        (&["RRULE:FREQ=MONTHLY;BYMONTHDAY=15"], "Every month on day 15"),
        (&["RRULE:FREQ=WEEKLY;UNTIL=20261231T235959Z"], "Every week, until Dec 31, 2026"),
        (&["RRULE:FREQ=WEEKLY", "EXDATE:20261012T130000Z"], "Every week, with exceptions"),
        (&["RRULE:FREQ=MONTHLY;BYMONTHDAY=1,-1"], "Every month on day 1 and the last day"),
        (&["RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=4TH"], "Every year on the fourth Thu in Nov"),
        (&["RRULE:FREQ=WEEKLY", "RDATE:20261021T180000Z"], "Every week, plus 1 added date"),
        (&["RRULE:FREQ=HOURLY"], "RRULE:FREQ=HOURLY"),
    ];

    for (rules, phrase) in cases {
        assert_eq!(describe_rules(Some(&strings(rules)), Some(&NY)).as_deref(), Some(phrase), "{rules:?}");
    }
}

fn round_trip(given: Repeat, rules: &[&str]) {
    assert_eq!(given.to_rules(&NY).unwrap(), strings(rules));
    assert_eq!(Repeat::from_rules(Some(&strings(rules)), &NY), Some(given), "{rules:?}");
}

#[test]
fn a_repeat_round_trips_through_rules() {
    round_trip(repeat("day"), &["RRULE:FREQ=DAILY"]);
    round_trip(
        Repeat { interval: 2, weekdays: days(&["tue", "thu"]), week_starts_on: Some("sun".into()), ..repeat("week") },
        &["RRULE:FREQ=WEEKLY;INTERVAL=2;BYDAY=TU,TH;WKST=SU"],
    );
    round_trip(Repeat { month_days: Some(vec![1, -1]), count: Some(12), ..repeat("month") }, &["RRULE:FREQ=MONTHLY;BYMONTHDAY=1,-1;COUNT=12"]);
    round_trip(Repeat { nth_weekdays: Some(vec![(-1, "fri".into())]), ..repeat("month") }, &["RRULE:FREQ=MONTHLY;BYDAY=-1FR"]);
    round_trip(Repeat { months: Some(vec![11]), nth_weekdays: Some(vec![(4, "thu".into())]), ..repeat("year") }, &["RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=4TH"]);
    // As a split leaves it: a second before the split event.
    round_trip(
        Repeat { until: Some(Until::DateTime(PyDt::Aware(at(19, 9) - chrono::Duration::seconds(1)))), ..repeat("week") },
        &["RRULE:FREQ=WEEKLY;UNTIL=20261019T125959Z"],
    );
    round_trip(
        Repeat { skipped: Some(vec![PyDt::Aware(at(12, 9)), PyDt::Aware(at(19, 9))]), added: Some(vec![PyDt::Aware(at(21, 14))]), ..repeat("week") },
        &["RRULE:FREQ=WEEKLY", "EXDATE;TZID=America/New_York:20261012T090000,20261019T090000", "RDATE;TZID=America/New_York:20261021T140000"],
    );
}

#[test]
fn an_until_date_is_the_end_of_that_day_in_the_series_zone() {
    assert_eq!(Repeat { until: date(2026, 12, 31), ..repeat("day") }.to_rules(&NY).unwrap(), strings(&["RRULE:FREQ=DAILY;UNTIL=20270101T045959Z"]));
}

#[test]
fn reads_times_given_in_utc_or_another_zone_in_the_series_zone() {
    let read = Repeat::from_rules(
        Some(&strings(&["RRULE:FREQ=WEEKLY;UNTIL=20261231", "EXDATE:20261012T130000Z", "RDATE;TZID=Europe/London:20261021T190000"])),
        &NY,
    )
    .unwrap();

    assert_eq!(read.until, date(2026, 12, 31));
    assert_eq!(read.skipped, Some(vec![PyDt::Aware(at(12, 9))]));
    assert_eq!(read.added, Some(vec![PyDt::Aware(at(21, 14))]));
    let Some(PyDt::Aware(skipped)) = read.skipped.as_ref().unwrap().first().cloned() else { panic!() };
    assert_eq!(skipped.offset().local_minus_utc(), -4 * 3600);
}

#[test]
fn reads_rules_it_cant_say_as_none() {
    let cases: [&[&str]; 7] = [
        &["RRULE:FREQ=HOURLY"],
        &["RRULE:FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-1"],
        &["RRULE:FREQ=WEEKLY", "EXRULE:FREQ=MONTHLY"],
        &["RRULE:FREQ=WEEKLY", "RRULE:FREQ=DAILY"],
        &["RRULE:FREQ=YEARLY;BYDAY=20MO"],
        &["RRULE:FREQ=WEEKLY", "EXDATE;VALUE=DATE:20261012"],
        &["RRULE:BYDAY=MO"],
    ];

    for rules in cases {
        assert_eq!(Repeat::from_rules(Some(&strings(rules)), &NY), None, "{rules:?}");
    }
}

#[test]
fn refuses_what_it_cant_write() {
    let cases: Vec<(Repeat, &str)> = vec![
        (repeat("fortnight"), "every must be one of"),
        (Repeat { interval: 0, ..repeat("week") }, "interval must be at least 1"),
        (Repeat { count: Some(3), until: date(2026, 12, 31), ..repeat("week") }, "count or until"),
        (Repeat { count: Some(0), ..repeat("week") }, "count must be at least 1"),
        (Repeat { weekdays: days(&["monday"]), ..repeat("week") }, "not 'monday'"),
        (Repeat { nth_weekdays: Some(vec![(6, "mon".into())]), ..repeat("month") }, "nth must be 1 to 5"),
        (Repeat { nth_weekdays: Some(vec![(1, "mon".into())]), ..repeat("week") }, "not \"week\""),
        (Repeat { nth_weekdays: Some(vec![(4, "thu".into())]), ..repeat("year") }, "need months"),
        (Repeat { month_days: Some(vec![0]), ..repeat("month") }, "not 0"),
        (Repeat { month_days: Some(vec![1]), ..repeat("week") }, "not for every \"week\""),
        (Repeat { months: Some(vec![13]), ..repeat("year") }, "not 13"),
    ];

    for (given, message) in cases {
        let error = given.to_rules(&NY).unwrap_err();
        assert!(error.to_string().contains(message), "{message:?} isn't in: {error}");
    }
}
