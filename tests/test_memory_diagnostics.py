import io
import tracemalloc

import pytest

from utilities import memory_diagnostics
from utilities.memory_diagnostics import log_memory, track

_LOGGER_NAME = "utilities.memory_diagnostics"


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """_previous_tracemalloc_snapshot/_objgraph_peak_stats persist across
    calls by design (each logs growth since the previous one), which would
    otherwise leak state between tests."""
    monkeypatch.setattr(memory_diagnostics, "_previous_tracemalloc_snapshot", None)
    monkeypatch.setattr(memory_diagnostics, "_objgraph_peak_stats", {})


class TestCurrentRssBytes:
    def test_parses_the_vmrss_line(self, monkeypatch):
        fake_status = "VmPeak:\t  123 kB\nVmRSS:\t 45678 kB\nVmData:\t   1 kB\n"
        monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(fake_status))

        assert memory_diagnostics._current_rss_bytes() == 45678 * 1024

    def test_returns_none_when_the_file_is_unavailable(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise FileNotFoundError

        monkeypatch.setattr("builtins.open", _raise)

        assert memory_diagnostics._current_rss_bytes() is None

    def test_returns_none_when_vmrss_is_missing(self, monkeypatch):
        monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO("VmPeak:\t123 kB\n"))

        assert memory_diagnostics._current_rss_bytes() is None


class TestLogRss:
    def test_logs_when_available(self, monkeypatch, caplog):
        monkeypatch.setattr(memory_diagnostics, "_current_rss_bytes", lambda: 100 * 1024 * 1024)

        with caplog.at_level("INFO", logger=_LOGGER_NAME):
            memory_diagnostics._log_rss("my-op")

        assert any(
            "my-op" in r.message and "100.0 MB" in r.message for r in caplog.records
        )

    def test_logs_nothing_when_unavailable(self, monkeypatch, caplog):
        monkeypatch.setattr(memory_diagnostics, "_current_rss_bytes", lambda: None)

        with caplog.at_level("INFO", logger=_LOGGER_NAME):
            memory_diagnostics._log_rss("my-op")

        assert caplog.records == []


class TestLogTracemallocGrowth:
    def test_does_nothing_when_not_tracing(self, caplog):
        assert not tracemalloc.is_tracing()

        with caplog.at_level("INFO", logger=_LOGGER_NAME):
            memory_diagnostics._log_tracemalloc_growth("my-op")

        assert caplog.records == []

    def test_first_call_takes_a_baseline_without_logging(self, caplog):
        tracemalloc.start()
        try:
            with caplog.at_level("INFO", logger=_LOGGER_NAME):
                memory_diagnostics._log_tracemalloc_growth("my-op")

            assert caplog.records == []
            assert memory_diagnostics._previous_tracemalloc_snapshot is not None
        finally:
            tracemalloc.stop()

    def test_logs_growth_since_the_previous_call(self, caplog):
        tracemalloc.start()
        try:
            memory_diagnostics._log_tracemalloc_growth("baseline")
            leak = ["x" * 1000 for _ in range(20000)]

            with caplog.at_level("INFO", logger=_LOGGER_NAME):
                memory_diagnostics._log_tracemalloc_growth("my-op")

            assert any("tracemalloc growth" in r.message for r in caplog.records)
            del leak
        finally:
            tracemalloc.stop()

    def test_resets_the_baseline_once_tracing_stops(self):
        tracemalloc.start()
        memory_diagnostics._log_tracemalloc_growth("my-op")
        tracemalloc.stop()

        memory_diagnostics._log_tracemalloc_growth("my-op")

        assert memory_diagnostics._previous_tracemalloc_snapshot is None


class TestLogObjgraphGrowth:
    def test_logs_growth_of_a_type_that_increased(self, caplog):
        class _MemoryDiagnosticsTestThing:
            pass

        memory_diagnostics._log_objgraph_growth("baseline")
        things = [_MemoryDiagnosticsTestThing() for _ in range(2000)]

        with caplog.at_level("INFO", logger=_LOGGER_NAME):
            memory_diagnostics._log_objgraph_growth("my-op")

        assert any(
            "objgraph growth" in r.message and "_MemoryDiagnosticsTestThing" in r.message
            for r in caplog.records
        )
        del things


class TestLogMemory:
    def test_calls_all_three_helpers_in_order(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            memory_diagnostics, "_log_rss", lambda label: calls.append(("rss", label))
        )
        monkeypatch.setattr(
            memory_diagnostics,
            "_log_tracemalloc_growth",
            lambda label: calls.append(("tracemalloc", label)),
        )
        monkeypatch.setattr(
            memory_diagnostics,
            "_log_objgraph_growth",
            lambda label: calls.append(("objgraph", label)),
        )

        log_memory("my-op")

        assert calls == [("rss", "my-op"), ("tracemalloc", "my-op"), ("objgraph", "my-op")]


class TestTrack:
    def test_logs_at_start_and_end(self, monkeypatch):
        logged = []
        monkeypatch.setattr(memory_diagnostics, "log_memory", logged.append)

        with track("my-op"):
            assert logged == ["my-op start"]

        assert logged == ["my-op start", "my-op end"]

    def test_logs_the_end_even_when_the_block_raises(self, monkeypatch):
        logged = []
        monkeypatch.setattr(memory_diagnostics, "log_memory", logged.append)

        with pytest.raises(ValueError):
            with track("my-op"):
                raise ValueError("boom")

        assert logged == ["my-op start", "my-op end"]
