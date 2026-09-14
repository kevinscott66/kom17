"""``MessageStatsRepo`` against a real SQLite file.

Read-only at Stage 9 — writes (the per-message recorder) stay in legacy
until the activity-recording path migrates. The test seeds rows
directly via the ORM model and checks aggregation / windowing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import MessageStatsBase
from telegram_invite_bot.db.models.message_stats import MessageCount
from telegram_invite_bot.repositories.message_stats_repo import (
    DailyCount,
    MessageStatsRepo,
)
from tests.integration.repositories._session import build_session

RepoFixture = tuple[MessageStatsRepo, AsyncSession]


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[RepoFixture]:
    async with build_session(tmp_path, MessageStatsBase, "message_stats.db") as session:
        yield MessageStatsRepo(session), session


async def _seed(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int,
    day: date,
    count: int,
) -> None:
    session.add(
        MessageCount(
            user_id=user_id,
            chat_id=chat_id,
            date=day.isoformat(),
            count=count,
            last_message=datetime(2024, 1, 1, 12, 0, 0),
        )
    )
    await session.commit()


async def test_total_for_user_returns_zero_when_no_rows(repo: RepoFixture) -> None:
    stats_repo, _ = repo
    assert await stats_repo.total_for_user(42, -100) == 0


async def test_total_for_user_sums_all_days(repo: RepoFixture) -> None:
    stats_repo, session = repo
    today = date(2024, 6, 1)
    await _seed(session, user_id=42, chat_id=-100, day=today, count=10)
    await _seed(session, user_id=42, chat_id=-100, day=today - timedelta(days=1), count=5)
    await _seed(session, user_id=42, chat_id=-100, day=today - timedelta(days=30), count=3)
    # Row for a different user must NOT leak in.
    await _seed(session, user_id=99, chat_id=-100, day=today, count=1_000_000)
    # Row for a different chat must NOT leak in.
    await _seed(session, user_id=42, chat_id=-200, day=today, count=999)

    assert await stats_repo.total_for_user(42, -100) == 18


async def test_count_for_days_windowing(repo: RepoFixture) -> None:
    stats_repo, session = repo
    today = date(2024, 6, 10)
    for offset, count in enumerate([7, 5, 3, 11, 0, 2, 1, 4242]):
        await _seed(
            session,
            user_id=1,
            chat_id=1,
            day=today - timedelta(days=offset),
            count=count,
        )
    # days=1 → today only.
    assert await stats_repo.count_for_days(1, 1, days=1, today=today) == 7
    # days=7 → today + previous 6 → 7+5+3+11+0+2+1 = 29 (excludes the 4242 day).
    assert await stats_repo.count_for_days(1, 1, days=7, today=today) == 29


async def test_count_for_days_ignores_rows_dated_after_today(repo: RepoFixture) -> None:
    """The window is closed at the top, not just at the bottom.

    A row stamped later than ``today`` is reachable whenever a chat's
    counters were written under a ``STATS_TIMEZONE`` ahead of the one the
    caller passes now. It must not leak into any window — otherwise the
    same period reads one number here and another via ``last_n_days``,
    which has always been bounded on both ends.
    """
    stats_repo, session = repo
    today = date(2024, 6, 10)
    await _seed(session, user_id=1, chat_id=1, day=today, count=7)
    await _seed(session, user_id=1, chat_id=1, day=today + timedelta(days=1), count=4242)
    assert await stats_repo.count_for_days(1, 1, days=1, today=today) == 7
    assert await stats_repo.count_for_days(1, 1, days=30, today=today) == 7
    # ``total_for_user`` is windowless by contract and still sees it.
    assert await stats_repo.total_for_user(1, 1) == 4249


async def test_count_for_days_rejects_zero(repo: RepoFixture) -> None:
    stats_repo, _ = repo
    with pytest.raises(ValueError, match="days must be >= 1"):
        await stats_repo.count_for_days(1, 1, days=0, today=date(2024, 6, 1))


async def test_last_n_days_returns_newest_first_and_skips_missing(
    repo: RepoFixture,
) -> None:
    stats_repo, session = repo
    today = date(2024, 6, 10)
    # Seed only days {today, today-2, today-6}; today-1, today-3..5 missing.
    await _seed(session, user_id=1, chat_id=1, day=today, count=10)
    await _seed(session, user_id=1, chat_id=1, day=today - timedelta(days=2), count=4)
    await _seed(session, user_id=1, chat_id=1, day=today - timedelta(days=6), count=1)
    # Out of window: today-7
    await _seed(session, user_id=1, chat_id=1, day=today - timedelta(days=7), count=999)

    rows = await stats_repo.last_n_days(1, 1, days=7, today=today)
    assert rows == [
        DailyCount(date="2024-06-10", count=10),
        DailyCount(date="2024-06-08", count=4),
        DailyCount(date="2024-06-04", count=1),
    ]


async def test_count_since_inclusive(repo: RepoFixture) -> None:
    stats_repo, session = repo
    today = date(2024, 6, 10)
    await _seed(session, user_id=1, chat_id=1, day=today, count=3)
    await _seed(session, user_id=1, chat_id=1, day=today - timedelta(days=1), count=5)
    await _seed(session, user_id=1, chat_id=1, day=today - timedelta(days=2), count=7)

    # since=today-1 → today and yesterday: 3+5 = 8 (excludes day -2).
    assert await stats_repo.count_since(1, 1, since=today - timedelta(days=1)) == 8


async def test_chat_totals_by_date_sums_across_users(repo: RepoFixture) -> None:
    """Per-chat aggregation: rows for different users on the same day collapse."""
    stats_repo, session = repo
    today = date(2024, 6, 10)
    await _seed(session, user_id=1, chat_id=-100, day=today, count=3)
    await _seed(session, user_id=2, chat_id=-100, day=today, count=4)
    await _seed(session, user_id=1, chat_id=-100, day=today - timedelta(days=1), count=10)
    # Different chat must NOT leak in.
    await _seed(session, user_id=1, chat_id=-200, day=today, count=999)

    rows = await stats_repo.chat_totals_by_date(-100, days=7, today=today)
    assert rows == [
        DailyCount(date="2024-06-10", count=7),  # 3 + 4
        DailyCount(date="2024-06-09", count=10),
    ]


async def test_chat_totals_by_date_empty_returns_empty(repo: RepoFixture) -> None:
    stats_repo, _ = repo
    assert await stats_repo.chat_totals_by_date(-100, days=7, today=date(2024, 6, 10)) == []


async def test_chat_totals_by_date_excludes_future_rows(repo: RepoFixture) -> None:
    """A row dated *after* ``today`` (e.g. clock skew during seed) must be ignored.

    Otherwise the card would render a future date and the total would
    drift in the user's favour. The repo clamps the upper bound to
    ``today.isoformat()`` for exactly this case.
    """
    stats_repo, session = repo
    today = date(2024, 6, 10)
    await _seed(session, user_id=1, chat_id=-100, day=today, count=3)
    await _seed(session, user_id=1, chat_id=-100, day=today + timedelta(days=1), count=999)

    rows = await stats_repo.chat_totals_by_date(-100, days=7, today=today)
    assert rows == [DailyCount(date="2024-06-10", count=3)]


async def test_chat_totals_by_date_rejects_zero(repo: RepoFixture) -> None:
    stats_repo, _ = repo
    with pytest.raises(ValueError, match="days must be >= 1"):
        await stats_repo.chat_totals_by_date(-100, days=0, today=date(2024, 6, 10))


async def test_last_n_days_with_days_1(repo: RepoFixture) -> None:
    """``days=1`` returns just today's row (or empty), no leakage."""
    stats_repo, session = repo
    today = date(2024, 6, 10)
    await _seed(session, user_id=1, chat_id=1, day=today, count=11)
    await _seed(session, user_id=1, chat_id=1, day=today - timedelta(days=1), count=999)

    rows = await stats_repo.last_n_days(1, 1, days=1, today=today)
    assert rows == [DailyCount(date="2024-06-10", count=11)]


# ── newcomer_count (RR-1 #6 /chatstats 👥 block) ─────────────────────


async def test_newcomer_count_only_counts_first_ever_appearances(
    repo: RepoFixture,
) -> None:
    """A veteran who posted this week is NOT new; a first-timer is.

    This is the assertion that separates the honest implementation from
    the naive one: filtering rows to the window *before* grouping would
    make user 1 look new too, since their earliest in-window row is
    trivially in the window.
    """
    stats_repo, session = repo
    today = date(2024, 6, 10)
    # Veteran: first seen a month ago, still active today.
    await _seed(session, user_id=1, chat_id=-100, day=today - timedelta(days=30), count=5)
    await _seed(session, user_id=1, chat_id=-100, day=today, count=2)
    # Newcomer: first (and only) row is inside the week.
    await _seed(session, user_id=2, chat_id=-100, day=today - timedelta(days=1), count=4)

    assert await stats_repo.newcomer_count(-100, days=7, today=today) == 1


async def test_newcomer_count_is_scoped_to_the_chat(repo: RepoFixture) -> None:
    """Being an old-timer elsewhere doesn't make you a veteran here."""
    stats_repo, session = repo
    today = date(2024, 6, 10)
    await _seed(session, user_id=1, chat_id=-200, day=today - timedelta(days=90), count=50)
    await _seed(session, user_id=1, chat_id=-100, day=today, count=1)

    assert await stats_repo.newcomer_count(-100, days=7, today=today) == 1
    assert await stats_repo.newcomer_count(-200, days=7, today=today) == 0


async def test_newcomer_count_ignores_rows_dated_after_today(repo: RepoFixture) -> None:
    """Clock skew must not conjure newcomers out of the future."""
    stats_repo, session = repo
    today = date(2024, 6, 10)
    await _seed(session, user_id=1, chat_id=-100, day=today + timedelta(days=1), count=3)

    assert await stats_repo.newcomer_count(-100, days=7, today=today) == 0


async def test_newcomer_count_empty_chat_is_zero(repo: RepoFixture) -> None:
    stats_repo, _ = repo
    assert await stats_repo.newcomer_count(-100, days=7, today=date(2024, 6, 10)) == 0


async def test_newcomer_count_rejects_zero_days(repo: RepoFixture) -> None:
    stats_repo, _ = repo
    with pytest.raises(ValueError, match="days must be >= 1"):
        await stats_repo.newcomer_count(-100, days=0, today=date(2024, 6, 10))
