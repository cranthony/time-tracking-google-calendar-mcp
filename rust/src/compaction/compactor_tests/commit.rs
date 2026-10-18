//! TestCommit, TestResume, TestGetProposalAndAbandon and TestDayByDay.

use super::*;

fn bare_notes() -> Setup {
    let setup = Setup::new(&[], None, "11:30");
    setup.append_note("09:00", None);
    setup.append_note("09:30", None);
    setup
}

fn planned(setup: &Setup, decisions: Vec<EventDecision>) -> String {
    setup.dry_run(decisions).compaction_id.unwrap()
}

#[test]
fn applies_the_plan_journals_each_step_and_stamps_the_notes() {
    let setup = Setup::standard();
    let id = planned(&setup, setup.email_then_report());

    let result = setup.commit(&id);

    assert_eq!(result.status, "applied");
    let email = setup.patch("e1");
    assert_eq!((moment_in(&email, "start"), moment_in(&email, "end")), (Some(time_at("09:05")), Some(time_at("10:20"))));
    // Only what changed: the report still ended as planned.
    let report = setup.patch("e2");
    assert_eq!((moment_in(&report, "start"), moment_in(&report, "end")), (Some(time_at("10:20")), None));
    let journal = setup.load(&id);
    assert_eq!(journal.status, STAMPED);
    assert!(journal.steps.iter().all(|s| s.status == "done"));
    assert!(setup.notes(false).is_empty());
    let stamps: std::collections::BTreeSet<Option<String>> = setup.notes(true).into_iter().map(|n| n.note.compaction_id).collect();
    assert_eq!(stamps, [Some(id)].into_iter().collect());
}

#[test]
fn the_patch_carries_only_what_changed() {
    let setup = Setup::standard();
    let id = planned(&setup, setup.email_then_report());

    setup.commit(&id);

    let email = setup.patch("e1");
    assert!(email.get("summary").is_none() && email.get("description").is_none(), "{email}");
}

#[test]
fn cancelled_events_are_patched_to_cancelled() {
    let setup = bare_notes();
    let id = planned(&setup, vec![EventDecision { event_id: Some("e2".into()), ..EventDecision::new("cancel") }]);

    setup.commit(&id);

    assert_eq!(setup.patch("e2")["status"], "cancelled");
}

#[test]
fn created_events_get_deterministic_ids() {
    let setup = bare_notes();
    let id = planned(&setup, setup.coffee_instead_of_email());

    setup.commit(&id);

    let created = setup.creates();
    assert_eq!(created.len(), 1);
    let step = setup.load(&id).steps.into_iter().find(|s| s.action == "create").unwrap();
    assert_eq!(created[0]["id"], json!(format!("cmp{id}s{:03}", step.step)));
    assert_eq!(created[0]["summary"], "Coffee");
}

#[test]
fn a_retried_create_that_already_exists_counts_as_done() {
    let setup = bare_notes();
    let id = planned(&setup, setup.coffee_instead_of_email());
    setup.fail_next("POST", "/events", 409);

    let result = setup.commit(&id);

    assert_eq!(result.status, "applied");
    assert_eq!(setup.load(&id).status, STAMPED);
}

#[test]
fn other_create_errors_are_not_swallowed() {
    let setup = bare_notes();
    let id = planned(&setup, setup.coffee_instead_of_email());
    setup.fail_next("POST", "/events", 500);

    let error = setup.try_commit(&id).err().unwrap();

    assert_eq!(crate::google::status_of(&error), Some(500));
}

#[test]
fn committing_twice_is_harmless() {
    let setup = Setup::standard();
    let id = planned(&setup, setup.email_then_report());
    setup.commit(&id);
    let patches = setup.patches().len();

    let again = setup.commit(&id);

    assert_eq!(again.status, "already_compacted");
    assert_eq!(setup.patches().len(), patches);
}

#[test]
fn refuses_when_a_note_was_added_after_the_preview() {
    let setup = Setup::standard();
    let id = planned(&setup, setup.email_then_report());
    setup.append_note("11:00", Some("one more"));

    refused(setup.try_commit(&id), "changed since.*run a new dry run");

    assert!(setup.patches().is_empty());
}

#[test]
fn refuses_when_the_calendar_changed_after_the_preview() {
    let setup = Setup::standard();
    let id = planned(&setup, setup.email_then_report());
    setup.change_event(Event { id: Some("e1".into()), summary: Some("Someone renamed this".into()), ..Default::default() });

    refused(setup.try_commit(&id), "changed since");

    assert!(setup.patches().is_empty());
}

#[test]
fn refuses_an_abandoned_compaction() {
    let setup = Setup::standard();
    let id = planned(&setup, setup.email_then_report());
    setup.call(|c| c.abandon(&id)).unwrap();

    refused(setup.try_commit(&id), "abandoned");
}

#[test]
fn an_unknown_compaction_id_is_reported() {
    refused(Setup::standard().try_commit("nope"), "no compaction");
}

// -- resuming -----------------------------------------------------------------------

/// A plan whose second write (the report's patch) fails.
fn failed_partway() -> (Setup, String) {
    let setup = Setup::standard();
    let id = planned(&setup, setup.email_then_report());
    setup.fail_next("PATCH", "/events/e2", 500);
    assert!(setup.try_commit(&id).is_err());
    (setup, id)
}

#[test]
fn a_failure_partway_leaves_the_journal_at_exactly_that_point() {
    let (setup, id) = failed_partway();

    let journal = setup.load(&id);
    assert_eq!(journal.status, APPLYING);
    let statuses: Vec<&str> = journal.steps.iter().map(|s| s.status.as_str()).collect();
    assert_eq!(statuses, vec!["done", "pending"]);
    // Its notes are still uncompacted, so nothing is lost.
    assert_eq!(setup.note_ids(), vec![setup.note_id(2), setup.note_id(3)]);
}

#[test]
fn committing_again_finishes_only_what_was_left() {
    let (setup, id) = failed_partway();
    setup.t.fake.clear_requests();

    let result = setup.commit(&id);

    assert_eq!(result.status, "applied");
    let patched: Vec<String> = setup.patches().into_iter().map(|(id, _)| id).collect();
    assert_eq!(patched, strings(&["e2"]));
    assert_eq!(setup.load(&id).status, STAMPED);
    assert!(setup.notes(false).is_empty());
}

#[test]
fn resuming_does_not_recheck_the_calendar_it_is_already_changing() {
    // The first step already moved the email on the calendar.
    let (setup, id) = failed_partway();
    assert_eq!(setup.event("e1").start, Some(time_at("09:05")));

    assert_eq!(setup.commit(&id).status, "applied");
}

// -- getting a proposal, and abandoning one -------------------------------------------

#[test]
fn getting_the_proposal_returns_its_current_revision_without_applying_it() {
    let setup = Setup::standard();
    let planned = setup.dry_run(setup.email_then_report());

    let proposal = setup.call(|c| c.get_proposal(None, None)).unwrap();

    assert_eq!((Some(proposal.id.clone()), proposal.revision, proposal.state.as_str()), (planned.proposal_id.clone(), 1, "awaiting_review"));
    assert_eq!(proposal.changes, planned.changes);
    let events = proposal.events.unwrap();
    let email = events.iter().find(|e| e.id == "e1").unwrap();
    assert_eq!((email.status.as_str(), email.decided_by.as_deref()), ("adjusted", Some("claude")));
    assert_eq!((email.start, email.planned_start), (Some(time_at("09:05")), Some(time_at("09:00"))));
    assert_eq!(events.iter().find(|e| e.id == "e3").unwrap().status, "planned");
    assert!(setup.calendar_writes().is_empty());
}

#[test]
fn abandoning_leaves_the_notes_uncompacted_and_unblocks_a_new_compaction() {
    let setup = Setup::standard();
    let first = planned(&setup, setup.email_then_report());
    setup.set_status(&first, APPLYING);

    let result = setup.call(|c| c.abandon(&first)).unwrap();

    assert_eq!(result.status, "abandoned");
    assert_eq!(setup.note_ids(), vec![setup.note_id(2), setup.note_id(3)]);
    assert_eq!(setup.dry_run(setup.email_then_report()).status, "proposed");
}

#[test]
fn a_finished_compaction_cannot_be_abandoned() {
    let setup = Setup::standard();
    let id = planned(&setup, setup.email_then_report());
    setup.commit(&id);

    refused(setup.call(|c| c.abandon(&id)), "already complete");
}

// -- day by day -----------------------------------------------------------------------

#[test]
fn a_later_days_notes_are_compacted_with_the_earlier_days() {
    let setup = Setup::standard();
    setup.append_note("08:00+1", Some("next day"));
    setup.set_now("09:00+1");
    let id = planned(&setup, setup.email_then_report());
    setup.commit(&id);

    let context = setup.prepare();

    assert!(context.notes.is_empty());
    // One compaction for both days.
    let stamps: std::collections::BTreeSet<Option<String>> = setup.notes(true).into_iter().map(|n| n.note.compaction_id).collect();
    assert_eq!(stamps, [Some(id)].into_iter().collect());
}

#[test]
fn the_next_days_compaction_window_starts_where_the_last_one_left_off() {
    let events = vec![
        event_at("09:00-10:00").id("e1").summary("Email").priority(2).build(),
        event_at("20:00-07:00+1").id("s1").summary("Sleep").priority(0).sleep().build(),
        event_at("07:00+1-07:30+1").id("gr1").summary("Getting Ready").priority(2).build(),
    ];
    let setup = Setup::new(&[("09:05", "email")], Some(events), "07:10+1");
    let id = planned(&setup, vec![]);
    setup.commit(&id);
    assert_eq!(setup.journal().last_stamped_now().unwrap(), Some(time_at("07:10+1")));
    setup.append_note("08:15+1", Some("left for work"));
    setup.set_now("08:30+1");

    let context = setup.prepare();

    assert_eq!(context.compaction_window_start, Some(time_at("07:10+1")));
    assert_eq!(event_ids(&context)[..2], strings(&["s1", "gr1"]));
}

#[test]
fn the_requests_a_compaction_of_many_events_and_notes_makes() {
    // Six pieces of work, each moved to the notes that start and end it,
    // and every note annotated with its event. [Sheets reads, Sheets
    // writes, Calendar reads, Calendar writes] for each step, and the
    // HTTP requests to Calendar: committing makes the changes in one batch
    // and marks them done in the journal together, so neither grows with
    // the number of events changed. The tabs were read as the setup made
    // them, and are kept, so no step reads Sheets; the calendar is kept
    // too, so only the first step reads it (a sync).
    let at = |minutes: usize| format!("{:02}:{:02}", 8 + minutes / 60, minutes % 60);
    let mut events: Vec<Event> = (0..6).map(|i| event_at(&format!("{}-{}", at(30 * i), at(30 * i + 30))).id(&format!("w{i}")).summary("Work").priority(2).build()).collect();
    events.push(event_at("20:00-07:00+1").id("s1").summary("Sleep").priority(0).sleep().build());
    let times: Vec<String> = (0..7).map(|i| at(30 * i + 5)).collect();
    let notes: Vec<(&str, &str)> = times.iter().map(|time| (time.as_str(), "working")).collect();
    let setup = Setup::new(&notes, Some(events), &at(200));
    let mut made = Vec::new();
    let mut step = |label: &'static str| {
        made.push((label, setup.t.fake.quota_counts(), setup.t.fake.calendar_round_trips()));
        setup.t.fake.clear_requests();
    };

    setup.prepare();
    step("prepare");
    let ids: Vec<String> = (0..7).map(|i| setup.note_id(i + 2)).collect();
    setup.t.fake.clear_requests();
    let decisions = (0..6)
        .map(|i| EventDecision { event_id: Some(format!("w{i}")), start_note: Some(ids[i].clone()), end_note: Some(ids[i + 1].clone()), ..EventDecision::new("keep") })
        .collect();
    let annotate_notes = ids.iter().enumerate().map(|(i, id)| (id.clone(), format!("w{}", i.min(5)))).collect();
    let id = setup.try_dry_run(decisions, DryRun { annotate_notes, ..Default::default() }).unwrap().compaction_id.unwrap();
    step("dry_run");
    assert_eq!(setup.commit(&id).status, "applied");
    step("commit");

    assert_eq!(made, vec![("prepare", [0, 0, 1, 0], 1), ("dry_run", [0, 1, 0, 0], 0), ("commit", [0, 4, 0, 6], 1)]);
}

#[test]
fn a_create_refused_a_priority_label_removed_since_is_made_once_its_back() {
    let setup = bare_notes();
    let id = planned(&setup, setup.coffee_instead_of_email());
    // The labels were made sure of, then removed behind the app's back.
    {
        let _held = WRITE_LOCK.hold();
        setup.t.app.actions().unwrap().ensure_priority_labels().unwrap();
        setup.t.app.calendar_client().unwrap().replace_event_labels(&[], None).unwrap();
    }

    let result = setup.commit(&id);

    assert_eq!(result.status, "applied");
    let creates: Vec<Value> = setup.t.fake.requests().into_iter().filter(|r| r.method == "POST" && r.path.ends_with("/events")).map(|r| r.body).collect();
    assert_eq!(creates.len(), 2);
    assert_eq!(creates[1]["summary"], "Coffee");
}

#[test]
fn a_change_calendar_asks_to_slow_down_is_made_again_on_its_own() {
    let setup = Setup::standard();
    let id = planned(&setup, setup.email_then_report());
    setup.fail_next("PATCH", "/events/e2", 429);

    let result = setup.commit(&id);

    assert_eq!(result.status, "applied");
    let patched: Vec<String> = setup.patches().into_iter().map(|(id, _)| id).collect();
    assert_eq!(patched, strings(&["e1", "e2", "e2"]));
    assert_eq!(setup.event("e2").start, Some(time_at("10:20")));
}
