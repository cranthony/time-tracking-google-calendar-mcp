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
    def test_logs_the_given_value(self, caplog):
        with caplog.at_level("INFO", logger=_LOGGER_NAME):
            memory_diagnostics._log_rss("my-op", 100 * 1024 * 1024)

        assert any(
            "my-op" in r.message and "100.0 MB" in r.message for r in caplog.records
        )


def _fake_open(responses: dict[str, str]):
    """A fake `open` returning `io.StringIO(responses[path])` for a
    request matching a key in `responses`, raising `FileNotFoundError`
    (like the real thing) for anything else."""

    def _open(path, *args, **kwargs):
        if path in responses:
            return io.StringIO(responses[path])
        raise FileNotFoundError(path)

    return _open


_CGROUP_V2_PATH = "/sys/fs/cgroup/memory.max"
_CGROUP_V1_PATH = "/sys/fs/cgroup/memory/memory.limit_in_bytes"


class TestMemoryLimitBytes:
    def test_prefers_the_environment_override(self, monkeypatch):
        monkeypatch.setenv("MEMORY_LIMIT_BYTES", "123456")
        monkeypatch.setattr("builtins.open", _fake_open({_CGROUP_V2_PATH: "999\n"}))

        assert memory_diagnostics._memory_limit_bytes() == 123456

    def test_falls_back_to_cgroup_when_override_is_not_a_valid_integer(self, monkeypatch, caplog):
        monkeypatch.setenv("MEMORY_LIMIT_BYTES", "not-a-number")
        monkeypatch.setattr("builtins.open", _fake_open({_CGROUP_V2_PATH: "536870912\n"}))

        with caplog.at_level("WARNING", logger=_LOGGER_NAME):
            result = memory_diagnostics._memory_limit_bytes()

        assert result == 536870912
        assert any("MEMORY_LIMIT_BYTES" in r.message for r in caplog.records)

    def test_reads_cgroup_v2_memory_max(self, monkeypatch):
        monkeypatch.delenv("MEMORY_LIMIT_BYTES", raising=False)
        monkeypatch.setattr("builtins.open", _fake_open({_CGROUP_V2_PATH: "536870912\n"}))

        assert memory_diagnostics._memory_limit_bytes() == 536870912

    def test_falls_back_to_cgroup_v1_when_v2_says_max(self, monkeypatch):
        monkeypatch.delenv("MEMORY_LIMIT_BYTES", raising=False)
        monkeypatch.setattr(
            "builtins.open",
            _fake_open({_CGROUP_V2_PATH: "max\n", _CGROUP_V1_PATH: "536870912\n"}),
        )

        assert memory_diagnostics._memory_limit_bytes() == 536870912

    def test_falls_back_to_cgroup_v1_when_v2_is_unavailable(self, monkeypatch):
        monkeypatch.delenv("MEMORY_LIMIT_BYTES", raising=False)
        monkeypatch.setattr("builtins.open", _fake_open({_CGROUP_V1_PATH: "536870912\n"}))

        assert memory_diagnostics._memory_limit_bytes() == 536870912

    def test_treats_cgroup_v1_sentinel_as_no_limit(self, monkeypatch):
        monkeypatch.delenv("MEMORY_LIMIT_BYTES", raising=False)
        monkeypatch.setattr(
            "builtins.open", _fake_open({_CGROUP_V1_PATH: "9223372036854771712\n"})
        )

        assert memory_diagnostics._memory_limit_bytes() is None

    def test_returns_none_when_nothing_is_available(self, monkeypatch):
        monkeypatch.delenv("MEMORY_LIMIT_BYTES", raising=False)
        monkeypatch.setattr("builtins.open", _fake_open({}))

        assert memory_diagnostics._memory_limit_bytes() is None


class TestSyncTracemallocTracing:
    def test_does_nothing_when_the_limit_is_unknown(self, monkeypatch):
        assert not tracemalloc.is_tracing()
        monkeypatch.setattr(memory_diagnostics, "_memory_limit_bytes", lambda: None)

        memory_diagnostics._sync_tracemalloc_tracing(10**12)

        assert not tracemalloc.is_tracing()

    def test_does_nothing_below_the_threshold_while_not_tracing(self, monkeypatch):
        assert not tracemalloc.is_tracing()
        monkeypatch.setattr(memory_diagnostics, "_memory_limit_bytes", lambda: 100)

        memory_diagnostics._sync_tracemalloc_tracing(49)  # 49% of the limit

        assert not tracemalloc.is_tracing()

    def test_starts_tracemalloc_at_the_threshold(self, monkeypatch):
        assert not tracemalloc.is_tracing()
        monkeypatch.setattr(memory_diagnostics, "_memory_limit_bytes", lambda: 100)

        try:
            memory_diagnostics._sync_tracemalloc_tracing(50)  # exactly 50% of the limit

            assert tracemalloc.is_tracing()
        finally:
            tracemalloc.stop()

    def test_does_not_restart_if_already_tracing_above_the_threshold(self, monkeypatch, caplog):
        tracemalloc.start()
        try:
            monkeypatch.setattr(memory_diagnostics, "_memory_limit_bytes", lambda: 100)

            with caplog.at_level("WARNING", logger=_LOGGER_NAME):
                memory_diagnostics._sync_tracemalloc_tracing(90)  # 90% of the limit

            assert tracemalloc.is_tracing()
            assert caplog.records == []
        finally:
            tracemalloc.stop()

    def test_logs_a_warning_when_it_starts_tracing(self, monkeypatch, caplog):
        monkeypatch.setattr(memory_diagnostics, "_memory_limit_bytes", lambda: 100)

        try:
            with caplog.at_level("WARNING", logger=_LOGGER_NAME):
                memory_diagnostics._sync_tracemalloc_tracing(50)

            assert any("starting tracemalloc" in r.message for r in caplog.records)
        finally:
            tracemalloc.stop()

    def test_stops_tracemalloc_once_back_below_the_threshold(self, monkeypatch):
        tracemalloc.start()
        monkeypatch.setattr(memory_diagnostics, "_memory_limit_bytes", lambda: 100)

        memory_diagnostics._sync_tracemalloc_tracing(49)  # 49% of the limit -- no hysteresis

        assert not tracemalloc.is_tracing()

    def test_does_not_stop_while_still_tracing_at_the_threshold(self, monkeypatch):
        tracemalloc.start()
        try:
            monkeypatch.setattr(memory_diagnostics, "_memory_limit_bytes", lambda: 100)

            memory_diagnostics._sync_tracemalloc_tracing(50)  # exactly 50% of the limit

            assert tracemalloc.is_tracing()
        finally:
            tracemalloc.stop()

    def test_does_nothing_below_the_threshold_while_not_tracing_again(self, monkeypatch):
        # Not tracing and below the threshold: nothing to start, nothing
        # to stop.
        assert not tracemalloc.is_tracing()
        monkeypatch.setattr(memory_diagnostics, "_memory_limit_bytes", lambda: 100)

        memory_diagnostics._sync_tracemalloc_tracing(10)

        assert not tracemalloc.is_tracing()

    def test_logs_a_warning_when_it_stops_tracing(self, monkeypatch, caplog):
        tracemalloc.start()
        monkeypatch.setattr(memory_diagnostics, "_memory_limit_bytes", lambda: 100)

        with caplog.at_level("WARNING", logger=_LOGGER_NAME):
            memory_diagnostics._sync_tracemalloc_tracing(49)

        assert any("stopping tracemalloc" in r.message for r in caplog.records)


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
    def test_calls_the_helpers_in_order_when_rss_is_available(self, monkeypatch):
        calls = []
        monkeypatch.setattr(memory_diagnostics, "_current_rss_bytes", lambda: 100)
        monkeypatch.setattr(
            memory_diagnostics,
            "_log_rss",
            lambda label, rss: calls.append(("rss", label, rss)),
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
        monkeypatch.setattr(
            memory_diagnostics,
            "_sync_tracemalloc_tracing",
            lambda rss: calls.append(("sync", rss)),
        )

        log_memory("my-op")

        # tracemalloc growth (and objgraph growth) are logged before the
        # sync that might stop tracemalloc, so a stop never discards
        # growth this operation hasn't had a chance to log yet.
        assert calls == [
            ("rss", "my-op", 100),
            ("tracemalloc", "my-op"),
            ("objgraph", "my-op"),
            ("sync", 100),
        ]

    def test_skips_rss_and_sync_when_rss_is_unavailable(self, monkeypatch):
        calls = []
        monkeypatch.setattr(memory_diagnostics, "_current_rss_bytes", lambda: None)
        monkeypatch.setattr(memory_diagnostics, "_log_rss", lambda label, rss: calls.append("rss"))
        monkeypatch.setattr(
            memory_diagnostics,
            "_log_tracemalloc_growth",
            lambda label: calls.append("tracemalloc"),
        )
        monkeypatch.setattr(
            memory_diagnostics, "_log_objgraph_growth", lambda label: calls.append("objgraph")
        )
        monkeypatch.setattr(
            memory_diagnostics, "_sync_tracemalloc_tracing", lambda rss: calls.append("sync")
        )

        log_memory("my-op")

        assert calls == ["tracemalloc", "objgraph"]

    def test_logs_final_tracemalloc_growth_before_stopping(self, monkeypatch, caplog):
        # End-to-end: crossing the threshold starts tracemalloc; dropping
        # back below it stops tracemalloc -- but not before this same
        # call has already logged whatever grew up to that point.
        monkeypatch.setattr(memory_diagnostics, "_memory_limit_bytes", lambda: 100)
        rss_values = iter([60, 60, 10])  # start, establish baseline, drop back below
        monkeypatch.setattr(memory_diagnostics, "_current_rss_bytes", lambda: next(rss_values))
        leak = None
        try:
            log_memory("op1")  # 60% -- starts tracemalloc (nothing to diff against yet)
            assert tracemalloc.is_tracing()

            log_memory("op2")  # still 60% -- takes tracemalloc's first real snapshot

            leak = ["x" * 1000 for _ in range(20000)]  # something for op3 to see as growth

            with caplog.at_level("INFO", logger=_LOGGER_NAME):
                log_memory("op3")  # 10% -- logs growth from `leak`, then stops

            assert not tracemalloc.is_tracing()
            assert any(
                "op3" in r.message and "tracemalloc growth" in r.message
                for r in caplog.records
            )
        finally:
            del leak
            if tracemalloc.is_tracing():
                tracemalloc.stop()


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
