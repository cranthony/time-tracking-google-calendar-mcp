//! The time notes tab: moments worth recording, each a timestamp and an
//! optional description, stamped with the compaction that consumed it. A
//! note's id is its own, if a client chose one when it made it, or else
//! its timestamp and row. Each note has a version, which goes up by one
//! whenever it's edited. A write sent again is made once by the rules
//! every kind the app writes keeps (see rows::Versioned): a note made
//! again under its id is given as it is; an edit or deletion given the
//! version last seen is refused with a `Conflict` if it's changed since
//! -- unless it's an edit one version on and already as it'd leave it, or
//! a deletion of one gone already.

use crate::event::{parse_iso, Moment};
use crate::google::sheets::{Rows, SheetsClient, TabRange};
use crate::py;
use crate::sheet::rows::{self, Conflict, Row};
use crate::sheet::{ensure_tab, MIN_TAB_ROWS, TIME_NOTES};
use anyhow::{anyhow, bail};
use serde_json::{json, Value};

const FIRST_DATA_ROW: i64 = 2;
const MAX_ROWS: i64 = 50;
const TRIM_TO_ROWS: i64 = 25;
pub const FIELDS: [&str; 3] = ["timestamp", "description", "compaction_id"];
/// The columns added once they're needed: a note's own id and its
/// version.
const ID: &str = "id";
const VERSION: &str = "version";
const LAST_COLUMN: &str = "E";
const HEADER_RANGE: &str = "A1:E1";
const DATA_RANGE: &str = "A2:E";

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
    /// The id a client chose for it, if any.
    pub own_id: Option<String>,
    pub version: Option<i64>,
}

impl SheetNote {
    pub fn id(&self) -> String {
        match &self.own_id {
            Some(own) => own.clone(),
            None => format!("{}#{}", py::isoformat(&self.note.timestamp), self.row),
        }
    }

    /// NoteWithId's JSON: its id, then the note's fields and its version.
    pub fn with_id(&self) -> Value {
        json!({
            "id": self.id(),
            "timestamp": py::pydantic_datetime(&self.note.timestamp),
            "description": self.note.description,
            "compaction_id": self.note.compaction_id,
            "version": self.version,
        })
    }

    fn as_row(&self) -> Row {
        match self.with_id() {
            Value::Object(row) => row,
            _ => unreachable!("with_id is an object"),
        }
    }
}

/// A cell of `data`, by its column's name in `header`, trimmed.
fn cell<'a>(header: &[String], data: &'a [String], name: &str) -> Option<&'a str> {
    header.iter().position(|h| h == name).and_then(|i| data.get(i)).map(|c| c.trim()).filter(|c| !c.is_empty())
}

/// `cells` (a row under `header`), with the `name` cell set to `value`.
fn set_cell(header: &[String], cells: &mut [String], name: &str, value: String) {
    if let Some(i) = header.iter().position(|h| h == name) {
        cells[i] = value;
    }
}

/// The column letter of the `index`th column.
fn column(index: usize) -> char {
    (b'A' + index as u8) as char
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
    rows: Rows,
}

impl Read {
    /// The note `note_id` names, if it's still there: the one with that
    /// id of its own, or else the one at the timestamp and row its id
    /// gives -- or why there's none.
    fn find(&self, note_id: &str) -> Result<&SheetNote, anyhow::Error> {
        if let Some(found) = self.notes.iter().find(|n| n.own_id.as_deref() == Some(note_id)) {
            return Ok(found);
        }
        if !note_id.contains('#') && rows::chosen_id_shape(note_id) {
            bail!("there's no note {} -- it was deleted since it was read; list the notes again for current ids", py::repr(note_id));
        }
        let (timestamp, row) = parse_note_id(note_id)?;
        match self.notes.iter().rev().find(|n| n.row == row).filter(|n| n.note.timestamp == timestamp) {
            Some(found) => Ok(found),
            None => bail!(
                "row {row} no longer holds the note {} -- it was edited or removed since it was read; list the notes again for current ids",
                py::repr(note_id)
            ),
        }
    }

    /// The ids of the notes that have their own.
    fn own_ids(&self) -> Vec<Option<&str>> {
        self.notes.iter().map(|n| n.own_id.as_deref()).collect()
    }
}

fn parse(header: Vec<String>, rows: Rows) -> anyhow::Result<Read> {
    let mut notes = Vec::new();
    for (offset, row) in rows.iter().enumerate() {
        if row.iter().any(|c| !c.trim().is_empty()) {
            notes.push(SheetNote {
                row: FIRST_DATA_ROW + offset as i64,
                note: NotedTime::from_row(&header, row)?,
                own_id: cell(&header, row, ID).map(str::to_string),
                version: cell(&header, row, VERSION).and_then(crate::event::python_int),
            });
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
    Ok(Read { notes, latest_compacted: latest, header, row_count: rows.len() as i64, rows })
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

    /// Add `note` as a new row at the end, garbage-collecting first, at
    /// version 1 -- with `own_id`, the id the client chose for it, if it
    /// gave one. One there already with that id and as `note` is, is the
    /// same note made again: it's given as it is.
    pub fn append(&self, note: &NotedTime, own_id: Option<&str>) -> anyhow::Result<SheetNote> {
        let read = self.read_all()?;
        let own_id = own_id.map(str::trim).filter(|given| !given.is_empty());
        if let Some(made) = own_id.and_then(|id| read.notes.iter().find(|n| n.own_id.as_deref() == Some(id))) {
            if made.note.timestamp == note.timestamp && made.note.description == note.description {
                return Ok(made.clone());
            }
        }
        let own_id = own_id.map(|given| rows::chosen_id(Some(given), read.own_ids())).transpose()?;
        let next_row = FIRST_DATA_ROW + read.row_count - self.garbage_collect_from(&read)?;
        let header = self.with_columns(&read.header, own_id.is_some())?;
        let new = SheetNote { row: next_row, note: note.clone(), own_id, version: Some(1) };
        self.write_note(&header, &new, None)?;
        Ok(new)
    }

    /// Edit the note `note_id` -- unless it's changed since `if_version`,
    /// if given (see the module's doc).
    pub fn edit(&self, note_id: &str, timestamp: Option<Moment>, description: Option<&str>, if_version: Option<i64>) -> anyhow::Result<SheetNote> {
        let Some((found, header, original)) = self.uncompacted_row(note_id, if_version.is_some())? else {
            return Err(Conflict { kind: "note", id: note_id.into(), expected: if_version.unwrap_or_default().into(), current: None }.into());
        };
        let note = &found.note;
        let edited = NotedTime {
            timestamp: timestamp.unwrap_or(note.timestamp),
            description: match description {
                None => note.description.clone(),
                Some(d) if d.trim().is_empty() => None,
                Some(d) => Some(d.to_string()),
            },
            compaction_id: note.compaction_id.clone(),
        };
        if let Some(expected) = if_version.filter(|v| found.version != Some(*v)) {
            // One on, and already so: this edit, sent again.
            if found.version == Some(expected + 1) && edited == *note {
                return Ok(found);
            }
            return Err(Conflict { kind: "note", id: note_id.into(), expected: expected.into(), current: Some(found.as_row()) }.into());
        }
        let version = if edited != *note { Some(found.version.unwrap_or(0) + 1) } else { found.version };
        let header = self.with_columns(&header, false)?;
        let edited = SheetNote { note: edited, version, ..found };
        self.write_note(&header, &edited, Some(&original))?;
        Ok(edited)
    }

    /// Delete the note `note_id` -- unless it's changed since
    /// `if_version`, if given. Returns it as it was; None if, given a
    /// version, it was gone already.
    pub fn delete(&self, note_id: &str, if_version: Option<i64>) -> anyhow::Result<Option<SheetNote>> {
        let Some((found, header, _)) = self.uncompacted_row(note_id, if_version.is_some())? else { return Ok(None) };
        if let Some(expected) = if_version.filter(|v| found.version != Some(*v)) {
            return Err(Conflict { kind: "note", id: note_id.into(), expected: expected.into(), current: Some(found.as_row()) }.into());
        }
        let row = found.row;
        self.sheets.write_rows_in_sheet(
            &self.spreadsheet_id,
            self.sheet_id,
            &format!("A{row}:{}{row}", column(header.len().max(1) - 1)),
            &vec![vec![String::new(); header.len()]],
        )?;
        Ok(Some(found))
    }

    /// The uncompacted note `note_id`, the header, and its row's cells --
    /// refused if it's not there, or compacted; but None if it's not there
    /// and the client gave the version it saw (`versioned`), so it's
    /// changed -- or gone -- since.
    fn uncompacted_row(&self, note_id: &str, versioned: bool) -> anyhow::Result<Option<(SheetNote, Vec<String>, Vec<String>)>> {
        let read = self.read_all()?;
        let found = match read.find(note_id) {
            Ok(found) => found.clone(),
            Err(_) if versioned && (note_id.contains('#') || rows::chosen_id_shape(note_id)) => return Ok(None),
            Err(why) => return Err(why),
        };
        if let Some(compaction) = &found.note.compaction_id {
            bail!(
                "note {} was already compacted (by {}), so it can't be changed -- change the calendar event it became instead",
                py::repr(note_id),
                py::repr(compaction)
            );
        }
        let offset = (found.row - FIRST_DATA_ROW) as usize;
        let cells = read.rows.get(offset).cloned().unwrap_or_default();
        Ok(Some((found, read.header, cells)))
    }

    /// `header`, with a version column -- and an id column, if `own_id`
    /// -- added at the end if it hasn't one yet.
    fn with_columns(&self, header: &[String], own_id: bool) -> anyhow::Result<Vec<String>> {
        let mut wanted = vec![VERSION];
        if own_id {
            wanted.insert(0, ID);
        }
        let mut grown = header.to_vec();
        for name in wanted {
            if !grown.iter().any(|h| h == name) {
                grown.push(name.into());
            }
        }
        if grown.len() > header.len() {
            let last = column(grown.len() - 1);
            self.sheets.write_rows_in_sheet(&self.spreadsheet_id, self.sheet_id, &format!("A1:{last}1"), &vec![grown.clone()])?;
        }
        Ok(grown)
    }

    /// Write `note` to its row, under `header`, keeping the cells of
    /// columns it doesn't have from `original`.
    fn write_note(&self, header: &[String], note: &SheetNote, original: Option<&Vec<String>>) -> anyhow::Result<()> {
        let mut cells = note.note.to_row(header, original);
        if let Some(own) = &note.own_id {
            set_cell(header, &mut cells, ID, own.clone());
        }
        set_cell(header, &mut cells, VERSION, note.version.map(|v| v.to_string()).unwrap_or_default());
        let row = note.row;
        self.sheets.write_rows_in_sheet(
            &self.spreadsheet_id,
            self.sheet_id,
            &format!("A{row}:{}{row}", column(header.len().max(1) - 1)),
            &vec![cells],
        )
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
        let read = self.read_all()?;
        let mut wanted: Vec<i64> = Vec::new();
        for note_id in note_ids {
            let found = read.find(note_id)?;
            if let Some(other) = found.note.compaction_id.as_deref().filter(|c| *c != compaction_id) {
                bail!("note {} was already compacted by {}", py::repr(note_id), py::repr(other));
            }
            wanted.push(found.row);
        }
        let index = read.header.iter().position(|h| h == "compaction_id").ok_or_else(|| anyhow!("'compaction_id' is not in list"))?;
        let column = column(index);
        let mut rows: Vec<i64> = wanted;
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
