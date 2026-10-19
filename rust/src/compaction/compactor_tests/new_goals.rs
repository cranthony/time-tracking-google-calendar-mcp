//! New goals: compaction proposes goals from what the notes say the user
//! means to do, naming the people they're with or for -- new ones by
//! ref -- and they're created when the proposal is.

use super::*;

fn people() -> Vec<Value> {
    vec![json!({"id": "sam", "name": "Sam", "status": "active"})]
}

fn new_goals_setup() -> Setup {
    let goals = vec![json!({"id": "g1", "title": "Move house", "status": "active", "deadline": "2099-12-31"})];
    Setup::with(&[("09:05", "email"), ("10:20", "told Sam I'd help him move")], None, "11:30", Stores { people: people(), goals, ..Default::default() })
}

fn additions(given: Value) -> Additions {
    Additions::from_json_dict(Some(&given)).unwrap()
}

fn fence() -> Value {
    json!({"ref": "new:help-sam", "title": "Help Sam move", "deadline": "2099-06-01", "for_ids": ["sam"], "with_ids": ["new:alex"], "attribution_hints": "helping Sam move"})
}

fn proposed(setup: &Setup, given: Value) -> anyhow::Result<CompactionResult> {
    setup.try_dry_run(setup.email_then_report(), DryRun { additions: additions(given), ..Default::default() })
}

fn goal_titled(setup: &Setup, title: &str) -> crate::stores::goals::Goal {
    setup.compactor.goals.as_ref().unwrap().all().unwrap().into_iter().find(|g| g.title.as_deref() == Some(title)).expect("the goal")
}

fn person_id(setup: &Setup, name: &str) -> String {
    setup.compactor.people.as_ref().unwrap().get_person(name).unwrap().id.unwrap()
}

#[test]
fn prepare_lists_every_open_goal_with_its_people() {
    let goals = vec![
        json!({"id": "g1", "title": "Write a book", "status": "active", "deadline": "2099-12-31", "attribution_hints": "writing", "with_ids": ["sam"]}),
        json!({"id": "g2", "title": "Juggle", "status": "inactive"}),
        json!({"id": "g3", "title": "Run", "status": "done", "deadline": "2099-12-31"}),
    ];
    let setup = Setup::with(&[("09:05", "email")], None, "11:30", Stores { people: people(), goals, ..Default::default() });

    let context = setup.prepare().to_json();

    assert_eq!(
        context["goals"],
        json!([
            {"id": "g1", "title": "Write a book", "path": [], "status": "active", "deadline": "2099-12-31", "attribution_hints": "writing", "with_ids": ["sam"]},
            {"id": "g2", "title": "Juggle", "path": [], "status": "inactive", "deadline": null},
        ])
    );
    assert!(DECISION_GUIDE.contains("NEW GOALS: when a note says the user wants"));
}

#[test]
fn a_new_goal_is_created_active_with_its_people_when_the_proposal_is() {
    let setup = new_goals_setup();
    let both = json!({"people": [{"ref": "new:alex", "name": "Alex"}], "goals": [fence()]});

    let result = proposed(&setup, both).unwrap();
    let p = result.proposal_id.unwrap();
    assert!(setup.get_proposal().additions.unwrap().contains_key("goals"));
    assert_eq!(setup.prepare().proposal.unwrap().new_goals.len(), 1);
    assert_eq!(setup.call(|c| c.confirm(&p, 1)).unwrap().status, "applied");

    let made = goal_titled(&setup, "Help Sam move");
    assert_eq!(made.status, Some(crate::stores::goals::GoalStatus::Active));
    assert_eq!((made.for_ids, made.with_ids), (Some(strings(&["sam"])), Some(vec![person_id(&setup, "alex")])));
    assert_eq!(made.attribution_hints.as_deref(), Some("helping Sam move"));
}

#[test]
fn a_new_goal_with_the_title_of_an_open_one_is_that_one() {
    let setup = new_goals_setup();

    let p = proposed(&setup, json!({"goals": [{"ref": "new:move", "title": "move house", "deadline": "2099-06-01"}]})).unwrap().proposal_id.unwrap();
    setup.call(|c| c.confirm(&p, 1)).unwrap();

    assert_eq!(setup.compactor.goals.as_ref().unwrap().all().unwrap().len(), 1);
}

#[test]
fn new_goals_naming_strangers_or_breaking_the_goal_rules_are_refused() {
    let setup = new_goals_setup();

    refused(proposed(&setup, json!({"goals": [fence()]})), r"'new:help-sam' names \['new:alex'\], who aren't people");
    refused(proposed(&setup, json!({"goals": [{"ref": "new:x", "title": "Someday"}]})), "is active, so it needs a deadline");
    refused(
        proposed(&setup, json!({"goals": [{"ref": "new:x", "title": "Both", "deadline": "2099-06-01", "with_ids": ["sam"], "for_ids": ["sam"]}]})),
        r"names \['sam'\] both with",
    );
    refused(proposed(&setup, json!({"goals": [{"ref": "new:x", "title": "Late", "deadline": "2100-06-01", "parent_id": "g1"}]})), "is after its parent");
}

#[test]
fn a_goal_settled_now_waits_for_the_new_people_it_names() {
    let setup = new_goals_setup();
    let both = json!({"people": [{"ref": "new:alex", "name": "Alex"}], "goals": [fence()]});
    let p = proposed(&setup, both).unwrap().proposal_id.unwrap();
    let settle = |revision: i64, choices: Vec<Value>| {
        setup.try_amend(&p, revision, Amend { additions: choices.iter().map(AdditionChoice::from_value).collect(), ..Default::default() })
    };

    refused(settle(1, vec![json!({"ref": "new:help-sam", "use": "create"})]), r"names \['new:alex'\]: settle them first");
    // Settled together, the person is made first.
    let proposal = settle(1, vec![json!({"ref": "new:help-sam", "use": "create", "name": "Help Sam pack"}), json!({"ref": "new:alex", "use": "create"})]).unwrap();

    let made = goal_titled(&setup, "Help Sam pack");
    assert_eq!(made.with_ids, Some(vec![person_id(&setup, "alex")]));
    assert!(proposal.settled_additions.iter().any(|s| s.ref_ == "new:help-sam" && s.id == made.id));
}

#[test]
fn a_dropped_person_is_left_out_of_the_new_goal() {
    let setup = new_goals_setup();
    let both = json!({"people": [{"ref": "new:alex", "name": "Alex"}], "goals": [fence()]});
    let p = proposed(&setup, both).unwrap().proposal_id.unwrap();

    setup.try_amend(&p, 1, Amend { additions: vec![AdditionChoice::from_value(&json!({"ref": "new:alex", "use": "drop"}))], ..Default::default() }).unwrap();
    setup.call(|c| c.confirm(&p, 2)).unwrap();

    let made = goal_titled(&setup, "Help Sam move");
    assert_eq!((made.with_ids, made.for_ids), (None, Some(strings(&["sam"]))));
}
