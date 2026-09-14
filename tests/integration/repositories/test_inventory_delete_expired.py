"""``InventoryRepo.delete_expired`` against a real SQLite file (L-26).

Covered behaviour:

* rows with ``expires`` strictly in the past are deleted;
* rows with ``expires`` in the future survive;
* rows with NULL ``expires`` (permanent entitlements) survive;
* the row exactly AT ``now`` survives (strict ``<`` boundary);
* ``used`` rows are deleted too (predicate is expiry-only, legacy parity);
* the returned count matches the number of rows actually deleted;
* a sweep with nothing due returns 0 and deletes nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import InventoryItem
from telegram_invite_bot.repositories.inventory_repo import InventoryRepo
from tests.integration.repositories._session import build_session

RepoFixture = tuple[InventoryRepo, AsyncSession]

_NOW = datetime(2026, 6, 10, 12, 0, 0)


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[RepoFixture]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as session:
        yield InventoryRepo(session), session


def _item(
    *,
    user_id: int,
    item_id: int,
    expires: datetime | None,
    used: bool = False,
) -> InventoryItem:
    return InventoryItem(
        user_id=user_id,
        item_id=item_id,
        purchase_date=_NOW - timedelta(days=30),
        used=used,
        expires=expires,
    )


async def _count(session: AsyncSession) -> int:
    stmt = select(func.count()).select_from(InventoryItem)
    return int((await session.execute(stmt)).scalar_one())


async def test_deletes_only_past_expiry(repo: RepoFixture) -> None:
    inv_repo, session = repo
    session.add(_item(user_id=1, item_id=1, expires=_NOW - timedelta(hours=1)))  # expired
    session.add(_item(user_id=1, item_id=2, expires=_NOW + timedelta(hours=1)))  # future
    session.add(_item(user_id=1, item_id=3, expires=None))  # permanent
    await session.commit()

    deleted = await inv_repo.delete_expired(_NOW)
    await session.commit()

    assert deleted == 1
    assert await _count(session) == 2
    survivors = (await session.execute(select(InventoryItem.item_id))).scalars().all()
    assert set(survivors) == {2, 3}


async def test_boundary_is_strict(repo: RepoFixture) -> None:
    inv_repo, session = repo
    session.add(_item(user_id=1, item_id=1, expires=_NOW))  # exactly now → survives
    await session.commit()

    deleted = await inv_repo.delete_expired(_NOW)
    await session.commit()

    assert deleted == 0
    assert await _count(session) == 1


async def test_used_expired_row_is_reaped(repo: RepoFixture) -> None:
    inv_repo, session = repo
    session.add(_item(user_id=1, item_id=1, expires=_NOW - timedelta(days=1), used=True))
    await session.commit()

    deleted = await inv_repo.delete_expired(_NOW)
    await session.commit()

    assert deleted == 1
    assert await _count(session) == 0


async def test_nothing_due_returns_zero(repo: RepoFixture) -> None:
    inv_repo, session = repo
    session.add(_item(user_id=1, item_id=1, expires=None))
    session.add(_item(user_id=1, item_id=2, expires=_NOW + timedelta(days=5)))
    await session.commit()

    deleted = await inv_repo.delete_expired(_NOW)
    await session.commit()

    assert deleted == 0
    assert await _count(session) == 2
