"""Forwarding a browser-based client's OAuth calls to WorkOS AuthKit.

A client running in a web page (e.g. the Time Tracker web app) can't call
AuthKit's registration and token endpoints itself: they answer without
CORS headers, so the browser hides every response from the page. Clients
that run anywhere else (Claude.ai's servers, the desktop and Android apps)
call AuthKit directly and never touch these routes.

So this server offers the two endpoints on its own origin, which already
allows cross-origin calls (see server.with_cors), and forwards each
request to AuthKit unchanged, handing back AuthKit's response unchanged.
The upstream URLs are fixed from WORKOS_AUTHKIT_DOMAIN, so this can't be
pointed anywhere else. Nothing here needs auth: registration and the
token endpoint are unauthenticated in OAuth anyway, which is exactly why
AuthKit exposes them publicly too.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable
from email.message import Message

from starlette.requests import Request
from starlette.responses import Response

REGISTER_PATH = "/oauth/register"
TOKEN_PATH = "/oauth/token"

_UPSTREAM_PATHS = {REGISTER_PATH: "/oauth2/register", TOKEN_PATH: "/oauth2/token"}
_REQUEST_HEADERS = ("Content-Type", "Accept")
_RESPONSE_HEADERS = ("Content-Type", "Cache-Control", "Pragma")
# Cloudflare, in front of AuthKit, rejects urllib's default User-Agent
# ("Python-urllib/3.x") on the registration endpoint (error 1010).
_USER_AGENT = "time-tracking-google-calendar-mcp oauth-proxy"
_MAX_BODY_BYTES = 64 * 1024
_TIMEOUT_SECONDS = 30

Handler = Callable[[Request], Awaitable[Response]]


def oauth_proxy_handlers(authkit_domain: str) -> dict[str, Handler]:
    """A POST handler per path this server should offer, keyed by path."""
    base = authkit_domain.rstrip("/")
    return {path: _forwarder(base + upstream) for path, upstream in _UPSTREAM_PATHS.items()}


def _forwarder(url: str) -> Handler:
    async def forward(request: Request) -> Response:
        body = await request.body()
        if len(body) > _MAX_BODY_BYTES:
            return Response("Request body too large", status_code=413)
        headers = {name: request.headers[name] for name in _REQUEST_HEADERS if name in request.headers}
        # urllib is blocking, so run it off the event loop -- the same
        # approach workos_auth takes for its own calls to AuthKit.
        status, response_headers, content = await asyncio.to_thread(_post, url, body, headers)
        return Response(content, status_code=status, headers=response_headers)

    return forward


def _post(url: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(
        url, data=body, headers={**headers, "User-Agent": _USER_AGENT}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return response.status, _kept_headers(response.headers), response.read()
    except urllib.error.HTTPError as error:
        # An OAuth error response (e.g. 400 invalid_grant) is still a
        # response the client needs to see, not a failure of ours.
        return error.code, _kept_headers(error.headers), error.read()


def _kept_headers(headers: Message) -> dict[str, str]:
    return {name: value for name in _RESPONSE_HEADERS if (value := headers.get(name)) is not None}
