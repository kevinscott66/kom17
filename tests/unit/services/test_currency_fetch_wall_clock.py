"""#1936: the currency fetch is bounded by wall clock, not just by phase.

``CurrencyService`` builds its httpx client with a single ``timeout``
number, which httpx spreads over connect / write / pool / read as four
*independent* budgets. None of them bounds their sum. ``send_capped``
streams the body with ``aiter_bytes()``, so an upstream that answers the
handshake and then emits one small chunk just under the read timeout,
forever, is never timed out by httpx at all.

That is not a cosmetic difference here. ``services/payments/fx.py``
caps the money-path client at ``FX_UPSTREAM_TIMEOUT_SECONDS`` (2.5)
specifically so it loses the race to ``FX_TIMEOUT_SECONDS`` (3.0): when
*we* give up, the offline table is computed and cached, and the next
caller answers from memory; when the caller's ``wait_for`` gives up
first it cancels us before anything is cached, and every later call
pays the same three seconds again with no cache entry to expire
(#1614). A drip flips exactly that order.

These tests exercise the seam through a ``MockTransport``, which
enforces no timeouts of its own — so what they measure is our cap and
nothing else.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator

import httpx

from telegram_invite_bot.services.currency_service import (
    _FALLBACK_CACHE_TTL_SECONDS,
    CurrencyService,
)

_PAYLOAD = json.dumps({"rates": {"RUB": 90.0, "EUR": 0.9}}).encode()


def _dripping_transport(chunk_pause: float) -> httpx.MockTransport:
    """A well-behaved-looking upstream that answers one byte at a time.

    Every individual read completes in ``chunk_pause``; only their sum
    is long. This is the shape a per-phase timeout cannot see.
    """

    async def handler(_request: httpx.Request) -> httpx.Response:
        async def drip() -> AsyncIterator[bytes]:
            for byte in _PAYLOAD:
                await asyncio.sleep(chunk_pause)
                yield bytes([byte])

        return httpx.Response(200, content=drip())

    return httpx.MockTransport(handler)


async def test_a_dripping_upstream_is_cut_off_by_the_total_budget() -> None:
    """The body would take ~2.7 s to arrive in 0.05 s sips; the service
    is built with a 0.3 s budget and must give up inside it — and it
    must report the giving-up as an ordinary "no rates", because the
    caller's whole degradation story is built on ``None``.
    """
    transport = _dripping_transport(chunk_pause=0.05)
    async with httpx.AsyncClient(transport=transport) as client:
        service = CurrencyService(client=client, timeout=0.3)
        started = time.monotonic()
        result = await service._fetch()  # noqa: SLF001 — the unit under test
        elapsed = time.monotonic() - started

    assert result is None, "the drip was allowed to run past the client budget"
    # Generous ceiling: the point is that it is bounded at all, not that
    # the event loop is punctual. The unbounded read would take ~2.7 s.
    assert elapsed < 1.5


async def test_the_cap_does_not_touch_an_upstream_that_answers() -> None:
    """The control. Without it the test above would also pass on a
    service that had simply stopped fetching anything.
    """

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_PAYLOAD)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        service = CurrencyService(client=client, timeout=0.3)
        result = await service._fetch()  # noqa: SLF001 — the unit under test

    assert result == {"rates": {"RUB": 90.0, "EUR": 0.9}}


async def test_the_timeout_reaches_the_cache_as_a_fallback_entry() -> None:
    """The consequence #1614 actually cares about: a cut-off fetch must
    leave a *cached offline* table behind, so the next caller answers
    from memory instead of repeating the wait. A cancellation from the
    outside caches nothing — that is the state this fix exists to keep
    the service out of.

    The TTL is the discriminator, not the mere presence of an entry: an
    unbounded read eventually delivers the drip in full and caches it as
    a *real* table for the configured hour, which looks identical from
    the outside until the pricing is an hour stale.
    """
    transport = _dripping_transport(chunk_pause=0.05)
    async with httpx.AsyncClient(transport=transport) as client:
        service = CurrencyService(client=client, timeout=0.3)
        first = await service.get_rate("RUB")
        assert first is not None

        cached = service._cache  # noqa: SLF001 — the cache entry is the assertion
        assert cached is not None
        assert cached[2] == _FALLBACK_CACHE_TTL_SECONDS, "cached as a published rate"

        # Same instance, no second fetch: the entry is in the cache.
        started = time.monotonic()
        second = await service.get_rate("RUB")
        assert time.monotonic() - started < 0.1

    assert second == first
