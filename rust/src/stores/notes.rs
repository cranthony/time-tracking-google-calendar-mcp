//! The time notes tab (utilities/noted_time_sheet.py): moments worth
//! recording, each a timestamp and an optional description, stamped with
//! the compaction that consumed it. A note's id is its timestamp and row.

use crate::event::{parse_iso, Moment};
use crate::google::sheets::{Rows, SheetsClient, TabRange};
use crate::py;
use crate::sheet::{ensure_tab, MIN_TAB_ROWS, TIME_NOTES};
use anyhow::{anyhow, bail};
use serde_json::{json, Value};

const FIRST_DATA_ROW: i64 = 2;
const MAX_ROWS: i64 = 50;
const TRIM_TO_ROWS: i64 = 25;
pub const FIELDS: [&str; 3] = ["timestamp", "description", "compaction_id"];
const LAST_COLUMN: &str = "C";
const HEADER_RANGE: &str = "A1:C1";
const DATA_RANGE: &str = "A2:C";

#[derive(Debug, Clone, PartialEq)]
pub struct NotedTime {
    pub timestamp: Moment,
    pub description: Option<String>,
    pub compaction_id: Option<String>,
}

impl NotedTime {
    pub fn from_row(header: &[String], data: &[String]) -> anyhow::Result<NotedTime> {
        let cell = |name: &str| -> String {
            header.iter().position(|h| h == name).and_then(|i| data.get(i)).cloned().unwrap_or_default()
        };
        let timestamp = cell("timestamp");
        if timestamp.is_empty() {
            bail!("Row is missing a timestamp: {}", py::repr_list(data));
        }
        Ok(NotedTime {
            timestamp: parse_iso(&timestamp, None)?,
            description: Some(cell("description")).filter(|d| !d.is_empty()),
            compaction_id: Some(cell("compaction_id")).filter(|c| !c.is_empty()),
        })
    }

    pub fn to_row(&self, header: &[String], original: Option<&Vec<String>>) -> Vec<String> {
        header
            .iter()
            .enumerate()
            .map(|(i, name)| match name.as_str() {
                "timestamp" => py::isoformat(&self.timestamp),
                "description" => self.description.clone().unwrap_or_default(),
                "compaction_id" => self.compaction_id.clone().unwrap_or_default(),
                _ => original.and_then(|o| o.get(i)).cloned().unwrap_or_default(),
            })
            .collect()
    }

    /// As a client sees it (NotedTime's JSON).
    pub fn to_json(&self) -> Value {
        json!({
            "timestamp": py::pydantic_datetime(&self.timestamp),
            "description": self.description,
            "compaction_id": self.compaction_id,
        })
    }
}

#[derive(Debug, Clone, PartialEq)]
pub struct SheetNote {
    pub row: i64,
    pub note: NotedTime,
}

impl SheetNote {
    pub fn id(&self) -> String {
        format!("{}#{}", py::isoformat(&self.note.timestamp), self.row)
    }

    /// NoteWithId's JSON: its id, then the note's fields.
    pub fn with_id(&self) -> Value {
        json!({
            "id": self.id(),
            "timestamp": py::pydantic_datetime(&self.note.timestamp),
            "description": self.note.description,
            "compaction_id": self.note.compaction_id,
        })
    }
}

/// The (timestamp, row) a note id encodes.
pub fn parse_note_id(note_id: &str) -> anyhow::Result<(Moment, i64)> {
    if let Some((timestamp, row)) = note_id.rsplit_once('#') {
        if !row.is_empty() && row.chars().all(|c| c.is_ascii_digit()) {
            if let Ok(moment) = parse_iso(timestamp, None) {
                return Ok((moment, row.parse()?));
            }
        }
    }
    bail!("{} isn't a note id (expected e.g. '2026-01-01T09:05:00+00:00#5')", py::repr(note_id))
}

pub struct Read {
    pub notes: Vec<SheetNote>,
    pub latest_compacted: Option<SheetNote>,
    pub header: Vec<String>,
    pub row_count: i64,
}

fn parse(header: Vec<String>, rows: Rows) -> anyhow::Result<Read> {
    let mut notes = Vec::new();
    for (offset, row) in rows.iter().enumerate() {
        if row.iter().any(|c| !c.trim().is_empty()) {
            notes.push(SheetNote { row: FIRST_DATA_ROW + offset as i64, note: NotedTime::from_row(&header, row)? });
        }
    }
    let latest = notes
        .iter()
        .filter(|n| n.note.compaction_id.is_some())
        .fold(None::<&SheetNote>, |best, n| match best {
            Some(b) if (b.note.timestamp, b.row) >= (n.note.timestamp, n.row) => Some(b),
            _ => Some(n),
        })
        .cloned();
    Ok(Read { notes, latest_compacted: latest, header, row_count: rows.len() as i64 })
}

#[derive(Clone)]
pub struct NotedTimeSheet {
    sheets: SheetsClient,
    spreadsheet_id: String,
    sheet_id: i64,
}

impl NotedTimeSheet {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<NotedTimeSheet> {
        let (sheet_id, created) = ensure_tab(sheets, spreadsheet_id, TIME_NOTES.0, TIME_NOTES.1)?;
        if created {
            let header: Vec<String> = FIELDS.iter().map(|s| s.to_string()).collect();
            sheets.write_rows_in_sheet(spreadsheet_id, sheet_id, HEADER_RANGE, &vec![header])?;
        }
        Ok(NotedTimeSheet { sheets: sheets.clone(), spreadsheet_id: spreadsheet_id.into(), sheet_id })
    }

    pub fn spreadsheet_id(&self) -> &str {
        &self.spreadsheet_id
    }

    pub fn whole_tab(&self) -> TabRange {
        TabRange::new(&self.spreadsheet_id, self.sheet_id, &format!("A1:{LAST_COLUMN}"))
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheets.prefetch(ranges)
    }

    pub fn read_with_rows(&self, include_compacted: bool) -> anyhow::Result<Vec<SheetNote>> {
        let notes = self.read_all()?.notes;
        Ok(if include_compacted { notes } else { notes.into_iter().filter(|n| n.note.compaction_id.is_none()).collect() })
    }

    pub fn read_with_latest_compacted(&self) -> anyhow::Result<(Vec<SheetNote>, Option<NotedTime>)> {
        let read = self.read_all()?;
        let uncompacted = read.notes.iter().filter(|n| n.note.compaction_id.is_none()).cloned().collect();
        Ok((uncompacted, read.latest_compacted.map(|n| n.note)))
    }

    pub fn read_all(&self) -> anyhow::Result<Read> {
        let (header, rows) = self.read_header_and(DATA_RANGE)?;
        parse(header, rows)
    }

    /// Every note, sorted by timestamp (stably).
    pub fn read(&self, include_compacted: bool) -> anyhow::Result<Vec<NotedTime>> {
        let mut notes: Vec<NotedTime> = self.read_with_rows(include_compacted)?.into_iter().map(|n| n.note).collect();
        notes.sort_by(|a, b| a.timestamp.cmp(&b.timestamp));
        Ok(notes)
    }

    /// Add `note` as a new row at the end, garbage-collecting first.
    pub fn append(&self, note: &NotedTime) -> anyhow::Result<SheetNote> {
        let read = self.read_all()?;
        let next_row = FIRST_DATA_ROW + read.row_count - self.garbage_collect_from(&read)?;
        self.sheets.write_rows_in_sheet(
            &self.spreadsheet_id,
            self.sheet_id,
            &format!("A{next_row}:{LAST_COLUMN}"),
            &vec![note.to_row(&read.header, None)],
        )?;
        Ok(SheetNote { row: next_row, note: note.clone() })
    }

    pub fn edit(&self, note_id: &str, timestamp: Option<Moment>, description: Option<&str>) -> anyhow::Result<SheetNote> {
        let (row, note, header, original) = self.uncompacted_row(note_id)?;
        let edited = NotedTime {
            timestamp: timestamp.unwrap_or(note.timestamp),
            description: match description {
                None => note.description.clone(),
                Some(d) if d.trim().is_empty() => None,
                Some(d) => Some(d.to_string()),
            },
            compaction_id: note.compaction_id.clone(),
        };
        self.sheets.write_rows_in_sheet(
            &self.spreadsheet_id,
            self.sheet_id,
            &format!("A{row}:{LAST_COLUMN}{row}"),
            &vec![edited.to_row(&header, Some(&original))],
        )?;
        Ok(SheetNote { row, note: edited })
    }

    pub fn delete(&self, note_id: &str) -> anyhow::Result<NotedTime> {
        let (row, note, header, _) = self.uncompacted_row(note_id)?;
        self.sheets.write_rows_in_sheet(
            &self.spreadsheet_id,
            self.sheet_id,
            &format!("A{row}:{LAST_COLUMN}{row}"),
            &vec![vec![String::new(); header.len()]],
        )?;
        Ok(note)
    }

    fn uncompacted_row(&self, note_id: &str) -> anyhow::Result<(i64, NotedTime, Vec<String>, Vec<String>)> {
        let (timestamp, row) = parse_note_id(note_id)?;
        let (header, rows) = self.read_header_and(DATA_RANGE)?;
        let offset = row - FIRST_DATA_ROW;
        let cells: Vec<String> = if offset >= 0 && (offset as usize) < rows.len() { rows[offset as usize].clone() } else { Vec::new() };
        let note = if cells.iter().any(|c| !c.trim().is_empty()) { Some(NotedTime::from_row(&header, &cells)?) } else { None };
        let Some(note) = note.filter(|n| n.timestamp == timestamp) else {
            bail!(
                "row {row} no longer holds the note {} -- it was edited or removed since it was read; list the notes again for current ids",
                py::repr(note_id)
            );
        };
        if let Some(compaction) = &note.compaction_id {
            bail!(
                "note {} was already compacted (by {}), so it can't be changed -- change the calendar event it became instead",
                py::repr(note_id),
                py::repr(compaction)
            );
        }
        Ok((row, note, header, cells))
    }

    pub fn garbage_collect(&self) -> anyhow::Result<()> {
        let read = self.read_all()?;
        self.garbage_collect_from(&read)?;
        Ok(())
    }

    fn garbage_collect_from(&self, read: &Read) -> anyhow::Result<i64> {
        if read.row_count <= MAX_ROWS {
            return Ok(0);
        }
        let excess = read.row_count - TRIM_TO_ROWS;
        let keep = read.latest_compacted.as_ref().map(|n| n.row);
        let mut deletable = 0;
        for row in FIRST_DATA_ROW..FIRST_DATA_ROW + excess {
            let note = read.notes.iter().rev().find(|n| n.row == row).map(|n| &n.note);
            if Some(row) == keep || note.is_some_and(|n| n.compaction_id.is_none()) {
                break;
            }
            deletable += 1;
        }
        if deletable == 0 {
            return Ok(0);
        }
        self.sheets.delete_rows(&self.spreadsheet_id, self.sheet_id, FIRST_DATA_ROW, FIRST_DATA_ROW + deletable - 1, MIN_TAB_ROWS)?;
        Ok(deletable)
    }

    /// Stamp `compaction_id` onto the notes `note_ids`, refusing (writing
    /// nothing) if any no longer holds its note or another compaction's.
    pub fn mark_compacted(&self, note_ids: &[String], compaction_id: &str) -> anyhow::Result<()> {
        if note_ids.is_empty() {
            return Ok(());
        }
        let wanted: Vec<(Moment, i64)> = note_ids.iter().map(|i| parse_note_id(i)).collect::<anyhow::Result<_>>()?;
        let read = self.read_all()?;
        for ((timestamp, row), note_id) in wanted.iter().zip(note_ids) {
            let note = read.notes.iter().rev().find(|n| n.row == *row).map(|n| &n.note);
            let Some(note) = note.filter(|n| n.timestamp == *timestamp) else {
                bail!("row {row} no longer holds the note {} -- it was edited or removed since it was read", py::repr(note_id));
            };
            if let Some(other) = note.compaction_id.as_deref().filter(|c| *c != compaction_id) {
                bail!("note {} was already compacted by {}", py::repr(note_id), py::repr(other));
            }
        }
        let index = read.header.iter().position(|h| h == "compaction_id").ok_or_else(|| anyhow!("'compaction_id' is not in list"))?;
        let column = (b'A' + index as u8) as char;
        let mut rows: Vec<i64> = wanted.iter().map(|(_, r)| *r).collect();
        rows.sort();
        rows.dedup();
        for (first, last) in contiguous_runs(&rows) {
            let values: Rows = (first..=last).map(|_| vec![compaction_id.to_string()]).collect();
            self.sheets.write_rows_in_sheet(&self.spreadsheet_id, self.sheet_id, &format!("{column}{first}:{column}{last}"), &values)?;
        }
        Ok(())
    }

    fn read_header_and(&self, range: &str) -> anyhow::Result<(Vec<String>, Rows)> {
        let mut found = self.sheets.read_ranges_in_sheet(&self.spreadsheet_id, self.sheet_id, &[HEADER_RANGE, range])?;
        let rows = found.pop().unwrap_or_default();
        let header_rows = found.pop().unwrap_or_default();
        let header = header_rows.first().cloned().unwrap_or_default();
        let missing: Vec<&str> = FIELDS.into_iter().filter(|f| !header.iter().any(|h| h == f)).collect();
        if !missing.is_empty() {
            // Python prints a set here; its order isn't fixed either.
            let names: Vec<String> = missing.iter().map(|m| py::repr(m)).collect();
            bail!("Missing expected header columns at {HEADER_RANGE}: {{{}}}", names.join(", "));
        }
        Ok((header, rows))
    }
}

fn contiguous_runs(sorted: &[i64]) -> Vec<(i64, i64)> {
    let mut runs: Vec<(i64, i64)> = Vec::new();
    for row in sorted {
        match runs.last_mut() {
            Some(last) if *row == last.1 + 1 => last.1 = *row,
            _ => runs.push((*row, *row)),
        }
    }
    runs
}

#[cfg(test)]
#[path = "notes_tests.rs"]
mod tests;
