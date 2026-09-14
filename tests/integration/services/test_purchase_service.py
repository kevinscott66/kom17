"""Atomic ``PurchaseService`` against a real SQLite economy DB.

The service is the entire correctness story for Stage 17 — every
race-condition guarantee is encoded in its UPDATE-with-WHERE
statements. The handler is a thin shell around it (parse + render),
so the integration tests here are where the legacy parity has to
hold.

Cases covered:

* happy path — balance debited, stock decremented, inventory row
  inserted, transaction logged, all in one commit.
* infinite stock — debit + insert + log; stock unchanged.
* item not found — no DB side effects.
* out-of-stock (``stock == 0``) — no DB side effects.
* insufficient funds — no DB side effects, no inventory leak.
* stock race — second buyer (whose copy of the entity claims
  ``stock=1``) loses; debit must roll back, not strand the wallet.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.core.entities.shop import PurchaseStatus
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import (
    EconomyUser,
    InventoryItem,
    ShopItem,
    Transaction,
)
from telegram_invite_bot.services.purchase_service import PurchaseService


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with sessionmaker() as s:
            yield s
    finally:
        await engine.dispose()


async def _seed_wallet(session: AsyncSession, *, user_id: int, balance: int) -> None:
    session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))


async def _seed_item(session: AsyncSession, *, id_: int, name: str, price: int, stock: int) -> None:
    session.add(
        ShopItem(id=id_, name=name, description="", price=price, type="unwarn", stock=stock)
    )


async def test_happy_path_debits_decrements_inserts_logs(
    session: AsyncSession,
) -> None:
    await _seed_wallet(session, user_id=42, balance=500)
    await _seed_item(session, id_=1, name="Plushie", price=100, stock=3)
    await session.commit()
    service = PurchaseService(session)

    outcome = await service.purchase(user_id=42, item_id=1, now=datetime(2025, 6, 1, 12, 0, 0))
    await session.commit()

    assert outcome.status is PurchaseStatus.OK
    assert outcome.new_balance == 400
    assert outcome.new_stock == 2
    assert outcome.inventory_id is not None

    wallet = await session.get(EconomyUser, 42)
    assert wallet is not None
    assert wallet.balance == 400
    assert wallet.total_spent == 100

    item = await session.get(ShopItem, 1)
    assert item is not None
    assert item.stock == 2

    inv_rows = (await session.execute(select(InventoryItem))).scalars().all()
    assert len(inv_rows) == 1
    assert inv_rows[0].user_id == 42 and inv_rows[0].item_id == 1
    assert inv_rows[0].used is False

    tx_rows = (await session.execute(select(Transaction))).scalars().all()
    assert len(tx_rows) == 1
    tx = tx_rows[0]
    assert tx.from_id == 42 and tx.to_id == 0
    # Positive magnitude + direction in from/to — the ledger's single
    # convention. This writer used to store ``-price`` (legacy's sign),
    # which cancelled other spends inside /balance's weekly SUM.
    assert tx.amount == 100
    assert tx.type == "shop"
    assert tx.reason == "Покупка: Plushie"


async def test_infinite_stock_leaves_stock_alone(session: AsyncSession) -> None:
    """An infinite-stock item (``stock == -1``) must not have its
    column touched — legacy short-circuits the decrement at bot.py:13245
    and we mirror that. Otherwise stock would drift to -2 over time.
    """
    await _seed_wallet(session, user_id=42, balance=500)
    await _seed_item(session, id_=1, name="Forever", price=50, stock=-1)
    await session.commit()
    service = PurchaseService(session)

    outcome = await service.purchase(user_id=42, item_id=1)
    await session.commit()

    assert outcome.status is PurchaseStatus.OK
    assert outcome.new_stock == -1
    item = await session.get(ShopItem, 1)
    assert item is not None
    assert item.stock == -1


async def test_item_not_found_has_no_side_effects(session: AsyncSession) -> None:
    await _seed_wallet(session, user_id=42, balance=500)
    await session.commit()
    service = PurchaseService(session)

    outcome = await service.purchase(user_id=42, item_id=999)
    await session.commit()

    assert outcome.status is PurchaseStatus.ITEM_NOT_FOUND
    wallet = await session.get(EconomyUser, 42)
    assert wallet is not None
    assert wallet.balance == 500
    assert (await session.scalar(select(func.count()).select_from(InventoryItem))) == 0
    assert (await session.scalar(select(func.count()).select_from(Transaction))) == 0


async def test_out_of_stock_is_inert(session: AsyncSession) -> None:
    await _seed_wallet(session, user_id=42, balance=500)
    await _seed_item(session, id_=1, name="OOS", price=100, stock=0)
    await session.commit()
    service = PurchaseService(session)

    outcome = await service.purchase(user_id=42, item_id=1)
    await session.commit()

    assert outcome.status is PurchaseStatus.OUT_OF_STOCK
    wallet = await session.get(EconomyUser, 42)
    assert wallet is not None
    assert wallet.balance == 500
    assert (await session.scalar(select(func.count()).select_from(InventoryItem))) == 0


async def test_insufficient_funds_is_inert(session: AsyncSession) -> None:
    """The atomic ``WHERE balance >= ?`` guard is the only thing
    between a poor user and a debit-into-negative. Pin it.
    """
    await _seed_wallet(session, user_id=42, balance=50)
    await _seed_item(session, id_=1, name="Pricey", price=100, stock=5)
    await session.commit()
    service = PurchaseService(session)

    outcome = await service.purchase(user_id=42, item_id=1)
    await session.commit()

    assert outcome.status is PurchaseStatus.INSUFFICIENT_FUNDS
    wallet = await session.get(EconomyUser, 42)
    assert wallet is not None
    assert wallet.balance == 50  # untouched
    item = await session.get(ShopItem, 1)
    assert item is not None
    assert item.stock == 5  # untouched
    assert (await session.scalar(select(func.count()).select_from(InventoryItem))) == 0
    assert (await session.scalar(select(func.count()).select_from(Transaction))) == 0


async def test_no_wallet_row_treated_as_insufficient(session: AsyncSession) -> None:
    """If the caller forgot to seed via ``EconomyRepo.get_or_create``,
    the UPDATE finds no row and we report INSUFFICIENT_FUNDS rather
    than silently creating a negative-balance wallet. Documented in
    the service docstring.
    """
    await _seed_item(session, id_=1, name="X", price=10, stock=5)
    await session.commit()
    service = PurchaseService(session)

    outcome = await service.purchase(user_id=42, item_id=1)
    await session.commit()

    assert outcome.status is PurchaseStatus.INSUFFICIENT_FUNDS
    assert await session.get(EconomyUser, 42) is None


async def test_stock_race_rolls_back_the_debit(session: AsyncSession) -> None:
    """Simulate "between read and decrement, someone else took the
    last unit". We read the item entity (``stock=1``), then a
    concurrent flow sets stock to 0 in the DB, then we attempt the
    purchase. The decrement WHERE fails, and the debit must roll
    back — the wallet must NOT be left short.
    """
    await _seed_wallet(session, user_id=42, balance=500)
    await _seed_item(session, id_=1, name="LastOne", price=100, stock=1)
    await session.commit()
    service = PurchaseService(session)

    # Pre-fetch via the service path so the entity ``item`` it captures
    # internally thinks stock=1 — but mutate the DB underneath to 0.
    # We re-implement the test by mutating between the fetch and the
    # decrement: the cleanest way is to manually run the steps the
    # service runs and assert rollback. Equivalent via the public API:
    # set the row to 0 before calling purchase, but the public path
    # would short-circuit on the ``stock == 0`` pre-check. Instead we
    # set the row to a value the pre-check accepts but the decrement
    # rejects: stock=1 fetched, then concurrent flow sets to 0.
    # SQLAlchemy's identity map serves the same entity, so we issue
    # a raw UPDATE to bypass it.
    from sqlalchemy import update

    # Trigger the service mid-flight by monkey-patching _fetch_item to
    # return the optimistic snapshot, then mutate the DB.
    original_fetch = service._fetch_item  # noqa: SLF001

    async def racing_fetch(item_id: int):  # type: ignore[no-untyped-def]
        snap = await original_fetch(item_id)
        # Someone else takes the last unit between fetch and decrement.
        await session.execute(update(ShopItem).where(ShopItem.id == item_id).values(stock=0))
        await session.flush()
        return snap

    service._fetch_item = racing_fetch  # type: ignore[method-assign]  # noqa: SLF001

    outcome = await service.purchase(user_id=42, item_id=1)

    assert outcome.status is PurchaseStatus.OUT_OF_STOCK

    # Critical: the rollback inside the service must have undone the
    # debit. A failure here means real money would disappear.
    wallet = await session.get(EconomyUser, 42)
    assert wallet is not None
    assert wallet.balance == 500, "stock race must NOT debit the wallet"
    assert (await session.scalar(select(func.count()).select_from(InventoryItem))) == 0
    assert (await session.scalar(select(func.count()).select_from(Transaction))) == 0


# ---------------------------------------------------------------------------
# #1986: the failure path rolls back ITS OWN work, not the session's
# ---------------------------------------------------------------------------


async def test_lost_stock_race_keeps_unrelated_work_on_the_session(
    session: AsyncSession,
) -> None:
    """A lost stock race undoes the debit, not the whole session.

    #1986. ``PurchaseService`` is handed the update's shared economy
    session (``middlewares/economy.py:176``); the bare
    ``session.rollback()`` on the race branch discarded whatever else
    the update had already written to it.

    The race is made deterministic rather than raced: ``_fetch_item``
    is made to report the stock the buyer saw a moment ago, while the
    catalog row is already at zero — which is exactly the state the
    guarded ``WHERE stock > 0`` decrement exists to catch.
    """
    await _seed_wallet(session, user_id=42, balance=500)
    await _seed_wallet(session, user_id=777001, balance=50)
    await _seed_item(session, id_=1, name="Plushie", price=100, stock=0)
    await session.commit()
    service = PurchaseService(session)

    fresh = service._fetch_item  # noqa: SLF001 — the stale read is the point

    async def _stale(item_id: int) -> object:
        item = await fresh(item_id)
        return None if item is None else replace(item, stock=2)

    service._fetch_item = _stale  # type: ignore[method-assign] # noqa: SLF001

    other = await session.get(EconomyUser, 777001)
    assert other is not None
    other.balance = 777
    await session.flush()

    outcome = await service.purchase(user_id=42, item_id=1)
    await session.commit()
    session.expire_all()

    assert outcome.status is PurchaseStatus.OUT_OF_STOCK
    buyer = await session.get(EconomyUser, 42)
    assert buyer is not None
    assert buyer.balance == 500  # the debit is undone
    other = await session.get(EconomyUser, 777001)
    assert other is not None
    assert other.balance == 777
