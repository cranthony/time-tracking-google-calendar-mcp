import itertools
from datetime import datetime, timezone

from utilities import determinism

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def test_now_is_the_clock_unless_fixed(monkeypatch):
    monkeypatch.delenv("CTT_NOW", raising=False)
    before = datetime.now(timezone.utc)
    assert before <= determinism.now() <= datetime.now(timezone.utc)
    monkeypatch.setenv("CTT_NOW", "2026-10-07T15:30:00-04:00")
    assert determinism.now() == datetime(2026, 10, 7, 19, 30, tzinfo=timezone.utc)


def test_random_tokens_are_random_unless_deterministic(monkeypatch):
    monkeypatch.delenv("CTT_DETERMINISTIC_IDS", raising=False)
    tokens = {determinism.random_token(ALPHABET, 6) for _ in range(20)}
    assert len(tokens) == 20
    assert all(len(t) == 6 and set(t) <= set(ALPHABET) for t in tokens)


def test_deterministic_tokens_are_a_fixed_sequence(monkeypatch):
    """The sequence the Rust port has to make too: token n is the first
    bytes of SHA-256("ctt-id:<n>"), each mapped into the alphabet."""
    monkeypatch.setenv("CTT_DETERMINISTIC_IDS", "1")
    monkeypatch.setattr(determinism, "_counter", itertools.count(1))
    assert [determinism.random_token(ALPHABET, 6) for _ in range(2)] == ["4709iy", "bcyx3f"]
