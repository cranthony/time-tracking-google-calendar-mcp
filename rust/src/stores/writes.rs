//! The writes a client has made, each by the id it gave it (its
//! write_id, in a tool call's `_meta`): so a write sent again -- its
//! answer lost on the way back, say -- is answered as it was the first
//! time instead of being made twice. Each is kept 30 days, in the Writes
//! tab.

use crate::contract::RowItem;
use crate::google::sheets::SheetsClient;
use crate::sheet::rows::RowSheet;
use crate::sheet::WRITES;
use chrono::{DateTime, Duration, FixedOffset};
use serde::{Deserialize, Serialize};
use serde_json::Value;

/// How long a write is remembered: longer than a client waits to send
/// one again.
pub const KEPT_FOR: Duration = Duration::days(30);

/// A result longer than this, as JSON, isn't kept -- a cell holds 50,000
/// characters -- and a write sent again is only told it was made.
pub const MAX_RESULT_LENGTH: usize = 45_000;

/// One write: its id, the tool it called, when, and what it gave.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, RowItem)]
pub struct WriteRecord {
    pub id: Option<String>,
    pub tool: Option<String>,
    pub at: Option<String>,
    /// What the tool gave; none if it was too long to keep.
    pub result: Option<Value>,
}

#[derive(Clone)]
pub struct Writes {
    sheet: RowSheet,
}

impl Writes {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<Writes> {
        let row = <WriteRecord as crate::sheet::rows::RowItem>::ROW_TYPE;
        Ok(Writes { sheet: RowSheet::ensure(sheets, spreadsheet_id, WRITES.0, WRITES.1, row, &["id", "tool"])? })
    }

    /// The write made with `id`, if it was.
    pub fn find(&self, id: &str) -> anyhow::Result<Option<WriteRecord>> {
        Ok(self.sheet.read_items::<WriteRecord>()?.into_iter().find(|w| w.id.as_deref() == Some(id)))
    }

    /// Keeps that `tool` made the write `id`, giving `result`, at `now`;
    /// forgets those older than `KEPT_FOR`.
    pub fn record(&self, id: &str, tool: &str, result: &Value, now: DateTime<FixedOffset>) -> anyhow::Result<()> {
        let since = now - KEPT_FOR;
        let mut kept: Vec<WriteRecord> = self
            .sheet
            .read_items::<WriteRecord>()?
            .into_iter()
            .filter(|w| {
                w.at.as_deref().and_then(|at| DateTime::parse_from_rfc3339(at).ok()).is_some_and(|at| at >= since)
            })
            .collect();
        let short = crate::py::json_compact_ascii(result).len() <= MAX_RESULT_LENGTH;
        kept.push(WriteRecord {
            id: Some(id.into()),
            tool: Some(tool.into()),
            at: Some(now.to_rfc3339()),
            result: short.then(|| result.clone()),
        });
        self.sheet.write_items(&kept)
    }
}

#[cfg(test)]
#[path = "writes_tests.rs"]
mod tests;
