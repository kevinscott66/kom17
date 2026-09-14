"""Tests for ``utils.numbers``.

Three formatters, each tested across the threshold boundaries that
matter for display. The legacy versions had no tests at all and were
shipping behavioral quirks (``rstrip(".")`` interacting with trim-zero
on integer-valued floats); the test cases below pin those quirks so a
future refactor doesn't silently change wallet-balance rendering for
the existing user base.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.utils.numbers import (
    MAX_DB_INT,
    balance_tier_emoji,
    format_amount_compact,
    format_amount_fine,
    format_number,
    format_xp_short,
    format_xp_spark,
    page_offset,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0"),
        (1, "1"),
        (999, "999"),
        (1_000, "1 000"),
        (12_345, "12 345"),
        (1_234_567, "1 234 567"),
        (-1_234_567, "-1 234 567"),
        (1234.5, "1 234.5"),  # float-safe: decimal point preserved
        (1_000_000.0, "1 000 000.0"),
    ],
)
def test_format_number(value: int | float, expected: str) -> None:
    assert format_number(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0"),
        (500, "500"),
        (999, "999"),
        (1_000, "1.00K"),  # threshold exact
        (1_500, "1.50K"),
        (999_999, "1000.00K"),  # just under M — stays in K with K-form
        (1_000_000, "1.00M"),  # threshold exact
        (2_500_000, "2.50M"),
        (12_345_678, "12.35M"),
    ],
)
def test_format_amount_compact(value: float, expected: str) -> None:
    assert format_amount_compact(value) == expected


def test_format_amount_compact_truncates_sub_one_to_zero() -> None:
    # Documented quirk inherited from legacy: amounts < 1 stringify
    # to ``int(amount)`` which is ``0``. Callers that need fractional
    # display below 1 should use ``format_amount_fine`` instead. This
    # test pins the behavior so it doesn't silently change.
    assert format_amount_compact(0.5) == "0"
    assert format_amount_compact(0.999) == "0"


@pytest.mark.parametrize(
    "value,expected",
    [
        (12_345_678, "12.35M"),
        (1_500_000, "1.50M"),
        (1_000, "1.00K"),
        (12.5, "12.50"),
        (1.0, "1.00"),
        (0.5, "0.5"),  # 6-decimal tier, trailing zeros trimmed
        (0.123_456, "0.123456"),
        (0.000_001, "0.000001"),  # exact lower bound of the 6-dec tier
        (0.000_000_5, "0.0000005"),  # 8-decimal tier kicks in
        (0.000_000_01, "0.00000001"),
    ],
)
def test_format_amount_fine(value: float, expected: str) -> None:
    assert format_amount_fine(value) == expected


def test_format_amount_fine_trims_trailing_zeros_and_dot() -> None:
    # The ``.rstrip("0").rstrip(".")`` chain in the implementation must
    # never leave a stranded decimal point. ``0.5`` formatted to 6
    # decimals = ``"0.500000"`` → trim 0s → ``"0.5"`` (the ``.``
    # survives because it's not trailing). Confirm.
    assert "." in format_amount_fine(0.5)
    assert not format_amount_fine(0.5).endswith(".")
    # And the inverse: a value that round-trips to a whole number in
    # the tier must not end in a stray dot. ``0.000001`` displays as
    # ``"0.000001"`` so this can't trigger here directly, but if
    # callers ever pass a value that formats to ``"1.000000"`` in the
    # 6-dec tier (none reachable today, but defensive), the dot must
    # not survive.
    # Synthetic: a value that the 6-dec tier would print as "0.100000".
    assert format_amount_fine(0.1) == "0.1"


@pytest.mark.parametrize(
    "amount,emoji",
    [
        (0, "👛"),
        (1, "👛"),
        (999, "👛"),
        (1_000, "🪙"),  # tier boundary
        (4_999, "🪙"),
        (5_000, "💰"),  # tier boundary
        (9_999, "💰"),
        (10_000, "💎"),  # tier boundary
        (1_000_000, "💎"),
        (-1, "👛"),  # negative falls through
        (-1_000_000, "👛"),
    ],
)
def test_balance_tier_emoji(amount: int, emoji: str) -> None:
    assert balance_tier_emoji(amount) == emoji


# ---------------------------------------------------------------------------
# XP short-forms (RR-5 #49/#52) — the two live side by side on purpose.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("xp", "expected"),
    [
        (0, "+0"),
        (5, "+5"),
        (999, "+999"),
        (1_000, "+1k"),  # threshold
        (1_500, "+1k"),  # FLOOR, not round — the catalog reads as magnitude
        (3_000, "+3k"),
        (999_999, "+999k"),
        (1_000_000, "+1M"),  # threshold
        (1_500_000, "+1.5M"),
        (2_000_000, "+2M"),  # a bare .0 tail is trimmed
    ],
)
def test_format_xp_short(xp: int, expected: str) -> None:
    assert format_xp_short(xp) == expected


@pytest.mark.parametrize(
    ("xp", "lang", "expected"),
    [
        (0, "ru", "0"),  # non-positive is a bare 0, never "+0k"
        (-5, "ru", "0"),
        (200, "ru", "200"),
        (999, "en", "999"),
        (1_000, "ru", "1k"),  # threshold, .0 tail trimmed
        (2_500, "ru", "2,5k"),  # RU decimal comma
        (2_500, "en", "2.5k"),  # EN decimal point
        (3_000, "en", "3k"),
        (1_000_000, "ru", "1000k"),  # no M step here — legacy stopped at k
    ],
)
def test_format_xp_spark(xp: int, lang: str, expected: str) -> None:
    assert format_xp_spark(xp, lang) == expected


# ---------------------------------------------------------------------------
# page_offset (#1984) — the one helper here that guards a query rather
# than a string.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("index", "size", "expected"),
    [
        (0, 10, 0),
        (1, 10, 10),
        (3, 25, 75),
        (-1, 10, 0),  # a page before the first one is the first one
        (-999, 10, 0),
        (5, 0, 5),  # a nonsense page size reads as 1, never a ZeroDivisionError
        (5, -3, 5),
    ],
)
def test_page_offset_ordinary_pages(index: int, size: int, expected: int) -> None:
    assert page_offset(index, size) == expected


@pytest.mark.parametrize("size", [1, 10, 25, 50])
def test_page_offset_never_leaves_what_sqlite_can_bind(size: int) -> None:
    """The whole point: the PRODUCT is what gets bound.

    ``DbInt`` (#1978) bounds the page number itself, which is why the
    ceiling below is reachable from a real callback payload. Multiplying
    it by a page size put the result back outside the range that bound
    exists to enforce.
    """
    assert page_offset(MAX_DB_INT, size) <= MAX_DB_INT
    assert page_offset(MAX_DB_INT // 2, size) <= MAX_DB_INT


def test_page_offset_clamps_late_enough_to_be_useless_as_a_page() -> None:
    """The clamp must not be reachable by paging.

    A ceiling low enough to hit by pressing "next" would silently pin a
    real user to a page they did not ask for. At ten rows a page the cap
    is ~9.2e17 pages, so the only way to reach it is to forge the
    payload — and a forged page has no rows either way.
    """
    assert page_offset(10**17, 10) == 10**18
    assert page_offset(MAX_DB_INT, 10) == (MAX_DB_INT // 10) * 10
