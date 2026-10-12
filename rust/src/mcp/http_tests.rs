//! The HTTP transport: its settings, auth, CORS, timing and OAuth proxy.

use super::*;

fn vars(pairs: &[(&str, &str)]) -> impl Fn(&str) -> Option<String> {
    let pairs: Vec<(String, String)> = pairs.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect();
    move |name| pairs.iter().find(|(k, _)| k == name).map(|(_, v)| v.clone())
}

const BASE: [(&str, &str); 3] =
    [("WORKOS_AUTHKIT_DOMAIN", "https://my-tenant.authkit.app"), ("MCP_PUBLIC_URL", "https://explicit.example/mcp"), ("MCP_ALLOWED_USER_IDS", "user_1")];

/// The base settings with `changes` (an empty value unsets one).
fn config(changes: &[(&str, &str)]) -> anyhow::Result<HttpConfig> {
    let mut all: Vec<(&str, &str)> = BASE.iter().copied().filter(|(k, _)| !changes.iter().any(|(c, _)| c == k)).collect();
    all.extend(changes.iter().copied());
    HttpConfig::for_mode(vars(&all), false)
}

#[test]
fn the_authkit_domain_is_needed() {
    for unset in [config(&[("WORKOS_AUTHKIT_DOMAIN", "")])] {
        assert!(unset.err().unwrap().to_string().contains("WORKOS_AUTHKIT_DOMAIN is not set"));
    }

    assert_eq!(config(&[]).unwrap().authkit_domain, "https://my-tenant.authkit.app");
}

#[test]
fn the_resource_url_prefers_the_explicit_one_then_renders() {
    assert_eq!(config(&[("RENDER_EXTERNAL_URL", "https://from-render.onrender.com")]).unwrap().resource, "https://explicit.example/mcp");
    assert_eq!(config(&[("MCP_PUBLIC_URL", ""), ("RENDER_EXTERNAL_URL", "https://my-service.onrender.com")]).unwrap().resource, "https://my-service.onrender.com/mcp");
    assert_eq!(config(&[("MCP_PUBLIC_URL", ""), ("RENDER_EXTERNAL_URL", "https://my-service.onrender.com/")]).unwrap().resource, "https://my-service.onrender.com/mcp");
    assert!(config(&[("MCP_PUBLIC_URL", "")]).err().unwrap().to_string().contains("Neither MCP_PUBLIC_URL nor RENDER_EXTERNAL_URL is set"));
}

#[test]
fn the_allowed_user_ids_are_split_and_trimmed_and_needed() {
    assert_eq!(config(&[("MCP_ALLOWED_USER_IDS", " user_1 , user_2,,")]).unwrap().allowed_user_ids, vec!["user_1".to_string(), "user_2".to_string()]);

    // None means no one, never everyone.
    for value in ["", " , "] {
        assert!(config(&[("MCP_ALLOWED_USER_IDS", value)]).err().unwrap().to_string().contains("MCP_ALLOWED_USER_IDS is not set"), "{value:?}");
    }
}

#[test]
fn the_cors_origins_are_split_trimmed_and_lose_trailing_slashes() {
    assert!(config(&[]).unwrap().cors_origins.is_empty());
    assert_eq!(
        config(&[("MCP_CORS_ALLOWED_ORIGINS", " https://a.example/ , https://b.example,,")]).unwrap().cors_origins,
        vec!["https://a.example".to_string(), "https://b.example".to_string()]
    );
}

#[test]
fn the_port_defaults_to_renders() {
    assert_eq!(config(&[]).unwrap().port, 10000);
    assert_eq!(config(&[("PORT", "8000")]).unwrap().port, 8000);
}

// -- a build that skips sign-in ------------------------------------------------------------

fn disabled(changes: &[(&str, &str)]) -> anyhow::Result<HttpConfig> {
    HttpConfig::for_mode(vars(changes), true)
}

#[test]
fn this_builds_settings_follow_its_mode() {
    // A build without AUTH_DISABLED needs WorkOS; one with it doesn't.
    assert_eq!(HttpConfig::from_vars(vars(&[])).is_ok(), crate::auth_mode::disabled());
}

#[test]
fn skipping_sign_in_needs_no_workos_settings_and_says_which_it_ignores() {
    let config = disabled(&[]).unwrap();
    assert!(config.auth_disabled && config.ignored.is_empty());
    assert_eq!(config.port, 10000);

    let config = disabled(&BASE).unwrap();
    assert_eq!(config.ignored, vec!["WORKOS_AUTHKIT_DOMAIN", "MCP_ALLOWED_USER_IDS"]);
    assert!(config.allowed_user_ids.is_empty());
}

#[test]
fn skipping_sign_in_refuses_to_run_on_render() {
    let refused = disabled(&[("RENDER_EXTERNAL_URL", "https://my-service.onrender.com")]).err().unwrap().to_string();
    assert!(refused.contains("won't run where the internet can reach it"), "{refused}");
    // Where sign-in is required, Render is fine.
    assert!(config(&[("RENDER_EXTERNAL_URL", "https://my-service.onrender.com")]).is_ok());
}

#[test]
fn the_metadata_urls_follow_the_resource() {
    let config = config(&[]).unwrap();

    assert_eq!(config.resource_metadata_url(), "https://explicit.example/.well-known/oauth-protected-resource/mcp");
    assert_eq!(config.issuer_url(), "https://my-tenant.authkit.app/");
    assert_eq!(config.issuer(), "https://my-tenant.authkit.app");
}

// -- the endpoints, served ---------------------------------------------------------------
// Token checks, the OAuth proxy, CORS and timing over HTTP: a real server on a
// local port, with a fixed RSA test key standing in for AuthKit's.

const AUTHKIT: &str = "https://example.authkit.app";
const RESOURCE: &str = "https://my-service.onrender.com/mcp";

fn keys() -> Value {
    serde_json::from_str(include_str!("test_keys.json")).unwrap()
}

fn http_config(authkit: &str) -> HttpConfig {
    HttpConfig {
        authkit_domain: authkit.into(),
        resource: RESOURCE.into(),
        allowed_user_ids: vec!["user_123".into(), "user_456".into()],
        cors_origins: vec!["https://tracker.example".into()],
        port: 0,
        auth_disabled: false,
        ignored: Vec::new(),
    }
}

struct Served {
    url: String,
    app: Arc<App>,
}

/// The router on a local port, its key set holding the test key.
fn served(authkit: &str) -> Served {
    served_with(http_config(authkit))
}

fn served_with(config: HttpConfig) -> Served {
    let t = crate::testing::app::test_app();
    let state = shared(t.app.clone(), config);
    *state.keys.lock().unwrap() = vec![keys()["key"]["jwk"].clone()];
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let port = listener.local_addr().unwrap().port();
    let app = router(state);
    std::thread::spawn(move || {
        tokio::runtime::Runtime::new().unwrap().block_on(async move {
            let listener = tokio::net::TcpListener::from_std(listener).unwrap();
            axum::serve(listener, app).await.unwrap();
        })
    });
    Served { url: format!("http://127.0.0.1:{port}"), app: t.app }
}

fn now() -> i64 {
    chrono::Utc::now().timestamp()
}

fn claims(changes: Value) -> Value {
    let mut claims = json!({"iss": AUTHKIT, "aud": RESOURCE, "sub": "user_123", "client_id": "client_abc", "scope": "", "exp": now() + 300});
    for (k, v) in changes.as_object().unwrap() {
        if v.is_null() {
            claims.as_object_mut().unwrap().remove(k);
        } else {
            claims[k] = v.clone();
        }
    }
    claims
}

fn token_with(key: &str, claims: &Value) -> String {
    let mut header = jsonwebtoken::Header::new(jsonwebtoken::Algorithm::RS256);
    header.kid = Some("test-key".into());
    let pem = keys()[key]["pem"].as_str().unwrap().to_string();
    jsonwebtoken::encode(&header, claims, &jsonwebtoken::EncodingKey::from_rsa_pem(pem.as_bytes()).unwrap()).unwrap()
}

fn token(changes: Value) -> String {
    token_with("key", &claims(changes))
}

struct Answer {
    status: u16,
    headers: Vec<(String, String)>,
    body: String,
}

impl Answer {
    fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(k, _)| k.eq_ignore_ascii_case(name)).map(|(_, v)| v.as_str())
    }
}

fn send(method: &str, url: &str, headers: &[(&str, &str)], body: Option<&[u8]>) -> Answer {
    let mut request = ureq::request(method, url);
    for (k, v) in headers {
        request = request.set(k, v);
    }
    let result = match body {
        Some(body) => request.send_bytes(body),
        None => request.call(),
    };
    let response = match result {
        Ok(r) => r,
        Err(ureq::Error::Status(_, r)) => r,
        Err(e) => panic!("{e}"),
    };
    let status = response.status();
    let headers = response.headers_names().into_iter().map(|n| (n.clone(), response.header(&n).unwrap_or_default().to_string())).collect();
    Answer { status, headers, body: response.into_string().unwrap_or_default() }
}

const ACCEPT: (&str, &str) = ("Accept", "application/json, text/event-stream");

fn initialize(served: &Served, bearer: &str) -> Answer {
    let auth = format!("Bearer {bearer}");
    let body = json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}});
    send("POST", &format!("{}/mcp", served.url), &[("Authorization", &auth), ("Content-Type", "application/json"), ACCEPT], Some(body.to_string().as_bytes()))
}

fn accepted(changes: Value) -> bool {
    let served = served(AUTHKIT);
    match initialize(&served, &token(changes)).status {
        200 => true,
        401 => false,
        other => panic!("{other}"),
    }
}

#[test]
fn a_valid_token_is_accepted() {
    assert!(accepted(json!({})));
}

#[test]
fn an_expired_token_is_rejected() {
    assert!(!accepted(json!({"exp": now() - 60})));
}

#[test]
fn a_token_for_another_issuer_or_audience_is_rejected() {
    assert!(!accepted(json!({"iss": "https://not-workos.example"})));
    assert!(!accepted(json!({"aud": "https://someone-elses-server.example/mcp"})));
}

#[test]
fn a_token_signed_by_another_key_or_not_a_token_is_rejected() {
    let served = served(AUTHKIT);

    assert_eq!(initialize(&served, &token_with("other", &claims(json!({})))).status, 401);
    assert_eq!(initialize(&served, "not-a-jwt").status, 401);
}

#[test]
fn only_the_allowed_users_are_accepted() {
    assert!(accepted(json!({"sub": "user_456"})));
    assert!(!accepted(json!({"sub": "user_stranger"})));
    // Nor a token naming no user at all.
    assert!(!accepted(json!({"sub": null})));
}

#[test]
fn a_token_for_the_unslashed_domain_verifies_when_configured_with_a_trailing_slash() {
    let served = served(&format!("{AUTHKIT}/"));

    assert_eq!(initialize(&served, &token(json!({}))).status, 200);
}

#[test]
fn without_a_token_it_says_where_to_get_one() {
    let served = served(AUTHKIT);

    let answer = send("POST", &format!("{}/mcp", served.url), &[ACCEPT], Some(b"{}"));

    assert_eq!(answer.status, 401);
    assert!(answer.header("www-authenticate").unwrap().contains("resource_metadata=\"https://my-service.onrender.com/.well-known/oauth-protected-resource/mcp\""));
    let metadata = send("GET", &format!("{}/.well-known/oauth-protected-resource/mcp", served.url), &[], None);
    let metadata: Value = serde_json::from_str(&metadata.body).unwrap();
    assert_eq!((metadata["resource"].clone(), metadata["authorization_servers"].clone()), (json!(RESOURCE), json!([format!("{AUTHKIT}/")])));
}

// -- CORS --------------------------------------------------------------------------------

fn preflight(served: &Served, origin: &str) -> Answer {
    send(
        "OPTIONS",
        &format!("{}/mcp", served.url),
        &[("Origin", origin), ("Access-Control-Request-Method", "POST"), ("Access-Control-Request-Headers", "authorization,content-type,mcp-session-id,mcp-protocol-version")],
        None,
    )
}

#[test]
fn answers_a_preflight_from_a_local_or_configured_origin_before_auth() {
    let served = served(AUTHKIT);

    for origin in ["http://localhost:8765", "http://127.0.0.1:3000", "https://tracker.example"] {
        let answer = preflight(&served, origin);
        assert_eq!(answer.status, 200, "{origin}");
        assert_eq!(answer.header("access-control-allow-origin"), Some(origin));
    }
}

#[test]
fn rejects_a_preflight_from_any_other_origin() {
    let served = served(AUTHKIT);

    for origin in ["https://evil.example", "http://localhost.evil.example", "https://localhost:8765"] {
        let answer = preflight(&served, origin);
        assert_eq!(answer.status, 400, "{origin}");
        assert!(answer.header("access-control-allow-origin").is_none());
    }
}

#[test]
fn exposes_the_session_id() {
    let served = served(AUTHKIT);
    let auth = format!("Bearer {}", token(json!({})));
    let body = json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}});

    let answer = send("POST", &format!("{}/mcp", served.url), &[("Origin", "http://localhost:8765"), ("Authorization", &auth), ("Content-Type", "application/json"), ACCEPT], Some(body.to_string().as_bytes()));

    assert!(answer.header("mcp-session-id").is_some());
    assert!(answer.header("access-control-expose-headers").unwrap().to_lowercase().contains("mcp-session-id"));
}

// -- timing ------------------------------------------------------------------------------

#[test]
fn a_call_over_http_is_recorded_with_the_whole_requests_time() {
    let served = served(AUTHKIT);
    let bearer = token(json!({}));
    let auth = format!("Bearer {bearer}");
    let session = initialize(&served, &bearer).header("mcp-session-id").unwrap().to_string();
    let headers = [("Authorization", auth.as_str()), ("Content-Type", "application/json"), ACCEPT, ("Mcp-Session-Id", session.as_str())];
    send("POST", &format!("{}/mcp", served.url), &headers, Some(json!({"jsonrpc": "2.0", "method": "notifications/initialized"}).to_string().as_bytes()));

    let called = send("POST", &format!("{}/mcp", served.url), &headers, Some(json!({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_health", "arguments": {}}}).to_string().as_bytes()));

    assert_eq!(called.status, 200, "{}", called.body);
    let calls = served.app.health.calls();
    let (_, health) = calls.iter().find(|(n, _)| n == "get_health").unwrap();
    assert_eq!(health.len(), 1);
    assert!(health[0].ok);
    assert!(health[0].total_ms.is_some_and(|t| t >= health[0].work_ms));
}

// -- the OAuth proxy ---------------------------------------------------------------------

/// A stand-in for AuthKit: records each request, and answers JSON -- a 400
/// for a refresh token it doesn't know.
fn fake_authkit() -> (String, Arc<Mutex<Vec<Value>>>) {
    use std::io::{BufRead, BufReader, Read, Write};
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
    let seen = Arc::new(Mutex::new(Vec::new()));
    let log = seen.clone();
    std::thread::spawn(move || {
        for stream in listener.incoming() {
            let Ok(mut stream) = stream else { continue };
            let mut reader = BufReader::new(stream.try_clone().unwrap());
            let mut line = String::new();
            reader.read_line(&mut line).unwrap();
            let path = line.split_whitespace().nth(1).unwrap_or_default().to_string();
            let mut headers: Vec<(String, String)> = Vec::new();
            loop {
                let mut header = String::new();
                reader.read_line(&mut header).unwrap();
                let header = header.trim_end().to_string();
                if header.is_empty() {
                    break;
                }
                if let Some((k, v)) = header.split_once(':') {
                    headers.push((k.trim().to_lowercase(), v.trim().to_string()));
                }
            }
            let length: usize = headers.iter().find(|(k, _)| k == "content-length").and_then(|(_, v)| v.parse().ok()).unwrap_or(0);
            let mut body = vec![0; length];
            reader.read_exact(&mut body).unwrap();
            let body = String::from_utf8_lossy(&body).to_string();
            let header = |name: &str| headers.iter().find(|(k, _)| k == name).map(|(_, v)| v.clone());
            log.lock().unwrap().push(json!({"path": path, "body": body, "content_type": header("content-type"), "user_agent": header("user-agent")}));
            let (status, payload) = if body.contains("refresh_token=unknown") { (400, json!({"error": "invalid_grant"})) } else { (200, json!({"ok": path})) };
            let payload = payload.to_string();
            let reply = format!(
                "HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nSet-Cookie: upstream=1\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
                payload.len()
            );
            let _ = stream.write_all(reply.as_bytes());
        }
    });
    (url, seen)
}

#[test]
fn forwards_registration_to_authkit() {
    let (authkit, seen) = fake_authkit();
    let served = served(&authkit);

    let answer = send("POST", &format!("{}/oauth/register", served.url), &[("Content-Type", "application/json")], Some(br#"{"client_name":"Time Tracker (web)"}"#));

    assert_eq!(answer.status, 200);
    assert_eq!(serde_json::from_str::<Value>(&answer.body).unwrap(), json!({"ok": "/oauth2/register"}));
    assert_eq!(
        *seen.lock().unwrap(),
        vec![json!({"path": "/oauth2/register", "body": r#"{"client_name":"Time Tracker (web)"}"#, "content_type": "application/json", "user_agent": PROXY_USER_AGENT})]
    );
}

#[test]
fn forwards_a_token_request_to_authkit() {
    let (authkit, seen) = fake_authkit();
    let served = served(&authkit);

    let answer = send("POST", &format!("{}/oauth/token", served.url), &[("Content-Type", "application/x-www-form-urlencoded")], Some(b"grant_type=authorization_code&code=c"));

    assert_eq!(serde_json::from_str::<Value>(&answer.body).unwrap(), json!({"ok": "/oauth2/token"}));
    let first = seen.lock().unwrap()[0].clone();
    assert_eq!((first["path"].clone(), first["body"].clone(), first["content_type"].clone()), (json!("/oauth2/token"), json!("grant_type=authorization_code&code=c"), json!("application/x-www-form-urlencoded")));
}

#[test]
fn passes_oauth_errors_through_keeping_only_safe_headers() {
    let (authkit, _) = fake_authkit();
    let served = served(&authkit);

    let answer = send("POST", &format!("{}/oauth/token", served.url), &[("Content-Type", "application/x-www-form-urlencoded")], Some(b"grant_type=refresh_token&refresh_token=unknown"));

    assert_eq!(answer.status, 400);
    assert_eq!(serde_json::from_str::<Value>(&answer.body).unwrap(), json!({"error": "invalid_grant"}));
    assert_eq!(answer.header("cache-control"), Some("no-store"));
    assert!(answer.header("set-cookie").is_none());
}

#[test]
fn rejects_an_oversized_body_without_forwarding_it() {
    let (authkit, seen) = fake_authkit();
    let served = served(&authkit);

    let answer = send("POST", &format!("{}/oauth/register", served.url), &[("Content-Type", "application/json")], Some(&vec![b'x'; MAX_PROXY_BODY + 1]));

    assert_eq!(answer.status, 413);
    assert!(seen.lock().unwrap().is_empty());
}

// -- served, skipping sign-in ---------------------------------------------------------------

fn served_without_sign_in() -> Served {
    served_with(HttpConfig { auth_disabled: true, ..http_config(AUTHKIT) })
}

#[test]
fn skipping_sign_in_trusts_every_request_token_or_not() {
    let served = served_without_sign_in();

    for bearer in ["", "not-a-jwt", &token(json!({"sub": "user_stranger"}))] {
        assert_eq!(initialize(&served, bearer).status, 200, "{bearer:?}");
    }
    let body = json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}});
    let answer = send("POST", &format!("{}/mcp", served.url), &[("Content-Type", "application/json"), ACCEPT], Some(body.to_string().as_bytes()));
    assert_eq!(answer.status, 200);
    assert!(answer.header("www-authenticate").is_none());
}

#[test]
fn skipping_sign_in_serves_nothing_to_sign_in_with() {
    let served = served_without_sign_in();

    assert_eq!(send("GET", &format!("{}/.well-known/oauth-protected-resource/mcp", served.url), &[], None).status, 404);
    for path in ["/oauth/register", "/oauth/token"] {
        assert_eq!(send("POST", &format!("{}{path}", served.url), &[], Some(b"{}")).status, 404, "{path}");
    }
}

#[test]
fn the_handshake_says_whether_this_build_skips_sign_in() {
    let served = served_without_sign_in();

    let answer = initialize(&served, "");

    let says = answer.body.contains("Authentication is disabled") && answer.body.contains("(sign-in off)");
    assert_eq!(says, crate::auth_mode::disabled(), "{}", answer.body);
}
