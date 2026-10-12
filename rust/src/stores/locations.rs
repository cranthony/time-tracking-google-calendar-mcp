//! A calendar's locations (utilities/locations.py): where its events
//! happen, each a name and a hint for recognizing it, in the Locations tab.

use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::sheet::rows::{self, Kind, Row, RowSheet, RowType};
use crate::sheet::LOCATIONS;
use anyhow::bail;
use serde_json::{json, Value};

pub const ROW: RowType = RowType { name: "Location", fields: &[("id", Kind::Str), ("name", Kind::Str), ("hint", Kind::Str)] };
pub const CLEARABLE_FIELDS: [&str; 1] = ["hint"];
pub const MAX_NAME_LENGTH: usize = 50;

#[derive(Clone)]
pub struct Locations {
    sheet: RowSheet,
}

impl Locations {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<Locations> {
        Ok(Locations { sheet: RowSheet::ensure(sheets, spreadsheet_id, LOCATIONS.0, LOCATIONS.1, ROW, &["id", "name"])? })
    }

    pub fn whole_tab(&self) -> TabRange {
        self.sheet.whole_tab()
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheet.prefetch(ranges)
    }

    pub fn all(&self) -> anyhow::Result<Vec<Row>> {
        self.sheet.read()
    }

    pub fn get_location(&self, id_or_name: &str) -> anyhow::Result<Row> {
        find(&self.all()?, id_or_name)
    }

    pub fn create_location(&self, location: &Row) -> anyhow::Result<Value> {
        let mut locations = self.all()?;
        let mut new = location.clone();
        let id = rows::new_id(locations.iter().map(|l| rows::text(l, "id")).collect::<Vec<_>>());
        new.insert("id".into(), json!(id));
        locations.push(new.clone());
        self.write(&locations)?;
        Ok(json!({ "location": Value::Object(new), "created_id": id }))
    }

    pub fn update_location(&self, location: &Row, clear_fields: &[String]) -> anyhow::Result<Row> {
        let Some(id) = rows::text(location, "id").filter(|i| !i.is_empty()).map(str::to_string) else {
            bail!("update_location needs the location's id");
        };
        rows::check_clear(location, clear_fields, &CLEARABLE_FIELDS)?;
        let mut locations = self.all()?;
        let Some(index) = locations.iter().position(|l| rows::text(l, "id") == Some(id.as_str())) else {
            find(&locations, &id)?;
            unreachable!("find raises for an id that isn't there")
        };
        locations[index] = rows::updated(&locations[index], location, clear_fields, &["id"]);
        self.write(&locations)?;
        Ok(locations[index].clone())
    }

    pub fn delete_location(&self, location_id: &str) -> anyhow::Result<Row> {
        let locations = self.all()?;
        let deleted = match locations.iter().find(|l| rows::text(l, "id") == Some(location_id)) {
            Some(found) => found.clone(),
            None => find(&locations, location_id)?,
        };
        if rows::text(&deleted, "id") != Some(location_id) {
            bail!(
                "Delete a location by its id: {} is {}",
                py::repr(rows::text(&deleted, "name").unwrap_or_default()),
                rows::text(&deleted, "id").unwrap_or_default()
            );
        }
        let kept: Vec<Row> = locations.into_iter().filter(|l| rows::text(l, "id") != Some(location_id)).collect();
        self.sheet.write(&kept)?;
        Ok(deleted)
    }

    fn write(&self, locations: &[Row]) -> anyhow::Result<()> {
        let problems = location_problems(locations);
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        self.sheet.write(locations)
    }
}

/// The location with this id, or else this name (ignoring case); an error
/// suggesting close matches if there's none.
pub fn find(locations: &[Row], id_or_name: &str) -> anyhow::Result<Row> {
    let key = |row: &Row| rows::text(row, "name").unwrap_or_default().to_lowercase();
    // Python builds {name: location}, so of two with one name the later wins.
    let by_name = |name: &str| locations.iter().rev().find(|l| key(l) == name).cloned();
    if let Some(found) = locations.iter().find(|l| rows::text(l, "id") == Some(id_or_name)) {
        return Ok(found.clone());
    }
    if let Some(found) = by_name(&id_or_name.to_lowercase()) {
        return Ok(found);
    }
    let ids: Vec<String> = dedup(locations.iter().filter_map(|l| rows::text(l, "id").filter(|i| !i.is_empty()).map(str::to_string)));
    let names: Vec<String> = dedup(locations.iter().map(key));
    let mut close: Vec<Row> = Vec::new();
    for id in crate::difflib::get_close_matches(id_or_name, &ids, 3) {
        if let Some(l) = locations.iter().rev().find(|l| rows::text(l, "id") == Some(id.as_str())) {
            close.push(l.clone());
        }
    }
    for name in crate::difflib::get_close_matches(&id_or_name.to_lowercase(), &names, 3) {
        if let Some(l) = by_name(&name) {
            close.push(l);
        }
    }
    let hints: Vec<String> = dedup(close.iter().map(|l| {
        format!("{} ({})", rows::text(l, "id").unwrap_or("None"), rows::text(l, "name").unwrap_or("None"))
    }));
    let hint = hints.join(", ");
    bail!(
        "There's no location with the id or name {}{} (get_locations lists them)",
        py::repr(id_or_name),
        if hint.is_empty() { String::new() } else { format!("; did you mean {hint}?") }
    )
}

/// The items, first occurrences kept, in order (`dict.fromkeys`).
pub fn dedup(items: impl IntoIterator<Item = String>) -> Vec<String> {
    let mut out: Vec<String> = Vec::new();
    for item in items {
        if !out.contains(&item) {
            out.push(item);
        }
    }
    out
}

pub fn location_problems(locations: &[Row]) -> Vec<String> {
    let mut problems = Vec::new();
    let mut names: Vec<(String, Row)> = Vec::new();
    for location in locations {
        let id = rows::text(location, "id").filter(|i| !i.is_empty());
        let name = location.get("name").and_then(Value::as_str);
        let label = match id {
            Some(id) => format!("location {id}"),
            None => format!("location {}", name.map(py::repr).unwrap_or_else(|| "None".into())),
        };
        let Some(name) = name.filter(|n| !n.trim().is_empty()) else {
            problems.push(format!("{label} needs a name"));
            continue;
        };
        if name.chars().count() > MAX_NAME_LENGTH {
            problems.push(format!("{label}'s name is longer than {MAX_NAME_LENGTH} characters"));
        }
        let key = name.trim().to_lowercase();
        if let Some((_, other)) = names.iter().find(|(k, _)| *k == key) {
            problems.push(format!(
                "there's already a location named {} ({}); location names must be unique -- use that one, or pick another name",
                py::repr(rows::text(other, "name").unwrap_or_default()),
                rows::text(other, "id").unwrap_or("None")
            ));
        }
        names.retain(|(k, _)| *k != key);
        names.push((key, location.clone()));
    }
    problems
}
