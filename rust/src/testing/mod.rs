//! Helpers for the unit tests: events and times from short strings, so a
//! test's day reads at a glance.
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
    pub fn description(mut self, description: &str) -> Self {
        self.0.description = Some(description.into());
        self
    }
    pub fn actions(mut self, ids: &[&str]) -> Self {
        self.0.action_ids = Some(shares(ids));
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

/// Facts from JSON as they're stored ({"locations": [...], "with": [...]}).
pub fn facts(json: Value) -> Facts {
    Facts::from_json(&json.to_string()).unwrap()
}

/// Action shares, each an action with neither minutes nor percent.
pub fn shares(ids: &[&str]) -> Vec<crate::stores::actions::ActionShare> {
    ids.iter().map(|&id| id.into()).collect()
}

pub fn strings(items: &[&str]) -> Vec<String> {
    items.iter().map(|s| s.to_string()).collect()
}

pub mod app;
pub mod fake_google;

#[cfg(test)]
mod tests {
    //! The helpers' own tests.
    use super::*;

    fn utc(day: u32, hour: u32, minute: u32) -> Moment {
        chrono::DateTime::parse_from_rfc3339(&format!("2026-01-{day:02}T{hour:02}:{minute:02}:00+00:00")).unwrap()
    }

    #[test]
    fn time_at_parses_hh_mm_on_the_day() {
        assert_eq!(time_at("09:30"), utc(1, 9, 30));
    }

    #[test]
    fn time_at_plus_one_falls_on_the_day_after() {
        assert_eq!(time_at("07:00+1"), utc(2, 7, 0));
    }

    #[test]
    #[should_panic]
    fn time_at_rejects_an_unrecognized_format() {
        time_at("9:30am");
    }

    #[test]
    fn event_at_sets_start_and_end() {
        let event = event_at("09:00-10:30").build();
        assert_eq!((event.start, event.end), (Some(time_at("09:00")), Some(time_at("10:30"))));
    }

    #[test]
    fn event_at_can_end_on_the_day_after_it_starts() {
        let event = event_at("20:00-07:00+1").build();
        assert_eq!((event.start, event.end), (Some(time_at("20:00")), Some(time_at("07:00+1"))));
        assert!(event.end > event.start);
    }

    #[test]
    fn event_at_defaults_the_summary_to_event() {
        assert_eq!(event_at("09:00-10:00").build().summary.as_deref(), Some("Event"));
    }

    #[test]
    fn event_at_overrides_apply_and_take_precedence() {
        let event = event_at("09:00-10:00").id("abc123").summary("Focus block").priority(1).build();
        assert_eq!((event.id.as_deref(), event.summary.as_deref(), event.priority), (Some("abc123"), Some("Focus block"), Some(1)));
    }
}
