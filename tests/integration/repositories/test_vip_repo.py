"""``VipRepo`` against an in-memory economy schema.

Two grant sources, two methods, four meaningful states each:
present-active / present-expired / present-future / missing.
Stage 12 ships read-only — writes still happen via legacy ``/buy``
and admin commands.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, UserGroupVip
from telegram_invite_bot.repositories.vip_repo import DEFAULT_VIP, VipRepo
from tests.integration.repositories._session import build_session

RepoFixture = tuple[VipRepo, AsyncSession]


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[RepoFixture]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as session:
        yield VipRepo(session), session


# ---------------------------------------------------------------------------
# Global VIP (users.vip_till)
# ---------------------------------------------------------------------------


async def test_global_vip_active_returns_default_profile(repo: RepoFixture) -> None:
    vip_repo, session = repo
    future = datetime(2024, 12, 31, tzinfo=UTC).timestamp()
    session.add(EconomyUser(user_id=42, balance=100, language="ru", vip_till=future))
    await session.commit()

    now = datetime(2024, 6, 15, tzinfo=UTC)
    profile = await vip_repo.get_active_profile(42, now=now)

    # Shared singleton — same instance every call, immutable.
    assert profile is DEFAULT_VIP
    assert profile.daily_bonus_percent == 15
    assert profile.tax_discount_percent == 50
    assert profile.message_bonus == 1


async def test_global_vip_expired_returns_none(repo: RepoFixture) -> None:
    vip_repo, session = repo
    past = datetime(2024, 1, 1, tzinfo=UTC).timestamp()
    session.add(EconomyUser(user_id=42, balance=100, language="ru", vip_till=past))
    await session.commit()

    now = datetime(2024, 6, 15, tzinfo=UTC)
    assert await vip_repo.get_active_profile(42, now=now) is None


async def test_global_vip_null_returns_none(repo: RepoFixture) -> None:
    """No grant ever (or cleared by admin) → ``vip_till IS NULL`` → None."""
    vip_repo, session = repo
    session.add(EconomyUser(user_id=42, balance=100, language="ru", vip_till=None))
    await session.commit()

    now = datetime(2024, 6, 15, tzinfo=UTC)
    assert await vip_repo.get_active_profile(42, now=now) is None


async def test_global_vip_missing_user_returns_none(repo: RepoFixture) -> None:
    """Wallet row absent → repo returns None rather than raising."""
    vip_repo, _ = repo
    assert await vip_repo.get_active_profile(99999, now=datetime(2024, 6, 15, tzinfo=UTC)) is None


async def test_global_vip_at_exact_deadline_treated_as_expired(repo: RepoFixture) -> None:
    """Legacy uses ``<= time.time()`` as the cutoff (``bot.py:13496``).
    Pin the boundary so a flip to ``<`` would surface here, not in
    production where users would silently lose a day of VIP."""
    vip_repo, session = repo
    deadline = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
    session.add(EconomyUser(user_id=42, balance=100, language="ru", vip_till=deadline.timestamp()))
    await session.commit()

    assert await vip_repo.get_active_profile(42, now=deadline) is None
    just_before = deadline - timedelta(seconds=1)
    assert await vip_repo.get_active_profile(42, now=just_before) is DEFAULT_VIP


# ---------------------------------------------------------------------------
# Group-scoped VIP (user_group_vip)
# ---------------------------------------------------------------------------


async def test_group_vip_active_returns_default_profile(repo: RepoFixture) -> None:
    vip_repo, session = repo
    future = datetime(2024, 12, 31, tzinfo=UTC).timestamp()
    session.add(UserGroupVip(user_id=42, group_id=-1001, vip_till=future))
    await session.commit()

    now = datetime(2024, 6, 15, tzinfo=UTC)
    assert await vip_repo.get_active_profile(42, now=now, group_id=-1001) is DEFAULT_VIP


async def test_group_vip_missing_row_returns_none(repo: RepoFixture) -> None:
    vip_repo, _ = repo
    now = datetime(2024, 6, 15, tzinfo=UTC)
    assert await vip_repo.get_active_profile(42, now=now, group_id=-1001) is None


# ---------------------------------------------------------------------------
# grant_global (Stage 28 writer)
# ---------------------------------------------------------------------------


async def test_grant_global_creates_row_when_wallet_missing(repo: RepoFixture) -> None:
    """If no wallet row exists (edge: tests forgot to seed, or admin
    /reset between purchase and /use) the UPSERT seeds with vip_till
    set. We assert the row appears with the right vip_till; the
    canonical wallet defaults (balance=100 from EconomyRepo) are NOT
    applied here on purpose — see grant_global docstring.
    """
    vip_repo, session = repo
    now = datetime(2024, 6, 15, tzinfo=UTC)

    granted = await vip_repo.grant_global(user_id=42, now=now, duration=timedelta(days=30))
    await session.commit()

    expected = now + timedelta(days=30)
    assert granted == expected
    row = await session.get(EconomyUser, 42)
    assert row is not None
    assert row.vip_till == expected.timestamp()


async def test_grant_global_stacks_on_top_of_an_active_grant(repo: RepoFixture) -> None:
    """#192: legacy ADDS the new term to the existing expiry
    (``bot.py:13442-13444``), it does not take a maximum. The port took
    ``MAX(existing, now + duration)``, which turns every re-purchase made
    while VIP is still active into 5000 COM for zero extra days — and
    ``👑 VIP статус`` is sold with ``stock=-1``, so re-purchasing is the
    normal case, not an edge one.
    """
    vip_repo, session = repo
    now = datetime(2024, 6, 15, tzinfo=UTC)
    active_till = datetime(2025, 6, 1, tzinfo=UTC)
    session.add(
        EconomyUser(user_id=42, balance=100, language="ru", vip_till=active_till.timestamp())
    )
    await session.commit()

    granted = await vip_repo.grant_global(user_id=42, now=now, duration=timedelta(days=30))
    await session.commit()

    expected = active_till + timedelta(days=30)
    assert granted == expected
    row = await session.get(EconomyUser, 42)
    assert row is not None
    assert row.vip_till == expected.timestamp()


async def test_grant_global_measures_from_now_when_the_old_grant_lapsed(
    repo: RepoFixture,
) -> None:
    """The other half of ``MAX(IFNULL(vip_till, 0), now)``: stacking is
    from the LATER of the stored expiry and now. A user whose VIP ran
    out in January must get a full 30 days from today, not 30 days
    counted from a date already in the past — which would hand them an
    expiry that has already elapsed.
    """
    vip_repo, session = repo
    lapsed = datetime(2024, 1, 1, tzinfo=UTC)
    now = datetime(2024, 6, 15, tzinfo=UTC)
    session.add(EconomyUser(user_id=42, balance=100, language="ru", vip_till=lapsed.timestamp()))
    await session.commit()

    granted = await vip_repo.grant_global(user_id=42, now=now, duration=timedelta(days=30))
    await session.commit()

    expected = now + timedelta(days=30)
    assert granted == expected
    row = await session.get(EconomyUser, 42)
    assert row is not None
    assert row.vip_till == expected.timestamp()


async def test_grant_global_promotes_null_vip_till(repo: RepoFixture) -> None:
    """``vip_till IS NULL`` (never-VIP wallet) is the case the
    ``IFNULL(vip_till, 0)`` coalesce inside grant_global exists for —
    without it, SQL arithmetic on NULL yields NULL and the grant would
    silently no-op for every first-ever VIP purchase.
    """
    vip_repo, session = repo
    session.add(EconomyUser(user_id=42, balance=100, language="ru", vip_till=None))
    await session.commit()

    now = datetime(2024, 6, 15, tzinfo=UTC)
    granted = await vip_repo.grant_global(user_id=42, now=now, duration=timedelta(days=30))
    await session.commit()

    expected = now + timedelta(days=30)
    assert granted == expected
    row = await session.get(EconomyUser, 42)
    assert row is not None
    assert row.vip_till == expected.timestamp()


async def test_grant_global_twice_adds_both_terms(repo: RepoFixture) -> None:
    """The end-to-end shape of the money bug: two purchases, two terms.
    Under the old MAX the second call was a no-op and the buyer paid
    5000 COM for nothing.
    """
    vip_repo, session = repo
    now = datetime(2024, 6, 15, tzinfo=UTC)

    first = await vip_repo.grant_global(user_id=42, now=now, duration=timedelta(days=30))
    # Same second, as an impatient double purchase would be.
    second = await vip_repo.grant_global(user_id=42, now=now, duration=timedelta(days=30))
    await session.commit()

    assert first == now + timedelta(days=30)
    assert second == now + timedelta(days=60)


async def test_global_and_group_vip_are_independent(repo: RepoFixture) -> None:
    """Holding global VIP must NOT auto-grant group VIP. A handler
    that calls with ``group_id=<chat>`` reads the per-chat row only;
    matches legacy's separate-source design."""
    vip_repo, session = repo
    future = datetime(2024, 12, 31, tzinfo=UTC).timestamp()
    # Only global granted.
    session.add(EconomyUser(user_id=42, balance=100, language="ru", vip_till=future))
    await session.commit()

    now = datetime(2024, 6, 15, tzinfo=UTC)
    assert await vip_repo.get_active_profile(42, now=now) is DEFAULT_VIP
    assert await vip_repo.get_active_profile(42, now=now, group_id=-1001) is None


async def test_naive_now_does_not_extend_an_expired_vip(
    repo: RepoFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same guard as ``PrivilegesRepo``: a naive ``now`` is coerced, not
    read as local time.

    Without it, a VIP that lapsed one second ago kept paying its
    ``message_bonus`` for three more hours on the MSK production host.
    """
    vip_repo, session = repo
    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    try:
        aware = datetime(2024, 6, 15, 12, 0, tzinfo=UTC)
        session.add(
            EconomyUser(
                user_id=42,
                balance=100,
                language="ru",
                vip_till=(aware - timedelta(seconds=1)).timestamp(),
            )
        )
        await session.commit()
        naive = aware.replace(tzinfo=None)

        records: list[str] = []
        handler_id = logger.add(records.append, level="ERROR", format="{message}")
        try:
            assert await vip_repo.get_active_profile(42, now=naive) is None
        finally:
            logger.remove(handler_id)
        assert len(records) == 1, records
        assert "VipRepo.get_active_profile" in records[0]
    finally:
        monkeypatch.undo()
        time.tzset()
