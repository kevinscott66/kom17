"""Unit tests for the private formatters in ``handlers.shop``.

The e2e suite covers the happy paths via the dispatcher; what's left
uncovered are the *negative* branches of two pure render functions
that the e2e seam can't reach without contorting the service:

* ``_format_stock_hint(0)`` — the "нет в наличии" suffix. Items at
  stock=0 are filtered at the repo (legacy parity), so a regular
  ``/shop`` test will never see the hint rendered. But the function
  still has to exist and stay correct, because ``ShopItemsRepo`` has
  an ``include_out_of_stock=True`` path used by admin tooling.
* ``_format_purchase_success`` with ``new_balance=None`` and
  ``new_stock=None`` (or negative) — the service always populates them
  on real OK outcomes, but the dataclass defaults to ``None`` and a
  future code path that returns an OK without re-reading state
  (e.g. an optimistic concurrent confirmation) would land here. The
  formatter must render the truncated reply without a TypeError.

Locking these as unit tests keeps the e2e suite focused on the
service+handler contract and the formatter branches independently
covered.
"""

from __future__ import annotations

from telegram_invite_bot.core.entities.shop import (
    PurchaseOutcome,
    PurchaseStatus,
    ShopItemEntity,
)
from telegram_invite_bot.handlers.shop import (
    _format_purchase_success,
    _format_stock_hint,
)

# ── _format_stock_hint ───────────────────────────────────────────────────


def test_format_stock_hint_zero_renders_out_of_stock_suffix() -> None:
    """stock=0 → "нет в наличии". This branch is only reachable via
    the admin ``include_out_of_stock=True`` repo path; the default
    ``/shop`` render hides these rows.
    """
    assert _format_stock_hint(0, "ru") == " <i>(нет в наличии)</i>"


def test_format_stock_hint_positive_renders_remaining() -> None:
    assert _format_stock_hint(7, "ru") == " <i>(осталось: 7)</i>"


def test_format_stock_hint_negative_renders_nothing() -> None:
    """stock=-1 is the "infinite" sentinel (legacy parity). Any
    negative value collapses to "no hint" so a future bug that lets
    -2 leak in still degrades gracefully rather than rendering
    "(осталось: -2)".
    """
    assert _format_stock_hint(-1, "ru") == ""
    assert _format_stock_hint(-99, "ru") == ""


# ── _format_purchase_success ─────────────────────────────────────────────


def _item(*, price: int = 100, name: str = "Plushie") -> ShopItemEntity:
    return ShopItemEntity(id=1, name=name, description="", price=price, type="unwarn", stock=-1)


def test_format_purchase_success_full_outcome_renders_balance_and_stock() -> None:
    """Baseline: when the service reports both new_balance and a
    finite new_stock, the reply lists each on its own line. Locks the
    happy path so the more interesting None-branch tests below don't
    accidentally pass by *also* omitting required text.
    """
    outcome = PurchaseOutcome(
        status=PurchaseStatus.OK,
        item=_item(),
        new_balance=400,
        new_stock=3,
    )
    body = _format_purchase_success(outcome, "ru")
    assert "Куплено" in body
    assert "Остаток: <b>400</b>" in body
    assert "Осталось на складе: <i>3</i>" in body


def test_format_purchase_success_skips_balance_when_none() -> None:
    """``new_balance=None`` means the service didn't compute the
    post-debit balance for us (defensive default in
    :class:`PurchaseOutcome`). The reply must drop the line entirely
    rather than render "Остаток: <b>None</b>", which would leak
    Python repr into a user-facing message.
    """
    outcome = PurchaseOutcome(
        status=PurchaseStatus.OK,
        item=_item(),
        new_balance=None,
        new_stock=3,
    )
    body = _format_purchase_success(outcome, "ru")
    assert "Остаток" not in body
    assert "Куплено" in body
    assert "Осталось на складе: <i>3</i>" in body


def test_format_purchase_success_skips_stock_when_negative() -> None:
    """The ``-1`` sentinel marks an infinite-stock item (legacy
    parity). We must NOT render "Осталось на складе: -1" — that would
    expose the internal sentinel to users. The reply also has to
    handle ``new_stock=None`` the same way (no line).
    """
    body_neg = _format_purchase_success(
        PurchaseOutcome(
            status=PurchaseStatus.OK,
            item=_item(),
            new_balance=400,
            new_stock=-1,
        ),
        "ru",
    )
    body_none = _format_purchase_success(
        PurchaseOutcome(
            status=PurchaseStatus.OK,
            item=_item(),
            new_balance=400,
            new_stock=None,
        ),
        "ru",
    )
    for body in (body_neg, body_none):
        assert "Осталось на складе" not in body
        assert "Остаток: <b>400</b>" in body
