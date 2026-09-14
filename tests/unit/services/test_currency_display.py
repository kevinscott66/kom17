"""Display-currency resolution + amount rendering — RR-6 #66/#67.

Pure functions, so they get pure tests: no DB, no dispatcher, no FX
call. What they pin is legacy parity — ``format_amount`` (bot.py:3245)
and ``get_user_currency`` (bot.py:3380) are the originals, and the split
port either dropped them or paraphrased them into something that reads
differently on a user's screen.
"""

from __future__ import annotations

import re

import pytest

from telegram_invite_bot.services.currency_service import (
    AVAILABLE_CURRENCIES,
    DEFAULT_CURRENCY,
    DEFAULT_CURRENCY_EN,
    _format_converted,
    currency_label,
    effective_currency,
    format_display_amount,
)

# ── effective_currency ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("stored", "lang", "expected"),
    [
        (None, "ru", "RUB"),
        ("", "ru", "RUB"),
        ("   ", "ru", "RUB"),
        ("ton", "ru", "TON"),  # case/whitespace normalised, like legacy
        (" Ton ", "ru", "TON"),
        ("ZZZ", "ru", "RUB"),  # unknown → default, never echoed back
        (None, "en", "USD"),
        ("TON", "en", "TON"),  # a real choice survives the EN fallback
    ],
)
def test_effective_currency(stored: str | None, lang: str, expected: str) -> None:
    assert effective_currency(stored, lang) == expected


def test_english_user_with_stored_rub_falls_back_to_usd() -> None:
    """The legacy quirk, ported verbatim and on purpose.

    ``display_currency`` has a server default of the literal ``'RUB'``,
    so "chose RUB" and "never chose" are the same bytes. Legacy resolved
    that ambiguity in favour of USD for English users. The LIVE telebot
    still reads this column, so diverging here would quote one user two
    different currencies depending on which bot answered.
    """
    assert effective_currency("RUB", "en") == DEFAULT_CURRENCY_EN
    assert effective_currency("RUB", "ru") == DEFAULT_CURRENCY


# ── currency_label ───────────────────────────────────────────────────


def test_currency_label_is_emoji_name_code() -> None:
    assert currency_label("USD", "en") == "🇺🇸 US Dollar (USD)"
    assert currency_label("USD", "ru").endswith("(USD)")
    assert "Доллар" in currency_label("USD", "ru")


def test_currency_label_of_an_unknown_code_degrades_to_the_code() -> None:
    assert currency_label("XXX", "ru") == "XXX"


# ── _format_converted ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "decimals", "expected"),
    [
        (0.0, 2, "0"),
        (1.5, 2, "1.5"),  # trailing zeros trimmed, as legacy did
        (1.0, 2, "1"),
        (0.11, 2, "0.11"),
        (999.99, 2, "999.99"),
        (1_000.0, 2, "1.00K"),
        (1_500.0, 0, "1.50K"),  # the K step ignores per-currency decimals
        (999_999.0, 2, "1000.00K"),
        (1_000_000.0, 2, "1.00M"),
        (2_500_000.0, 0, "2.50M"),
    ],
)
def test_format_converted(value: float, decimals: int, expected: str) -> None:
    assert _format_converted(value, decimals) == expected


def test_sub_thousand_values_keep_their_decimals() -> None:
    """Regression guard: the compact formatter truncates below 1000.

    Reusing ``format_amount_compact`` for the converted half would print
    ``$0`` where the user is owed ``$0.11`` — which is why this branch
    exists at all.
    """
    assert _format_converted(0.11, 2) == "0.11"


# ── format_display_amount ────────────────────────────────────────────


def test_com_stays_a_single_coin_figure() -> None:
    """No ``(~…)`` tail when the display currency IS the internal coin."""
    assert format_display_amount(1500, "COM", 1.0) == "1.50K 🪙"


def test_symbol_placement_follows_the_currency() -> None:
    # USD prefixes, RUB suffixes — straight from legacy's per-currency
    # ``format`` templates, which the first port dropped entirely.
    assert format_display_amount(1000, "USD", 0.001) == "1.00K 🪙 (~$1)"
    assert format_display_amount(1000, "RUB", 0.1) == "1.00K 🪙 (~100 ₽)"
    assert format_display_amount(1000, "EUR", 0.0009) == "1.00K 🪙 (~0.9€)"


def test_include_com_false_drops_the_coin_half() -> None:
    assert format_display_amount(1000, "USD", 0.001, include_com=False) == "$1"


def test_large_amounts_abbreviate_on_both_halves() -> None:
    rendered = format_display_amount(10_000_000, "USD", 0.001)
    assert rendered.startswith("10.00M 🪙")
    assert "10.00K" in rendered


def test_ton_keeps_fine_precision() -> None:
    """A TON amount rounded to two places is usually just ``0``."""
    rendered = format_display_amount(1000, "TON", 0.0000012)
    assert "0.0012" in rendered


def test_unknown_code_degrades_to_the_default_currency() -> None:
    """Defensive: a junk cell renders rubles, never a KeyError."""
    assert format_display_amount(100, "XXX", 0.1) == format_display_amount(
        100, DEFAULT_CURRENCY, 0.1
    )


def test_no_symbol_can_break_out_of_html() -> None:
    """Every rendered string lands in a ``parse_mode=HTML`` message.

    The ``format`` templates are project-owned constants, so ``str.format``
    can't be attacker-driven — but a currency symbol containing ``<`` or
    ``&`` would still corrupt the card for everyone.
    """
    for code, meta in AVAILABLE_CURRENCIES.items():
        symbol = str(meta.get("symbol", ""))
        assert "<" not in symbol and "&" not in symbol, code
        # ...and no template reaches for anything but the two values the
        # renderer passes, so ``str.format`` has no attribute to walk.
        template = str(meta.get("format", "{amount}"))
        assert set(re.findall(r"{([^}]*)}", template)) <= {"amount", "symbol"}, code
