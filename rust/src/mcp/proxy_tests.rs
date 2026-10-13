//! The proxy check's settings and claims.

use super::*;
use serde_json::json;

const EMAIL: &str = "proxy@corp-project.iam.gserviceaccount.com";
const HOST: &str = "tracker.corp.example";
const AUDIENCE: &str = "//iam.googleapis.com/projects/123456/locations/global/workloadIdentityPools/proxy";
const NOW: i64 = 1_800_000_000;

fn vars(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
    let pairs: Vec<(String, String)> = pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
    move |name| pairs.iter().find(|(k, _)| k == name).map(|(_, v)| v.clone())
}

fn check() -> ProxyCheck {
    ProxyCheck { email: EMAIL.into(), host: HOST.into(), audience: AUDIENCE.into() }
}

/// A token with `changes` to the proxy's usual claims (null removes one),
/// its signature stripped as the proxy passes it on.
pub fn token(changes: Value) -> String {
    let mut claims = json!({"iss": "https://accounts.google.com", "aud": AUDIENCE, "email": EMAIL, "email_verified": true, "iat": NOW - 10, "exp": NOW + 3590, "sub": "1234"});
    for (k, v) in changes.as_object().unwrap() {
        if v.is_null() {
            claims.as_object_mut().unwrap().remove(k);
        } else {
            claims[k] = v.clone();
        }
    }
    let part = |v: &Value| base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(v.to_string());
    format!("Bearer {}.{}.", part(&json!({"alg": "RS256", "kid": "k1", "typ": "JWT"})), part(&claims))
}

#[test]
fn the_settings_are_all_or_nothing() {
    assert_eq!(ProxyCheck::from_vars(vars(&[])).unwrap(), None);
    let all = [("ALLOWED_PROXY_EMAIL", EMAIL), ("ALLOWED_HOST", "Tracker.Corp.Example:443"), ("PROXY_AUDIENCE", AUDIENCE)];
    assert_eq!(ProxyCheck::from_vars(vars(&all)).unwrap(), Some(check()));
    let refused = ProxyCheck::from_vars(vars(&all[..1])).err().unwrap().to_string();
    assert!(refused.contains("ALLOWED_HOST and PROXY_AUDIENCE isn't set"), "{refused}");
}

#[test]
fn a_request_through_the_proxy_passes() {
    assert_eq!(check().problem(Some(&token(json!({}))), Some(HOST), NOW), None);
    // Signed or not, with an audience list, the host in any case and with a
    // port, and the email in any case.
    let signed = format!("{}sig", token(json!({})));
    assert_eq!(check().problem(Some(&signed), Some("TRACKER.corp.example:443"), NOW), None);
    let listed = token(json!({"aud": ["other", AUDIENCE], "email": EMAIL.to_uppercase(), "email_verified": "true"}));
    assert_eq!(check().problem(Some(listed.trim_start_matches("Bearer ")), Some(HOST), NOW), None);
    assert_eq!(check().problem(Some(&token(json!({"iss": "accounts.google.com"}))), Some(HOST), NOW), None);
}

#[test]
fn anything_else_is_refused_saying_why() {
    let cases = [
        (Some(token(json!({}))), Some("tracker-abc.a.run.app"), "isn't ALLOWED_HOST"),
        (Some(token(json!({}))), None, "no Host"),
        (None, Some(HOST), "no X-Serverless-Authorization token"),
        (Some("Bearer not-a-jwt".into()), Some(HOST), "isn't a JWT"),
        (Some("Bearer a.%%%.".into()), Some(HOST), "isn't a JWT"),
        (Some(token(json!({"iss": "https://evil.example"}))), Some(HOST), "isn't Google's"),
        (Some(token(json!({"aud": "https://tracker-abc.a.run.app"}))), Some(HOST), "isn't for PROXY_AUDIENCE"),
        (Some(token(json!({"aud": null}))), Some(HOST), "isn't for PROXY_AUDIENCE"),
        (Some(token(json!({"exp": NOW - 61}))), Some(HOST), "has expired"),
        (Some(token(json!({"exp": null}))), Some(HOST), "has expired"),
        (Some(token(json!({"iat": NOW + 61}))), Some(HOST), "issued in the future"),
        (Some(token(json!({"email": "someone@corp.example"}))), Some(HOST), "isn't ALLOWED_PROXY_EMAIL"),
        (Some(token(json!({"email_verified": false}))), Some(HOST), "isn't verified"),
        (Some(token(json!({"email_verified": null}))), Some(HOST), "isn't verified"),
    ];
    for (token, host, why) in cases {
        let problem = check().problem(token.as_deref(), host, NOW).unwrap_or_default();
        assert!(problem.contains(why), "{why:?} isn't in {problem:?}");
    }
    // A clock a little off is fine.
    assert_eq!(check().problem(Some(&token(json!({"exp": NOW - 30, "iat": NOW + 30}))), Some(HOST), NOW), None);
}

#[test]
fn a_problem_never_holds_the_token() {
    let token = token(json!({"aud": "elsewhere"}));
    let problem = check().problem(Some(&token), Some(HOST), NOW).unwrap();
    assert!(!problem.contains(token.trim_start_matches("Bearer ").split('.').nth(1).unwrap()));
}
