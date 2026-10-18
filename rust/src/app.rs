//! The server's state: configuration, the Google clients, and each store,
//! built the first time a tool needs it -- under WRITE_LOCK, since building
//! one can write (a new spreadsheet or tab). Each store makes its own
//! look at the calendar's metadata spreadsheet, then its tab.

use crate::google::calendar::CalendarClient;
use crate::google::sheets::SheetsClient;
use crate::google::Google;
use crate::health::{HealthRecorder, HealthTab, HealthWriter, SheetWarning};
use crate::sheet::{ensure_spreadsheet, retitle_spreadsheet};
use crate::stores::actions::Actions;
use crate::stores::cancellations::Cancellations;
use crate::stores::goals::Goals;
use crate::stores::habits::{action_scopes, Habits};
use crate::stores::locations::Locations;
use crate::stores::people::People;
use crate::stores::traits::Traits;
use crate::stores::journal::CompactionJournal;
use crate::stores::notes::NotedTimeSheet;
use crate::stores::schedule::CompactionSchedule;
use crate::stores::scoring::ScoringDefaultsTab;
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
    /// Setting up Google access from a browser (crate::setup).
    pub setup: crate::setup::Setup,
    /// The calendar a setup made, where GOOGLE_CALENDAR_ID wasn't set.
    calendar_made: OnceLock<String>,
    google: OnceLock<Arc<Google>>,
    calendar: OnceLock<CalendarClient>,
    locations: OnceLock<Locations>,
    actions: OnceLock<Actions>,
    people: OnceLock<People>,
    traits: OnceLock<Traits>,
    habits: OnceLock<Habits>,
    goals: OnceLock<Goals>,
    cancellations: OnceLock<Cancellations>,
    journal: OnceLock<CompactionJournal>,
    notes: OnceLock<NotedTimeSheet>,
    schedule: OnceLock<CompactionSchedule>,
    scoring_defaults: OnceLock<ScoringDefaultsTab>,
    recurrences: OnceLock<Recurrences>,
    writer: OnceLock<Arc<HealthWriter>>,
    sheets_refresh: OnceLock<Refresh>,
    calendar_sync: OnceLock<Refresh>,
    me: OnceLock<std::sync::Weak<App>>,
}

/// How often the server brings what it keeps up to date: the metadata
/// spreadsheet checked against its copy (TIME_TRACKER_SHEETS_REFRESH), or
/// the calendar synced (TIME_TRACKER_CALENDAR_SYNC). Every so many
/// seconds, in the background (the default, every 300); before every tool
/// call ("every_call", for tests that change things behind its back); or
/// never ("off"). A tool that writes syncs the calendar as it first reads
/// it, whatever this says.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refresh {
    Every(std::time::Duration),
    EveryCall,
    Off,
}

pub const REFRESH_SECONDS: u64 = 300;

impl Refresh {
    pub fn from_var(value: Option<&str>) -> Refresh {
        match value.map(str::trim) {
            Some("off") => Refresh::Off,
            Some("every_call") => Refresh::EveryCall,
            Some(seconds) => match seconds.parse::<u64>() {
                Ok(n) if n > 0 => Refresh::Every(std::time::Duration::from_secs(n)),
                _ => Refresh::Every(std::time::Duration::from_secs(REFRESH_SECONDS)),
            },
            None => Refresh::Every(std::time::Duration::from_secs(REFRESH_SECONDS)),
        }
    }

    /// Run `work` from a thread of its own named `name`, every period this
    /// says, while `app` lives.
    fn start(self, app: &Arc<App>, name: &str, work: fn(&App)) {
        let Refresh::Every(period) = self else { return };
        let app = Arc::downgrade(app);
        let spawned = std::thread::Builder::new().name(name.into()).spawn(move || loop {
            std::thread::sleep(period);
            let Some(app) = app.upgrade() else { return };
            work(&app);
        });
        if let Err(error) = spawned {
            tracing::error!("couldn't start {name}: {error}");
        }
    }
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
            setup: crate::setup::Setup::default(),
            calendar_made: OnceLock::new(),
            google: OnceLock::new(),
            calendar: OnceLock::new(),
            locations: OnceLock::new(),
            actions: OnceLock::new(),
            people: OnceLock::new(),
            traits: OnceLock::new(),
            habits: OnceLock::new(),
            goals: OnceLock::new(),
            cancellations: OnceLock::new(),
            journal: OnceLock::new(),
            notes: OnceLock::new(),
            schedule: OnceLock::new(),
            scoring_defaults: OnceLock::new(),
            recurrences: OnceLock::new(),
            writer: OnceLock::new(),
            sheets_refresh: OnceLock::new(),
            calendar_sync: OnceLock::new(),
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

    /// The credentials, if they've been loaded.
    pub fn loaded_google(&self) -> Option<Arc<Google>> {
        self.google.get().cloned()
    }

    /// Use `google`'s token from now on: in place of the one loaded (for
    /// every client holding it), or as the first.
    pub fn install_google(&self, google: Arc<Google>) {
        match self.google.get() {
            Some(current) => current.take_credentials_of(&google),
            None => {
                let _ = self.google.set(google);
            }
        }
    }

    /// The calendar: GOOGLE_CALENDAR_ID's, or the one a setup made.
    pub fn calendar_id(&self) -> anyhow::Result<String> {
        match self.calendar_made.get() {
            Some(id) => Ok(id.clone()),
            None => self.config.calendar_id().map(str::to_string),
        }
    }

    /// Use `id`, a calendar a setup just made, where GOOGLE_CALENDAR_ID
    /// isn't set.
    pub fn set_calendar_id(&self, id: &str) {
        let _ = self.calendar_made.set(id.to_string());
    }

    /// `_build_calendar_and_sheets_clients`: a fresh pair of clients.
    pub fn clients(&self) -> anyhow::Result<(CalendarClient, SheetsClient)> {
        let google = self.google()?;
        Ok((CalendarClient::new(google.clone(), &self.calendar_id()?), SheetsClient::new(google)))
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
            let me = self.me();
            let habits: crate::stores::foundations::NodesFn = Arc::new(move || me.habits()?.nodes());
            Ok(People::ensure(&sheets, &id, Some(self.trait_ids_fn()))?.with_habits(habits))
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
            let me = self.me();
            let people: crate::stores::foundations::NodesFn = Arc::new(move || me.people()?.nodes());
            Ok(Habits::ensure(&sheets, &id, Some(scopes), Some(self.trait_ids_fn()))?.with_people(people))
        })
    }

    pub fn goals(&self) -> anyhow::Result<Goals> {
        built(&self.goals, || {
            let (_, sheets, id) = self.spreadsheet()?;
            Goals::ensure(&sheets, &id)
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

    pub fn scoring_defaults(&self) -> anyhow::Result<ScoringDefaultsTab> {
        built(&self.scoring_defaults, || {
            let (_, sheets, id) = self.spreadsheet()?;
            ScoringDefaultsTab::ensure(&sheets, &id)
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
    /// The writer opens it as the server starts, so this is also where a
    /// spreadsheet found under another title gets its own back.
    pub fn health_tab(&self) -> anyhow::Result<HealthTab> {
        let _held = WRITE_LOCK.hold();
        let (calendar, sheets) = self.clients()?;
        let (id, new) = ensure_spreadsheet(&calendar, &sheets)?;
        if !new {
            retitle_spreadsheet(&sheets, &id)?;
        }
        HealthTab::ensure(&sheets, &id)
    }

    /// Start writing health metrics in the background -- not with
    /// TIME_TRACKER_HEALTH_WRITER=off (conformance/ compares each tool's requests,
    /// which the writer's would interleave with).
    pub fn start_health_writer(self: &Arc<Self>) {
        if std::env::var("TIME_TRACKER_HEALTH_WRITER").as_deref() == Ok("off") {
            return;
        }
        let _ = self.writer.set(HealthWriter::start(self.clone()));
    }

    /// Renew recurring series before Google stops expanding them
    /// (Recurrences::renew) from a thread of its own: a minute after the
    /// server starts, then daily -- hourly while it fails (before Google is
    /// set up, say). Not with TIME_TRACKER_SERIES_RENEWAL=off (conformance/
    /// compares each tool's requests, which these would interleave with).
    pub fn start_series_renewal(self: &Arc<Self>) {
        if std::env::var("TIME_TRACKER_SERIES_RENEWAL").as_deref() == Ok("off") {
            return;
        }
        let app = Arc::downgrade(self);
        let spawned = std::thread::Builder::new().name("series-renewal".into()).spawn(move || {
            let mut wait = std::time::Duration::from_secs(60);
            loop {
                std::thread::sleep(wait);
                let Some(app) = app.upgrade() else { return };
                let renewed = (|| {
                    let _held = WRITE_LOCK.hold();
                    app.recurrences()?.renew(&crate::determinism::now())
                })();
                wait = match renewed {
                    Ok(_) => std::time::Duration::from_secs(24 * 60 * 60),
                    Err(error) => {
                        tracing::warn!("series renewal: {error:#}");
                        std::time::Duration::from_secs(60 * 60)
                    }
                };
            }
        });
        if let Err(error) = spawned {
            tracing::error!("couldn't start series renewal: {error}");
        }
    }

    pub fn sheets_refresh(&self) -> Refresh {
        *self.sheets_refresh.get_or_init(|| Refresh::from_var(std::env::var("TIME_TRACKER_SHEETS_REFRESH").ok().as_deref()))
    }

    pub fn calendar_sync(&self) -> Refresh {
        *self.calendar_sync.get_or_init(|| Refresh::from_var(std::env::var("TIME_TRACKER_CALENDAR_SYNC").ok().as_deref()))
    }

    /// Sync what the server keeps of the calendar (see CalendarClient::sync)
    /// and warn of what changed elsewhere. Its failures are logged, not
    /// returned: a read syncs again when it needs to, and fails then.
    pub fn sync_calendar(&self) {
        if self.google.get().is_none() {
            return;
        }
        let Ok(client) = self.calendar_client() else { return };
        if let Err(error) = client.sync() {
            tracing::warn!("couldn't sync the calendar: {error:#}");
        }
        self.warn_of_outside_changes();
    }

    /// Warn of the events syncs brought that the server didn't write --
    /// changed outside it -- since this last looked: one warning for them
    /// all (see HealthRecorder::sheet_warnings).
    pub fn warn_of_outside_changes(&self) {
        let Some(client) = self.google.get().and_then(|_| self.calendar_client().ok()) else { return };
        let changed = client.take_outside();
        if changed.is_empty() {
            return;
        }
        tracing::warn!("{} event(s) were changed outside the server", changed.len());
        self.health.record_sheet_warnings(vec![SheetWarning::of_calendar(&changed, crate::health::now_ms())]);
    }

    /// Have the next read of the calendar sync first: before a tool that
    /// writes, so it writes on what's there now. Free for one that never
    /// reads the calendar.
    pub fn calendar_stale(&self) {
        if let Some(client) = self.google.get().and_then(|_| self.calendar_client().ok()) {
            client.mark_stale();
        }
    }

    /// Sync the calendar every so often from a thread of its own (unless
    /// TIME_TRACKER_CALENDAR_SYNC says otherwise), as well as before every
    /// tool that writes. It never holds the write lock.
    pub fn start_calendar_sync(self: &Arc<Self>) {
        self.calendar_sync().start(self, "calendar-sync", App::sync_calendar);
    }

    /// Check each spreadsheet the server keeps against Sheets, putting back
    /// whatever was changed by hand (see `SheetsClient::refresh`), and warn
    /// of what was (see `HealthRecorder::sheet_warnings`). With `wait`, wait
    /// for the write lock to put things back; else a tool writing leaves
    /// them for the next refresh, which warns of them then. Its failures
    /// are logged, not returned: nothing waits on a refresh.
    pub fn refresh_sheets(&self, wait: bool) {
        let Some(google) = self.google.get() else { return };
        let sheets = SheetsClient::new(google.clone());
        for spreadsheet_id in sheets.kept_spreadsheets() {
            match sheets.refresh(&spreadsheet_id, &[crate::sheet::HEALTH.0], wait) {
                Ok(refreshed) if refreshed.is_empty() || refreshed.deferred => {}
                Ok(refreshed) => {
                    tracing::warn!("the metadata spreadsheet was changed by hand: {refreshed:?}");
                    self.health.record_sheet_warnings(SheetWarning::of(&refreshed, crate::health::now_ms()));
                }
                Err(error) => tracing::warn!("couldn't check the metadata spreadsheet {spreadsheet_id}: {error:#}"),
            }
        }
    }

    /// Refresh the spreadsheets every so often from a thread of its own
    /// (unless TIME_TRACKER_SHEETS_REFRESH says otherwise). It gives way to
    /// tools: it reads without the write lock, and puts back what it finds
    /// only when no tool holds the lock.
    pub fn start_sheets_refresher(self: &Arc<Self>) {
        self.sheets_refresh().start(self, "sheets-refresher", |app| app.refresh_sheets(false));
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
