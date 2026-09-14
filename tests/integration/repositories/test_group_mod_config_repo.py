"""Real-SQLite tests for :class:`GroupModConfigRepo` (L-43, cluster P).

Invariants:

* ``get_or_default`` on a missing group returns the defaults view and
  does NOT persist a row (reads are side-effect-free).
* The default values mirror the moderation pipeline's hardcoded globals.
* ``set_field`` creates a row from defaults when absent, persisting only
  the one named field's override (others stay at default).
* A second ``set_field`` on the same group merges (does not reset the
  previously-set field).
* ``set_field`` rejects an unknown field name.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import ModerationBase

# Importing the model registers it on ModerationBase.metadata so
# create_all builds the group_mod_config table for these tests.
from telegram_invite_bot.db.models.group_mod_config import GroupModConfig
from telegram_invite_bot.repositories.group_mod_config_repo import GroupModConfigRepo
from tests.integration.repositories._session import build_session

_GROUP = -1001234567890


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, ModerationBase, "moderation.db") as s:
        yield s


async def test_get_or_default_missing_returns_defaults(session: AsyncSession) -> None:
    repo = GroupModConfigRepo(session)
    cfg = await repo.get_or_default(_GROUP)
    assert cfg.group_id == _GROUP
    assert cfg.automod_enabled is True
    assert cfg.profanity_enabled is True
    assert cfg.max_warns == 3
    assert cfg.mute_minutes == 1440
    assert cfg.autoban_enabled is True
    # L-54: per-group economy earn toggle defaults ON (legacy parity —
    # earning was gated only globally).
    assert cfg.coins_enabled is True


async def test_set_field_coins_enabled(session: AsyncSession) -> None:
    repo = GroupModConfigRepo(session)
    updated = await repo.set_field(group_id=_GROUP, field="coins_enabled", value=False)
    assert updated.coins_enabled is False
    reread = await repo.get_or_default(_GROUP)
    assert reread.coins_enabled is False
    # Other fields stayed at defaults.
    assert reread.automod_enabled is True
    assert reread.max_warns == 3


async def test_get_or_default_does_not_persist(session: AsyncSession) -> None:
    repo = GroupModConfigRepo(session)
    await repo.get_or_default(_GROUP)
    await session.flush()
    count = (await session.execute(select(func.count()).select_from(GroupModConfig))).scalar_one()
    assert count == 0


async def test_set_field_bool_creates_row(session: AsyncSession) -> None:
    repo = GroupModConfigRepo(session)
    updated = await repo.set_field(group_id=_GROUP, field="automod_enabled", value=False)
    assert updated.automod_enabled is False
    # Other fields stayed at defaults.
    assert updated.max_warns == 3
    assert updated.mute_minutes == 1440

    reread = await repo.get_or_default(_GROUP)
    assert reread.automod_enabled is False
    assert reread.profanity_enabled is True


async def test_set_field_int(session: AsyncSession) -> None:
    repo = GroupModConfigRepo(session)
    await repo.set_field(group_id=_GROUP, field="max_warns", value=5)
    cfg = await repo.get_or_default(_GROUP)
    assert cfg.max_warns == 5


async def test_set_field_merges_not_resets(session: AsyncSession) -> None:
    repo = GroupModConfigRepo(session)
    await repo.set_field(group_id=_GROUP, field="max_warns", value=7)
    await repo.set_field(group_id=_GROUP, field="mute_minutes", value=30)
    cfg = await repo.get_or_default(_GROUP)
    # First field preserved across the second update.
    assert cfg.max_warns == 7
    assert cfg.mute_minutes == 30
    # Untouched fields still default.
    assert cfg.autoban_enabled is True


async def test_set_field_single_row_per_group(session: AsyncSession) -> None:
    repo = GroupModConfigRepo(session)
    await repo.set_field(group_id=_GROUP, field="max_warns", value=4)
    await repo.set_field(group_id=_GROUP, field="max_warns", value=6)
    await session.flush()
    count = (await session.execute(select(func.count()).select_from(GroupModConfig))).scalar_one()
    assert count == 1


async def test_set_field_unknown_raises(session: AsyncSession) -> None:
    repo = GroupModConfigRepo(session)
    with pytest.raises(ValueError, match="unknown group_mod_config field"):
        await repo.set_field(group_id=_GROUP, field="bogus", value=1)
