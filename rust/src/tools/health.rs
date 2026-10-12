//! get_health: the server's health metrics (see crate::health).

use super::{ToolError, ToolResult};
use crate::app::App;
use crate::health::{iso, latency_summary, round1};
use serde_json::{json, Map, Value};

pub fn get_health(app: &App, args: &Map<String, Value>) -> ToolResult {
    if let Some(writer) = app.health_writer() {
        // What the tab kept, from before this start, taken in.
        writer.wait_loaded(10);
    }
    let samples = args.get("samples").and_then(Value::as_bool).unwrap_or(true);
    let mut calls = app.health.calls();
    if let Some(tool) = args.get("tool_name").and_then(Value::as_str) {
        if !calls.iter().any(|(n, _)| n == tool) {
            let known: Vec<&str> = calls.iter().map(|(n, _)| n.as_str()).collect();
            let known = if known.is_empty() { "none yet".to_string() } else { known.join(", ") };
            return Err(ToolError::Message(format!("No calls of {} are recorded; those that are: {known}", crate::py::repr(tool))));
        }
        calls.retain(|(n, _)| n == tool);
    }
    let tools: Vec<Value> = calls
        .iter()
        .map(|(name, recorded)| {
            let totals: Vec<i64> = recorded.iter().filter_map(|c| c.total_ms).collect();
            json!({
                "tool": name,
                "count": recorded.len(),
                "errors": recorded.iter().filter(|c| !c.ok).count(),
                "work": latency_summary(&recorded.iter().map(|c| c.work_ms).collect::<Vec<_>>()),
                "total": latency_summary(&totals),
                "calls": if samples {
                    Value::Array(recorded.iter().map(|c| json!({
                        "at": iso(c.at_ms),
                        "work_ms": c.work_ms,
                        "total_ms": c.total_ms,
                        "overhead_ms": c.total_ms.map(|t| t - c.work_ms),
                        "ok": c.ok,
                    })).collect())
                } else {
                    Value::Null
                },
            })
        })
        .collect();
    let memory = app.health.memory();
    let mib: Vec<f64> = memory.iter().map(|m| round1(m.rss_kib as f64 / 1024.0)).collect();
    let min = mib.iter().copied().fold(None, |a: Option<f64>, b| Some(a.map_or(b, |a| a.min(b))));
    let max = mib.iter().copied().fold(None, |a: Option<f64>, b| Some(a.map_or(b, |a| a.max(b))));
    Ok(json!({
        "tools": tools,
        "memory": if samples {
            Value::Array(memory.iter().zip(&mib).map(|(m, v)| json!({"at": iso(m.at_ms), "rss_mib": v})).collect())
        } else {
            Value::Null
        },
        "memory_latest_mib": mib.last(),
        "memory_min_mib": min,
        "memory_max_mib": max,
        "restarts": app.health.restarts().into_iter().map(iso).collect::<Vec<_>>(),
    }))
}
