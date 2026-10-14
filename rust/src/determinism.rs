//! The clock and the random ids, made fixed for conformance/ when asked, so
//! the server makes the same ids in the same order every run:
//!
//! - `now()` is the current time, or always `TIME_TRACKER_NOW` (ISO 8601 with an
//!   offset) if that's set.
//! - `random_token(alphabet, length)` is random, or with
//!   `TIME_TRACKER_DETERMINISTIC_IDS` set, token n (from 1, across the process) is
//!   the first `length` bytes of SHA-256("tt-id:<n>"), each mapped to
//!   `alphabet[byte % len]`.

use chrono::{DateTime, FixedOffset, Utc};
use sha2::{Digest, Sha256};
use std::sync::atomic::{AtomicU64, Ordering};

static COUNTER: AtomicU64 = AtomicU64::new(1);

#[cfg(test)]
thread_local! {
    /// The unit tests' clock: env vars are process-wide, and tests run in
    /// parallel, so each test thread sets its own.
    pub static TEST_NOW: std::cell::Cell<Option<DateTime<FixedOffset>>> = const { std::cell::Cell::new(None) };
}

pub fn now() -> DateTime<FixedOffset> {
    #[cfg(test)]
    if let Some(now) = TEST_NOW.with(std::cell::Cell::get) {
        return now;
    }
    now_from(std::env::var("TIME_TRACKER_NOW").ok())
}

/// `now()`, with `TIME_TRACKER_NOW` as given.
fn now_from(fixed: Option<String>) -> DateTime<FixedOffset> {
    if let Some(moment) = fixed.filter(|f| !f.is_empty()).and_then(|f| DateTime::parse_from_rfc3339(&f).ok()) {
        return moment;
    }
    Utc::now().fixed_offset()
}

pub fn random_token(alphabet: &str, length: usize) -> String {
    let letters: Vec<char> = alphabet.chars().collect();
    if std::env::var("TIME_TRACKER_DETERMINISTIC_IDS").map(|v| !v.is_empty()).unwrap_or(false) {
        let n = COUNTER.fetch_add(1, Ordering::SeqCst);
        let digest = Sha256::digest(format!("tt-id:{n}").as_bytes());
        return digest[..length].iter().map(|b| letters[*b as usize % letters.len()]).collect();
    }
    let mut bytes = vec![0u8; length];
    getrandom(&mut bytes);
    bytes.iter().map(|b| letters[*b as usize % letters.len()]).collect()
}

/// Random bytes, from the OS. (uuid's v4 generator is already a
/// dependency and draws from the same source.)
fn getrandom(buf: &mut [u8]) {
    let mut filled = 0;
    while filled < buf.len() {
        let id = uuid::Uuid::new_v4();
        for b in id.as_bytes() {
            if filled < buf.len() {
                buf[filled] = *b;
                filled += 1;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_fixed_sequence_is_pinned() {
        let letters: Vec<char> = "0123456789abcdefghijklmnopqrstuvwxyz".chars().collect();
        let token = |n: u64| -> String {
            let digest = Sha256::digest(format!("tt-id:{n}").as_bytes());
            digest[..6].iter().map(|b| letters[*b as usize % 36]).collect()
        };
        assert_eq!(token(1), "co2yzn");
        assert_eq!(token(2), "3dnjav");
    }

    #[test]
    fn now_is_the_clock_unless_fixed() {
        let before = Utc::now();
        let now = now_from(None);
        assert!(before <= now && now <= Utc::now());
        assert_eq!(now_from(Some("2026-10-07T15:30:00-04:00".into())), DateTime::parse_from_rfc3339("2026-10-07T19:30:00Z").unwrap());
    }

    #[test]
    fn random_tokens_are_random_unless_deterministic() {
        // No test sets TIME_TRACKER_DETERMINISTIC_IDS: it's process-wide.
        let alphabet = "0123456789abcdefghijklmnopqrstuvwxyz";
        let tokens: std::collections::BTreeSet<String> = (0..20).map(|_| random_token(alphabet, 6)).collect();
        assert_eq!(tokens.len(), 20);
        assert!(tokens.iter().all(|t| t.len() == 6 && t.chars().all(|c| alphabet.contains(c))));
    }
}
