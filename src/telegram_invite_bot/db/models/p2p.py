"""ORM mappings for the P2P COM marketplace (#64, DESIGN_P2P.md §2.1).

Three tables on the ``economy`` metadata, kept in THIS module (not the
shared ``db/models/economy.py``) so the P2P feature never forces an
edit to the file both pipelines' economy models share — same posture as
:mod:`telegram_invite_bot.db.models.promo`.

``p2p_sell_orders`` / ``p2p_trades`` exist on prod in the legacy shape
(``bot.py:5175-5218``, ``docs/prod_schemas.sql:521``) with dead fields
the design explicitly does NOT port (``expires_at``,
``completed_count``, ``total_sold`` on orders; ``payment_method``,
``payment_details``, ``dispute_reason``, ``escrow_release_tx`` on
trades). Migration ``0010_p2p`` rebuilds both tables into this exact
shape (live columns copied over, ``dispute_resolved_by/At`` renamed to
``resolved_by/At``); ``create_all`` builds them for tests directly.

``p2p_seller_stats`` is NET-NEW: legacy kept the three live reputation
counters inside ``user_withdrawal_limits`` (bot.py:5220-5234) together
with withdrawal-limit plumbing the new pipeline models differently
(``WithdrawService`` derives usage from request timestamps, not stored
counters). A tiny self-contained stats table avoids touching the
withdraw domain. The legacy ``rating`` column is a dead always-5.0
field and is NOT ported (deviation D3).

Money semantics (DESIGN_P2P.md §2.2):

* **Escrow-on-create** — the seller's wallet is debited when the order
  is created; the escrow IS ``remaining_com``. Cancel returns
  ``remaining_com``; every fill carves a slice out of it into a trade.
* The race-safe fill is a guarded ``UPDATE ... WHERE status='active'
  AND remaining_com >= :take`` (:meth:`P2pRepo.fill`) — schema-level
  there is nothing to enforce, the guard IS the invariant.
* Trade transitions are status-guarded UPDATEs, never read-then-write.

Status vocabularies (TEXT, not enums — matches the legacy writer and
keeps prod rows round-tripping):

* order: ``active`` / ``completed`` / ``cancelled``.
* trade: ``pending`` → ``paid`` → ``confirmed``; ``disputed`` (from
  pending|paid) → ``dispute_refund_buyer`` | ``confirmed`` |
  ``dispute_returned_seller`` (D1); ``cancelled_timeout`` (D2 expiry,
  from pending only).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from telegram_invite_bot.db.models.base import EconomyBase


class P2pSellOrder(EconomyBase):
    """One sell order in ``economy.p2p_sell_orders``.

    ``amount_com`` is the original escrowed size (display only after
    creation); ``remaining_com`` is the live escrow — what cancel
    refunds and what fills decrement. ``price_per_com`` is the fixed
    market rate from :data:`telegram_invite_bot.core.p2p.P2P_COM_RATES`
    at creation time (legacy is market-price-only).

    ``payment_methods`` / ``min_amount`` / ``max_amount`` are optional
    free-text/fiat-bound hints rendered on the order card; they are NOT
    enforced by the money paths (legacy parity — bot.py never enforced
    them either).
    """

    __tablename__ = "p2p_sell_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_com: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_com: Mapped[int] = mapped_column(Integer, nullable=False)
    price_per_com: Mapped[float] = mapped_column(Float, nullable=False)
    fiat_currency: Mapped[str] = mapped_column(String, nullable=False)
    payment_methods: Mapped[str | None] = mapped_column(Text, nullable=True)
    min_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_p2p_sell_orders_user", "user_id"),
        # Composite index backing the order book's hot read:
        # WHERE status='active' AND fiat_currency=? ORDER BY price_per_com.
        Index(
            "idx_p2p_sell_orders_book",
            "status",
            "fiat_currency",
            "price_per_com",
        ),
    )


class P2pTrade(EconomyBase):
    """One trade (a fill against an order) in ``economy.p2p_trades``.

    ``amount_com`` is the escrow slice carved out of the order's
    ``remaining_com`` at creation; exactly ONE of these eventually
    happens to it: released to the buyer (``p2p_release`` on confirm /
    dispute-release), returned to the seller (``p2p_refund``, D1), or
    returned to the order's ``remaining_com`` (D2 expiry).

    ``resolved_by`` / ``resolved_at`` are the dispute-resolution stamps
    (legacy ``dispute_resolved_by`` / ``dispute_resolved_at``, renamed —
    the D2 expiry doesn't use them, only admin resolutions do).
    """

    __tablename__ = "p2p_trades"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    seller_id: Mapped[int] = mapped_column(Integer, nullable=False)
    buyer_id: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_com: Mapped[int] = mapped_column(Integer, nullable=False)
    price_per_com: Mapped[float] = mapped_column(Float, nullable=False)
    total_fiat: Mapped[float] = mapped_column(Float, nullable=False)
    fiat_currency: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    created_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_p2p_trades_order", "order_id"),
        Index("idx_p2p_trades_buyer", "buyer_id"),
        Index("idx_p2p_trades_seller", "seller_id"),
        # Backs the D2 expiry scan (status='pending' AND created_at < cutoff)
        # and any "all disputed" admin view.
        Index("idx_p2p_trades_status", "status"),
    )


class P2pSellerStats(EconomyBase):
    """Per-seller reputation counters in ``economy.p2p_seller_stats``.

    The three LIVE counters legacy kept in ``user_withdrawal_limits``
    (successful_trades / total_withdrawn_com → ``total_sold_com`` /
    dispute_count). Upserted with in-SQL increments
    (:meth:`P2pRepo.record_successful_trade` /
    :meth:`P2pRepo.record_dispute`) so concurrent confirms both count.
    Absence of a row == all zeros (the repo's ``get_stats`` defaults).

    The dead ``rating`` always-5.0 column is NOT ported (D3) — cards
    render the real counters instead of the fake score.
    """

    __tablename__ = "p2p_seller_stats"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    successful_trades: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_sold_com: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dispute_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
