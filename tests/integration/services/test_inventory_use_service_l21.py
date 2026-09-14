"""``InventoryUseService`` L-21 coverage: xp_boost / custom_title / unwarn.

xp_boost is a full grant kind (consume + privilege write) handled by the
service. custom_title and unwarn are handler-completed kinds: the service
classifies them, surfaces the pending entry id, and does NOT consume —
the FSM (custom_title) / moderation flow (unwarn) own the consume so the
abandon-without-consume / refuse-without-consume contracts hold. These
tests pin exactly that: NEEDS_* outcomes leave the entry UNUSED.
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

_NOW = datetime(2026, 5, 15, 12, 0, 0)


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


async def _seed_item(session: AsyncSession, *, item_id: int, name: str, type_: str) -> int:
    session.add(ShopItem(id=item_id, name=name, description="", price=100, type=type_, stock=-1))
    return item_id


async def _seed_entry(session: AsyncSession, *, user_id: int, item_id: int) -> int:
    entry = InventoryItem(
        user_id=user_id,
        item_id=item_id,
        purchase_date=_NOW - timedelta(days=1),
        used=False,
        expires=None,
    )
    session.add(entry)
    await session.flush()
    return entry.id


# ----------------------------------------------------------------------
# XP_BOOST — full grant
# ----------------------------------------------------------------------


async def test_use_xp_boost_writes_privilege_and_marks_used(
    session: AsyncSession,
) -> None:
    item_id = await _seed_item(session, item_id=9, name="⚡ Ускорение", type_="xp_boost")
    session.add(EconomyUser(user_id=42, balance=100, language="ru"))
    entry_id = await _seed_entry(session, user_id=42, item_id=item_id)
    await session.commit()

    result = await _service(session).use(user_id=42, entry_id=entry_id, now=_NOW)
    await session.commit()

    assert result.outcome is UseOutcome.SUCCESS
    assert result.kind is InventoryEffectKind.XP_BOOST
    assert result.xp_boost_multiplier == 2
    assert result.xp_boost_expires_at == _NOW + timedelta(minutes=60)

    priv = await session.get(UserPrivilege, (42, "xp_boost", 0))
    assert priv is not None
    assert priv.value == '{"multiplier": 2}'
    assert priv.expires_at == (_NOW + timedelta(minutes=60)).timestamp()

    row = await session.get(InventoryItem, entry_id)
    assert row is not None and row.used is True


# ----------------------------------------------------------------------
# CUSTOM_TITLE — handler-completed, NOT consumed by the service
# ----------------------------------------------------------------------


async def test_use_custom_title_returns_needs_title_input_without_consuming(
    session: AsyncSession,
) -> None:
    item_id = await _seed_item(session, item_id=10, name="📝 Свой титул", type_="custom_title")
    session.add(EconomyUser(user_id=42, balance=100, language="ru"))
    entry_id = await _seed_entry(session, user_id=42, item_id=item_id)
    await session.commit()

    result = await _service(session).use(user_id=42, entry_id=entry_id, now=_NOW)
    await session.commit()

    assert result.outcome is UseOutcome.NEEDS_TITLE_INPUT
    assert result.kind is InventoryEffectKind.CUSTOM_TITLE
    assert result.pending_item_id == item_id
    # The entry is the handler's to consume after the FSM step — the
    # service must NOT have marked it used.
    row = await session.get(InventoryItem, entry_id)
    assert row is not None and row.used is False
    # No custom_title privilege written yet.
    assert await session.get(UserPrivilege, (42, "custom_title", 0)) is None


# ----------------------------------------------------------------------
# UNWARN — handler-completed, NOT consumed by the service
# ----------------------------------------------------------------------


async def test_use_unwarn_returns_needs_moderation_without_consuming(
    session: AsyncSession,
) -> None:
    item_id = await _seed_item(session, item_id=11, name="🛡️ Снятие предупреждения", type_="unwarn")
    session.add(EconomyUser(user_id=42, balance=100, language="ru"))
    entry_id = await _seed_entry(session, user_id=42, item_id=item_id)
    await session.commit()

    result = await _service(session).use(user_id=42, entry_id=entry_id, now=_NOW)
    await session.commit()

    assert result.outcome is UseOutcome.NEEDS_MODERATION
    assert result.kind is InventoryEffectKind.UNWARN
    assert result.pending_item_id == item_id
    # Moderation lives in a different DB; the service consumes nothing.
    row = await session.get(InventoryItem, entry_id)
    assert row is not None and row.used is False


async def test_needs_outcomes_still_reject_used_entry(session: AsyncSession) -> None:
    # The used pre-check fires before the NEEDS_* branches, so a
    # double-click on an already-redeemed custom_title surfaces
    # ALREADY_USED, not a second FSM prompt.
    item_id = await _seed_item(session, item_id=12, name="📝 Свой титул", type_="custom_title")
    session.add(EconomyUser(user_id=42, balance=100, language="ru"))
    entry = InventoryItem(
        user_id=42,
        item_id=item_id,
        purchase_date=_NOW - timedelta(days=1),
        used=True,
        expires=None,
    )
    session.add(entry)
    await session.flush()
    await session.commit()

    result = await _service(session).use(user_id=42, entry_id=entry.id, now=_NOW)
    assert result.outcome is UseOutcome.ALREADY_USED
