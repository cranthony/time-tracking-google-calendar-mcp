//! The app's icon -- the Android and web apps' launcher icon, a calendar
//! page whose body is a clock, white on teal (time-tracker-client's
//! tool/icon/generate.py draws it; rust/assets/ holds copies). HTTP serves
//! it at /favicon.svg, /favicon.png and /favicon.ico, where clients that
//! show a server by its host's favicon find it, and initialize's
//! serverInfo lists it for clients that read MCP icons.

use base64::Engine;
use serde_json::{json, Value};

pub const SVG: &[u8] = include_bytes!("../../assets/favicon.svg");
/// 192x192.
pub const PNG: &[u8] = include_bytes!("../../assets/favicon.png");
/// 16x16 to 256x256.
pub const ICO: &[u8] = include_bytes!("../../assets/favicon.ico");

/// serverInfo's icons: the hosted SVG and PNG when the server is at a
/// public https address (`base`), else the SVG inline as a data: URI --
/// over stdio, or on localhost, where a client can't fetch it.
pub fn server_icons(base: Option<&str>) -> Value {
    match base.filter(|b| b.starts_with("https://")) {
        Some(base) => json!([
            {"src": format!("{base}/favicon.svg"), "mimeType": "image/svg+xml", "sizes": ["any"]},
            {"src": format!("{base}/favicon.png"), "mimeType": "image/png", "sizes": ["192x192"]}
        ]),
        None => {
            let data = base64::engine::general_purpose::STANDARD.encode(SVG);
            json!([{"src": format!("data:image/svg+xml;base64,{data}"), "mimeType": "image/svg+xml", "sizes": ["any"]}])
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_public_server_links_its_hosted_icons() {
        let icons = server_icons(Some("https://mcp.example"));
        assert_eq!(icons[0]["src"], "https://mcp.example/favicon.svg");
        assert_eq!(icons[1]["src"], "https://mcp.example/favicon.png");
        assert_eq!(icons[1]["sizes"], json!(["192x192"]));
    }

    #[test]
    fn otherwise_the_svg_is_inline() {
        for base in [None, Some("http://localhost:8000")] {
            let icons = server_icons(base);
            assert_eq!(icons.as_array().unwrap().len(), 1);
            let src = icons[0]["src"].as_str().unwrap();
            let data = src.strip_prefix("data:image/svg+xml;base64,").unwrap();
            assert_eq!(base64::engine::general_purpose::STANDARD.decode(data).unwrap(), SVG);
        }
    }

    #[test]
    fn the_icons_are_what_they_say() {
        assert!(SVG.starts_with(b"<svg "));
        assert!(PNG.starts_with(b"\x89PNG"));
        assert!(ICO.starts_with(&[0, 0, 1, 0]));
    }
}
