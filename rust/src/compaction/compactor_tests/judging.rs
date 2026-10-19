//! TestJudgments, TestHabitBackfill, TestStayingUpPastTheLastCompaction,
//! TestFollowThrough and TestPrioritiesKept.

use super::*;
use crate::sheet::{ACTIONS, HABITS};
use crate::testing::facts;

fn sam() -> Vec<Value> {
    vec![json!({"id": "sam", "name": "Sam", "status": "active"})]
}

fn heard() -> Value {
    json!({
        "id": "heard", "name": "Heard", "status": "active", "definition": "Listen.",
        "parts": [{"kind": "judgment", "rubric": "Were they heard?", "ratings": {"0": "no", "1": "yes"}, "facts": ["general_notes", "fact:worries"]}],
        "facts": [{"id": "worries", "name": "Worries", "summary_prompt": "What weighs on them."}],
    })
}

fn judged(people: Vec<Value>, traits: Vec<Value>) -> Setup {
    Setup::with(&[("09:05", "email")], None, "11:30", Stores { people, traits: Some(traits), ..Default::default() })
}

fn with_sam(given: Value) -> Vec<EventDecision> {
    vec![EventDecision { facts: Some(facts(given)), ..keep("e1") }]
}

/// Applied: the email was with Sam, who vented about work.
fn applied() -> (Setup, CompactionResult) {
    let setup = judged(sam(), vec![heard()]);
    let id = setup.dry_run(with_sam(json!({"with": ["sam"], "notes": {"sam": "vented about work"}}))).compaction_id.unwrap();
    let applied = setup.commit(&id);
    (setup, applied)
}

fn judgment(request_id: &str, rating: i64, reasoning: &str) -> Judgment {
    Judgment { request_id: request_id.into(), rating, reasoning: reasoning.into() }
}

fn record(setup: &Setup, compaction_id: &str, judgments: &[Judgment]) -> anyhow::Result<JudgmentsResult> {
    setup.call(|c| c.record_judgments(compaction_id, judgments))
}

fn judgments_of(setup: &Setup, event_id: &str) -> Value {
    Value::Object(setup.event(event_id).judgments.unwrap_or_default())
}

// -- judgments ------------------------------------------------------------------------
// Once its facts are written, a compaction's events are judged, and it
// isn't complete until they are.

#[test]
fn applying_asks_for_the_judgments_and_isnt_complete_until_theyre_made() {
    let (setup, applied) = applied();

    let due = applied.judgments.as_ref().unwrap();
    assert_eq!(due.events.len(), 1);
    let event = &due.events[0];
    assert_eq!((event.event_id.as_str(), event.summary.as_deref()), ("e1", Some("Email")));
    assert_eq!(event.people, vec![("self".to_string(), strings(&["heard/judgment"])), ("sam".to_string(), strings(&["heard/judgment"]))]);
    assert!(applied.message.contains("isn't complete yet: make the 2 judgment(s)"), "{}", applied.message);
    assert!(due.to_json()["instructions"].as_str().unwrap().contains("JUDGE EACH ONE YOURSELF"));
    assert_eq!(setup.prepare().judgments_pending, applied.compaction_id);
}

#[test]
fn each_part_is_given_once_however_many_judgments_name_it() {
    let (_setup, applied) = applied();

    let parts = &applied.judgments.as_ref().unwrap().parts;
    assert_eq!(parts.len(), 1);
    let part = &parts[0];
    assert_eq!(
        (part.key.as_str(), part.trait_name.as_str(), part.engagement.as_str(), part.rubric.clone(), part.ratings.clone(), part.facts.clone()),
        ("heard/judgment", "Heard", "with", json!("Were they heard?"), json!({"0": "no", "1": "yes"}), strings(&["general_notes", "fact:worries"]))
    );
}

#[test]
fn they_come_beside_the_final_timeline_the_user_approved() {
    let setup = judged(sam(), vec![heard()]);
    let planned = setup.dry_run(with_sam(json!({"with": ["sam"]})));

    let applied = setup.commit(planned.compaction_id.as_ref().unwrap());

    assert_eq!(applied.timeline.unwrap().text, planned.timeline.unwrap().text);
    assert!(applied.message.contains("of the events as `timeline` shows them"));
}

#[test]
fn nothing_about_judging_is_sent_before_the_plan_is_approved() {
    let setup = judged(sam(), vec![heard()]);

    let context = setup.prepare();
    let planned = setup.dry_run(with_sam(json!({"with": ["sam"]})));

    let sent = format!("{}{}", context.to_json(), planned.to_json());
    assert!(!sent.contains("Were they heard?"));
}

#[test]
fn a_persons_own_parts_are_given_under_a_key_of_their_own() {
    let own = json!([{"kind": "judgment", "rubric": "Did Sam feel heard?", "ratings": {"0": "no", "1": "yes"}, "facts": ["general_notes"]}]);
    let setup = judged(vec![json!({"id": "sam", "name": "Sam", "status": "active", "traits": {"parts": {"heard": own}}})], vec![heard()]);
    let id = setup.dry_run(with_sam(json!({"with": ["sam"]}))).compaction_id.unwrap();

    let applied = setup.commit(&id);

    let parts: Vec<(String, Value)> = applied.judgments.unwrap().parts.into_iter().map(|p| (p.key, p.rubric)).collect();
    assert_eq!(
        parts,
        vec![("heard/judgment".to_string(), json!("Were they heard?")), ("heard/judgment@sam".to_string(), json!("Did Sam feel heard?"))]
    );
}

#[test]
fn each_persons_recent_history_is_given_once() {
    let mut trait_ = heard();
    trait_["parts"][0]["facts"] = json!([{"fact": "action_history", "lookback_days": 7}]);
    let mut events = vec![Event {
        start: Some(time_at("09:00") - Duration::days(1)),
        end: Some(time_at("10:00") - Duration::days(1)),
        action_ids: Some(crate::testing::shares(&["guitar"])),
        facts: Some(facts(json!({"with": ["sam"]}))),
        ..event_at("09:00-10:00").id("g1").summary("Guitar").build()
    }];
    events.extend(day());
    let setup = Setup::with(
        &[("09:05", "email")],
        Some(events),
        "11:30",
        Stores { people: sam(), traits: Some(vec![trait_]), actions: vec![json!({"id": "guitar", "name": "Play guitar", "status": "active"})], ..Default::default() },
    );
    let id = setup.dry_run(with_sam(json!({"with": ["sam"]}))).compaction_id.unwrap();

    let applied = setup.commit(&id);

    let history: Vec<(String, i64, Vec<(String, i64)>)> =
        applied.judgments.unwrap().history.into_iter().map(|(p, h)| (p, h.days, h.actions)).collect();
    let guitar = vec![("Play guitar".to_string(), 1)];
    assert_eq!(history, vec![("self".to_string(), 7, guitar.clone()), ("sam".to_string(), 7, guitar)]);
}

#[test]
fn recording_them_all_completes_it() {
    let (setup, applied) = applied();
    let id = applied.compaction_id.unwrap();

    let partial = record(&setup, &id, &[judgment("e1/sam/heard/judgment", 1, "Talked it out.")]).unwrap();
    assert_eq!((partial.complete, partial.remaining.clone()), (false, strings(&["e1/self/heard/judgment"])));

    let done = record(&setup, &id, &[judgment("e1/self/heard/judgment", 0, "Mostly listened.")]).unwrap();

    assert!(done.complete && done.remaining.is_empty());
    assert_eq!(judgments_of(&setup, "e1")["sam"]["heard"]["judgment"]["rating"], 1);
    assert!(setup.call(|c| c.judgments_due(None, false)).unwrap().is_none_or(|d| d.events.is_empty()));
    assert_eq!(setup.prepare().judgments_pending, None);
}

#[test]
fn a_judgment_can_be_redone() {
    let (setup, applied) = applied();
    let id = applied.compaction_id.unwrap();
    record(&setup, &id, &[judgment("e1/sam/heard/judgment", 1, "Talked it out.")]).unwrap();

    let redo = setup.call(|c| c.judgments_due(Some(&id), true)).unwrap().unwrap();
    record(&setup, &id, &[judgment("e1/sam/heard/judgment", 0, "Only half.")]).unwrap();

    assert_eq!(redo.events.len(), 1);
    assert_eq!(
        redo.events[0].current.clone().map(Value::Object),
        Some(json!({"sam": {"heard/judgment": {"rating": 1, "reasoning": "Talked it out."}}}))
    );
    assert_eq!(judgments_of(&setup, "e1")["sam"]["heard"]["judgment"]["reasoning"], "Only half.");
}

#[test]
fn an_unapplied_compaction_has_nothing_to_judge() {
    let setup = judged(vec![], vec![heard()]);
    let id = setup.dry_run(vec![]).compaction_id.unwrap();

    refused(setup.call(|c| c.judgments_due(Some(&id), false)), "hasn't been applied");
}

// -- habit backfill ---------------------------------------------------------------------
// A habit's judgments of its settled events, backfilled by hand: from
// prepare_habit_judgments to record_judgments, by the backfill's id.

fn backfill_setup(status: &str) -> Setup {
    let (setup, _) = applied();
    // Email, and Lunch after the compaction, are both in the habit's scope;
    // only Email is settled.
    setup.t.seed(ACTIONS.1, &[json!({"id": "mail", "name": "Do email", "status": "active"})]);
    setup.change_event(Event { id: Some("e1".into()), action_ids: Some(crate::testing::shares(&["mail"])), ..Default::default() });
    setup.change_event(Event { id: Some("e3".into()), action_ids: Some(crate::testing::shares(&["mail"])), facts: Some(facts(json!({"notes": {"self": "late"}}))), ..Default::default() });
    setup.t.seed(HABITS.1, &[json!({"id": "inbox", "name": "Inbox", "action_id": "mail", "status": status})]);
    setup
}

fn habit_due(setup: &Setup, habit: &str, since: Option<Moment>, redo: bool) -> anyhow::Result<HabitJudgmentsDue> {
    setup.call(|c| c.habit_judgments_due(habit, since, redo))
}

#[test]
fn gives_a_backfill_of_its_settled_events_in_scope_and_records_them_by_its_id() {
    let setup = backfill_setup("active");

    let due = habit_due(&setup, "Inbox", None, false).unwrap();

    assert_eq!(due.backfill_id, format!("habit:inbox@{}", py::isoformat(&due.since)));
    assert_eq!(due.until, time_at("11:30"));
    assert_eq!(due.since, due.until - Duration::days(30 + 7));
    let events: Vec<(String, Vec<(String, Vec<String>)>)> = due.due.events.iter().map(|e| (e.event_id.clone(), e.people.clone())).collect();
    assert_eq!(events, vec![("e1".to_string(), vec![("habit:inbox".to_string(), strings(&["heard/judgment"]))])]);
    assert!(due.to_json()["instructions"].as_str().unwrap().contains("backfill"));

    let result = record(&setup, &due.backfill_id, &[judgment("e1/habit:inbox/heard/judgment", 1, "Cleared it calmly.")]).unwrap();

    assert_eq!((result.complete, result.remaining), (true, vec![]));
    assert_eq!(judgments_of(&setup, "e1")["habit:inbox"]["heard"]["judgment"]["rating"], 1);
    assert!(habit_due(&setup, "inbox", None, false).unwrap().due.events.is_empty());
}

#[test]
fn a_backfill_is_redone_with_the_judgments_made() {
    let setup = backfill_setup("active");
    let due = habit_due(&setup, "inbox", None, false).unwrap();
    record(&setup, &due.backfill_id, &[judgment("e1/habit:inbox/heard/judgment", 1, "Calm.")]).unwrap();

    let redo = habit_due(&setup, "inbox", None, true).unwrap();
    record(&setup, &redo.backfill_id, &[judgment("e1/habit:inbox/heard/judgment", 0, "Rushed.")]).unwrap();

    assert_eq!(redo.due.events.len(), 1);
    assert_eq!(
        redo.due.events[0].current.clone().map(Value::Object),
        Some(json!({"habit:inbox": {"heard/judgment": {"rating": 1, "reasoning": "Calm."}}}))
    );
    assert_eq!(judgments_of(&setup, "e1")["habit:inbox"]["heard"]["judgment"]["reasoning"], "Rushed.");
}

#[test]
fn a_backfill_runs_from_when_its_asked() {
    let setup = backfill_setup("active");

    // After Email started: nothing settled since.
    assert!(habit_due(&setup, "inbox", Some(time_at("09:30")), false).unwrap().due.events.is_empty());
}

#[test]
fn only_an_active_habit_and_one_there() {
    let setup = backfill_setup("archived");

    refused(habit_due(&setup, "inbox", None, false), "is archived: only an active habit is judged");
    refused(habit_due(&setup, "nope", None, false), "There's no habit with the id or name 'nope'");
}

#[test]
fn a_judgment_that_isnt_the_backfills_is_refused() {
    let setup = backfill_setup("active");
    let due = habit_due(&setup, "inbox", None, false).unwrap();

    refused(record(&setup, &due.backfill_id, &[judgment("e1/sam/heard/judgment", 1, "x")]), "isn't one of");
}

// -- staying up past the last compaction ------------------------------------------------------
// The night that prompted settling up to the last compaction: compacted at
// 00:32 while work ran on (planned to 00:47, then getting ready for bed and
// sleep from 01:17), the next notes came at 01:56 and 02:55 -- during the
// planned night, but the user hadn't gone to bed.

fn staying_up() -> Setup {
    let events = vec![
        event_at("22:22-23:07").id("dinner").summary("Dinner").priority(2).build(),
        event_at("23:07-00:47+1").id("work").summary("Working").priority(2).build(),
        event_at("00:47+1-01:17+1").id("gr").summary("Get ready for bed").priority(2).build(),
        event_at("01:17+1-07:00+1").id("s0").summary("Sleep").priority(0).sleep().build(),
        event_at("07:00+1-08:00+1").id("up").summary("Get up").priority(2).build(),
        event_at("08:00+1-12:00+1").id("w1").summary("Work").priority(3).build(),
        Event {
            id: Some("s1".into()),
            summary: Some("Sleep".into()),
            start: Some(time_at("23:00+1")),
            end: Some(time_at("07:00+1") + Duration::days(1)),
            priority: Some(0),
            is_end_of_day_sleep: Some(true),
            ..Default::default()
        },
    ];
    let setup = Setup::new(&[("01:56+1", "Restructuring goals"), ("02:55+1", "Getting ready for bed"), ("09:00+1", "up at last")], Some(events), "09:30+1");
    setup.start_plain("prev", time_at("00:32+1"));
    setup.set_status("prev", STAMPED);
    setup
}

fn categories<T>(result: anyhow::Result<T>, pattern: &str) -> Vec<String> {
    let Err(error) = result else { panic!("{pattern:?} wasn't refused") };
    let refusal = compaction_error(&error).expect("a CompactionError");
    assert!(regex::Regex::new(pattern).unwrap().is_match(&refusal.message), "{pattern:?} isn't in: {}", refusal.message);
    refusal.categories.clone()
}

#[test]
fn the_window_starts_at_the_last_compaction_not_the_first_note() {
    let setup = staying_up();

    let context = setup.prepare();

    let days = context.days.as_ref().unwrap();
    assert_eq!(days.len(), 2);
    assert_eq!(days[0].compaction_window_start, time_at("00:32+1"));
    assert_eq!(days[0].note_ids, vec![setup.note_id(2), setup.note_id(3)]);
    assert_eq!(days[1].note_ids, vec![setup.note_id(4)]);
    // The work going on at the last compaction, and the evening planned
    // after it, are offered -- and the work says how far it's settled.
    let offered = event_ids(&context);
    assert!(["work", "gr", "s0"].iter().all(|id| offered.contains(&id.to_string())), "{offered:?}");
    let by_id = |id: &str| context.events.iter().find(|e| e.id == id).unwrap();
    assert_eq!(by_id("work").history_until, Some(time_at("00:32+1")));
    assert_eq!(by_id("gr").history_until, None);
}

#[test]
fn the_work_runs_on_and_bedtime_moves_with_it() {
    let setup = staying_up();
    let n2 = setup.note_id(3);

    let planned = setup.dry_run(vec![
        EventDecision { end_note: Some(n2), ..keep("work") },
        EventDecision { event_id: Some("gr".into()), ..EventDecision::new("cancel") },
        EventDecision { start: Some(time_at("03:10+1")), ..keep("s0") },
    ]);

    let work = change(&planned, "work").after.clone().unwrap();
    assert_eq!((work.start, work.end), (Some(time_at("23:07")), Some(time_at("02:55+1"))));
    assert_eq!(change(&planned, "gr").action, "cancel");
    let night = change(&planned, "s0").after.clone().unwrap();
    assert_eq!((night.start, night.end), (Some(time_at("03:10+1")), Some(time_at("07:00+1"))));
}

#[test]
fn what_the_last_compaction_settled_stays_settled() {
    let cases = [
        (EventDecision { end: Some(time_at("00:20+1")), ..keep("work") }, "going on until"),
        (EventDecision { start: Some(time_at("23:30")), ..keep("work") }, "its start can't move"),
        (EventDecision { event_id: Some("work".into()), ..EventDecision::new("cancel") }, "can't be cancelled"),
    ];
    let setup = staying_up();

    for (decision, message) in cases {
        assert_eq!(categories(setup.try_dry_run(vec![decision], DryRun::default()), message), strings(&["compacted"]));
    }
}

#[test]
fn a_new_event_cant_reach_back_over_what_was_settled_before_the_window() {
    let setup = staying_up();

    let found = categories(
        setup.try_dry_run(
            vec![EventDecision { summary: Some("Snack".into()), start: Some(time_at("22:50")), end: Some(time_at("23:05")), ..EventDecision::new("create") }],
            DryRun::default(),
        ),
        "runs back over 'Dinner'",
    );

    assert!(found.contains(&"overlap".to_string()), "{found:?}");
}

// -- follow-through ------------------------------------------------------------------------
// An event the user says didn't happen counts against the follow-through
// of the people it was planned with: the timeline says so, and applying it
// records it.

fn follow_through() -> Setup {
    let mut events = day();
    events[0].facts = Some(facts(json!({"with": ["sam"]})));
    Setup::with(
        &[("09:05", "email")],
        Some(events),
        "11:30",
        Stores { people: sam(), traits: Some(vec![json!({"id": "reliable", "name": "Reliable", "status": "active", "parts": [{"kind": "follow_through"}]})]), ..Default::default() },
    )
}

fn cancel(event_id: &str) -> EventDecision {
    EventDecision { event_id: Some(event_id.into()), ..EventDecision::new("cancel") }
}

fn cancellations(setup: &Setup) -> Vec<(String, String, String)> {
    let text = |row: &crate::sheet::rows::Row, name: &str| row.get(name).and_then(Value::as_str).unwrap_or_default().to_string();
    setup.compactor.cancellations.as_ref().unwrap().all().unwrap().iter().map(crate::sheet::rows::RowItem::to_row).collect::<Vec<_>>().iter().map(|r| (text(r, "id"), text(r, "summary"), text(r, "source"))).collect()
}

#[test]
fn the_timeline_lists_whom_a_cancelled_event_counts_against() {
    let setup = follow_through();

    let planned = setup.dry_run(vec![cancel("e1")]);

    let timeline = planned.timeline.unwrap();
    assert!(timeline.text.contains("Follow-through:\n  ✗ Email: Me (Reliable), Sam (Reliable)\n"), "{}", timeline.text);
    assert!(timeline.text.contains("✗ counts against follow-through"));
    let email = timeline.events.iter().find(|e| e.event_id.as_deref() == Some("e1")).unwrap();
    assert_eq!(email.follow_through, strings(&["Me (Reliable)", "Sam (Reliable)"]));
    assert!(cancellations(&setup).is_empty()); // Nothing's recorded until it's applied.
}

#[test]
fn applying_it_records_the_cancellation_for_each_of_them() {
    let setup = follow_through();
    let id = setup.dry_run(vec![cancel("e1")]).compaction_id.unwrap();

    let applied = setup.commit(&id);

    assert!(applied.timeline.unwrap().text.contains("Follow-through:"));
    let source = format!("compaction {id}");
    assert_eq!(
        cancellations(&setup),
        vec![("e1/sam/with".to_string(), "Email".to_string(), source.clone()), ("e1/self/with".to_string(), "Email".to_string(), source)]
    );
}

#[test]
fn a_cancel_that_doesnt_count_is_neither_listed_nor_recorded() {
    let setup = follow_through();
    let planned = setup.dry_run(vec![EventDecision { counts_against_follow_through: Some(false), ..cancel("e1") }]);

    setup.commit(planned.compaction_id.as_ref().unwrap());

    assert!(!planned.timeline.unwrap().text.contains("Follow-through:"));
    assert!(cancellations(&setup).is_empty());
}

#[test]
fn the_proposal_tells_the_app_whether_a_cancel_counts_and_against_whom() {
    let setup = follow_through();
    let planned = setup.dry_run(vec![cancel("e1")]);

    let events = setup.get_proposal().events.unwrap();
    let email = events.iter().find(|e| e.id == "e1").unwrap();
    assert_eq!((email.status.as_str(), email.counts_against_follow_through), ("cancelled", Some(true)));
    assert_eq!(email.follow_through, strings(&["Me (Reliable)", "Sam (Reliable)"]));
    let report = events.iter().find(|e| e.id == "e2").unwrap();
    assert_eq!((report.counts_against_follow_through, report.follow_through.clone()), (None, vec![]));

    // The user flips it by cancelling it again.
    let flipped = setup.amend(planned.proposal_id.as_ref().unwrap(), 1, vec![EventDecision { counts_against_follow_through: Some(false), ..cancel("e1") }]);

    let events = flipped.events.unwrap();
    let email = events.iter().find(|e| e.id == "e1").unwrap();
    assert_eq!((email.counts_against_follow_through, email.follow_through.clone(), email.decided_by.as_deref()), (Some(false), vec![], Some("user")));
}

#[test]
fn a_merge_isnt_a_cancellation() {
    let setup = follow_through();
    let planned = setup.dry_run(vec![EventDecision { event_id: Some("e1".into()), into: Some("e2".into()), ..EventDecision::new("merge") }]);

    setup.commit(planned.compaction_id.as_ref().unwrap());

    assert!(!planned.timeline.unwrap().text.contains("Follow-through:"));
    assert!(cancellations(&setup).is_empty());
}

// -- priorities kept --------------------------------------------------------------------------
// What compaction settles keeps the priority its actions gave it then.

#[test]
fn a_compacted_event_is_given_its_actions_priority_as_its_own() {
    let mut events = day();
    for (i, event) in events.iter_mut().enumerate().take(3) {
        event.priority = if i == 1 { Some(3) } else { None }; // The report's own: kept.
        event.action_ids = Some(crate::testing::shares(&["mail"]));
    }
    let setup = Setup::with(
        &[("09:05", "email")],
        Some(events),
        "11:30",
        Stores { actions: vec![json!({"id": "mail", "name": "Do email", "status": "active", "priority": 1})], ..Default::default() },
    );

    let planned = setup.dry_run(vec![]);

    // Written even though it happened as planned.
    let email = change(&planned, "e1");
    assert_eq!(email.after.as_ref().unwrap().priority, Some(1));
    let (before, after) = (email.before.as_ref().unwrap(), email.after.as_ref().unwrap());
    assert_eq!((before.start, before.end), (after.start, after.end));
    // Its own priority: nothing to write.
    assert!(!planned.changes.iter().any(|c| c.event_id.as_deref() == Some("e2")));
    // Lunch, still to come, keeps following its actions.
    assert!(planned.changes.iter().filter(|c| c.event_id.as_deref() == Some("e3")).all(|c| c.after.as_ref().unwrap().priority.is_none()));
}
