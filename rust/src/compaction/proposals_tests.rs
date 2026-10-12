//! Proposals' tests (tests/test_compaction_proposals.py, ported).

use super::*;
use crate::compaction::plan::CompactionPlan;
use crate::google::sheets::SheetsClient;
use crate::stores::journal::{CompactionJournal, PlannedDay, RevisionMeta, APPLYING, PROPOSED, STAMPED};
use crate::testing::fake_google::FakeGoogle;
use crate::testing::{strings, time_at};
use serde_json::json;

const P: &str = "0123456789ab";

fn edit(seq: i64, event_id: Option<&str>, fields: Value) -> UserEdit {
    edit_with_status(seq, event_id, "active", fields)
}

fn edit_with_status(seq: i64, event_id: Option<&str>, status: &str, fields: Value) -> UserEdit {
    UserEdit {
        id: format!("{P}u{seq}"),
        seq,
        event_id: event_id.map(str::to_string),
        edit: fields.as_object().cloned().unwrap(),
        status: status.into(),
        created: time_at("11:00"),
        base_revision: 1,
    }
}

fn by(merged: &Merged, name: &str) -> EventDecision {
    merged
        .decisions
        .iter()
        .find(|d| (if d.action == "create" { d.key.as_deref() } else { d.event_id.as_deref() }) == Some(name))
        .cloned()
        .unwrap_or_else(|| panic!("no decision for {name}"))
}

fn names(merged: &Merged) -> Vec<String> {
    merged.decisions.iter().map(|d| if d.action == "create" { d.key.clone() } else { d.event_id.clone() }.unwrap_or_default()).collect()
}

fn iso(at: &str) -> String {
    crate::py::isoformat(&time_at(at))
}

#[test]
fn an_edit_overrides_claudes_decision_field_by_field() {
    let claude = vec![EventDecision {
        event_id: Some("e1".into()),
        start_note: Some("n1".into()),
        end_note: Some("n2".into()),
        summary: Some("Email".into()),
        ..EventDecision::new("keep")
    }];
    let merged = merge(P, &claude, &[edit(1, Some("e1"), json!({"action": "keep", "end": iso("10:30")}))], MergeOptions::default());
    let email = by(&merged, "e1");
    assert_eq!(
        (email.start_note.as_deref(), email.end_note.as_deref(), email.end, email.summary.as_deref()),
        (Some("n1"), None, Some(time_at("10:30")), Some("Email"))
    );
    assert_eq!(merged.decided_by.items, vec![("e1".to_string(), "user".to_string())]);
}

#[test]
fn a_later_edit_wins_and_as_planned_clears_everything_before_it() {
    let claude = vec![EventDecision { event_id: Some("e1".into()), ..EventDecision::new("cancel") }];
    let edits = vec![
        edit(1, Some("e1"), json!({"action": "keep", "summary": "Inbox"})),
        edit(2, Some("e2"), json!({"action": "cancel", "counts_against_follow_through": false})),
        edit(3, Some("e1"), json!({"action": "as_planned"})),
    ];
    let merged = merge(P, &claude, &edits, MergeOptions::default());
    assert_eq!(names(&merged), strings(&["e2"]));
    assert_eq!(by(&merged, "e2").counts_against_follow_through, Some(false));
}

#[test]
fn the_users_creates_are_keyed_by_their_edit_and_can_be_cancelled() {
    let edits = vec![
        edit(1, None, json!({"action": "create", "summary": "Walk", "start": iso("11:00"), "end": iso("11:20")})),
        edit(2, None, json!({"action": "create", "summary": "Nap", "start": iso("13:00"), "end": iso("13:20")})),
        edit(3, Some(&format!("{P}u2")), json!({"action": "cancel"})),
    ];
    let merged = merge(P, &[], &edits, MergeOptions::default());
    assert_eq!(names(&merged), vec![format!("{P}u1")]);
}

#[test]
fn an_edit_of_a_key_or_event_that_isnt_there_is_unknown() {
    let edits = vec![edit(1, Some(&format!("{P}c4")), json!({"action": "keep", "summary": "x"})), edit(2, Some("gone"), json!({"action": "keep", "summary": "y"}))];
    let known = strings(&["e1"]);
    let merged = merge(P, &[], &edits, MergeOptions { known_ids: Some(&known), ..Default::default() });
    assert_eq!(merged.unknown.iter().map(|e| e.seq).collect::<Vec<_>>(), vec![1, 2]);
    assert!(merged.decisions.is_empty());
}

#[test]
fn edits_that_arent_active_are_left_out() {
    let merged = merge(P, &[], &[edit_with_status(1, Some("e1"), "replaced", json!({"action": "keep", "summary": "x"}))], MergeOptions::default());
    assert!(merged.decisions.is_empty());
}

#[test]
fn a_key_of_an_event_already_created_follows_it() {
    let aliases = vec![(format!("{P}c1"), "cmpx".to_string())];
    let merged =
        merge(P, &[], &[edit(1, Some(&format!("{P}c1")), json!({"action": "keep", "summary": "Long walk"}))], MergeOptions { aliases: &aliases, ..Default::default() });
    assert_eq!(by(&merged, "cmpx").summary.as_deref(), Some("Long walk"));
}

#[test]
fn the_users_note_edits_override_claudes_and_as_planned_restores_them() {
    let edits = vec![
        edit(1, Some("t#1"), json!({"action": "note", "use": "annotate"})),
        edit(2, Some("t#2"), json!({"action": "note", "use": "annotate", "event_id": "e2"})),
        edit(3, Some("t#3"), json!({"action": "note", "use": "ignore"})),
        edit(4, Some("t#2"), json!({"action": "as_planned"})),
    ];
    let ignore = strings(&["t#1"]);
    let targets = vec![("t#2".to_string(), "e1".to_string())];
    let merged = merge(P, &[], &edits, MergeOptions { claude_ignore: &ignore, claude_targets: &targets, ..Default::default() });
    assert_eq!(merged.ignore_notes, strings(&["t#3"]));
    let mut note_targets = merged.note_targets.items.clone();
    note_targets.sort();
    assert_eq!(note_targets, vec![("t#1".to_string(), None), ("t#2".to_string(), Some("e1".to_string()))]);
    let mut decided = merged.notes_decided_by.items.clone();
    decided.sort();
    assert_eq!(decided, vec![("t#1".into(), "user".into()), ("t#2".into(), "claude".into()), ("t#3".into(), "user".into())]);
}

#[test]
fn a_note_edit_naming_a_note_or_event_that_isnt_there_is_unknown() {
    let edits = vec![
        edit(1, Some("t#9"), json!({"action": "note", "use": "ignore"})),
        edit(2, Some("t#1"), json!({"action": "note", "use": "annotate", "event_id": "gone"})),
    ];
    let (known, notes) = (strings(&["e1"]), strings(&["t#1"]));
    let merged = merge(P, &[], &edits, MergeOptions { known_ids: Some(&known), known_notes: Some(&notes), ..Default::default() });
    assert_eq!(merged.unknown.iter().map(|e| e.seq).collect::<Vec<_>>(), vec![1, 2]);
    assert!(merged.note_targets.is_empty());
}

#[test]
fn a_note_left_with_a_create_the_user_cancelled_goes_where_it_falls() {
    let walk = EventDecision { summary: Some("Walk".into()), key: Some(format!("{P}c1")), ..EventDecision::new("create") };
    let targets = vec![("t#1".to_string(), format!("{P}c1"))];
    let merged = merge(P, &[walk], &[edit(1, Some(&format!("{P}c1")), json!({"action": "cancel"}))], MergeOptions { claude_targets: &targets, ..Default::default() });
    assert!(merged.note_targets.is_empty());
}

#[test]
fn ids() {
    assert!(is_key(P, Some(&format!("{P}c12"))) && is_key(P, Some(&format!("{P}u3"))));
    assert!(!is_key(P, Some("e1")) && !is_key(P, Some(&format!("{P}f1"))));
    assert_eq!(split_feedback_id(&format!("{P}f7")), Some((P.to_string(), 7)));
    assert_eq!(split_feedback_id("nope"), None);
}

// -- journal rows -----------------------------------------------------------------

fn journal() -> (FakeGoogle, CompactionJournal) {
    let fake = FakeGoogle::start();
    let sheets = SheetsClient::new(fake.google());
    let _held = crate::write_lock::WRITE_LOCK.hold();
    let spreadsheet = sheets.create_spreadsheet("Calendar Metadata").unwrap();
    let journal = CompactionJournal::ensure(&sheets, &spreadsheet).unwrap();
    (fake, journal)
}

fn revision(journal: &CompactionJournal, number: i64) -> Vec<crate::stores::journal::JournalCompaction> {
    let meta = RevisionMeta {
        proposal: P.into(),
        revision: number,
        window_start: Some(time_at("08:00")),
        created: Some(time_at("11:30")),
        base: None,
        user_seq: 0,
        by: "claude".into(),
        reason: "proposed".into(),
        claude_decisions: vec![EventDecision { event_id: Some("e1".into()), summary: Some("Inbox".into()), ..EventDecision::new("keep") }],
        ..Default::default()
    };
    let plan = CompactionPlan::default();
    let day = |id: String| PlannedDay {
        compaction_id: id,
        now: time_at("11:30"),
        note_ids: Vec::new(),
        decisions: &[],
        plan: &plan,
        ignore_notes: Vec::new(),
        additions: Map::new(),
        note_targets: Vec::new(),
    };
    let _held = crate::write_lock::WRITE_LOCK.hold();
    journal.start_batch(&[day(format!("{P}r{number}")), day(format!("{P}r{number}d2"))], Some(&meta), &[]).unwrap();
    journal.load_batch(&format!("{P}r{number}")).unwrap()
}

#[test]
fn a_revision_round_trips_its_meta_and_claudes_decisions() {
    let (_fake, journal) = journal();
    let days = revision(&journal, 1);
    let (first, second) = (&days[0], &days[1]);
    assert_eq!((first.status.as_str(), first.proposal.as_deref(), first.revision, second.proposal.as_deref()), (PROPOSED, Some(P), 1, Some(P)));
    assert_eq!(
        first.meta().claude_decisions,
        vec![EventDecision { event_id: Some("e1".into()), summary: Some("Inbox".into()), ..EventDecision::new("keep") }]
    );
    assert_eq!(first.meta().window_start, Some(time_at("08:00")));
    assert_eq!(journal.revisions(P).unwrap(), vec![(1, format!("{P}r1"), PROPOSED.to_string())]);
}

#[test]
fn a_proposal_applied_partway_is_still_open() {
    let (_fake, journal) = journal();
    let mut days = revision(&journal, 1);
    let _held = crate::write_lock::WRITE_LOCK.hold();
    let (first, second) = days.split_at_mut(1);
    journal.set_status(&mut first[0], STAMPED).unwrap();
    journal.set_status(&mut second[0], APPLYING).unwrap();
    assert_eq!(journal.open_proposal().unwrap().as_deref(), Some(P));
    journal.set_status(&mut second[0], STAMPED).unwrap();
    assert_eq!(journal.open_proposal().unwrap(), None);
}

#[test]
fn user_edits_and_feedback_round_trip() {
    let (_fake, journal) = journal();
    let _held = crate::write_lock::WRITE_LOCK.hold();
    let user_edit = edit(1, Some("e1"), json!({"action": "keep", "summary": "Inbox"}));
    journal.add_user_edits(std::slice::from_ref(&user_edit)).unwrap();
    let item = Feedback {
        id: format!("{P}f1"),
        seq: 1,
        text: "hm".into(),
        event_id: Some("e1".into()),
        note_id: None,
        at: Some(time_at("10:00")),
        by: "user".into(),
        created: time_at("11:31"),
        status: "open".into(),
        reply: None,
        answered_in: None,
        superseded_by: None,
    };
    journal.add_feedback(&item, P).unwrap();
    let edits = journal.user_edits(P).unwrap();
    let feedback = journal.feedback(P).unwrap();
    assert_eq!(edits.len(), 1);
    assert_eq!(edits[0].1, user_edit);
    assert_eq!(feedback[0].1, item);
    journal.set_user_edit_status(edits[0].0, "replaced").unwrap();
    let answered = Feedback { status: "answered".into(), reply: Some("done".into()), answered_in: Some(2), ..item };
    journal.update_feedback(feedback[0].0, &answered).unwrap();
    assert_eq!(journal.user_edits(P).unwrap()[0].1.status, "replaced");
    assert_eq!(journal.feedback(P).unwrap()[0].1, answered);
}
