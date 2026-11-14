//! The module's tests, against the fake Google.

use super::*;
use crate::testing::fake_google::FakeGoogle;
use crate::write_lock::WRITE_LOCK;
use serde_json::{json, Value};

struct Fixture {
    _fake: FakeGoogle,
    examples: TraitExamples,
    trait_ids: Vec<String>,
    person_ids: Vec<String>,
}

fn fixture() -> Fixture {
    let fake = FakeGoogle::start();
    let sheets = SheetsClient::new(fake.google());
    let _held = WRITE_LOCK.hold();
    let spreadsheet = sheets.create_spreadsheet("Calendar Metadata").unwrap();
    let examples = TraitExamples::ensure(&sheets, &spreadsheet).unwrap();
    Fixture { _fake: fake, examples, trait_ids: strings(&["generous", "brave"]), person_ids: strings(&["self", "p1", "c1"]) }
}

fn strings(items: &[&str]) -> Vec<String> {
    crate::testing::strings(items)
}

fn given(value: Value) -> TraitExample {
    serde_json::from_value(value).unwrap()
}

fn story(extra: Value) -> Value {
    let mut out = json!({"trait_id": "generous", "when": "2026-11-02", "person_id": "p1", "what": "Drove across town with soup.", "why": "Gave an evening to someone who needed it."});
    for (k, v) in extra.as_object().unwrap() {
        out[k] = v.clone();
    }
    out
}

fn refused<T: std::fmt::Debug>(result: anyhow::Result<T>, pattern: &str) {
    let Err(error) = result else { panic!("{pattern:?} wasn't refused: {result:?}") };
    assert!(regex::Regex::new(pattern).unwrap().is_match(&error.to_string()), "{pattern:?} isn't in: {error}");
}

impl Fixture {
    fn known(&self) -> Known<'_> {
        Known { trait_ids: &self.trait_ids, person_ids: &self.person_ids }
    }

    fn create(&self, value: Value) -> anyhow::Result<TraitExample> {
        let _held = WRITE_LOCK.hold();
        self.examples.create_trait_example(&given(value), &self.known())
    }

    fn update(&self, value: Value, clear: &[&str]) -> anyhow::Result<TraitExample> {
        let _held = WRITE_LOCK.hold();
        self.examples.update_trait_example(&given(value), &strings(clear), &self.known())
    }

    fn ids(&self, trait_id: Option<&str>, event_ids: Option<&[&str]>) -> Vec<String> {
        let event_ids = event_ids.map(strings);
        self.examples.get_trait_examples(trait_id, event_ids.as_deref()).unwrap().into_iter().map(|e| e.id.unwrap()).collect()
    }
}

#[test]
fn creates_and_lists_newest_first() {
    let f = fixture();

    let a = f.create(story(json!({"when": "2026-10-01", "event_id": "e1"}))).unwrap();
    let b = f.create(story(json!({"when": "2026-11-05", "trait_id": "brave", "person_id": null, "who": "a stranger on the train"}))).unwrap();
    let c = f.create(story(json!({"when": "2026-10-01", "person_id": "self"}))).unwrap();

    assert!(a.id.is_some());
    assert_eq!(f.ids(None, None), [b.id.clone().unwrap(), c.id.clone().unwrap(), a.id.clone().unwrap()]);
    assert_eq!(f.ids(Some("generous"), None), [c.id.unwrap(), a.id.clone().unwrap()]);
    assert_eq!(f.ids(None, Some(&["e1", "e9"])), [a.id.unwrap()]);
    assert_eq!(f.examples.all().unwrap()[1].who.as_deref(), Some("a stranger on the train"));
}

#[test]
fn a_create_sent_again_with_its_own_id_is_made_once() {
    let f = fixture();

    let first = f.create(story(json!({"id": "my-story"}))).unwrap();
    let again = f.create(story(json!({"id": "my-story"}))).unwrap();

    assert_eq!(first.id, again.id);
    assert_eq!(f.examples.all().unwrap().len(), 1);
}

#[test]
fn refuses_what_isnt_well_formed_saying_why() {
    let f = fixture();
    let cases = [
        (json!({"trait_id": "kind"}), "its trait_id 'kind' isn't a trait"),
        (json!({"trait_id": null}), "needs a trait_id"),
        (json!({"when": "Nov 2"}), r#"its when 'Nov 2' isn't a date"#),
        (json!({"when": null}), "needs a when"),
        (json!({"person_id": null}), r#"needs who did it: a person_id \("self" for the user\), or who"#),
        (json!({"person_id": "p9"}), "its person_id 'p9' isn't a person or a community"),
        (json!({"who": " "}), "its who must be non-empty text"),
        (json!({"what": ""}), "needs a what: what they did"),
        (json!({"why": "x".repeat(2001)}), "its why is 2001 characters; it can be at most 2000"),
        (json!({"event_id": ""}), "its event_id must be an event's id"),
    ];
    for (change, pattern) in cases {
        refused(f.create(story(change)), &format!("^The trait example: .*{pattern}"));
    }
    assert!(f.examples.all().unwrap().is_empty());
}

#[test]
fn updates_by_version_and_clears() {
    let f = fixture();
    let made = f.create(story(json!({"event_id": "e1", "who": "my neighbour"}))).unwrap();
    let id = made.id.clone().unwrap();

    let updated = f.update(json!({"id": id, "why": "Made time when it was scarce.", "version": 1}), &["event_id", "person_id"]).unwrap();

    assert_eq!((updated.why.as_deref(), updated.event_id.as_deref(), updated.person_id.as_deref()), (Some("Made time when it was scarce."), None, None));
    assert_eq!(updated.what, made.what);
    refused(f.update(json!({"id": id, "what": "x", "version": 1}), &[]), "changed after version 1 was read");
    // Who did it is still needed.
    refused(f.update(json!({"id": id, "version": 2}), &["who"]), "needs who did it");
    refused(f.update(json!({"id": id, "version": 2}), &["what"]), "Can't clear");
    refused(f.update(json!({"id": "nope", "what": "x"}), &[]), "There's no trait example with the id 'nope'");
}

#[test]
fn deletes_unless_changed_since() {
    let f = fixture();
    let id = f.create(story(json!({}))).unwrap().id.unwrap();
    f.update(json!({"id": id, "what": "More soup.", "version": 1}), &[]).unwrap();

    let _held = WRITE_LOCK.hold();
    refused(f.examples.delete_trait_example(&id, Some(1)), "changed after version 1 was read");
    let deleted = f.examples.delete_trait_example(&id, Some(2)).unwrap();

    assert_eq!(deleted.what.as_deref(), Some("More soup."));
    assert!(f.examples.all().unwrap().is_empty());
    // Gone already, it's done.
    assert_eq!(f.examples.delete_trait_example(&id, Some(2)).unwrap().id, Some(id));
}
