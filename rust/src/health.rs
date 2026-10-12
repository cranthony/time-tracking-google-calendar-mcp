//! Health metrics: each tool call's latency, the memory after it, and the
//! server's starts -- the last KEEP of each, recorded in memory and written
//! to the Health tab in the background, in the same encoding Python reads
//! and writes (so either server carries on the other's series).

use crate::google::sheets::SheetsClient;
use crate::contract::RowItem;
use crate::sheet::rows::{self, RowSheet, RowType};
use crate::sheet::HEALTH;
use crate::write_lock::HEALTH_TAB_LOCK;
use base64::Engine;
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::VecDeque;
use std::io::{Read, Write};
use std::sync::{Arc, Condvar, Mutex};

pub const KEEP: usize = 100;
pub const FLUSH_SECONDS: u64 = 30;
pub const SERVER: &str = "server";
pub const MEMORY: &str = "memory";
pub const RESTARTS: &str = "restarts";
pub const TOOL_PREFIX: &str = "tool:";
const VERSION: &str = "1";

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct CallSample {
    pub at_ms: i64,
    pub work_ms: i64,
    pub total_ms: Option<i64>,
    pub ok: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct MemorySample {
    pub at_ms: i64,
    pub rss_kib: i64,
}

/// A row of the Health tab: one source's series, encoded.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize, RowItem)]
pub struct HealthRow {
    pub id: Option<String>,
    pub source: Option<String>,
    pub series: Option<String>,
    pub count: Option<i64>,
    pub latest: Option<String>,
    pub data: Option<String>,
}

pub const HEALTH_ROW: RowType = <HealthRow as rows::RowItem>::ROW_TYPE;

// -- encoding -------------------------------------------------------------------

fn zigzag(n: i64) -> u64 {
    ((n << 1) ^ (n >> 63)) as u64
}

fn unzigzag(n: u64) -> i64 {
    ((n >> 1) as i64) ^ -((n & 1) as i64)
}

fn put(out: &mut Vec<u8>, mut n: u64) {
    loop {
        let byte = (n & 0x7f) as u8;
        n >>= 7;
        if n != 0 {
            out.push(byte | 0x80);
        } else {
            out.push(byte);
            return;
        }
    }
}

fn take(data: &[u8], mut at: usize) -> anyhow::Result<(u64, usize)> {
    let (mut n, mut shift) = (0u64, 0);
    loop {
        let byte = *data.get(at).ok_or_else(|| anyhow::anyhow!("index out of range"))?;
        at += 1;
        n |= ((byte & 0x7f) as u64) << shift;
        shift += 7;
        if byte & 0x80 == 0 {
            return Ok((n, at));
        }
    }
}

fn pack(columns: &[Vec<i64>]) -> String {
    let mut out = Vec::new();
    put(&mut out, columns.first().map(|c| c.len()).unwrap_or(0) as u64);
    for column in columns {
        let mut previous = 0i64;
        for value in column {
            put(&mut out, zigzag(value - previous));
            previous = *value;
        }
    }
    let mut encoder = flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::new(9));
    encoder.write_all(&out).expect("writing to memory");
    let compressed = encoder.finish().expect("writing to memory");
    format!("{VERSION}{}", base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(compressed))
}

fn unpack(text: &str, width: usize) -> anyhow::Result<Vec<Vec<i64>>> {
    let Some(body) = text.strip_prefix(VERSION) else {
        anyhow::bail!("unknown health data format {}", crate::py::repr(&text.chars().take(1).collect::<String>()));
    };
    let compressed = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(body.trim_end_matches('='))?;
    let mut data = Vec::new();
    flate2::read::ZlibDecoder::new(&compressed[..]).read_to_end(&mut data)?;
    let (count, mut at) = take(&data, 0)?;
    let mut columns = Vec::new();
    for _ in 0..width {
        let (mut column, mut previous) = (Vec::new(), 0i64);
        for _ in 0..count {
            let (delta, next) = take(&data, at)?;
            at = next;
            previous += unzigzag(delta);
            column.push(previous);
        }
        columns.push(column);
    }
    Ok(columns)
}

pub fn encode_calls(calls: &[CallSample]) -> String {
    pack(&[
        calls.iter().map(|c| c.at_ms).collect(),
        calls.iter().map(|c| c.work_ms).collect(),
        calls.iter().map(|c| c.total_ms.map_or(0, |t| (t - c.work_ms).max(0) + 1)).collect(),
        calls.iter().map(|c| c.ok as i64).collect(),
    ])
}

pub fn decode_calls(text: &str) -> anyhow::Result<Vec<CallSample>> {
    let columns = unpack(text, 4)?;
    Ok((0..columns[0].len())
        .map(|i| {
            let (w, o) = (columns[1][i], columns[2][i]);
            CallSample { at_ms: columns[0][i], work_ms: w, total_ms: if o == 0 { None } else { Some(w + o - 1) }, ok: columns[3][i] != 0 }
        })
        .collect())
}

pub fn encode_memory(samples: &[MemorySample]) -> String {
    pack(&[samples.iter().map(|s| s.at_ms).collect(), samples.iter().map(|s| s.rss_kib).collect()])
}

pub fn decode_memory(text: &str) -> anyhow::Result<Vec<MemorySample>> {
    let columns = unpack(text, 2)?;
    Ok((0..columns[0].len()).map(|i| MemorySample { at_ms: columns[0][i], rss_kib: columns[1][i] }).collect())
}

pub fn encode_times(times: &[i64]) -> String {
    pack(&[times.to_vec()])
}

pub fn decode_times(text: &str) -> anyhow::Result<Vec<i64>> {
    Ok(unpack(text, 1)?.remove(0))
}

// -- recording -------------------------------------------------------------------

#[cfg(test)]
thread_local! {
    /// The unit tests' memory reading (Some(None): none to be had).
    pub static TEST_RSS: std::cell::Cell<Option<Option<i64>>> = const { std::cell::Cell::new(None) };
    /// The unit tests' clock: each reading 1234 ms after the last.
    pub static TEST_CLOCK_MS: std::cell::Cell<Option<i64>> = const { std::cell::Cell::new(None) };
}

/// This process's resident memory, in KiB (Linux only).
pub fn rss_kib() -> Option<i64> {
    #[cfg(test)]
    if let Some(rss) = TEST_RSS.with(std::cell::Cell::get) {
        return rss;
    }
    let status = std::fs::read_to_string("/proc/self/status").ok()?;
    status.lines().find(|l| l.starts_with("VmRSS:"))?.split_whitespace().nth(1)?.parse().ok()
}

fn now_ms() -> i64 {
    #[cfg(test)]
    if let Some(ms) = TEST_CLOCK_MS.with(std::cell::Cell::get) {
        TEST_CLOCK_MS.with(|c| c.set(Some(ms + 1234)));
        return ms + 1234;
    }
    Utc::now().timestamp_millis()
}

#[derive(Default)]
struct Series {
    calls: Vec<(String, VecDeque<CallSample>)>,
    memory: VecDeque<MemorySample>,
    restarts: VecDeque<i64>,
    dirty: Vec<String>,
}

pub struct HealthRecorder {
    series: Mutex<Series>,
}

/// The latest KEEP of `older` and `newer` by time, each sample once.
fn merged<T: Copy + PartialEq>(older: Vec<T>, newer: &VecDeque<T>, at: impl Fn(&T) -> i64) -> VecDeque<T> {
    let mut seen: Vec<T> = Vec::new();
    for sample in older.into_iter().chain(newer.iter().copied()) {
        if !seen.contains(&sample) {
            seen.push(sample);
        }
    }
    seen.sort_by_key(|s| at(s));
    let skip = seen.len().saturating_sub(KEEP);
    seen.into_iter().skip(skip).collect()
}

fn push_kept<T>(deque: &mut VecDeque<T>, item: T) {
    deque.push_back(item);
    while deque.len() > KEEP {
        deque.pop_front();
    }
}

impl HealthRecorder {
    pub fn new() -> HealthRecorder {
        HealthRecorder { series: Mutex::new(Series::default()) }
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Series> {
        self.series.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn dirty(series: &mut Series, name: String) {
        if !series.dirty.contains(&name) {
            series.dirty.push(name);
        }
    }

    pub fn record_call(&self, tool: &str, work_ms: i64, total_ms: Option<i64>, ok: bool) {
        let at = now_ms();
        let rss = rss_kib();
        let mut series = self.lock();
        let index = match series.calls.iter().position(|(n, _)| n == tool) {
            Some(i) => i,
            None => {
                series.calls.push((tool.to_string(), VecDeque::new()));
                series.calls.len() - 1
            }
        };
        push_kept(&mut series.calls[index].1, CallSample { at_ms: at, work_ms, total_ms, ok });
        Self::dirty(&mut series, format!("{TOOL_PREFIX}{tool}"));
        if let Some(rss) = rss {
            push_kept(&mut series.memory, MemorySample { at_ms: at, rss_kib: rss });
            Self::dirty(&mut series, MEMORY.into());
        }
    }

    pub fn record_restart(&self) {
        let mut series = self.lock();
        push_kept(&mut series.restarts, now_ms());
        Self::dirty(&mut series, RESTARTS.into());
    }

    /// Take in the series as the tab kept them, from before this start.
    pub fn merge(&self, rows: &[HealthRow]) {
        let mut series = self.lock();
        for row in rows {
            let (Some(source), Some(name), Some(data)) = (row.source.as_deref(), row.series.as_deref(), row.data.as_deref()) else {
                continue;
            };
            if source != SERVER || data.is_empty() {
                continue;
            }
            let result: anyhow::Result<()> = (|| {
                if let Some(tool) = name.strip_prefix(TOOL_PREFIX) {
                    let older = decode_calls(data)?;
                    let index = match series.calls.iter().position(|(n, _)| n == tool) {
                        Some(i) => i,
                        None => {
                            series.calls.push((tool.to_string(), VecDeque::new()));
                            series.calls.len() - 1
                        }
                    };
                    let mine = series.calls[index].1.clone();
                    series.calls[index].1 = merged(older, &mine, |c| c.at_ms);
                } else if name == MEMORY {
                    let older = decode_memory(data)?;
                    let mine = series.memory.clone();
                    series.memory = merged(older, &mine, |s| s.at_ms);
                } else if name == RESTARTS {
                    let older = decode_times(data)?;
                    let mine = series.restarts.clone();
                    series.restarts = merged(older, &mine, |t| *t);
                }
                Ok(())
            })();
            if let Err(error) = result {
                tracing::warn!("health: skipping unreadable row {}: {error}", row.id.as_deref().unwrap_or_default());
            }
        }
    }

    pub fn take_dirty(&self) -> Option<Vec<HealthRow>> {
        let mut series = self.lock();
        if series.dirty.is_empty() {
            return None;
        }
        let mut dirty = std::mem::take(&mut series.dirty);
        dirty.sort();
        Some(dirty.iter().map(|name| Self::row(&series, name)).collect())
    }

    pub fn mark_dirty(&self, changed: &[HealthRow]) {
        let mut series = self.lock();
        for row in changed {
            if let Some(name) = row.series.as_deref() {
                Self::dirty(&mut series, name.to_string());
            }
        }
    }

    /// Each tool's calls, by tool name (sorted, as Python's dict is).
    pub fn calls(&self) -> Vec<(String, Vec<CallSample>)> {
        let series = self.lock();
        let mut calls: Vec<(String, Vec<CallSample>)> = series.calls.iter().map(|(n, s)| (n.clone(), s.iter().copied().collect())).collect();
        calls.sort_by(|a, b| a.0.cmp(&b.0));
        calls
    }

    pub fn memory(&self) -> Vec<MemorySample> {
        self.lock().memory.iter().copied().collect()
    }

    pub fn restarts(&self) -> Vec<i64> {
        self.lock().restarts.iter().copied().collect()
    }

    fn row(series: &Series, name: &str) -> HealthRow {
        let (data, latest, count) = if let Some(tool) = name.strip_prefix(TOOL_PREFIX) {
            let samples: Vec<CallSample> = series.calls.iter().find(|(n, _)| n == tool).map(|(_, s)| s.iter().copied().collect()).unwrap_or_default();
            (encode_calls(&samples), samples.last().map(|s| s.at_ms), samples.len())
        } else if name == MEMORY {
            let samples: Vec<MemorySample> = series.memory.iter().copied().collect();
            (encode_memory(&samples), samples.last().map(|s| s.at_ms), samples.len())
        } else {
            let samples: Vec<i64> = series.restarts.iter().copied().collect();
            (encode_times(&samples), samples.last().copied(), samples.len())
        };
        HealthRow {
            id: Some(format!("{SERVER}:{name}")),
            source: Some(SERVER.into()),
            series: Some(name.into()),
            count: Some(count as i64),
            latest: latest.map(iso),
            data: Some(data),
        }
    }
}

impl Default for HealthRecorder {
    fn default() -> Self {
        HealthRecorder::new()
    }
}

/// `at_ms` as ISO 8601 UTC, to the millisecond, with a "Z".
pub fn iso(at_ms: i64) -> String {
    let moment: DateTime<Utc> = DateTime::from_timestamp_millis(at_ms).unwrap_or_default();
    moment.format("%Y-%m-%dT%H:%M:%S%.3fZ").to_string()
}

// -- the tab ---------------------------------------------------------------------

#[derive(Clone)]
pub struct HealthTab {
    sheet: RowSheet,
}

impl HealthTab {
    pub fn ensure(sheets: &SheetsClient, spreadsheet_id: &str) -> anyhow::Result<HealthTab> {
        let _held = HEALTH_TAB_LOCK.hold();
        Ok(HealthTab { sheet: RowSheet::ensure(sheets, spreadsheet_id, HEALTH.0, HEALTH.1, HEALTH_ROW, &["id", "source", "series", "data"])? })
    }

    pub fn read(&self) -> anyhow::Result<Vec<HealthRow>> {
        self.sheet.read_items()
    }

    /// Put `changed` in place of the rows with their ids, adding new ones.
    pub fn write(&self, changed: &[HealthRow]) -> anyhow::Result<()> {
        let _held = HEALTH_TAB_LOCK.hold();
        let mut all: Vec<HealthRow> = Vec::new();
        for row in self.read()?.into_iter().chain(changed.iter().cloned()) {
            all.retain(|r| r.id != row.id);
            all.push(row);
        }
        all.sort_by(|a, b| {
            let key = |r: &HealthRow| (r.source.clone().unwrap_or_default(), r.series.clone().unwrap_or_default());
            key(a).cmp(&key(b))
        });
        self.sheet.write_items(&all)
    }
}

/// Writes the recorder's changes to the Health tab from a thread of its
/// own: opens the tab, takes in what it kept, records this start, then
/// writes what changed every FLUSH_SECONDS, and once more on exit.
pub struct HealthWriter {
    recorder: Arc<crate::app::App>,
    tab: Mutex<Option<HealthTab>>,
    stop: (Mutex<bool>, Condvar),
    flushing: Mutex<()>,
    pub loaded: (Mutex<bool>, Condvar),
}

impl HealthWriter {
    pub fn start(app: Arc<crate::app::App>) -> Arc<HealthWriter> {
        app.health.record_restart();
        let writer = Arc::new(HealthWriter {
            recorder: app,
            tab: Mutex::new(None),
            stop: (Mutex::new(false), Condvar::new()),
            flushing: Mutex::new(()),
            loaded: (Mutex::new(false), Condvar::new()),
        });
        let running = writer.clone();
        std::thread::Builder::new().name("health-writer".into()).spawn(move || running.run()).expect("starting the health writer");
        writer
    }

    fn run(&self) {
        self.load();
        loop {
            let (lock, wake) = &self.stop;
            let stopped = lock.lock().unwrap_or_else(|e| e.into_inner());
            let (stopped, _) = wake.wait_timeout(stopped, std::time::Duration::from_secs(FLUSH_SECONDS)).unwrap_or_else(|e| e.into_inner());
            if *stopped {
                return;
            }
            drop(stopped);
            if self.tab.lock().unwrap_or_else(|e| e.into_inner()).is_none() {
                self.load();
            } else {
                self.flush();
            }
        }
    }

    fn load(&self) {
        let opened = (|| -> anyhow::Result<HealthTab> {
            let tab = self.recorder.health_tab()?;
            self.recorder.health.merge(&tab.read()?);
            Ok(tab)
        })();
        match opened {
            Ok(tab) => *self.tab.lock().unwrap_or_else(|e| e.into_inner()) = Some(tab),
            Err(error) => tracing::error!("health: couldn't read the Health tab: {error:?}"),
        }
        let (lock, done) = &self.loaded;
        *lock.lock().unwrap_or_else(|e| e.into_inner()) = true;
        done.notify_all();
        self.flush();
    }

    pub fn wait_loaded(&self, seconds: u64) {
        let (lock, done) = &self.loaded;
        let loaded = lock.lock().unwrap_or_else(|e| e.into_inner());
        let _ = done.wait_timeout_while(loaded, std::time::Duration::from_secs(seconds), |l| !*l);
    }

    pub fn flush(&self) {
        let _flushing = self.flushing.lock().unwrap_or_else(|e| e.into_inner());
        let Some(tab) = self.tab.lock().unwrap_or_else(|e| e.into_inner()).clone() else { return };
        let Some(changed) = self.recorder.health.take_dirty() else { return };
        if let Err(error) = tab.write(&changed) {
            tracing::error!("health: couldn't write the Health tab: {error:?}");
            self.recorder.health.mark_dirty(&changed);
        }
    }

    /// Write what's left, then stop.
    pub fn stop(&self) {
        let (lock, wake) = &self.stop;
        *lock.lock().unwrap_or_else(|e| e.into_inner()) = true;
        wake.notify_all();
        self.flush();
    }
}

// -- reading it back ---------------------------------------------------------------

/// Python's `round()`: halves to even.
fn round_half_even(x: f64) -> f64 {
    let rounded = x.round();
    if (x - x.trunc()).abs() == 0.5 {
        2.0 * (x / 2.0).round()
    } else {
        rounded
    }
}

fn percentile(values: &[i64], q: f64) -> i64 {
    let mut ordered = values.to_vec();
    ordered.sort();
    let index = round_half_even(q * (ordered.len() as f64 - 1.0)).max(0.0) as usize;
    ordered[index.min(ordered.len() - 1)]
}

/// The median, 95th percentile and slowest, in ms.
pub fn latency_summary(values: &[i64]) -> Option<Value> {
    if values.is_empty() {
        return None;
    }
    let mut ordered = values.to_vec();
    ordered.sort();
    let n = ordered.len();
    let median = if n % 2 == 1 { ordered[n / 2] as f64 } else { (ordered[n / 2 - 1] + ordered[n / 2]) as f64 / 2.0 };
    Some(json!({ "median_ms": median.trunc() as i64, "p95_ms": percentile(values, 0.95), "max_ms": ordered[n - 1] }))
}

/// Python's `round(x, 1)`, near enough for a memory figure.
pub fn round1(x: f64) -> f64 {
    round_half_even(x * 10.0) / 10.0
}

#[cfg(test)]
#[path = "health_tests.rs"]
mod recorder_tests;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trips_the_encoding() {
        let calls = vec![
            CallSample { at_ms: 1_000, work_ms: 12, total_ms: Some(15), ok: true },
            CallSample { at_ms: 2_500, work_ms: 30, total_ms: None, ok: false },
        ];
        assert_eq!(decode_calls(&encode_calls(&calls)).unwrap(), calls);
        assert_eq!(decode_times(&encode_times(&[5, 3, 9])).unwrap(), vec![5, 3, 9]);
    }

    #[test]
    fn reads_what_python_wrote() {
        // encode_times([1, 2, 3]) as the Python server wrote it.
        assert_eq!(decode_times("1eNpjZmJiAgAAHAAK").unwrap(), vec![1, 2, 3]);
    }
}
