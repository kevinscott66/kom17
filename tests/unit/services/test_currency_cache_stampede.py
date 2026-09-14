"""An expired rate cache must cost one upstream fetch, not one per caller.

``CurrencyService`` is a singleton shared by every handler and both fiat
webhooks, and its refresh is an ``await`` point. Reading the cache and
then fetching without a guard means every request that arrives while the
table is stale sees the same miss and starts its own call to
exchangerate-api — a quota that is billed per call, and a latency each
waiter pays in full instead of the first one paying it for all of them.

The TTL-expiry case is the one that actually happens in production: the
table is warm for an hour, then a burst lands on the boundary. Cold start
is covered too, because startup warming and the first webhook can race.
"""

from __future__ import annotations

import asyncio

from telegram_invite_bot.services.currency_service import CurrencyService


class _Counting(CurrencyService):
    """Counts fetches and yields during one, so callers can pile up.

    The ``sleep(0)`` loop is the whole point: without it the fetch never
    gives the event loop a chance to schedule the other callers, and the
    test would pass even against an unguarded cache.
    """

    def __init__(self) -> None:
        super().__init__()
        self.fetches = 0
        self.clock = 1_000.0

    def _now(self) -> float:
        return self.clock

    async def _compute_base_rates(self) -> tuple[dict[str, float], bool]:
        self.fetches += 1
        for _ in range(5):
            await asyncio.sleep(0)
        # ``False`` = a real table, so these fetches are cached for the
        # full TTL and the boundary test below still steps over it.
        return {"USD": 0.001, "RUB": 0.09}, False


async def test_a_cold_burst_fetches_once_not_once_per_caller() -> None:
    service = _Counting()

    await asyncio.gather(*(service.get_rate("RUB") for _ in range(20)))

    assert service.fetches == 1, "each caller ran its own upstream fetch"


async def test_a_burst_on_the_ttl_boundary_fetches_once() -> None:
    """The production shape: warm for an hour, then everyone misses at once."""
    service = _Counting()
    await service.get_rate("RUB")
    assert service.fetches == 1

    # Step past the TTL so the next read finds the table stale.
    service.clock += service._cache_ttl + 1.0  # noqa: SLF001

    await asyncio.gather(*(service.get_rate("RUB") for _ in range(20)))

    assert service.fetches == 2, "the expiry fanned out into one fetch per caller"


async def test_the_refreshed_table_is_the_one_every_caller_gets() -> None:
    """Collapsing the fetches must not leave a waiter holding a stale table."""
    service = _Counting()
    await service.get_rate("RUB")
    service.clock += service._cache_ttl + 1.0  # noqa: SLF001

    results = await asyncio.gather(*(service.usd_to_rub() for _ in range(10)))

    assert results == [90.0] * 10


async def test_a_warm_read_does_not_queue_behind_a_refresh() -> None:
    """Hits stay outside the lock — a cache hit must never wait on a fetch."""
    service = _Counting()
    await service.get_rate("RUB")

    async with service._fill_lock:  # noqa: SLF001
        # Somebody else is mid-refresh. The table is still warm, so this
        # read has no business waiting for them.
        rate = await asyncio.wait_for(service.get_rate("RUB"), timeout=1.0)

    assert rate == 0.09
    assert service.fetches == 1
