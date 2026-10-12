//! A calendar's locations: where its events happen, each a name and a hint
//! for recognizing it, in the Locations tab.

use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::sheet::rows::{self, RowSheet, RowType};
use crate::sheet::LOCATIONS;
use crate::contract::{RowItem, Schema};
use anyhow::bail;
use serde::{Deserialize, Serialize};


/// One location: where the user's events happen (see get_locations).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema, RowItem)]
#[schema(deny_unknown)]
pub struct Location {
    pub id: Option<String>,
    pub name: Option<String>,
    pub hint: Option<String>,
}

/// A location's fields that clear_fields can blank.
#[derive(Clone, Copy, Debug, Serialize, Deserialize, Schema)]
#[serde(rename_all = "lowercase")]
pub enum ClearableField {
    Hint,
}

#[derive(Debug, Serialize, Deserialize, Schema)]
pub struct CreatedLocation {
    pub location: Location,
    pub created_id: String,
}

pub const ROW: RowType = <Location as rows::RowItem>::ROW_TYPE;
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

    pub fn all(&self) -> anyhow::Result<Vec<Location>> {
        self.sheet.read_items()
    }

    pub fn get_location(&self, id_or_name: &str) -> anyhow::Result<Location> {
        find(&self.all()?, id_or_name)
    }

    pub fn create_location(&self, location: &Location) -> anyhow::Result<CreatedLocation> {
        let mut locations = self.all()?;
        let id = rows::new_id(locations.iter().map(|l| l.id.as_deref()).collect::<Vec<_>>());
        let new = Location { id: Some(id.clone()), ..location.clone() };
        locations.push(new.clone());
        self.write(&locations)?;
        Ok(CreatedLocation { location: new, created_id: id })
    }

    pub fn update_location(&self, location: &Location, clear_fields: &[String]) -> anyhow::Result<Location> {
        let Some(id) = location.id.clone().filter(|i| !i.is_empty()) else {
            bail!("update_location needs the location's id");
        };
        rows::check_clear_item(location, clear_fields, &CLEARABLE_FIELDS)?;
        let mut locations = self.all()?;
        let Some(index) = locations.iter().position(|l| l.id.as_deref() == Some(id.as_str())) else {
            find(&locations, &id)?;
            unreachable!("find raises for an id that isn't there")
        };
        locations[index] = rows::updated_item(&locations[index], location, clear_fields, &["id"]);
        self.write(&locations)?;
        Ok(locations[index].clone())
    }

    pub fn delete_location(&self, location_id: &str) -> anyhow::Result<Location> {
        let locations = self.all()?;
        let deleted = match locations.iter().find(|l| l.id.as_deref() == Some(location_id)) {
            Some(found) => found.clone(),
            None => find(&locations, location_id)?,
        };
        if deleted.id.as_deref() != Some(location_id) {
            bail!(
                "Delete a location by its id: {} is {}",
                py::repr(deleted.name.as_deref().unwrap_or_default()),
                deleted.id.as_deref().unwrap_or_default()
            );
        }
        let kept: Vec<Location> = locations.into_iter().filter(|l| l.id.as_deref() != Some(location_id)).collect();
        self.sheet.write_items(&kept)?;
        Ok(deleted)
    }

    fn write(&self, locations: &[Location]) -> anyhow::Result<()> {
        let problems = location_problems(locations);
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        self.sheet.write_items(locations)
    }
}

/// The location with this id, or else this name (ignoring case); an error
/// suggesting close matches if there's none.
pub fn find(locations: &[Location], id_or_name: &str) -> anyhow::Result<Location> {
    let key = |l: &Location| l.name.as_deref().unwrap_or_default().to_lowercase();
    // Python builds {name: location}, so of two with one name the later wins.
    let by_name = |name: &str| locations.iter().rev().find(|l| key(l) == name).cloned();
    if let Some(found) = locations.iter().find(|l| l.id.as_deref() == Some(id_or_name)) {
        return Ok(found.clone());
    }
    if let Some(found) = by_name(&id_or_name.to_lowercase()) {
        return Ok(found);
    }
    let ids: Vec<String> = dedup(locations.iter().filter_map(|l| l.id.clone().filter(|i| !i.is_empty())));
    let names: Vec<String> = dedup(locations.iter().map(key));
    let mut close: Vec<Location> = Vec::new();
    for id in crate::difflib::get_close_matches(id_or_name, &ids, 3) {
        if let Some(l) = locations.iter().rev().find(|l| l.id.as_deref() == Some(id.as_str())) {
            close.push(l.clone());
        }
    }
    for name in crate::difflib::get_close_matches(&id_or_name.to_lowercase(), &names, 3) {
        if let Some(l) = by_name(&name) {
            close.push(l);
        }
    }
    let hints: Vec<String> = dedup(close.iter().map(|l| format!("{} ({})", l.id.as_deref().unwrap_or("None"), l.name.as_deref().unwrap_or("None"))));
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

pub fn location_problems(locations: &[Location]) -> Vec<String> {
    let mut problems = Vec::new();
    let mut names: Vec<(String, &Location)> = Vec::new();
    for location in locations {
        let id = location.id.as_deref().filter(|i| !i.is_empty());
        let name = location.name.as_deref();
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
                py::repr(other.name.as_deref().unwrap_or_default()),
                other.id.as_deref().unwrap_or("None")
            ));
        }
        names.retain(|(k, _)| *k != key);
        names.push((key, location));
    }
    problems
}

#[cfg(test)]
#[path = "locations_tests.rs"]
mod tests;
