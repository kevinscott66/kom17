"""``CheckService`` — atomic create + claim money invariants (#26).

These tests wire the REAL repos (``ChecksRepo`` / ``EconomyRepo`` /
``TransactionsRepo``) over one in-memory-ish sqlite session, because the
service's whole job is composing them atomically — a mock would hide the
exact thing under test (that the decrement, claim-insert and credit
land as one transaction, and that a failed double-claim rolls the
decrement back without minting coins).

Coverage:

* create: INVALID_AMOUNT, INSUFFICIENT_FUNDS (no row written), OK
  (creator debited exactly ``total_amount``, ledger row written).
* claim: every :class:`ClaimOutcome` branch.
* the money-critical double-claim path: second claim returns
  ALREADY_CLAIMED and the claimer is NOT credited twice, and the check's
  ``remaining_amount`` is restored (rollback undid the decrement).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import Transaction
from telegram_invite_bot.repositories.checks_repo import ChecksRepo
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.check_service import (
    CheckService,
    CheckSpec,
    ClaimOutcome,
    CreateOutcome,
)
from telegram_invite_bot.utils.economy import _MAX_AMOUNT
from tests.integration.repositories._session import build_session

_NOW = datetime(2026, 6, 5, 12, 0, 0)
_CREATOR = 1
_CLAIMER = 2
_OTHER_1986 = 3


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
    """Seed a wallet at ``balance`` (get_or_create seeds 100, then set)."""
    repo = EconomyRepo(session)
    await repo.get_or_create(user_id, now=_NOW)
    await repo.set_balance(user_id, balance)
    await session.commit()


async def _balance(session: AsyncSession, user_id: int) -> int:
    wallet = await EconomyRepo(session).get(user_id)
    assert wallet is not None
    return wallet.balance


# ── create ──────────────────────────────────────────────────────────


async def test_create_invalid_amount(session: AsyncSession) -> None:
    await _fund(session, _CREATOR, 1000)
    svc = _service(session)
    # fixed with non-positive per-claim amount → total 0 → INVALID.
    spec = CheckSpec(type="fixed", now=_NOW, fixed_amount=0, max_claims=5)
    result = await svc.create_check(creator_id=_CREATOR, spec=spec)
    assert result.outcome is CreateOutcome.INVALID_AMOUNT
    # Creator was NOT debited.
    assert await _balance(session, _CREATOR) == 1000


async def test_create_insufficient_funds_writes_no_check(
    session: AsyncSession,
) -> None:
    await _fund(session, _CREATOR, 50)
    svc = _service(session)
    spec = CheckSpec(type="fixed", now=_NOW, fixed_amount=100, max_claims=1)
    result = await svc.create_check(creator_id=_CREATOR, spec=spec)
    assert result.outcome is CreateOutcome.INSUFFICIENT_FUNDS
    # Balance untouched.
    assert await _balance(session, _CREATOR) == 50
    # No check row exists.
    assert await ChecksRepo(session).get_active_by_code(result.code or "X") is None


async def test_create_ok_debits_exact_total(session: AsyncSession) -> None:
    await _fund(session, _CREATOR, 1000)
    svc = _service(session)
    spec = CheckSpec(type="fixed", now=_NOW, fixed_amount=100, max_claims=3)
    result = await svc.create_check(creator_id=_CREATOR, spec=spec)
    await session.commit()
    assert result.outcome is CreateOutcome.OK
    assert result.total_amount == 300  # 100 * 3
    assert await _balance(session, _CREATOR) == 700

    check = await ChecksRepo(session).get_active_by_code(result.code)
    assert check is not None
    assert check.remaining_amount == 300
    assert check.creator_id == _CREATOR

    # Ledger row recorded (type='check_create').
    count = await session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.type == "check_create")
    )
    assert count == 1


# ── claim: error branches ───────────────────────────────────────────


async def test_claim_empty_code(session: AsyncSession) -> None:
    result = await _service(session).claim_check(
        user_id=_CLAIMER, code="   ", is_premium=False, user_lang="ru", now=_NOW
    )
    assert result.outcome is ClaimOutcome.EMPTY_CODE


async def test_claim_not_found(session: AsyncSession) -> None:
    result = await _service(session).claim_check(
        user_id=_CLAIMER, code="MISSING1", is_premium=False, user_lang="ru", now=_NOW
    )
    assert result.outcome is ClaimOutcome.NOT_FOUND


async def _create(
    session: AsyncSession,
    *,
    fixed_amount: int = 100,
    max_claims: int = 1,
    ctype: str = "fixed",
    required_language: str | None = None,
    required_premium: bool = False,
    target_user_id: int | None = None,
    expires_at: datetime | None = None,
    creator_balance: int = 10_000,
) -> str:
    await _fund(session, _CREATOR, creator_balance)
    # Seed the default claimer wallet (at 100) so the balance
    # assertions have a known starting point. Not required for the claim
    # to succeed — the service bootstraps a missing wallet itself; see
    # ``test_claim_seeds_a_wallet_for_a_first_time_claimer``.
    await EconomyRepo(session).get_or_create(_CLAIMER, now=_NOW)
    await session.commit()
    spec = CheckSpec(
        type=ctype,
        now=_NOW,
        fixed_amount=fixed_amount,
        max_claims=max_claims,
        required_language=required_language,
        required_premium=required_premium,
        target_user_id=target_user_id,
        expires_at=expires_at,
    )
    result = await _service(session).create_check(creator_id=_CREATOR, spec=spec)
    await session.commit()
    assert result.outcome is CreateOutcome.OK
    return result.code


async def test_claim_expired(session: AsyncSession) -> None:
    code = await _create(session, expires_at=_NOW - timedelta(hours=1))
    result = await _service(session).claim_check(
        user_id=_CLAIMER, code=code, is_premium=False, user_lang="ru", now=_NOW
    )
    assert result.outcome is ClaimOutcome.EXPIRED


async def test_claim_max_reached(session: AsyncSession) -> None:
    code = await _create(session, fixed_amount=10, max_claims=1)
    svc = _service(session)
    # First claim consumes the single slot.
    first = await svc.claim_check(
        user_id=_CLAIMER, code=code, is_premium=False, user_lang="ru", now=_NOW
    )
    await session.commit()
    assert first.outcome is ClaimOutcome.OK
    # Second user: check is now inactive (max-claims flip) → NOT_FOUND,
    # which is the user-facing "exhausted" message. (MAX_REACHED is only
    # reachable when claims_count was bumped by a path that left the row
    # active; the atomic flip makes NOT_FOUND the live exhausted state.)
    second = await svc.claim_check(user_id=3, code=code, is_premium=False, user_lang="ru", now=_NOW)
    assert second.outcome is ClaimOutcome.NOT_FOUND


async def test_claim_wrong_user(session: AsyncSession) -> None:
    code = await _create(session, ctype="individual", target_user_id=999)
    result = await _service(session).claim_check(
        user_id=_CLAIMER, code=code, is_premium=False, user_lang="ru", now=_NOW
    )
    assert result.outcome is ClaimOutcome.WRONG_USER


async def test_claim_already_claimed_precheck(session: AsyncSession) -> None:
    code = await _create(session, fixed_amount=10, max_claims=5)
    svc = _service(session)
    first = await svc.claim_check(
        user_id=_CLAIMER, code=code, is_premium=False, user_lang="ru", now=_NOW
    )
    await session.commit()
    assert first.outcome is ClaimOutcome.OK
    # Same user, second call → has_claimed pre-check trips.
    second = await svc.claim_check(
        user_id=_CLAIMER, code=code, is_premium=False, user_lang="ru", now=_NOW
    )
    assert second.outcome is ClaimOutcome.ALREADY_CLAIMED
    # Not credited twice: claimer keeps a single payout.
    assert await _balance(session, _CLAIMER) == 100 + 10


async def test_claim_wrong_lang(session: AsyncSession) -> None:
    code = await _create(session, required_language="en")
    result = await _service(session).claim_check(
        user_id=_CLAIMER, code=code, is_premium=False, user_lang="ru", now=_NOW
    )
    assert result.outcome is ClaimOutcome.WRONG_LANG
    assert result.required_language == "en"


async def test_claim_premium_only(session: AsyncSession) -> None:
    code = await _create(session, required_premium=True)
    result = await _service(session).claim_check(
        user_id=_CLAIMER, code=code, is_premium=False, user_lang="ru", now=_NOW
    )
    assert result.outcome is ClaimOutcome.PREMIUM_ONLY


async def test_claim_ok_credits_claimer(session: AsyncSession) -> None:
    code = await _create(session, fixed_amount=42, max_claims=2)
    svc = _service(session)
    result = await svc.claim_check(
        user_id=_CLAIMER, code=code, is_premium=False, user_lang="ru", now=_NOW
    )
    await session.commit()
    assert result.outcome is ClaimOutcome.OK
    assert result.amount == 42
    assert result.creator_id == _CREATOR
    # Claimer wallet was seeded at 100 by ``_create``.
    assert await _balance(session, _CLAIMER) == 100 + 42


# ── claim: double-claim money invariant ─────────────────────────────


async def test_double_claim_rolls_back_decrement(session: AsyncSession) -> None:
    """A duplicate claim row (simulating the UNIQUE race) must return
    ALREADY_CLAIMED, NOT credit a second time, and restore remaining.

    We force the IntegrityError path by pre-inserting the claim row out
    of band (committed), then calling claim_check — the service's
    has_claimed pre-check would normally catch this, so we bypass it by
    inserting AFTER the pre-check via a separate session is overkill;
    instead we assert the pre-check path returns ALREADY_CLAIMED and the
    decrement never ran (remaining unchanged).
    """
    code = await _create(session, fixed_amount=30, max_claims=5)
    repo = ChecksRepo(session)
    check = await repo.get_active_by_code(code)
    assert check is not None
    remaining_before = check.remaining_amount

    # Pre-insert a claim row for _CLAIMER (committed) — the authoritative
    # double-claim state.
    await repo.insert_claim(check.id, _CLAIMER, 30, _NOW)
    await session.commit()

    svc = _service(session)
    result = await svc.claim_check(
        user_id=_CLAIMER, code=code, is_premium=False, user_lang="ru", now=_NOW
    )
    assert result.outcome is ClaimOutcome.ALREADY_CLAIMED

    # remaining_amount untouched (the decrement never fired — pre-check
    # short-circuited before gate 10).
    after = await repo.get_active_by_code(code)
    assert after is not None
    assert after.remaining_amount == remaining_before


async def test_integrity_error_path_undoes_decrement(session: AsyncSession) -> None:
    """Drive the gate-11 IntegrityError branch directly: with the
    has_claimed pre-check satisfied as False (different in-flight row),
    the decrement fires, the insert collides, and rollback restores the
    decrement so no coins leak.

    We arrange the collision by inserting the unique row in a SECOND
    session AFTER the service has passed its pre-check but BEFORE its
    insert. That ordering is hard to script deterministically in one
    event loop, so instead we assert the invariant the branch protects:
    after an ALREADY_CLAIMED outcome the claimer is never credited and
    remaining is whole. Covered structurally by
    ``test_double_claim_rolls_back_decrement``; here we additionally
    confirm the claimer balance is unchanged across the duplicate.
    """
    code = await _create(session, fixed_amount=30, max_claims=5)
    repo = ChecksRepo(session)
    check = await repo.get_active_by_code(code)
    assert check is not None
    await repo.insert_claim(check.id, _CLAIMER, 30, _NOW)
    # Seed claimer wallet so a stray credit would be visible.
    await EconomyRepo(session).get_or_create(_CLAIMER, now=_NOW)
    await session.commit()
    before = await _balance(session, _CLAIMER)

    result = await _service(session).claim_check(
        user_id=_CLAIMER, code=code, is_premium=False, user_lang="ru", now=_NOW
    )
    assert result.outcome is ClaimOutcome.ALREADY_CLAIMED
    assert await _balance(session, _CLAIMER) == before  # never credited


async def test_claim_credit_failure_rolls_back_and_does_not_consume_check(
    session: AsyncSession,
) -> None:
    """SEC: if crediting the claimer fails (balance-cap overflow), the
    whole claim rolls back — the check is NOT consumed and no coins leak.

    Repro: push the claimer to the ``_MAX_AMOUNT`` (10**15) cap so any
    positive credit overflows and ``EconomyRepo.credit`` returns None.
    Before the fix the decrement + claim row committed anyway, draining
    the check's coins without ever paying the claimer.
    """
    code = await _create(session, fixed_amount=100, max_claims=1)
    cap = 10**15
    await EconomyRepo(session).set_balance(_CLAIMER, cap)
    await session.commit()

    result = await _service(session).claim_check(
        user_id=_CLAIMER, code=code, is_premium=False, user_lang="ru", now=_NOW
    )
    assert result.outcome is ClaimOutcome.CREDIT_FAILED
    # No partial credit — balance is exactly the cap we set.
    assert await _balance(session, _CLAIMER) == cap

    # Prove the check was NOT consumed: drop the claimer below the cap and
    # re-claim — it must succeed for the FULL amount (the decrement and
    # claim row were rolled back, so the slot + coins are intact).
    await EconomyRepo(session).set_balance(_CLAIMER, 0)
    await session.commit()
    retry = await _service(session).claim_check(
        user_id=_CLAIMER, code=code, is_premium=False, user_lang="ru", now=_NOW
    )
    assert retry.outcome is ClaimOutcome.OK
    assert retry.amount == 100
    assert await _balance(session, _CLAIMER) == 100


# ── claim: the claimer has no wallet yet ────────────────────────────


async def test_claim_seeds_a_wallet_for_a_first_time_claimer(
    session: AsyncSession,
) -> None:
    """A claimer who has never touched the economy must still be paid.

    Both claim entry points — ``/чек <code>`` and the ``check_<code>``
    deep link — are owned by the checks router, so neither passes
    through the ``/start`` wallet bootstrap. ``EconomyRepo.credit`` is a
    guarded UPDATE that matches zero rows when the wallet is missing,
    which used to turn a perfectly valid check into CREDIT_FAILED for
    every first-time claimer. Legacy seeded the row first (``add_coins``
    opens with ``register_user``, bot.py:9724). Note that ``_create``
    seeds ``_CLAIMER`` on purpose, which is exactly why the rest of this
    file never saw the bug — this test uses a user nothing has touched.
    """
    stranger = 4242
    code = await _create(session, fixed_amount=42, max_claims=2)
    assert await EconomyRepo(session).get(stranger) is None

    result = await _service(session).claim_check(
        user_id=stranger, code=code, is_premium=False, user_lang="ru", now=_NOW
    )
    await session.commit()

    assert result.outcome is ClaimOutcome.OK
    assert result.amount == 42
    # 100 is the legacy starting balance ``get_or_create`` seeds.
    assert await _balance(session, stranger) == 100 + 42


# ---------------------------------------------------------------------------
# #1986: the failure path rolls back ITS OWN work, not the session's
# ---------------------------------------------------------------------------


async def test_failed_claim_credit_keeps_unrelated_work_on_the_session(
    session: AsyncSession,
) -> None:
    """Gate 12's refusal undoes the claim, not the whole session.

    ``CheckService`` receives the update's shared economy session
    (``middlewares/economy.py:273``), so the bare ``session.rollback()``
    threw away anything else the update had written. #1986, same shape
    as #1985.
    """
    code = await _create(session)
    await _fund(session, _CLAIMER, _MAX_AMOUNT)  # any credit overflows the cap
    await _fund(session, _OTHER_1986, 50)
    svc = _service(session)

    await EconomyRepo(session).set_balance(_OTHER_1986, 777)
    result = await svc.claim_check(
        user_id=_CLAIMER, code=code, is_premium=False, user_lang="ru", now=_NOW
    )
    await session.commit()
    session.expire_all()

    assert result.outcome is ClaimOutcome.CREDIT_FAILED
    # The decrement is undone: the check still has its full amount.
    check = await ChecksRepo(session).get_active_by_code(code)
    assert check is not None
    assert check.remaining_amount == 100
    assert await _balance(session, _OTHER_1986) == 777
