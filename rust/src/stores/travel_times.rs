//! Travel times: rough estimates of how long it takes to get between two
//! places -- each a location or a location group -- by each way of
//! getting there (an action or action group: "Drive", "Transit"), in the
//! Travel Times tab. A pair counts both ways unless it's one_way.

use crate::contract::{RowItem, Schema};
use crate::google::sheets::TabWrite;
use crate::py;
use crate::sheet::rows::{self, RowType};
use crate::stores::locations::{enclosing, Location, LocationGroup, Locations};
use anyhow::bail;
use serde::{Deserialize, Serialize};

/// How long one way of getting there takes: `minutes`, or from `minutes`
/// to `max_minutes`.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize, Schema)]
#[schema(deny_unknown)]
pub struct TravelMode {
    /// The action or action group it's done by ("Drive", "Transit").
    pub mode_id: String,
    pub minutes: i64,
    /// The most it takes, if it varies; at least minutes.
    pub max_minutes: Option<i64>,
}

/// How long it takes to get between two places (see get_travel_times).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, Schema, RowItem)]
#[schema(deny_unknown)]
pub struct TravelTime {
    pub id: Option<String>,
    /// A location or location group.
    pub from_id: Option<String>,
    /// A location or location group.
    pub to_id: Option<String>,
    /// Whether it's only from from_id to to_id; otherwise it's both ways.
    pub one_way: Option<bool>,
    pub times: Option<Vec<TravelMode>>,
    pub note: Option<String>,
    /// Goes up by one each time it changes. Give it to an update (and to a delete, as if_version): it's the version you last read, required -- one saved before versions is version 0 -- changed since, the change is refused; on a conflict, read it again: if what changed elsewhere is in fields you aren't changing, make your change again on it as it is now; if it's in fields you're changing, or it was deleted (or, deleting it, it was changed since), stop and ask the user -- showing what changed and what you meant to do -- and never overwrite their change, or put back what was deleted, without their OK.
    pub version: Option<i64>,
}

/// A travel time's fields that clear_fields can blank.
#[derive(Clone, Copy, Debug, Serialize, Deserialize, Schema)]
pub enum ClearableField {
    #[serde(rename = "note")]
    Note,
}

#[derive(Debug, Serialize, Deserialize, Schema)]
pub struct CreatedTravelTime {
    pub travel_time: TravelTime,
    pub created_id: String,
}

/// The travel time that best answers how long it takes to get from one
/// place to another (see estimate_travel_time).
#[derive(Debug, Serialize, Deserialize, Schema)]
pub struct TravelEstimate {
    /// The places it's between, as asked or the groups they're in -- from
    /// where to where, whichever way the travel time was given.
    pub from_id: String,
    pub to_id: String,
    pub travel_time: TravelTime,
    /// With a mode, the one time for it (its own, or its nearest group's);
    /// without, every time the travel time has.
    pub times: Vec<TravelMode>,
}

pub const ROW: RowType = <TravelTime as rows::RowItem>::ROW_TYPE;
pub const CLEARABLE_FIELDS: [&str; 1] = ["note"];

impl Locations {
    pub fn travel_times(&self) -> anyhow::Result<Vec<TravelTime>> {
        self.travel_sheet.read_items()
    }

    /// Every travel time; with `place_id`, those from or to it.
    pub fn get_travel_times(&self, place_id: Option<&str>) -> anyhow::Result<Vec<TravelTime>> {
        let all = self.travel_times()?;
        Ok(match place_id {
            None => all,
            Some(id) => all.into_iter().filter(|t| t.from_id.as_deref() == Some(id) || t.to_id.as_deref() == Some(id)).collect(),
        })
    }

    /// `modes` are the ids a mode_id can be: every action's and group's.
    pub fn create_travel_time(&self, travel: &TravelTime, modes: &[String]) -> anyhow::Result<CreatedTravelTime> {
        self.prefetch(&self.all_tabs())?;
        let mut all = self.travel_times()?;
        let id = match rows::created(travel, &all, &[])? {
            rows::Created::New(id) => id,
            rows::Created::Again(made) => return Ok(CreatedTravelTime { created_id: made.id.clone().unwrap_or_default(), travel_time: made }),
        };
        let new = TravelTime { id: Some(id.clone()), version: None, ..travel.clone() };
        all.push(new.clone());
        self.write_travel_times(&all, modes)?;
        Ok(CreatedTravelTime { travel_time: new, created_id: id })
    }

    pub fn update_travel_time(&self, travel: &TravelTime, clear_fields: &[String], modes: &[String]) -> anyhow::Result<TravelTime> {
        let Some(id) = travel.id.clone().filter(|i| !i.is_empty()) else {
            bail!("update_travel_time needs the travel time's id");
        };
        rows::check_clear_item(travel, clear_fields, &CLEARABLE_FIELDS)?;
        self.prefetch(&self.all_tabs())?;
        let mut all = self.travel_times()?;
        let Some(index) = all.iter().position(|t| t.id.as_deref() == Some(id.as_str())) else {
            rows::expect_item_version::<TravelTime>(&id, None, travel.version)?;
            bail!("There's no travel time with the id {} (get_travel_times lists them)", py::repr(&id));
        };
        all[index] = rows::updated_item(&all[index], travel, clear_fields, &["id"])?;
        self.write_travel_times(&all, modes)?;
        Ok(all[index].clone())
    }

    /// Deletes a travel time -- unless it's changed since version
    /// `if_version`, if given.
    pub fn delete_travel_time(&self, travel_time_id: &str, if_version: Option<i64>) -> anyhow::Result<TravelTime> {
        let all = self.travel_times()?;
        let found = all.iter().find(|t| t.id.as_deref() == Some(travel_time_id)).cloned();
        if !rows::deleting(travel_time_id, found.as_ref(), if_version)? && if_version.is_some() {
            return Ok(rows::gone(travel_time_id));
        }
        let Some(deleted) = found else {
            bail!("There's no travel time with the id {} (get_travel_times lists them)", py::repr(travel_time_id));
        };
        let kept: Vec<TravelTime> = all.into_iter().filter(|t| t.id.as_deref() != Some(travel_time_id)).collect();
        self.travel_sheet.write_items(&kept)?;
        Ok(deleted)
    }

    /// Takes out every travel time from or to `place_id`, a location or
    /// group being deleted.
    /// The write that forgets the travel times to and from `place_id`, if
    /// there are any.
    pub(crate) fn forgetting_travel_times(&self, place_id: &str) -> anyhow::Result<Option<TabWrite>> {
        let all = self.travel_times()?;
        let names = |t: &TravelTime| t.from_id.as_deref() == Some(place_id) || t.to_id.as_deref() == Some(place_id);
        if !all.iter().any(names) {
            return Ok(None);
        }
        let kept: Vec<TravelTime> = all.into_iter().filter(|t| !names(t)).collect();
        Ok(Some(self.travel_sheet.prepared_items(&kept)?))
    }

    /// How long it takes from `from_id` to `to_id` (each a location or
    /// group): the travel time between them, or else between the groups
    /// they're in, fewest steps out first. With `mode_ids` (a mode and the groups
    /// it's in, nearest first), only a travel time with a time for one of
    /// them, nearest first. A one-way travel time beats a two-way one
    /// between the same places. None if there's none.
    pub fn estimate_travel_time(&self, from_id: &str, to_id: &str, mode_ids: Option<&[String]>) -> anyhow::Result<Option<TravelEstimate>> {
        self.prefetch(&self.all_tabs())?;
        let (locations, groups) = (self.all()?, self.groups()?);
        let from = places_around(from_id, &locations, &groups)?;
        let to = places_around(to_id, &locations, &groups)?;
        let all = self.travel_times()?;
        let mut pairs: Vec<(usize, usize)> = (0..from.len()).flat_map(|i| (0..to.len()).map(move |j| (i, j))).collect();
        // Fewest steps out first; of those, the most even (so it's the
        // same either way round), then from's nearer.
        pairs.sort_by_key(|(i, j)| (i + j, *i.max(j), *i));
        for (i, j) in pairs {
            let (a, b) = (from[i].as_str(), to[j].as_str());
            let one_way = all.iter().filter(|t| t.one_way == Some(true) && t.from_id.as_deref() == Some(a) && t.to_id.as_deref() == Some(b));
            let both_ways = all.iter().filter(|t| t.one_way != Some(true) && between(t, a, b));
            let found: Vec<&TravelTime> = one_way.chain(both_ways).collect();
            let estimate = |travel: &TravelTime, times: Vec<TravelMode>| TravelEstimate {
                from_id: a.to_string(),
                to_id: b.to_string(),
                travel_time: travel.clone(),
                times,
            };
            match mode_ids {
                None => {
                    if let Some(travel) = found.first() {
                        return Ok(Some(estimate(travel, travel.times.clone().unwrap_or_default())));
                    }
                }
                Some(modes) => {
                    for mode in modes {
                        for travel in &found {
                            if let Some(time) = travel.times.iter().flatten().find(|t| &t.mode_id == mode) {
                                return Ok(Some(estimate(travel, vec![time.clone()])));
                            }
                        }
                    }
                }
            }
        }
        Ok(None)
    }

    fn write_travel_times(&self, all: &[TravelTime], modes: &[String]) -> anyhow::Result<()> {
        let problems = travel_problems(all, &self.all()?, &self.groups()?, modes);
        if !problems.is_empty() {
            bail!("{}", problems.join("; "));
        }
        self.travel_sheet.write_items(all)
    }
}

/// Whether `travel` is between `a` and `b`, either way round.
fn between(travel: &TravelTime, a: &str, b: &str) -> bool {
    let (from, to) = (travel.from_id.as_deref(), travel.to_id.as_deref());
    (from == Some(a) && to == Some(b)) || (from == Some(b) && to == Some(a))
}

/// `id` (a location or group), and the groups it's in, nearest first.
fn places_around(id: &str, locations: &[Location], groups: &[LocationGroup]) -> anyhow::Result<Vec<String>> {
    let group_id = if let Some(location) = locations.iter().rev().find(|l| l.id.as_deref() == Some(id)) {
        location.group_id.clone()
    } else if let Some(group) = groups.iter().rev().find(|g| g.id.as_deref() == Some(id)) {
        group.group_id.clone()
    } else {
        bail!("{} isn't a location or a location group (get_locations and get_location_groups list them)", py::repr(id));
    };
    let mut around = vec![id.to_string()];
    around.extend(enclosing(groups, group_id.as_deref()).iter().filter_map(|g| g.id.clone()).filter(|g| g != id));
    Ok(around)
}

/// Everything wrong with the travel times as a whole, as phrases.
pub fn travel_problems(all: &[TravelTime], locations: &[Location], groups: &[LocationGroup], modes: &[String]) -> Vec<String> {
    let is_place = |id: &str| locations.iter().any(|l| l.id.as_deref() == Some(id)) || groups.iter().any(|g| g.id.as_deref() == Some(id));
    let mut problems = Vec::new();
    for (index, travel) in all.iter().enumerate() {
        let label = format!("travel time {}", travel.id.as_deref().unwrap_or("None"));
        let (from, to) = (travel.from_id.as_deref().filter(|i| !i.is_empty()), travel.to_id.as_deref().filter(|i| !i.is_empty()));
        for (end, id) in [("from_id", from), ("to_id", to)] {
            match id {
                None => problems.push(format!("{label} needs a {end}")),
                Some(id) if !is_place(id) => {
                    problems.push(format!("{label}'s {end} {} isn't a location or a location group", py::repr(id)))
                }
                Some(_) => {}
            }
        }
        if from.is_some() && from == to {
            problems.push(format!("{label} is from a place to itself"));
        }
        let times = travel.times.as_deref().unwrap_or_default();
        for (i, time) in times.iter().enumerate() {
            if !modes.contains(&time.mode_id) {
                problems.push(format!("{label}'s mode_id {} isn't an action or an action group", py::repr(&time.mode_id)));
            }
            if times[..i].iter().any(|t| t.mode_id == time.mode_id) {
                problems.push(format!("{label} has more than one time for {}", py::repr(&time.mode_id)));
            }
            if time.minutes <= 0 {
                problems.push(format!("{label}'s minutes for {} must be more than 0", py::repr(&time.mode_id)));
            }
            if time.max_minutes.is_some_and(|max| max < time.minutes) {
                problems.push(format!("{label}'s max_minutes for {} is less than its minutes", py::repr(&time.mode_id)));
            }
        }
        // Two travel times can't say how long the same way takes.
        let (Some(a), Some(b)) = (from, to) else { continue };
        let one_way = travel.one_way == Some(true);
        let same = all[..index].iter().find(|other| {
            if one_way != (other.one_way == Some(true)) {
                return false;
            }
            if one_way {
                other.from_id.as_deref() == Some(a) && other.to_id.as_deref() == Some(b)
            } else {
                between(other, a, b)
            }
        });
        if let Some(other) = same {
            problems.push(format!(
                "{label} is between the same places as travel time {}{}; give each way's times in one",
                other.id.as_deref().unwrap_or("None"),
                if one_way { ", the same way" } else { "" }
            ));
        }
    }
    problems
}

// Written by the app: made once by their ids and versions.
impl rows::Versioned for TravelTime {}

#[cfg(test)]
#[path = "travel_times_tests.rs"]
mod tests;
