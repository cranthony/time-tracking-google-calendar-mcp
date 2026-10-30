//! MCP over streamable HTTP, as on Render (MCP_TRANSPORT=streamable-http):
//! an OAuth resource server whose bearer tokens WorkOS AuthKit issues, CORS
//! for browser clients (with_cors), the OAuth proxy to AuthKit, the web
//! app's sign-in cookie (crate::mcp::session), and each tool call's
//! whole-request time for the health metrics (with_health).
//! A build that skips sign-in (crate::auth_mode) trusts every request, and
//! serves neither the protected-resource metadata nor the OAuth proxy.
//! Every answer -- status, headers that matter, body byte for byte -- is
//! held to conformance/'s golden records.

use crate::app::App;
use crate::mcp::icon;
use crate::mcp::proxy::{self, ProxyCheck};
use crate::mcp::session::{self, Session, SessionKey};
use axum::body::{Body, Bytes};
use axum::extract::{Request, State};
use axum::http::{HeaderMap, HeaderValue, Method, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::Router;
use regex::Regex;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::convert::Infallible;
use std::path::{Path, PathBuf};
use std::sync::{Arc, LazyLock, Mutex};
use std::time::{Duration, Instant};

const PROTECTED_RESOURCE_PATH: &str = "/.well-known/oauth-protected-resource/mcp";
const ALLOWED_HEADERS: &str = "Accept, Accept-Language, Authorization, Content-Language, Content-Type, Last-Event-ID, Mcp-Protocol-Version, Mcp-Session-Id";
const ALLOWED_HEADER_NAMES: [&str; 8] =
    ["accept", "accept-language", "authorization", "content-language", "content-type", "last-event-id", "mcp-protocol-version", "mcp-session-id"];
const ALLOWED_METHODS: [&str; 3] = ["GET", "POST", "DELETE"];
const EXPOSED_HEADERS: &str = "Mcp-Session-Id, WWW-Authenticate";
const MAX_PROXY_BODY: usize = 64 * 1024;
const PROXY_USER_AGENT: &str = "time-tracking-google-calendar-mcp oauth-proxy";

static LOCAL_ORIGIN: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^http://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$").unwrap());

pub struct HttpConfig {
    pub authkit_domain: String,
    pub resource: String,
    pub allowed_user_ids: Vec<String>,
    pub cors_origins: Vec<String>,
    pub port: u16,
    /// Whether every request is trusted, with no sign-in: this build's
    /// crate::auth_mode.
    pub auth_disabled: bool,
    /// The sign-in settings given, though a build that skips sign-in
    /// doesn't read them.
    pub ignored: Vec<&'static str>,
    /// The static site served at "/" beside the endpoints (a built web
    /// app): STATIC_SITE_DIR, if set.
    pub static_site: Option<PathBuf>,
    /// The check that every request came through the IAP proxy in front
    /// (crate::mcp::proxy), if ALLOWED_PROXY_EMAIL, ALLOWED_HOST and
    /// PROXY_AUDIENCE ask for it.
    pub proxy: Option<ProxyCheck>,
    /// The Secret Manager secret a browser setup adds each new Google
    /// token to (crate::setup): SETUP_TOKEN_SECRET, if set.
    pub token_secret: Option<String>,
    /// The key the web app's sign-in cookie is sealed with
    /// (crate::mcp::session): SESSION_SECRET, if set. Without it there
    /// is no cookie, and the app signs in again on each page load.
    pub session_key: Option<SessionKey>,
}

/// The settings only sign-in reads.
const SIGN_IN_VARS: [&str; 3] = ["WORKOS_AUTHKIT_DOMAIN", "MCP_ALLOWED_USER_IDS", "SESSION_SECRET"];

impl HttpConfig {
    pub fn from_env() -> anyhow::Result<HttpConfig> {
        HttpConfig::from_vars(|name| std::env::var(name).ok())
    }

    /// The configuration from `get`, an environment's variables, for this
    /// build.
    pub fn from_vars(get: impl Fn(&str) -> Option<String>) -> anyhow::Result<HttpConfig> {
        HttpConfig::for_mode(get, crate::auth_mode::disabled())
    }

    /// The configuration from `get` for a build that skips sign-in, or
    /// doesn't: it needs no WorkOS settings, and won't run on Render.
    pub fn for_mode(get: impl Fn(&str) -> Option<String>, auth_disabled: bool) -> anyhow::Result<HttpConfig> {
        let var = |name: &str| get(name).filter(|v| !v.is_empty());
        let cors_origins = var("MCP_CORS_ALLOWED_ORIGINS")
            .unwrap_or_default()
            .split(',')
            .map(|s| s.trim().trim_end_matches('/').to_string())
            .filter(|s| !s.is_empty())
            .collect();
        let port = var("PORT").and_then(|p| p.parse().ok()).unwrap_or(10000);
        let static_site = var("STATIC_SITE_DIR").map(|dir| static_site_dir(&dir)).transpose()?;
        let proxy = ProxyCheck::from_vars(&var)?;
        let token_secret = var("SETUP_TOKEN_SECRET").map(|name| token_secret_name(&name)).transpose()?;
        let session_key = var("SESSION_SECRET").map(|secret| SessionKey::from_secret(&secret)).transpose()?;
        if auth_disabled {
            if var("RENDER_EXTERNAL_URL").is_some() {
                anyhow::bail!(
                    "This build skips sign-in (it was built with AUTH_DISABLED), and RENDER_EXTERNAL_URL says it's on Render: it won't run where the internet can reach it. Deploy a build made without AUTH_DISABLED."
                );
            }
            if var("K_SERVICE").is_some() && proxy.is_none() {
                anyhow::bail!(
                    "This build skips sign-in (it was built with AUTH_DISABLED), and K_SERVICE says it's on Cloud Run: it runs there only behind the IAP proxy. Set ALLOWED_PROXY_EMAIL, ALLOWED_HOST and PROXY_AUDIENCE -- see the README's \"Behind an IAP proxy\" section."
                );
            }
            return Ok(HttpConfig {
                authkit_domain: String::new(),
                resource: var("MCP_PUBLIC_URL").unwrap_or_default(),
                allowed_user_ids: Vec::new(),
                cors_origins,
                port,
                auth_disabled,
                ignored: SIGN_IN_VARS.into_iter().filter(|name| var(name).is_some()).collect(),
                static_site,
                proxy,
                token_secret,
                session_key: None,
            });
        }
        let required = |name: &str, message: &str| var(name).ok_or_else(|| anyhow::anyhow!("{message}"));
        let authkit_domain = required(
            "WORKOS_AUTHKIT_DOMAIN",
            "WORKOS_AUTHKIT_DOMAIN is not set. Set it to your WorkOS AuthKit domain (e.g. \"https://your-tenant.authkit.app\") -- see the README's \"Deploying\" section.",
        )?;
        let resource = match var("MCP_PUBLIC_URL") {
            Some(url) => url,
            None => match var("RENDER_EXTERNAL_URL") {
                Some(url) => format!("{}/mcp", url.trim_end_matches('/')),
                None => anyhow::bail!(
                    "Neither MCP_PUBLIC_URL nor RENDER_EXTERNAL_URL is set. Set MCP_PUBLIC_URL to this server's public MCP endpoint URL (e.g. \"https://your-service.onrender.com/mcp\") -- see the README's \"Deploying\" section."
                ),
            },
        };
        let allowed_user_ids: Vec<String> = var("MCP_ALLOWED_USER_IDS")
            .unwrap_or_default()
            .split(',')
            .map(|s| s.trim().to_string())
            .filter(|s| !s.is_empty())
            .collect();
        if allowed_user_ids.is_empty() {
            anyhow::bail!(
                "MCP_ALLOWED_USER_IDS is not set. Set it to your WorkOS user id (e.g. \"user_01ABC...\", from the Users page of the WorkOS dashboard) -- see the README's \"Deploying\" section."
            );
        }
        Ok(HttpConfig { authkit_domain, resource, allowed_user_ids, cors_origins, port, auth_disabled, ignored: Vec::new(), static_site, proxy, token_secret, session_key })
    }

    /// The domain as an issuer URL (pydantic's AnyHttpUrl gives a bare host
    /// a "/" path), for the protected-resource metadata.
    fn issuer_url(&self) -> String {
        let after_scheme = self.authkit_domain.split_once("://").map(|(_, rest)| rest).unwrap_or(&self.authkit_domain);
        if after_scheme.contains('/') {
            self.authkit_domain.clone()
        } else {
            format!("{}/", self.authkit_domain)
        }
    }

    /// The domain as tokens' issuer: no trailing slash.
    fn issuer(&self) -> String {
        self.authkit_domain.trim_end_matches('/').to_string()
    }

    /// Where a browser reaches this server: MCP_PUBLIC_URL without /mcp,
    /// or the host the proxy forwards.
    pub fn public_base(&self) -> Option<String> {
        if !self.resource.is_empty() {
            return Some(self.resource.trim_end_matches('/').trim_end_matches("/mcp").to_string());
        }
        self.proxy.as_ref().map(|p| format!("https://{}", p.host))
    }

    /// This server's own origin, from its public URL: where the web app
    /// is when the server serves it (STATIC_SITE_DIR).
    fn own_origin(&self) -> Option<String> {
        let (scheme, rest) = self.resource.split_once("://")?;
        let host = rest.split('/').next().filter(|h| !h.is_empty())?;
        Some(format!("{scheme}://{host}"))
    }

    fn resource_metadata_url(&self) -> String {
        let (scheme, rest) = self.resource.split_once("://").unwrap_or(("https", &self.resource));
        let (host, path) = match rest.find('/') {
            Some(i) => (&rest[..i], &rest[i..]),
            None => (rest, ""),
        };
        format!("{scheme}://{host}/.well-known/oauth-protected-resource{path}")
    }
}

struct Shared {
    app: Arc<App>,
    config: HttpConfig,
    sessions: Mutex<HashMap<String, crate::mcp::Session>>,
    keys: Mutex<Vec<Value>>,
    agent: ureq::Agent,
    /// One cookie refresh at a time, so two requests with the same cookie
    /// don't both spend its refresh token.
    refreshing: Mutex<()>,
    /// The sessions lately refreshed, by the refresh token they replaced:
    /// a request still carrying the old cookie gets the new session
    /// instead of a refresh AuthKit would refuse.
    refreshed: Mutex<HashMap<String, (Session, Instant)>>,
}

pub async fn serve(app: Arc<App>) -> anyhow::Result<()> {
    let config = HttpConfig::from_env()?;
    if config.auth_disabled {
        tracing::warn!("{}", crate::auth_mode::WARNING);
        for name in &config.ignored {
            tracing::warn!("{name} is set, but this build skips sign-in, so it's ignored.");
        }
    } else if std::env::var("AUTH_DISABLED").is_ok_and(|v| !v.is_empty()) {
        tracing::warn!("AUTH_DISABLED is set in the environment, but it's read when the server is built, not when it runs: this build requires sign-in as usual.");
    }
    if let Some(dir) = &config.static_site {
        tracing::info!("Serving the static site in {} at /", dir.display());
    }
    if let Some(check) = &config.proxy {
        tracing::info!("Taking only requests through the proxy: {} for {}, Host {}", check.email, check.audience, check.host);
    }
    let port = config.port;
    app.setup.set_base(&config.public_base().unwrap_or_else(|| format!("http://localhost:{port}")));
    if let Some(name) = &config.token_secret {
        tracing::info!("A browser setup adds each new Google token to the secret {name}");
        app.setup.set_token_secret(crate::setup::TokenSecret::new(name));
    }
    if config.session_key.is_some() && !config.auth_disabled {
        tracing::info!("The web app can stay signed in with a cookie, for {} days at a time", session::DAYS);
    }
    let router = router(shared(app, config));
    let listener = tokio::net::TcpListener::bind(("0.0.0.0", port)).await?;
    tracing::info!("Uvicorn running on http://0.0.0.0:{port}");
    axum::serve(listener, router).with_graceful_shutdown(shutdown()).await?;
    Ok(())
}

fn shared(app: Arc<App>, config: HttpConfig) -> Arc<Shared> {
    Arc::new(Shared {
        app,
        config,
        sessions: Mutex::new(HashMap::new()),
        keys: Mutex::new(Vec::new()),
        agent: ureq::AgentBuilder::new().timeout(Duration::from_secs(30)).build(),
        refreshing: Mutex::new(()),
        refreshed: Mutex::new(HashMap::new()),
    })
}

/// The endpoints: MCP, its protected-resource metadata, the OAuth proxy
/// and, with SESSION_SECRET, the web app's sign-in cookie -- behind CORS.
/// A build that skips sign-in has only MCP: nothing to send a client off
/// to sign in.
fn router(shared: Arc<Shared>) -> Router {
    let mut router = Router::new()
        .route("/mcp", get(mcp_get).post(mcp_post).delete(mcp_delete))
        .route("/setup", get(setup_start))
        .route("/setup/callback", get(setup_callback))
        .route("/setup/paste", post(setup_paste))
        .route("/favicon.svg", get(|| async { favicon("image/svg+xml", icon::SVG) }))
        .route("/favicon.png", get(|| async { favicon("image/png", icon::PNG) }))
        .route("/favicon.ico", get(|| async { favicon("image/x-icon", icon::ICO) }));
    if !shared.config.auth_disabled {
        router = router
            .route(PROTECTED_RESOURCE_PATH, get(protected_resource))
            .route("/oauth/register", post(oauth_register))
            .route("/oauth/token", post(oauth_token));
        if shared.config.session_key.is_some() {
            router = router.route("/session", get(session_get).post(session_post).delete(session_delete));
        }
    }
    router
        .method_not_allowed_fallback(method_not_allowed)
        .fallback(site)
        .layer(middleware::from_fn_with_state(shared.clone(), cors))
        .layer(middleware::from_fn_with_state(shared.clone(), through_proxy))
        .with_state(shared)
}

/// The app's icon (crate::mcp::icon), cacheable for a day.
fn favicon(content_type: &'static str, bytes: &'static [u8]) -> Response {
    let mut response = Response::new(Body::from(bytes));
    response.headers_mut().insert("content-type", HeaderValue::from_static(content_type));
    response.headers_mut().insert("cache-control", HeaderValue::from_static("public, max-age=86400"));
    response
}

/// With a proxy check, only what came through the proxy (see
/// crate::mcp::proxy): anything else is refused, the log saying why.
async fn through_proxy(State(shared): State<Arc<Shared>>, request: Request, next: Next) -> Response {
    let problem = shared.config.proxy.as_ref().and_then(|check| {
        let header = |name: &str| request.headers().get(name).and_then(|v| v.to_str().ok());
        let host = header("host").or_else(|| request.uri().authority().map(|a| a.as_str()));
        check.problem(header(proxy::TOKEN_HEADER), host, chrono::Utc::now().timestamp())
    });
    match problem {
        None => next.run(request).await,
        Some(problem) => {
            tracing::warn!("Refused {} {}: not through the proxy -- {problem}", request.method(), request.uri().path());
            text_response(StatusCode::FORBIDDEN, "Forbidden")
        }
    }
}

// -- setting up Google access (crate::setup) ---------------------------------------

/// A setup page: never cached, never sent on as a referrer.
fn setup_page(status: StatusCode, html: String) -> Response {
    let mut response = Response::new(Body::from(html));
    *response.status_mut() = status;
    let headers = response.headers_mut();
    headers.insert("content-type", HeaderValue::from_static("text/html; charset=utf-8"));
    headers.insert("cache-control", HeaderValue::from_static("no-store"));
    headers.insert("referrer-policy", HeaderValue::from_static("no-referrer"));
    response
}

fn query_of(request: &Request) -> Vec<(String, String)> {
    crate::google::consent::query_params(request.uri().query().unwrap_or(""))
}

fn param(params: &[(String, String)], key: &str) -> Option<String> {
    params.iter().find(|(k, _)| k == key).map(|(_, v)| v.clone())
}

/// The cookie `name` the browser sent, if any.
fn cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    let cookies = headers.get_all("cookie").iter().filter_map(|c| c.to_str().ok()).collect::<Vec<_>>().join("; ");
    cookies.split(';').filter_map(|c| c.trim().split_once('=')).find(|(k, _)| *k == name).map(|(_, v)| v.to_string())
}

/// The browser's setup session, from its cookie.
fn setup_session(headers: &HeaderMap) -> Option<String> {
    cookie_value(headers, crate::setup::COOKIE)
}

/// Where the browser reached this server: its configured URL, or the Host
/// it asked for (http on localhost, https elsewhere).
fn browser_base(shared: &Shared, headers: &HeaderMap) -> String {
    if let Some(base) = shared.app.setup.base() {
        return base.to_string();
    }
    let host = headers.get("host").and_then(|h| h.to_str().ok()).unwrap_or("localhost");
    let local = ["localhost", "127.0.0.1", "[::1]"].iter().any(|l| host == *l || host.starts_with(&format!("{l}:")));
    format!("{}://{host}", if local { "http" } else { "https" })
}

/// /setup?link=...: a link from google_setup_link starts a setup, off to
/// Google (or, for a desktop app's client, a page to paste its answer in).
async fn setup_start(State(shared): State<Arc<Shared>>, request: Request) -> Response {
    let link = param(&query_of(&request), "link").unwrap_or_default();
    if !shared.app.setup.take_link(&link) {
        return setup_page(
            StatusCode::FORBIDDEN,
            crate::setup::error_page("This setup link is missing, expired, or already used. Ask for a new one with the google_setup_link tool."),
        );
    }
    let base = browser_base(&shared, request.headers());
    let app = shared.app.clone();
    let started = tokio::task::spawn_blocking(move || -> anyhow::Result<Option<(String, String, bool)>> {
        if crate::setup::need(&app).is_none() {
            return Ok(None);
        }
        app.setup.start(&app, &base).map(Some)
    })
    .await;
    let (session, url, web) = match started {
        Ok(Ok(Some(started))) => started,
        Ok(Ok(None)) => {
            return setup_page(StatusCode::CONFLICT, crate::setup::error_page("Google access already works with the calendar: there's nothing to set up."))
        }
        Ok(Err(error)) => return setup_page(StatusCode::INTERNAL_SERVER_ERROR, crate::setup::error_page(&format!("{error:#}"))),
        Err(error) => return setup_page(StatusCode::INTERNAL_SERVER_ERROR, crate::setup::error_page(&error.to_string())),
    };
    let mut response = if web {
        let mut response = setup_page(StatusCode::SEE_OTHER, String::new());
        if let Ok(location) = HeaderValue::from_str(&url) {
            response.headers_mut().insert("location", location);
        }
        response
    } else {
        setup_page(StatusCode::OK, crate::setup::paste_page(&url))
    };
    let cookie = format!("{}={session}; Path=/setup; HttpOnly; Secure; SameSite=Lax; Max-Age=900", crate::setup::COOKIE);
    if let Ok(cookie) = HeaderValue::from_str(&cookie) {
        response.headers_mut().insert("set-cookie", cookie);
    }
    response
}

/// Finish the browser's setup with Google's answer (its query parameters).
async fn setup_finish(shared: Arc<Shared>, headers: &HeaderMap, answer: Vec<(String, String)>) -> Response {
    if let Some(error) = param(&answer, "error") {
        return setup_page(StatusCode::BAD_REQUEST, crate::setup::error_page(&format!("Google didn't grant access ({error}). Ask google_setup_link for a new link to try again.")));
    }
    let (Some(code), Some(state)) = (param(&answer, "code"), param(&answer, "state")) else {
        return setup_page(StatusCode::BAD_REQUEST, crate::setup::error_page("That isn't Google's answer: it has no code. Copy the whole address the sign-in tab ended on."));
    };
    let Some(session) = setup_session(headers) else {
        return setup_page(
            StatusCode::FORBIDDEN,
            crate::setup::error_page("This browser didn't start this setup (it has no setup cookie): finish in the browser you opened the link in, or ask google_setup_link for a new link."),
        );
    };
    let app = shared.app.clone();
    let finished = tokio::task::spawn_blocking(move || app.setup.finish(&app, &session, &code, &state)).await;
    match finished {
        Ok(Ok(finished)) => {
            tracing::info!("Google access set up: calendar {}{}", finished.calendar_id, if finished.created { " (new)" } else { "" });
            let mut response = setup_page(StatusCode::OK, crate::setup::finished_page(&finished));
            let expired = format!("{}=; Path=/setup; HttpOnly; Secure; SameSite=Lax; Max-Age=0", crate::setup::COOKIE);
            if let Ok(expired) = HeaderValue::from_str(&expired) {
                response.headers_mut().insert("set-cookie", expired);
            }
            response
        }
        Ok(Err(error)) => {
            tracing::warn!("Google setup failed: {error:#}");
            setup_page(StatusCode::BAD_REQUEST, crate::setup::error_page(&format!("{error:#}")))
        }
        Err(error) => setup_page(StatusCode::INTERNAL_SERVER_ERROR, crate::setup::error_page(&error.to_string())),
    }
}

/// /setup/callback: where Google sends a web application client's browser.
async fn setup_callback(State(shared): State<Arc<Shared>>, request: Request) -> Response {
    let answer = query_of(&request);
    let headers = request.headers().clone();
    setup_finish(shared, &headers, answer).await
}

/// /setup/paste: the address a desktop app client's browser landed on.
async fn setup_paste(State(shared): State<Arc<Shared>>, headers: HeaderMap, body: Bytes) -> Response {
    let form = crate::google::consent::query_params(&String::from_utf8_lossy(&body));
    let address = param(&form, "address").unwrap_or_default();
    let query = address.trim().split_once('?').map(|(_, q)| q).unwrap_or("");
    let answer = crate::google::consent::query_params(query.split('#').next().unwrap_or(""));
    setup_finish(shared, &headers, answer).await
}

/// SETUP_TOKEN_SECRET: a secret's resource name, "projects/<p>/secrets/<s>".
fn token_secret_name(name: &str) -> anyhow::Result<String> {
    let name = name.trim();
    let parts: Vec<&str> = name.split('/').collect();
    if parts.len() != 4 || parts[0] != "projects" || parts[2] != "secrets" || parts[1].is_empty() || parts[3].is_empty() {
        anyhow::bail!(
            "SETUP_TOKEN_SECRET is {name:?}, but it should name a Secret Manager secret as \"projects/<project>/secrets/<secret>\" -- see the README's \"Setting up Google access from a browser\" section."
        );
    }
    Ok(name.to_string())
}

/// STATIC_SITE_DIR's directory, which must hold the site's index.html.
fn static_site_dir(dir: &str) -> anyhow::Result<PathBuf> {
    let path = PathBuf::from(dir);
    if !path.join("index.html").is_file() {
        anyhow::bail!(
            "STATIC_SITE_DIR is {dir:?}, but there's no index.html in it. Set it to the directory of a built web app (e.g. the Flutter app's build/web), or unset it -- see the README's \"Serving the web app\" section."
        );
    }
    Ok(path)
}

/// The static site, for whatever the endpoints don't answer: its files
/// for a GET or HEAD -- or, for a page a browser loads that isn't one,
/// its index.html, so the app's own paths survive a reload. Anything
/// else (an API request, a probe of /.well-known/, another method) gets
/// the 404 it gets without a site. Every answer is revalidated, so a new
/// deploy is picked up at once.
async fn site(State(shared): State<Arc<Shared>>, request: Request) -> Response {
    let Some(dir) = shared.config.static_site.clone() else {
        return not_found().await;
    };
    if request.method() != Method::GET && request.method() != Method::HEAD {
        return not_found().await;
    }
    let page = is_page_load(&request);
    let index = dir.join("index.html");
    let fallback = tower::service_fn(move |request: Request| {
        let index = index.clone();
        async move {
            if !page {
                return Ok::<Response, Infallible>(not_found().await);
            }
            Ok(serve_file(&index, request).await)
        }
    });
    let served = tower::ServiceExt::oneshot(tower_http::services::ServeDir::new(&dir).fallback(fallback), request).await;
    let mut response = match served {
        Ok(response) => response.into_response(),
        Err(never) => match never {},
    };
    if response.status().is_success() || response.status() == StatusCode::NOT_MODIFIED {
        response.headers_mut().insert("cache-control", HeaderValue::from_static("no-cache"));
    }
    response
}

/// `request` as a browser's page load: one that takes HTML, not under
/// /.well-known/ (which clients probe, and must find missing).
fn is_page_load(request: &Request) -> bool {
    let accepts_html = request.headers().get("accept").and_then(|a| a.to_str().ok()).is_some_and(|a| a.contains("text/html"));
    accepts_html && !request.uri().path().starts_with("/.well-known/")
}

async fn serve_file(path: &Path, request: Request) -> Response {
    match tower::ServiceExt::oneshot(tower_http::services::ServeFile::new(path), request).await {
        Ok(response) => response.into_response(),
        Err(never) => match never {},
    }
}

/// Render stops a service with SIGTERM: finish what's under way, then exit.
async fn shutdown() {
    let interrupt = tokio::signal::ctrl_c();
    #[cfg(unix)]
    {
        let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("listening for SIGTERM");
        tokio::select! {
            _ = interrupt => {},
            _ = terminate.recv() => {},
        }
    }
    #[cfg(not(unix))]
    {
        let _ = interrupt.await;
    }
}

fn text_response(status: StatusCode, text: &'static str) -> Response {
    let mut response = Response::new(Body::from(text));
    *response.status_mut() = status;
    response.headers_mut().insert("content-type", HeaderValue::from_static("text/plain; charset=utf-8"));
    response
}

async fn not_found() -> Response {
    text_response(StatusCode::NOT_FOUND, "Not Found")
}

async fn method_not_allowed() -> Response {
    text_response(StatusCode::METHOD_NOT_ALLOWED, "Method Not Allowed")
}

/// Whether a page at `origin` may use this server: a local one, one of
/// MCP_CORS_ALLOWED_ORIGINS, or this server's own.
fn allowed_origin(shared: &Shared, origin: &str) -> bool {
    LOCAL_ORIGIN.is_match(origin) || shared.config.cors_origins.iter().any(|o| o == origin) || shared.config.own_origin().as_deref() == Some(origin)
}

/// Whether a request may use the browser's sign-in cookie: a GET, or one
/// whose Origin (which a browser always sends with anything else) is
/// allowed. So a form on another site, which the browser would send the
/// cookie with, can't act as the user -- nor can a page the server's CORS
/// settings don't name.
fn may_use_cookie(shared: &Shared, method: &Method, headers: &HeaderMap) -> bool {
    match headers.get("origin").and_then(|o| o.to_str().ok()) {
        Some(origin) => allowed_origin(shared, origin),
        None => *method == Method::GET,
    }
}

/// Starlette's CORSMiddleware, as with_cors configures it.
async fn cors(State(shared): State<Arc<Shared>>, request: Request, next: Next) -> Response {
    let Some(origin) = request.headers().get("origin").and_then(|o| o.to_str().ok()).map(str::to_string) else {
        return next.run(request).await;
    };
    if request.method() == Method::OPTIONS && request.headers().contains_key("access-control-request-method") {
        let mut failures: Vec<&str> = Vec::new();
        let allowed = allowed_origin(&shared, &origin);
        if !allowed {
            failures.push("origin");
        }
        let method = request.headers().get("access-control-request-method").and_then(|m| m.to_str().ok()).unwrap_or_default();
        if !ALLOWED_METHODS.contains(&method) {
            failures.push("method");
        }
        if let Some(asked) = request.headers().get("access-control-request-headers").and_then(|h| h.to_str().ok()) {
            if asked.split(',').map(|h| h.trim().to_lowercase()).any(|h| !ALLOWED_HEADER_NAMES.contains(&h.as_str())) {
                failures.push("headers");
            }
        }
        if request.headers().contains_key("access-control-request-private-network") {
            failures.push("private-network");
        }
        let (status, text) = if failures.is_empty() {
            (StatusCode::OK, "OK".to_string())
        } else {
            (StatusCode::BAD_REQUEST, format!("Disallowed CORS {}", failures.join(", ")))
        };
        let mut response = Response::new(Body::from(text));
        *response.status_mut() = status;
        let headers = response.headers_mut();
        headers.insert("vary", HeaderValue::from_static("Origin"));
        headers.insert("access-control-allow-methods", HeaderValue::from_static("GET, POST, DELETE"));
        headers.insert("access-control-max-age", HeaderValue::from_static("600"));
        headers.insert("access-control-allow-headers", HeaderValue::from_static(ALLOWED_HEADERS));
        if allowed {
            headers.insert("access-control-allow-origin", HeaderValue::from_str(&origin).unwrap());
            headers.insert("access-control-allow-credentials", HeaderValue::from_static("true"));
        }
        headers.insert("content-type", HeaderValue::from_static("text/plain; charset=utf-8"));
        return response;
    }
    let mut response = next.run(request).await;
    let headers = response.headers_mut();
    headers.insert("access-control-expose-headers", HeaderValue::from_static(EXPOSED_HEADERS));
    if allowed_origin(&shared, &origin) {
        headers.insert("access-control-allow-origin", HeaderValue::from_str(&origin).unwrap());
        // The web app sends its sign-in cookie: the browser shows it the
        // answer only if the server says it takes credentials.
        headers.insert("access-control-allow-credentials", HeaderValue::from_static("true"));
        headers.append("vary", HeaderValue::from_static("Origin"));
    }
    response
}

async fn protected_resource(State(shared): State<Arc<Shared>>) -> Response {
    // Keys in the order the Python SDK writes them, compact.
    let body = format!(
        "{{\"resource\":{},\"authorization_servers\":[{}],\"scopes_supported\":[],\"bearer_methods_supported\":[\"header\"]}}",
        serde_json::to_string(&shared.config.resource).unwrap(),
        serde_json::to_string(&shared.config.issuer_url()).unwrap()
    );
    let mut response = Response::new(Body::from(body));
    response.headers_mut().insert("content-type", HeaderValue::from_static("application/json"));
    response.headers_mut().insert("cache-control", HeaderValue::from_static("public, max-age=3600"));
    response
}

fn unauthorized(shared: &Shared) -> Response {
    let mut response = Response::new(Body::from("{\"error\": \"invalid_token\", \"error_description\": \"Authentication required\"}"));
    *response.status_mut() = StatusCode::UNAUTHORIZED;
    response.headers_mut().insert("content-type", HeaderValue::from_static("application/json"));
    response.headers_mut().insert(
        "www-authenticate",
        HeaderValue::from_str(&format!(
            "Bearer error=\"invalid_token\", error_description=\"Authentication required\", resource_metadata=\"{}\"",
            shared.config.resource_metadata_url()
        ))
        .unwrap(),
    );
    response
}

/// What verifying a request found: whether it's allowed, and the
/// Set-Cookie its answer carries, if the browser's sign-in cookie is to
/// change (renewed, refreshed, or dropped as no good).
struct Auth {
    ok: bool,
    set_cookie: Option<String>,
}

impl Auth {
    fn allowed() -> Auth {
        Auth { ok: true, set_cookie: None }
    }

    fn denied() -> Auth {
        Auth { ok: false, set_cookie: None }
    }

    /// Denied, and the browser's cookie dropped: it's no good any more.
    fn signed_out() -> Auth {
        Auth { ok: false, set_cookie: Some(session::clear_cookie()) }
    }

    /// Allowed, with the browser's cookie replaced by `session`.
    fn renewed(key: &SessionKey, session: &Session) -> Auth {
        Auth { ok: true, set_cookie: Some(session::set_cookie(&key.seal(session))) }
    }

    /// `response` with the cookie change, if any.
    fn apply(&self, mut response: Response) -> Response {
        if let Some(cookie) = self.set_cookie.as_deref().and_then(|c| HeaderValue::from_str(c).ok()) {
            response.headers_mut().insert("set-cookie", cookie);
        }
        response
    }
}

/// WorkOSTokenVerifier: the token's signature against AuthKit's keys, its
/// issuer, audience and expiry, and its user among those allowed. The
/// token is the request's bearer token or, failing that, the one in the
/// web app's sign-in cookie (session_auth). A build that skips sign-in
/// trusts every request, token or not.
async fn verify(shared: &Arc<Shared>, method: &Method, headers: &HeaderMap) -> Auth {
    if shared.config.auth_disabled {
        return Auth::allowed();
    }
    let bearer = headers
        .get("authorization")
        .and_then(|h| h.to_str().ok())
        .and_then(|h| h.strip_prefix("Bearer ").or_else(|| h.strip_prefix("bearer ")))
        .map(str::to_string);
    if let Some(token) = bearer {
        let shared = shared.clone();
        return tokio::task::spawn_blocking(move || match check_token(&shared, &token) {
            Ok(Some(_)) => Auth::allowed(),
            Ok(None) => Auth::denied(),
            Err(reason) => {
                tracing::warn!(
                    "Rejected bearer token: {reason} (expected issuer={}, audience={})",
                    crate::py::repr(&shared.config.issuer()),
                    crate::py::repr(&shared.config.resource)
                );
                Auth::denied()
            }
        })
        .await
        .unwrap_or(Auth::denied());
    }
    let (Some(key), Some(cookie)) = (shared.config.session_key.clone(), cookie_value(headers, session::COOKIE)) else {
        return Auth::denied();
    };
    if !may_use_cookie(shared, method, headers) {
        tracing::warn!("Rejected a request with the sign-in cookie: its Origin {} isn't allowed", crate::py::repr(headers.get("origin").and_then(|o| o.to_str().ok()).unwrap_or("")));
        return Auth::denied();
    }
    let shared = shared.clone();
    tokio::task::spawn_blocking(move || session_auth(&shared, &key, &cookie)).await.unwrap_or(Auth::denied())
}

/// The sign-in cookie's session: allowed while its access token verifies
/// (renewing the cookie hourly, so the session slides), refreshed through
/// AuthKit once the token expires, and signed out when it has ended, was
/// tampered with, can't be refreshed or its user is no longer allowed.
fn session_auth(shared: &Shared, key: &SessionKey, cookie: &str) -> Auth {
    let Some(session) = key.open(cookie) else {
        tracing::warn!("Rejected a sign-in cookie: not one this server sealed");
        return Auth::signed_out();
    };
    let now = chrono::Utc::now().timestamp();
    if session.ended(now) {
        tracing::info!("The sign-in cookie's session ended: signing the browser out");
        return Auth::signed_out();
    }
    if session::token_expiry(&session.access_token).is_some_and(|exp| exp <= now + session::REFRESH_AHEAD) {
        return match refresh_session(shared, &session, now) {
            Ok(fresh) => Auth::renewed(key, &fresh),
            Err(reason) => {
                tracing::warn!("Couldn't refresh the sign-in cookie's session: {reason}; signing the browser out");
                Auth::signed_out()
            }
        };
    }
    match check_token(shared, &session.access_token) {
        Ok(Some(_)) if session.due_for_renewal(now) => Auth::renewed(key, &session.issued_at(now)),
        Ok(Some(_)) => Auth::allowed(),
        Ok(None) => Auth::signed_out(),
        Err(reason) => {
            tracing::warn!("Rejected the sign-in cookie's token: {reason}");
            Auth::signed_out()
        }
    }
}

/// How long a refreshed session answers for the cookie it replaced.
const REFRESHED_FOR: Duration = Duration::from_secs(120);

/// `session` with new tokens from AuthKit, issued at `now`: one refresh
/// at a time, and a cookie lately refreshed gets the session that
/// replaced it (AuthKit rotates refresh tokens, so refreshing it again
/// would fail).
fn refresh_session(shared: &Shared, session: &Session, now: i64) -> Result<Session, String> {
    let _one_at_a_time = shared.refreshing.lock().unwrap_or_else(|e| e.into_inner());
    {
        let mut refreshed = shared.refreshed.lock().unwrap_or_else(|e| e.into_inner());
        refreshed.retain(|_, (_, at)| at.elapsed() < REFRESHED_FOR);
        if let Some((fresh, _)) = refreshed.get(&session.refresh_token) {
            return Ok(fresh.clone());
        }
    }
    let (status, answer) = authkit_token(
        shared,
        &[("grant_type", "refresh_token"), ("refresh_token", &session.refresh_token), ("client_id", &session.client_id), ("resource", &shared.config.resource)],
    )?;
    if status != 200 {
        return Err(format!("AuthKit answered {status}: {}", answer.get("error").and_then(Value::as_str).unwrap_or("no error given")));
    }
    let fresh = session_from(shared, &answer, &session.client_id, now).map_err(|(_, _, description)| description)?;
    if fresh.user != session.user {
        return Err(format!("the refreshed token is for {}, not {}", crate::py::repr(&fresh.user), crate::py::repr(&session.user)));
    }
    shared.refreshed.lock().unwrap_or_else(|e| e.into_inner()).insert(session.refresh_token.clone(), (fresh.clone(), Instant::now()));
    Ok(fresh)
}

/// A session from AuthKit's token answer, issued at `now`, for the
/// allowed user it names -- or the status, OAuth error and description
/// to answer with.
fn session_from(shared: &Shared, answer: &Value, client_id: &str, now: i64) -> Result<Session, (StatusCode, &'static str, String)> {
    let Some(access_token) = answer.get("access_token").and_then(Value::as_str) else {
        return Err((StatusCode::BAD_GATEWAY, "server_error", "AuthKit's answer has no access token".into()));
    };
    let Some(refresh_token) = answer.get("refresh_token").and_then(Value::as_str) else {
        return Err((StatusCode::BAD_GATEWAY, "server_error", "AuthKit's answer has no refresh token: the client must be registered for the refresh_token grant".into()));
    };
    let user = match check_token(shared, access_token) {
        Ok(Some(user)) => user,
        Ok(None) => return Err((StatusCode::FORBIDDEN, "access_denied", "This user isn't allowed to use this server".into())),
        Err(reason) => return Err((StatusCode::UNAUTHORIZED, "invalid_token", format!("The token AuthKit issued doesn't verify: {reason}"))),
    };
    Ok(Session { access_token: access_token.into(), refresh_token: refresh_token.into(), client_id: client_id.into(), user, issued: 0, until: 0 }.issued_at(now))
}

/// AuthKit's token endpoint's answer to `form`: its status and JSON.
fn authkit_token(shared: &Shared, form: &[(&str, &str)]) -> Result<(u16, Value), String> {
    let url = format!("{}/oauth2/token", shared.config.issuer());
    let answered = match shared.agent.post(&url).set("User-Agent", PROXY_USER_AGENT).set("Accept", "application/json").send_form(form) {
        Ok(response) => response,
        Err(ureq::Error::Status(_, response)) => response,
        Err(error) => return Err(format!("AuthKit couldn't be reached: {error}")),
    };
    let status = answered.status();
    let text = answered.into_string().map_err(|e| format!("AuthKit's answer couldn't be read: {e}"))?;
    Ok((status, serde_json::from_str(&text).unwrap_or(Value::Null)))
}

/// The token's user, if it verifies and the user is allowed: None for a
/// good token of someone else's.
fn check_token(shared: &Shared, token: &str) -> Result<Option<String>, String> {
    let header = jsonwebtoken::decode_header(token).map_err(|e| e.to_string())?;
    let kid = header.kid.ok_or_else(|| "the token has no kid".to_string())?;
    let key = signing_key(shared, &kid)?;
    let mut validation = jsonwebtoken::Validation::new(jsonwebtoken::Algorithm::RS256);
    validation.leeway = 0;
    validation.set_issuer(&[shared.config.issuer()]);
    validation.set_audience(&[shared.config.resource.clone()]);
    validation.set_required_spec_claims(&["exp", "iss", "aud"]);
    let claims = jsonwebtoken::decode::<Value>(token, &key, &validation).map_err(|e| e.to_string())?.claims;
    let user = claims.get("sub").and_then(Value::as_str).unwrap_or_default();
    if !shared.config.allowed_user_ids.iter().any(|u| u == user) {
        tracing::warn!("Rejected bearer token for user {}: not in MCP_ALLOWED_USER_IDS", crate::py::repr(user));
        return Ok(None);
    }
    Ok(Some(user.to_string()))
}

/// AuthKit's signing key `kid`: from the cached keys, fetching them again
/// once if it isn't there.
fn signing_key(shared: &Shared, kid: &str) -> Result<jsonwebtoken::DecodingKey, String> {
    let find = |keys: &[Value]| keys.iter().find(|k| k.get("kid").and_then(Value::as_str) == Some(kid)).cloned();
    let cached = find(&shared.keys.lock().unwrap_or_else(|e| e.into_inner()));
    let jwk = match cached {
        Some(jwk) => jwk,
        None => {
            let url = format!("{}/oauth2/jwks", shared.config.issuer());
            let set: Value = shared.agent.get(&url).call().map_err(|e| e.to_string())?.into_json().map_err(|e| e.to_string())?;
            let keys = set.get("keys").and_then(Value::as_array).cloned().unwrap_or_default();
            *shared.keys.lock().unwrap_or_else(|e| e.into_inner()) = keys.clone();
            find(&keys).ok_or_else(|| format!("Unable to find a signing key that matches: \"{kid}\""))?
        }
    };
    let jwk: jsonwebtoken::jwk::Jwk = serde_json::from_value(jwk).map_err(|e| e.to_string())?;
    jsonwebtoken::DecodingKey::from_jwk(&jwk).map_err(|e| e.to_string())
}

fn session_id_of(headers: &HeaderMap) -> Option<String> {
    headers.get("mcp-session-id").and_then(|h| h.to_str().ok()).map(str::to_string)
}

fn json_error(status: StatusCode, code: i64, message: &str) -> Response {
    let body = json!({"jsonrpc": "2.0", "id": "server-error", "error": {"code": code, "message": message}});
    let mut response = Response::new(Body::from(body.to_string()));
    *response.status_mut() = status;
    response.headers_mut().insert("content-type", HeaderValue::from_static("application/json"));
    response
}

async fn mcp_post(State(shared): State<Arc<Shared>>, headers: HeaderMap, body: Bytes) -> Response {
    let started = Instant::now();
    let auth = verify(&shared, &Method::POST, &headers).await;
    if !auth.ok {
        return auth.apply(unauthorized(&shared));
    }
    let accept = headers.get("accept").and_then(|a| a.to_str().ok()).unwrap_or_default();
    if !(accept.contains("application/json") && accept.contains("text/event-stream")) {
        return json_error(StatusCode::NOT_ACCEPTABLE, -32600, "Not Acceptable: Client must accept both application/json and text/event-stream");
    }
    let message: Value = match serde_json::from_slice(&body) {
        Ok(message) => message,
        Err(_) => return json_error(StatusCode::BAD_REQUEST, -32700, "Parse error: Invalid JSON"),
    };
    let is_initialize = message.get("method").and_then(Value::as_str) == Some("initialize");
    let session_id = match session_id_of(&headers) {
        Some(id) => {
            if !shared.sessions.lock().unwrap_or_else(|e| e.into_inner()).contains_key(&id) {
                return json_error(StatusCode::NOT_FOUND, -32001, "Session not found");
            }
            id
        }
        None if is_initialize => {
            if shared.config.auth_disabled {
                tracing::warn!("New session, unauthenticated: {}", crate::auth_mode::WARNING);
            }
            let id = uuid::Uuid::new_v4().simple().to_string();
            shared.sessions.lock().unwrap_or_else(|e| e.into_inner()).insert(id.clone(), crate::mcp::Session::new());
            id
        }
        None => return json_error(StatusCode::BAD_REQUEST, -32600, "Bad Request: Missing session ID"),
    };
    let is_request = message.get("id").is_some() && message.get("method").is_some();
    let handled = {
        let shared = shared.clone();
        let session_id = session_id.clone();
        // In flight until the closure ends (or is dropped unrun): the last
        // to end trims the allocator.
        let in_flight = crate::allocator::CALLS.start();
        tokio::task::spawn_blocking(move || {
            let _in_flight = in_flight;
            let mut session = {
                let sessions = shared.sessions.lock().unwrap_or_else(|e| e.into_inner());
                crate::mcp::Session { initialized: sessions.get(&session_id).is_some_and(|s| s.initialized) }
            };
            let mut timings = Vec::new();
            let response = crate::mcp::handle(&shared.app, &mut session, &message, &mut timings);
            if let Some(stored) = shared.sessions.lock().unwrap_or_else(|e| e.into_inner()).get_mut(&session_id) {
                stored.initialized |= session.initialized;
            }
            (response, timings)
        })
        .await
    };
    let (response, timings) = handled.unwrap_or((None, Vec::new()));
    let mut out = if !is_request || response.is_none() {
        let mut accepted = Response::new(Body::empty());
        *accepted.status_mut() = StatusCode::ACCEPTED;
        accepted.headers_mut().insert("content-type", HeaderValue::from_static("application/json"));
        accepted
    } else {
        let data = serde_json::to_string(&response.unwrap()).unwrap();
        let mut stream = Response::new(Body::from(format!("event: message\r\ndata: {data}\r\n\r\n")));
        stream.headers_mut().insert("content-type", HeaderValue::from_static("text/event-stream"));
        stream.headers_mut().insert("cache-control", HeaderValue::from_static("no-cache, no-transform"));
        stream
    };
    out.headers_mut().insert("mcp-session-id", HeaderValue::from_str(&session_id).unwrap());
    // with_health: each tool call with the whole request's time.
    let total_ms = started.elapsed().as_millis() as i64;
    for (name, work_ms, ok) in timings {
        shared.app.health.record_call(&name, work_ms, Some(total_ms.max(work_ms)), ok);
    }
    auth.apply(out)
}

async fn mcp_get(State(shared): State<Arc<Shared>>, headers: HeaderMap) -> Response {
    let auth = verify(&shared, &Method::GET, &headers).await;
    if !auth.ok {
        return auth.apply(unauthorized(&shared));
    }
    let Some(id) = session_id_of(&headers) else {
        return json_error(StatusCode::BAD_REQUEST, -32600, "Bad Request: Missing session ID");
    };
    if !shared.sessions.lock().unwrap_or_else(|e| e.into_inner()).contains_key(&id) {
        return json_error(StatusCode::NOT_FOUND, -32001, "Session not found");
    }
    // The stream for server-sent requests and notifications: this server
    // sends none, so it's held open, quiet.
    let stream = futures_lite_pending();
    let mut response = Response::new(Body::from_stream(stream));
    response.headers_mut().insert("content-type", HeaderValue::from_static("text/event-stream"));
    response.headers_mut().insert("cache-control", HeaderValue::from_static("no-cache, no-transform"));
    response.headers_mut().insert("mcp-session-id", HeaderValue::from_str(&id).unwrap());
    auth.apply(response)
}

fn futures_lite_pending() -> impl futures_core::Stream<Item = Result<Bytes, std::io::Error>> + Send {
    struct Pending;
    impl futures_core::Stream for Pending {
        type Item = Result<Bytes, std::io::Error>;
        fn poll_next(self: std::pin::Pin<&mut Self>, _: &mut std::task::Context<'_>) -> std::task::Poll<Option<Self::Item>> {
            std::task::Poll::Pending
        }
    }
    Pending
}

async fn mcp_delete(State(shared): State<Arc<Shared>>, headers: HeaderMap) -> Response {
    let auth = verify(&shared, &Method::DELETE, &headers).await;
    if !auth.ok {
        return auth.apply(unauthorized(&shared));
    }
    let Some(id) = session_id_of(&headers) else {
        return json_error(StatusCode::BAD_REQUEST, -32600, "Bad Request: Missing session ID");
    };
    if shared.sessions.lock().unwrap_or_else(|e| e.into_inner()).remove(&id).is_none() {
        return json_error(StatusCode::NOT_FOUND, -32001, "Session not found");
    }
    StatusCode::OK.into_response()
}

// -- the web app's sign-in cookie (crate::mcp::session) ----------------------------------

/// An OAuth-style JSON error, never cached.
fn oauth_error(status: StatusCode, error: &str, description: &str) -> Response {
    let mut response = Response::new(Body::from(json!({"error": error, "error_description": description}).to_string()));
    *response.status_mut() = status;
    response.headers_mut().insert("content-type", HeaderValue::from_static("application/json"));
    response.headers_mut().insert("cache-control", HeaderValue::from_static("no-store"));
    response
}

fn no_content() -> Response {
    let mut response = Response::new(Body::empty());
    *response.status_mut() = StatusCode::NO_CONTENT;
    response.headers_mut().insert("cache-control", HeaderValue::from_static("no-store"));
    response
}

/// POST /session: the web app, back from AuthKit with an authorization
/// code, trades it for tokens -- which stay here, sealed in the cookie
/// the answer sets, never in the page. The body is JSON: the code, the
/// PKCE code_verifier, and the redirect_uri and client_id the app
/// registered. AuthKit's refusal passes through as it is.
async fn session_post(State(shared): State<Arc<Shared>>, headers: HeaderMap, body: Bytes) -> Response {
    if !may_use_cookie(&shared, &Method::POST, &headers) {
        return oauth_error(StatusCode::FORBIDDEN, "access_denied", "This page's origin isn't allowed to sign in here");
    }
    if body.len() > MAX_PROXY_BODY {
        return oauth_error(StatusCode::PAYLOAD_TOO_LARGE, "invalid_request", "Request body too large");
    }
    let request: Value = serde_json::from_slice(&body).unwrap_or(Value::Null);
    let field = |name: &str| request.get(name).and_then(Value::as_str).filter(|v| !v.is_empty()).map(str::to_string);
    let (Some(code), Some(verifier), Some(redirect_uri), Some(client_id)) = (field("code"), field("code_verifier"), field("redirect_uri"), field("client_id")) else {
        return oauth_error(StatusCode::BAD_REQUEST, "invalid_request", "The body must be JSON with code, code_verifier, redirect_uri and client_id");
    };
    let Some(key) = shared.config.session_key.clone() else {
        return not_found().await;
    };
    let shared = shared.clone();
    let answered = tokio::task::spawn_blocking(move || {
        let now = chrono::Utc::now().timestamp();
        let (status, answer) = authkit_token(
            &shared,
            &[
                ("grant_type", "authorization_code"),
                ("code", &code),
                ("code_verifier", &verifier),
                ("redirect_uri", &redirect_uri),
                ("client_id", &client_id),
                ("resource", &shared.config.resource),
            ],
        )
        .map_err(|reason| (StatusCode::BAD_GATEWAY, "server_error", reason))?;
        if status != 200 {
            return Err((StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY), "", answer.to_string()));
        }
        session_from(&shared, &answer, &client_id, now).map(|session| key.seal(&session))
    })
    .await;
    match answered {
        Ok(Ok(sealed)) => {
            tracing::info!("The web app signed in; its session lasts {} days at a time", session::DAYS);
            let mut response = no_content();
            if let Ok(cookie) = HeaderValue::from_str(&session::set_cookie(&sealed)) {
                response.headers_mut().insert("set-cookie", cookie);
            }
            response
        }
        // AuthKit's own refusal, as it is.
        Ok(Err((status, "", body))) => {
            let mut response = Response::new(Body::from(body));
            *response.status_mut() = status;
            response.headers_mut().insert("content-type", HeaderValue::from_static("application/json"));
            response.headers_mut().insert("cache-control", HeaderValue::from_static("no-store"));
            response
        }
        Ok(Err((status, error, description))) => {
            tracing::warn!("The web app's sign-in failed: {description}");
            oauth_error(status, error, &description)
        }
        Err(error) => oauth_error(StatusCode::INTERNAL_SERVER_ERROR, "server_error", &error.to_string()),
    }
}

/// GET /session: whether the browser is signed in -- 204, with the cookie
/// renewed or refreshed as a request would, or 401, dropping a cookie
/// that's no good.
async fn session_get(State(shared): State<Arc<Shared>>, headers: HeaderMap) -> Response {
    let auth = verify(&shared, &Method::GET, &headers).await;
    if auth.ok {
        auth.apply(no_content())
    } else {
        auth.apply(oauth_error(StatusCode::UNAUTHORIZED, "invalid_token", "Not signed in"))
    }
}

/// DELETE /session: signs the browser out by dropping its cookie. The
/// tokens in it stay valid at AuthKit until they expire, but nothing
/// holds them any more.
async fn session_delete(State(shared): State<Arc<Shared>>, headers: HeaderMap) -> Response {
    if !may_use_cookie(&shared, &Method::DELETE, &headers) {
        return oauth_error(StatusCode::FORBIDDEN, "access_denied", "This page's origin isn't allowed to sign out here");
    }
    Auth::signed_out().apply(no_content())
}

// -- the OAuth proxy ---------------------------------------------------------------------

async fn oauth_register(State(shared): State<Arc<Shared>>, headers: HeaderMap, body: Bytes) -> Response {
    forward(shared, "/oauth2/register", headers, body).await
}

async fn oauth_token(State(shared): State<Arc<Shared>>, headers: HeaderMap, body: Bytes) -> Response {
    forward(shared, "/oauth2/token", headers, body).await
}

/// The request to AuthKit unchanged (its Content-Type and
/// Accept), AuthKit's answer back unchanged (its status, Content-Type and
/// Cache-Control, and body).
async fn forward(shared: Arc<Shared>, upstream: &'static str, headers: HeaderMap, body: Bytes) -> Response {
    if body.len() > MAX_PROXY_BODY {
        let mut response = Response::new(Body::from("Request body too large"));
        *response.status_mut() = StatusCode::PAYLOAD_TOO_LARGE;
        return response;
    }
    let url = format!("{}{upstream}", shared.config.issuer());
    let content_type = headers.get("content-type").and_then(|h| h.to_str().ok()).map(str::to_string);
    let accept = headers.get("accept").and_then(|h| h.to_str().ok()).map(str::to_string);
    let agent = shared.agent.clone();
    let result = tokio::task::spawn_blocking(move || {
        let mut request = agent.post(&url).set("User-Agent", PROXY_USER_AGENT);
        // urllib sends a form's content type for a body with none given.
        request = request.set("Content-Type", content_type.as_deref().unwrap_or("application/x-www-form-urlencoded"));
        if let Some(accept) = &accept {
            request = request.set("Accept", accept);
        }
        let answered = match request.send_bytes(&body) {
            Ok(response) => response,
            Err(ureq::Error::Status(_, response)) => response,
            Err(error) => return Err(error.to_string()),
        };
        let status = answered.status();
        let kept: Vec<(&'static str, String)> = [("content-type", "Content-Type"), ("cache-control", "Cache-Control")]
            .into_iter()
            .filter_map(|(lower, name)| answered.header(name).map(|v| (lower, v.to_string())))
            .collect();
        let mut content = Vec::new();
        std::io::Read::read_to_end(&mut answered.into_reader(), &mut content).map_err(|e| e.to_string())?;
        Ok((status, kept, content))
    })
    .await;
    match result {
        Ok(Ok((status, kept, content))) => {
            let mut response = Response::new(Body::from(content));
            *response.status_mut() = StatusCode::from_u16(status).unwrap_or(StatusCode::BAD_GATEWAY);
            for (name, value) in kept {
                if let Ok(value) = HeaderValue::from_str(&value) {
                    response.headers_mut().insert(name, value);
                }
            }
            response
        }
        _ => text_response(StatusCode::INTERNAL_SERVER_ERROR, "Internal Server Error"),
    }
}

#[cfg(test)]
#[path = "http_tests.rs"]
mod tests;
