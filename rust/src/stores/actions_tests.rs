//! The module's tests, against the fake Google's calendar labels. Python
//! shrank MAX_LABELS for two tests; here the calendar is filled with other
//! apps' labels instead, leaving the same room.

use super::*;
use crate::sheet::{ACTIONS, ACTION_GROUPS};
use crate::stores::groups::{default_color, priority_label_id, PRIORITIES};
use crate::testing::app::{test_app, TestApp};
use crate::sheet::rows::{Row, RowItem};
use crate::write_lock::WRITE_LOCK;
use serde_json::{json, Value};

struct Fixture {
    t: TestApp,
    actions: Actions,
}

/// Actions on a calendar with one of Calendar's own unnamed labels,
/// which actions always leave alone.
fn fixture() -> Fixture {
    fixture_with_labels(vec![json!({"id": "default-1", "backgroundColor": "#039be5"})])
}

fn fixture_with_labels(labels: Vec<Value>) -> Fixture {
    let t = test_app();
    {
        let _held = WRITE_LOCK.hold();
        let client = t.app.calendar_client().unwrap();
        let (_, etag) = client.list_event_labels().unwrap();
        let labels: Vec<EventLabel> = labels.iter().map(|l| EventLabel::from_api(l).unwrap()).collect();
        client.replace_event_labels(&labels, etag.as_deref()).unwrap();
    }
    let actions = t.app.actions().unwrap();
    t.fake.clear_requests();
    Fixture { t, actions }
}

fn action(given: Value) -> Action {
    serde_json::from_value(given).unwrap()
}

fn group(given: Value) -> ActionGroup {
    serde_json::from_value(given).unwrap()
}

/// A typed result as JSON, to read as the tools' clients do.
fn json_of<T: serde::Serialize>(value: T) -> Value {
    serde_json::to_value(value).unwrap()
}

fn strings(items: &[&str]) -> Vec<String> {
    crate::testing::strings(items)
}

impl Fixture {
    fn held<T: serde::Serialize>(&self, f: impl FnOnce(&Actions) -> anyhow::Result<T>) -> anyhow::Result<Value> {
        let _held = WRITE_LOCK.hold();
        f(&self.actions).map(json_of)
    }

    fn create(&self, given: Value) -> String {
        self.held(|a| a.create_action(&action(given))).unwrap()["created_id"].as_str().unwrap().to_string()
    }

    fn group(&self, given: Value) -> String {
        self.held(|a| a.create_action_group(&group(given))).unwrap()["created_id"].as_str().unwrap().to_string()
    }

    fn update(&self, given: Value) -> Value {
        self.held(|a| a.update_action(&action(given), &[])).unwrap()
    }

    fn all(&self) -> Vec<Row> {
        self.actions.all().unwrap().iter().map(RowItem::to_row).collect()
    }

    fn labels(&self) -> Vec<Value> {
        self.t.fake.calendar_resource(&self.t.calendar_id).pointer("/labelProperties/eventLabels").and_then(Value::as_array).cloned().unwrap_or_default()
    }

    /// The calendar's named labels but the priorities', which every sync
    /// keeps: id -> (name, color).
    fn named(&self) -> Vec<(String, (String, String))> {
        let priorities: Vec<String> = PRIORITIES.iter().map(|p| priority_label_id(Some(*p))).collect();
        self.labels()
            .into_iter()
            .filter(|l| l.get("name").is_some_and(|n| !n.is_null()))
            .filter(|l| !priorities.contains(&l["id"].as_str().unwrap().to_string()))
            .map(|l| (l["id"].as_str().unwrap().to_string(), (l["name"].as_str().unwrap().to_string(), l["backgroundColor"].as_str().unwrap().to_string())))
            .collect()
    }

    fn names(&self) -> Vec<(String, String)> {
        self.named().into_iter().map(|(_, v)| v).collect()
    }

    fn label_writes(&self) -> usize {
        self.t.fake.requests().iter().filter(|r| r.method == "PATCH" && r.path.ends_with(&format!("/calendars/{}", self.t.calendar_id))).count()
    }

    fn label_of(&self, id: &str) -> String {
        self.all().iter().find(|a| rows::text(a, "id") == Some(id)).and_then(|a| rows::text(a, "label_id")).unwrap().to_string()
    }

    fn get(&self, id_or_name: &str) -> Value {
        json_of(self.actions.get_action(id_or_name).unwrap())
    }
}

fn refused<T>(result: anyhow::Result<T>, pattern: &str) {
    let Err(error) = result else { panic!("{pattern:?} wasn't refused") };
    assert!(regex::Regex::new(pattern).unwrap().is_match(&error.to_string()), "{pattern:?} isn't in: {error}");
}

fn ids(listed: &Value) -> Vec<String> {
    listed.as_array().unwrap().iter().map(|a| a["id"].as_str().unwrap().to_string()).collect()
}

// -- the tab --------------------------------------------------------------------------

#[test]
fn adds_an_empty_actions_tab_once() {
    let f = fixture();
    f.create(json!({"name": "Walk"}));

    let again = {
        let _held = WRITE_LOCK.hold();
        let (calendar, sheets) = f.t.app.clients().unwrap();
        Actions::ensure(&calendar, &sheets, &f.t.spreadsheet()).unwrap()
    };

    let names: Vec<String> = again.all().unwrap().into_iter().filter_map(|a| a.name).collect();
    assert_eq!(names, strings(&["Walk"]));
    assert!(f.t.fake.tab_titles(&f.t.spreadsheet()).contains(&ACTIONS.1.to_string()));
}

// -- creating ---------------------------------------------------------------------------

#[test]
fn assigns_an_id_and_label_id_and_is_proposed() {
    let f = fixture();

    let created = f.held(|a| a.create_action(&action(json!({"name": "Play guitar", "priority": 1, "note": "for fun"})))).unwrap();

    let all = f.all();
    assert_eq!(all.len(), 1);
    let id = rows::text(&all[0], "id").unwrap();
    assert_eq!((created["created_id"].as_str(), id.len()), (Some(id), 6));
    assert!(rows::text(&all[0], "label_id").is_some_and(|l| !l.is_empty()));
    assert_eq!(rows::text(&all[0], "status"), Some("proposed"));
    let changed = &created["changed"][0];
    assert_eq!(changed["name"], "Play guitar");
    assert_eq!(changed["effective_color"], default_color(Some(1)));
    assert_eq!(changed["holds_label"], true);
}

#[test]
fn refuses_a_color_that_isnt_hex() {
    for color in ["red", "#12345", "#1234567", "123456", "#ggg000", ""] {
        let f = fixture();

        refused(f.held(|a| a.create_action(&action(json!({"name": "Walk", "background_color": color})))), "isn't a color: give one as #rrggbb");

        assert!(f.all().is_empty(), "{color}");
    }
}

#[test]
fn refuses_a_color_that_isnt_hex_on_update_and_on_groups() {
    let f = fixture();
    let walk = f.create(json!({"name": "Walk"}));
    let outside = f.group(json!({"name": "Outside"}));

    refused(f.held(|a| a.update_action(&action(json!({"id": walk, "background_color": "blue"})), &[])), "isn't a color");
    refused(f.held(|a| a.create_action_group(&group(json!({"name": "Inside", "background_color": "blue"})))), "isn't a color");
    refused(f.held(|a| a.update_action_group(&group(json!({"id": outside, "background_color": "blue"})), &[])), "isn't a color");
    f.update(json!({"id": walk, "background_color": "#A4BDFC"}));
}

#[test]
fn ignores_an_id_or_label_id_given() {
    let f = fixture();

    let created = f.create(json!({"id": "mine", "label_id": "theirs", "name": "Walk"}));

    assert_ne!(created, "mine");
    assert_ne!(f.label_of(&created), "theirs");
}

#[test]
fn adds_its_label_to_the_calendar_leaving_others_alone() {
    let f = fixture_with_labels(vec![
        json!({"id": "default-1", "backgroundColor": "#039be5"}),
        json!({"id": "other", "name": "Someone else's", "backgroundColor": "#111111"}),
    ]);

    let walk = f.create(json!({"name": "Walk", "status": "active", "background_color": "#222222"}));

    let mut named = f.named();
    named.sort();
    let mut expected = vec![
        ("other".to_string(), ("Someone else's".to_string(), "#111111".to_string())),
        (f.label_of(&walk), ("Walk".to_string(), "#222222".to_string())),
    ];
    expected.sort();
    assert_eq!(named, expected);
    assert!(f.labels().iter().any(|l| l["id"] == "default-1"));
}

#[test]
fn refuses_a_name_already_used_whatever_its_case_or_status() {
    for name in ["walk", " WALK "] {
        let f = fixture();
        let walk = f.create(json!({"name": "Walk"}));
        f.update(json!({"id": walk, "status": "deleted"}));

        refused(f.held(|a| a.create_action(&action(json!({"name": name})))), r"already an action named 'Walk' \(.*, deleted\)");

        assert_eq!(f.all().len(), 1);
    }
}

#[test]
fn refuses_what_isnt_well_formed() {
    let cases = [
        (json!({}), "needs a name"),
        (json!({"name": "x".repeat(51)}), "longer than 50 characters"),
        // A status that isn't one can't be given: the schema refuses it,
        // and a cell holding one makes the tab unreadable.
    ];
    let f = fixture();

    for (given, message) in cases {
        refused(f.held(|a| a.create_action(&action(given))), message);
    }

    assert!(f.all().is_empty());
    assert_eq!(f.label_writes(), 0);
}

// -- labels ----------------------------------------------------------------------------------

#[test]
fn archiving_frees_the_label_and_reactivating_restores_the_same_one() {
    let f = fixture();
    let walk = f.create(json!({"name": "Walk", "status": "active"}));
    let label = f.label_of(&walk);

    f.update(json!({"id": walk, "status": "archived"}));
    assert!(f.named().is_empty());

    f.update(json!({"id": walk, "status": "active"}));
    assert_eq!(f.named().into_iter().map(|(id, _)| id).collect::<Vec<_>>(), vec![label]);
}

/// Other apps' labels, so that `room` labels are left for actions: the
/// unnamed label takes one, the priorities four.
fn crowded(room: usize) -> Fixture {
    let mut labels = vec![json!({"id": "default-1", "backgroundColor": "#039be5"})];
    labels.extend((0..MAX_LABELS as usize - 5 - room).map(|i| json!({"id": format!("other-{i}"), "name": format!("Other {i}"), "backgroundColor": "#000000"})));
    fixture_with_labels(labels)
}

#[test]
fn active_actions_beyond_the_calendars_room_are_refused() {
    let f = crowded(1);
    f.create(json!({"name": "Walk", "status": "active"}));

    refused(f.held(|a| a.create_action(&action(json!({"name": "Run", "status": "active"})))), "at most 1 actions can be active");
}

#[test]
fn proposed_actions_hold_labels_only_while_theres_room() {
    let f = crowded(2);
    let walk = f.create(json!({"name": "Walk"}));
    let run = f.create(json!({"name": "Run"}));
    let swim = f.create(json!({"name": "Swim"}));

    let listed = json_of(f.actions.get_actions(None).unwrap());
    let holds: Vec<(String, bool)> = listed["actions"].as_array().unwrap().iter().map(|a| (a["name"].as_str().unwrap().to_string(), a["holds_label"].as_bool().unwrap())).collect();
    assert_eq!(holds, vec![("Walk".to_string(), true), ("Run".to_string(), true), ("Swim".to_string(), false)]);

    // Activating Swim takes Run's label: active ones come first.
    let changes = f.update(json!({"id": swim, "status": "active"}));
    let mut changed: Vec<(String, bool)> = changes["changed"].as_array().unwrap().iter().map(|a| (a["id"].as_str().unwrap().to_string(), a["holds_label"].as_bool().unwrap())).collect();
    changed.sort();
    let mut expected = vec![(swim.clone(), true), (run.clone(), false)];
    expected.sort();
    assert_eq!(changed, expected);
    let mut names: Vec<String> = f.names().into_iter().map(|(n, _)| n).filter(|n| !n.starts_with("Other ")).collect();
    names.sort();
    assert_eq!(names, strings(&["Swim", "Walk"]));
    assert_eq!(changes["label_slots_used"], MAX_LABELS);

    // Archiving Walk gives Run its label back.
    let changes = f.update(json!({"id": walk, "status": "archived"}));
    let mut changed: Vec<(String, bool)> = changes["changed"].as_array().unwrap().iter().map(|a| (a["id"].as_str().unwrap().to_string(), a["holds_label"].as_bool().unwrap())).collect();
    changed.sort();
    let mut expected = vec![(walk, false), (run, true)];
    expected.sort();
    assert_eq!(changed, expected);
}

#[test]
fn renaming_and_recoloring_update_the_label() {
    let f = fixture();
    let walk = f.create(json!({"name": "Walk", "status": "active"}));

    f.update(json!({"id": walk, "name": "Stroll", "priority": 3}));

    assert_eq!(f.names(), vec![("Stroll".to_string(), default_color(Some(3)).to_string())]);
}

#[test]
fn an_unchanged_label_isnt_rewritten() {
    let f = fixture();
    let walk = f.create(json!({"name": "Walk", "status": "active"}));
    let writes = f.label_writes();

    f.update(json!({"id": walk, "note": "outside"}));

    assert_eq!(f.label_writes(), writes);
}

#[test]
fn a_concurrent_label_change_fails_loudly() {
    let f = fixture();
    let calendar = f.t.calendar_id.clone();
    f.t.fake.with(|s| s.fail_next.push(("PATCH".into(), format!("/calendars/{calendar}"), 412, "Precondition Failed".into())));

    let Err(error) = f.held(|a| a.create_action(&action(json!({"name": "Walk", "status": "active"})))) else { panic!("not refused") };

    assert!(crate::google::calendar::is_label_conflict(&error), "{error:#}");
}

// -- updating ----------------------------------------------------------------------------------

#[test]
fn sets_given_fields_and_keeps_the_rest() {
    let f = fixture();
    let outdoors = f.group(json!({"name": "Outdoors"}));
    let walk = f.create(json!({"name": "Walk", "priority": 1, "note": "outside", "group_id": outdoors}));

    let changes = f.update(json!({"id": walk, "status": "active", "note": "in the park"}));

    let all = f.all();
    let one = &all[0];
    assert_eq!(
        (rows::text(one, "name"), rows::text(one, "status"), one["priority"].as_i64(), rows::text(one, "note"), rows::text(one, "group_id")),
        (Some("Walk"), Some("active"), Some(1), Some("in the park"), Some(outdoors.as_str()))
    );
    assert_eq!(ids(&changes["changed"]), vec![walk]);
}

#[test]
fn clears_fields() {
    let f = fixture();
    let walk = f.create(json!({"name": "Walk", "priority": 1, "note": "outside", "background_color": "#123456"}));

    f.held(|a| a.update_action(&action(json!({"id": walk})), &strings(&["priority", "note", "background_color"]))).unwrap();

    let all = f.all();
    assert!(all[0]["priority"].is_null() && all[0]["note"].is_null() && all[0]["background_color"].is_null());
}

#[test]
fn never_changes_the_label_id() {
    let f = fixture();
    let walk = f.create(json!({"name": "Walk"}));
    let label = f.label_of(&walk);

    f.update(json!({"id": walk, "label_id": "other"}));

    assert_eq!(f.label_of(&walk), label);
}

#[test]
fn may_keep_its_own_name_in_another_case_but_not_another_actions() {
    let f = fixture();
    let walk = f.create(json!({"name": "Walk"}));
    let run = f.create(json!({"name": "Run"}));

    f.update(json!({"id": walk, "name": "walk"}));
    assert_eq!(f.get(&walk)["name"], "walk");

    refused(f.held(|a| a.update_action(&action(json!({"id": run, "name": "walk"})), &[])), "already an action named 'walk'");
}

#[test]
fn update_refuses_what_it_cant_do() {
    let cases: [(Value, &[&str], &str); 4] = [
        (json!({}), &[], "needs the action's id"),
        (json!({"id": "nope"}), &[], "no action with the id or name 'nope'"),
        (json!({"id": "x"}), &["name"], r"Can't clear \['name'\]"),
        (json!({"id": "x", "note": "n"}), &["note"], r"Can't both set and clear \['note'\]"),
    ];
    let f = fixture();

    for (given, clear, message) in cases {
        refused(f.held(|a| a.update_action(&action(given), &strings(clear))), message);
    }
}

// -- listing and finding ---------------------------------------------------------------------------

#[test]
fn lists_proposed_and_active_ones_by_default() {
    let f = fixture();
    f.create(json!({"name": "Walk"}));
    f.create(json!({"name": "Run", "status": "active"}));
    f.create(json!({"name": "Swim", "status": "archived"}));
    f.create(json!({"name": "Fly", "status": "deleted"}));

    let names = |listing: Value| -> Vec<String> { listing["actions"].as_array().unwrap().iter().map(|a| a["name"].as_str().unwrap().to_string()).collect() };
    assert_eq!(names(json_of(f.actions.get_actions(None).unwrap())), strings(&["Walk", "Run"]));
    assert_eq!(names(json_of(f.actions.get_actions(Some(&strings(&["archived", "deleted"]))).unwrap())), strings(&["Swim", "Fly"]));
}

#[test]
fn counts_label_slots() {
    let f = fixture();
    f.create(json!({"name": "Walk", "status": "active"}));
    f.create(json!({"name": "Swim", "status": "archived"}));

    let listing = json_of(f.actions.get_actions(None).unwrap());

    // Walk's, the unnamed label and the four priorities'.
    assert_eq!((listing["label_slots_used"].as_u64(), listing["label_slots_total"].as_u64()), (Some(6), Some(200)));
}

#[test]
fn refuses_an_unknown_status() {
    refused(fixture().actions.get_actions(Some(&strings(&["inactive"]))), "Unknown action status");
}

#[test]
fn finds_one_by_id_or_by_name_ignoring_case_and_suggests_close_matches() {
    let f = fixture();
    let walk = f.create(json!({"name": "Walk"}));
    let guitar = f.create(json!({"name": "Play guitar"}));

    assert_eq!(f.get(&walk)["name"], "Walk");
    assert_eq!(f.get("WALK")["id"], json!(walk));
    refused(f.actions.get_action("play guitr"), &format!(r"did you mean {guitar} \(Play guitar\)\?"));
}

// -- groups ----------------------------------------------------------------------------------------

fn groups(f: &Fixture) -> Vec<Value> {
    json_of(f.actions.get_action_groups().unwrap()).as_array().unwrap().clone()
}

#[test]
fn creates_groups_inside_groups() {
    let f = fixture();
    let creative = f.group(json!({"name": "Creative", "priority": 1}));
    let guitar = f.group(json!({"name": "Guitar", "group_id": creative}));

    let listed = groups(&f);
    let guitar_listed = listed.iter().find(|g| g["name"] == "Guitar").unwrap();

    assert_eq!(guitar_listed["path"], "Creative › Guitar");
    assert_eq!(guitar_listed["effective_priority"], 1);
    assert_eq!(json_of(f.actions.get_action_group("guitar").unwrap())["id"], json!(guitar));
    assert!(f.t.fake.tab_titles(&f.t.spreadsheet()).contains(&ACTION_GROUPS.1.to_string()));
}

#[test]
fn lists_enclosing_groups_first() {
    let f = fixture();
    let top = f.group(json!({"name": "Top"}));
    let inner = f.group(json!({"name": "Inner"}));
    f.held(|a| a.update_action_group(&group(json!({"id": inner, "group_id": top})), &[])).unwrap();
    f.group(json!({"name": "Other"}));

    let names: Vec<Value> = groups(&f).iter().map(|g| g["name"].clone()).collect();
    assert_eq!(names, vec![json!("Top"), json!("Inner"), json!("Other")]);
}

#[test]
fn actions_inherit_their_groups_color_and_priority() {
    let f = fixture();
    let creative = f.group(json!({"name": "Creative", "background_color": "#123456", "priority": 3}));
    let guitar = f.group(json!({"name": "Guitar", "group_id": creative}));
    let play = f.create(json!({"name": "Play guitar", "group_id": guitar, "status": "active"}));

    let listed = f.get(&play);

    assert_eq!((listed["path"].clone(), listed["effective_color"].clone(), listed["effective_priority"].clone()), (json!("Creative › Guitar › Play guitar"), json!("#123456"), json!(3)));
    assert_eq!(f.names(), vec![("Play guitar".to_string(), "#123456".to_string())]);
}

#[test]
fn recoloring_a_group_recolors_its_actions_labels() {
    let f = fixture();
    let creative = f.group(json!({"name": "Creative", "background_color": "#123456"}));
    let play = f.create(json!({"name": "Play guitar", "group_id": creative, "status": "active"}));
    f.create(json!({"name": "Walk", "status": "active", "background_color": "#000000"}));

    let changes = f.held(|a| a.update_action_group(&group(json!({"id": creative, "background_color": "#654321"})), &[])).unwrap();

    assert_eq!(ids(&changes["affected_actions"]), vec![play]);
    assert_eq!(changes["affected_actions"][0]["effective_color"], "#654321");
    assert!(f.names().contains(&("Play guitar".to_string(), "#654321".to_string())));
}

#[test]
fn a_group_may_share_an_actions_name_but_not_another_groups() {
    let f = fixture();
    f.create(json!({"name": "Cooking"}));
    f.group(json!({"name": "Cooking"}));

    refused(f.held(|a| a.create_action_group(&group(json!({"name": "cooking"})))), r"already an action group named 'Cooking' \(.*\); group names must");
}

#[test]
fn group_refuses_what_isnt_well_formed() {
    let f = fixture();

    refused(f.held(|a| a.create_action_group(&group(json!({})))), "needs a name");
    refused(f.held(|a| a.create_action_group(&group(json!({"name": "X", "group_id": "nope"})))), "is inside 'nope', which isn't an action group");
}

#[test]
fn refuses_a_group_inside_itself() {
    let f = fixture();
    let top = f.group(json!({"name": "Top"}));
    let inner = f.group(json!({"name": "Inner", "group_id": top}));

    refused(f.held(|a| a.update_action_group(&group(json!({"id": top, "group_id": inner})), &[])), "can't be inside itself");
}

#[test]
fn refuses_an_action_in_a_group_that_doesnt_exist() {
    refused(fixture().held(|a| a.create_action(&action(json!({"name": "Walk", "group_id": "nope"})))), "group_id 'nope' isn't an action group");
}

#[test]
fn group_clears_fields() {
    let f = fixture();
    let top = f.group(json!({"name": "Top"}));
    let inner = f.group(json!({"name": "Inner", "group_id": top, "note": "n"}));

    f.held(|a| a.update_action_group(&group(json!({"id": inner})), &strings(&["group_id", "note"]))).unwrap();

    let listed = json_of(f.actions.get_action_group(&inner).unwrap());
    assert!(listed["group_id"].is_null() && listed["note"].is_null());
}

#[test]
fn an_unknown_group_suggests_close_matches() {
    let f = fixture();
    let guitar = f.group(json!({"name": "Guitar"}));

    refused(f.actions.get_action_group("guitr"), &format!(r"did you mean {guitar} \(Guitar\)"));
    refused(f.held(|a| a.update_action_group(&group(json!({"id": "nope", "note": "x"})), &[])), "no action group");
}

#[test]
fn deleting_a_group_moves_its_actions_and_groups_up_to_its_parent() {
    let f = fixture();
    let creative = f.group(json!({"name": "Creative", "background_color": "#111111"}));
    let guitar = f.group(json!({"name": "Guitar", "group_id": creative, "background_color": "#222222"}));
    let lessons = f.group(json!({"name": "Lessons", "group_id": guitar}));
    let play = f.create(json!({"name": "Play guitar", "group_id": guitar, "status": "active"}));
    let sing = f.create(json!({"name": "Sing", "group_id": creative}));

    let deleted = f.held(|a| a.delete_action_group(&guitar, None)).unwrap();

    assert_eq!(deleted["deleted"]["name"], "Guitar");
    assert_eq!(ids(&deleted["changed"]), vec![lessons]);
    assert_eq!(deleted["changed"][0]["path"], "Creative › Lessons");
    assert_eq!(ids(&deleted["affected_actions"]), vec![play.clone()]);
    assert_eq!(f.get(&play)["group_id"], json!(creative));
    assert_eq!(f.get(&sing)["group_id"], json!(creative));
    let mut names: Vec<String> = groups(&f).iter().map(|g| g["name"].as_str().unwrap().to_string()).collect();
    names.sort();
    assert_eq!(names, strings(&["Creative", "Lessons"]));
    assert!(f.names().contains(&("Play guitar".to_string(), "#111111".to_string())));
}

#[test]
fn a_top_level_groups_contents_move_to_the_top() {
    let f = fixture();
    let top = f.group(json!({"name": "Top"}));
    let play = f.create(json!({"name": "Play", "group_id": top}));

    f.held(|a| a.delete_action_group(&top, None)).unwrap();

    assert!(f.get(&play)["group_id"].is_null());
    assert!(groups(&f).is_empty());
}

#[test]
fn deleting_a_group_needs_its_id() {
    let f = fixture();
    let top = f.group(json!({"name": "Top"}));

    refused(f.held(|a| a.delete_action_group("top", None)), &format!("by its id: 'Top' is {top}"));
    refused(f.held(|a| a.delete_action_group("nothing", None)), "no action group");
}

// -- the tree --------------------------------------------------------------------------------------

#[test]
fn the_tree_gives_priorities_names_and_labels() {
    let f = fixture();
    let food = f.group(json!({"name": "Food", "priority": 1}));
    let cook = f.create(json!({"name": "Cook", "group_id": food, "status": "active"}));

    let tree = f.actions.tree().unwrap();

    assert_eq!((tree.priority(&cook), tree.priority("nope")), (Some(1), None));
    assert_eq!((tree.name(&cook), tree.name("nope")), ("Cook".to_string(), "(unknown action nope)".to_string()));
    let label = f.label_of(&cook);
    assert_eq!(tree.action_for_label(Some(&label)).and_then(|a| a.id.as_deref()), Some(cook.as_str()));
}

#[test]
fn the_tree_checks_ids_for_events() {
    let f = fixture();
    let cook = f.create(json!({"name": "Cook"}));
    let gone = f.create(json!({"name": "Gone", "status": "deleted"}));
    let tree = f.actions.tree().unwrap();

    tree.check_action_ids(&[cook.clone()], &[]).unwrap();
    tree.check_action_ids(&[gone.clone()], &[gone.clone()]).unwrap();
    refused(tree.check_action_ids(&[gone], &[]), "Deleted actions can't be given to an event");
    refused(tree.check_action_ids(&strings(&["Cok"]), &[]), &format!(r"no action with the id or name 'Cok'; did you mean {cook} \(Cook\)"));
}

// -- priority colors ---------------------------------------------------------------------------------

#[test]
fn priority_colors_start_out_as_the_defaults_and_are_made_with_the_first_labels() {
    let f = fixture();
    let colors: Vec<(i64, String)> =
        json_of(f.actions.priority_colors().unwrap()).as_array().unwrap().iter().map(|c| (c["priority"].as_i64().unwrap(), c["color"].as_str().unwrap().to_string())).collect();
    assert_eq!(colors, PRIORITIES.iter().map(|p| (*p, default_color(Some(*p)).to_string())).collect::<Vec<_>>());

    f.create(json!({"name": "Walk", "status": "active"}));

    let labels = f.labels();
    for p in PRIORITIES {
        let label = labels.iter().find(|l| l["id"] == json!(priority_label_id(Some(p)))).unwrap();
        assert_eq!((label["name"].clone(), label["backgroundColor"].clone()), (json!(format!("Priority {p}")), json!(default_color(Some(p)))));
    }
}

#[test]
fn recoloring_a_priority_recolors_the_actions_that_take_its_color() {
    let f = fixture();
    let walk = f.create(json!({"name": "Walk", "status": "active", "priority": 1}));
    f.create(json!({"name": "Run", "status": "active", "priority": 1, "background_color": "#222222"}));
    f.create(json!({"name": "Read", "status": "active", "priority": 3}));

    let change = f.held(|a| a.set_priority_color(1, "#123456")).unwrap();

    let ones: Vec<Value> = change["colors"].as_array().unwrap().iter().filter(|c| c["priority"] == 1).map(|c| c["color"].clone()).collect();
    assert_eq!(ones, vec![json!("#123456")]);
    let affected: Vec<Value> = change["affected_actions"].as_array().unwrap().iter().map(|a| a["name"].clone()).collect();
    assert_eq!(affected, vec![json!("Walk")]);
    assert_eq!(change["affected_actions"][0]["effective_color"], "#123456");
    let labels = f.labels();
    let label = |id: &str| labels.iter().find(|l| l["id"] == json!(id)).map(|l| (l["name"].clone(), l["backgroundColor"].clone())).unwrap();
    assert_eq!(label(&priority_label_id(Some(1))), (json!("Priority 1"), json!("#123456")));
    assert_eq!(label(&f.label_of(&walk)), (json!("Walk"), json!("#123456")));
    assert!(f.names().contains(&("Run".to_string(), "#222222".to_string())));
    assert_eq!(f.get("Walk")["effective_color"], "#123456");
}

#[test]
fn refuses_what_isnt_a_priority_or_color() {
    let f = fixture();

    for (priority, color, message) in [(4, "#123456", "no priority 4"), (1, "red", "#rrggbb"), (1, "#12345", "#rrggbb")] {
        refused(f.held(|a| a.set_priority_color(priority, color)), message);
    }

    assert_eq!(f.label_writes(), 0);
}
