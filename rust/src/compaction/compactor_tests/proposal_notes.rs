//! TestExtendingAProposal, TestProposalNotes and TestSettlingAdditions.

use super::*;
use crate::compaction::proposals::ProposalNote;
use crate::testing::facts;

fn note<'a>(proposal: &'a Proposal, note_id: &str) -> &'a ProposalNote {
    proposal.notes.iter().find(|n| n.id == note_id).unwrap_or_else(|| panic!("no note {note_id} in the proposal"))
}

fn event<'a>(proposal: &'a Proposal, event_id: &str) -> &'a ProposalEvent {
    proposal.events.as_ref().unwrap().iter().find(|e| e.id == event_id).unwrap_or_else(|| panic!("no {event_id} in the proposal"))
}

fn note_edit(given: Value) -> NoteEdit {
    NoteEdit::from_value(&given)
}

fn notes(setup: &Setup, p: &str, revision: i64, edits: Vec<NoteEdit>) -> anyhow::Result<Proposal> {
    setup.try_amend(p, revision, Amend { notes: edits, ..Default::default() })
}

fn edits(proposal: &Proposal) -> Vec<Value> {
    proposal.user_edits.iter().map(|e| Value::Object(e.edit.clone())).collect()
}

// -- extending a proposal ---------------------------------------------------------------
// The user extends the open proposal past where Claude's revision ran to:
// the notes it takes in are added where they fall.

fn extendable() -> (Setup, String) {
    let setup = Setup::standard();
    let p = setup.dry_run(setup.email_then_report()).proposal_id.unwrap();
    // Later, a note after the proposal's end.
    setup.append_note("12:05", Some("lunch"));
    setup.set_now("12:30");
    (setup, p)
}

fn extend(setup: &Setup, p: &str, through: &str, edits: Vec<NoteEdit>) -> anyhow::Result<Proposal> {
    setup.try_amend(p, 1, Amend { notes: edits, through: Some(time_at(through)), ..Default::default() })
}

#[test]
fn it_runs_on_and_takes_in_the_notes_there() {
    let (setup, p) = extendable();

    let proposal = extend(&setup, &p, "12:15", vec![]).unwrap();

    assert_eq!((proposal.through, proposal.claude_through), (time_at("12:15"), Some(time_at("11:30"))));
    let lunch = note(&proposal, &setup.note_id(4));
    assert_eq!((lunch.use_.as_str(), lunch.event_id.as_deref(), lunch.decided_by.as_deref()), ("annotates", Some("e3"), Some("user")));
    let made: Vec<(Option<String>, Value)> = proposal.user_edits.iter().map(|e| (e.event_id.clone(), Value::Object(e.edit.clone()))).collect();
    assert_eq!(made, vec![(Some(setup.note_id(4)), json!({"action": "note", "use": "annotate"}))]);
    // Kept as the revision's end, as it's planned again.
    let again = setup.get_proposal();
    assert_eq!((again.through, again.claude_through), (time_at("12:15"), Some(time_at("11:30"))));
}

#[test]
fn a_note_the_user_says_otherwise_about_is_left_as_they_say() {
    let (setup, p) = extendable();

    let proposal = extend(&setup, &p, "12:15", vec![note_edit(json!({"note_id": setup.note_id(4), "use": "ignore"}))]).unwrap();

    assert_eq!(note(&proposal, &setup.note_id(4)).use_, "ignored");
}

#[test]
fn with_no_notes_there_it_still_runs_on() {
    let setup = Setup::standard();
    let p = setup.dry_run(setup.email_then_report()).proposal_id.unwrap();
    setup.set_now("12:30");

    let proposal = extend(&setup, &p, "12:15", vec![]).unwrap();

    assert_eq!(proposal.through, time_at("12:15"));
    assert!(proposal.user_edits.is_empty());
}

#[test]
fn it_can_only_be_extended_and_never_past_now() {
    let (setup, p) = extendable();

    refused(extend(&setup, &p, "11:00", vec![]), "only be extended");
    refused(extend(&setup, &p, "13:00", vec![]), "past now");
    assert_eq!(setup.journal().revisions(&p).unwrap(), vec![(1, format!("{p}r1"), PROPOSED.to_string())]);
}

#[test]
fn claudes_next_revision_runs_to_now() {
    let (setup, p) = extendable();
    extend(&setup, &p, "12:15", vec![]).unwrap();

    setup.try_dry_run(setup.email_then_report(), DryRun::revising(&p, 2)).unwrap();

    let proposal = setup.get_proposal();
    assert_eq!((proposal.through, proposal.claude_through), (time_at("12:30"), Some(time_at("12:30"))));
}

// -- proposal notes ---------------------------------------------------------------------
// What notes are for: Claude ignores a note, or adds it to a particular
// event; the user's note edits override that.

fn ignoring_the_email_note() -> (Setup, String) {
    let setup = Setup::standard();
    let p = setup.try_dry_run(vec![], DryRun { ignore_notes: Some(vec![setup.note_id(2)]), ..Default::default() }).unwrap().proposal_id.unwrap();
    (setup, p)
}

fn annotate(setup: &Setup, event_id: Option<&str>) -> NoteEdit {
    note_edit(json!({"note_id": setup.note_id(2), "use": "annotate", "event_id": event_id}))
}

#[test]
fn the_proposal_says_what_each_note_is_for_and_who_said_so() {
    let (setup, _p) = ignoring_the_email_note();

    let proposal = setup.get_proposal();

    let (email, report) = (note(&proposal, &setup.note_id(2)), note(&proposal, &setup.note_id(3)));
    assert_eq!((email.use_.as_str(), email.event_id.as_deref(), email.decided_by.as_deref()), ("ignored", None, Some("claude")));
    assert_eq!((report.use_.as_str(), report.event_id.as_deref(), report.decided_by.as_deref()), ("annotates", Some("e2"), None));
}

#[test]
fn the_user_annotates_a_note_claude_ignored() {
    let (setup, p) = ignoring_the_email_note();

    let proposal = notes(&setup, &p, 1, vec![annotate(&setup, None)]).unwrap();

    let email = note(&proposal, &setup.note_id(2));
    assert_eq!((email.use_.as_str(), email.event_id.as_deref(), email.decided_by.as_deref()), ("annotates", Some("e1"), Some("user")));
    assert_eq!(event(&proposal, "e1").description.as_deref(), Some("Notes:\n- 09:05 email"));
    assert_eq!(edits(&proposal), vec![json!({"action": "note", "use": "annotate"})]);
}

#[test]
fn the_user_adds_a_note_to_another_event() {
    let (setup, p) = ignoring_the_email_note();

    let proposal = notes(&setup, &p, 1, vec![annotate(&setup, Some("e2"))]).unwrap();

    assert_eq!(note(&proposal, &setup.note_id(2)).event_id.as_deref(), Some("e2"));
    assert_eq!(event(&proposal, "e2").description.as_deref(), Some("Notes:\n- 09:05 email\n- 10:20 report"));
}

#[test]
fn annotating_an_edge_note_adds_it_to_its_event_and_keeps_the_edge() {
    let setup = Setup::standard();
    let p = setup.dry_run(setup.email_then_report()).proposal_id.unwrap();
    let before = setup.get_proposal();
    let before = note(&before, &setup.note_id(2));
    assert_eq!((before.use_.as_str(), before.event_id.as_deref(), before.edge_of.as_deref()), ("edge", Some("e1"), Some("e1")));

    let proposal = notes(&setup, &p, 1, vec![annotate(&setup, None)]).unwrap();

    let email = note(&proposal, &setup.note_id(2));
    assert_eq!(
        (email.use_.as_str(), email.event_id.as_deref(), email.edge_of.as_deref(), email.decided_by.as_deref()),
        ("annotates", Some("e1"), Some("e1"), Some("user"))
    );
    let email_event = event(&proposal, "e1");
    assert_eq!((email_event.start, email_event.description.as_deref()), (Some(time_at("09:05")), Some("Notes:\n- 09:05 email")));
}

#[test]
fn an_edge_note_added_to_another_event_still_sets_its_edge() {
    let setup = Setup::standard();
    let p = setup.dry_run(setup.email_then_report()).proposal_id.unwrap();

    let proposal = notes(&setup, &p, 1, vec![annotate(&setup, Some("e2"))]).unwrap();

    let email = note(&proposal, &setup.note_id(2));
    assert_eq!((email.use_.as_str(), email.event_id.as_deref(), email.edge_of.as_deref()), ("annotates", Some("e2"), Some("e1")));
    assert_eq!(event(&proposal, "e1").start, Some(time_at("09:05")));
    assert_eq!(event(&proposal, "e2").description.as_deref(), Some("Notes:\n- 09:05 email"));
}

#[test]
fn as_planned_puts_a_note_back_as_claude_had_it() {
    let (setup, p) = ignoring_the_email_note();
    notes(&setup, &p, 1, vec![annotate(&setup, None)]).unwrap();

    let proposal = setup.try_amend(&p, 2, Amend { as_planned: vec![setup.note_id(2)], ..Default::default() }).unwrap();

    assert_eq!(note(&proposal, &setup.note_id(2)).use_, "ignored");
}

#[test]
fn claudes_revision_keeps_the_users_note_edits() {
    let (setup, p) = ignoring_the_email_note();
    notes(&setup, &p, 1, vec![annotate(&setup, None)]).unwrap();

    setup.try_dry_run(vec![], DryRun { ignore_notes: Some(vec![setup.note_id(2)]), ..DryRun::revising(&p, 2) }).unwrap();

    assert_eq!(note(&setup.get_proposal(), &setup.note_id(2)).use_, "annotates");
    assert_eq!(setup.prepare().proposal.unwrap().ignore_notes, vec![setup.note_id(2)]);
}

#[test]
fn a_note_edit_naming_a_note_or_event_that_isnt_there_is_refused() {
    let (setup, p) = ignoring_the_email_note();

    refused(notes(&setup, &p, 1, vec![note_edit(json!({"note_id": "2026-01-01T08:00:00+00:00#9", "use": "ignore"}))]), "aren't in the proposal");
    refused(notes(&setup, &p, 1, vec![annotate(&setup, Some("nope"))]), "aren't in the proposal");
    assert!(setup.journal().user_edits(&p).unwrap().is_empty());
}

#[test]
fn claude_adds_a_note_to_a_particular_event() {
    let setup = Setup::standard();

    let result = setup.try_dry_run(vec![], DryRun { annotate_notes: vec![(setup.note_id(2), "e2".into())], ..Default::default() }).unwrap();

    assert_eq!(change(&result, "e2").after.as_ref().unwrap().description.as_deref(), Some("Notes:\n- 09:05 email\n- 10:20 report"));
    let context = setup.prepare().proposal.unwrap();
    assert_eq!(context.annotate_notes, vec![json!({"note_id": setup.note_id(2), "event_id": "e2"})]);
    let proposal = setup.get_proposal();
    let email = note(&proposal, &setup.note_id(2));
    assert_eq!((email.use_.as_str(), email.decided_by.as_deref()), ("annotates", Some("claude")));
}

#[test]
fn claude_may_change_a_users_note_edit_answering_feedback_about_the_note() {
    let (setup, p) = ignoring_the_email_note();
    notes(&setup, &p, 1, vec![annotate(&setup, None)]).unwrap();
    setup.set_now("11:31");
    let item = setup.call(|c| c.add_note(&p, "put this with the report", None, None, Some(setup.note_id(2)))).unwrap();
    setup.set_now("11:32");

    setup
        .try_dry_run(
            vec![],
            DryRun {
                annotate_notes: vec![(setup.note_id(2), "e2".into())],
                replies: vec![FeedbackReply { feedback_id: item.id.clone(), reply: "Moved it to the report".into() }],
                ..DryRun::revising(&p, 2)
            },
        )
        .unwrap();

    let proposal = setup.get_proposal();
    assert_eq!(note(&proposal, &setup.note_id(2)).event_id.as_deref(), Some("e2"));
    let statuses: Vec<&str> = proposal.user_edits.iter().map(|e| e.status.as_str()).collect();
    assert_eq!(statuses, vec!["replaced"]);
    assert_eq!(proposal.feedback[0].note_id, Some(setup.note_id(2)));
}

#[test]
fn confirming_writes_the_note_where_the_user_put_it() {
    let (setup, p) = ignoring_the_email_note();
    notes(&setup, &p, 1, vec![annotate(&setup, Some("e2"))]).unwrap();

    assert_eq!(setup.call(|c| c.confirm(&p, 2)).unwrap().status, "applied");

    assert_eq!(setup.patch("e2")["description"], "Notes:\n- 09:05 email\n- 10:20 report");
}

// -- settling additions -------------------------------------------------------------------
// The user settles what a proposal adds -- create it now, say it's one
// already there, or drop it -- without confirming the rest.

struct Settling {
    setup: Setup,
    p: String,
    key: String,
    decisions: Vec<EventDecision>,
}

fn new_additions() -> Additions {
    Additions::new(
        &[json!({"ref": "new:coffee", "name": "Drink coffee"})],
        &[json!({"ref": "new:alex", "name": "Alex", "context": "Sam's friend"})],
        &[json!({"ref": "new:cafe", "name": "Corner cafe", "hint": "the cafe on Main"})],
    )
}

fn settling() -> Settling {
    let setup = Setup::with(&[], None, "11:30", Stores { people: vec![json!({"id": "sam", "name": "Sam", "status": "active"})], ..Default::default() });
    setup.append_note("09:00", None);
    setup.append_note("09:30", None);
    let mut decisions = setup.coffee_instead_of_email();
    decisions[0].action_ids = Some(strings(&["new:coffee"]));
    decisions[0].facts = Some(facts(json!({"location": "new:cafe", "with": ["sam", "new:alex"], "notes": {"sam": "tired", "new:alex": "new in town"}})));
    let result = setup.try_dry_run(decisions.clone(), DryRun { additions: new_additions(), ..Default::default() }).unwrap();
    let key = result.changes.iter().find(|c| c.action == "create").unwrap().key.clone().unwrap();
    Settling { setup, p: result.proposal_id.unwrap(), key, decisions }
}

impl Settling {
    fn settle(&self, revision: i64, choice: Value) -> anyhow::Result<Proposal> {
        self.setup.try_amend(&self.p, revision, Amend { additions: vec![AdditionChoice::from_value(&choice)], ..Default::default() })
    }

    fn coffee<'a>(&self, proposal: &'a Proposal) -> &'a ProposalEvent {
        event(proposal, &self.key)
    }

    fn person_id(&self, name: &str) -> String {
        self.setup.compactor.people.as_ref().unwrap().get_person(name).unwrap()["id"].as_str().unwrap().to_string()
    }

    fn people_names(&self) -> Vec<String> {
        self.setup.compactor.people.as_ref().unwrap().all().unwrap().iter().map(|p| p["name"].as_str().unwrap().to_string()).collect()
    }
}

fn settled(proposal: &Proposal) -> Vec<Value> {
    proposal.settled_additions.iter().map(|s| s.to_json()).collect()
}

fn has(additions: &Option<Map<String, Value>>, kind: &str) -> bool {
    additions.as_ref().is_some_and(|a| a.contains_key(kind))
}

#[test]
fn creating_a_person_now_names_them_by_id_from_then_on() {
    let s = settling();

    let proposal = s.settle(1, json!({"ref": "new:alex", "use": "create", "context": "Sam's college friend"})).unwrap();

    let alex = s.setup.compactor.people.as_ref().unwrap().get_person("alex").unwrap();
    assert_eq!((alex["context"].as_str(), alex["status"].as_str()), (Some("Sam's college friend"), Some("active")));
    let id = alex["id"].as_str().unwrap().to_string();
    assert_eq!(settled(&proposal), vec![json!({"ref": "new:alex", "use": "create", "id": id})]);
    assert!(!has(&proposal.additions, "people"));
    let facts = s.coffee(&proposal).facts.clone().unwrap();
    assert_eq!(facts.with(), vec!["sam".to_string(), id.clone()]);
    assert_eq!(facts.notes, Some(json!({"sam": "tired", id: "new in town"})));
    assert!(s.setup.creates().is_empty());
}

#[test]
fn one_already_there_is_named_by_its_id() {
    let s = settling();

    let proposal = s.settle(1, json!({"ref": "new:alex", "use": "existing", "id": "sam"})).unwrap();

    let facts = s.coffee(&proposal).facts.clone().unwrap();
    // Sam was there already, and keeps their own note.
    assert_eq!((facts.with(), facts.notes), (strings(&["sam"]), Some(json!({"sam": "tired"}))));
    assert!(!s.people_names().contains(&"Alex".to_string()));
}

#[test]
fn a_dropped_one_is_left_out_of_the_events() {
    let s = settling();

    let proposal = s.settle(1, json!({"ref": "new:cafe", "use": "drop"})).unwrap();

    assert!(s.coffee(&proposal).facts.as_ref().unwrap().location_id.as_ref().is_none_or(Value::is_null));
    assert!(!has(&proposal.additions, "locations"));
    assert_eq!(settled(&proposal), vec![json!({"ref": "new:cafe", "use": "drop", "id": null})]);
}

#[test]
fn an_action_approved_now_is_created_active() {
    let s = settling();

    let proposal = s.settle(1, json!({"ref": "new:coffee", "use": "create"})).unwrap();

    let all = s.setup.compactor.actions.as_ref().unwrap().all().unwrap();
    assert_eq!(all.len(), 1);
    assert_eq!((all[0].name.as_deref(), all[0].status), (Some("Drink coffee"), Some(crate::stores::actions::ActionStatus::Active)));
    assert_eq!(s.coffee(&proposal).action_ids, Some(vec![all[0].id.clone().unwrap()]));
}

#[test]
fn claudes_revision_keeps_what_the_user_settled() {
    let s = settling();
    s.settle(1, json!({"ref": "new:alex", "use": "create"})).unwrap();
    let alex = s.person_id("alex");

    let context = s.setup.prepare().proposal.unwrap();
    let refs: Vec<&str> = context.new_people.iter().map(|n| n["ref"].as_str().unwrap()).collect();
    assert_eq!(refs, vec!["new:alex"]);
    let settled: Vec<Value> = context.settled_additions.iter().map(|a| a.to_json()).collect();
    assert_eq!(settled, vec![json!({"ref": "new:alex", "use": "create", "id": alex})]);
    // Claude sends it all again: Alex isn't refused as already there.
    let revised = s.setup.try_dry_run(s.decisions.clone(), DryRun { additions: new_additions(), ..DryRun::revising(&s.p, 2) }).unwrap();

    let created = revised.changes.iter().find(|c| c.action == "create").unwrap();
    assert_eq!(created.after.as_ref().unwrap().facts.as_ref().unwrap()["with"], json!(["sam", alex]));
    assert!(!has(&revised.additions, "people"));
}

#[test]
fn confirming_creates_only_whats_left() {
    let s = settling();
    s.settle(1, json!({"ref": "new:alex", "use": "create"})).unwrap();

    assert_eq!(s.setup.call(|c| c.confirm(&s.p, 2)).unwrap().status, "applied");

    assert_eq!(s.people_names().iter().filter(|n| *n == "Alex").count(), 1);
    let actions: Vec<String> = s.setup.compactor.actions.as_ref().unwrap().all().unwrap().iter().map(|a| a.name.as_deref().unwrap().to_string()).collect();
    assert_eq!(actions, strings(&["Drink coffee"]));
    let id = s.setup.creates().last().unwrap()["id"].as_str().unwrap().to_string();
    assert_eq!(s.setup.event(&id).facts.unwrap().with(), vec!["sam".to_string(), s.person_id("alex")]);
}

#[test]
fn as_planned_on_a_ref_leaves_it_unsettled() {
    let s = settling();
    s.settle(1, json!({"ref": "new:cafe", "use": "drop"})).unwrap();

    let proposal = s.setup.try_amend(&s.p, 2, Amend { as_planned: strings(&["new:cafe"]), ..Default::default() }).unwrap();

    assert!(proposal.settled_additions.is_empty());
    let refs: Vec<&str> = proposal.additions.as_ref().unwrap()["locations"].as_array().unwrap().iter().map(|l| l["ref"].as_str().unwrap()).collect();
    assert_eq!(refs, vec!["new:cafe"]);
    assert_eq!(s.coffee(&proposal).facts.as_ref().unwrap().location_id, Some(json!("new:cafe")));
}

#[test]
fn a_choice_that_cant_be_made_is_refused() {
    let cases = [
        (json!({"ref": "new:nobody", "use": "drop"}), "isn't something the proposal adds"),
        (json!({"ref": "new:alex", "use": "existing", "id": "nope"}), "there's no person 'nope'"),
        (json!({"ref": "new:alex", "use": "create", "id": "sam"}), "only 'existing' takes an id"),
    ];
    let s = settling();

    for (choice, message) in cases {
        refused(s.settle(1, choice), message);
    }

    assert!(s.setup.journal().user_edits(&s.p).unwrap().is_empty());
}

#[test]
fn nothing_is_created_for_an_amend_thats_refused() {
    let s = settling();

    assert!(s
        .setup
        .try_amend(
            &s.p,
            1,
            Amend {
                decisions: vec![EventDecision { summary: Some("x".into()), ..keep("nope") }],
                additions: vec![AdditionChoice::from_value(&json!({"ref": "new:alex", "use": "create"}))],
                ..Default::default()
            },
        )
        .is_err());

    assert_eq!(s.people_names(), strings(&["Me", "Sam"]));
}
