//! The calendar's events, kept for the process's life: every event, series
//! master and exception (cancelled ones included), unexpanded, as a
//! listing with singleEvents=false gives them -- brought up to date by
//! Calendar's incremental sync (syncToken), which sends only what changed.
//! CalendarClient lists and gets events from here, expanding series as
//! Google does (google::expand), rather than asking Calendar each time.
//!
//! A write marks the copy stale, so the next read syncs first; a sync
//! clears that only if no write began or ended while it ran (each moves
//! `generation` on as it begins and again as it ends), so what it read
//! can't be older than a write. Syncs of one calendar run one at a time.
//!
//! What a sync brings that the server didn't write itself -- edited
//! elsewhere -- is kept for the app to warn of (`take_outside`). A write
//! marks its event (and series) as the server's own until a sync brings
//! back the etag Calendar answered it with: Calendar's feed can lag a
//! write, so a sync just after it may not have it yet. A series' mark
//! isn't ended so: an edit to a series (ending it, moving it) changes
//! instances too -- cancelling or keeping its edited ones -- which come
//! as items of their own, in that sync or a later one. It lapses, like a
//! mark that's never matched -- a delete, an answer lost --
//! `MARK_LAPSES` after its write ends, silently.
//!
//! Nor is what a full listing already had: the sync token it ends with
//! can be from before some of what it listed, so the syncs after it
//! bring those again -- hours later, now and then -- unchanged. Only
//! what changed after the newest change it listed (`listed_through`, by
//! Calendar's own clock) is kept to warn of.
//!
//! Only the last `KEPT_DAYS` are kept, so what's kept stays about a year's
//! size however long the calendar's history: an event (or exception) that
//! ended, and was to have happened, before `Store::from` is dropped as a
//! sync or write brings it. Series masters are kept whatever their dates,
//! to expand. CalendarClient asks Calendar itself for anything earlier.
//!
//! Kept per Calendar root (a fake's address, in tests) and calendar.

use super::expand::{instance_base, is_master, parse_moment};
use chrono::{DateTime, Utc};
use chrono_tz::Tz;
use serde_json::Value;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, Instant};

/// How long a write's mark lasts after the write ends, if no sync brings
/// back its etag first.
pub const MARK_LAPSES: Duration = Duration::from_secs(5 * 60);

/// How many days back events are kept: about the 13 months the app reads.
pub const KEPT_DAYS: i64 = 400;

/// Where what's kept starts, as of `now`.
pub fn kept_from(now: DateTime<Utc>) -> DateTime<Utc> {
    now - chrono::Duration::days(KEPT_DAYS)
}

/// Where what's kept starts now. A unit test that hasn't set the clock
/// (determinism::TEST_NOW) keeps everything, so its dates don't fall out
/// of what's kept as time passes.
pub fn kept_from_now() -> DateTime<Utc> {
    #[cfg(test)]
    if crate::determinism::TEST_NOW.with(std::cell::Cell::get).is_none() {
        return DateTime::parse_from_rfc3339("1900-01-01T00:00:00Z").unwrap().with_timezone(&Utc);
    }
    kept_from(crate::determinism::now().with_timezone(&Utc))
}

/// The earliest and latest `event` touches, in `zone`: its start and end,
/// and for an exception, when it was to have been. `None` for one with
/// no times it can read.
pub fn reach(event: &Value, zone: Tz) -> Option<(DateTime<Utc>, DateTime<Utc>)> {
    let times: Vec<DateTime<Utc>> = ["start", "end", "originalStartTime"]
        .iter()
        .filter_map(|key| event.get(*key).and_then(|v| parse_moment(v, zone)).map(|w| w.key(zone)))
        .collect();
    Some((*times.iter().min()?, *times.iter().max()?))
}

/// Whether `event` is too old to keep, with what's kept starting at
/// `from`: not a series' master, and over by then -- with a day to spare,
/// as an all-day event's times are read in `zone`, which may not be the
/// one the listing that needs it is in.
pub fn too_old(event: &Value, from: DateTime<Utc>, zone: Tz) -> bool {
    !is_master(event) && reach(event, zone).is_some_and(|(_, latest)| latest < from - chrono::Duration::days(1))
}

/// A mark on an event (or, for `Store::written_all`, every event): its
/// changes are the server's own while a write to it is under way, or
/// until `until`.
#[derive(Default)]
pub struct Mark {
    /// Writes under way.
    writing: usize,
    /// When it lapses, once no write is under way.
    until: Option<Instant>,
    /// The etags Calendar answered writes to this very event with: a sync
    /// bringing one back ends the mark.
    etags: HashSet<String>,
}

impl Mark {
    fn holds(&self, now: Instant) -> bool {
        self.writing > 0 || self.until.is_some_and(|until| now < until)
    }
}

#[derive(Default)]
pub struct Store {
    /// Every event by id, as the last sync left it.
    pub events: BTreeMap<String, Value>,
    /// Where the next sync starts; `None` before the first.
    pub token: Option<String>,
    /// A write since the last sync: the next read syncs first.
    pub stale: bool,
    /// Goes up as each write begins, and again as it ends.
    pub generation: u64,
    /// The events written lately (and the series they're in), by id: their
    /// changes are the server's own while each mark holds.
    pub written: HashMap<String, Mark>,
    /// Something was written that may change any event (a label).
    pub written_all: Option<Mark>,
    /// Events a sync brought that the server didn't write, newest last.
    pub outside: Vec<Value>,
    /// When the newest change the last full listing had was made: a sync
    /// bringing an event changed no later is that listing catching up.
    pub listed_through: Option<DateTime<Utc>>,
    /// The calendar's time zone, as the last sync gave it.
    pub zone: Option<chrono_tz::Tz>,
    /// Where what's kept starts: every event that ends at or after it is
    /// here; `None` before the first sync.
    pub from: Option<DateTime<Utc>>,
}

impl Store {
    /// Note a write to event `id` (or, `None`, to anything) beginning.
    pub fn writing(&mut self, id: Option<&str>) {
        self.stale = true;
        self.generation += 1;
        match id {
            Some(id) => {
                for key in keys(id) {
                    self.written.entry(key).or_default().writing += 1;
                }
            }
            None => self.written_all.get_or_insert_with(Mark::default).writing += 1,
        }
    }

    /// Note that write's end, `now`: Calendar answered it with `etag`
    /// (`None` for no answer, or none with one).
    pub fn wrote(&mut self, id: Option<&str>, etag: Option<&str>, now: Instant) {
        self.generation += 1;
        let until = Some(now + MARK_LAPSES);
        let end = |mark: &mut Mark| {
            mark.writing = mark.writing.saturating_sub(1);
            mark.until = until;
        };
        match id {
            Some(id) => {
                for key in keys(id) {
                    if let Some(mark) = self.written.get_mut(&key) {
                        end(mark);
                        if key == id {
                            mark.etags.extend(etag.map(str::to_string));
                        }
                    }
                }
            }
            None => {
                if let Some(mark) = self.written_all.as_mut() {
                    end(mark);
                }
            }
        }
    }

    /// Whether `item`, brought by a sync `now`, is the server's own change.
    pub fn is_ours(&self, item: &Value, now: Instant) -> bool {
        if self.written_all.as_ref().is_some_and(|mark| mark.holds(now)) {
            return true;
        }
        let id = item["id"].as_str().unwrap_or_default();
        let series = item["recurringEventId"].as_str().unwrap_or_default();
        [id, instance_base(id), series, instance_base(series)]
            .iter()
            .any(|i| !i.is_empty() && self.written.get(*i).is_some_and(|mark| mark.holds(now)))
    }

    /// Note a full listing of `items`: what changed by its newest change is
    /// what the server took as it was, not a change after (see
    /// `changed_since_listed`).
    pub fn listed(&mut self, items: &[Value]) {
        self.listed_through = items.iter().filter_map(updated).max();
    }

    /// Whether `item`, brought by a sync since the last full listing,
    /// changed after what that listing had: not that listing's feed
    /// catching up.
    pub fn changed_since_listed(&self, item: &Value) -> bool {
        match (updated(item), self.listed_through) {
            (Some(at), Some(listed)) => at > listed,
            _ => true,
        }
    }

    /// Note `item`, brought by a sync: if it's the etag a write was answered
    /// with, that write is back, and its event's mark ends (unless another
    /// write to it is under way) -- not a series', which also covers the
    /// instances the write changed, still to come or be looked at.
    pub fn brought(&mut self, item: &Value) {
        if is_master(item) {
            return;
        }
        let (Some(id), Some(etag)) = (item["id"].as_str(), item["etag"].as_str()) else { return };
        if self.written.get(id).is_some_and(|mark| mark.writing == 0 && mark.etags.contains(etag)) {
            self.written.remove(id);
        }
    }

    /// Keep `event`, as a sync brought it or a write was answered with --
    /// unless what's kept of it is newer (by `updated`, which Calendar
    /// moves on with each change): Calendar's feed can lag a write the
    /// server already has the answer to, and mustn't take it back.
    /// One too old to keep (see `too_old`) is dropped instead.
    pub fn keep(&mut self, event: Value) {
        let Some(id) = event["id"].as_str().map(str::to_string) else { return };
        if self.from.is_some_and(|from| too_old(&event, from, self.zone.unwrap_or(chrono_tz::UTC))) {
            self.events.remove(&id);
            return;
        }
        if self.events.get(&id).is_some_and(|kept| newer(kept, &event)) {
            return;
        }
        self.events.insert(id, event);
    }

    /// After a full listing (the first sync, or one Calendar asked for
    /// with a 410): drop the events kept that it didn't list -- but not
    /// those the server wrote lately, whose answers it may have before
    /// the listing does. What it did list was kept as `keep` keeps.
    pub fn keep_only(&mut self, listed: &HashSet<String>, now: Instant) {
        let gone: Vec<String> =
            self.events.iter().filter(|(id, event)| !listed.contains(*id) && !self.is_ours(event, now)).map(|(id, _)| id.clone()).collect();
        for id in gone {
            self.events.remove(&id);
        }
    }

    /// Start what's kept at `from` (never earlier than it started), dropping
    /// what's now too old.
    pub fn start_at(&mut self, from: DateTime<Utc>) {
        let from = self.from.map_or(from, |kept| kept.max(from));
        self.from = Some(from);
        let zone = self.zone.unwrap_or(chrono_tz::UTC);
        self.events.retain(|_, event| !too_old(event, from, zone));
    }

    /// Drop the marks that have lapsed by `now`.
    pub fn lapse(&mut self, now: Instant) {
        self.written.retain(|_, mark| mark.holds(now));
        if self.written_all.as_ref().is_some_and(|mark| !mark.holds(now)) {
            self.written_all = None;
        }
    }
}

/// Whether `a` was changed after `b`, by their `updated`; not if either
/// has none.
fn newer(a: &Value, b: &Value) -> bool {
    matches!((updated(a), updated(b)), (Some(a), Some(b)) if a > b)
}

/// When `event` last changed, by its `updated`.
fn updated(event: &Value) -> Option<DateTime<Utc>> {
    event["updated"].as_str().and_then(|u| DateTime::parse_from_rfc3339(u).ok()).map(|u| u.with_timezone(&Utc))
}

/// The ids a write to event `id` marks: it, its series (for an instance),
/// and each one's base, which a split series' parts share.
fn keys(id: &str) -> Vec<String> {
    let mut keys = Vec::new();
    for one in [Some(id), series_of(id)].into_iter().flatten() {
        for key in [one, instance_base(one)] {
            if !keys.iter().any(|k| k == key) {
                keys.push(key.to_string());
            }
        }
    }
    keys
}

/// The series an instance's id names ("<series>_<start>"), if it's one.
pub fn series_of(id: &str) -> Option<&str> {
    let (series, when) = id.rsplit_once('_')?;
    let digits = |t: &str| !t.is_empty() && t.chars().all(|c| c.is_ascii_digit());
    let timed = when.len() == 16 && when.ends_with('Z') && when.as_bytes()[8] == b'T' && digits(&when[..8]) && digits(&when[9..15]);
    (timed || (when.len() == 8 && digits(when))).then_some(series)
}

type Key = (String, String);

static STORES: LazyLock<Mutex<HashMap<Key, Store>>> = LazyLock::new(|| Mutex::new(HashMap::new()));
static SYNCING: LazyLock<Mutex<HashMap<Key, Arc<Mutex<()>>>>> = LazyLock::new(|| Mutex::new(HashMap::new()));

/// Run `f` on calendar `calendar_id`'s store, for Calendar at `root`.
pub fn with<T>(root: &str, calendar_id: &str, f: impl FnOnce(&mut Store) -> T) -> T {
    let mut stores = STORES.lock().unwrap_or_else(|e| e.into_inner());
    f(stores.entry((root.to_string(), calendar_id.to_string())).or_default())
}

/// The lock a sync of `calendar_id` holds, so syncs of it run one at a
/// time: one applied after a later one would undo it.
pub fn sync_lock(root: &str, calendar_id: &str) -> Arc<Mutex<()>> {
    let mut locks = SYNCING.lock().unwrap_or_else(|e| e.into_inner());
    locks.entry((root.to_string(), calendar_id.to_string())).or_default().clone()
}

/// About how many bytes of the heap `value` owns (see crate::heap), as
/// serde_json holds it: each string's text, each list's values, and each
/// object's entries (a hash, a key and a value each, in order) and its
/// hash table of their places, with room for as many entries as the table
/// has.
pub fn estimated_bytes(value: &Value) -> usize {
    use crate::heap::{block, buffer, hash_capacity, hash_table};
    use std::mem::size_of;
    match value {
        Value::String(text) => block(text.capacity()),
        Value::Array(items) => buffer::<Value>(items.capacity()) + items.iter().map(estimated_bytes).sum::<usize>(),
        Value::Object(entries) if entries.is_empty() => 0,
        Value::Object(entries) => {
            let entry = size_of::<usize>() + size_of::<String>() + size_of::<Value>();
            block(hash_capacity(entries.len()) * entry)
                + hash_table(entries.len(), size_of::<usize>())
                + entries.iter().map(|(key, v)| block(key.capacity()) + estimated_bytes(v)).sum::<usize>()
        }
        _ => 0,
    }
}

/// About how many bytes the events kept of every calendar take, for
/// Calendar at `root` (see `estimated_bytes`).
pub fn size(root: &str) -> usize {
    let stores = STORES.lock().unwrap_or_else(|e| e.into_inner());
    stores
        .iter()
        .filter(|((r, _), _)| r == root)
        .map(|(_, store)| {
            let entry = std::mem::size_of::<String>() + std::mem::size_of::<Value>();
            crate::heap::btree(store.events.len(), entry)
                + store.events.iter().map(|(id, event)| crate::heap::block(id.capacity()) + estimated_bytes(event)).sum::<usize>()
        })
        .sum()
}

/// Forget what's kept of `calendar_id`, so it's synced afresh -- as when
/// the server starts. Tests call this after changing a fake's calendar
/// behind the server's back.
pub fn forget(root: &str, calendar_id: &str) {
    with(root, calendar_id, |store| {
        *store = Store { generation: store.generation + 1, ..Store::default() };
    });
}

#[cfg(test)]
#[path = "synced_tests.rs"]
mod tests;
