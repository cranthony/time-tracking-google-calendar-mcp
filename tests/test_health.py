"""Health metrics: encoding, the recorder, the Health tab and its writer,
and the server timing tool calls -- their own work, and over HTTP, the
whole request's -- for get_health (see utilities/health.py)."""

from __future__ import annotations

import threading

import pytest
from starlette.testclient import TestClient

import server
from calendar_clients.google_sheets import SheetsClient
from calendar_clients.write_lock import WriteLockNotHeldError
from tests.fake_sheets import FakeSheets, FakeSheetsService
from utilities import health
from utilities.health import (
    KEEP,
    Call,
    HealthRecorder,
    HealthRow,
    HealthTab,
    HealthWriter,
    MemorySample,
)


class _Clock:
    def __init__(self, start_ms: int = 1_791_000_000_000) -> None:
        self.ms = start_ms

    def __call__(self) -> int:
        self.ms += 1234
        return self.ms


def _recorder(rss=lambda: 300_000) -> HealthRecorder:
    return HealthRecorder(now_ms=_Clock(), rss=rss)


class TestEncoding:
    def test_calls_round_trip(self):
        calls = [
            Call(at_ms=1_791_000_000_000, work_ms=312, total_ms=401, ok=True),
            Call(at_ms=1_791_000_004_567, work_ms=0, total_ms=None, ok=False),
            Call(at_ms=1_791_000_004_567, work_ms=25_000, total_ms=25_000, ok=True),
        ]

        assert health.decode_calls(health.encode_calls(calls)) == calls

    def test_memory_and_times_round_trip(self):
        memory = [MemorySample(at_ms=1_791_000_000_000 + i * 997, rss_kib=300_000 - i * 37) for i in range(5)]
        times = [1_791_000_000_000, 1_791_000_100_000]

        assert health.decode_memory(health.encode_memory(memory)) == memory
        assert health.decode_times(health.encode_times(times)) == times
        assert health.decode_times(health.encode_times([])) == []

    def test_a_hundred_calls_take_little_room(self):
        clock = _Clock()
        calls = [Call(at_ms=clock(), work_ms=200 + i * 7 % 300, total_ms=260 + i * 7 % 300, ok=i % 9 != 0) for i in range(KEEP)]

        data = health.encode_calls(calls)

        assert len(data) < 1000
        assert health.decode_calls(data) == calls

    def test_an_unknown_format_is_refused(self):
        with pytest.raises(ValueError):
            health.decode_calls("9abc")


class TestRecorder:
    def test_keeps_each_tools_last_hundred_and_the_memory_across_them(self):
        recorder = _recorder()

        for i in range(KEEP + 20):
            recorder.record_call("get_notes" if i % 2 else "note", work_ms=i, total_ms=None, ok=True)

        calls = recorder.calls()
        assert len(calls["get_notes"]) == 60
        assert len(calls["note"]) == 60
        # Across tools, not per tool: the last hundred calls, whichever.
        assert len(recorder.memory()) == KEEP
        assert recorder.memory()[-1].at_ms == calls["get_notes"][-1].at_ms
        assert [c.work_ms for c in calls["note"]][-1] == KEEP + 18

        recorder.record_call("note", work_ms=1, total_ms=None, ok=True)
        assert len(recorder.calls()["note"]) == 61

    def test_keeps_no_memory_where_it_can_t_be_read(self):
        recorder = _recorder(rss=lambda: None)

        recorder.record_call("note", work_ms=5, total_ms=None, ok=True)

        assert recorder.memory() == []
        assert [r.series for r in recorder.take_dirty()] == ["tool:note"]

    def test_gives_what_changed_once(self):
        recorder = _recorder()
        recorder.record_call("note", work_ms=5, total_ms=9, ok=True)
        recorder.record_restart()

        rows = recorder.take_dirty()

        assert [r.id for r in rows] == ["server:memory", "server:restarts", "server:tool:note"]
        assert recorder.take_dirty() is None
        recorder.mark_dirty(rows[:1])
        assert [r.series for r in recorder.take_dirty()] == ["memory"]

    def test_carries_on_from_what_the_tab_kept(self):
        before = _recorder()
        for i in range(KEEP):
            before.record_call("note", work_ms=i, total_ms=i + 3, ok=True)
        before.record_restart()
        kept = before.rows()
        after = HealthRecorder(now_ms=_Clock(start_ms=1_792_000_000_000), rss=lambda: 1)
        after.record_restart()
        after.record_call("note", work_ms=999, total_ms=None, ok=False)

        after.merge(kept)

        notes = after.calls()["note"]
        assert len(notes) == KEEP
        # The oldest go: the one recorded since is the latest.
        assert notes[0].work_ms == 1
        assert notes[-1] == Call(at_ms=notes[-1].at_ms, work_ms=999, total_ms=None, ok=False)
        assert len(after.restarts()) == 2
        assert after.memory()[-1].rss_kib == 1

    def test_skips_a_row_it_can_t_read(self):
        recorder = _recorder()

        recorder.merge(
            [
                HealthRow(id="server:tool:note", source="server", series="tool:note", data="1!!"),
                HealthRow(id="app:tool:x", source="app", series="tool:x", data="whatever"),
            ]
        )

        assert recorder.calls() == {}


def _sheets() -> tuple[FakeSheets, SheetsClient]:
    fake = FakeSheets()
    return fake, SheetsClient(FakeSheetsService(fake))


class TestHealthTab:
    def test_writes_without_the_write_lock_and_keeps_others_rows(self):
        fake, sheets = _sheets()
        spreadsheet_id = fake.create_spreadsheet("Calendar Metadata")
        tab = HealthTab.ensure(sheets, spreadsheet_id)
        app_row = HealthRow(id="app:tool:sync", source="app", series="tool:sync", count=1, data="x")
        tab.write([app_row])
        recorder = _recorder()
        recorder.record_call("note", work_ms=5, total_ms=9, ok=True)

        tab.write(recorder.take_dirty())

        rows = {r.id: r for r in tab.read()}
        assert set(rows) == {"app:tool:sync", "server:memory", "server:tool:note"}
        assert rows["app:tool:sync"].data == "x"
        assert health.decode_calls(rows["server:tool:note"].data)[0].total_ms == 9

    @pytest.mark.without_write_lock
    def test_nothing_else_writes_without_a_lock(self):
        fake, sheets = _sheets()
        spreadsheet_id = fake.create_spreadsheet("Calendar Metadata")

        with pytest.raises(WriteLockNotHeldError):
            sheets.add_sheet(spreadsheet_id, "Anything")


class _Tab:
    """A Health tab in memory, failing to write while told to."""

    def __init__(self, rows: list[HealthRow] | None = None) -> None:
        self.rows = {r.id: r for r in rows or []}
        self.writes: list[list[str]] = []
        self.fail = False

    def read(self) -> list[HealthRow]:
        return list(self.rows.values())

    def write(self, changed: list[HealthRow]) -> None:
        if self.fail:
            raise OSError("offline")
        self.writes.append([r.id for r in changed])
        self.rows.update({r.id: r for r in changed})


class TestHealthWriter:
    def test_takes_in_what_was_kept_records_the_start_and_writes_what_changes(self):
        earlier = HealthRecorder(now_ms=_Clock(start_ms=1_790_000_000_000), rss=lambda: 1)
        earlier.record_restart()
        tab = _Tab(earlier.rows())
        recorder = _recorder()
        writer = HealthWriter(recorder, lambda: tab, interval=3600)

        writer.start()
        assert writer.loaded.wait(5)
        writer.flush()

        assert len(recorder.restarts()) == 2
        assert "server:restarts" in tab.writes[0]
        recorder.record_call("note", work_ms=5, total_ms=None, ok=True)
        writer.flush()
        assert tab.writes[-1] == ["server:memory", "server:tool:note"]
        writer.flush()
        assert len(tab.writes) == 2  # Nothing changed.
        writer.stop()

    def test_writes_again_what_it_couldn_t(self):
        tab = _Tab()
        recorder = _recorder()
        writer = HealthWriter(recorder, lambda: tab, interval=3600)
        writer.start()
        assert writer.loaded.wait(5)
        tab.fail = True
        recorder.record_call("note", work_ms=5, total_ms=None, ok=True)

        writer.flush()
        tab.fail = False
        writer.stop()

        assert "server:tool:note" in tab.rows

    def test_keeps_trying_to_open_the_tab(self):
        tab = _Tab()
        opened = threading.Event()
        tries = []

        def open_tab():
            tries.append(1)
            if len(tries) == 1:
                raise OSError("offline")
            opened.set()
            return tab

        recorder = _recorder()
        writer = HealthWriter(recorder, open_tab, interval=0.01)
        writer.start()

        assert opened.wait(5)
        writer.stop()
        assert "server:restarts" in tab.rows


@pytest.fixture
def recorder(monkeypatch):
    recorder = _recorder()
    monkeypatch.setattr(server, "HEALTH", recorder)
    monkeypatch.setattr(server, "_health_writer", None)
    return recorder


class TestServerTiming:
    def test_a_call_not_over_http_is_recorded_with_its_own_time(self, recorder):
        server.get_health()

        (call,) = recorder.calls()["get_health"]
        assert call.ok and call.total_ms is None

    def test_a_call_that_raises_is_an_error(self, recorder):
        with pytest.raises(Exception):
            server.get_health(tool_name="nothing")

        (call,) = recorder.calls()["get_health"]
        assert not call.ok

    def test_a_call_over_http_is_recorded_with_the_whole_request_s_time(self, recorder):
        # Through the SDK's own transport: the tool sees its request's
        # timing in the context the transport handed the message on with.
        app = server.with_health(server.with_cors(server.mcp.streamable_http_app()))
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        with TestClient(app, base_url="http://localhost:8000") as client:
            init = client.post(
                "/mcp",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1"},
                    },
                },
            )
            assert init.status_code == 200, init.text
            session = {**headers}
            if "mcp-session-id" in init.headers:
                session["Mcp-Session-Id"] = init.headers["mcp-session-id"]
            client.post("/mcp", headers=session, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
            called = client.post(
                "/mcp",
                headers=session,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_health", "arguments": {}}},
            )
            assert called.status_code == 200, called.text

        (call,) = recorder.calls()["get_health"]
        assert call.ok
        assert call.total_ms is not None and call.total_ms >= call.work_ms

    def test_get_health_reports_each_tool_s_calls_the_memory_and_restarts(self, recorder):
        recorder.record_restart()
        for i in range(10):
            recorder.record_call("note", work_ms=100 + i, total_ms=150 + i, ok=i != 3)

        report = server.get_health(tool_name="note")

        (note,) = report.tools
        assert note.count == 10 and note.errors == 1
        assert note.work.median_ms == 104 and note.work.max_ms == 109
        assert note.total.p95_ms == 159
        assert note.calls[0].overhead_ms == 50
        assert note.calls[0].at.endswith("Z")
        assert len(report.memory) == 10 and report.memory_latest_mib == round(300_000 / 1024, 1)
        assert len(report.restarts) == 1
        assert server.get_health(samples=False).tools[0].calls is None
