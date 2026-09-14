"""Integration tests for ``BotGroupsRepo`` (L-49 write-side).

The load-bearing contract is :meth:`transfer_ownership`'s WHERE guard:
the ``added_by_user_id == from_user_id`` predicate makes the UPDATE
atomic against a concurrent transfer — a stale caller updates zero rows
and gets ``False`` instead of stealing the attribution.

The #111 block adds the second one: :meth:`register` must leave
``added_by_user_id`` alone on a re-add, or "kick the bot, add it back"
becomes a way to take over a group's 15% payout cut.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    create_async_engine,
)

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import BotGroup
from telegram_invite_bot.repositories.bot_groups_repo import BotGroupsRepo

OWNER = 42
OTHER = 99
TARGET = 777


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'users.db'}")
    async with eng.begin() as conn:
        await conn.run_sync(UsersBase.metadata.create_all)
    try:
        yield eng
    finally:
        await eng.dispose()


async def _seed(engine: AsyncEngine, rows: list[tuple[int, str | None, int]]) -> None:
    async with AsyncSession(engine) as session:
        for chat_id, title, added_by in rows:
            session.add(BotGroup(chat_id=chat_id, chat_title=title, added_by_user_id=added_by))
        await session.commit()


@pytest.mark.asyncio
async def test_list_owned_scopes_and_orders(engine: AsyncEngine) -> None:
    await _seed(engine, [(-1002, "B", OWNER), (-1001, "A", OWNER), (-1003, "C", OTHER)])
    async with AsyncSession(engine) as session:
        rows = await BotGroupsRepo(session).list_owned(OWNER)
    # chat_id ascending (stable picker pages), other owners excluded.
    assert rows == [(-1002, "B"), (-1001, "A")]


@pytest.mark.asyncio
async def test_get_owned_hides_foreign_and_unknown(engine: AsyncEngine) -> None:
    await _seed(engine, [(-1001, "A", OWNER)])
    async with AsyncSession(engine) as session:
        repo = BotGroupsRepo(session)
        assert await repo.get_owned(-1001, OWNER) == (-1001, "A")
        # Foreign and unknown are indistinguishable — both None.
        assert await repo.get_owned(-1001, OTHER) is None
        assert await repo.get_owned(-9999, OWNER) is None


@pytest.mark.asyncio
async def test_get_active_ignores_who_registered_the_group(engine: AsyncEngine) -> None:
    """#1926: the ``grp_`` deep link asks "is this a real group of
    ours", never "is it yours" — the person tapping the button is an
    ordinary member. Unknown chats still read as ``None``."""
    await _seed(engine, [(-1001, "A", OWNER)])
    async with AsyncSession(engine) as session:
        repo = BotGroupsRepo(session)
        assert await repo.get_active(-1001) == (-1001, "A")
        assert await repo.get_active(-9999) is None


@pytest.mark.asyncio
async def test_get_active_hides_a_group_the_bot_has_left(engine: AsyncEngine) -> None:
    """A deactivated row must not be a chat anyone can point their DM
    at — the bot cannot post there, and the row still carries a payout
    attribution."""
    await _seed(engine, [(-1001, "A", OWNER)])
    async with AsyncSession(engine) as session:
        repo = BotGroupsRepo(session)
        assert await repo.deactivate(-1001)
        await session.commit()
    async with AsyncSession(engine) as session:
        assert await BotGroupsRepo(session).get_active(-1001) is None


@pytest.mark.asyncio
async def test_transfer_ownership_moves_attribution(engine: AsyncEngine) -> None:
    await _seed(engine, [(-1001, "A", OWNER)])
    async with AsyncSession(engine) as session:
        repo = BotGroupsRepo(session)
        assert await repo.transfer_ownership(-1001, from_user_id=OWNER, to_user_id=TARGET)
        await session.commit()
    async with AsyncSession(engine) as session:
        repo = BotGroupsRepo(session)
        assert await repo.get_owned(-1001, TARGET) == (-1001, "A")
        assert await repo.get_owned(-1001, OWNER) is None


@pytest.mark.asyncio
async def test_transfer_ownership_guard_rejects_stale_owner(engine: AsyncEngine) -> None:
    await _seed(engine, [(-1001, "A", OWNER)])
    async with AsyncSession(engine) as session:
        repo = BotGroupsRepo(session)
        # OTHER never owned the row — zero rows match, nothing moves.
        assert not await repo.transfer_ownership(-1001, from_user_id=OTHER, to_user_id=TARGET)
        await session.commit()
    async with AsyncSession(engine) as session:
        assert await BotGroupsRepo(session).get_owned(-1001, OWNER) == (-1001, "A")


# --------------------------------------------------------------------------
# #111 — the bot's own membership: register on join, deactivate on removal
# --------------------------------------------------------------------------


async def _read(engine: AsyncEngine, chat_id: int) -> BotGroup | None:
    async with AsyncSession(engine) as session:
        return await session.get(BotGroup, chat_id)


@pytest.mark.asyncio
async def test_register_inserts_a_new_group(engine: AsyncEngine) -> None:
    async with AsyncSession(engine) as session:
        await BotGroupsRepo(session).register(
            -1001, added_by_user_id=OWNER, chat_title="A", has_admin_rights=True
        )
        await session.commit()

    row = await _read(engine, -1001)
    assert row is not None
    assert (row.added_by_user_id, row.chat_title, row.bot_has_admin_rights) == (
        OWNER,
        "A",
        1,
    )
    assert row.is_active == 1
    # ``added_at`` is what "/mygroups" shows as "since"; a join must stamp it.
    assert row.added_at


@pytest.mark.asyncio
async def test_register_again_never_moves_the_attribution(engine: AsyncEngine) -> None:
    """The payout-hijack guard.

    OTHER re-adds a bot that OWNER originally registered. The title and
    the rights flag are refreshed (they describe the chat as it is now),
    but the row keeps naming OWNER — otherwise kicking the bot and adding
    it back would hand OTHER the group's 15% cut, and would silently undo
    a deliberate ``/transfer_rights``.
    """
    await _seed(engine, [(-1001, "Old title", OWNER)])
    before = await _read(engine, -1001)
    assert before is not None

    async with AsyncSession(engine) as session:
        await BotGroupsRepo(session).register(
            -1001, added_by_user_id=OTHER, chat_title="New title", has_admin_rights=True
        )
        await session.commit()

    row = await _read(engine, -1001)
    assert row is not None
    assert row.added_by_user_id == OWNER
    assert row.chat_title == "New title"
    assert row.bot_has_admin_rights == 1
    # "Since when has the bot been here" does not restart on a re-add.
    assert row.added_at == before.added_at


@pytest.mark.asyncio
async def test_deactivate_hides_the_group_from_every_read(engine: AsyncEngine) -> None:
    await _seed(engine, [(-1001, "A", OWNER)])
    async with AsyncSession(engine) as session:
        repo = BotGroupsRepo(session)
        assert await repo.deactivate(-1001) is True
        await session.commit()

    # The row survives — only its visibility changes.
    row = await _read(engine, -1001)
    assert row is not None
    assert row.is_active == 0
    assert row.added_by_user_id == OWNER

    async with AsyncSession(engine) as session:
        repo = BotGroupsRepo(session)
        assert await repo.list_owned(OWNER) == []
        assert await repo.get_owned(-1001, OWNER) is None
        # A group the bot is not in cannot be handed to anyone either.
        assert not await repo.transfer_ownership(-1001, from_user_id=OWNER, to_user_id=TARGET)


@pytest.mark.asyncio
async def test_deactivate_is_idempotent(engine: AsyncEngine) -> None:
    """Telegram can deliver the same leave transition twice, and a chat
    the bot was never in can emit one too. Both report ``False``."""
    await _seed(engine, [(-1001, "A", OWNER)])
    async with AsyncSession(engine) as session:
        repo = BotGroupsRepo(session)
        assert await repo.deactivate(-1001) is True
        assert await repo.deactivate(-1001) is False
        assert await repo.deactivate(-9999) is False
        await session.commit()


@pytest.mark.asyncio
async def test_register_revives_a_deactivated_group(engine: AsyncEngine) -> None:
    await _seed(engine, [(-1001, "A", OWNER)])
    async with AsyncSession(engine) as session:
        await BotGroupsRepo(session).deactivate(-1001)
        await session.commit()
    async with AsyncSession(engine) as session:
        await BotGroupsRepo(session).register(
            -1001, added_by_user_id=OTHER, chat_title="A", has_admin_rights=False
        )
        await session.commit()

    async with AsyncSession(engine) as session:
        # Visible again, and still OWNER's.
        assert await BotGroupsRepo(session).get_owned(-1001, OWNER) == (-1001, "A")
