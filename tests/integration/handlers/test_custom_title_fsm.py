"""Handler coverage for the custom_title FSM title-input step (L-21).

Drives :func:`handlers.custom_title.handle_custom_title_text` against a
real SQLite economy session plus a real aiogram :class:`FSMContext`
(MemoryStorage) and a minimal fake :class:`Message`. Pins the
consume + grant on a valid title, the reprompt-without-consume on an
empty / all-invisible title, and the bidi/zero-width sanitisation.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db import Checkpoint
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import (
    EconomyUser,
    InventoryItem,
    ShopItem,
    UserPrivilege,
)
from telegram_invite_bot.fsm.custom_title import CustomTitleStates
from telegram_invite_bot.handlers.custom_title import (
    PENDING_ENTRY_FIELD,
    handle_custom_title_text,
)
from telegram_invite_bot.repositories.inventory_repo import InventoryRepo
from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
from telegram_invite_bot.repositories.shop_items_repo import ShopItemsRepo

_NOW = datetime(2026, 5, 15, 12, 0, 0)
_USER = 42


@dataclass
class _FakeUser:
    id: int = _USER
    is_bot: bool = False
    first_name: str = "T"
    username: str | None = "t"


@dataclass
class _FakeMessage:
    text: str | None
    from_user: _FakeUser = field(default_factory=_FakeUser)
    replies: list[str] = field(default_factory=list)
    fail_reply: bool = False

    async def reply(self, text: str, **_kwargs: object) -> None:
        if self.fail_reply:
            raise RuntimeError("synthetic transport failure")
        self.replies.append(text)


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


def _state() -> FSMContext:
    storage = MemoryStorage()
    key = StorageKey(bot_id=1, chat_id=_USER, user_id=_USER)
    return FSMContext(storage=storage, key=key)


async def _seed(session: AsyncSession) -> int:
    session.add(
        ShopItem(
            id=10,
            name="📝 Свой титул",
            description="",
            price=100,
            type="custom_title",
            stock=-1,
        )
    )
    session.add(EconomyUser(user_id=_USER, balance=100, language="ru"))
    entry = InventoryItem(
        user_id=_USER,
        item_id=10,
        purchase_date=_NOW - timedelta(days=1),
        used=False,
        expires=None,
    )
    session.add(entry)
    await session.flush()
    await session.commit()
    return entry.id


async def _run(session: AsyncSession, state: FSMContext, text: str | None) -> _FakeMessage:
    msg = _FakeMessage(text=text)
    await handle_custom_title_text(
        msg,  # type: ignore[arg-type]
        state,
        InventoryRepo(session),
        ShopItemsRepo(session),
        PrivilegesRepo(session),
        "ru",
    )
    return msg


async def test_valid_title_consumes_entry_and_writes_grant(
    session: AsyncSession,
) -> None:
    entry_id = await _seed(session)
    state = _state()
    await state.set_state(CustomTitleStates.awaiting_title)
    await state.set_data({PENDING_ENTRY_FIELD: entry_id})

    msg = await _run(session, state, "Грозный админ")
    await session.commit()

    assert any("Титул установлен" in r for r in msg.replies)
    priv = await session.get(UserPrivilege, (_USER, "custom_title", 0))
    assert priv is not None
    assert json.loads(priv.value)["title"] == "Грозный админ"
    row = await session.get(InventoryItem, entry_id)
    assert row is not None and row.used is True
    # FSM cleared.
    assert await state.get_state() is None


async def test_empty_title_reprompts_without_consuming(session: AsyncSession) -> None:
    entry_id = await _seed(session)
    state = _state()
    await state.set_state(CustomTitleStates.awaiting_title)
    await state.set_data({PENDING_ENTRY_FIELD: entry_id})

    await _run(session, state, "   ")
    await session.commit()

    # Not consumed, no grant, still in-state for a retry.
    row = await session.get(InventoryItem, entry_id)
    assert row is not None and row.used is False
    assert await session.get(UserPrivilege, (_USER, "custom_title", 0)) is None
    assert await state.get_state() == CustomTitleStates.awaiting_title.state


async def test_all_invisible_title_is_rejected(session: AsyncSession) -> None:
    entry_id = await _seed(session)
    state = _state()
    await state.set_state(CustomTitleStates.awaiting_title)
    await state.set_data({PENDING_ENTRY_FIELD: entry_id})

    # Only zero-width + bidi control chars → sanitises to "".
    await _run(session, state, "\u200b\u202e\ufeff")
    await session.commit()

    row = await session.get(InventoryItem, entry_id)
    assert row is not None and row.used is False
    assert await session.get(UserPrivilege, (_USER, "custom_title", 0)) is None


async def test_bidi_chars_stripped_from_stored_title(session: AsyncSession) -> None:
    entry_id = await _seed(session)
    state = _state()
    await state.set_state(CustomTitleStates.awaiting_title)
    await state.set_data({PENDING_ENTRY_FIELD: entry_id})

    await _run(session, state, "a\u202eb\u200cc")
    await session.commit()

    priv = await session.get(UserPrivilege, (_USER, "custom_title", 0))
    assert priv is not None
    assert json.loads(priv.value)["title"] == "abc"


async def test_grant_outlives_a_failing_confirmation(session: AsyncSession) -> None:
    """The FSM clear must not outlive the writes it declares finished.

    ``state.clear()`` writes to the FSM store (prod runs
    ``FSM_BACKEND=sqlite``), which is not the economy session. Without the
    handler's checkpoint, a raise on the confirmation reply sends the
    middleware into ``session.rollback()`` — returning the entry and
    dropping the privilege — while the FSM already says the flow is done.
    The checkpoint commits the pair first, so the two stores agree
    whichever way the reply goes.
    """
    entry_id = await _seed(session)
    state = _state()
    await state.set_state(CustomTitleStates.awaiting_title)
    await state.set_data({PENDING_ENTRY_FIELD: entry_id})

    checkpoint = Checkpoint()
    checkpoint.track(session)
    msg = _FakeMessage(text="Барон", fail_reply=True)

    with pytest.raises(RuntimeError, match="synthetic"):
        await handle_custom_title_text(
            msg,  # type: ignore[arg-type]
            state,
            InventoryRepo(session),
            ShopItemsRepo(session),
            PrivilegesRepo(session),
            "ru",
            checkpoint,
        )

    # What ``middlewares/base`` does for a handler that raised.
    await session.rollback()

    row = await session.get(InventoryItem, entry_id)
    assert row is not None and row.used is True
    assert await session.get(UserPrivilege, (_USER, "custom_title", 0)) is not None
    assert await state.get_state() is None
