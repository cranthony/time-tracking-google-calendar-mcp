//! `load_dotenv()`: settings from a `.env` file in the working directory,
//! for running the server locally (see .env.example). A variable already
//! set in the environment wins, and a missing file is no error -- deployed,
//! the environment is set for real.

use std::path::Path;

pub fn load(path: &Path) {
    let Ok(text) = std::fs::read_to_string(path) else { return };
    for (key, value) in parse(&text) {
        if std::env::var_os(&key).is_none() {
            std::env::set_var(key, value);
        }
    }
}

/// Each `KEY=value` line: blank lines and `#` comments skipped, an
/// `export ` prefix allowed, a value in single or double quotes taken as
/// quoted, and an unquoted one cut at an inline ` #` comment.
pub fn parse(text: &str) -> Vec<(String, String)> {
    let mut out = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let line = line.strip_prefix("export ").unwrap_or(line);
        let Some((key, value)) = line.split_once('=') else { continue };
        let key = key.trim();
        if key.is_empty() {
            continue;
        }
        let value = value.trim();
        let value = match value.chars().next() {
            Some(quote @ ('"' | '\'')) => match closing(&value[1..], quote) {
                Some(end) => {
                    let inner = &value[1..1 + end];
                    if quote == '"' { inner.replace("\\n", "\n").replace("\\\"", "\"") } else { inner.to_string() }
                }
                None => value.to_string(),
            },
            _ => match value.find(" #") {
                Some(comment) => value[..comment].trim_end().to_string(),
                None => value.to_string(),
            },
        };
        out.push((key.to_string(), value));
    }
    out
}

/// Where `quote` closes `text`: the first not escaped with a backslash
/// (in double quotes; single quotes take everything literally).
fn closing(text: &str, quote: char) -> Option<usize> {
    let mut escaped = false;
    for (i, c) in text.char_indices() {
        if escaped {
            escaped = false;
        } else if c == '\\' && quote == '"' {
            escaped = true;
        } else if c == quote {
            return Some(i);
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::parse;

    #[test]
    fn reads_what_env_example_shows() {
        let parsed = parse(
            "# comment\n\nGOOGLE_CALENDAR_ID=abc@group.calendar.google.com\nexport A='x y' \nB=\"q\\\"r\"\nC=plain # note\n#   MCP_TRANSPORT=streamable-http\nEMPTY=\n",
        );
        let pairs: Vec<(&str, &str)> = parsed.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
        assert_eq!(
            pairs,
            vec![("GOOGLE_CALENDAR_ID", "abc@group.calendar.google.com"), ("A", "x y"), ("B", "q\"r"), ("C", "plain"), ("EMPTY", "")]
        );
    }
}
