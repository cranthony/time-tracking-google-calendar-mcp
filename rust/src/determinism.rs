//! The clock and the random ids, made fixed for conformance/ when asked --
//! exactly as utilities/determinism.py does, so both servers make the
//! same ids in the same order:
//!
//! - `now()` is the current time, or always `CTT_NOW` (ISO 8601 with an
//!   offset) if that's set.
//! - `random_token(alphabet, length)` is random, or with
//!   `CTT_DETERMINISTIC_IDS` set, token n (from 1, across the process) is
//!   the first `length` bytes of SHA-256("ctt-id:<n>"), each mapped to
//!   `alphabet[byte % len]`.

use chrono::{DateTime, FixedOffset, Utc};
use sha2::{Digest, Sha256};
use std::sync::atomic::{AtomicU64, Ordering};

static COUNTER: AtomicU64 = AtomicU64::new(1);

pub fn now() -> DateTime<FixedOffset> {
    if let Ok(fixed) = std::env::var("CTT_NOW") {
        if !fixed.is_empty() {
            if let Ok(moment) = DateTime::parse_from_rfc3339(&fixed) {
                return moment;
            }
        }
    }
    Utc::now().fixed_offset()
}

pub fn random_token(alphabet: &str, length: usize) -> String {
    let letters: Vec<char> = alphabet.chars().collect();
    if std::env::var("CTT_DETERMINISTIC_IDS").map(|v| !v.is_empty()).unwrap_or(false) {
        let n = COUNTER.fetch_add(1, Ordering::SeqCst);
        let digest = Sha256::digest(format!("ctt-id:{n}").as_bytes());
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
    fn the_fixed_sequence_matches_python() {
        // tests/test_determinism.py pins the same two.
        let letters: Vec<char> = "0123456789abcdefghijklmnopqrstuvwxyz".chars().collect();
        let token = |n: u64| -> String {
            let digest = Sha256::digest(format!("ctt-id:{n}").as_bytes());
            digest[..6].iter().map(|b| letters[*b as usize % 36]).collect()
        };
        assert_eq!(token(1), "4709iy");
        assert_eq!(token(2), "bcyx3f");
    }
}
