//! The module's tests, against the fake Google: the stores' tabs and the
//! calendar seeded directly. (Python's requests also carried each part's
//! resolved facts and a framing sentence, which no tool returned; the Rust
//! port gives what the tools do -- each part once, and each person's
//! history -- so those tests check that instead.)

use super::*;
use crate::sheet::{ACTIONS, ACTION_GROUPS, HABITS, LOCATIONS, PEOPLE, TRAITS};
use crate::stores::action_calendar::ActionCalendar;
use crate::testing::app::{test_app, TestApp};
use crate::testing::facts;

const TONIGHT: &str = "e0001";

fn day() -> Moment {
    crate::event::parse_iso("2026-10-05T18:00:00+00:00", None).unwrap()
}

fn new_part() -> Value {
    json!({
        "kind": "judgment",
        "rubric": "Was this activity or place new?",
        "ratings": {"0": "routine", "1": "a twist", "2": "new", "3": "adventurous"},
        "facts": ["action", {"fact": "action_history", "lookback_days": 30}, "location", "location_history"],
    })
}

fn for_part() -> Value {
    json!({
        "kind": "judgment",
        "engagement_type": "for",
        "rubric": "Was it built on what matters to them?",
        "ratings": {"0": "no", "1": "yes"},
        "facts": ["action", "person_notes"],
    })
}

fn with_notes_part() -> Value {
    json!({
        "kind": "judgment",
        "rubric": "Were they heard?",
        "ratings": {"0": "no", "1": "yes"},
        "facts": ["person_notes", "general_notes", "what_matters"],
    })
}

fn traits() -> Vec<Value> {
    vec![
        json!({"id": "adventurous", "name": "Adventurous", "status": "active", "definition": "New things together.", "parts": [new_part()]}),
        json!({"id": "thoughtful", "name": "Thoughtful", "status": "active", "parts": [with_notes_part(), for_part(), {"kind": "follow_through"}]}),
        json!({"id": "off", "name": "Off", "status": "off", "parts": [new_part()]}),
    ]
}

fn people() -> Vec<Value> {
    vec![
        json!({"id": "self", "name": "Me", "status": "active"}),
        json!({"id": "sam", "name": "Sam", "status": "active", "context": "salsa", "what_matters": "- starts a new job"}),
        json!({"id": "mom", "name": "Mom", "status": "active"}),
    ]
}

fn event(id: &str, days_ago: i64) -> Event {
    let start = day() - Duration::days(days_ago);
    Event { id: Some(id.into()), summary: Some("Dinner".into()), start: Some(start), end: Some(start + Duration::hours(2)), ..Default::default() }
}

fn tonight() -> Event {
    Event {
        action_ids: Some(vec!["hike".into()]),
        description: Some("Long climb".into()),
        facts: Some(facts(json!({"locations": ["peak"], "with": ["sam"], "for": ["mom"], "notes": {"self": "tired", "sam": "loved it"}}))),
        ..event(TONIGHT, 0)
    }
}

fn span() -> (Moment, Moment) {
    (day(), day() + Duration::hours(2))
}

struct Setup {
    t: TestApp,
    judging: Judging,
}

impl Setup {
    fn new(events: &[Event], people: Vec<Value>, traits: Vec<Value>) -> Setup {
        let t = test_app();
        let app = &t.app;
        let judging = Judging {
            client: ActionCalendar::new(app.calendar_client().unwrap(), app.actions().unwrap()),
            actions: app.actions().unwrap(),
            people: app.people().unwrap(),
            locations: app.locations().unwrap(),
            traits: app.traits().unwrap(),
            habits: Some(app.habits().unwrap()),
        };
        t.seed(
            ACTIONS.1,
            &[json!({"id": "cook", "name": "Cook", "group_id": "food"}), json!({"id": "hike", "name": "Hike", "group_id": "outdoors"})],
        );
        t.seed(ACTION_GROUPS.1, &[json!({"id": "outdoors", "name": "Outdoors"}), json!({"id": "food", "name": "Food"})]);
        t.seed(PEOPLE.1, &people);
        t.seed(LOCATIONS.1, &[json!({"id": "home", "name": "Home"}), json!({"id": "peak", "name": "The peak"})]);
        t.seed(TRAITS.1, &traits);
        for event in events {
            t.add_event(event);
        }
        Setup { t, judging }
    }

    fn standard(events: &[Event]) -> Setup {
        Setup::new(events, people(), traits())
    }

    fn with_habits(self, habits: &[Value]) -> Setup {
        self.t.seed(HABITS.1, habits);
        self
    }

    fn requests(&self, event_ids: &[&str], include_judged: bool) -> Vec<JudgmentRequest> {
        self.judging.requests(&strings(event_ids), span(), include_judged).unwrap()
    }

    fn ids(&self) -> Vec<String> {
        self.requests(&[TONIGHT], false).into_iter().map(|r| r.id).collect()
    }

    fn judgments(&self, event_id: &str) -> Value {
        Value::Object(self.judging.client.get_event(event_id).unwrap().judgments.unwrap_or_default())
    }

    fn record(&self, requests: &[JudgmentRequest], judgments: &[(&str, i64, &str)]) -> anyhow::Result<usize> {
        let judgments: Vec<Judgment> =
            judgments.iter().map(|(id, rating, reasoning)| Judgment { request_id: id.to_string(), rating: *rating, reasoning: reasoning.to_string() }).collect();
        let _held = crate::write_lock::WRITE_LOCK.hold();
        self.judging.record(requests, &judgments)
    }
}

fn strings(items: &[&str]) -> Vec<String> {
    crate::testing::strings(items)
}

fn counts(pairs: &[(&str, i64)]) -> Vec<(String, i64)> {
    pairs.iter().map(|(k, v)| (k.to_string(), *v)).collect()
}

// -- requests ---------------------------------------------------------------------

#[test]
fn one_per_person_trait_and_judgment_part_by_engagement() {
    let s = Setup::standard(&[tonight()]);

    assert_eq!(
        s.ids(),
        strings(&[
            "e0001/self/adventurous/judgment",
            "e0001/self/thoughtful/judgment",
            "e0001/sam/adventurous/judgment",
            "e0001/sam/thoughtful/judgment",
            "e0001/mom/thoughtful/judgment#2", // Mom wasn't there: only "for" parts.
        ])
    );
}

#[test]
fn gives_each_part_once_with_its_rubric_ratings_engagement_and_facts() {
    let s = Setup::standard(&[tonight()]);
    let requests = s.requests(&[TONIGHT], false);

    let due = s.judging.due("c1", &requests).unwrap();

    let keys: Vec<&str> = due.parts.iter().map(|p| p.key.as_str()).collect();
    assert_eq!(keys, vec!["adventurous/judgment", "thoughtful/judgment", "thoughtful/judgment#2"]);
    let new = &due.parts[0];
    assert_eq!((new.trait_name.as_str(), new.engagement.as_str()), ("Adventurous", "with"));
    assert_eq!(new.definition, Some(json!("New things together.")));
    assert_eq!(new.rubric, json!("Was this activity or place new?"));
    assert_eq!(new.ratings, new_part()["ratings"]);
    assert_eq!(new.facts, strings(&["action", "action_history", "location", "location_history"]));
    assert_eq!(due.parts[2].engagement, "for");
    assert_eq!(
        due.events[0].people,
        vec![
            ("self".to_string(), strings(&["adventurous/judgment", "thoughtful/judgment"])),
            ("sam".to_string(), strings(&["adventurous/judgment", "thoughtful/judgment"])),
            ("mom".to_string(), strings(&["thoughtful/judgment#2"])),
        ]
    );
    assert_eq!(due.count(), 5);
}

#[test]
fn history_is_what_they_did_together_within_the_lookback() {
    let earlier = [
        Event { action_ids: Some(crate::testing::shares(&["cook"])), facts: Some(facts(json!({"locations": ["home"], "with": ["sam"]}))), ..event("e0002", 3) },
        Event { action_ids: Some(crate::testing::shares(&["cook"])), facts: Some(facts(json!({"locations": ["home"]}))), ..event("e0003", 5) }, // without Sam
        Event { action_ids: Some(crate::testing::shares(&["hike"])), facts: Some(facts(json!({"locations": ["peak"], "with": ["sam"]}))), ..event("e0004", 40) }, // too long ago
    ];
    let mut events = vec![tonight()];
    events.extend(earlier);
    let s = Setup::standard(&events);

    let due = s.judging.due("c1", &s.requests(&[TONIGHT], false)).unwrap();

    let history = |person: &str| due.history.iter().find(|(p, _)| p == person).map(|(_, h)| h.clone()).unwrap();
    let sam = history("sam");
    assert_eq!(sam.days, 30);
    assert_eq!(sam.actions, counts(&[("Cook", 1)]));
    assert_eq!(sam.locations, counts(&[("Home", 1)]));
    // The user was at all of them.
    assert_eq!(history("self").actions, counts(&[("Cook", 2)]));
}

#[test]
fn a_persons_traits_choose_and_replace() {
    let mut own = new_part();
    own["rubric"] = json!("New to Sam?");
    let people = vec![
        json!({"id": "self", "name": "Me", "status": "active", "traits": {"select": ["thoughtful"]}}),
        json!({"id": "sam", "name": "Sam", "status": "active", "traits": {"parts": {"adventurous": [own]}}}),
    ];
    let event = Event { facts: Some(facts(json!({"with": ["sam"]}))), ..tonight() };
    let s = Setup::new(&[event], people, traits());

    let requests = s.requests(&[TONIGHT], false);

    let ids: Vec<&str> = requests.iter().map(|r| r.id.as_str()).collect();
    assert_eq!(ids, vec!["e0001/self/thoughtful/judgment", "e0001/sam/adventurous/judgment", "e0001/sam/thoughtful/judgment"]);
    let due = s.judging.due("c1", &requests).unwrap();
    let sams = due.parts.iter().find(|p| p.key == "adventurous/judgment@sam").unwrap();
    assert_eq!(sams.rubric, json!("New to Sam?"));
}

#[test]
fn skips_events_without_facts_cancelled_ones_and_ones_not_asked_for() {
    let bare = Event { summary: Some("Email".into()), ..event("bare0", 0) };
    let gone = Event { id: Some("gone0".into()), status: Some("cancelled".into()), ..tonight() };
    let s = Setup::standard(&[tonight(), bare, gone]);

    assert!(s.requests(&["bare0", "gone0"], false).is_empty());
    assert!(s.requests(&[], false).is_empty());
}

#[test]
fn made_judgments_are_left_out_unless_redone() {
    let made = json!({"sam": {"adventurous": {"judgment": {"rating": 2, "scale": 3, "reasoning": "First climb."}}}});
    let event = Event { judgments: made.as_object().cloned(), ..tonight() };
    let s = Setup::standard(&[event]);

    assert!(!s.ids().contains(&"e0001/sam/adventurous/judgment".to_string()));
    let redo = s.requests(&[TONIGHT], true);
    let request = redo.iter().find(|r| r.id == "e0001/sam/adventurous/judgment").unwrap();
    assert_eq!(request.current, Some(made["sam"]["adventurous"]["judgment"].clone()));
}

// -- record -----------------------------------------------------------------------

#[test]
fn writes_each_judgment_beside_the_ones_made_with_its_scale() {
    let made = json!({"sam": {"thoughtful": {"judgment": {"rating": 1, "scale": 1, "reasoning": "Listened."}}}});
    let s = Setup::standard(&[Event { judgments: made.as_object().cloned(), ..tonight() }]);
    let requests = s.requests(&[TONIGHT], true);
    s.t.fake.clear_requests();

    let recorded = s
        .record(&requests, &[("e0001/sam/adventurous/judgment", 3, "  Their first   summit. "), ("e0001/self/adventurous/judgment", 2, "A new trail.")])
        .unwrap();

    assert_eq!(recorded, 2);
    let updates: Vec<String> = s.t.fake.requests().into_iter().filter(|r| r.path.contains("/events/") && r.method != "GET").map(|r| r.path).collect();
    assert_eq!(updates.len(), 1, "{updates:?}");
    assert_eq!(
        s.judgments(TONIGHT),
        json!({
            "sam": {
                "thoughtful": {"judgment": {"rating": 1, "scale": 1, "reasoning": "Listened."}},
                "adventurous": {"judgment": {"rating": 3, "scale": 3, "reasoning": "Their first summit."}},
            },
            "self": {"adventurous": {"judgment": {"rating": 2, "scale": 3, "reasoning": "A new trail."}}},
        })
    );
}

#[test]
fn refuses_what_doesnt_answer_a_request_before_writing_anything() {
    let long = "x".repeat(301);
    let cases: [(&str, i64, &str, &str); 4] = [
        ("e0001/x/y/z", 1, "r", "isn't one of the compaction's judgment requests"),
        ("e0001/sam/adventurous/judgment", 4, "r", "the rating 4 isn't one of its ratings (0, 1, 2, 3)"),
        ("e0001/sam/adventurous/judgment", 1, " ", "give a line of reasoning"),
        ("e0001/sam/adventurous/judgment", 1, &long, "keep the reasoning to one line"),
    ];
    let s = Setup::standard(&[tonight()]);
    let requests = s.requests(&[TONIGHT], false);
    s.t.fake.clear_requests();

    for (id, rating, reasoning, problem) in cases {
        let error = s.record(&requests, &[("e0001/self/adventurous/judgment", 1, "ok"), (id, rating, reasoning)]).unwrap_err();
        assert!(error.to_string().contains(problem), "{error}");
    }
    assert_eq!(s.t.fake.requests().into_iter().filter(|r| r.method != "GET").count(), 0);
    assert_eq!(s.judgments(TONIGHT), json!({}));
}

// -- habits -----------------------------------------------------------------------
// A habit is judged as a person there is, under `habit:<id>`, for an event
// in its scope: its action, or under its group.

#[test]
fn a_habit_is_judged_for_an_event_in_scope_by_its_group_or_its_action_but_not_out_of_it() {
    let s = Setup::standard(&[tonight()]).with_habits(&[
        json!({"id": "out", "name": "Get outside", "action_id": "outdoors", "status": "active"}),
        json!({"id": "hik", "name": "Hiking", "action_id": "hike", "status": "active"}),
        json!({"id": "ckg", "name": "Cooking", "action_id": "cook", "status": "active"}),
        json!({"id": "old", "name": "Old", "action_id": "hike", "status": "archived"}),
    ]);

    let ids = s.ids();

    assert!(ids.contains(&"e0001/habit:out/adventurous/judgment".to_string()), "{ids:?}");
    assert!(ids.contains(&"e0001/habit:hik/thoughtful/judgment".to_string()));
    assert!(!ids.iter().any(|i| i.contains("habit:ckg") || i.contains("habit:old")));
    // Always "with": never a "for" part.
    assert!(!ids.iter().any(|i| i.starts_with("e0001/habit:") && i.ends_with("#2")));
}

#[test]
fn a_habit_has_its_own_rubric() {
    let mut own = with_notes_part();
    own["rubric"] = json!("Did you pace yourself?");
    let s = Setup::standard(&[tonight()]).with_habits(&[json!({
        "id": "hik", "name": "Hiking", "action_id": "hike", "status": "active", "note": "Steady, not fast",
        "traits": {"select": ["thoughtful"], "parts": {"thoughtful": [own]}},
    })]);

    let requests = s.requests(&[TONIGHT], false);

    let ids: Vec<&str> = requests.iter().filter(|r| r.person_id == "habit:hik").map(|r| r.id.as_str()).collect();
    assert_eq!(ids, vec!["e0001/habit:hik/thoughtful/judgment"]);
    let due = s.judging.due("c1", &requests).unwrap();
    let part = due.parts.iter().find(|p| p.key == "thoughtful/judgment@habit:hik").unwrap();
    assert_eq!(part.rubric, json!("Did you pace yourself?"));
}

#[test]
fn a_habits_history_is_its_events_in_scope() {
    let before = Event { action_ids: Some(crate::testing::shares(&["hike"])), facts: Some(facts(json!({"locations": ["peak"]}))), ..event("e0000", 3) };
    let cooked = Event { action_ids: Some(crate::testing::shares(&["cook"])), facts: Some(facts(json!({"locations": ["home"]}))), ..event("c0000", 2) };
    let s = Setup::standard(&[before, cooked, tonight()])
        .with_habits(&[json!({"id": "out", "name": "Get outside", "action_id": "outdoors", "status": "active"})]);

    let due = s.judging.due("c1", &s.requests(&[TONIGHT], false)).unwrap();

    let (_, history) = due.history.iter().find(|(p, _)| p == "habit:out").unwrap();
    assert_eq!((history.days, history.actions.clone()), (30, counts(&[("Hike", 1)])));
}

#[test]
fn a_habits_judgments_are_recorded_on_the_event_under_its_id() {
    let s = Setup::standard(&[tonight()]).with_habits(&[json!({"id": "hik", "name": "Hiking", "action_id": "hike", "status": "active"})]);
    let requests = s.requests(&[TONIGHT], false);

    s.record(&requests, &[("e0001/habit:hik/adventurous/judgment", 1, "A new trail")]).unwrap();

    assert_eq!(s.judgments(TONIGHT)["habit:hik"], json!({"adventurous": {"judgment": {"rating": 1, "scale": 3, "reasoning": "A new trail"}}}));
}
