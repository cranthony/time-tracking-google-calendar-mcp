//! Helpers for the unit tests: events and times from short strings, so a
//! test's day reads at a glance (tests/event_time_helpers.py, ported).
//!
//! All times are on one day (`DAY`, in UTC); "+1" after a time means the
//! day after (an overnight sleep spans "20:00-07:00+1").

use crate::event::{Event, Moment};
use crate::facts::Facts;
use chrono::{FixedOffset, NaiveDate, TimeZone};
use serde_json::Value;

/// A time on `DAY` ("HH:MM", or "HH:MM+1" for the day after), in UTC.
pub fn time_at(time: &str) -> Moment {
    let (clock, next) = match time.strip_suffix("+1") {
        Some(clock) => (clock, 1),
        None => (time, 0),
    };
    let (hour, minute) = clock.split_once(':').unwrap_or_else(|| panic!("{time:?} doesn't look like HH:MM"));
    let day = NaiveDate::from_ymd_opt(2026, 1, 1).unwrap() + chrono::Duration::days(next);
    FixedOffset::east_opt(0)
        .unwrap()
        .from_local_datetime(&day.and_hms_opt(hour.parse().unwrap(), minute.parse().unwrap(), 0).unwrap())
        .unwrap()
}

/// An event spanning "HH:MM-HH:MM" (either end "+1"), summary "Event",
/// built further with the setters below.
pub fn event_at(range: &str) -> EventBuilder {
    let (start, end) = range.split_once('-').unwrap();
    EventBuilder(Event { summary: Some("Event".into()), start: Some(time_at(start)), end: Some(time_at(end)), ..Default::default() })
}

pub struct EventBuilder(pub Event);

impl EventBuilder {
    pub fn id(mut self, id: &str) -> Self {
        self.0.id = Some(id.into());
        self
    }
    pub fn summary(mut self, summary: &str) -> Self {
        self.0.summary = Some(summary.into());
        self
    }
    pub fn priority(mut self, priority: i64) -> Self {
        self.0.priority = Some(priority);
        self
    }
    pub fn sleep(mut self) -> Self {
        self.0.is_end_of_day_sleep = Some(true);
        self
    }
    pub fn description(mut self, description: &str) -> Self {
        self.0.description = Some(description.into());
        self
    }
    pub fn actions(mut self, ids: &[&str]) -> Self {
        self.0.action_ids = Some(ids.iter().map(|s| s.to_string()).collect());
        self
    }
    pub fn facts(mut self, facts: Facts) -> Self {
        self.0.facts = Some(facts);
        self
    }
    pub fn status(mut self, status: &str) -> Self {
        self.0.status = Some(status.into());
        self
    }
    pub fn build(self) -> Event {
        self.0
    }
}

impl From<EventBuilder> for Event {
    fn from(builder: EventBuilder) -> Event {
        builder.0
    }
}

/// Facts from JSON as they're stored ({"location": ..., "with": [...]}).
pub fn facts(json: Value) -> Facts {
    Facts::from_json(&json.to_string()).unwrap()
}

pub fn strings(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| s.to_string()).collect()
}

pub mod fake_google;
