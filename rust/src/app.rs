//! The server's state: configuration, the Google clients, and each store,
//! built the first time a tool needs it -- under WRITE_LOCK, since building
//! one can write (a new spreadsheet or tab) -- as server.py's get_*
//! helpers and config.py's build_* functions do. Each store is built the
//! way Python builds it, request for request: its own look at the
//! calendar's metadata spreadsheet, then its tab.

use crate::google::calendar::CalendarClient;
use crate::google::sheets::SheetsClient;
use crate::google::Google;
use crate::health::HealthRecorder;
use crate::sheet::ensure_spreadsheet;
use crate::stores::locations::Locations;
use crate::write_lock::WRITE_LOCK;
use anyhow::{anyhow, Context};
use std::path::PathBuf;
use std::sync::{Arc, OnceLock};

pub struct Config {
    pub calendar_id: Option<String>,
    pub token_path: PathBuf,
    pub credentials_path: PathBuf,
}

impl Config {
    pub fn from_env() -> Config {
        let var = |name: &str| std::env::var(name).ok().filter(|v| !v.is_empty());
        Config {
            calendar_id: var("GOOGLE_CALENDAR_ID"),
            token_path: PathBuf::from(var("GOOGLE_OAUTH_TOKEN_PATH").unwrap_or_else(|| "token.json".into())),
            credentials_path: PathBuf::from(var("GOOGLE_OAUTH_CREDENTIALS_PATH").unwrap_or_else(|| "credentials.json".into())),
        }
    }

    pub fn calendar_id(&self) -> anyhow::Result<&str> {
        self.calendar_id.as_deref().ok_or_else(|| {
            anyhow!(
                "GOOGLE_CALENDAR_ID is not set. Run create_calendar to create this app's dedicated calendar and print its ID, then set GOOGLE_CALENDAR_ID to it -- see the README's \"Calendar access model\" section."
            )
        })
    }
}

pub struct App {
    pub config: Config,
    pub health: HealthRecorder,
    google: OnceLock<Arc<Google>>,
    calendar: OnceLock<CalendarClient>,
    locations: OnceLock<Locations>,
}

/// Build a store once, under WRITE_LOCK -- checking again once the lock is
/// held, in case another call built it meanwhile.
fn built<T: Clone>(slot: &OnceLock<T>, build: impl FnOnce() -> anyhow::Result<T>) -> anyhow::Result<T> {
    if let Some(value) = slot.get() {
        return Ok(value.clone());
    }
    let _held = WRITE_LOCK.hold();
    if let Some(value) = slot.get() {
        return Ok(value.clone());
    }
    let value = build()?;
    let _ = slot.set(value.clone());
    Ok(value)
}

impl App {
    pub fn new(config: Config) -> App {
        App {
            config,
            health: HealthRecorder::new(),
            google: OnceLock::new(),
            calendar: OnceLock::new(),
            locations: OnceLock::new(),
        }
    }

    /// The credentials, loaded (and refreshed if need be) once.
    pub fn google(&self) -> anyhow::Result<Arc<Google>> {
        if let Some(google) = self.google.get() {
            return Ok(google.clone());
        }
        let google = Arc::new(
            Google::load(&self.config.token_path, &self.config.credentials_path).context("loading the OAuth credentials")?,
        );
        let _ = self.google.set(google);
        Ok(self.google.get().unwrap().clone())
    }

    /// `_build_calendar_and_sheets_clients`: a fresh pair of clients.
    pub fn clients(&self) -> anyhow::Result<(CalendarClient, SheetsClient)> {
        let google = self.google()?;
        Ok((CalendarClient::new(google.clone(), self.config.calendar_id()?), SheetsClient::new(google)))
    }

    pub fn calendar_client(&self) -> anyhow::Result<CalendarClient> {
        built(&self.calendar, || Ok(self.clients()?.0))
    }

    /// The metadata spreadsheet, looked up afresh, as each build_* does.
    fn spreadsheet(&self) -> anyhow::Result<(CalendarClient, SheetsClient, String)> {
        let (calendar, sheets) = self.clients()?;
        let (id, _new) = ensure_spreadsheet(&calendar, &sheets)?;
        Ok((calendar, sheets, id))
    }

    pub fn locations(&self) -> anyhow::Result<Locations> {
        built(&self.locations, || {
            let (_, sheets, id) = self.spreadsheet()?;
            Locations::ensure(&sheets, &id)
        })
    }
}
