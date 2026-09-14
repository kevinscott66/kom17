"""``TransactionsRepo.voice_today_count`` — daily quota counter (L-90).

Backs the per-tier voice quota. Counts ``type='tts'`` debit rows
(``from_id=user``) on the current UTC day, MINUS same-day
``type='tts_refund'`` credits (``to_id=user``) — so a failed-and-refunded
synthesis does not burn a quota slot (the refund-skew case the L-90
brief flagged as the reason a naive COUNT would be wrong).

Pinned here:

* N debits today → count N.
* A same-day refund cancels one debit (net N-1).
* Yesterday's debits do NOT count toward today.
* Another user's / a ``shop`` row never leak.
* Zero-state returns 0, not a crash.
* A refund-heavy day clamps at zero (never negative).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import Transaction
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from tests.integration.repositories._session import build_session


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as s:
        yield s


# Ledger stores naive UTC; seed naive to match the writer.
_TODAY = datetime(2026, 6, 5, 12, 0, 0)
_YESTERDAY = _TODAY - timedelta(days=1)
_NOW = datetime(2026, 6, 5, 18, 0, 0, tzinfo=UTC)


async def _seed(session: AsyncSession, rows: list[Transaction]) -> None:
    session.add_all(rows)
    await session.commit()


def _tts_debit(user_id: int, when: datetime = _TODAY) -> Transaction:
    return Transaction(from_id=user_id, amount=1, type="tts", reason="vip_emoji_voice", date=when)


def _tts_refund(user_id: int, when: datetime = _TODAY) -> Transaction:
    return Transaction(to_id=user_id, amount=1, type="tts_refund", reason="fail", date=when)


async def test_zero_state(session: AsyncSession) -> None:
    repo = TransactionsRepo(session)
    assert await repo.voice_today_count(42, now=_NOW) == 0


async def test_counts_today_debits(session: AsyncSession) -> None:
    await _seed(session, [_tts_debit(42), _tts_debit(42), _tts_debit(42)])
    repo = TransactionsRepo(session)
    assert await repo.voice_today_count(42, now=_NOW) == 3


async def test_same_day_refund_cancels_a_debit(session: AsyncSession) -> None:
    """3 debits − 1 same-day refund = 2 net (failed call frees its slot)."""
    await _seed(
        session,
        [_tts_debit(42), _tts_debit(42), _tts_debit(42), _tts_refund(42)],
    )
    repo = TransactionsRepo(session)
    assert await repo.voice_today_count(42, now=_NOW) == 2


async def test_yesterday_does_not_count(session: AsyncSession) -> None:
    await _seed(
        session,
        [_tts_debit(42, _YESTERDAY), _tts_debit(42, _YESTERDAY), _tts_debit(42)],
    )
    repo = TransactionsRepo(session)
    assert await repo.voice_today_count(42, now=_NOW) == 1


async def test_other_user_and_shop_rows_do_not_leak(session: AsyncSession) -> None:
    await _seed(
        session,
        [
            _tts_debit(42),
            _tts_debit(99),  # different user
            Transaction(from_id=42, amount=5, type="shop", reason="buy", date=_TODAY),
        ],
    )
    repo = TransactionsRepo(session)
    assert await repo.voice_today_count(42, now=_NOW) == 1


async def test_refund_heavy_clamps_at_zero(session: AsyncSession) -> None:
    await _seed(session, [_tts_debit(42), _tts_refund(42), _tts_refund(42)])
    repo = TransactionsRepo(session)
    assert await repo.voice_today_count(42, now=_NOW) == 0
