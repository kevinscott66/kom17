"""Transactional shop purchase — Stage 17.

Wraps the four legacy steps of :func:`bot.py.buy_item` (bot.py:13207)
inside one async session so a failure at any point rolls the whole
attempt back. The middleware (:class:`EconomyMiddleware`) holds the
session boundary; this service only issues statements.

Why a service (not a repo method)
---------------------------------
The operation crosses three tables (``users``, ``shop_items``,
``inventory``, ``transactions``) and has business decisions (which
order to mutate in, what counts as a "race"). Repos stay table-shaped
and the orchestration logic lives here.

Out of scope (each deferred for a real reason)
---------------------------------------------
* **Post-purchase auto-apply** (VIP activation, luck-gift, color_nick,
  double_daily, …) — legacy's ``_apply_post_purchase_effects``
  (bot.py:13138, not a symbol in this package) touches privileges, status modifiers, group-scoped
  state. Each effect deserves its own port; the inventory row created
  here surfaces in /inventory just like a freshly-bought-but-not-yet-
  used row in legacy.
* **Group purchases** — legacy passes ``group_id`` and routes a
  percentage to the group treasury (bot.py:13240). Depends on the
  groups module we haven't ported.
* **Referral + developer purchase commissions** (legacy
  ``apply_purchase_commissions``, bot.py:13243) — wired here by #75
  and DELIBERATELY REMOVED by T-020/R10. A shop buy is paid in coins
  the buyer already holds: the price is burned, and minting 15% of it
  straight back to the inviter and the developer turned the bot's
  largest coin SINK into a 15%-leaky one. Those commissions still run
  on every path where real money enters — Stars, Crypto Pay,
  YooKassa, Stripe — because there the mint is an acquisition cost
  paid for by an incoming payment. See ``docs/ECONOMY_RATE_AUDIT.md``
  §8.5.
* **Process-wide ``_purchase_in_progress`` lock** — legacy guards with
  an in-memory ``set[user_id]`` (bot.py:13211). The transactional
  rowcount guards (``WHERE balance >= ?`` and
  ``WHERE stock > 0 OR stock = -1``) make double-spend impossible at
  the DB level; the in-memory set was protecting against telebot's
  blocking handler model, which we don't have.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from loguru import logger
from sqlalchemy import CursorResult, select, update

from telegram_invite_bot.core.entities.shop import (
    ACTIVATABLE_ITEM_TYPES,
    PurchaseOutcome,
    PurchaseStatus,
    ShopItemEntity,
)
from telegram_invite_bot.db.models.economy import (
    EconomyUser,
    InventoryItem,
    ShopItem,
    Transaction,
)
from telegram_invite_bot.repositories.shop_items_repo import shop_item_to_entity

log = logger.bind(component="services.purchase")

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql.dml import Update


_INFINITE_STOCK = -1


class PurchaseService:
    """Atomic /buy execution against the shared economy session.

    T-020/R10: this service mints nothing. A shop buy debits the
    buyer and writes an inventory row — that is the whole money flow.
    It used to also pay a referral + developer commission (#75, legacy
    bot.py:13243); see the module docstring for why that was removed
    and where those commissions still run.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def purchase(
        self,
        *,
        user_id: int,
        item_id: int,
        now: datetime | None = None,
    ) -> PurchaseOutcome:
        """Try to buy one of ``item_id`` for ``user_id``.

        Mutations happen in this order, all under the caller's session:

        1. SELECT the item — if missing, ``ITEM_NOT_FOUND``.
        2. If ``stock == 0`` — ``OUT_OF_STOCK`` (cheap pre-check;
           the rowcount guard below also covers a race).
        2a. If the ``type`` is not in :data:`ACTIVATABLE_ITEM_TYPES` —
           ``NO_EFFECT`` (#2006). Also before any write, and for a
           harder reason than the two above: this sale cannot be undone
           later. The effect planner has no branch for such a type, and
           :class:`InventoryUseService` refuses BEFORE consuming, so the
           buyer would hold a row that can never be used and that
           nothing in the bot can refund. ``ShopItemsRepo`` keeps the
           same rows out of ``/shop``; the check is repeated here
           because the id is a small guessable integer and inline buy
           keyboards from an older render stay live in chat history.
        3. Atomic debit:
           ``UPDATE users SET balance = balance - p, total_spent =
           total_spent + p WHERE user_id = ? AND balance >= p``.
           ``rowcount == 0`` means either the wallet didn't exist or
           the balance check failed — return ``INSUFFICIENT_FUNDS``.
        4. Atomic stock decrement (if not infinite):
           ``UPDATE shop_items SET stock = stock - 1 WHERE id = ? AND
           stock > 0``. ``rowcount == 0`` means someone else took the
           last unit between (1) and (4) — refund by leaving the
           session uncommitted is impossible because we've already
           debited; instead we undo the debit by rolling back the
           SAVEPOINT that steps (3)-(4) run under, and propagate
           ``OUT_OF_STOCK``. The middleware still commits the update
           normally — it only rolls back on a RAISED exception, and this
           path returns — but by then the debit is gone.
        5. INSERT into inventory + transactions.

        Step ordering rationale: legacy decrements stock first, then
        debits, then inserts — and refunds manually on inventory
        failure. We do debit-then-stock so a stock race wastes one
        UPDATE rather than one UPDATE + one refund; the rollback on
        failure is identical in user-visible terms.

        #1986: the savepoint replaced a ``self._session.rollback()``.
        This session is the update's — ``middlewares/base.py`` keeps
        exactly ONE — so undoing the purchase used to undo everything
        else written on it as well. Same argument as
        :meth:`P2pService.cancel_order` (#1985) and #209's
        ``expire_pending``.
        """
        now = now or datetime.now(UTC).replace(tzinfo=None)
        # #1951: ``now`` is the LEDGER clock — ``transactions.date`` is
        # naive UTC at every writer in the repo, and every reader bounds
        # it in UTC (``message_reward_day_total`` converts local midnights
        # before comparing). The ``inventory`` table is the opposite by
        # explicit contract: ``used_date`` is written from the handlers'
        # naive-local ``datetime.now()`` and ``expires`` is filtered
        # against the same clock in ``InventoryRepo.list_for_user``.
        # Writing one ``now`` to both put the two halves of a single
        # purchase three hours apart on the MSK host — measured on prod
        # row 48, bought and used 53 ms apart, stamped 10:56 and 13:56.
        # Same instant, re-rendered in the host zone.
        purchased_at = now.replace(tzinfo=UTC).astimezone().replace(tzinfo=None)

        item = await self._fetch_item(item_id)
        if item is None:
            return PurchaseOutcome(status=PurchaseStatus.ITEM_NOT_FOUND)
        if item.stock == 0:
            return PurchaseOutcome(status=PurchaseStatus.OUT_OF_STOCK, item=item)
        if item.type not in ACTIVATABLE_ITEM_TYPES:
            # #2006. Nothing has been written yet, and nothing will be:
            # the refusal is the whole point. Logged at WARNING because
            # reaching it means the catalog is offering a type this
            # package cannot activate — an operator wrote the row and
            # is waiting for a sale that must not happen.
            log.bind(user_id=user_id, item_id=item_id, item_type=item.type).warning(
                "refused a purchase of an item type nothing can activate"
            )
            return PurchaseOutcome(status=PurchaseStatus.NO_EFFECT, item=item)

        # Steps 3-4 (debit + stock decrement) are one SAVEPOINT: a lost
        # stock race undoes the debit and nothing else (#1986).
        async with self._session.begin_nested() as savepoint:
            debit_result = await self._execute_update(
                update(EconomyUser)
                .where(
                    EconomyUser.user_id == user_id,
                    EconomyUser.balance >= item.price,
                )
                .values(
                    balance=EconomyUser.balance - item.price,
                    total_spent=EconomyUser.total_spent + item.price,
                    last_seen=now,
                )
            )
            if debit_result.rowcount == 0:
                # Either no wallet row (caller should have seeded it via
                # EconomyRepo.get_or_create on /balance or /start) or the
                # wallet doesn't have enough coins. Don't seed-and-debit
                # here — refusing without side effects is the safer
                # default; the user will hit /balance and retry.
                return PurchaseOutcome(status=PurchaseStatus.INSUFFICIENT_FUNDS, item=item)

            new_stock: int
            if item.stock == _INFINITE_STOCK:
                new_stock = _INFINITE_STOCK
            else:
                stock_result = await self._execute_update(
                    update(ShopItem)
                    .where(ShopItem.id == item.id, ShopItem.stock > 0)
                    .values(stock=ShopItem.stock - 1)
                )
                if stock_result.rowcount == 0:
                    # Lost a race with another buyer. Roll the savepoint
                    # back so the debit above goes with it. This path
                    # RETURNS, so the middleware's ``except: rollback()``
                    # never fires — it commits, and what it commits must
                    # therefore no longer contain the debit.
                    await savepoint.rollback()
                    log.bind(
                        uid=user_id,
                        item_id=item_id,
                    ).warning("stock race: aborted purchase")
                    return PurchaseOutcome(status=PurchaseStatus.OUT_OF_STOCK, item=item)
                new_stock = item.stock - 1

        inv_row = InventoryItem(
            user_id=user_id,
            item_id=item.id,
            purchase_date=purchased_at,  # #1951: naive LOCAL, unlike ``date`` below
            used=False,
        )
        self._session.add(inv_row)
        self._session.add(
            Transaction(
                from_id=user_id,
                to_id=0,
                # Positive magnitude, direction in ``from_id``/``to_id`` —
                # the ledger's one convention (see the ``Transaction``
                # docstring). Legacy wrote spends negative; this row used
                # to copy that, and it was the ONLY new-pipeline writer
                # that did. Mixed signs cancel inside the weekly
                # ``SUM(amount)`` behind /balance, so a 100-coin purchase
                # plus a 100-coin activity in the same week reported
                # "sent: 0". Reads now take ``ABS`` for the legacy rows
                # still sitting in prod; new rows all point one way.
                amount=item.price,
                # Match legacy reason format exactly (bot.py:13237) so
                # admin /transactions audits can be greppped across both
                # systems during the migration.
                reason=f"Покупка: {item.name}",
                date=now,
                type="shop",
            )
        )
        await self._session.flush()

        # Re-read the wallet to surface the post-debit balance without
        # mirroring arithmetic the DB already did. ``flush`` ensures
        # the UPDATE landed; the row is still in the identity map.
        wallet = await self._session.get(EconomyUser, user_id)
        # noqa reason (#286): not a money guard — the debit already
        # landed and was flushed; this only narrows the read-back so the
        # response can quote a balance. A miss here rolls the purchase
        # back rather than mispricing it.
        assert wallet is not None  # noqa: S101 — UPDATE rowcount=1 ⇒ row exists
        new_balance = wallet.balance

        log.bind(
            uid=user_id,
            item_id=item_id,
            price=item.price,
            new_balance=new_balance,
            new_stock=new_stock,
        ).info("purchase succeeded")

        # T-020/R10: nothing is minted here. #75 used to run
        # ``apply_purchase_commissions(user_id, item.price)`` at this
        # point (legacy bot.py:13243); the buy is paid in coins that
        # already exist, so paying 10% to the inviter and 5% to the
        # developer handed 15% of every burn back as fresh supply.
        return PurchaseOutcome(
            status=PurchaseStatus.OK,
            item=item,
            new_balance=new_balance,
            new_stock=new_stock,
            inventory_id=inv_row.id,
        )

    async def _execute_update(self, stmt: Update) -> CursorResult[object]:
        """Run ``stmt`` and return the typed cursor for ``.rowcount`` access.

        ``AsyncSession.execute`` is typed to return ``Result[Any]`` in
        stubs — fine for SELECT-style use where ``.scalars()`` narrows
        it back, but for UPDATE the value we care about is
        ``.rowcount`` which lives on :class:`CursorResult`. Two call
        sites (the atomic debit and the stock decrement) each wrapped
        the same ``cast("CursorResult[object]", ...)`` over the same
        ``self._session.execute(...)``. Centralising the cast keeps
        the rowcount-guard idiom legible at the call site and means
        the typing workaround can evolve in one place if SQLAlchemy's
        stubs ever return a precise ``CursorResult`` for ``Update``.
        """
        return cast("CursorResult[object]", await self._session.execute(stmt))

    async def _fetch_item(self, item_id: int) -> ShopItemEntity | None:
        """Load one catalog row as an entity.

        #192: this used to build its OWN ``ShopItemEntity`` alongside
        ``ShopItemsRepo._to_entity``, and the two drifted — the repo
        learned to decode the ``data`` blob while this copy kept
        dropping it, so an item bought through /buy carried different
        parameters than the same item listed in /shop. Delegating to
        the repo's builder makes that class of divergence impossible:
        there is exactly one row→entity mapping in the codebase.
        """
        row = (
            await self._session.execute(select(ShopItem).where(ShopItem.id == item_id))
        ).scalar_one_or_none()
        if row is None:
            return None
        return shop_item_to_entity(row)
