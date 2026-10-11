"""Run a server under test against conformance/fake_google.py, over MCP.

The server is whatever `CTT_SERVER` names: "python" (the default) runs
`python server.py`; anything else is a command line for another build
(the Rust port's binary, say). Either way it's started as a subprocess
over stdio, with its Google API root pointed at the fake, a fixed clock
(`CTT_NOW`), deterministic ids (`CTT_DETERMINISTIC_IDS`) and no
background health writer (`CTT_HEALTH_WRITER=off`) -- see
utilities/determinism.py.

What each call returned, the requests the server sent Google, and the
fake's state afterwards are all recorded on the `Session`, for a
scenario to check and for `CTT_RECORD`/`CTT_COMPARE` to compare across
servers (see conftest.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import socket
import sys
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import uvicorn
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from conformance.fake_google import FakeGoogle

ROOT = Path(__file__).resolve().parent.parent

DEFAULT_NOW = "2026-10-07T15:30:00-04:00"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeGoogleServer:
    """A FakeGoogle served on localhost, in a background thread."""

    def __init__(self, google: FakeGoogle) -> None:
        self.google = google
        self.port = _free_port()
        self._server = uvicorn.Server(uvicorn.Config(google.app(), host="127.0.0.1", port=self.port, log_level="warning", timeout_keep_alive=3600))
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    @property
    def root(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def __enter__(self) -> "FakeGoogleServer":
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("fake Google didn't start")
            time.sleep(0.01)
        return self

    def __exit__(self, *exc) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


def server_command() -> list[str]:
    spec = os.environ.get("CTT_SERVER", "python")
    if spec == "python":
        return [sys.executable, str(ROOT / "server.py")]
    return shlex.split(spec, posix=os.name != "nt")


@dataclass
class Call:
    tool: str
    arguments: dict
    is_error: bool
    content: Any
    """The structured result if there is one, else the text content."""

    def as_dict(self) -> dict:
        return {"tool": self.tool, "arguments": self.arguments, "is_error": self.is_error, "content": self.content}


@dataclass
class Session:
    google: FakeGoogle
    calendar_id: str
    client: ClientSession
    loop: asyncio.AbstractEventLoop | None = None
    """The loop `client` lives on, if not the caller's (see `ServerThread`)."""
    calls: list[Call] = field(default_factory=list)

    async def _call_tool(self, tool: str, arguments: dict):
        if self.loop is None:
            return await self.client.call_tool(tool, arguments)
        future = asyncio.run_coroutine_threadsafe(self.client.call_tool(tool, arguments), self.loop)
        return await asyncio.wrap_future(future)

    async def call(self, tool: str, **arguments: Any) -> Any:
        """Call `tool`, returning its result -- raising ToolFailed if it
        answered with an error."""
        call = await self._call(tool, arguments)
        if call.is_error:
            raise ToolFailed(tool, call.content)
        return call.content

    async def attempt(self, tool: str, **arguments: Any) -> Call:
        """Call `tool`, whether it succeeds or not: for a case whose outcome
        is whatever the golden record says it is."""
        return await self._call(tool, arguments)

    async def call_error(self, tool: str, **arguments: Any) -> str:
        """Call `tool`, expecting an error; returns its text."""
        call = await self._call(tool, arguments)
        assert call.is_error, f"{tool} succeeded: {call.content!r}"
        return call.content

    async def _call(self, tool: str, arguments: dict) -> Call:
        result = await self._call_tool(tool, arguments)
        texts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
        if result.is_error:
            content: Any = "\n".join(texts)
        elif result.structured_content is not None:
            content = result.structured_content
            # A tool returning a plain value (a list, a str) has it wrapped.
            if set(content) == {"result"}:
                content = content["result"]
        else:
            content = "\n".join(texts)
        if tool == "get_health" and not result.is_error:
            content = _masked_timings(content)
        if isinstance(content, str):
            content = unversioned(content)
        call = Call(tool, arguments, bool(result.is_error), content)
        self.calls.append(call)
        return call

    def record(self) -> dict:
        return {
            "calls": [c.as_dict() for c in self.calls],
            "requests": [r.as_dict() for r in self.google.requests],
            "state": self.google.snapshot(skip_tabs=("Health",)),
        }


def unversioned(text: str) -> str:
    """`text` with the pydantic version taken out of its error links
    (".../2.13/v/missing"): it's whichever pydantic got installed, not
    something the server decides."""
    return re.sub(r"errors\.pydantic\.dev/[0-9.]+/", "errors.pydantic.dev/<version>/", text)


_TIMING_KEYS = {"at", "work_ms", "total_ms", "overhead_ms", "median_ms", "p95_ms", "max_ms", "rss_mib", "memory_latest_mib", "memory_min_mib", "memory_max_mib"}


def _masked_timings(value: Any) -> Any:
    """get_health's result with its times and sizes -- which differ from
    run to run -- replaced by what kind of value each was, so its shape and
    counts are still compared."""
    if isinstance(value, dict):
        masked = {k: (type(v).__name__ if k in _TIMING_KEYS and v is not None else _masked_timings(v)) for k, v in value.items()}
        for key in masked:
            if key.startswith("memory"):
                # Whether memory can be measured depends on the platform.
                masked[key] = "<memory>"
        return masked
    if isinstance(value, list):
        return [_masked_timings(v) for v in value]
    return value


class ToolFailed(AssertionError):
    def __init__(self, tool: str, text: str) -> None:
        super().__init__(f"{tool} failed: {text}")
        self.text = text


@asynccontextmanager
async def running_server(google: FakeGoogle, calendar_id: str, *, now: str = DEFAULT_NOW, env: dict[str, str] | None = None):
    with FakeGoogleServer(google) as fake, tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        token_path = Path(tmp) / "token.json"
        token_path.write_text(json.dumps({
            "token": "fake-access-0",
            "refresh_token": "fake-refresh",
            "token_uri": f"{fake.root}token",
            "client_id": "fake-client",
            "client_secret": "fake-secret",
            "scopes": ["https://www.googleapis.com/auth/calendar.app.created", "https://www.googleapis.com/auth/drive.file"],
            "expiry": "2099-01-01T00:00:00Z",
        }))
        credentials_path = Path(tmp) / "credentials.json"
        credentials_path.write_text(json.dumps({"installed": {"client_id": "fake-client", "client_secret": "fake-secret", "token_uri": f"{fake.root}token", "auth_uri": f"{fake.root}auth", "redirect_uris": ["http://localhost"]}}))
        server_env = {
            **os.environ,
            "GOOGLE_API_ROOT": fake.root,
            "GOOGLE_CALENDAR_ID": calendar_id,
            "GOOGLE_OAUTH_TOKEN_PATH": str(token_path),
            "GOOGLE_OAUTH_CREDENTIALS_PATH": str(credentials_path),
            "MCP_TRANSPORT": "stdio",
            "CTT_NOW": now,
            "CTT_DETERMINISTIC_IDS": "1",
            "CTT_HEALTH_WRITER": "off",
            "PYTHONIOENCODING": "utf-8",
            **(env or {}),
        }
        command = server_command()
        params = StdioServerParameters(command=command[0], args=command[1:], env=server_env, cwd=str(ROOT))
        # CTT_SERVER_LOG keeps the server's stderr (its logs), for debugging.
        with open(os.environ.get("CTT_SERVER_LOG") or Path(tmp) / "stderr.log", "w+", encoding="utf-8") as errlog:
            try:
                async with stdio_client(params, errlog=errlog) as (read, write):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        yield Session(google, calendar_id, client)
            except BaseException:
                errlog.seek(0)
                sys.stderr.write(errlog.read()[-20000:])
                raise


class ServerThread:
    """`running_server` on an event loop of its own, in a background
    thread -- so a pytest fixture can start it in setup and stop it in
    teardown (which pytest-asyncio may run in different tasks, and anyio's
    cancel scopes must be left in the task that entered them)."""

    def __init__(self, google: FakeGoogle, calendar_id: str, **kwargs) -> None:
        self._args = (google, calendar_id)
        self._kwargs = kwargs
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._stop: asyncio.Event | None = None
        self._task = None

    def start(self) -> Session:
        self._thread.start()
        ready: "asyncio.Future[Session]" = asyncio.run_coroutine_threadsafe(self._make_ready(), self._loop).result()
        self._task = asyncio.run_coroutine_threadsafe(self._serve(ready), self._loop)
        try:
            return asyncio.run_coroutine_threadsafe(self._wait(ready), self._loop).result(timeout=120)
        except BaseException:
            if self._task.done():
                self._task.result()
            raise

    async def _make_ready(self):
        self._stop = asyncio.Event()
        return self._loop.create_future()

    async def _wait(self, ready):
        return await ready

    async def _serve(self, ready) -> None:
        try:
            async with running_server(*self._args, **self._kwargs) as session:
                session.loop = self._loop
                ready.set_result(session)
                await self._stop.wait()
        except BaseException as error:
            if not ready.done():
                ready.set_exception(error)
            raise

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self._stop.set)
        try:
            self._task.result(timeout=60)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
