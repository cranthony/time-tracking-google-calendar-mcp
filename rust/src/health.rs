//! The server's health metrics (utilities/health.py): each tool call's
//! latency, the memory after it, and the server's starts -- recorded in
//! memory, written to the Health tab in the background.

use std::collections::VecDeque;
use std::sync::Mutex;

pub const SAMPLES: usize = 100;

#[derive(Debug, Clone)]
pub struct CallSample {
    pub at_ms: i64,
    pub work_ms: i64,
    pub total_ms: Option<i64>,
    pub ok: bool,
}

#[derive(Default)]
struct Recorded {
    calls: Vec<(String, VecDeque<CallSample>)>,
}

pub struct HealthRecorder {
    recorded: Mutex<Recorded>,
}

impl HealthRecorder {
    pub fn new() -> HealthRecorder {
        HealthRecorder { recorded: Mutex::new(Recorded::default()) }
    }

    pub fn record_call(&self, tool: &str, work_ms: i64, total_ms: Option<i64>, ok: bool) {
        let at_ms = chrono::Utc::now().timestamp_millis();
        let mut recorded = self.recorded.lock().unwrap_or_else(|e| e.into_inner());
        let index = match recorded.calls.iter().position(|(name, _)| name == tool) {
            Some(i) => i,
            None => {
                recorded.calls.push((tool.to_string(), VecDeque::new()));
                recorded.calls.len() - 1
            }
        };
        let samples = &mut recorded.calls[index].1;
        samples.push_back(CallSample { at_ms, work_ms, total_ms, ok });
        while samples.len() > SAMPLES {
            samples.pop_front();
        }
    }

    pub fn calls(&self) -> Vec<(String, Vec<CallSample>)> {
        let recorded = self.recorded.lock().unwrap_or_else(|e| e.into_inner());
        recorded.calls.iter().map(|(n, s)| (n.clone(), s.iter().cloned().collect())).collect()
    }
}

impl Default for HealthRecorder {
    fn default() -> Self {
        HealthRecorder::new()
    }
}
