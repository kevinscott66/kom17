"""``TreasuryRepo`` — group-treasury balance ops (L-28/L-41).

The treasury is the prod-existing ``groups_donations.total_donations``
column (legacy ``group_treasury_pay``, bot.py:10956-10982). These tests
pin the three repo invariants the payout service builds on:

* ``get_balance`` distinguishes missing-row (None) from zero, and
  collapses a NULL column to 0 the way legacy's reader does
  (bot.py:10917-10918);
* ``debit`` is race-guarded — the ``total_donations >= amount``
  predicate lives in the UPDATE itself, so over-draws and NULL
  balances reject via rowcount instead of going negative
  (bot.py:10969-10974);
* ``credit`` (the compensating refund) restores exactly what the debit
  took.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.repositories.treasury_repo import TreasuryRepo
from tests.integration.repositories._session import build_session

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

_GROUP = -100123


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as s:
        yield s


async def _seed(session: AsyncSession, group_id: int, total: int | None) -> None:
    await session.execute(
        text("INSERT INTO groups_donations (group_id, total_donations) VALUES (:g, :t)"),
        {"g": group_id, "t": total},
    )
    await session.flush()


async def _total(session: AsyncSession, group_id: int) -> int | None:
    row = (
        await session.execute(
            text("SELECT total_donations FROM groups_donations WHERE group_id=:g"),
            {"g": group_id},
        )
    ).first()
    assert row is not None
    return row[0]


# ── get_balance ─────────────────────────────────────────────────────


async def test_get_balance_missing_row_is_none(session: AsyncSession) -> None:
    assert await TreasuryRepo(session).get_balance(_GROUP) is None


async def test_get_balance_null_column_collapses_to_zero(session: AsyncSession) -> None:
    await _seed(session, _GROUP, None)
    assert await TreasuryRepo(session).get_balance(_GROUP) == 0


async def test_get_balance_reads_value(session: AsyncSession) -> None:
    await _seed(session, _GROUP, 5000)
    assert await TreasuryRepo(session).get_balance(_GROUP) == 5000


# ── debit ───────────────────────────────────────────────────────────


async def test_debit_decrements_and_reports_true(session: AsyncSession) -> None:
    await _seed(session, _GROUP, 5000)
    repo = TreasuryRepo(session)
    assert await repo.debit(_GROUP, 1500) is True
    assert await _total(session, _GROUP) == 3500


async def test_debit_overdraw_rejected_without_mutation(session: AsyncSession) -> None:
    await _seed(session, _GROUP, 1000)
    repo = TreasuryRepo(session)
    assert await repo.debit(_GROUP, 1001) is False
    assert await _total(session, _GROUP) == 1000


async def test_debit_exact_balance_drains_to_zero(session: AsyncSession) -> None:
    await _seed(session, _GROUP, 1000)
    repo = TreasuryRepo(session)
    assert await repo.debit(_GROUP, 1000) is True
    assert await _total(session, _GROUP) == 0


async def test_debit_null_balance_rejected(session: AsyncSession) -> None:
    await _seed(session, _GROUP, None)
    assert await TreasuryRepo(session).debit(_GROUP, 1) is False


async def test_debit_missing_row_rejected(session: AsyncSession) -> None:
    assert await TreasuryRepo(session).debit(_GROUP, 1) is False


async def test_debit_non_positive_amount_rejected(session: AsyncSession) -> None:
    await _seed(session, _GROUP, 1000)
    repo = TreasuryRepo(session)
    assert await repo.debit(_GROUP, 0) is False
    assert await repo.debit(_GROUP, -5) is False
    assert await _total(session, _GROUP) == 1000


# ── credit (compensating refund) ────────────────────────────────────


async def test_credit_restores_debited_amount(session: AsyncSession) -> None:
    await _seed(session, _GROUP, 2000)
    repo = TreasuryRepo(session)
    assert await repo.debit(_GROUP, 1500) is True
    assert await repo.credit(_GROUP, 1500) is True
    assert await _total(session, _GROUP) == 2000


async def test_credit_missing_row_reports_false(session: AsyncSession) -> None:
    assert await TreasuryRepo(session).credit(_GROUP, 100) is False
