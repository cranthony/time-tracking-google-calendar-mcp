//! A calendar's locations: where its events happen, each a name, a hint
//! for recognizing it and a map location, in the Locations tab -- and the
//! location groups that gather them (a neighborhood, a city), in the
//! Location Groups tab; and how long it takes to get between them, in
//! the Travel Times tab (see travel_times.rs).

use crate::google::sheets::{SheetsClient, TabRange};
use crate::py;
use crate::sheet::rows::{self, RowSheet, RowType};
use crate::sheet::{LOCATIONS, LOCATION_GROUPS, TRAVEL_TIMES};
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
    /// Where it is, as a maps app would find it -- an address, a place's
    /// name, or a link -- for an event there with no location of its own.
    pub map_location: Option<String>,
    /// The location group it's in, if any (see get_location_groups).
    pub group_id: Option<String>,
}

/// A location as get_locations lists it: with its path through its groups
/// ("New York › Brooklyn › Home").
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct ListedLocation {
    pub id: Option<String>,
    pub name: Option<String>,
    pub hint: Option<String>,
    pub map_location: Option<String>,
    pub group_id: Option<String>,
    pub path: String,
}

/// A location's fields that clear_fields can blank.
#[derive(Clone, Copy, Debug, Serialize, Deserialize, Schema)]
#[serde(rename_all = "lowercase")]
pub enum ClearableField {
    Hint,
    #[serde(rename = "map_location")]
    MapLocation,
    #[serde(rename = "group_id")]
    GroupId,
}

#[derive(Debug, Serialize, Deserialize, Schema)]
pub struct CreatedLocation {
    pub location: Location,
    pub created_id: String,
}

/// One location group: a name that gathers locations -- a neighborhood,
/// a city -- inside another group, if it has a group_id (see
/// get_location_groups).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema, RowItem)]
#[schema(deny_unknown)]
pub struct LocationGroup {
    pub id: Option<String>,
    /// The group it's inside, if any.
    pub group_id: Option<String>,
    pub name: Option<String>,
    pub note: Option<String>,
}

/// A location group as get_location_groups lists it: with its path
/// through the groups it's inside ("New York › Brooklyn").
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
pub struct ListedLocationGroup {
    pub id: Option<String>,
    pub group_id: Option<String>,
    pub name: Option<String>,
    pub note: Option<String>,
    pub path: String,
}

/// A location group's fields that clear_fields can blank.
#[derive(Clone, Copy, Debug, Serialize, Deserialize, Schema)]
pub enum GroupClearableField {
    #[serde(rename = "group_id")]
    GroupId,
    #[serde(rename = "note")]
    Note,
}

#[derive(Debug, Serialize, Deserialize, Schema)]
pub struct CreatedLocationGroup {
    pub group: ListedLocationGroup,
    pub created_id: String,
}

/// A deleted location group, and what moved up out of it.
#[derive(Debug, Serialize, Deserialize, Schema)]
pub struct DeletedLocationGroup {
    pub deleted: LocationGroup,
    pub moved_groups: Vec<ListedLocationGroup>,
    pub moved_locations: Vec<ListedLocation>,
}

pub const ROW: RowType = <Location as rows::RowItem>::ROW_TYPE;
pub const GROUP_ROW: RowType = <LocationGroup as rows::RowItem>::ROW_TYPE;
pub const CLEARABLE_FIELDS: [&str; 3] = ["hint", "map_location", "group_id"];
pub const GROUP_CLEARABLE_FIELDS: [&str; 2] = ["group_id", "note"];
pub const MAX_NAME_LENGTH: usize = 50;

#[derive(Clone)]
pub struct Locations {
    sheet: RowSheet,
    group_sheet: RowSheet,
    pub(crate) travel_sheet: RowSheet,
}

impl Locations {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<Locations> {
        Ok(Locations {
            sheet: RowSheet::ensure(sheets, spreadsheet_id, LOCATIONS.0, LOCATIONS.1, ROW, &["id", "name"])?,
            group_sheet: RowSheet::ensure(sheets, spreadsheet_id, LOCATION_GROUPS.0, LOCATION_GROUPS.1, GROUP_ROW, &["id", "name"])?,
            travel_sheet: RowSheet::ensure(
                sheets,
                spreadsheet_id,
                TRAVEL_TIMES.0,
                TRAVEL_TIMES.1,
                crate::stores::travel_times::ROW,
                &["id", "from_id", "to_id"],
            )?,
        })
    }

    /// The locations' and groups' tabs.
    pub fn whole_tabs(&self) -> Vec<TabRange> {
        vec![self.sheet.whole_tab(), self.group_sheet.whole_tab()]
    }

    /// Those, and the travel times'.
    pub fn all_tabs(&self) -> Vec<TabRange> {
        vec![self.sheet.whole_tab(), self.group_sheet.whole_tab(), self.travel_sheet.whole_tab()]
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheet.prefetch(ranges)
    }

    pub fn all(&self) -> anyhow::Result<Vec<Location>> {
        self.sheet.read_items()
    }

    pub fn groups(&self) -> anyhow::Result<Vec<LocationGroup>> {
        self.group_sheet.read_items()
    }

    pub fn get_location(&self, id_or_name: &str) -> anyhow::Result<Location> {
        find(&self.all()?, id_or_name)
    }

    /// Every location, with its path.
    pub fn listed_locations(&self) -> anyhow::Result<Vec<ListedLocation>> {
        self.prefetch(&self.whole_tabs())?;
        let groups = self.groups()?;
        Ok(self.all()?.iter().map(|l| listed(l, &groups)).collect())
    }

    pub fn listed_location(&self, id_or_name: &str) -> anyhow::Result<ListedLocation> {
        self.prefetch(&self.whole_tabs())?;
        let groups = self.groups()?;
        Ok(listed(&find(&self.all()?, id_or_name)?, &groups))
    }

    /// Every location group, enclosing groups before those inside them.
    pub fn get_location_groups(&self) -> anyhow::Result<Vec<ListedLocationGroup>> {
        let groups = self.groups()?;
        Ok(ordered(&groups).into_iter().map(|g| listed_group(g, &groups)).collect())
    }

    pub fn get_location_group(&self, id_or_name: &str) -> anyhow::Result<ListedLocationGroup> {
        let groups = self.groups()?;
        Ok(listed_group(&find_group(&groups, id_or_name)?, &groups))
    }

    /// An id that's neither a location's nor a group's, so either can be
    /// named by it where both are taken.
    fn new_id(locations: &[Location], groups: &[LocationGroup]) -> String {
        rows::new_id(locations.iter().map(|l| l.id.as_deref()).chain(groups.iter().map(|g| g.id.as_deref())).collect::<Vec<_>>())
    }

    pub fn create_location(&self, location: &Location) -> anyhow::Result<CreatedLocation> {
        self.prefetch(&self.whole_tabs())?;
        let mut locations = self.all()?;
        let groups = self.groups()?;
        let id = Self::new_id(&locations, &groups);
        let new = Location { id: Some(id.clone()), ..location.clone() };
        locations.push(new.clone());
        self.write(&locations, &groups)?;
        Ok(CreatedLocation { location: new, created_id: id })
    }

    pub fn update_location(&self, location: &Location, clear_fields: &[String]) -> anyhow::Result<Location> {
        let Some(id) = location.id.clone().filter(|i| !i.is_empty()) else {
            bail!("update_location needs the location's id");
        };
        rows::check_clear_item(location, clear_fields, &CLEARABLE_FIELDS)?;
        self.prefetch(&self.whole_tabs())?;
        let mut locations = self.all()?;
        let Some(index) = locations.iter().position(|l| l.id.as_deref() == Some(id.as_str())) else {
            find(&locations, &id)?;
            unreachable!("find raises for an id that isn't there")
        };
        locations[index] = rows::updated_item(&locations[index], location, clear_fields, &["id"]);
        self.write(&locations, &self.groups()?)?;
        Ok(locations[index].clone())
    }

    /// Deletes a location, and the travel times from or to it.
    pub fn delete_location(&self, location_id: &str) -> anyhow::Result<Location> {
        self.prefetch(&self.all_tabs())?;
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
        self.forget_travel_times(location_id)?;
        Ok(deleted)
    }

    pub fn create_location_group(&self, group: &LocationGroup) -> anyhow::Result<CreatedLocationGroup> {
        self.prefetch(&self.whole_tabs())?;
        let mut groups = self.groups()?;
        let id = Self::new_id(&self.all()?, &groups);
        let new = LocationGroup { id: Some(id.clone()), ..group.clone() };
        groups.push(new.clone());
        self.write_groups(&groups)?;
        Ok(CreatedLocationGroup { group: listed_group(&new, &groups), created_id: id })
    }

    pub fn update_location_group(&self, group: &LocationGroup, clear_fields: &[String]) -> anyhow::Result<ListedLocationGroup> {
        let Some(id) = group.id.clone().filter(|i| !i.is_empty()) else {
            bail!("update_location_group needs the group's id");
        };
        rows::check_clear_item(group, clear_fields, &GROUP_CLEARABLE_FIELDS)?;
        let mut groups = self.groups()?;
        let Some(index) = groups.iter().position(|g| g.id.as_deref() == Some(id.as_str())) else {
            find_group(&groups, &id)?;
            unreachable!("find_group raises for an id that isn't there")
        };
        groups[index] = rows::updated_item(&groups[index], group, clear_fields, &["id"]);
        self.write_groups(&groups)?;
        Ok(listed_group(&groups[index], &groups))
    }

    /// Deletes a group, and the travel times from or to it; what was
    /// inside it moves up into the group it was inside, or to the top
    /// level.
    pub fn delete_location_group(&self, group_id: &str) -> anyhow::Result<DeletedLocationGroup> {
        self.prefetch(&self.all_tabs())?;
        let groups = self.groups()?;
        let deleted = match groups.iter().find(|g| g.id.as_deref() == Some(group_id)) {
            Some(found) => found.clone(),
            None => find_group(&groups, group_id)?,
        };
        if deleted.id.as_deref() != Some(group_id) {
            bail!(
                "Delete a location group by its id: {} is {}",
                py::repr(deleted.name.as_deref().unwrap_or_default()),
                deleted.id.as_deref().unwrap_or_default()
            );
        }
        let in_it = |g: &Option<String>| g.as_deref() == Some(group_id);
        let moved = |current: &Option<String>| if in_it(current) { deleted.group_id.clone() } else { current.clone() };
        let groups_after: Vec<LocationGroup> = groups
            .iter()
            .filter(|g| g.id.as_deref() != Some(group_id))
            .map(|g| LocationGroup { group_id: moved(&g.group_id), ..g.clone() })
            .collect();
        let locations = self.all()?;
        let locations_after: Vec<Location> = locations.iter().map(|l| Location { group_id: moved(&l.group_id), ..l.clone() }).collect();
        let mut problems = group_problems(&groups_after);
        problems.extend(placement_problems(&locations_after, &groups_after));
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        self.group_sheet.write_items(&groups_after)?;
        if locations.iter().any(|l| in_it(&l.group_id)) {
            self.sheet.write_items(&locations_after)?;
        }
        self.forget_travel_times(group_id)?;
        Ok(DeletedLocationGroup {
            moved_groups: groups_after
                .iter()
                .zip(groups.iter().filter(|g| g.id.as_deref() != Some(group_id)))
                .filter(|(_, before)| in_it(&before.group_id))
                .map(|(g, _)| listed_group(g, &groups_after))
                .collect(),
            moved_locations: locations_after
                .iter()
                .zip(&locations)
                .filter(|(_, before)| in_it(&before.group_id))
                .map(|(l, _)| listed(l, &groups_after))
                .collect(),
            deleted,
        })
    }

    fn write(&self, locations: &[Location], groups: &[LocationGroup]) -> anyhow::Result<()> {
        let mut problems = location_problems(locations);
        problems.extend(placement_problems(locations, groups));
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        self.sheet.write_items(locations)
    }

    fn write_groups(&self, groups: &[LocationGroup]) -> anyhow::Result<()> {
        let problems = group_problems(groups);
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        self.group_sheet.write_items(groups)
    }
}

/// The group with this id (the last, if there are several).
fn group_by_id<'a>(groups: &'a [LocationGroup], id: &str) -> Option<&'a LocationGroup> {
    if id.is_empty() {
        return None;
    }
    groups.iter().rev().find(|g| g.id.as_deref() == Some(id))
}

/// The group `group_id` names and the groups it's inside, nearest first,
/// stopping at a missing group or a cycle.
pub fn enclosing<'a>(groups: &'a [LocationGroup], group_id: Option<&str>) -> Vec<&'a LocationGroup> {
    let mut found: Vec<&LocationGroup> = Vec::new();
    let mut next = group_id.and_then(|g| group_by_id(groups, g));
    while let Some(group) = next {
        if found.iter().any(|f| f.id == group.id) {
            break;
        }
        found.push(group);
        next = group.group_id.as_deref().and_then(|g| group_by_id(groups, g));
    }
    found
}

/// `name`, after the names of the groups `group_id` is inside, outermost
/// first -- stopping short of `own_id`, if it comes round to itself.
fn path(groups: &[LocationGroup], name: Option<&str>, group_id: Option<&str>, own_id: Option<&str>) -> String {
    let mut names: Vec<&str> = vec![name.unwrap_or_default()];
    for g in enclosing(groups, group_id) {
        if own_id.is_some() && g.id.as_deref() == own_id {
            break;
        }
        names.push(g.name.as_deref().unwrap_or_default());
    }
    names.reverse();
    names.join(" › ")
}

pub fn listed(location: &Location, groups: &[LocationGroup]) -> ListedLocation {
    ListedLocation {
        id: location.id.clone(),
        name: location.name.clone(),
        hint: location.hint.clone(),
        map_location: location.map_location.clone(),
        group_id: location.group_id.clone(),
        path: path(groups, location.name.as_deref(), location.group_id.as_deref(), None),
    }
}

pub fn listed_group(group: &LocationGroup, groups: &[LocationGroup]) -> ListedLocationGroup {
    ListedLocationGroup {
        id: group.id.clone(),
        group_id: group.group_id.clone(),
        name: group.name.clone(),
        note: group.note.clone(),
        path: path(groups, group.name.as_deref(), group.group_id.as_deref(), group.id.as_deref()),
    }
}

/// Enclosing groups before those inside them, siblings in sheet order;
/// groups inside a missing one last.
fn ordered(groups: &[LocationGroup]) -> Vec<&LocationGroup> {
    let parent_of = |g: &LocationGroup| g.group_id.clone().filter(|p| group_by_id(groups, p).is_some());
    fn visit(groups: &[LocationGroup], index: usize, order: &mut Vec<usize>, parent_of: &dyn Fn(&LocationGroup) -> Option<String>) {
        if order.contains(&index) {
            return;
        }
        order.push(index);
        let Some(id) = groups[index].id.as_deref().filter(|i| !i.is_empty()) else { return };
        for (i, child) in groups.iter().enumerate() {
            if parent_of(child).as_deref() == Some(id) {
                visit(groups, i, order, parent_of);
            }
        }
    }
    let mut order: Vec<usize> = Vec::new();
    let roots: Vec<usize> = (0..groups.len()).filter(|i| parent_of(&groups[*i]).is_none()).collect();
    for index in roots.into_iter().chain(0..groups.len()) {
        visit(groups, index, &mut order, &parent_of);
    }
    order.into_iter().map(|i| &groups[i]).collect()
}

/// The location group with this id, or else this name (ignoring case); an
/// error suggesting close matches if there's none.
pub fn find_group(groups: &[LocationGroup], id_or_name: &str) -> anyhow::Result<LocationGroup> {
    let key = |g: &LocationGroup| g.name.as_deref().unwrap_or_default().to_lowercase();
    let by_name = |name: &str| groups.iter().rev().find(|g| key(g) == name).cloned();
    if let Some(found) = groups.iter().find(|g| g.id.as_deref() == Some(id_or_name)) {
        return Ok(found.clone());
    }
    if let Some(found) = by_name(&id_or_name.to_lowercase()) {
        return Ok(found);
    }
    let ids: Vec<String> = dedup(groups.iter().filter_map(|g| g.id.clone().filter(|i| !i.is_empty())));
    let names: Vec<String> = dedup(groups.iter().map(key));
    let mut close: Vec<LocationGroup> = Vec::new();
    for id in crate::difflib::get_close_matches(id_or_name, &ids, 3) {
        if let Some(g) = groups.iter().rev().find(|g| g.id.as_deref() == Some(id.as_str())) {
            close.push(g.clone());
        }
    }
    for name in crate::difflib::get_close_matches(&id_or_name.to_lowercase(), &names, 3) {
        if let Some(g) = by_name(&name) {
            close.push(g);
        }
    }
    let hints: Vec<String> = dedup(close.iter().map(|g| format!("{} ({})", g.id.as_deref().unwrap_or("None"), g.name.as_deref().unwrap_or("None"))));
    let hint = hints.join(", ");
    bail!(
        "There's no location group with the id or name {}{} (get_location_groups lists them)",
        py::repr(id_or_name),
        if hint.is_empty() { String::new() } else { format!("; did you mean {hint}?") }
    )
}

/// Everything wrong with the location groups as a whole, as phrases.
pub fn group_problems(groups: &[LocationGroup]) -> Vec<String> {
    let mut problems = Vec::new();
    let mut seen_ids: Vec<&str> = Vec::new();
    let mut names: Vec<(String, &LocationGroup)> = Vec::new();
    for group in groups {
        let id = group.id.as_deref().filter(|i| !i.is_empty());
        let label = match id {
            Some(id) => format!("location group {id}"),
            None => format!("location group {}", group.name.as_deref().map(py::repr).unwrap_or_else(|| "None".into())),
        };
        match id {
            None => problems.push(format!("{label} has no id")),
            Some(id) if seen_ids.contains(&id) => problems.push(format!("location group id {} is used more than once", py::repr(id))),
            Some(id) => seen_ids.push(id),
        }
        match group.name.as_deref().filter(|n| !n.trim().is_empty()) {
            None => problems.push(format!("{label} needs a name")),
            Some(name) if name.chars().count() > MAX_NAME_LENGTH => {
                problems.push(format!("{label}'s name is longer than {MAX_NAME_LENGTH} characters"))
            }
            Some(name) => {
                let key = name.trim().to_lowercase();
                if let Some((_, other)) = names.iter().find(|(k, _)| *k == key) {
                    problems.push(format!(
                        "there's already a location group named {} ({}); group names must be unique (a location may share one)",
                        py::repr(other.name.as_deref().unwrap_or_default()),
                        other.id.as_deref().unwrap_or("None")
                    ));
                }
                names.retain(|(k, _)| *k != key);
                names.push((key, group));
            }
        }
        if let Some(parent) = group.group_id.as_deref().filter(|p| !p.is_empty()) {
            match group_by_id(groups, parent) {
                None => problems.push(format!("{label} is inside {}, which isn't a location group", py::repr(parent))),
                Some(_) => {
                    if id.is_some() && enclosing(groups, Some(parent)).iter().any(|g| g.id.as_deref() == id) {
                        problems.push(format!("{label} can't be inside itself, directly or through other groups"));
                    }
                }
            }
        }
    }
    problems
}

/// Each location whose group_id isn't a location group's.
pub fn placement_problems(locations: &[Location], groups: &[LocationGroup]) -> Vec<String> {
    locations
        .iter()
        .filter_map(|l| {
            let group = l.group_id.as_deref().filter(|g| !g.is_empty())?;
            group_by_id(groups, group).is_none().then(|| {
                format!(
                    "location {} is in {}, which isn't a location group (get_location_groups lists them)",
                    l.id.as_deref().or(l.name.as_deref()).unwrap_or("None"),
                    py::repr(group)
                )
            })
        })
        .collect()
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
