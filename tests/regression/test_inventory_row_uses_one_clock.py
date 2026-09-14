"""#1951: every datetime column of an ``inventory`` row must share one frame.

``PurchaseService.purchase`` derived ONE ``now`` — naive UTC — and wrote
it to two columns in two different tables. That is right for
``transactions.date`` (naive UTC at every writer in the repo) and wrong
for ``inventory.purchase_date``: the rest of that table is naive LOCAL
by explicit contract. ``used_date`` comes from the handlers'
``datetime.now()  # noqa: DTZ005 — match legacy naive convention``
(``handlers/shop.py``, ``handlers/custom_title.py``) and ``expires`` is
filtered against the same clock in ``InventoryRepo.list_for_user``.

Measured on the live prod ``economy.db`` — row 48, one item bought and
used within 53 ms of each other::

    purchase_date = 2026-08-28 10:56:49.316504
    used_date     = 2026-08-28 13:56:49.369374

Three hours apart inside a single row, because the host is MSK. The
legacy-written rows above it (42, 43, ...) hold ``purchase_date ==
used_date`` to the second, so the port is the one that drifted.

Nothing compares ``purchase_date`` against a bound today, so this is a
display defect rather than a live money hole: ``/inventory`` prints
``purchase_date`` and ``expires`` on ONE line in two frames, and the
purchase receipt one second earlier quotes the naive-local grant. A
midnight purchase is listed as yesterday. It is also one
``WHERE purchase_date >= ?`` away from becoming a real 3-hour hole, and
``UNIQUE(user_id, item_id, purchase_date)`` — the retried-``/buy``
dedupe key — is keyed in a frame no other writer of that table uses.

The fix splits the two clocks at the one place they were conflated.
``now`` stays the ledger clock; the inventory row takes the same
instant re-rendered in the host zone.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import (
    EconomyUser,
    InventoryItem,
    ShopItem,
    Transaction,
)
from telegram_invite_bot.repositories.inventory_repo import InventoryRepo
from telegram_invite_bot.services.purchase_service import PurchaseService
from tests.integration.repositories._session import build_session

_USER = 42
_ITEM = 1
_PRICE = 100

# A summer instant, so the offset is unambiguous under Europe/Moscow
# (permanent UTC+3 since 2014 — no DST branch to reason about).
_UTC_NOW = datetime(2026, 6, 1, 21, 30, 0)  # noqa: DTZ001 — naive UTC, the ledger frame
_MSK_NOW = datetime(2026, 6, 2, 0, 30, 0)  # noqa: DTZ001 — the same instant, naive LOCAL


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    async with build_session(tmp_path, EconomyBase, "economy.db") as s:
        yield s


@pytest.fixture
def moscow(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """Pin the host zone so "local" means UTC+3, as it does on prod."""
    monkeypatch.setenv("TZ", "Europe/Moscow")
    time.tzset()
    try:
        yield
    finally:
        monkeypatch.undo()
        time.tzset()


async def _seed(session: AsyncSession) -> None:
    session.add(EconomyUser(user_id=_USER, balance=10_000, language="ru"))
    session.add(
        ShopItem(id=_ITEM, name="Plushie", description="", price=_PRICE, type="unwarn", stock=-1)
    )
    await session.commit()


async def test_purchase_date_is_the_local_wall_clock(session: AsyncSession, moscow: None) -> None:
    """The instant is 21:30 UTC; the row must say 00:30 — the next day."""
    await _seed(session)

    await PurchaseService(session).purchase(user_id=_USER, item_id=_ITEM, now=_UTC_NOW)
    await session.commit()

    row = (await session.execute(select(InventoryItem))).scalars().one()
    assert row.purchase_date == _MSK_NOW


async def test_the_ledger_row_keeps_the_utc_clock(session: AsyncSession, moscow: None) -> None:
    """Guard against fixing the wrong half.

    ``transactions.date`` is naive UTC at every writer (``transactions_repo``
    and this service) and every reader bounds it in UTC —
    ``message_reward_day_total`` converts local midnights into UTC before
    comparing. Moving it would break the /balance windows.
    """
    await _seed(session)

    await PurchaseService(session).purchase(user_id=_USER, item_id=_ITEM, now=_UTC_NOW)
    await session.commit()

    tx = (await session.execute(select(Transaction))).scalars().one()
    assert tx.date == _UTC_NOW


async def test_purchase_and_use_of_one_row_are_seconds_apart(
    session: AsyncSession, moscow: None
) -> None:
    """The prod symptom, reproduced: buy, then use, in one wall-clock second.

    ``consume`` is called exactly as the handlers call it — with a naive
    LOCAL ``datetime.now()``. The two stamps on the row have to land in
    the same frame or a 53 ms round trip reads as three hours.
    """
    await _seed(session)

    outcome = await PurchaseService(session).purchase(user_id=_USER, item_id=_ITEM)
    await session.commit()
    assert outcome.inventory_id is not None

    used_at = datetime.now()  # noqa: DTZ005 — the handlers' clock, verbatim
    await InventoryRepo(session).consume(
        user_id=_USER, inventory_id=outcome.inventory_id, now=used_at
    )
    await session.commit()

    row = (await session.execute(select(InventoryItem))).scalars().one()
    assert row.used_date is not None
    assert abs(row.used_date - row.purchase_date) < timedelta(minutes=1)


async def test_the_default_clock_follows_the_host_zone(session: AsyncSession, moscow: None) -> None:
    """No ``now`` passed — the path prod actually takes (``handlers/shop.py``
    calls ``purchase(user_id=..., item_id=...)`` and nothing else).
    """
    await _seed(session)

    before = datetime.now()  # noqa: DTZ005 — naive local
    await PurchaseService(session).purchase(user_id=_USER, item_id=_ITEM)
    await session.commit()
    after = datetime.now()  # noqa: DTZ005 — naive local

    row = (await session.execute(select(InventoryItem))).scalars().one()
    assert before <= row.purchase_date <= after


async def test_a_fresh_row_is_not_listed_as_bought_in_the_past(
    session: AsyncSession, moscow: None
) -> None:
    """``/inventory`` renders ``purchase_date`` and ``expires`` on ONE line.

    ``expires`` is naive local — ``list_for_user`` filters it against a
    naive-local clock — so a just-bought row whose window opened a minute
    ago must not claim a purchase date three hours older than the window.
    """
    await _seed(session)

    now_local = datetime.now()  # noqa: DTZ005 — the repo's own clock
    outcome = await PurchaseService(session).purchase(user_id=_USER, item_id=_ITEM)
    assert outcome.inventory_id is not None
    await session.execute(
        update(InventoryItem)
        .where(InventoryItem.id == outcome.inventory_id)
        .values(expires=now_local + timedelta(days=7))
    )
    await session.commit()

    entry = (await InventoryRepo(session).list_for_user(_USER))[0]
    assert entry.expires is not None
    elapsed = entry.expires - entry.purchase_date
    assert abs(elapsed - timedelta(days=7)) < timedelta(minutes=1)
