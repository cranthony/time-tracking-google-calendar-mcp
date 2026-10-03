import threading

import pytest

from calendar_clients.write_lock import WRITE_LOCK, WriteLock, WriteLockNotHeldError, requires_write_lock


def _in_another_thread(fn):
    """`fn()`'s result, run on a new thread -- or what it raised, re-raised here."""
    outcome = {}

    def run():
        try:
            outcome["result"] = fn()
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


@pytest.mark.without_write_lock
class TestWriteLock:
    def test_is_held_only_by_the_thread_that_entered_it(self):
        lock = WriteLock()
        assert not lock.held()

        with lock:
            assert lock.held()
            assert not _in_another_thread(lock.held)

        assert not lock.held()

    def test_is_reentrant_and_held_until_the_outermost_exit(self):
        lock = WriteLock()

        with lock:
            with lock:
                assert lock.held()
            assert lock.held()

        assert not lock.held()

    def test_another_thread_waits_for_it(self):
        lock = WriteLock()
        entered = threading.Event()

        def enter():
            with lock:
                entered.set()

        with lock:
            thread = threading.Thread(target=enter)
            thread.start()
            assert not entered.wait(0.05)
        thread.join()

        assert entered.is_set()


@pytest.mark.without_write_lock
class TestRequiresWriteLock:
    @requires_write_lock
    def _write(self, value):
        return value

    def test_refuses_without_the_lock(self):
        with pytest.raises(WriteLockNotHeldError, match="_write writes to Google"):
            self._write(1)

    def test_runs_with_the_lock(self):
        with WRITE_LOCK:
            assert self._write(1) == 1

    def test_refuses_on_a_thread_other_than_the_holders(self):
        with WRITE_LOCK:
            with pytest.raises(WriteLockNotHeldError):
                _in_another_thread(lambda: self._write(1))
