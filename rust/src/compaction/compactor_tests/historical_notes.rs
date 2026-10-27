//! Historical notes: a note written for a time an earlier compaction already
//! settled revisits the event it falls within -- what happened at it, not
//! when -- and that event is judged again.

use super::*;
use crate::testing::facts;

fn sam() -> Vec<Value> {
    vec![json!({"id": "sam", "name": "Sam", "status": "active"})]
}

fn heard() -> Value {
    json!({
        "id": "heard", "name": "Heard", "status": "active", "definition": "Listen.",
        "parts": [{"kind": "judgment", "rubric": "Were they heard?", "ratings": {"0": "no", "1": "yes"}, "facts": ["general_notes"]}],
    })
}

fn judgment(request_id: &str, rating: i64) -> Judgment {
    Judgment { request_id: request_id.into(), rating, reasoning: "they talked it through".into() }
}

/// The morning compacted at 11:30 -- the email was with Sam -- and judged.
/// Then, at 13:00, a note for 09:30, during the email.
fn historical_note_on_the_email() -> Setup {
    let setup = Setup::with(&[("09:05", "email")], None, "11:30", Stores { people: sam(), traits: Some(vec![heard()]), ..Default::default() });
    let id = setup.dry_run(vec![EventDecision { facts: Some(facts(json!({"with": ["sam"]}))), ..keep("e1") }]).compaction_id.unwrap();
    setup.commit(&id);
    let judged = [judgment("e1/self/heard/judgment", 1), judgment("e1/sam/heard/judgment", 0)];
    setup.call(|c| c.record_judgments(&id, &judged)).unwrap();
    setup.append_note("09:30", Some("Sam was worried about his move"));
    setup.set_now("13:00");
    setup.t.fake.clear_requests();
    setup
}

fn historical_note_id(setup: &Setup) -> String {
    setup.notes(false).into_iter().find(|n| n.note.timestamp == time_at("09:30")).unwrap().id()
}

fn worried() -> Vec<EventDecision> {
    vec![EventDecision { facts: Some(facts(json!({"with": ["sam"], "notes": {"sam": "worried about his move"}}))), ..keep("e1") }]
}

#[test]
fn a_historical_note_revisits_the_settled_event_it_falls_within() {
    let setup = historical_note_on_the_email();

    let context = setup.prepare();

    // The span still starts at the last compaction; the email comes first,
    // revisited, settled whole.
    assert_eq!(context.compaction_window_start, Some(time_at("11:30")));
    assert_eq!(event_ids(&context), strings(&["e1", "e3"]));
    let email = &context.events[0];
    assert!(email.revisited);
    assert_eq!(email.history_until, Some(time_at("10:00")));
    assert!(!context.events[1].revisited);
    assert_eq!(ids(&context.notes, |n| n.id.as_str()), vec![historical_note_id(&setup)]);
    assert_eq!(context.notes[0].candidates[0], "e1");
    assert_eq!(context.to_json()["events"][0]["revisited"], json!(true));
    assert!(context.to_json()["events"][1].get("revisited").is_none());
}

#[test]
fn the_timeline_shows_what_it_revisits_first_under_its_date() {
    let setup = historical_note_on_the_email();

    let text = setup.prepare().timeline.unwrap().text;

    let revisited = text.find("━━ Thu 01 Jan · revisited ").unwrap_or_else(|| panic!("{text}"));
    let span = text.find("━━ Thu 01 Jan ━").unwrap_or_else(|| panic!("{text}"));
    assert!(revisited < span, "{text}");
    let earlier = &text[revisited..span];
    assert!(earlier.contains("Email") && earlier.contains("Sam was worried"), "{text}");
    assert!(!earlier.contains("now ┄"), "{text}");
    assert!(text[span..].contains("Lunch"), "{text}");
}

#[test]
fn a_note_long_after_the_fact_doesnt_move_the_span() {
    // Before, the span started at the oldest note, so one from ten days
    // back took the round there.
    let setup = historical_note_on_the_email();
    let long_ago = time_at("09:30") - Duration::days(10);
    let mut old = event_at("09:00-10:00").id("old1").summary("Walk").build();
    old.start = Some(time_at("09:00") - Duration::days(10));
    old.end = Some(time_at("10:00") - Duration::days(10));
    setup.t.seed_event(&old);
    let note = NotedTime { timestamp: long_ago, description: Some("long walk with Sam".into()), compaction_id: None };
    setup.call(|c| c.notes.append(&note, None).unwrap());

    let context = setup.prepare();

    assert_eq!(context.compaction_window_start, Some(time_at("11:30")));
    assert_eq!(context.now, Some(time_at("13:00")));
    assert_eq!(event_ids(&context), strings(&["old1", "e1", "e3"]));
    assert!(context.events[0].revisited && context.events[1].revisited);
}

#[test]
fn what_happened_at_it_changes_and_the_note_is_added_to_it() {
    let setup = historical_note_on_the_email();

    let planned = setup.dry_run(worried());

    let email = change(&planned, "e1");
    assert_eq!(email.action, "update");
    let after = email.after.as_ref().unwrap();
    assert_eq!((after.start, after.end), (Some(time_at("09:00")), Some(time_at("10:00"))));
    assert!(after.description.as_deref().unwrap().contains("09:30 Sam was worried about his move"), "{:?}", after.description);
    assert_eq!(after.facts.as_ref().unwrap()["notes"], json!({"sam": "worried about his move"}));
    let proposal = setup.get_proposal();
    let shown = proposal.events.unwrap().into_iter().find(|e| e.id == "e1").unwrap();
    assert!(shown.revisited);
    assert_eq!(shown.to_json()["revisited"], json!(true));
}

#[test]
fn its_times_dont_move_nor_is_it_cancelled_or_merged() {
    let setup = historical_note_on_the_email();
    let note = historical_note_id(&setup);

    refused(setup.try_dry_run(vec![EventDecision { end: Some(time_at("10:15")), ..keep("e1") }], DryRun::default()), "revisited for a historical note, so its times can't move");
    refused(setup.try_dry_run(vec![EventDecision { end_note: Some(note), ..keep("e1") }], DryRun::default()), "its times can't move");
    refused(
        setup.try_dry_run(
            vec![EventDecision { event_id: Some("e1".into()), counts_against_follow_through: Some(false), ..EventDecision::new("cancel") }],
            DryRun::default(),
        ),
        "revisited for a historical note, so it can't be cancelled",
    );
    refused(
        setup.try_dry_run(vec![EventDecision { event_id: Some("e3".into()), into: Some("e1".into()), ..EventDecision::new("merge") }], DryRun::default()),
        "e1 is revisited for a historical note, so it can't be merged",
    );
}

#[test]
fn applying_it_stamps_the_note_and_judges_the_event_again() {
    let setup = historical_note_on_the_email();
    let note = historical_note_id(&setup);
    let proposed = setup.dry_run(worried());
    let id = proposed.compaction_id.unwrap();

    let applied = setup.commit(&id);

    let email = setup.event("e1");
    assert!(email.description.as_deref().unwrap().contains("Sam was worried about his move"));
    assert_eq!(email.judgments, None);
    assert!(setup.notes(false).iter().all(|n| n.id() != note));
    assert_eq!(setup.load(&id).revisits, strings(&["e1"]));
    let due = applied.judgments.unwrap();
    assert_eq!(due.events.iter().map(|e| e.event_id.as_str()).collect::<Vec<_>>(), vec!["e1"]);
    assert_eq!(due.events[0].people, vec![("self".to_string(), strings(&["heard/judgment"])), ("sam".to_string(), strings(&["heard/judgment"]))]);
}

#[test]
fn only_the_revisited_events_judgments_go() {
    // Lunch, in this round's window, is judged as it always was: it had no
    // judgments to keep.
    let setup = historical_note_on_the_email();
    let decisions = vec![worried().remove(0), EventDecision { facts: Some(facts(json!({"with": ["sam"]}))), ..keep("e3") }];
    let id = setup.dry_run(decisions).compaction_id.unwrap();

    setup.commit(&id);

    let cleared: Vec<String> = setup
        .patches()
        .into_iter()
        .filter(|(_, body)| body["extendedProperties"]["private"].as_object().is_some_and(|p| p.keys().any(|k| k.contains("judgments"))))
        .map(|(id, _)| id)
        .collect();
    assert_eq!(cleared, strings(&["e1"]));
}

#[test]
fn a_historical_note_that_falls_within_no_event_is_just_stamped() {
    // 11:00 to 12:00 has nothing planned, and the morning's settled.
    let setup = historical_note_on_the_email();
    setup.append_note("11:10", Some("stretched"));

    let planned = setup.dry_run(worried());

    assert!(planned.warnings.iter().any(|w| w.contains("doesn't fall within any event")), "{:?}", planned.warnings);
    let id = planned.compaction_id.unwrap();
    setup.commit(&id);
    assert!(setup.notes(false).is_empty());
}
