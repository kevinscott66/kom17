"""Unit tests for the P2P sell-interview limits line (``методы; мин; макс``).

Both bounds are typed by the seller as free text and parsed with
``int(float(x))``, which has two escape hatches a reader does not expect
from a helper documented as "silently drop malformed numbers":

* ``float("1e400")`` is ``inf``, and ``int(inf)`` raises **OverflowError**
  — not ValueError. The ``contextlib.suppress(ValueError)`` around the
  parse therefore does not catch it, and a seller who typed one long
  number got an unhandled error instead of an order.
* ``float("1e30")`` is *finite*, so it parses cleanly into a Python int
  of 10^30 — past every guard, all the way to the order INSERT, where
  sqlite3 refuses to bind anything wider than 64 bits.

The two live at different layers on purpose: the first is a parse
failure (the token is not a number we can represent), the second is a
number we can represent but must not store. This file pins the parse
half; the store half is pinned in
``tests/integration/services/test_p2p_service.py``.
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.handlers.p2p import _parse_limits
from telegram_invite_bot.services.p2p_service import limits_are_sane
from telegram_invite_bot.utils.economy import _MAX_AMOUNT


@pytest.mark.parametrize("token", ["1e400", "inf", "-inf", "INF", "Infinity"])
def test_non_finite_bound_is_dropped_not_raised(token: str) -> None:
    """A token that floats to infinity lands in the malformed bucket.

    ``suppress(ValueError)`` reads like it covers "anything unparseable",
    and for every token a human is likely to type it does. Infinity is
    the one that slips: it survives ``float()`` and dies in ``int()``
    with a *different* exception, so the failure surfaced as a crash in
    the middle of the sell interview rather than as an ignored bound.

    Asserted in both positions because they are separate ``suppress``
    blocks — fixing one and not the other is the obvious half-fix.
    """
    methods, min_amount, max_amount = _parse_limits(f"карта; {token}; 5000")
    assert (methods, min_amount, max_amount) == ("карта", None, 5000)

    methods, min_amount, max_amount = _parse_limits(f"карта; 100; {token}")
    assert (methods, min_amount, max_amount) == ("карта", 100, None)


def test_ordinary_malformed_bounds_still_parse_the_same_way() -> None:
    """The non-finite fix must not widen what else gets swallowed.

    ``nan`` and plain words were already dropped by the ValueError arm;
    they are re-asserted here so a future "just catch Exception" keeps
    failing review — the point of the parse is that a *number* the book
    cannot use is dropped, not that every error is.
    """
    assert _parse_limits("карта; abc; 5000") == ("карта", None, 5000)
    assert _parse_limits("карта; nan; 5000") == ("карта", None, 5000)
    assert _parse_limits("карта; 100.9; 5000.2") == ("карта", 100, 5000)
    assert _parse_limits("карта") == ("карта", None, None)


def test_limits_above_the_storable_ceiling_are_rejected() -> None:
    """A finite but absurd bound must be refused, not stored.

    ``1e30`` is the shape that reached the database: it passes
    ``float()``, passes ``int()``, is positive, and is ordered correctly
    against its partner — every check the pair had. The seller's escrow
    was already debited by then, so the bind error rolled back a
    half-built order and showed a generic failure.

    The ceiling is :data:`_MAX_AMOUNT` (the documented balance cap)
    rather than the driver's ``2**63``: nothing above it can ever be
    filled, so refusing it costs the seller nothing real and keeps one
    number as *the* ceiling across the wallet and the book.
    """
    assert limits_are_sane(_MAX_AMOUNT, None) is True
    assert limits_are_sane(None, _MAX_AMOUNT) is True

    assert limits_are_sane(_MAX_AMOUNT + 1, None) is False
    assert limits_are_sane(None, _MAX_AMOUNT + 1) is False
    assert limits_are_sane(10**30, 10**31) is False
    assert limits_are_sane(2**63, None) is False
