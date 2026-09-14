"""``InventoryRepo.list_for_user`` against a real SQLite file.

Read-only Stage 16 surface. Covered behaviour:

* empty list for users with no purchases.
* JOIN with ``shop_items`` so we get the item name (the handler
  doesn't issue a second query per row).
* ``ORDER BY purchase_date DESC`` matches the legacy query.
* ``LIMIT 30`` is this port's own cap (``InventoryRepo._DEFAULT_LIMIT``),
  not a legacy one. #1657: the parity claim here cited
  ``bot/handlers/shop.py``, a directory that has never existed, and
  legacy's ``InventoryManager.get_user_inventory`` issues no ``LIMIT``
  whatsoever — it fetches every row and drops the expired ones in
  Python afterwards.
* ``used`` round-trips as a bool — SQLite stores it as INTEGER and
  legacy writes ``0``/``1``; ``bool(row.used)`` in the repo coerces
  cleanly, but we assert it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import InventoryItem, ShopItem
from telegram_invite_bot.repositories.inventory_repo import InventoryRepo
from tests.integration.repositories._session import build_session

RepoFixture = tuple[InventoryRepo, AsyncSession]


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[RepoFixture]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as session:
        yield InventoryRepo(session), session


async def _seed_item(session: AsyncSession, *, id_: int, name: str) -> None:
    session.add(ShopItem(id=id_, name=name, price=10, type="unwarn", stock=-1))


async def test_list_for_user_empty(repo: RepoFixture) -> None:
    inv_repo, _ = repo
    assert await inv_repo.list_for_user(42) == []


async def test_list_for_user_joins_item_name(repo: RepoFixture) -> None:
    inv_repo, session = repo
    await _seed_item(session, id_=1, name="Plushie")
    session.add(InventoryItem(user_id=42, item_id=1, purchase_date=datetime(2025, 1, 1, 12, 0, 0)))
    await session.commit()

    rows = await inv_repo.list_for_user(42)
    assert len(rows) == 1
    assert rows[0].item_name == "Plushie"
    assert rows[0].used is False


async def test_list_for_user_orders_desc(repo: RepoFixture) -> None:
    inv_repo, session = repo
    await _seed_item(session, id_=1, name="X")
    base = datetime(2025, 1, 1, 0, 0, 0)
    for offset in (0, 5, 2):
        session.add(
            InventoryItem(user_id=42, item_id=1, purchase_date=base + timedelta(days=offset))
        )
    await session.commit()

    rows = await inv_repo.list_for_user(42)
    dates = [r.purchase_date for r in rows]
    assert dates == sorted(dates, reverse=True)


async def test_list_for_user_respects_limit(repo: RepoFixture) -> None:
    inv_repo, session = repo
    await _seed_item(session, id_=1, name="X")
    base = datetime(2025, 1, 1, 0, 0, 0)
    for i in range(35):
        session.add(InventoryItem(user_id=42, item_id=1, purchase_date=base + timedelta(minutes=i)))
    await session.commit()

    rows = await inv_repo.list_for_user(42)
    assert len(rows) == 30  # default legacy cap


async def test_list_for_user_isolates_users(repo: RepoFixture) -> None:
    inv_repo, session = repo
    await _seed_item(session, id_=1, name="X")
    session.add(InventoryItem(user_id=1, item_id=1, purchase_date=datetime(2025, 1, 1)))
    session.add(InventoryItem(user_id=2, item_id=1, purchase_date=datetime(2025, 1, 2)))
    await session.commit()

    assert len(await inv_repo.list_for_user(1)) == 1
    assert len(await inv_repo.list_for_user(2)) == 1


async def test_used_items_hidden_by_default(repo: RepoFixture) -> None:
    """Legacy ``/inventory`` calls with ``include_used=False``
    (bot.py:23867) — used items disappear from the user-facing view.
    Admin/debug callers can flip the flag to see them.
    """
    inv_repo, session = repo
    await _seed_item(session, id_=1, name="X")
    session.add(InventoryItem(user_id=42, item_id=1, purchase_date=datetime(2025, 1, 1), used=True))
    session.add(
        InventoryItem(user_id=42, item_id=1, purchase_date=datetime(2025, 1, 2), used=False)
    )
    await session.commit()

    default_rows = await inv_repo.list_for_user(42)
    assert [r.purchase_date for r in default_rows] == [datetime(2025, 1, 2)]
    assert default_rows[0].used is False

    all_rows = await inv_repo.list_for_user(42, include_used=True)
    assert len(all_rows) == 2
    # ``used`` round-trips as a real bool even though SQLite stores INTEGER.
    assert {r.used for r in all_rows} == {True, False}


async def test_null_used_is_hidden_like_legacy(repo: RepoFixture) -> None:
    """Prod schema is ``used BOOLEAN DEFAULT 0`` (docs/prod_schemas.sql)
    so a ``NULL`` is a malformed legacy row. Legacy ``AND i.used = 0``
    excludes NULL by standard SQL three-valued logic — we must too,
    or a user would suddenly see rows legacy never showed them.

    Seeding via raw SQL because the ORM applies ``default=False`` on
    insert; ``InventoryItem(used=None)`` would silently become 0.
    """
    from sqlalchemy import text

    inv_repo, session = repo
    await _seed_item(session, id_=1, name="X")
    await session.execute(
        text(
            "INSERT INTO inventory (user_id, item_id, purchase_date, used) "
            "VALUES (42, 1, '2025-01-01 00:00:00', NULL)"
        )
    )
    session.add(
        InventoryItem(user_id=42, item_id=1, purchase_date=datetime(2025, 1, 2), used=False)
    )
    await session.commit()

    default_rows = await inv_repo.list_for_user(42)
    assert [r.purchase_date for r in default_rows] == [datetime(2025, 1, 2)]

    all_rows = await inv_repo.list_for_user(42, include_used=True)
    assert len(all_rows) == 2


async def test_consume_marks_entry_used_and_returns_true(repo: RepoFixture) -> None:
    """Stage 28: race-safe consume marks the row and signals success
    via the bool return. The follow-up SELECT confirms ``used = True``
    and ``used_date = now`` so an admin audit query that filters on
    ``used_date`` (legacy parity at bot.py:13166) sees the timestamp.
    """
    inv_repo, session = repo
    await _seed_item(session, id_=1, name="X")
    session.add(InventoryItem(user_id=42, item_id=1, purchase_date=datetime(2025, 1, 1)))
    await session.commit()
    entry_id = (await session.execute(select(InventoryItem.id))).scalar_one()

    now = datetime(2025, 6, 1, 12, 0, 0)
    won = await inv_repo.consume(user_id=42, inventory_id=entry_id, now=now)
    await session.commit()

    assert won is True
    row = await session.get(InventoryItem, entry_id)
    assert row is not None
    assert row.used is True
    assert row.used_date == now


async def test_consume_second_call_loses_race(repo: RepoFixture) -> None:
    """The race-safety guarantee Stage 28 hinges on: a second consume
    of the same (user, entry) pair, after the first won, must return
    False. The WHERE ``used = 0`` filters the row out — rowcount is 0.
    """
    inv_repo, session = repo
    await _seed_item(session, id_=1, name="X")
    session.add(InventoryItem(user_id=42, item_id=1, purchase_date=datetime(2025, 1, 1)))
    await session.commit()
    entry_id = (await session.execute(select(InventoryItem.id))).scalar_one()

    now = datetime(2025, 6, 1, 12, 0, 0)
    first = await inv_repo.consume(user_id=42, inventory_id=entry_id, now=now)
    second = await inv_repo.consume(user_id=42, inventory_id=entry_id, now=now)
    await session.commit()

    assert first is True
    assert second is False


async def test_consume_wrong_user_returns_false(repo: RepoFixture) -> None:
    """Defence-in-depth: a hand-crafted callback that reaches consume
    with another user's entry_id must fail rather than steal the row.
    The service layer already checks ownership via ``get_for_user``;
    this re-check on the WHERE clause makes the auth posture robust
    to a future refactor that drops the upstream guard.
    """
    inv_repo, session = repo
    await _seed_item(session, id_=1, name="X")
    session.add(InventoryItem(user_id=42, item_id=1, purchase_date=datetime(2025, 1, 1)))
    await session.commit()
    entry_id = (await session.execute(select(InventoryItem.id))).scalar_one()

    now = datetime(2025, 6, 1, 12, 0, 0)
    stolen = await inv_repo.consume(user_id=999, inventory_id=entry_id, now=now)
    await session.commit()

    assert stolen is False
    row = await session.get(InventoryItem, entry_id)
    assert row is not None
    assert row.used is False  # original owner's row untouched


async def test_expired_items_hidden_by_default(repo: RepoFixture) -> None:
    """Legacy filters ``expires < now`` (bot.py:12919). Pin the same
    contract at the repo so the handler doesn't have to know about
    expiry semantics.
    """
    inv_repo, session = repo
    await _seed_item(session, id_=1, name="X")
    now = datetime(2025, 6, 1, 12, 0, 0)
    session.add(
        InventoryItem(
            user_id=42,
            item_id=1,
            purchase_date=datetime(2025, 1, 1),
            expires=datetime(2025, 5, 1),  # expired before ``now``
        )
    )
    session.add(
        InventoryItem(
            user_id=42,
            item_id=1,
            purchase_date=datetime(2025, 2, 1),
            expires=datetime(2025, 7, 1),  # still valid
        )
    )
    session.add(
        InventoryItem(
            user_id=42,
            item_id=1,
            purchase_date=datetime(2025, 3, 1),
            expires=None,  # never expires
        )
    )
    await session.commit()

    default_rows = await inv_repo.list_for_user(42, now=now)
    assert {r.purchase_date for r in default_rows} == {
        datetime(2025, 2, 1),
        datetime(2025, 3, 1),
    }

    all_rows = await inv_repo.list_for_user(42, now=now, include_expired=True)
    assert len(all_rows) == 3
