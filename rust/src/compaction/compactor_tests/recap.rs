//! TestEditingAProposalsEventFields and TestRecap.

use super::*;
use crate::compaction::proposals::{ProposalNote, RecapPoint};

fn proposed(decisions: Vec<EventDecision>) -> (Setup, String) {
    let setup = Setup::standard();
    let p = setup.dry_run(decisions).proposal_id.unwrap();
    (setup, p)
}

fn event<'a>(proposal: &'a Proposal, event_id: &str) -> &'a ProposalEvent {
    proposal.events.as_ref().unwrap().iter().find(|e| e.id == event_id).unwrap_or_else(|| panic!("no {event_id} in the proposal"))
}

fn note<'a>(proposal: &'a Proposal, note_id: &str) -> &'a ProposalNote {
    proposal.notes.iter().find(|n| n.id == note_id).unwrap_or_else(|| panic!("no note {note_id} in the proposal"))
}

fn described(event_id: &str, description: &str) -> EventDecision {
    EventDecision { description: Some(description.into()), ..keep(event_id) }
}

fn annotate_email(setup: &Setup, use_: &str) -> Vec<NoteEdit> {
    vec![NoteEdit::from_value(&json!({"note_id": setup.note_id(2), "use": use_}))]
}

// -- editing a proposal's event fields ---------------------------------------------------
// The user sets an event's whole description, its location or its priority
// in a proposal, as update_event would on the calendar.

#[test]
fn a_description_the_user_writes_is_the_last_word() {
    let (setup, p) = proposed(vec![]);
    assert_eq!(event(&setup.get_proposal(), "e1").description.as_deref(), Some("Notes:\n- 09:05 email"));

    let proposal = setup.amend(&p, 1, vec![described("e1", "Inbox zero")]);

    assert_eq!(event(&proposal, "e1").description.as_deref(), Some("Inbox zero"));
    let email = note(&proposal, &setup.note_id(2));
    assert_eq!((email.use_.as_str(), email.decided_by.as_deref()), ("ignored", None));
}

#[test]
fn a_note_the_users_description_keeps_still_counts_as_added() {
    let (setup, p) = proposed(vec![]);

    let proposal = setup.amend(&p, 1, vec![described("e1", "Inbox zero\n\nNotes:\n- 09:05 email")]);

    assert_eq!(event(&proposal, "e1").description.as_deref(), Some("Inbox zero\n\nNotes:\n- 09:05 email"));
    assert_eq!(note(&proposal, &setup.note_id(2)).use_, "annotates");
}

#[test]
fn a_note_counts_as_kept_only_by_its_exact_line() {
    let (setup, p) = proposed(vec![]);

    let proposal = setup.amend(&p, 1, vec![described("e1", "Cleared my email backlog")]);

    assert_eq!(note(&proposal, &setup.note_id(2)).use_, "ignored");
    let dropped: Vec<Value> = proposal.user_edits.iter().map(|e| e.edit["dropped_notes"].clone()).collect();
    assert_eq!(dropped, vec![json!([setup.note_id(2)])]);
}

#[test]
fn a_note_annotated_after_the_description_is_added_below_it() {
    let (setup, p) = proposed(vec![]);
    setup.amend(&p, 1, vec![described("e1", "Inbox zero")]);

    let proposal = setup.try_amend(&p, 2, Amend { notes: annotate_email(&setup, "annotate"), ..Default::default() }).unwrap();

    assert_eq!(event(&proposal, "e1").description.as_deref(), Some("Inbox zero\n\nNotes:\n- 09:05 email"));
    assert_eq!(note(&proposal, &setup.note_id(2)).use_, "annotates");
}

#[test]
fn a_note_written_later_is_added_below_it_and_the_left_out_one_stays_out() {
    let (setup, p) = proposed(vec![]);
    setup.amend(&p, 1, vec![described("e1", "Inbox zero")]);
    setup.append_note("09:30", Some("phone rang"));

    let proposal = setup.get_proposal();

    assert_eq!(event(&proposal, "e1").description.as_deref(), Some("Inbox zero\n\nNotes:\n- 09:30 phone rang"));
    assert_eq!(note(&proposal, &setup.note_id(2)).use_, "ignored");
    // Confirming plans it again with the new note, to confirm that.
    let result = setup.call(|c| c.confirm(&p, 2)).unwrap();
    assert_eq!(result.status, "rechecked");
    assert_eq!(event(result.proposal.as_ref().unwrap(), "e1").description.as_deref(), Some("Inbox zero\n\nNotes:\n- 09:30 phone rang"));
}

#[test]
fn an_explicit_note_edit_in_the_same_call_wins() {
    let (setup, p) = proposed(vec![]);

    let proposal = setup
        .try_amend(
            &p,
            1,
            Amend { decisions: vec![described("e1", "Inbox zero\n\nNotes:\n- 09:05 email")], notes: annotate_email(&setup, "ignore"), ..Default::default() },
        )
        .unwrap();

    assert_eq!(note(&proposal, &setup.note_id(2)).use_, "ignored");
}

#[test]
fn what_it_left_out_stays_out_through_claudes_revisions() {
    let (setup, p) = proposed(vec![]);
    setup.amend(&p, 1, vec![described("e1", "Inbox zero")]);

    setup.try_dry_run(vec![], DryRun::revising(&p, 2)).unwrap();

    let proposal = setup.get_proposal();
    assert_eq!(event(&proposal, "e1").description.as_deref(), Some("Inbox zero"));
    assert_eq!(note(&proposal, &setup.note_id(2)).use_, "ignored");
}

#[test]
fn it_replaces_claudes_annotate_text_too() {
    let (setup, p) = proposed(vec![EventDecision { annotate: Some("mostly replies".into()), ..keep("e1") }]);

    let proposal = setup.amend(&p, 1, vec![described("e1", "Inbox zero")]);

    assert_eq!(event(&proposal, "e1").description.as_deref(), Some("Inbox zero"));
}

#[test]
fn location_and_priority_are_set_and_written_on_confirming() {
    let (setup, p) = proposed(vec![]);

    let proposal = setup.amend(&p, 1, vec![EventDecision { location: Some("Office".into()), priority: Some(3), ..keep("e2") }]);

    let report = event(&proposal, "e2");
    assert_eq!((report.location.as_deref(), report.priority), (Some("Office"), Some(3)));
    assert_eq!(setup.call(|c| c.confirm(&p, 2)).unwrap().status, "applied");
    let written = setup.event("e2");
    assert_eq!((written.location.as_deref(), written.priority), (Some("Office"), Some(3)));
    assert_eq!(setup.patch("e2")["location"], "Office");
}

#[test]
fn a_description_too_long_for_calendar_is_refused() {
    let (setup, p) = proposed(vec![]);

    refused(setup.try_amend(&p, 1, Amend { decisions: vec![described("e1", &"x".repeat(9000))], ..Default::default() }), "over the 8192 Calendar keeps");
}

#[test]
fn a_cleared_description_is_written_empty() {
    let (setup, p) = proposed(vec![]);
    setup.amend(&p, 1, vec![described("e1", "")]);

    setup.call(|c| c.confirm(&p, 2)).unwrap();

    assert_eq!(setup.patch("e1")["description"], "");
}

// -- recaps -------------------------------------------------------------------------------
// Claude's recap of a revision: what it did and what it's unsure of, each
// point citing the events and notes it's about.

fn recap(did_events: &[&str], unsure_notes: &[String], unsure_events: &[&str]) -> Recap {
    Recap {
        overview: "A **quiet** morning".into(),
        did: vec![RecapPoint { text: "Moved the *email*:\n- to 09:05".into(), event_ids: strings(did_events), note_ids: vec![] }],
        unsure: vec![RecapPoint { text: "Was it a report?".into(), event_ids: strings(unsure_events), note_ids: unsure_notes.to_vec() }],
    }
}

fn with_recap(recap: Recap) -> DryRun {
    DryRun { recap: Some(recap), ..Default::default() }
}

#[test]
fn a_recap_is_kept_with_the_revision_and_handed_back() {
    let setup = Setup::standard();
    let given = recap(&["e1"], &[setup.note_id(3)], &["e2"]);

    setup.try_dry_run(setup.email_then_report(), with_recap(given.clone())).unwrap();

    let proposal = setup.get_proposal();
    assert_eq!((proposal.recap.clone(), proposal.recap_revision), (Some(given.clone()), Some(1)));
    assert_eq!(setup.prepare().proposal.unwrap().recap, Some(given));
}

#[test]
fn the_users_edits_keep_the_recap_and_say_which_revision_its_of() {
    let setup = Setup::standard();
    let result = setup.try_dry_run(setup.email_then_report(), with_recap(recap(&["e1"], &[], &[]))).unwrap();

    let proposal = setup.amend(result.proposal_id.as_ref().unwrap(), 1, vec![EventDecision { summary: Some("Inbox".into()), ..keep("e1") }]);

    assert_eq!((proposal.revision, proposal.recap, proposal.recap_revision), (2, Some(recap(&["e1"], &[], &[])), Some(1)));
}

#[test]
fn claudes_next_revision_replaces_the_recap() {
    let setup = Setup::standard();
    let p = setup.try_dry_run(setup.email_then_report(), with_recap(recap(&["e1"], &[], &[]))).unwrap().proposal_id.unwrap();
    setup.amend(&p, 1, vec![EventDecision { summary: Some("Inbox".into()), ..keep("e1") }]);
    let later = recap(&["e2"], &[], &[]);

    setup.try_dry_run(setup.email_then_report(), DryRun { recap: Some(later.clone()), ..DryRun::revising(&p, 2) }).unwrap();

    let proposal = setup.get_proposal();
    assert_eq!((proposal.recap, proposal.recap_revision), (Some(later), Some(3)));
}

#[test]
fn a_recap_names_a_new_event_by_its_ref_till_it_has_a_key() {
    let setup = Setup::standard();
    let mut decisions = setup.email_then_report();
    decisions.push(EventDecision { summary: Some("Walk".into()), start: Some(time_at("11:00")), end: Some(time_at("11:20")), ..EventDecision::new("create") });

    let result = setup
        .try_dry_run(
            decisions,
            DryRun {
                recap: Some(recap(&["walk"], &[], &[])),
                annotate_notes: vec![(setup.note_id(2), "walk".into())],
                create_refs: vec![Some("walk".into())],
                ..Default::default()
            },
        )
        .unwrap();

    let p = result.proposal_id.unwrap();
    let proposal = setup.get_proposal();
    assert_eq!(proposal.recap.as_ref().unwrap().did[0].event_ids, vec![format!("{p}c1")]);
    assert_eq!(note(&proposal, &setup.note_id(2)).event_id, Some(format!("{p}c1")));
}

#[test]
fn a_ref_given_twice_is_refused() {
    let setup = Setup::standard();
    let mut decisions = setup.email_then_report();
    decisions.push(EventDecision { summary: Some("Walk".into()), start: Some(time_at("11:00")), end: Some(time_at("11:20")), ..EventDecision::new("create") });
    decisions.push(EventDecision { summary: Some("Stroll".into()), start: Some(time_at("11:20")), end: Some(time_at("11:25")), ..EventDecision::new("create") });

    refused(
        setup.try_dry_run(decisions, DryRun { create_refs: vec![Some("walk".into()), Some("walk".into())], ..Default::default() }),
        "the ref 'walk' is another create's too",
    );
}

#[test]
fn a_recap_citing_what_isnt_in_the_proposal_is_refused_and_writes_nothing() {
    let setup = Setup::standard();

    let message = refused(setup.try_dry_run(setup.email_then_report(), with_recap(recap(&["nope"], &["n-nope".into()], &[]))), "recap");

    assert!(message.contains("recap.did[0]: 'nope' isn't one of the proposal's events"), "{message}");
    assert!(message.contains("recap.unsure[0]: 'n-nope' isn't one of the proposal's notes"), "{message}");
    assert_eq!(setup.journal().open_proposal().unwrap(), None);
}

#[test]
fn a_recap_that_doesnt_say_what_claude_did_is_refused() {
    let setup = Setup::standard();

    refused(
        setup.try_dry_run(setup.email_then_report(), with_recap(Recap { overview: "Fine".into(), did: vec![], unsure: vec![] })),
        "`did` is empty",
    );
}

#[test]
fn a_recap_round_trips_through_the_journal() {
    let given = recap(&["e1"], &["n1".into()], &[]);

    assert_eq!(Recap::from_json_dict(&given.to_json_dict()), given);
}
