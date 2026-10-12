//! The tools, called as a client calls them -- MCP tools/call, its
//! arguments checked against the contract's schema -- on the fake Google.
//! Where Python checked that a tool delegated to a mocked store, these
//! check what the store then holds, or what the tool hands back.

use crate::event::{Event, Moment};
use crate::testing::app::{test_app, TestApp};
use serde_json::{json, Value};

mod compaction;
mod events;
mod reads;
mod stores;

pub struct Server {
    pub t: TestApp,
}

impl Server {
    pub fn new() -> Server {
        Server { t: test_app() }
    }

    /// The tool's result, or its error's text.
    pub fn call(&self, name: &str, arguments: Value) -> Result<Value, String> {
        let mut timings = Vec::new();
        let result = crate::mcp::call_tool(&self.t.app, &json!({"name": name, "arguments": arguments}), &mut timings);
        if result["isError"] == true {
            return Err(result["content"][0]["text"].as_str().unwrap_or_default().to_string());
        }
        let structured = result["structuredContent"].clone();
        let contract = crate::mcp::contract_tool(name).unwrap();
        Ok(if crate::mcp::wrapped(contract) { structured["result"].clone() } else { structured })
    }

    pub fn ok(&self, name: &str, arguments: Value) -> Value {
        self.call(name, arguments).unwrap_or_else(|e| panic!("{name}: {e}"))
    }

    /// The tool's error text, which must match `pattern`.
    pub fn refused(&self, name: &str, arguments: Value, pattern: &str) -> String {
        let Err(text) = self.call(name, arguments) else { panic!("{name} wasn't refused") };
        assert!(regex::Regex::new(pattern).unwrap().is_match(&text), "{pattern:?} isn't in: {text}");
        text
    }

    pub fn seed(&self, events: &[Event]) {
        for event in events {
            self.t.seed_event(event);
        }
        self.t.fake.clear_requests();
    }

    pub fn event(&self, id: &str) -> Event {
        self.t.app.calendar_client().unwrap().get_event(id).unwrap()
    }

    /// Event writes the calendar was sent.
    pub fn event_writes(&self) -> usize {
        self.t.fake.requests().iter().filter(|r| r.method != "GET" && r.path.contains("/events")).count()
    }
}

pub fn at(hour: u32, minute: u32) -> Moment {
    chrono::DateTime::parse_from_rfc3339(&format!("2026-01-01T{hour:02}:{minute:02}:00+00:00")).unwrap()
}

/// An instant as the tools write one.
pub fn when(moment: Moment) -> Value {
    json!(crate::py::pydantic_datetime(&moment))
}

pub fn focus(id: &str, start: Moment, end: Moment) -> Event {
    Event { id: Some(id.into()), summary: Some("Focus block".into()), start: Some(start), end: Some(end), ..Default::default() }
}

pub fn strings(items: &[&str]) -> Vec<String> {
    crate::testing::strings(items)
}
