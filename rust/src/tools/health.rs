//! get_health: the server's health metrics (see crate::health).

use super::{from_store, ToolError};
use crate::app::App;
use crate::contract::{tool, Schema};
use crate::health::{iso, latency_summary, round1};
use serde::{Deserialize, Serialize};
use serde_json::{json, Number, Value};

/// One tool call: when it ended (UTC), how long the tool's own work
/// took, and -- if it came over HTTP -- how long the whole request took,
/// auth and transport included, and how much of that wasn't the tool's
/// work (`overhead_ms`: `work_ms` and it stack to `total_ms`, to graph on
/// one timeline). `ok`: it returned, rather than raising; a refusal the
/// tool returns as its result is still ok.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct HealthCall {
    pub at: String,
    pub work_ms: i64,
    pub total_ms: Option<i64>,
    pub overhead_ms: Option<i64>,
    pub ok: bool,
}

/// The median, 95th percentile and slowest of some calls' latencies.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct LatencySummary {
    pub median_ms: i64,
    pub p95_ms: i64,
    pub max_ms: i64,
}

/// The server's resident memory (RSS), in MiB, after a tool call --
/// any tool's -- ended (UTC).
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct MemorySample {
    pub at: String,
    pub rss_mib: Number,
}

/// One tool's last calls (up to 100): how many, how many failed, and
/// their latencies -- the tool's own work, and the whole request's.
#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct ToolHealth {
    pub tool: String,
    pub count: i64,
    pub errors: i64,
    pub work: Option<LatencySummary>,
    pub total: Option<LatencySummary>,
    pub calls: Option<Vec<HealthCall>>,
}

#[derive(Clone, Debug, Serialize, Deserialize, Schema)]
pub struct HealthReport {
    pub tools: Vec<ToolHealth>,
    pub memory: Option<Vec<MemorySample>>,
    pub memory_latest_mib: Option<Number>,
    pub memory_min_mib: Option<Number>,
    pub memory_max_mib: Option<Number>,
    pub restarts: Vec<String>,
    /// How this build signs people in: "required" (WorkOS sign-in) or
    /// "disabled" (built with AUTH_DISABLED: every request is trusted).
    pub auth: String,
}

/// The server's health metrics, to graph or look for patterns in: for
/// each tool, its last 100 calls -- when each ended, how long the tool's
/// own work took and how long the whole request took (auth and transport
/// included), and whether it succeeded -- with the median, 95th
/// percentile and slowest of each; the server's memory after each of its
/// last 100 calls, whichever tool; and its last 100 restarts. Kept across
/// restarts, in the Health tab of the calendar's metadata spreadsheet;
/// the last few seconds' may not be written yet, but are given here.
/// Also auth: "required", or "disabled" for a build that skips sign-in.
///
/// `tool_name`: just that tool's calls. `samples`: false for just the
/// summaries, without each call and memory sample. Read-only.
#[tool]
pub fn get_health(app: &App, tool_name: Option<String>, #[schema(default = "true")] samples: bool) -> Result<HealthReport, ToolError> {
    if let Some(writer) = app.health_writer() {
        // What the tab kept, from before this start, taken in.
        writer.wait_loaded(10);
    }
    let mut calls = app.health.calls();
    if let Some(tool) = tool_name.as_deref() {
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
    from_store(json!({
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
        "auth": crate::auth_mode::name(),
    }))
}
