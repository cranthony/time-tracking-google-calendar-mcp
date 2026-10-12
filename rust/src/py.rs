//! Python's own ways of writing values, reproduced exactly: the Rust port
//! has to send Google, and say to clients, byte for byte what the Python
//! server did (see conformance/). Error messages quote names with
//! `repr()` (`{name!r}`), request bodies carry times as
//! `datetime.isoformat()`, and JSON cells are `json.dumps(...)`.

use chrono::{DateTime, FixedOffset, Offset, Timelike};
use serde_json::Value;
use std::fmt::Write;

/// `repr(text)`: single-quoted unless it holds a `'` and no `"`, with
/// backslashes, the quote and non-printable characters escaped.
pub fn repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for c in text.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if is_printable(c) => out.push(c),
            c => {
                let code = c as u32;
                if code < 0x100 {
                    let _ = write!(out, "\\x{code:02x}");
                } else if code < 0x10000 {
                    let _ = write!(out, "\\u{code:04x}");
                } else {
                    let _ = write!(out, "\\U{code:08x}");
                }
            }
        }
    }
    out.push(quote);
    out
}

/// `str.isprintable()` for one character, as near as needs be: control
/// and format characters, and separators other than the space, aren't.
fn is_printable(c: char) -> bool {
    let code = c as u32;
    if c == ' ' {
        return true;
    }
    !(code < 0x20
        || (0x7f..=0xa0).contains(&code)
        || code == 0xad
        || (0x2000..=0x200f).contains(&code)
        || (0x2028..=0x202f).contains(&code)
        || (0x205f..=0x206f).contains(&code)
        || code == 0x3000
        || code == 0xfeff
        || (0xfff9..=0xfffb).contains(&code)
        || (0xd800..=0xdfff).contains(&code)
        || (0xe000..=0xf8ff).contains(&code))
}

/// `repr()` of a list of strings: `['a', 'b']`.
pub fn repr_list<S: AsRef<str>>(items: &[S]) -> String {
    let inner: Vec<String> = items.iter().map(|s| repr(s.as_ref())).collect();
    format!("[{}]", inner.join(", "))
}

/// `datetime.isoformat()` of an aware datetime: seconds always,
/// microseconds only when there are some, and the offset as `+HH:MM`
/// (with seconds only if it has them).
pub fn isoformat(moment: &DateTime<FixedOffset>) -> String {
    let mut out = moment.format("%Y-%m-%dT%H:%M:%S").to_string();
    let micros = moment.nanosecond() / 1000;
    if micros != 0 {
        let _ = write!(out, ".{micros:06}");
    }
    out.push_str(&offset_text(moment.offset().fix().local_minus_utc()));
    out
}

fn offset_text(seconds: i32) -> String {
    let sign = if seconds < 0 { '-' } else { '+' };
    let total = seconds.abs();
    let (hours, minutes, secs) = (total / 3600, (total % 3600) / 60, total % 60);
    if secs != 0 {
        format!("{sign}{hours:02}:{minutes:02}:{secs:02}")
    } else {
        format!("{sign}{hours:02}:{minutes:02}")
    }
}

/// `json.dumps(value, separators=(",", ":"), ensure_ascii=False)`: compact,
/// keys in insertion order, non-ASCII kept as it is.
pub fn json_compact(value: &Value) -> String {
    let mut out = String::new();
    write_json(&mut out, value, false, ",", ":");
    out
}

/// `json.dumps(value, separators=(",", ":"))`: compact, non-ASCII escaped
/// as `\uXXXX` (Python's default, ensure_ascii=True).
pub fn json_compact_ascii(value: &Value) -> String {
    let mut out = String::new();
    write_json(&mut out, value, true, ",", ":");
    out
}

/// `json.dumps(value)`: `", "` and `": "` separators, ASCII only.
pub fn json_default(value: &Value) -> String {
    let mut out = String::new();
    write_json(&mut out, value, true, ", ", ": ");
    out
}

/// A tool result as the Python SDK writes it as text (pydantic's
/// `to_json(value, indent=2)`): two-space indents, keys in order,
/// non-ASCII kept, empty lists and objects as `[]` and `{}`.
pub fn json_pretty(value: &Value) -> String {
    let mut out = String::new();
    write_pretty(&mut out, value, 0);
    out
}

fn write_pretty(out: &mut String, value: &Value, depth: usize) {
    match value {
        Value::Array(items) if !items.is_empty() => {
            out.push_str("[\n");
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(",\n");
                }
                out.push_str(&"  ".repeat(depth + 1));
                write_pretty(out, item, depth + 1);
            }
            out.push('\n');
            out.push_str(&"  ".repeat(depth));
            out.push(']');
        }
        Value::Object(map) if !map.is_empty() => {
            out.push_str("{\n");
            for (i, (key, item)) in map.iter().enumerate() {
                if i > 0 {
                    out.push_str(",\n");
                }
                out.push_str(&"  ".repeat(depth + 1));
                write_json_string(out, key, false);
                out.push_str(": ");
                write_pretty(out, item, depth + 1);
            }
            out.push('\n');
            out.push_str(&"  ".repeat(depth));
            out.push('}');
        }
        other => write_json(out, other, false, ",", ":"),
    }
}

fn write_json(out: &mut String, value: &Value, ascii: bool, item_sep: &str, key_sep: &str) {
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&python_number(n)),
        Value::String(s) => write_json_string(out, s, ascii),
        Value::Array(items) => {
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(item_sep);
                }
                write_json(out, item, ascii, item_sep, key_sep);
            }
            out.push(']');
        }
        Value::Object(map) => {
            out.push('{');
            for (i, (key, item)) in map.iter().enumerate() {
                if i > 0 {
                    out.push_str(item_sep);
                }
                write_json_string(out, key, ascii);
                out.push_str(key_sep);
                write_json(out, item, ascii, item_sep, key_sep);
            }
            out.push('}');
        }
    }
}

/// A JSON number as Python writes it: integers as digits, floats with a
/// `.0` when whole (`repr(float)`).
pub fn python_number(n: &serde_json::Number) -> String {
    if let Some(i) = n.as_i64() {
        return i.to_string();
    }
    if let Some(u) = n.as_u64() {
        return u.to_string();
    }
    float_repr(n.as_f64().unwrap_or(0.0))
}

/// `repr(float)`: the shortest text that reads back as the same float,
/// with `.0` on whole numbers and exponents as Python writes them.
pub fn float_repr(value: f64) -> String {
    if value.is_nan() {
        return "NaN".into();
    }
    if value.is_infinite() {
        return if value > 0.0 { "Infinity".into() } else { "-Infinity".into() };
    }
    let abs = value.abs();
    if abs != 0.0 && !(1e-4..1e16).contains(&abs) {
        // Python switches to exponent notation outside this range.
        let text = format!("{value:e}");
        let (mantissa, exponent) = text.split_once('e').unwrap();
        let exp: i32 = exponent.parse().unwrap();
        let sign = if exp < 0 { '-' } else { '+' };
        return format!("{mantissa}e{sign}{:02}", exp.abs());
    }
    let text = format!("{value}");
    if text.contains('.') {
        text
    } else {
        format!("{text}.0")
    }
}

fn write_json_string(out: &mut String, s: &str, ascii: bool) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                let _ = write!(out, "\\u{:04x}", c as u32);
            }
            c if ascii && (c as u32) > 0x7e => {
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    let _ = write!(out, "\\u{unit:04x}");
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// `sorted(set_of_strings)` printed as Python would: `['a', 'b']`.
pub fn sorted_repr<S: AsRef<str>>(items: impl IntoIterator<Item = S>) -> String {
    let mut all: Vec<String> = items.into_iter().map(|s| s.as_ref().to_string()).collect();
    all.sort();
    all.dedup();
    repr_list(&all)
}

#[cfg(test)]
mod tests {
    use super::*;
    use chrono::TimeZone;

    #[test]
    fn repr_quotes_like_python() {
        assert_eq!(repr("abc"), "'abc'");
        assert_eq!(repr("it's"), "\"it's\"");
        assert_eq!(repr("it's \"x\""), "'it\\'s \"x\"'");
        assert_eq!(repr("a\nb\\c"), "'a\\nb\\\\c'");
        assert_eq!(repr("café ☕"), "'café ☕'");
        assert_eq!(repr("\u{7}"), "'\\x07'");
    }

    #[test]
    fn isoformat_like_python() {
        let tz = FixedOffset::west_opt(4 * 3600).unwrap();
        let moment = tz.with_ymd_and_hms(2026, 10, 7, 9, 0, 0).unwrap();
        assert_eq!(isoformat(&moment), "2026-10-07T09:00:00-04:00");
        let utc = FixedOffset::east_opt(0).unwrap().with_ymd_and_hms(2026, 1, 2, 3, 4, 5).unwrap();
        assert_eq!(isoformat(&utc), "2026-01-02T03:04:05+00:00");
        let micro = utc + chrono::Duration::microseconds(1500);
        assert_eq!(isoformat(&micro), "2026-01-02T03:04:05.001500+00:00");
    }

    #[test]
    fn json_like_python() {
        let value = serde_json::json!({"b": [1, 2.0, "é"], "a": null});
        assert_eq!(json_compact(&value), "{\"b\":[1,2.0,\"é\"],\"a\":null}");
        assert_eq!(json_compact_ascii(&value), "{\"b\":[1,2.0,\"\\u00e9\"],\"a\":null}");
        assert_eq!(json_default(&value), "{\"b\": [1, 2.0, \"\\u00e9\"], \"a\": null}");
        assert_eq!(float_repr(0.1), "0.1");
        assert_eq!(float_repr(1e20), "1e+20");
        assert_eq!(float_repr(1.5e-7), "1.5e-07");
    }
}
