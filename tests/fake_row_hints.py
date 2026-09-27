"""A tiny in-memory stand-in for `utilities.row_hints.RowHints`, for
tests of `NotedTimeSheet`/`CompactionJournal` that don't care how hints
are persisted -- just that "no hint yet" is the default, so their
existing full-read behavior is exercised unless a test deliberately
seeds one."""

from __future__ import annotations


class FakeRowHints:
    def __init__(self) -> None:
        self._values: dict[str, int] = {}

    def get(self, name: str) -> int | None:
        return self._values.get(name)

    def set(self, name: str, row_number: int) -> None:
        self._values[name] = row_number
