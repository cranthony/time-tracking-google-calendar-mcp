//! How close the server's estimates of what it keeps (google::kept for
//! Sheet tabs, google::synced for the calendar) come to what it really
//! allocates -- measured by a counting allocator, which this test program
//! installs for itself alone (so the unit tests aren't slowed or skewed).
//!
//! Each cache is built as the server builds it: the API's JSON parsed,
//! then what's kept copied out of it into the real store, the rest
//! dropped. What it allocates is counted as glibc sets memory aside for
//! it on Linux, where the server runs: each block's size plus an 8-byte
//! header, rounded up to 16 bytes, 32 at least.
//!
//! One test, run alone: the allocator counts the whole process.

use serde_json::{json, Value};
use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicI64, Ordering};
use time_tracker::google::{kept, synced};

struct Counting;

static HELD: AtomicI64 = AtomicI64::new(0);

/// What glibc's malloc sets aside for a block of `size` bytes.
fn footprint(size: usize) -> i64 {
    (((size + 8 + 15) & !15).max(32)) as i64
}

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, layout: Layout) -> *mut u8 {
        HELD.fetch_add(footprint(layout.size()), Ordering::SeqCst);
        unsafe { System.alloc(layout) }
    }

    unsafe fn alloc_zeroed(&self, layout: Layout) -> *mut u8 {
        HELD.fetch_add(footprint(layout.size()), Ordering::SeqCst);
        unsafe { System.alloc_zeroed(layout) }
    }

    unsafe fn dealloc(&self, ptr: *mut u8, layout: Layout) {
        HELD.fetch_sub(footprint(layout.size()), Ordering::SeqCst);
        unsafe { System.dealloc(ptr, layout) }
    }

    unsafe fn realloc(&self, ptr: *mut u8, layout: Layout, new_size: usize) -> *mut u8 {
        HELD.fetch_add(footprint(new_size) - footprint(layout.size()), Ordering::SeqCst);
        unsafe { System.realloc(ptr, layout, new_size) }
    }
}

#[global_allocator]
static ALLOCATOR: Counting = Counting;

fn held() -> i64 {
    HELD.load(Ordering::SeqCst)
}

/// A deterministic stand-in for randomness: `n`'s `i`th pick below `below`.
fn pick(n: usize, i: usize, below: usize) -> usize {
    (n.wrapping_mul(2654435761).wrapping_add(i.wrapping_mul(40503)) >> 7) % below
}

fn text(n: usize, i: usize, longest: usize) -> String {
    let words = ["walk", "lunch", "with", "a", "friend", "at", "the", "cafe", "finished", "report", "gym", "call", "notes", "reading"];
    let mut out = String::new();
    let want = pick(n, i, longest + 1);
    let mut k = 0;
    while out.len() < want {
        if !out.is_empty() {
            out.push(' ');
        }
        out.push_str(words[pick(n, i + k, words.len())]);
        k += 1;
    }
    out
}

/// A Sheets values response for a tab of `rows` rows like the server's
/// item tabs: a header, then ids, names, numbers, JSON lists and notes,
/// some cells empty.
fn tab_response(rows: usize) -> String {
    let header = ["id", "name", "status", "group_id", "priority", "note", "circles", "traits", "what_matters", "version", "label_id", "commute"];
    let mut values = vec![json!(header)];
    for r in 0..rows {
        let cell = |c: usize| -> String {
            match c {
                0 => format!("{:06x}", pick(r, 0, 1 << 24)),
                1 => text(r, 1, 24),
                2 => ["active", "archived", "off"][pick(r, 2, 3)].to_string(),
                4 | 9 => pick(r, c, 9).to_string(),
                6 | 7 => if pick(r, c, 3) == 0 { String::new() } else { format!("[\"{}\", \"{}\"]", text(r, c, 8), text(r, c + 1, 8)) },
                10 => format!("{:08x}-{:04x}-5{:03x}-8{:03x}-{:012x}", pick(r, 10, 1 << 30), pick(r, 11, 1 << 16), pick(r, 12, 4096), pick(r, 13, 4096), pick(r, 14, 1 << 30)),
                _ => if pick(r, c, 2) == 0 { String::new() } else { text(r, c, 60) },
            }
        };
        let mut row: Vec<String> = (0..header.len()).map(cell).collect();
        while row.last().is_some_and(String::is_empty) {
            row.pop();
        }
        values.push(json!(row));
    }
    json!({"range": "Actions!A1:Z", "majorDimension": "ROWS", "values": values}).to_string()
}

/// A calendar sync response of `count` events like the server's: some
/// series masters and exceptions, private properties (priority, actions,
/// facts and judgments as JSON), descriptions of every length.
fn events_response(count: usize) -> String {
    let items: Vec<Value> = (0..count)
        .map(|n| {
            let id = format!("{:026x}", (n as u128).wrapping_mul(0x9e37_79b9_7f4a_7c15_f39c_c060_5ced_c834));
            let mut private = serde_json::Map::new();
            private.insert("cascading-time-tracker-priority".into(), json!(pick(n, 1, 5).to_string()));
            if pick(n, 2, 3) > 0 {
                private.insert("cascading-time-tracker-action_ids".into(), json!(format!("{:06x} {:06x}", pick(n, 3, 1 << 24), pick(n, 4, 1 << 24))));
            }
            if pick(n, 5, 2) == 0 {
                let facts = json!({"location_ids": [format!("{:06x}", pick(n, 6, 1 << 24))], "with_ids": [format!("{:06x}", pick(n, 7, 1 << 24))], "notes": {"self": text(n, 8, 120)}});
                private.insert("cascading-time-tracker-facts_0".into(), json!(facts.to_string()));
            }
            if pick(n, 9, 4) == 0 {
                let judgments = json!({"self": {"close": {"connection": {"rating": 1, "reasoning": text(n, 10, 200), "scale": 2}}}});
                private.insert("cascading-time-tracker-judgments_0".into(), json!(judgments.to_string()));
            }
            let mut event = json!({
                "kind": "calendar#event",
                "etag": format!("\"{}\"", 1_000_000_000_000_000u64 + n as u64),
                "id": id,
                "status": if pick(n, 11, 20) == 0 { "cancelled" } else { "confirmed" },
                // As long as a real link, made up.
                "htmlLink": format!("https://www.google.com/calendar/event?eid={}", "x".repeat(150)),
                "created": "2026-09-08T12:50:22.000Z",
                "updated": "2026-10-15T04:45:46.090Z",
                "summary": text(n, 12, 30),
                "creator": {"email": "someone@example.com"},
                "organizer": {"email": format!("{}@group.calendar.google.com", "0".repeat(64)), "displayName": "Time Tracker", "self": true},
                "start": {"dateTime": "2026-10-09T08:00:00-04:00", "timeZone": "America/New_York"},
                "end": {"dateTime": "2026-10-09T08:15:00-04:00", "timeZone": "America/New_York"},
                "iCalUID": format!("{id}@google.com"),
                "sequence": pick(n, 13, 3),
                "extendedProperties": {"private": private},
                "reminders": {"useDefault": true},
                "eventType": "default",
            });
            if pick(n, 14, 3) > 0 {
                event["description"] = json!(text(n, 15, 400));
            }
            match pick(n, 16, 25) {
                0 => event["recurrence"] = json!(["RRULE:FREQ=DAILY;UNTIL=20261017T035959Z"]),
                1 | 2 => {
                    event["recurringEventId"] = json!("seriesseriesseriesseries01");
                    event["originalStartTime"] = json!({"dateTime": "2026-10-09T08:00:00-04:00", "timeZone": "America/New_York"});
                }
                _ => {}
            }
            event
        })
        .collect();
    json!({"kind": "calendar#events", "timeZone": "America/New_York", "nextSyncToken": "CPDAlvWDx70CEPDAlvWDx70CGAU=", "items": items}).to_string()
}

/// How far `estimate` is from `actual`, as a fraction of `actual`.
fn off(estimate: usize, actual: i64) -> f64 {
    (estimate as f64 - actual as f64) / actual as f64
}

/// Each estimate within this much of what's really held. (When written:
/// tabs exactly, the calendar within 0.3% -- its tree's nodes, how full
/// they are, being the one guess.)
const TOLERANCE: f64 = 0.05;

#[test]
fn the_estimates_of_what_is_kept_are_close_to_what_is_allocated() {
    let mut report = Vec::new();

    // A Sheet tab, kept as SheetsClient keeps one it reads.
    for rows in [20, 300, 2000] {
        let text = tab_response(rows);
        let root = format!("memory-test-sheets-{rows}");
        kept::with(&root, "sheet", |tabs| {
            tabs.entry(1).or_default();
        });
        let before = held();
        let response: Value = serde_json::from_str(&text).unwrap();
        let kept_rows: Vec<Vec<String>> = response["values"]
            .as_array()
            .unwrap()
            .iter()
            .map(|row| row.as_array().unwrap().iter().map(|c| c.as_str().unwrap().to_string()).collect())
            .collect();
        drop(response);
        kept::with(&root, "sheet", |tabs| tabs.get_mut(&1).unwrap().rows = Some(kept_rows));
        let actual = held() - before;
        let estimate: usize = kept::sizes(&root).iter().map(|(_, bytes)| bytes).sum();
        report.push((format!("a tab of {rows} rows"), estimate, actual));
    }

    // The calendar, kept as a sync keeps it.
    for count in [50, 2000] {
        let text = events_response(count);
        let root = format!("memory-test-calendar-{count}");
        synced::with(&root, "cal", |_| ());
        let before = held();
        let response: Value = serde_json::from_str(&text).unwrap();
        let items: Vec<Value> = response.get("items").and_then(Value::as_array).cloned().unwrap_or_default();
        drop(response);
        synced::with(&root, "cal", |store| {
            for item in items {
                if let Some(id) = item["id"].as_str() {
                    store.events.insert(id.to_string(), item);
                }
            }
        });
        let actual = held() - before;
        let estimate = synced::size(&root);
        report.push((format!("a calendar of {count} events"), estimate, actual));
    }

    for (what, estimate, actual) in &report {
        println!("{what}: estimated {estimate} bytes, allocated {actual}: {:+.1}%", off(*estimate, *actual) * 100.0);
    }
    for (what, estimate, actual) in &report {
        assert!(off(*estimate, *actual).abs() <= TOLERANCE, "{what}: estimated {estimate}, allocated {actual}");
    }
}
