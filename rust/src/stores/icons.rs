//! A trait's icon: SVG markup the app draws, single-colour so it can tint
//! it (black or white on an event's block, the theme's text colour
//! elsewhere). Checked before it's kept, so what the app is given can't
//! run anything, load anything or bring its own colours.

/// The most an icon's markup can be, in characters.
pub const MAX_ICON_CHARS: usize = 8192;

/// The colours an icon's fill and stroke can be: the one the app tints it
/// with, or none.
const PAINTS: [&str; 2] = ["currentColor", "none"];

/// Elements an icon can't have: scripts, other documents, pictures,
/// styles, links and animations.
const FORBIDDEN_ELEMENTS: [&str; 11] =
    ["script", "foreignobject", "image", "style", "a", "animate", "animatemotion", "animatetransform", "animatecolor", "set", "discard"];

/// What's wrong with `icon` as a trait's icon, each said as following
/// "its icon": nothing if it's well formed.
pub fn icon_problems(icon: &str) -> Vec<String> {
    if icon.trim().is_empty() {
        return vec!["must be SVG markup (clear it with clear_fields to have none)".into()];
    }
    let length = icon.chars().count();
    if length > MAX_ICON_CHARS {
        return vec![format!("is {length} characters; it can be at most {MAX_ICON_CHARS}")];
    }
    let document = match roxmltree::Document::parse(icon) {
        Ok(document) => document,
        Err(error) => return vec![format!("isn't well-formed XML: {error}")],
    };
    let root = document.root_element();
    if root.tag_name().name() != "svg" {
        return vec![format!("must be an <svg> element, not <{}>", root.tag_name().name())];
    }
    let mut problems = Vec::new();
    if root.attributes().all(|a| a.name() != "viewBox") {
        problems.push("needs a viewBox on its <svg>, so the app can scale it".to_string());
    }
    for node in root.descendants().filter(|n| n.is_element()) {
        let element = node.tag_name().name();
        if FORBIDDEN_ELEMENTS.contains(&element.to_lowercase().as_str()) {
            problems.push(format!("can't have <{element}>: no scripts, foreign objects, images, styles, links or animations"));
        }
        for attribute in node.attributes() {
            let (name, value) = (attribute.name(), attribute.value());
            if name.to_lowercase().starts_with("on") {
                problems.push(format!("can't have the event handler {name} (on <{element}>)"));
            } else if name == "href" && !value.trim_start().starts_with('#') {
                problems.push(format!("can only link within itself, to a #fragment: <{element}>'s href is {}", quoted(value)));
            } else if name == "fill" || name == "stroke" {
                problems.extend(paint_problem(element, name, value));
            } else if name == "style" {
                for (property, value) in declarations(value) {
                    if property == "fill" || property == "stroke" {
                        problems.extend(paint_problem(element, &property, &value));
                    }
                }
            }
            if let Some(url) = outside_url(value) {
                problems.push(format!("can only use url(#...), within itself: <{element}>'s {name} has {}", quoted(&url)));
            }
        }
    }
    problems.dedup();
    problems
}

/// What's wrong with `value` as the fill or stroke of an `element`.
fn paint_problem(element: &str, property: &str, value: &str) -> Option<String> {
    let value = value.trim().trim_end_matches("!important").trim();
    (!PAINTS.iter().any(|p| p.eq_ignore_ascii_case(value))).then(|| {
        format!("must be one colour, so the app can tint it: <{element}>'s {property} is {}; use currentColor or none", quoted(value))
    })
}

/// An inline style's declarations, each (property in lowercase, value).
fn declarations(style: &str) -> Vec<(String, String)> {
    style
        .split(';')
        .filter_map(|d| d.split_once(':'))
        .map(|(property, value)| (property.trim().to_lowercase(), value.trim().to_string()))
        .collect()
}

/// The first url(...) in `value` that doesn't point within the icon
/// (url(#...)), if there's one.
fn outside_url(value: &str) -> Option<String> {
    let lower = value.to_ascii_lowercase();
    let mut from = 0;
    while let Some(at) = lower[from..].find("url(").map(|i| i + from) {
        let rest = value[at + 4..].trim_start().trim_start_matches(['"', '\'']);
        if !rest.starts_with('#') {
            let end = value[at..].find(')').map(|i| at + i + 1).unwrap_or(value.len());
            return Some(value[at..end].to_string());
        }
        from = at + 4;
    }
    None
}

fn quoted(value: &str) -> String {
    let short: String = value.chars().take(60).collect();
    let more = if short.len() < value.len() { "..." } else { "" };
    format!("\"{short}{more}\"")
}

#[cfg(test)]
#[path = "icons_tests.rs"]
mod tests;
