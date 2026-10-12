//! The module's tests, against the fake Google's token endpoint. (Its
//! memory tracking was dropped from the port, and the per-thread
//! connections it tests are ureq's own.)

use super::*;
use crate::testing::fake_google::FakeGoogle;

fn temp_path(name: &str) -> PathBuf {
    std::env::temp_dir().join(format!("ctt-auth-{}-{name}", uuid::Uuid::new_v4()))
}

fn token_json(fake: &FakeGoogle, token: &str, expiry: &str, refresh_token: Option<&str>) -> String {
    let mut token = json!({"token": token, "token_uri": format!("{}token", fake.root), "client_id": "id", "client_secret": "secret", "expiry": expiry});
    if let Some(refresh) = refresh_token {
        token["refresh_token"] = json!(refresh);
    }
    token.to_string()
}

fn expired(fake: &FakeGoogle) -> String {
    token_json(fake, "stale", "2020-01-01T00:00:00Z", Some("refresh-token"))
}

fn refreshes(fake: &FakeGoogle) -> Vec<Value> {
    fake.requests().into_iter().filter(|r| r.path == "/token").map(|r| r.body).collect()
}

/// A request through `google` to the fake: the bearer token it was sent
/// with.
fn bearer_sent(fake: &FakeGoogle, google: &Google) -> Option<String> {
    let calendar = fake.add_calendar(Some("UTC"));
    fake.clear_requests();
    google.request("GET", &google.calendar_url(&format!("calendars/{calendar}")), &[], None, None).unwrap();
    fake.requests().into_iter().rfind(|r| r.path.ends_with(&calendar)).unwrap().bearer
}

#[test]
fn asks_for_calendar_and_drive_file() {
    assert!(SCOPES.contains(&"https://www.googleapis.com/auth/calendar.app.created"));
    assert!(SCOPES.contains(&"https://www.googleapis.com/auth/drive.file"));
}

#[test]
fn an_expired_token_is_refreshed_and_written_back() {
    let fake = FakeGoogle::start();
    let path = temp_path("token.json");
    std::fs::write(&path, expired(&fake)).unwrap();

    let google = Google::load(&path, Path::new("credentials.json")).unwrap();

    let sent = refreshes(&fake);
    assert_eq!(sent.len(), 1);
    assert_eq!(
        (sent[0]["grant_type"].as_str(), sent[0]["refresh_token"].as_str(), sent[0]["client_id"].as_str()),
        (Some("refresh_token"), Some("refresh-token"), Some("id"))
    );
    let written: Value = serde_json::from_str(&std::fs::read_to_string(&path).unwrap()).unwrap();
    let fresh = written["token"].as_str().unwrap().to_string();
    assert!(fresh.starts_with("fresh-"), "{written}");
    // What else it held is kept.
    assert_eq!(written["refresh_token"], "refresh-token");
    let credentials = google.credentials.lock().unwrap().clone();
    assert_eq!(credentials.token.as_deref(), Some(fresh.as_str()));
    assert!(credentials.valid());
    let _ = std::fs::remove_file(&path);
}

#[test]
fn a_token_path_that_cant_be_written_still_works_from_memory() {
    // A read-only Secret File: the next start refreshes again.
    let fake = FakeGoogle::start();
    let path = temp_path("missing-dir").join("token.json");
    let google = Google::from_token_json_at(&expired(&fake), &path, Some(fake.root.clone())).unwrap();

    let bearer = bearer_sent(&fake, &google);

    assert!(bearer.is_some_and(|b| b.starts_with("fresh-")));
    assert!(!path.exists());
}

#[test]
fn a_valid_token_is_used_as_it_is() {
    let fake = FakeGoogle::start();
    let google = Google::from_token_json_at(&token_json(&fake, "good", "2099-01-01T00:00:00Z", Some("r")), &temp_path("token.json"), Some(fake.root.clone())).unwrap();

    assert_eq!(bearer_sent(&fake, &google).as_deref(), Some("good"));
    assert!(refreshes(&fake).is_empty());
}

#[test]
fn a_401_refreshes_the_token_and_tries_again_once() {
    let fake = FakeGoogle::start();
    let google = Google::from_token_json_at(&token_json(&fake, "revoked", "2099-01-01T00:00:00Z", Some("r")), &temp_path("token.json"), Some(fake.root.clone())).unwrap();
    let calendar = fake.add_calendar(Some("UTC"));
    fake.with(|s| s.fail_next.push(("GET".into(), format!("/calendars/{calendar}"), 401, "Invalid Credentials".into())));

    google.request("GET", &google.calendar_url(&format!("calendars/{calendar}")), &[], None, None).unwrap();

    let tries: Vec<Option<String>> = fake.requests().into_iter().filter(|r| r.path.ends_with(&calendar)).map(|r| r.bearer).collect();
    assert_eq!(tries.len(), 2);
    assert_eq!(tries[0].as_deref(), Some("revoked"));
    assert!(tries[1].as_ref().is_some_and(|b| b.starts_with("fresh-")));
}

#[test]
fn an_expired_token_with_nothing_to_refresh_it_says_to_run_create_calendar() {
    let fake = FakeGoogle::start();
    let path = temp_path("token.json");
    std::fs::write(&path, token_json(&fake, "stale", "2020-01-01T00:00:00Z", None)).unwrap();

    let error = Google::load(&path, Path::new("credentials.json")).err().unwrap();

    assert!(error.to_string().contains("run create_calendar again"), "{error:#}");
    assert!(refreshes(&fake).is_empty());
    let _ = std::fs::remove_file(&path);
}

#[test]
fn a_missing_token_says_where_it_looked() {
    let path = temp_path("absent.json");

    let error = Google::load(&path, Path::new("credentials.json")).err().unwrap();

    assert!(error.to_string().contains("No usable OAuth token at"), "{error:#}");
}
