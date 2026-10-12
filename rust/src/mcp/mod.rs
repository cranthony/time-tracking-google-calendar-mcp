//! The MCP protocol: JSON-RPC requests (initialize, ping, tools/list,
//! tools/call) answered as conformance/'s golden records have them, keys
//! in order -- the HTTP scenarios compare bodies byte for byte. Transports (stdio, streamable HTTP) hand messages here.

pub mod http;
pub mod schema;
pub mod stdio;

use crate::py;
use crate::tools::{self, Outcome, ToolError};
use serde_json::{json, Map, Value};
use std::sync::{Arc, LazyLock};

/// The tool list, as tools/list sends it: each tool's name, description,
/// and input and output schemas, derived from the code (see `contract`;
/// contract/tools.json is a snapshot of it).
pub static CONTRACT: LazyLock<Vec<Value>> = LazyLock::new(|| tools::registry().iter().map(|t| t.entry()).collect());

const HANDSHAKE_PROTOCOL_VERSIONS: [&str; 4] = ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"];
const LATEST_HANDSHAKE_VERSION: &str = "2025-11-25";
pub const SERVER_NAME: &str = "time-tracking-google-calendar-mcp";

const METHOD_NOT_FOUND: i64 = -32601;
const INVALID_PARAMS: i64 = -32602;
const INVALID_REQUEST: i64 = -32600;
const PARSE_ERROR: i64 = -32700;

pub fn contract_tool(name: &str) -> Option<&'static Value> {
    CONTRACT.iter().find(|t| t["name"].as_str() == Some(name))
}

/// Whether `name`'s result goes in `{"result": ...}` -- a tool returning
/// something other than an object (a list, a string).
pub(crate) fn wrapped(tool: &Value) -> bool {
    let name = tool["name"].as_str().unwrap_or_default();
    tool.pointer("/outputSchema/title").and_then(Value::as_str) == Some(&format!("{name}Output"))
}

pub struct Session {
    pub initialized: bool,
}

impl Session {
    pub fn new() -> Session {
        Session { initialized: false }
    }
}

impl Default for Session {
    fn default() -> Self {
        Session::new()
    }
}

/// A tool call as the health metrics record it: its name, how long its
/// own work took, and whether it succeeded.
pub type Timing = (String, i64, bool);

/// One JSON-RPC message in, its response out (`None` for a notification).
/// Each tool call's timing goes in `timings`, for the transport to record
/// -- over HTTP, with the whole request's time beside it.
pub fn handle(app: &Arc<crate::app::App>, session: &mut Session, message: &Value, timings: &mut Vec<Timing>) -> Option<Value> {
    let Some(object) = message.as_object() else {
        return Some(error_response(Value::Null, INVALID_REQUEST, "Invalid Request"));
    };
    let method = object.get("method").and_then(Value::as_str);
    let id = object.get("id").cloned();
    let Some(method) = method else {
        // A response to something we never asked: ignore it.
        return None;
    };
    let Some(id) = id else {
        if method == "notifications/initialized" {
            session.initialized = true;
        }
        return None;
    };
    let params = object.get("params").cloned().unwrap_or(Value::Object(Map::new()));
    let result = match method {
        "initialize" => {
            session.initialized = true;
            Ok(initialize_result(&params))
        }
        "ping" => Ok(json!({})),
        "tools/list" => Ok(json!({ "tools": *CONTRACT })),
        "tools/call" => Ok(call_tool(app, &params, timings)),
        _ => Err((METHOD_NOT_FOUND, "Method not found")),
    };
    Some(match result {
        Ok(result) => {
            let mut response = Map::new();
            response.insert("jsonrpc".into(), json!("2.0"));
            response.insert("id".into(), id);
            response.insert("result".into(), result);
            Value::Object(response)
        }
        Err((code, text)) => error_response(id, code, text),
    })
}

pub fn parse_error_response() -> Value {
    error_response(Value::Null, PARSE_ERROR, "Parse error")
}

fn error_response(id: Value, code: i64, message: &str) -> Value {
    let mut error = Map::new();
    error.insert("code".into(), json!(code));
    error.insert("message".into(), json!(message));
    let mut response = Map::new();
    response.insert("jsonrpc".into(), json!("2.0"));
    response.insert("id".into(), id);
    response.insert("error".into(), Value::Object(error));
    Value::Object(response)
}

#[allow(dead_code)]
const _UNUSED: i64 = INVALID_PARAMS;

fn initialize_result(params: &Value) -> Value {
    let requested = params.get("protocolVersion").and_then(Value::as_str).unwrap_or_default();
    let negotiated =
        if HANDSHAKE_PROTOCOL_VERSIONS.contains(&requested) { requested } else { LATEST_HANDSHAKE_VERSION };
    // Keys in the order the Python SDK sends them.
    let mut result = json!({
        "capabilities": {
            "experimental": {},
            "prompts": {"listChanged": false},
            "resources": {"listChanged": false, "subscribe": false},
            "tools": {"listChanged": false}
        },
        "protocolVersion": negotiated,
        "serverInfo": {"name": SERVER_NAME, "version": ""}
    });
    // A build that skips sign-in says so to whoever connects.
    if crate::auth_mode::disabled() {
        result["instructions"] = json!(crate::auth_mode::INSTRUCTIONS);
        result["serverInfo"]["title"] = json!(format!("{SERVER_NAME} (sign-in off)"));
    }
    result
}

fn text_item(text: String) -> Value {
    let mut item = Map::new();
    item.insert("text".into(), Value::String(text));
    item.insert("type".into(), json!("text"));
    Value::Object(item)
}

fn error_result(text: String) -> Value {
    let mut result = Map::new();
    result.insert("content".into(), json!([text_item(text)]));
    result.insert("isError".into(), json!(true));
    Value::Object(result)
}

/// A tool's result as text, as the Python SDK renders it: a list one item
/// each, a string as it is, anything else as indented JSON.
fn content_of(value: &Value) -> Vec<Value> {
    match value {
        Value::Array(items) => items.iter().flat_map(content_of).collect(),
        Value::String(text) => vec![text_item(text.clone())],
        other => vec![text_item(py::json_pretty(other))],
    }
}

pub fn call_tool(app: &Arc<crate::app::App>, params: &Value, timings: &mut Vec<Timing>) -> Value {
    let name = params.get("name").and_then(Value::as_str).unwrap_or_default().to_string();
    let Some(contract) = contract_tool(&name) else {
        return error_result(format!("Unknown tool: {name}"));
    };
    let arguments = params.get("arguments").cloned().filter(|a| !a.is_null()).unwrap_or(json!({}));
    let validator = schema::Validator::new(&contract["inputSchema"]);
    let arguments = match validator.validate(&arguments) {
        Ok(checked) => checked,
        Err(errors) => {
            let lines: Vec<String> =
                errors.iter().map(|e| format!("- {}: {} [type={}]", e.loc, e.message, e.kind)).collect();
            return error_result(format!("Error executing tool {name}: invalid arguments\n{}", lines.join("\n")));
        }
    };
    let started = std::time::Instant::now();
    let outcome = tools::run(app, &name, arguments);
    let work_ms = started.elapsed().as_millis() as i64;
    timings.push((name.clone(), work_ms, outcome.is_ok()));
    match outcome {
        Ok(Outcome(value)) => {
            let structured = if wrapped(contract) { json!({ "result": value }) } else { value.clone() };
            let mut result = Map::new();
            result.insert("content".into(), Value::Array(content_of(&value)));
            result.insert("isError".into(), json!(false));
            result.insert("structuredContent".into(), structured);
            Value::Object(result)
        }
        Err(ToolError::Message(text)) => {
            tracing::info!("Tool {} failed: {}", py::repr(&name), py::repr(&text));
            error_result(format!("Error executing tool {name}: {text}"))
        }
        Err(ToolError::Unexpected(error)) => {
            tracing::error!("Tool {} raised an unexpected exception: {error:?}", py::repr(&name));
            error_result(format!("Error executing tool {name}"))
        }
    }
}
