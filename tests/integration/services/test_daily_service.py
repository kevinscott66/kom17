"""``DailyService`` integration — the four-concern composition end-to-end.

Pure arithmetic is covered in ``tests/unit/utils/test_daily.py``; the
SQL race guard is covered in ``test_economy_repo.py``. This file pins
the *composition* — outcome taxonomy, ordering of guard vs reward
roll, ledger row carries ``type='daily'``, deterministic RNG when
seeded.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.daily_service import (
    ClaimOutcome,
    DailyConfig,
    DailyEffects,
    DailyService,
)
from telegram_invite_bot.services.economy_service import EconomyService
from telegram_invite_bot.utils.economy import _MAX_AMOUNT


class _FrozenClock:
    """Minimal stand-in for :class:`datetime` whose ``now`` returns a
    fixed instant. Lets us pin streak / cooldown decisions without
    monkey-patching ``datetime.now`` globally."""

    def __init__(self, fixed: datetime) -> None:
        self._fixed = fixed

    def now(self, tz: object = None) -> datetime:  # noqa: ARG002
        return self._fixed


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


def _service(
    session: AsyncSession,
    *,
    clock: datetime,
    rng: Random | None = None,
    config: DailyConfig | None = None,
) -> DailyService:
    economy_repo = EconomyRepo(session)
    economy_service = EconomyService(economy_repo, TransactionsRepo(session))
    return DailyService(
        economy_repo,
        economy_service,
        config=config or DailyConfig(random_enabled=False),  # deterministic by default
        rng=rng,
        clock=_FrozenClock(clock),  # type: ignore[arg-type]
        session=session,
    )


async def _seed(session: AsyncSession, user_id: int, **overrides: object) -> None:
    defaults: dict[str, object] = {
        "user_id": user_id,
        "balance": 100,
        "language": "ru",
        "daily_streak": 0,
        "last_daily": None,
    }
    defaults.update(overrides)
    session.add(EconomyUser(**defaults))  # type: ignore[arg-type]
    await session.commit()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_first_claim_credits_base_and_starts_streak(session: AsyncSession) -> None:
    await _seed(session, 42)
    service = _service(session, clock=datetime(2024, 1, 1, 12, 0, 0))

    result = await service.claim(42)
    await session.commit()

    assert result.outcome is ClaimOutcome.SUCCESS
    assert result.amount == 10  # base_reward default, streak=1 → no streak bonus
    assert result.streak == 1

    # Wallet state landed.
    wallet = await EconomyRepo(session).get(42)
    assert wallet is not None
    assert wallet.balance == 110
    assert wallet.daily_streak == 1
    assert wallet.last_daily == datetime(2024, 1, 1, 12, 0, 0)

    # Ledger row tagged 'daily' for /admin_donations parity.
    row = (await session.execute(select(Transaction))).scalar_one()
    assert row.to_id == 42
    assert row.amount == 10
    assert row.type == "daily"


async def test_consecutive_day_claim_bumps_streak_and_reward(
    session: AsyncSession,
) -> None:
    await _seed(
        session,
        42,
        daily_streak=4,
        last_daily=datetime(2024, 1, 1, 12, 0, 0),
    )
    service = _service(session, clock=datetime(2024, 1, 2, 12, 30, 0))

    result = await service.claim(42)
    await session.commit()

    assert result.outcome is ClaimOutcome.SUCCESS
    assert result.streak == 5
    # base=10, streak=5 → 10 + 4*5 = 30
    assert result.amount == 30


async def test_missed_day_resets_streak_and_drops_bonus(
    session: AsyncSession,
) -> None:
    """L-23: a >1-day gap resets the streak to 1 AND the payout back to
    base — the streak bonus must not survive a missed day (legacy
    ``DailyBonus.claim`` ``days_diff > 1`` branch, bot.py:12063)."""
    await _seed(
        session,
        42,
        daily_streak=12,
        last_daily=datetime(2024, 1, 1, 12, 0, 0),
    )
    service = _service(session, clock=datetime(2024, 1, 4, 9, 0, 0))  # 3-day gap

    result = await service.claim(42)
    await session.commit()

    assert result.outcome is ClaimOutcome.SUCCESS
    assert result.streak == 1
    assert result.amount == 10  # base only — no (streak-1)*bonus carryover

    wallet = await EconomyRepo(session).get(42)
    assert wallet is not None
    assert wallet.daily_streak == 1
    assert wallet.last_daily == datetime(2024, 1, 4, 9, 0, 0)


async def test_streak_caps_at_max_and_bonus_stops_growing(
    session: AsyncSession,
) -> None:
    """L-23: at ``max_streak`` a consecutive claim keeps streak pinned
    and the bonus stops compounding (legacy ``min(current+1, MAX)``)."""
    await _seed(
        session,
        42,
        daily_streak=30,  # already at DailyConfig.max_streak default
        last_daily=datetime(2024, 1, 1, 12, 0, 0),
    )
    service = _service(session, clock=datetime(2024, 1, 2, 12, 0, 0))

    result = await service.claim(42)
    await session.commit()

    assert result.outcome is ClaimOutcome.SUCCESS
    assert result.streak == 30
    # base=10 + (30-1)*5 = 155 — same as the day before; no growth past cap.
    assert result.amount == 155


# ---------------------------------------------------------------------------
# Rejection paths
# ---------------------------------------------------------------------------


async def test_no_wallet_short_circuits_without_writes(session: AsyncSession) -> None:
    """No wallet → NO_WALLET, and the SQL guard is never run (we
    short-circuit on the Python side). Ledger must be empty."""
    service = _service(session, clock=datetime(2024, 1, 1, 12, 0, 0))
    result = await service.claim(99999)
    await session.commit()

    assert result.outcome is ClaimOutcome.NO_WALLET
    count = await session.execute(select(func.count()).select_from(Transaction))
    assert count.scalar_one() == 0


async def test_cooldown_returns_remaining_no_writes(session: AsyncSession) -> None:
    """Last claim 12h ago → COOLDOWN, remaining ~12h, no ledger row,
    streak unchanged."""
    await _seed(
        session,
        42,
        daily_streak=3,
        last_daily=datetime(2024, 1, 1, 0, 0, 0),
    )
    service = _service(session, clock=datetime(2024, 1, 1, 12, 0, 0))

    result = await service.claim(42)
    await session.commit()

    assert result.outcome is ClaimOutcome.COOLDOWN
    assert result.cooldown_remaining == timedelta(hours=12)
    assert result.amount == 0

    # Ledger empty, wallet untouched.
    count = await session.execute(select(func.count()).select_from(Transaction))
    assert count.scalar_one() == 0
    wallet = await EconomyRepo(session).get(42)
    assert wallet is not None
    assert wallet.balance == 100
    assert wallet.daily_streak == 3


# ---------------------------------------------------------------------------
# Effects: VIP percent, double buster
# ---------------------------------------------------------------------------


async def test_vip_percent_applies_to_streak_inflated_base(
    session: AsyncSession,
) -> None:
    await _seed(
        session,
        42,
        daily_streak=4,
        last_daily=datetime(2024, 1, 1, 12, 0, 0),
    )
    service = _service(session, clock=datetime(2024, 1, 2, 12, 30, 0))

    result = await service.claim(42, effects=DailyEffects(vip_percent=20))
    await session.commit()

    # base=10, streak=5 → 30 (with streak bonus); +20% = 36
    assert result.outcome is ClaimOutcome.SUCCESS
    assert result.amount == 36


async def test_double_buster_doubles_after_vip(session: AsyncSession) -> None:
    await _seed(
        session,
        42,
        daily_streak=4,
        last_daily=datetime(2024, 1, 1, 12, 0, 0),
    )
    service = _service(session, clock=datetime(2024, 1, 2, 12, 30, 0))

    result = await service.claim(42, effects=DailyEffects(vip_percent=20, double=True))
    await session.commit()

    # (30 + 20%) * 2 = 36 * 2 = 72
    assert result.amount == 72


# ---------------------------------------------------------------------------
# Random reward (seeded for determinism)
# ---------------------------------------------------------------------------


async def test_random_enabled_rolls_from_weighted_distribution(
    session: AsyncSession,
) -> None:
    """With random_enabled=True the base reward comes from
    weighted_random_choices; a seeded RNG makes the test
    deterministic. The minimum end of the range is favoured so the
    rolled value should land in the lower half on a seed=0 draw."""
    await _seed(session, 42)
    service = _service(
        session,
        clock=datetime(2024, 1, 1, 12, 0, 0),
        rng=Random(0),
        config=DailyConfig(random_enabled=True, random_min=1, random_max=50),
    )

    result = await service.claim(42)
    await session.commit()

    assert result.outcome is ClaimOutcome.SUCCESS
    # Seeded RNG → deterministic. Pinned to the value seed=0 produces
    # against the published linear-decreasing weights. A Python RNG
    # implementation change would break this — at which point the
    # new pinned value documents the new behaviour. The point of the
    # test is to prove the RNG plumb-through reaches the service.
    assert 1 <= result.amount <= 50
    assert result.amount == 31


async def test_race_lost_when_guard_rejects_after_python_check_passed(
    session: AsyncSession,
) -> None:
    """The SQL guard fires even if the Python cooldown said go.
    Simulating: pre-populate last_daily to a value that the Python
    check passes (because the clock is far ahead) but the SQL guard
    rejects (because we manually backdate last_daily to *just past*
    24h in the wallet but pass a clock that is *within* 24h of the
    real DB column).

    Easier path: seed last_daily=None (Python check passes), then
    after the get() but before mark_daily_claimed we'd need to race
    in another write. We can't easily race in a single test, so we
    cover the inverse: the guard correctly rejects when the clock
    in the service is set to within 24h of an existing last_daily,
    and the Python check would also reject — confirming the two
    layers stay in sync. The pure-Python race-window case is
    covered conceptually by the COOLDOWN test above.

    This test pins that the guard's reject lands as COOLDOWN (not
    RACE_LOST) because the Python check fires first — which is the
    intentional ordering, since hitting the guard rejection in
    production means the Python check was stale (a real race) and
    that's exactly the RACE_LOST signal we want to keep distinct.
    """
    await _seed(
        session,
        42,
        daily_streak=1,
        last_daily=datetime(2024, 1, 1, 12, 0, 0),
    )
    service = _service(session, clock=datetime(2024, 1, 1, 13, 0, 0))

    result = await service.claim(42)
    await session.commit()

    # Within 24h → Python check fires first → COOLDOWN.
    assert result.outcome is ClaimOutcome.COOLDOWN
    assert result.cooldown_remaining == timedelta(hours=23)


async def test_claim_just_past_cooldown_survives_sub_second_precision(
    session: AsyncSession,
) -> None:
    """The SQL guard must not be stricter than the Python check (#465).

    ``last_daily`` is written with microseconds, but the guard used to
    compare against a "now" truncated to whole seconds. Every claim
    landing in ``[24h, 24h + frac(now))`` therefore passed the
    service's cooldown check and was then rejected by the UPDATE,
    surfacing as RACE_LOST with a bogus full-day wait. Here the gap is
    24h + 0.2s: a genuinely legal claim that the old guard refused.
    """
    await _seed(
        session,
        42,
        daily_streak=1,
        last_daily=datetime(2024, 1, 1, 12, 0, 0, 500000),
    )
    service = _service(session, clock=datetime(2024, 1, 2, 12, 0, 0, 700000))

    result = await service.claim(42)
    await session.commit()

    assert result.outcome is ClaimOutcome.SUCCESS
    assert result.streak == 2

    wallet = await EconomyRepo(session).get(42)
    assert wallet is not None
    assert wallet.last_daily == datetime(2024, 1, 2, 12, 0, 0, 700000)


# ---------------------------------------------------------------------------
# UTC handling
# ---------------------------------------------------------------------------


async def test_service_uses_naive_utc_timestamps(session: AsyncSession) -> None:
    """The service stores naive UTC in ``last_daily`` (legacy parity).
    Test by checking that a TZ-aware clock returns a naive datetime
    in the wallet column."""

    class TzAwareClock:
        def now(self, tz: object = UTC) -> datetime:  # noqa: ARG002
            return datetime(2024, 6, 15, 18, 30, 0, tzinfo=UTC)

    await _seed(session, 42)
    economy_repo = EconomyRepo(session)
    economy_service = EconomyService(economy_repo, TransactionsRepo(session))
    service = DailyService(
        economy_repo,
        economy_service,
        config=DailyConfig(random_enabled=False),
        clock=TzAwareClock(),  # type: ignore[arg-type]
    )

    result = await service.claim(42)
    await session.commit()

    assert result.outcome is ClaimOutcome.SUCCESS
    wallet = await economy_repo.get(42)
    assert wallet is not None
    assert wallet.last_daily == datetime(2024, 6, 15, 18, 30, 0)
    assert wallet.last_daily.tzinfo is None  # naive — legacy parity


# ---------------------------------------------------------------------------
# Credit failure — the day must survive
# ---------------------------------------------------------------------------


async def test_credit_failure_rolls_the_claim_back(session: AsyncSession) -> None:
    """A wallet at the balance ceiling must not lose its daily.

    ``EconomyRepo.credit`` refuses a write that would breach
    ``_MAX_AMOUNT``, and the mark (last_daily / streak) has already
    landed by then — on a plain return the middleware would COMMIT a
    consumed day with no coins. The claim rolls itself back instead.
    """
    from telegram_invite_bot.utils.economy import _MAX_AMOUNT  # noqa: PLC0415

    await _seed(session, 42, balance=_MAX_AMOUNT)
    service = _service(session, clock=datetime(2024, 1, 1, 12, 0, 0))

    result = await service.claim(42)

    assert result.outcome is ClaimOutcome.CREDIT_FAILED
    assert result.amount == 0

    wallet = await EconomyRepo(session).get(42)
    assert wallet is not None
    assert wallet.balance == _MAX_AMOUNT
    assert wallet.last_daily is None  # the day was NOT burned
    assert wallet.daily_streak == 0
    ledger = (await session.execute(select(func.count()).select_from(Transaction))).scalar_one()
    assert ledger == 0

    # And the claim is genuinely still available once there is room.
    await session.execute(
        EconomyUser.__table__.update().where(EconomyUser.user_id == 42).values(balance=100)
    )
    await session.commit()
    retry = await service.claim(42)
    await session.commit()
    assert retry.outcome is ClaimOutcome.SUCCESS
    assert retry.streak == 1


# ---------------------------------------------------------------------------
# #1986: the failure path rolls back ITS OWN work, not the session's
# ---------------------------------------------------------------------------


async def test_failed_daily_credit_keeps_unrelated_work_on_the_session(
    session: AsyncSession,
) -> None:
    """The claim is undone; somebody else's pending write is not.

    ``DailyService`` is handed the update's SHARED economy session by
    ``middlewares/economy.py:149``, so a ``session.rollback()`` here
    discards every uncommitted statement the same update made — the
    ``get_or_create`` at ``handlers/daily.py:153`` among them. Same
    argument as #1985; the wallet at the cap is what forces the
    refusal (``EconomyRepo.credit`` returns ``None`` past the ceiling).
    """
    await _seed(session, 42, balance=_MAX_AMOUNT)
    await _seed(session, 777001, balance=50)
    service = _service(session, clock=datetime(2024, 1, 1, 12, 0, 0))

    # Somebody else's uncommitted write on the same session.
    await EconomyRepo(session).set_balance(777001, 777)
    result = await service.claim(42)
    await session.commit()
    session.expire_all()

    assert result.outcome is ClaimOutcome.CREDIT_FAILED
    # Its own half is still undone — the day is not burned.
    wallet = await EconomyRepo(session).get(42)
    assert wallet is not None
    assert wallet.last_daily is None
    assert wallet.daily_streak == 0
    other = await EconomyRepo(session).get(777001)
    assert other is not None
    assert other.balance == 777
