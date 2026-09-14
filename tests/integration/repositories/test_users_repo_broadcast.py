"""``UsersRepo.all_user_ids`` against a real SQLite file (Cluster A2).

The /broadcast confirm handler snapshots the full audience with this
read (legacy ``SELECT user_id FROM users``, bot.py:26002). Separate file
from ``test_users_repo.py`` — that module is owned by the Stage-4 epic;
this read-method belongs to the L-94 broadcast cluster.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.repositories.users_repo import UsersRepo
from tests.integration.repositories._session import build_session

RepoFixture = tuple[UsersRepo, AsyncSession]


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[RepoFixture]:
    async with build_session(tmp_path, UsersBase, "users.db") as session:
        yield UsersRepo(session), session


async def test_all_user_ids_empty_table(repo: RepoFixture) -> None:
    users_repo, _session = repo
    assert await users_repo.all_user_ids() == []


async def test_all_user_ids_returns_every_row(repo: RepoFixture) -> None:
    users_repo, session = repo
    for uid, name in ((42, "Alice"), (43, "Bob"), (44, "Carol")):
        await users_repo.upsert_from_telegram(
            user_id=uid,
            username=name.lower(),
            first_name=name,
            last_name=None,
            language_code="en",
            is_premium=False,
        )
    await session.commit()

    ids = await users_repo.all_user_ids()
    assert sorted(ids) == [42, 43, 44]
    assert all(isinstance(uid, int) for uid in ids)
