//! The write-ahead journal of note compactions (utilities/
//! compaction_journal.py), one tab of the metadata spreadsheet: one row
//! per fact, in eight columns. The compaction port fills the rest in;
//! the event tools need where history ends.

use crate::event::{parse_iso, Moment};
use crate::google::sheets::{Rows, SheetsClient, TabRange};
use crate::sheet::{ensure_tab, COMPACTIONS};
use serde_json::Value;

pub const PLANNED: &str = "planned";
pub const PROPOSED: &str = "proposed";
pub const SUPERSEDED: &str = "superseded";
pub const APPLYING: &str = "applying";
pub const APPLIED: &str = "applied";
pub const STAMPED: &str = "stamped";
pub const FAILED: &str = "failed";
pub const ABANDONED: &str = "abandoned";
pub const OPEN_STATUSES: [&str; 2] = [APPLYING, APPLIED];
pub const USER_EDIT: &str = "user_decision";
pub const FEEDBACK: &str = "feedback";
pub const CLAUDE_DECISION: &str = "claude_decision";

pub const HEADER: [&str; 8] = ["compaction_id", "step", "kind", "event_id", "before", "after", "status", "detail"];
pub const HEADER_RANGE: &str = "A1:H1";
pub const DATA_RANGE: &str = "A2:H";
pub const FIRST_DATA_ROW: i64 = 2;

#[derive(Clone)]
pub struct CompactionJournal {
    pub(crate) sheets: SheetsClient,
    pub(crate) spreadsheet_id: String,
    pub(crate) sheet_id: i64,
}

impl CompactionJournal {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<CompactionJournal> {
        let (sheet_id, created) = ensure_tab(sheets, spreadsheet_id, COMPACTIONS.0, COMPACTIONS.1)?;
        if created {
            let header: Vec<String> = HEADER.iter().map(|s| s.to_string()).collect();
            sheets.write_rows_in_sheet(spreadsheet_id, sheet_id, HEADER_RANGE, &vec![header])?;
        }
        Ok(CompactionJournal { sheets: sheets.clone(), spreadsheet_id: spreadsheet_id.into(), sheet_id })
    }

    pub fn whole_tab(&self) -> TabRange {
        TabRange::new(&self.spreadsheet_id, self.sheet_id, "A1:H")
    }

    pub fn prefetch(&self, ranges: &[TabRange]) -> anyhow::Result<()> {
        self.sheets.prefetch(ranges)
    }

    /// Every data row through the last non-blank one, padded to the full
    /// width.
    pub fn read_rows(&self) -> anyhow::Result<Rows> {
        let rows = self.sheets.read_rows_in_sheet(&self.spreadsheet_id, self.sheet_id, DATA_RANGE)?;
        Ok(rows
            .into_iter()
            .map(|mut row| {
                while row.len() < HEADER.len() {
                    row.push(String::new());
                }
                row
            })
            .collect())
    }

    /// The `now` of the most recently stamped compaction, if any: where
    /// history ends.
    pub fn last_stamped_now(&self) -> anyhow::Result<Option<Moment>> {
        let mut latest: Option<Moment> = None;
        for row in self.read_rows()? {
            if row[2] != "compaction" || row[6] != STAMPED {
                continue;
            }
            let detail: Value = serde_json::from_str(&row[7])?;
            let now = parse_iso(detail["now"].as_str().unwrap_or_default(), None)?;
            if latest.is_none_or(|l| now > l) {
                latest = Some(now);
            }
        }
        Ok(latest)
    }
}
