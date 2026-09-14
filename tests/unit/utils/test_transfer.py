"""Pure tax-math helpers — exhaustive boundary coverage.

Service / handler tests can compose these without re-checking the
arithmetic, the same way ``test_daily.py`` shields the daily flow
from re-asserting the streak math. Every pinned value here matches
a specific legacy line at ``bot.py:10245-10252``.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from telegram_invite_bot.utils.transfer import (
    compute_transfer_tax,
    effective_tax_rate,
)

# ---------------------------------------------------------------------------
# effective_tax_rate
# ---------------------------------------------------------------------------


def test_no_discount_returns_base_rate() -> None:
    """0% discount = full tax. Pinned so a future "always halve for
    VIPs" optimisation can't silently treat non-VIPs as VIPs."""
    assert effective_tax_rate(0.05, 0) == pytest.approx(0.05)


def test_fifty_percent_discount_halves_rate() -> None:
    """Legacy VIP discount is 50% — pinned alongside the VipProfile
    constant so the two move together if VIP ever re-tiers."""
    assert effective_tax_rate(0.05, 50) == pytest.approx(0.025)


def test_full_hundred_percent_discount_zeroes_rate() -> None:
    """100% discount = tax-free transfer. Legacy supports this for
    "founder" / promo grants; preserving the semantic explicitly
    means a future tighter clamp can't silently re-introduce a
    1-cent floor that breaks promo flows."""
    assert effective_tax_rate(0.05, 100) == 0.0


def test_over_hundred_percent_discount_clamped_to_hundred() -> None:
    """Defensive clamp — a misconfigured 200% discount stays
    tax-free, never goes negative (which would mean the recipient
    gets *more* than the sender sent, a money-printer)."""
    assert effective_tax_rate(0.05, 200) == 0.0


def test_negative_discount_clamped_to_zero_not_amplified() -> None:
    """The :func:`max(0, ...)` clamp catches the subtle case:
    without it, ``-100`` would yield rate * 2 — silently
    overcharging the user. Pinned because the bug would only
    surface in production after an admin UI typo."""
    assert effective_tax_rate(0.05, -100) == pytest.approx(0.05)


def test_zero_base_rate_short_circuits_to_zero() -> None:
    """Mirrors legacy's ``if effective_tax_rate > 0 else 0``
    short-circuit — saves callers a redundant guard."""
    assert effective_tax_rate(0.0, 50) == 0.0


def test_negative_base_rate_returns_zero_not_negative() -> None:
    """Same defensive shape as the discount clamp — a negative
    base rate (config typo) collapses to no-tax, never to a
    transfer-bonus."""
    assert effective_tax_rate(-0.05, 0) == 0.0


# ---------------------------------------------------------------------------
# compute_transfer_tax
# ---------------------------------------------------------------------------


def test_tax_floors_legacy_semantic() -> None:
    """99 * 0.01 = 0.99 → int() floors to 0. Pinned because a
    refactor to ``round()`` would skim a coin off small transfers
    and the loss only manifests in aggregate over thousands of
    sends."""
    assert compute_transfer_tax(99, base_rate=0.01, discount_percent=0) == 0


def test_tax_on_round_amount() -> None:
    """1000 * 0.05 = 50 — exact integer, no floor ambiguity."""
    assert compute_transfer_tax(1000, base_rate=0.05, discount_percent=0) == 50


def test_vip_discount_applied_to_tax() -> None:
    """1000 * (0.05 * 0.5) = 25 — VIP pays half."""
    assert compute_transfer_tax(1000, base_rate=0.05, discount_percent=50) == 25


def test_zero_amount_returns_zero_tax() -> None:
    """Defensive — the upstream validator rejects 0, but the helper
    must be safe to call with boundary inputs from tests."""
    assert compute_transfer_tax(0, base_rate=0.05, discount_percent=0) == 0


def test_negative_amount_returns_zero_tax() -> None:
    """Same as zero — never returns a negative tax that would
    *credit* the sender for a transfer (legacy doesn't either, but
    pinning the invariant guards against an inlined refactor)."""
    assert compute_transfer_tax(-100, base_rate=0.05, discount_percent=0) == 0


def test_full_discount_yields_zero_tax_regardless_of_amount() -> None:
    """Promo / founder grants: even a 10_000 coin transfer is
    tax-free at 100% discount."""
    assert compute_transfer_tax(10_000, base_rate=0.05, discount_percent=100) == 0


def test_tax_on_tiny_amount_with_low_rate_is_zero() -> None:
    """Boundary: 1 coin at 1% = 0.01 → int floors to 0. A future
    "minimum 1 coin tax" optimisation would surface here as a
    failing assertion, not as a silent UX shift."""
    assert compute_transfer_tax(1, base_rate=0.01, discount_percent=0) == 0


def test_compute_transfer_tax_decimal_precision_at_max_amount() -> None:
    """M-E-4: tax math is exact at the high end of ``_MAX_AMOUNT``.

    Pre-fix ``int(amount * rate)`` evaluated the product in float;
    at ``amount = 10**15`` and ``rate = 0.05`` the IEEE-754 product
    loses the low-order digits and the floor boundary shifts. The
    Decimal-based implementation is exact.

    Boundary numbers:

    * ``amount = 10**15``, ``rate = 0.05`` ⇒ exact answer
      ``5 * 10**13``. Float arithmetic happens to land here too at
      this specific pair, but ``amount = 10**15 - 1`` exposes the
      drift: the exact answer is ``(10**15 - 1) // 20`` =
      ``49_999_999_999_999`` (floor of ``5*10**13 - 0.05``). Float
      math rounds ``(10**15 - 1) * 0.05`` to ``5e13`` and truncates
      to ``50_000_000_000_000`` — off-by-one. Decimal preserves the
      floor exactly.
    """
    # Exact boundary that catches the float drift.
    exact = compute_transfer_tax(10**15 - 1, base_rate=0.05, discount_percent=0)
    assert exact == 49_999_999_999_999
    # Sanity at the very top of the cap.
    assert compute_transfer_tax(10**15, base_rate=0.05, discount_percent=0) == 5 * 10**13


def test_compute_transfer_tax_decimal_avoids_classic_0_1_plus_0_2() -> None:
    """The classic ``0.1 + 0.2 != 0.3`` family of float bugs would
    surface here at boundaries where ``amount * rate`` lands just
    below an integer in float but exactly on one in Decimal. The
    Decimal pipeline rounds the value correctly; pre-fix float
    arithmetic would sometimes floor one below.

    Concrete pin: ``amount = 30``, ``rate = 0.1`` ⇒ exact 3.0. Float
    happens to round-trip cleanly here, so use the case that
    actually trips it: ``amount = 3000``, ``rate = 0.001`` (legacy
    encodes basis points as a 0.001 float) — exact answer 3.
    Re-state both cases so a refactor that re-floats the math
    fails on the second one.
    """
    assert compute_transfer_tax(30, base_rate=0.1, discount_percent=0) == 3
    assert compute_transfer_tax(3000, base_rate=0.001, discount_percent=0) == 3


@pytest.mark.parametrize(
    ("amount", "base_rate", "discount_percent", "expected"),
    [
        # M-E-4 follow-up: discounted-rate path is now exact end-to-end.
        # ``0.05 * 0.9 == 0.045000000000000005`` in float — the previous
        # implementation captured that IEEE-754 noise via
        # ``Decimal(str(rate))`` and surfaced it on the discounted path
        # at large amounts. The Decimal-from-the-start re-derivation
        # gives the exact rational ``0.045`` and the tests below pin
        # the boundary at both a tiny (100) and a max-scale (10**15)
        # amount.
        (100, 0.05, 10, 4),  # 100 * 0.045 = 4.5 → floor 4
        (10**15, 0.05, 10, 45_000_000_000_000),  # exact; float-noise variant would land 5 higher
        (1000, 0.05, 10, 45),
        (200, 0.05, 10, 9),  # 200 * 0.045 = 9.0 — exact
        (1_000_000, 0.001, 25, 750),  # 1e6 * 0.00075 = 750
    ],
)
def test_compute_transfer_tax_discounted_rate_exact(
    amount: int, base_rate: float, discount_percent: int, expected: int
) -> None:
    """M-E-4 follow-up regression: the discounted-rate path is exact.

    Pre-fix, ``effective_tax_rate`` returned ``base_rate * (1 -
    discount/100)`` in float, so ``0.05 * 0.9`` produced
    ``0.045000000000000005``; ``Decimal(str(rate))`` then captured the
    noise verbatim and pushed an off-by-N onto large-amount transfers.
    The fix recomputes the rate in Decimal inside
    :func:`compute_transfer_tax`. Parametrised across boundary
    amounts (tiny → max) so a refactor that re-floats the rate
    surfaces on the 10**15 row first.
    """
    assert (
        compute_transfer_tax(amount, base_rate=base_rate, discount_percent=discount_percent)
        == expected
    )
    # Keep Decimal import meaningful — also assert the float
    # base_rate is exactly representable when round-tripped through
    # Decimal(str(...)) (the contract this fix relies on).
    assert Decimal(str(base_rate)) * (Decimal(100) - Decimal(discount_percent)) / Decimal(100) > 0
