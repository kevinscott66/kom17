"""``AiQuotaRepo`` — focused integration tests for M-P-2.

The audit (``audits/01_profile_stats_ai_vip.md`` SEV-2, tracked to
closure as M-P-2 in ``audits/03_after_iter2.md``) requires that
``ai_daily_requests`` be a real per-user, per-day counter — not an
in-process counter and not a wallet-side ledger.

The day boundary is the LOCAL calendar day, matching legacy's
``date.today().isoformat()`` on the same table (bot.py:38478 read,
bot.py:38487 write). Keying it on the UTC day instead would split one
MSK production day across two rows and move the reset to 03:00 MSK.

What we test:

* :meth:`get_and_increment` lands count=1 on a cold row.
* Subsequent calls on the same day bump the same row to 2, 3 ...
* A "next day" call starts a fresh row at 1 so the calendar boundary
  resets the quota.
* An *aware* ``now`` is converted to the host zone, not to UTC.
* Two users on the same day are isolated.
* :meth:`release` (#1965) gives back exactly one slot and refuses to
  drive the counter below zero.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.ai_quota import AiDailyRequest  # noqa: F401
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.repositories.ai_quota_repo import AiQuotaRepo, _today_iso
from tests.integration.repositories._session import build_session


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, UsersBase, "users.db") as s:
        yield s


async def test_cold_row_lands_at_one(session: AsyncSession) -> None:
    repo = AiQuotaRepo(session)
    now = datetime(2026, 5, 27, 10, 0)  # noqa: DTZ001 — naive local, as stored
    assert await repo.get_and_increment(42, now=now) == 1


async def test_consecutive_increments_bump_same_row(session: AsyncSession) -> None:
    repo = AiQuotaRepo(session)
    now = datetime(2026, 5, 27, 10, 0)  # noqa: DTZ001 — naive local, as stored
    counts = [await repo.get_and_increment(42, now=now) for _ in range(5)]
    assert counts == [1, 2, 3, 4, 5]


async def test_next_local_day_starts_fresh_row(session: AsyncSession) -> None:
    repo = AiQuotaRepo(session)
    day1 = datetime(2026, 5, 27, 23, 30)  # noqa: DTZ001 — naive local wall clock
    day2 = day1 + timedelta(hours=1)  # crosses local midnight
    await repo.get_and_increment(42, now=day1)
    await repo.get_and_increment(42, now=day1)
    # Same calendar day → 3
    assert await repo.get_and_increment(42, now=day1) == 3
    # Next local day → fresh row, back to 1
    assert await repo.get_and_increment(42, now=day2) == 1


def test_aware_now_is_read_in_local_time_not_utc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """22:30 UTC is already *tomorrow* in MSK — the row must follow MSK.

    Guards the legacy-parity boundary: keying on the UTC day would
    file this call under 05-27 while legacy's ``date.today()`` on the
    production host files it under 05-28.
    """
    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    try:
        aware = datetime(2026, 5, 27, 22, 30, tzinfo=UTC)
        assert _today_iso(aware) == "2026-05-28"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_default_now_follows_the_host_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """No ``now`` → the host's local calendar day, same as legacy."""
    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    try:
        before = datetime.now(UTC).astimezone().date().isoformat()
        got = _today_iso()
        after = datetime.now(UTC).astimezone().date().isoformat()
        # Tolerate the (vanishingly rare) midnight tick between reads.
        assert got in {before, after}
    finally:
        monkeypatch.undo()
        time.tzset()


async def test_users_are_isolated(session: AsyncSession) -> None:
    repo = AiQuotaRepo(session)
    now = datetime(2026, 5, 27, 10, 0)  # noqa: DTZ001 — naive local, as stored
    assert await repo.get_and_increment(1, now=now) == 1
    assert await repo.get_and_increment(2, now=now) == 1
    assert await repo.get_and_increment(1, now=now) == 2
    assert await repo.get_and_increment(2, now=now) == 2


async def test_release_gives_back_one_slot(session: AsyncSession) -> None:
    """#1965: the narrow inverse of the increment.

    Only the cancellation path in ``handlers/ai.py`` may call it — a
    slot spent on a *failed* upstream stays spent by design.
    """
    repo = AiQuotaRepo(session)
    now = datetime(2026, 5, 27, 10, 0)  # noqa: DTZ001 — naive local, as stored
    await repo.get_and_increment(42, now=now)
    await repo.get_and_increment(42, now=now)

    assert await repo.release(42, now=now) is True
    # The next consume reads 2, so exactly one slot came back.
    assert await repo.get_and_increment(42, now=now) == 2


async def test_release_cannot_drive_the_counter_negative(session: AsyncSession) -> None:
    """A double release, or one for a day with no row, must be a no-op.

    The ``count > 0`` predicate is what makes the return value
    meaningful: ``False`` says the guard held, not that the write
    failed. Without it a stray release would hand out a free slot on
    the next day's first call, since the row would start at -1.
    """
    repo = AiQuotaRepo(session)
    now = datetime(2026, 5, 27, 10, 0)  # noqa: DTZ001 — naive local, as stored

    # No row at all for this user/day.
    assert await repo.release(99, now=now) is False

    await repo.get_and_increment(99, now=now)
    assert await repo.release(99, now=now) is True
    assert await repo.release(99, now=now) is False
    assert await repo.get_and_increment(99, now=now) == 1


async def test_release_is_scoped_to_the_day_it_names(session: AsyncSession) -> None:
    """Yesterday's row is a different row; releasing today must not
    reach it. The counter's day is LOCAL (see ``_today_iso``)."""
    repo = AiQuotaRepo(session)
    day1 = datetime(2026, 5, 27, 23, 30)  # noqa: DTZ001 — naive local wall clock
    day2 = day1 + timedelta(hours=1)
    await repo.get_and_increment(7, now=day1)

    assert await repo.release(7, now=day2) is False
    assert await repo.get_and_increment(7, now=day1) == 2
