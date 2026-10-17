"""Run a server under test against conformance/fake_google.py, over MCP.

The server is the Rust build in rust/target (debug, or else release --
`cargo build` first), or whatever command line `TIME_TRACKER_SERVER` names. It's
started as a subprocess over stdio, with its Google API root pointed at
the fake, a fixed clock (`TIME_TRACKER_NOW`), deterministic ids
(`TIME_TRACKER_DETERMINISTIC_IDS`) and no background health writer
(`TIME_TRACKER_HEALTH_WRITER=off`) -- see rust/src/determinism.rs.

What each call returned, the requests the server sent Google, and the
fake's state afterwards are all recorded on the `Session`, for a
scenario to check and for conftest.py to compare with its golden record.
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
    spec = os.environ.get("TIME_TRACKER_SERVER")
    if spec:
        return shlex.split(spec, posix=os.name != "nt")
    name = "time-tracker-server.exe" if os.name == "nt" else "time-tracker-server"
    for profile in ("debug", "release"):
        built = ROOT / "rust" / "target" / profile / name
        if built.exists():
            return [str(built)]
    raise RuntimeError("No Rust server build: run `cargo build --manifest-path rust/Cargo.toml` first, or set TIME_TRACKER_SERVER")


@dataclass
class Call:
    tool: str
    arguments: dict
    is_error: bool
    content: Any
    """The structured result if there is one, else the text content."""

    text: str = ""
    """The text content, which is what a model reads: for a structured
    result, its JSON as the server rendered it."""

    def as_dict(self) -> dict:
        return {"tool": self.tool, "arguments": self.arguments, "is_error": self.is_error, "content": self.content, "text": self.text}


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

    async def _read(self, tool: str, arguments: dict) -> Any:
        """What `tool`, a read, gives -- not recorded as a call."""
        result = await self._call_tool(tool, arguments)
        if result.is_error or result.structured_content is None:
            return None
        content = result.structured_content
        return content["result"] if set(content) == {"result"} else content

    async def _version(self, kind: str, item_id: str) -> Any:
        """The version the `kind` `item_id` is now, as a client reads it
        before changing it; None if there's none."""
        single = {
            "action": ("get_action", "id_or_name"),
            "action_group": ("get_action_group", "id_or_name"),
            "person": ("get_person", "id_or_name"),
            "circle": ("get_circle", "id_or_name"),
            "location": ("get_location", "id_or_name"),
            "location_group": ("get_location_group", "id_or_name"),
            "habit": ("get_habit", "id_or_name"),
            "goal": ("get_goal", "id_or_title"),
            "event": ("get_event", "id"),
        }
        listed = {
            "trait": ("get_traits", {"statuses": ["active", "off", "archived"]}),
            "travel_time": ("get_travel_times", {}),
            "note": ("get_notes", {"include_compacted": True}),
        }
        key = "etag" if kind == "event" else "version"
        if kind in single:
            tool, name = single[kind]
            found = await self._read(tool, {name: item_id})
            return found.get(key) if isinstance(found, dict) else None
        if kind in listed:
            tool, args = listed[kind]
            for item in await self._read(tool, args) or []:
                if isinstance(item, dict) and item.get("id") == item_id:
                    return item.get(key)
        return None

    async def _seen(self, tool: str, arguments: dict) -> dict:
        """[arguments], with the version last read of each item [tool]
        changes filled in where they give none -- as a client sends a
        change, having read what it changes."""
        arguments = json.loads(json.dumps(arguments))

        async def fill(at: Any, kind: str, id_key: str, key: str) -> None:
            if not isinstance(at, dict) or not isinstance(at.get(id_key), str) or at.get(key) is not None:
                return
            version = await self._version(kind, at[id_key])
            if version is not None:
                at[key] = version

        if tool in ("update_event", "delete_event"):
            for update in arguments.get("updates") or []:
                await fill(update.get("event"), "event", "id", "etag")
            for cancel in arguments.get("cancels") or []:
                await fill(cancel, "event", "event_id", "etag")
        elif tool == "update_goals":
            for update in arguments.get("updates") or []:
                await fill(update.get("goal"), "goal", "id", "version")
            for delete in arguments.get("deletes") or []:
                await fill(delete, "goal", "goal_id", "if_version")
        elif tool == "edit_note":
            await fill(arguments, "note", "note_id", "version")
        elif tool == "delete_note":
            await fill(arguments, "note", "note_id", "if_version")
        else:
            rows = {
                "update_action": ("action", "action"),
                "update_action_group": ("group", "action_group"),
                "update_person": ("person", "person"),
                "update_circle": ("circle", "circle"),
                "update_location": ("location", "location"),
                "update_location_group": ("group", "location_group"),
                "update_habit": ("habit", "habit"),
                "update_trait": ("trait", "trait"),
                "update_travel_time": ("travel_time", "travel_time"),
                "update_goal": ("goal", "goal"),
            }
            deletes = {
                "delete_action_group": ("group_id", "action_group"),
                "delete_circle": ("circle_id", "circle"),
                "delete_location": ("location_id", "location"),
                "delete_location_group": ("group_id", "location_group"),
                "delete_travel_time": ("travel_time_id", "travel_time"),
                "delete_goal": ("goal_id", "goal"),
            }
            if tool in rows:
                argument, kind = rows[tool]
                await fill(arguments.get(argument), kind, "id", "version")
            if tool in deletes:
                id_key, kind = deletes[tool]
                await fill(arguments, kind, id_key, "if_version")
        return arguments

    async def _call(self, tool: str, arguments: dict) -> Call:
        arguments = await self._seen(tool, arguments)
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
        text = "\n".join(texts)
        if tool == "get_health" and not result.is_error:
            content = _masked_timings(content)
            text = json.dumps(_masked_timings(json.loads(text))) if text.startswith("{") else text
        if isinstance(content, str):
            content = argument_errors(unversioned(content))
        content = masked_http_errors(content)
        text = masked_http_errors(argument_errors(unversioned(text)))
        call = Call(tool, arguments, bool(result.is_error), content, text)
        self.calls.append(call)
        return call

    def record(self) -> dict:
        return masked_http_errors({
            "calls": [c.as_dict() for c in self.calls],
            "requests": [r.as_dict() for r in self.google.requests],
            "state": self.google.snapshot(skip_tabs=("Health",)),
        })


def unversioned(text: str) -> str:
    """`text` with the pydantic version taken out of its error links
    (".../2.13/v/missing"): it's whichever pydantic got installed, not
    something the server decides."""
    return re.sub(r"errors\.pydantic\.dev/[0-9.]+/", "errors.pydantic.dev/<version>/", text)


_HTTP_ERROR = re.compile(r"<HttpError (\d+) when requesting [^>]*>")


def masked_http_errors(value: Any) -> Any:
    """`value` with every Google API error a message quotes ("<HttpError
    410 when requesting http://127.0.0.1:<port>/... returned ...>") down to
    its status: the fake's port differs from run to run, and each server's
    client words the rest its own way."""
    if isinstance(value, str):
        return _HTTP_ERROR.sub(r"<HttpError \1>", value)
    if isinstance(value, dict):
        return {k: masked_http_errors(v) for k, v in value.items()}
    if isinstance(value, list):
        return [masked_http_errors(v) for v in value]
    return value


_PYDANTIC_ERRORS = re.compile(r"^(Error executing tool \w+: )\d+ validation errors? for \w+Arguments\n(.*)$", re.S)
_PYDANTIC_ERROR = re.compile(r"^(\S[^\n]*)\n  [^\n]*\[type=(\w+)", re.M)
_RUST_ERRORS = re.compile(r"^(Error executing tool \w+: )invalid arguments\n(.*)$", re.S)
_RUST_ERROR = re.compile(r"^- ([^:\n]+): [^\n]*\[type=(\w+)\]$", re.M)


def argument_errors(text: str) -> str:
    """An error for arguments the tool's schema refused, as just which
    fields and what kind of error ("location.unknown_field
    [unexpected_keyword_argument]"): the Python server words them as
    pydantic does, the Rust one more simply -- by the user's choice, only
    the fields and the kinds (pydantic's names for them) must agree."""
    for whole, each in ((_PYDANTIC_ERRORS, _PYDANTIC_ERROR), (_RUST_ERRORS, _RUST_ERROR)):
        match = whole.match(text)
        if match:
            found = sorted(f"{loc} [{kind}]" for loc, kind in each.findall(match.group(2)))
            return f"{match.group(1)}invalid arguments: " + "; ".join(found)
    return text


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
            "TIME_TRACKER_NOW": now,
            "TIME_TRACKER_DETERMINISTIC_IDS": "1",
            "TIME_TRACKER_HEALTH_WRITER": "off",
            "PYTHONIOENCODING": "utf-8",
            **(env or {}),
        }
        command = server_command()
        params = StdioServerParameters(command=command[0], args=command[1:], env=server_env, cwd=str(ROOT))
        # TIME_TRACKER_SERVER_LOG keeps the server's stderr (its logs), for debugging.
        with open(os.environ.get("TIME_TRACKER_SERVER_LOG") or Path(tmp) / "stderr.log", "w+", encoding="utf-8") as errlog:
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
