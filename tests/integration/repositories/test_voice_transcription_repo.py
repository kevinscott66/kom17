"""Real-SQLite tests for :class:`VoiceTranscriptionRepo` (L-70 / L-72).

Invariants:

* ``add`` persists a row with a Python-side ``created_at``.
* ``stats`` on an empty group → zeroes/``None``.
* ``stats`` aggregates ``total``, the most-recent ``last_text`` /
  ``last_created``, and the mean ``avg_processing_ms``.
* ``stats`` is group-scoped (rows for another group don't leak in).
* ``count_since`` counts only this group's rows at/after the cutoff
  (backs the per-group daily Whisper ceiling).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import VoiceTranscription
from telegram_invite_bot.repositories.voice_transcription_repo import (
    VoiceTranscriptionRepo,
)
from tests.integration.repositories._session import build_session

_GID = -1001234567890
_OTHER_GID = -1009999999999


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, UsersBase, "users.db") as s:
        yield s


async def _add(repo: VoiceTranscriptionRepo, **over: object) -> None:
    base: dict[str, object] = {
        "group_id": _GID,
        "user_id": 42,
        "message_id": 100,
        "file_id": "fid",
        "file_unique_id": "uid",
        "duration": 5,
        "transcribed_text": "hello",
        "language": "ru",
        "model_used": "whisper-1",
        "processing_time": 1000,
    }
    base.update(over)
    await repo.add(**base)  # type: ignore[arg-type]


async def test_stats_empty(session: AsyncSession) -> None:
    stats = await VoiceTranscriptionRepo(session).stats(_GID)
    assert stats.total == 0
    assert stats.last_text is None
    assert stats.last_created is None
    assert stats.avg_processing_ms is None


async def test_add_and_stats(session: AsyncSession) -> None:
    repo = VoiceTranscriptionRepo(session)
    await _add(repo, transcribed_text="first", processing_time=1000)
    await _add(repo, transcribed_text="second", processing_time=3000)
    stats = await repo.stats(_GID)
    assert stats.total == 2
    assert stats.last_text == "second"  # most recent by id
    assert stats.last_created is not None
    assert stats.avg_processing_ms == 2000  # (1000+3000)/2


async def test_stats_group_scoped(session: AsyncSession) -> None:
    repo = VoiceTranscriptionRepo(session)
    await _add(repo, group_id=_GID, transcribed_text="mine")
    await _add(repo, group_id=_OTHER_GID, transcribed_text="theirs")
    stats = await repo.stats(_GID)
    assert stats.total == 1
    assert stats.last_text == "mine"


async def test_add_handles_null_processing_time(session: AsyncSession) -> None:
    repo = VoiceTranscriptionRepo(session)
    await _add(repo, processing_time=None)
    stats = await repo.stats(_GID)
    assert stats.total == 1
    assert stats.avg_processing_ms is None


async def test_count_since_ignores_rows_before_the_cutoff(
    session: AsyncSession,
) -> None:
    """Yesterday's traffic must not eat today's Whisper allowance.

    ``add`` always stamps *now*, so the stale row is inserted with an
    explicit ``created_at`` — the same thing the previous UTC day would
    have written.
    """
    repo = VoiceTranscriptionRepo(session)
    cutoff = datetime(2026, 8, 12, 0, 0, 0)  # noqa: DTZ001 — naive UTC, as stored
    session.add(
        VoiceTranscription(
            group_id=_GID,
            user_id=42,
            created_at=cutoff - timedelta(seconds=1),
        )
    )
    await session.flush()
    assert await repo.count_since(_GID, cutoff) == 0

    session.add(VoiceTranscription(group_id=_GID, user_id=42, created_at=cutoff))
    await session.flush()
    # The boundary itself counts: ``>=`` means a row written exactly at
    # midnight belongs to the new day, not the old one.
    assert await repo.count_since(_GID, cutoff) == 1


async def test_count_since_is_group_scoped(session: AsyncSession) -> None:
    repo = VoiceTranscriptionRepo(session)
    await _add(repo, group_id=_GID)
    await _add(repo, group_id=_OTHER_GID)
    await _add(repo, group_id=_OTHER_GID)
    epoch = datetime(1970, 1, 1)  # noqa: DTZ001 — naive UTC, as stored
    assert await repo.count_since(_GID, epoch) == 1
    assert await repo.count_since(_OTHER_GID, epoch) == 2
