"""Fixtures for the conformance scenarios.

Each scenario gets a fresh fake Google holding one calendar, and the
server under test (see harness.py) running against it. When the scenario
ends, what happened -- every call's result, every request the server
sent Google, and Google's state after -- is compared with the scenario's
golden record in conformance/golden/: a server that differs from it in
any of those fails. The records were written by the original Python
server, which the Rust one was ported from and held to; the scenarios'
own assertions say what that behaviour is meant to be.

    python -m pytest -n auto                       # the Rust build in rust/target
    CTT_SERVER=path/to/ctt-server python -m pytest -n auto
    CTT_UPDATE_GOLDEN=1 python -m pytest           # rewrite the records

Only rewrite the records for a deliberate change of behaviour, and read
the diff: whatever it shows is what the server now does differently.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from conformance.fake_google import FakeGoogle
from conformance.harness import DEFAULT_NOW, ServerThread

GOLDEN = Path(__file__).resolve().parent / "golden"


def _first_difference(expected, actual, path="$"):
    if type(expected) is not type(actual):
        return f"{path}: expected {json.dumps(expected)[:300]}, got {json.dumps(actual)[:300]}"
    if isinstance(expected, dict):
        for key in sorted(set(expected) | set(actual), key=str):
            if key not in actual:
                return f"{path}.{key}: missing (expected {json.dumps(expected[key])[:300]})"
            if key not in expected:
                return f"{path}.{key}: unexpected {json.dumps(actual[key])[:300]}"
            found = _first_difference(expected[key], actual[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(expected, list):
        for index, (e, a) in enumerate(zip(expected, actual)):
            found = _first_difference(e, a, f"{path}[{index}]")
            if found:
                return found
        if len(expected) != len(actual):
            extra = actual[len(expected):] or expected[len(actual):]
            return f"{path}: expected {len(expected)} items, got {len(actual)} (first extra: {json.dumps(extra[0])[:300]})"
        return None
    if isinstance(expected, str) and isinstance(actual, str) and expected != actual:
        # Long texts (a tool's result as text) differ somewhere deep in:
        # show where.
        at = next((i for i, (e, a) in enumerate(zip(expected, actual)) if e != a), min(len(expected), len(actual)))
        start = max(0, at - 150)
        return (
            f"{path}: differs at character {at}:\n"
            f"  expected ...{json.dumps(expected[start:at + 150])}\n"
            f"  got      ...{json.dumps(actual[start:at + 150])}"
        )
    if expected != actual:
        return f"{path}: expected {json.dumps(expected)[:300]}, got {json.dumps(actual)[:300]}"
    return None


def check_golden(request, record: dict) -> None:
    module = Path(request.node.fspath).stem
    path = GOLDEN / module / f"{request.node.name}.json"
    rendered = json.dumps(record, indent=1, ensure_ascii=False, sort_keys=True) + "\n"
    if os.environ.get("CTT_UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8", newline="\n")
        return
    if not path.exists():
        pytest.fail(f"no golden record {path.relative_to(GOLDEN.parent)}; write it with CTT_UPDATE_GOLDEN=1")
    expected = json.loads(path.read_text(encoding="utf-8"))
    actual = json.loads(rendered)
    if os.environ.get("CTT_ACTUAL_DIR"):
        # The record as this run made it, to compare whole.
        actual_path = Path(os.environ["CTT_ACTUAL_DIR"]) / module / f"{request.node.name}.json"
        actual_path.parent.mkdir(parents=True, exist_ok=True)
        actual_path.write_text(rendered, encoding="utf-8", newline="\n")
    difference = _first_difference(expected, actual)
    if difference:
        pytest.fail(f"differs from {path.relative_to(GOLDEN.parent)}:\n{difference}")


@pytest.fixture
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def calendar_id(request, google: FakeGoogle) -> str:
    """The calendar the server runs against: `@pytest.mark.calendar(...)`
    passes FakeGoogle.add_calendar its arguments (time_zone=None for one
    with no zone, say)."""
    marker = request.node.get_closest_marker("calendar")
    return google.add_calendar(**(marker.kwargs if marker else {}))


@pytest.fixture
def now() -> str:
    """The server's fixed clock; override in a module to move it."""
    return DEFAULT_NOW


@pytest.fixture
def session(request, google, calendar_id, now):
    runner = ServerThread(google, calendar_id, now=now)
    server = runner.start()
    try:
        yield server
        record = server.record()
    finally:
        runner.stop()
    if not request.node.get_closest_marker("no_golden"):
        check_golden(request, record)


def pytest_configure(config):
    config.addinivalue_line("markers", "no_golden: don't compare this scenario with a golden record")
    config.addinivalue_line("markers", "calendar(**kwargs): how to make the scenario's calendar (see the calendar_id fixture)")
