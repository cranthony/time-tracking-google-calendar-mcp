//! The module's tests. They pass in either build; CI runs them in both.

use super::*;

#[test]
fn true_1_and_yes_in_any_case_turn_sign_in_off() {
    for value in ["true", "TRUE", "True", "1", "yes", "YES", " true "] {
        assert!(turns_off(Some(value)), "{value:?}");
    }
    for value in [None, Some(""), Some("false"), Some("0"), Some("no"), Some("on"), Some("truee")] {
        assert!(!turns_off(value), "{value:?}");
    }
}

#[test]
fn the_build_says_which_it_is() {
    assert_eq!(disabled(), turns_off(option_env!("AUTH_DISABLED")));
    assert_eq!(name(), if disabled() { "disabled" } else { "required" });
}
