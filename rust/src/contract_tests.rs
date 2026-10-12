//! The contract's shape, and contract/tools.json as a snapshot of it.

use super::*;

/// The contract as contract/tools.json has it.
fn rendered() -> String {
    serde_json::to_string_pretty(&*crate::mcp::CONTRACT).unwrap() + "\n"
}

#[test]
fn tools_json_is_the_contract() {
    // A deliberate change: CTT_UPDATE_CONTRACT=1 cargo test, and commit
    // the file -- its diff is what clients will see change.
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../contract/tools.json");
    let derived = rendered();
    if std::env::var_os("CTT_UPDATE_CONTRACT").is_some() {
        std::fs::write(&path, &derived).unwrap();
        return;
    }
    let committed = std::fs::read_to_string(&path).unwrap().replace("\r\n", "\n");
    if committed != derived {
        let first = committed.lines().zip(derived.lines()).position(|(a, b)| a != b).unwrap_or(committed.lines().count().min(derived.lines().count()));
        let around = |text: &str| text.lines().skip(first.saturating_sub(3)).take(8).collect::<Vec<_>>().join("\n");
        panic!(
            "contract/tools.json isn't the contract (rerun with CTT_UPDATE_CONTRACT=1 to write it), from line {}:\n--- tools.json\n{}\n--- derived\n{}",
            first + 1,
            around(&committed),
            around(&derived)
        );
    }
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

#[test]
fn an_option_of_a_plain_type_takes_null_as_a_type() {
    let mut defs = Defs::default();
    assert_eq!(<Option<String>>::schema(&mut defs), json!({"type": ["string", "null"]}));
    assert_eq!(<Option<Vec<String>>>::schema(&mut defs), json!({"items": {"type": "string"}, "type": ["array", "null"]}));
    assert_eq!(<Option<DateOrDateTime>>::schema(&mut defs)["anyOf"].as_array().unwrap().len(), 3);
}
