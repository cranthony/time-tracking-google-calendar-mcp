"""Point-in-time memory diagnostics for narrowing down what's driving this
process's memory usage, logged at the start and end of a named operation
(an MCP tool call, `load_credentials`, ...) so a spike can be attributed
to whichever operation was running when it happened.

Three signals, each logged if available:
- RSS (this process's actual resident memory, from `/proc/self/status` --
  Linux only, e.g. the Render container this app is deployed to; silently
  omitted elsewhere, such as local Windows dev).
- `tracemalloc` growth since the end of the previously tracked operation
  -- only if `tracemalloc` is tracing. Nothing here ever stops it once
  started, and it's never started at all until RSS is first seen past
  `_TRACING_THRESHOLD` of this process's own memory limit (see
  `_memory_limit_bytes`) -- so its overhead (continuous, non-trivial) is
  only paid once things are actually heading toward an OOM kill.
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
import os
import tracemalloc
from collections.abc import Iterator

try:
    import objgraph
except ImportError:  # pragma: no cover - objgraph is a normal dependency; only optional as a safety net.
    objgraph = None

logger = logging.getLogger(__name__)

_TOP_N = 5
"""How many tracemalloc/objgraph entries to log per tracked operation."""

_TRACING_THRESHOLD = 0.5
"""Fraction of this process's own memory limit RSS has to cross before
`tracemalloc` gets started (see `_maybe_start_tracemalloc`)."""

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


def _memory_limit_bytes() -> int | None:
    """This process's own memory limit, in bytes, or `None` if it can't be
    determined. Tried in order:

    - `MEMORY_LIMIT_BYTES`, an explicit override -- for a host that
      doesn't expose one of the below (or to test this against a smaller
      limit than the real one).
    - cgroup v2's `memory.max` (the modern default on most container
      platforms, Render included, as best as can be confirmed -- Render
      doesn't document a dedicated memory-limit environment variable, but
      the limit it enforces has to live in the cgroup doing the
      enforcing, regardless of platform specifics). Its content is the
      literal string "max" if there's no limit.
    - cgroup v1's `memory.limit_in_bytes`, for hosts still on the legacy
      hierarchy. "No limit" here isn't a sentinel string but an
      absurdly large number (best practice: reject anything at or above
      2**62, far beyond any real container's limit) -- `LLONG_MAX` rounded
      down to the page size, specifically.
    """
    env_override = os.environ.get("MEMORY_LIMIT_BYTES")
    if env_override:
        try:
            return int(env_override)
        except ValueError:
            logger.warning("MEMORY_LIMIT_BYTES=%r is not a valid integer; ignoring", env_override)

    try:
        with open("/sys/fs/cgroup/memory.max", encoding="ascii") as limit_file:
            value = limit_file.read().strip()
        if value != "max":
            return int(value)
    except (OSError, ValueError):
        pass

    try:
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes", encoding="ascii") as limit_file:
            value = int(limit_file.read().strip())
        if value < 2**62:
            return value
    except (OSError, ValueError):
        pass

    return None


def _maybe_start_tracemalloc(rss_bytes: int) -> None:
    """Start `tracemalloc` if it isn't already, once `rss_bytes` crosses
    `_TRACING_THRESHOLD` of this process's own memory limit -- see the
    module docstring."""
    if tracemalloc.is_tracing():
        return
    limit = _memory_limit_bytes()
    if limit is None or rss_bytes < limit * _TRACING_THRESHOLD:
        return
    logger.warning(
        "RSS %.1f MB crossed %.0f%% of the %.1f MB memory limit -- starting tracemalloc",
        rss_bytes / (1024 * 1024),
        _TRACING_THRESHOLD * 100,
        limit / (1024 * 1024),
    )
    tracemalloc.start()


def _log_rss(label: str) -> None:
    rss = _current_rss_bytes()
    if rss is not None:
        logger.info("[%s] RSS: %.1f MB", label, rss / (1024 * 1024))
        _maybe_start_tracemalloc(rss)


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
    """Log RSS, `tracemalloc` growth (if tracing), and `objgraph` growth,
    each line tagged with `label`. Prefer `track` below, which calls this
    at both the start and end of an operation -- including when it
    raises."""
    _log_rss(label)
    _log_tracemalloc_growth(label)
    _log_objgraph_growth(label)


@contextlib.contextmanager
def track(label: str) -> Iterator[None]:
    """Wrap an operation (an MCP tool call, `load_credentials`, ...) to
    `log_memory` both when it starts and when it finishes -- the latter
    including when it raises, so a call that fails partway through (e.g.
    mid-OAuth-flow) is still attributed instead of silently skipped.

    The "start" log is a sanity check as much as anything: since growth is
    reported since the *previous* tracked operation, the diff it shows
    should, in theory, be near zero -- nothing tracked should have run in
    between. If it isn't, something is allocating outside of any tracked
    operation (a background task, GC timing, ...), which is itself worth
    knowing.
    """
    log_memory(f"{label} start")
    try:
        yield
    finally:
        log_memory(f"{label} end")
