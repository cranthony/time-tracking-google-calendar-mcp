//! The OAuth consent flow for a missing or dead token, as
//! `google_auth_oauthlib`'s `InstalledAppFlow.run_local_server(port=0)`
//! runs it for `load_credentials`: a listener on a free local port as the
//! redirect, the browser opened to Google's consent page (the address
//! printed too), the code that comes back exchanged for a token -- with
//! PKCE -- and the token written to `token.json` in google-auth's format.
//! The command-line tools run it; the server's /setup (crate::setup) uses
//! its parts with the browser it's given.

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

/// The OAuth client in a client secret file (credentials.json): a desktop
/// app's ("installed"), which Google sends back only to localhost, or a web
/// application's ("web"), which it sends back to a registered address.
#[derive(Debug, Clone, PartialEq)]
pub struct Client {
    pub id: String,
    pub secret: Option<String>,
    pub auth_uri: String,
    pub token_uri: String,
    pub web: bool,
}

/// The client in the secret file at `path`.
pub fn read_client(path: &Path) -> anyhow::Result<Client> {
    let text = std::fs::read_to_string(path)
        .with_context(|| format!("No OAuth client secret file at {} (see the README's \"Google OAuth credentials\")", path.display()))?;
    client_of(&text)
}

/// The client in a secret file's `text`.
pub fn client_of(text: &str) -> anyhow::Result<Client> {
    let secrets: Value = serde_json::from_str(text).context("the client secret file isn't JSON")?;
    let (client, web) = match (secrets.get("installed"), secrets.get("web")) {
        (Some(client), _) => (client, false),
        (None, Some(client)) => (client, true),
        _ => bail!("the client secret file has no \"installed\" or \"web\" client"),
    };
    let field = |key: &str| client.get(key).and_then(Value::as_str).map(str::to_string);
    Ok(Client {
        id: field("client_id").ok_or_else(|| anyhow!("the client secret file has no client_id"))?,
        secret: field("client_secret"),
        auth_uri: field("auth_uri").unwrap_or_else(|| "https://accounts.google.com/o/oauth2/auth".into()),
        token_uri: field("token_uri").unwrap_or_else(|| "https://oauth2.googleapis.com/token".into()),
        web,
    })
}

/// A request's secrets: its state (matched when Google sends the browser
/// back) and its PKCE verifier (sent with the code), each random.
pub fn new_secrets() -> (String, String) {
    (random_text(), random_text() + &random_text())
}

/// Google's consent page for `client`, sending the browser back to
/// `redirect_uri` with a code -- for every scope, offline (so with a
/// refresh token), always asking (so a refresh token comes back even for
/// an account that granted access before).
pub fn auth_url(client: &Client, redirect_uri: &str, state: &str, verifier: &str) -> String {
    let challenge = base64::engine::general_purpose::URL_SAFE_NO_PAD.encode(Sha256::digest(verifier.as_bytes()));
    format!(
        "{}?response_type=code&client_id={}&redirect_uri={}&scope={}&state={}&code_challenge={challenge}&code_challenge_method=S256&access_type=offline&prompt=consent",
        client.auth_uri,
        encode(&client.id),
        encode(redirect_uri),
        encode(&SCOPES.join(" ")),
        encode(state),
    )
}

/// The query parameters of `query` (a URL's, after its "?"), decoded.
pub fn query_params(query: &str) -> Vec<(String, String)> {
    query.split('&').filter_map(|p| p.split_once('=')).map(|(k, v)| (decode(k), decode(v))).collect()
}

/// Exchange `code` (sent back to `redirect_uri`) for a token, as token.json
/// holds it in google-auth's format.
pub fn exchange(client: &Client, code: &str, redirect_uri: &str, verifier: &str) -> anyhow::Result<String> {
    let mut form = vec![
        ("grant_type", "authorization_code".to_string()),
        ("code", code.to_string()),
        ("redirect_uri", redirect_uri.to_string()),
        ("client_id", client.id.clone()),
        ("code_verifier", verifier.to_string()),
    ];
    if let Some(secret) = &client.secret {
        form.push(("client_secret", secret.clone()));
    }
    let pairs: Vec<(&str, &str)> = form.iter().map(|(k, v)| (*k, v.as_str())).collect();
    let answer: Value = match ureq::post(&client.token_uri).send_form(&pairs) {
        Ok(r) => r.into_json()?,
        Err(ureq::Error::Status(status, r)) => bail!("Exchanging the authorization code failed ({status}): {}", r.into_string().unwrap_or_default()),
        Err(e) => return Err(e).context("Exchanging the authorization code failed"),
    };
    let token = answer.get("access_token").and_then(Value::as_str).ok_or_else(|| anyhow!("the token endpoint gave no access_token"))?;
    if answer.get("refresh_token").and_then(Value::as_str).is_none() {
        bail!("Google gave no refresh token, so the server couldn't keep its access: try again");
    }
    let expiry = chrono::Utc::now() + chrono::Duration::seconds(answer.get("expires_in").and_then(Value::as_i64).unwrap_or(3600));
    Ok(json!({
        "token": token,
        "refresh_token": answer.get("refresh_token"),
        "token_uri": client.token_uri,
        "client_id": client.id,
        "client_secret": client.secret,
        "scopes": SCOPES,
        "universe_domain": "googleapis.com",
        "account": "",
        "expiry": expiry.format("%Y-%m-%dT%H:%M:%S%.6fZ").to_string(),
    })
    .to_string())
}

/// Run the consent flow with the client in `credentials_path`, and write
/// the token it gets to `token_path`.
pub fn consent(token_path: &Path, credentials_path: &Path) -> anyhow::Result<()> {
    consent_on(token_path, &read_client(credentials_path)?, 0)
}

/// Run the consent flow with `client`, listening for Google's redirect on
/// `port` of localhost (any free one for 0: a desktop app's client takes
/// any; a web application's needs the exact address registered), and write
/// the token it gets to `token_path`.
pub fn consent_on(token_path: &Path, client: &Client, port: u16) -> anyhow::Result<()> {
    let client = client.clone();
    let listener = TcpListener::bind(("localhost", port)).context("couldn't listen for the OAuth redirect")?;
    let redirect_uri = format!("http://localhost:{}/", listener.local_addr()?.port());
    let (state, verifier) = new_secrets();
    let url = auth_url(&client, &redirect_uri, &state, &verifier);
    println!("Please visit this URL to authorize this application: {url}");
    open_browser(&url);

    let (code, given_state) = loop {
        let (mut stream, _) = listener.accept()?;
        let mut line = String::new();
        BufReader::new(&stream).read_line(&mut line)?;
        let path = line.split_whitespace().nth(1).unwrap_or("");
        let params = query_params(path.split_once('?').map(|(_, q)| q).unwrap_or(""));
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
    let token = exchange(&client, &code, &redirect_uri, &verifier)?;
    std::fs::write(token_path, token).with_context(|| format!("couldn't write the token to {}", token_path.display()))?;
    Ok(())
}
