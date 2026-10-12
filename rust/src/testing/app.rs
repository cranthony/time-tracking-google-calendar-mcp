//! The whole server's state on the fake Google: a calendar of its own (in
//! UTC), an `App` on it, and ways to seed the metadata spreadsheet's tabs
//! and the calendar directly.

use super::fake_google::FakeGoogle;
use crate::app::{App, Config};
use crate::event::Event;
use crate::py;
use crate::sheet::ensure_spreadsheet;
use crate::write_lock::WRITE_LOCK;
use serde_json::Value;
use std::sync::Arc;

pub struct TestApp {
    pub fake: FakeGoogle,
    pub app: Arc<App>,
    pub calendar_id: String,
}

pub fn test_app() -> TestApp {
    test_app_in(Some("UTC"))
}

/// A test app on a calendar in `time_zone` (or none).
pub fn test_app_in(time_zone: Option<&str>) -> TestApp {
    let fake = FakeGoogle::start();
    let calendar_id = fake.add_calendar(time_zone);
    let config = Config { calendar_id: Some(calendar_id.clone()), token_path: "unused".into(), credentials_path: "unused".into() };
    let app = Arc::new(App::with_google(config, fake.google()));
    app.set_self();
    TestApp { fake, app, calendar_id }
}

/// A cell as RowSheet writes it: text as is, lists and objects as JSON.
fn cell(value: Option<&Value>) -> String {
    match value {
        None | Some(Value::Null) => String::new(),
        Some(Value::String(text)) => text.clone(),
        Some(value @ (Value::Array(_) | Value::Object(_))) => py::json_compact_ascii(value),
        Some(value) => value.to_string(),
    }
}

impl TestApp {
    /// The metadata spreadsheet's id, made the first time.
    pub fn spreadsheet(&self) -> String {
        let (calendar, sheets) = self.app.clients().unwrap();
        let _held = WRITE_LOCK.hold();
        ensure_spreadsheet(&calendar, &sheets).unwrap().0
    }

    /// Replace the rows of the tab titled `title` (which must exist: build
    /// its store first) with `rows`, each an object by column name.
    pub fn seed(&self, title: &str, rows: &[Value]) {
        let spreadsheet = self.spreadsheet();
        let header = self.fake.rows(&spreadsheet, title).remove(0);
        let cells = rows.iter().map(|row| header.iter().map(|name| cell(row.get(name))).collect()).collect();
        self.fake.replace_rows(&spreadsheet, title, cells);
    }

    /// A tab's rows below its header, each an object by column name (blank
    /// cells left out).
    pub fn tab(&self, title: &str) -> Vec<serde_json::Map<String, Value>> {
        let mut rows = self.fake.rows(&self.spreadsheet(), title);
        let header = rows.remove(0);
        rows.into_iter()
            .map(|row| {
                header.iter().zip(row).filter(|(_, text)| !text.is_empty()).map(|(name, text)| (name.clone(), Value::String(text))).collect()
            })
            .collect()
    }

    /// Put `event` straight into the fake calendar, with its own id however
    /// short (Google would refuse "e1" from a client).
    pub fn seed_event(&self, event: &Event) {
        let mut body = event.insert_body().unwrap();
        if let Some(id) = &event.id {
            body.insert("id".into(), Value::String(id.clone()));
        }
        self.fake.add_event(&self.calendar_id, Value::Object(body));
    }

    /// Put `event` on the calendar as is, with its own id if it has one.
    pub fn add_event(&self, event: &Event) -> Event {
        let client = self.app.calendar_client().unwrap();
        let _held = WRITE_LOCK.hold();
        client.create_event(event).unwrap()
    }
}
