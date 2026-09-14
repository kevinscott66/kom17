"""Pure tax-math helpers for the /send (transfer) flow.

The legacy block at ``bot.py:10244-10250``::

    effective_tax_rate = COINS_TRANSFER_TAX
    vip_discount = ItemEffects.get_transfer_tax_discount_percent(from_id)
    if vip_discount > 0:
        effective_tax_rate = COINS_TRANSFER_TAX * max(0.0, 1 - vip_discount / 100)
    tax = int(amount * effective_tax_rate) if effective_tax_rate > 0 else 0
    final_amount = amount - tax

The ``if vip_discount > 0`` guard is quoted here on purpose: an earlier
version of this docstring showed the formula without it and so made
legacy look like it turned a negative discount into a surcharge. It
never could — the guard skips the line entirely.

Three behaviours rolled into one inline block: discount clamping (a
discount above 100 cannot drive the rate negative), int-truncation
(``int()`` floors positive floats), and the conditional skip when
rate is 0. Extracting them here gives the upcoming TransferService
a pure, fully-unit-tested arithmetic core — same shape as
:mod:`telegram_invite_bot.utils.daily` carved out for /daily before
the service / handler ports landed.

Tests live in ``tests/unit/utils/test_transfer.py``; the service
layer composes these helpers and tests the *flow*, not the math.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal


def effective_tax_rate(base_rate: float, discount_percent: int) -> float:
    """Apply a VIP percent-discount to a base tax rate.

    Mirrors ``COINS_TRANSFER_TAX * max(0.0, 1 - vip_discount / 100)``
    at ``bot.py:10247``, together with the ``if vip_discount > 0``
    guard on the line above it. The two together mean legacy never
    applied a negative discount at all — the guard skips the formula
    and the base rate stands. This helper folds both into one clamp:
    ``max(0, …)`` reproduces the guard and ``min(100, …)`` reproduces
    legacy's own ``max(0.0, …)``, so a misconfigured discount (a
    negative number sneaking through admin UI, a future "penalty" flag
    flipped wrong) still cannot inflate the tax beyond ``base_rate``.
    That is parity, not a repair of a legacy overcharge.

    Symmetrically, ``discount_percent >= 100`` collapses the rate
    to 0 (transfer becomes tax-free) — kept explicit, not silently
    clamped to e.g. 99, because legacy treats 100% discount as
    "VIP gets free transfers" and the new pipeline must preserve
    that semantic exactly.

    Returns 0.0 when ``base_rate`` is 0 (or non-positive) — saves
    the caller a redundant guard and matches the legacy
    ``if effective_tax_rate > 0 else 0`` short-circuit.
    """
    if base_rate <= 0:
        return 0.0
    clamped = max(0, min(100, discount_percent))
    return base_rate * (1 - clamped / 100)


def compute_transfer_tax(amount: int, *, base_rate: float, discount_percent: int) -> int:
    """Return the integer-floored tax for a gross transfer ``amount``.

    Composes :func:`effective_tax_rate` with the documented floor
    rounding policy at ``bot.py:10249``. Floor (not round, not ceil)
    is the legacy semantic — a user transferring 99 coins at 1%
    pays 0 tax, not 1. Pinning floor keeps "you sent 99, recipient
    got 99" reproducible across both pipelines; any change to
    rounding here would silently skim a coin off every small
    transfer.

    Precision (M-E-4 in audits/01_economy.md): the multiplication
    is performed in ``Decimal`` (not ``float``) so it stays exact at
    the high end of ``_MAX_AMOUNT`` (10**15). The previous
    ``int(amount * rate)`` form lost the lower ~10 digits for very
    large amounts because IEEE-754 doubles only carry ~15
    significant decimal digits — at ``amount ~ 10**15`` and
    ``rate = 0.05`` the float product silently misplaced the
    floor boundary. Decimal arithmetic with explicit ``ROUND_FLOOR``
    quantisation produces the same answer as the float form for
    every realistic input (small amount, sane rate) and is exact
    at the extremes a future ``_MAX_AMOUNT`` bump would expose.

    The ``base_rate`` parameter is still ``float`` (legacy parity:
    settings store the rate as a JSON number); we convert via
    ``Decimal(str(...))`` to round-trip its decimal-literal form
    rather than its binary expansion (``Decimal(0.05)`` would carry
    the IEEE noise tail).

    Negative or zero ``amount`` returns 0 — defensive, even though
    the upstream validator should reject those before they reach
    the tax math. Keeps this helper safe to call in tests with
    boundary inputs.
    """
    if amount <= 0:
        return 0
    if base_rate <= 0:
        return 0
    # M-E-4 follow-up: compute the effective rate in Decimal too. The
    # float helper ``effective_tax_rate`` is kept as the public
    # docstring surface, but for the integer-tax math we re-derive the
    # rate from ``Decimal(str(base_rate))`` and an integer-percent
    # discount so the multiplication is exact end-to-end. Going
    # through float here would re-introduce the IEEE-754 drift the
    # audit flagged (e.g. ``0.05 * 0.9 == 0.045000000000000005``).
    clamped_discount = max(0, min(100, discount_percent))
    if clamped_discount >= 100:
        return 0
    base_decimal = Decimal(str(base_rate))
    effective = base_decimal * (Decimal(100) - Decimal(clamped_discount)) / Decimal(100)
    if effective <= 0:
        return 0
    product = Decimal(amount) * effective
    return int(product.quantize(Decimal(1), rounding=ROUND_FLOOR))
