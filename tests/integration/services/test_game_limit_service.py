"""``GameLimitService`` — persistent anti-abuse cap policy (L-25).

Verifies the three caps over a real ``game_plays`` table:

* a fresh user is allowed;
* a play within ``COOLDOWN_SEC`` blocks with COOLDOWN + a wait_sec;
* past the cooldown, the per-hour cap blocks at ``MAX_PER_HOUR``;
* the per-day cap blocks at ``MAX_PER_DAY`` once the hour window has
  rolled past;
* ``record`` persists across a fresh service instance (the whole point
  vs the in-memory limiter — survives a "restart").
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.repositories.game_limits_repo import GameLimitsRepo
from telegram_invite_bot.services.game_limit_service import (
    COOLDOWN_SEC,
    MAX_PER_DAY,
    MAX_PER_HOUR,
    GameAbuse,
    GameLimitService,
)

_NOW = datetime(2026, 6, 10, 12, 0, 0)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


@pytest.fixture
def service(session: AsyncSession) -> GameLimitService:
    return GameLimitService(GameLimitsRepo(session))


async def test_fresh_user_allowed(service: GameLimitService) -> None:
    check = await service.check(1, now=_NOW)
    assert check.allowed is True


async def test_cooldown_blocks(service: GameLimitService, session: AsyncSession) -> None:
    await service.record(1, game="roulette", now=_NOW)
    await session.commit()

    check = await service.check(1, now=_NOW + timedelta(seconds=COOLDOWN_SEC - 10))
    assert check.allowed is False
    assert check.reason is GameAbuse.COOLDOWN
    assert check.wait_sec == 10


async def test_cooldown_clears_after_window(
    service: GameLimitService, session: AsyncSession
) -> None:
    await service.record(1, game="roulette", now=_NOW)
    await session.commit()
    # Just past the cooldown, single play → allowed again.
    check = await service.check(1, now=_NOW + timedelta(seconds=COOLDOWN_SEC + 1))
    assert check.allowed is True


async def test_per_hour_cap(service: GameLimitService, session: AsyncSession) -> None:
    # Stamp MAX_PER_HOUR plays spaced past the cooldown but inside the
    # hour window, then a check at the end must hit the hour cap.
    base = _NOW
    for i in range(MAX_PER_HOUR):
        when = base + timedelta(seconds=i * (COOLDOWN_SEC + 1))
        await service.record(1, game="roulette", now=when)
    await session.commit()

    # A check far enough past the last play to clear the cooldown but
    # still within the hour of the first play.
    last = base + timedelta(seconds=(MAX_PER_HOUR - 1) * (COOLDOWN_SEC + 1))
    check = await service.check(1, now=last + timedelta(seconds=COOLDOWN_SEC + 1))
    assert check.allowed is False
    assert check.reason is GameAbuse.MAX_PER_HOUR


async def test_per_day_cap(service: GameLimitService, session: AsyncSession) -> None:
    # Spread MAX_PER_DAY plays across the day so no rolling hour holds
    # MAX_PER_HOUR of them — isolates the day cap from the hour cap.
    base = _NOW
    spacing = timedelta(hours=20) / MAX_PER_DAY
    for i in range(MAX_PER_DAY):
        await service.record(1, game="roulette", now=base + spacing * i)
    await session.commit()

    last = base + spacing * (MAX_PER_DAY - 1)
    check = await service.check(1, now=last + timedelta(seconds=COOLDOWN_SEC + 1))
    assert check.allowed is False
    assert check.reason is GameAbuse.MAX_PER_DAY


async def test_record_survives_new_service_instance(
    session: AsyncSession,
) -> None:
    # The persistence guarantee: a new service over the same DB still
    # sees the recorded play (in-memory limiter would forget it).
    s1 = GameLimitService(GameLimitsRepo(session))
    await s1.record(1, game="roulette", now=_NOW)
    await session.commit()

    s2 = GameLimitService(GameLimitsRepo(session))
    check = await s2.check(1, now=_NOW + timedelta(seconds=10))
    assert check.allowed is False
    assert check.reason is GameAbuse.COOLDOWN


async def test_include_cooldown_false_skips_only_the_cooldown(
    service: GameLimitService, session: AsyncSession
) -> None:
    """#1664: the PvP challenge commands opt out of the 180 s clock.

    A challenge is an invitation, not a settled play — it resolves
    minutes later and only if the other seat agrees — so it answers to
    the two window caps and not to a clock meant for self-served plays
    that settle the instant they are made. The windows are deliberately
    NOT exempt: the budget is shared, and the counts still come back.
    """
    await service.record(1, game="roulette", now=_NOW)
    await session.commit()

    inside = _NOW + timedelta(seconds=COOLDOWN_SEC - 10)
    assert (await service.check(1, now=inside)).reason is GameAbuse.COOLDOWN

    exempt = await service.check(1, now=inside, include_cooldown=False)
    assert exempt.allowed is True
    assert exempt.plays_in_hour == 1
    assert exempt.plays_in_day == 1


async def test_include_cooldown_false_still_answers_to_the_window_caps(
    service: GameLimitService, session: AsyncSession
) -> None:
    """The exemption is one cap wide, not a bypass of the budget."""
    for i in range(MAX_PER_HOUR):
        await service.record(1, game="roulette", now=_NOW - timedelta(seconds=i))
    await session.commit()

    check = await service.check(1, now=_NOW, include_cooldown=False)
    assert check.allowed is False
    assert check.reason is GameAbuse.MAX_PER_HOUR
    assert check.plays_in_hour == MAX_PER_HOUR
