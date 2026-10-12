//! Create a new Google Calendar for this app to use, and print its ID
//! (create_calendar.py, ported).
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

use ctt::app::{App, Config};
use ctt::google::calendar::{is_label_conflict, CalendarClient};
use ctt::google::Google;
use ctt::write_lock::WRITE_LOCK;
use std::sync::Arc;

const DEFAULT_SUMMARY: &str = "Time Tracking";
const DEFAULT_DESCRIPTION: &str = "Calendar managed by Cascading Time Tracker";
const USAGE: &str = "usage: create_calendar [-h] [--description DESCRIPTION] [summary]";

fn main() {
    if let Err(error) = run() {
        eprintln!("{error:#}");
        std::process::exit(1);
    }
}

fn run() -> anyhow::Result<()> {
    ctt::dotenv::load(std::path::Path::new(".env"));
    let mut summary: Option<String> = None;
    let mut description = DEFAULT_DESCRIPTION.to_string();
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "-h" | "--help" => {
                println!("{USAGE}\n\nCreate a new Google Calendar for this app to use, and print its ID.");
                return Ok(());
            }
            "--description" => description = args.next().ok_or_else(|| anyhow::anyhow!("{USAGE}\nargument --description: expected one argument"))?,
            other if other.starts_with("--description=") => description = other["--description=".len()..].to_string(),
            other if summary.is_none() && !other.starts_with('-') => summary = Some(other.to_string()),
            other => anyhow::bail!("{USAGE}\nunrecognized arguments: {other}"),
        }
    }
    let summary = summary.unwrap_or_else(|| DEFAULT_SUMMARY.to_string());
    let mut config = Config::from_env();
    let _held = WRITE_LOCK.hold();

    let calendar_id = match config.calendar_id.clone() {
        Some(existing) => {
            println!("GOOGLE_CALENDAR_ID is already set to {} -- not creating a new calendar.", ctt::py::repr(&existing));
            existing
        }
        None => {
            let google = Arc::new(Google::load_or_consent(&config.token_path, &config.credentials_path)?);
            let id = CalendarClient::new(google, "").create_calendar(&summary, Some(&description), None)?;
            println!("Created calendar {} with id: {id}", ctt::py::repr(&summary));
            println!("Set GOOGLE_CALENDAR_ID to this value.");
            id
        }
    };
    // A token is needed below either way: the consent flow if there's none.
    Google::load_or_consent(&config.token_path, &config.credentials_path)?;
    config.calendar_id = Some(calendar_id);
    let app = Arc::new(App::new(config));
    app.set_self();
    let made = app.actions().and_then(|actions| {
        app.noted_time_sheet()?;
        Ok(actions.spreadsheet_id().to_string())
    });
    let spreadsheet_id = match made {
        Ok(id) => id,
        // Another writer changed the calendar meanwhile (a concurrent run of
        // this same tool): running again just finds what it made.
        Err(error) if is_label_conflict(&error) => {
            eprintln!("{error}");
            std::process::exit(1);
        }
        Err(error) => return Err(error),
    };
    println!("Calendar metadata sheet: https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit");
    Ok(())
}
