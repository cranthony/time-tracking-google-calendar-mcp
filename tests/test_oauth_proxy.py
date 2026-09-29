import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from oauth_proxy import REGISTER_PATH, TOKEN_PATH, oauth_proxy_handlers


class _FakeAuthKit(BaseHTTPRequestHandler):
    """Records each request and answers like AuthKit: JSON, and a 400 for
    a refresh token it doesn't know."""

    requests: list[dict] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"])).decode()
        _FakeAuthKit.requests.append(
            {
                "path": self.path,
                "body": body,
                "content_type": self.headers["Content-Type"],
                "user_agent": self.headers["User-Agent"],
            }
        )
        status = 400 if "refresh_token=unknown" in body else 200
        payload = json.dumps({"error": "invalid_grant"} if status == 400 else {"ok": self.path}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie", "upstream=1")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def authkit():
    _FakeAuthKit.requests = []
    server = HTTPServer(("127.0.0.1", 0), _FakeAuthKit)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()


@pytest.fixture
def client(authkit):
    handlers = oauth_proxy_handlers(authkit)
    app = Starlette(routes=[Route(path, handler, methods=["POST"]) for path, handler in handlers.items()])
    return TestClient(app)


def test_forwards_registration_to_authkit(client):
    response = client.post(REGISTER_PATH, json={"client_name": "Time Tracker (web)"})

    assert response.status_code == 200
    assert response.json() == {"ok": "/oauth2/register"}
    assert _FakeAuthKit.requests == [
        {
            "path": "/oauth2/register",
            "body": '{"client_name":"Time Tracker (web)"}',
            "content_type": "application/json",
            "user_agent": "time-tracking-google-calendar-mcp oauth-proxy",
        }
    ]


def test_forwards_token_request_to_authkit(client):
    response = client.post(TOKEN_PATH, data={"grant_type": "authorization_code", "code": "c"})

    assert response.json() == {"ok": "/oauth2/token"}
    assert _FakeAuthKit.requests[0]["path"] == "/oauth2/token"
    assert _FakeAuthKit.requests[0]["body"] == "grant_type=authorization_code&code=c"
    assert _FakeAuthKit.requests[0]["content_type"] == "application/x-www-form-urlencoded"


def test_passes_oauth_errors_through(client):
    response = client.post(TOKEN_PATH, data={"grant_type": "refresh_token", "refresh_token": "unknown"})

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_grant"}


def test_keeps_only_safe_response_headers(client):
    response = client.post(REGISTER_PATH, json={})

    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers


def test_rejects_oversized_bodies_without_forwarding(client):
    response = client.post(REGISTER_PATH, content=b"x" * (64 * 1024 + 1))

    assert response.status_code == 413
    assert _FakeAuthKit.requests == []
