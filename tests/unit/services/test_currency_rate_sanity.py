"""The FX table must only ever hold finite, positive rates.

``_compute_base_rates`` copies whatever the upstream sent into the table
that prices ``/rate``, ``/convert`` and the profile card's rouble line.
An ``isinstance(x, int | float)`` check accepts four values that are not
rates at all — ``True``, ``NaN``, ``Infinity`` and ``0``/negative — and
none of them fail where they enter. They surface a screen away as a
``0.00 €`` quote, a ``nan`` rouble line, or an ``OverflowError`` out of
``round(balance * rate)``. Every leg is pinned here rather than in each
consumer's suite, because the invariant belongs to the table.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from telegram_invite_bot.services.currency_service import (
    COM_TO_CURRENCY,
    CurrencyService,
)
from telegram_invite_bot.services.payments.rates import FALLBACK_USD_TO_RUB


class _Pinned(CurrencyService):
    """A service whose upstream answer is whatever the test hands it."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__()
        self._payload = payload

    async def _fetch(self) -> dict[str, Any] | None:  # noqa: PLR6301
        return self._payload


async def _table(rates: dict[str, Any]) -> dict[str, float]:
    # The discarded half of the pair is the fallback flag the cache
    # reads (#1615); every case here is about the table itself.
    table, _ = await _Pinned({"rates": rates})._compute_base_rates()  # noqa: SLF001
    return table


@pytest.mark.parametrize(
    "junk",
    [
        pytest.param(0, id="zero"),
        pytest.param(-1.5, id="negative"),
        pytest.param(float("nan"), id="nan"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(True, id="bool"),
        pytest.param("0.9", id="string"),
    ],
    # ``True`` is the sly one: ``bool`` subclasses ``int``, so the old
    # check priced the euro at 1.0 USD without anything looking wrong.
)
async def test_a_junk_leg_keeps_the_offline_price(junk: object) -> None:
    """A currency the upstream mispriced falls back, it does not go to 0."""
    table = await _table({"RUB": 100.0, "EUR": junk})

    assert table["EUR"] == COM_TO_CURRENCY["EUR"]


async def test_a_junk_rub_leg_falls_back_to_the_anchor() -> None:
    """RUB is the leg real money is priced through — it must never be NaN."""
    table = await _table({"RUB": float("nan"), "EUR": 0.9})

    # base["RUB"] / base["USD"] is the fix ``usd_to_rub()`` hands the
    # payment code; NaN there passes every ``<= 0`` guard downstream.
    assert table["RUB"] / table["USD"] == pytest.approx(FALLBACK_USD_TO_RUB)


async def test_every_rate_in_the_table_is_finite_and_positive() -> None:
    """The whole-table invariant, on a payload that is junk end to end."""
    table = await _table(
        {"RUB": 0, "EUR": float("inf"), "GBP": float("nan"), "KZT": -3, "USD": True}
    )

    assert table
    for code, rate in table.items():
        assert math.isfinite(rate), code
        assert rate > 0.0, code


async def test_a_good_payload_is_still_used() -> None:
    """The guard must not reject real rates along with the junk."""
    table = await _table({"RUB": 100.0, "EUR": 0.9})

    assert table["EUR"] == pytest.approx(table["USD"] * 0.9)
    assert table["RUB"] / table["USD"] == pytest.approx(100.0)
