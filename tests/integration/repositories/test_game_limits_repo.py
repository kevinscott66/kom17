"""``GameLimitsRepo`` against a real SQLite file (L-25).

Covers the persistence primitives the anti-abuse policy composes:

* ``record`` appends a stamp; ``last_play_at`` returns the latest;
* ``last_play_at`` is None for a user that never played;
* ``count_since`` respects the rolling-window ``>= since`` boundary;
* ``count_since`` aggregates across games (caps are shared);
* ``delete_older_than`` prunes only stamps strictly before the cutoff.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.game_limits import GamePlay
from telegram_invite_bot.repositories.game_limits_repo import GameLimitsRepo
from tests.integration.repositories._session import build_session

RepoFixture = tuple[GameLimitsRepo, AsyncSession]

_NOW = datetime(2026, 6, 10, 12, 0, 0)


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[RepoFixture]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as session:
        yield GameLimitsRepo(session), session


async def _total(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(GamePlay))).scalar_one())


async def test_record_then_last_play(repo: RepoFixture) -> None:
    repo_, session = repo
    await repo_.record(1, game="roulette", now=_NOW - timedelta(minutes=5))
    await repo_.record(1, game="roulette", now=_NOW)
    await session.commit()

    assert await repo_.last_play_at(1) == _NOW


async def test_last_play_none_for_unknown(repo: RepoFixture) -> None:
    repo_, _ = repo
    assert await repo_.last_play_at(999) is None


async def test_count_since_boundary(repo: RepoFixture) -> None:
    repo_, session = repo
    await repo_.record(1, game="roulette", now=_NOW - timedelta(hours=2))  # outside hour
    await repo_.record(1, game="roulette", now=_NOW - timedelta(minutes=30))  # inside hour
    await repo_.record(1, game="duel", now=_NOW)  # inside hour, different game
    await session.commit()

    within_hour = await repo_.count_since(1, since=_NOW - timedelta(hours=1))
    assert within_hour == 2  # cross-game aggregate, hour-old excluded

    within_day = await repo_.count_since(1, since=_NOW - timedelta(days=1))
    assert within_day == 3


async def test_count_since_inclusive_at_boundary(repo: RepoFixture) -> None:
    repo_, session = repo
    since = _NOW - timedelta(hours=1)
    await repo_.record(1, game="roulette", now=since)  # exactly at boundary → counted
    await session.commit()
    assert await repo_.count_since(1, since=since) == 1


async def test_delete_older_than(repo: RepoFixture) -> None:
    repo_, session = repo
    await repo_.record(1, game="roulette", now=_NOW - timedelta(days=3))  # stale
    await repo_.record(1, game="roulette", now=_NOW - timedelta(hours=1))  # fresh
    await session.commit()

    cutoff = _NOW - timedelta(days=2)
    deleted = await repo_.delete_older_than(cutoff)
    await session.commit()

    assert deleted == 1
    assert await _total(session) == 1
    assert await repo_.last_play_at(1) == _NOW - timedelta(hours=1)
