//! The tools, called as a client calls them -- MCP tools/call, its
//! arguments checked against the contract's schema -- on the fake Google.
//! Where Python checked that a tool delegated to a mocked store, these
//! check what the store then holds, or what the tool hands back.

use crate::event::{Event, Moment};
use crate::testing::app::{test_app, TestApp};
use serde_json::{json, Value};

mod compaction;
mod events;
mod requests;
mod stores;
mod writes;

pub struct Server {
    pub t: TestApp,
}

impl Server {
    pub fn new() -> Server {
        Server { t: test_app() }
    }

    /// The tool's result, or its error's text -- a write sent, as a client
    /// sends it, with the version it last read of each item it changes,
    /// where it gives none (see `seen`; `raw` sends what it's given).
    pub fn call(&self, name: &str, arguments: Value) -> Result<Value, String> {
        let arguments = self.seen(name, arguments);
        self.as_is(name, arguments)
    }

    /// The tool's result, or its error's text, sent with [arguments] as
    /// they are.
    pub fn as_is(&self, name: &str, arguments: Value) -> Result<Value, String> {
        let mut timings = Vec::new();
        let result = crate::mcp::call_tool(&self.t.app, &json!({"name": name, "arguments": arguments}), &mut timings);
        if result["isError"] == true {
            return Err(result["content"][0]["text"].as_str().unwrap_or_default().to_string());
        }
        let structured = result["structuredContent"].clone();
        let contract = crate::mcp::contract_tool(name).unwrap();
        Ok(if crate::mcp::wrapped(contract) { structured["result"].clone() } else { structured })
    }

    /// The version each item [name]'s write changes is now, read as a
    /// client reads it, filled in where [arguments] give none.
    pub fn seen(&self, name: &str, mut arguments: Value) -> Value {
        let read = |tool: &str, args: Value| -> Option<Value> {
            let mut timings = Vec::new();
            let result = crate::mcp::call_tool(&self.t.app, &json!({"name": tool, "arguments": args}), &mut timings);
            (result["isError"] != true).then(|| {
                let structured = result["structuredContent"].clone();
                if crate::mcp::wrapped(crate::mcp::contract_tool(tool).unwrap()) { structured["result"].clone() } else { structured }
            })
        };
        let in_list = |tool: &str, args: Value, id: &str| -> Option<Value> {
            read(tool, args)?.as_array()?.iter().find(|i| i["id"] == id).map(|i| i["version"].clone())
        };
        let version_of = |kind: &str, id: &str| -> Option<Value> {
            match kind {
                "event" => self.t.app.calendar_client().ok()?.get_event(id).ok()?.etag.0.map(Value::from),
                "action" => read("get_action", json!({"id_or_name": id})).map(|i| i["version"].clone()),
                "action_group" => read("get_action_group", json!({"id_or_name": id})).map(|i| i["version"].clone()),
                "person" => read("get_person", json!({"id_or_name": id})).map(|i| i["version"].clone()),
                "circle" => read("get_circle", json!({"id_or_name": id})).map(|i| i["version"].clone()),
                "location" => read("get_location", json!({"id_or_name": id})).map(|i| i["version"].clone()),
                "location_group" => read("get_location_group", json!({"id_or_name": id})).map(|i| i["version"].clone()),
                "habit" => read("get_habit", json!({"id_or_name": id})).map(|i| i["version"].clone()),
                "goal" => read("get_goal", json!({"id_or_title": id})).map(|i| i["version"].clone()),
                "trait" => in_list("get_traits", json!({"statuses": ["active", "off", "archived"]}), id),
                "travel_time" => in_list("get_travel_times", json!({}), id),
                "note" => in_list("get_notes", json!({"include_compacted": true}), id),
                _ => None,
            }
            .filter(|v| !v.is_null())
        };
        // Into the object at `at`, as `key`, the version of the `kind` its
        // `id_key` names, unless it says one.
        let fill = |at: &mut Value, kind: &str, id_key: &str, key: &str| {
            let Some(id) = at.get(id_key).and_then(Value::as_str).map(str::to_string) else { return };
            if at.get(key).is_some_and(|v| !v.is_null()) {
                return;
            }
            if let Some(version) = version_of(kind, &id) {
                at[key] = version;
            }
        };
        let each = |list: Option<&mut Value>, f: &dyn Fn(&mut Value)| {
            if let Some(Value::Array(items)) = list {
                items.iter_mut().for_each(f);
            }
        };
        match name {
            "update_event" | "delete_event" => {
                each(arguments.get_mut("updates"), &|u| {
                    if let Some(event) = u.get_mut("event") {
                        fill(event, "event", "id", "etag")
                    }
                });
                each(arguments.get_mut("cancels"), &|c| fill(c, "event", "event_id", "etag"));
            }
            "update_goals" => {
                each(arguments.get_mut("updates"), &|u| {
                    if let Some(goal) = u.get_mut("goal") {
                        fill(goal, "goal", "id", "version")
                    }
                });
                each(arguments.get_mut("deletes"), &|d| fill(d, "goal", "goal_id", "if_version"));
            }
            "edit_note" => fill(&mut arguments, "note", "note_id", "version"),
            "delete_note" => fill(&mut arguments, "note", "note_id", "if_version"),
            _ => {
                let rows = [
                    ("update_action", "action", "action", None),
                    ("update_action_group", "group", "action_group", Some(("delete_action_group", "group_id"))),
                    ("update_person", "person", "person", None),
                    ("update_circle", "circle", "circle", Some(("delete_circle", "circle_id"))),
                    ("update_location", "location", "location", Some(("delete_location", "location_id"))),
                    ("update_location_group", "group", "location_group", Some(("delete_location_group", "group_id"))),
                    ("update_habit", "habit", "habit", None),
                    ("update_trait", "trait", "trait", None),
                    ("update_travel_time", "travel_time", "travel_time", Some(("delete_travel_time", "travel_time_id"))),
                    ("update_goal", "goal", "goal", Some(("delete_goal", "goal_id"))),
                ];
                for (update, argument, kind, delete) in rows {
                    if name == update {
                        if let Some(item) = arguments.get_mut(argument) {
                            fill(item, kind, "id", "version");
                        }
                    }
                    if let Some((delete, id_key)) = delete {
                        if name == delete {
                            fill(&mut arguments, kind, id_key, "if_version");
                        }
                    }
                }
            }
        }
        arguments
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
