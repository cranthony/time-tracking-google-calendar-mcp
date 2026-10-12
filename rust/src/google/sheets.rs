//! Google Sheets, behind a thin client (calendar_clients/google_sheets.py):
//! spreadsheets, tabs, rows and columns, knowing nothing of what a tab
//! means. Reads of a tab inside one tool call are cached (see
//! `CachedReads`), and a 429 is retried with backoff, as in Python.

use super::{status_of, Google};
use crate::py;
use crate::write_lock;
use anyhow::{anyhow, bail};
use regex::Regex;
use serde_json::{json, Map, Value};
use std::cell::RefCell;
use std::sync::{Arc, LazyLock};

/// A range of one tab: `range` ("A1:C", no sheet name) of tab `sheet_id`.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct TabRange {
    pub spreadsheet_id: String,
    pub sheet_id: i64,
    pub range: String,
}

impl TabRange {
    pub fn new(spreadsheet_id: &str, sheet_id: i64, range: &str) -> TabRange {
        TabRange { spreadsheet_id: spreadsheet_id.into(), sheet_id, range: range.into() }
    }
}

pub type Rows = Vec<Vec<String>>;

thread_local! {
    /// What `CachedReads` holds for the tool call running on this thread:
    /// `None` outside one. A tool call runs wholly on one thread, so this
    /// plays the part of Python's ContextVar.
    static READ_CACHE: RefCell<Option<Vec<(TabRange, Rows)>>> = const { RefCell::new(None) };
}

/// Within this guard's life, a read repeating an earlier one (same tab,
/// within the range read before) is answered from memory; a write forgets
/// that tab first. Nested guards share the outer one's cache.
pub struct CachedReads {
    outer: bool,
}

impl CachedReads {
    pub fn start() -> CachedReads {
        let outer = READ_CACHE.with(|c| {
            let mut cache = c.borrow_mut();
            if cache.is_none() {
                *cache = Some(Vec::new());
                true
            } else {
                false
            }
        });
        CachedReads { outer }
    }
}

impl Drop for CachedReads {
    fn drop(&mut self) {
        if self.outer {
            READ_CACHE.with(|c| *c.borrow_mut() = None);
        }
    }
}

fn caching() -> bool {
    READ_CACHE.with(|c| c.borrow().is_some())
}

fn cached_rows(wanted: &TabRange) -> Option<Rows> {
    READ_CACHE.with(|c| {
        let cache = c.borrow();
        let cache = cache.as_ref()?;
        if let Some((_, rows)) = cache.iter().find(|(r, _)| r == wanted) {
            return Some(rows.clone());
        }
        let inner = grid_range(wanted.sheet_id, &wanted.range).ok()?;
        for (cached, rows) in cache {
            if cached.spreadsheet_id != wanted.spreadsheet_id || cached.sheet_id != wanted.sheet_id {
                continue;
            }
            let outer = grid_range(cached.sheet_id, &cached.range).ok()?;
            if contains(&outer, &inner) {
                return Some(within(rows, &outer, &inner));
            }
        }
        None
    })
}

fn remember(range: &TabRange, rows: &Rows) {
    READ_CACHE.with(|c| {
        if let Some(cache) = c.borrow_mut().as_mut() {
            cache.retain(|(r, _)| r != range);
            cache.push((range.clone(), rows.clone()));
        }
    });
}

/// Forget cached reads of `sheet_id` -- or every tab, for `None`.
fn forget(spreadsheet_id: &str, sheet_id: Option<i64>) {
    READ_CACHE.with(|c| {
        if let Some(cache) = c.borrow_mut().as_mut() {
            cache.retain(|(r, _)| !(r.spreadsheet_id == spreadsheet_id && sheet_id.map_or(true, |s| s == r.sheet_id)));
        }
    });
}

#[derive(Debug, Clone, PartialEq)]
pub struct GridRange {
    pub sheet_id: i64,
    pub start_row: i64,
    pub start_column: i64,
    pub end_column: i64,
    pub end_row: Option<i64>,
}

impl GridRange {
    pub fn to_json(&self) -> Value {
        let mut map = Map::new();
        map.insert("sheetId".into(), json!(self.sheet_id));
        map.insert("startRowIndex".into(), json!(self.start_row));
        map.insert("startColumnIndex".into(), json!(self.start_column));
        map.insert("endColumnIndex".into(), json!(self.end_column));
        if let Some(end) = self.end_row {
            map.insert("endRowIndex".into(), json!(end));
        }
        Value::Object(map)
    }
}

static A1_RANGE: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^([A-Z]+)(\d+):([A-Z]+)(\d*)$").unwrap());

/// "A2:D" (open-ended) or "B5:C7" (bounded) as a 0-based, end-exclusive
/// GridRange on `sheet_id`.
pub fn grid_range(sheet_id: i64, range: &str) -> anyhow::Result<GridRange> {
    let caps = A1_RANGE.captures(range).ok_or_else(|| anyhow!("Unsupported range {}", py::repr(range)))?;
    let last_row = &caps[4];
    Ok(GridRange {
        sheet_id,
        start_row: caps[2].parse::<i64>()? - 1,
        start_column: column_number(&caps[1]) - 1,
        end_column: column_number(&caps[3]),
        end_row: if last_row.is_empty() { None } else { Some(last_row.parse()?) },
    })
}

pub fn column_number(letters: &str) -> i64 {
    letters.bytes().fold(0, |n, b| n * 26 + (b - b'A' + 1) as i64)
}

fn contains(outer: &GridRange, inner: &GridRange) -> bool {
    if !(outer.start_column <= inner.start_column && inner.end_column <= outer.end_column && outer.start_row <= inner.start_row) {
        return false;
    }
    match (outer.end_row, inner.end_row) {
        (None, _) => true,
        (Some(o), Some(i)) => i <= o,
        (Some(_), None) => false,
    }
}

fn within(rows: &Rows, outer: &GridRange, inner: &GridRange) -> Rows {
    let first = (inner.start_row - outer.start_row) as usize;
    let last = inner.end_row.map(|e| (e - outer.start_row) as usize);
    let left = (inner.start_column - outer.start_column) as usize;
    let right = (inner.end_column - outer.start_column) as usize;
    let mut result: Rows = Vec::new();
    let end = last.unwrap_or(rows.len()).min(rows.len());
    for row in rows.iter().take(end).skip(first) {
        let mut cells: Vec<String> = row.iter().skip(left).take(right.saturating_sub(left)).cloned().collect();
        while cells.last().is_some_and(|c| c.is_empty()) {
            cells.pop();
        }
        result.push(cells);
    }
    while result.last().is_some_and(|r| r.is_empty()) {
        result.pop();
    }
    result
}

const MAX_RATE_LIMIT_RETRIES: u32 = 6;

#[derive(Clone)]
pub struct SheetsClient {
    google: Arc<Google>,
}

fn rows_of(value: Option<&Value>) -> Rows {
    value
        .and_then(Value::as_array)
        .map(|rows| {
            rows.iter()
                .map(|row| row.as_array().map(|cells| cells.iter().map(cell_text).collect()).unwrap_or_default())
                .collect()
        })
        .unwrap_or_default()
}

fn cell_text(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

impl SheetsClient {
    pub fn new(google: Arc<Google>) -> SheetsClient {
        SheetsClient { google }
    }

    /// One request, retried with randomized exponential backoff (about a
    /// minute in all) on a 429 -- and only then: other failures may have
    /// been applied, and some requests aren't safe to repeat.
    fn execute(&self, method: &str, path: &str, query: &[(&str, String)], body: Option<&Value>) -> anyhow::Result<Value> {
        let url = self.google.sheets_url(path);
        let mut attempt = 0;
        loop {
            match self.google.request(method, &url, query, body, None) {
                Err(error) if status_of(&error) == Some(429) && attempt < MAX_RATE_LIMIT_RETRIES => {
                    let fraction = (uuid::Uuid::new_v4().as_u128() % 1_000_000) as f64 / 1_000_000.0;
                    std::thread::sleep(std::time::Duration::from_secs_f64(fraction * 2f64.powi(attempt as i32)));
                    attempt += 1;
                }
                other => return other,
            }
        }
    }

    pub fn create_spreadsheet(&self, title: &str) -> anyhow::Result<String> {
        write_lock::require("SheetsClient.create_spreadsheet")?;
        let response = self.execute(
            "POST",
            "v4/spreadsheets",
            &[("fields", "spreadsheetId".into())],
            Some(&json!({"properties": {"title": title}})),
        )?;
        Ok(response["spreadsheetId"].as_str().ok_or_else(|| anyhow!("no spreadsheetId"))?.to_string())
    }

    fn batch_update(&self, spreadsheet_id: &str, body: Value, query: &[(&str, String)]) -> anyhow::Result<Value> {
        self.execute("POST", &format!("v4/spreadsheets/{spreadsheet_id}:batchUpdate"), query, Some(&body))
    }

    pub fn add_sheet(&self, spreadsheet_id: &str, title: &str, tab_color: Option<&Value>) -> anyhow::Result<i64> {
        write_lock::require("SheetsClient.add_sheet")?;
        let mut properties = Map::new();
        properties.insert("title".into(), json!(title));
        if let Some(color) = tab_color {
            properties.insert("tabColor".into(), color.clone());
        }
        let response =
            self.batch_update(spreadsheet_id, json!({"requests": [{"addSheet": {"properties": properties}}]}), &[])?;
        response["replies"][0]["addSheet"]["properties"]["sheetId"].as_i64().ok_or_else(|| anyhow!("no sheetId"))
    }

    pub fn update_sheet_properties(
        &self,
        spreadsheet_id: &str,
        sheet_id: i64,
        title: Option<&str>,
        tab_color: Option<&Value>,
    ) -> anyhow::Result<()> {
        write_lock::require("SheetsClient.update_sheet_properties")?;
        let mut properties = Map::new();
        properties.insert("sheetId".into(), json!(sheet_id));
        let mut fields = Vec::new();
        if let Some(title) = title {
            properties.insert("title".into(), json!(title));
            fields.push("title");
        }
        if let Some(color) = tab_color {
            properties.insert("tabColor".into(), color.clone());
            fields.push("tabColor");
        }
        if fields.is_empty() {
            return Ok(());
        }
        self.batch_update(
            spreadsheet_id,
            json!({"requests": [{"updateSheetProperties": {"properties": properties, "fields": fields.join(",")}}]}),
            &[],
        )?;
        Ok(())
    }

    pub fn get_sheet_title(&self, spreadsheet_id: &str, sheet_id: i64) -> anyhow::Result<String> {
        let response =
            self.execute("GET", &format!("v4/spreadsheets/{spreadsheet_id}"), &[("fields", "sheets.properties".into())], None)?;
        for sheet in response.get("sheets").and_then(Value::as_array).into_iter().flatten() {
            if sheet["properties"]["sheetId"].as_i64() == Some(sheet_id) {
                return Ok(sheet["properties"]["title"].as_str().unwrap_or_default().to_string());
            }
        }
        bail!("No sheet with sheetId {sheet_id} in spreadsheet {spreadsheet_id}")
    }

    pub fn create_sheet_metadata(&self, spreadsheet_id: &str, sheet_id: i64, key: &str, value: &str) -> anyhow::Result<()> {
        write_lock::require("SheetsClient.create_sheet_metadata")?;
        self.batch_update(
            spreadsheet_id,
            json!({"requests": [{"createDeveloperMetadata": {"developerMetadata": {
                "metadataKey": key, "metadataValue": value, "location": {"sheetId": sheet_id}, "visibility": "PROJECT"
            }}}]}),
            &[],
        )?;
        Ok(())
    }

    pub fn find_sheet_id(&self, spreadsheet_id: &str, key: &str, value: &str) -> anyhow::Result<Option<i64>> {
        let response = self.execute(
            "POST",
            &format!("v4/spreadsheets/{spreadsheet_id}/developerMetadata:search"),
            &[],
            Some(&json!({"dataFilters": [{"developerMetadataLookup": {"metadataKey": key, "metadataValue": value}}]})),
        )?;
        Ok(response
            .get("matchedDeveloperMetadata")
            .and_then(Value::as_array)
            .and_then(|m| m.first())
            .and_then(|m| m["developerMetadata"]["location"]["sheetId"].as_i64()))
    }

    pub fn set_column_width(&self, spreadsheet_id: &str, sheet_id: i64, column_index: i64, pixel_width: i64) -> anyhow::Result<()> {
        write_lock::require("SheetsClient.set_column_width")?;
        self.batch_update(
            spreadsheet_id,
            json!({"requests": [{"updateDimensionProperties": {
                "range": {"sheetId": sheet_id, "dimension": "COLUMNS", "startIndex": column_index, "endIndex": column_index + 1},
                "properties": {"pixelSize": pixel_width},
                "fields": "pixelSize"
            }}]}),
            &[],
        )?;
        Ok(())
    }

    /// Delete rows `start_row`..=`end_row` (1-based), then add blank rows at
    /// the bottom if that leaves fewer than `keep_at_least`.
    pub fn delete_rows(&self, spreadsheet_id: &str, sheet_id: i64, start_row: i64, end_row: i64, keep_at_least: i64) -> anyhow::Result<()> {
        write_lock::require("SheetsClient.delete_rows")?;
        forget(spreadsheet_id, Some(sheet_id));
        let response = self.batch_update(
            spreadsheet_id,
            json!({
                "requests": [{"deleteDimension": {"range": {
                    "sheetId": sheet_id, "dimension": "ROWS", "startIndex": start_row - 1, "endIndex": end_row
                }}}],
                "includeSpreadsheetInResponse": true,
                "responseIncludeGridData": false
            }),
            &[("fields", "updatedSpreadsheet(sheets(properties(sheetId,gridProperties(rowCount))))".into())],
        )?;
        let row_count = response
            .pointer("/updatedSpreadsheet/sheets")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .find(|s| s["properties"]["sheetId"].as_i64() == Some(sheet_id))
            .and_then(|s| s["properties"]["gridProperties"]["rowCount"].as_i64());
        match row_count {
            Some(count) if count < keep_at_least => {
                self.batch_update(
                    spreadsheet_id,
                    json!({"requests": [{"appendDimension": {"sheetId": sheet_id, "dimension": "ROWS", "length": keep_at_least - count}}]}),
                    &[],
                )?;
            }
            _ => {}
        }
        Ok(())
    }

    pub fn read_rows(&self, spreadsheet_id: &str, sheet_range: &str) -> anyhow::Result<Rows> {
        let response =
            self.execute("GET", &format!("v4/spreadsheets/{spreadsheet_id}/values/{}", encode_range(sheet_range)), &[], None)?;
        Ok(rows_of(response.get("values")))
    }

    pub fn write_rows(&self, spreadsheet_id: &str, sheet_range: &str, rows: &Rows) -> anyhow::Result<()> {
        write_lock::require("SheetsClient.write_rows")?;
        forget(spreadsheet_id, None);
        self.execute(
            "PUT",
            &format!("v4/spreadsheets/{spreadsheet_id}/values/{}", encode_range(sheet_range)),
            &[("valueInputOption", "RAW".into())],
            Some(&json!({"values": rows})),
        )?;
        Ok(())
    }

    pub fn read_rows_in_sheet(&self, spreadsheet_id: &str, sheet_id: i64, range: &str) -> anyhow::Result<Rows> {
        Ok(self.read_ranges_in_sheet(spreadsheet_id, sheet_id, &[range])?.remove(0))
    }

    pub fn read_ranges_in_sheet(&self, spreadsheet_id: &str, sheet_id: i64, ranges: &[&str]) -> anyhow::Result<Vec<Rows>> {
        let wanted: Vec<TabRange> = ranges.iter().map(|r| TabRange::new(spreadsheet_id, sheet_id, r)).collect();
        let mut found: Vec<Option<Rows>> = wanted.iter().map(cached_rows).collect();
        let mut missing: Vec<TabRange> = Vec::new();
        for (range, rows) in wanted.iter().zip(&found) {
            if rows.is_none() && !missing.contains(range) {
                missing.push(range.clone());
            }
        }
        if !missing.is_empty() {
            let fetched = self.fetch(spreadsheet_id, &missing)?;
            for (i, range) in wanted.iter().enumerate() {
                if found[i].is_none() {
                    found[i] = fetched.iter().find(|(r, _)| r == range).map(|(_, rows)| rows.clone());
                }
            }
        }
        Ok(found.into_iter().map(Option::unwrap_or_default).collect())
    }

    /// Inside a tool call, read every one of `ranges` not cached yet --
    /// one request per spreadsheet -- so later reads come from memory.
    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        if !caching() {
            return Ok(());
        }
        let mut missing: Vec<TabRange> = Vec::new();
        for range in ranges {
            if cached_rows(range).is_none() && !missing.contains(range) {
                missing.push(range.clone());
            }
        }
        let mut spreadsheets: Vec<String> = Vec::new();
        for range in &missing {
            if !spreadsheets.contains(&range.spreadsheet_id) {
                spreadsheets.push(range.spreadsheet_id.clone());
            }
        }
        for spreadsheet_id in spreadsheets {
            let these: Vec<TabRange> = missing.iter().filter(|r| r.spreadsheet_id == spreadsheet_id).cloned().collect();
            self.fetch(&spreadsheet_id, &these)?;
        }
        Ok(())
    }

    fn fetch(&self, spreadsheet_id: &str, ranges: &[TabRange]) -> anyhow::Result<Vec<(TabRange, Rows)>> {
        let grids: Vec<GridRange> = ranges.iter().map(|r| grid_range(r.sheet_id, &r.range)).collect::<anyhow::Result<_>>()?;
        let filters: Vec<Value> = grids.iter().map(|g| json!({"gridRange": g.to_json()})).collect();
        let response = self.execute(
            "POST",
            &format!("v4/spreadsheets/{spreadsheet_id}/values:batchGetByDataFilter"),
            &[],
            Some(&json!({"dataFilters": filters, "majorDimension": "ROWS"})),
        )?;
        let values = values_by_filter(&response, &grids);
        let fetched: Vec<(TabRange, Rows)> = ranges.iter().cloned().zip(values).collect();
        for (range, rows) in &fetched {
            remember(range, rows);
        }
        Ok(fetched)
    }

    /// Write `rows` at `range` of tab `sheet_id`, through a bounded
    /// GridRange -- falling back to a title-qualified A1 range (which grows
    /// the grid) when they'd land past the tab's last row.
    pub fn write_rows_in_sheet(&self, spreadsheet_id: &str, sheet_id: i64, range: &str, rows: &Rows) -> anyhow::Result<()> {
        write_lock::require("SheetsClient.write_rows_in_sheet")?;
        if rows.is_empty() {
            return Ok(());
        }
        forget(spreadsheet_id, Some(sheet_id));
        let mut grid = grid_range(sheet_id, range)?;
        grid.end_row = Some(grid.start_row + rows.len() as i64);
        let result = self.execute(
            "POST",
            &format!("v4/spreadsheets/{spreadsheet_id}/values:batchUpdateByDataFilter"),
            &[],
            Some(&json!({
                "valueInputOption": "RAW",
                "data": [{"dataFilter": {"gridRange": grid.to_json()}, "majorDimension": "ROWS", "values": rows}]
            })),
        );
        match result {
            Err(error) if status_of(&error) == Some(400) => {}
            Err(error) => return Err(error),
            Ok(response) => {
                let wanted: usize = rows.iter().map(Vec::len).sum();
                if response.get("totalUpdatedCells").and_then(Value::as_i64).unwrap_or(0) >= wanted as i64 {
                    return Ok(());
                }
            }
        }
        let title = self.get_sheet_title(spreadsheet_id, sheet_id)?;
        let qualified = format!("'{}'!{range}", title.replace('\'', "''"));
        self.write_rows(spreadsheet_id, &qualified, rows)
    }
}

/// Each grid range's rows from a batchGetByDataFilter answer, matched by
/// the filter each value range echoes, else by order.
fn values_by_filter(response: &Value, grids: &[GridRange]) -> Vec<Rows> {
    let mut by_range: Vec<Option<Rows>> = vec![None; grids.len()];
    let mut unmatched: Vec<Rows> = Vec::new();
    for value_range in response.get("valueRanges").and_then(Value::as_array).into_iter().flatten() {
        let values = rows_of(value_range.pointer("/valueRange/values"));
        let echoed: Vec<Value> = value_range
            .get("dataFilters")
            .and_then(Value::as_array)
            .into_iter()
            .flatten()
            .filter_map(|f| f.get("gridRange").cloned())
            .collect();
        let index = grids.iter().enumerate().position(|(i, g)| by_range[i].is_none() && echoed.contains(&g.to_json()));
        match index {
            Some(i) => by_range[i] = Some(values),
            None => unmatched.push(values),
        }
    }
    let mut leftovers = unmatched.into_iter();
    by_range.into_iter().map(|v| v.unwrap_or_else(|| leftovers.next().unwrap_or_default())).collect()
}

/// A1 ranges go in the URL path: escape what a path can't hold, as the API
/// client does (its quote() keeps only letters, digits and "_.-~").
fn encode_range(range: &str) -> String {
    super::segment(range)
}
