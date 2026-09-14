"""``RpRateLimiter`` — the sliding window AND the table behind it.

The limiter is module-level and lives for the whole process, so its
state has two things to get right at once: the per-key window must keep
rejecting a flooder, and the table of keys must not accumulate one deque
per (chat, user) pair that ever ran an RP action. The sweep is what
makes the second true, and the first is exactly what a careless sweep
would break — so both are pinned here.
"""

from __future__ import annotations

from telegram_invite_bot.handlers.rp import RpRateLimiter


class _Clock:
    """Manual clock — the limiter takes ``time_fn`` for this reason."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_window_admits_the_cap_and_rejects_beyond_it() -> None:
    clock = _Clock()
    limiter = RpRateLimiter(time_fn=clock)

    for _ in range(RpRateLimiter._MAX_IN_WINDOW):
        assert limiter.allow(chat_id=-1, user_id=7) is True
    assert limiter.allow(chat_id=-1, user_id=7) is False

    # The oldest stamp ages out → one more slot opens, not a full reset.
    clock.advance(RpRateLimiter._WINDOW_SEC + 0.1)
    assert limiter.allow(chat_id=-1, user_id=7) is True


def test_idle_keys_are_swept() -> None:
    """One-off users must not pin a deque for the life of the process.

    Every key here goes quiet well before the sweep runs, so after it
    fires the table should hold only the key that is still active.
    """
    clock = _Clock()
    limiter = RpRateLimiter(time_fn=clock)

    for uid in range(RpRateLimiter._SWEEP_EVERY - 1):
        assert limiter.allow(chat_id=-1, user_id=uid) is True
    assert len(limiter._hits) == RpRateLimiter._SWEEP_EVERY - 1

    # Everyone above has now gone quiet for longer than the window.
    clock.advance(RpRateLimiter._WINDOW_SEC + 1)
    assert limiter.allow(chat_id=-1, user_id=999_999) is True
    assert set(limiter._hits) == {(-1, 999_999)}


def test_sweep_does_not_hand_a_flooder_a_fresh_allowance() -> None:
    """The sweep must skip keys with a stamp still inside the window.

    Dropping a live key is not a memory optimisation — the next call
    rebuilds an empty deque and admits, which resets the flooder's
    allowance to full. That is the failure mode an LRU cap would have
    here, and the reason this table is swept by staleness instead.
    """
    clock = _Clock()
    limiter = RpRateLimiter(time_fn=clock)

    flooder = {"chat_id": -1, "user_id": 777}
    for _ in range(RpRateLimiter._MAX_IN_WINDOW):
        assert limiter.allow(**flooder) is True
    assert limiter.allow(**flooder) is False

    # Push the limiter past its sweep threshold with unrelated traffic.
    # The flooder's stamps are still inside the window, so they survive.
    for uid in range(RpRateLimiter._SWEEP_EVERY + 1):
        limiter.allow(chat_id=-1, user_id=uid)

    assert (-1, 777) in limiter._hits
    assert limiter.allow(**flooder) is False


def test_keys_are_scoped_per_chat() -> None:
    """A user drained in one group still acts in another."""
    clock = _Clock()
    limiter = RpRateLimiter(time_fn=clock)

    for _ in range(RpRateLimiter._MAX_IN_WINDOW):
        limiter.allow(chat_id=-1, user_id=7)
    assert limiter.allow(chat_id=-1, user_id=7) is False
    assert limiter.allow(chat_id=-2, user_id=7) is True
