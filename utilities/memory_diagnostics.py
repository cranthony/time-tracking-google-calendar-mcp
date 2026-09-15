"""Point-in-time memory diagnostics for narrowing down what's driving this
process's memory usage, logged around a named operation (an MCP tool call,
`load_credentials`, ...) so a spike can be attributed to whichever
operation was running when it happened.

Three signals, each logged if available:
- RSS (this process's actual resident memory, from `/proc/self/status` --
  Linux only, e.g. the Render container this app is deployed to; silently
  omitted elsewhere, such as local Windows dev).
- `tracemalloc` growth since the end of the previously tracked operation
  -- only if `tracemalloc` is already tracing. Starting/stopping it is
  someone else's job (e.g. a background sampler that turns it on once
  memory crosses some threshold); this module only reports on it if it's
  already running, so using `track` costs nothing extra until that
  happens.
- `objgraph` object-count growth since the previously tracked operation --
  cheap (a `gc.collect()` plus a count-by-type pass), so always run.

Because both `tracemalloc` and `objgraph` report growth *since the last
call*, logged this way they approximate "what this operation allocated" --
exactly only if tracked operations don't run concurrently with each other
or with untracked allocation activity.
"""

from __future__ import annotations

import contextlib
import logging
import tracemalloc
from collections.abc import Iterator

try:
    import objgraph
except ImportError:  # pragma: no cover - objgraph is a normal dependency; only optional as a safety net.
    objgraph = None

logger = logging.getLogger(__name__)

_TOP_N = 5
"""How many tracemalloc/objgraph entries to log per tracked operation."""

_previous_tracemalloc_snapshot: tracemalloc.Snapshot | None = None
"""Set the first time `tracemalloc` is seen tracing; compared against on
every later call so each log line shows growth since the previous tracked
operation, not since the process started. Reset to None whenever
`tracemalloc` isn't tracing, so it starts fresh next time it is."""

_objgraph_peak_stats: dict[str, int] = {}
"""Passed to `objgraph.growth` instead of relying on its own mutable
default argument, so this module's notion of "peak seen so far" doesn't
depend on `objgraph`'s internals."""


def _current_rss_bytes() -> int | None:
    """This process's current RSS, or `None` if `/proc/self/status` isn't
    available (anything but Linux) or couldn't be parsed."""
    try:
        with open("/proc/self/status", encoding="ascii") as status_file:
            for line in status_file:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _log_rss(label: str) -> None:
    rss = _current_rss_bytes()
    if rss is not None:
        logger.info("[%s] RSS: %.1f MB", label, rss / (1024 * 1024))


def _log_tracemalloc_growth(label: str) -> None:
    global _previous_tracemalloc_snapshot
    if not tracemalloc.is_tracing():
        _previous_tracemalloc_snapshot = None
        return
    snapshot = tracemalloc.take_snapshot()
    if _previous_tracemalloc_snapshot is not None:
        top_stats = snapshot.compare_to(_previous_tracemalloc_snapshot, "lineno")[:_TOP_N]
        for stat in top_stats:
            logger.info("[%s] tracemalloc growth: %s", label, stat)
    _previous_tracemalloc_snapshot = snapshot


def _log_objgraph_growth(label: str) -> None:
    if objgraph is None:
        return
    for type_name, count, delta in objgraph.growth(
        limit=_TOP_N, peak_stats=_objgraph_peak_stats
    ):
        logger.info("[%s] objgraph growth: %s: %d (+%d)", label, type_name, count, delta)


def log_memory(label: str) -> None:
    """Log RSS, `tracemalloc` growth (if tracing), and `objgraph` growth
    for `label` -- an operation that just finished. Prefer `track` below
    to cover an operation that might raise."""
    _log_rss(label)
    _log_tracemalloc_growth(label)
    _log_objgraph_growth(label)


@contextlib.contextmanager
def track(label: str) -> Iterator[None]:
    """Wrap an operation (an MCP tool call, `load_credentials`, ...) to
    `log_memory(label)` when it finishes -- including when it raises, so a
    call that fails partway through (e.g. mid-OAuth-flow) is still
    attributed instead of silently skipped."""
    try:
        yield
    finally:
        log_memory(label)
