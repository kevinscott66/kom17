"""Unit coverage for :func:`services.xp_boost.active_xp_multiplier` (L-21).

The resolver reads the ``xp_boost`` privilege row back and returns the
multiplier the per-message earn path multiplies coins by. Tests use a
real SQLite economy DB (same fixture shape as
``test_inventory_use_service``) plus the module-level TTL cache, which is
cleared between tests so cached answers don't bleed across cases.

``_NOW`` is AWARE on purpose. The resolver hands ``now`` to
``PrivilegesRepo.get_active``, which compares it against a legacy
``time.time()`` REAL via ``.timestamp()`` — and ``.timestamp()`` on a
naive value reads the wall clock in the HOST's zone. These tests used
to seed ``expires_at`` from the same naive ``_NOW``, so both sides were
wrong by the same 3 hours on an MSK host and the mismatch was
invisible. Seeding from an aware instant is what makes the epoch the
test writes and the epoch the repo compares the same number.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import UserPrivilege
from telegram_invite_bot.services import xp_boost
from telegram_invite_bot.services.xp_boost import active_xp_multiplier, clear_cache

_NOW = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
_USER = 7001


@pytest.fixture(autouse=True)
def _clear_cache() -> AsyncIterator[None]:
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


async def _grant(session: AsyncSession, *, multiplier: object, expires_at: float) -> None:
    session.add(
        UserPrivilege(
            user_id=_USER,
            privilege_type="xp_boost",
            group_id=0,
            expires_at=expires_at,
            value=json.dumps({"multiplier": multiplier}),
        )
    )
    await session.commit()


async def test_no_row_returns_baseline(session: AsyncSession) -> None:
    assert await active_xp_multiplier(session, _USER, _NOW) == 1.0


async def test_active_boost_returns_stored_multiplier(session: AsyncSession) -> None:
    future = (_NOW + timedelta(hours=1)).timestamp()
    await _grant(session, multiplier=2, expires_at=future)
    assert await active_xp_multiplier(session, _USER, _NOW) == 2.0


async def test_expired_boost_returns_baseline(session: AsyncSession) -> None:
    past = (_NOW - timedelta(minutes=1)).timestamp()
    await _grant(session, multiplier=2, expires_at=past)
    assert await active_xp_multiplier(session, _USER, _NOW) == 1.0


async def test_sub_baseline_multiplier_is_clamped(session: AsyncSession) -> None:
    # A stored value below 1.0 must never tax the user below baseline.
    future = (_NOW + timedelta(hours=1)).timestamp()
    await _grant(session, multiplier=0.5, expires_at=future)
    assert await active_xp_multiplier(session, _USER, _NOW) == 1.0


async def test_malformed_payload_returns_baseline(session: AsyncSession) -> None:
    future = (_NOW + timedelta(hours=1)).timestamp()
    session.add(
        UserPrivilege(
            user_id=_USER,
            privilege_type="xp_boost",
            group_id=0,
            expires_at=future,
            value="not json",
        )
    )
    await session.commit()
    assert await active_xp_multiplier(session, _USER, _NOW) == 1.0


async def test_boolean_multiplier_rejected(session: AsyncSession) -> None:
    # bool is an int subclass — guard against {"multiplier": true}.
    future = (_NOW + timedelta(hours=1)).timestamp()
    await _grant(session, multiplier=True, expires_at=future)
    assert await active_xp_multiplier(session, _USER, _NOW) == 1.0


async def test_result_is_cached_within_ttl(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin monotonic so the cached value is reused on the second call.
    monkeypatch.setattr(xp_boost.time, "monotonic", lambda: 1000.0)
    future = (_NOW + timedelta(hours=1)).timestamp()
    await _grant(session, multiplier=3, expires_at=future)
    assert await active_xp_multiplier(session, _USER, _NOW) == 3.0
    # Delete the row underneath the cache — within the TTL window the
    # cached 3.0 must still be returned (proves the read was cached).
    await session.execute(UserPrivilege.__table__.delete())
    await session.commit()
    assert await active_xp_multiplier(session, _USER, _NOW) == 3.0


async def test_cache_refreshes_after_ttl(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(xp_boost.time, "monotonic", lambda: clock["now"])
    future = (_NOW + timedelta(hours=1)).timestamp()
    await _grant(session, multiplier=2, expires_at=future)
    assert await active_xp_multiplier(session, _USER, _NOW) == 2.0
    # Drop the row, advance past the cache TTL → re-read sees no row.
    await session.execute(UserPrivilege.__table__.delete())
    await session.commit()
    clock["now"] = 1000.0 + xp_boost._CACHE_TTL_SECONDS + 1.0
    assert await active_xp_multiplier(session, _USER, _NOW) == 1.0


@pytest.mark.parametrize("multiplier", [float("inf"), float("nan")])
async def test_non_finite_multiplier_returns_baseline(
    session: AsyncSession, multiplier: float
) -> None:
    """``json.dumps`` writes bare ``Infinity``/``NaN`` and ``json.loads``
    reads them back, so a corrupt row really can carry one. ``inf`` is
    the dangerous half — it clears every ``>= 1.0`` check and would
    multiply the user's coin earnings without bound.
    """
    future = (_NOW + timedelta(hours=1)).timestamp()
    await _grant(session, multiplier=multiplier, expires_at=future)
    assert await active_xp_multiplier(session, _USER, _NOW) == 1.0


async def test_absurd_finite_multiplier_returns_baseline(
    session: AsyncSession,
) -> None:
    # Past the sanity ceiling the row is corruption, not a promotion.
    future = (_NOW + timedelta(hours=1)).timestamp()
    await _grant(session, multiplier=xp_boost._MAX_MULTIPLIER + 1, expires_at=future)
    assert await active_xp_multiplier(session, _USER, _NOW) == 1.0


async def test_multiplier_at_the_ceiling_is_honoured(session: AsyncSession) -> None:
    # The bound is inclusive — a legitimately generous boost still works.
    future = (_NOW + timedelta(hours=1)).timestamp()
    await _grant(session, multiplier=xp_boost._MAX_MULTIPLIER, expires_at=future)
    assert await active_xp_multiplier(session, _USER, _NOW) == xp_boost._MAX_MULTIPLIER


async def test_cache_entry_never_outlives_the_boost_it_describes(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lapsed boost must stop paying immediately, not at the next TTL.

    The cache holds only the resolved float, so an entry written one
    second before expiry would otherwise keep DOUBLING every coin credit
    for the rest of the window — real money, out of the owner's pocket,
    for a grant the user no longer holds. Legacy never did that: its
    cache entry expired exactly with the boost
    (``bot.py:13574-13577``), ``cache_get`` evicted on that TTL
    (``bot.py:4352``), and the reader re-checked the stored ``expires``
    on top of both (``bot.py:13711-13712``).

    Five seconds of boost, ten seconds later, well inside the thirty
    second TTL: the answer must be baseline.
    """
    clock = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    await _grant(session, multiplier=2, expires_at=(_NOW + timedelta(seconds=5)).timestamp())
    assert await active_xp_multiplier(session, _USER, _NOW) == 2.0

    clock["now"] = 1010.0
    assert xp_boost._CACHE_TTL_SECONDS > 10.0
    assert await active_xp_multiplier(session, _USER, _NOW + timedelta(seconds=10)) == 1.0


async def test_never_expiring_grant_still_caches_for_the_whole_ttl(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``expires_at <= 0`` means "never expires" — there is no deadline to
    # clamp against, so such a row keeps the full read-amplification
    # damper rather than losing it to the new guard.
    clock = {"now": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["now"])
    await _grant(session, multiplier=2, expires_at=0.0)
    assert await active_xp_multiplier(session, _USER, _NOW) == 2.0

    await session.execute(delete(UserPrivilege))
    await session.commit()
    clock["now"] = 1000.0 + xp_boost._CACHE_TTL_SECONDS - 1.0
    assert await active_xp_multiplier(session, _USER, _NOW) == 2.0
