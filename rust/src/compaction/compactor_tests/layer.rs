//! Calendar writes while a proposal is open: checked against the calendar
//! with its layer on top, and those the layer bears on kept with it for
//! the abandon dialog (see compaction::layer).

use super::*;
use crate::compaction::proposals::CalendarEdit;

/// The event tool `name`'s result, or its error's text, called as a
/// client calls it.
fn tool(name: &str, setup: &Setup, arguments: Value) -> Result<Value, String> {
    let mut timings = Vec::new();
    let result = crate::mcp::call_tool(&setup.t.app, &json!({"name": name, "arguments": arguments}), &mut timings);
    if result["isError"] == true {
        return Err(result["content"][0]["text"].as_str().unwrap_or_default().to_string());
    }
    Ok(result["structuredContent"].clone())
}

fn when(time: &str) -> String {
    py::isoformat(&time_at(time))
}

fn create(setup: &Setup, summary: &str, span: (&str, &str)) -> Result<Value, String> {
    tool("create_event", setup, json!({"events": [{"summary": summary, "start": when(span.0), "end": when(span.1)}]}))
}

fn calendar_edits(setup: &Setup) -> Vec<CalendarEdit> {
    setup.get_proposal().calendar_edits
}

/// A morning of work that runs on past now (11:30): Claude ends it at
/// 11:00 from the note, freeing the rest of it -- the calendar still has
/// it to 12:30 until it's confirmed.
fn work_ended_early() -> (Setup, String) {
    let events = vec![
        event_at("09:00-10:00").id("e1").summary("Email").priority(2).build(),
        event_at("10:00-12:30").id("w1").summary("Work").priority(2).build(),
        event_at("12:30-13:30").id("e3").summary("Lunch").priority(1).build(),
        event_at("20:00-07:00+1").id("s1").summary("Sleep").priority(0).build(),
    ];
    let setup = Setup::new(&[("09:05", "email"), ("11:00", "work done")], Some(events), "11:30");
    let ended = EventDecision { end_note: Some(setup.note_id(3)), ..keep("w1") };
    let p = setup.dry_run(vec![ended]).proposal_id.unwrap();
    (setup, p)
}

// -- the overlap check sees the layer ------------------------------------------------

#[test]
fn a_write_under_the_tail_of_the_layer_is_refused() {
    let setup = Setup::standard();
    let p = setup.dry_run(setup.email_then_report()).proposal_id.unwrap();
    // The user ran the report on past through, to 12:30.
    let proposal = setup.amend(&p, 1, vec![EventDecision { end: Some(time_at("12:30")), ..keep("e2") }]);
    assert_eq!((proposal.through, proposal.layer_end), (time_at("11:30"), time_at("12:30")));

    let refusal = create(&setup, "Call", ("11:45", "12:15")).unwrap_err();

    assert!(refusal.contains("lands under the open compaction proposal: 'Report' runs until 12:30 there"), "{refusal}");
    assert!(refusal.contains("End it earlier in the proposal first"), "{refusal}");
    assert!(setup.creates().is_empty());
    // Past the layer's end, the calendar is as it was.
    create(&setup, "Call", ("13:00", "13:30")).unwrap();
    assert!(calendar_edits(&setup).is_empty());
}

#[test]
fn a_write_into_time_the_layer_freed_is_allowed_and_kept() {
    let (setup, _p) = work_ended_early();
    // Without the layer, the calendar's Work would be in the way.
    let made = create(&setup, "Call", ("11:45", "12:15")).unwrap();

    let call = made["events"][0]["id"].as_str().unwrap().to_string();
    let edits = calendar_edits(&setup);
    assert_eq!(edits.len(), 1);
    let edit = &edits[0];
    assert_eq!((edit.event.event_id.as_str(), edit.event.summary.as_deref(), edit.kind.as_str()), (call.as_str(), Some("Call"), "freed"));
    assert_eq!((edit.event.start, edit.event.end), (time_at("11:45"), time_at("12:15")));
    let overlaps = edit.overlaps.as_ref().unwrap();
    assert_eq!((overlaps.event_id.as_str(), overlaps.summary.as_deref()), ("w1", Some("Work")));
    assert_eq!((overlaps.start, overlaps.end), (time_at("10:00"), time_at("12:30")));
    let shown = setup.call(|c| c.get_proposal(None, None)).unwrap().to_json();
    assert_eq!(
        shown["calendar_edits"][0]["overlaps"],
        json!({"event_id": "w1", "summary": "Work", "start": py::pydantic_datetime(&time_at("10:00")), "end": py::pydantic_datetime(&time_at("12:30"))})
    );
    assert_eq!(shown["calendar_edits"][0]["kind"], "freed");
}

#[test]
fn what_overlaps_is_read_from_the_calendar_as_it_is_now() {
    let (setup, _p) = work_ended_early();
    let made = create(&setup, "Call", ("11:45", "12:15")).unwrap();
    let call = made["events"][0]["id"].as_str().unwrap().to_string();

    // Moved later by hand in Calendar, past Work.
    setup.change_event(Event { start: Some(time_at("13:30")), end: Some(time_at("14:00")), ..setup.event(&call) });

    let edit = &calendar_edits(&setup)[0];
    assert_eq!((edit.event.start, edit.overlaps.as_ref()), (time_at("13:30"), None));
}

#[test]
fn a_write_to_an_event_the_layer_covers_is_kept() {
    let setup = Setup::standard();
    let p = setup.dry_run(setup.email_then_report()).proposal_id.unwrap();
    // The report, run on to 13:00, covers lunch.
    let proposal = setup.amend(&p, 1, vec![EventDecision { end: Some(time_at("13:00")), ..keep("e2") }]);
    assert!(proposal.events.as_ref().unwrap().iter().any(|e| e.id == "e3" && e.covered_by.as_deref() == Some("e2")));
    let etag = setup.event("e3").etag.0.unwrap();

    // The user drags lunch out from under it, to later.
    tool("update_event", &setup, json!({"updates": [{"event": {"id": "e3", "start": when("14:00"), "end": when("15:00"), "etag": etag}}]})).unwrap();

    let edits = calendar_edits(&setup);
    assert_eq!(edits.len(), 1);
    assert_eq!((edits[0].event.event_id.as_str(), edits[0].kind.as_str(), edits[0].overlaps.as_ref()), ("e3", "covered", None));
    assert_eq!(edits[0].event.start, time_at("14:00"));
    // No longer under the layer, it's no longer covered.
    let proposal = setup.get_proposal();
    assert!(proposal.events.as_ref().unwrap().iter().all(|e| e.id != "e3" || e.covered_by.is_none()));
}

#[test]
fn a_write_the_layer_doesnt_bear_on_isnt_kept() {
    let (setup, _p) = work_ended_early();

    create(&setup, "Call", ("14:00", "14:30")).unwrap();

    assert!(calendar_edits(&setup).is_empty());
}

// -- abandoning ---------------------------------------------------------------------------

#[test]
fn abandoning_writes_nothing_and_leaves_the_overlap() {
    let (setup, p) = work_ended_early();
    let made = create(&setup, "Call", ("11:45", "12:15")).unwrap();
    let call = made["events"][0]["id"].as_str().unwrap().to_string();
    setup.t.fake.clear_requests();

    setup.call(|c| c.abandon(&p)).unwrap();

    assert!(setup.calendar_writes().is_empty());
    let (work, call) = (setup.event("w1"), setup.event(&call));
    assert_eq!((work.start, work.end), (Some(time_at("10:00")), Some(time_at("12:30"))));
    assert_eq!((call.start, call.end), (Some(time_at("11:45")), Some(time_at("12:15"))));
    // With no proposal open, writes see the calendar as it is.
    assert!(create(&setup, "Walk", ("12:00", "12:10")).unwrap_err().contains("overlaps"));
}

#[test]
fn the_next_round_copes_with_the_overlap_left() {
    let (setup, p) = work_ended_early();
    create(&setup, "Call", ("11:45", "12:15")).unwrap();
    setup.call(|c| c.abandon(&p)).unwrap();
    setup.set_now("12:45");

    // Nothing decided: the two overlapping events are as planned.
    let planned = setup.dry_run(vec![]);
    let proposal = setup.get_proposal();
    let ids: Vec<&str> = proposal.events.as_ref().unwrap().iter().map(|e| e.id.as_str()).collect();
    assert!(ids.contains(&"w1"), "{ids:?}");
    assert!(proposal.problem.is_none());

    // Deciding one of them: the work ended at 11:00, the call is as planned.
    let ended = EventDecision { end_note: Some(setup.note_id(3)), ..keep("w1") };
    let revised = setup.try_dry_run(vec![ended], DryRun::revising(planned.proposal_id.as_ref().unwrap(), 1)).unwrap();
    assert_eq!(change(&revised, "w1").after.as_ref().unwrap().end, Some(time_at("11:00")));
    let id = revised.proposal_id.unwrap();
    assert_eq!(setup.call(|c| c.confirm(&id, 2)).unwrap().status, "applied");
    assert_eq!(setup.event("w1").end, Some(time_at("11:00")));
}

// -- the events listed end with the layer ------------------------------------------------

fn listed(proposal: &Proposal) -> Vec<(String, String)> {
    proposal.events.as_ref().unwrap().iter().map(|e| (e.id.clone(), e.status.clone())).collect()
}

#[test]
fn a_planned_event_past_the_layers_end_isnt_listed() {
    let setup = Setup::standard();
    setup.dry_run(setup.email_then_report());

    let proposal = setup.get_proposal();

    let ids: Vec<String> = listed(&proposal).into_iter().map(|(id, _)| id).collect();
    // Lunch and Sleep, untouched after it, are drawn from the calendar.
    assert_eq!(proposal.layer_end, time_at("11:30"));
    assert!(!ids.iter().any(|id| id == "e3" || id == "s1"), "{ids:?}");
    // The report still going on at through is listed.
    assert!(ids.iter().any(|id| id == "e2"), "{ids:?}");
}

#[test]
fn an_event_the_layer_cuts_past_through_is_still_listed() {
    let setup = Setup::standard();
    let p = setup.dry_run(setup.email_then_report()).proposal_id.unwrap();

    // The user ran the report on to 12:30, cutting lunch.
    let proposal = setup.amend(&p, 1, vec![EventDecision { end: Some(time_at("12:30")), ..keep("e2") }]);

    let lunch = proposal.events.as_ref().unwrap().iter().find(|e| e.id == "e3").unwrap();
    assert_eq!((lunch.status.as_str(), lunch.cut_by.as_deref(), lunch.start), ("adjusted", Some("e2"), Some(time_at("12:30"))));
    assert_eq!(lunch.planned_start, Some(time_at("12:00")));
    // Sleep, past the layer's end and untouched, isn't.
    assert!(!listed(&proposal).iter().any(|(id, _)| id == "s1"), "{:?}", listed(&proposal));
}

#[test]
fn an_event_the_layer_covers_past_through_is_still_listed() {
    let setup = Setup::standard();
    let p = setup.dry_run(setup.email_then_report()).proposal_id.unwrap();

    // The report, run on to 13:00, covers lunch.
    let proposal = setup.amend(&p, 1, vec![EventDecision { end: Some(time_at("13:00")), ..keep("e2") }]);

    assert_eq!(proposal.layer_end, time_at("13:00"));
    let lunch = proposal.events.as_ref().unwrap().iter().find(|e| e.id == "e3").unwrap();
    assert_eq!((lunch.status.as_str(), lunch.covered_by.as_deref()), ("cancelled", Some("e2")));
    assert!(!listed(&proposal).iter().any(|(id, _)| id == "s1"), "{:?}", listed(&proposal));
}

#[test]
fn an_amend_over_an_event_the_proposal_decides_is_refused_in_the_users_words() {
    let setup = Setup::standard();
    let p = setup.dry_run(setup.email_then_report()).proposal_id.unwrap();
    // Claude decided both: the user runs the email on over the report.
    let refusal = setup
        .try_amend(&p, 1, Amend { decisions: vec![EventDecision { end: Some(time_at("11:00")), ..keep("e1") }], ..Default::default() })
        .unwrap_err()
        .to_string();

    assert!(refusal.starts_with("Nothing was changed: this edit would overlap an event the proposal already decides."), "{refusal}");
    assert!(!refusal.contains("Resend the whole batch"), "{refusal}");
    assert!(refusal.contains("overlaps 'Report'"), "{refusal}");
}

// -- an edit keeps the edges it didn't move where the layer had them ----------------

fn span_of(proposal: &Proposal, id: &str) -> (Option<Moment>, Option<Moment>, Option<String>) {
    let e = proposal.events.as_ref().unwrap().iter().find(|e| e.id == id).unwrap();
    (e.start, e.end, e.decided_by.clone())
}

#[test]
fn an_end_edit_of_a_cut_event_keeps_its_cut_start() {
    let setup = Setup::standard();
    let p = setup.dry_run(setup.email_then_report()).proposal_id.unwrap();
    // The report run on to 12:30 cuts lunch to start then.
    setup.amend(&p, 1, vec![EventDecision { end: Some(time_at("12:30")), ..keep("e2") }]);

    // Lunch's end dragged later: its start is still where the layer cut it.
    let proposal = setup.amend(&p, 2, vec![EventDecision { end: Some(time_at("13:30")), ..keep("e3") }]);

    assert_eq!(span_of(&proposal, "e3"), (Some(time_at("12:30")), Some(time_at("13:30")), Some("user".into())));
}

#[test]
fn an_edge_left_free_follows_what_cut_it() {
    let setup = Setup::standard();
    let p = setup.dry_run(setup.email_then_report()).proposal_id.unwrap();
    setup.amend(&p, 1, vec![EventDecision { end: Some(time_at("12:30")), ..keep("e2") }]);
    setup.amend(&p, 2, vec![EventDecision { end: Some(time_at("13:30")), ..keep("e3") }]);

    // The report run on further: lunch's start, never set, follows it.
    let proposal = setup.amend(&p, 3, vec![EventDecision { end: Some(time_at("12:45")), ..keep("e2") }]);

    assert_eq!(span_of(&proposal, "e3"), (Some(time_at("12:45")), Some(time_at("13:30")), Some("user".into())));
}

#[test]
fn an_edge_set_by_the_edit_still_overlaps_and_is_refused() {
    let setup = Setup::standard();
    let p = setup.dry_run(setup.email_then_report()).proposal_id.unwrap();
    setup.amend(&p, 1, vec![EventDecision { end: Some(time_at("12:30")), ..keep("e2") }]);

    // Lunch's start set under the report: that's the user's, and overlaps.
    let refusal = setup
        .try_amend(&p, 2, Amend { decisions: vec![EventDecision { start: Some(time_at("12:15")), ..keep("e3") }], ..Default::default() })
        .unwrap_err()
        .to_string();

    assert!(refusal.contains("overlaps"), "{refusal}");
}

#[test]
fn confirming_writes_the_cut_start_an_end_edit_kept() {
    let setup = Setup::standard();
    let p = setup.dry_run(setup.email_then_report()).proposal_id.unwrap();
    setup.amend(&p, 1, vec![EventDecision { end: Some(time_at("12:30")), ..keep("e2") }]);
    setup.amend(&p, 2, vec![EventDecision { end: Some(time_at("13:30")), ..keep("e3") }]);

    setup.call(|c| c.confirm(&p, 3)).unwrap();

    let lunch = setup.event("e3");
    assert_eq!((lunch.start, lunch.end), (Some(time_at("12:30")), Some(time_at("13:30"))));
}
