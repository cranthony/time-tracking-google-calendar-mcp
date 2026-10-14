//! The location tools.

use super::{refused, texts, ToolError};
use crate::app::App;
use crate::contract::tool;
use crate::stores::locations::{ClearableField, CreatedLocation, Location};

/// Every location: a place the user's events happen ("Home", "Salsa
/// studio"), each with an id, a name, a hint for recognizing when an
/// event or note refers to it, and a map location: where it is, as a maps
/// app would find it, for its events with no location of their own.
/// Read-only.
#[tool]
pub fn get_locations(app: &App) -> Result<Vec<Location>, ToolError> {
    app.locations()?.all().map_err(refused)
}

/// One location, by id or else by name (ignoring case). If there's
/// none, the error suggests close matches. Read-only.
#[tool]
pub fn get_location(app: &App, id_or_name: String) -> Result<Location, ToolError> {
    app.locations()?.get_location(&id_or_name).map_err(refused)
}

/// Add a location: a name (unique among locations) and a hint for
/// recognizing when an event or note refers to it -- other names for it,
/// an address, what happens there ("the apartment; 'home', 'my place'")
/// -- and, if it has one, a map_location: an address, a place's name or a
/// link, opened for its events with no location of their own. id is
/// assigned. Returns the location and its id as created_id.
#[tool(writes)]
pub fn create_location(app: &App, location: Location) -> Result<CreatedLocation, ToolError> {
    app.locations()?.create_location(&location).map_err(refused)
}

/// Rename a location or change its hint or map_location, by id. Omitted
/// properties keep their value; list hint or map_location in clear_fields
/// to blank it. Returns it as updated.
#[tool(writes)]
pub fn update_location(app: &App, location: Location, clear_fields: Option<Vec<ClearableField>>) -> Result<Location, ToolError> {
    let clear = texts(&clear_fields.unwrap_or_default());
    app.locations()?.update_location(&location, &clear).map_err(refused)
}

/// Delete a location, by id. Returns it as it was.
#[tool(writes)]
pub fn delete_location(app: &App, location_id: String) -> Result<Location, ToolError> {
    app.locations()?.delete_location(&location_id).map_err(refused)
}
