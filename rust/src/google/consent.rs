//! The OAuth consent flow for a missing or dead token, as
//! `google_auth_oauthlib`'s `InstalledAppFlow.run_local_server(port=0)`
//! runs it for `load_credentials`: a listener on a free local port as the
//! redirect, the browser opened to Google's consent page (the address
//! printed too), the code that comes back exchanged for a token -- with
//! PKCE -- and the token written to `token.json` in google-auth's format.
//! Only the command-line tools run it: the server is never interactive.

use super::SCOPES;
use anyhow::{anyhow, bail, Context};
use base64::Engine;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::path::Path;

const DONE: &str = "The authentication flow has completed. You may close this window.";

fn random_text() -> String {
    format!("{}{}", uuid::Uuid::new_v4().simple(), uuid::Uuid::new_v4().simple())
}

fn encode(text: &str) -> String {
    let mut out = String::new();
    for byte in text.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => out.push(byte as char),
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

fn decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out: Vec<u8> = Vec::new();
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).unwrap_or("");
                match u8::from_str_radix(hex, 16) {
                    Ok(b) => out.push(b),
                    Err(_) => out.extend_from_slice(&bytes[i..i + 3]),
                }
                i += 3;
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            b => {
                out.push(b);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn open_browser(url: &str) {
    let opened = if cfg!(target_os = "windows") {
        std::process::Command::new("cmd").args(["/C", "start", "", url]).spawn()
    } else if cfg!(target_os = "macos") {
        std::process::Command::new("open").arg(url).spawn()
    } else {
        std::process::Command::new("xdg-open").arg(url).spawn()
    };
    if opened.is_err() {
        eprintln!("(Couldn't open a browser: open the address above yourself.)");
    }
}

/// Run the consent flow with the client in `credentials_path`, and write
/// the token it gets to `token_path`.
pub fn consent(token_path: &Path, credentials_path: &Path) -> anyhow::Result<()> {
    let text = std::fs::read_to_string(credentials_path)
        .with_context(|| format!("No OAuth client secret file at {} (see the README's \"Google OAuth credentials\")", credentials_path.display()))?;
    let secrets: Value = serde_json::from_str(&text).context("the client secret file isn't JSON")?;
    let client = secrets.get("installed").or_else(|| secrets.get("web")).ok_or_else(|| anyhow!("the client secret file has no \"installed\" client"))?;
    let field = |key: &str| client.get(key).and_then(Value::as_str).map(str::to_string);
    let client_id = field("client_id").ok_or_else(|| anyhow!("the client secret file has no client_id"))?;
    let client_secret = field("client_secret");
    let auth_uri = field("auth_uri").unwrap_or_else(|| "https://accounts.google.com/o/oauth2/auth".into());
    let token_uri = field("token_uri").unwrap_or_else(|| "https://oauth2.googleapis.com/token".into());

    let listener = TcpListener::bind(("localhost", 0)).context("couldn't listen for the OAuth redirect")?;
    let redirect_uri = format!("http://localhost:{}/", listener.local_addr()?.port());
    let state = random_text();
    let verifier = random_text() + &random_text();
    let challenge = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
    let url = format!(
        "{auth_uri}?response_type=code&client_id={}&redirect_uri={}&scope={}&state={}&code_challenge={challenge}&code_challenge_method=S256&access_type=offline",
        encode(&client_id),
        encode(&redirect_uri),
        encode(&SCOPES.join(" ")),
        encode(&state),
    );
    println!("Please visit this URL to authorize this application: {url}");
    open_browser(&url);

    let (code, given_state) = loop {
        let (mut stream, _) = listener.accept()?;
        let mut line = String::new();
        BufReader::new(&stream).read_line(&mut line)?;
        let path = line.split_whitespace().nth(1).unwrap_or("");
        let query = path.split_once('?').map(|(_, q)| q).unwrap_or("");
        let params: Vec<(String, String)> =
            query.split('&').filter_map(|p| p.split_once('=')).map(|(k, v)| (decode(k), decode(v))).collect();
        let get = |key: &str| params.iter().find(|(k, _)| k == key).map(|(_, v)| v.clone());
        let body = if get("code").is_some() || get("error").is_some() { DONE } else { "" };
        let _ = write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len());
        if let Some(error) = get("error") {
            bail!("Authorization was refused: {error}");
        }
        if let Some(code) = get("code") {
            break (code, get("state"));
        }
    };
    if given_state.as_deref() != Some(state.as_str()) {
        bail!("The authorization response didn't match this request (its state differs): try again");
    }

    let mut form = vec![
        ("grant_type", "authorization_code".to_string()),
        ("code", code),
        ("redirect_uri", redirect_uri),
        ("client_id", client_id.clone()),
        ("code_verifier", verifier),
    ];
    if let Some(secret) = &client_secret {
        form.push(("client_secret", secret.clone()));
    }
    let pairs: Vec<(&str, &str)> = form.iter().map(|(k, v)| (*k, v.as_str())).collect();
    let answer: Value = match ureq::post(&token_uri).send_form(&pairs) {
        Ok(r) => r.into_json()?,
        Err(ureq::Error::Status(status, r)) => bail!("Exchanging the authorization code failed ({status}): {}", r.into_string().unwrap_or_default()),
        Err(e) => return Err(e).context("Exchanging the authorization code failed"),
    };
    let token = answer.get("access_token").and_then(Value::as_str).ok_or_else(|| anyhow!("the token endpoint gave no access_token"))?;
    let expiry = chrono::Utc::now() + chrono::Duration::seconds(answer.get("expires_in").and_then(Value::as_i64).unwrap_or(3600));
    let written = json!({
        "token": token,
        "refresh_token": answer.get("refresh_token"),
        "token_uri": token_uri,
        "client_id": client_id,
        "client_secret": client_secret,
        "scopes": SCOPES,
        "universe_domain": "googleapis.com",
        "account": "",
        "expiry": expiry.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string(),
    });
    std::fs::write(token_path, written.to_string()).with_context(|| format!("couldn't write the token to {}", token_path.display()))?;
    Ok(())
}
