"""Async repository for the P2P marketplace tables (#64).

Owns ``p2p_sell_orders`` / ``p2p_trades`` / ``p2p_seller_stats`` —
data-shape operations only. Money composition (escrow debits, release
credits, ledger rows, rollbacks) lives in
:class:`~telegram_invite_bot.services.p2p_service.P2pService`, which
runs this repo on the SAME shared economy session as ``EconomyRepo`` /
``TransactionsRepo`` so every flow commits atomically.

Race posture (DESIGN_P2P.md §2.2.2/§2.2.4 — the hardening over legacy):

* :meth:`fill` is the project-standard atomic guard:
  ``UPDATE ... SET remaining_com = remaining_com - :take WHERE id = :id
  AND status = 'active' AND remaining_com >= :take`` with a rowcount
  check. Two concurrent buyers cannot oversell one order — the second
  guard sees the post-first ``remaining_com`` and gets ``rowcount == 0``
  (fixes the legacy read-then-write oversell at bot.py:19632-19680).
* Every trade transition is a guarded ``UPDATE ... WHERE status =
  :expected`` (legacy checked status in Python and then wrote
  unconditionally, e.g. the dispute open at bot.py:20198).
* :meth:`cancel_guard` flips ``active → cancelled`` and reports the
  refundable ``remaining_com`` in one statement, so two cancel taps
  can't refund twice.

Stats counters are upserted with in-SQL increments (the
``credit``/``debit`` posture) so two concurrent confirms both count.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from sqlalchemy import false, func, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.db.models.p2p import P2pSellerStats, P2pSellOrder, P2pTrade

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.engine import CursorResult
    from sqlalchemy.ext.asyncio import AsyncSession


# Trade statuses (TEXT in the schema; centralised here so the service,
# the UI clusters and tests share one vocabulary).
TRADE_PENDING = "pending"
TRADE_PAID = "paid"
TRADE_CONFIRMED = "confirmed"
TRADE_DISPUTED = "disputed"
TRADE_DISPUTE_REFUND_BUYER = "dispute_refund_buyer"
TRADE_DISPUTE_RETURNED_SELLER = "dispute_returned_seller"  # deviation D1
TRADE_CANCELLED_TIMEOUT = "cancelled_timeout"  # deviation D2

# Order statuses.
ORDER_ACTIVE = "active"
ORDER_COMPLETED = "completed"
ORDER_CANCELLED = "cancelled"


class P2pRepo:
    """P2P tables access. Constructed per request with an open session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def create_order(
        self,
        *,
        user_id: int,
        amount_com: int,
        price_per_com: float,
        fiat_currency: str,
        payment_methods: str | None,
        min_amount: int | None,
        max_amount: int | None,
        now: datetime,
    ) -> P2pSellOrder:
        """Insert an ``active`` order with ``remaining_com = amount_com``.

        Mirrors the legacy insert at bot.py:19487-19491. The caller
        (service) has ALREADY debited the seller — the escrow invariant
        is "this row exists ⇔ the wallet paid for it", enforced by both
        landing in one transaction.
        """
        row = P2pSellOrder(
            user_id=user_id,
            amount_com=amount_com,
            remaining_com=amount_com,
            price_per_com=price_per_com,
            fiat_currency=fiat_currency,
            payment_methods=payment_methods,
            min_amount=min_amount,
            max_amount=max_amount,
            status=ORDER_ACTIVE,
            created_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_order(self, order_id: int) -> P2pSellOrder | None:
        return await self._session.get(P2pSellOrder, order_id)

    async def list_active(
        self,
        *,
        currency: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[P2pSellOrder]:
        """Order book page: active orders, cheapest first.

        Mirrors the legacy book read (bot.py:19759-19770 / 20103-20114)
        — ``status='active'`` + optional currency filter, ``ORDER BY
        price_per_com ASC``. Adds ``remaining_com > 0`` (paranoia: a
        drained order should already be ``completed``) and an ``id``
        tiebreaker so equal-priced orders page deterministically
        (legacy's book could flicker on ties). ``limit``/``offset``
        give the UI clusters real paging instead of legacy's LIMIT 20.
        """
        clamped = max(1, min(limit, 50))
        stmt = (
            select(P2pSellOrder)
            .where(P2pSellOrder.status == ORDER_ACTIVE, P2pSellOrder.remaining_com > 0)
            .order_by(P2pSellOrder.price_per_com.asc(), P2pSellOrder.id.asc())
            .limit(clamped)
            .offset(max(offset, 0))
        )
        if currency is not None:
            stmt = stmt.where(P2pSellOrder.fiat_currency == currency)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def my_orders(self, user_id: int, *, limit: int = 15) -> list[P2pSellOrder]:
        """A user's own orders, newest first (legacy bot.py:19534-19537)."""
        stmt = (
            select(P2pSellOrder)
            .where(P2pSellOrder.user_id == user_id)
            .order_by(P2pSellOrder.id.desc())
            .limit(max(1, min(limit, 50)))
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_active_orders(self, user_id: int) -> int:
        """How many of this seller's orders are still on the book.

        Counted in SQL for the same reason as
        :meth:`count_open_as_buyer`: the menu card used to derive this by
        filtering :meth:`my_orders`, whose ``limit=15`` is a *display*
        cap. A seller with fifteen newer cancelled or completed rows saw
        «0 активных ордеров» while their orders were live and selling
        (#720) — legacy counted unbounded in SQL (bot.py:19231-19234) and
        never had the bug.

        ``remaining_com > 0`` matches :meth:`list_active`, so the number
        on the card is the number of rows a buyer can actually find.
        """
        stmt = (
            select(func.count())
            .select_from(P2pSellOrder)
            .where(
                P2pSellOrder.user_id == user_id,
                P2pSellOrder.status == ORDER_ACTIVE,
                P2pSellOrder.remaining_com > 0,
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def lock_writer(self) -> None:
        """Take this database's writer lock now, before the buy-side reads.

        #1503, and the same mechanism ``WithdrawalsRepo.lock_writer``
        uses for #776. SQLite serialises writers, not readers, and this
        project opens transactions lazily: ``db/engines.py:204-210``
        issues ``BEGIN IMMEDIATE`` only for a *write*-headed statement,
        and ``"select"`` is not one. So the buy-side prologue — the
        order re-fetch, the seller's bounds, and above all the open-trade
        count behind :data:`MAX_OPEN_TRADES_PER_BUYER` — ran outside any
        transaction, and two taps could both count the same trades before
        either inserted. :meth:`fill` guards the *order* against
        overselling, but nothing guarded the *buyer* against holding
        one more open trade than the cap allows.

        This UPDATE matches nothing (``WHERE false``); its ``UPDATE``
        head is what the engines hook keys on, so the connection enters
        ``BEGIN IMMEDIATE`` and a second caller blocks on
        ``PRAGMA busy_timeout`` (5 000 ms, ``db/pragma.py:63``) until the
        first commits, then counts *committed* trades. Writing no rows
        is the point: the lock has to be acquirable before we know
        whether the buy will be granted, and a refusal must release it
        having changed nothing.
        """
        await self._session.execute(
            update(P2pTrade)
            .where(false())
            .values(status=P2pTrade.status)
            .execution_options(synchronize_session=False)
        )

    async def fill(self, order_id: int, take: int) -> bool:
        """Atomically carve ``take`` COM out of an active order's escrow.

        THE race guard (§2.2.2)::

            UPDATE p2p_sell_orders
               SET remaining_com = remaining_com - :take
             WHERE id = :id AND status = 'active' AND remaining_com >= :take

        ``rowcount == 0`` (False) means the order drained, completed or
        was cancelled between the caller's read and this write — the
        caller MUST abort its trade. ``take`` is trusted positive (the
        service validates bounds first).
        """
        stmt = (
            update(P2pSellOrder)
            .where(
                P2pSellOrder.id == order_id,
                P2pSellOrder.status == ORDER_ACTIVE,
                P2pSellOrder.remaining_com >= take,
            )
            .values(remaining_com=P2pSellOrder.remaining_com - take)
        )
        result = cast("CursorResult[object]", await self._session.execute(stmt))
        return result.rowcount > 0

    async def complete_if_drained(self, order_id: int) -> bool:
        """Flip ``active → completed`` iff the escrow hit zero.

        Mirrors legacy's "new_rem == 0 → completed" (bot.py:19935) but
        as a guarded UPDATE so it composes safely after :meth:`fill`
        under concurrency. True iff this call did the flip.
        """
        stmt = (
            update(P2pSellOrder)
            .where(
                P2pSellOrder.id == order_id,
                P2pSellOrder.status == ORDER_ACTIVE,
                P2pSellOrder.remaining_com == 0,
            )
            .values(status=ORDER_COMPLETED)
        )
        result = cast("CursorResult[object]", await self._session.execute(stmt))
        return result.rowcount > 0

    async def cancel_guard(self, order_id: int, user_id: int) -> int | None:
        """Cancel an active order; return the refundable ``remaining_com``.

        Guard mirrors legacy's checks (owner bot.py:19577, active &
        remaining > 0 bot.py:19580) but in ONE statement, so a double
        tap / concurrent fill can't double-refund: the loser sees no
        row. ``RETURNING remaining_com`` reads the PRE-cancel escrow
        (the column is not in SET), and a second statement zeroes it —
        same transaction, matching legacy's ``remaining_com = 0`` write
        (bot.py:19584). ``None`` == guard rejected (not found / not
        yours / not active / drained).
        """
        stmt = (
            update(P2pSellOrder)
            .where(
                P2pSellOrder.id == order_id,
                P2pSellOrder.user_id == user_id,
                P2pSellOrder.status == ORDER_ACTIVE,
                P2pSellOrder.remaining_com > 0,
            )
            .values(status=ORDER_CANCELLED)
            .returning(P2pSellOrder.remaining_com)
        )
        result = await self._session.execute(stmt)
        remaining = result.scalar_one_or_none()
        if remaining is None:
            return None
        await self._session.execute(
            update(P2pSellOrder).where(P2pSellOrder.id == order_id).values(remaining_com=0)
        )
        return int(remaining)

    async def return_slice(self, order_id: int, amount: int) -> bool:
        """Return an expired trade's slice to the order's escrow (D2).

        ``remaining_com += amount`` and the order goes (back) to
        ``active`` — covering both the partial-fill case (still active)
        and the "this fill completed the order" case (completed →
        active again, per the design's "order back to active if it was
        completed by that fill"). A ``cancelled`` order is deliberately
        NOT matched — its escrow was already refunded to the seller, so
        the service refunds the slice to the seller's wallet instead.
        """
        stmt = (
            update(P2pSellOrder)
            .where(
                P2pSellOrder.id == order_id,
                P2pSellOrder.status.in_((ORDER_ACTIVE, ORDER_COMPLETED)),
            )
            .values(
                remaining_com=P2pSellOrder.remaining_com + amount,
                status=ORDER_ACTIVE,
            )
        )
        result = cast("CursorResult[object]", await self._session.execute(stmt))
        return result.rowcount > 0

    # ------------------------------------------------------------------
    # Trades
    # ------------------------------------------------------------------

    async def create_trade(
        self,
        *,
        order_id: int,
        seller_id: int,
        buyer_id: int,
        amount_com: int,
        price_per_com: float,
        total_fiat: float,
        fiat_currency: str,
        now: datetime,
    ) -> P2pTrade:
        """Insert a ``pending`` trade (legacy bot.py:19929-19932).

        The caller has ALREADY won the :meth:`fill` guard for
        ``amount_com`` — the slice this row represents is reserved.
        """
        row = P2pTrade(
            order_id=order_id,
            seller_id=seller_id,
            buyer_id=buyer_id,
            amount_com=amount_com,
            price_per_com=price_per_com,
            total_fiat=total_fiat,
            fiat_currency=fiat_currency,
            status=TRADE_PENDING,
            created_at=now,
        )
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_trade(self, trade_id: int) -> P2pTrade | None:
        return await self._session.get(P2pTrade, trade_id)

    async def my_trades(self, user_id: int, *, limit: int = 20) -> list[P2pTrade]:
        """Trades where the user sits on either seat, newest first
        (legacy bot.py:20074-20076)."""
        stmt = (
            select(P2pTrade)
            .where(or_(P2pTrade.seller_id == user_id, P2pTrade.buyer_id == user_id))
            .order_by(P2pTrade.id.desc())
            .limit(max(1, min(limit, 50)))
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def list_disputed(self, *, limit: int = 20) -> list[P2pTrade]:
        """Every trade awaiting an admin ruling, oldest first.

        #1687. The delivery-time admin card (``handle_dispute_open`` in
        ``handlers/p2p_trade.py``) fires exactly once and best-effort:
        it is skipped outright when ``ADMIN_CHAT_ID`` is unset, and a
        send that fails is only logged. Nothing re-issued it, so a lost
        card stranded the escrow — :meth:`expire_pending_guard` wants
        ``pending`` and will not touch ``disputed``, and since #1685 the
        frozen slot also stops the buyer opening another trade.

        Oldest first, the opposite of :meth:`my_trades`: that one is a
        history, this one is a work queue, and the escrow frozen
        longest is the one to rule on next. Bounded like every other
        listing here; :meth:`count_disputed` reports the untruncated
        backlog beside it.
        """
        stmt = (
            select(P2pTrade)
            .where(P2pTrade.status == TRADE_DISPUTED)
            .order_by(P2pTrade.id.asc())
            .limit(max(1, min(limit, 50)))
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_disputed(self) -> int:
        """Size of the whole dispute backlog, ignoring any page bound.

        Counted in SQL rather than by measuring :meth:`list_disputed`,
        whose ``limit`` would report the page size as the total — the
        same silently-partial signal the console exists to remove.
        """
        stmt = select(func.count()).select_from(P2pTrade).where(P2pTrade.status == TRADE_DISPUTED)
        return int((await self._session.execute(stmt)).scalar_one())

    async def count_open_as_buyer(self, buyer_id: int) -> int:
        """How many trades this buyer currently holds open.

        "Open" is every status that still holds the seller's COM in
        escrow: ``pending``, ``paid`` and ``disputed``. Only the four
        terminal statuses release it, so only they free a slot.

        #1685. ``disputed`` used to be missing here, which turned the
        D4 cap into a formality: :meth:`mark_disputed` moves a trade
        out of ``pending``/``paid`` on the buyer's own say-so, at no
        cost and with no gate, while the escrow stays frozen and
        :meth:`expire_pending_guard` can no longer reap it. Buying and
        immediately disputing therefore refilled the slot forever, and
        the exact attack D4 exists to stop — one account freezing the
        whole book — was three taps away.

        Counted in SQL rather than by measuring :meth:`my_trades`,
        whose ``limit`` would silently cap the answer at exactly the
        number a cap check must not trust.
        """
        stmt = (
            select(func.count())
            .select_from(P2pTrade)
            .where(
                P2pTrade.buyer_id == buyer_id,
                P2pTrade.status.in_((TRADE_PENDING, TRADE_PAID, TRADE_DISPUTED)),
            )
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def mark_paid(self, trade_id: int, buyer_id: int, now: datetime) -> bool:
        """Guarded ``pending → paid`` by the trade's buyer.

        Legacy checked buyer ownership in the SELECT and status in
        Python (bot.py:19979-19990); here both ride the UPDATE's WHERE
        so a re-tap / concurrent expiry can't double-transition.
        """
        stmt = (
            update(P2pTrade)
            .where(
                P2pTrade.id == trade_id,
                P2pTrade.buyer_id == buyer_id,
                P2pTrade.status == TRADE_PENDING,
            )
            .values(status=TRADE_PAID, paid_at=now)
        )
        result = cast("CursorResult[object]", await self._session.execute(stmt))
        return result.rowcount > 0

    async def confirm_from_paid(self, trade_id: int, seller_id: int, now: datetime) -> bool:
        """Guarded ``paid → confirmed`` by the trade's seller.

        The double-release guard: a second confirm tap finds
        ``status != 'paid'`` and returns False, so the service never
        credits the buyer twice (legacy's Python check at bot.py:20039
        had a TOCTOU window).
        """
        stmt = (
            update(P2pTrade)
            .where(
                P2pTrade.id == trade_id,
                P2pTrade.seller_id == seller_id,
                P2pTrade.status == TRADE_PAID,
            )
            .values(status=TRADE_CONFIRMED, confirmed_at=now)
        )
        result = cast("CursorResult[object]", await self._session.execute(stmt))
        return result.rowcount > 0

    async def mark_disputed(self, trade_id: int) -> bool:
        """Guarded ``pending|paid → disputed``.

        Legacy wrote ``disputed`` unconditionally (bot.py:20198) — even
        onto confirmed/resolved trades. The status guard is §2.2.4
        hardening; participant authorisation lives in the service (it
        needs the trade row anyway for the admin notification payload).
        """
        stmt = (
            update(P2pTrade)
            .where(
                P2pTrade.id == trade_id,
                P2pTrade.status.in_((TRADE_PENDING, TRADE_PAID)),
            )
            .values(status=TRADE_DISPUTED)
        )
        result = cast("CursorResult[object]", await self._session.execute(stmt))
        return result.rowcount > 0

    async def resolve_disputed(
        self,
        trade_id: int,
        *,
        new_status: str,
        resolved_by: int,
        now: datetime,
        set_confirmed_at: bool = False,
    ) -> bool:
        """Guarded ``disputed → <resolution>`` with the resolver stamps.

        ``set_confirmed_at`` is True only for the confirm-seller outcome
        (legacy stamps ``confirmed_at`` there, bot.py:20290). The
        ``WHERE status='disputed'`` guard means two admins racing the
        same dispute produce exactly one resolution.
        """
        values: dict[str, object] = {
            "status": new_status,
            "resolved_by": resolved_by,
            "resolved_at": now,
        }
        if set_confirmed_at:
            values["confirmed_at"] = now
        stmt = (
            update(P2pTrade)
            .where(P2pTrade.id == trade_id, P2pTrade.status == TRADE_DISPUTED)
            .values(**values)
        )
        result = cast("CursorResult[object]", await self._session.execute(stmt))
        return result.rowcount > 0

    async def expire_pending_guard(self, trade_id: int, cutoff: datetime) -> bool:
        """Guarded ``pending → cancelled_timeout`` for a stale trade (D2).

        The ``created_at < cutoff`` re-check rides the UPDATE so a
        concurrent ``mark_paid`` (buyer clicking at the deadline) wins
        cleanly: whichever statement runs first flips the status and
        the loser's guard fails. ``paid`` trades never expire.
        """
        stmt = (
            update(P2pTrade)
            .where(
                P2pTrade.id == trade_id,
                P2pTrade.status == TRADE_PENDING,
                P2pTrade.created_at < cutoff,
            )
            .values(status=TRADE_CANCELLED_TIMEOUT)
        )
        result = cast("CursorResult[object]", await self._session.execute(stmt))
        return result.rowcount > 0

    async def list_pending_older_than(
        self, cutoff: datetime, *, limit: int, order_id: int | None = None
    ) -> list[P2pTrade]:
        """Pending trades created before ``cutoff`` — the D2 expiry scan.

        ``order_id`` narrows the scan for the lazy on-access path (the
        service expires only the trades blocking the order being
        opened); the 60-second money sweep passes ``None`` for the full
        pass. (#1620: this line used to say "hourly". #268 split the
        cadences, and the sweep has run once a minute ever since —
        ``economy_cleanup._MONEY_TICK_SECONDS``. A per-row cost budgeted
        for 24 passes a day is paid 1440 times.)

        ``limit`` is mandatory, not defaulted (#1619) — see the twin
        method on ``PvpRepo`` for the full reasoning. Both scans have
        the same shape and the same caller.
        """
        stmt = (
            select(P2pTrade)
            .where(
                P2pTrade.status == TRADE_PENDING,
                P2pTrade.created_at < cutoff,
            )
            .order_by(P2pTrade.id.asc())
            .limit(limit)
        )
        if order_id is not None:
            stmt = stmt.where(P2pTrade.order_id == order_id)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Seller stats
    # ------------------------------------------------------------------

    async def get_stats(self, user_id: int) -> P2pSellerStats:
        """The seller's counters; an all-zeros transient default when no
        row exists yet (absence == never traded). The default is NOT
        persisted — only the increment writers create rows.
        """
        row = await self._session.get(P2pSellerStats, user_id)
        if row is not None:
            return row
        return P2pSellerStats(
            user_id=user_id, successful_trades=0, total_sold_com=0, dispute_count=0
        )

    async def record_successful_trade(self, user_id: int, amount_com: int) -> None:
        """``successful_trades += 1`` and ``total_sold_com += amount_com``.

        Upsert with in-SQL increments (legacy's INSERT OR IGNORE +
        UPDATE pair at bot.py:20044-20047, collapsed to one statement)
        so two concurrent confirms both count.
        """
        stmt = (
            sqlite_insert(P2pSellerStats)
            .values(
                user_id=user_id,
                successful_trades=1,
                total_sold_com=amount_com,
                dispute_count=0,
            )
            .on_conflict_do_update(
                index_elements=["user_id"],
                set_={
                    "successful_trades": P2pSellerStats.successful_trades + 1,
                    "total_sold_com": P2pSellerStats.total_sold_com + amount_com,
                },
            )
        )
        await self._session.execute(stmt)

    async def record_dispute(self, user_id: int) -> None:
        """``dispute_count += 1`` (legacy bot.py:20242-20246)."""
        stmt = (
            sqlite_insert(P2pSellerStats)
            .values(
                user_id=user_id,
                successful_trades=0,
                total_sold_com=0,
                dispute_count=1,
            )
            .on_conflict_do_update(
                index_elements=["user_id"],
                set_={"dispute_count": P2pSellerStats.dispute_count + 1},
            )
        )
        await self._session.execute(stmt)
