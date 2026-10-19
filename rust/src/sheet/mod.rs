//! The calendar's metadata spreadsheet and its row-per-item tabs.

pub mod rows;

use crate::google::calendar::CalendarClient;
use crate::google::kept::TabIdentity;
use crate::google::sheets::SheetsClient;
use serde_json::{json, Value};

/// The server keeps its own copy of what's in the spreadsheet, so an edit
/// made by hand is overwritten -- the title says so.
pub const SPREADSHEET_TITLE: &str = "Calendar Metadata (do not edit by hand)";
const SPREADSHEET_ID_METADATA_KEY: &str = "calendar-metadata-spreadsheet-id";
const SHEET_ROLE_METADATA_KEY: &str = "sheet-role";
pub const MIN_TAB_ROWS: i64 = 1000;

pub fn tab_color() -> Value {
    json!({"red": 0.26, "green": 0.52, "blue": 0.96})
}

pub const TIME_NOTES: (&str, &str) = ("uncompacted-time-notes", "Noted Times");
pub const COMPACTIONS: (&str, &str) = ("compactions", "Compactions");
pub const TRAITS: (&str, &str) = ("traits", "Traits");
pub const ACTIONS: (&str, &str) = ("actions", "Actions");
pub const ACTION_GROUPS: (&str, &str) = ("action-groups", "Action Groups");
pub const PEOPLE: (&str, &str) = ("people", "People");
pub const CIRCLES: (&str, &str) = ("circles", "Circles");
pub const LOCATIONS: (&str, &str) = ("locations", "Locations");
pub const LOCATION_GROUPS: (&str, &str) = ("location-groups", "Location Groups");
pub const TRAVEL_TIMES: (&str, &str) = ("travel-times", "Travel Times");
pub const HABITS: (&str, &str) = ("habits", "Habits");
// Not "goals": the old goals' tab (replaced by actions) may still be there.
pub const GOALS: (&str, &str) = ("outcome-goals", "Outcome Goals");
pub const COMPACTION_SCHEDULE: (&str, &str) = ("compaction-schedule", "Compaction Schedule");
pub const CANCELLATIONS: (&str, &str) = ("cancellations", "Cancellations");
pub const PERSON_FACTS: (&str, &str) = ("person-facts", "Person Facts");
pub const FACT_LOGS: (&str, &str) = ("fact-logs", "Fact Logs");
pub const HEALTH: (&str, &str) = ("health", "Health");
pub const SCORING_DEFAULTS: (&str, &str) = ("scoring-defaults", "Scoring Defaults");

/// This calendar's metadata spreadsheet id, if it has one yet.
pub fn spreadsheet_of(calendar: &CalendarClient) -> anyhow::Result<Option<String>> {
    calendar.get_calendar_metadata(SPREADSHEET_ID_METADATA_KEY)
}

/// This calendar's metadata spreadsheet id, and whether it was just made.
pub fn ensure_spreadsheet(calendar: &CalendarClient, sheets: &SheetsClient) -> anyhow::Result<(String, bool)> {
    if let Some(id) = spreadsheet_of(calendar)? {
        return Ok((id, false));
    }
    let id = sheets.create_spreadsheet(SPREADSHEET_TITLE)?;
    calendar.set_calendar_metadata(SPREADSHEET_ID_METADATA_KEY, Some(&id))?;
    Ok((id, true))
}

/// Give spreadsheet `id` its title, SPREADSHEET_TITLE: one made under an
/// older title, or renamed by hand, gets it back. One write, no read.
pub fn retitle_spreadsheet(sheets: &SheetsClient, id: &str) -> anyhow::Result<()> {
    sheets.set_spreadsheet_title(id, SPREADSHEET_TITLE)
}

/// The tab tagged `role`, and whether it was just made (tagged first: for
/// a tab that starts empty).
pub fn ensure_tab(sheets: &SheetsClient, spreadsheet_id: &str, role: &str, title: &str) -> anyhow::Result<(i64, bool)> {
    if let Some(id) = find_tab(sheets, spreadsheet_id, role, title)? {
        return Ok((id, false));
    }
    let id = sheets.add_sheet(spreadsheet_id, title, Some(&tab_color()))?;
    sheets.create_sheet_metadata(spreadsheet_id, id, SHEET_ROLE_METADATA_KEY, role)?;
    identify(sheets, spreadsheet_id, id, role, title);
    Ok((id, true))
}

/// The tab tagged `role` (titled `title` when it was made), if there is one.
pub fn find_tab(sheets: &SheetsClient, spreadsheet_id: &str, role: &str, title: &str) -> anyhow::Result<Option<i64>> {
    let id = sheets.find_sheet_id(spreadsheet_id, SHEET_ROLE_METADATA_KEY, role)?;
    if let Some(id) = id {
        identify(sheets, spreadsheet_id, id, role, title);
    }
    Ok(id)
}

/// Tell the kept copy what tab `id` is, so it can be made again if it's
/// deleted by hand.
fn identify(sheets: &SheetsClient, spreadsheet_id: &str, id: i64, role: &str, title: &str) {
    let identity = TabIdentity { title: title.into(), color: Some(tab_color()), key: SHEET_ROLE_METADATA_KEY.into(), value: role.into() };
    sheets.identify(spreadsheet_id, id, identity);
}

/// Make (or adopt `reuse_sheet_id`, renamed and colored) a tab for
/// `role`, fill it with `populate`, and only then tag it -- so a failure
/// partway leaves no tagged-but-empty tab.
pub fn create_tab(
    sheets: &SheetsClient,
    spreadsheet_id: &str,
    role: &str,
    title: &str,
    reuse_sheet_id: Option<i64>,
    populate: impl FnOnce(i64) -> anyhow::Result<()>,
) -> anyhow::Result<i64> {
    let id = match reuse_sheet_id {
        Some(id) => {
            sheets.update_sheet_properties(spreadsheet_id, id, Some(title), Some(&tab_color()))?;
            id
        }
        None => sheets.add_sheet(spreadsheet_id, title, Some(&tab_color()))?,
    };
    populate(id)?;
    sheets.create_sheet_metadata(spreadsheet_id, id, SHEET_ROLE_METADATA_KEY, role)?;
    identify(sheets, spreadsheet_id, id, role, title);
    Ok(id)
}

#[cfg(test)]
#[path = "mod_tests.rs"]
mod tests;
