//! google_setup_link: a link to set up Google access from a browser (see
//! crate::setup).

use super::ToolError;
use crate::app::App;
use crate::contract::{tool, Schema};
use crate::setup::{need, LINK_MINUTES};
use serde::{Deserialize, Serialize};

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct SetupLink {
    /// The page to open in a browser: good once, within `expires_in_minutes`.
    pub url: String,
    pub expires_in_minutes: i64,
    /// Why Google access needs setting up.
    pub reason: String,
}

/// A link to set up the server's Google access in a browser, when it
/// needs it: there's no calendar yet (GOOGLE_CALENDAR_ID isn't set), or
/// the Google token stopped working (revoked, or expired). Give the user
/// the link to open: they sign in with Google there, and the server takes
/// on the new token at once -- making the calendar and its spreadsheet if
/// there's none -- and shows the token, to keep as the server's
/// token.json secret. The link works once, for a few minutes, and only
/// from this server's own URL. Refused when Google access already works,
/// and over stdio (run create_calendar instead).
#[tool]
pub fn google_setup_link(app: &App) -> Result<SetupLink, ToolError> {
    let Some(base) = app.setup.base().map(str::to_string) else {
        return Err(ToolError::Message(
            "Setting up Google access in a browser needs the server running over HTTP (MCP_TRANSPORT=streamable-http). Over stdio, run create_calendar instead.".into(),
        ));
    };
    let Some(reason) = need(app) else {
        return Err(ToolError::Message("Google access already works with the calendar: there's nothing to set up.".into()));
    };
    let token = app.setup.new_link();
    Ok(SetupLink { url: format!("{base}/setup?link={token}"), expires_in_minutes: LINK_MINUTES as i64, reason: reason.describe() })
}
