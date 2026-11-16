//! The open proposal's layer, as calendar writes see it: what's decided
//! lies over the calendar, cutting, covering and splitting what's under
//! it (see compaction::plan's `imply`), and freeing what a decided event
//! no longer takes. Read from the current revision as it was planned --
//! its journaled changes -- not planned again, so writes stay fast.
//!
//! A calendar write while a proposal is open is checked against the
//! calendar with the layer on top (see event_changes' `Overlay`), and one
//! the layer bears on is kept with the proposal (`calendar_edits`), for the
//! app to say what abandoning it leaves.

use crate::event::{Event, Moment};
use crate::stores::event_changes::{Batch, Overlay};
use crate::stores::journal::{CalendarEditRecord, CompactionJournal, ABANDONED, STAMPED};

pub const CUT: &str = "cut";
pub const COVERED: &str = "covered";
pub const FREED: &str = "freed";

/// The open proposal's layer, as its current revision planned it.
#[derive(Debug, Clone)]
pub struct OpenLayer {
    pub proposal: String,
    pub overlay: Overlay,
    /// The calendar events the layer cuts (or splits) and covers: (id,
    /// `CUT` or `COVERED`).
    pub implied: Vec<(String, &'static str)>,
    /// Time a decided event no longer takes (ended earlier, started later,
    /// moved or cancelled): (that event's id, from, to).
    pub freed: Vec<(String, Moment, Moment)>,
}

/// The open proposal's layer, if a proposal is open.
pub fn open_layer(journal: &CompactionJournal) -> anyhow::Result<Option<OpenLayer>> {
    let Some(proposal) = journal.open_proposal()? else { return Ok(None) };
    let Some((_, first, _)) = journal.revisions(&proposal)?.pop() else { return Ok(None) };
    let days = journal.load_batch(&first)?;
    let mut layer = OpenLayer { proposal, overlay: Overlay::default(), implied: Vec::new(), freed: Vec::new() };
    for change in days.iter().filter(|d| d.status != STAMPED && d.status != ABANDONED).flat_map(|d| d.changes()) {
        let named = change.event_id.clone().or(change.key.clone()).unwrap_or_default();
        let after = change.after.as_ref().map(|a| a.to_event(Some(&named)));
        if change.implied_by.is_some() {
            match change.action.as_str() {
                "cancel" => {
                    layer.overlay.replaced.push((named.clone(), None));
                    layer.implied.push((named, COVERED));
                }
                "update" => {
                    layer.overlay.replaced.push((named.clone(), after));
                    layer.implied.push((named, CUT));
                }
                _ => layer.overlay.added.extend(after),
            }
            continue;
        }
        if let Some(id) = &change.event_id {
            layer.overlay.replaced.push((id.clone(), None));
        }
        let before = change.before.as_ref().and_then(|b| Some((b.start?, b.end?)));
        let kept = after.as_ref().and_then(|a| Some((a.start?, a.end?)));
        if let (Some(id), Some((start, end))) = (&change.event_id, before) {
            for (from, to) in minus((start, end), kept) {
                layer.freed.push((id.clone(), from, to));
            }
        }
        if change.action != "cancel" {
            layer.overlay.layer.extend(after);
        }
    }
    Ok(Some(layer))
}

/// What of `span` `kept` doesn't take.
fn minus(span: (Moment, Moment), kept: Option<(Moment, Moment)>) -> Vec<(Moment, Moment)> {
    let Some((start, end)) = kept else { return vec![span] };
    let mut left = Vec::new();
    if span.0 < start.min(span.1) {
        left.push((span.0, start.min(span.1)));
    }
    if end.max(span.0) < span.1 {
        left.push((end.max(span.0), span.1));
    }
    left
}

impl OpenLayer {
    /// How the layer bears on a write of `before` (None for a create) as
    /// `after` (None for a cancel): `CUT` or `COVERED` for an event it
    /// cuts or covers, `FREED` for one landing in time it frees.
    pub fn bears_on(&self, before: Option<&Event>, after: Option<&Event>) -> Option<&'static str> {
        if let Some(id) = before.and_then(|b| b.id.as_deref()) {
            if let Some((_, kind)) = self.implied.iter().find(|(i, _)| i == id) {
                return Some(kind);
            }
        }
        let after = after.filter(|a| a.end() > a.start())?;
        let own = after.id.as_deref();
        self.freed
            .iter()
            .any(|(id, from, to)| Some(id.as_str()) != own && *from < after.end() && after.start() < *to)
            .then_some(FREED)
    }

    /// The writes of `batch` -- made, `written` as they were -- the layer
    /// bears on, as kept with the proposal.
    pub fn edits_of(&self, batch: &Batch, written: &[Event], now: Moment) -> Vec<CalendarEditRecord> {
        let offset = batch.again.len();
        let mut found = Vec::new();
        for (index, change) in batch.changes.iter().enumerate() {
            let Some(kind) = self.bears_on(change.before.as_ref(), change.after.as_ref()) else { continue };
            let made = written.get(offset + index);
            let shown = change.after.as_ref().or(change.before.as_ref());
            let Some(event) = made.filter(|m| m.start.is_some()).or(shown) else { continue };
            let Some(event_id) = made.and_then(|m| m.id.clone()).or(event.id.clone()).filter(|i| !i.is_empty()) else { continue };
            found.push(CalendarEditRecord {
                seq: 0,
                event_id,
                kind: kind.into(),
                summary: event.summary.clone(),
                start: event.start(),
                end: event.end(),
                created: now,
            });
        }
        found
    }
}
