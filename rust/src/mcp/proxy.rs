//! The proxy check: every request must come through the IAP-style proxy
//! in front of the server -- its OIDC token in X-Serverless-Authorization
//! naming ALLOWED_PROXY_EMAIL, minted for PROXY_AUDIENCE, by Google, and
//! the Host it forwards, ALLOWED_HOST.
//!
//! The proxy strips the token's signature before passing it on, so the
//! server can't verify it: it checks the claims. That's only as strong as
//! the guarantee that nothing but the proxy can reach the server -- Cloud
//! Run's ingress and invoker IAM have to make sure of that (see the
//! README's "Behind an IAP proxy").

use base64::Engine;
use serde_json::Value;

/// The token's issuers: Google's, with and without a scheme.
const ISSUERS: [&str; 2] = ["https://accounts.google.com", "accounts.google.com"];
/// How far off the proxy's clock may be from this server's, in seconds.
const LEEWAY_SECONDS: i64 = 60;
pub const TOKEN_HEADER: &str = "x-serverless-authorization";
/// The settings, all three needed once any is set.
pub const VARS: [&str; 3] = ["ALLOWED_PROXY_EMAIL", "ALLOWED_HOST", "PROXY_AUDIENCE"];

#[derive(Debug, Clone, PartialEq)]
pub struct ProxyCheck {
    pub email: String,
    pub host: String,
    pub audience: String,
}

impl ProxyCheck {
    /// The check `get`'s variables ask for: none if none is set, or an
    /// error naming those missing if only some are.
    pub fn from_vars(get: impl Fn(&str) -> Option<String>) -> anyhow::Result<Option<ProxyCheck>> {
        let values: Vec<Option<String>> = VARS.iter().map(|name| get(name).map(|v| v.trim().to_string()).filter(|v| !v.is_empty())).collect();
        if values.iter().all(Option::is_none) {
            return Ok(None);
        }
        let missing: Vec<&str> = VARS.iter().zip(&values).filter(|(_, v)| v.is_none()).map(|(name, _)| *name).collect();
        if !missing.is_empty() {
            anyhow::bail!(
                "The proxy check needs ALLOWED_PROXY_EMAIL, ALLOWED_HOST and PROXY_AUDIENCE together, but {} isn't set -- see the README's \"Behind an IAP proxy\" section.",
                missing.join(" and ")
            );
        }
        let [email, host, audience] = [values[0].clone().unwrap(), values[1].clone().unwrap(), values[2].clone().unwrap()];
        Ok(Some(ProxyCheck { email, host: bare_host(&host).to_lowercase(), audience }))
    }

    /// What's wrong with a request carrying `token` (X-Serverless-
    /// Authorization) and `host` at `now` (seconds since the epoch), if
    /// anything: why it didn't come through the proxy. Never the token
    /// itself.
    pub fn problem(&self, token: Option<&str>, host: Option<&str>, now: i64) -> Option<String> {
        match host.map(|h| bare_host(h).to_lowercase()) {
            None => return Some("no Host".into()),
            Some(h) if h != self.host => return Some(format!("Host {h:?} isn't ALLOWED_HOST")),
            _ => {}
        }
        let Some(token) = token else {
            return Some("no X-Serverless-Authorization token".into());
        };
        let claims = match claims_of(token) {
            Ok(claims) => claims,
            Err(why) => return Some(format!("the X-Serverless-Authorization token {why}")),
        };
        let text = |name: &str| claims.get(name).and_then(Value::as_str);
        if !text("iss").is_some_and(|iss| ISSUERS.contains(&iss)) {
            return Some("the token isn't Google's (iss)".into());
        }
        let audience_matches = match claims.get("aud") {
            Some(Value::String(aud)) => *aud == self.audience,
            Some(Value::Array(auds)) => auds.iter().any(|a| a.as_str() == Some(self.audience.as_str())),
            _ => false,
        };
        if !audience_matches {
            return Some("the token isn't for PROXY_AUDIENCE (aud)".into());
        }
        match claims.get("exp").and_then(Value::as_i64) {
            Some(exp) if exp + LEEWAY_SECONDS > now => {}
            _ => return Some("the token has expired (exp)".into()),
        }
        if claims.get("iat").and_then(Value::as_i64).is_some_and(|iat| iat - LEEWAY_SECONDS > now) {
            return Some("the token was issued in the future (iat)".into());
        }
        if !text("email").is_some_and(|email| email.eq_ignore_ascii_case(&self.email)) {
            return Some(format!("the token's email {:?} isn't ALLOWED_PROXY_EMAIL", text("email").unwrap_or_default()));
        }
        let verified = match claims.get("email_verified") {
            Some(Value::Bool(b)) => *b,
            Some(Value::String(s)) => s == "true",
            _ => false,
        };
        if !verified {
            return Some("the token's email isn't verified (email_verified)".into());
        }
        None
    }
}

/// `host` without a port.
fn bare_host(host: &str) -> &str {
    let host = host.trim();
    if host.starts_with('[') {
        // An IPv6 literal: [::1]:8080.
        return host.split_once(']').map(|(h, _)| &host[..h.len() + 1]).unwrap_or(host);
    }
    host.rsplit_once(':').filter(|(_, port)| port.chars().all(|c| c.is_ascii_digit())).map(|(h, _)| h).unwrap_or(host)
}

/// The claims of `token` ("Bearer header.payload[.signature]", the
/// signature usually stripped), unverified -- or what's wrong with it.
fn claims_of(token: &str) -> Result<serde_json::Map<String, Value>, &'static str> {
    let token = token.trim();
    let token = token.strip_prefix("Bearer ").or_else(|| token.strip_prefix("bearer ")).unwrap_or(token).trim();
    let mut parts = token.split('.');
    let (Some(_header), Some(payload)) = (parts.next(), parts.next()) else {
        return Err("isn't a JWT");
    };
    let bytes = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(payload.trim_end_matches('=')).map_err(|_| "isn't a JWT")?;
    match serde_json::from_slice(&bytes) {
        Ok(Value::Object(claims)) => Ok(claims),
        _ => Err("isn't a JWT"),
    }
}

#[cfg(test)]
#[path = "proxy_tests.rs"]
mod tests;
