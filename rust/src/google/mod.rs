//! Talking to Google: OAuth credentials (calendar_clients/google_auth.py)
//! and one place every request goes through, so errors come back as
//! Google's status codes for callers to act on (a 409 on a create that
//! already happened, a 412 on a stale etag, a 429 to retry...).

pub mod calendar;
pub mod consent;
pub mod sheets;

use anyhow::{anyhow, Context};
use chrono::{DateTime, Duration, NaiveDateTime, Utc};
use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

/// Every scope this app asks for: see calendar_clients/google_auth.py's
/// SCOPES for why each, and why both come from one token.
pub const SCOPES: [&str; 2] =
    ["https://www.googleapis.com/auth/calendar.app.created", "https://www.googleapis.com/auth/drive.file"];

/// google-auth treats a token as expired this long before it really is.
const REFRESH_THRESHOLD_SECONDS: i64 = 3 * 60 + 45;

/// A failed request: Google's status, and its message.
#[derive(Debug, Clone)]
pub struct GoogleError {
    pub status: u16,
    pub message: String,
    pub uri: String,
}

impl std::fmt::Display for GoogleError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "<HttpError {} when requesting {} returned \"{}\">", self.status, self.uri, self.message)
    }
}

impl std::error::Error for GoogleError {}

/// The status of `error` if it's a Google error.
pub fn status_of(error: &anyhow::Error) -> Option<u16> {
    error.downcast_ref::<GoogleError>().map(|e| e.status)
}

#[derive(Debug, Clone)]
struct Credentials {
    token: Option<String>,
    refresh_token: Option<String>,
    token_uri: String,
    client_id: Option<String>,
    client_secret: Option<String>,
    expiry: Option<NaiveDateTime>,
    raw: Map<String, Value>,
}

impl Credentials {
    fn valid(&self) -> bool {
        match (&self.token, self.expiry) {
            (None, _) => false,
            (Some(_), None) => true,
            (Some(_), Some(expiry)) => {
                Utc::now().naive_utc() < expiry - Duration::seconds(REFRESH_THRESHOLD_SECONDS)
            }
        }
    }
}

/// The user's OAuth token, shared by every request, refreshed as it
/// expires and (best effort) written back to `token.json`.
pub struct Google {
    credentials: Mutex<Credentials>,
    token_path: PathBuf,
    agent: ureq::Agent,
    calendar_root: String,
    sheets_root: String,
}

impl Google {
    /// `load_credentials`: the cached token at `token_path`, refreshed if
    /// it's expired. (The consent flow for a missing token is only for
    /// create_calendar, which runs it itself: see `consent`.)
    pub fn load(token_path: &Path, _credentials_path: &Path) -> anyhow::Result<Google> {
        let text = std::fs::read_to_string(token_path)
            .with_context(|| format!("No usable OAuth token at {}", token_path.display()))?;
        let google = Google::from_token_json(&text, token_path)?;
        google.ensure_valid()?;
        Ok(google)
    }

    /// `load_credentials` for a command-line tool: as `load`, but a missing
    /// token -- or one with nothing to refresh it with -- runs the consent
    /// flow first (see consent.rs), as Python's does.
    pub fn load_or_consent(token_path: &Path, credentials_path: &Path) -> anyhow::Result<Google> {
        let usable = match std::fs::read_to_string(token_path) {
            Ok(text) => {
                let google = Google::from_token_json(&text, token_path)?;
                let credentials = google.credentials.lock().unwrap_or_else(|e| e.into_inner()).clone();
                credentials.valid() || credentials.refresh_token.is_some()
            }
            Err(_) => false,
        };
        if !usable {
            consent::consent(token_path, credentials_path)?;
        }
        Google::load(token_path, credentials_path)
    }

    pub fn from_token_json(text: &str, token_path: &Path) -> anyhow::Result<Google> {
        // conformance/'s fake Google (see conformance/fake_google.py).
        let root = std::env::var("GOOGLE_API_ROOT").ok().filter(|r| !r.is_empty());
        Google::from_token_json_at(text, token_path, root)
    }

    /// A client for the Google APIs under `root` (a fake's, for the unit
    /// tests and conformance/), or Google's own with none.
    pub fn from_token_json_at(text: &str, token_path: &Path, root: Option<String>) -> anyhow::Result<Google> {
        let raw: Map<String, Value> = serde_json::from_str(text).context("token.json isn't a JSON object")?;
        let text_of = |key: &str| raw.get(key).and_then(Value::as_str).map(str::to_string);
        let expiry = text_of("expiry").and_then(|e| parse_expiry(&e));
        let credentials = Credentials {
            token: text_of("token"),
            refresh_token: text_of("refresh_token"),
            token_uri: text_of("token_uri").unwrap_or_else(|| "https://oauth2.googleapis.com/token".into()),
            client_id: text_of("client_id"),
            client_secret: text_of("client_secret"),
            expiry,
            raw,
        };
        let (calendar_root, sheets_root) = match root {
            Some(root) => (format!("{root}calendar/v3/"), root),
            None => ("https://www.googleapis.com/calendar/v3/".into(), "https://sheets.googleapis.com/".into()),
        };
        let agent = ureq::AgentBuilder::new().timeout(std::time::Duration::from_secs(120)).build();
        Ok(Google { credentials: Mutex::new(credentials), token_path: token_path.to_path_buf(), agent, calendar_root, sheets_root })
    }

    pub fn calendar_url(&self, path: &str) -> String {
        format!("{}{}", self.calendar_root, path)
    }

    pub fn sheets_url(&self, path: &str) -> String {
        format!("{}{}", self.sheets_root, path)
    }

    fn ensure_valid(&self) -> anyhow::Result<()> {
        let mut credentials = self.credentials.lock().unwrap_or_else(|e| e.into_inner());
        if credentials.valid() {
            return Ok(());
        }
        self.refresh(&mut credentials)
    }

    fn refresh(&self, credentials: &mut Credentials) -> anyhow::Result<()> {
        let refresh_token = credentials
            .refresh_token
            .clone()
            .ok_or_else(|| anyhow!("The OAuth token has expired and has no refresh token; run create_calendar again"))?;
        let mut form = vec![("grant_type", "refresh_token".to_string()), ("refresh_token", refresh_token)];
        if let Some(id) = &credentials.client_id {
            form.push(("client_id", id.clone()));
        }
        if let Some(secret) = &credentials.client_secret {
            form.push(("client_secret", secret.clone()));
        }
        let pairs: Vec<(&str, &str)> = form.iter().map(|(k, v)| (*k, v.as_str())).collect();
        let response = self.agent.post(&credentials.token_uri).send_form(&pairs);
        let body: Value = match response {
            Ok(r) => r.into_json().context("the token endpoint's answer isn't JSON")?,
            Err(ureq::Error::Status(status, r)) => {
                let text = r.into_string().unwrap_or_default();
                anyhow::bail!("Refreshing the OAuth token failed ({status}): {text}")
            }
            Err(e) => return Err(e).context("Refreshing the OAuth token failed"),
        };
        let token = body.get("access_token").and_then(Value::as_str).ok_or_else(|| anyhow!("no access_token"))?;
        credentials.token = Some(token.to_string());
        if let Some(seconds) = body.get("expires_in").and_then(Value::as_i64) {
            credentials.expiry = Some(Utc::now().naive_utc() + Duration::seconds(seconds));
        }
        if let Some(new_refresh) = body.get("refresh_token").and_then(Value::as_str) {
            credentials.refresh_token = Some(new_refresh.to_string());
        }
        // Best effort, as in Python: a read-only Secret File just means the
        // next start refreshes again from the same refresh token.
        let mut raw = credentials.raw.clone();
        raw.insert("token".into(), json!(credentials.token));
        if let Some(expiry) = credentials.expiry {
            raw.insert("expiry".into(), json!(format!("{}Z", expiry.format("%Y-%m-%dT%H:%M:%S%.6f"))));
        }
        if let Err(error) = std::fs::write(&self.token_path, Value::Object(raw).to_string()) {
            tracing::warn!(
                "Could not write refreshed credentials to {}; continuing with in-memory credentials for this run. ({error})",
                self.token_path.display()
            );
        }
        Ok(())
    }

    fn bearer(&self) -> anyhow::Result<String> {
        self.ensure_valid()?;
        let credentials = self.credentials.lock().unwrap_or_else(|e| e.into_inner());
        Ok(format!("Bearer {}", credentials.token.clone().unwrap_or_default()))
    }

    fn force_refresh(&self) -> anyhow::Result<()> {
        let mut credentials = self.credentials.lock().unwrap_or_else(|e| e.into_inner());
        self.refresh(&mut credentials)
    }

    /// One request: `query` as given, `body` as JSON, `if_match` as the
    /// If-Match header. A 401 refreshes the token and tries again once, as
    /// google-auth's authorized transport does. Returns the JSON answer
    /// (`Value::Null` for an empty one).
    pub fn request(
        &self,
        method: &str,
        url: &str,
        query: &[(&str, String)],
        body: Option<&Value>,
        if_match: Option<&str>,
    ) -> anyhow::Result<Value> {
        let mut retried = false;
        loop {
            let mut request = self.agent.request(method, url).set("Authorization", &self.bearer()?);
            for (key, value) in query {
                request = request.query(key, value);
            }
            if let Some(etag) = if_match {
                request = request.set("If-Match", etag);
            }
            let full_uri = request.url().to_string();
            let result = match body {
                Some(body) => request.set("Content-Type", "application/json").send_string(&body.to_string()),
                None => request.call(),
            };
            match result {
                Ok(response) => {
                    let text = response.into_string().context("reading Google's answer")?;
                    if text.trim().is_empty() {
                        return Ok(Value::Null);
                    }
                    return serde_json::from_str(&text).context("Google's answer isn't JSON");
                }
                Err(ureq::Error::Status(401, _)) if !retried => {
                    retried = true;
                    self.force_refresh()?;
                }
                Err(ureq::Error::Status(status, response)) => {
                    let text = response.into_string().unwrap_or_default();
                    let message = serde_json::from_str::<Value>(&text)
                        .ok()
                        .and_then(|v| v.pointer("/error/message").and_then(Value::as_str).map(str::to_string))
                        .unwrap_or(text);
                    return Err(GoogleError { status, message, uri: full_uri }.into());
                }
                Err(error) => return Err(anyhow!(error).context(format!("requesting {full_uri}"))),
            }
        }
    }
}

fn parse_expiry(text: &str) -> Option<NaiveDateTime> {
    let trimmed = text.trim_end_matches('Z');
    NaiveDateTime::parse_from_str(trimmed, "%Y-%m-%dT%H:%M:%S%.f")
        .ok()
        .or_else(|| DateTime::parse_from_rfc3339(text).ok().map(|d| d.naive_utc()))
}

/// Percent-encode one path segment (a calendar or event id), as the API
/// client does: "a@b" becomes "a%40b".
pub fn segment(text: &str) -> String {
    let mut out = String::new();
    for byte in text.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' => out.push(byte as char),
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}
