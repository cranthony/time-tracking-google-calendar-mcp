//! What the server keeps of the tabs it uses: each tab's cells in
//! `KEPT_RANGE`, as the API reads them back, held for the process's life
//! and changed by the server's own writes as it makes them. The
//! spreadsheet is the server's alone (its title says so), so what's kept
//! is what's there, and reads come from here rather than from Sheets; a
//! refresh (`SheetsClient::refresh`) compares the two now and then and
//! puts back whatever was changed under it.
//!
//! Kept per Sheets root (a fake's address, in tests) and spreadsheet, so
//! every client of the same spreadsheet shares one copy.

use super::sheets::Rows;
use serde_json::Value;
use std::collections::{BTreeMap, HashMap};
use std::sync::{LazyLock, Mutex};

/// The cells kept of every tab: every range the server reads or writes is
/// within them.
pub const KEPT_RANGE: &str = "A1:Z";
pub const KEPT_COLUMNS: usize = 26;

#[derive(Clone, Debug, Default)]
pub struct Tab {
    /// The tab's cells, trimmed as the API trims them; `None` until read,
    /// or once forgotten.
    pub rows: Option<Rows>,
    /// Goes up as each write to the tab begins and again once it's kept,
    /// whether the tab is kept or not: what was read while a write was
    /// under way may be stale, so it isn't kept, nor taken for a hand edit.
    pub generation: u64,
    /// What the tab is, to make it again if it's deleted by hand.
    pub identity: Option<TabIdentity>,
}

/// A tab's title, color and tag (the developer metadata it's found by).
#[derive(Clone, Debug, PartialEq)]
pub struct TabIdentity {
    pub title: String,
    pub color: Option<Value>,
    pub key: String,
    pub value: String,
}

type Tabs = BTreeMap<i64, Tab>;

static KEPT: LazyLock<Mutex<HashMap<(String, String), Tabs>>> = LazyLock::new(|| Mutex::new(HashMap::new()));

/// Run `f` on spreadsheet `spreadsheet_id`'s tabs, as kept for Sheets at
/// `root`.
pub fn with<T>(root: &str, spreadsheet_id: &str, f: impl FnOnce(&mut Tabs) -> T) -> T {
    let mut kept = KEPT.lock().unwrap_or_else(|e| e.into_inner());
    f(kept.entry((root.to_string(), spreadsheet_id.to_string())).or_default())
}

/// About how many bytes `rows` take in memory: each cell's text, and a
/// String's and a Vec's own size for each cell and row.
pub fn estimated_bytes(rows: &Rows) -> usize {
    const HEADER: usize = std::mem::size_of::<String>();
    rows.iter().map(|row| HEADER + row.iter().map(|cell| HEADER + cell.len()).sum::<usize>()).sum()
}

/// Each kept tab of every spreadsheet, for Sheets at `root`: its title
/// (or "tab <id>", for one not identified) and about how many bytes it
/// takes (see `estimated_bytes`).
pub fn sizes(root: &str) -> Vec<(String, usize)> {
    let kept = KEPT.lock().unwrap_or_else(|e| e.into_inner());
    let mut sizes = Vec::new();
    for ((r, _), tabs) in kept.iter() {
        if r != root {
            continue;
        }
        for (id, tab) in tabs {
            if let Some(rows) = &tab.rows {
                let title = tab.identity.as_ref().map_or_else(|| format!("tab {id}"), |i| i.title.clone());
                sizes.push((title, estimated_bytes(rows)));
            }
        }
    }
    sizes.sort();
    sizes
}

/// The spreadsheets anything is kept of, for Sheets at `root`.
pub fn spreadsheets(root: &str) -> Vec<String> {
    let kept = KEPT.lock().unwrap_or_else(|e| e.into_inner());
    let mut ids: Vec<String> =
        kept.iter().filter(|((r, _), tabs)| r == root && tabs.values().any(|t| t.rows.is_some())).map(|((_, id), _)| id.clone()).collect();
    ids.sort();
    ids
}

/// Forget every tab of `spreadsheet_id`, so each is read afresh -- as
/// when the server starts. Tests call this after editing a fake's
/// spreadsheet behind the server's back.
pub fn forget_spreadsheet(root: &str, spreadsheet_id: &str) {
    with(root, spreadsheet_id, |tabs| {
        for tab in tabs.values_mut() {
            tab.rows = None;
            tab.generation += 1;
        }
    });
}

/// Cell (`row`, `column`) of `rows`, 0-based; empty past the end.
pub fn cell(rows: &Rows, row: usize, column: usize) -> &str {
    rows.get(row).and_then(|r| r.get(column)).map(String::as_str).unwrap_or("")
}

/// Write `values` into `rows` from (`start_row`, `start_column`), as
/// Sheets does: each cell given replaces what's there, an empty one
/// clears it, and cells not given are left alone.
pub fn set_cells(rows: &mut Rows, start_row: usize, start_column: usize, values: &Rows) {
    for (i, row) in values.iter().enumerate() {
        let r = start_row + i;
        for (j, text) in row.iter().enumerate() {
            let c = start_column + j;
            if c >= KEPT_COLUMNS {
                break;
            }
            if text.is_empty() && cell(rows, r, c).is_empty() {
                continue;
            }
            while rows.len() <= r {
                rows.push(Vec::new());
            }
            let cells = &mut rows[r];
            while cells.len() <= c {
                cells.push(String::new());
            }
            cells[c] = text.clone();
        }
    }
    trim(rows);
}

/// Delete rows `start`..`end` (0-based, end-exclusive), moving those below
/// up, as deleteDimension does.
pub fn delete_rows(rows: &mut Rows, start: usize, end: usize) {
    if start < rows.len() {
        rows.drain(start..end.min(rows.len()));
    }
    trim(rows);
}

/// Drop each row's trailing empty cells and the trailing empty rows, as
/// the API leaves them out of what it reads back.
pub fn trim(rows: &mut Rows) {
    for row in rows.iter_mut() {
        while row.last().is_some_and(String::is_empty) {
            row.pop();
        }
    }
    while rows.last().is_some_and(Vec::is_empty) {
        rows.pop();
    }
}

/// A cell that isn't as kept: (row, column), 0-based, what's there, and
/// what the server keeps.
#[derive(Clone, Debug, PartialEq)]
pub struct Differs {
    pub row: usize,
    pub column: usize,
    pub found: String,
    pub kept: String,
}

/// Every cell where `found` isn't `kept`.
pub fn differences(found: &Rows, kept: &Rows) -> Vec<Differs> {
    let mut out = Vec::new();
    for r in 0..found.len().max(kept.len()) {
        let width = found.get(r).map_or(0, Vec::len).max(kept.get(r).map_or(0, Vec::len));
        for c in 0..width {
            let (f, k) = (cell(found, r, c), cell(kept, r, c));
            if f != k {
                out.push(Differs { row: r, column: c, found: f.to_string(), kept: k.to_string() });
            }
        }
    }
    out
}

/// A cell's A1 name: (0, 0) is "A1".
pub fn a1(row: usize, column: usize) -> String {
    format!("{}{}", column_letters(column), row + 1)
}

pub fn column_letters(column: usize) -> String {
    let mut n = column + 1;
    let mut letters = Vec::new();
    while n > 0 {
        let rem = (n - 1) % 26;
        letters.push((b'A' + rem as u8) as char);
        n = (n - 1) / 26;
    }
    letters.iter().rev().collect()
}

#[cfg(test)]
#[path = "kept_tests.rs"]
mod tests;
