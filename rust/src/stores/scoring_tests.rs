//! The module's tests: checking scoring settings, and the defaults'
//! tab against the fake Google.

use super::*;
use crate::testing::app::{test_app, TestApp};
use serde_json::json;

fn defaults() -> (TestApp, ScoringDefaultsTab) {
    let t = test_app();
    let tab = t.app.scoring_defaults().unwrap();
    (t, tab)
}

fn set(tab: &ScoringDefaultsTab, given: Value, clear: &[&str]) -> anyhow::Result<ScoringDefaults> {
    let _held = crate::write_lock::WRITE_LOCK.hold();
    tab.set(&serde_json::from_value(given).unwrap(), &crate::testing::strings(clear))
}

fn ids(items: &[&str]) -> Vec<String> {
    crate::testing::strings(items)
}

#[test]
fn an_aggregation_takes_each_of_its_fields() {
    let full = json!({
        "statistic": "percentile",
        "percentile": 25,
        "weights": {"judgment": 2, "abc123": 0.5},
        "limit": 2,
        "limit_by": "worst",
        "cap_below": {"threshold": 40, "cap": 50},
        "missing": 0,
    });
    assert_eq!(aggregation_problems(&full, Some(&ids(&["judgment", "abc123"]))), Vec::<String>::new());
    assert_eq!(aggregation_problems(&json!({}), None), Vec::<String>::new());
    assert_eq!(aggregation_problems(&json!({"missing": "skip", "statistic": "mean"}), None), Vec::<String>::new());
}

#[test]
fn an_aggregation_says_whats_wrong() {
    let cases = [
        (json!([]), "must be an object"),
        (json!({"median": true}), "has no field \"median\""),
        (json!({"statistic": "min"}), "\"statistic\" must be \"mean\" or \"percentile\""),
        (json!({"statistic": "percentile"}), "needs \"percentile\""),
        (json!({"percentile": 50}), "only for \"statistic\": \"percentile\""),
        (json!({"statistic": "percentile", "percentile": 101}), "from 0 to 100"),
        (json!({"weights": [1]}), "\"weights\" must be an object"),
        (json!({"weights": {"a": -1}}), "'a''s weight must be a number, 0 or more"),
        (json!({"limit": 2}), "needs \"limit_by\""),
        (json!({"limit": 1.5, "limit_by": "best"}), "whole number"),
        (json!({"limit": 2, "limit_by": "recent"}), "\"limit_by\" must be \"best\" or \"worst\""),
        (json!({"limit_by": "best"}), "only for a \"limit\""),
        (json!({"cap_below": {"threshold": 40}}), "\"cap_below\" must be"),
        (json!({"missing": "zero"}), "\"missing\" must be"),
    ];
    for (value, said) in cases {
        let problems = aggregation_problems(&value, None);
        assert!(problems.iter().any(|p| p.contains(said)), "{value}: {said:?} isn't in {problems:?}");
    }
}

#[test]
fn a_traits_weights_name_its_parts() {
    let problems = aggregation_problems(&json!({"weights": {"gone": 2}}), Some(&ids(&["judgment"])));
    assert_eq!(problems, vec!["\"weights\" names 'gone', which isn't one of its parts (by id: judgment)".to_string()]);
    // Elsewhere, ids aren't checked.
    assert!(aggregation_problems(&json!({"weights": {"gone": 2}}), None).is_empty());
}

#[test]
fn a_judgment_parts_scoring_limits_by_events_or_hours() {
    let ok = [
        json!({"weight_by": "duration", "statistic": "percentile", "percentile": 50, "limit": 10.5, "limit_by": "best"}),
        json!({"limit": 5, "limit_by": "recent"}),
        json!({}),
    ];
    for value in ok {
        assert_eq!(judgment_scoring_problems(&value), Vec::<String>::new(), "{value}");
    }
    let cases = [
        (json!({"limit": 2.5, "limit_by": "best"}), "whole number"),
        (json!({"weight_by": "minutes"}), "\"weight_by\" must be \"event\" or \"duration\""),
        (json!({"limit": 2, "limit_by": "longest"}), "\"best\", \"worst\" or \"recent\""),
        (json!({"weights": {}}), "has no field \"weights\""),
    ];
    for (value, said) in cases {
        let problems = judgment_scoring_problems(&value);
        assert!(problems.iter().any(|p| p.contains(said)), "{value}: {said:?} isn't in {problems:?}");
    }
}

#[test]
fn the_defaults_start_as_none() {
    let (t, tab) = defaults();
    assert_eq!(tab.get().unwrap(), ScoringDefaults::default());
    assert!(t.fake.tab_titles(&t.spreadsheet()).contains(&SCORING_DEFAULTS.1.to_string()));
}

#[test]
fn sets_the_levels_given_and_keeps_the_rest() {
    let (_t, tab) = defaults();
    set(&tab, json!({"traits": {"statistic": "percentile", "percentile": 25}, "circles": {"limit": 3, "limit_by": "worst"}}), &[]).unwrap();
    let saved = set(&tab, json!({"habit_layers": {"cap_below": {"threshold": 40, "cap": 50}}}), &["circles"]).unwrap();

    let expected: ScoringDefaults = serde_json::from_value(json!({
        "traits": {"statistic": "percentile", "percentile": 25},
        "habit_layers": {"cap_below": {"threshold": 40, "cap": 50}},
    }))
    .unwrap();
    assert_eq!(saved, expected);
    assert_eq!(tab.get().unwrap(), expected);
}

#[test]
fn refuses_a_level_that_isnt_well_formed() {
    let (_t, tab) = defaults();
    let error = set(&tab, json!({"circles": {"statistic": "max"}}), &[]).unwrap_err().to_string();
    assert_eq!(error, "The scoring defaults: circles: \"statistic\" must be \"mean\" or \"percentile\"");
    let error = set(&tab, json!({"circles": {}}), &["circles"]).unwrap_err().to_string();
    assert!(error.contains("circles is both given and cleared"), "{error}");
    assert_eq!(tab.get().unwrap(), ScoringDefaults::default());
}
