//! The README's table of contents, kept in step with its headings, and its
//! links to its own headings, kept pointing at one.

use std::path::PathBuf;

const START: &str = "<!-- toc: written by `CTT_UPDATE_README=1 cargo test` from the headings below; don't edit by hand -->";
const END: &str = "<!-- /toc -->";

fn readme_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../README.md")
}

fn readme() -> String {
    std::fs::read_to_string(readme_path()).unwrap().replace("\r\n", "\n")
}

/// The headings of `text` below its title (levels 2 and 3), outside code
/// blocks: (level, title).
fn headings(text: &str) -> Vec<(usize, String)> {
    let mut fence: Option<String> = None;
    let mut found = Vec::new();
    for line in text.lines() {
        let trimmed = line.trim_start();
        if trimmed.starts_with("```") || trimmed.starts_with("~~~") {
            let marker: String = trimmed.chars().take_while(|c| *c == '`' || *c == '~').collect();
            match &fence {
                Some(open) if marker.starts_with(open.as_str()) => fence = None,
                Some(_) => {}
                None => fence = Some(marker),
            }
            continue;
        }
        if fence.is_some() {
            continue;
        }
        let level = line.chars().take_while(|c| *c == '#').count();
        if (2..=3).contains(&level) && line[level..].starts_with(' ') {
            found.push((level, line[level..].trim().trim_end_matches('#').trim().to_string()));
        }
    }
    found
}

/// The anchor GitHub gives a heading: lowercase, its punctuation dropped
/// (but hyphens and underscores), spaces as hyphens.
fn slug(title: &str) -> String {
    title
        .to_lowercase()
        .chars()
        .filter(|c| c.is_alphanumeric() || *c == ' ' || *c == '-' || *c == '_')
        .map(|c| if c == ' ' { '-' } else { c })
        .collect()
}

/// Each heading's anchor, numbered as GitHub numbers a repeated one ("-1",
/// "-2", ...).
fn anchors(headings: &[(usize, String)]) -> Vec<String> {
    let mut seen: Vec<String> = Vec::new();
    headings
        .iter()
        .map(|(_, title)| {
            let base = slug(title);
            let repeats = seen.iter().filter(|s| **s == base).count();
            seen.push(base.clone());
            if repeats == 0 { base } else { format!("{base}-{repeats}") }
        })
        .collect()
}

/// The table of contents for `text`'s headings, between its markers.
fn toc(text: &str) -> String {
    let found = headings(text);
    let mut lines = vec![START.to_string(), String::new(), "**Contents**".to_string(), String::new()];
    for ((level, title), anchor) in found.iter().zip(anchors(&found)) {
        lines.push(format!("{}- [{title}](#{anchor})", "  ".repeat(level - 2)));
    }
    lines.push(String::new());
    lines.push(END.to_string());
    lines.join("\n")
}

/// `text` with its table of contents as its headings have it.
fn with_toc(text: &str) -> String {
    let start = text.find(START).expect("README.md has no table of contents marker: put START and END from readme_tests.rs where it goes");
    let end = text[start..].find(END).map(|e| start + e + END.len()).expect("README.md's table of contents has no end marker");
    format!("{}{}{}", &text[..start], toc(text), &text[end..])
}

#[test]
fn the_readmes_table_of_contents_follows_its_headings() {
    // A heading added, renamed or removed: CTT_UPDATE_README=1 cargo test,
    // and commit README.md.
    let text = readme();
    let updated = with_toc(&text);
    if std::env::var_os("CTT_UPDATE_README").is_some() {
        if updated != text {
            std::fs::write(readme_path(), &updated).unwrap();
        }
        return;
    }
    assert!(updated == text, "README.md's table of contents doesn't follow its headings: rerun with CTT_UPDATE_README=1 to write it");
}

#[test]
fn the_readmes_links_to_its_headings_reach_one() {
    let text = readme();
    let known = anchors(&headings(&text));
    let mut broken = Vec::new();
    for piece in text.split("](#").skip(1) {
        let anchor = piece.split(')').next().unwrap_or_default();
        if !known.iter().any(|k| k == anchor) {
            broken.push(anchor.to_string());
        }
    }
    assert!(broken.is_empty(), "README.md links to headings it doesn't have: {broken:?}");
}

#[test]
fn headings_are_slugged_as_github_does() {
    assert_eq!(slug("Without sign-in, behind a corporate proxy"), "without-sign-in-behind-a-corporate-proxy");
    assert_eq!(slug("People, circles and locations"), "people-circles-and-locations");
    assert_eq!(slug("Why `tools`, not resources?"), "why-tools-not-resources");
    let found = headings("# Title\n## A\n```bash\n# not a heading\n## nor this\n```\n### B b\n## A\n####### no\n");
    assert_eq!(found, vec![(2, "A".to_string()), (3, "B b".to_string()), (2, "A".to_string())]);
    assert_eq!(anchors(&found), vec!["a", "b-b", "a-1"]);
}
