"""Health metrics: how long the server's tool calls take, how much memory
it uses, and when it restarts -- the last `KEEP` of each, kept in the
**Health** tab of the calendar's metadata spreadsheet (see
utilities/calendar_metadata_sheet.py), for `get_health` to give back to
graph, or to look for patterns in.

**What's kept.** One series per row:

- `tool:<name>` -- each of the last `KEEP` calls of that tool: when it
  ended, how long the tool's own work took (`work_ms`), how long the
  whole request took, auth and transport included (`total_ms`, when it
  came over HTTP -- see `with_health` in server.py), and whether it
  succeeded (`ok`: it returned, rather than raising -- a refusal the tool
  returns as its result is still `ok`).
- `memory` -- the process's resident memory (RSS, from `/proc`, so only
  on Linux, where the server runs) at the end of each of the last `KEEP`
  tool calls, whichever tool it was.
- `restarts` -- when each of the last `KEEP` server processes started:
  there's one server, so each start after the first is a restart, a
  crash or a deploy.

Each row is `id` (`<source>:<series>`), `source` (`server` now; the app's
own, later), `series`, `count`, `latest` (when its last sample was, for
reading the tab by eye) and `data`: the samples, compactly encoded (see
`encode`) -- around a kilobyte for a hundred calls.

**Without slowing tool calls.** A call's sample is appended to memory
(`HealthRecorder.record_call`, a few microseconds under a lock); a
background thread (`HealthWriter`) writes what changed every
`FLUSH_SECONDS`, and once more as the process exits. On starting, it
reads what the tab has, so the series carry on across restarts, and adds
the start to `restarts`. Samples from the last few seconds before a
crash are lost.

The tab is written by the writer, holding `HEALTH_TAB_LOCK` rather than
`WRITE_LOCK` (see calendar_clients/write_lock.py): nothing else writes
it but under the same lock, so a write never waits for a tool's.
"""

from __future__ import annotations

import atexit
import base64
import logging
import statistics
import threading
import time
import zlib
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from calendar_clients.google_sheets import SheetsClient
from calendar_clients.write_lock import HEALTH_TAB_LOCK
from utilities import calendar_metadata_sheet
from utilities.row_sheet import RowSheet

logger = logging.getLogger(__name__)

KEEP = 100
"""How many samples each series keeps: the latest."""

FLUSH_SECONDS = 30.0
"""How often the writer writes what changed."""

SERVER = "server"
"""The source of the server's own series."""

MEMORY = "memory"
RESTARTS = "restarts"
TOOL_PREFIX = "tool:"

_VERSION = "1"


@dataclass(frozen=True)
class Call:
    """One tool call: when it ended (ms since the epoch), how long its own
    work took, how long the whole request took (None when it didn't come
    over HTTP), and whether it succeeded."""

    at_ms: int
    work_ms: int
    total_ms: int | None
    ok: bool


@dataclass(frozen=True)
class MemorySample:
    """The process's resident memory, in KiB, at the end of a tool call."""

    at_ms: int
    rss_kib: int


@dataclass(kw_only=True)
class HealthRow:
    """A row of the Health tab: one series, its samples in `data`."""

    id: str
    source: str
    series: str
    count: int | None = None
    latest: str | None = None
    data: str | None = None


# -- encoding ------------------------------------------------------------------


def _zigzag(n: int) -> int:
    return (n << 1) ^ (n >> 63)


def _unzigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)


def _put(out: bytearray, n: int) -> None:
    """`n`, a non-negative int, as a varint: 7 bits a byte, low first."""
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return


def _take(data: bytes, at: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        byte = data[at]
        at += 1
        n |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return n, at


def _pack(columns: list[list[int]]) -> str:
    """Equal-length int columns, each delta-encoded (so a slowly changing
    one is mostly small numbers), zigzagged, as varints, column by
    column, deflated, as URL-safe base64 -- prefixed with the format's
    version."""
    out = bytearray()
    _put(out, len(columns[0]) if columns else 0)
    for column in columns:
        previous = 0
        for value in column:
            _put(out, _zigzag(value - previous))
            previous = value
    return _VERSION + base64.urlsafe_b64encode(zlib.compress(bytes(out), 9)).decode().rstrip("=")


def _unpack(text: str, width: int) -> list[list[int]]:
    if not text.startswith(_VERSION):
        raise ValueError(f"unknown health data format {text[:1]!r}")
    body = text[len(_VERSION) :]
    data = zlib.decompress(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    count, at = _take(data, 0)
    columns = []
    for _ in range(width):
        column, previous = [], 0
        for _ in range(count):
            delta, at = _take(data, at)
            previous += _unzigzag(delta)
            column.append(previous)
        columns.append(column)
    return columns


def encode_calls(calls: Iterable[Call]) -> str:
    calls = list(calls)
    return _pack(
        [
            [c.at_ms for c in calls],
            [c.work_ms for c in calls],
            # Overhead past the tool's work, plus one; zero for none.
            [0 if c.total_ms is None else max(0, c.total_ms - c.work_ms) + 1 for c in calls],
            [int(c.ok) for c in calls],
        ]
    )


def decode_calls(text: str) -> list[Call]:
    at, work, overhead, ok = _unpack(text, 4)
    return [
        Call(at_ms=a, work_ms=w, total_ms=None if o == 0 else w + o - 1, ok=bool(k))
        for a, w, o, k in zip(at, work, overhead, ok)
    ]


def encode_memory(samples: Iterable[MemorySample]) -> str:
    samples = list(samples)
    return _pack([[s.at_ms for s in samples], [s.rss_kib for s in samples]])


def decode_memory(text: str) -> list[MemorySample]:
    at, rss = _unpack(text, 2)
    return [MemorySample(at_ms=a, rss_kib=r) for a, r in zip(at, rss)]


def encode_times(times: Iterable[int]) -> str:
    return _pack([list(times)])


def decode_times(text: str) -> list[int]:
    return _unpack(text, 1)[0]


# -- recording -------------------------------------------------------------------


def rss_kib() -> int | None:
    """This process's resident memory, in KiB, from `/proc/self/status`
    (Linux only); None elsewhere."""
    try:
        with open("/proc/self/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass
class _Series:
    calls: dict[str, deque[Call]] = field(default_factory=dict)
    memory: deque[MemorySample] = field(default_factory=lambda: deque(maxlen=KEEP))
    restarts: deque[int] = field(default_factory=lambda: deque(maxlen=KEEP))


class HealthRecorder:
    """The server's series, in memory: what tool calls add to, what the
    writer writes, and what `get_health` reads. Thread-safe."""

    def __init__(self, *, now_ms: Callable[[], int] = _now_ms, rss: Callable[[], int | None] = rss_kib) -> None:
        self._lock = threading.Lock()
        self._series = _Series()
        self._dirty: set[str] = set()
        self._now_ms = now_ms
        self._rss = rss

    def record_call(self, tool: str, *, work_ms: int, total_ms: int | None, ok: bool) -> None:
        """A call of `tool` that just ended, and the memory in use now."""
        at = self._now_ms()
        rss = self._rss()
        with self._lock:
            calls = self._series.calls.setdefault(tool, deque(maxlen=KEEP))
            calls.append(Call(at_ms=at, work_ms=work_ms, total_ms=total_ms, ok=ok))
            self._dirty.add(TOOL_PREFIX + tool)
            if rss is not None:
                self._series.memory.append(MemorySample(at_ms=at, rss_kib=rss))
                self._dirty.add(MEMORY)

    def record_restart(self) -> None:
        """This process started, now."""
        with self._lock:
            self._series.restarts.append(self._now_ms())
            self._dirty.add(RESTARTS)

    def merge(self, rows: Iterable[HealthRow]) -> None:
        """Takes in the server's series as the tab kept them, from before
        this process started: each sample older than those recorded since
        goes first, and the latest `KEEP` are kept. A row that can't be
        read (a hand edit) is skipped."""
        with self._lock:
            for row in rows:
                if row.source != SERVER or not row.data:
                    continue
                try:
                    if row.series.startswith(TOOL_PREFIX):
                        tool = row.series[len(TOOL_PREFIX) :]
                        mine = self._series.calls.get(tool, deque())
                        self._series.calls[tool] = _merged(decode_calls(row.data), mine, lambda c: c.at_ms)
                    elif row.series == MEMORY:
                        self._series.memory = _merged(decode_memory(row.data), self._series.memory, lambda s: s.at_ms)
                    elif row.series == RESTARTS:
                        self._series.restarts = _merged(decode_times(row.data), self._series.restarts, lambda t: t)
                except (ValueError, zlib.error, IndexError) as exc:
                    logger.warning("health: skipping unreadable row %s: %s", row.id, exc)

    def take_dirty(self) -> list[HealthRow] | None:
        """The rows of the series changed since last asked, or None if
        none changed. Changed again before they're written, they're asked
        for again ([`mark_dirty`])."""
        with self._lock:
            if not self._dirty:
                return None
            dirty, self._dirty = self._dirty, set()
            return [self._row(series) for series in sorted(dirty)]

    def mark_dirty(self, rows: Iterable[HealthRow]) -> None:
        """`rows` weren't written after all: write them next time."""
        with self._lock:
            self._dirty.update(row.series for row in rows)

    def rows(self) -> list[HealthRow]:
        """Every series' row, as it is now."""
        with self._lock:
            names = [TOOL_PREFIX + t for t in sorted(self._series.calls)] + [MEMORY, RESTARTS]
            return [self._row(name) for name in names]

    def calls(self) -> dict[str, list[Call]]:
        with self._lock:
            return {tool: list(calls) for tool, calls in sorted(self._series.calls.items())}

    def memory(self) -> list[MemorySample]:
        with self._lock:
            return list(self._series.memory)

    def restarts(self) -> list[int]:
        with self._lock:
            return list(self._series.restarts)

    def _row(self, series: str) -> HealthRow:
        if series.startswith(TOOL_PREFIX):
            samples = list(self._series.calls.get(series[len(TOOL_PREFIX) :], ()))
            data, latest = encode_calls(samples), samples[-1].at_ms if samples else None
        elif series == MEMORY:
            samples = list(self._series.memory)
            data, latest = encode_memory(samples), samples[-1].at_ms if samples else None
        else:
            samples = list(self._series.restarts)
            data, latest = encode_times(samples), samples[-1] if samples else None
        return HealthRow(
            id=f"{SERVER}:{series}",
            source=SERVER,
            series=series,
            count=len(samples),
            latest=None if latest is None else iso(latest),
            data=data,
        )


def _merged(older, newer, at) -> deque:
    """The latest `KEEP` of `older` (read back) and `newer` (recorded
    since), by time; a sample in both counts once."""
    seen = {}
    for sample in [*older, *newer]:
        seen[sample] = None
    return deque(sorted(seen, key=at)[-KEEP:], maxlen=KEEP)


def iso(at_ms: int) -> str:
    """`at_ms` as an ISO 8601 UTC timestamp, to the millisecond."""
    return datetime.fromtimestamp(at_ms / 1000, tz=timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


# -- the tab, and writing to it --------------------------------------------------


class HealthTab:
    """The Health tab: its rows, read and written whole. Writing keeps
    other sources' rows (the app's, later) as they are."""

    def __init__(self, sheet: RowSheet[HealthRow]) -> None:
        self._sheet = sheet

    @staticmethod
    def ensure(sheets_client: SheetsClient, spreadsheet_id: str) -> "HealthTab":
        """The tab, adding it the first time. Adding a tab writes, so this
        holds `HEALTH_TAB_LOCK`."""
        with HEALTH_TAB_LOCK:
            return HealthTab(
                RowSheet.ensure(
                    sheets_client,
                    spreadsheet_id,
                    role=calendar_metadata_sheet.HEALTH_SHEET_ROLE,
                    title=calendar_metadata_sheet.HEALTH_SHEET_TITLE,
                    row_type=HealthRow,
                    required=("id", "source", "series", "data"),
                )
            )

    def read(self) -> list[HealthRow]:
        return self._sheet.read()

    def write(self, changed: list[HealthRow]) -> None:
        """Puts `changed` in place of the rows with their ids, adding those
        new, the rest kept -- read and written holding `HEALTH_TAB_LOCK`,
        so nothing else's write to it comes in between."""
        with HEALTH_TAB_LOCK:
            rows = {row.id: row for row in self._sheet.read()}
            rows.update({row.id: row for row in changed})
            self._sheet.write(sorted(rows.values(), key=lambda r: (r.source, r.series)))


class HealthWriter:
    """Writes the recorder's changes to the Health tab, from a thread of
    its own: once started, it opens the tab (`open_tab`), takes in what
    it kept, adds this start to `restarts`, then writes what changed every
    `interval` seconds, and once more as the process exits. A failure is
    logged, and what wasn't written is tried again next time."""

    def __init__(
        self,
        recorder: HealthRecorder,
        open_tab: Callable[[], HealthTab],
        *,
        interval: float = FLUSH_SECONDS,
    ) -> None:
        self._recorder = recorder
        self._open_tab = open_tab
        self._interval = interval
        self._tab: HealthTab | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._flush_lock = threading.Lock()
        self.loaded = threading.Event()
        """Set once what the tab kept is taken in (or that's failed)."""

    def start(self) -> None:
        self._recorder.record_restart()
        self._thread = threading.Thread(target=self._run, name="health-writer", daemon=True)
        self._thread.start()
        atexit.register(self.stop)

    def stop(self) -> None:
        """Writes what's left, then stops. Waits for a write already under
        way, a little."""
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        self.flush()

    def _run(self) -> None:
        self._load()
        while not self._stop.wait(self._interval):
            # Couldn't open it before (offline, say): try again.
            if self._tab is None:
                self._load()
            else:
                self.flush()

    def _load(self) -> None:
        try:
            tab = self._open_tab()
            self._recorder.merge(tab.read())
            self._tab = tab
        except Exception:  # noqa: BLE001 -- health is a bonus; the server carries on.
            logger.exception("health: couldn't read the Health tab")
        finally:
            self.loaded.set()
        self.flush()

    def flush(self) -> None:
        """Writes the series changed since the last write, if any."""
        with self._flush_lock:
            if self._tab is None:
                return
            rows = self._recorder.take_dirty()
            if rows is None:
                return
            try:
                self._tab.write(rows)
            except Exception:  # noqa: BLE001 -- tried again next time.
                logger.exception("health: couldn't write the Health tab")
                self._recorder.mark_dirty(rows)


# -- reading it back --------------------------------------------------------------


def _percentile(values: list[int], q: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))]


def latency_summary(values: list[int]) -> dict[str, int] | None:
    """The median, 95th percentile and slowest of `values`, in ms."""
    if not values:
        return None
    return {
        "median_ms": int(statistics.median(values)),
        "p95_ms": _percentile(values, 0.95),
        "max_ms": max(values),
    }

