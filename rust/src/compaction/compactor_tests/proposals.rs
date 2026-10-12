//! TestProposals: Claude proposes, the user edits it, leaves feedback or
//! confirms it -- see docs/compaction-proposals.md.

use super::*;
use crate::stores::journal::FAILED;

fn proposed() -> (Setup, String) {
    let setup = Setup::standard();
    let p = setup.dry_run(setup.email_then_report()).proposal_id.unwrap();
    (setup, p)
}

fn event<'a>(proposal: &'a Proposal, event_id: &str) -> &'a ProposalEvent {
    proposal.events.as_ref().unwrap().iter().find(|e| e.id == event_id).unwrap_or_else(|| panic!("no {event_id} in the proposal"))
}

fn renamed(setup: &Setup, index: usize, summary: &str) -> Vec<EventDecision> {
    let mut decisions = setup.email_then_report();
    decisions[index].summary = Some(summary.into());
    decisions
}

fn rename(event_id: &str, summary: &str) -> EventDecision {
    EventDecision { summary: Some(summary.into()), ..keep(event_id) }
}

fn walk() -> EventDecision {
    EventDecision { summary: Some("Walk".into()), start: Some(time_at("11:00")), end: Some(time_at("11:20")), ..EventDecision::new("create") }
}

fn add_note(setup: &Setup, p: &str, text: &str, event_id: Option<&str>) -> Feedback {
    setup.call(|c| c.add_note(p, text, event_id.map(str::to_string), None, None)).unwrap()
}

fn reply(feedback: &Feedback, text: &str) -> Vec<FeedbackReply> {
    vec![FeedbackReply { feedback_id: feedback.id.clone(), reply: text.into() }]
}

fn confirm(setup: &Setup, p: &str, revision: i64) -> anyhow::Result<ProposalResult> {
    setup.call(|c| c.confirm(p, revision))
}

fn summary(setup: &Setup) -> ProposalSummary {
    setup.call(|c| c.proposal_summary()).unwrap().unwrap()
}

fn stamped_before(at: &str) -> Setup {
    let setup = Setup::new(&[], None, "11:30");
    setup.start_plain("prev", time_at(at));
    setup.set_status("prev", STAMPED);
    setup
}

#[test]
fn a_day_that_went_to_plan_is_proposed_without_notes() {
    let setup = stamped_before("08:00");

    assert_eq!(setup.dry_run(vec![]).status, "proposed");

    let proposal = setup.get_proposal();
    assert_eq!((proposal.window_start, proposal.through), (time_at("08:00"), time_at("11:30")));
    let events: Vec<(String, String)> =
        proposal.events.unwrap().into_iter().filter(|e| e.id.starts_with('e')).map(|e| (e.id, e.status)).collect();
    let expected: Vec<(String, String)> =
        [("e1", "on_schedule"), ("e2", "on_schedule"), ("e3", "planned")].iter().map(|(a, b)| (a.to_string(), b.to_string())).collect();
    assert_eq!(events, expected);
}

#[test]
fn prepare_hands_claude_the_open_proposal() {
    let (setup, p) = proposed();
    setup.amend(&p, 1, vec![rename("e1", "Inbox")]);

    let context = setup.prepare().proposal.unwrap();

    assert_eq!((context.id.as_str(), context.revision, context.state.as_str(), context.user_seq), (p.as_str(), 2, "awaiting_review", 1));
    let updated: Vec<&str> = context.updates.iter().map(|u| u["event_id"].as_str().unwrap()).collect();
    assert_eq!(updated, vec!["e1", "e2"]);
    let edits: Vec<Value> = context.user_edits.iter().map(|e| Value::Object(e.edit.clone())).collect();
    assert_eq!(edits, vec![json!({"action": "keep", "event_id": "e1", "summary": "Inbox"})]);
}

#[test]
fn the_status_summarizes_the_open_proposal() {
    let (setup, p) = proposed();
    add_note(&setup, &p, "one thing", None);

    let summary = summary(&setup);

    assert_eq!((summary.id.as_str(), summary.revision, summary.state.as_str(), summary.open_feedback), (p.as_str(), 1, "awaiting_claude", 1));
    assert_eq!(summary.through, time_at("11:30"));
}

// -- the user's edits -----------------------------------------------------------------

#[test]
fn an_edit_is_laid_over_claudes_decisions_field_by_field() {
    let (setup, p) = proposed();

    let proposal = setup.amend(&p, 1, vec![rename("e1", "Inbox")]);

    assert_eq!((proposal.revision, proposal.by.as_str(), proposal.reason.as_str()), (2, "user", "user edit"));
    let email = event(&proposal, "e1");
    assert_eq!(
        (email.summary.as_deref(), email.start, email.end, email.decided_by.as_deref()),
        (Some("Inbox"), Some(time_at("09:05")), Some(time_at("10:20")), Some("user"))
    );
    let edits: Vec<(String, Option<String>, String, i64)> =
        proposal.user_edits.iter().map(|e| (e.id.clone(), e.event_id.clone(), e.status.clone(), e.base_revision)).collect();
    assert_eq!(edits, vec![(format!("{p}u1"), Some("e1".to_string()), "active".to_string(), 1)]);
    assert_eq!(setup.load(&format!("{p}r1")).status, SUPERSEDED);
    assert!(setup.calendar_writes().is_empty());
}

#[test]
fn an_edit_that_overlaps_is_refused_and_writes_nothing() {
    let (setup, p) = proposed();

    refused(setup.try_amend(&p, 1, Amend { decisions: vec![EventDecision { end: Some(time_at("10:40")), ..keep("e1") }], ..Default::default() }), "overlaps");

    assert_eq!(setup.journal().revisions(&p).unwrap(), vec![(1, format!("{p}r1"), PROPOSED.to_string())]);
    assert!(setup.journal().user_edits(&p).unwrap().is_empty());
}

#[test]
fn an_edit_changing_history_is_refused_unless_the_user_approved_it() {
    // The last compaction ran at 09:30, as the email was going on.
    let setup = stamped_before("09:30");
    setup.dry_run(vec![]);
    let p = setup.get_proposal().id;
    let moved = EventDecision { start: Some(time_at("09:10")), ..keep("e1") };

    let Err(error) = setup.try_amend(&p, 1, Amend { decisions: vec![moved.clone()], ..Default::default() }) else { panic!("not refused") };
    let refusal = compaction_error(&error).unwrap();
    assert!(refusal.message.contains("its start can't move"), "{}", refusal.message);
    assert_eq!(refusal.categories, strings(&["compacted"]));

    let proposal = setup.amend(&p, 1, vec![EventDecision { allow_history: Some(true), ..moved }]);

    assert_eq!(event(&proposal, "e1").start, Some(time_at("09:10")));
    assert_eq!(proposal.user_edits[0].edit["allow_history"], json!(true));
    // Kept with the edit: a revision planned again from it keeps to it.
    assert_eq!(confirm(&setup, &p, proposal.revision).unwrap().status, "applied");
}

#[test]
fn an_edit_of_an_event_not_in_the_proposal_is_refused() {
    let (setup, p) = proposed();

    refused(setup.try_amend(&p, 1, Amend { decisions: vec![rename("nope", "Something")], ..Default::default() }), "'nope'");
}

#[test]
fn as_planned_clears_claudes_decision() {
    let (setup, p) = proposed();

    let proposal = setup.try_amend(&p, 1, Amend { as_planned: strings(&["e1"]), ..Default::default() }).unwrap();

    let email = event(&proposal, "e1");
    assert_eq!(
        (email.start, email.end, email.status.as_str(), email.decided_by.as_deref()),
        (Some(time_at("09:00")), Some(time_at("10:00")), "on_schedule", None)
    );
}

#[test]
fn the_user_adds_an_event_and_edits_it_by_its_key() {
    let (setup, p) = proposed();
    let key = format!("{p}u1");

    let first = setup.amend(&p, 1, vec![walk()]);
    let second = setup.amend(&p, 2, vec![EventDecision { end: Some(time_at("11:25")), ..keep(&key) }]);

    let added = event(&first, &key);
    assert_eq!((added.status.as_str(), added.decided_by.as_deref()), ("new", Some("user")));
    assert_eq!(event(&second, &key).end, Some(time_at("11:25")));
}

#[test]
fn the_user_edits_claudes_new_event_by_its_key_and_it_follows_the_key() {
    let setup = Setup::standard();
    let mut decisions = setup.email_then_report();
    decisions.push(walk());
    let result = setup.dry_run(decisions);
    let p = result.proposal_id.clone().unwrap();
    let key = format!("{p}c1");
    let keys: Vec<Option<String>> = result.changes.iter().filter(|c| c.action == "create").map(|c| c.key.clone()).collect();
    assert_eq!(keys, vec![Some(key.clone())]);

    let proposal = setup.amend(&p, 1, vec![EventDecision { summary: Some("Long walk".into()), end: Some(time_at("11:25")), ..keep(&key) }]);

    let walked = event(&proposal, &key);
    assert_eq!((walked.summary.as_deref(), walked.end), (Some("Long walk"), Some(time_at("11:25"))));
    // Claude keeps it by sending its key back; the user's edit follows.
    assert_eq!(setup.prepare().proposal.unwrap().creates[0]["key"], json!(key));
    let mut decisions = setup.email_then_report();
    decisions.push(EventDecision { key: Some(key.clone()), ..walk() });
    let revised = setup.try_dry_run(decisions, DryRun::revising(&p, 2)).unwrap();
    let created: Vec<&CompactionChange> = revised.changes.iter().filter(|c| c.action == "create").collect();
    assert_eq!(created.len(), 1);
    assert_eq!((created[0].key.clone(), created[0].after.as_ref().unwrap().summary.as_deref()), (Some(key), Some("Long walk")));
}

#[test]
fn a_key_that_isnt_the_proposals_is_refused() {
    let (setup, p) = proposed();

    refused(setup.try_dry_run(vec![EventDecision { key: Some(format!("{p}c9")), ..walk() }], DryRun::revising(&p, 1)), "isn't one of proposal");
}

#[test]
fn an_edit_from_an_older_revision_lands_on_the_current_one() {
    let (setup, p) = proposed();
    // Claude renames the report meanwhile.
    setup.try_dry_run(renamed(&setup, 1, "Quarterly report"), DryRun::revising(&p, 1)).unwrap();

    let proposal = setup.amend(&p, 1, vec![rename("e2", "Report draft"), rename("e1", "Inbox")]);

    assert_eq!(proposal.revision, 3);
    assert_eq!((event(&proposal, "e1").summary.as_deref(), event(&proposal, "e2").summary.as_deref()), (Some("Inbox"), Some("Report draft")));
    assert_eq!(proposal.replaced, Some(strings(&["e2"])));
}

#[test]
fn claudes_revision_keeps_the_users_edits() {
    let (setup, p) = proposed();
    setup.amend(&p, 1, vec![rename("e1", "Inbox")]);

    // Claude started from revision 1, before the edit.
    let result = setup.try_dry_run(renamed(&setup, 0, "Mail"), DryRun::revising(&p, 1)).unwrap();

    assert_eq!(change(&result, "e1").after.as_ref().unwrap().summary.as_deref(), Some("Inbox"));
}

#[test]
fn changed_since_an_earlier_revision() {
    let (setup, p) = proposed();
    setup.amend(&p, 1, vec![rename("e1", "Inbox")]);
    setup.amend(&p, 2, vec![rename("e2", "Draft")]);

    let since = |revision: i64| setup.call(|c| c.get_proposal(None, Some(revision))).unwrap().changed_since;
    assert_eq!(since(1), Some(strings(&["e1", "e2"])));
    assert_eq!(since(2), Some(strings(&["e2"])));
}

// -- feedback ---------------------------------------------------------------------------

#[test]
fn feedback_waits_for_claude_and_blocks_confirming() {
    let (setup, p) = proposed();

    let item = add_note(&setup, &p, "the report was a draft", Some("e2"));

    assert_eq!((item.id.clone(), item.status.as_str(), item.by.as_str()), (format!("{p}f1"), "open", "user"));
    assert_eq!(setup.get_proposal().state, "awaiting_claude");
    refused(confirm(&setup, &p, 1), "waiting for Claude");
}

#[test]
fn claude_answers_the_feedback_it_saw_with_a_revision() {
    let (setup, p) = proposed();
    setup.set_now("11:31");
    let item = add_note(&setup, &p, "call the report a draft", Some("e2"));
    setup.set_now("11:32");
    setup.amend(&p, 1, vec![rename("e1", "Inbox")]); // Written after the note.
    let decisions = renamed(&setup, 1, "Report draft");

    refused(setup.try_dry_run(decisions.clone(), DryRun::revising(&p, 2)), &format!("answer every open feedback item.*{}", item.id));
    setup.try_dry_run(decisions, DryRun { replies: reply(&item, "Renamed it"), ..DryRun::revising(&p, 2) }).unwrap();

    let proposal = setup.get_proposal();
    assert_eq!((proposal.revision, proposal.reason.as_str(), proposal.state.as_str()), (3, "revised for notes", "awaiting_review"));
    let feedback: Vec<(String, Option<String>, Option<i64>)> = proposal.feedback.iter().map(|f| (f.status.clone(), f.reply.clone(), f.answered_in)).collect();
    assert_eq!(feedback, vec![("answered".to_string(), Some("Renamed it".to_string()), Some(3))]);
    assert_eq!(event(&proposal, "e2").summary.as_deref(), Some("Report draft"));
}

#[test]
fn feedback_added_after_claude_began_may_stay_open() {
    let (setup, p) = proposed();
    setup.set_now("11:31");
    add_note(&setup, &p, "one more thing", None);

    let result = setup.try_dry_run(setup.email_then_report(), DryRun::revising(&p, 1)).unwrap();

    assert!(result.message.contains("came in after your revision began"), "{}", result.message);
    assert_eq!(setup.get_proposal().state, "awaiting_claude");
}

#[test]
fn a_note_can_be_withdrawn_but_not_once_answered() {
    let (setup, p) = proposed();
    let first = add_note(&setup, &p, "never mind", None);
    assert_eq!(setup.call(|c| c.withdraw_note(&first.id)).unwrap().status, "withdrawn");
    assert_eq!(setup.call(|c| c.withdraw_note(&first.id)).unwrap().status, "withdrawn");
    setup.set_now("11:31");
    let second = add_note(&setup, &p, "rename the report", None);
    setup
        .try_dry_run(setup.email_then_report(), DryRun { replies: reply(&second, "Kept it, it was the report"), ..DryRun::revising(&p, 1) })
        .unwrap();

    refused(setup.call(|c| c.withdraw_note(&second.id)), "already answered, in revision 2");
    assert_eq!(setup.get_proposal().state, "awaiting_review");
}

#[test]
fn claude_overrides_a_users_edit_only_answering_feedback_about_it() {
    let (setup, p) = proposed();
    setup.amend(&p, 1, vec![rename("e1", "Inbox")]);
    let decisions = renamed(&setup, 0, "Mail");
    setup.try_dry_run(decisions.clone(), DryRun::revising(&p, 2)).unwrap();
    assert_eq!(event(&setup.get_proposal(), "e1").summary.as_deref(), Some("Inbox"));
    setup.set_now("11:31");
    let item = add_note(&setup, &p, "actually, call it Mail", Some("e1"));
    setup.set_now("11:32");

    setup.try_dry_run(decisions, DryRun { replies: reply(&item, "Renamed it"), ..DryRun::revising(&p, 3) }).unwrap();

    let proposal = setup.get_proposal();
    assert_eq!(event(&proposal, "e1").summary.as_deref(), Some("Mail"));
    let statuses: Vec<&str> = proposal.user_edits.iter().map(|e| e.status.as_str()).collect();
    assert_eq!(statuses, vec!["replaced"]);
}

#[test]
fn a_users_edit_made_after_claude_began_stands_over_its_answer() {
    let (setup, p) = proposed();
    setup.set_now("11:31");
    let item = add_note(&setup, &p, "call the email Mail", Some("e1"));
    setup.set_now("11:32");
    setup.amend(&p, 1, vec![rename("e1", "Inbox")]);

    // Claude started from revision 1, before the edit.
    setup.try_dry_run(renamed(&setup, 0, "Mail"), DryRun { replies: reply(&item, "Renamed it"), ..DryRun::revising(&p, 1) }).unwrap();

    let proposal = setup.get_proposal();
    assert_eq!(event(&proposal, "e1").summary.as_deref(), Some("Inbox"));
    assert_eq!(proposal.feedback[0].superseded_by, Some(vec![format!("{p}u1")]));
    let statuses: Vec<&str> = proposal.user_edits.iter().map(|e| e.status.as_str()).collect();
    assert_eq!(statuses, vec!["active"]);
}

// -- confirming ---------------------------------------------------------------------------

#[test]
fn confirming_applies_it_and_history_runs_to_its_through() {
    let (setup, p) = proposed();

    let result = confirm(&setup, &p, 1).unwrap();

    assert_eq!((result.status.as_str(), result.proposal.as_ref().unwrap().state.as_str()), ("applied", "applied"));
    let patched: Vec<String> = setup.patches().into_iter().map(|(id, _)| id).collect();
    assert!(patched.contains(&"e1".to_string()) && patched.contains(&"e2".to_string()));
    assert_eq!(setup.journal().last_stamped_now().unwrap(), Some(time_at("11:30")));
    assert!(setup.notes(false).is_empty());
    assert_eq!(setup.journal().open_proposal().unwrap(), None);
}

#[test]
fn only_the_current_revision_can_be_confirmed() {
    let (setup, p) = proposed();
    setup.amend(&p, 1, vec![rename("e1", "Inbox")]);

    refused(confirm(&setup, &p, 1), "isn't proposal .* current one");

    assert!(setup.patches().is_empty());
}

#[test]
fn a_change_since_it_was_proposed_makes_a_revision_to_confirm_again() {
    let (setup, p) = proposed();
    setup.append_note("10:40", Some("phone rang"));

    let result = confirm(&setup, &p, 1).unwrap();

    assert_eq!(result.status, "rechecked");
    let proposal = result.proposal.unwrap();
    assert_eq!((proposal.revision, proposal.by.as_str(), proposal.reason.as_str()), (2, "server", "recheck"));
    assert_eq!(proposal.changed_since, Some(strings(&["e2"])));
    assert!(setup.patches().is_empty());
    assert_eq!(confirm(&setup, &p, 2).unwrap().status, "applied");
}

#[test]
fn one_that_no_longer_plans_is_handed_to_claude() {
    let (setup, p) = proposed();
    setup.t.seed_event(&event_at("10:30-10:50").id("x1").summary("Call").priority(2).build());

    let result = confirm(&setup, &p, 1).unwrap();

    let proposal = result.proposal.unwrap();
    assert_eq!((result.status.as_str(), proposal.state.as_str()), ("needs_claude", "awaiting_claude"));
    let by: Vec<&str> = proposal.feedback.iter().map(|f| f.by.as_str()).collect();
    assert_eq!(by, vec!["server"]);
    assert!(setup.patches().is_empty());
}

#[test]
fn an_apply_that_stops_partway_is_finished_later() {
    let (setup, p) = proposed();
    setup.fail_next("PATCH", "/events/e2", 500);

    refused(confirm(&setup, &p, 1), "finish_proposal");
    assert_eq!(summary(&setup).state, "applying");
    refused(setup.try_amend(&p, 1, Amend { decisions: vec![rename("e1", "Inbox")], ..Default::default() }), "being applied");

    assert_eq!(setup.call(|c| c.finish(&p)).unwrap().status, "applied");
    assert_eq!(setup.journal().open_proposal().unwrap(), None);
}

#[test]
fn a_write_that_can_never_succeed_proposes_whats_left() {
    // The report was deleted meanwhile.
    let (setup, p) = proposed();
    setup.fail_next("PATCH", "/events/e2", 404);

    let result = confirm(&setup, &p, 1).unwrap();

    assert_eq!(result.status, "rebuilt");
    assert_eq!(setup.load(&format!("{p}r1")).status, FAILED);
    let proposal = result.proposal.unwrap();
    assert_eq!(
        (proposal.revision, proposal.by.as_str(), proposal.reason.as_str(), proposal.state.as_str()),
        (2, "server", "apply failed", "awaiting_review")
    );
    assert!(!proposal.events.as_ref().unwrap().iter().any(|e| e.id == "e2"));
    assert!(proposal.warnings[0].contains("leaving out what was about e2"), "{:?}", proposal.warnings);
    assert_eq!(confirm(&setup, &p, 2).unwrap().status, "applied");
}

#[test]
fn a_proposal_can_be_abandoned() {
    let (setup, p) = proposed();

    assert_eq!(setup.call(|c| c.abandon(&p)).unwrap().status, "abandoned");

    assert_eq!(setup.journal().open_proposal().unwrap(), None);
    assert_ne!(setup.dry_run(setup.email_then_report()).proposal_id, Some(p));
}

#[test]
fn only_the_last_few_superseded_revisions_are_kept_whole() {
    let (setup, p) = proposed();
    for revision in 1..6 {
        setup.try_dry_run(setup.email_then_report(), DryRun::revising(&p, revision)).unwrap();
    }

    setup.call(|c| c.journal.garbage_collect()).unwrap();

    // 1-5 superseded, 6 current: the newest three superseded kept whole,
    // the rest only their compaction rows.
    let whole: Vec<bool> = (1..7).map(|n| !setup.load(&format!("{p}r{n}")).steps.is_empty()).collect();
    assert_eq!(whole, vec![false, false, true, true, true, true]);
    assert_eq!(setup.journal().revision_meta(&p, 1).unwrap().unwrap().revision, 1);
}
