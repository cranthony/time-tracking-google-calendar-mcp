//! The module's tests, against the fake Google.

use super::*;
use crate::testing::app::{test_app, TestApp};

fn circadian() -> (TestApp, Circadian) {
    let t = test_app();
    let circadian = t.app.circadian().unwrap();
    (t, circadian)
}

fn day(text: &str) -> NaiveDate {
    parsed_date(text).unwrap()
}

const TODAY: &str = "2026-11-02";

fn set(circadian: &Circadian, date: &str, time: &str, note: Option<&str>) -> anyhow::Result<()> {
    let _held = crate::write_lock::WRITE_LOCK.hold();
    circadian.set_wake_time(date, time, note, day(TODAY))
}

fn shown(circadian: &Circadian) -> Vec<(String, String, Option<String>)> {
    let revisions = circadian
        .revisions(&circadian.windows().unwrap(), None)
        .unwrap();
    revisions
        .into_iter()
        .map(|r| (r.date, r.time, r.note))
        .collect()
}

fn new_york() -> Tz {
    "America/New_York".parse().unwrap()
}

fn revision_on(date: &str, time: &str) -> Revision {
    Revision {
        id: format!("r{date}"),
        date: date.into(),
        time: time.into(),
        note: None,
        temperature_minimum: String::new(),
    }
}

#[test]
fn starts_empty_in_two_tabs_of_its_own_with_the_default_windows() {
    let (t, circadian) = circadian();

    assert!(shown(&circadian).is_empty());
    assert_eq!(
        circadian.windows().unwrap(),
        Windows {
            minimum_before_waking_minutes: 120,
            dim_before_minutes: 360,
            bright_after_minutes: 240
        }
    );
    let titles = t.fake.tab_titles(&t.spreadsheet());
    assert!(titles.contains(&NATURAL_WAKE_TIMES.1.to_string()));
    assert!(titles.contains(&CIRCADIAN_WINDOWS.1.to_string()));
    // Nothing is written to the windows' tab until they're set.
    assert_eq!(t.fake.rows(&t.spreadsheet(), CIRCADIAN_WINDOWS.1).len(), 1);
}

#[test]
fn keeps_a_wake_time_per_date_oldest_first_replacing_one_given_again() {
    let (_t, circadian) = circadian();

    set(
        &circadian,
        "2026-11-02",
        "6:40",
        Some("  woke without alarm "),
    )
    .unwrap();
    set(&circadian, "2026-10-20", "07:15", None).unwrap();
    let first = circadian.revisions(&Windows::default(), None).unwrap();
    set(&circadian, "2026-11-02", "06:30", None).unwrap();

    assert_eq!(
        shown(&circadian),
        vec![
            ("2026-10-20".into(), "07:15".into(), None),
            ("2026-11-02".into(), "06:30".into(), None)
        ]
    );
    let again = circadian.revisions(&Windows::default(), None).unwrap();
    assert_eq!(first[1].note.as_deref(), Some("woke without alarm"));
    assert_eq!(again[1].id, first[1].id);
    assert_ne!(again[0].id, again[1].id);
}

#[test]
fn trims_wake_times_dated_more_than_366_days_before_the_newest() {
    let (_t, circadian) = circadian();

    set(&circadian, "2025-10-31", "07:00", None).unwrap();
    set(&circadian, "2025-11-01", "07:05", None).unwrap();
    set(&circadian, "2026-11-02", "06:40", None).unwrap();

    let dates: Vec<String> = shown(&circadian).into_iter().map(|(d, _, _)| d).collect();
    assert_eq!(dates, vec!["2025-11-01", "2026-11-02"]);
}

#[test]
fn an_older_date_set_later_keeps_the_newest_and_trims_itself() {
    let (_t, circadian) = circadian();

    set(&circadian, "2026-11-02", "06:40", None).unwrap();
    set(&circadian, "2024-01-01", "08:00", None).unwrap();

    let dates: Vec<String> = shown(&circadian).into_iter().map(|(d, _, _)| d).collect();
    assert_eq!(dates, vec!["2026-11-02"]);
}

#[test]
fn refuses_what_isnt_a_date_time_or_short_note_saying_what_to_send() {
    let (_t, circadian) = circadian();
    let long = "x".repeat(MAX_NOTE_LENGTH + 1);
    let cases: Vec<(&str, &str, Option<&str>, &str)> = vec![
        ("2026-13-01", "06:40", None, "'2026-13-01' isn't a date: give it as \"YYYY-MM-DD\" (e.g. \"2026-11-02\")"),
        ("11/02/2026", "06:40", None, "isn't a date"),
        ("2026-11-02", "6:40am", None, "'6:40am' isn't a time of day: give it as \"HH:MM\", 24-hour (e.g. \"06:40\")"),
        ("2026-11-02", "24:00", None, "isn't a time of day"),
        ("2027-11-03", "06:40", None, "2027-11-03 is more than a year from today (2026-11-02): give a date on or before 2027-11-02"),
        ("2026-11-02", "06:40", Some(long.as_str()), "the note is longer than 500 characters"),
    ];

    for (date, time, note, expected) in cases {
        let error = set(&circadian, date, time, note).unwrap_err().to_string();
        assert!(error.contains(expected), "{date} {time}: {error}");
    }
    set(
        &circadian,
        "2027-11-02",
        "06:40",
        Some(&"x".repeat(MAX_NOTE_LENGTH)),
    )
    .unwrap();
    assert_eq!(shown(&circadian).len(), 1);
}

#[test]
fn sets_the_windows_whole_or_refuses_any_out_of_range() {
    let (_t, circadian) = circadian();
    let _held = crate::write_lock::WRITE_LOCK.hold();
    let windows = Windows {
        minimum_before_waking_minutes: 90,
        dim_before_minutes: 0,
        bright_after_minutes: 1440,
    };

    assert_eq!(circadian.set_windows(&windows).unwrap(), windows);
    assert_eq!(circadian.windows().unwrap(), windows);
    let error = circadian
        .set_windows(&Windows {
            minimum_before_waking_minutes: -1,
            dim_before_minutes: 360,
            bright_after_minutes: 1441,
        })
        .unwrap_err()
        .to_string();
    assert_eq!(
        error,
        "minimum_before_waking_minutes is -1: give a whole number of minutes from 0 to 1440; bright_after_minutes is 1441: give a whole number of minutes from 0 to 1440"
    );
    assert_eq!(circadian.windows().unwrap(), windows);
}

#[test]
fn a_window_missing_from_its_tab_has_its_default() {
    let (t, circadian) = circadian();
    t.seed(
        CIRCADIAN_WINDOWS.1,
        &[
            json!({"id": "dim_before_minutes", "minutes": 300}),
            json!({"id": "other", "minutes": 5}),
        ],
    );
    t.fake.forget_kept(&t.spreadsheet());

    assert_eq!(
        circadian.windows().unwrap(),
        Windows {
            dim_before_minutes: 300,
            ..Windows::default()
        }
    );
}

#[test]
fn a_revisions_temperature_minimum_can_fall_the_day_before() {
    let (_t, circadian) = circadian();
    set(&circadian, "2026-11-02", "01:10", None).unwrap();
    set(&circadian, "2026-10-01", "06:40", None).unwrap();

    let revisions = circadian
        .revisions(&Windows::default(), Some(&new_york()))
        .unwrap();

    let minimums: Vec<&str> = revisions
        .iter()
        .map(|r| r.temperature_minimum.as_str())
        .collect();
    assert_eq!(minimums, vec!["04:40", "23:10"]);
}

#[test]
fn each_day_takes_the_latest_revision_on_or_before_it_and_days_before_any_are_left_out() {
    let revisions = vec![
        revision_on("2026-10-03", "06:40"),
        revision_on("2026-10-05", "07:30"),
    ];

    let got = days(
        &revisions,
        &Windows::default(),
        &new_york(),
        day("2026-10-02"),
        day("2026-10-06"),
    )
    .unwrap();

    let shown: Vec<(&str, &str, &str)> = got
        .iter()
        .map(|d| {
            (
                d.date.as_str(),
                d.revision_id.as_str(),
                d.temperature_minimum.0.as_str(),
            )
        })
        .collect();
    assert_eq!(
        shown,
        vec![
            ("2026-10-03", "r2026-10-03", "2026-10-03T04:40:00-04:00"),
            ("2026-10-04", "r2026-10-03", "2026-10-04T04:40:00-04:00"),
            ("2026-10-05", "r2026-10-05", "2026-10-05T05:30:00-04:00"),
            ("2026-10-06", "r2026-10-05", "2026-10-06T05:30:00-04:00"),
        ]
    );
    assert_eq!(
        got[0].dim,
        Window {
            start: DateTimeText("2026-10-02T22:40:00-04:00".into()),
            end: DateTimeText("2026-10-03T04:40:00-04:00".into())
        }
    );
    assert_eq!(
        got[0].bright,
        Window {
            start: DateTimeText("2026-10-03T04:40:00-04:00".into()),
            end: DateTimeText("2026-10-03T08:40:00-04:00".into())
        }
    );
}

#[test]
fn a_minimum_before_midnight_puts_both_windows_across_it() {
    let revisions = vec![revision_on("2026-10-01", "01:00")];

    let got = days(
        &revisions,
        &Windows::default(),
        &new_york(),
        day("2026-10-10"),
        day("2026-10-10"),
    )
    .unwrap();

    assert_eq!(got[0].temperature_minimum.0, "2026-10-09T23:00:00-04:00");
    assert_eq!(got[0].dim.start.0, "2026-10-09T17:00:00-04:00");
    assert_eq!(got[0].bright.end.0, "2026-10-10T03:00:00-04:00");
}

#[test]
fn a_window_across_the_dst_change_keeps_its_length_in_time_not_on_the_clock() {
    // New York's clocks go back an hour at 02:00 on 2026-11-01.
    let revisions = vec![revision_on("2026-10-01", "06:40")];

    let got = days(
        &revisions,
        &Windows::default(),
        &new_york(),
        day("2026-11-01"),
        day("2026-11-02"),
    )
    .unwrap();

    assert_eq!(got[0].temperature_minimum.0, "2026-11-01T04:40:00-05:00");
    // Six hours before is 23:40 the evening before: five on the clock.
    assert_eq!(got[0].dim.start.0, "2026-10-31T23:40:00-04:00");
    assert_eq!(got[0].bright.end.0, "2026-11-01T08:40:00-05:00");
    assert_eq!(
        got[1].dim,
        Window {
            start: DateTimeText("2026-11-01T22:40:00-05:00".into()),
            end: DateTimeText("2026-11-02T04:40:00-05:00".into())
        }
    );
}

#[test]
fn refuses_a_range_backwards_or_over_400_days() {
    let revisions = vec![revision_on("2026-10-01", "06:40")];
    let zone = new_york();

    let backwards = days(
        &revisions,
        &Windows::default(),
        &zone,
        day("2026-11-02"),
        day("2026-11-01"),
    )
    .unwrap_err()
    .to_string();
    let long = days(
        &revisions,
        &Windows::default(),
        &zone,
        day("2026-01-01"),
        day("2027-02-05"),
    )
    .unwrap_err()
    .to_string();

    assert_eq!(
        backwards,
        "to (2026-11-01) is before from (2026-11-02): give from first, then to, on or after it"
    );
    assert_eq!(
        long,
        "from 2026-01-01 to 2027-02-05 is 401 days: give a range of at most 400"
    );
    assert_eq!(
        days(
            &revisions,
            &Windows::default(),
            &zone,
            day("2026-01-01"),
            day("2027-02-04")
        )
        .unwrap()
        .len(),
        127
    );
}
