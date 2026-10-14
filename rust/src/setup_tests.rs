//! Setting up Google access, against the fake Google.

use super::*;
use crate::app::Config;
use crate::testing::fake_google::FakeGoogle;
use serde_json::{json, Value};
use std::path::PathBuf;

/// A scratch directory of its own.
fn scratch() -> PathBuf {
    let dir = std::env::temp_dir().join(format!("time-tracker-setup-{}", uuid::Uuid::new_v4().simple()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// A client secret file for the fake's token endpoint: a web
/// application's, or a desktop app's.
pub fn credentials(fake: &FakeGoogle, dir: &std::path::Path, web: bool) -> PathBuf {
    let client = json!({"client_id": "client-1.apps.googleusercontent.com", "client_secret": "shh", "auth_uri": "https://accounts.example/o/oauth2/auth", "token_uri": format!("{}token", fake.root)});
    let path = dir.join("credentials.json");
    std::fs::write(&path, json!({ if web { "web" } else { "installed" }: client }).to_string()).unwrap();
    path
}

/// An app on the fake: on `calendar_id`, or none, with its client secret
/// file and a token.json in a scratch directory.
pub fn app_on(fake: &FakeGoogle, calendar_id: Option<String>, web: bool) -> Arc<App> {
    let dir = scratch();
    let config = Config { calendar_id, token_path: dir.join("token.json"), credentials_path: credentials(fake, &dir, web) };
    let app = Arc::new(App::with_google(config, fake.google()));
    app.set_self();
    app
}

/// The query parameter `key` of `url`.
pub fn url_param(url: &str, key: &str) -> String {
    let query = url.split_once('?').unwrap().1;
    consent::query_params(query).into_iter().find(|(k, _)| k == key).map(|(_, v)| v).unwrap()
}

#[test]
fn setup_is_needed_only_without_a_calendar_or_a_working_token() {
    let fake = FakeGoogle::start();
    let calendar = fake.add_calendar(Some("UTC"));

    assert_eq!(need(&app_on(&fake, None, false)), Some(Need::FirstSetup));
    assert_eq!(need(&app_on(&fake, Some(calendar), false)), None);
    let broken = need(&app_on(&fake, Some("gone@group.calendar.google.com".into()), false));
    assert!(matches!(broken, Some(Need::Reconnect(_))), "{broken:?}");
}

#[test]
fn a_link_works_once() {
    let setup = Setup::default();
    let link = setup.new_link();

    assert!(!setup.take_link("made-up"));
    assert!(setup.take_link(&link));
    assert!(!setup.take_link(&link));
}

#[test]
fn a_first_setup_makes_the_calendar_and_takes_on_the_token() {
    let fake = FakeGoogle::start();
    let app = app_on(&fake, None, false);

    let (session, url, web) = app.setup.start(&app, "https://tracker.example").unwrap();
    assert!(!web);
    assert_eq!(url_param(&url, "redirect_uri"), DESKTOP_REDIRECT);
    assert_eq!((url_param(&url, "prompt"), url_param(&url, "access_type")), ("consent".into(), "offline".into()));
    assert!(url.starts_with("https://accounts.example/o/oauth2/auth?"), "{url}");
    let finished = app.setup.finish(&app, &session, "code-1", &url_param(&url, "state")).unwrap();

    assert!(finished.created);
    assert_eq!(app.calendar_id().unwrap(), finished.calendar_id);
    assert!(finished.spreadsheet_url.starts_with("https://docs.google.com/spreadsheets/d/"));
    let token: Value = serde_json::from_str(&finished.token).unwrap();
    assert!(token["refresh_token"].as_str().unwrap().starts_with("refresh-"), "{token}");
    assert_eq!(token["client_id"], "client-1.apps.googleusercontent.com");
    assert!(finished.saved);
    let saved: Value = serde_json::from_str(&std::fs::read_to_string(&app.config.token_path).unwrap()).unwrap();
    assert_eq!(saved["refresh_token"], token["refresh_token"]);
    // The server works on it at once, and there's nothing left to set up.
    assert_eq!(need(&app), None);
    let exchanged = fake.requests().into_iter().find(|r| r.path == "/token").unwrap();
    assert_eq!(exchanged.body["code_verifier"].as_str().map(str::len), Some(128));
    assert_eq!((exchanged.body["code"].clone(), exchanged.body["redirect_uri"].clone()), (json!("code-1"), json!(DESKTOP_REDIRECT)));
}

#[test]
fn reconnecting_keeps_the_calendar() {
    let fake = FakeGoogle::start();
    let calendar = fake.add_calendar(Some("UTC"));
    let app = app_on(&fake, Some(calendar.clone()), true);
    let before = app.google().unwrap();

    let (session, url, web) = app.setup.start(&app, "https://tracker.example/").unwrap();
    assert!(web);
    assert_eq!(url_param(&url, "redirect_uri"), "https://tracker.example/setup/callback");
    let finished = app.setup.finish(&app, &session, "code-2", &url_param(&url, "state")).unwrap();

    assert!(!finished.created);
    assert_eq!((finished.calendar_id.clone(), app.calendar_id().unwrap()), (calendar.clone(), calendar));
    // Every client already holding the old token has the new one.
    let current: Value = serde_json::from_str(&finished.token).unwrap();
    assert!(Arc::ptr_eq(&before, &app.google().unwrap()));
    assert!(finished.saved);
    let saved: Value = serde_json::from_str(&std::fs::read_to_string(&app.config.token_path).unwrap()).unwrap();
    assert_eq!(saved["refresh_token"], current["refresh_token"]);
}

#[test]
fn a_token_that_cant_see_the_calendar_isnt_taken_on() {
    let fake = FakeGoogle::start();
    let app = app_on(&fake, Some("someone-elses@group.calendar.google.com".into()), false);

    let (session, url, _) = app.setup.start(&app, "https://tracker.example").unwrap();
    let refused = app.setup.finish(&app, &session, "code-3", &url_param(&url, "state")).err().unwrap();

    assert!(format!("{refused:#}").contains("can't see the calendar GOOGLE_CALENDAR_ID names"), "{refused:#}");
}

#[test]
fn only_the_setup_that_started_it_can_finish_once() {
    let fake = FakeGoogle::start();
    let app = app_on(&fake, None, false);
    let (session, url, _) = app.setup.start(&app, "https://tracker.example").unwrap();
    let state = url_param(&url, "state");

    let wrong_state = app.setup.finish(&app, &session, "code", "forged").err().unwrap().to_string();
    assert!(wrong_state.contains("doesn't match this setup"), "{wrong_state}");
    // A failed answer uses the setup up, as does a finished one.
    let again = app.setup.finish(&app, &session, "code", &state).err().unwrap().to_string();
    assert!(again.contains("has expired, was already finished"), "{again}");
    let other = app.setup.finish(&app, "another-browser", "code", &state).err().unwrap().to_string();
    assert!(other.contains("has expired, was already finished"), "{other}");
}

#[test]
fn the_tool_gives_a_link_only_when_setup_is_needed_and_over_http() {
    let fake = FakeGoogle::start();
    let fresh = app_on(&fake, None, false);

    let stdio = crate::tools::run(&fresh, "google_setup_link", json!({})).err().unwrap();
    assert!(matches!(&stdio, crate::tools::ToolError::Message(m) if m.contains("needs the server running over HTTP")));
    fresh.setup.set_base("https://tracker.example/");
    let link = crate::tools::run(&fresh, "google_setup_link", json!({})).ok().unwrap().0;
    let url = link["url"].as_str().unwrap();
    assert!(url.starts_with("https://tracker.example/setup?link="), "{url}");
    assert!(fresh.setup.take_link(&url_param(url, "link")));
    assert!(link["reason"].as_str().unwrap().contains("GOOGLE_CALENDAR_ID isn't set"), "{link}");

    let calendar = fake.add_calendar(Some("UTC"));
    let working = app_on(&fake, Some(calendar), false);
    working.setup.set_base("https://tracker.example");
    let refused = crate::tools::run(&working, "google_setup_link", json!({})).err().unwrap();
    assert!(matches!(&refused, crate::tools::ToolError::Message(m) if m.contains("nothing to set up")));
}

#[test]
fn the_finished_page_shows_the_token_escaped() {
    let finished = Finished {
        token: r#"{"token": "<a&b>"}"#.into(),
        calendar_id: "c@group.calendar.google.com".into(),
        created: true,
        spreadsheet_url: "https://docs.google.com/spreadsheets/d/s/edit".into(),
        saved: false,
        stored: None,
    };

    let html = finished_page(&finished);

    assert!(html.contains("{&quot;token&quot;: &quot;&lt;a&amp;b&gt;&quot;}"), "{html}");
    assert!(html.contains("Set <code>GOOGLE_CALENDAR_ID</code> to <code>c@group.calendar.google.com</code>"));
    assert!(html.contains("couldn't be written"));
}

const SECRET: &str = "projects/corp-project/secrets/time-tracker-google-token";

fn secret_on(fake: &FakeGoogle) -> TokenSecret {
    TokenSecret { name: SECRET.into(), metadata_root: fake.root.clone(), api_root: fake.root.clone() }
}

#[test]
fn with_a_token_secret_the_token_is_added_to_it_and_not_shown() {
    let fake = FakeGoogle::start();
    let app = app_on(&fake, None, false);
    app.setup.set_token_secret(secret_on(&fake));

    let (session, url, _) = app.setup.start(&app, "https://tracker.example").unwrap();
    let finished = app.setup.finish(&app, &session, "code-5", &url_param(&url, "state")).unwrap();

    assert_eq!(finished.stored, Some(Ok(format!("{SECRET}/versions/1"))));
    assert_eq!(fake.secret_versions(), vec![(SECRET.to_string(), finished.token.clone())]);
    let added = fake.requests().into_iter().find(|r| r.path.ends_with(":addVersion")).unwrap();
    assert_eq!(added.bearer.as_deref(), Some("metadata-token"));
    let html = finished_page(&finished);
    assert!(html.contains("saved as a new version of its secret") && html.contains("/versions/1"), "{html}");
    assert!(!html.contains("refresh-") && !html.contains("<textarea"), "{html}");
}

#[test]
fn a_secret_that_cant_be_written_leaves_the_token_to_copy() {
    let fake = FakeGoogle::start();
    let app = app_on(&fake, None, false);
    app.setup.set_token_secret(secret_on(&fake));
    fake.with(|s| s.fail_next.push(("POST".into(), ":addVersion".into(), 403, "Permission 'secretmanager.versions.add' denied".into())));

    let (session, url, _) = app.setup.start(&app, "https://tracker.example").unwrap();
    let finished = app.setup.finish(&app, &session, "code-6", &url_param(&url, "state")).unwrap();

    let why = finished.stored.clone().unwrap().err().unwrap();
    assert!(why.contains(SECRET) && why.contains("403") && why.contains("secretmanager.versions.add"), "{why}");
    // Set up all the same: the server is using the token.
    assert_eq!(need(&app), None);
    let html = finished_page(&finished);
    assert!(html.contains("Add it to the secret yourself") && html.contains("<textarea"), "{html}");
}

#[test]
fn the_metadata_server_needs_its_header() {
    let fake = FakeGoogle::start();
    let answer = ureq::get(&format!("{}computeMetadata/v1/instance/service-accounts/default/token", fake.root)).call();
    assert!(matches!(answer, Err(ureq::Error::Status(403, _))));
}
