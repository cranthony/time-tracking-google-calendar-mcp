//! TestPrepare and TestTheCompactionWindow.

use super::*;

#[test]
fn offers_the_days_notes_with_candidates_and_the_days_events() {
    let setup = Setup::standard();

    let context = setup.prepare();

    assert_eq!(ids(&context.notes, |n| n.id.as_str()), vec![setup.note_id(2), setup.note_id(3)]);
    assert_eq!(context.notes[0].description.as_deref(), Some("email"));
    // 09:05 is inside Email, adjacent to Report (starts 10:00, within the
    // hour window) -- nearest first.
    assert_eq!(context.notes[0].candidates[..2], strings(&["e1", "e2"]));
    assert_eq!(event_ids(&context), strings(&["e1", "e2", "e3", "s1"]));
    assert_eq!(context.now, Some(time_at("11:30")));
    assert_eq!(context.day_end, Some(time_at("07:00+1")));
    assert_eq!(context.remaining_note_count, 0);
    assert_eq!(context.open_compaction, None);
    assert!(DECISION_GUIDE.contains("SILENCE MEANS ON SCHEDULE"));
    assert!(DECISION_GUIDE.contains("READ EACH NOTE'S TENSE"));
    assert_eq!(context.to_json()["instructions"], json!(DECISION_GUIDE));
}

#[test]
fn the_timeline_shows_a_future_event_only_when_a_note_is_near_it() {
    // "starting lunch" at 11:20 may start the lunch planned for noon.
    let context = Setup::new(&[("11:20", "starting lunch")], None, "11:30").prepare();

    // (The day starts at the note, the first of it.) Sleep is still ahead
    // and nowhere near a note: offered, but not shown.
    assert_eq!(event_ids(&context), strings(&["e3", "s1"]));
    assert_eq!(context.notes[0].candidates, strings(&["e3"]));
    assert_eq!(timeline_event_ids(context.timeline.as_ref().unwrap()), strings(&["e3"]));
}

#[test]
fn offers_the_notes_beside_the_planned_events_as_a_timeline() {
    let context = Setup::standard().prepare();
    let timeline = context.timeline.unwrap();

    let texts: Vec<Option<String>> = timeline.notes.iter().map(|n| n.text.clone()).collect();
    assert_eq!(texts, vec![Some("email".to_string()), Some("report".to_string())]);
    // Lunch (12:00) and Sleep are still ahead, and no note is near them.
    assert_eq!(timeline_event_ids(&timeline), strings(&["e1", "e2"]));
    assert!(timeline.events.iter().all(|e| e.status == "planned"));
    assert!(timeline.text.contains("● email"));
    assert!(timeline.text.contains("┌ Email"));
}

#[test]
fn with_no_uncompacted_notes_there_is_nothing_to_offer() {
    let context = Setup::new(&[], None, "11:30").prepare();

    assert!(context.notes.is_empty() && context.events.is_empty());
}

#[test]
fn offers_every_day_of_notes_up_to_now() {
    let setup = Setup::standard();
    setup.append_note("08:00+1", Some("next day"));
    setup.set_now("09:00+1");

    let context = setup.prepare();

    assert_eq!(ids(&context.notes, |n| n.id.as_str()), vec![setup.note_id(2), setup.note_id(3), setup.note_id(4)]);
    assert_eq!(context.remaining_note_count, 0);
    let days: Vec<(String, Vec<String>)> = context.days.unwrap().into_iter().map(|d| (d.label, d.note_ids)).collect();
    assert_eq!(
        days,
        vec![("Thu 01 Jan".to_string(), vec![setup.note_id(2), setup.note_id(3)]), ("Fri 02 Jan".to_string(), vec![setup.note_id(4)])]
    );
}

#[test]
fn a_single_day_lists_no_days() {
    assert!(Setup::standard().prepare().days.is_none());
}

#[test]
fn leaves_notes_written_after_now_for_later() {
    let setup = Setup::standard();
    setup.append_note("11:45", Some("not yet"));

    let context = setup.prepare();

    assert_eq!(ids(&context.notes, |n| n.id.as_str()), vec![setup.note_id(2), setup.note_id(3)]);
    assert_eq!(context.remaining_note_count, 1);
}

#[test]
fn the_days_run_on_to_now_each_ending_with_its_night() {
    let setup = Setup::standard();
    setup.set_now("12:00+1");

    let context = setup.prepare();

    let days: Vec<(Moment, Moment)> = context.days.unwrap().iter().map(|d| (d.compaction_window_start, d.day_end)).collect();
    assert_eq!(days, vec![(time_at("09:05"), time_at("07:00+1")), (time_at("07:00+1"), time_at("07:00+1") + Duration::days(1))]);
    assert_eq!(context.now, Some(time_at("12:00+1")));
}

#[test]
fn skips_compacted_notes() {
    let setup = Setup::standard();
    let first = setup.note_id(2);
    setup.call(|c| c.notes.mark_compacted(&[first], "old").unwrap());

    assert_eq!(ids(&setup.prepare().notes, |n| n.id.as_str()), vec![setup.note_id(3)]);
}

#[test]
fn reports_an_open_compaction() {
    let setup = Setup::standard();
    let planned = setup.dry_run(setup.email_then_report());
    let id = planned.compaction_id.clone().unwrap();
    setup.set_status(&id, APPLYING);

    assert_eq!(setup.prepare().open_compaction, Some(id));
}

// -- the compaction window ----------------------------------------------------------
// The notes are on the day after the first; the night before them is the
// first day's evening.

pub(super) fn window_events() -> Vec<Event> {
    vec![
        event_at("19:00-20:00").id("r0").summary("Reading").priority(2).build(),
        event_at("20:00-07:00+1").id("s0").summary("Sleep").priority(0).sleep().build(),
        event_at("07:00+1-08:00+1").id("gr").summary("Getting Ready").priority(2).build(),
        event_at("09:00+1-10:00+1").id("w1").summary("Work").priority(2).build(),
        event_at("10:00+1-11:00+1").id("w2").summary("Email").priority(2).build(),
        Event {
            id: Some("s1".into()),
            summary: Some("Sleep".into()),
            start: Some(time_at("22:00+1")),
            end: Some(time_at("07:00+1") + Duration::days(1)),
            priority: Some(0),
            is_end_of_day_sleep: Some(true),
            ..Default::default()
        },
    ]
}

fn window_setup(note_at: &str) -> Setup {
    Setup::new(&[(note_at, "note")], Some(window_events()), "11:30+1")
}

/// Record a finished compaction whose `now` was `now`, as if the previous
/// round had been compacted then.
fn stamp_a_compaction_at(setup: &Setup, now: &str) {
    setup.start_plain("prev", time_at(now));
    setup.set_status("prev", STAMPED);
}

#[test]
fn with_no_earlier_compaction_it_starts_when_the_night_before_ended() {
    let context = window_setup("09:05+1").prepare();

    assert_eq!(context.compaction_window_start, Some(time_at("07:00+1")));
    // The night's sleep ended right as the window starts, so it's offered
    // too -- it may need stretching ("slept in").
    assert_eq!(event_ids(&context), strings(&["s0", "gr", "w1", "w2", "s1"]));
    assert_eq!(context.day_end, Some(time_at("07:00+1") + Duration::days(1)));
}

#[test]
fn a_compaction_before_the_night_settles_the_rest_of_its_day_first() {
    // Compacted at 19:45, before bed: reading ran on past it, and the night
    // hadn't begun -- so they're settled first, with no notes of their own,
    // before the morning the note is in.
    let setup = window_setup("09:05+1");
    stamp_a_compaction_at(&setup, "19:45");

    let context = setup.prepare();

    assert_eq!(context.compaction_window_start, Some(time_at("19:45")));
    let days: Vec<(Moment, Vec<String>)> = context.days.as_ref().unwrap().iter().map(|d| (d.compaction_window_start, d.note_ids.clone())).collect();
    assert_eq!(days, vec![(time_at("19:45"), vec![]), (time_at("07:00+1"), vec![setup.note_id(2)])]);
    assert_eq!(event_ids(&context), strings(&["r0", "s0", "gr", "w1", "w2", "s1"]));
}

#[test]
fn the_days_between_the_last_compaction_and_the_notes_are_proposed_too() {
    // Notes two days on: the days in between, with no notes, are still
    // confirmed -- as planned, unless the user says otherwise.
    let setup = window_setup("09:05+1");
    stamp_a_compaction_at(&setup, "19:45");
    let later = time_at("09:05+1") + Duration::days(2);
    let note = setup.note_id(2);
    setup.call(|c| c.notes.edit(&note, Some(later), None, None).unwrap());
    setup.set_moment(later + Duration::hours(1));

    let context = setup.prepare();

    assert_eq!(context.compaction_window_start, Some(time_at("19:45")));
    let days = context.days.unwrap();
    assert_eq!(days.last().unwrap().note_ids, vec![setup.note_id(2)]);
    assert!(days[..days.len() - 1].iter().all(|d| d.note_ids.is_empty()));
}

#[test]
fn a_later_compaction_the_same_day_starts_the_window_there() {
    // Compacted at 10:05, just after work ended -- so work is offered too,
    // to be stretched if it ran late, but nothing before it is.
    let setup = window_setup("10:20+1");
    stamp_a_compaction_at(&setup, "10:05+1");

    let context = setup.prepare();

    assert_eq!(context.compaction_window_start, Some(time_at("10:05+1")));
    assert_eq!(event_ids(&context), strings(&["w1", "w2", "s1"]));
}

#[test]
fn the_event_just_before_the_window_can_be_stretched() {
    // Compacted as work ended: the email after it hadn't started yet.
    let setup = window_setup("10:20+1");
    stamp_a_compaction_at(&setup, "10:00+1");

    let result = setup.dry_run(vec![
        EventDecision { end_note: Some(setup.note_id(2)), ..keep("w1") },
        EventDecision { start_note: Some(setup.note_id(2)), ..keep("w2") },
    ]);

    assert_eq!(change(&result, "w1").after.as_ref().unwrap().end, Some(time_at("10:20+1")));
}

#[test]
fn an_event_that_ended_longer_before_the_window_is_not_offered() {
    let setup = window_setup("10:40+1");
    stamp_a_compaction_at(&setup, "10:30+1");

    assert_eq!(event_ids(&setup.prepare()), strings(&["w2", "s1"]));
}

fn compacted_note(setup: &Setup, at: &str, description: &str) {
    setup.append_note(at, Some(description));
    let last = setup.notes(true).iter().map(|n| n.row).max().unwrap();
    let id = setup.note_id(last);
    setup.call(|c| c.notes.mark_compacted(&[id], "prev").unwrap());
}

#[test]
fn the_last_compacted_note_is_offered_as_context() {
    let setup = window_setup("10:20+1");
    compacted_note(&setup, "09:30+1", "earlier");
    compacted_note(&setup, "09:55+1", "starting email");
    stamp_a_compaction_at(&setup, "10:05+1");

    let context = setup.prepare();

    let previous = context.previous_note.as_ref().unwrap();
    assert_eq!(previous.description.as_deref(), Some("starting email"));
    assert_eq!(previous.timestamp, time_at("09:55+1"));
    // Context only: it's not one of this round's notes.
    assert_eq!(ids(&context.notes, |n| n.id.as_str()), vec![setup.note_id(2)]);
    assert!(context.timeline.unwrap().text.contains("✓ starting email (compacted)"));
}

#[test]
fn the_last_compacted_note_is_offered_however_long_before_the_window() {
    let setup = window_setup("10:20+1");
    compacted_note(&setup, "07:30+1", "starting email");
    stamp_a_compaction_at(&setup, "10:05+1");

    let previous = setup.prepare().previous_note.unwrap();

    assert_eq!(previous.description.as_deref(), Some("starting email"));
    assert_eq!(previous.timestamp, time_at("07:30+1"));
}

#[test]
fn both_timelines_show_the_latest_compacted_note_and_the_last_compaction() {
    // However long before the window the note was written.
    let setup = window_setup("10:20+1");
    compacted_note(&setup, "09:45+1", "starting email");
    stamp_a_compaction_at(&setup, "10:05+1");

    let before = setup.prepare().timeline.unwrap();
    let after = setup.dry_run(vec![]).timeline.unwrap();

    for timeline in [before, after] {
        assert_eq!(timeline.last_compaction, Some(time_at("10:05+1")));
        assert!(timeline.text.contains("✓ starting email (compacted)"), "{}", timeline.text);
        assert!(timeline.text.contains("10:05 ┄┄ last compaction ┄"), "{}", timeline.text);
    }
}

#[test]
fn a_later_round_reads_its_notes_and_the_previous_note_in_one_request() {
    // Google Sheets caps read requests per minute, so the notes tab costs a
    // later round's prepare one read, with the previous note found among
    // its rows.
    let setup = Setup::new(&[("09:05+1", "email"), ("09:58+1", "done with work")], Some(window_events()), "10:05+1");
    let first = setup.dry_run(vec![]).compaction_id.unwrap();
    setup.commit(&first);
    setup.append_note("10:20+1", Some("report"));
    setup.set_now("10:30+1");
    let notes_tab = setup.compactor.notes.whole_tab().sheet_id;
    setup.t.fake.clear_requests();

    let context = setup.prepare();

    assert_eq!(context.previous_note.unwrap().description.as_deref(), Some("done with work"));
    let reads = setup
        .t
        .fake
        .requests()
        .into_iter()
        .filter(|r| r.path.ends_with("values:batchGetByDataFilter"))
        .filter(|r| r.body["dataFilters"].as_array().unwrap().iter().any(|f| f["gridRange"]["sheetId"] == json!(notes_tab)))
        .count();
    assert_eq!(reads, 1);
}

#[test]
fn a_note_written_before_the_planned_wake_up_starts_the_day_there() {
    let context = window_setup("06:40+1").prepare();

    assert_eq!(context.compaction_window_start, Some(time_at("06:40+1")));
    // The night's sleep runs into the day, so it's offered (to be
    // shortened), but it isn't the day's own end.
    assert_eq!(event_ids(&context), strings(&["s0", "gr", "w1", "w2", "s1"]));
    assert_eq!(context.day_end, Some(time_at("07:00+1") + Duration::days(1)));
}
