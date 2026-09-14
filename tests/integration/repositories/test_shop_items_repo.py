"""``ShopItemsRepo.list_all`` against a real SQLite file.

Stage 16 surface is read-only, so the test set is small. We cover:

* empty catalog → empty list (handler renders the "shop empty" message).
* multi-row order matches legacy (``ORDER BY id``).
* ``stock=NULL`` collapses to ``-1`` (legacy COALESCE semantics) —
  this is the only non-trivial mapping decision in the repo.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import ShopItem
from telegram_invite_bot.repositories.shop_items_repo import ShopItemsRepo
from tests.integration.repositories._session import build_session

RepoFixture = tuple[ShopItemsRepo, AsyncSession]


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[RepoFixture]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as session:
        yield ShopItemsRepo(session), session


async def test_list_all_empty(repo: RepoFixture) -> None:
    items_repo, _ = repo
    assert await items_repo.list_all() == []


async def test_list_all_orders_by_price(repo: RepoFixture) -> None:
    """Legacy SQL is ``ORDER BY price`` (bot.py:12445/12448) — cheapest
    first matters for discovery and we must not regress to insert order.
    """
    items_repo, session = repo
    session.add_all(
        [
            ShopItem(id=1, name="Expensive", price=300, type="unwarn", stock=5),
            ShopItem(id=2, name="Cheap", price=10, type="unwarn", stock=5),
            ShopItem(id=3, name="Mid", price=100, type="unwarn", stock=5),
        ]
    )
    await session.commit()

    rows = await items_repo.list_all()
    assert [r.price for r in rows] == [10, 100, 300]
    assert [r.name for r in rows] == ["Cheap", "Mid", "Expensive"]


async def test_list_all_hides_out_of_stock_by_default(repo: RepoFixture) -> None:
    """Legacy ``ShopManager.get_all_items()`` defaults to
    ``WHERE stock > 0 OR stock = -1`` (bot.py:12448) — sold-out rows
    are hidden from /shop. The admin view passes True for visibility.
    """
    items_repo, session = repo
    session.add_all(
        [
            ShopItem(id=1, name="Sold out", price=1, type="unwarn", stock=0),
            ShopItem(id=2, name="In stock", price=2, type="unwarn", stock=5),
            ShopItem(id=3, name="Infinite", price=3, type="unwarn", stock=-1),
            ShopItem(id=4, name="NULL stock", price=4, type="unwarn", stock=None),
        ]
    )
    await session.commit()

    visible = await items_repo.list_all()
    assert {r.name for r in visible} == {"In stock", "Infinite", "NULL stock"}

    full = await items_repo.list_all(include_out_of_stock=True)
    assert {r.name for r in full} == {
        "Sold out",
        "In stock",
        "Infinite",
        "NULL stock",
    }


async def test_null_stock_collapses_to_minus_one(repo: RepoFixture) -> None:
    """Old rows seeded before ``stock`` was added land here as NULL.

    Legacy ``/shop`` COALESCEs that to -1 ("infinite"). The handler's
    stock-hint logic relies on that — guarding the contract at the
    repo boundary keeps the handler branch-free.
    """
    items_repo, session = repo
    session.add(ShopItem(id=1, name="Legacy", price=5, type="unwarn", stock=None))
    await session.commit()

    rows = await items_repo.list_all()
    assert rows[0].stock == -1


async def test_list_by_type_filters_and_orders_by_price(repo: RepoFixture) -> None:
    """``/vip_shop`` reads only ``type='vip'`` rows, cheapest first.

    Mirrors :meth:`list_all`'s price-ASC ordering and out-of-stock
    hiding, scoped to one type — non-VIP rows and sold-out VIP rows are
    excluded from the default surface.
    """
    items_repo, session = repo
    session.add_all(
        [
            ShopItem(id=1, name="VIP 3mo", price=2500, type="vip", stock=-1),
            ShopItem(id=2, name="VIP 1mo", price=1000, type="vip", stock=-1),
            ShopItem(id=3, name="Luck box", price=50, type="luck", stock=-1),
            ShopItem(id=4, name="VIP sold out", price=10, type="vip", stock=0),
        ]
    )
    await session.commit()

    vip = await items_repo.list_by_type("vip")
    # Only in-stock VIP rows, cheapest first.
    assert [r.name for r in vip] == ["VIP 1mo", "VIP 3mo"]
    # The luck row never appears in the VIP-typed surface.
    assert all(r.type == "vip" for r in vip)


async def test_list_by_type_empty_for_unknown_type(repo: RepoFixture) -> None:
    """A type with no rows returns ``[]`` so the handler renders its
    empty-state rather than crashing.
    """
    items_repo, session = repo
    session.add(ShopItem(id=1, name="VIP", price=100, type="vip", stock=-1))
    await session.commit()

    assert await items_repo.list_by_type("nonexistent") == []


async def test_list_by_type_includes_out_of_stock_when_asked(repo: RepoFixture) -> None:
    """Admin-style view surfaces sold-out VIP rows too, mirroring
    :meth:`list_all`'s ``include_out_of_stock`` flag.
    """
    items_repo, session = repo
    session.add_all(
        [
            ShopItem(id=1, name="VIP live", price=100, type="vip", stock=-1),
            ShopItem(id=2, name="VIP gone", price=50, type="vip", stock=0),
        ]
    )
    await session.commit()

    full = await items_repo.list_by_type("vip", include_out_of_stock=True)
    assert {r.name for r in full} == {"VIP live", "VIP gone"}


async def test_description_defaults_to_empty_string(repo: RepoFixture) -> None:
    """Handler concatenates ``description`` into HTML without a None-guard.

    The repo coerces ``NULL`` → ``""`` to keep the handler boring.
    """
    items_repo, session = repo
    session.add(ShopItem(id=1, name="X", description=None, price=1, type="unwarn", stock=1))
    await session.commit()

    rows = await items_repo.list_all()
    assert rows[0].description == ""
