"""Pure P2P marketplace constants + arithmetic (no I/O).

``P2P_COM_RATES`` is the legacy fixed market-rate table, verbatim from
``bot.py:19185`` — minus ``BTC``, which legacy defined but never put on
a button (the currency keyboards at bot.py:19338/19602 offer exactly
the six currencies below), so it is dead config we deliberately do not
port. Legacy is market-price-only: the seller cannot type a custom
price, the order's ``price_per_com`` IS the rate from this table.

``P2P_CURRENCIES`` preserves the legacy button order (RUB/USD then
UAH/EUR then USDT/TON) so the P2/P3 keyboards render identically.
"""

from __future__ import annotations

# Verbatim values from bot.py:19185 (BTC excluded — dead, no button).
P2P_COM_RATES: dict[str, float] = {
    "RUB": 1.0,
    "USD": 0.011,
    "UAH": 0.45,
    "EUR": 0.010,
    "TON": 0.002,
    "USDT": 0.011,
}

# Legacy keyboard order (bot.py:19338-19340): RUB USD / UAH EUR / USDT TON.
P2P_CURRENCIES: tuple[str, ...] = ("RUB", "USD", "UAH", "EUR", "USDT", "TON")


def is_supported_currency(currency: str) -> bool:
    """True iff ``currency`` (case-insensitive) has a market rate."""
    return currency.upper() in P2P_COM_RATES


def market_rate(currency: str) -> float | None:
    """The fixed market rate for ``currency`` per 1 COM; ``None`` if
    unsupported. Legacy fell back to ``1.0`` (bot.py:19356) — we surface
    the unsupported case instead so a typo'd callback can't silently
    price an order in the wrong currency.
    """
    return P2P_COM_RATES.get(currency.upper())


def estimate_fiat(amount_com: int, currency: str) -> float | None:
    """``amount_com`` COM at the market rate, in fiat; ``None`` if the
    currency is unsupported. Mirrors the legacy estimate at
    ``bot.py:19357`` (``amount * rate``)."""
    rate = market_rate(currency)
    if rate is None:
        return None
    return amount_com * rate


__all__ = [
    "P2P_COM_RATES",
    "P2P_CURRENCIES",
    "estimate_fiat",
    "is_supported_currency",
    "market_rate",
]
