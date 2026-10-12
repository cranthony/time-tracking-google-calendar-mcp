//! The calendar's metadata spreadsheet (utilities/calendar_metadata_sheet.py)
//! and its row-per-item tabs (utilities/row_sheet.py).

pub mod rows;

use crate::google::calendar::CalendarClient;
use crate::google::sheets::SheetsClient;
use serde_json::{json, Value};

pub const SPREADSHEET_TITLE: &str = "Calendar Metadata";
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
pub const HABITS: (&str, &str) = ("habits", "Habits");
pub const COMPACTION_SCHEDULE: (&str, &str) = ("compaction-schedule", "Compaction Schedule");
pub const CANCELLATIONS: (&str, &str) = ("cancellations", "Cancellations");
pub const HEALTH: (&str, &str) = ("health", "Health");

/// This calendar's metadata spreadsheet id, and whether it was just made.
pub fn ensure_spreadsheet(calendar: &CalendarClient, sheets: &SheetsClient) -> anyhow::Result<(String, bool)> {
    if let Some(id) = calendar.get_calendar_metadata(SPREADSHEET_ID_METADATA_KEY)? {
        return Ok((id, false));
    }
    let id = sheets.create_spreadsheet(SPREADSHEET_TITLE)?;
    calendar.set_calendar_metadata(SPREADSHEET_ID_METADATA_KEY, Some(&id))?;
    Ok((id, true))
}

/// The tab tagged `role`, and whether it was just made (tagged first: for
/// a tab that starts empty).
pub fn ensure_tab(sheets: &SheetsClient, spreadsheet_id: &str, role: &str, title: &str) -> anyhow::Result<(i64, bool)> {
    if let Some(id) = sheets.find_sheet_id(spreadsheet_id, SHEET_ROLE_METADATA_KEY, role)? {
        return Ok((id, false));
    }
    let id = sheets.add_sheet(spreadsheet_id, title, Some(&tab_color()))?;
    sheets.create_sheet_metadata(spreadsheet_id, id, SHEET_ROLE_METADATA_KEY, role)?;
    Ok((id, true))
}

pub fn find_tab(sheets: &SheetsClient, spreadsheet_id: &str, role: &str) -> anyhow::Result<Option<i64>> {
    sheets.find_sheet_id(spreadsheet_id, SHEET_ROLE_METADATA_KEY, role)
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
    Ok(id)
}

#[cfg(test)]
#[path = "mod_tests.rs"]
mod tests;
