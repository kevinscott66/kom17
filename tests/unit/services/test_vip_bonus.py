"""Unit coverage for :func:`services.vip_bonus.active_message_bonus` (#490).

The resolver answers "how many extra coins does this user's VIP grant
add to a message reward" — legacy
``ItemEffects.get_message_reward_bonus`` (``bot.py:13537-13542``). Tests
use a real SQLite economy DB (same fixture shape as
``test_xp_boost``) plus the module-level TTL cache, cleared between
tests so cached answers don't bleed across cases.

``_NOW`` is AWARE on purpose. The resolver hands ``now`` to
``VipRepo.get_active_profile``, which compares it against ``vip_till``
(a legacy ``time.time()`` REAL) via ``.timestamp()`` — and
``.timestamp()`` on a naive value reads the wall clock in the HOST's
zone. These tests used to seed ``vip_till`` from the same naive
``_NOW``, so both sides were wrong by the same 3 hours on an MSK host
and the mismatch was invisible. Seeding from an aware instant is what
makes the epoch the test writes and the epoch the repo compares the
same number.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, UserGroupVip
from telegram_invite_bot.services.vip_bonus import active_message_bonus, clear_cache

_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
_USER = 8001


@pytest.fixture(autouse=True)
def _clear_cache() -> Iterator[None]:
    clear_cache()
    yield
    clear_cache()


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


async def _wallet(session: AsyncSession, *, vip_till: float | None) -> None:
    session.add(EconomyUser(user_id=_USER, balance=100, vip_till=vip_till))
    await session.commit()


async def test_active_vip_grant_adds_the_perk(session: AsyncSession) -> None:
    await _wallet(session, vip_till=(_NOW + timedelta(days=1)).timestamp())
    assert await active_message_bonus(session, _USER, _NOW) == 1


async def test_expired_vip_grant_adds_nothing(session: AsyncSession) -> None:
    await _wallet(session, vip_till=(_NOW - timedelta(seconds=1)).timestamp())
    assert await active_message_bonus(session, _USER, _NOW) == 0


async def test_user_who_was_never_vip_adds_nothing(session: AsyncSession) -> None:
    await _wallet(session, vip_till=None)
    assert await active_message_bonus(session, _USER, _NOW) == 0


async def test_unknown_user_adds_nothing(session: AsyncSession) -> None:
    assert await active_message_bonus(session, 999_999, _NOW) == 0


async def test_group_scoped_vip_is_not_read(session: AsyncSession) -> None:
    """Legacy called ``get_vip_profile(user_id)`` with one argument, which
    reads ``users.vip_till`` only — a per-chat grant never fed the
    message reward."""
    await _wallet(session, vip_till=None)
    session.add(
        UserGroupVip(
            user_id=_USER,
            group_id=-100,
            vip_till=(_NOW + timedelta(days=1)).timestamp(),
        )
    )
    await session.commit()
    assert await active_message_bonus(session, _USER, _NOW) == 0


async def test_answer_is_cached_across_calls(session: AsyncSession) -> None:
    """The no-VIP answer is cached too — that is the whole point on a path
    that runs once per group message."""
    await _wallet(session, vip_till=None)
    assert await active_message_bonus(session, _USER, _NOW) == 0

    # Grant VIP behind the cache's back: the stale 0 must still be served.
    row = await session.get(EconomyUser, _USER)
    assert row is not None
    row.vip_till = (_NOW + timedelta(days=1)).timestamp()
    await session.commit()
    assert await active_message_bonus(session, _USER, _NOW) == 0

    clear_cache()
    assert await active_message_bonus(session, _USER, _NOW) == 1
