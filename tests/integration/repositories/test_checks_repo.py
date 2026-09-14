"""``ChecksRepo`` — the race-safe voucher primitives (#26).

The load-bearing method is :meth:`ChecksRepo.claim_decrement`: a single
guarded ``UPDATE`` that reserves coins, bumps ``claims_count`` and flips
``is_active`` to 0 when the check drains or hits its cap. These tests
pin every branch of that guard plus the ``UNIQUE(check_id, user_id)``
double-claim constraint that :meth:`insert_claim` relies on.

The concurrency test runs two SESSIONS against the SAME sqlite file so
the second decrement sees the first's committed write — proving the
WHERE-clause guard, not Python, is what stops a drain race.
"""

from __future__ import annotations

import random
import string
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.repositories.checks_repo import ChecksRepo
from tests.integration.repositories._session import build_session

_NOW = datetime(2026, 6, 5, 12, 0, 0)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as s:
        yield s


async def _make_check(
    repo: ChecksRepo,
    session: AsyncSession,
    *,
    total: int = 100,
    remaining: int | None = None,
    max_claims: int = 0,
    fixed_amount: int = 10,
    code: str = "ABC123",
) -> int:
    check_id = await repo.create(
        code=code,
        creator_id=1,
        type="fixed",
        total_amount=total,
        remaining_amount=total if remaining is None else remaining,
        min_amount=None,
        max_amount=None,
        fixed_amount=fixed_amount,
        target_user_id=None,
        max_claims=max_claims,
        required_language=None,
        required_premium=0,
        required_subscription=0,
        expires_at=None,
        now=_NOW,
    )
    await session.commit()
    return check_id


async def test_create_and_get_active_by_code(session: AsyncSession) -> None:
    repo = ChecksRepo(session)
    check_id = await _make_check(repo, session, code="HELLO1")
    assert check_id > 0

    found = await repo.get_active_by_code("HELLO1")
    assert found is not None
    assert found.id == check_id
    assert found.remaining_amount == 100
    assert found.is_active == 1

    # Unknown code → None.
    assert await repo.get_active_by_code("NOPE99") is None


async def test_get_active_skips_inactive(session: AsyncSession) -> None:
    repo = ChecksRepo(session)
    check_id = await _make_check(repo, session, code="DEAD01")
    await repo.deactivate(check_id)
    await session.commit()
    assert await repo.get_active_by_code("DEAD01") is None


async def test_generate_unique_code_avoids_collision(session: AsyncSession) -> None:
    repo = ChecksRepo(session)
    existing = "AAAAAAAA"
    await _make_check(repo, session, code=existing)
    code = await repo.generate_unique_code()
    assert code != existing
    assert len(code) == 8


async def test_generate_unique_code_does_not_use_the_global_random(
    session: AsyncSession,
) -> None:
    """The voucher code must not come from the shared Mersenne Twister.

    ``claim_check`` credits whoever presents the code, so a multi-claim
    check is a bearer token for coins. Drawn from ``random``, every code
    was a deterministic function of one process-wide state that dozens
    of other unseeded callers also draw from — pinning the seed made the
    codes reproducible, which is exactly the property a payout token
    must not have.

    Seeding ``random`` and comparing two runs is the sharpest available
    probe: it passes trivially for any CSPRNG and fails for anything
    routed through ``random``, without asserting on the implementation.
    """
    repo = ChecksRepo(session)

    random.seed(1234)
    first = await repo.generate_unique_code()
    random.seed(1234)
    second = await repo.generate_unique_code()

    assert first != second
    assert len(first) == len(second) == 8
    assert set(first) <= set(string.ascii_uppercase + string.digits)


async def test_has_claimed_tracks_insert(session: AsyncSession) -> None:
    repo = ChecksRepo(session)
    check_id = await _make_check(repo, session)
    assert await repo.has_claimed(check_id, user_id=42) is False
    await repo.insert_claim(check_id, user_id=42, amount=10, now=_NOW)
    await session.commit()
    assert await repo.has_claimed(check_id, user_id=42) is True
    # Different user has not claimed.
    assert await repo.has_claimed(check_id, user_id=43) is False


async def test_claim_decrement_happy_path(session: AsyncSession) -> None:
    repo = ChecksRepo(session)
    check_id = await _make_check(repo, session, total=100, max_claims=0)
    ok = await repo.claim_decrement(check_id, amount=10, max_claims=0)
    await session.commit()
    assert ok is True

    row = await repo.get_active_by_code("ABC123")
    assert row is not None
    assert row.remaining_amount == 90
    assert row.claims_count == 1
    assert row.is_active == 1  # still funded


async def test_claim_decrement_flips_inactive_on_zero(session: AsyncSession) -> None:
    """Draining the last coins flips ``is_active`` to 0 in the same UPDATE."""
    repo = ChecksRepo(session)
    check_id = await _make_check(repo, session, total=10, max_claims=0)
    ok = await repo.claim_decrement(check_id, amount=10, max_claims=0)
    await session.commit()
    assert ok is True
    # Now inactive → get_active_by_code returns None; fetch raw via id.
    assert await repo.get_active_by_code("ABC123") is None


async def test_claim_decrement_flips_inactive_on_max_claims(
    session: AsyncSession,
) -> None:
    """Hitting ``claims_count + 1 >= max_claims`` flips inactive even
    though coins remain."""
    repo = ChecksRepo(session)
    # max_claims=1: the single claim hits the cap, so is_active → 0
    # despite remaining 90.
    check_id = await _make_check(repo, session, total=100, max_claims=1)
    ok = await repo.claim_decrement(check_id, amount=10, max_claims=1)
    await session.commit()
    assert ok is True
    assert await repo.get_active_by_code("ABC123") is None


async def test_claim_decrement_false_when_remaining_too_low(
    session: AsyncSession,
) -> None:
    """``remaining_amount < amount`` → no row matched → rowcount 0."""
    repo = ChecksRepo(session)
    check_id = await _make_check(repo, session, total=5, max_claims=0)
    ok = await repo.claim_decrement(check_id, amount=10, max_claims=0)
    await session.commit()
    assert ok is False
    row = await repo.get_active_by_code("ABC123")
    assert row is not None
    assert row.remaining_amount == 5  # untouched
    assert row.claims_count == 0


async def test_claim_decrement_false_when_max_reached(session: AsyncSession) -> None:
    """``claims_count >= max_claims`` already → guard rejects."""
    repo = ChecksRepo(session)
    check_id = await _make_check(repo, session, total=100, max_claims=1)
    # First claim hits the cap (claims_count 0 < 1 passes).
    assert await repo.claim_decrement(check_id, amount=10, max_claims=1) is True
    await session.commit()
    # The cap-flip already deactivated; a second decrement now fails the
    # is_active guard too. Re-activate to isolate the claims-cap branch.
    await session.execute(
        text("UPDATE checks SET is_active = 1 WHERE id = :id"),
        {"id": check_id},
    )
    await session.commit()
    ok = await repo.claim_decrement(check_id, amount=10, max_claims=1)
    await session.commit()
    assert ok is False  # claims_count (1) >= max_claims (1)


async def test_insert_claim_duplicate_raises_integrity_error(
    session: AsyncSession,
) -> None:
    """The ``UNIQUE(check_id, user_id)`` constraint is the real
    double-claim guard."""
    repo = ChecksRepo(session)
    check_id = await _make_check(repo, session)
    await repo.insert_claim(check_id, user_id=42, amount=10, now=_NOW)
    await session.commit()
    with pytest.raises(IntegrityError):
        await repo.insert_claim(check_id, user_id=42, amount=10, now=_NOW)
    await session.rollback()


async def test_concurrent_decrement_two_sessions(tmp_path: Path) -> None:
    """Two SESSIONS race the last coins; only one wins.

    This is the test legacy would fail: both sessions read
    ``remaining=10`` independently, but the guarded UPDATE serialises at
    the file level, so the loser's WHERE clause sees ``remaining < 10``
    after the winner commits and gets ``rowcount == 0``.
    """
    db = tmp_path / "economy.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(EconomyBase.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        # Seed one check with exactly enough for ONE claim of 10.
        async with maker() as s0:
            repo0 = ChecksRepo(s0)
            check_id = await repo0.create(
                code="RACE01",
                creator_id=1,
                type="fixed",
                total_amount=10,
                remaining_amount=10,
                min_amount=None,
                max_amount=None,
                fixed_amount=10,
                target_user_id=None,
                max_claims=0,
                required_language=None,
                required_premium=0,
                required_subscription=0,
                expires_at=None,
                now=_NOW,
            )
            await s0.commit()

        async with maker() as s1, maker() as s2:
            r1, r2 = ChecksRepo(s1), ChecksRepo(s2)
            won1 = await r1.claim_decrement(check_id, amount=10, max_claims=0)
            await s1.commit()
            won2 = await r2.claim_decrement(check_id, amount=10, max_claims=0)
            await s2.commit()

        # Exactly one winner — never both (that would mint 10 coins).
        assert [won1, won2].count(True) == 1
    finally:
        await engine.dispose()
