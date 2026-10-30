"""The server hosted over streamable HTTP, as on Render: its OAuth
protected-resource metadata, bearer tokens checked against WorkOS
AuthKit's keys (served by the fake, signed here), only allowed users let
in, CORS for browser clients, and the OAuth proxy to AuthKit.

Each exchange -- what was sent, and the status, the headers that matter
and the body that came back -- is recorded and compared with the golden
record, like the stdio scenarios' calls.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from conformance.conftest import check_golden
from conformance.fake_google import FakeGoogle
from conformance.harness import DEFAULT_NOW, FakeGoogleServer, ROOT, _free_port, server_command, unversioned, argument_errors

ALLOWED = "user_01ALLOWED"
OTHER = "user_01STRANGER"
ORIGIN = "https://app.example"
KEPT_HEADERS = ("content-type", "www-authenticate", "access-control-allow-origin", "access-control-allow-methods", "access-control-allow-headers", "access-control-expose-headers", "access-control-allow-credentials", "cache-control", "set-cookie", "x-dropped")
SESSION_SECRET = "a conformance secret, long enough to be taken as one"
COOKIE = "time_tracker_session"


class HttpServer:
    def __init__(self, google: FakeGoogle, calendar_id: str, fake: FakeGoogleServer, tmp: Path) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        google.jwks = {"keys": [{**public, "kid": "test-key", "use": "sig", "alg": "RS256"}]}
        self.issuer = fake.root.rstrip("/")
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.resource = f"{self.base}/mcp"
        token_path = tmp / "token.json"
        token_path.write_text(json.dumps({"token": "fake-access-0", "refresh_token": "fake-refresh", "token_uri": f"{fake.root}token", "client_id": "c", "client_secret": "s", "scopes": ["https://www.googleapis.com/auth/calendar.app.created", "https://www.googleapis.com/auth/drive.file"], "expiry": "2099-01-01T00:00:00Z"}))
        env = {
            **os.environ,
            "GOOGLE_API_ROOT": fake.root,
            "GOOGLE_CALENDAR_ID": calendar_id,
            "GOOGLE_OAUTH_TOKEN_PATH": str(token_path),
            "GOOGLE_OAUTH_CREDENTIALS_PATH": str(tmp / "credentials.json"),
            "MCP_TRANSPORT": "streamable-http",
            "PORT": str(self.port),
            "WORKOS_AUTHKIT_DOMAIN": self.issuer + "/",
            "MCP_PUBLIC_URL": self.resource,
            "MCP_ALLOWED_USER_IDS": f"{ALLOWED}, user_01SECOND",
            "MCP_CORS_ALLOWED_ORIGINS": ORIGIN,
            "SESSION_SECRET": SESSION_SECRET,
            "TIME_TRACKER_NOW": DEFAULT_NOW,
            "TIME_TRACKER_DETERMINISTIC_IDS": "1",
            "TIME_TRACKER_HEALTH_WRITER": "off",
            "TIME_TRACKER_SERIES_RENEWAL": "off",
            "PYTHONIOENCODING": "utf-8",
        }
        self._log = open(os.environ.get("TIME_TRACKER_SERVER_LOG") or tmp / "server.log", "w+", encoding="utf-8")
        self.process = subprocess.Popen(server_command(), env=env, cwd=str(ROOT), stdout=self._log, stderr=subprocess.STDOUT)
        self.exchanges: list[dict] = []
        self._session_id: str | None = None
        self._next_id = 0
        # Generous: a Python server starting alongside other scenarios is slow.
        deadline = time.monotonic() + 180
        while True:
            try:
                urllib.request.urlopen(f"{self.base}/.well-known/oauth-protected-resource/mcp", timeout=2).read()
                break
            except (urllib.error.URLError, ConnectionError):
                if self.process.poll() is not None or time.monotonic() > deadline:
                    self._log.seek(0)
                    raise RuntimeError("server didn't start:\n" + self._log.read()[-5000:])
                time.sleep(0.2)

    def token(self, *, sub: str = ALLOWED, audience: str | None = None, issuer: str | None = None, expires_in: int = 3600, key=None, kid: str = "test-key") -> str:
        now = int(time.time())
        claims = {"sub": sub, "iss": issuer or self.issuer, "aud": audience or self.resource, "iat": now, "exp": now + expires_in, "client_id": "client_test", "scope": "openid"}
        return jwt.encode(claims, key or self.key, algorithm="RS256", headers={"kid": kid})

    def fetch(self, method: str, path: str, *, headers: dict | None = None, body: bytes | None = None, record_body: bool = True) -> tuple[int, dict, bytes]:
        request = urllib.request.Request(self.base + path, data=body, method=method, headers=headers or {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, timeout=60) as response:
                status, response_headers, content = response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            status, response_headers, content = error.code, error.headers, error.read()
        kept = {name: response_headers[name] for name in KEPT_HEADERS if response_headers.get(name) is not None}
        if "www-authenticate" in kept:
            kept["www-authenticate"] = kept["www-authenticate"].replace(self.base, "<server>")
        if "set-cookie" in kept:
            # The sealed session differs each time (a fresh nonce).
            kept["set-cookie"] = re.sub(rf"^{COOKIE}=[^;]+", f"{COOKIE}=<sealed>", kept["set-cookie"])
        masked = {"authorization": "<token>", "mcp-session-id": "<session>", "cookie": "<cookie>"}
        sent = {k: masked.get(k.lower(), v) for k, v in (headers or {}).items()}
        self.exchanges.append({
            "request": {"method": method, "path": path, "headers": sent, "body": (body or b"").decode()},
            "status": status,
            "headers": kept,
            "body": _normalized(content.decode(errors="replace"), self.base, self.issuer) if record_body else "<not recorded>",
        })
        return status, dict(response_headers), content

    def rpc(self, method: str, params: dict | None = None, *, token: str | None = None, cookie: str | None = None, origin: str | None = ORIGIN) -> tuple[int, dict | None]:
        """A request with a bearer `token`, or the sign-in `cookie` (from
        the page at `origin`, as a browser would say)."""
        self._next_id += 1
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if cookie is not None:
            headers["Cookie"] = cookie
            if origin is not None:
                headers["Origin"] = origin
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        status, response_headers, content = self.fetch("POST", "/mcp", headers=headers, body=json.dumps({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params or {}}).encode())
        session_id = next((v for k, v in response_headers.items() if k.lower() == "mcp-session-id"), None)
        if session_id:
            self._session_id = session_id
        if status != 200:
            return status, None
        text = content.decode()
        if text.startswith("{"):
            return status, json.loads(text)
        data = [line[len("data:"):].strip() for line in text.splitlines() if line.startswith("data:")]
        return status, json.loads(data[-1]) if data else None

    def initialize(self, token: str | None = None, *, cookie: str | None = None) -> None:
        status, result = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "conformance", "version": "1"}}, token=token, cookie=cookie)
        assert status == 200, status
        self.rpc_notify("notifications/initialized", token, cookie=cookie)

    def rpc_notify(self, method: str, token: str | None, *, cookie: str | None = None) -> None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Mcp-Session-Id": self._session_id or "", "MCP-Protocol-Version": "2025-06-18"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if cookie is not None:
            headers.update({"Cookie": cookie, "Origin": ORIGIN})
        self.fetch("POST", "/mcp", headers=headers, body=json.dumps({"jsonrpc": "2.0", "method": method}).encode())

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self._log.close()


def _normalized(text: str, base: str, issuer: str) -> str:
    """Addresses that change from run to run, made fixed -- as they are,
    and as a form (a token request's body) encodes them."""
    text = text.replace(base, "<server>").replace(issuer, "<authkit>")
    text = text.replace(urllib.parse.quote(base, safe=""), "<server>").replace(urllib.parse.quote(issuer, safe=""), "<authkit>")
    text = unversioned(text)
    return "".join(_with_argument_errors(line) for line in text.splitlines(keepends=True))


def _with_argument_errors(line: str) -> str:
    """An SSE `data:` line carrying an MCP response, with a tool's refusal
    of its arguments compared by field and kind only (see
    harness.argument_errors) -- the rest of the line left exactly as sent."""
    if not line.startswith("data: "):
        return line
    ending = line[len(line.rstrip("\r\n")):]
    try:
        message = json.loads(line[len("data: "):])
    except ValueError:
        return line
    content = message.get("result", {}).get("content") if isinstance(message, dict) else None
    if not isinstance(content, list) or not any(argument_errors(c.get("text", "")) != c.get("text", "") for c in content):
        return line
    for item in content:
        item["text"] = argument_errors(item.get("text", ""))
    return "data: " + json.dumps(message, separators=(",", ":"), ensure_ascii=False) + ending


@pytest.fixture
def http(request, google, calendar_id):
    with FakeGoogleServer(google) as fake, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        server = HttpServer(google, calendar_id, fake, Path(tmp))
        try:
            yield server
        finally:
            server.stop()
        record = {
            "exchanges": server.exchanges,
            "authkit_requests": [{**r, "user_agent": r["user_agent"]} for r in google.authkit_requests],
            "google_requests": [r.as_dict() for r in google.requests],
        }
    check_golden(request, json.loads(_normalized(json.dumps(record), server.base, server.issuer)))


def test_protected_resource_metadata(http):
    status, _, content = http.fetch("GET", "/.well-known/oauth-protected-resource/mcp")
    assert status == 200
    metadata = json.loads(content)
    assert metadata["resource"] == http.resource
    assert http.issuer in json.dumps(metadata["authorization_servers"])


def test_tokens(http):
    # None, malformed, wrong key, wrong audience, wrong issuer, expired, unknown key.
    assert http.rpc("tools/list")[0] == 401
    assert http.rpc("tools/list", token="not-a-jwt")[0] == 401
    stranger_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert http.rpc("tools/list", token=http.token(key=stranger_key))[0] == 401
    assert http.rpc("tools/list", token=http.token(audience="https://elsewhere.example/mcp"))[0] == 401
    assert http.rpc("tools/list", token=http.token(issuer="https://evil.example"))[0] == 401
    assert http.rpc("tools/list", token=http.token(expires_in=-60))[0] == 401
    assert http.rpc("tools/list", token=http.token(kid="other-key"))[0] == 401
    # A real token for someone not allowed.
    assert http.rpc("tools/list", token=http.token(sub=OTHER))[0] == 401
    # The allowed users get in.
    for user in (ALLOWED, "user_01SECOND"):
        token = http.token(sub=user)
        http._session_id = None
        http.initialize(token)
        status, listed = http.rpc("tools/list", token=token)
        assert status == 200 and len(listed["result"]["tools"]) == 82


def test_tool_calls_over_http(http):
    token = http.token()
    http.initialize(token)
    status, result = http.rpc("tools/call", {"name": "create_location", "arguments": {"location": {"name": "Home"}}}, token=token)
    assert status == 200 and not result["result"].get("isError")
    status, result = http.rpc("tools/call", {"name": "get_location", "arguments": {"id_or_name": "nowhere"}}, token=token)
    assert result["result"]["isError"] is True
    status, result = http.rpc("tools/call", {"name": "no_such_tool", "arguments": {}}, token=token)
    assert status == 200
    status, result = http.rpc("tools/call", {"name": "create_location", "arguments": {"location": "not an object"}}, token=token)
    assert status == 200


def test_cors(http):
    for origin in (ORIGIN, "http://localhost:5173", "http://127.0.0.1:8080", "https://evil.example"):
        http.fetch("OPTIONS", "/mcp", headers={"Origin": origin, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization, content-type, mcp-session-id, mcp-protocol-version"})
    status, headers, _ = http.fetch("POST", "/mcp", headers={"Origin": ORIGIN, "Content-Type": "application/json"}, body=b"{}")
    assert status == 401
    assert headers.get("access-control-allow-origin") == ORIGIN or headers.get("Access-Control-Allow-Origin") == ORIGIN


def _set_cookie(headers: dict) -> str | None:
    return next((v for k, v in headers.items() if k.lower() == "set-cookie"), None)


def test_session(http, google):
    """The web app's sign-in cookie (the README's "Keeping the web app
    signed in"): set by POST /session for an allowed user from an allowed
    origin, taken by /mcp in place of a bearer token, refreshed through
    AuthKit once its token expires, dropped when that fails, and
    dropped by DELETE /session."""
    def issue(form: dict) -> tuple[int, dict]:
        issued = lambda access, refresh: (200, {"access_token": access, "refresh_token": refresh, "expires_in": 3600, "token_type": "Bearer"})
        if form.get("grant_type") == "authorization_code":
            match form.get("code"):
                case "good":
                    return issued(http.token(), "refresh-good")
                case "expiring":
                    return issued(http.token(expires_in=20), "refresh-good")
                case "doomed":
                    return issued(http.token(expires_in=20), "refresh-dead")
                case "stranger":
                    return issued(http.token(sub=OTHER), "refresh-good")
            return 400, {"error": "invalid_grant", "error_description": "The code is unknown"}
        if form.get("grant_type") == "refresh_token" and form.get("refresh_token") == "refresh-good":
            return issued(http.token(), "refresh-next")
        return 400, {"error": "invalid_grant"}
    google.authkit_tokens = issue

    def sign_in(code: str, origin: str | None = ORIGIN) -> tuple[int, dict, bytes]:
        headers = {"Content-Type": "application/json", **({"Origin": origin} if origin else {})}
        body = {"code": code, "code_verifier": "verifier", "redirect_uri": f"{ORIGIN}/callback.html", "client_id": "client_web"}
        return http.fetch("POST", "/session", headers=headers, body=json.dumps(body).encode())

    # No origin, another site's, a stranger's code, a code AuthKit refuses: no cookie.
    assert sign_in("good", origin=None)[0] == 403
    assert sign_in("good", origin="https://evil.example")[0] == 403
    status, headers, _ = sign_in("stranger")
    assert status == 403 and _set_cookie(headers) is None
    assert sign_in("bad")[0] == 400
    assert http.fetch("POST", "/session", headers={"Content-Type": "application/json", "Origin": ORIGIN}, body=b'{"code": "good"}')[0] == 400
    # Signed in: the cookie stands in for the bearer token, from an allowed origin.
    status, headers, _ = sign_in("good")
    assert status == 204
    set_cookie = _set_cookie(headers)
    assert set_cookie.startswith(f"{COOKIE}=") and "HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie, set_cookie
    cookie = set_cookie.split(";")[0]
    http.initialize(cookie=cookie)
    status, listed = http.rpc("tools/list", cookie=cookie)
    assert status == 200 and len(listed["result"]["tools"]) == 82
    assert http.rpc("tools/list", cookie=cookie, origin=None)[0] == 401
    assert http.rpc("tools/list", cookie=cookie, origin="https://evil.example")[0] == 401
    assert http.fetch("GET", "/session", headers={"Cookie": cookie})[0] == 204
    assert http.fetch("GET", "/session")[0] == 401
    # A token about to expire is refreshed through the cookie, the answer setting a new one.
    status, headers, _ = sign_in("expiring")
    expiring = _set_cookie(headers).split(";")[0]
    http._session_id = None
    status, headers, _ = http.fetch("POST", "/mcp", headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18", "Cookie": expiring, "Origin": ORIGIN}, body=json.dumps({"jsonrpc": "2.0", "id": 99, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "conformance", "version": "1"}}}).encode())
    assert status == 200 and _set_cookie(headers).startswith(f"{COOKIE}=") and "Max-Age=0" not in _set_cookie(headers)
    # One that can't be refreshed is dropped.
    status, headers, _ = sign_in("doomed")
    doomed = _set_cookie(headers).split(";")[0]
    status, headers, _ = http.fetch("GET", "/session", headers={"Cookie": doomed})
    assert status == 401 and "Max-Age=0" in _set_cookie(headers)
    # Signing out drops the cookie.
    status, headers, _ = http.fetch("DELETE", "/session", headers={"Origin": ORIGIN})
    assert status == 204 and "Max-Age=0" in _set_cookie(headers)
    assert http.fetch("DELETE", "/session")[0] == 403


def test_oauth_proxy(http):
    status, headers, content = http.fetch("POST", "/oauth/register", headers={"Content-Type": "application/json", "Accept": "application/json", "X-Extra": "dropped"}, body=b'{"client_name": "web app", "redirect_uris": ["https://app.example/cb"]}')
    assert status == 201
    status, _, _ = http.fetch("POST", "/oauth/token", headers={"Content-Type": "application/x-www-form-urlencoded"}, body=b"grant_type=authorization_code&code=invalid")
    assert status == 400
    status, _, _ = http.fetch("POST", "/oauth/token", headers={"Content-Type": "application/json"}, body=b"x" * (64 * 1024 + 1))
    assert status == 413
    status, _, _ = http.fetch("GET", "/oauth/token")
    assert status == 405
