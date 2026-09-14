"""``GroupTreasuryService`` — /group_pay money invariants (L-28/L-41).

Real repos over one sqlite session (same posture as the CheckService
tests) because the service's whole job is composing the treasury debit,
the wallet credit and the ledger row atomically — mocks would hide the
SAVEPOINT semantics under test.

Money pins:

* SUCCESS: treasury debited exactly ``amount``, payee credited exactly
  ``amount``, ONE ``type='treasury_payout'`` ledger row, and a
  ``rating_history`` snapshot row for the group (legacy write-for-write
  parity, bot.py:10976-10977).
* INSUFFICIENT_FUNDS: missing row / low balance — nothing mutated, no
  ledger row, ``available`` carries the observed balance for the
  legacy «Доступно: N» line (bot.py:10967).
* CREDIT_FAILED (the hardening over legacy): a wallet-cap overflow on
  the credit rolls the treasury debit back via the SAVEPOINT — legacy
  commits the debit BEFORE crediting (bot.py:10975-10976) and would
  burn the coins.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select, text

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import Transaction
from telegram_invite_bot.repositories.donations_rating_repo import DonationsRatingRepo
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.repositories.treasury_repo import TreasuryRepo
from telegram_invite_bot.services.treasury_service import (
    GroupTreasuryService,
    PayoutOutcome,
)
from telegram_invite_bot.utils.economy import _MAX_AMOUNT
from tests.integration.repositories._session import build_session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

_GROUP = -100777
_OWNER = 42

# ``rating_history`` ships in migration 0009 (raw-SQL table, no ORM
# model), so the fixture adds it next to the ORM-created tables — same
# approach as tests/integration/repositories/test_donations_rating_repo.py.
_RATING_HISTORY_DDL = (
    "CREATE TABLE rating_history ("
    "  group_id INTEGER NOT NULL,"
    "  date TEXT NOT NULL,"
    "  total_donations INTEGER NOT NULL,"
    "  position INTEGER,"
    "  PRIMARY KEY (group_id, date)"
    ")"
)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as s:
        await s.execute(text(_RATING_HISTORY_DDL))
        await s.commit()
        yield s


def _service(session: AsyncSession) -> GroupTreasuryService:
    return GroupTreasuryService(
        TreasuryRepo(session),
        EconomyRepo(session),
        TransactionsRepo(session),
        DonationsRatingRepo(session),
    )


async def _seed_treasury(session: AsyncSession, total: int) -> None:
    await session.execute(
        text(
            "INSERT INTO groups_donations (group_id, total_donations, group_xp) VALUES (:g, :t, 0)"
        ),
        {"g": _GROUP, "t": total},
    )
    await session.commit()


async def _treasury(session: AsyncSession) -> int:
    row = (
        await session.execute(
            text("SELECT total_donations FROM groups_donations WHERE group_id=:g"),
            {"g": _GROUP},
        )
    ).first()
    assert row is not None
    return int(row[0])


async def _wallet_balance(session: AsyncSession, user_id: int) -> int:
    wallet = await EconomyRepo(session).get(user_id)
    assert wallet is not None
    return wallet.balance


async def _ledger_rows(session: AsyncSession) -> int:
    count = await session.scalar(
        select(func.count()).select_from(Transaction).where(Transaction.type == "treasury_payout")
    )
    return int(count or 0)


# ── rejections ──────────────────────────────────────────────────────


async def test_invalid_amount(session: AsyncSession) -> None:
    result = await _service(session).payout(group_id=_GROUP, to_user_id=_OWNER, amount=0)
    assert result.outcome is PayoutOutcome.INVALID_AMOUNT


async def test_missing_group_row_is_insufficient(session: AsyncSession) -> None:
    result = await _service(session).payout(group_id=_GROUP, to_user_id=_OWNER, amount=1000)
    assert result.outcome is PayoutOutcome.INSUFFICIENT_FUNDS
    assert result.available == 0
    assert await _ledger_rows(session) == 0


async def test_low_balance_reports_available(session: AsyncSession) -> None:
    await _seed_treasury(session, 700)
    result = await _service(session).payout(group_id=_GROUP, to_user_id=_OWNER, amount=1000)
    assert result.outcome is PayoutOutcome.INSUFFICIENT_FUNDS
    assert result.available == 700
    assert await _treasury(session) == 700
    assert await _ledger_rows(session) == 0


# ── success ─────────────────────────────────────────────────────────


async def test_success_moves_money_and_writes_ledger(session: AsyncSession) -> None:
    await _seed_treasury(session, 5000)
    # Pre-create the wallet so the balance delta is measurable against
    # the get_or_create seed value.
    before = (await EconomyRepo(session).get_or_create(_OWNER)).balance
    await session.commit()

    result = await _service(session).payout(group_id=_GROUP, to_user_id=_OWNER, amount=1500)
    await session.commit()

    assert result.outcome is PayoutOutcome.SUCCESS
    assert result.amount == 1500
    assert result.available == 5000  # pre-debit balance
    assert await _treasury(session) == 3500
    assert await _wallet_balance(session, _OWNER) == before + 1500
    assert await _ledger_rows(session) == 1


async def test_success_writes_rating_history_snapshot(session: AsyncSession) -> None:
    await _seed_treasury(session, 2000)
    result = await _service(session).payout(group_id=_GROUP, to_user_id=_OWNER, amount=2000)
    await session.commit()
    assert result.outcome is PayoutOutcome.SUCCESS
    rows = (
        await session.execute(
            text("SELECT COUNT(*) FROM rating_history WHERE group_id=:g"),
            {"g": _GROUP},
        )
    ).scalar()
    assert rows == 1


async def test_success_auto_creates_missing_wallet(session: AsyncSession) -> None:
    await _seed_treasury(session, 3000)
    result = await _service(session).payout(group_id=_GROUP, to_user_id=_OWNER, amount=1000)
    await session.commit()
    assert result.outcome is PayoutOutcome.SUCCESS
    wallet = await EconomyRepo(session).get(_OWNER)
    assert wallet is not None  # self-healed via get_or_create


# ── credit failure → compensating rollback ──────────────────────────


async def test_credit_failure_rolls_treasury_debit_back(session: AsyncSession) -> None:
    """Wallet at the balance cap → ``credit`` returns None → SAVEPOINT
    rollback restores the treasury and no ledger row is written.

    This is the HANDLER's view — ``handlers/group_pay.py:203`` rolls the
    session back on any non-success and the line below mirrors it. That
    outer rollback does NOT make the assertions free: measured, this
    test fails (``assert 4000 == 5000``) when
    ``treasury_service.py:156`` is removed. Its companion
    ``test_credit_failure_rollback_survives_a_commit`` covers the caller
    that commits instead, so neither posture can regress unnoticed.

    Legacy cannot pass either test: ``group_treasury_pay`` commits the
    treasury UPDATE (bot.py:10975) before ``add_coins`` runs, so a
    failed credit leaves the treasury short with nobody paid.
    """
    await _seed_treasury(session, 5000)
    repo = EconomyRepo(session)
    await repo.get_or_create(_OWNER)
    await repo.set_balance(_OWNER, _MAX_AMOUNT)
    await session.commit()

    result = await _service(session).payout(group_id=_GROUP, to_user_id=_OWNER, amount=1000)
    # Handler rolls back on non-success; mirror it.
    await session.rollback()

    assert result.outcome is PayoutOutcome.CREDIT_FAILED
    assert await _treasury(session) == 5000
    assert await _wallet_balance(session, _OWNER) == _MAX_AMOUNT
    assert await _ledger_rows(session) == 0


async def test_credit_failure_rollback_survives_a_commit(session: AsyncSession) -> None:
    """#1273: a caller that COMMITS after CREDIT_FAILED sees no debit.

    Same setup as above, but without the handler's compensating
    rollback — the posture of any future caller that keeps working
    after a refused payout instead of tearing its whole update down.
    ``group_pay`` is the only caller today and it rolls back, so this
    is the test that keeps the service's own guarantee from quietly
    becoming the handler's. Measured: fails (``assert 4000 == 5000``)
    when ``savepoint.rollback()`` (``treasury_service.py:156``) is
    removed, which is exactly the legacy bug the service was written
    to fix.
    """
    await _seed_treasury(session, 5000)
    repo = EconomyRepo(session)
    await repo.get_or_create(_OWNER)
    await repo.set_balance(_OWNER, _MAX_AMOUNT)
    await session.commit()

    result = await _service(session).payout(group_id=_GROUP, to_user_id=_OWNER, amount=1000)
    await session.commit()

    assert result.outcome is PayoutOutcome.CREDIT_FAILED
    assert await _treasury(session) == 5000
    assert await _wallet_balance(session, _OWNER) == _MAX_AMOUNT
    assert await _ledger_rows(session) == 0
