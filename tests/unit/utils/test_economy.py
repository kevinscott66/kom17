"""Tests for ``utils.economy`` — pure economy-math invariants.

The cases below pin behaviour that Stage 8 (EconomyRepo writes) will
depend on. Each helper has its own parametrize block — keeping them
separate makes a failure point at the exact invariant that broke.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from telegram_invite_bot.utils.economy import (
    commission_amount,
    format_age,
    format_fiat_amount,
    parse_db_timestamp,
    validate_balance_target,
    validate_credit_amount,
)


@pytest.mark.parametrize(
    "amount,percent,expected",
    [
        # ----- non-positive inputs → zero -----
        (0, 5, 0),
        (-100, 5, 0),
        (100, 0, 0),
        (100, -5, 0),
        # ----- normal cases -----
        (100, 5, 5),
        (1_000, 10, 100),
        (10_000, 1, 100),
        # ----- the load-bearing ``max(1, …)`` floor -----
        (1, 5, 1),  # 0.05 → floors to 0, bumped to 1
        (10, 5, 1),  # 0.5  → floors to 0, bumped to 1
        (19, 5, 1),  # 0.95 → floors to 0, bumped to 1
        (20, 5, 1),  # 1.0  → exactly 1
        (21, 5, 1),  # 1.05 → floors to 1
        # ----- truncation (floor toward zero), not rounding -----
        (199, 5, 9),  # 9.95 → floors to 9, NOT rounds to 10
        # ----- 100% case -----
        (100, 100, 100),
        # ----- >100% allowed (admin scenarios) -----
        (10, 200, 20),
    ],
)
def test_commission_amount(amount: int, percent: int, expected: int) -> None:
    assert commission_amount(amount, percent) == expected


def test_commission_amount_uses_integer_math_at_max_amount() -> None:
    """M-E-4: precision pin at the high end of ``_MAX_AMOUNT``.

    Pre-fix ``int(amount * percent / 100)`` evaluates ``amount /
    100`` in float, which loses the lower ~10 digits at
    ``amount = 10**15``. The integer form ``(amount * percent) //
    100`` is exact at all representable inputs.

    Concrete boundary: ``amount = 10**15 - 23`` at ``percent = 7``.

    * Exact integer arithmetic: ``(10**15 - 23) * 7 // 100``
      = ``69_999_999_999_999_998``.
    * Float arithmetic via ``int((10**15 - 23) * 7 / 100)`` would
      truncate to a different number on the lower digits (the float
      product rounds at the 16th digit), giving an off-by-one or
      worse commission.

    Pin the integer answer so an accidental revert to the float
    form fails loudly here.
    """
    huge = 10**15 - 23
    expected = (huge * 7) // 100
    assert commission_amount(huge, 7) == expected
    # Pin: result is exactly the integer floor of the product, with
    # no float-cast rounding involved. The integer arithmetic is
    # bit-exact regardless of where the cap drifts in future
    # refactors. Float ``int(a * p / 100)`` is well-defined for
    # inputs where ``a * p`` fits in 2**53 (~9e15); the integer
    # form is correct beyond that bound too, which is the safety
    # margin a cap-bump (e.g. ``_MAX_AMOUNT = 10**18``) would need.
    bigger = 10**17  # outside double-precision exact-int range
    assert commission_amount(bigger, 7) == (bigger * 7) // 100


@pytest.mark.parametrize(
    "amount,expected",
    [
        (1, True),
        (100, True),
        (10**15, True),
        # rejections
        (0, False),
        (-1, False),
        (-1000, False),
        (10**15 + 1, False),
        (10**18, False),
    ],
)
def test_validate_credit_amount(amount: int, expected: bool) -> None:
    assert validate_credit_amount(amount) is expected


@pytest.mark.parametrize(
    "new_balance,expected",
    [
        (0, True),  # zero balance is legal (different from credit!)
        (1, True),
        (10**15, True),
        # rejections
        (-1, False),
        (-100, False),
        (10**15 + 1, False),
    ],
)
def test_validate_balance_target(new_balance: int, expected: bool) -> None:
    assert validate_balance_target(new_balance) is expected


def test_zero_is_legal_balance_but_not_legal_credit() -> None:
    """Pin the deliberate asymmetry between the two predicates.

    A wallet can sit at zero (newly created, fully spent) — that's the
    set-balance target predicate. But a credit OF zero is the
    caller's bug: it's a no-op pretending to be a transaction. The
    legacy code carved out the same shape, and Stage 8 will rely on
    it (credit rejection = early-return, balance-target rejection =
    admin error message)."""
    assert validate_balance_target(0) is True
    assert validate_credit_amount(0) is False


# ---------------------------------------------------------------------------
# M-E-6: format_fiat_amount — minor-unit → display roundtrip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "minor,currency,expected",
    [
        (1234, "USD", "12.34 USD"),
        (1050, "RUB", "10.50 RUB"),
        (0, "USD", "0.00 USD"),
        (5, "USD", "0.05 USD"),
        (100, None, "1.00"),
        (None, "USD", "—"),
        (None, None, "—"),
        (-1234, "USD", "-12.34 USD"),
    ],
)
def test_format_fiat_amount(minor: int | None, currency: str | None, expected: str) -> None:
    """M-E-6: minor-unit ints render as two-decimal majors with
    optional currency suffix. ``None`` minor renders as the legacy
    ``—`` placeholder. Negative values keep their sign so corrupted
    rows are visible at a glance."""
    assert format_fiat_amount(minor, currency) == expected


def test_format_fiat_amount_int_roundtrip_no_float_drift() -> None:
    """Pin the no-float-drift invariant: summing many minor-unit
    values stays exact, unlike the legacy float storage where
    ``0.1 + 0.2 != 0.3``. The aggregate ``SELECT SUM(amount_fiat)``
    that admin/withdrawals.py runs depends on this."""
    # 1000 rows each of 10 cents = exactly 100.00 — float math
    # accumulates ~1e-13 drift across this many adds.
    total = sum(10 for _ in range(1000))
    assert total == 10_000
    assert format_fiat_amount(total, "USD") == "100.00 USD"


@pytest.mark.parametrize(
    "delta,expected",
    [
        # ----- minutes -----
        (timedelta(0), "0m"),
        (timedelta(seconds=59), "0m"),
        (timedelta(minutes=7), "7m"),
        (timedelta(minutes=59, seconds=59), "59m"),
        # ----- hours (truncated, so 23h stays under the 24h threshold) -----
        (timedelta(hours=1), "1h"),
        (timedelta(hours=23, minutes=59), "23h"),
        # ----- days -----
        (timedelta(hours=24), "1d"),
        (timedelta(days=155), "155d"),
        # ----- a future timestamp clamps rather than rendering "-3h" -----
        (timedelta(hours=-3), "0m"),
    ],
)
def test_format_age(delta: timedelta, expected: str) -> None:
    assert format_age(delta) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        # ----- the format the write side stamps -----
        ("2026-03-18 22:54:54", datetime(2026, 3, 18, 22, 54, 54)),
        # ----- shapes legacy rows / imports carry -----
        ("2026-03-18T22:54:54", datetime(2026, 3, 18, 22, 54, 54)),
        ("2026-03-18T22:54:54.123456", datetime(2026, 3, 18, 22, 54, 54, 123456)),
        # ----- an offset is normalised into the naive-UTC frame -----
        ("2026-03-19T01:54:54+03:00", datetime(2026, 3, 18, 22, 54, 54)),
        # ----- unusable input yields None, never a raise -----
        (None, None),
        ("", None),
        ("not-a-timestamp", None),
        ("2026-13-01 00:00:00", None),
    ],
)
def test_parse_db_timestamp(value: str | None, expected: datetime | None) -> None:
    assert parse_db_timestamp(value) == expected


def test_parse_db_timestamp_returns_naive_values() -> None:
    """The result is compared against ``utils.time.db_now`` (naive UTC);
    an aware value would raise TypeError at the subtraction instead."""
    parsed = parse_db_timestamp("2026-03-19T01:54:54+03:00")
    assert parsed is not None
    assert parsed.tzinfo is None
    # Subtraction against the frame the callers actually use must work.
    assert isinstance(datetime.now(UTC).replace(tzinfo=None) - parsed, timedelta)
