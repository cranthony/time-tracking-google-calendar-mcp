//! TestSleepingInPastTheSpan, TestSleepingIn, TestEditAndDeleteNotes,
//! TestMovingFutureEvents, TestGarbageCollection and TestActions.

use super::*;
use crate::sheet::COMPACTIONS;

fn cancel(event_id: &str) -> EventDecision {
    EventDecision { event_id: Some(event_id.into()), ..EventDecision::new("cancel") }
}

// -- sleeping in past the span ------------------------------------------------------
// A compaction made in the night -- its span ends with that night -- that
// says the night ran on past the morning's events: they're its to settle
// too.

fn night_setup() -> Setup {
    let events = vec![
        event_at("21:00-22:00").id("e1").summary("Reading").priority(2).build(),
        event_at("22:00-07:00+1").id("s1").summary("Sleep").priority(0).sleep().build(),
        event_at("07:00+1-08:00+1").id("gr1").summary("Getting Ready").priority(2).build(),
        event_at("09:15+1-12:00+1").id("w1").summary("Work").priority(3).build(),
        event_at("22:00+1-23:30+1").id("s2").summary("Sleep").priority(0).sleep().build(),
    ];
    Setup::new(&[("21:05", "reading")], Some(events), "06:30+1")
}

#[test]
fn the_span_ends_with_the_night() {
    assert_eq!(event_ids(&night_setup().prepare()), strings(&["e1", "s1"]));
}

#[test]
fn a_later_end_takes_in_what_it_runs_into() {
    let setup = night_setup();

    let planned = setup.dry_run(vec![EventDecision { end: Some(time_at("09:00+1")), ..keep("s1") }, cancel("gr1")]);

    assert_eq!(change(&planned, "s1").after.as_ref().unwrap().end, Some(time_at("09:00+1")));
    assert_eq!(change(&planned, "gr1").action, "cancel");
    // Work, at 9:15, it doesn't reach.
    assert!(!planned.changes.iter().any(|c| c.event_id.as_deref() == Some("w1")));
}

#[test]
fn what_it_runs_into_must_be_settled() {
    let setup = night_setup();

    refused(
        setup.try_dry_run(vec![EventDecision { end: Some(time_at("09:00+1")), ..keep("s1") }], DryRun::default()),
        "(?s)'Sleep'.*overlaps 'Getting Ready'",
    );
}

#[test]
fn what_it_doesnt_reach_is_still_outside() {
    let setup = night_setup();

    refused(
        setup.try_dry_run(
            vec![
                EventDecision { end: Some(time_at("09:00+1")), ..keep("s1") },
                cancel("gr1"),
                EventDecision { start: Some(time_at("09:30+1")), ..keep("w1") },
            ],
            DryRun::default(),
        ),
        "w1' isn't one of this day's events",
    );
}

// -- sleeping in --------------------------------------------------------------------
// The event that ended just before the compaction window -- here, last
// night's sleep -- is offered with it, so a late wake-up can extend it.

fn slept_in() -> Setup {
    let events = vec![
        event_at("09:00-10:00").id("e1").summary("Email").priority(2).build(),
        event_at("20:00-07:00+1").id("s1").summary("Sleep").priority(0).sleep().build(),
        event_at("07:00+1-08:00+1").id("gr1").summary("Getting Ready").priority(2).build(),
        event_at("08:00+1-12:00+1").id("w1").summary("Work").priority(3).build(),
        event_at("22:00+1-23:30+1").id("s2").summary("Sleep").priority(0).sleep().build(),
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

    assert_eq!(event_ids(&context), strings(&["s1", "gr1", "w1", "s2"]));
    assert!(context.notes[0].candidates.contains(&"s1".to_string()));
    assert_eq!(context.day_end, Some(time_at("23:30+1")));
}

#[test]
fn extending_it_over_the_morning_needs_the_morning_moved_too() {
    let setup = slept_in();

    refused(
        setup.try_dry_run(vec![EventDecision { end_note: Some(setup.note_id(3)), ..keep("s1") }], DryRun::default()),
        r"(?s)'Sleep' \(20:00–08:30, as asked\) overlaps 'Getting Ready'",
    );
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
fn a_keep_with_new_times_reschedules_a_future_event() {
    let setup = Setup::standard();
    let mut decisions = setup.email_then_report();
    decisions.push(EventDecision { start: Some(time_at("12:15")), end: Some(time_at("12:45")), ..keep("e3") });

    let planned = setup.dry_run(decisions);
    setup.commit(planned.compaction_id.as_ref().unwrap());

    let changed: std::collections::BTreeSet<String> = planned.changes.iter().filter_map(|c| c.event_id.clone()).collect();
    assert_eq!(changed, ["e1", "e2", "e3"].iter().map(|s| s.to_string()).collect());
    let lunch = setup.patch("e3");
    assert_eq!((moment_in(&lunch, "start"), moment_in(&lunch, "end")), (Some(time_at("12:15")), Some(time_at("12:45"))));
}

#[test]
fn moving_bedtime_near_the_end_of_the_day_plans_and_commits() {
    let setup = Setup::new(&[("09:05", "email"), ("10:20", "report")], None, "19:45");
    let mut decisions = setup.email_then_report();
    decisions.push(EventDecision { start: Some(time_at("22:30")), end: Some(time_at("08:00") + Duration::days(1)), ..keep("s1") });

    let planned = setup.dry_run(decisions);
    setup.commit(planned.compaction_id.as_ref().unwrap());

    assert_eq!(planned.status, "proposed");
    assert!(planned.warnings.iter().any(|w| w.contains("doesn't adjust the next day")), "{:?}", planned.warnings);
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
        action_ids: Some(strings(actions)),
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
    events[1].action_ids = Some(strings(&["work"])); // Report already has one: nothing suggested
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
            people: vec![json!({"id": "sam", "name": "Sam", "context": "salsa", "status": "active", "what_matters": "tea"})],
            locations: vec![json!({"id": "home", "name": "Home", "hint": "the apartment"})],
            ..Default::default()
        },
    );

    let context = setup.prepare();

    let people: Vec<(Value, Value, Value, Value)> =
        context.people.unwrap().iter().map(|p| (p["id"].clone(), p["name"].clone(), p["context"].clone(), p["what_matters"].clone())).collect();
    assert_eq!(
        people,
        vec![(json!("self"), json!("Me"), Value::Null, Value::Null), (json!("sam"), json!("Sam"), json!("salsa"), json!("tea"))]
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

    decisions[0].action_ids = Some(strings(&["wrk"]));
    refused(setup.try_dry_run(decisions.clone(), DryRun::default()), "no action with the id or name 'wrk'; did you mean work");
    decisions[0].action_ids = Some(strings(&["gone"]));
    refused(setup.try_dry_run(decisions, DryRun::default()), "Deleted actions can't be given to an event");
}

#[test]
fn created_events_are_written_with_their_actions() {
    let setup = bare_with_actions();
    let mut decisions = setup.coffee_instead_of_email();
    decisions[0].action_ids = Some(strings(&["work"]));
    let id = setup.dry_run(decisions).compaction_id.unwrap();

    setup.commit(&id);

    let created = setup.creates();
    assert_eq!(created.len(), 1);
    let id = created[0]["id"].as_str().unwrap();
    assert_eq!(setup.event(id).action_ids, Some(strings(&["work"])));
}

#[test]
fn an_actions_only_change_is_patched_as_just_that() {
    let setup = with_actions(&[("09:05", "x")], day());
    let id = setup.dry_run(vec![EventDecision { action_ids: Some(strings(&["mail"])), ..keep("e1") }]).compaction_id.unwrap();

    setup.commit(&id);

    assert!(setup.patches().iter().any(|(id, _)| id == "e1"));
    assert_eq!(setup.event("e1").action_ids, Some(strings(&["mail"])));
}
