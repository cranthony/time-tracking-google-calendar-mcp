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
fn a_static_site_needs_its_index_html() {
    let site = site_dir();

    assert!(config(&[]).unwrap().static_site.is_none());
    assert_eq!(config(&[("STATIC_SITE_DIR", site.to_str().unwrap())]).unwrap().static_site, Some(site.clone()));
    assert_eq!(disabled(&[("STATIC_SITE_DIR", site.to_str().unwrap())]).unwrap().static_site, Some(site.clone()));
    let empty = site.parent().unwrap().to_path_buf();
    let refused = config(&[("STATIC_SITE_DIR", empty.to_str().unwrap())]).err().unwrap().to_string();
    assert!(refused.contains("there's no index.html in it"), "{refused}");
}

#[test]
fn skipping_sign_in_on_cloud_run_needs_the_proxy_check() {
    let proxied = [("K_SERVICE", "tracker"), ("ALLOWED_PROXY_EMAIL", "proxy@p.iam.gserviceaccount.com"), ("ALLOWED_HOST", "tracker.corp.example"), ("PROXY_AUDIENCE", "aud")];

    let refused = disabled(&proxied[..1]).err().unwrap().to_string();
    assert!(refused.contains("runs there only behind the IAP proxy"), "{refused}");
    assert_eq!(disabled(&proxied).unwrap().proxy.unwrap().host, "tracker.corp.example");
    // Elsewhere, and where sign-in is required, it's up to the deployment.
    assert!(disabled(&[]).unwrap().proxy.is_none());
    assert!(config(&proxied[..1]).unwrap().proxy.is_none());
    assert!(config(&proxied).unwrap().proxy.is_some());
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
        static_site: None,
        proxy: None,
        token_secret: None,
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
    served_on(crate::testing::app::test_app().app, config)
}

/// The router for `app` on a local port.
fn served_on(app: Arc<App>, config: HttpConfig) -> Served {
    let t = Served { url: String::new(), app };
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
    let data = answer.body.split("data: ").nth(1).unwrap().trim();
    let result = &serde_json::from_str::<Value>(data).unwrap()["result"];
    let flag = &result["_meta"][crate::auth_mode::META_KEY];
    assert_eq!(flag, &if crate::auth_mode::disabled() { json!(true) } else { Value::Null }, "{result}");
}

// -- served, with a static site -------------------------------------------------------------

const INDEX: &str = "<!doctype html><title>app</title>";

/// A built web app in a directory of its own, beside a file that isn't
/// the site's.
fn site_dir() -> std::path::PathBuf {
    let parent = std::env::temp_dir().join(format!("time-tracker-site-{}", uuid::Uuid::new_v4().simple()));
    let site = parent.join("web");
    std::fs::create_dir_all(site.join("assets")).unwrap();
    std::fs::write(site.join("index.html"), INDEX).unwrap();
    std::fs::write(site.join("main.dart.js"), "main();").unwrap();
    std::fs::write(site.join("assets").join("app.wasm"), b"\0asm").unwrap();
    std::fs::write(parent.join("secret.txt"), "secret").unwrap();
    site
}

fn served_site(auth_disabled: bool) -> Served {
    served_with(HttpConfig { static_site: Some(site_dir()), auth_disabled, ..http_config(AUTHKIT) })
}

const PAGE: (&str, &str) = ("Accept", "text/html,application/xhtml+xml,*/*;q=0.8");

#[test]
fn a_static_site_serves_its_files_revalidated() {
    let served = served_site(false);

    let index = send("GET", &format!("{}/", served.url), &[PAGE], None);
    assert_eq!((index.status, index.body.as_str()), (200, INDEX));
    assert_eq!(index.header("cache-control"), Some("no-cache"));
    let script = send("GET", &format!("{}/main.dart.js", served.url), &[], None);
    assert_eq!((script.status, script.body.as_str()), (200, "main();"));
    assert!(script.header("content-type").unwrap().contains("javascript"), "{:?}", script.headers);
    let wasm = send("GET", &format!("{}/assets/app.wasm", served.url), &[], None);
    assert_eq!(wasm.header("content-type"), Some("application/wasm"));
    let again = send("GET", &format!("{}/main.dart.js", served.url), &[("If-Modified-Since", script.header("last-modified").unwrap())], None);
    assert_eq!(again.status, 304);
}

#[test]
fn a_page_the_site_doesnt_have_is_its_index_but_nothing_else_is() {
    let served = served_site(false);

    let deep = send("GET", &format!("{}/today/2026-10-12", served.url), &[PAGE], None);
    assert_eq!((deep.status, deep.body.as_str()), (200, INDEX));
    // An API request, a probe of /.well-known/ or another method gets the
    // 404 it got before.
    assert_eq!(send("GET", &format!("{}/missing.js", served.url), &[], None).status, 404);
    assert_eq!(send("GET", &format!("{}/.well-known/oauth-authorization-server", served.url), &[PAGE], None).status, 404);
    assert_eq!(send("POST", &format!("{}/main.dart.js", served.url), &[], Some(b"{}")).status, 404);
}

#[test]
fn a_static_site_never_serves_what_isnt_in_it() {
    let served = served_site(false);

    for path in ["/%2e%2e/secret.txt", "/..%2fsecret.txt", "/assets/%2e%2e/%2e%2e/secret.txt"] {
        let answer = send("GET", &format!("{}{path}", served.url), &[], None);
        assert!(!answer.body.contains("secret"), "{path}: {}", answer.body);
        assert_ne!(answer.status, 200, "{path}");
    }
}

#[test]
fn the_endpoints_come_before_the_site() {
    let served = served_site(false);

    let mcp = send("GET", &format!("{}/mcp", served.url), &[PAGE], None);
    assert_eq!(mcp.status, 401);
    assert_ne!(mcp.body, INDEX);
    let metadata = send("GET", &format!("{}/.well-known/oauth-protected-resource/mcp", served.url), &[], None);
    assert_eq!(metadata.status, 200);
    assert!(metadata.body.contains("authorization_servers"), "{}", metadata.body);
}

#[test]
fn skipping_sign_in_with_a_site_still_has_no_metadata() {
    let served = served_site(true);

    assert_eq!(send("GET", &format!("{}/.well-known/oauth-protected-resource/mcp", served.url), &[PAGE], None).status, 404);
    assert_eq!(send("GET", &format!("{}/", served.url), &[PAGE], None).body, INDEX);
}

#[test]
fn without_a_site_a_page_load_is_not_found() {
    let served = served(AUTHKIT);

    let answer = send("GET", &format!("{}/", served.url), &[PAGE], None);
    assert_eq!((answer.status, answer.body.as_str()), (404, "Not Found"));
}

// -- served, behind the proxy check ---------------------------------------------------------

const PROXY_EMAIL: &str = "proxy@corp-project.iam.gserviceaccount.com";
const PROXY_AUDIENCE: &str = "//iam.googleapis.com/projects/123456";

fn served_behind_proxy(auth_disabled: bool) -> Served {
    let proxy = ProxyCheck { email: PROXY_EMAIL.into(), host: "127.0.0.1".into(), audience: PROXY_AUDIENCE.into(), signature: None };
    served_with(HttpConfig { proxy: Some(proxy), auth_disabled, static_site: Some(site_dir()), ..http_config(AUTHKIT) })
}

/// The proxy's token, its signature stripped, with `changes` to its claims.
fn proxy_token(changes: Value) -> String {
    let mut claims = json!({"iss": "https://accounts.google.com", "aud": PROXY_AUDIENCE, "email": PROXY_EMAIL, "email_verified": true, "iat": now(), "exp": now() + 3600});
    for (k, v) in changes.as_object().unwrap() {
        claims[k] = v.clone();
    }
    let part = |v: &Value| base64::Engine::encode(&base64::engine::general_purpose::URL_SAFE_NO_PAD, v.to_string());
    format!("Bearer {}.{}.", part(&json!({"alg": "RS256", "typ": "JWT"})), part(&claims))
}

#[test]
fn behind_the_proxy_only_what_came_through_it_is_served() {
    let served = served_behind_proxy(false);
    let through = proxy_token(json!({}));

    let page = send("GET", &format!("{}/", served.url), &[PAGE, ("X-Serverless-Authorization", &through)], None);
    assert_eq!((page.status, page.body.as_str()), (200, INDEX));
    for headers in [vec![PAGE], vec![PAGE, ("X-Serverless-Authorization", "Bearer forged")]] {
        let refused = send("GET", &format!("{}/", served.url), &headers, None);
        assert_eq!((refused.status, refused.body.as_str()), (403, "Forbidden"), "{headers:?}");
    }
    let elsewhere = proxy_token(json!({"email": "someone@corp.example"}));
    assert_eq!(send("GET", &format!("{}/main.dart.js", served.url), &[("X-Serverless-Authorization", &elsewhere)], None).status, 403);
    // The endpoints too, sign-in or not.
    assert_eq!(send("GET", &format!("{}/.well-known/oauth-protected-resource/mcp", served.url), &[], None).status, 403);
    let metadata = send("GET", &format!("{}/.well-known/oauth-protected-resource/mcp", served.url), &[("X-Serverless-Authorization", &through)], None);
    assert_eq!(metadata.status, 200);
}

#[test]
fn behind_the_proxy_without_sign_in_mcp_works_through_it() {
    let served = served_behind_proxy(true);
    let body = json!({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}});
    let initialize = |headers: &[(&str, &str)]| {
        let mut all = vec![("Content-Type", "application/json"), ACCEPT];
        all.extend_from_slice(headers);
        send("POST", &format!("{}/mcp", served.url), &all, Some(body.to_string().as_bytes()))
    };

    assert_eq!(initialize(&[]).status, 403);
    assert_eq!(initialize(&[("X-Serverless-Authorization", &proxy_token(json!({"exp": now() - 120})))]).status, 403);
    assert_eq!(initialize(&[("X-Serverless-Authorization", &proxy_token(json!({})))]).status, 200);
}

// -- served: setting up Google access -----------------------------------------------------

use crate::setup::tests::{app_on, url_param};

/// A request that doesn't follow redirects, with `cookie` if given.
fn send_raw(method: &str, url: &str, cookie: Option<&str>, body: Option<&str>) -> Answer {
    let agent = ureq::AgentBuilder::new().redirects(0).build();
    let mut request = agent.request(method, url);
    if let Some(cookie) = cookie {
        request = request.set("Cookie", cookie);
    }
    let result = match body {
        Some(body) => request.set("Content-Type", "application/x-www-form-urlencoded").send_string(body),
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

/// The server for a fresh app (no calendar) on the fake, with its
/// client secret file: a web application's or a desktop app's.
fn served_fresh(fake: &crate::testing::fake_google::FakeGoogle, web: bool) -> Served {
    let served = served_on(app_on(fake, None, web), http_config(AUTHKIT));
    served.app.setup.set_base(&served.url);
    served
}

/// The cookie a setup page set, as the browser sends it back.
fn cookie_of(answer: &Answer) -> String {
    answer.header("set-cookie").unwrap().split(';').next().unwrap().to_string()
}

#[test]
fn a_setup_link_with_a_desktop_client_finishes_with_the_pasted_address() {
    let fake = crate::testing::fake_google::FakeGoogle::start();
    let served = served_fresh(&fake, false);
    let link = served.app.setup.new_link();

    let page = send_raw("GET", &format!("{}/setup?link={link}", served.url), None, None);
    assert_eq!(page.status, 200);
    assert_eq!(page.header("cache-control"), Some("no-store"));
    let cookie = page.header("set-cookie").unwrap();
    assert!(cookie.contains("HttpOnly") && cookie.contains("Secure") && cookie.contains("SameSite=Lax") && cookie.contains("Path=/setup"), "{cookie}");
    let href = page.body.split("href=\"").nth(1).unwrap().split('"').next().unwrap().replace("&amp;", "&");
    let state = url_param(&href, "state");
    // The link is used up.
    assert_eq!(send_raw("GET", &format!("{}/setup?link={link}", served.url), None, None).status, 403);

    let address = format!("http://localhost/?state={state}&code=4/abc&scope=x");
    let form = format!("address={}", address.replace('&', "%26").replace('?', "%3F").replace('/', "%2F").replace(':', "%3A").replace('=', "%3D"));
    let without_cookie = send_raw("POST", &format!("{}/setup/paste", served.url), None, Some(&form));
    assert_eq!(without_cookie.status, 403);
    let finished = send_raw("POST", &format!("{}/setup/paste", served.url), Some(&cookie_of(&page)), Some(&form));

    assert_eq!(finished.status, 200, "{}", finished.body);
    assert_eq!(finished.header("cache-control"), Some("no-store"));
    let calendar = served.app.calendar_id().unwrap();
    assert!(finished.body.contains(&calendar) && finished.body.contains("refresh-"), "{}", finished.body);
}

#[test]
fn a_setup_link_with_a_web_client_goes_to_google_and_back() {
    let fake = crate::testing::fake_google::FakeGoogle::start();
    let served = served_fresh(&fake, true);

    let start = send_raw("GET", &format!("{}/setup?link={}", served.url, served.app.setup.new_link()), None, None);
    assert_eq!(start.status, 303);
    let location = start.header("location").unwrap().to_string();
    assert_eq!(url_param(&location, "redirect_uri"), format!("{}/setup/callback", served.url));
    let state = url_param(&location, "state");

    let back = format!("{}/setup/callback?state={state}&code=4%2Fxyz", served.url);
    let finished = send_raw("GET", &back, Some(&cookie_of(&start)), None);
    assert_eq!(finished.status, 200, "{}", finished.body);
    assert!(served.app.calendar_id().is_ok());
    // Once set up, a new link has nothing to start.
    let start = send_raw("GET", &format!("{}/setup?link={}", served.url, served.app.setup.new_link()), None, None);
    assert_eq!(start.status, 409, "{}", start.body);
}

#[test]
fn setup_refuses_a_missing_link_and_a_server_already_set_up() {
    let served = served(AUTHKIT);
    served.app.setup.set_base(&served.url);

    assert_eq!(send_raw("GET", &format!("{}/setup", served.url), None, None).status, 403);
    assert_eq!(send_raw("GET", &format!("{}/setup?link=guessed", served.url), None, None).status, 403);
    let working = send_raw("GET", &format!("{}/setup?link={}", served.url, served.app.setup.new_link()), None, None);
    assert_eq!(working.status, 409);
    assert!(working.body.contains("nothing to set up"));
    let refused = send_raw("GET", &format!("{}/setup/callback?error=access_denied", served.url), None, None);
    assert_eq!(refused.status, 400);
    assert!(refused.body.contains("access_denied"));
}

#[test]
fn the_token_secret_is_a_secrets_resource_name() {
    assert_eq!(config(&[]).unwrap().token_secret, None);
    let name = "projects/corp-project/secrets/time-tracker-google-token";
    assert_eq!(config(&[("SETUP_TOKEN_SECRET", name)]).unwrap().token_secret.as_deref(), Some(name));
    assert_eq!(disabled(&[("SETUP_TOKEN_SECRET", name)]).unwrap().token_secret.as_deref(), Some(name));
    for wrong in ["time-tracker-google-token", "projects/p/secrets/s/versions/latest", "projects//secrets/s"] {
        let refused = config(&[("SETUP_TOKEN_SECRET", wrong)]).err().unwrap().to_string();
        assert!(refused.contains("should name a Secret Manager secret"), "{wrong}: {refused}");
    }
}

#[test]
fn the_public_base_is_the_mcp_url_without_mcp_or_the_proxys_host() {
    assert_eq!(config(&[]).unwrap().public_base().as_deref(), Some("https://explicit.example"));
    let proxied = [("ALLOWED_PROXY_EMAIL", "p@x.iam.gserviceaccount.com"), ("ALLOWED_HOST", "tracker.corp.example"), ("PROXY_AUDIENCE", "a")];
    assert_eq!(disabled(&proxied).unwrap().public_base().as_deref(), Some("https://tracker.corp.example"));
    assert_eq!(disabled(&[]).unwrap().public_base(), None);
}

#[test]
fn the_icon_is_served_without_signing_in() {
    let served = served(AUTHKIT);

    let svg = send("GET", &format!("{}/favicon.svg", served.url), &[], None);
    assert_eq!((svg.status, svg.body.as_bytes()), (200, icon::SVG));
    assert_eq!(svg.header("content-type"), Some("image/svg+xml"));
    assert_eq!(svg.header("cache-control"), Some("public, max-age=86400"));
    for (path, content_type, bytes) in [("/favicon.png", "image/png", icon::PNG), ("/favicon.ico", "image/x-icon", icon::ICO)] {
        let answer = send("GET", &format!("{}{path}", served.url), &[], None);
        assert_eq!(answer.status, 200, "{path}");
        assert_eq!(answer.header("content-type"), Some(content_type));
        assert_eq!(answer.header("content-length"), Some(bytes.len().to_string().as_str()));
    }
}
