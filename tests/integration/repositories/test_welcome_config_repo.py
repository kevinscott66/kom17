"""Real-SQLite tests for :class:`WelcomeConfigRepo` (L-57).

Invariants:

* ``get`` returns ``None`` for an unconfigured group.
* ``set_template`` upserts the template and forces ``enabled=True``.
* ``set_template`` after ``/welcome_off`` re-enables (no silent suppression).
* ``set_enabled`` toggles the flag, creating a row if none exists.
* ``clear`` removes the row entirely.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import ModerationBase
from telegram_invite_bot.repositories.welcome_config_repo import WelcomeConfigRepo
from tests.integration.repositories._session import build_session

# Supergroup id beyond 32-bit range — proves the BigInteger PK round-trips.
_GID = -1001234567890


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, ModerationBase, "moderation.db") as s:
        yield s


async def test_get_missing_returns_none(session: AsyncSession) -> None:
    repo = WelcomeConfigRepo(session)
    assert await repo.get(_GID) is None


async def test_set_template_inserts_enabled(session: AsyncSession) -> None:
    repo = WelcomeConfigRepo(session)
    await repo.set_template(_GID, "Hi {user}, welcome to {chat}!")
    row = await repo.get(_GID)
    assert row is not None
    assert row.group_id == _GID
    assert row.template == "Hi {user}, welcome to {chat}!"
    assert row.enabled is True


async def test_set_template_updates_existing(session: AsyncSession) -> None:
    repo = WelcomeConfigRepo(session)
    await repo.set_template(_GID, "first")
    await repo.set_template(_GID, "second")
    row = await repo.get(_GID)
    assert row is not None
    assert row.template == "second"


async def test_set_template_reenables_after_off(session: AsyncSession) -> None:
    repo = WelcomeConfigRepo(session)
    await repo.set_template(_GID, "hello")
    await repo.set_enabled(_GID, enabled=False)
    # Re-setting a template implies the admin wants it live again.
    await repo.set_template(_GID, "hello again")
    row = await repo.get(_GID)
    assert row is not None
    assert row.enabled is True


async def test_set_enabled_creates_row_when_absent(session: AsyncSession) -> None:
    repo = WelcomeConfigRepo(session)
    await repo.set_enabled(_GID, enabled=False)
    row = await repo.get(_GID)
    assert row is not None
    assert row.enabled is False
    assert row.template is None


async def test_set_enabled_toggle(session: AsyncSession) -> None:
    repo = WelcomeConfigRepo(session)
    await repo.set_template(_GID, "x")
    await repo.set_enabled(_GID, enabled=False)
    assert (await repo.get(_GID)).enabled is False  # type: ignore[union-attr]
    await repo.set_enabled(_GID, enabled=True)
    assert (await repo.get(_GID)).enabled is True  # type: ignore[union-attr]


async def test_clear_removes_row(session: AsyncSession) -> None:
    repo = WelcomeConfigRepo(session)
    await repo.set_template(_GID, "x")
    await repo.clear(_GID)
    assert await repo.get(_GID) is None
