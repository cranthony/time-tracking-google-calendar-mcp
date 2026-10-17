//! TestEditNote, TestDeleteNote, TestPrepareCompaction, TestCompactNotes,
//! TestProposalTools, TestAbandonCompaction, TestGetCompactionStatus and
//! TestJudgmentTools: the compaction tools' arguments, as the compactor
//! takes them, end to end. (What the compactor then does is
//! compaction/compactor_tests' to check.)

use super::*;
use crate::stores::journal::JournalCompaction;

/// Email (09:00-10:00) and Report (10:00-11:00), notes at 09:05 and 10:20,
/// and the clock at 11:30.
fn day() -> (Server, String, String) {
    let server = Server::new();
    server.seed(&[
        Event { summary: Some("Email".into()), priority: Some(2), ..focus("e1", at(9, 0), at(10, 0)) },
        Event { summary: Some("Report".into()), priority: Some(2), ..focus("e2", at(10, 0), at(11, 0)) },
    ]);
    let note = |time: &str, text: &str| server.ok("note", json!({"noted_time": {"timestamp": format!("2026-01-01T{time}:00+00:00"), "description": text}}))["id"].as_str().unwrap().to_string();
    let (n1, n2) = (note("09:05", "email"), note("10:20", "report"));
    crate::determinism::TEST_NOW.with(|c| c.set(Some(at(11, 30))));
    (server, n1, n2)
}

fn recap() -> Value {
    json!({"overview": "A quiet day", "did": [{"text": "Kept it as planned"}], "unsure": []})
}

fn compact(server: &Server, mut args: Value) -> Value {
    args["recap"] = recap();
    server.ok("compact_notes", args)
}

fn journal(server: &Server, compaction_id: &Value) -> JournalCompaction {
    server.t.app.journal().unwrap().load(compaction_id.as_str().unwrap()).unwrap()
}

fn decisions(server: &Server, compaction_id: &Value) -> Vec<Value> {
    journal(server, compaction_id).decisions.iter().map(|d| d.to_json_dict()).collect()
}

// -- notes --------------------------------------------------------------------------------

#[test]
fn edit_note_keeps_its_id() {
    let (server, n1, _) = day();

    let edited = server.ok("edit_note", json!({"note_id": n1, "timestamp": "2026-01-01T09:30:00+00:00"}));

    assert_eq!(edited["id"], json!(n1));
    assert_eq!(edited["description"], "email");
}

#[test]
fn a_refused_note_edit_or_delete_is_a_tool_error() {
    let (server, _, _) = day();

    server.refused("edit_note", json!({"note_id": "2026-01-01T08:00:00+00:00#2", "description": "x"}), "no longer holds the note");
    server.refused("delete_note", json!({"note_id": "2026-01-01T08:00:00+00:00#2"}), "no longer holds the note");
}

#[test]
fn delete_note_returns_the_note() {
    let (server, _, n2) = day();

    let deleted = server.ok("delete_note", json!({"note_id": n2}));

    assert_eq!(deleted["description"], "report");
    assert_eq!(server.ok("get_notes", json!({})).as_array().unwrap().len(), 1);
}

// -- preparing and proposing ---------------------------------------------------------------

#[test]
fn prepare_compaction_offers_the_days_notes() {
    let (server, n1, n2) = day();

    let context = server.ok("prepare_compaction", json!({}));

    let notes: Vec<Value> = context["notes"].as_array().unwrap().iter().map(|n| n["id"].clone()).collect();
    assert_eq!(notes, vec![json!(n1), json!(n2)]);
    assert!(context["instructions"].as_str().unwrap().contains("SILENCE MEANS ON SCHEDULE"));
}

#[test]
fn a_dry_run_plans_its_updates_creates_and_cancels() {
    let (server, n1, n2) = day();

    let result = compact(
        &server,
        json!({
            "updates": [{"event_id": "e1", "end": "2026-01-01T09:30:00+00:00"}],
            "creates": [{"summary": "Walk", "start": "2026-01-01T09:30:00+00:00", "end_note": n2}],
            "cancels": [{"event_id": "e2", "counts_against_follow_through": false}],
        }),
    );

    assert_eq!(result["status"], "proposed");
    let made = decisions(&server, &result["compaction_id"]);
    assert_eq!(made.len(), 3);
    assert_eq!((made[0]["action"].clone(), made[0]["event_id"].clone(), made[0]["end"].clone()), (json!("keep"), json!("e1"), json!("2026-01-01T09:30:00+00:00")));
    assert_eq!((made[1]["action"].clone(), made[1]["summary"].clone(), made[1]["end_note"].clone()), (json!("create"), json!("Walk"), json!(n2)));
    assert_eq!((made[2]["action"].clone(), made[2]["event_id"].clone(), made[2]["counts_against_follow_through"].clone()), (json!("cancel"), json!("e2"), json!(false)));
    let _ = n1;
}

#[test]
fn a_dry_run_passes_ignored_notes_through() {
    let (server, _, n2) = day();

    let result = compact(&server, json!({"updates": [{"event_id": "e1"}], "ignore_notes": [n2]}));

    assert_eq!(journal(&server, &result["compaction_id"]).ignore_notes, vec![n2]);
}

#[test]
fn a_dry_run_with_no_decisions_records_everything_as_on_schedule() {
    let (server, _, _) = day();

    let result = compact(&server, json!({}));

    assert_eq!(result["status"], "proposed");
    assert!(decisions(&server, &result["compaction_id"]).is_empty());
}

#[test]
fn a_dry_run_passes_what_it_adds_through() {
    let (server, _, _) = day();

    let result = compact(
        &server,
        json!({
            "new_actions": [{"ref": "new:juggle", "name": "Juggle"}],
            "new_people": [{"ref": "new:alex", "name": "Alex"}],
            "new_locations": [{"ref": "new:park", "name": "Park"}],
            "updates": [{"event_id": "e1", "action_ids": ["new:juggle"], "facts": {"location_ids": ["new:park"], "with_ids": ["new:alex"]}}],
        }),
    );

    assert_eq!(
        result["additions"],
        json!({"actions": [{"ref": "new:juggle", "name": "Juggle"}], "people": [{"ref": "new:alex", "name": "Alex"}], "locations": [{"ref": "new:park", "name": "Park"}]})
    );
}

#[test]
fn revising_a_proposal_passes_it_and_the_replies_through() {
    let (server, _, _) = day();
    let first = compact(&server, json!({}));
    let p = first["proposal_id"].clone();
    crate::determinism::TEST_NOW.with(|c| c.set(Some(at(11, 31))));
    let item = server.ok("add_proposal_note", json!({"proposal_id": p, "text": "the report was a draft", "event_id": "e2"}));
    crate::determinism::TEST_NOW.with(|c| c.set(Some(at(11, 32))));

    let revised = compact(&server, json!({"proposal_id": p, "revision": 1, "replies": [{"feedback_id": item["id"], "reply": "Noted"}]}));

    assert_eq!((revised["proposal_id"].clone(), revised["revision"].clone()), (p.clone(), json!(2)));
    let proposal = server.ok("get_proposal", json!({}));
    assert_eq!((proposal["feedback"][0]["status"].clone(), proposal["feedback"][0]["reply"].clone()), (json!("answered"), json!("Noted")));
}

#[test]
fn compaction_errors_become_tool_errors() {
    let (server, _, _) = day();

    server.refused("compact_notes", json!({"recap": recap(), "updates": [{"event_id": "nope"}]}), "isn't one of this day's events");
}

// -- proposals ----------------------------------------------------------------------------------

fn proposed() -> (Server, Value, String, String) {
    let (server, n1, n2) = day();
    let p = compact(&server, json!({}))["proposal_id"].clone();
    (server, p, n1, n2)
}

#[test]
fn get_proposal_gives_the_current_revision_and_what_changed_since() {
    let (server, p, _, _) = proposed();
    server.ok("amend_proposal", json!({"proposal_id": p, "revision": 1, "updates": [{"event": {"id": "e1", "summary": "Inbox"}}]}));

    let proposal = server.ok("get_proposal", json!({"since_revision": 1}));

    assert_eq!((proposal["id"].clone(), proposal["revision"].clone(), proposal["changed_since"].clone()), (p, json!(2), json!(["e1"])));
}

#[test]
fn amend_proposal_takes_the_event_tools_types() {
    let (server, p, n1, n2) = proposed();

    let proposal = server.ok(
        "amend_proposal",
        json!({
            "proposal_id": p,
            "revision": 1,
            "updates": [{"event": {"id": "e1", "end": "2026-01-01T09:50:00+00:00", "description": "Inbox zero", "priority": 1}, "clear_fields": ["location"], "start_note": n1}],
            "creates": [{"summary": "Walk", "start": "2026-01-01T11:00:00+00:00", "end": "2026-01-01T11:20:00+00:00", "location": "the park"}],
            "cancels": [{"event_id": "e2", "counts_against_follow_through": true}],
            "notes": [{"note_id": n2, "use": "ignore"}],
        }),
    );

    let edits: Vec<Value> = proposal["user_edits"].as_array().unwrap().iter().map(|e| e["edit"].clone()).collect();
    let keep = edits.iter().find(|e| e["action"] == "keep").unwrap();
    assert_eq!(
        (keep["event_id"].clone(), keep["end"].clone(), keep["start_note"].clone(), keep["description"].clone(), keep["location"].clone(), keep["priority"].clone()),
        (json!("e1"), json!("2026-01-01T09:50:00+00:00"), json!(n1), json!("Inbox zero"), json!(""), json!(1))
    );
    let create = edits.iter().find(|e| e["action"] == "create").unwrap();
    assert_eq!((create["summary"].clone(), create["location"].clone()), (json!("Walk"), json!("the park")));
    let cancel = edits.iter().find(|e| e["action"] == "cancel").unwrap();
    assert_eq!((cancel["event_id"].clone(), cancel["counts_against_follow_through"].clone()), (json!("e2"), json!(true)));
    assert!(edits.iter().any(|e| e["action"] == "note" && e["use"] == "ignore"));
}

#[test]
fn amend_proposal_changes_history_only_once_the_user_approves() {
    // The last compaction ran at 09:30, as the email was going on.
    let server = Server::new();
    server.seed(&[Event { summary: Some("Email".into()), ..focus("e1", at(9, 0), at(10, 0)) }]);
    {
        let journal = server.t.app.journal().unwrap();
        let _held = crate::write_lock::WRITE_LOCK.hold();
        let plan = crate::compaction::plan::CompactionPlan::default();
        let day = crate::stores::journal::PlannedDay {
            compaction_id: "prev".into(),
            now: at(9, 30),
            note_ids: vec![],
            decisions: &[],
            plan: &plan,
            ignore_notes: vec![],
            additions: Default::default(),
            note_targets: vec![],
        };
        journal.start_batch(&[day], None, &[]).unwrap();
        let mut prev = journal.load("prev").unwrap();
        journal.set_status(&mut prev, crate::stores::journal::STAMPED).unwrap();
    }
    crate::determinism::TEST_NOW.with(|c| c.set(Some(at(11, 30))));
    let p = compact(&server, json!({}))["proposal_id"].clone();
    let moved = json!({"proposal_id": p, "revision": 1, "updates": [{"event": {"id": "e1", "start": "2026-01-01T09:10:00+00:00"}}]});

    server.refused("amend_proposal", moved.clone(), "allow_compacted_changes");
    let mut allowed = moved;
    allowed["allow_compacted_changes"] = json!(true);
    let proposal = server.ok("amend_proposal", allowed);

    assert_eq!(proposal["user_edits"][0]["edit"]["allow_history"], true);
}

#[test]
fn clearing_a_description_or_facts_sends_them_empty() {
    let (server, p, _, _) = proposed();

    let proposal = server.ok("amend_proposal", json!({"proposal_id": p, "revision": 1, "updates": [{"event": {"id": "e1"}, "clear_fields": ["description", "facts"]}]}));

    let edit = &proposal["user_edits"][0]["edit"];
    assert_eq!(edit["description"], "");
    assert!(edit["facts"].is_null() || edit["facts"] == json!({}), "{edit}");
}

#[test]
fn amend_proposal_refuses_what_a_proposal_cant_take() {
    let (server, p, _, _) = proposed();

    for (update, message) in [
        (json!({"event": {"id": "e1", "is_cancelled": true}}), "put it in `cancels`"),
        (json!({"event": {"id": "e1", "judgments": {}}}), "judgments are made once"),
        (json!({"event": {"id": "e1"}, "clear_fields": ["priority"]}), "can't clear an event's priority"),
        (json!({"event": {"summary": "x"}}), "needs the event's id"),
        (json!({"event": {"id": "e1", "description": "x"}, "clear_fields": ["description"]}), "description: set or cleared, not both"),
    ] {
        server.refused("amend_proposal", json!({"proposal_id": p, "revision": 1, "updates": [update]}), message);
    }

    assert!(server.t.app.journal().unwrap().user_edits(p.as_str().unwrap()).unwrap().is_empty());
}

#[test]
fn notes_for_claude_are_added_and_withdrawn() {
    let (server, p, _, _) = proposed();

    let item = server.ok("add_proposal_note", json!({"proposal_id": p, "text": "it was a walk", "event_id": "e1"}));
    assert_eq!((item["status"].clone(), item["event_id"].clone(), item["text"].clone()), (json!("open"), json!("e1"), json!("it was a walk")));

    assert_eq!(server.ok("withdraw_proposal_note", json!({"feedback_id": item["id"]}))["status"], "withdrawn");
}

#[test]
fn confirming_applies_and_finishing_after_says_so() {
    let (server, p, _, _) = proposed();

    let confirmed = server.ok("confirm_proposal", json!({"proposal_id": p, "revision": 1}));
    assert_eq!(confirmed["status"], "applied");

    // Nothing left to finish: it says it's applied.
    assert_eq!(server.ok("finish_proposal", json!({"proposal_id": p}))["status"], "applied");
}

#[test]
fn proposal_errors_become_tool_errors() {
    let (server, p, _, _) = proposed();
    server.ok("add_proposal_note", json!({"proposal_id": p, "text": "one thing"}));

    server.refused("confirm_proposal", json!({"proposal_id": p, "revision": 1}), "waiting for Claude");
}

#[test]
fn abandoning_a_proposal_and_its_errors() {
    let (server, p, _, _) = proposed();

    assert_eq!(server.ok("abandon_compaction", json!({"proposal_id": p}))["status"], "abandoned");
    server.refused("abandon_compaction", json!({"proposal_id": "nope"}), "");
}

// -- status and judgments -------------------------------------------------------------------------

#[test]
fn the_status_reports_the_last_compaction_and_latest_compacted_note() {
    let (server, p, _, _) = proposed();
    server.ok("confirm_proposal", json!({"proposal_id": p, "revision": 1}));

    let status = server.ok("get_compaction_status", json!({}));

    assert_eq!(status["last_compaction"], when(at(11, 30)));
    assert_eq!(status["latest_compacted_note"]["description"], "report");
    assert!(status["proposal"].is_null());
}

#[test]
fn the_status_is_empty_before_any_compaction() {
    let server = Server::new();

    let status = server.ok("get_compaction_status", json!({}));

    assert_eq!(status, json!({"last_compaction": null, "latest_compacted_note": null, "judgments_pending": null, "proposal": null}));
}

#[test]
fn prepare_judgments_without_a_compaction_says_so() {
    let server = Server::new();

    server.refused("prepare_judgments", json!({}), "there's no applied compaction to judge");
}

#[test]
fn judgments_are_prepared_and_recorded_for_an_applied_compaction() {
    let (server, p, _, _) = proposed();
    let confirmed = server.ok("confirm_proposal", json!({"proposal_id": p, "revision": 1}));
    let compaction_id = confirmed["proposal"]["id"].as_str().map(|id| format!("{id}r1")).unwrap();

    let due = server.ok("prepare_judgments", json!({"compaction_id": compaction_id}));
    assert_eq!(due["compaction_id"], json!(compaction_id));

    server.refused("record_judgments", json!({"compaction_id": compaction_id, "judgments": [{"request_id": "e1/x/y/z", "rating": 1, "reasoning": "r"}]}), "isn't one of");
}
