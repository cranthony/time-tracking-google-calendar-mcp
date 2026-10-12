//! The derived contract against contract/tools.json, byte for byte.

use super::*;
use crate::mcp::HAND_WRITTEN;

fn pretty(value: &Value) -> String {
    serde_json::to_string_pretty(value).unwrap()
}

#[test]
fn each_typed_tool_is_described_as_tools_json_has_it() {
    for tool in crate::tools::typed() {
        let expected = HAND_WRITTEN.iter().find(|e| e["name"] == tool.name).unwrap_or_else(|| panic!("{} isn't in tools.json", tool.name));
        let derived = tool.entry();
        if derived != *expected || pretty(&derived) != pretty(expected) {
            let (want, got) = (pretty(expected), pretty(&derived));
            let first = want.lines().zip(got.lines()).position(|(a, b)| a != b).unwrap_or(want.lines().count().min(got.lines().count()));
            let around = |text: &str| text.lines().skip(first.saturating_sub(3)).take(8).collect::<Vec<_>>().join("\n");
            panic!("{}'s entry differs at line {first}:\n--- tools.json\n{}\n--- derived\n{}", tool.name, around(&want), around(&got));
        }
    }
}

#[test]
fn every_tool_is_typed_or_hand_written_but_not_both_unknown() {
    let names: Vec<&str> = crate::mcp::CONTRACT.iter().filter_map(|e| e["name"].as_str()).collect();
    for tool in crate::tools::typed() {
        assert!(names.contains(&tool.name), "{} is typed but not listed", tool.name);
    }
}

#[test]
fn titles_fields_as_pydantic_does() {
    assert_eq!(title_case("created_id"), "Created Id");
    assert_eq!(title_case("id_or_name"), "Id Or Name");
    assert_eq!(title_case("id"), "Id");
}

#[test]
fn sorts_keys_but_keeps_field_order() {
    let sorted = sort_keys(json!({"type": "object", "properties": {"b": {"type": "x", "title": "B"}, "a": {}}, "additionalProperties": false}));
    assert_eq!(sorted.to_string(), r#"{"additionalProperties":false,"properties":{"b":{"title":"B","type":"x"},"a":{}},"type":"object"}"#);
}

#[test]
fn a_literal_is_its_const_or_enum() {
    assert_eq!(literal(&["hint"]), json!({"const": "hint", "type": "string"}));
    assert_eq!(literal(&["a", "b"]), json!({"enum": ["a", "b"], "type": "string"}));
}
