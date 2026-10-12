//! tests/test_create_calendar.py, against the fake Google.

use super::*;
use crate::sheet::{ACTIONS, TIME_NOTES};
use crate::testing::fake_google::FakeGoogle;

fn config(calendar_id: Option<&str>) -> Config {
    Config { calendar_id: calendar_id.map(str::to_string), token_path: "unused".into(), credentials_path: "unused".into() }
}

fn run(fake: &FakeGoogle, args: &[&str], calendar_id: Option<&str>) -> anyhow::Result<String> {
    let args: Vec<String> = args.iter().map(|a| a.to_string()).collect();
    let mut out = Vec::new();
    run_with(&args, config(calendar_id), fake.google(), &mut out)?;
    Ok(String::from_utf8(out).unwrap())
}

fn created_calendars(fake: &FakeGoogle) -> Vec<serde_json::Value> {
    fake.requests().into_iter().filter(|r| r.method == "POST" && r.path.ends_with("/calendars")).map(|r| r.body).collect()
}

#[test]
fn creates_the_calendar_and_its_metadata_sheet_when_unconfigured() {
    let fake = FakeGoogle::start();

    let out = run(&fake, &[], None).unwrap();

    assert_eq!(created_calendars(&fake), vec![serde_json::json!({"summary": DEFAULT_SUMMARY, "description": DEFAULT_DESCRIPTION})]);
    let id = out.lines().next().unwrap().rsplit(' ').next().unwrap().to_string();
    assert!(out.contains("Set GOOGLE_CALENDAR_ID to this value."));
    let sheet = out.lines().last().unwrap().trim_start_matches("Calendar metadata sheet: https://docs.google.com/spreadsheets/d/").trim_end_matches("/edit").to_string();
    let tabs = fake.tab_titles(&sheet);
    assert!(tabs.contains(&ACTIONS.1.to_string()) && tabs.contains(&TIME_NOTES.1.to_string()), "{tabs:?}");
    // The spreadsheet is the new calendar's.
    let client = CalendarClient::new(fake.google(), &id);
    assert_eq!(client.get_calendar_metadata("calendar-metadata-spreadsheet-id").unwrap(), Some(sheet));
}

#[test]
fn names_the_calendar_and_describes_it_as_asked() {
    let fake = FakeGoogle::start();

    let out = run(&fake, &["Focus", "--description", "Mine"], None).unwrap();

    assert!(out.starts_with("Created calendar 'Focus' with id: "), "{out}");
    assert_eq!(created_calendars(&fake), vec![serde_json::json!({"summary": "Focus", "description": "Mine"})]);
}

#[test]
fn only_ensures_the_sheet_when_a_calendar_is_configured() {
    let fake = FakeGoogle::start();
    let existing = fake.add_calendar(Some("UTC"));

    let out = run(&fake, &[], Some(&existing)).unwrap();
    let again = run(&fake, &[], Some(&existing)).unwrap();

    assert!(created_calendars(&fake).is_empty());
    assert!(out.contains(&format!("GOOGLE_CALENDAR_ID is already set to '{existing}'")), "{out}");
    assert!(out.contains("https://docs.google.com/spreadsheets/d/"));
    // Safe to run again: the same spreadsheet.
    assert_eq!(out.lines().last(), again.lines().last());
}

#[test]
fn a_concurrent_label_change_is_a_conflict() {
    let fake = FakeGoogle::start();
    let existing = fake.add_calendar(Some("UTC"));
    fake.with(|s| s.fail_next.push(("PATCH".into(), format!("/calendars/{existing}"), 412, "Precondition Failed".into())));

    let error = run(&fake, &[], Some(&existing)).err().unwrap();

    assert!(is_label_conflict(&error) || format!("{error:#}").contains("412"), "{error:#}");
}

#[test]
fn refuses_arguments_it_doesnt_know() {
    let fake = FakeGoogle::start();

    let error = run(&fake, &["a", "b"], None).err().unwrap();

    assert!(error.to_string().contains("unrecognized arguments: b"), "{error}");
}
