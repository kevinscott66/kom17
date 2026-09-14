"""Unit tests for the /send transfer gate (#500).

Legacy's ``_check_transfer_rate_limit`` (bot.py:18830-18838) had three
properties this middleware has to keep, and the port had lost all three:
a ceiling of six transfers per minute (bot.py:2552), a sustained rate to
match, and an early return for developers (bot.py:18832). The port
shipped a burst of three, one slot back per minute, and no bypass — a
gate six times tighter than the one users were used to, on the command
where a wrong "⏳ Слишком часто" is most annoying.

The clock is overridden rather than monkey-patched so the tests read as
a timeline and mypy keeps its grip on the subclass.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from aiogram.types import User

from telegram_invite_bot.middlewares.transfer_rate_limit import (
    _DEFAULT_CAPACITY,
    _DEFAULT_REFILL_PER_SECOND,
    TransferRateLimitMiddleware,
)

_LEGACY_TRANSFERS_PER_MINUTE = 6


class _FixedClock(TransferRateLimitMiddleware):
    """Middleware whose ``_now`` is a writable timeline cursor."""

    now: float = 1_000.0

    def _now(self) -> float:
        return self.now


class _Event:
    """Minimal message stand-in: a sender plus an awaitable ``answer``."""

    def __init__(self, user_id: int) -> None:
        self.from_user = User(id=user_id, is_bot=False, first_name="T", language_code="ru")
        self.answer = AsyncMock()


def test_defaults_match_the_legacy_ceiling() -> None:
    """Six per minute is the operator default legacy shipped (bot.py:2552).

    Pinned as a literal so a future tuning edit is a deliberate act and
    not a silent re-introduction of the 3-burst regression.
    """
    assert _DEFAULT_CAPACITY == _LEGACY_TRANSFERS_PER_MINUTE
    assert _DEFAULT_REFILL_PER_SECOND == _LEGACY_TRANSFERS_PER_MINUTE / 60.0


async def test_six_transfers_pass_before_the_gate_closes() -> None:
    middleware = _FixedClock()
    handler = AsyncMock(return_value="handled")
    event: Any = _Event(user_id=4242)

    for _ in range(_LEGACY_TRANSFERS_PER_MINUTE):
        assert await middleware(handler, event, {"lang": "ru"}) == "handled"

    assert await middleware(handler, event, {"lang": "ru"}) is None
    assert handler.await_count == _LEGACY_TRANSFERS_PER_MINUTE
    event.answer.assert_awaited_once()


async def test_a_slot_returns_every_ten_seconds() -> None:
    """Sustained rate is six a minute, so one slot back per ten seconds."""
    middleware = _FixedClock()
    handler = AsyncMock(return_value="handled")
    event: Any = _Event(user_id=4243)

    for _ in range(_LEGACY_TRANSFERS_PER_MINUTE):
        await middleware(handler, event, {"lang": "ru"})
    assert await middleware(handler, event, {"lang": "ru"}) is None

    middleware.now += 9.0
    assert await middleware(handler, event, {"lang": "ru"}) is None

    middleware.now += 1.5
    assert await middleware(handler, event, {"lang": "ru"}) == "handled"


async def test_exempt_user_never_consumes_a_token() -> None:
    """Legacy returned before both ``check`` and ``add`` (bot.py:18832-18833).

    The assertion on the bucket table is the load-bearing half: an
    exempt user who still drained tokens would hit the gate the moment
    the bypass was lifted, which is exactly the bug a naive early
    return placed after the bucket write would introduce.
    """
    developer_id = 123456789
    middleware = _FixedClock(is_exempt=lambda user_id: user_id == developer_id)
    handler = AsyncMock(return_value="handled")
    event: Any = _Event(user_id=developer_id)

    for _ in range(_LEGACY_TRANSFERS_PER_MINUTE * 3):
        assert await middleware(handler, event, {"lang": "ru"}) == "handled"

    event.answer.assert_not_awaited()
    assert middleware._buckets == {}


async def test_non_exempt_user_is_still_gated_when_a_predicate_is_set() -> None:
    middleware = _FixedClock(is_exempt=lambda user_id: user_id == 1)
    handler = AsyncMock(return_value="handled")
    event: Any = _Event(user_id=2)

    for _ in range(_LEGACY_TRANSFERS_PER_MINUTE):
        await middleware(handler, event, {"lang": "ru"})

    assert await middleware(handler, event, {"lang": "ru"}) is None
