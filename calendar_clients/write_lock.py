"""The one lock every write to Google holds, and the check that it does.

The MCP server runs every (sync) tool on its own worker thread, so tool
calls that arrive together really do run at once. Reading alongside one
another is fine, but writes here read, decide, then write -- a note goes
in the row found free, a goal edit rewrites the whole goals tab -- and two
of those interleaved can lose one's write. So every tool that may write
holds `WRITE_LOCK` for its whole call (see server.py's `writes`), and
each method of CalendarClient/SheetsClient that writes is marked
`@requires_write_lock`, which refuses to run unless this thread holds it:
a tool that writes without saying so fails loudly instead of racing.
"""

from __future__ import annotations

import functools
import threading
from collections.abc import Callable
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")


class WriteLockNotHeldError(RuntimeError):
    """A write to Google was attempted without holding `WRITE_LOCK`."""


class WriteLock:
    """A reentrant lock that knows which thread holds it."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._owner: int | None = None
        self._depth = 0

    def __enter__(self) -> "WriteLock":
        self._lock.acquire()
        self._owner = threading.get_ident()
        self._depth += 1
        return self

    def __exit__(self, *exc_info) -> None:
        self._depth -= 1
        if not self._depth:
            self._owner = None
        self._lock.release()

    def held(self) -> bool:
        """Whether this thread holds the lock. Only the holder ever sets
        the owner to its own id, so this needs no lock of its own."""
        return self._owner == threading.get_ident()


WRITE_LOCK = WriteLock()


def requires_write_lock(method: Callable[P, R]) -> Callable[P, R]:
    """Mark `method` as writing to Google: calling it without holding
    `WRITE_LOCK` raises WriteLockNotHeldError."""

    @functools.wraps(method)
    def checked(*args: P.args, **kwargs: P.kwargs) -> R:
        if not WRITE_LOCK.held():
            raise WriteLockNotHeldError(
                f"{method.__qualname__} writes to Google, so its caller must hold WRITE_LOCK"
            )
        return method(*args, **kwargs)

    return checked
