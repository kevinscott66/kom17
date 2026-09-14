"""#1960: deactivating an already-deactivated warning is not a success.

``remove_last_warning`` picked its row with a SELECT and then wrote it
with a blind ``UPDATE ... WHERE id = ?``, returning an unconditional
``True``. The two statements share no snapshot — a connection runs in
autocommit until a write promotes it to ``BEGIN IMMEDIATE``
(``db/engines.py``) — so a second caller that read the same id before
the first committed re-deactivated the row and was told it had lifted a
warning.

One caller over, that is money. ``InventoryUseService`` hands the shop's
unwarn item to the handler UNCONSUMED (``handlers/shop.py``), so two
taps on two SEPARATE 800-coin items both clear the ``used = 0``
pre-check, both land in ``_apply_unwarn``, both find the same lone
warning, and each then consumes its OWN entry — two items burned, one
warning lifted. #1133 made the consume the arbiter for the mirror case
(two warnings, one item); it cannot arbitrate this one, because neither
consume loses.

Sequentially the bug is invisible: the second call's SELECT finds
nothing and refuses. So the interleave is staged explicitly here — the
loser's SELECT runs, the winner's whole call runs and commits, and only
then does the loser's UPDATE reach the file.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import ModerationBase
from telegram_invite_bot.db.models.moderation import ModerationLog, Warning
from telegram_invite_bot.repositories.moderation_repo import ModerationRepo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

_USER = 555
_CHAT = -100_1
_ADMIN = 7


@asynccontextmanager
async def _two_sessions(tmp_path: Path) -> AsyncIterator[tuple[AsyncSession, AsyncSession]]:
    """Two independent sessions over one file — the shape prod races in.

    ``build_session`` in ``tests/integration/repositories`` yields a
    single session, which cannot express this at all.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'moderation.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(ModerationBase.metadata.create_all)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as winner, sessionmaker() as loser:
            yield winner, loser
    finally:
        await engine.dispose()


async def _seed_one_warning(session: AsyncSession) -> None:
    session.add(
        Warning(
            user_id=_USER,
            chat_id=_CHAT,
            reason="spam",
            admin_id=_ADMIN,
            date=datetime.now(UTC).replace(tzinfo=None),
            expires=None,
            active=True,
        )
    )
    await session.commit()


def _park_after_first_statement(session: AsyncSession, gate: asyncio.Event) -> asyncio.Event:
    """Freeze ``session`` between its SELECT and everything after it."""
    reached = asyncio.Event()
    original = session.execute
    seen = 0

    async def _execute(*args: Any, **kwargs: Any) -> Any:
        nonlocal seen
        result = await original(*args, **kwargs)
        seen += 1
        if seen == 1:
            reached.set()
            await gate.wait()
        return result

    session.execute = _execute  # type: ignore[method-assign]
    return reached


async def _active_warnings(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(Warning).where(Warning.active.is_(True))
    return int((await session.execute(stmt)).scalar_one())


async def _unwarn_log_rows(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(ModerationLog).where(ModerationLog.action == "unwarn")
    return int((await session.execute(stmt)).scalar_one())


@pytest.fixture
async def staged(tmp_path: Path) -> AsyncIterator[tuple[bool, bool, AsyncSession]]:
    """Run the staged race once; yield ``(winner, loser, session)``."""
    async with _two_sessions(tmp_path) as (winner_session, loser_session):
        await _seed_one_warning(winner_session)

        gate = asyncio.Event()
        reached = _park_after_first_statement(loser_session, gate)
        loser = asyncio.create_task(
            ModerationRepo(loser_session).remove_last_warning(
                user_id=_USER, chat_id=_CHAT, admin_id=_USER, reason="second item"
            )
        )
        await reached.wait()

        won = await ModerationRepo(winner_session).remove_last_warning(
            user_id=_USER, chat_id=_CHAT, admin_id=_USER, reason="first item"
        )
        await winner_session.commit()

        gate.set()
        lost = await loser
        await loser_session.commit()

        yield won, lost, winner_session


async def test_the_second_caller_is_refused(
    staged: tuple[bool, bool, AsyncSession],
) -> None:
    """The whole bug: both callers used to be told they lifted one."""
    won, lost, _ = staged

    assert won is True
    assert lost is False


async def test_the_warning_is_lifted_exactly_once(
    staged: tuple[bool, bool, AsyncSession],
) -> None:
    """The row itself — one warning in, one warning out."""
    _, _, session = staged

    assert await _active_warnings(session) == 0


async def test_the_refused_caller_writes_no_audit_row(
    staged: tuple[bool, bool, AsyncSession],
) -> None:
    """A refusal that logs an ``unwarn`` is the legacy defect #252(7)
    says was not ported — and the blind UPDATE quietly re-introduced it
    for every racing ``/unwarn``."""
    _, _, session = staged

    assert await _unwarn_log_rows(session) == 1


async def test_a_lone_caller_still_lifts_its_warning(tmp_path: Path) -> None:
    """The half that must not change."""
    async with _two_sessions(tmp_path) as (session, _):
        await _seed_one_warning(session)

        removed = await ModerationRepo(session).remove_last_warning(
            user_id=_USER, chat_id=_CHAT, admin_id=_USER, reason="only tap"
        )
        await session.commit()

        assert removed is True
        assert await _active_warnings(session) == 0
        assert await _unwarn_log_rows(session) == 1


async def test_a_caller_with_nothing_to_lift_is_still_refused(tmp_path: Path) -> None:
    """The no-warning path keeps refusing without consuming anything."""
    async with _two_sessions(tmp_path) as (session, _):
        removed = await ModerationRepo(session).remove_last_warning(
            user_id=_USER, chat_id=_CHAT, admin_id=_USER, reason="nothing here"
        )

        assert removed is False
        assert await _unwarn_log_rows(session) == 0
