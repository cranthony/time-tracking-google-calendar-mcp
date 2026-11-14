use super::*;

const STAR: &str = r#"<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2l3 7h7l-6 4 2 7-6-4-6 4 2-7-6-4h7z"/></svg>"#;

fn svg(inner: &str) -> String {
    format!(r#"<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" viewBox="0 0 24 24">{inner}</svg>"#)
}

fn says(icon: &str, pattern: &str) {
    let problems = icon_problems(icon);
    assert!(problems.iter().any(|p| regex::Regex::new(pattern).unwrap().is_match(p)), "{pattern:?} isn't in {problems:?}");
}

#[test]
fn a_single_colour_icon_is_well_formed() {
    assert_eq!(icon_problems(STAR), Vec::<String>::new());
    let outlined = svg(r##"<defs><clipPath id="c"><rect width="24" height="24"/></clipPath></defs><g clip-path="url(#c)" style="stroke: currentColor; fill:none"><circle cx="12" cy="12" r="9" stroke="currentColor"/><use href="#c"/><use xlink:href="#c"/></g>"##);
    assert_eq!(icon_problems(&outlined), Vec::<String>::new());
}

#[test]
fn blank_or_long_markup_is_refused() {
    says("  ", "must be SVG markup");
    let long = svg(&"<path d=\"M0 0\"/>".repeat(600));
    says(&long, "can be at most 8192");
}

#[test]
fn it_must_be_an_svg_with_a_view_box() {
    says("<svg viewBox='0 0 1 1'>", "isn't well-formed XML");
    says("<g viewBox='0 0 1 1'/>", r"must be an <svg> element, not <g>");
    says("<svg/>", "needs a viewBox");
    says("<!DOCTYPE svg [<!ENTITY x 'y'>]><svg viewBox='0 0 1 1'/>", "isn't well-formed XML");
}

#[test]
fn nothing_active_or_from_outside_is_allowed() {
    for element in ["script", "foreignObject", "image", "style", "a", "animate", "animateTransform", "set"] {
        says(&svg(&format!("<{element}/>")), &format!("can't have <{element}>"));
    }
    says(&svg(r#"<rect onclick="x()" width="1" height="1"/>"#), "event handler onclick");
    says(&svg(r#"<use href="other.svg#x"/>"#), "can only link within itself");
    says(&svg(r#"<use xlink:href="https://example.com/x.svg#y"/>"#), "can only link within itself");
    says(&svg(r#"<g clip-path="url(https://example.com/x)"/>"#), r"can only use url\(#...\).*clip-path");
    says(&svg(r#"<g style="mask: url( 'x.svg' )"/>"#), r"can only use url\(#...\).*style");
}

#[test]
fn every_fill_and_stroke_is_current_color_or_none() {
    says(&svg(r##"<path fill="#f00" d="M0 0"/>"##), r##"<path>'s fill is "#f00"; use currentColor or none"##);
    says(&svg(r#"<path stroke="black" d="M0 0"/>"#), r#"stroke is "black""#);
    says(&svg(r#"<path style="fill: red" d="M0 0"/>"#), r#"fill is "red""#);
    says(&svg(r##"<path fill="url(#g)" d="M0 0"/>"##), r#"fill is "url\(#g\)""#);
    assert_eq!(icon_problems(&svg(r#"<path fill="currentcolor" stroke="NONE" d="M0 0"/>"#)), Vec::<String>::new());
}
