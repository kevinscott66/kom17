"""Real-SQLite tests for :class:`RankRepo` (ranks epic R1).

Invariants:

* ``merged_matrix`` with an empty table == the in-code legacy defaults
  (delta-only semantics — empty DB means "virgin settings.json");
* DB rows OVERLAY the defaults (widen or narrow a single cell) without
  mutating ``DEFAULT_RANK_PERMISSIONS`` itself;
* both reads are TTL-cached module-level; writers invalidate, and the
  ``clear_*`` hooks give tests deterministic isolation;
* command overrides: set/overwrite/reset-one/reset-all round-trips.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.core.ranks import DEFAULT_RANK_PERMISSIONS
from telegram_invite_bot.db.models.base import ModerationBase

# Importing the models registers them on ModerationBase.metadata so
# create_all builds the rank tables for these tests.
from telegram_invite_bot.db.models.rank_tables import (
    CommandRankOverride,
    RankPermissionOverride,
)
from telegram_invite_bot.repositories.rank_repo import (
    RankRepo,
    clear_command_override_cache,
    clear_rank_matrix_cache,
)
from tests.integration.repositories._session import build_session


@pytest.fixture(autouse=True)
def _isolate_caches() -> None:
    clear_rank_matrix_cache()
    clear_command_override_cache()


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, ModerationBase, "moderation.db") as s:
        yield s


async def test_empty_table_yields_pure_defaults(session: AsyncSession) -> None:
    matrix = await RankRepo(session).merged_matrix()
    assert matrix == DEFAULT_RANK_PERMISSIONS
    # Equal by value but NOT the same dicts — callers must not be able
    # to reach the in-code defaults through the merged result.
    assert matrix is not DEFAULT_RANK_PERMISSIONS
    assert matrix[2] is not DEFAULT_RANK_PERMISSIONS[2]


async def test_override_overlays_one_cell_without_touching_defaults(
    session: AsyncSession,
) -> None:
    repo = RankRepo(session)
    assert DEFAULT_RANK_PERMISSIONS[2]["can_ban"] is False
    await repo.set_permission(2, "can_ban", True)
    matrix = await repo.merged_matrix()
    assert matrix[2]["can_ban"] is True
    # Sibling cells and other ranks untouched.
    assert matrix[2]["can_warn"] is True
    assert matrix[3] == DEFAULT_RANK_PERMISSIONS[3]
    # The in-code constant must never be mutated by an overlay.
    assert DEFAULT_RANK_PERMISSIONS[2]["can_ban"] is False


async def test_override_can_target_rank_without_default_row(
    session: AsyncSession,
) -> None:
    repo = RankRepo(session)
    # Rank 0 has no defaults row; an explicit grant creates one.
    await repo.set_permission(0, "can_warn", True)
    matrix = await repo.merged_matrix()
    assert matrix[0] == {"can_warn": True}


async def test_set_permission_upserts_same_cell(session: AsyncSession) -> None:
    repo = RankRepo(session)
    await repo.set_permission(2, "can_ban", True)
    await repo.set_permission(2, "can_ban", False)
    matrix = await repo.merged_matrix()
    assert matrix[2]["can_ban"] is False


async def test_matrix_read_is_cached_until_cleared(session: AsyncSession) -> None:
    repo = RankRepo(session)
    await repo.merged_matrix()  # fill cache
    # Write a row BEHIND the repo's back (raw ORM insert — no
    # invalidation), prove the cached merge is served, then clear.
    session.add(RankPermissionOverride(rank=1, permission="can_mute", allowed=1))
    await session.flush()
    assert (await repo.merged_matrix())[1]["can_mute"] is False  # stale by design
    clear_rank_matrix_cache()
    assert (await repo.merged_matrix())[1]["can_mute"] is True


async def test_set_permission_leaves_invalidation_to_the_caller(
    session: AsyncSession,
) -> None:
    """The writer must NOT clear the cache itself.

    Clearing inside the still-open transaction lets a concurrent update
    re-fill the cache from the pre-write row for a whole fresh TTL; the
    handler clears after ``session_for`` commits instead. Pinning the
    contract here keeps someone from "helpfully" moving the clear back
    into the repo.
    """
    repo = RankRepo(session)
    await repo.merged_matrix()  # fill cache
    await repo.set_permission(2, "can_ban", True)
    assert (await repo.merged_matrix())[2]["can_ban"] is False  # stale by design
    clear_rank_matrix_cache()
    assert (await repo.merged_matrix())[2]["can_ban"] is True


async def test_command_overrides_roundtrip(session: AsyncSession) -> None:
    repo = RankRepo(session)
    assert await repo.command_overrides() == {}
    clear_command_override_cache()

    # Every read below needs its own clear: invalidation is the
    # caller's job now (see the repo module docstring).
    await repo.set_command_override("ban", 3)
    clear_command_override_cache()
    assert await repo.command_overrides() == {"ban": 3}
    await repo.set_command_override("ban", 6)  # upsert overwrites
    clear_command_override_cache()
    assert await repo.command_overrides() == {"ban": 6}

    assert await repo.reset_command_override("ban") is True
    assert await repo.reset_command_override("ban") is False  # already gone
    clear_command_override_cache()
    assert await repo.command_overrides() == {}


async def test_reset_all_command_overrides(session: AsyncSession) -> None:
    repo = RankRepo(session)
    await repo.set_command_override("ban", 3)
    await repo.set_command_override("warn", 1)
    assert await repo.reset_all_command_overrides() == 2
    clear_command_override_cache()
    assert await repo.command_overrides() == {}


async def test_command_overrides_cached_until_cleared(session: AsyncSession) -> None:
    repo = RankRepo(session)
    await repo.command_overrides()  # fill cache (empty)
    session.add(CommandRankOverride(command_key="warn", min_rank=4))
    await session.flush()
    assert await repo.command_overrides() == {}  # stale by design
    clear_command_override_cache()
    assert await repo.command_overrides() == {"warn": 4}
