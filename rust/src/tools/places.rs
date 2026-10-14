//! The location, location group and travel time tools.

use super::{refused, texts, ToolError};
use crate::app::App;
use crate::contract::tool;
use crate::stores::groups::Placed;
use crate::stores::travel_times::{self, CreatedTravelTime, TravelEstimate, TravelTime};
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

/// Every action's and action group's id: what a travel time's mode_id can
/// be.
fn mode_ids(app: &App) -> anyhow::Result<Vec<String>> {
    let tree = app.actions()?.tree()?;
    let actions = tree.actions.iter().filter_map(|a| a.id.clone());
    Ok(actions.chain(tree.groups.groups.iter().filter_map(|g| g.id.clone())).filter(|i| !i.is_empty()).collect())
}

/// `mode_id` and the action groups it's in, nearest first.
fn mode_and_groups(app: &App, mode_id: &str) -> anyhow::Result<Vec<String>> {
    let tree = app.actions()?.tree()?;
    let item: &dyn Placed = match (tree.by_id(mode_id), tree.groups.by_id(mode_id)) {
        (Some(action), _) => action,
        (None, Some(group)) => group,
        (None, None) => anyhow::bail!("{} isn't an action or an action group", crate::py::repr(mode_id)),
    };
    let mut ids = vec![mode_id.to_string()];
    ids.extend(tree.groups.enclosing(item).iter().filter_map(|g| g.id.clone()));
    Ok(ids)
}

/// Prefetch the locations', groups', travel times' and actions' tabs at
/// once.
fn prefetch_travel(app: &App) -> anyhow::Result<()> {
    let mut ranges = app.locations()?.all_tabs();
    ranges.extend(app.actions()?.whole_tabs());
    app.locations()?.prefetch(&ranges)
}

/// Every travel time: a rough estimate of how long it takes to get between
/// two places (from_id and to_id, each a location or a location group),
/// each way of getting there (times: a mode_id -- an action or action
/// group, such as "Drive" or "Transit" -- and minutes, or minutes to
/// max_minutes), with a note. It counts both ways unless it's one_way.
/// With place_id, only those from or to it. Read-only.
#[tool]
pub fn get_travel_times(app: &App, place_id: Option<String>) -> Result<Vec<TravelTime>, ToolError> {
    app.locations()?.get_travel_times(place_id.as_deref()).map_err(refused)
}

/// Add a travel time between from_id and to_id (each a location or a
/// location group): its times, each a mode_id (an action or action group)
/// and minutes (more than 0), and max_minutes if it varies; one_way if it's
/// only from from_id to to_id (otherwise it counts both ways); and a note.
/// Only one travel time can say how long each way between two places
/// takes: a two-way one, and a one-way one for a way that's different. id
/// is assigned. Returns it, and its id as created_id.
#[tool(writes)]
pub fn create_travel_time(app: &App, travel_time: TravelTime) -> Result<CreatedTravelTime, ToolError> {
    prefetch_travel(app).map_err(refused)?;
    let modes = mode_ids(app).map_err(refused)?;
    app.locations()?.create_travel_time(&travel_time, &modes).map_err(refused)
}

/// Update a travel time by id: its from_id, to_id, one_way, times
/// (replaced whole) or note. Omitted properties keep their value; list
/// note in clear_fields to blank it. Returns it as updated.
#[tool(writes)]
pub fn update_travel_time(
    app: &App,
    travel_time: TravelTime,
    clear_fields: Option<Vec<travel_times::ClearableField>>,
) -> Result<TravelTime, ToolError> {
    prefetch_travel(app).map_err(refused)?;
    let modes = mode_ids(app).map_err(refused)?;
    let clear = texts(&clear_fields.unwrap_or_default());
    app.locations()?.update_travel_time(&travel_time, &clear, &modes).map_err(refused)
}

/// Delete a travel time, by id. Returns it as it was. (Deleting a location
/// or location group deletes the travel times from or to it.)
#[tool(writes)]
pub fn delete_travel_time(app: &App, travel_time_id: String) -> Result<TravelTime, ToolError> {
    app.locations()?.delete_travel_time(&travel_time_id).map_err(refused)
}

/// How long it takes to get from from_id to to_id (each a location or a
/// location group), as best the travel times say: the one between them,
/// or else between the groups they're in -- the fewest steps out first,
/// and of those, the same steps out on each side, so it's the same either
/// way round. A one-way travel time beats a two-way one
/// between the same places. With mode_id (an action or action group),
/// only a travel time with a time for it, or else for its nearest group
/// that has one, and just that time. Returns the places it's between,
/// the travel time, and its times; null if no travel time says.
/// Read-only.
#[tool]
pub fn estimate_travel_time(app: &App, from_id: String, to_id: String, mode_id: Option<String>) -> Result<Option<TravelEstimate>, ToolError> {
    prefetch_travel(app).map_err(refused)?;
    let modes = match mode_id {
        Some(mode) => Some(mode_and_groups(app, &mode).map_err(refused)?),
        None => None,
    };
    app.locations()?.estimate_travel_time(&from_id, &to_id, modes.as_deref()).map_err(refused)
}
