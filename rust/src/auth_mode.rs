//! Whether this build skips sign-in: `AUTH_DISABLED`, read when the server
//! is built (`AUTH_DISABLED=true cargo build`), never when it runs, so a
//! binary built without it always requires WorkOS sign-in. A build with it
//! trusts every request as the single user -- for a corporate network,
//! behind a proxy that signs people in -- and says so wherever it can: in
//! its logs, the MCP handshake and get_health. It refuses to run on Render.

/// Whether `value` turns sign-in off: true, 1 or yes, in any case.
pub fn turns_off(value: Option<&str>) -> bool {
    value.is_some_and(|v| ["true", "1", "yes"].contains(&v.trim().to_lowercase().as_str()))
}

/// Whether this build skips sign-in.
pub fn disabled() -> bool {
    turns_off(option_env!("AUTH_DISABLED"))
}

/// The build's mode, as get_health reports it: "required" or "disabled".
pub fn name() -> &'static str {
    if disabled() {
        "disabled"
    } else {
        "required"
    }
}

/// What the logs say, at the start and for each new session, in a build
/// that skips sign-in.
pub const WARNING: &str = "AUTHENTICATION DISABLED: this build (AUTH_DISABLED) trusts every request as the single user. Run it only behind a proxy that signs people in.";

/// What a client is told as it connects to a build that skips sign-in.
pub const INSTRUCTIONS: &str =
    "Authentication is disabled in this build of the server (AUTH_DISABLED): it trusts every request as its single user, and relies on the network in front of it to sign people in.";

#[cfg(test)]
#[path = "auth_mode_tests.rs"]
mod tests;
