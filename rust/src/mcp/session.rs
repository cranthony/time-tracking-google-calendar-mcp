//! A browser's sign-in, kept in a cookie (see the README's "Keeping the
//! web app signed in"): the tokens AuthKit issued it, sealed with a key
//! only this server has, from SESSION_SECRET. The cookie is HttpOnly, so
//! the page's scripts never see the tokens, and sealed, so nothing but a
//! server with the key can open it. The server keeps no sessions itself:
//! every instance with the key opens any cookie, and a new key signs
//! every browser out.

use base64::Engine;
use chacha20poly1305::aead::{Aead, AeadCore, KeyInit, OsRng};
use chacha20poly1305::{ChaCha20Poly1305, Key, Nonce};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};

pub const COOKIE: &str = "time_tracker_session";
/// How long a session lasts unused: a use within that extends it.
pub const DAYS: i64 = 7;
pub const MAX_AGE: i64 = DAYS * 24 * 60 * 60;
/// How old a cookie gets before a use re-issues it, extending the session.
pub const RENEW_AFTER: i64 = 60 * 60;
/// How soon before the access token's expiry it's refreshed.
pub const REFRESH_AHEAD: i64 = 30;
/// Fewer characters than this, and SESSION_SECRET is refused.
pub const MIN_SECRET_CHARS: usize = 32;
/// Bound to the ciphertext, so a sealed value is only ever read as this.
const PURPOSE: &[u8] = b"time_tracker_session/1";
const NONCE_LEN: usize = 12;

/// What a session cookie holds.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Session {
    #[serde(rename = "a")]
    pub access_token: String,
    #[serde(rename = "r")]
    pub refresh_token: String,
    /// The OAuth client the browser registered, which refreshes need.
    #[serde(rename = "c")]
    pub client_id: String,
    /// The signed-in user's id.
    #[serde(rename = "u")]
    pub user: String,
    /// When the cookie was issued, in seconds since the epoch.
    #[serde(rename = "i")]
    pub issued: i64,
    /// When the session ends however it's used, in seconds since the epoch.
    #[serde(rename = "e")]
    pub until: i64,
}

impl Session {
    /// A session issued at `now`, lasting [MAX_AGE].
    pub fn issued_at(mut self, now: i64) -> Session {
        self.issued = now;
        self.until = now + MAX_AGE;
        self
    }

    pub fn ended(&self, now: i64) -> bool {
        now >= self.until
    }

    /// Whether a use at `now` should re-issue the cookie, extending the
    /// session: not on every request, just hourly.
    pub fn due_for_renewal(&self, now: i64) -> bool {
        now - self.issued >= RENEW_AFTER
    }
}

/// The key sessions are sealed with: SESSION_SECRET, hashed.
#[derive(Clone)]
pub struct SessionKey([u8; 32]);

impl std::fmt::Debug for SessionKey {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("SessionKey(..)")
    }
}

impl PartialEq for SessionKey {
    fn eq(&self, other: &SessionKey) -> bool {
        self.0 == other.0
    }
}

impl SessionKey {
    /// The key from SESSION_SECRET: any text of [MIN_SECRET_CHARS] or more.
    pub fn from_secret(secret: &str) -> anyhow::Result<SessionKey> {
        let secret = secret.trim();
        if secret.chars().count() < MIN_SECRET_CHARS {
            anyhow::bail!(
                "SESSION_SECRET is too short: it needs at least {MIN_SECRET_CHARS} characters (any long random text; `openssl rand -base64 48` makes one). Set it, or unset it to turn off the web app's sign-in cookie -- see the README's \"Keeping the web app signed in\" section."
            );
        }
        Ok(SessionKey(Sha256::digest(secret.as_bytes()).into()))
    }

    /// `session` as a cookie value: a fresh nonce and the sealed JSON,
    /// base64url.
    pub fn seal(&self, session: &Session) -> String {
        let cipher = ChaCha20Poly1305::new(Key::from_slice(&self.0));
        let nonce = ChaCha20Poly1305::generate_nonce(&mut OsRng);
        let plain = serde_json::to_vec(session).expect("a session serializes");
        let sealed = cipher.encrypt(&nonce, chacha20poly1305::aead::Payload { msg: &plain, aad: PURPOSE }).expect("sealing can't fail");
        let mut bytes = nonce.to_vec();
        bytes.extend(sealed);
        base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(bytes)
    }

    /// The session a cookie value holds, if this key sealed it and it's
    /// untouched since.
    pub fn open(&self, value: &str) -> Option<Session> {
        let bytes = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(value.trim()).ok()?;
        if bytes.len() <= NONCE_LEN {
            return None;
        }
        let (nonce, sealed) = bytes.split_at(NONCE_LEN);
        let cipher = ChaCha20Poly1305::new(Key::from_slice(&self.0));
        let plain = cipher.decrypt(Nonce::from_slice(nonce), chacha20poly1305::aead::Payload { msg: sealed, aad: PURPOSE }).ok()?;
        serde_json::from_slice(&plain).ok()
    }
}

/// The Set-Cookie that keeps `value` for [MAX_AGE]: for the whole site,
/// hidden from scripts, over HTTPS (browsers make an exception for
/// localhost), and only on same-site requests.
pub fn set_cookie(value: &str) -> String {
    format!("{COOKIE}={value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age={MAX_AGE}")
}

/// The Set-Cookie that drops the cookie.
pub fn clear_cookie() -> String {
    format!("{COOKIE}=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0")
}

/// When a token (a JWT) expires, from its claims, unverified: the seconds
/// since the epoch, if it has one.
pub fn token_expiry(token: &str) -> Option<i64> {
    let payload = token.split('.').nth(1)?;
    let bytes = base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(payload.trim_end_matches('=')).ok()?;
    let claims: serde_json::Value = serde_json::from_slice(&bytes).ok()?;
    claims.get("exp").and_then(serde_json::Value::as_i64)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn key() -> SessionKey {
        SessionKey::from_secret("correct horse battery staple, and then some more").unwrap()
    }

    fn session() -> Session {
        Session { access_token: "at".into(), refresh_token: "rt".into(), client_id: "client_1".into(), user: "user_1".into(), issued: 1_000, until: 1_000 + MAX_AGE }
    }

    #[test]
    fn a_secret_must_be_long_enough() {
        assert!(SessionKey::from_secret("short").err().unwrap().to_string().contains("too short"));
        assert!(SessionKey::from_secret(&"x".repeat(MIN_SECRET_CHARS)).is_ok());
        // Hashed, so any text works, and the same text gives the same key.
        assert_eq!(key(), key());
        assert_ne!(key(), SessionKey::from_secret(&"y".repeat(MIN_SECRET_CHARS)).unwrap());
    }

    #[test]
    fn a_sealed_session_opens_with_its_key_only() {
        let sealed = key().seal(&session());

        assert_eq!(key().open(&sealed), Some(session()));
        assert!(!sealed.contains("at") || !sealed.contains("user_1"));
        assert_eq!(SessionKey::from_secret(&"y".repeat(MIN_SECRET_CHARS)).unwrap().open(&sealed), None);
        // Each sealing differs (a fresh nonce), and both open.
        let again = key().seal(&session());
        assert_ne!(sealed, again);
        assert_eq!(key().open(&again), Some(session()));
    }

    #[test]
    fn a_touched_or_malformed_value_does_not_open() {
        let sealed = key().seal(&session());
        let mut touched = sealed.clone();
        let last = touched.pop().unwrap();
        touched.push(if last == 'A' { 'B' } else { 'A' });

        assert_eq!(key().open(&touched), None);
        assert_eq!(key().open(""), None);
        assert_eq!(key().open("not base64!"), None);
        assert_eq!(key().open("AAAA"), None);
    }

    #[test]
    fn a_session_ends_and_renews_by_the_clock() {
        let session = session();

        assert!(!session.ended(1_000 + MAX_AGE - 1));
        assert!(session.ended(1_000 + MAX_AGE));
        assert!(!session.due_for_renewal(1_000 + RENEW_AFTER - 1));
        assert!(session.due_for_renewal(1_000 + RENEW_AFTER));
        let renewed = session.issued_at(5_000);
        assert_eq!((renewed.issued, renewed.until), (5_000, 5_000 + MAX_AGE));
    }

    #[test]
    fn the_cookies_keep_or_drop_the_value() {
        assert_eq!(set_cookie("v"), "time_tracker_session=v; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=604800");
        assert_eq!(clear_cookie(), "time_tracker_session=; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=0");
    }

    #[test]
    fn a_tokens_expiry_is_read_unverified() {
        let payload = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(br#"{"sub":"u","exp":1700000000}"#);
        assert_eq!(token_expiry(&format!("eyJhbGciOiJSUzI1NiJ9.{payload}.sig")), Some(1_700_000_000));
        assert_eq!(token_expiry("not-a-jwt"), None);
        assert_eq!(token_expiry("a.b.c"), None);
    }
}
