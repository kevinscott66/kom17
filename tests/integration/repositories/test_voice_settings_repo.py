"""Real-SQLite tests for :class:`VoiceSettingsRepo` (L-70).

Invariants:

* ``get`` on an unconfigured group → all defaults (transcription off).
* ``set_enabled`` / ``set_target`` / ``set_language`` upsert (create row
  on first write, update only the touched column afterwards).
* Independent column writes don't clobber each other.
* ``toggle_*`` flips in SQL, from the committed value, not a stale read (#740).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.repositories.voice_settings_repo import VoiceSettingsRepo
from tests.integration.repositories._session import build_session

_GID = -1001234567890


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, UsersBase, "users.db") as s:
        yield s


async def test_get_missing_returns_defaults(session: AsyncSession) -> None:
    settings = await VoiceSettingsRepo(session).get(_GID)
    assert settings.enabled is False
    assert settings.target == "chat"
    assert settings.language == "ru"
    assert settings.log_chat_id is None
    assert settings.auto_delete is False
    assert settings.only_admins is False


async def test_set_enabled_creates_and_toggles(session: AsyncSession) -> None:
    repo = VoiceSettingsRepo(session)
    await repo.set_enabled(_GID, enabled=True)
    assert (await repo.get(_GID)).enabled is True
    await repo.set_enabled(_GID, enabled=False)
    assert (await repo.get(_GID)).enabled is False


async def test_set_target_and_language(session: AsyncSession) -> None:
    repo = VoiceSettingsRepo(session)
    await repo.set_target(_GID, "admins")
    await repo.set_language(_GID, "en")
    settings = await repo.get(_GID)
    assert settings.target == "admins"
    assert settings.language == "en"


async def test_independent_columns_do_not_clobber(session: AsyncSession) -> None:
    repo = VoiceSettingsRepo(session)
    await repo.set_enabled(_GID, enabled=True)
    await repo.set_language(_GID, "en")
    # Enabling must survive a later language write (and vice-versa).
    settings = await repo.get(_GID)
    assert settings.enabled is True
    assert settings.language == "en"
    assert settings.target == "chat"  # untouched → default


async def test_toggle_enabled_creates_row_and_flips(session: AsyncSession) -> None:
    """First tap on an unconfigured group turns transcription on."""
    repo = VoiceSettingsRepo(session)
    assert await repo.toggle_enabled(_GID) is True
    assert (await repo.get(_GID)).enabled is True
    assert await repo.toggle_enabled(_GID) is False
    assert (await repo.get(_GID)).enabled is False


async def test_toggle_auto_delete_and_only_admins(session: AsyncSession) -> None:
    repo = VoiceSettingsRepo(session)
    assert await repo.toggle_auto_delete(_GID) is True
    assert await repo.toggle_only_admins(_GID) is True
    settings = await repo.get(_GID)
    assert settings.auto_delete is True
    assert settings.only_admins is True
    assert settings.enabled is False  # untouched by the sibling flips
    assert await repo.toggle_auto_delete(_GID) is False
    assert (await repo.get(_GID)).only_admins is True


async def test_toggle_flips_from_the_committed_value(tmp_path: Path) -> None:
    """#740: the new value comes from the database, not from an earlier read.

    The handler used to read the flag, negate it in Python and write the
    result back. This reproduces the half of that hazard which IS
    deterministic: a value read before another session's commit is stale
    by the time the write lands, and the flip must not be computed from
    it. The genuinely concurrent case — two writes racing inside one
    statement window — is argued from the transaction semantics in
    :meth:`VoiceSettingsRepo._toggle` (the read is only inside the
    ``BEGIN IMMEDIATE`` when it is part of the write statement) and is
    not reproduced here; a test that interleaves two connections mid
    statement would be timing-dependent.
    """
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'users.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(UsersBase.metadata.create_all)
        maker = async_sessionmaker(engine, expire_on_commit=False)

        async with maker() as s1, maker() as s2:
            repo1 = VoiceSettingsRepo(s1)
            # The pre-read the handler used to hold across its write.
            assert (await repo1.get(_GID)).enabled is False

            # Another admin flips it on and commits in between.
            assert await VoiceSettingsRepo(s2).toggle_enabled(_GID) is True
            await s2.commit()

            # The first tap must flip OFF, from the live row.
            assert await repo1.toggle_enabled(_GID) is False
            await s1.commit()

        async with maker() as s3:
            assert (await VoiceSettingsRepo(s3).get(_GID)).enabled is False
    finally:
        await engine.dispose()
