//! AUTH_DISABLED is read when the server is built (see src/auth_mode.rs):
//! changing it rebuilds the server, and a build that skips sign-in -- or
//! a value that doesn't, though it looks meant to -- says so.

fn main() {
    println!("cargo:rerun-if-env-changed=AUTH_DISABLED");
    if let Ok(value) = std::env::var("AUTH_DISABLED") {
        if ["true", "1", "yes"].contains(&value.trim().to_lowercase().as_str()) {
            println!("cargo:warning=AUTH_DISABLED={value}: this build skips sign-in and trusts every request as the single user.");
        } else if !value.trim().is_empty() {
            println!("cargo:warning=AUTH_DISABLED={value} isn't true, 1 or yes: this build requires sign-in as usual.");
        }
    }
}
