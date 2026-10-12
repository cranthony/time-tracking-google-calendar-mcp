//! The module's tests, against the fake Google.

use super::*;
use crate::sheet::rows::Row;
use crate::testing::app::{test_app, TestApp};

fn schedule() -> (TestApp, CompactionSchedule) {
    let t = test_app();
    let schedule = t.app.compaction_schedule().unwrap();
    (t, schedule)
}

fn hint(id: Option<&str>, time: Option<&str>, label: Option<&str>) -> Row {
    let mut row = HINT_ROW.empty();
    row.insert("id".into(), json!(id));
    row.insert("time".into(), json!(time));
    row.insert("label".into(), json!(label));
    row
}

fn at(time: &str) -> Row {
    hint(None, Some(time), None)
}

fn set(schedule: &CompactionSchedule, hints: &[Row]) -> anyhow::Result<Vec<Row>> {
    let _held = crate::write_lock::WRITE_LOCK.hold();
    schedule.set_hints(hints)
}

fn field(hint: &Row, name: &str) -> Option<String> {
    rows::text(hint, name).map(str::to_string)
}

#[test]
fn starts_with_none_in_a_tab_of_its_own() {
    let (t, schedule) = schedule();

    assert!(schedule.all().unwrap().is_empty());
    assert!(t.fake.tab_titles(&t.spreadsheet()).contains(&COMPACTION_SCHEDULE.1.to_string()));
    let tagged = t.fake.requests().into_iter().any(|r| r.body.to_string().contains(&format!("\"metadataValue\":\"{}\"", COMPACTION_SCHEDULE.0)));
    assert!(tagged);
}

#[test]
fn sets_them_whole_earliest_first_each_with_an_id() {
    let (t, schedule) = schedule();

    let saved = set(
        &schedule,
        &[
            hint(None, Some("18:30"), Some("Evening compaction")),
            hint(None, Some("7:05"), Some("  Morning compaction ")),
            hint(None, Some("12:00"), Some("   ")),
        ],
    )
    .unwrap();

    let shown: Vec<(Option<String>, Option<String>)> = saved.iter().map(|h| (field(h, "time"), field(h, "label"))).collect();
    assert_eq!(
        shown,
        vec![
            (Some("07:05".into()), Some("Morning compaction".into())),
            (Some("12:00".into()), None),
            (Some("18:30".into()), Some("Evening compaction".into())),
        ]
    );
    let ids: Vec<String> = saved.iter().filter_map(|h| field(h, "id")).filter(|i| !i.is_empty()).collect();
    assert_eq!(ids.len(), 3);
    assert_eq!(ids.iter().collect::<std::collections::BTreeSet<_>>().len(), 3);
    // Read back as saved, by anyone.
    let again = {
        let (_, sheets) = t.app.clients().unwrap();
        let _held = crate::write_lock::WRITE_LOCK.hold();
        CompactionSchedule::ensure(&sheets, &t.spreadsheet()).unwrap()
    };
    assert_eq!(again.all().unwrap(), saved);
}

#[test]
fn replacing_them_keeps_the_ids_given_and_drops_the_rest() {
    let (_t, schedule) = schedule();
    let first = set(&schedule, &[at("07:00"), at("19:00")]).unwrap();
    let (morning, evening) = (field(&first[0], "id").unwrap(), field(&first[1], "id").unwrap());

    let saved = set(&schedule, &[hint(Some(&morning), Some("07:30"), Some("Morning"))]).unwrap();

    assert_eq!(saved, vec![hint(Some(&morning), Some("07:30"), Some("Morning"))]);
    assert_eq!(schedule.all().unwrap(), saved);
    assert!(!schedule.all().unwrap().iter().any(|h| field(h, "id") == Some(evening.clone())));
}

#[test]
fn an_empty_list_clears_them() {
    let (_t, schedule) = schedule();
    set(&schedule, &[at("07:00")]).unwrap();

    assert!(set(&schedule, &[]).unwrap().is_empty());
    assert!(schedule.all().unwrap().is_empty());
}

#[test]
fn refuses_what_isnt_a_schedule_writing_nothing() {
    let long = "x".repeat(101);
    let cases: Vec<(Vec<Row>, &str)> = vec![
        (vec![at("7")], "'7' isn't a time of day"),
        (vec![at("24:00")], "'24:00' isn't a time of day"),
        (vec![at("07:60")], "isn't a time of day"),
        (vec![hint(None, None, None)], "None isn't a time of day"),
        (vec![at("07:00"), at("7:00")], "07:00 is given twice"),
        (vec![hint(None, Some("07:00"), Some(&long))], "longer than 100 characters"),
    ];
    let (_t, schedule) = schedule();
    set(&schedule, &[at("06:00")]).unwrap();

    for (hints, message) in cases {
        let error = set(&schedule, &hints).unwrap_err();
        assert!(error.to_string().contains(message), "{error}");
        let times: Vec<Option<String>> = schedule.all().unwrap().iter().map(|h| field(h, "time")).collect();
        assert_eq!(times, vec![Some("06:00".to_string())]);
    }
}

#[test]
fn reads_times_of_day() {
    for (text, time) in [("7:30", Some("07:30")), (" 07:30 ", Some("07:30")), ("00:00", Some("00:00")), ("23:59", Some("23:59")), ("7.30", None), ("", None)] {
        assert_eq!(normalized_time(Some(text)).as_deref(), time, "{text:?}");
    }
}
