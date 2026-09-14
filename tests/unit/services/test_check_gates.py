"""``CheckService`` claim-time filter gates (L-85..L-88) + create
pre-compute / hold validation (L-100).

Wires the REAL repos over one sqlite session (same posture as
``test_check_service.py``): the gates read the check row and the
claimer's ``economy.users`` row off the shared session, so a mock would
hide the exact column-plumbing under test.

Coverage:

* L-88 blocked_users → BLOCKED_USER (and a non-blocked user passes).
* L-85 min_age → MIN_AGE (young account rejected; aged account passes;
  no-``registered`` row rejected conservatively).
* L-86 min_activity → MIN_ACTIVITY (handler-supplied count + the
  ``games_played`` fallback; ``None`` activity skips the gate).
* L-87 allowed_countries → COUNTRY_BLOCKED (best-effort: only enforced
  when a country is supplied; otherwise PASS).
* L-100 create hold: ``random`` average payout pre-computed and the
  creator debited exactly that; insufficient balance writes no row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.repositories.checks_repo import ChecksRepo
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.check_service import (
    CheckService,
    CheckSpec,
    ClaimOutcome,
    CreateOutcome,
)
from tests.integration.repositories._session import build_session

_NOW = datetime(2026, 6, 5, 12, 0, 0)
_CREATOR = 1
_CLAIMER = 2


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as s:
        yield s


def _service(session: AsyncSession) -> CheckService:
    return CheckService(
        ChecksRepo(session),
        EconomyRepo(session),
        TransactionsRepo(session),
        session,
    )


async def _fund(session: AsyncSession, user_id: int, balance: int) -> None:
    repo = EconomyRepo(session)
    await repo.get_or_create(user_id, now=_NOW)
    await repo.set_balance(user_id, balance)
    await session.commit()


async def _set_user(
    session: AsyncSession,
    user_id: int,
    *,
    registered: datetime | None,
    games_played: int = 0,
) -> None:
    """Seed/patch the claimer's economy row for age/activity gates."""
    await EconomyRepo(session).get_or_create(user_id, now=_NOW)
    row = await session.get(EconomyUser, user_id)
    assert row is not None
    row.registered = registered
    row.games_played = games_played
    await session.commit()


async def _make_check(session: AsyncSession, spec: CheckSpec) -> str:
    await _fund(session, _CREATOR, 1_000_000)
    # Seed the claimer's wallet so a passing claim can be credited (else
    # ``credit`` returns None → CREDIT_FAILED, masking the gate result).
    await EconomyRepo(session).get_or_create(_CLAIMER, now=_NOW)
    await session.commit()
    result = await _service(session).create_check(creator_id=_CREATOR, spec=spec)
    assert result.outcome is CreateOutcome.OK
    return result.code


async def _claim(
    session: AsyncSession,
    code: str,
    *,
    activity: int | None = None,
    country: str | None = None,
) -> ClaimOutcome:
    return (
        await _service(session).claim_check(
            user_id=_CLAIMER,
            code=code,
            is_premium=False,
            user_lang="ru",
            now=_NOW,
            claimant_activity=activity,
            claimant_country=country,
        )
    ).outcome


# ── L-88 blocked_users ──────────────────────────────────────────────


async def test_blocked_user_rejected(session: AsyncSession) -> None:
    code = await _make_check(
        session,
        CheckSpec(
            type="fixed",
            now=_NOW,
            fixed_amount=10,
            max_claims=5,
            blocked_users=[_CLAIMER],
        ),
    )
    assert await _claim(session, code) is ClaimOutcome.BLOCKED_USER


async def test_non_blocked_user_passes(session: AsyncSession) -> None:
    code = await _make_check(
        session,
        CheckSpec(
            type="fixed",
            now=_NOW,
            fixed_amount=10,
            max_claims=5,
            blocked_users=[999],
        ),
    )
    assert await _claim(session, code) is ClaimOutcome.OK


# ── L-85 min_age ────────────────────────────────────────────────────


async def test_min_age_young_account_rejected(session: AsyncSession) -> None:
    # Registered 2 days ago; gate wants 7.
    await _set_user(session, _CLAIMER, registered=_NOW - timedelta(days=2))
    code = await _make_check(
        session,
        CheckSpec(type="fixed", now=_NOW, fixed_amount=10, max_claims=5, min_age=7),
    )
    assert await _claim(session, code) is ClaimOutcome.MIN_AGE


async def test_min_age_aged_account_passes(session: AsyncSession) -> None:
    await _set_user(session, _CLAIMER, registered=_NOW - timedelta(days=30))
    code = await _make_check(
        session,
        CheckSpec(type="fixed", now=_NOW, fixed_amount=10, max_claims=5, min_age=7),
    )
    assert await _claim(session, code) is ClaimOutcome.OK


async def test_min_age_unknown_registration_rejected(session: AsyncSession) -> None:
    await _set_user(session, _CLAIMER, registered=None)
    code = await _make_check(
        session,
        CheckSpec(type="fixed", now=_NOW, fixed_amount=10, max_claims=5, min_age=7),
    )
    assert await _claim(session, code) is ClaimOutcome.MIN_AGE


# ── L-86 min_activity ───────────────────────────────────────────────


async def test_min_activity_below_threshold_rejected(session: AsyncSession) -> None:
    code = await _make_check(
        session,
        CheckSpec(type="fixed", now=_NOW, fixed_amount=10, max_claims=5, min_activity=50),
    )
    assert await _claim(session, code, activity=10) is ClaimOutcome.MIN_ACTIVITY


async def test_min_activity_uses_games_played_fallback(session: AsyncSession) -> None:
    # No handler-supplied activity → service falls back to games_played.
    await _set_user(session, _CLAIMER, registered=_NOW, games_played=3)
    code = await _make_check(
        session,
        CheckSpec(type="fixed", now=_NOW, fixed_amount=10, max_claims=5, min_activity=50),
    )
    assert await _claim(session, code) is ClaimOutcome.MIN_ACTIVITY


async def test_min_activity_met_passes(session: AsyncSession) -> None:
    code = await _make_check(
        session,
        CheckSpec(type="fixed", now=_NOW, fixed_amount=10, max_claims=5, min_activity=50),
    )
    assert await _claim(session, code, activity=100) is ClaimOutcome.OK


# ── L-87 allowed_countries (best-effort) ────────────────────────────


async def test_country_not_allowed_rejected(session: AsyncSession) -> None:
    code = await _make_check(
        session,
        CheckSpec(
            type="fixed",
            now=_NOW,
            fixed_amount=10,
            max_claims=5,
            allowed_countries=["ru", "by"],
        ),
    )
    assert await _claim(session, code, country="US") is ClaimOutcome.COUNTRY_BLOCKED


async def test_country_allowed_passes(session: AsyncSession) -> None:
    code = await _make_check(
        session,
        CheckSpec(
            type="fixed",
            now=_NOW,
            fixed_amount=10,
            max_claims=5,
            allowed_countries=["ru", "by"],
        ),
    )
    assert await _claim(session, code, country="ru") is ClaimOutcome.OK


async def test_country_unknown_passes_best_effort(session: AsyncSession) -> None:
    # No country supplied → gate is a no-op (documented limitation).
    code = await _make_check(
        session,
        CheckSpec(
            type="fixed",
            now=_NOW,
            fixed_amount=10,
            max_claims=5,
            allowed_countries=["ru"],
        ),
    )
    assert await _claim(session, code, country=None) is ClaimOutcome.OK


# ── L-100 create hold / pre-compute ─────────────────────────────────


async def test_random_hold_is_average_times_count(session: AsyncSession) -> None:
    await _fund(session, _CREATOR, 1000)
    spec = CheckSpec(type="random", now=_NOW, min_amount=10, max_amount=30, max_claims=4)
    result = await _service(session).create_check(creator_id=_CREATOR, spec=spec)
    assert result.outcome is CreateOutcome.OK
    # avg(10,30)=20 * 4 = 80 held.
    assert result.total_amount == 80
    wallet = await EconomyRepo(session).get(_CREATOR)
    assert wallet is not None
    assert wallet.balance == 1000 - 80


async def test_random_insufficient_balance_writes_no_row(session: AsyncSession) -> None:
    await _fund(session, _CREATOR, 50)
    spec = CheckSpec(type="random", now=_NOW, min_amount=10, max_amount=30, max_claims=4)
    result = await _service(session).create_check(creator_id=_CREATOR, spec=spec)
    assert result.outcome is CreateOutcome.INSUFFICIENT_FUNDS
    wallet = await EconomyRepo(session).get(_CREATOR)
    assert wallet is not None
    assert wallet.balance == 50
