"""``EmojiBadgeRepo`` — the VIP cosmetic-badge store (#25).

These tests pin the CRUD surface (get / upsert / clear) plus the
load-bearing batch read :meth:`EmojiBadgeRepo.active_badges`, which the
``/top`` leaderboard uses to decorate names in one JOIN. The batch
method is the only place the repo applies the render-time VIP gate
(``economy.users.vip_till > now``); the per-user :meth:`get` is a pure
read that trusts the service layer to gate.

``_NOW`` and the two seeded timestamps are AWARE on purpose.
``active_badges`` compares ``now`` against ``vip_till`` (a legacy
``time.time()`` REAL) via ``.timestamp()``, and ``.timestamp()`` on a
naive value reads the wall clock in the HOST's zone. Seeding both
sides from the same naive constant made the two errors cancel, so the
tests passed while stating nothing about the real frame — the same
trap ``test_xp_boost`` fell into. Aware constants make the epoch the
test writes and the epoch the repo compares the same number.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.repositories.emoji_badge_repo import EmojiBadgeRepo
from tests.integration.repositories._session import build_session

_NOW = datetime(2026, 6, 5, 12, 0, 0, tzinfo=UTC)
_FUTURE_TS = datetime(2026, 7, 5, 12, 0, 0, tzinfo=UTC).timestamp()
_PAST_TS = datetime(2026, 5, 5, 12, 0, 0, tzinfo=UTC).timestamp()


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as s:
        yield s


async def _seed_user(session: AsyncSession, user_id: int, vip_till: float | None) -> None:
    session.add(EconomyUser(user_id=user_id, balance=0, language="ru", vip_till=vip_till))
    await session.commit()


async def test_get_missing_returns_none(session: AsyncSession) -> None:
    repo = EmojiBadgeRepo(session)
    assert await repo.get(42) is None


async def test_upsert_then_get(session: AsyncSession) -> None:
    repo = EmojiBadgeRepo(session)
    await repo.upsert(user_id=42, emoji="👑", now=_NOW)
    await session.commit()
    assert await repo.get(42) == "👑"


async def test_upsert_replaces_existing(session: AsyncSession) -> None:
    """Re-equip overwrites the single per-user row, not a second insert."""
    repo = EmojiBadgeRepo(session)
    await repo.upsert(user_id=42, emoji="👑", now=_NOW)
    await repo.upsert(user_id=42, emoji="💎", now=_NOW)
    await session.commit()
    assert await repo.get(42) == "💎"


async def test_clear_is_idempotent(session: AsyncSession) -> None:
    repo = EmojiBadgeRepo(session)
    await repo.upsert(user_id=42, emoji="👑", now=_NOW)
    await session.commit()
    await repo.clear(42)
    await repo.clear(42)  # second clear is a no-op, not an error
    await session.commit()
    assert await repo.get(42) is None


async def test_active_badges_empty_input(session: AsyncSession) -> None:
    repo = EmojiBadgeRepo(session)
    assert await repo.active_badges([], now=_NOW) == {}


async def test_active_badges_only_vip_active(session: AsyncSession) -> None:
    """A user shows iff they have a badge AND a live VIP grant."""
    repo = EmojiBadgeRepo(session)
    # 1: VIP-active + badge → shows.
    await _seed_user(session, 1, _FUTURE_TS)
    await repo.upsert(user_id=1, emoji="👑", now=_NOW)
    # 2: lapsed VIP + badge → hidden.
    await _seed_user(session, 2, _PAST_TS)
    await repo.upsert(user_id=2, emoji="💎", now=_NOW)
    # 3: never-VIP (vip_till NULL) + badge → hidden.
    await _seed_user(session, 3, None)
    await repo.upsert(user_id=3, emoji="🔥", now=_NOW)
    # 4: VIP-active but no badge → absent.
    await _seed_user(session, 4, _FUTURE_TS)
    await session.commit()

    result = await repo.active_badges([1, 2, 3, 4], now=_NOW)
    assert result == {1: "👑"}


async def test_active_badges_respects_id_filter(session: AsyncSession) -> None:
    """Only requested ids come back even when others qualify."""
    repo = EmojiBadgeRepo(session)
    await _seed_user(session, 1, _FUTURE_TS)
    await repo.upsert(user_id=1, emoji="👑", now=_NOW)
    await _seed_user(session, 2, _FUTURE_TS)
    await repo.upsert(user_id=2, emoji="💎", now=_NOW)
    await session.commit()

    result = await repo.active_badges([1], now=_NOW)
    assert result == {1: "👑"}
