//! The location tools.

use super::{args_str, args_strings, refused, ToolResult};
use crate::app::App;
use crate::stores::locations::ROW;
use serde_json::{Map, Value};

pub fn get_locations(app: &App, _args: &Map<String, Value>) -> ToolResult {
    let rows = app.locations()?.all().map_err(refused)?;
    Ok(Value::Array(rows.into_iter().map(Value::Object).collect()))
}

pub fn get_location(app: &App, args: &Map<String, Value>) -> ToolResult {
    Ok(Value::Object(app.locations()?.get_location(args_str(args, "id_or_name")).map_err(refused)?))
}

pub fn create_location(app: &App, args: &Map<String, Value>) -> ToolResult {
    let location = ROW.from_value(&args["location"]);
    app.locations()?.create_location(&location).map_err(refused)
}

pub fn update_location(app: &App, args: &Map<String, Value>) -> ToolResult {
    let location = ROW.from_value(&args["location"]);
    Ok(Value::Object(app.locations()?.update_location(&location, &args_strings(args, "clear_fields")).map_err(refused)?))
}

pub fn delete_location(app: &App, args: &Map<String, Value>) -> ToolResult {
    Ok(Value::Object(app.locations()?.delete_location(args_str(args, "location_id")).map_err(refused)?))
}
