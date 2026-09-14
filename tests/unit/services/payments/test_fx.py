"""Unit tests for reading the USD/RUB fix on a money path.

One rule, tested from every angle it can be broken from: **never fail,
never block**. A rouble top-up that 500s because a free FX endpoint is
having a bad afternoon is a worse outcome than a top-up priced at the
offline anchor, and a ``/topup`` screen that hangs for 30 seconds on a
dead socket is worse than both.
"""

from __future__ import annotations

import asyncio

import pytest
from loguru import logger

from telegram_invite_bot.services.payments.fx import (
    FX_TIMEOUT_SECONDS,
    resolve_usd_to_rub,
)
from telegram_invite_bot.services.payments.rates import FALLBACK_USD_TO_RUB


class FakeCurrency:
    def __init__(
        self, *, rate: float | None = None, exc: Exception | None = None, delay: float = 0.0
    ) -> None:
        self._rate = rate
        self._exc = exc
        self._delay = delay
        self.calls = 0

    async def usd_to_rub(self) -> float:
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._exc is not None:
            raise self._exc
        assert self._rate is not None
        return self._rate


@pytest.mark.asyncio
async def test_no_service_prices_at_the_anchor() -> None:
    # The service is optional wiring — /topup must still quote.
    assert await resolve_usd_to_rub(None) == FALLBACK_USD_TO_RUB


@pytest.mark.asyncio
async def test_live_fix_is_used_when_available() -> None:
    service = FakeCurrency(rate=97.5)
    assert await resolve_usd_to_rub(service) == 97.5  # type: ignore[arg-type]
    assert service.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("upstream 500"),
        ValueError("garbage payload"),
        # Not a subclass of Exception's usual suspects but still must
        # not escape onto a money path.
        KeyError("RUB"),
    ],
)
async def test_upstream_failure_degrades_to_the_anchor(exc: Exception) -> None:
    assert await resolve_usd_to_rub(FakeCurrency(exc=exc)) == (  # type: ignore[arg-type]
        FALLBACK_USD_TO_RUB
    )


@pytest.mark.asyncio
async def test_a_hanging_upstream_does_not_hang_the_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Shorten the budget rather than sleeping through the real one; what
    # is being pinned is that the wait is bounded at all.
    monkeypatch.setattr("telegram_invite_bot.services.payments.fx.FX_TIMEOUT_SECONDS", 0.01)
    service = FakeCurrency(rate=97.5, delay=5.0)
    assert await resolve_usd_to_rub(service) == FALLBACK_USD_TO_RUB  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_the_failure_log_names_which_failure_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degraded price must say *why* it degraded.

    Production surfaced this: seven days of journal on
    the production host carry three lines reading ``FX lookup failed ()
    — pricing at the offline anchor 90.0``. The parentheses are empty
    because the message interpolated ``str(exc)`` and the most likely
    exception on this path — :class:`TimeoutError`, raised by
    :func:`asyncio.wait_for` — stringifies to the empty string.

    That is the one detail an operator cannot do without here. This
    module's design turns on which of two timeouts fires first: when
    the service's own client gives up it caches the offline table and
    recovers a minute later, but when this module's ``wait_for`` wins
    it cancels the fetch before anything is cached and every later
    call repeats the wait — the docstring on
    :data:`FX_UPSTREAM_TIMEOUT_SECONDS` calls that "priced at the
    offline anchor forever, with no cache entry to expire and no way
    back". A timeout and an HTTP 500 are the two states you must tell
    apart to know which one you are in, and an empty ``()`` tells you
    neither.
    """
    monkeypatch.setattr("telegram_invite_bot.services.payments.fx.FX_TIMEOUT_SECONDS", 0.01)
    service = FakeCurrency(rate=97.5, delay=5.0)
    lines: list[str] = []
    handler_id = logger.add(lines.append, level="WARNING", format="{message}")
    try:
        rate = await resolve_usd_to_rub(service)  # type: ignore[arg-type]
    finally:
        logger.remove(handler_id)

    assert rate == FALLBACK_USD_TO_RUB
    assert lines, "the degradation was not logged at all"
    assert "TimeoutError" in lines[0], (
        "the warning does not name the exception type, so a timeout and an "
        f"upstream error are indistinguishable in the journal: {lines[0]!r}"
    )


def test_the_timeout_is_short_enough_to_sit_under_a_button_press() -> None:
    # A user waiting on an inline keyboard edit; Telegram's callback
    # answer window is 30s but a card that takes seconds already reads
    # as broken.
    assert 0 < FX_TIMEOUT_SECONDS <= 5.0
