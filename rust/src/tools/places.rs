//! The location and location group tools.

use super::{refused, texts, ToolError};
use crate::app::App;
use crate::contract::tool;
use crate::stores::locations::{
    ClearableField, CreatedLocation, CreatedLocationGroup, DeletedLocationGroup, GroupClearableField, ListedLocation,
    ListedLocationGroup, Location, LocationGroup,
};

/// Every location: a place the user's events happen ("Home", "Salsa
/// studio"), each with an id, a name, a hint for recognizing when an
/// event or note refers to it, a map location: where it is, as a maps
/// app would find it, for its events with no location of their own, and
/// the location group it's in (group_id), with its path through its
/// groups ("New York › Brooklyn › Home"). Read-only.
#[tool]
pub fn get_locations(app: &App) -> Result<Vec<ListedLocation>, ToolError> {
    app.locations()?.listed_locations().map_err(refused)
}

/// One location, by id or else by name (ignoring case), as get_locations
/// lists it. If there's none, the error suggests close matches.
/// Read-only.
#[tool]
pub fn get_location(app: &App, id_or_name: String) -> Result<ListedLocation, ToolError> {
    app.locations()?.listed_location(&id_or_name).map_err(refused)
}

/// Add a location: a name (unique among locations) and a hint for
/// recognizing when an event or note refers to it -- other names for it,
/// an address, what happens there ("the apartment; 'home', 'my place'")
/// -- and, if it has one, a map_location: an address, a place's name or a
/// link, opened for its events with no location of their own, and a
/// group_id: the location group it's in. id is assigned. Returns the
/// location and its id as created_id.
#[tool(writes)]
pub fn create_location(app: &App, location: Location) -> Result<CreatedLocation, ToolError> {
    app.locations()?.create_location(&location).map_err(refused)
}

/// Rename a location or change its hint, map_location or group_id, by
/// id. Omitted properties keep their value; list hint, map_location or
/// group_id in clear_fields to blank it. Returns it as updated.
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

/// Every location group: a name that gathers locations ("Brooklyn", "New
/// York"). Groups can be inside other groups (group_id), and a location is
/// in at most one group. Enclosing groups come before the ones inside
/// them. Each has its id, group_id, name, note, and its path through the
/// groups it's inside ("New York › Brooklyn"). Read-only.
#[tool]
pub fn get_location_groups(app: &App) -> Result<Vec<ListedLocationGroup>, ToolError> {
    app.locations()?.get_location_groups().map_err(refused)
}

/// One location group, by its id or else its name (ignoring case), as
/// get_location_groups lists it. If there's none, the error suggests close
/// matches. Read-only.
#[tool]
pub fn get_location_group(app: &App, id_or_name: String) -> Result<ListedLocationGroup, ToolError> {
    app.locations()?.get_location_group(&id_or_name).map_err(refused)
}

/// Create a location group: a name (unique among groups, though a
/// location may share it; at most 50 characters), and optionally a
/// group_id (the group it's inside) and a note. id is assigned, and is
/// never a location's. Put locations in it with update_location's
/// group_id. Returns the group and its id as created_id.
#[tool(writes)]
pub fn create_location_group(app: &App, group: LocationGroup) -> Result<CreatedLocationGroup, ToolError> {
    app.locations()?.create_location_group(&group).map_err(refused)
}

/// Update a location group by id: its group_id (move it inside another
/// group; clear it to make it top-level), name (still unique among
/// groups) or note. Omitted properties keep their value; list group_id or
/// note in clear_fields to blank it. A group can't end up inside itself.
/// Returns it as updated.
#[tool(writes)]
pub fn update_location_group(
    app: &App,
    group: LocationGroup,
    clear_fields: Option<Vec<GroupClearableField>>,
) -> Result<ListedLocationGroup, ToolError> {
    let clear = texts(&clear_fields.unwrap_or_default());
    app.locations()?.update_location_group(&group, &clear).map_err(refused)
}

/// Delete a location group, by id. Its locations and the groups inside it
/// aren't deleted: they move up into the group it was inside (or to the
/// top level, if it was top-level). Returns the group as it was
/// (deleted), and the groups and locations moved up out of it.
#[tool(writes)]
pub fn delete_location_group(app: &App, group_id: String) -> Result<DeletedLocationGroup, ToolError> {
    app.locations()?.delete_location_group(&group_id).map_err(refused)
}
