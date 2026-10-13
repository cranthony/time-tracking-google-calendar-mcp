//! Setting up Google access from a browser, for a server that has none
//! yet -- no calendar (GOOGLE_CALENDAR_ID unset) -- or whose token stopped
//! working. The google_setup_link tool hands out a link, single-use and
//! short-lived, so only a caller the server already trusts (signed in with
//! WorkOS, or through the proxy) can start. The link's /setup sends the
//! browser to Google's consent page; Google sends it back with a code --
//! to /setup/callback for a web application's OAuth client, or, for a
//! desktop app's (which Google only sends back to localhost), as an address
//! the user pastes into /setup/paste. The code becomes a token the server
//! takes on at once: with no calendar, it makes one (and its metadata
//! spreadsheet), as create_calendar does; with one, it checks the new token
//! can see it. The page shows the token, to keep in token.json's secret.
//! Once Google access works, there's nothing to set up: the tool refuses.

use crate::app::App;
use crate::cli::create_calendar::{DEFAULT_DESCRIPTION, DEFAULT_SUMMARY};
use crate::google::calendar::CalendarClient;
use crate::google::consent::{self, Client};
use crate::google::Google;
use crate::write_lock::WRITE_LOCK;
use anyhow::{bail, Context};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

/// How long a link from google_setup_link lasts, unused.
pub const LINK_MINUTES: u64 = 10;
/// How long a started setup lasts, for the user to consent.
const FLOW_MINUTES: u64 = 15;
/// Where Google sends a desktop app's browser back: nothing listens there,
/// so the page fails to load, and the user pastes its address.
pub const DESKTOP_REDIRECT: &str = "http://localhost/";
/// The cookie that ties a started setup to the browser that started it.
pub const COOKIE: &str = "ctt_setup";

/// Why Google access needs setting up.
#[derive(Debug, Clone, PartialEq)]
pub enum Need {
    /// There's no calendar yet.
    FirstSetup,
    /// There's a calendar, but the token doesn't work: why.
    Reconnect(String),
}

impl Need {
    pub fn describe(&self) -> String {
        match self {
            Need::FirstSetup => "GOOGLE_CALENDAR_ID isn't set: signing in makes this app's calendar and its spreadsheet".into(),
            Need::Reconnect(why) => format!("the Google token doesn't work ({why}): signing in again replaces it, keeping the calendar"),
        }
    }
}

struct Flow {
    session: String,
    state: String,
    verifier: String,
    redirect_uri: String,
    client: Client,
    expires: Instant,
}

/// What a finished setup made.
#[derive(Debug, Clone)]
pub struct Finished {
    /// The new token, as token.json holds it.
    pub token: String,
    pub calendar_id: String,
    /// Whether the calendar is new (GOOGLE_CALENDAR_ID wasn't set).
    pub created: bool,
    pub spreadsheet_url: String,
    /// Whether the token was written to GOOGLE_OAUTH_TOKEN_PATH.
    pub saved: bool,
}

/// The setups under way, and the links that start them.
#[derive(Default)]
pub struct Setup {
    base: OnceLock<String>,
    links: Mutex<Vec<(String, Instant)>>,
    flows: Mutex<Vec<Flow>>,
}

/// Whether Google access needs setting up, and why: none if the token
/// works with the calendar.
pub fn need(app: &App) -> Option<Need> {
    let Ok(calendar_id) = app.calendar_id() else {
        return Some(Need::FirstSetup);
    };
    let google = match app.google() {
        Ok(google) => google,
        Err(error) => return Some(Need::Reconnect(format!("{error:#}"))),
    };
    match CalendarClient::new(google, &calendar_id).get_calendar() {
        Ok(_) => None,
        Err(error) => Some(Need::Reconnect(format!("{error:#}"))),
    }
}

impl Setup {
    /// Where this server is reached from a browser (its public URL, without
    /// /mcp): for links and Google's redirect. Set when it starts serving
    /// HTTP; unset over stdio.
    pub fn set_base(&self, base: &str) {
        let _ = self.base.set(base.trim_end_matches('/').to_string());
    }

    pub fn base(&self) -> Option<&str> {
        self.base.get().map(String::as_str)
    }

    /// A new link's token, good once within LINK_MINUTES.
    pub fn new_link(&self) -> String {
        let token = consent::new_secrets().0;
        let mut links = self.links.lock().unwrap_or_else(|e| e.into_inner());
        links.retain(|(_, expires)| *expires > Instant::now());
        links.push((token.clone(), Instant::now() + Duration::from_secs(LINK_MINUTES * 60)));
        token
    }

    /// Whether `token` is a link's, unused and unexpired -- using it up.
    pub fn take_link(&self, token: &str) -> bool {
        let mut links = self.links.lock().unwrap_or_else(|e| e.into_inner());
        links.retain(|(_, expires)| *expires > Instant::now());
        match links.iter().position(|(t, _)| t == token) {
            Some(index) => {
                links.remove(index);
                true
            }
            None => false,
        }
    }

    /// Start a setup with the client in GOOGLE_OAUTH_CREDENTIALS_PATH, from
    /// a browser that reached this server at `base`: its session (for the
    /// browser's cookie), Google's consent page, and whether the client is
    /// a web application's (Google sends the browser back by itself) or a
    /// desktop app's (the user pastes where it lands).
    pub fn start(&self, app: &App, base: &str) -> anyhow::Result<(String, String, bool)> {
        let client = consent::read_client(&app.config.credentials_path)?;
        let redirect_uri = if client.web { format!("{}/setup/callback", base.trim_end_matches('/')) } else { DESKTOP_REDIRECT.to_string() };
        let (state, verifier) = consent::new_secrets();
        let session = consent::new_secrets().0;
        let url = consent::auth_url(&client, &redirect_uri, &state, &verifier);
        let web = client.web;
        let mut flows = self.flows.lock().unwrap_or_else(|e| e.into_inner());
        flows.retain(|f| f.expires > Instant::now());
        flows.push(Flow { session: session.clone(), state, verifier, redirect_uri, client, expires: Instant::now() + Duration::from_secs(FLOW_MINUTES * 60) });
        Ok((session, url, web))
    }

    /// Finish the setup the browser's `session` started, with the `code`
    /// and `state` Google sent back: the server takes on the token, making
    /// the calendar if there's none.
    pub fn finish(&self, app: &App, session: &str, code: &str, state: &str) -> anyhow::Result<Finished> {
        let flow = {
            let mut flows = self.flows.lock().unwrap_or_else(|e| e.into_inner());
            flows.retain(|f| f.expires > Instant::now());
            match flows.iter().position(|f| f.session == session) {
                Some(index) => flows.remove(index),
                None => bail!("This setup has expired, was already finished, or was started in another browser: ask google_setup_link for a new link"),
            }
        };
        if flow.state != state {
            bail!("Google's answer doesn't match this setup (its state differs): ask google_setup_link for a new link");
        }
        let token = consent::exchange(&flow.client, code, &flow.redirect_uri, &flow.verifier)?;
        let google = Arc::new(match app.loaded_google() {
            Some(current) => current.with_token_json(&token)?,
            None => Google::from_token_json(&token, &app.config.token_path)?,
        });
        let (calendar_id, created) = {
            let _held = WRITE_LOCK.hold();
            let found = match app.calendar_id() {
                Ok(id) => {
                    CalendarClient::new(google.clone(), &id).get_calendar().with_context(|| {
                        format!(
                            "The Google account you signed in with can't see the calendar GOOGLE_CALENDAR_ID names ({id}). Sign in with the account that owns it -- and with the OAuth client that made it, since a calendar belongs to the app that created it"
                        )
                    })?;
                    (id, false)
                }
                Err(_) => (CalendarClient::new(google.clone(), "").create_calendar(DEFAULT_SUMMARY, Some(DEFAULT_DESCRIPTION), None)?, true),
            };
            app.install_google(google);
            if found.1 {
                app.set_calendar_id(&found.0);
            }
            found
        };
        let actions = app.actions()?;
        app.noted_time_sheet()?;
        let saved = std::fs::write(&app.config.token_path, &token).is_ok();
        Ok(Finished {
            token,
            calendar_id,
            created,
            spreadsheet_url: format!("https://docs.google.com/spreadsheets/d/{}/edit", actions.spreadsheet_id()),
            saved,
        })
    }
}

// -- the pages ---------------------------------------------------------------------

fn escape(text: &str) -> String {
    text.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;").replace('\'', "&#39;")
}

fn page(title: &str, body: &str) -> String {
    format!(
        r#"<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer"><title>{title}</title>
<style>
body {{ font: 16px/1.5 system-ui, sans-serif; max-width: 42rem; margin: 2rem auto; padding: 0 1rem; color: #1f2328; background: #fff; }}
@media (prefers-color-scheme: dark) {{ body {{ color: #e6edf3; background: #0d1117; }} textarea, code {{ background: #161b22; color: inherit; }} }}
textarea {{ width: 100%; font: 13px ui-monospace, monospace; box-sizing: border-box; }}
code {{ font: 14px ui-monospace, monospace; padding: 0 .2em; }}
.button {{ display: inline-block; padding: .5rem 1rem; border-radius: .4rem; background: #1f6feb; color: #fff; text-decoration: none; border: 0; font: inherit; cursor: pointer; }}
.warn {{ border-left: 4px solid #d29922; padding-left: .75rem; }}
</style></head><body><h1>{title}</h1>
{body}
</body></html>"#,
        title = escape(title)
    )
}

/// A page saying why setup can't go on.
pub fn error_page(message: &str) -> String {
    page("Google setup didn't work", &format!("<p>{}</p>", escape(message)))
}

/// The page for a desktop app's client: sign in, then paste where Google
/// sent the browser.
pub fn paste_page(auth_url: &str) -> String {
    page(
        "Set up Google access",
        &format!(
            r#"<p><a class="button" href="{url}" target="_blank" rel="noopener">Sign in with Google</a></p>
<p>Google will then send that tab to an address starting <code>{redirect}</code> that doesn't load. That's expected: copy the whole address from that tab's address bar, paste it here, and finish.</p>
<form method="post" action="/setup/paste">
<p><textarea name="address" rows="4" required placeholder="{redirect}?state=...&amp;code=..."></textarea></p>
<p><button class="button" type="submit">Finish</button></p>
</form>"#,
            url = escape(auth_url),
            redirect = escape(DESKTOP_REDIRECT)
        ),
    )
}

/// The page once set up: the calendar, and the token to keep.
pub fn finished_page(finished: &Finished) -> String {
    let calendar = if finished.created {
        format!(
            "<p>Made this app's calendar. Set <code>GOOGLE_CALENDAR_ID</code> to <code>{id}</code> where the server is deployed, so it's still used after a restart.</p>",
            id = escape(&finished.calendar_id)
        )
    } else {
        format!("<p>The new token works with the calendar (<code>{}</code>).</p>", escape(&finished.calendar_id))
    };
    let saved = if finished.saved {
        "<p>The token was also written to <code>GOOGLE_OAUTH_TOKEN_PATH</code>.</p>"
    } else {
        "<p class=\"warn\">The token couldn't be written to <code>GOOGLE_OAUTH_TOKEN_PATH</code> (a read-only secret, say): the server is using it now, but it's lost when the server restarts unless you keep it below.</p>"
    };
    page(
        "Google access is set up",
        &format!(
            r#"{calendar}
<p>Metadata spreadsheet: <a href="{sheet}">{sheet}</a></p>
{saved}
<h2>The token</h2>
<p>Keep it as the server's <code>token.json</code>: on Render, paste it into the <code>token.json</code> Secret File; on Cloud Run, add it as a new version of the secret mounted at <code>GOOGLE_OAUTH_TOKEN_PATH</code> (<code>gcloud secrets versions add SECRET --data-file=-</code>, then paste it). It's a credential: don't keep it anywhere else. This page isn't cached; it's shown only now.</p>
<p><textarea id="token" rows="8" readonly>{token}</textarea></p>
<p><button class="button" type="button" onclick="navigator.clipboard.writeText(document.getElementById('token').value).then(() => this.textContent = 'Copied')">Copy the token</button></p>"#,
            sheet = escape(&finished.spreadsheet_url),
            token = escape(&finished.token),
        ),
    )
}

#[cfg(test)]
#[path = "setup_tests.rs"]
pub(crate) mod tests;
