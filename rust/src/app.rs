//! The server's state: configuration, the Google clients, and each store,
//! built the first time a tool needs it -- under WRITE_LOCK, since building
//! one can write (a new spreadsheet or tab). Each store makes its own
//! look at the calendar's metadata spreadsheet, then its tab.

use crate::google::calendar::CalendarClient;
use crate::google::sheets::SheetsClient;
use crate::google::Google;
use crate::health::{HealthRecorder, HealthTab, HealthWriter};
use crate::sheet::ensure_spreadsheet;
use crate::stores::actions::Actions;
use crate::stores::cancellations::Cancellations;
use crate::stores::habits::{action_scopes, Habits};
use crate::stores::locations::Locations;
use crate::stores::people::People;
use crate::stores::traits::Traits;
use crate::stores::journal::CompactionJournal;
use crate::stores::notes::NotedTimeSheet;
use crate::stores::schedule::CompactionSchedule;
use crate::stores::recurrences::Recurrences;
use crate::stores::action_calendar::ActionCalendar;
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
        Config::from_vars(|name| std::env::var(name).ok())
    }

    /// The configuration from `get`, an environment's variables.
    pub fn from_vars(get: impl Fn(&str) -> Option<String>) -> Config {
        let var = |name: &str| get(name).filter(|v| !v.is_empty());
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
    actions: OnceLock<Actions>,
    people: OnceLock<People>,
    traits: OnceLock<Traits>,
    habits: OnceLock<Habits>,
    cancellations: OnceLock<Cancellations>,
    journal: OnceLock<CompactionJournal>,
    notes: OnceLock<NotedTimeSheet>,
    schedule: OnceLock<CompactionSchedule>,
    recurrences: OnceLock<Recurrences>,
    writer: OnceLock<Arc<HealthWriter>>,
    me: OnceLock<std::sync::Weak<App>>,
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
            actions: OnceLock::new(),
            people: OnceLock::new(),
            traits: OnceLock::new(),
            habits: OnceLock::new(),
            cancellations: OnceLock::new(),
            journal: OnceLock::new(),
            notes: OnceLock::new(),
            schedule: OnceLock::new(),
            recurrences: OnceLock::new(),
            writer: OnceLock::new(),
            me: OnceLock::new(),
        }
    }

    /// An app on `google`, already loaded (create_calendar's, or the unit
    /// tests' fake), rather than token.json.
    pub fn with_google(config: Config, google: Arc<Google>) -> App {
        let app = App::new(config);
        let _ = app.google.set(google);
        app
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

    /// Let stores reach back for others lazily (a person's traits are only
    /// checked against the Traits tab when they're set).
    pub fn set_self(self: &Arc<Self>) {
        let _ = self.me.set(Arc::downgrade(self));
    }

    fn me(&self) -> Arc<App> {
        self.me.get().and_then(std::sync::Weak::upgrade).expect("App::set_self was called")
    }

    pub fn actions(&self) -> anyhow::Result<Actions> {
        built(&self.actions, || {
            let (calendar, sheets, id) = self.spreadsheet()?;
            Actions::ensure(&calendar, &sheets, &id)
        })
    }

    pub fn traits(&self) -> anyhow::Result<Traits> {
        built(&self.traits, || {
            let (_, sheets, id) = self.spreadsheet()?;
            Traits::ensure(&sheets, &id)
        })
    }

    fn trait_ids_fn(&self) -> crate::stores::people::TraitIds {
        let me = self.me();
        Arc::new(move || me.traits()?.ids())
    }

    pub fn people(&self) -> anyhow::Result<People> {
        built(&self.people, || {
            let (_, sheets, id) = self.spreadsheet()?;
            People::ensure(&sheets, &id, Some(self.trait_ids_fn()))
        })
    }

    pub fn habits(&self) -> anyhow::Result<Habits> {
        built(&self.habits, || {
            let (_, sheets, id) = self.spreadsheet()?;
            let me = self.me();
            let scopes: crate::stores::habits::ScopesFn = Arc::new(move || {
                let actions = me.actions()?;
                Ok(action_scopes(&actions.all()?, &actions.groups()?))
            });
            Habits::ensure(&sheets, &id, Some(scopes), Some(self.trait_ids_fn()))
        })
    }

    pub fn cancellations(&self) -> anyhow::Result<Cancellations> {
        built(&self.cancellations, || {
            let (actions, people, traits, habits) = (self.actions()?, self.people()?, self.traits()?, self.habits()?);
            let (_, sheets, id) = self.spreadsheet()?;
            Cancellations::ensure(&sheets, &id, people, traits, actions, Some(habits))
        })
    }

    pub fn journal(&self) -> anyhow::Result<CompactionJournal> {
        built(&self.journal, || {
            let (_, sheets, id) = self.spreadsheet()?;
            CompactionJournal::ensure(&sheets, &id)
        })
    }

    pub fn noted_time_sheet(&self) -> anyhow::Result<NotedTimeSheet> {
        built(&self.notes, || {
            let (_, sheets, id) = self.spreadsheet()?;
            NotedTimeSheet::ensure(&sheets, &id)
        })
    }

    pub fn compaction_schedule(&self) -> anyhow::Result<CompactionSchedule> {
        built(&self.schedule, || {
            let (_, sheets, id) = self.spreadsheet()?;
            CompactionSchedule::ensure(&sheets, &id)
        })
    }

    /// Recurrences, kept (with its ActionCalendar) for the server's life.
    pub fn recurrences(&self) -> anyhow::Result<Recurrences> {
        built(&self.recurrences, || {
            let client = self.calendar_client()?;
            Ok(Recurrences::new(ActionCalendar::new(client, self.actions()?)))
        })
    }

    /// The Health tab, for the writer: finding (or making) the metadata
    /// spreadsheet may write to the calendar, so this holds WRITE_LOCK.
    pub fn health_tab(&self) -> anyhow::Result<HealthTab> {
        let _held = WRITE_LOCK.hold();
        let (_, sheets, id) = self.spreadsheet()?;
        HealthTab::ensure(&sheets, &id)
    }

    /// Start writing health metrics in the background -- not with
    /// CTT_HEALTH_WRITER=off (conformance/ compares each tool's requests,
    /// which the writer's would interleave with).
    pub fn start_health_writer(self: &Arc<Self>) {
        if std::env::var("CTT_HEALTH_WRITER").as_deref() == Ok("off") {
            return;
        }
        let _ = self.writer.set(HealthWriter::start(self.clone()));
    }

    pub fn health_writer(&self) -> Option<&Arc<HealthWriter>> {
        self.writer.get()
    }

    pub fn locations(&self) -> anyhow::Result<Locations> {
        built(&self.locations, || {
            let (_, sheets, id) = self.spreadsheet()?;
            Locations::ensure(&sheets, &id)
        })
    }
}

#[cfg(test)]
#[path = "app_tests.rs"]
mod tests;
