//! A tab holding one item per row: actions, groups, people, circles,
//! locations, habits. Columns are found by header name, so columns added by
//! hand are kept, and a field gets its column the first time a row has a
//! value for it. A cell is its field's text: a number as digits, a list or
//! object as JSON, nothing for null.
//!
//! A row is an ordered JSON object with every field of its kind (null for
//! unset).
//!
//! A kind with a `version` field is versioned: each row's version goes up
//! by one whenever a write changes it (a new row starts at 1), so a client
//! can say which version it last saw. A row written with a version that
//! isn't the one kept -- changed by someone else since it was read -- is
//! refused with a `Conflict`, naming the row as it is now. A cell edited by hand that can't be read as its field's kind (a
//! number that isn't one, JSON that isn't JSON) makes the tab unreadable:
//! reading it fails with a `CellError` naming each such cell, which every
//! tool shows as it is, so the user can fix the cell.

use super::{create_tab, find_tab};
use crate::google::sheets::{Rows, SheetsClient, TabRange, TabWrite};
use crate::py;
use anyhow::bail;
use serde_json::{Map, Value};

const HEADER_RANGE: &str = "A1:Z1";
const DATA_RANGE: &str = "A2:Z";
const WHOLE_RANGE: &str = "A1:Z";
const ID_ALPHABET: &str = "0123456789abcdefghijklmnopqrstuvwxyz";
const ID_LENGTH: usize = 6;

pub type Row = Map<String, Value>;

/// A tab with cells that can't be read as their columns' kinds: each by
/// its row (as the spreadsheet numbers it) and column.
#[derive(Debug, Clone, PartialEq)]
pub struct CellError {
    pub tab: String,
    pub cells: Vec<String>,
}

impl std::fmt::Display for CellError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(
            f,
            "The {} tab has cells that can't be read: {}. Fix them in the spreadsheet, or clear them.",
            self.tab,
            self.cells.join("; ")
        )
    }
}

impl std::error::Error for CellError {}

/// A write refused because the row it changes isn't the version the
/// client last saw: it's changed since, or gone. `current` is the row as
/// it is now (None if it's been deleted).
#[derive(Debug, Clone, PartialEq)]
pub struct Conflict {
    pub kind: &'static str,
    pub id: String,
    /// The version the client last saw.
    pub expected: i64,
    pub current: Option<Row>,
}

impl Conflict {
    pub fn current_version(&self) -> Option<i64> {
        self.current.as_ref().and_then(|r| r.get("version")).and_then(Value::as_i64)
    }
}

impl std::fmt::Display for Conflict {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self.current_version() {
            _ if self.current.is_none() => write!(
                f,
                "{} {} was deleted after version {} was read",
                self.kind,
                py::repr(&self.id),
                self.expected
            ),
            Some(now) => write!(
                f,
                "{} {} changed after version {} was read: it's version {} now",
                self.kind,
                py::repr(&self.id),
                self.expected,
                now
            ),
            None => write!(f, "{} {} changed after version {} was read", self.kind, py::repr(&self.id), self.expected),
        }
    }
}

impl std::error::Error for Conflict {}

thread_local! {
    /// The versions written in the tool call running on this thread, by
    /// id; None outside one. An id two kinds share is noted as `None`:
    /// which is which can't be told.
    static WRITTEN: std::cell::RefCell<Option<Vec<(String, Option<i64>)>>> = const { std::cell::RefCell::new(None) };
}

/// Within this guard's life -- a tool call's -- each versioned row written
/// is noted with its version, so the tool's result can be given with the
/// versions as written (`stamp`), not as they were read.
pub struct Written {
    outer: bool,
}

impl Written {
    pub fn start() -> Written {
        let outer = WRITTEN.with(|w| {
            let mut written = w.borrow_mut();
            if written.is_none() {
                *written = Some(Vec::new());
                true
            } else {
                false
            }
        });
        Written { outer }
    }

    fn note(id: &str, version: i64) {
        WRITTEN.with(|w| {
            let mut written = w.borrow_mut();
            let Some(written) = written.as_mut() else { return };
            match written.iter_mut().find(|(i, _)| i == id) {
                Some((_, kept)) if *kept != Some(version) => *kept = None,
                Some(_) => {}
                None => written.push((id.into(), Some(version))),
            }
        });
    }

    /// `result`, with each item in it that has a version -- an object with
    /// an id and a version -- given the version just written for its id.
    pub fn stamp(&self, result: &mut Value) {
        WRITTEN.with(|w| {
            let written = w.borrow();
            let Some(written) = written.as_ref() else { return };
            stamp(result, written);
        });
    }
}

fn stamp(value: &mut Value, written: &[(String, Option<i64>)]) {
    match value {
        Value::Object(map) => {
            if map.contains_key("version") {
                let id = map.get("id").and_then(Value::as_str).map(str::to_string);
                if let Some(Some(version)) = id.and_then(|id| written.iter().find(|(i, _)| *i == id).map(|(_, v)| *v)) {
                    map.insert("version".into(), Value::from(version));
                }
            }
            for child in map.values_mut() {
                stamp(child, written);
            }
        }
        Value::Array(items) => {
            for item in items {
                stamp(item, written);
            }
        }
        _ => {}
    }
}

impl Drop for Written {
    fn drop(&mut self) {
        if self.outer {
            WRITTEN.with(|w| *w.borrow_mut() = None);
        }
    }
}

/// Refuse a write -- a deletion, say -- to the `kind` row `id`, as it is
/// now (`current`, None if it's gone), if the client said it last saw
/// version `expected` and it's not that version now.
pub fn expect_version(kind: &'static str, id: &str, current: Option<&Row>, expected: Option<i64>) -> anyhow::Result<()> {
    let Some(expected) = expected else { return Ok(()) };
    let now = current.and_then(|r| r.get("version")).and_then(Value::as_i64);
    if current.is_some() && now == Some(expected) {
        return Ok(());
    }
    Err(Conflict { kind, id: id.into(), expected, current: current.cloned() }.into())
}

/// `expect_version` for a typed item.
pub fn expect_item_version<T: RowItem>(id: &str, current: Option<&T>, expected: Option<i64>) -> anyhow::Result<()> {
    expect_version(T::ROW_TYPE.name, id, current.map(RowItem::to_row).as_ref(), expected)
}

/// A typed row: a struct whose fields are its tab's columns (see
/// `#[derive(RowItem)]`, which derives this).
pub trait RowItem: serde::Serialize + serde::de::DeserializeOwned {
    /// Its kind's name and columns, from its fields.
    const ROW_TYPE: RowType;

    /// Whether `value`, read from the cell of column `field`, fits that
    /// field's type; if not, why.
    fn check(field: &str, value: &Value) -> Result<(), String>;

    /// The item as a row: every column, null when unset.
    fn to_row(&self) -> Row {
        Self::ROW_TYPE.from_value(&serde_json::to_value(self).expect("a row item is JSON"))
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Kind {
    Str,
    Int,
    Json,
}

/// A kind of row: its type name (for messages), and its fields in column
/// order with how each is kept.
#[derive(Debug, Clone, Copy)]
pub struct RowType {
    pub name: &'static str,
    pub fields: &'static [(&'static str, Kind)],
}

impl RowType {
    pub fn empty(&self) -> Row {
        self.fields.iter().map(|(name, _)| (name.to_string(), Value::Null)).collect()
    }

    /// A row from a client's object (already checked against the schema):
    /// its fields, in order, null for those not given.
    pub fn from_value(&self, value: &Value) -> Row {
        self.fields.iter().map(|(name, _)| (name.to_string(), value.get(*name).cloned().unwrap_or(Value::Null))).collect()
    }

    fn kind(&self, name: &str) -> Option<Kind> {
        self.fields.iter().find(|(n, _)| *n == name).map(|(_, k)| *k)
    }
}

thread_local! {
    /// Whether the tool call running on this thread may choose new items'
    /// ids (see `ClientIds`).
    static CLIENT_IDS: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

/// Within this guard's life -- a call made once, by its write_id -- a new
/// item may have the id the client chose (see `chosen_id`). Other calls'
/// ids are assigned, whatever they give, as they always were.
pub struct ClientIds {
    was: bool,
}

impl ClientIds {
    pub fn allow() -> ClientIds {
        ClientIds { was: CLIENT_IDS.replace(true) }
    }
}

/// Whether the call running on this thread may choose new items' ids.
pub fn client_ids_allowed() -> bool {
    CLIENT_IDS.get()
}

impl Drop for ClientIds {
    fn drop(&mut self) {
        CLIENT_IDS.set(self.was);
    }
}

/// The id for a new item: `given`, the client's own choice -- so it can
/// name the item before the server has it -- in a call that may choose
/// it (see `ClientIds`), if it's well formed (2 to 40 lowercase letters,
/// digits and dashes) and not `taken`; or else a new one.
pub fn chosen_id<'a>(given: Option<&str>, taken: impl IntoIterator<Item = Option<&'a str>> + Clone) -> anyhow::Result<String> {
    let given = given.filter(|_| CLIENT_IDS.get());
    let Some(given) = given.map(str::trim).filter(|g| !g.is_empty()) else {
        return Ok(new_id(taken));
    };
    let formed = (2..=40).contains(&given.len())
        && given.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-');
    if !formed {
        bail!("An id is 2 to 40 lowercase letters, digits and dashes: {} isn't", py::repr(given));
    }
    if taken.into_iter().any(|t| t == Some(given)) {
        bail!("The id {} is already used", py::repr(given));
    }
    Ok(given.into())
}

/// A short random id not in `taken`.
pub fn new_id<'a>(taken: impl IntoIterator<Item = Option<&'a str>> + Clone) -> String {
    loop {
        let candidate = crate::determinism::random_token(ID_ALPHABET, ID_LENGTH);
        if !taken.clone().into_iter().any(|t| t == Some(candidate.as_str())) {
            return candidate;
        }
    }
}

pub fn is_set(row: &Row, name: &str) -> bool {
    row.get(name).is_some_and(|v| !v.is_null())
}

pub fn text<'a>(row: &'a Row, name: &str) -> Option<&'a str> {
    row.get(name).and_then(Value::as_str)
}

/// Refuse `clear_fields` that can't be cleared, or that `item` also sets.
pub fn check_clear(item: &Row, clear_fields: &[String], clearable: &[&str]) -> anyhow::Result<()> {
    let unknown: Vec<&String> = clear_fields.iter().filter(|c| !clearable.contains(&c.as_str())).collect();
    if !unknown.is_empty() {
        let mut sorted_clearable: Vec<&str> = clearable.to_vec();
        sorted_clearable.sort();
        bail!("Can't clear {}; clearable fields are {}", py::sorted_repr(unknown), py::repr_list(&sorted_clearable));
    }
    let mut both: Vec<&String> = clear_fields.iter().filter(|name| is_set(item, name)).collect();
    both.sort();
    if !both.is_empty() {
        bail!("Can't both set and clear {}", py::repr_list(&both));
    }
    Ok(())
}

/// `current` with whichever of `given`'s fields are set (but the
/// read-only ones), and `clear_fields` blanked.
pub fn updated(current: &Row, given: &Row, clear_fields: &[String], read_only: &[&str]) -> Row {
    let mut out = current.clone();
    for (name, value) in given {
        if !read_only.contains(&name.as_str()) && !value.is_null() {
            out.insert(name.clone(), value.clone());
        }
    }
    for name in clear_fields {
        out.insert(name.clone(), Value::Null);
    }
    out
}

/// A row as a `T` (its values already checked).
pub fn item_of<T: RowItem>(row: Row) -> T {
    serde_json::from_value(Value::Object(row)).expect("a checked row fits its item")
}

/// `check_clear` for a typed item.
pub fn check_clear_item<T: RowItem>(item: &T, clear_fields: &[String], clearable: &[&str]) -> anyhow::Result<()> {
    check_clear(&item.to_row(), clear_fields, clearable)
}

/// `updated` for typed items.
pub fn updated_item<T: RowItem>(current: &T, given: &T, clear_fields: &[String], read_only: &[&str]) -> T {
    item_of(updated(&current.to_row(), &given.to_row(), clear_fields, read_only))
}

#[derive(Clone)]
pub struct RowSheet {
    sheets: SheetsClient,
    spreadsheet_id: String,
    sheet_id: i64,
    row_type: RowType,
    required: &'static [&'static str],
    title: String,
}

impl RowSheet {
    /// The tab tagged `role`, adding it (with its header row) the first time.
    pub fn ensure(
        sheets: &SheetsClient,
        spreadsheet_id: &str,
        role: &str,
        title: &str,
        row_type: RowType,
        required: &'static [&'static str],
    ) -> anyhow::Result<RowSheet> {
        Self::ensure_seeded(sheets, spreadsheet_id, role, title, row_type, required, &[])
    }

    /// The tab tagged `role`, adding it the first time with its header
    /// row and `seed`'s rows.
    pub fn ensure_seeded(
        sheets: &SheetsClient,
        spreadsheet_id: &str,
        role: &str,
        title: &str,
        row_type: RowType,
        required: &'static [&'static str],
        seed: &[Row],
    ) -> anyhow::Result<RowSheet> {
        let header: Vec<String> = row_type.fields.iter().map(|(n, _)| n.to_string()).collect();
        let sheet_id = match find_tab(sheets, spreadsheet_id, role)? {
            Some(id) => id,
            None => create_tab(sheets, spreadsheet_id, role, title, None, |new_id| {
                sheets.write_rows_in_sheet(spreadsheet_id, new_id, HEADER_RANGE, &vec![header.clone()])?;
                if seed.is_empty() {
                    return Ok(());
                }
                let rows: Rows = seed.iter().map(|item| cells(row_type, &header, item, None)).collect();
                sheets.write_rows_in_sheet(spreadsheet_id, new_id, DATA_RANGE, &rows)
            })?,
        };
        Ok(RowSheet { sheets: sheets.clone(), spreadsheet_id: spreadsheet_id.into(), sheet_id, row_type, required, title: title.into() })
    }

    pub fn spreadsheet_id(&self) -> &str {
        &self.spreadsheet_id
    }

    pub fn sheets(&self) -> &SheetsClient {
        &self.sheets
    }

    pub fn whole_tab(&self) -> TabRange {
        TabRange::new(&self.spreadsheet_id, self.sheet_id, WHOLE_RANGE)
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheets.prefetch(ranges)
    }

    /// Every row, in sheet order, blank rows skipped -- or a CellError
    /// naming every cell that can't be read.
    pub fn read(&self) -> anyhow::Result<Vec<Row>> {
        self.read_checked(|_, _| Ok(()))
    }

    /// Every row as a `T` -- or a CellError naming every cell that can't
    /// be read, or doesn't fit its field's type.
    pub fn read_items<T: RowItem>(&self) -> anyhow::Result<Vec<T>> {
        let rows = self.read_checked(T::check)?;
        rows.into_iter()
            .map(|row| serde_json::from_value(Value::Object(row)).map_err(|e| anyhow::anyhow!("a checked {} row doesn't fit: {e}", T::ROW_TYPE.name)))
            .collect()
    }

    /// Overwrite the data rows with `items` (see `write`).
    pub fn write_items<T: RowItem>(&self, items: &[T]) -> anyhow::Result<()> {
        self.write(&items.iter().map(RowItem::to_row).collect::<Vec<_>>())
    }

    /// The write `write_items` makes, to make with others in one request
    /// (`SheetsClient::write_tabs`).
    pub fn prepared_items<T: RowItem>(&self, items: &[T]) -> anyhow::Result<TabWrite> {
        self.prepared(&items.iter().map(RowItem::to_row).collect::<Vec<_>>())
    }


    fn read_checked(&self, check: impl Fn(&str, &Value) -> Result<(), String>) -> anyhow::Result<Vec<Row>> {
        let (header, rows) = self.read_raw()?;
        let mut items = Vec::new();
        let mut bad = Vec::new();
        for (i, row) in rows.iter().enumerate() {
            if row.iter().any(|c| !c.trim().is_empty()) {
                // The spreadsheet's numbering: the header is row 1.
                let number = i + 2;
                let item = self.from_row(&header, row, number, &mut bad);
                for (name, value) in &item {
                    if value.is_null() {
                        continue;
                    }
                    if let Err(why) = check(name, value) {
                        let text = header.iter().position(|h| h == name).and_then(|c| row.get(c)).map(|c| c.trim()).unwrap_or("");
                        bad.push(format!("row {number}, column {name}: {} doesn't fit ({why})", py::repr(text)));
                    }
                }
                items.push(item);
            }
        }
        if !bad.is_empty() {
            return Err(CellError { tab: self.title.clone(), cells: bad }.into());
        }
        Ok(items)
    }

    /// Overwrite the data rows with `items`, keeping unknown columns'
    /// cells, and blanking rows left over from a longer list.
    pub fn write(&self, items: &[Row]) -> anyhow::Result<()> {
        let write = self.prepared(items)?;
        self.sheets.write_rows_in_sheet(&self.spreadsheet_id, write.sheet_id, &write.range, &write.rows)
    }

    /// The write `write` makes: for a versioned kind, each row with its
    /// version -- one up for a row it changes, 1 for a new one -- noted for
    /// the tool's result (see `Written`); or a `Conflict` for a row given
    /// with a version that isn't the one kept.
    pub fn prepared(&self, items: &[Row]) -> anyhow::Result<TabWrite> {
        let (original_header, previous) = self.read_raw()?;
        let items = self.versioned(&original_header, &previous, items)?;
        let items = &items[..];
        let mut header = original_header.clone();
        for (name, _) in self.row_type.fields {
            if !original_header.iter().any(|h| h == name) && items.iter().any(|item| is_set(item, name)) {
                header.push(name.to_string());
            }
        }
        let mut by_id: Vec<(Value, &Vec<String>)> = Vec::new();
        let id_column = header.iter().position(|h| h == "id");
        for row in &previous {
            if row.iter().any(|c| !c.trim().is_empty()) {
                let id = id_column
                    .and_then(|i| row.get(i))
                    .map(|c| c.trim())
                    .filter(|c| !c.is_empty())
                    .map(|c| Value::String(c.into()))
                    .unwrap_or(Value::Null);
                if let Some(slot) = by_id.iter_mut().find(|(k, _)| *k == id) {
                    slot.1 = row;
                } else {
                    by_id.push((id, row));
                }
            }
        }
        let mut rows: Rows = items
            .iter()
            .map(|item| {
                let id = item.get("id").cloned().unwrap_or(Value::Null);
                let original = by_id.iter().find(|(k, _)| *k == id).map(|(_, r)| *r);
                self.to_row(&header, item, original)
            })
            .collect();
        for _ in items.len()..previous.len() {
            rows.push(vec![String::new(); header.len()]);
        }
        if header == original_header {
            Ok(TabWrite { sheet_id: self.sheet_id, range: DATA_RANGE.into(), rows })
        } else {
            let mut all = vec![header];
            all.extend(rows);
            Ok(TabWrite { sheet_id: self.sheet_id, range: WHOLE_RANGE.into(), rows: all })
        }
    }

    /// `items`, each with its version, if the kind has versions: as kept
    /// for a row this doesn't change, one up for one it does, and 1 for a
    /// new one -- or a `Conflict` for one given with a version that isn't
    /// the one kept.
    fn versioned(&self, header: &[String], previous: &Rows, items: &[Row]) -> anyhow::Result<Vec<Row>> {
        if self.row_type.kind("version").is_none() {
            return Ok(items.to_vec());
        }
        let id_column = header.iter().position(|h| h == "id");
        let version_column = header.iter().position(|h| h == "version");
        let kept = |id: &str| {
            previous.iter().rev().find(|row| id_column.and_then(|i| row.get(i)).is_some_and(|c| c.trim() == id))
        };
        let mut out = Vec::with_capacity(items.len());
        for item in items {
            let mut item = item.clone();
            let original = text(&item, "id").and_then(kept);
            let given = item.get("version").and_then(Value::as_i64);
            let version = match original {
                None => 1,
                Some(original) => {
                    let was = version_column
                        .and_then(|i| original.get(i))
                        .and_then(|c| crate::event::python_int(c.trim()));
                    if let Some(given) = given.filter(|g| Some(*g) != was) {
                        let current = self.from_row(header, original, 0, &mut Vec::new());
                        return Err(Conflict {
                            kind: self.row_type.name,
                            id: text(&item, "id").unwrap_or_default().into(),
                            expected: given,
                            current: Some(current),
                        }
                        .into());
                    }
                    let cells = cells(self.row_type, header, &item, Some(original));
                    let changed = header.iter().enumerate().any(|(i, name)| {
                        name != "version"
                            && self.row_type.kind(name).is_some()
                            && cells.get(i).map(String::as_str).unwrap_or("")
                                != original.get(i).map(|c| c.trim()).unwrap_or("")
                    });
                    match (was, changed) {
                        (Some(was), false) => was,
                        (was, _) => was.unwrap_or(0) + 1,
                    }
                }
            };
            item.insert("version".into(), Value::from(version));
            if let Some(id) = text(&item, "id") {
                Written::note(id, version);
            }
            out.push(item);
        }
        Ok(out)
    }

    fn read_raw(&self) -> anyhow::Result<(Vec<String>, Rows)> {
        let mut found = self.sheets.read_ranges_in_sheet(&self.spreadsheet_id, self.sheet_id, &[HEADER_RANGE, DATA_RANGE])?;
        let rows = found.pop().unwrap_or_default();
        let header_rows = found.pop().unwrap_or_default();
        let header: Vec<String> = header_rows.first().map(|h| h.iter().map(|c| c.trim().to_string()).collect()).unwrap_or_default();
        let missing: Vec<&str> = self.required.iter().copied().filter(|name| !header.iter().any(|h| h == name)).collect();
        if !missing.is_empty() {
            bail!("The {} tab's header row is missing columns: {}", self.title, py::repr_list(&missing));
        }
        Ok((header, rows))
    }

    /// Spreadsheet row `number`'s item, adding to `bad` each cell that
    /// can't be read as its column's kind.
    fn from_row(&self, header: &[String], data: &[String], number: usize, bad: &mut Vec<String>) -> Row {
        let mut row = self.row_type.empty();
        for (i, name) in header.iter().enumerate() {
            let Some(kind) = self.row_type.kind(name) else { continue };
            let text = data.get(i).map(|c| c.trim()).unwrap_or("");
            if text.is_empty() {
                continue;
            }
            let value = match kind {
                Kind::Int => crate::event::python_int(text).map(Value::from).ok_or("isn't a whole number"),
                // A yes or no is written as Python prints it, and Sheets
                // may give it back in capitals.
                Kind::Json => match text.to_lowercase().as_str() {
                    "true" => Ok(Value::Bool(true)),
                    "false" => Ok(Value::Bool(false)),
                    _ => serde_json::from_str(text).map_err(|_| "isn't valid JSON"),
                },
                Kind::Str => Ok(Value::String(text.into())),
            };
            match value {
                Ok(value) => {
                    row.insert(name.clone(), value);
                }
                Err(why) => bad.push(format!("row {number}, column {name}: {} {why}", py::repr(text))),
            }
        }
        row
    }

    fn to_row(&self, header: &[String], item: &Row, original: Option<&Vec<String>>) -> Vec<String> {
        cells(self.row_type, header, item, original)
    }
}

/// `item`'s cells under `header`: its fields' values as text, and the
/// cells of columns it doesn't have from `original`.
fn cells(row_type: RowType, header: &[String], item: &Row, original: Option<&Vec<String>>) -> Vec<String> {
    header
        .iter()
        .enumerate()
        .map(|(i, name)| {
            if row_type.kind(name).is_some() {
                match item.get(name) {
                    None | Some(Value::Null) => String::new(),
                    Some(Value::String(s)) => s.clone(),
                    Some(v @ (Value::Array(_) | Value::Object(_))) => py::json_compact_ascii(v),
                    Some(Value::Bool(b)) => if *b { "True" } else { "False" }.to_string(),
                    Some(Value::Number(n)) => py::python_number(n),
                }
            } else {
                original.and_then(|o| o.get(i)).cloned().unwrap_or_default()
            }
        })
        .collect()
}

#[cfg(test)]
#[path = "rows_tests.rs"]
mod tests;
