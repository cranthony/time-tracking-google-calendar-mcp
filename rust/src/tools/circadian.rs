//! The circadian rhythm tools: the times the user woke naturally, and the
//! temperature minimum and dim and bright windows derived from them.

use super::{refused, ToolError};
use crate::app::App;
use crate::contract::{tool, Schema};
use crate::google::calendar::is_time_zone_not_set;
use crate::stores::circadian::{date_given, days, CircadianDay, Revision, Windows};
use chrono_tz::Tz;
use serde::{Deserialize, Serialize};

/// The user's circadian rhythm, as get_circadian_rhythm gives it.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct CircadianRhythm {
    /// The calendar's time zone, which wake times are in; null if it has none.
    pub time_zone: Option<String>,
    pub windows: Windows,
    /// Oldest first; each applies from its date until the next one's.
    pub revisions: Vec<Revision>,
    /// Each day from `from` to `to` with a revision on or before it --
    /// only when both are given, and null otherwise.
    pub days: Option<Vec<CircadianDay>>,
}

/// The wake times and windows, as set_natural_wake_time and
/// set_circadian_windows give them back.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct CircadianSettings {
    /// The calendar's time zone, which wake times are in; null if it has none.
    pub time_zone: Option<String>,
    pub windows: Windows,
    /// Oldest first; each applies from its date until the next one's.
    pub revisions: Vec<Revision>,
}

/// The calendar's time zone, or None if it has none.
fn time_zone(app: &App) -> anyhow::Result<Option<Tz>> {
    match app.calendar_client()?.get_time_zone() {
        Ok(zone) => Ok(Some(zone)),
        Err(error) if is_time_zone_not_set(&error) => Ok(None),
        Err(error) => Err(error),
    }
}

fn settings(app: &App) -> Result<CircadianSettings, ToolError> {
    let circadian = app.circadian()?;
    circadian.prefetch()?;
    let zone = time_zone(app)?;
    let windows = circadian.windows().map_err(refused)?;
    let revisions = circadian
        .revisions(&windows, zone.as_ref())
        .map_err(refused)?;
    Ok(CircadianSettings {
        time_zone: zone.map(|z| z.name().to_string()),
        windows,
        revisions,
    })
}

/// The user's circadian rhythm. They say when they woke naturally (no
/// alarm) on a day, and it applies from that day on, until a later one.
/// From it comes their temperature minimum, the low point of their body
/// clock (by default 2 hours before waking), and two windows around it:
/// dim, the hours before the minimum (by default 6), when bright light
/// and hard exercise shift their schedule later -- so keep them dim and
/// calm; and bright, the hours after it (by default 4), when bright light
/// and exercise hold their schedule or shift it earlier -- so seek them.
/// Plan routines with these: wind down in the dim window, get outside or
/// exercise in the bright one.
///
/// windows are the lengths, in minutes; revisions are the wake times,
/// oldest first, each with its own date's temperature_minimum ("HH:MM").
/// With `from` and `to` ("YYYY-MM-DD", inclusive, at most 400 days),
/// days gives each day's temperature minimum and windows as instants in
/// the calendar's time_zone (days before the first revision are left
/// out); the dim window can start the evening before. Read-only.
#[tool]
pub fn get_circadian_rhythm(
    app: &App,
    from: Option<String>,
    to: Option<String>,
) -> Result<CircadianRhythm, ToolError> {
    let range = match (from.as_deref(), to.as_deref()) {
        (Some(from), Some(to)) => Some((
            date_given(from).map_err(refused)?,
            date_given(to).map_err(refused)?,
        )),
        (None, None) => None,
        _ => {
            return Err(ToolError::Message(
                "Give both from and to (\"YYYY-MM-DD\") for days, or neither".into(),
            ))
        }
    };
    let CircadianSettings {
        time_zone: zone_name,
        windows,
        revisions,
    } = settings(app)?;
    let days = match range {
        Some((from, to)) => {
            // Days are instants in the calendar's zone: none set is refused.
            let zone = app.calendar_client()?.get_time_zone()?;
            Some(days(&revisions, &windows, &zone, from, to).map_err(refused)?)
        }
        None => None,
    };
    Ok(CircadianRhythm {
        time_zone: zone_name,
        windows,
        revisions,
        days,
    })
}

/// Record the time the user woke naturally (without an alarm) on `date`
/// ("YYYY-MM-DD"), at `time` ("HH:MM", 24-hour, in the calendar's time
/// zone), with an optional note (at most 500 characters). It applies from
/// that date on, until a later one: their temperature minimum and dim and
/// bright windows follow it (see get_circadian_rhythm). A date already
/// set is replaced, its note too (leave note out to clear it). Wake times
/// dated more than 366 days before the newest are dropped. Returns the
/// wake times and windows as saved.
#[tool(writes)]
pub fn set_natural_wake_time(
    app: &App,
    date: String,
    time: String,
    note: Option<String>,
) -> Result<CircadianSettings, ToolError> {
    let circadian = app.circadian()?;
    circadian.prefetch()?;
    let now = crate::determinism::now();
    let today = match time_zone(app)? {
        Some(zone) => crate::zoned::in_zone(&now, &zone).date_naive(),
        None => now.date_naive(),
    };
    circadian
        .set_wake_time(&date, &time, note.as_deref(), today)
        .map_err(refused)?;
    settings(app)
}

/// Set how the user's temperature minimum and windows are derived from
/// their wake time, each a whole number of minutes from 0 to 1440:
/// minimum_before_waking_minutes (how long before waking the minimum is;
/// default 120), dim_before_minutes (how long the dim window before the
/// minimum is; default 360) and bright_after_minutes (how long the bright
/// window after it is; default 240). All three are given; to change one,
/// get the others first (get_circadian_rhythm). Returns the wake times and
/// windows as saved.
#[tool(writes)]
pub fn set_circadian_windows(
    app: &App,
    minimum_before_waking_minutes: i64,
    dim_before_minutes: i64,
    bright_after_minutes: i64,
) -> Result<CircadianSettings, ToolError> {
    let windows = Windows {
        minimum_before_waking_minutes,
        dim_before_minutes,
        bright_after_minutes,
    };
    let circadian = app.circadian()?;
    circadian.prefetch()?;
    circadian.set_windows(&windows).map_err(refused)?;
    settings(app)
}
