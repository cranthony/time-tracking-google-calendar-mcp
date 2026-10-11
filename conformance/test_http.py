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
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from conformance.conftest import check_golden
from conformance.fake_google import FakeGoogle
from conformance.harness import DEFAULT_NOW, FakeGoogleServer, ROOT, _free_port, server_command, unversioned

ALLOWED = "user_01ALLOWED"
OTHER = "user_01STRANGER"
ORIGIN = "https://app.example"
KEPT_HEADERS = ("content-type", "www-authenticate", "access-control-allow-origin", "access-control-allow-methods", "access-control-allow-headers", "access-control-expose-headers", "access-control-allow-credentials", "cache-control", "x-dropped")


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
            "CTT_NOW": DEFAULT_NOW,
            "CTT_DETERMINISTIC_IDS": "1",
            "CTT_HEALTH_WRITER": "off",
            "PYTHONIOENCODING": "utf-8",
        }
        self._log = open(os.environ.get("CTT_SERVER_LOG") or tmp / "server.log", "w+", encoding="utf-8")
        self.process = subprocess.Popen(server_command(), env=env, cwd=str(ROOT), stdout=self._log, stderr=subprocess.STDOUT)
        self.exchanges: list[dict] = []
        self._session_id: str | None = None
        self._next_id = 0
        deadline = time.monotonic() + 60
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
        masked = {"authorization": "<token>", "mcp-session-id": "<session>"}
        sent = {k: masked.get(k.lower(), v) for k, v in (headers or {}).items()}
        self.exchanges.append({
            "request": {"method": method, "path": path, "headers": sent, "body": (body or b"").decode()},
            "status": status,
            "headers": kept,
            "body": _normalized(content.decode(errors="replace"), self.base, self.issuer) if record_body else "<not recorded>",
        })
        return status, dict(response_headers), content

    def rpc(self, method: str, params: dict | None = None, *, token: str | None = None) -> tuple[int, dict | None]:
        self._next_id += 1
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
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

    def initialize(self, token: str) -> None:
        status, result = self.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "conformance", "version": "1"}}, token=token)
        assert status == 200, status
        self.rpc_notify("notifications/initialized", token)

    def rpc_notify(self, method: str, token: str) -> None:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "Authorization": f"Bearer {token}", "Mcp-Session-Id": self._session_id or "", "MCP-Protocol-Version": "2025-06-18"}
        self.fetch("POST", "/mcp", headers=headers, body=json.dumps({"jsonrpc": "2.0", "method": method}).encode())

    def stop(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self._log.close()


def _normalized(text: str, base: str, issuer: str) -> str:
    """Addresses that change from run to run, made fixed."""
    return unversioned(text.replace(base, "<server>").replace(issuer, "<authkit>"))


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
        assert status == 200 and len(listed["result"]["tools"]) == 62


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


def test_oauth_proxy(http):
    status, headers, content = http.fetch("POST", "/oauth/register", headers={"Content-Type": "application/json", "Accept": "application/json", "X-Extra": "dropped"}, body=b'{"client_name": "web app", "redirect_uris": ["https://app.example/cb"]}')
    assert status == 201
    status, _, _ = http.fetch("POST", "/oauth/token", headers={"Content-Type": "application/x-www-form-urlencoded"}, body=b"grant_type=authorization_code&code=invalid")
    assert status == 400
    status, _, _ = http.fetch("POST", "/oauth/token", headers={"Content-Type": "application/json"}, body=b"x" * (64 * 1024 + 1))
    assert status == 413
    status, _, _ = http.fetch("GET", "/oauth/token")
    assert status == 405
