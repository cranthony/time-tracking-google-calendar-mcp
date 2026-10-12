//! TestDryRun.

use super::*;

#[test]
fn plans_and_journals_without_touching_the_calendar_or_the_notes() {
    let setup = Setup::standard();

    let result = setup.dry_run(setup.email_then_report());

    assert_eq!(result.status, "proposed");
    let proposal = result.proposal_id.clone().unwrap();
    assert_eq!((result.revision, result.compaction_id.clone()), (Some(1), Some(format!("{proposal}r1"))));
    let changed: std::collections::BTreeSet<String> = result.changes.iter().filter_map(|c| c.event_id.clone()).collect();
    assert_eq!(changed, ["e1", "e2"].iter().map(|s| s.to_string()).collect());
    assert_eq!(setup.load(result.compaction_id.as_ref().unwrap()).status, PROPOSED);
    assert!(setup.calendar_writes().is_empty());
    assert_eq!(setup.note_ids(), vec![setup.note_id(2), setup.note_id(3)]);
}

#[test]
fn tells_the_model_only_the_user_confirms_it() {
    let setup = Setup::standard();

    setup.prepare();
    let result = setup.dry_run(setup.email_then_report());

    for text in [DECISION_GUIDE, result.message.as_str()] {
        assert!(text.contains("Nothing is applied until the user confirms it, in the app"), "{text}");
        assert!(text.contains("confirm_proposal isn't yours to call"), "{text}");
    }
}

#[test]
fn returns_the_resulting_timeline_to_show_the_user() {
    let setup = Setup::standard();

    let result = setup.dry_run(setup.email_then_report());

    let timeline = result.timeline.as_ref().unwrap();
    let email = timeline.events.iter().find(|e| e.event_id.as_deref() == Some("e1")).unwrap();
    assert_eq!(email.status, "adjusted");
    assert_eq!((email.start_note.clone(), email.end_note.clone()), (Some(setup.note_id(2)), Some(setup.note_id(3))));
    assert!(result.message.contains("timeline"));
    assert!(timeline.text.contains("└ Email ends · +20m (was 10:00)"), "{}", timeline.text);
}

#[test]
fn with_no_decisions_every_past_event_is_recorded_on_schedule() {
    // Where they were planned, with their notes added -- nothing more: the
    // compaction's own time says they're settled.
    let setup = Setup::standard();

    let result = setup.dry_run(vec![]);

    let changed: Vec<String> = result.changes.iter().filter_map(|c| c.event_id.clone()).collect();
    assert_eq!(changed.iter().collect::<std::collections::BTreeSet<_>>().len(), 2);
    assert!(changed.contains(&"e1".to_string()) && changed.contains(&"e2".to_string()));
    for change in &result.changes {
        let before = change.before.clone().unwrap();
        let after = EventState { description: before.description.clone(), ..change.after.clone().unwrap() };
        assert_eq!(after, before);
    }
}

#[test]
fn journals_the_decisions_and_ignored_notes() {
    let setup = Setup::standard();

    let result = setup
        .try_dry_run(setup.email_then_report(), DryRun { ignore_notes: Some(vec![setup.note_id(3)]), ..Default::default() })
        .unwrap();

    let journal = setup.load(result.compaction_id.as_ref().unwrap());
    assert_eq!(journal.decisions, setup.email_then_report());
    assert_eq!(journal.ignore_notes, vec![setup.note_id(3)]);
}

#[test]
fn invalid_decisions_raise_with_what_to_fix() {
    let setup = Setup::standard();

    refused(setup.try_dry_run(vec![keep("nope")], DryRun::default()), "isn't one of this day's events");
}

#[test]
fn overlapping_past_events_are_rejected_for_the_model_to_resolve() {
    let setup = Setup::standard();

    refused(
        setup.try_dry_run(vec![EventDecision { end_note: Some(setup.note_id(3)), ..keep("e1") }], DryRun::default()),
        "'Email'.*overlaps 'Report'",
    );

    assert!(setup.journal().compactions_with_status(&[PLANNED]).unwrap().is_empty());
}

#[test]
fn nothing_to_compact_when_there_are_no_notes() {
    assert_eq!(Setup::new(&[], None, "11:30").dry_run(vec![]).status, "nothing_to_compact");
}

#[test]
fn refuses_to_start_while_another_compaction_is_open() {
    let setup = Setup::standard();
    let first = setup.dry_run(setup.email_then_report()).compaction_id.unwrap();
    setup.set_status(&first, APPLYING);

    refused(setup.try_dry_run(setup.email_then_report(), DryRun::default()), &format!("compaction {first} is applying"));
}

#[test]
fn with_a_proposal_open_a_new_one_is_refused_but_it_can_be_revised() {
    let setup = Setup::standard();
    let first = setup.dry_run(setup.email_then_report());
    let proposal = first.proposal_id.clone().unwrap();

    refused(setup.try_dry_run(setup.email_then_report(), DryRun::default()), &format!("proposal {proposal} is open"));
    let second = setup.try_dry_run(setup.email_then_report(), DryRun::revising(&proposal, 1)).unwrap();

    assert_eq!((second.status.as_str(), second.proposal_id.clone(), second.revision), ("proposed", Some(proposal), Some(2)));
    assert_eq!(setup.load(first.compaction_id.as_ref().unwrap()).status, SUPERSEDED);
    assert_eq!(setup.load(second.compaction_id.as_ref().unwrap()).status, PROPOSED);
}

#[test]
fn a_plan_from_before_proposals_is_replaced_by_a_new_proposal() {
    let setup = Setup::standard();
    setup.start_plain("old", time_at("11:00"));

    let result = setup.dry_run(setup.email_then_report());

    assert_eq!(setup.load("old").status, ABANDONED);
    assert!(result.message.contains("Replaced 1 unapplied plan"), "{}", result.message);
}

#[test]
fn a_superseded_revision_can_no_longer_be_committed() {
    let setup = Setup::standard();
    let first = setup.dry_run(setup.email_then_report());
    setup.try_dry_run(setup.email_then_report(), DryRun::revising(first.proposal_id.as_ref().unwrap(), 1)).unwrap();

    refused(setup.try_commit(first.compaction_id.as_ref().unwrap()), "superseded");

    assert!(setup.calendar_writes().is_empty());
}

#[test]
fn a_rejected_plan_can_be_redone_with_corrected_decisions() {
    let setup = Setup::standard();
    let first = setup.dry_run(setup.email_then_report());

    // The user says the report never happened -- redo it without touching
    // the calendar or calling prepare again.
    let revised = setup
        .try_dry_run(
            vec![
                EventDecision { start_note: Some(setup.note_id(2)), end_note: Some(setup.note_id(3)), ..keep("e1") },
                EventDecision { event_id: Some("e2".into()), ..EventDecision::new("cancel") },
            ],
            DryRun::revising(first.proposal_id.as_ref().unwrap(), 1),
        )
        .unwrap();

    assert_eq!(change(&revised, "e1").after.as_ref().unwrap().end, Some(time_at("10:20")));
    assert_eq!(change(&revised, "e2").action, "cancel");
    assert!(setup.calendar_writes().is_empty());
}

#[test]
fn a_plan_that_fails_validation_leaves_the_earlier_plan_alone() {
    let setup = Setup::standard();
    let first = setup.dry_run(setup.email_then_report());

    assert!(setup.try_dry_run(vec![keep("nope")], DryRun::revising(first.proposal_id.as_ref().unwrap(), 1)).is_err());

    assert_eq!(setup.load(first.compaction_id.as_ref().unwrap()).status, PROPOSED);
}

#[test]
fn a_rejected_plan_can_be_redone_with_a_rename_and_a_note() {
    let setup = Setup::standard();
    let first = setup.dry_run(setup.email_then_report());

    let mut decisions = setup.email_then_report();
    decisions[0].summary = Some("Deep work".into());
    decisions[0].annotate = Some("phone rang".into());
    let revised = setup.try_dry_run(decisions, DryRun::revising(first.proposal_id.as_ref().unwrap(), 1)).unwrap();

    let email = change(&revised, "e1").after.clone().unwrap();
    assert_eq!(email.summary.as_deref(), Some("Deep work"));
    assert_eq!(email.description.as_deref(), Some("Notes:\n- phone rang"));
    assert!(setup.calendar_writes().is_empty());
}
