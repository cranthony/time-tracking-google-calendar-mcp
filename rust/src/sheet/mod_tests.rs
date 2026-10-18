//! The module's tests, against the fake Google.

use super::*;
use crate::testing::fake_google::FakeGoogle;
use crate::write_lock::WRITE_LOCK;

fn clients(fake: &FakeGoogle) -> (CalendarClient, SheetsClient) {
    let calendar = fake.add_calendar(Some("UTC"));
    (CalendarClient::new(fake.google(), &calendar), SheetsClient::new(fake.google()))
}

#[test]
fn creates_a_spreadsheet_when_nothing_is_tracked_then_reuses_it() {
    let fake = FakeGoogle::start();
    let (calendar, sheets) = clients(&fake);
    let _held = WRITE_LOCK.hold();

    let (id, new) = ensure_spreadsheet(&calendar, &sheets).unwrap();

    assert!(new);
    assert_eq!(calendar.get_calendar_metadata(SPREADSHEET_ID_METADATA_KEY).unwrap(), Some(id.clone()));
    let created = fake.requests().into_iter().find(|r| r.path.ends_with("v4/spreadsheets")).unwrap();
    assert_eq!(created.body["properties"]["title"], SPREADSHEET_TITLE);
    fake.clear_requests();

    let (again, new) = ensure_spreadsheet(&calendar, &sheets).unwrap();

    assert_eq!((again, new), (id, false));
    assert!(fake.requests().iter().all(|r| r.method == "GET"));
}

#[test]
fn retitling_is_one_write_and_no_read() {
    let fake = FakeGoogle::start();
    let (_, sheets) = clients(&fake);
    let _held = WRITE_LOCK.hold();
    let spreadsheet = sheets.create_spreadsheet("Calendar Metadata").unwrap();
    let before = fake.quota_counts();

    retitle_spreadsheet(&sheets, &spreadsheet).unwrap();

    let after = fake.quota_counts();
    assert_eq!([after[0] - before[0], after[1] - before[1]], [0, 1]);
    assert_eq!(fake.spreadsheet_title(&spreadsheet), "Calendar Metadata (do not edit by hand)");
}

#[test]
fn adds_and_tags_a_new_tab_then_reuses_it() {
    let fake = FakeGoogle::start();
    let (_, sheets) = clients(&fake);
    let _held = WRITE_LOCK.hold();
    let spreadsheet = sheets.create_spreadsheet(SPREADSHEET_TITLE).unwrap();
    fake.clear_requests();

    let (id, created) = ensure_tab(&sheets, &spreadsheet, "uncompacted-time-notes", "Uncompacted Time Notes").unwrap();

    assert!(created);
    let request = fake.requests().into_iter().find(|r| r.path.ends_with(":batchUpdate")).unwrap();
    let added = &request.body["requests"][0]["addSheet"]["properties"];
    assert_eq!((added["title"].clone(), added["tabColor"].clone()), (json!("Uncompacted Time Notes"), tab_color()));
    assert_eq!(sheets.find_sheet_id(&spreadsheet, SHEET_ROLE_METADATA_KEY, "uncompacted-time-notes").unwrap(), Some(id));
    fake.clear_requests();

    let (again, created) = ensure_tab(&sheets, &spreadsheet, "uncompacted-time-notes", "Uncompacted Time Notes").unwrap();

    assert_eq!((again, created), (id, false));
    assert!(fake.requests().iter().all(|r| !r.path.ends_with(":batchUpdate")));
}
