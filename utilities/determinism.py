"""The clock and the random ids, made fixed for conformance/ when asked.

conformance/ runs the same scenarios against this server and its Rust
port and compares what each did, so both read the time and make ids the
same way, through here:

- `now()` is the current time -- or, if `CTT_NOW` is set (an ISO 8601
  time with an offset), always that.
- `random_token(alphabet, length)` is a random string -- or, if
  `CTT_DETERMINISTIC_IDS` is set, the next of a fixed sequence: token n
  (counting from 1, across every call in the process) is the first
  `length` bytes of SHA-256("ctt-id:<n>"), each byte mapped to
  `alphabet[byte % len(alphabet)]`.

Neither is set outside conformance/.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import secrets
import threading
from datetime import datetime, timezone

_counter = itertools.count(1)
_counter_lock = threading.Lock()


def now() -> datetime:
    fixed = os.environ.get("CTT_NOW")
    if fixed:
        return datetime.fromisoformat(fixed)
    return datetime.now(timezone.utc)


def random_token(alphabet: str, length: int) -> str:
    if not os.environ.get("CTT_DETERMINISTIC_IDS"):
        return "".join(secrets.choice(alphabet) for _ in range(length))
    with _counter_lock:
        n = next(_counter)
    digest = hashlib.sha256(f"ctt-id:{n}".encode()).digest()
    return "".join(alphabet[byte % len(alphabet)] for byte in digest[:length])
