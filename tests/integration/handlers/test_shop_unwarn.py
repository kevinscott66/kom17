"""#1133: the shop unwarn item must lift exactly one warning per entry.

``InventoryUseService.use`` returns ``NEEDS_MODERATION`` *without*
consuming (``services/inventory_use_service.py:344-365``): the warning
lives in ``moderation.db`` and the inventory entry in ``economy.db``, so
the handler owns both halves. That hand-off is what makes
``_apply_unwarn`` the only handler-completed kind whose consume is a
cross-database compensation problem, and nothing joined the two halves
before this module existed.

The invariant these tests pin is a conservation law, not a happy path:
**warnings removed == entries consumed**. The interesting direction is
the second redemption of an entry that another update already spent —
the removal must be rolled back, not committed with a success reply.

The last test in the module holds the same law across the OTHER tear
(#1277): the moderation half is durable the moment ``_apply_unwarn``
returns, so anything the handler does afterwards that can raise must
not be able to take the consume back down with it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.db import Checkpoint
from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import EconomyBase, ModerationBase
from telegram_invite_bot.db.models.economy import EconomyUser, InventoryItem, ShopItem
from telegram_invite_bot.db.models.moderation import Warning
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.shop import _apply_unwarn, handle_inventory_use
from telegram_invite_bot.keyboards.builders.shop import InventoryUse
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.inventory_repo import InventoryRepo
from telegram_invite_bot.repositories.moderation_repo import ModerationRepo
from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
from telegram_invite_bot.repositories.shop_items_repo import ShopItemsRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.repositories.vip_repo import VipRepo
from telegram_invite_bot.services.inventory_use_service import InventoryUseService

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext
    from aiogram.types import CallbackQuery
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.config.settings import Settings

_NOW = datetime(2026, 5, 15, 12, 0, 0)
_USER = 42
_CHAT = -1001234567890
_ITEM = 8


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    economy = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    moderation = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'moderation.db'}")
    async with economy.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    async with moderation.begin() as conn:
        await conn.run_sync(ModerationBase.metadata.create_all)
    try:
        yield EngineRegistry(
            engines={DBName.ECONOMY: economy, DBName.MODERATION: moderation},
            sessions={
                DBName.ECONOMY: async_sessionmaker(economy, expire_on_commit=False),
                DBName.MODERATION: async_sessionmaker(moderation, expire_on_commit=False),
            },
        )
    finally:
        await economy.dispose()
        await moderation.dispose()


async def _seed_entry(registry: EngineRegistry) -> int:
    async with registry.session(DBName.ECONOMY)() as session:
        session.add(
            ShopItem(
                id=_ITEM,
                name="🛡️ Снятие предупреждения",
                description="",
                price=800,
                type="unwarn",
                stock=40,
            )
        )
        session.add(EconomyUser(user_id=_USER, balance=100, language="ru"))
        entry = InventoryItem(
            user_id=_USER,
            item_id=_ITEM,
            purchase_date=_NOW - timedelta(days=1),
            used=False,
            expires=None,
        )
        session.add(entry)
        await session.flush()
        entry_id = int(entry.id)
        await session.commit()
    return entry_id


async def _seed_warnings(registry: EngineRegistry, count: int) -> None:
    async with registry.session(DBName.MODERATION)() as session:
        repo = ModerationRepo(session)
        for i in range(count):
            await repo.add_warning(
                user_id=_USER,
                chat_id=_CHAT,
                admin_id=1,
                reason=f"seed {i}",
            )
        await session.commit()


async def _active_warnings(registry: EngineRegistry) -> int:
    async with registry.session(DBName.MODERATION)() as session:
        total = await session.scalar(
            select(func.count())
            .select_from(Warning)
            .where(Warning.user_id == _USER, Warning.active.is_(True))
        )
    return int(total or 0)


async def _entry_used(registry: EngineRegistry, entry_id: int) -> bool:
    async with registry.session(DBName.ECONOMY)() as session:
        row = await session.get(InventoryItem, entry_id)
        assert row is not None
        return bool(row.used)


async def _redeem(registry: EngineRegistry, entry_id: int) -> bool:
    """One /inventory 🎁 tap: its own economy session, committed at exit.

    Mirrors the real update lifecycle — ``SessionMiddleware`` opens the
    economy session and commits it after the handler returns.
    """
    async with registry.session(DBName.ECONOMY)() as session:
        removed = await _apply_unwarn(
            registry=registry,
            inventory_repo=InventoryRepo(session),
            user_id=_USER,
            entry_id=entry_id,
            main_chat_id=_CHAT,
            now=datetime.now(UTC).replace(tzinfo=None),
        )
        await session.commit()
    return removed


async def test_one_redemption_lifts_one_warning_and_spends_the_entry(
    registry: EngineRegistry,
) -> None:
    entry_id = await _seed_entry(registry)
    await _seed_warnings(registry, 2)

    assert await _redeem(registry, entry_id) is True

    assert await _active_warnings(registry) == 1
    assert await _entry_used(registry, entry_id) is True


async def test_a_second_redemption_of_a_spent_entry_lifts_nothing(
    registry: EngineRegistry,
) -> None:
    """#1133: the consume rowcount is the authority, not the removal.

    Two concurrent taps both pass the service's ``used=0`` pre-check and
    both reach here; each opens its *own* moderation session and each
    can find a distinct active warning to soft-delete. Only one consume
    can match ``used=0``, so the loser must put its removal back — it
    has already been paid for by nobody.

    Replaying that sequentially is exactly equivalent and fully
    deterministic: the second call's removal must not survive.
    """
    entry_id = await _seed_entry(registry)
    await _seed_warnings(registry, 2)

    assert await _redeem(registry, entry_id) is True
    assert await _redeem(registry, entry_id) is False

    # Conservation law: one entry spent, one warning lifted.
    assert await _active_warnings(registry) == 1
    assert await _entry_used(registry, entry_id) is True


async def test_no_active_warning_refuses_without_consuming(
    registry: EngineRegistry,
) -> None:
    """The refuse-without-consume contract: no warning, no charge."""
    entry_id = await _seed_entry(registry)

    assert await _redeem(registry, entry_id) is False

    assert await _entry_used(registry, entry_id) is False


async def test_an_expired_warning_is_not_liftable(registry: EngineRegistry) -> None:
    """``remove_last_warning`` filters expired rows, so the entry survives."""
    entry_id = await _seed_entry(registry)
    async with registry.session(DBName.MODERATION)() as session:
        session.add(
            Warning(
                user_id=_USER,
                chat_id=_CHAT,
                reason="stale",
                admin_id=1,
                date=_NOW - timedelta(days=60),
                expires=_NOW - timedelta(days=30),
                active=True,
            )
        )
        await session.commit()

    assert await _redeem(registry, entry_id) is False

    assert await _active_warnings(registry) == 1
    assert await _entry_used(registry, entry_id) is False


def _service(session: AsyncSession) -> InventoryUseService:
    return InventoryUseService(
        InventoryRepo(session),
        VipRepo(session),
        PrivilegesRepo(session),
        ShopItemsRepo(session),
        EconomyRepo(session),
        TransactionsRepo(session),
    )


def _flooded_callback() -> CallbackQuery:
    """A 🎁 tap whose ``answer()`` raises, like flood control does.

    ``message=None`` keeps ``_safe_edit`` a no-op (it bails on anything
    that is not a real ``Message``), so the raise this test cares about
    is unambiguously the one from ``callback.answer()``.
    """

    async def answer(*args: object, **kwargs: object) -> None:
        raise TelegramRetryAfter(
            method="sendMessage",  # type: ignore[arg-type]
            message="Too Many Requests",
            retry_after=7,
        )

    return cast(
        "CallbackQuery",
        SimpleNamespace(from_user=SimpleNamespace(id=_USER), message=None, answer=answer),
    )


async def _use_via_handler(registry: EngineRegistry, entry_id: int) -> None:
    """Drive the real handler and let the middleware react to the raise.

    ``BaseSessionMiddleware`` rolls the economy session back on any
    exception out of the handler and commits it otherwise
    (``middlewares/base.py:113-116`` / ``:131-132``); this mirrors both
    arms so the test sees exactly what production would persist.
    """
    async with registry.session(DBName.ECONOMY)() as session:
        checkpoint = Checkpoint()
        checkpoint.track(session)
        try:
            await handle_inventory_use(
                _flooded_callback(),
                InventoryUse(entry_id=entry_id),
                _service(session),
                InventoryRepo(session),
                cast("FSMContext", None),
                registry,
                cast("Settings", SimpleNamespace(bot=SimpleNamespace(main_chat_id=_CHAT))),
                "ru",
                checkpoint=checkpoint,
            )
        except TelegramRetryAfter:
            await session.rollback()
        else:  # pragma: no cover — the stub callback always raises
            await session.commit()


async def test_a_failing_result_card_cannot_give_the_spent_entry_back(
    registry: EngineRegistry,
) -> None:
    """#1277: the consume must survive a Telegram failure after the lift.

    ``_apply_unwarn`` commits moderation.db before it returns, so the
    warning is gone for good. Without the handler's ``checkpoint()`` the
    economy consume still rides the middleware commit that never comes,
    and the user walks away with the warning lifted and the 800-coin
    entry still unused — repeatably, once per warning.
    """
    entry_id = await _seed_entry(registry)
    await _seed_warnings(registry, 2)

    await _use_via_handler(registry, entry_id)

    assert await _active_warnings(registry) == 1
    assert await _entry_used(registry, entry_id) is True
