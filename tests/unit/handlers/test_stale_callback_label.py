"""The stale-callback metric label must stay bounded (#159).

``handlers/stale_callback`` counts every unmatched tap. The payload it
counts arrives from outside the process, so the one thing that can go
wrong here is not "wrong label" but "unbounded label": Prometheus keeps
a distinct time series per label value forever, and a counter fed raw
``callback_data`` would grow one series per ``shop_buy:1``,
``shop_buy:2``, … — a leak in the bot and a much more expensive one in
whatever scrapes it.

So the whole contract is: the label is a prefix the tree actually
registers, or one of two fixed sentinels. Nothing else, ever.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.handlers.stale_callback import (
    NO_DATA_LABEL,
    UNKNOWN_LABEL,
    prefix_label,
)

KNOWN = frozenset({"shop_buy", "menu", "p2p_order"})


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        # The ordinary case: a known prefix, fields stripped off.
        ("shop_buy:42:group", "shop_buy"),
        # No fields at all — still the prefix.
        ("menu", "menu"),
        # Trailing separator, empty field: the head is what matters.
        ("menu:", "menu"),
        # A prefix nobody registers. The point of the whole function.
        ("shop_buy_v2:42", UNKNOWN_LABEL),
        ("", UNKNOWN_LABEL),
        (None, NO_DATA_LABEL),
    ],
)
def test_label_is_the_prefix_or_a_sentinel(data: str | None, expected: str) -> None:
    assert prefix_label(data, KNOWN) == expected


@pytest.mark.parametrize(
    "hostile",
    [
        # An id per tap — the shape that turns a counter into a leak.
        "trade_9f2c1b0e-7a41-4d3b-9c8e-0f5a2d7b6c31",
        # Someone's idea of a label injection.
        'prefix="x",y="z"',
        # A payload as long as Telegram allows (64 bytes).
        "z" * 64,
        # Separator-only, and separator-first: the head is empty, which
        # must land on the sentinel rather than on an empty label.
        ":",
        ":menu",
        # Newlines and unicode — Prometheus label values accept them,
        # which is exactly why the allowlist has to be the gate.
        "menu\nshop_buy",
        "меню:1",
    ],
)
def test_nothing_unknown_ever_becomes_a_label(hostile: str) -> None:
    """Whatever arrives, the label is from a set of five values here."""
    assert prefix_label(hostile, KNOWN) in KNOWN | {UNKNOWN_LABEL, NO_DATA_LABEL}
    assert prefix_label(hostile, KNOWN) == UNKNOWN_LABEL


def test_an_empty_known_set_collapses_everything() -> None:
    """Degenerate but worth pinning: no prefixes → no labels but the two.

    If the derivation in ``callback_prefixes`` ever came back empty —
    an aiogram refactor that moves ``CallbackQueryFilter``, say — the
    metric goes blind rather than unbounded. Losing the breakdown is
    recoverable; losing the bound is not.
    """
    assert prefix_label("shop_buy:1", frozenset()) == UNKNOWN_LABEL
