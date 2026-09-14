"""``PromoRepo`` + ``PromoService`` — race-safe gift-code primitives (L-96).

The load-bearing methods are :meth:`PromoRepo.reserve_use` (the global
``max_uses`` cap, held by one guarded UPDATE) and
:meth:`PromoRepo.insert_redemption_once` (the per-user-once conditional
INSERT). These tests pin every branch of both, plus the full
:class:`PromoService` redeem flow over a real sqlite file so the
credit + ledger composition is exercised end to end.

The concurrency tests run two SESSIONS against the SAME sqlite file so
the second reserve/insert sees the first's committed write — proving the
SQL guard, not Python, stops a double-spend.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase

# Importing the promo models registers their tables on ``EconomyBase``
# so ``create_all`` builds them. The economy_repo import pulls in the
# wallet model the same way.
from telegram_invite_bot.db.models.promo import PromoCode  # noqa: F401
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.promo_repo import PromoRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.promo_service import (
    MAX_PROMO_MINT_TOTAL,
    MAX_PROMO_REWARD_COINS,
    CreateOutcome,
    PromoService,
    RedeemOutcome,
)
from telegram_invite_bot.utils.economy import _MAX_AMOUNT
from tests.integration.repositories._session import build_session

_NOW = datetime(2026, 6, 10, 12, 0, 0)
_REDEEMER_1986 = 8601
_OTHER_1986 = 8602


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as s:
        yield s


def _service(session: AsyncSession) -> PromoService:
    return PromoService(
        PromoRepo(session),
        EconomyRepo(session),
        TransactionsRepo(session),
        session,
    )


async def _seed_wallet(session: AsyncSession, user_id: int) -> None:
    await EconomyRepo(session).get_or_create(user_id, now=_NOW)
    await session.commit()


# --------------------------------------------------------------------------
# Repo-level guards
# --------------------------------------------------------------------------


async def test_reserve_use_respects_max_uses(session: AsyncSession) -> None:
    repo = PromoRepo(session)
    code_id = await repo.create_code(
        code="GIFT",
        reward_coins=10,
        max_uses=2,
        per_user_once=False,
        created_by=1,
        now=_NOW,
    )
    await session.commit()

    assert await repo.reserve_use(code_id, 2) is True
    assert await repo.reserve_use(code_id, 2) is True
    # Third reserve fails the cap and the code is now inactive.
    assert await repo.reserve_use(code_id, 2) is False
    promo = await repo.get_by_code("GIFT")
    assert promo is not None
    assert promo.used_count == 2
    assert promo.active is False


async def test_reserve_use_unlimited(session: AsyncSession) -> None:
    repo = PromoRepo(session)
    code_id = await repo.create_code(
        code="INF",
        reward_coins=5,
        max_uses=0,
        per_user_once=False,
        created_by=1,
        now=_NOW,
    )
    await session.commit()
    for _ in range(5):
        assert await repo.reserve_use(code_id, 0) is True
    promo = await repo.get_by_code("INF")
    assert promo is not None
    assert promo.active is True  # unlimited never auto-deactivates


async def test_insert_redemption_once_blocks_second(session: AsyncSession) -> None:
    repo = PromoRepo(session)
    code_id = await repo.create_code(
        code="ONCE",
        reward_coins=10,
        max_uses=0,
        per_user_once=True,
        created_by=1,
        now=_NOW,
    )
    await session.commit()
    assert await repo.insert_redemption_once(code_id, 42, 10, _NOW) is True
    # Same user again → conditional insert finds the row, no second row.
    assert await repo.insert_redemption_once(code_id, 42, 10, _NOW) is False
    assert await repo.redemption_count(code_id) == 1
    # A different user still succeeds.
    assert await repo.insert_redemption_once(code_id, 43, 10, _NOW) is True
    assert await repo.redemption_count(code_id) == 2


# --------------------------------------------------------------------------
# Service flow
# --------------------------------------------------------------------------


async def test_create_invalid_and_duplicate(session: AsyncSession) -> None:
    svc = _service(session)
    assert (
        await svc.create_code(
            code="",
            reward_coins=10,
            max_uses=5,
            per_user_once=True,
            created_by=1,
        )
    ).outcome is CreateOutcome.INVALID
    assert (
        await svc.create_code(
            code="X",
            reward_coins=0,
            max_uses=5,
            per_user_once=True,
            created_by=1,
        )
    ).outcome is CreateOutcome.INVALID

    ok = await svc.create_code(
        code="dup",
        reward_coins=10,
        max_uses=5,
        per_user_once=True,
        created_by=1,
    )
    assert ok.outcome is CreateOutcome.OK
    assert ok.code == "DUP"  # normalised uppercase
    await session.commit()

    dup = await svc.create_code(
        code="DUP",
        reward_coins=99,
        max_uses=5,
        per_user_once=True,
        created_by=1,
    )
    assert dup.outcome is CreateOutcome.DUPLICATE


async def test_create_enforces_the_mint_budget(session: AsyncSession) -> None:
    """T-019: a promo code cannot mint coins without a ceiling.

    Minting is developer-gated, so this guards a typo rather than an
    attacker — but a redeemed code cannot be recalled, so the guard has
    to sit at mint time. See ``docs/ECONOMY_RATE_AUDIT.md`` §2.4 / R3.
    """
    svc = _service(session)

    # Unlimited uses = unbounded total mint. No longer accepted.
    assert (
        await svc.create_code(
            code="INFINITE",
            reward_coins=1,
            max_uses=0,
            per_user_once=True,
            created_by=1,
        )
    ).outcome is CreateOutcome.INVALID

    # Per-redeem reward over the ceiling.
    assert (
        await svc.create_code(
            code="FAT",
            reward_coins=MAX_PROMO_REWARD_COINS + 1,
            max_uses=1,
            per_user_once=True,
            created_by=1,
        )
    ).outcome is CreateOutcome.INVALID

    # Each redeem is fine, but reward × max_uses busts the total.
    assert (
        await svc.create_code(
            code="WIDE",
            reward_coins=MAX_PROMO_REWARD_COINS,
            max_uses=MAX_PROMO_MINT_TOTAL // MAX_PROMO_REWARD_COINS + 1,
            per_user_once=True,
            created_by=1,
        )
    ).outcome is CreateOutcome.INVALID

    # Exactly at both ceilings still mints — the bound is inclusive.
    assert (
        await svc.create_code(
            code="EDGE",
            reward_coins=MAX_PROMO_REWARD_COINS,
            max_uses=MAX_PROMO_MINT_TOTAL // MAX_PROMO_REWARD_COINS,
            per_user_once=True,
            created_by=1,
        )
    ).outcome is CreateOutcome.OK


async def test_redeem_credits_wallet(session: AsyncSession) -> None:
    await _seed_wallet(session, 100)
    svc = _service(session)
    await svc.create_code(
        code="WELCOME",
        reward_coins=50,
        max_uses=5,
        per_user_once=True,
        created_by=1,
        now=_NOW,
    )
    await session.commit()

    result = await svc.redeem(user_id=100, code="welcome", now=_NOW)
    assert result.outcome is RedeemOutcome.OK
    assert result.reward_coins == 50
    assert result.new_balance == 150  # 100 welcome + 50 promo
    await session.commit()

    wallet = await EconomyRepo(session).get(100)
    assert wallet is not None
    assert wallet.balance == 150


async def test_redeem_seeds_a_missing_wallet(session: AsyncSession) -> None:
    """#739: a valid code must not answer CREDIT_FAILED to a new user.

    Every other redeem test calls ``_seed_wallet`` first, which is why
    this hole stayed invisible. ``/promo`` is reachable in a private
    chat without ever passing through the ``/start`` bootstrap, and
    ``EconomyRepo.credit`` is a guarded UPDATE that matches zero rows
    when the wallet does not exist yet.
    """
    svc = _service(session)
    await svc.create_code(
        code="FIRST",
        reward_coins=25,
        max_uses=5,
        per_user_once=True,
        created_by=1,
        now=_NOW,
    )
    await session.commit()
    assert await EconomyRepo(session).get(101) is None

    result = await svc.redeem(user_id=101, code="FIRST", now=_NOW)

    assert result.outcome is RedeemOutcome.OK
    assert result.reward_coins == 25
    await session.commit()
    wallet = await EconomyRepo(session).get(101)
    assert wallet is not None
    assert wallet.balance == 125  # 100 welcome + 25 promo


async def test_redeem_not_found_and_already_redeemed(session: AsyncSession) -> None:
    await _seed_wallet(session, 200)
    svc = _service(session)
    assert (await svc.redeem(user_id=200, code="NOPE", now=_NOW)).outcome is (
        RedeemOutcome.NOT_FOUND
    )

    await svc.create_code(
        code="ONE",
        reward_coins=10,
        max_uses=5,
        per_user_once=True,
        created_by=1,
        now=_NOW,
    )
    await session.commit()
    assert (await svc.redeem(user_id=200, code="ONE", now=_NOW)).outcome is (RedeemOutcome.OK)
    await session.commit()
    # Second redeem by same user on a once-only code.
    assert (await svc.redeem(user_id=200, code="ONE", now=_NOW)).outcome is (
        RedeemOutcome.ALREADY_REDEEMED
    )


async def test_redeem_exhausted_reads_as_not_found(session: AsyncSession) -> None:
    """A code whose last slot was consumed flips ``active=0`` and then
    reads as NOT_FOUND on the next redeem (step 2 short-circuits before
    the reserve). EXHAUSTED is reserved for the narrow race window where
    the code is still active but the reserve UPDATE loses — exercised in
    :func:`test_reserve_race_across_sessions` at the repo level."""
    await _seed_wallet(session, 300)
    await _seed_wallet(session, 301)
    svc = _service(session)
    await svc.create_code(
        code="LIM",
        reward_coins=10,
        max_uses=1,
        per_user_once=False,
        created_by=1,
        now=_NOW,
    )
    await session.commit()
    assert (await svc.redeem(user_id=300, code="LIM", now=_NOW)).outcome is (RedeemOutcome.OK)
    await session.commit()
    assert (await svc.redeem(user_id=301, code="LIM", now=_NOW)).outcome is (
        RedeemOutcome.NOT_FOUND
    )


async def test_reserve_race_across_sessions(tmp_path: Path) -> None:
    """Two sessions, same file: only one wins the last use slot."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(EconomyBase.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        async with maker() as s0:
            code_id = await PromoRepo(s0).create_code(
                code="RACE",
                reward_coins=10,
                max_uses=1,
                per_user_once=False,
                created_by=1,
                now=_NOW,
            )
            await s0.commit()

        async with maker() as s1, maker() as s2:
            r1 = await PromoRepo(s1).reserve_use(code_id, 1)
            await s1.commit()
            r2 = await PromoRepo(s2).reserve_use(code_id, 1)
            await s2.commit()
        assert [r1, r2] == [True, False]
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------
# #1986: the failure path rolls back ITS OWN work, not the session's
# ---------------------------------------------------------------------------


async def test_failed_redeem_credit_keeps_unrelated_work_on_the_session(
    session: AsyncSession,
) -> None:
    """Gate 6's refusal undoes the redeem, not the whole session.

    #1986. ``PromoService`` is built on the update's shared economy
    session (``middlewares/economy.py:283``); a bare
    ``session.rollback()`` there discards every other uncommitted
    statement of the same update.
    """
    svc = _service(session)
    created = await svc.create_code(
        code="OVERFLOW",
        reward_coins=10,
        max_uses=5,
        per_user_once=True,
        created_by=1,
        now=_NOW,
    )
    assert created.outcome is CreateOutcome.OK
    await session.commit()

    repo = EconomyRepo(session)
    await repo.get_or_create(_REDEEMER_1986, now=_NOW)
    await repo.set_balance(_REDEEMER_1986, _MAX_AMOUNT)
    await repo.get_or_create(_OTHER_1986, now=_NOW)
    await session.commit()

    await repo.set_balance(_OTHER_1986, 777)
    result = await svc.redeem(user_id=_REDEEMER_1986, code="OVERFLOW", now=_NOW)
    await session.commit()
    session.expire_all()

    assert result.outcome is RedeemOutcome.CREDIT_FAILED
    # The reserved use slot is given back.
    promo = await PromoRepo(session).get_by_code("OVERFLOW")
    assert promo is not None
    assert promo.used_count == 0
    other = await EconomyRepo(session).get(_OTHER_1986)
    assert other is not None
    assert other.balance == 777
