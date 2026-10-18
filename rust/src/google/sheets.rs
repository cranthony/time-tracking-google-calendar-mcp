//! Google Sheets, behind a thin client: spreadsheets, tabs, rows and
//! columns, knowing nothing of what a tab means. Every tab read is kept
//! for the process's life and kept up to date by the client's own writes
//! (see `kept`), so reads come from memory after the first; a write sends
//! only the rows it changes; `refresh` checks what's kept against the
//! spreadsheet and puts back what was changed by hand. A 429 is retried
//! with backoff, as in Python.

use super::kept::{self, Differs, TabIdentity, KEPT_RANGE};
use super::{status_of, Google};
use crate::py;
use crate::write_lock::{self, WRITE_LOCK};
use anyhow::{anyhow, bail};
use regex::Regex;
use serde_json::{json, Map, Value};
#[cfg(test)]
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

#[cfg(test)]
thread_local! {
    /// The unit tests' backoff: each wait, recorded rather than slept.
    pub static PAUSES: RefCell<Vec<f64>> = const { RefCell::new(Vec::new()) };
}

fn pause(seconds: f64) {
    #[cfg(test)]
    PAUSES.with(|p| p.borrow_mut().push(seconds));
    #[cfg(not(test))]
    std::thread::sleep(std::time::Duration::from_secs_f64(seconds));
}

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
                    pause(fraction * 2f64.powi(attempt as i32));
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

    pub fn set_spreadsheet_title(&self, spreadsheet_id: &str, title: &str) -> anyhow::Result<()> {
        write_lock::require("SheetsClient.set_spreadsheet_title")?;
        self.batch_update(
            spreadsheet_id,
            json!({"requests": [{"updateSpreadsheetProperties": {"properties": {"title": title}, "fields": "title"}}]}),
            &[],
        )?;
        Ok(())
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
        let id = response["replies"][0]["addSheet"]["properties"]["sheetId"].as_i64().ok_or_else(|| anyhow!("no sheetId"))?;
        self.kept(spreadsheet_id, |tabs| {
            let tab = tabs.entry(id).or_default();
            tab.rows = Some(Rows::new());
            tab.generation += 1;
        });
        Ok(id)
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
        self.writing_to(spreadsheet_id, sheet_id);
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
        );
        let response = match response {
            Ok(response) => response,
            Err(error) => {
                self.forget_tab(spreadsheet_id, sheet_id);
                return Err(error);
            }
        };
        self.kept(spreadsheet_id, |tabs| {
            let tab = tabs.entry(sheet_id).or_default();
            tab.generation += 1;
            if let Some(rows) = tab.rows.as_mut() {
                kept::delete_rows(rows, (start_row - 1) as usize, end_row as usize);
            }
        });
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

    /// Write `rows` at A1 range `sheet_range` (the tab's title in it):
    /// what's kept of every tab is forgotten, not knowing which it was.
    pub fn write_rows(&self, spreadsheet_id: &str, sheet_range: &str, rows: &Rows) -> anyhow::Result<()> {
        write_lock::require("SheetsClient.write_rows")?;
        kept::forget_spreadsheet(&self.root(), spreadsheet_id);
        self.put_rows(spreadsheet_id, sheet_range, rows)
    }

    fn put_rows(&self, spreadsheet_id: &str, sheet_range: &str, rows: &Rows) -> anyhow::Result<()> {
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

    /// Each of `ranges` of tab `sheet_id`, cut from what's kept of the
    /// tab (read first if it isn't kept yet); a range reaching past the
    /// kept columns is read from Sheets.
    pub fn read_ranges_in_sheet(&self, spreadsheet_id: &str, sheet_id: i64, ranges: &[&str]) -> anyhow::Result<Vec<Rows>> {
        let outer = grid_range(sheet_id, KEPT_RANGE)?;
        let grids: Vec<GridRange> = ranges.iter().map(|r| grid_range(sheet_id, r)).collect::<anyhow::Result<_>>()?;
        if grids.iter().all(|g| contains(&outer, g)) {
            let rows = match self.kept_rows(spreadsheet_id, sheet_id) {
                Some(rows) => rows,
                None => self.load(spreadsheet_id, &[sheet_id])?.remove(0),
            };
            return Ok(grids.iter().map(|g| within(&rows, &outer, g)).collect());
        }
        let wanted: Vec<TabRange> = ranges.iter().map(|r| TabRange::new(spreadsheet_id, sheet_id, r)).collect();
        Ok(self.fetch(spreadsheet_id, &wanted)?.into_iter().map(|(_, rows)| rows).collect())
    }

    /// Read every tab of `ranges` not kept yet -- one request per
    /// spreadsheet -- so later reads of them come from memory.
    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        let mut spreadsheets: Vec<&str> = Vec::new();
        for range in ranges {
            if !spreadsheets.contains(&range.spreadsheet_id.as_str()) {
                spreadsheets.push(&range.spreadsheet_id);
            }
        }
        for spreadsheet_id in spreadsheets {
            let mut missing: Vec<i64> = Vec::new();
            for range in ranges.iter().filter(|r| r.spreadsheet_id == spreadsheet_id) {
                if !missing.contains(&range.sheet_id) && self.kept_rows(spreadsheet_id, range.sheet_id).is_none() {
                    missing.push(range.sheet_id);
                }
            }
            if !missing.is_empty() {
                self.load(spreadsheet_id, &missing)?;
            }
        }
        Ok(())
    }

    fn root(&self) -> String {
        self.google.sheets_url("")
    }

    fn kept<T>(&self, spreadsheet_id: &str, f: impl FnOnce(&mut std::collections::BTreeMap<i64, kept::Tab>) -> T) -> T {
        kept::with(&self.root(), spreadsheet_id, f)
    }

    fn kept_rows(&self, spreadsheet_id: &str, sheet_id: i64) -> Option<Rows> {
        self.kept(spreadsheet_id, |tabs| tabs.get(&sheet_id).and_then(|t| t.rows.clone()))
    }

    /// Read tabs `sheet_ids` whole (their kept cells) in one request, and
    /// keep each -- unless a write to it began meanwhile, which would make
    /// what was read stale.
    fn load(&self, spreadsheet_id: &str, sheet_ids: &[i64]) -> anyhow::Result<Vec<Rows>> {
        let generations: Vec<u64> = self.kept(spreadsheet_id, |tabs| sheet_ids.iter().map(|id| tabs.get(id).map_or(0, |t| t.generation)).collect());
        let ranges: Vec<TabRange> = sheet_ids.iter().map(|id| TabRange::new(spreadsheet_id, *id, KEPT_RANGE)).collect();
        let fetched: Vec<Rows> = self.fetch(spreadsheet_id, &ranges)?.into_iter().map(|(_, rows)| rows).collect();
        self.kept(spreadsheet_id, |tabs| {
            for ((id, generation), rows) in sheet_ids.iter().zip(&generations).zip(&fetched) {
                let tab = tabs.entry(*id).or_default();
                if tab.generation == *generation && tab.rows.is_none() {
                    tab.rows = Some(rows.clone());
                }
            }
        });
        Ok(fetched)
    }

    /// Note what tab `sheet_id` is, so a refresh can make it again if it's
    /// deleted by hand.
    pub fn identify(&self, spreadsheet_id: &str, sheet_id: i64, identity: TabIdentity) {
        self.kept(spreadsheet_id, |tabs| tabs.entry(sheet_id).or_default().identity = Some(identity));
    }

    /// A write to tab `sheet_id` is beginning: a read already under way
    /// mustn't be kept.
    fn writing_to(&self, spreadsheet_id: &str, sheet_id: i64) {
        self.kept(spreadsheet_id, |tabs| tabs.entry(sheet_id).or_default().generation += 1);
    }

    /// Forget what's kept of tab `sheet_id`, so it's read afresh: after a
    /// write that may have been only partly made.
    fn forget_tab(&self, spreadsheet_id: &str, sheet_id: i64) {
        self.kept(spreadsheet_id, |tabs| {
            let tab = tabs.entry(sheet_id).or_default();
            tab.rows = None;
            tab.generation += 1;
        });
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
        Ok(ranges.iter().cloned().zip(values).collect())
    }

    /// Write `rows` at `range` of tab `sheet_id` -- only those of them
    /// that change what's kept, when the tab is kept.
    pub fn write_rows_in_sheet(&self, spreadsheet_id: &str, sheet_id: i64, range: &str, rows: &Rows) -> anyhow::Result<()> {
        write_lock::require("SheetsClient.write_rows_in_sheet")?;
        self.write_runs(spreadsheet_id, &[TabWrite { sheet_id, range: range.into(), rows: rows.clone() }])
    }
}

/// `rows` to write at `range` of tab `sheet_id`, for `write_tabs`.
#[derive(Debug, Clone)]
pub struct TabWrite {
    pub sheet_id: i64,
    pub range: String,
    pub rows: Rows,
}

/// A run of rows to write: `rows` at `grid` (bounded to them).
#[derive(Debug, Clone)]
struct Run {
    grid: GridRange,
    rows: Rows,
}

impl SheetsClient {
    /// Make each of `writes` -- to tabs of `spreadsheet_id` -- in one
    /// request, sending only the rows that change what's kept.
    pub fn write_tabs(&self, spreadsheet_id: &str, writes: &[TabWrite]) -> anyhow::Result<()> {
        write_lock::require("SheetsClient.write_tabs")?;
        self.write_runs(spreadsheet_id, writes)
    }

    /// `writes`, as the runs of rows that change what's kept (all of a
    /// write's rows, for a tab not kept or a range past the kept
    /// columns), made in one request, then kept. Nothing is sent when
    /// nothing changes.
    fn write_runs(&self, spreadsheet_id: &str, writes: &[TabWrite]) -> anyhow::Result<()> {
        let mut runs: Vec<Run> = Vec::new();
        for write in writes.iter().filter(|w| !w.rows.is_empty()) {
            let mut grid = grid_range(write.sheet_id, &write.range)?;
            grid.end_row = Some(grid.start_row + write.rows.len() as i64);
            runs.extend(self.changing(spreadsheet_id, grid, &write.rows));
        }
        if runs.is_empty() {
            return Ok(());
        }
        let mut tabs: Vec<i64> = runs.iter().map(|r| r.grid.sheet_id).collect();
        tabs.dedup();
        for tab in &tabs {
            self.writing_to(spreadsheet_id, *tab);
        }
        match self.send(spreadsheet_id, &runs) {
            Ok(()) => {
                let outer_start = |g: &GridRange| (g.start_row as usize, g.start_column as usize);
                // Up again once it's kept: a refresh that read the tab
                // while the write was under way mustn't take the write for
                // a hand edit.
                self.kept(spreadsheet_id, |kept_tabs| {
                    for run in &runs {
                        let tab = kept_tabs.entry(run.grid.sheet_id).or_default();
                        tab.generation += 1;
                        if let Some(rows) = tab.rows.as_mut() {
                            let (row, column) = outer_start(&run.grid);
                            kept::set_cells(rows, row, column, &run.rows);
                        }
                    }
                });
                Ok(())
            }
            Err(error) => {
                for tab in &tabs {
                    self.forget_tab(spreadsheet_id, *tab);
                }
                Err(error)
            }
        }
    }

    /// The runs of `rows` (to write at `grid`) that change a kept tab's
    /// cells -- or all of them, as one run, when the tab isn't kept or
    /// `grid` reaches past the kept columns.
    fn changing(&self, spreadsheet_id: &str, grid: GridRange, rows: &Rows) -> Vec<Run> {
        let outer = grid_range(grid.sheet_id, KEPT_RANGE).expect("KEPT_RANGE is a range");
        let kept_rows = if contains(&outer, &grid) { self.kept_rows(spreadsheet_id, grid.sheet_id) } else { None };
        let Some(kept_rows) = kept_rows else {
            return vec![Run { grid, rows: rows.clone() }];
        };
        let (top, left) = (grid.start_row as usize, grid.start_column as usize);
        let changes = |i: usize| rows[i].iter().enumerate().any(|(j, text)| kept::cell(&kept_rows, top + i, left + j) != text);
        let mut runs = Vec::new();
        let mut i = 0;
        while i < rows.len() {
            if !changes(i) {
                i += 1;
                continue;
            }
            let start = i;
            while i < rows.len() && changes(i) {
                i += 1;
            }
            let run_grid = GridRange { start_row: (top + start) as i64, end_row: Some((top + i) as i64), ..grid.clone() };
            runs.push(Run { grid: run_grid, rows: rows[start..i].to_vec() });
        }
        runs
    }

    /// Make `runs` in one request, through bounded GridRanges -- falling
    /// back, for one past its tab's last row, to a title-qualified A1
    /// range (which grows the grid), one run at a time.
    fn send(&self, spreadsheet_id: &str, runs: &[Run]) -> anyhow::Result<()> {
        let data: Vec<Value> =
            runs.iter().map(|r| json!({"dataFilter": {"gridRange": r.grid.to_json()}, "majorDimension": "ROWS", "values": r.rows})).collect();
        let result = self.execute(
            "POST",
            &format!("v4/spreadsheets/{spreadsheet_id}/values:batchUpdateByDataFilter"),
            &[],
            Some(&json!({"valueInputOption": "RAW", "data": data})),
        );
        match result {
            Err(error) if status_of(&error) == Some(400) => {}
            Err(error) => return Err(error),
            Ok(response) => {
                let wanted: usize = runs.iter().flat_map(|r| r.rows.iter().map(Vec::len)).sum();
                if response.get("totalUpdatedCells").and_then(Value::as_i64).unwrap_or(0) >= wanted as i64 {
                    return Ok(());
                }
            }
        }
        if runs.len() > 1 {
            for run in runs {
                self.send(spreadsheet_id, std::slice::from_ref(run))?;
            }
            return Ok(());
        }
        let run = &runs[0];
        let title = self.get_sheet_title(spreadsheet_id, run.grid.sheet_id)?;
        let range = format!(
            "{}:{}{}",
            kept::a1(run.grid.start_row as usize, run.grid.start_column as usize),
            kept::column_letters(run.grid.end_column as usize - 1),
            run.grid.end_row.unwrap_or(run.grid.start_row + run.rows.len() as i64)
        );
        let qualified = format!("'{}'!{range}", title.replace('\'', "''"));
        self.put_rows(spreadsheet_id, &qualified, &run.rows)
    }
}

/// What a refresh found in one spreadsheet: tabs whose cells weren't as
/// kept, and tabs deleted -- each put back unless `deferred`.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Refreshed {
    pub changed: Vec<ChangedTab>,
    pub deleted: Vec<DeletedTab>,
    /// A tool was writing, so nothing was put back: the next refresh will.
    pub deferred: bool,
}

impl Refreshed {
    pub fn is_empty(&self) -> bool {
        self.changed.is_empty() && self.deleted.is_empty()
    }
}

/// A tab with cells changed under the server: each cell, as found and as
/// kept, and whether the kept ones were written back.
#[derive(Debug, Clone, PartialEq)]
pub struct ChangedTab {
    pub sheet_id: i64,
    pub title: Option<String>,
    pub cells: Vec<Differs>,
    pub restored: bool,
}

/// A tab deleted under the server, and whether it was made again (with
/// its id, title, tag and kept cells).
#[derive(Debug, Clone, PartialEq)]
pub struct DeletedTab {
    pub sheet_id: i64,
    pub title: Option<String>,
    pub restored: bool,
}

/// A kept tab as a refresh began: how far along its writes were, and its
/// cells.
struct Seen {
    sheet_id: i64,
    generation: u64,
    rows: Rows,
    identity: Option<TabIdentity>,
}

impl SheetsClient {
    /// The spreadsheets anything is kept of.
    pub fn kept_spreadsheets(&self) -> Vec<String> {
        kept::spreadsheets(&self.root())
    }

    /// Read every kept tab of `spreadsheet_id` (but those tagged with one
    /// of `skip`) in one request, without the write lock, and compare each
    /// with what's kept; then put back whatever was changed or deleted by
    /// hand -- holding the write lock, if it's free (with `wait`, waiting
    /// for it). A tab written while it was read is left for the next
    /// refresh: what was read of it may be older than the write.
    pub fn refresh(&self, spreadsheet_id: &str, skip: &[&str], wait: bool) -> anyhow::Result<Refreshed> {
        let seen: Vec<Seen> = self.kept(spreadsheet_id, |tabs| {
            tabs.iter()
                .filter(|(_, t)| !t.identity.as_ref().is_some_and(|i| skip.contains(&i.value.as_str())))
                .filter_map(|(id, t)| Some(Seen { sheet_id: *id, generation: t.generation, rows: t.rows.clone()?, identity: t.identity.clone() }))
                .collect()
        });
        let mut report = Refreshed::default();
        if seen.is_empty() {
            return Ok(report);
        }
        let ranges = |ids: &[i64]| -> Vec<TabRange> { ids.iter().map(|id| TabRange::new(spreadsheet_id, *id, KEPT_RANGE)).collect() };
        let ids: Vec<i64> = seen.iter().map(|s| s.sheet_id).collect();
        let (found, gone): (Vec<(i64, Rows)>, Vec<i64>) = match self.fetch(spreadsheet_id, &ranges(&ids)) {
            Ok(fetched) => (ids.iter().copied().zip(fetched.into_iter().map(|(_, rows)| rows)).collect(), Vec::new()),
            Err(error) if status_of(&error) == Some(400) => {
                let present = self.sheet_ids(spreadsheet_id)?;
                let (here, gone): (Vec<i64>, Vec<i64>) = ids.iter().partition(|id| present.contains(id));
                let fetched = if here.is_empty() { Vec::new() } else { self.fetch(spreadsheet_id, &ranges(&here))? };
                (here.iter().copied().zip(fetched.into_iter().map(|(_, rows)| rows)).collect(), gone)
            }
            Err(error) => return Err(error),
        };
        let unwritten = |s: &Seen| self.kept(spreadsheet_id, |tabs| tabs.get(&s.sheet_id).is_some_and(|t| t.generation == s.generation && t.rows.is_some()));
        let seen_of = |id: i64| seen.iter().find(|s| s.sheet_id == id).expect("every tab read was seen");
        let title_of = |s: &Seen| s.identity.as_ref().map(|i| i.title.clone());
        for (id, rows) in &found {
            let s = seen_of(*id);
            let cells = kept::differences(rows, &s.rows);
            if !cells.is_empty() && unwritten(s) {
                report.changed.push(ChangedTab { sheet_id: *id, title: title_of(s), cells, restored: false });
            }
        }
        for id in &gone {
            report.deleted.push(DeletedTab { sheet_id: *id, title: title_of(seen_of(*id)), restored: false });
        }
        if report.is_empty() {
            return Ok(report);
        }
        let held = if wait { Some(WRITE_LOCK.hold()) } else { WRITE_LOCK.try_hold() };
        let Some(_held) = held else {
            report.deferred = true;
            return Ok(report);
        };
        for changed in &mut report.changed {
            let s = seen_of(changed.sheet_id);
            if !unwritten(s) {
                continue;
            }
            let found_rows = &found.iter().find(|(id, _)| *id == changed.sheet_id).expect("a changed tab was read").1;
            self.send(spreadsheet_id, &restoring(changed.sheet_id, &changed.cells, &s.rows, found_rows))?;
            changed.restored = true;
        }
        for deleted in &mut report.deleted {
            let s = seen_of(deleted.sheet_id);
            match &s.identity {
                Some(identity) if unwritten(s) => {
                    self.recreate(spreadsheet_id, s.sheet_id, identity, &s.rows)?;
                    deleted.restored = true;
                }
                _ => self.forget_tab(spreadsheet_id, s.sheet_id),
            }
        }
        Ok(report)
    }

    /// Every tab's id.
    fn sheet_ids(&self, spreadsheet_id: &str) -> anyhow::Result<Vec<i64>> {
        let response =
            self.execute("GET", &format!("v4/spreadsheets/{spreadsheet_id}"), &[("fields", "sheets.properties".into())], None)?;
        Ok(response.get("sheets").and_then(Value::as_array).into_iter().flatten().filter_map(|s| s["properties"]["sheetId"].as_i64()).collect())
    }

    /// Make tab `sheet_id` again, deleted by hand: the same id, title
    /// (" (restored)" after it if another tab has taken it), color and
    /// tag, and `rows`.
    fn recreate(&self, spreadsheet_id: &str, sheet_id: i64, identity: &TabIdentity, rows: &Rows) -> anyhow::Result<()> {
        let add = |title: &str| {
            let mut properties = json!({"sheetId": sheet_id, "title": title});
            if let Some(color) = &identity.color {
                properties["tabColor"] = color.clone();
            }
            self.batch_update(spreadsheet_id, json!({"requests": [{"addSheet": {"properties": properties}}]}), &[])
        };
        match add(&identity.title) {
            Err(error) if status_of(&error) == Some(400) => {
                add(&format!("{} (restored)", identity.title))?;
            }
            other => {
                other?;
            }
        }
        self.create_sheet_metadata(spreadsheet_id, sheet_id, &identity.key, &identity.value)?;
        if !rows.is_empty() {
            let grid = GridRange { sheet_id, start_row: 0, start_column: 0, end_column: kept::KEPT_COLUMNS as i64, end_row: Some(rows.len() as i64) };
            self.send(spreadsheet_id, &[Run { grid, rows: rows.clone() }])?;
        }
        Ok(())
    }
}

/// The writes that put `cells` of tab `sheet_id` back as kept: each row
/// with a changed cell, as kept, cleared out to as wide as it was found;
/// a run of such rows to a write.
fn restoring(sheet_id: i64, cells: &[Differs], kept_rows: &Rows, found: &Rows) -> Vec<Run> {
    let mut rows: Vec<usize> = cells.iter().map(|d| d.row).collect();
    rows.sort();
    rows.dedup();
    let row_as_kept = |r: usize| -> Vec<String> {
        let width = found.get(r).map_or(0, Vec::len).max(kept_rows.get(r).map_or(0, Vec::len));
        (0..width).map(|c| kept::cell(kept_rows, r, c).to_string()).collect()
    };
    let mut runs: Vec<Run> = Vec::new();
    for r in rows {
        match runs.last_mut() {
            Some(run) if run.grid.end_row == Some(r as i64) => {
                run.rows.push(row_as_kept(r));
                run.grid.end_row = Some(r as i64 + 1);
            }
            _ => runs.push(Run {
                grid: GridRange { sheet_id, start_row: r as i64, start_column: 0, end_column: kept::KEPT_COLUMNS as i64, end_row: Some(r as i64 + 1) },
                rows: vec![row_as_kept(r)],
            }),
        }
    }
    runs
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

#[cfg(test)]
#[path = "sheets_tests.rs"]
mod tests;
