//! Create a new Google Calendar for this app to use, and print its ID
//! (create_calendar.py, ported; the create_calendar binary runs it).
//!
//! This app's OAuth scope (calendar.app.created) only lets it see and
//! manage calendars it has created itself -- see the README's "Calendar
//! access model". Run this once to create that dedicated calendar, then
//! set GOOGLE_CALENDAR_ID to the id it prints. It also makes sure the
//! calendar's metadata spreadsheet exists, with its Actions and noted-times
//! tabs, and is safe to run again: with GOOGLE_CALENDAR_ID already set, no
//! calendar is created, and only what's missing of the spreadsheet is.
//! The first run, with no token yet, opens the browser to ask for access.
//!
//! Usage:
//!     create_calendar ["Calendar name"] [--description "..."]

use crate::app::{App, Config};
use crate::google::calendar::{is_label_conflict, CalendarClient};
use crate::google::Google;
use crate::write_lock::WRITE_LOCK;
use std::io::Write;
use std::sync::Arc;

pub const DEFAULT_SUMMARY: &str = "Time Tracking";
pub const DEFAULT_DESCRIPTION: &str = "Calendar managed by Cascading Time Tracker";
const USAGE: &str = "usage: create_calendar [-h] [--description DESCRIPTION] [summary]";

pub fn main() {
    crate::dotenv::load(std::path::Path::new(".env"));
    let args: Vec<String> = std::env::args().skip(1).collect();
    let result = (|| -> anyhow::Result<()> {
        if args.iter().any(|a| a == "-h" || a == "--help") {
            println!("{USAGE}\n\nCreate a new Google Calendar for this app to use, and print its ID.");
            return Ok(());
        }
        let config = Config::from_env();
        // The consent flow first, if there's no token yet.
        let google = Arc::new(Google::load_or_consent(&config.token_path, &config.credentials_path)?);
        run_with(&args, config, google, &mut std::io::stdout())
    })();
    match result {
        Ok(()) => {}
        // Another writer changed the calendar meanwhile (a concurrent run of
        // this same tool): running again just finds what it made.
        Err(error) if is_label_conflict(&error) => {
            eprintln!("{error}");
            std::process::exit(1);
        }
        Err(error) => {
            eprintln!("{error:#}");
            std::process::exit(1);
        }
    }
}

/// Create the calendar (unless `config` names one) and its metadata
/// spreadsheet, on `google`, printing to `out`.
pub fn run_with(args: &[String], mut config: Config, google: Arc<Google>, out: &mut dyn Write) -> anyhow::Result<()> {
    let mut summary: Option<String> = None;
    let mut description = DEFAULT_DESCRIPTION.to_string();
    let mut args = args.iter();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--description" => description = args.next().cloned().ok_or_else(|| anyhow::anyhow!("{USAGE}\nargument --description: expected one argument"))?,
            other if other.starts_with("--description=") => description = other["--description=".len()..].to_string(),
            other if summary.is_none() && !other.starts_with('-') => summary = Some(other.to_string()),
            other => anyhow::bail!("{USAGE}\nunrecognized arguments: {other}"),
        }
    }
    let summary = summary.unwrap_or_else(|| DEFAULT_SUMMARY.to_string());
    let _held = WRITE_LOCK.hold();

    let calendar_id = match config.calendar_id.clone() {
        Some(existing) => {
            writeln!(out, "GOOGLE_CALENDAR_ID is already set to {} -- not creating a new calendar.", crate::py::repr(&existing))?;
            existing
        }
        None => {
            let id = CalendarClient::new(google.clone(), "").create_calendar(&summary, Some(&description), None)?;
            writeln!(out, "Created calendar {} with id: {id}", crate::py::repr(&summary))?;
            writeln!(out, "Set GOOGLE_CALENDAR_ID to this value.")?;
            id
        }
    };
    config.calendar_id = Some(calendar_id);
    let app = Arc::new(App::with_google(config, google));
    app.set_self();
    let actions = app.actions()?;
    app.noted_time_sheet()?;
    writeln!(out, "Calendar metadata sheet: https://docs.google.com/spreadsheets/d/{}/edit", actions.spreadsheet_id())?;
    Ok(())
}

#[cfg(test)]
#[path = "create_calendar_tests.rs"]
mod tests;
