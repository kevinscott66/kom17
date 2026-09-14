"""#1903: a second ``double_daily`` buster must not be a paid no-op.

The shop's own copy for this item promises the **next** ``/daily``
will be doubled (``h_inventory_use_success_double_daily``), i.e. one
item buys one doubled claim. The row that carries it is presence-only:
``PrivilegesRepo.grant_buster`` upserts ``(user_id, 'double_daily', 0)``
with MAX semantics on ``expires_at``, and ``/daily`` deletes it on a
successful claim. Nothing in that shape can hold "two". So applying a
second buster while one is still armed used to consume the inventory
entry (``InventoryUseService`` step 5) and then re-upsert the row the
user already had — the second item paid for nothing.

That is the shape ``#192`` fixed for VIP, where a MAX-on-expiry write
turned a repeat purchase into a paid no-op. The fix here is the
conservative half of the same idea: refuse the use and keep the item,
so nothing the user paid for is destroyed. Banking charges instead
would need a second clock — one ``expires_at`` cannot keep five
charges alive when only one ``/daily`` a day can spend them.

The inventory entry survives the refusal on purpose:
``PurchaseService`` never writes ``InventoryItem.expires`` (it stays
NULL) and ``InventoryRepo.cleanup_expired`` only deletes rows with a
non-NULL past expiry, so a refused buster waits in ``/inventory``
indefinitely.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import (
    EconomyUser,
    InventoryItem,
    ShopItem,
    UserPrivilege,
)
from telegram_invite_bot.handlers.shop import _format_buster_already_active
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.inventory_repo import InventoryRepo
from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
from telegram_invite_bot.repositories.shop_items_repo import ShopItemsRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.repositories.vip_repo import VipRepo
from telegram_invite_bot.services.inventory_use_planner import InventoryEffectKind
from telegram_invite_bot.services.inventory_use_service import (
    InventoryUseService,
    UseOutcome,
)

_NOW = datetime(2026, 9, 9, 12, 0, 0)
_USER = 42
_ITEM = 2


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


def _service(session: AsyncSession) -> InventoryUseService:
    return InventoryUseService(
        InventoryRepo(session),
        VipRepo(session),
        PrivilegesRepo(session),
        ShopItemsRepo(session),
        EconomyRepo(session),
        TransactionsRepo(session),
    )


async def _seed(session: AsyncSession, *, entries: int) -> list[int]:
    session.add(
        ShopItem(
            id=_ITEM,
            name="2x daily",
            description="",
            price=500,
            type="double_daily",
            stock=-1,
        )
    )
    session.add(EconomyUser(user_id=_USER, balance=100, language="ru"))
    ids: list[int] = []
    for index in range(entries):
        row = InventoryItem(
            user_id=_USER,
            item_id=_ITEM,
            # Distinct instants — ``uq_inventory_user_item_dt`` is
            # ``(user_id, item_id, purchase_date)``, so two entries for
            # the same item need two purchase moments.
            purchase_date=_NOW - timedelta(days=1, seconds=index),
            used=False,
        )
        session.add(row)
        await session.flush()
        ids.append(row.id)
    await session.commit()
    return ids


async def test_the_first_buster_still_arms(session: AsyncSession) -> None:
    """Control: nothing about the ordinary single-use path changed."""
    (first,) = await _seed(session, entries=1)

    result = await _service(session).use(user_id=_USER, entry_id=first, now=_NOW)
    await session.commit()

    assert result.outcome is UseOutcome.SUCCESS
    assert result.kind is InventoryEffectKind.DOUBLE_DAILY_BUSTER
    assert result.buster_expires_at == _NOW + timedelta(days=3)


async def test_a_second_buster_is_refused_while_one_is_armed(
    session: AsyncSession,
) -> None:
    first, second = await _seed(session, entries=2)
    service = _service(session)

    assert (await service.use(user_id=_USER, entry_id=first, now=_NOW)).outcome is (
        UseOutcome.SUCCESS
    )
    await session.commit()

    result = await service.use(user_id=_USER, entry_id=second, now=_NOW + timedelta(hours=1))
    await session.commit()

    assert result.outcome is UseOutcome.BUSTER_ALREADY_ACTIVE
    assert result.kind is InventoryEffectKind.DOUBLE_DAILY_BUSTER
    # The refusal must carry the standing expiry so the card can tell
    # the user when their armed buster runs out rather than leaving
    # them to guess whether the item is broken.
    assert result.buster_expires_at == _NOW + timedelta(days=3)


async def test_the_refused_entry_stays_in_the_inventory(session: AsyncSession) -> None:
    first, second = await _seed(session, entries=2)
    service = _service(session)

    await service.use(user_id=_USER, entry_id=first, now=_NOW)
    await session.commit()
    await service.use(user_id=_USER, entry_id=second, now=_NOW + timedelta(hours=1))
    await session.commit()

    burned = await session.get(InventoryItem, first)
    kept = await session.get(InventoryItem, second)
    assert burned is not None
    assert kept is not None
    assert burned.used is True
    assert kept.used is False
    assert kept.used_date is None
    # NULL ``expires`` is what makes "keep it for later" safe — see the
    # module docstring.
    assert kept.expires is None


async def test_the_armed_row_is_left_exactly_as_it_was(session: AsyncSession) -> None:
    """The refusal writes nothing — not even a harmless MAX no-op."""
    first, second = await _seed(session, entries=2)
    service = _service(session)

    await service.use(user_id=_USER, entry_id=first, now=_NOW)
    await session.commit()
    before = await session.get(UserPrivilege, (_USER, "double_daily", 0))
    assert before is not None
    armed_at = before.expires_at

    await service.use(user_id=_USER, entry_id=second, now=_NOW + timedelta(hours=1))
    await session.commit()

    after = await session.get(UserPrivilege, (_USER, "double_daily", 0))
    assert after is not None
    assert after.expires_at == armed_at


async def test_a_buster_whose_window_has_passed_does_not_block(
    session: AsyncSession,
) -> None:
    """The gate reads ``get_active``, not row presence.

    An expired row is not an armed buster: ``/daily`` would ignore it
    and ``delete_expired`` has simply not swept it yet. Refusing on it
    would strand the item behind a privilege nobody can spend.
    """
    first, second = await _seed(session, entries=2)
    service = _service(session)

    await service.use(user_id=_USER, entry_id=first, now=_NOW)
    await session.commit()

    later = _NOW + timedelta(days=4)
    result = await service.use(user_id=_USER, entry_id=second, now=later)
    await session.commit()

    assert result.outcome is UseOutcome.SUCCESS
    assert result.buster_expires_at == later + timedelta(days=3)


async def test_a_never_expiring_row_refuses_without_a_date(session: AsyncSession) -> None:
    """``expires_at <= 0`` is legacy's "never expires" slot.

    This pipeline never writes it (``grant_buster`` always pins a
    3-day TTL), but ``get_active`` returns such a row as active, so the
    refusal must survive it — and must not hand the card a 1970 date.
    """
    (entry,) = await _seed(session, entries=1)
    session.add(
        UserPrivilege(
            user_id=_USER,
            privilege_type="double_daily",
            group_id=0,
            value=None,
            expires_at=0.0,
        )
    )
    await session.commit()

    result = await _service(session).use(user_id=_USER, entry_id=entry, now=_NOW)
    await session.commit()

    assert result.outcome is UseOutcome.BUSTER_ALREADY_ACTIVE
    assert result.buster_expires_at is None
    row = await session.get(InventoryItem, entry)
    assert row is not None
    assert row.used is False


def test_both_refusal_strings_exist_in_both_languages() -> None:
    """The card has two shapes; neither may fall back to a raw key."""
    dated = _format_buster_already_active(lang="ru", expires_at=_NOW)
    undated = _format_buster_already_active(lang="ru", expires_at=None)
    assert "2026-09-09 12:00:00" in dated
    assert "h_inventory_use_buster_active" not in dated
    assert "h_inventory_use_buster_active_no_expiry" not in undated
    assert "/inventory" in undated

    dated_en = _format_buster_already_active(lang="en", expires_at=_NOW)
    undated_en = _format_buster_already_active(lang="en", expires_at=None)
    assert "2026-09-09 12:00:00" in dated_en
    assert "h_inventory_use_buster_active" not in dated_en
    assert "h_inventory_use_buster_active_no_expiry" not in undated_en
