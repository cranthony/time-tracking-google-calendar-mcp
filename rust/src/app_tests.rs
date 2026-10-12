//! The server's configuration, and building its clients and stores. (The
//! HTTP transport's own settings are tested in mcp/http_tests.rs.)

use super::*;
use crate::testing::app::test_app;

fn vars(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
    let pairs: Vec<(String, String)> = pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
    move |name| pairs.iter().find(|(k, _)| k == name).map(|(_, v)| v.clone())
}

#[test]
fn the_calendar_id_is_needed_and_says_how_to_get_one() {
    for config in [Config::from_vars(vars(&[])), Config::from_vars(vars(&[("GOOGLE_CALENDAR_ID", "")]))] {
        let error = config.calendar_id().unwrap_err();
        assert!(error.to_string().contains("GOOGLE_CALENDAR_ID is not set. Run create_calendar"), "{error}");
    }

    assert_eq!(Config::from_vars(vars(&[("GOOGLE_CALENDAR_ID", "my-calendar-id")])).calendar_id().unwrap(), "my-calendar-id");
}

#[test]
fn the_credential_paths_default_to_gitignored_local_files() {
    let config = Config::from_vars(vars(&[]));

    assert_eq!((config.credentials_path, config.token_path), (PathBuf::from("credentials.json"), PathBuf::from("token.json")));
}

#[test]
fn the_credential_paths_come_from_the_environment_when_set() {
    let config = Config::from_vars(vars(&[("GOOGLE_OAUTH_CREDENTIALS_PATH", "/etc/secrets/credentials.json"), ("GOOGLE_OAUTH_TOKEN_PATH", "/etc/secrets/token.json")]));

    assert_eq!((config.credentials_path, config.token_path), (PathBuf::from("/etc/secrets/credentials.json"), PathBuf::from("/etc/secrets/token.json")));
}

#[test]
fn a_missing_token_says_where_it_looked() {
    let config = Config::from_vars(vars(&[("GOOGLE_CALENDAR_ID", "c"), ("GOOGLE_OAUTH_TOKEN_PATH", "no-such-dir/token.json")]));
    let app = App::new(config);

    let error = app.google().err().unwrap();

    assert!(format!("{error:#}").contains("No usable OAuth token at no-such-dir"), "{error:#}");
}

#[test]
fn a_store_needs_the_calendar_id() {
    let t = test_app();
    let app = App::with_google(Config::from_vars(vars(&[])), t.fake.google());

    let error = app.noted_time_sheet().err().unwrap();

    assert!(error.to_string().contains("GOOGLE_CALENDAR_ID is not set"), "{error}");
}

#[test]
fn every_store_is_built_once_in_the_calendars_one_spreadsheet() {
    let t = test_app();

    let (notes, journal) = (t.app.noted_time_sheet().unwrap(), t.app.journal().unwrap());
    t.app.actions().unwrap();
    t.app.people().unwrap();
    let requests = t.fake.requests().len();
    let again = t.app.noted_time_sheet().unwrap();

    // Built once: nothing more asked of Google the second time.
    assert_eq!(t.fake.requests().len(), requests);
    assert_eq!(again.spreadsheet_id(), notes.spreadsheet_id());
    assert_eq!(journal.whole_tab().spreadsheet_id, notes.spreadsheet_id());
    let created = t.fake.requests().iter().filter(|r| r.method == "POST" && r.path.ends_with("v4/spreadsheets")).count();
    assert_eq!(created, 1);
}

#[test]
fn the_calendar_client_is_on_the_configured_calendar() {
    let t = test_app();

    assert_eq!(t.app.calendar_client().unwrap().calendar_id(), t.calendar_id);
}
