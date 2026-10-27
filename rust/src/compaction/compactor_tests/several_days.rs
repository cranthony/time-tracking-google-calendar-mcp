//! TestSeveralDays: notes on two days -- an email on the first, and on the
//! second a late wake-up, then work. The night's sleep, `s1`, ran from the
//! first day's bedtime to the second day's morning.

use super::*;

fn events() -> Vec<Event> {
    vec![
        event_at("09:00-10:00").id("e1").summary("Email").priority(2).build(),
        event_at("20:00-07:00+1").id("s1").summary("Sleep").priority(0).build(),
        event_at("07:00+1-08:00+1").id("gr1").summary("Getting Ready").priority(2).build(),
        event_at("08:00+1-12:00+1").id("w1").summary("Work").priority(3).build(),
        event_at("22:00+1-23:30+1").id("s2").summary("Sleep").priority(0).build(),
    ]
}

fn setup_with(notes: &[(&str, &str)]) -> Setup {
    Setup::new(notes, Some(events()), "09:00+1")
}

fn setup() -> Setup {
    setup_with(&[("09:05", "email"), ("08:30+1", "finally up"), ("08:50+1", "working")])
}

/// "finally up" ends the night, which takes the next morning's getting
/// ready later with it -- and work, after it.
fn late_wake_up(setup: &Setup) -> Vec<EventDecision> {
    vec![
        EventDecision { start_note: Some(setup.note_id(2)), ..keep("e1") },
        EventDecision { end_note: Some(setup.note_id(3)), ..keep("s1") },
        EventDecision { start_note: Some(setup.note_id(3)), end: Some(time_at("09:00+1")), ..keep("gr1") },
        EventDecision { start: Some(time_at("09:00+1")), ..keep("w1") },
    ]
}

fn only_day(setup: &Setup, compaction_id: &str) -> JournalCompaction {
    let mut days = setup.journal().load_batch(compaction_id).unwrap();
    assert_eq!(days.len(), 1);
    days.remove(0)
}

fn day_change(day: &JournalCompaction, event_id: &str) -> CompactionChange {
    day.changes().into_iter().find(|c| c.event_id.as_deref() == Some(event_id)).unwrap()
}

#[test]
fn every_day_lists_within_one_listing_made_first() {
    // The rest are answered from the first, inside a tool call (see
    // CachedListings).
    let setup = setup();

    setup.dry_run(late_wake_up(&setup));

    let listings: Vec<LoggedRequest> = setup.t.fake.requests().into_iter().filter(|r| r.method == "GET" && r.path.ends_with("/events")).collect();
    assert_eq!(listings.len(), 1, "{listings:?}");
}

#[test]
fn each_day_after_the_first_starts_at_midnight() {
    let context = setup().prepare();

    let days: Vec<(Moment, Moment)> = context.days.as_ref().unwrap().iter().map(|d| (d.compaction_window_start, d.day_end)).collect();
    assert_eq!(days, vec![(time_at("00:00"), time_at("00:00+1")), (time_at("00:00+1"), time_at("10:00+1"))]);
    // The night between them is offered once, and to the second day's first
    // note, to end if it ran late. The next night is too far off.
    assert_eq!(event_ids(&context), strings(&["e1", "s1", "gr1", "w1"]));
    assert!(context.notes[1].candidates.contains(&"s1".to_string()));
}

#[test]
fn the_timeline_heads_each_day_with_its_date_and_only_the_last_has_now() {
    let text = setup().prepare().timeline.unwrap().text;

    let at = |piece: &str| text.find(piece).unwrap_or_else(|| panic!("{piece:?} isn't in {text}"));
    assert!(at("━━ Thu 01 Jan") < at("● email") && at("● email") < at("━━ Fri 02 Jan"));
    assert!(at("━━ Fri 02 Jan") < at("● finally up"));
    assert_eq!(text.matches("┄┄ now").count(), 1);
}

#[test]
fn a_dry_run_plans_and_journals_the_days_as_one_compaction() {
    let setup = setup();

    let planned = setup.dry_run(late_wake_up(&setup));

    let id = planned.compaction_id.clone().unwrap();
    let day = only_day(&setup, &id);
    assert_eq!((day.id.as_str(), day.status.as_str()), (id.as_str(), PROPOSED));
    assert_eq!(change(&planned, "s1").after.as_ref().unwrap().end, Some(time_at("08:30+1")));
    assert_eq!(change(&planned, "w1").after.as_ref().unwrap().start, Some(time_at("09:00+1")));
    let text = &planned.timeline.as_ref().unwrap().text;
    assert_eq!(text.matches("━━ ").count(), 2);
    assert_eq!(text.matches("→ note above set this edge").count(), 1);
    assert!(planned.message.contains("over 2 days"), "{}", planned.message);
}

#[test]
fn the_note_ending_the_night_ends_it_and_starts_the_morning() {
    let setup = setup();

    let planned = setup.dry_run(late_wake_up(&setup));

    let day = only_day(&setup, planned.compaction_id.as_ref().unwrap());
    assert_eq!(day.note_ids, vec![setup.note_id(2), setup.note_id(3), setup.note_id(4)]);
    assert_eq!(day.now, time_at("09:00+1"));
    // "finally up" ends the night, and starts getting ready.
    let getting_ready = day.decisions.iter().find(|d| d.event_id.as_deref() == Some("gr1")).unwrap();
    assert_eq!(getting_ready.start_note, Some(setup.note_id(3)));
    assert_eq!(change(&planned, "gr1").after.as_ref().unwrap().start, Some(time_at("08:30+1")));
    // The night's new end is just another change: nothing to warn of.
    assert!(!planned.warnings.iter().any(|w| w.contains("now ends at")));
}

#[test]
fn a_later_wake_up_is_checked_against_the_mornings_events() {
    let setup = setup();

    refused(
        setup.try_dry_run(vec![EventDecision { end_note: Some(setup.note_id(3)), ..keep("s1") }], DryRun::default()),
        "(?s)'Sleep'.*overlaps 'Getting Ready'",
    );
}

#[test]
fn a_late_wake_up_settles_the_next_morning_even_without_its_own_notes() {
    let setup = setup_with(&[("09:05", "email"), ("08:30+1", "finally up")]);

    let planned = setup.dry_run(late_wake_up(&setup));

    let id = planned.compaction_id.unwrap();
    let day = only_day(&setup, &id);
    assert_eq!(day.note_ids, vec![setup.note_id(2), setup.note_id(3)]);
    let decided: Vec<String> = day.decisions.iter().filter_map(|d| d.event_id.clone()).collect();
    assert_eq!(decided, strings(&["e1", "s1", "gr1", "w1"]));
    assert_eq!(setup.commit(&id).status, "applied");
    assert!(setup.notes(false).is_empty());
}

#[test]
fn an_early_wake_up_ends_the_night_there() {
    let setup = setup_with(&[("09:05", "email"), ("05:30+1", "woke up early"), ("07:30+1", "breakfast")]);

    let planned = setup.dry_run(vec![EventDecision { end_note: Some(setup.note_id(3)), ..keep("s1") }]);

    let day = only_day(&setup, planned.compaction_id.as_ref().unwrap());
    assert_eq!(day.note_ids, vec![setup.note_id(2), setup.note_id(3), setup.note_id(4)]);
    assert_eq!(day_change(&day, "s1").after.unwrap().end, Some(time_at("05:30+1")));
    assert!(planned.warnings.is_empty(), "{:?}", planned.warnings);
}

#[test]
fn a_note_in_the_night_that_doesnt_end_it_leaves_it_as_it_was() {
    let setup = setup_with(&[("09:05", "email"), ("03:00+1", "can't sleep"), ("07:30+1", "breakfast")]);

    let planned = setup.dry_run(vec![]);

    let day = only_day(&setup, planned.compaction_id.as_ref().unwrap());
    assert_eq!(day.note_ids, vec![setup.note_id(2), setup.note_id(3), setup.note_id(4)]);
    let night = day_change(&day, "s1").after.unwrap();
    assert_eq!(night.end, Some(time_at("07:00+1")));
    assert!(night.description.unwrap().contains("can't sleep"));
}

#[test]
fn the_night_is_decided_once() {
    let setup = setup();
    let mut decisions = late_wake_up(&setup);
    decisions[1].start = Some(time_at("21:00"));

    let planned = setup.dry_run(decisions);

    let day = only_day(&setup, planned.compaction_id.as_ref().unwrap());
    let decided: Vec<String> = day.decisions.iter().filter_map(|d| d.event_id.clone()).collect();
    assert_eq!(decided, strings(&["e1", "s1", "gr1", "w1"]));
    let nights: Vec<CompactionChange> = day.changes().into_iter().filter(|c| c.event_id.as_deref() == Some("s1")).collect();
    assert_eq!(nights.len(), 1);
    let night = nights[0].after.as_ref().unwrap();
    assert_eq!((night.start, night.end), (Some(time_at("21:00")), Some(time_at("08:30+1"))));
}

#[test]
fn a_second_decision_on_the_night_is_refused() {
    let setup = setup();
    let mut decisions = late_wake_up(&setup);
    decisions.push(EventDecision { annotate: Some("slept badly".into()), ..keep("s1") });

    refused(setup.try_dry_run(decisions, DryRun::default()), "event s1 has more than one decision");
}

#[test]
fn a_night_without_sleep_is_cancelled_like_any_event() {
    let setup = setup_with(&[("09:05", "email"), ("03:00+1", "still up, coding"), ("08:30+1", "breakfast")]);

    let planned = setup.dry_run(vec![EventDecision { event_id: Some("s1".into()), ..EventDecision::new("cancel") }]);

    let day = only_day(&setup, planned.compaction_id.as_ref().unwrap());
    assert_eq!(day.note_ids, vec![setup.note_id(2), setup.note_id(3), setup.note_id(4)]);
    assert_eq!(day.now, time_at("09:00+1"));
    let cancelled: Vec<String> = day.changes().into_iter().filter(|c| c.action == "cancel").filter_map(|c| c.event_id).collect();
    assert_eq!(cancelled, strings(&["s1"]));
    // The next morning is settled as planned, so there's nothing to write;
    // the days are still headed by their dates.
    assert!(!day.changes().iter().any(|c| c.event_id.as_deref() == Some("gr1")));
    assert_eq!(planned.timeline.unwrap().text.matches("━━ ").count(), 2);
}

#[test]
fn committing_applies_and_stamps_the_days() {
    let setup = setup();
    let id = setup.dry_run(late_wake_up(&setup)).compaction_id.unwrap();

    let result = setup.commit(&id);

    assert_eq!(result.status, "applied");
    assert!(setup.journal().load_batch(&id).unwrap().iter().all(|d| d.status == STAMPED));
    assert!(setup.notes(false).is_empty());
    assert_eq!(setup.journal().last_stamped_now().unwrap(), Some(time_at("09:00+1")));
    assert_eq!(moment_in(&setup.patch("s1"), "end"), Some(time_at("08:30+1")));
}

/// A plan applied up to getting ready, whose patch fails.
fn failed_on_the_second_day() -> (Setup, String) {
    let setup = setup();
    let id = setup.dry_run(late_wake_up(&setup)).compaction_id.unwrap();
    setup.fail_next("PATCH", "/events/gr1", 500);
    assert!(setup.try_commit(&id).is_err());
    (setup, id)
}

#[test]
fn a_failure_partway_resumes_where_it_stopped() {
    let (setup, id) = failed_on_the_second_day();

    assert_eq!(only_day(&setup, &id).status, APPLYING);
    // Stamped once it's all applied, not before.
    assert_eq!(setup.notes(false).len(), 3);

    assert_eq!(setup.commit(&id).status, "applied");
    assert!(setup.notes(false).is_empty());
}

#[test]
fn abandoning_partway_keeps_what_was_applied_and_the_notes() {
    let (setup, id) = failed_on_the_second_day();

    let result = setup.call(|c| c.abandon(&id)).unwrap();

    assert!(result.message.contains("stay applied"), "{}", result.message);
    let statuses: Vec<String> = setup.journal().load_batch(&id).unwrap().into_iter().map(|d| d.status).collect();
    assert_eq!(statuses, strings(&[ABANDONED]));
    assert_eq!(setup.notes(false).len(), 3);
}

#[test]
fn refuses_when_an_earlier_days_notes_changed_after_the_preview() {
    let setup = setup();
    let id = setup.dry_run(late_wake_up(&setup)).compaction_id.unwrap();
    setup.append_note("10:20", Some("one more"));

    refused(setup.try_commit(&id), "changed since");

    assert!(setup.patches().is_empty());
}

#[test]
fn getting_a_proposal_over_two_days_shows_both() {
    let setup = setup();
    let planned = setup.dry_run(late_wake_up(&setup));

    let proposal = setup.call(|c| c.get_proposal(None, None)).unwrap();

    assert_eq!(proposal.changes, planned.changes);
    assert_eq!(proposal.timeline.as_ref().unwrap().text.matches("━━ ").count(), 2);
    let events = proposal.events.unwrap();
    let night = events.iter().find(|e| e.id == "s1").unwrap();
    assert_eq!((night.status.as_str(), night.end), ("adjusted", Some(time_at("08:30+1"))));
    let notes: Vec<String> = proposal.notes.iter().map(|n| n.id.clone()).collect();
    assert_eq!(notes, vec![setup.note_id(2), setup.note_id(3), setup.note_id(4)]);
}

#[test]
fn an_open_batch_is_reported_by_its_own_id() {
    let setup = setup();
    let id = setup.dry_run(late_wake_up(&setup)).compaction_id.unwrap();
    setup.set_status(&id, APPLYING);

    assert_eq!(setup.prepare().open_compaction, Some(id.clone()));
    refused(setup.try_dry_run(vec![], DryRun::default()), &format!("compaction {id} is applying"));
}

/// A backlog: a round last compacted at 15:00 on the first day, then a
/// note at 18:00 each of nine days, a night after each, and on the
/// eighth day work from 14:00 to 16:00.
fn a_backlog() -> Setup {
    let midnight = time_at("00:00");
    let at = |day: i64, hours: i64| midnight + Duration::days(day) + Duration::hours(hours);
    let mut events: Vec<Event> = (0..10)
        .map(|day| Event { id: Some(format!("s{day}")), summary: Some("Sleep".into()), start: Some(at(day, 22)), end: Some(at(day + 1, 7)), priority: Some(0), ..Default::default() })
        .collect();
    events.push(Event { id: Some("w7".into()), summary: Some("Work".into()), start: Some(at(7, 14)), end: Some(at(7, 16)), priority: Some(2), ..Default::default() });
    let setup = Setup::new(&[], Some(events), "11:30");
    setup.start_plain("prev", at(0, 15));
    setup.set_status("prev", STAMPED);
    for day in 0..9 {
        let note = NotedTime { timestamp: at(day, 18), description: Some(format!("day {day}")), compaction_id: None };
        setup.call(|c| c.notes.append(&note, None).unwrap());
    }
    setup.set_moment(at(8, 19));
    setup
}

#[test]
fn takes_on_at_most_a_week_at_a_time() {
    let setup = a_backlog();

    let context = setup.prepare();

    // Exactly a week from its start, which takes in parts of eight days and
    // seven of the notes.
    assert_eq!(context.now, Some(time_at("15:00") + Duration::days(MAX_DAYS)));
    assert_eq!(context.days.unwrap().len() as i64, MAX_DAYS + 1);
    assert_eq!(context.remaining_note_count, 2);
}

#[test]
fn a_capped_span_settles_what_is_going_on_up_to_the_cap() {
    let setup = a_backlog();
    let cap = time_at("15:00") + Duration::days(MAX_DAYS);
    assert!(event_ids(&setup.prepare()).contains(&"w7".to_string()));

    let id = setup.dry_run(vec![]).compaction_id.unwrap();
    setup.commit(&id);

    assert_eq!(setup.journal().last_stamped_now().unwrap(), Some(cap));
    // The next round carries on from the cap, the work settled up to there.
    let next = setup.prepare();
    assert_eq!(next.compaction_window_start, Some(cap));
    assert_eq!(next.events.iter().find(|e| e.id == "w7").unwrap().history_until, Some(cap));
    assert_eq!(next.notes.len(), 2);
}
