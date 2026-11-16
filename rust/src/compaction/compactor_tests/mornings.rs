//! TestSleepingInPastTheSpan, TestSleepingIn, TestEditAndDeleteNotes,
//! TestMovingFutureEvents, TestGarbageCollection and TestActions.

use super::*;
use crate::sheet::COMPACTIONS;

fn cancel(event_id: &str) -> EventDecision {
    EventDecision { event_id: Some(event_id.into()), ..EventDecision::new("cancel") }
}

// -- sleeping in past the span ------------------------------------------------------
// A compaction made in the night -- its span ends at now, the night still
// going on -- that says the night ran on past the morning's events:
// they're its to settle too.

fn night_setup() -> Setup {
    let events = vec![
        event_at("21:00-22:00").id("e1").summary("Reading").priority(2).build(),
        event_at("22:00-07:00+1").id("s1").summary("Sleep").priority(0).build(),
        event_at("07:00+1-08:00+1").id("gr1").summary("Getting Ready").priority(2).build(),
        event_at("09:15+1-12:00+1").id("w1").summary("Work").priority(3).build(),
        event_at("22:00+1-23:30+1").id("s2").summary("Sleep").priority(0).build(),
    ];
    Setup::new(&[("21:05", "reading")], Some(events), "05:30+1")
}

#[test]
fn the_span_ends_at_now_with_the_night_going_on() {
    let context = night_setup().prepare();

    assert_eq!(event_ids(&context), strings(&["e1", "s1"]));
    assert_eq!(context.now, Some(time_at("05:30+1")));
    assert_eq!(context.day_end, Some(time_at("06:30+1")));
}

#[test]
fn a_round_in_the_evening_stops_at_now_and_the_next_settles_the_night() {
    let setup = Setup::standard();
    setup.set_now("18:30");
    let first = setup.prepare();
    assert_eq!(first.now, Some(time_at("18:30")));
    assert!(!event_ids(&first).contains(&"s1".to_string()));
    let id = setup.dry_run(setup.email_then_report()).compaction_id.unwrap();
    setup.commit(&id);
    setup.append_note("07:30+1", Some("up"));
    setup.set_now("08:00+1");

    let context = setup.prepare();

    assert_eq!(context.compaction_window_start, Some(time_at("18:30")));
    assert_eq!(event_ids(&context), strings(&["s1"]));
    let planned = setup.dry_run(vec![EventDecision { end_note: Some(setup.note_id(4)), ..keep("s1") }]);
    setup.commit(planned.compaction_id.as_ref().unwrap());
    assert_eq!(moment_in(&setup.patch("s1"), "end"), Some(time_at("07:30+1")));
}

#[test]
fn claude_cant_end_the_night_past_now() {
    let setup = night_setup();

    let message = refused(
        setup.try_dry_run(vec![EventDecision { end: Some(time_at("09:00+1")), ..keep("s1") }, cancel("gr1")], DryRun::default()),
        "decision 1 \\(keep s1\\): its end .* is after this round's now",
    );
    assert!(message.contains("decision 2 (cancel gr1): 'Getting Ready' starts at"), "{message}");
    let Err(error) = setup.try_dry_run(vec![EventDecision { end: Some(time_at("09:00+1")), ..keep("s1") }], DryRun::default()) else { panic!("not refused") };
    assert_eq!(compaction_error(&error).unwrap().categories, strings(&["past_through"]));
}

#[test]
fn the_users_later_end_covers_what_it_runs_over() {
    let setup = night_setup();
    let p = setup.dry_run(vec![]).proposal_id.unwrap();

    let proposal = setup.amend(&p, 1, vec![EventDecision { end: Some(time_at("09:00+1")), ..keep("s1") }]);

    assert_eq!((proposal.through, proposal.layer_end), (time_at("05:30+1"), time_at("09:00+1")));
    let covered = proposal.changes.iter().find(|c| c.event_id.as_deref() == Some("gr1")).unwrap();
    assert_eq!((covered.action.as_str(), covered.implied_by.as_deref()), ("cancel", Some("s1")));
    let shown = proposal.events.as_ref().unwrap().iter().find(|e| e.id == "gr1").unwrap();
    assert_eq!((shown.status.as_str(), shown.covered_by.as_deref(), shown.decided_by.as_deref()), ("cancelled", Some("s1"), None));
    assert_eq!(shown.counts_against_follow_through, Some(true));
    // Work, at 9:15, it doesn't reach.
    assert!(!proposal.changes.iter().any(|c| c.event_id.as_deref() == Some("w1")));
}

// -- moving past the span ------------------------------------------------------------
// Claude records the past: a decision that moves one of the span's events
// past its now is refused. The user may: what it lands on is cut.

fn reading_tomorrow() -> EventDecision {
    EventDecision { start: Some(time_at("09:15+1")), end: Some(time_at("10:15+1")), ..keep("e1") }
}

#[test]
fn claude_cant_move_an_event_past_now() {
    let setup = night_setup();

    refused(setup.try_dry_run(vec![reading_tomorrow()], DryRun::default()), "decision 1 \\(keep e1\\): its start .* and its end .* after this round's now");
}

#[test]
fn the_users_move_past_the_span_cuts_what_it_lands_on() {
    let setup = night_setup();
    let p = setup.dry_run(vec![]).proposal_id.unwrap();

    let proposal = setup.amend(&p, 1, vec![reading_tomorrow()]);

    let work = proposal.changes.iter().find(|c| c.event_id.as_deref() == Some("w1")).unwrap();
    assert_eq!((work.after.as_ref().unwrap().start, work.implied_by.as_deref()), (Some(time_at("10:15+1")), Some("e1")));
    // The next night, it doesn't reach.
    assert!(!proposal.changes.iter().any(|c| c.event_id.as_deref() == Some("s2")));
    assert_eq!(setup.call(|c| c.confirm(&p, 2)).unwrap().status, "applied");
    assert_eq!(setup.event("w1").start, Some(time_at("10:15+1")));
}

#[test]
fn a_move_past_the_span_takes_no_facts_there() {
    let setup = night_setup();
    let p = setup.dry_run(vec![]).proposal_id.unwrap();

    let proposal = setup.amend(&p, 1, vec![EventDecision { facts: Some(crate::testing::facts(json!({"notes": {"self": "a good chapter"}}))), ..reading_tomorrow() }]);

    assert_eq!(proposal.changes.iter().find(|c| c.event_id.as_deref() == Some("e1")).unwrap().after.as_ref().unwrap().facts, None);
}

// -- sleeping in --------------------------------------------------------------------
// The event that ended just before the compaction window -- here, last
// night's sleep -- is offered with it, so a late wake-up can extend it.

fn slept_in() -> Setup {
    let events = vec![
        event_at("09:00-10:00").id("e1").summary("Email").priority(2).build(),
        event_at("20:00-07:00+1").id("s1").summary("Sleep").priority(0).build(),
        event_at("07:00+1-08:00+1").id("gr1").summary("Getting Ready").priority(2).build(),
        event_at("08:00+1-12:00+1").id("w1").summary("Work").priority(3).build(),
        event_at("22:00+1-23:30+1").id("s2").summary("Sleep").priority(0).build(),
    ];
    let setup = Setup::new(&[("09:05", "email")], Some(events), "07:00+1");
    let id = setup.dry_run(vec![]).compaction_id.unwrap();
    setup.commit(&id);
    setup.append_note("08:30+1", Some("finally up"));
    setup.set_now("09:00+1");
    setup.t.fake.clear_requests();
    setup
}

#[test]
fn prepare_offers_it_to_the_days_first_note() {
    let context = slept_in().prepare();

    assert_eq!(event_ids(&context), strings(&["s1", "gr1", "w1"]));
    assert!(context.notes[0].candidates.contains(&"s1".to_string()));
    assert_eq!(context.day_end, Some(time_at("10:00+1")));
}

#[test]
fn a_late_wake_up_covers_and_cuts_the_morning_it_runs_over() {
    let setup = slept_in();

    let planned = setup.dry_run(vec![EventDecision { end_note: Some(setup.note_id(3)), ..keep("s1") }]);

    let ready = change(&planned, "gr1");
    assert_eq!((ready.action.as_str(), ready.reason.as_str(), ready.implied_by.as_deref()), ("cancel", "covered by 'Sleep'", Some("s1")));
    let work = change(&planned, "w1");
    assert_eq!((work.after.as_ref().unwrap().start, work.implied_by.as_deref()), (Some(time_at("08:30+1")), Some("s1")));
    assert!(planned.message.contains("2 of the changes are implied"), "{}", planned.message);
    let text = planned.timeline.as_ref().unwrap().text.split_whitespace().collect::<Vec<_>>().join(" ");
    assert!(text.contains("✕ Getting Ready · covered by Sleep"), "{text}");
    assert!(text.contains("cut by Sleep"), "{text}");

    setup.commit(planned.compaction_id.as_ref().unwrap());

    assert_eq!(moment_in(&setup.patch("s1"), "end"), Some(time_at("08:30+1")));
    assert_eq!(moment_in(&setup.patch("w1"), "start"), Some(time_at("08:30+1")));
    assert!(setup.event("gr1").is_cancelled());
    assert!(!setup.patches().iter().any(|(id, _)| id == "s2"));
}

#[test]
fn a_late_wake_up_extends_it_and_moves_the_rest_of_the_morning_as_decided() {
    let setup = slept_in();
    let planned = setup.dry_run(vec![
        EventDecision { end_note: Some(setup.note_id(3)), ..keep("s1") },
        EventDecision { start_note: Some(setup.note_id(3)), end: Some(time_at("09:00+1")), ..keep("gr1") },
        EventDecision { start: Some(time_at("09:00+1")), ..keep("w1") },
    ]);
    let night = change(&planned, "s1").after.clone().unwrap();
    assert_eq!((night.start, night.end), (Some(time_at("20:00")), Some(time_at("08:30+1"))));
    assert_eq!(change(&planned, "w1").after.as_ref().unwrap().start, Some(time_at("09:00+1")));

    setup.commit(planned.compaction_id.as_ref().unwrap());

    assert_eq!(moment_in(&setup.patch("s1"), "end"), Some(time_at("08:30+1")));
    assert!(!setup.patches().iter().any(|(id, _)| id == "s2"));
}

// -- editing and deleting notes -------------------------------------------------------

#[test]
fn edits_and_deletes_uncompacted_notes() {
    let setup = Setup::standard();

    let edited = setup.call(|c| c.edit_note(&setup.note_id(2), None, Some("inbox zero"), None)).unwrap();
    setup.call(|c| c.delete_note(&setup.note_id(3), None)).unwrap();

    let notes: Vec<(String, Option<String>)> = setup.notes(false).into_iter().map(|n| (n.id(), n.note.description)).collect();
    assert_eq!(notes, vec![(edited.id(), Some("inbox zero".to_string()))]);
}

#[test]
fn a_stale_id_is_a_compaction_error() {
    let setup = Setup::standard();
    let stale = setup.note_id(2);
    setup.call(|c| c.delete_note(&stale, None)).unwrap();

    refused(setup.call(|c| c.edit_note(&stale, Some(time_at("09:10")), None, None)), "there's no note");
}

#[test]
fn a_planned_compaction_does_not_block_it_but_can_no_longer_be_committed() {
    let setup = Setup::standard();
    let id = setup.dry_run(setup.email_then_report()).compaction_id.unwrap();

    setup.call(|c| c.edit_note(&setup.note_id(3), Some(time_at("10:25")), None, None)).unwrap();

    refused(setup.try_commit(&id), "changed since");
}

#[test]
fn refused_while_a_compaction_with_that_note_is_being_applied() {
    for edit in [true, false] {
        let setup = Setup::standard();
        let id = setup.dry_run(setup.email_then_report()).compaction_id.unwrap();
        setup.set_status(&id, APPLYING);
        let note_id = setup.note_id(2);

        if edit {
            refused(setup.call(|c| c.edit_note(&note_id, None, Some("x"), None)), "is applying");
        } else {
            refused(setup.call(|c| c.delete_note(&note_id, None)), "is applying");
        }

        assert_eq!(setup.notes(false)[0].note.description.as_deref(), Some("email"));
    }
}

#[test]
fn a_note_outside_the_compaction_being_applied_can_still_change() {
    let setup = Setup::standard();
    let id = setup.dry_run(setup.email_then_report()).compaction_id.unwrap();
    setup.set_status(&id, APPLYING);
    setup.append_note("08:00+1", Some("next day"));

    setup.call(|c| c.edit_note(&setup.note_id(4), None, Some("tomorrow"), None)).unwrap();

    assert_eq!(setup.notes(false).last().unwrap().note.description.as_deref(), Some("tomorrow"));
}

// -- moving future events ---------------------------------------------------------------

#[test]
fn claude_cant_reschedule_a_future_event() {
    let setup = Setup::standard();
    let mut decisions = setup.email_then_report();
    decisions.push(EventDecision { start: Some(time_at("12:15")), end: Some(time_at("12:45")), ..keep("e3") });

    refused(setup.try_dry_run(decisions, DryRun::default()), "decision 3 \\(keep e3\\): its start .* is after this round's now");
    // Nor cancel one, nor change what happened at it.
    let mut decisions = setup.email_then_report();
    decisions.push(cancel("e3"));
    refused(setup.try_dry_run(decisions, DryRun::default()), "'Lunch' starts at .* not before this round's now");
}

#[test]
fn but_a_keep_may_bring_a_future_events_start_before_now() {
    let setup = Setup::standard();
    let mut decisions = setup.email_then_report();
    decisions[1].end = Some(time_at("11:00"));
    decisions.push(EventDecision { start: Some(time_at("11:00")), end: Some(time_at("11:30")), ..keep("e3") });

    let planned = setup.dry_run(decisions);

    assert_eq!(change(&planned, "e3").after.as_ref().unwrap().start, Some(time_at("11:00")));
}

#[test]
fn the_user_may_move_bedtime_past_through_and_it_commits() {
    let setup = Setup::new(&[("09:05", "email"), ("10:20", "report")], None, "19:45");
    let p = setup.dry_run(setup.email_then_report()).proposal_id.unwrap();

    let proposal = setup.amend(&p, 1, vec![EventDecision { start: Some(time_at("22:30")), end: Some(time_at("08:00") + Duration::days(1)), ..keep("s1") }]);
    let result = setup.call(|c| c.confirm(&p, proposal.revision)).unwrap();

    assert_eq!(result.status, "applied");
    let night = setup.patch("s1");
    assert_eq!((moment_in(&night, "start"), moment_in(&night, "end")), (Some(time_at("22:30")), Some(time_at("08:00") + Duration::days(1))));
}

#[test]
fn with_no_notes_at_all_there_is_nothing_to_compact() {
    let setup = Setup::new(&[], None, "11:30");

    let result = setup.dry_run(vec![EventDecision { start: Some(time_at("12:15")), ..keep("e3") }]);

    assert_eq!(result.status, "nothing_to_compact");
}

// -- garbage collection ---------------------------------------------------------------

#[test]
fn prepare_garbage_collects_the_journal_first() {
    // Past the journal's 100-row budget, with every compaction finished.
    let setup = Setup::standard();
    let rows: Vec<Vec<String>> = (0..101)
        .map(|i| {
            let now = py::isoformat(&(time_at("08:00") - Duration::days(200 - i)));
            strings(&[&format!("old{i}"), "0", "compaction", "", "", "", STAMPED, &format!("{{\"now\": \"{now}\"}}")])
        })
        .collect();
    setup.t.fake.replace_rows(&setup.t.spreadsheet(), COMPACTIONS.1, rows);

    setup.prepare();

    assert!(setup.journal().read_rows().unwrap().len() <= 50);
}

// (TestEventLabels isn't ported: one test reached a labelled create only by
// monkeypatching the planner, the other counted label reads, which share
// their request with the calendar's other reads here.)

// -- actions ------------------------------------------------------------------------------

fn action(id: &str, name: &str, status: &str) -> Value {
    json!({"id": id, "name": name, "status": status})
}

fn actions() -> Vec<Value> {
    vec![action("work", "Time Tracker", "active"), action("mail", "Do email", "active"), action("gone", "Old", "deleted")]
}

fn with_actions(notes: &[(&str, &str)], events: Vec<Event>) -> Setup {
    Setup::with(notes, Some(events), "11:30", Stores { actions: actions(), ..Default::default() })
}

fn yesterday(event: &Event, actions: &[&str]) -> Event {
    Event {
        id: Some(format!("y{}", event.id.as_deref().unwrap())),
        start: Some(event.start() - Duration::days(1)),
        end: Some(event.end() - Duration::days(1)),
        action_ids: Some(crate::testing::shares(actions)),
        ..event.clone()
    }
}

#[test]
fn prepare_lists_actions_and_suggests_them_from_earlier_events_with_the_same_title() {
    let mut events = day();
    let mut all = vec![
        yesterday(&events[0], &["gone", "mail"]), // Email: a deleted action isn't suggested
        yesterday(&events[1], &["work"]),
    ];
    events[1].action_ids = Some(crate::testing::shares(&["work"])); // Report already has one: nothing suggested
    all.extend(events);
    let setup = with_actions(&[("09:05", "x")], all);

    let context = setup.prepare();

    let by_id = |id: &str| context.events.iter().find(|e| e.id == id).unwrap();
    assert_eq!(by_id("e1").suggested_action_ids, Some(strings(&["mail"])));
    assert_eq!(by_id("e2").suggested_action_ids, None);
    assert_eq!(by_id("e2").action_names, Some(strings(&["Time Tracker"])));
    let names: Vec<&str> = context.actions.as_ref().unwrap().iter().map(|a| a["name"].as_str().unwrap()).collect();
    assert_eq!(names, vec!["Time Tracker", "Do email"]); // not deleted ones
    let text = &context.timeline.as_ref().unwrap().text;
    assert!(text.contains("┌ Email\n          ◇ Do email"), "{text}");
    assert!(text.contains("├ Report\n          ◆ Time Tracker"), "{text}");
}

#[test]
fn prepare_marks_only_the_commute_actions() {
    let mut drive = action("drive", "Drive", "active");
    drive["commute"] = json!(true);
    let setup = Setup::with(&[("09:05", "x")], Some(day()), "11:30", Stores { actions: vec![action("work", "Time Tracker", "active"), drive], ..Default::default() });

    let context = setup.prepare();

    let listed = context.actions.as_ref().unwrap();
    assert_eq!(listed[0].get("commute"), None);
    assert_eq!(listed[1]["commute"], json!(true));
    assert!(DECISION_GUIDE.contains("WHERE: keep the location an event already has"));
}

#[test]
fn prepare_gives_the_people_and_locations_to_settle_facts_from() {
    let setup = Setup::with(
        &[("09:05", "x")],
        None,
        "11:30",
        Stores {
            people: vec![json!({"id": "sam", "name": "Sam", "context": "salsa", "status": "active"})],
            locations: vec![json!({"id": "home", "name": "Home", "hint": "the apartment"})],
            ..Default::default()
        },
    );

    let context = setup.prepare();

    let people: Vec<(Value, Value, Value, Value)> =
        context.people.unwrap().iter().map(|p| (p["id"].clone(), p["name"].clone(), p["context"].clone(), p["facts"].clone())).collect();
    // Without traits, there are no trait facts to show.
    assert_eq!(
        people,
        vec![(json!("self"), json!("Me"), Value::Null, Value::Null), (json!("sam"), json!("Sam"), json!("salsa"), Value::Null)]
    );
    let locations: Vec<(Value, Value, Value)> = context.locations.unwrap().iter().map(|l| (l["id"].clone(), l["name"].clone(), l["hint"].clone())).collect();
    assert_eq!(locations, vec![(json!("home"), json!("Home"), json!("the apartment"))]);
    assert!(DECISION_GUIDE.contains("COMPACTION ESTABLISHES THE FACTS"));
}

#[test]
fn future_events_get_no_suggestions() {
    let events = day();
    let mut all = vec![yesterday(&events[2], &["work"])];
    all.extend(events);
    let setup = with_actions(&[("09:05", "x")], all);

    let context = setup.prepare();

    // Lunch is after now.
    assert_eq!(context.events.iter().find(|e| e.id == "e3").unwrap().suggested_action_ids, None);
}

fn bare_with_actions() -> Setup {
    let setup = with_actions(&[], day());
    setup.append_note("09:00", None);
    setup.append_note("09:30", None);
    setup
}

#[test]
fn a_dry_run_refuses_unknown_or_deleted_actions() {
    let setup = bare_with_actions();
    let mut decisions = setup.coffee_instead_of_email();

    decisions[0].action_ids = Some(crate::testing::shares(&["wrk"]));
    refused(setup.try_dry_run(decisions.clone(), DryRun::default()), "no action with the id or name 'wrk'; did you mean work");
    decisions[0].action_ids = Some(crate::testing::shares(&["gone"]));
    refused(setup.try_dry_run(decisions, DryRun::default()), "Deleted actions can't be given to an event");
}

#[test]
fn created_events_are_written_with_their_actions() {
    let setup = bare_with_actions();
    let mut decisions = setup.coffee_instead_of_email();
    decisions[0].action_ids = Some(crate::testing::shares(&["work"]));
    let id = setup.dry_run(decisions).compaction_id.unwrap();

    setup.commit(&id);

    let created = setup.creates();
    assert_eq!(created.len(), 1);
    let id = created[0]["id"].as_str().unwrap();
    assert_eq!(setup.event(id).action_ids, Some(crate::testing::shares(&["work"])));
}

#[test]
fn an_actions_only_change_is_patched_as_just_that() {
    let setup = with_actions(&[("09:05", "x")], day());
    let id = setup.dry_run(vec![EventDecision { action_ids: Some(crate::testing::shares(&["mail"])), ..keep("e1") }]).compaction_id.unwrap();

    setup.commit(&id);

    assert!(setup.patches().iter().any(|(id, _)| id == "e1"));
    assert_eq!(setup.event("e1").action_ids, Some(crate::testing::shares(&["mail"])));
}
