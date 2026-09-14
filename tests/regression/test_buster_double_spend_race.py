"""Regression guard: two ``/use`` taps cannot burn two busters at once.

#1903 made a second ``double_daily`` buster a refusal instead of a paid
no-op: ``InventoryUseService`` reads
:meth:`PrivilegesRepo.get_active` and, if one is already armed, returns
``BUSTER_ALREADY_ACTIVE`` *before* step 5 consumes the entry. That fix
is what creates the precondition for this one — a refused buster stays
in ``/inventory`` indefinitely, so holding two spares is the normal
state rather than an edge case.

Between that read and the consume nothing used to hold the writer lock,
because this project opens the SQLite transaction *lazily* and only for
writes::

    db/engines.py:207   head[0].lower() in _NON_WRITE_HEADS  →  return
    db/engines.py:56    _NON_WRITE_HEADS = {"select", ...}

A ``SELECT``-only prologue therefore ran outside any transaction at all,
so two taps a few milliseconds apart both read "nothing armed" and both
went on to consume. Neither existing guard catches it:
:meth:`InventoryRepo.consume` is keyed on ``inventory_id`` and the two
taps carry two *different* entry ids, and
:meth:`PrivilegesRepo.grant_buster` is an upsert with
``MAX(expires_at, excluded.expires_at)``, so the two grants collapse
into the one row by design. The user is left with one armed buster and
one fewer item — a coin-priced purchase destroyed for nothing.

The remedy is the house one: make the first statement of the critical
section a write (:meth:`PrivilegesRepo.lock_writer`, an
``UPDATE … WHERE false``) so the hook above promotes the connection to
``BEGIN IMMEDIATE`` and the second caller waits out ``PRAGMA
busy_timeout`` (5 000 ms, ``db/pragma.py:63``) and then reads the
*committed* grant. Same mechanism as ``WithdrawalsRepo.lock_writer``
(#776) and ``P2pRepo.lock_writer`` (#1503).

Why this file and not ``test_double_daily_buster_is_not_burned``: that
suite builds its session from a bare ``create_async_engine``, which
never installs the ``db/engines.py`` listener. The fix is *entirely*
that listener's behaviour, so a test written against a bare engine
would pass with or without it. Here the engines come from
:func:`build_registry`, exactly as production builds them.

Two guards, deliberately different in kind:

* the behavioural one races two real ``use`` calls and counts consumed
  entries;
* the structural one records the SQL and asserts the lock statement
  comes before the privilege read, so the guard still bites if a future
  refactor reorders them in a way the timing test happens not to catch.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import event, func, select

from telegram_invite_bot.config.settings import AppEnv
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import (
    EconomyUser,
    InventoryItem,
    ShopItem,
)
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.inventory_repo import InventoryRepo
from telegram_invite_bot.repositories.privileges_repo import PrivilegesRepo
from telegram_invite_bot.repositories.shop_items_repo import ShopItemsRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.repositories.vip_repo import VipRepo
from telegram_invite_bot.services.inventory_use_service import (
    InventoryUseService,
    UseOutcome,
    UseResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry

_NOW = datetime(2026, 9, 9, 12, 0, 0)
_USER = 4242
_ITEM = 7


@pytest.fixture
async def registry(make_settings: Callable[..., Settings]) -> AsyncIterator[EngineRegistry]:
    """The production engine set — listeners and pragmas included.

    ``AppEnv.DEV`` because ``install_safety`` raises on an unbounded
    ``UPDATE`` under ``prod``; ``lock_writer`` carries an explicit
    ``WHERE false`` and so is bounded either way, but the surrounding
    suite should not depend on that reading.
    """
    reg = build_registry(make_settings(AppEnv.DEV))
    async with reg.engine(DBName.ECONOMY).begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    try:
        yield reg
    finally:
        await reg.dispose()


def _service(session: AsyncSession) -> InventoryUseService:
    return InventoryUseService(
        InventoryRepo(session),
        VipRepo(session),
        PrivilegesRepo(session),
        ShopItemsRepo(session),
        EconomyRepo(session),
        TransactionsRepo(session),
    )


async def _seed(registry: EngineRegistry, *, entries: int) -> list[int]:
    """One user holding ``entries`` unused busters."""
    async with session_for(registry, DBName.ECONOMY) as session:
        session.add(
            ShopItem(
                id=_ITEM, name="2x daily", description="", price=500, type="double_daily", stock=-1
            )
        )
        session.add(EconomyUser(user_id=_USER, balance=100, language="ru"))
        ids: list[int] = []
        for index in range(entries):
            # Distinct instants — ``uq_inventory_user_item_dt`` is
            # ``(user_id, item_id, purchase_date)``, so two entries for
            # the same item need two purchase moments.
            row = InventoryItem(
                user_id=_USER,
                item_id=_ITEM,
                purchase_date=_NOW - timedelta(days=1, seconds=index),
                used=False,
            )
            session.add(row)
            await session.flush()
            ids.append(row.id)
        return ids


async def _use(
    registry: EngineRegistry, entry_id: int, barrier: asyncio.Barrier | None = None
) -> UseResult:
    """One ``/use`` on its own session — i.e. its own connection.

    Production gives each update its own session (``middlewares/base``),
    so two taps really are two connections; sharing one here would let
    SQLAlchemy serialise the statements for us and the race could not
    reproduce even without the fix.

    ``barrier`` is what makes the race actually race. Opening the
    *second* connection is far more expensive than reusing the first
    (pool checkout, a new aiosqlite worker thread, the connect-time
    pragmas), and that asymmetry alone was enough to let the first
    caller finish before the second had connected — the test then
    passed against the unfixed service, which is worse than no test.
    """
    async with session_for(registry, DBName.ECONOMY) as session:
        service = _service(session)
        if barrier is not None:
            # Any statement will do — this one is a SELECT precisely
            # because a SELECT does *not* take the writer lock, so the
            # warm-up cannot itself serialise the two callers.
            await session.execute(select(func.count()).select_from(InventoryItem))
            await barrier.wait()
        return await service.use(user_id=_USER, entry_id=entry_id, now=_NOW)


async def _consumed(registry: EngineRegistry) -> int:
    async with session_for(registry, DBName.ECONOMY) as session:
        rows = await session.execute(
            select(func.count())
            .select_from(InventoryItem)
            .where(InventoryItem.user_id == _USER, InventoryItem.used.is_(True))
        )
        return int(rows.scalar_one())


async def test_two_concurrent_uses_burn_only_one_buster(registry: EngineRegistry) -> None:
    """Two ``use`` calls started together: one arms, one is refused.

    Without ``lock_writer`` both coroutines issue their ``get_active``
    ``SELECT`` before either writes — the first ``await`` inside
    aiosqlite hands control to the other — so both read "nothing armed"
    and both consume. That is the bug, and it is what makes this
    assertion non-vacuous: the failing shape is two successes and two
    items gone for one armed buster.
    """
    first, second = await _seed(registry, entries=2)

    barrier = asyncio.Barrier(2)
    left, right = await asyncio.gather(
        _use(registry, first, barrier), _use(registry, second, barrier)
    )

    outcomes = sorted(r.outcome.name for r in (left, right))
    assert outcomes == [UseOutcome.BUSTER_ALREADY_ACTIVE.name, UseOutcome.SUCCESS.name], (
        f"both callers were admitted against the same pre-grant read: {outcomes}"
    )
    assert await _consumed(registry) == 1, (
        "two inventory entries were consumed for one armed buster — the second "
        "item was paid for and destroyed"
    )


async def test_the_lock_is_taken_before_the_privilege_read(registry: EngineRegistry) -> None:
    """Structural half: the gate read must happen under the writer lock.

    The timing test above can only fail when the interleave happens;
    this one fails whenever the ordering property is lost, which is the
    thing the fix actually establishes. It watches the same listener
    hook the fix relies on, so a read that migrates above
    ``lock_writer`` is caught even if it never loses a race in CI.
    """
    (entry,) = await _seed(registry, entries=1)

    seen: list[str] = []

    @event.listens_for(registry.engine(DBName.ECONOMY).sync_engine, "before_cursor_execute")
    def _record(  # type: ignore[no-untyped-def]
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        seen.append(" ".join(statement.split()))

    try:
        result = await _use(registry, entry)
    finally:
        event.remove(registry.engine(DBName.ECONOMY).sync_engine, "before_cursor_execute", _record)

    assert result.outcome is UseOutcome.SUCCESS
    reads = [
        i for i, s in enumerate(seen) if s.upper().startswith("SELECT") and "user_privileges" in s
    ]
    writes = [i for i, s in enumerate(seen) if s.upper().startswith("UPDATE")]
    assert reads, "no privilege read recorded — the listener never fired"
    assert writes and writes[0] < reads[0], (
        "the buster gate read the privilege table before any write had taken "
        f"the writer lock: {seen}"
    )
