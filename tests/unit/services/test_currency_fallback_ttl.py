"""A guessed rate must not be cached as long as a published one (#1615).

``CurrencyService`` never raises: when the upstream is down, slow or
returns garbage it answers from the hardcoded offline table. That answer
used to be written into the cache with the same ``cache_ttl_seconds`` a
real fetch earns — an hour by default. One second of network trouble
landing on a cold cache therefore priced every exchange, market, game and
withdraw off a frozen anchor for the next hour, with nothing but a
warning in the log to say so, and no way to shorten it from outside.

The two kinds of answer now carry their own lifetime. These tests pin
both directions: the fallback entry expires in a minute, and the real one
still does not.
"""

from __future__ import annotations

from telegram_invite_bot.services.currency_service import (
    _FALLBACK_CACHE_TTL_SECONDS,
    CurrencyService,
)


class _Switchable(CurrencyService):
    """Serves whichever kind of table the test asks for, on a fake clock."""

    def __init__(self, *, fallback: bool, cache_ttl_seconds: float = 3600.0) -> None:
        super().__init__(cache_ttl_seconds=cache_ttl_seconds)
        self.fallback = fallback
        self.fetches = 0
        self.clock = 1_000.0

    def _now(self) -> float:
        return self.clock

    async def _compute_base_rates(self) -> tuple[dict[str, float], bool]:
        self.fetches += 1
        return {"USD": 0.001, "RUB": 0.09}, self.fallback


async def test_a_fallback_table_expires_in_a_minute() -> None:
    service = _Switchable(fallback=True)
    await service.get_rate("RUB")
    assert service.fetches == 1

    # Past the short TTL and nowhere near the configured hour: the only
    # thing that can expire this entry is its own shorter lifetime.
    service.clock += _FALLBACK_CACHE_TTL_SECONDS + 1.0

    await service.get_rate("RUB")
    assert service.fetches == 2, "the offline table was cached for the full TTL"


async def test_a_real_table_survives_that_same_step() -> None:
    """The control: without it the test above would pass on a broken cache."""
    service = _Switchable(fallback=False)
    await service.get_rate("RUB")
    service.clock += _FALLBACK_CACHE_TTL_SECONDS + 1.0

    await service.get_rate("RUB")
    assert service.fetches == 1, "a published rate lost its hour"


async def test_a_real_table_still_expires_on_its_own_ttl() -> None:
    service = _Switchable(fallback=False)
    await service.get_rate("RUB")
    service.clock += service._cache_ttl + 1.0  # noqa: SLF001

    await service.get_rate("RUB")
    assert service.fetches == 2


async def test_a_shorter_configured_ttl_is_not_lengthened() -> None:
    """A deployment asking for a fresher table must not be overruled.

    The degraded path picks the *minimum* of the two for exactly this
    case: a bare 60s constant would hand a service configured for a
    ten-second cache a six-times longer life on its worst answer.
    """
    service = _Switchable(fallback=True, cache_ttl_seconds=10.0)
    await service.get_rate("RUB")
    service.clock += 11.0

    await service.get_rate("RUB")
    assert service.fetches == 2, "the fallback entry outlived the configured TTL"


def test_the_short_ttl_is_shorter_than_the_default_one() -> None:
    # Pins the relationship the whole feature rests on, so a future edit
    # to either number cannot silently make them equal.
    assert 0.0 < _FALLBACK_CACHE_TTL_SECONDS < CurrencyService()._cache_ttl  # noqa: SLF001
