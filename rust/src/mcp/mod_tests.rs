//! The protocol's flags, answered as the transports would.

use super::*;
use crate::health::SheetWarning;
use crate::testing::app::test_app;

fn answer(app: &Arc<crate::app::App>, method: &str, params: Value) -> Value {
    let message = json!({"jsonrpc": "2.0", "id": 1, "method": method, "params": params});
    handle(app, &mut Session::new(), &message, &mut Vec::new()).unwrap()["result"].clone()
}

#[test]
fn the_handshake_and_tool_results_flag_recent_hand_edits_of_the_spreadsheet() {
    let t = test_app();
    let call = json!({"name": "get_health", "arguments": {"samples": false}});
    assert!(answer(&t.app, "initialize", json!({}))["_meta"][SHEET_WARNINGS_META_KEY].is_null());
    assert!(answer(&t.app, "tools/call", call.clone())["_meta"][SHEET_WARNINGS_META_KEY].is_null());

    let at_ms = crate::health::now_ms();
    t.app.health.record_sheet_warnings(vec![SheetWarning { at_ms, kind: "edited".into(), tab: Some("People".into()), cells: Vec::new(), cell_count: 1, restored: true }]);

    let flag = json!({"recent": 1, "latest": crate::health::iso(at_ms)});
    assert_eq!(answer(&t.app, "initialize", json!({}))["_meta"][SHEET_WARNINGS_META_KEY], flag);
    assert_eq!(answer(&t.app, "tools/call", call)["_meta"][SHEET_WARNINGS_META_KEY], flag);
}
