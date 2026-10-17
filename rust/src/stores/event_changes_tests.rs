//! The module's tests, against the fake Google.

use super::*;
use crate::testing::app::{test_app, TestApp};
use crate::testing::{event_at, time_at};
use crate::write_lock::WRITE_LOCK;
use serde_json::Value;

fn day() -> Vec<Event> {
    vec![
        event_at("09:00-10:00").id("email").summary("Email").build(),
        event_at("10:00-11:00").id("report").summary("Report").build(),
        event_at("11:00-11:30").id("coffee").summary("Coffee with Sam").build(),
        event_at("11:30-12:30").id("gym").summary("Gym").build(),
        event_at("12:30-13:15").id("lunch").summary("Lunch").build(),
        event_at("13:15-13:30").id("call").summary("Call").build(),
        event_at("23:00-07:00+1").id("s1").summary("Sleep").sleep().build(),
    ]
}

struct Fixture {
    t: TestApp,
    changes: EventChanges,
}

fn fixture(events: Vec<Event>, with_cancellations: bool) -> Fixture {
    let t = test_app();
    for event in &events {
        t.seed_event(event);
    }
    let client = ActionCalendar::new(t.app.calendar_client().unwrap(), t.app.actions().unwrap());
    let cancellations = with_cancellations.then(|| t.app.cancellations().unwrap());
    t.fake.clear_requests();
    Fixture { t, changes: EventChanges::new(client, cancellations) }
}

fn mv(event_id: &str, start: &str, end: &str) -> Event {
    Event { id: Some(event_id.into()), start: Some(time_at(start)), end: Some(time_at(end)), ..Default::default() }
}

fn cancel(event_id: &str, counts: bool) -> Cancel {
    Cancel { event_id: event_id.into(), counts_against_follow_through: counts, etag: None }
}

/// What `check` takes, Python's keyword arguments.
#[derive(Default)]
struct Asked {
    updates: Vec<Event>,
    creates: Vec<Event>,
    cancels: Vec<Cancel>,
    history_until: Option<Moment>,
    allow_compacted: bool,
}

impl Fixture {
    fn check(&self, asked: Asked) -> Result<Batch, ChangeError> {
        self.changes.check(asked.updates, asked.creates, asked.cancels, asked.history_until, asked.allow_compacted).unwrap()
    }

    fn apply(&self, batch: &Batch, source: &str) -> Vec<Event> {
        let _held = WRITE_LOCK.hold();
        self.changes.apply(batch, source).unwrap()
    }

    fn event(&self, id: &str) -> Event {
        self.t.app.calendar_client().unwrap().get_event(id).unwrap()
    }

    fn writes(&self) -> usize {
        self.t.fake.requests().iter().filter(|r| r.method != "GET" && r.path.contains("/events")).count()
    }
}

fn refused(result: Result<Batch, ChangeError>) -> ChangeError {
    match result {
        Ok(_) => panic!("not refused"),
        Err(error) => error,
    }
}

// -- one event --------------------------------------------------------------------------

#[test]
fn a_move_into_free_time_is_written() {
    let f = fixture(day(), false);

    let written = f.apply(&f.check(Asked { updates: vec![mv("call", "14:00", "14:15")], ..Default::default() }).unwrap(), "update_event");

    let moved: Vec<(Option<String>, Option<Moment>)> = written.into_iter().map(|e| (e.id, e.start)).collect();
    assert_eq!(moved, vec![(Some("call".to_string()), Some(time_at("14:00")))]);
}

#[test]
fn an_update_that_doesnt_move_it_is_never_checked_for_overlaps() {
    let mut events = day();
    events.push(event_at("09:30-09:45").id("clash").summary("Clash").build()); // Already overlapping Email.
    let f = fixture(events, false);

    f.apply(&f.check(Asked { updates: vec![Event { id: Some("email".into()), summary: Some("Inbox".into()), ..Default::default() }], ..Default::default() }).unwrap(), "update_event");

    assert_eq!(f.event("email").summary.as_deref(), Some("Inbox"));
}

#[test]
fn overlaps_the_batch_doesnt_cause_are_left_alone() {
    let mut events = day();
    events.push(event_at("09:30-09:45").id("clash").summary("Clash").build());
    let f = fixture(events, false);

    assert!(f.check(Asked { updates: vec![mv("report", "10:00", "10:45")], ..Default::default() }).is_ok());
}

// -- overlaps -----------------------------------------------------------------------------
// Example 1 of the design: move Lunch earlier, shifting Gym.

#[test]
fn a_move_into_another_event_is_refused_with_everything_to_fix_it_at_once() {
    let f = fixture(day(), false);

    let error = refused(f.check(Asked { updates: vec![mv("lunch", "11:30", "12:15")], ..Default::default() }));

    let message = &error.message;
    assert!(message.starts_with(RESEND_RULE));
    assert!(message.contains("- 'Lunch' (11:30–12:15, as asked) overlaps 'Gym' (11:30–12:30)."), "{message}");
    // The stretch it touches, through the night, as it would leave it.
    assert!(message.contains("  11:30–12:15  Lunch *  lunch"), "{message}");
    assert!(message.contains("  13:15–13:30  Call  call"), "{message}");
    assert!(message.contains("  23:00–07:00  Sleep  s1  (end of day)"), "{message}");
    assert_eq!(error.categories, vec!["overlap".to_string()]);
    assert_eq!(f.writes(), 0);
}

#[test]
fn every_problem_is_named_at_once() {
    let f = fixture(day(), false);

    let error = refused(f.check(Asked { updates: vec![mv("lunch", "11:30", "12:15"), mv("call", "09:30", "09:30")], ..Default::default() }));

    assert!(error.message.contains("'Lunch'") && error.message.contains("'Gym'"));
    assert!(error.message.contains("'Call' (09:30–09:30, as asked) would end no later than it starts"), "{}", error.message);
    assert_eq!(error.categories, vec!["nonpositive_length".to_string(), "overlap".to_string()]);
}

#[test]
fn moving_whats_in_the_way_in_the_same_batch_passes() {
    let f = fixture(day(), false);
    let batch = f.check(Asked { updates: vec![mv("lunch", "11:30", "12:15"), mv("gym", "12:15", "13:15")], ..Default::default() }).unwrap();

    f.apply(&batch, "update_event");

    let gym = f.event("gym");
    assert_eq!((gym.start, gym.end), (Some(time_at("12:15")), Some(time_at("13:15"))));
    let text = timeline(&batch).unwrap().text;
    assert!(text.contains("Lunch · ⇠1h00m (was 12:30–13:15)"), "{text}");
    assert!(text.contains("Gym · ⇢45m (was 11:30–12:30)"), "{text}");
}

#[test]
fn an_event_can_have_only_one_change() {
    let f = fixture(day(), false);

    let error = refused(f.check(Asked { updates: vec![mv("gym", "12:00", "13:00")], cancels: vec![cancel("gym", false)], ..Default::default() }));

    assert!(error.message.contains("event gym is in the batch twice"), "{}", error.message);
    assert_eq!(error.categories, vec!["duplicate_change".to_string()]);
}

#[test]
fn a_new_event_is_checked_too() {
    let f = fixture(day(), false);

    let error = refused(f.check(Asked {
        creates: vec![Event { summary: Some("Nap".into()), start: Some(time_at("10:30")), end: Some(time_at("11:15")), ..Default::default() }],
        ..Default::default()
    }));

    assert!(error.message.contains("'Report' (10:00–11:00) overlaps 'Nap' (10:30–11:15, as asked)"), "{}", error.message);
}

#[test]
fn a_new_event_needs_a_start_and_an_end() {
    let f = fixture(day(), false);

    let error = refused(f.check(Asked { creates: vec![Event { summary: Some("Nap".into()), start: Some(time_at("14:00")), ..Default::default() }], ..Default::default() }));

    assert!(error.message.contains("new event 1 ('Nap') needs a start and an end"), "{}", error.message);
}

// -- cancels ---------------------------------------------------------------------------------
// Example 2 of the design: move Gym earlier, cancelling Coffee.

fn recorded(f: &Fixture) -> Vec<(String, String)> {
    let text = |row: &crate::sheet::rows::Row, name: &str| row.get(name).and_then(Value::as_str).unwrap_or_default().to_string();
    f.t.app.cancellations().unwrap().all().unwrap().iter().map(crate::sheet::rows::RowItem::to_row).collect::<Vec<_>>().iter().map(|r| (text(r, "id"), text(r, "source"))).collect()
}

#[test]
fn cancelling_whats_in_the_way_makes_room() {
    let f = fixture(day(), true);
    let batch = f.check(Asked { updates: vec![mv("gym", "11:00", "12:00")], cancels: vec![cancel("coffee", false)], ..Default::default() }).unwrap();

    f.apply(&batch, "update_event");

    assert!(f.event("coffee").is_cancelled());
    assert!(recorded(&f).is_empty());
    assert!(timeline(&batch).unwrap().text.contains("✕ Coffee with Sam · cancelled"));
}

#[test]
fn a_cancel_that_counts_is_recorded_against_follow_through() {
    let f = fixture(day(), true);

    f.apply(&f.check(Asked { cancels: vec![cancel("coffee", true)], ..Default::default() }).unwrap(), "delete_event");

    let rows = recorded(&f);
    assert!(!rows.is_empty() && rows.iter().all(|(id, source)| id.starts_with("coffee/") && source == "delete_event"), "{rows:?}");
}

#[test]
fn an_unknown_or_cancelled_event_is_refused() {
    let mut events = day();
    events[0].status = Some("cancelled".into());
    let f = fixture(events, false);

    let error = refused(f.check(Asked { cancels: vec![cancel("nope", false), cancel("email", false)], ..Default::default() }));

    assert!(error.message.contains("there's no event 'nope'") && error.message.contains("is cancelled already"), "{}", error.message);
    assert_eq!(error.categories, vec!["unknown_event".to_string()]);
}

// -- history -----------------------------------------------------------------------------------
// The last compaction ran at 10:30: Email (over by then) and Report (still
// going on) are history.

fn until() -> Option<Moment> {
    Some(time_at("10:30"))
}

#[test]
fn what_started_before_the_last_compaction_cant_be_changed() {
    let batches = vec![
        Asked { updates: vec![Event { id: Some("email".into()), summary: Some("Inbox".into()), ..Default::default() }], ..Default::default() },
        Asked { updates: vec![mv("report", "10:15", "11:00")], ..Default::default() },
        Asked { cancels: vec![cancel("email", false)], ..Default::default() },
    ];
    let f = fixture(day(), false);

    for batch in batches {
        let error = refused(f.check(Asked { history_until: until(), ..batch }));
        assert!(error.message.contains("is history"), "{}", error.message);
        assert_eq!(error.categories, vec!["compacted".to_string()]);
    }
}

#[test]
fn nothing_can_be_put_before_it() {
    let batches = vec![
        Asked { creates: vec![Event { summary: Some("Nap".into()), start: Some(time_at("08:00")), end: Some(time_at("08:30")), ..Default::default() }], ..Default::default() },
        Asked { updates: vec![mv("lunch", "08:00", "08:30")], ..Default::default() },
    ];
    let f = fixture(day(), false);

    for batch in batches {
        let error = refused(f.check(Asked { history_until: until(), ..batch }));
        assert!(error.message.contains("can't start before then"), "{}", error.message);
        assert_eq!(error.categories, vec!["compacted".to_string()]);
    }
}

#[test]
fn unless_the_user_has_approved_changing_history() {
    let f = fixture(day(), false);

    let batch = f
        .check(Asked {
            updates: vec![Event { id: Some("email".into()), summary: Some("Inbox".into()), ..Default::default() }],
            history_until: until(),
            allow_compacted: true,
            ..Default::default()
        })
        .unwrap();
    f.apply(&batch, "update_event");

    assert_eq!(f.event("email").summary.as_deref(), Some("Inbox"));
}

#[test]
fn an_event_still_going_on_may_run_on() {
    let f = fixture(day(), false);
    let end_at = |at: &str| Asked { updates: vec![Event { id: Some("report".into()), end: Some(time_at(at)), ..Default::default() }], history_until: until(), ..Default::default() };

    assert!(f.check(end_at("10:45")).is_ok());

    assert!(refused(f.check(end_at("10:20"))).message.contains("is history"));
}

#[test]
fn without_a_compaction_nothing_is_history() {
    let f = fixture(day(), false);

    assert!(f.check(Asked { updates: vec![Event { id: Some("email".into()), summary: Some("Inbox".into()), ..Default::default() }], ..Default::default() }).is_ok());
}

// -- etags -------------------------------------------------------------------------

#[test]
fn a_change_from_the_etag_last_seen_is_made() {
    let f = fixture(day(), false);
    let etag = f.event("email").etag.0.unwrap();

    let renamed = Event { id: Some("email".into()), summary: Some("Inbox".into()), etag: Etag(Some(etag.clone())), ..Default::default() };
    let written = f.apply(&f.check(Asked { updates: vec![renamed], ..Default::default() }).unwrap(), "update_event");

    assert_eq!(written[0].summary.as_deref(), Some("Inbox"));
    assert_ne!(written[0].etag.0.as_deref(), Some(etag.as_str()));
    assert_eq!(f.t.fake.requests().iter().filter(|r| r.method == "PATCH").last().unwrap().if_match.as_deref(), Some(etag.as_str()));
}

#[test]
fn a_change_from_an_older_etag_is_stale_with_the_event_as_it_is_now() {
    let f = fixture(day(), false);
    let etag = f.event("email").etag.0.unwrap();
    f.apply(&f.check(Asked { updates: vec![Event { id: Some("email".into()), summary: Some("Inbox".into()), ..Default::default() }], ..Default::default() }).unwrap(), "update_event");

    let renamed = Event { id: Some("email".into()), summary: Some("Mail".into()), etag: Etag(Some(etag.clone())), ..Default::default() };
    let error = f.changes.check(vec![renamed], vec![], vec![], None, false).unwrap_err();
    let stale = &error.downcast_ref::<StaleEvents>().unwrap().items[0];
    assert_eq!(stale.event.as_ref().unwrap().summary.as_deref(), Some("Inbox"));

    let cancel = Cancel { etag: Some(etag), ..cancel("email", false) };
    assert!(f.changes.check(vec![], vec![], vec![cancel], None, false).unwrap_err().downcast_ref::<StaleEvents>().is_some());
    assert_eq!(f.event("email").summary.as_deref(), Some("Inbox"));
}

#[test]
fn a_change_made_elsewhere_after_the_check_is_stale_when_its_written() {
    let f = fixture(day(), false);
    let etag = f.event("email").etag.0.unwrap();
    let renamed = Event { id: Some("email".into()), summary: Some("Mail".into()), etag: Etag(Some(etag)), ..Default::default() };
    let batch = f.check(Asked { updates: vec![renamed], ..Default::default() }).unwrap();
    // Changed by someone else between the check and the write.
    f.apply(&f.check(Asked { updates: vec![Event { id: Some("email".into()), summary: Some("Inbox".into()), ..Default::default() }], ..Default::default() }).unwrap(), "update_event");

    let _held = WRITE_LOCK.hold();
    let error = f.changes.apply(&batch, "update_event").unwrap_err();
    let stopped = error.downcast_ref::<StaleEvents>().unwrap();
    assert_eq!(stopped.items[0].event.as_ref().unwrap().summary.as_deref(), Some("Inbox"));
    // It said how far it got: nothing written before it.
    assert_eq!(stopped.partly.as_ref().map(|p| (p.written.len(), p.of)), Some((0, 1)));
}

#[test]
fn a_batch_stopped_partway_says_what_it_wrote() {
    let f = fixture(day(), false);
    let etag = |id: &str| Etag(f.event(id).etag.0);
    let batch = f
        .check(Asked {
            updates: vec![
                Event { id: Some("email".into()), summary: Some("Inbox".into()), etag: etag("email"), ..Default::default() },
                Event { id: Some("report".into()), summary: Some("Draft".into()), etag: etag("report"), ..Default::default() },
            ],
            creates: vec![event_at("15:00-16:00").summary("Walk").build()],
            ..Default::default()
        })
        .unwrap();
    // The report changed elsewhere between the check and the write.
    f.apply(&f.check(Asked { updates: vec![Event { id: Some("report".into()), summary: Some("Report v2".into()), ..Default::default() }], ..Default::default() }).unwrap(), "update_event");

    let _held = WRITE_LOCK.hold();
    let error = f.changes.apply(&batch, "update_event").unwrap_err();

    let stopped = error.downcast_ref::<StaleEvents>().unwrap();
    let partly = stopped.partly.as_ref().unwrap();
    assert_eq!((partly.written.clone(), partly.of), (vec!["updated email".to_string()], 3));
    assert_eq!(stopped.items[0].event_id, "report");
    // As the client's told it.
    let told = crate::sheet::rows::Conflicts {
        items: vec![crate::sheet::rows::Conflict {
            kind: "event",
            id: "report".into(),
            expected: crate::sheet::rows::Seen::Etag("e".into()),
            current: None,
        }],
        partly: stopped.partly.clone(),
    }
    .to_string();
    assert!(told.starts_with("1 of the batch's 3 changes were written (updated email) before"), "{told}");
    assert!(told.contains("Sending the batch again"), "{told}");
}
