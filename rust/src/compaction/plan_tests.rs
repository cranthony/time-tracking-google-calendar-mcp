//! The planner's tests.

use super::*;
use crate::event::MAX_DESCRIPTION_BYTES;
use crate::stores::event_changes::RESEND_RULE;
use crate::testing::{event_at, facts, strings, time_at};
use chrono::Duration;
use serde_json::json;

fn day() -> Vec<Event> {
    vec![
        event_at("09:00-10:00").id("e1").summary("Email").priority(2).build(),
        event_at("10:00-11:00").id("e2").summary("Report").priority(2).build(),
        event_at("12:00-13:00").id("e3").summary("Lunch").priority(1).build(),
        event_at("14:00-15:00").id("e4").summary("Review").priority(3).build(),
        event_at("20:00-07:00+1").id("s1").summary("Sleep").priority(0).build(),
    ]
}

/// The salsa evening: notes only mark leaving early and finishing dinner.
fn evening() -> Vec<Event> {
    vec![
        event_at("17:00-18:15").id("w").summary("Work").priority(2).build(),
        event_at("18:30-19:30").id("salsa").summary("Google Salsa class").priority(2).build(),
        event_at("19:30-20:00").id("dinner").summary("Dinner").priority(2).build(),
        event_at("20:00-21:00").id("read").summary("Reading").priority(3).build(),
        event_at("22:00-07:00+1").id("s1").summary("Sleep").priority(0).build(),
    ]
}

/// Last night's sleep, then the next day.
fn overnight() -> Vec<Event> {
    vec![
        event_at("20:00-07:00+1").id("s0").summary("Sleep").priority(0).build(),
        event_at("07:00+1-08:00+1").id("gr").summary("Getting Ready").priority(2).build(),
        Event {
            end: Some(time_at("07:00+1") + Duration::days(1)),
            ..event_at("22:00+1-23:00+1").id("s1").summary("Sleep").priority(0).build()
        },
    ]
}

fn note(number: usize, at: &str, description: Option<&str>) -> PlanNote {
    PlanNote { id: format!("n{number}"), timestamp: time_at(at), description: description.map(str::to_string) }
}

fn keep(event_id: &str) -> EventDecision {
    EventDecision { event_id: Some(event_id.into()), ..EventDecision::new("keep") }
}

fn cancel(event_id: &str) -> EventDecision {
    EventDecision { event_id: Some(event_id.into()), ..EventDecision::new("cancel") }
}

fn create(summary: Option<&str>) -> EventDecision {
    EventDecision { summary: summary.map(str::to_string), ..EventDecision::new("create") }
}

#[derive(Default)]
struct Extra {
    ignore_notes: Vec<String>,
    names: Vec<(String, String)>,
    history_until: Option<Moment>,
    last_compaction: Option<Moment>,
    note_targets: Vec<(String, Option<String>)>,
}

fn plan_with(notes: &[PlanNote], decisions: &[EventDecision], events: &[Event], now: &str, extra: Extra) -> Result<CompactionPlan, ChangeError> {
    plan_compaction(
        notes,
        decisions,
        events,
        time_at(now),
        PlanOptions {
            ignore_notes: Some(&extra.ignore_notes),
            names: Some(&extra.names),
            history_until: extra.history_until,
            last_compaction: extra.last_compaction,
            note_targets: Some(&extra.note_targets),
            ..Default::default()
        },
    )
}

fn plan(notes: &[PlanNote], decisions: &[EventDecision]) -> CompactionPlan {
    plan_with(notes, decisions, &day(), "11:30", Extra::default()).unwrap()
}

fn refused(result: Result<CompactionPlan, ChangeError>) -> ChangeError {
    match result {
        Ok(_) => panic!("the plan wasn't refused"),
        Err(e) => e,
    }
}

fn refused_with(result: Result<CompactionPlan, ChangeError>, pattern: &str) -> ChangeError {
    let error = refused(result);
    let found = regex::Regex::new(pattern).unwrap().is_match(&error.message);
    assert!(found, "{pattern:?} not in {:?}", error.message);
    error
}

fn by_event(plan: &CompactionPlan, id: &str) -> Option<CompactionChange> {
    plan.changes.iter().rev().find(|c| c.event_id.as_deref() == Some(id)).cloned()
}

fn changed(plan: &CompactionPlan, id: &str) -> CompactionChange {
    by_event(plan, id).unwrap_or_else(|| panic!("no change to {id}"))
}

fn span(state: &EventState) -> (Moment, Moment) {
    (state.start.unwrap(), state.end.unwrap())
}

fn at(a: &str, b: &str) -> (Moment, Moment) {
    (time_at(a), time_at(b))
}

fn names(pairs: &[(&str, &str)]) -> Vec<(String, String)> {
    pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect()
}

fn targets(pairs: &[(&str, Option<&str>)]) -> Vec<(String, Option<String>)> {
    pairs.iter().map(|(k, v)| (k.to_string(), v.map(str::to_string))).collect()
}

fn uses(plan: &CompactionPlan) -> Vec<(String, (String, Option<String>))> {
    plan.note_uses.clone()
}

fn using(pairs: &[(&str, &str, Option<&str>)]) -> Vec<(String, (String, Option<String>))> {
    pairs.iter().map(|(n, u, e)| (n.to_string(), (u.to_string(), e.map(str::to_string)))).collect()
}

fn creates(plan: &CompactionPlan) -> Vec<CompactionChange> {
    plan.changes.iter().filter(|c| c.action == "create").cloned().collect()
}

fn timeline_text(plan: &CompactionPlan) -> String {
    plan.timeline.as_ref().unwrap().text.clone()
}

// -- silence means on schedule ----------------------------------------------------

#[test]
fn past_events_nobody_mentions_are_left_where_they_were_planned() {
    let plan = plan(&[], &[]);
    assert!(plan.changes.is_empty());
    assert_eq!(plan.warnings, strings(&["nothing on the calendar needs to change"]));
}

#[test]
fn an_event_still_in_progress_is_left_free_to_run_on() {
    let plan = plan_with(&[], &[], &day(), "10:30", Extra::default()).unwrap();
    assert!(plan.changes.iter().all(|c| c.event_id.is_none()));
}

#[test]
fn an_end_note_for_one_event_does_not_merge_it_with_the_one_before() {
    let plan = plan_with(
        &[note(1, "18:15", Some("Leaving for salsa early to prep")), note(2, "20:10", Some("Done with dinner"))],
        &[
            EventDecision { start_note: Some("n1".into()), end: Some(time_at("18:30")), ..create(Some("Salsa prep")) },
            EventDecision { end_note: Some("n2".into()), ..keep("dinner") },
            EventDecision { start_note: Some("n2".into()), ..keep("read") },
        ],
        &evening(),
        "22:30",
        Extra::default(),
    )
    .unwrap();
    assert!(by_event(&plan, "salsa").is_none());
    assert_eq!(span(changed(&plan, "dinner").after.as_ref().unwrap()), at("19:30", "20:10"));
    assert_eq!(span(changed(&plan, "read").after.as_ref().unwrap()), at("20:10", "21:00"));
    assert!(!plan.changes.iter().any(|c| c.action == "cancel"));
    let made = creates(&plan);
    assert_eq!(made.len(), 1);
    assert_eq!(span(made[0].after.as_ref().unwrap()), at("18:15", "18:30"));
}

// -- compacted --------------------------------------------------------------------

#[test]
fn an_earlier_compactions_event_may_run_on() {
    let plan = plan_with(
        &[note(1, "11:10", None)],
        &[EventDecision { end_note: Some("n1".into()), ..keep("e2") }],
        &day(),
        "11:30",
        Extra { history_until: Some(time_at("10:30")), ..Default::default() },
    )
    .unwrap();
    assert_eq!(span(changed(&plan, "e2").after.as_ref().unwrap()), at("10:00", "11:10"));
}

fn history_decisions(allow: Option<bool>) -> Vec<(EventDecision, &'static str)> {
    vec![
        (EventDecision { end: Some(time_at("10:20")), allow_history: allow, ..keep("e2") }, "going on until"),
        (EventDecision { start: Some(time_at("10:05")), allow_history: allow, ..keep("e2") }, "its start can't move"),
        (EventDecision { allow_history: allow, ..cancel("e2") }, "can't be cancelled"),
        (EventDecision { allow_history: allow, ..cancel("e1") }, "can't be cancelled"),
    ]
}

#[test]
fn but_keeps_what_it_settled() {
    for (decision, message) in history_decisions(None) {
        let error = refused_with(
            plan_with(&[], &[decision], &day(), "11:30", Extra { history_until: Some(time_at("10:30")), ..Default::default() }),
            message,
        );
        assert_eq!(error.categories, strings(&["compacted"]));
    }
}

#[test]
fn unless_the_user_approved_changing_it() {
    for (decision, _) in history_decisions(Some(true)) {
        let id = decision.event_id.clone().unwrap();
        let plan =
            plan_with(&[], &[decision], &day(), "11:30", Extra { history_until: Some(time_at("10:30")), ..Default::default() }).unwrap();
        assert!(by_event(&plan, &id).is_some());
    }
}

// -- keep -------------------------------------------------------------------------

#[test]
fn notes_set_the_edges_they_mark() {
    let plan = plan(
        &[note(1, "09:05", None), note(2, "10:20", None)],
        &[
            EventDecision { start_note: Some("n1".into()), end_note: Some("n2".into()), ..keep("e1") },
            EventDecision { start_note: Some("n2".into()), ..keep("e2") },
        ],
    );
    let email = changed(&plan, "e1");
    assert_eq!(span(email.after.as_ref().unwrap()), at("09:05", "10:20"));
    assert!(email.reason.contains("realigned"));
    assert_eq!(span(changed(&plan, "e2").after.as_ref().unwrap()), at("10:20", "11:00"));
    assert!(by_event(&plan, "e3").is_none() && by_event(&plan, "e4").is_none());
}

#[test]
fn an_explicit_time_wins_over_its_note() {
    let plan = plan(
        &[note(1, "08:30", Some("leaving in 15"))],
        &[EventDecision { start: Some(time_at("08:45")), start_note: Some("n1".into()), ..keep("e1") }],
    );
    assert_eq!(span(changed(&plan, "e1").after.as_ref().unwrap()), at("08:45", "10:00"));
    let email = plan.timeline.as_ref().unwrap().events.iter().find(|e| e.event_id.as_deref() == Some("e1")).unwrap().clone();
    assert_eq!(email.start_note.as_deref(), Some("n1"));
}

#[test]
fn renames_and_annotates() {
    let plan = plan(&[], &[EventDecision { summary: Some("Deep work".into()), annotate: Some("phone rang".into()), ..keep("e1") }]);
    let after = changed(&plan, "e1").after.unwrap();
    assert_eq!(after.summary.as_deref(), Some("Deep work"));
    assert_eq!(after.description.as_deref(), Some("Notes:\n- phone rang"));
}

#[test]
fn renaming_a_future_event_does_not_pin_it() {
    let after = changed(&plan(&[], &[EventDecision { summary: Some("Team lunch".into()), ..keep("e3") }]), "e3").after.unwrap();
    assert_eq!(after.summary.as_deref(), Some("Team lunch"));
    assert_eq!(span(&after), at("12:00", "13:00"));
}

#[test]
fn moving_a_future_event_reschedules_it_and_what_it_runs_into_must_move_too() {
    let lunch = EventDecision { start: Some(time_at("13:30")), end: Some(time_at("14:30")), ..keep("e3") };
    refused_with(
        plan_with(&[], &[lunch.clone()], &day(), "11:30", Extra::default()),
        &regex::escape("'Lunch' (13:30–14:30, as asked) overlaps 'Review' (14:00–15:00)"),
    );
    let plan = plan(&[], &[lunch, EventDecision { start: Some(time_at("14:30")), end: Some(time_at("15:30")), ..keep("e4") }]);
    let moved = changed(&plan, "e3");
    assert_eq!(span(moved.after.as_ref().unwrap()), at("13:30", "14:30"));
    assert!(moved.reason.contains("moved as requested"));
    assert_eq!(span(changed(&plan, "e4").after.as_ref().unwrap()), at("14:30", "15:30"));
}

#[test]
fn nothing_is_split_to_make_room() {
    let events = vec![
        event_at("12:00-14:00").id("work").summary("Work").priority(2).build(),
        event_at("20:00-07:00+1").id("s1").summary("Sleep").priority(0).build(),
    ];
    refused_with(
        plan_with(
            &[],
            &[EventDecision { start: Some(time_at("12:15")), end: Some(time_at("12:45")), ..create(Some("Lunch")) }],
            &events,
            "10:00",
            Extra::default(),
        ),
        &regex::escape("'Work' (12:00–14:00) overlaps 'Lunch'"),
    );
}

#[test]
fn does_not_mutate_its_inputs() {
    let events = day();
    let decisions = vec![EventDecision { summary: Some("Deep work".into()), end: Some(time_at("09:30")), ..keep("e1") }];
    plan_with(&[], &decisions, &events, "11:30", Extra::default()).unwrap();
    assert_eq!((events[0].summary.as_deref(), events[0].end), (Some("Email"), Some(time_at("10:00"))));
    assert_eq!(decisions[0].summary.as_deref(), Some("Deep work"));
}

// -- overlaps ---------------------------------------------------------------------

#[test]
fn an_overrun_into_a_past_event_is_rejected_naming_both() {
    let error = refused(plan_with(&[note(1, "10:20", None)], &[EventDecision { end_note: Some("n1".into()), ..keep("e1") }], &day(), "11:30", Extra::default()));
    assert!(error.message.contains("- 'Email' (09:00–10:20, as asked) overlaps 'Report' (10:00–11:00)."));
    assert!(error.message.starts_with(RESEND_RULE));
    assert!(error.message.contains("  10:00–11:00  Report  e2"));
    assert_eq!(error.categories, strings(&["overlap"]));
}

#[test]
fn it_passes_once_the_decisions_say_which_gives_way() {
    let plan = plan(
        &[note(1, "10:20", None)],
        &[EventDecision { end_note: Some("n1".into()), ..keep("e1") }, EventDecision { start_note: Some("n1".into()), ..keep("e2") }],
    );
    assert_eq!(span(changed(&plan, "e2").after.as_ref().unwrap()), at("10:20", "11:00"));
}

#[test]
fn an_overrun_into_an_event_still_in_progress_is_an_overlap_to_resolve() {
    refused_with(
        plan_with(&[note(1, "10:20", None)], &[EventDecision { end_note: Some("n1".into()), ..keep("e1") }], &day(), "10:30", Extra::default()),
        &regex::escape("overlaps 'Report' (10:00–11:00)"),
    );
}

#[test]
fn a_new_event_over_a_past_one_is_rejected() {
    refused_with(
        plan_with(&[], &[EventDecision { start: Some(time_at("09:30")), end: Some(time_at("09:45")), ..create(Some("Coffee")) }], &day(), "11:30", Extra::default()),
        "'Coffee'.*overlaps 'Email'|'Email'.*overlaps 'Coffee'",
    );
}

#[test]
fn a_note_cannot_silently_eat_into_last_nights_sleep() {
    refused_with(
        plan_with(
            &[note(1, "06:40+1", None)],
            &[EventDecision { start_note: Some("n1".into()), ..keep("gr") }],
            &overnight(),
            "09:00+1",
            Extra::default(),
        ),
        &regex::escape("'Sleep' (20:00–07:00) overlaps 'Getting Ready'"),
    );
}

#[test]
fn waking_early_shortens_last_nights_sleep_when_asked() {
    let plan = plan_with(
        &[note(1, "06:40+1", None)],
        &[EventDecision { end_note: Some("n1".into()), ..keep("s0") }, EventDecision { start_note: Some("n1".into()), ..keep("gr") }],
        &overnight(),
        "09:00+1",
        Extra::default(),
    )
    .unwrap();
    assert_eq!(changed(&plan, "s0").after.unwrap().end, Some(time_at("06:40+1")));
    assert!(by_event(&plan, "s1").is_none());
    assert!(plan.warnings.is_empty());
}

// -- cancel, create and merge -----------------------------------------------------

#[test]
fn cancel_marks_it_as_not_having_happened() {
    let change = changed(&plan(&[], &[cancel("e2")]), "e2");
    assert_eq!(change.action, "cancel");
    assert!(change.reason.contains("didn't happen"));
}

#[test]
fn create_makes_a_new_event() {
    let plan = plan(
        &[note(1, "11:05", Some("coffee")), note(2, "11:20", None)],
        &[EventDecision { start_note: Some("n1".into()), end_note: Some("n2".into()), action_ids: Some(crate::testing::shares(&["g1"])), ..create(Some("Coffee")) }],
    );
    let made = creates(&plan);
    assert_eq!(made.len(), 1);
    let after = made[0].after.clone().unwrap();
    assert_eq!(span(&after), at("11:05", "11:20"));
    assert_eq!(after.action_ids, Some(crate::testing::shares(&["g1"])));
    assert_eq!(after.description.as_deref(), Some("Notes:\n- 11:05 coffee"));
}

#[test]
fn keep_can_set_or_clear_an_events_actions_alone() {
    let mut events = day();
    events[1].action_ids = Some(crate::testing::shares(&["old"]));
    let plan = plan_with(
        &[],
        &[EventDecision { action_ids: Some(crate::testing::shares(&["g1", "g2"])), ..keep("e1") }, EventDecision { action_ids: Some(vec![]), ..keep("e2") }, keep("e3")],
        &events,
        "11:30",
        Extra::default(),
    )
    .unwrap();
    assert_eq!(changed(&plan, "e1").after.unwrap().action_ids, Some(crate::testing::shares(&["g1", "g2"])));
    assert_eq!(changed(&plan, "e2").after.unwrap().action_ids, Some(vec![]));
    if let Some(lunch) = by_event(&plan, "e3") {
        assert_eq!(lunch.after.unwrap().action_ids, lunch.before.unwrap().action_ids);
    }
}

#[test]
fn a_future_events_actions_change_without_pinning_it() {
    let change = changed(&plan(&[], &[EventDecision { action_ids: Some(crate::testing::shares(&["g1"])), ..keep("e4") }]), "e4");
    assert_eq!(change.reason, "set what was done at it (its actions)");
    assert_eq!(change.after.unwrap().action_ids, Some(crate::testing::shares(&["g1"])));
}

#[test]
fn the_timeline_marks_actions_and_totals_their_time() {
    let mut events = day();
    events[0].action_ids = Some(crate::testing::shares(&["work"]));
    events[1].action_ids = Some(crate::testing::shares(&["work"]));
    let plan = plan_with(
        &[],
        &[EventDecision { action_ids: Some(crate::testing::shares(&["work", "writing"])), ..keep("e2") }],
        &events,
        "11:30",
        Extra { names: names(&[("work", "Time Tracker"), ("writing", "Writing")]), ..Default::default() },
    )
    .unwrap();
    let text = timeline_text(&plan);
    assert!(text.contains("┌ Email\n          ◆ Time Tracker\n"), "{text}");
    assert!(text.contains("├ Report\n          ◆ Time Tracker ◇ Writing\n"), "{text}");
    assert!(text.contains("Action time:\n  2h00m  Time Tracker\n  1h00m  Writing\n"), "{text}");
}

#[test]
fn keep_records_facts_and_the_timeline_shows_them_compactly() {
    let given = facts(json!({"locations": ["home"], "with": ["sam"], "for": ["mom"], "notes": {"self": "tired", "sam": "loved it"}}));
    let plan = plan_with(
        &[],
        &[EventDecision { facts: Some(given), ..keep("e2") }],
        &day(),
        "11:30",
        Extra { names: names(&[("home", "Home"), ("sam", "Sam"), ("mom", "Mom"), ("self", "Me")]), ..Default::default() },
    )
    .unwrap();
    assert_eq!(
        changed(&plan, "e2").after.unwrap().facts,
        Some(json!({"locations": ["home"], "with": ["sam"], "for": ["mom"], "notes": {"self": "tired", "sam": "loved it"}}))
    );
    let text = timeline_text(&plan);
    assert!(text.contains("├ Report\n          ▹ @ Home · with Sam · for Mom\n          ▹ Me: tired\n          ▹ Sam: loved it\n"), "{text}");
    assert!(text.contains("▸ facts   ▹ facts being set"));
}

#[test]
fn facts_already_recorded_show_as_had_and_are_kept() {
    let mut events = day();
    events[1].facts = Some(facts(json!({"with": ["sam"]})));
    let plan = plan_with(
        &[],
        &[EventDecision { summary: Some("Report!".into()), ..keep("e2") }],
        &events,
        "11:30",
        Extra { names: names(&[("sam", "Sam")]), ..Default::default() },
    )
    .unwrap();
    assert!(timeline_text(&plan).contains("          ▸ with Sam\n"));
    assert_eq!(changed(&plan, "e2").after.unwrap().facts, Some(json!({"with": ["sam"]})));
}

#[test]
fn a_person_taking_part_of_an_event_has_their_share_after_their_name() {
    // A passing thought of someone: judged on its part of the event alone.
    let given = facts(json!({"with": ["sam"], "for": [{"person_id": "mom", "minutes": 3}]}));
    let plan = plan_with(
        &[],
        &[EventDecision { facts: Some(given), ..keep("e2") }],
        &day(),
        "11:30",
        Extra { names: names(&[("sam", "Sam"), ("mom", "Mom")]), ..Default::default() },
    )
    .unwrap();
    let text = timeline_text(&plan);
    assert!(text.contains("├ Report\n          ▹ with Sam · for Mom (3m)\n"), "{text}");
    assert!(crate::compaction::guide::DECISION_GUIDE.contains("PEOPLE IN PASSING: a note can say the user thought of someone"));
    assert!(crate::compaction::guide::JUDGMENT_INSTRUCTIONS.contains("is judged on that part alone"));
}

#[test]
fn create_needs_both_edges() {
    refused_with(
        plan_with(&[note(1, "11:05", None)], &[EventDecision { start_note: Some("n1".into()), ..create(Some("Coffee")) }], &day(), "11:30", Extra::default()),
        "needs both a start and an end",
    );
}

#[test]
fn create_needs_a_summary() {
    refused_with(
        plan_with(&[], &[EventDecision { start: Some(time_at("11:05")), end: Some(time_at("11:20")), ..create(None) }], &day(), "11:30", Extra::default()),
        "needs a summary",
    );
}

#[test]
fn merge_folds_one_event_into_another() {
    let plan = plan(&[], &[EventDecision { event_id: Some("e2".into()), into: Some("e1".into()), ..EventDecision::new("merge") }]);
    let report = changed(&plan, "e2");
    assert_eq!(report.action, "cancel");
    assert_eq!(report.reason, "merged into 'Email'");
    let email = changed(&plan, "e1").after.unwrap();
    assert_eq!(span(&email), at("09:00", "11:00"));
    assert_eq!(email.summary.as_deref(), Some("Email and Report"));
    let shown = plan.timeline.as_ref().unwrap().events.iter().find(|e| e.event_id.as_deref() == Some("e2")).unwrap().clone();
    assert_eq!((shown.status.as_str(), shown.merged_into.as_deref()), ("merged", Some("Email and Report")));
}

#[test]
fn a_rename_on_the_target_overrides_the_joined_title() {
    let plan = plan(
        &[],
        &[
            EventDecision { event_id: Some("e2".into()), into: Some("e1".into()), ..EventDecision::new("merge") },
            EventDecision { summary: Some("Morning work".into()), ..keep("e1") },
        ],
    );
    assert_eq!(changed(&plan, "e1").after.unwrap().summary.as_deref(), Some("Morning work"));
}

#[test]
fn cannot_merge_into_a_cancelled_event() {
    refused_with(
        plan_with(
            &[],
            &[EventDecision { event_id: Some("e2".into()), into: Some("e1".into()), ..EventDecision::new("merge") }, cancel("e1")],
            &day(),
            "11:30",
            Extra::default(),
        ),
        "itself being cancelled",
    );
}

#[test]
fn a_night_merges_like_any_other_event() {
    let plan =
        plan_with(&[], &[EventDecision { event_id: Some("e4".into()), into: Some("s1".into()), ..EventDecision::new("merge") }], &day(), "16:00", Extra::default())
            .unwrap();
    assert_eq!(changed(&plan, "e4").reason, "merged into 'Sleep'");
    assert_eq!(span(changed(&plan, "s1").after.as_ref().unwrap()), (time_at("14:00"), time_at("07:00") + Duration::days(1)));
}

// -- notes added to events --------------------------------------------------------

#[test]
fn a_note_that_sets_no_edge_is_added_to_the_event_it_falls_within() {
    let plan = plan(&[note(1, "09:20", Some("phone rang")), note(2, "09:40", Some("back to it"))], &[]);
    assert_eq!(changed(&plan, "e1").after.unwrap().description.as_deref(), Some("Notes:\n- 09:20 phone rang\n- 09:40 back to it"));
}

#[test]
fn notes_and_annotate_text_go_below_an_existing_description() {
    let mut events = day();
    events[0].description = Some("Inbox zero".into());
    let plan = plan_with(
        &[note(1, "09:20", Some("phone rang"))],
        &[EventDecision { annotate: Some("mostly replies".into()), ..keep("e1") }],
        &events,
        "11:30",
        Extra::default(),
    )
    .unwrap();
    assert_eq!(changed(&plan, "e1").after.unwrap().description.as_deref(), Some("Inbox zero\n\nNotes:\n- 09:20 phone rang\n- mostly replies"));
}

#[test]
fn a_note_already_in_the_description_isnt_added_again() {
    let mut events = day();
    events[0].description = Some("Inbox zero\n\nNotes:\n- 09:20 phone rang".into());
    let plan =
        plan_with(&[note(1, "09:20", Some("phone rang")), note(2, "09:40", Some("back to it"))], &[], &events, "11:30", Extra::default()).unwrap();
    assert_eq!(
        changed(&plan, "e1").after.unwrap().description.as_deref(),
        Some("Inbox zero\n\nNotes:\n- 09:20 phone rang\n- 09:40 back to it")
    );
}

#[test]
fn a_note_can_go_with_another_event_than_the_one_it_falls_within() {
    let plan = plan_with(
        &[note(1, "09:20", Some("notes for the report"))],
        &[],
        &day(),
        "11:30",
        Extra { note_targets: targets(&[("n1", Some("e2"))]), ..Default::default() },
    )
    .unwrap();
    assert!(by_event(&plan, "e1").is_none());
    assert_eq!(changed(&plan, "e2").after.unwrap().description.as_deref(), Some("Notes:\n- 09:20 notes for the report"));
    assert_eq!(uses(&plan), using(&[("n1", "annotates", Some("e2"))]));
}

#[test]
fn a_note_that_sets_an_edge_is_added_too_when_its_given_an_event() {
    let plan = plan_with(
        &[note(1, "09:20", Some("started late"))],
        &[EventDecision { start_note: Some("n1".into()), ..keep("e1") }],
        &day(),
        "11:30",
        Extra { note_targets: targets(&[("n1", Some("e1"))]), ..Default::default() },
    )
    .unwrap();
    let email = changed(&plan, "e1").after.unwrap();
    assert_eq!((email.start, email.description.as_deref()), (Some(time_at("09:20")), Some("Notes:\n- 09:20 started late")));
    assert_eq!(uses(&plan), using(&[("n1", "annotates", Some("e1"))]));
}

#[test]
fn an_edge_note_given_no_event_is_added_to_the_one_whose_edge_it_sets() {
    let plan = plan_with(
        &[note(1, "09:50", Some("done with email"))],
        &[EventDecision { end_note: Some("n1".into()), ..keep("e1") }],
        &day(),
        "11:30",
        Extra { note_targets: targets(&[("n1", None)]), ..Default::default() },
    )
    .unwrap();
    assert_eq!(changed(&plan, "e1").after.unwrap().description.as_deref(), Some("Notes:\n- 09:50 done with email"));
    assert!(by_event(&plan, "e2").is_none());
    assert_eq!(uses(&plan), using(&[("n1", "annotates", Some("e1"))]));
    assert_eq!(plan.note_edges, vec![("n1".to_string(), "e1".to_string())]);
}

#[test]
fn an_edge_notes_text_goes_with_the_event_it_ends_or_else_the_one_it_starts() {
    let plan = plan(
        &[note(1, "09:05", Some("on email")), note(2, "09:50", Some("done with email, on to the report"))],
        &[EventDecision { start_note: Some("n1".into()), end_note: Some("n2".into()), ..keep("e1") }, EventDecision { start_note: Some("n2".into()), ..keep("e2") }],
    );
    assert_eq!(
        changed(&plan, "e1").after.unwrap().description.as_deref(),
        Some("Notes:\n- 09:05 on email\n- 09:50 done with email, on to the report")
    );
    assert_eq!(changed(&plan, "e2").after.unwrap().description, None);
    assert_eq!(uses(&plan), using(&[("n1", "annotates", Some("e1")), ("n2", "annotates", Some("e1"))]));
}

#[test]
fn an_ignored_edge_note_still_sets_its_edge() {
    let plan = plan_with(
        &[note(1, "09:50", Some("done with email"))],
        &[EventDecision { end_note: Some("n1".into()), ..keep("e1") }],
        &day(),
        "11:30",
        Extra { ignore_notes: strings(&["n1"]), ..Default::default() },
    )
    .unwrap();
    assert_eq!(changed(&plan, "e1").after.unwrap().description, None);
    assert_eq!(uses(&plan), using(&[("n1", "edge", Some("e1"))]));
    assert_eq!(plan.note_edges, vec![("n1".to_string(), "e1".to_string())]);
}

#[test]
fn a_note_whose_event_is_cancelled_goes_where_it_falls() {
    let plan = plan_with(
        &[note(1, "09:20", Some("phone rang"))],
        &[cancel("e2")],
        &day(),
        "11:30",
        Extra { note_targets: targets(&[("n1", Some("e2"))]), ..Default::default() },
    )
    .unwrap();
    assert_eq!(changed(&plan, "e1").after.unwrap().description.as_deref(), Some("Notes:\n- 09:20 phone rang"));
    assert!(plan.warnings.iter().any(|w| w.contains("which is cancelled")));
}

#[test]
fn a_note_for_an_event_that_isnt_there_is_refused() {
    refused_with(
        plan_with(&[note(1, "09:20", Some("phone rang"))], &[], &day(), "11:30", Extra { note_targets: targets(&[("n1", Some("nope"))]), ..Default::default() }),
        "can't go with 'nope'",
    );
}

#[test]
fn a_note_cant_be_both_ignored_and_given_an_event() {
    refused_with(
        plan_with(
            &[note(1, "09:20", Some("phone rang"))],
            &[],
            &day(),
            "11:30",
            Extra { ignore_notes: strings(&["n1"]), note_targets: targets(&[("n1", Some("e2"))]), ..Default::default() },
        ),
        "both ignored and added",
    );
}

#[test]
fn the_plan_says_what_each_note_is_for() {
    let plan = plan_with(
        &[note(1, "09:05", Some("started")), note(2, "09:20", Some("phone rang")), note(3, "09:40", Some("aside")), note(4, "09:50", None)],
        &[EventDecision { start_note: Some("n1".into()), ..keep("e1") }],
        &day(),
        "11:30",
        Extra { ignore_notes: strings(&["n3"]), ..Default::default() },
    )
    .unwrap();
    assert_eq!(
        uses(&plan),
        using(&[("n1", "annotates", Some("e1")), ("n2", "annotates", Some("e1")), ("n3", "ignored", None), ("n4", "unused", None)])
    );
}

#[test]
fn a_created_event_keeps_its_key() {
    let plan = plan(&[], &[EventDecision { start: Some(time_at("11:00")), end: Some(time_at("11:20")), key: Some("k1".into()), ..create(Some("Walk")) }]);
    let made = creates(&plan);
    assert_eq!(made[0].key.as_deref(), Some("k1"));
    let shown = plan.timeline.as_ref().unwrap().events.iter().find(|e| e.status == "new").unwrap().clone();
    assert_eq!(shown.event_id.as_deref(), Some("k1"));
}

#[test]
fn notes_that_would_overflow_a_description_are_refused_naming_them() {
    let mut events = day();
    events[0].description = Some("x".repeat(MAX_DESCRIPTION_BYTES - 20));
    refused_with(
        plan_with(&[note(1, "09:20", Some("phone rang")), note(2, "09:40", Some("back to it"))], &[], &events, "11:30", Extra::default()),
        r"'Email'.*ignore_notes \(n1, n2\)",
    );
}

#[test]
fn an_overflowing_annotate_is_refused() {
    refused_with(
        plan_with(&[], &[EventDecision { annotate: Some("x".repeat(MAX_DESCRIPTION_BYTES)), ..keep("e1") }], &day(), "11:30", Extra::default()),
        r"'Email'.*shorten its `annotate`",
    );
}

#[test]
fn the_limit_counts_utf8_bytes() {
    refused_with(
        plan_with(&[], &[EventDecision { annotate: Some("é".repeat(MAX_DESCRIPTION_BYTES / 2)), ..keep("e1") }], &day(), "11:30", Extra::default()),
        "bytes",
    );
}

#[test]
fn a_description_that_just_fits_is_kept() {
    let annotate = "x".repeat(MAX_DESCRIPTION_BYTES - "Notes:\n- ".len());
    let plan = plan(&[], &[EventDecision { annotate: Some(annotate), ..keep("e1") }]);
    assert_eq!(changed(&plan, "e1").after.unwrap().description.unwrap().len(), MAX_DESCRIPTION_BYTES);
}

#[test]
fn a_note_added_to_a_future_event_still_in_progress_changes_only_its_description() {
    let plan = plan_with(&[note(1, "10:15", Some("started outline"))], &[], &day(), "10:30", Extra::default()).unwrap();
    assert_eq!(changed(&plan, "e2").reason, "added the notes that fall during it");
}

#[test]
fn ignored_notes_are_not_added() {
    let plan = plan_with(&[note(1, "09:20", Some("phone rang"))], &[], &day(), "11:30", Extra { ignore_notes: strings(&["n1"]), ..Default::default() }).unwrap();
    assert!(by_event(&plan, "e1").is_none());
    assert!(plan.timeline.as_ref().unwrap().notes[0].ignored);
}

#[test]
fn a_note_outside_every_event_is_reported() {
    let plan = plan(&[note(1, "11:10", Some("wandered"))], &[]);
    assert!(plan.warnings.iter().any(|w| w.contains("doesn't fall within any event")));
}

// -- categories -------------------------------------------------------------------

#[test]
fn an_overlap_is_categorized() {
    let error = refused(plan_with(&[note(1, "10:20", None)], &[EventDecision { end_note: Some("n1".into()), ..keep("e1") }], &day(), "11:30", Extra::default()));
    assert_eq!(error.categories, strings(&["overlap"]));
}

#[test]
fn mixed_problems_list_each_kind_once() {
    let error = refused(plan_with(
        &[],
        &[keep("nope"), keep("nada"), EventDecision { end_note: Some("n9".into()), ..keep("e1") }],
        &day(),
        "11:30",
        Extra::default(),
    ));
    assert_eq!(error.categories, strings(&["unknown_event", "unknown_note"]));
}

#[test]
fn an_untagged_problem_takes_the_category_of_where_it_was_found() {
    let error = refused(plan_with(&[], &[EventDecision { start: Some(time_at("09:30")), end: Some(time_at("09:45")), ..create(None) }], &day(), "11:30", Extra::default()));
    assert_eq!(error.categories, strings(&["malformed_decision"]));
}

#[test]
fn mixed_tagged_and_plain_problems_keep_both_categories() {
    let error = error_of(&[Issue::tagged("overlap", "a"), Issue::plain("b")], "facts");
    assert_eq!((error.message.as_str(), error.categories.clone()), ("a\nb", strings(&["facts", "overlap"])));
}

// -- validation -------------------------------------------------------------------

#[test]
fn an_unknown_event_id_is_rejected_with_the_valid_ones() {
    refused_with(plan_with(&[], &[keep("nope")], &day(), "11:30", Extra::default()), "valid event ids: e1, e2, e3, e4, s1");
}

#[test]
fn an_unknown_note_id_is_rejected_with_the_valid_ones() {
    refused_with(
        plan_with(&[note(1, "09:05", None)], &[EventDecision { start_note: Some("n9".into()), ..keep("e1") }], &day(), "11:30", Extra::default()),
        "'n9' isn't one of this round's notes; valid note ids: n1",
    );
}

#[test]
fn an_unknown_ignored_note_is_rejected() {
    refused_with(plan_with(&[], &[], &day(), "11:30", Extra { ignore_notes: strings(&["n9"]), ..Default::default() }), "ignore_notes: 'n9'");
}

#[test]
fn an_event_cannot_have_two_decisions() {
    refused_with(plan_with(&[], &[keep("e1"), cancel("e1")], &day(), "11:30", Extra::default()), "more than one decision");
}

#[test]
fn times_only_go_with_keep_and_create() {
    refused_with(
        plan_with(&[], &[EventDecision { end: Some(time_at("09:30")), ..cancel("e1") }], &day(), "11:30", Extra::default()),
        "only go with 'keep' or 'create'",
    );
}

#[test]
fn an_unknown_action_is_rejected() {
    refused_with(
        plan_with(&[], &[EventDecision { event_id: Some("e1".into()), ..EventDecision::new("skip") }], &day(), "11:30", Extra::default()),
        "unknown action 'skip'",
    );
}

#[test]
fn a_non_positive_length_is_rejected() {
    refused_with(plan_with(&[], &[EventDecision { end: Some(time_at("09:00")), ..keep("e1") }], &day(), "11:30", Extra::default()), "isn't a positive length");
}

#[test]
fn notes_after_now_are_rejected() {
    refused_with(plan_with(&[note(1, "12:00", None)], &[], &day(), "11:30", Extra::default()), "timestamped after now");
}

#[test]
fn every_problem_is_reported_at_once() {
    let error = refused(plan_with(&[], &[keep("nope"), EventDecision { start_note: Some("n9".into()), ..keep("e1") }], &day(), "11:30", Extra::default()));
    assert!(error.message.contains("'nope'") && error.message.contains("'n9'"));
}

#[test]
fn with_nothing_to_change_it_says_so() {
    let events: Vec<Event> = day().into_iter().filter(|e| e.id.as_deref() != Some("e1") && e.id.as_deref() != Some("e2")).collect();
    let plan = plan_with(&[], &[], &events, "11:30", Extra::default()).unwrap();
    assert!(plan.changes.is_empty());
    assert_eq!(plan.warnings, strings(&["nothing on the calendar needs to change"]));
}

// -- moving bedtime ---------------------------------------------------------------

fn bedtime_evening(extra: Option<Event>) -> Vec<Event> {
    let mut events = vec![event_at("17:00-18:00").id("e1").summary("Dinner").priority(1).build()];
    events.extend(extra);
    events.push(event_at("20:00-07:00+1").id("s1").summary("Sleep").priority(0).build());
    events
}

#[test]
fn a_later_bedtime_just_moves_it() {
    let plan = plan_with(&[], &[EventDecision { start: Some(time_at("22:30")), ..keep("s1") }], &day(), "08:00", Extra::default()).unwrap();
    assert_eq!(plan.changes.iter().filter_map(|c| c.event_id.clone()).collect::<Vec<_>>(), strings(&["s1"]));
    assert_eq!(span(changed(&plan, "s1").after.as_ref().unwrap()), (time_at("22:30"), time_at("07:00") + Duration::days(1)));
    assert!(plan.warnings.is_empty());
}

#[test]
fn an_earlier_bedtime_needs_what_runs_past_it_ended_first() {
    let reading = event_at("18:00-20:00").id("e5").summary("Reading").priority(3).build();
    let bedtime = EventDecision { start: Some(time_at("19:00")), ..keep("s1") };
    refused_with(
        plan_with(&[], &[bedtime.clone()], &bedtime_evening(Some(reading.clone())), "08:00", Extra::default()),
        &regex::escape("'Reading' (18:00–20:00) overlaps 'Sleep'"),
    );
    let plan = plan_with(
        &[],
        &[bedtime, EventDecision { end: Some(time_at("19:00")), ..keep("e5") }],
        &bedtime_evening(Some(reading)),
        "08:00",
        Extra::default(),
    )
    .unwrap();
    assert_eq!(span(changed(&plan, "e5").after.as_ref().unwrap()), at("18:00", "19:00"));
    assert!(creates(&plan).is_empty());
}

#[test]
fn an_earlier_bedtime_needs_what_starts_after_it_cancelled_first() {
    let journal = event_at("19:30-20:00").id("e5").summary("Journal").priority(1).build();
    let bedtime = EventDecision { start: Some(time_at("19:00")), ..keep("s1") };
    refused_with(
        plan_with(&[], &[bedtime.clone()], &bedtime_evening(Some(journal.clone())), "08:00", Extra::default()),
        &regex::escape("overlaps 'Journal' (19:30–20:00)"),
    );
    let plan = plan_with(&[], &[bedtime, cancel("e5")], &bedtime_evening(Some(journal)), "08:00", Extra::default()).unwrap();
    assert_eq!(changed(&plan, "e5").action, "cancel");
}

#[test]
fn moving_its_end_just_moves_it() {
    let plan = plan_with(&[], &[EventDecision { end: Some(time_at("08:00") + Duration::days(1)), ..keep("s1") }], &day(), "08:00", Extra::default()).unwrap();
    assert_eq!(plan.changes.iter().filter_map(|c| c.event_id.clone()).collect::<Vec<_>>(), strings(&["s1"]));
    assert!(plan.warnings.is_empty());
}

#[test]
fn facts_before_it_still_reflow_into_the_day() {
    let events = vec![
        event_at("20:00-07:00+1").id("s1").summary("Sleep").priority(0).build(),
        event_at("17:00-19:00").id("e5").summary("Reading").priority(3).build(),
    ];
    let plan = plan_with(
        &[note(1, "17:30", None), note(2, "19:30", None)],
        &[
            EventDecision { start_note: Some("n1".into()), end_note: Some("n2".into()), ..keep("e5") },
            EventDecision { start: Some(time_at("22:00")), ..keep("s1") },
        ],
        &events,
        "19:30",
        Extra::default(),
    )
    .unwrap();
    assert_eq!(span(changed(&plan, "e5").after.as_ref().unwrap()), at("17:30", "19:30"));
    assert_eq!(span(changed(&plan, "s1").after.as_ref().unwrap()), (time_at("22:00"), time_at("07:00") + Duration::days(1)));
}

#[test]
fn moving_last_nights_wake_up_just_moves_it() {
    let plan = plan_with(
        &[note(1, "07:20+1", Some("up"))],
        &[EventDecision { end_note: Some("n1".into()), ..keep("s0") }, EventDecision { start_note: Some("n1".into()), ..keep("gr") }],
        &overnight(),
        "09:00+1",
        Extra::default(),
    )
    .unwrap();
    assert_eq!(changed(&plan, "s0").after.unwrap().end, Some(time_at("07:20+1")));
    assert!(plan.warnings.is_empty());
}

// -- noted sleep ------------------------------------------------------------------

fn late_day() -> Vec<Event> {
    vec![
        event_at("21:00-23:30").id("e1").summary("Reading").priority(3).build(),
        event_at("00:00+1-07:00+1").id("s1").summary("Sleep").priority(0).build(),
    ]
}

fn late_notes() -> Vec<PlanNote> {
    vec![note(1, "23:47", Some("Starting to get ready for bed")), note(2, "00:01+1", Some("Finished"))]
}

fn get_ready() -> EventDecision {
    EventDecision { start_note: Some("n1".into()), end_note: Some("n2".into()), ..create(Some("Get ready for bed")) }
}

#[test]
fn a_later_bedtime_makes_room_for_an_activity_before_it() {
    let plan =
        plan_with(&late_notes(), &[get_ready(), EventDecision { start_note: Some("n2".into()), ..keep("s1") }], &late_day(), "07:00+1", Extra::default()).unwrap();
    assert_eq!(span(changed(&plan, "s1").after.as_ref().unwrap()), at("00:01+1", "07:00+1"));
    let made: Vec<(Option<String>, (Moment, Moment))> = creates(&plan).iter().map(|c| (c.after.clone().unwrap().summary, span(c.after.as_ref().unwrap()))).collect();
    assert_eq!(made, vec![(Some("Get ready for bed".to_string()), at("23:47", "00:01+1"))]);
}

#[test]
fn an_earlier_bedtime_needs_what_ran_past_it_ended_too() {
    let bedtime = EventDecision { start_note: Some("n1".into()), ..keep("s1") };
    refused_with(plan_with(&[note(1, "23:00", None)], &[bedtime.clone()], &late_day(), "07:00+1", Extra::default()), "'Reading' .*overlaps 'Sleep'");
    let plan = plan_with(
        &[note(1, "23:00", None)],
        &[bedtime, EventDecision { end_note: Some("n1".into()), ..keep("e1") }],
        &late_day(),
        "07:00+1",
        Extra::default(),
    )
    .unwrap();
    assert_eq!(span(changed(&plan, "s1").after.as_ref().unwrap()), at("23:00", "07:00+1"));
    assert_eq!(span(changed(&plan, "e1").after.as_ref().unwrap()), at("21:00", "23:00"));
}

#[test]
fn running_into_sleep_without_moving_it_is_rejected() {
    refused_with(plan_with(&late_notes(), &[get_ready()], &late_day(), "07:00+1", Extra::default()), "overlaps 'Sleep'");
}

// -- the timeline -----------------------------------------------------------------

fn salsa() -> CompactionPlan {
    plan_with(
        &[
            note(1, "18:15", Some("Leaving for salsa early to prep")),
            note(2, "19:00", Some("Learned the cross-body lead")),
            note(3, "20:10", Some("Done with dinner")),
        ],
        &[
            EventDecision { start_note: Some("n1".into()), end: Some(time_at("18:30")), ..create(Some("Salsa prep")) },
            EventDecision { end_note: Some("n3".into()), ..keep("dinner") },
            EventDecision { start_note: Some("n3".into()), ..keep("read") },
        ],
        &evening(),
        "20:30",
        Extra::default(),
    )
    .unwrap()
}

#[test]
fn reports_what_happened_to_each_event() {
    let plan = salsa();
    let statuses: Vec<(String, String)> = plan.timeline.unwrap().events.iter().map(|e| (e.summary.clone(), e.status.clone())).collect();
    let mut expected = vec![
        ("Work", "on_schedule"),
        ("Salsa prep", "new"),
        ("Google Salsa class", "on_schedule"),
        ("Dinner", "adjusted"),
        ("Reading", "adjusted"),
    ];
    let mut got: Vec<(&str, &str)> = statuses.iter().map(|(a, b)| (a.as_str(), b.as_str())).collect();
    got.sort();
    expected.sort();
    assert_eq!(got, expected);
}

#[test]
fn shows_a_future_event_only_once_the_plan_moves_it() {
    let plan = plan_with(
        &[],
        &[EventDecision { end: Some(time_at("20:30")), ..keep("dinner") }, EventDecision { start: Some(time_at("20:30")), ..keep("read") }],
        &evening(),
        "19:55",
        Extra::default(),
    )
    .unwrap();
    let timeline = plan.timeline.clone().unwrap();
    let status = |id: &str| timeline.events.iter().find(|e| e.event_id.as_deref() == Some(id)).map(|e| e.status.clone());
    assert_eq!(status("read").as_deref(), Some("adjusted"));
    assert_eq!(status("s1"), None);
    assert!(by_event(&plan, "s1").is_none());
}

#[test]
fn reports_what_each_note_did() {
    let timeline = salsa().timeline.unwrap();
    let note_of = |id: &str| timeline.notes.iter().find(|n| n.id == id).unwrap().clone();
    assert_eq!(note_of("n1").anchors, strings(&["start of Salsa prep"]));
    assert_eq!(note_of("n2").annotates.as_deref(), Some("Google Salsa class"));
    assert_eq!(note_of("n3").anchors, strings(&["end of Dinner", "start of Reading"]));
}

#[test]
fn renders_notes_and_events_in_one_narrow_column() {
    let text = timeline_text(&salsa());
    assert!(text.starts_with(concat!(
        "17:00 ┌ Work\n",
        "          ⚠ no action, no location\n",
        "18:15 ● Leaving for salsa early to prep\n",
        "        ↳ Salsa prep\n",
        "     →├ Salsa prep · new\n",
        "          ⚠ no action, no location\n",
        "18:30 ├ Google Salsa class\n",
        "          ⚠ no action, no location\n",
        "19:00 ● Learned the cross-body lead\n",
        "        ↳ Google Salsa class\n",
        "19:30 ├ Dinner\n",
        "          ⚠ no action, no location\n",
        "20:10 ● Done with dinner\n",
        "        ↳ Dinner\n",
        "     →└ Dinner ends · +10m (was 20:00)\n",
        "     →├ Reading · +10m (was 20:00)\n",
        "          ⚠ no action, no location\n",
        "20:30 ┄┄ now ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄\n",
        "21:00 └ Reading ends\n",
    )), "{text}");
    assert!(text.lines().map(|l| l.chars().count()).max().unwrap() <= 40);
}

#[test]
fn flags_what_each_past_event_is_missing() {
    let home = facts(json!({"locations": ["home"]}));
    let plan = plan_with(
        &[],
        &[
            EventDecision { action_ids: Some(crate::testing::shares(&["g1"])), facts: Some(home.clone()), ..keep("e1") },
            EventDecision { action_ids: Some(crate::testing::shares(&["g1"])), ..keep("e2") },
            EventDecision { facts: Some(home), ..keep("e4") },
        ],
        &day(),
        "12:30",
        Extra { names: names(&[("g1", "Email"), ("home", "Home")]), ..Default::default() },
    )
    .unwrap();
    let timeline = plan.timeline.clone().unwrap();
    let missing = |id: &str| timeline.events.iter().find(|e| e.event_id.as_deref() == Some(id)).unwrap().missing.clone();
    assert_eq!(missing("e1"), Vec::<String>::new());
    assert_eq!(missing("e2"), strings(&["location"]));
    assert_eq!(missing("e3"), strings(&["action", "location"]));
    // Review, at 14:00, hasn't happened: its facts aren't recorded, so
    // nothing changes it.
    assert!(!timeline.events.iter().any(|e| e.event_id.as_deref() == Some("e4")));
    assert!(!plan.changes.iter().any(|c| c.event_id.as_deref() == Some("e4")));
    let text = timeline.text;
    assert!(text.contains("├ Report\n          ◇ Email\n          ⚠ no location\n"), "{text}");
    assert!(text.contains("Missing:\n  ⚠ Report: location\n  ⚠ Lunch: action, location\n"), "{text}");
    assert!(text.contains("⚠ missing action or location"));
}

#[test]
fn flags_nothing_once_every_past_event_is_settled() {
    let home = facts(json!({"locations": ["home"]}));
    let decisions: Vec<EventDecision> =
        ["e1", "e2", "e3"].iter().map(|e| EventDecision { action_ids: Some(crate::testing::shares(&["g1"])), facts: Some(home.clone()), ..keep(e) }).collect();
    let plan = plan_with(&[], &decisions, &day(), "12:30", Extra::default()).unwrap();
    assert!(!timeline_text(&plan).contains('⚠'));
}

#[test]
fn flags_nothing_before_anything_is_decided() {
    let timeline = planned_timeline(&[], &day(), time_at("12:30"), &[], &[], None, None);
    assert!(timeline.events.iter().all(|e| e.missing.is_empty()));
    assert!(!timeline.text.contains('⚠'));
}

#[test]
fn wraps_a_long_note_under_its_text() {
    let plan = plan(&[note(1, "09:10", Some("finally got through the whole inbox after a long detour"))], &[]);
    assert!(timeline_text(&plan).contains("09:10 ● finally got through the whole\n        inbox after a long detour\n"));
}

#[test]
fn says_where_a_note_set_an_edge_at_another_time() {
    let plan = plan(&[note(1, "09:30", Some("leaving in 15"))], &[EventDecision { end_note: Some("n1".into()), end: Some(time_at("09:45")), ..keep("e1") }]);
    assert!(timeline_text(&plan).contains("09:30 ● leaving in 15\n        ↳ Email\n        → end of Email (09:45)\n"));
}

#[test]
fn marks_the_last_compaction_before_what_came_at_the_same_moment() {
    let plan = plan_with(&[note(1, "09:10", Some("email"))], &[], &day(), "11:30", Extra { last_compaction: Some(time_at("09:00")), ..Default::default() }).unwrap();
    let text = timeline_text(&plan);
    let lines: Vec<&str> = text.lines().collect();
    let marker = lines.iter().position(|l| l.contains("┄┄ last compaction")).unwrap();
    assert!(lines[marker].starts_with("09:00"));
    assert!(lines[marker + 1].contains("┌ Email"));
}

#[test]
fn dates_a_last_compaction_on_an_earlier_day() {
    let plan = plan_with(
        &[note(1, "09:10+1", Some("email"))],
        &[],
        &[event_at("09:00+1-10:00+1").id("e1").summary("Email").priority(2).build()],
        "11:30+1",
        Extra { last_compaction: Some(time_at("21:00")), ..Default::default() },
    )
    .unwrap();
    assert!(timeline_text(&plan).contains("┄┄ last compaction (Thu 01 Jan)"));
}

#[test]
fn shows_cancelled_and_moved_events() {
    let plan = plan(&[], &[cancel("e2"), EventDecision { start: Some(time_at("12:30")), end: Some(time_at("13:30")), ..keep("e3") }]);
    let text = timeline_text(&plan);
    assert!(text.contains("✕ Report · cancelled (was\n        10:00–11:00)"), "{text}");
    assert!(text.contains("┌ Lunch · ⇢30m (was 12:00–13:00)"), "{text}");
}

#[test]
fn marks_notes_that_were_not_added_anywhere() {
    let plan = plan_with(
        &[note(1, "11:10", Some("wandered")), note(2, "09:20", Some("skip me"))],
        &[],
        &day(),
        "11:30",
        Extra { ignore_notes: strings(&["n2"]), ..Default::default() },
    )
    .unwrap();
    let text = timeline_text(&plan);
    assert!(text.contains("○ wandered") && text.contains("○ skip me"));
}

// -- decisions and states as JSON -------------------------------------------------

#[test]
fn a_decision_round_trips_through_json() {
    let decision = EventDecision { start: Some(time_at("09:05")), end_note: Some("n2".into()), annotate: Some("x".into()), ..keep("e1") };
    assert_eq!(EventDecision::from_json_dict(&decision.to_json_dict()).unwrap(), decision);
}

#[test]
fn a_decisions_json_omits_unset_fields() {
    assert_eq!(cancel("e1").to_json_dict(), json!({"action": "cancel", "event_id": "e1"}));
}

#[test]
fn a_state_round_trips_through_json() {
    let state = EventState::from_event(&event_at("09:00-10:00").summary("Email").description("d").priority(2).build());
    assert_eq!(EventState::from_json_dict(&state.to_json_dict()).unwrap(), state);
}

#[test]
fn a_states_json_omits_unset_fields() {
    let state = EventState::from_event(&event_at("09:00-10:00").build());
    assert!(state.to_json_dict().get("description").is_none());
}

#[test]
fn to_event_builds_an_event_with_the_given_id() {
    let event = EventState::from_event(&event_at("09:00-10:00").priority(1).build()).to_event(Some("x1"));
    assert_eq!(event.id.as_deref(), Some("x1"));
    assert_eq!(event.priority, Some(1));
}

#[test]
fn reads_a_journal_written_when_events_were_marked_compacted() {
    let state = EventState::from_json_dict(&json!({
        "start": "2026-01-01T09:00:00+00:00", "end": "2026-01-01T10:00:00+00:00", "compacted_until": "2026-01-01T10:00:00+00:00"
    }))
    .unwrap();
    assert_eq!(state.end, Some(time_at("10:00")));
}
