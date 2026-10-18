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
//! can't be older than a write. Syncs of one calendar run one at a time. What a sync brings that the server didn't write itself -- edited
//! elsewhere -- is kept for the app to warn of (`take_outside`).
//!
//! Kept per Calendar root (a fake's address, in tests) and calendar.

use super::expand::instance_base;
use serde_json::Value;
use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::{Arc, LazyLock, Mutex};

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
    /// The events written since the last sync (and the series they're
    /// in), whose changes are the server's own.
    pub written: HashSet<String>,
    /// Something was written that may change any event (a label).
    pub written_all: bool,
    /// Events a sync brought that the server didn't write, newest last.
    pub outside: Vec<Value>,
    /// The calendar's time zone, as the last sync gave it.
    pub zone: Option<chrono_tz::Tz>,
}

impl Store {
    /// Note a write to event `id` (or, `None`, to anything) beginning.
    pub fn writing(&mut self, id: Option<&str>) {
        self.stale = true;
        self.generation += 1;
        match id {
            Some(id) => {
                for one in [Some(id), series_of(id)].into_iter().flatten() {
                    self.written.insert(one.to_string());
                    self.written.insert(instance_base(one).to_string());
                }
            }
            None => self.written_all = true,
        }
    }

    /// Whether `item`, brought by a sync, is the server's own change.
    pub fn is_ours(&self, item: &Value) -> bool {
        if self.written_all {
            return true;
        }
        let id = item["id"].as_str().unwrap_or_default();
        let series = item["recurringEventId"].as_str().unwrap_or_default();
        [id, instance_base(id), series, instance_base(series)].iter().any(|i| !i.is_empty() && self.written.contains(*i))
    }
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
