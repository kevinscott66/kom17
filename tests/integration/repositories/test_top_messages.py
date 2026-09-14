"""Real-SQLite tests for :meth:`MessageStatsRepo.top_users_by_messages`.

Pinning the invariants that the e2e test can't see clearly:

* The date window is **inclusive on both ends** — a row dated exactly
  ``today`` is counted, and a row dated exactly ``today - (days-1)``
  is counted, but ``today + 1`` (clock skew or wrong-TZ writer) is not.
* Tie-break is ``user_id ASC`` so concurrent equal-count rows render
  in a deterministic order across requests.
* Empty windows return ``[]`` — the handler decides the empty copy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import MessageStatsBase
from telegram_invite_bot.db.models.message_stats import MessageCount
from telegram_invite_bot.repositories.message_stats_repo import MessageStatsRepo
from tests.integration.repositories._session import build_session


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, MessageStatsBase, "stats.db") as s:
        yield s


def _row(*, chat_id: int, user_id: int, day: str, count: int) -> MessageCount:
    return MessageCount(chat_id=chat_id, user_id=user_id, date=day, count=count)


async def test_top_orders_by_total_desc_then_user_id(session: AsyncSession) -> None:
    session.add_all(
        [
            _row(chat_id=1, user_id=10, day="2024-06-01", count=5),
            _row(chat_id=1, user_id=10, day="2024-06-02", count=5),  # total 10
            _row(chat_id=1, user_id=20, day="2024-06-01", count=10),  # total 10 (tie)
            _row(chat_id=1, user_id=30, day="2024-06-02", count=100),  # total 100
        ]
    )
    await session.commit()
    rows = await MessageStatsRepo(session).top_users_by_messages(
        1, days=7, today=date(2024, 6, 5), limit=10
    )
    assert rows == [(30, 100), (10, 10), (20, 10)]


async def test_top_respects_date_window_inclusive(session: AsyncSession) -> None:
    """``today`` and ``today - (days-1)`` must both be inside the window;
    a row on ``today + 1`` (clock skew or wrong-TZ writer) is out.
    """
    today = date(2024, 6, 10)
    session.add_all(
        [
            _row(chat_id=1, user_id=1, day="2024-06-08", count=1),  # in (days=3)
            _row(chat_id=1, user_id=2, day="2024-06-10", count=2),  # in (today edge)
            _row(chat_id=1, user_id=3, day="2024-06-07", count=99),  # out
            _row(chat_id=1, user_id=4, day="2024-06-11", count=99),  # out (future)
        ]
    )
    await session.commit()
    rows = await MessageStatsRepo(session).top_users_by_messages(1, days=3, today=today, limit=10)
    assert {uid for uid, _ in rows} == {1, 2}


async def test_top_limit_is_applied(session: AsyncSession) -> None:
    session.add_all(
        [_row(chat_id=1, user_id=i, day="2024-06-01", count=100 - i) for i in range(1, 21)]
    )
    await session.commit()
    rows = await MessageStatsRepo(session).top_users_by_messages(
        1, days=1, today=date(2024, 6, 1), limit=5
    )
    assert [uid for uid, _ in rows] == [1, 2, 3, 4, 5]


async def test_top_scoped_by_chat(session: AsyncSession) -> None:
    session.add_all(
        [
            _row(chat_id=1, user_id=1, day="2024-06-01", count=10),
            _row(chat_id=2, user_id=1, day="2024-06-01", count=999),
        ]
    )
    await session.commit()
    rows = await MessageStatsRepo(session).top_users_by_messages(
        1, days=1, today=date(2024, 6, 1), limit=10
    )
    assert rows == [(1, 10)]


async def test_top_empty_window_returns_empty_list(session: AsyncSession) -> None:
    assert (
        await MessageStatsRepo(session).top_users_by_messages(
            1, days=7, today=date(2024, 6, 1), limit=10
        )
        == []
    )


async def test_top_rejects_invalid_args(session: AsyncSession) -> None:
    repo = MessageStatsRepo(session)
    with pytest.raises(ValueError, match="days"):
        await repo.top_users_by_messages(1, days=0, today=date(2024, 6, 1), limit=10)
    with pytest.raises(ValueError, match="limit"):
        await repo.top_users_by_messages(1, days=1, today=date(2024, 6, 1), limit=0)
