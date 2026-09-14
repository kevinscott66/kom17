"""Regression guard: two concurrent sell orders cannot share one slot.

``P2pService.create_sell_order`` counts the seller's live orders and
then inserts against the headroom that count granted. Between those two
steps nothing held the writer lock, because this project opens the
SQLite transaction *lazily* and only for writes::

    db/engines.py:207   head[0].lower() in _NON_WRITE_HEADS  →  return
    db/engines.py:56    _NON_WRITE_HEADS = {"select", ...}

So the ceiling read ran outside any transaction at all, and two taps a
few milliseconds apart both saw the same pre-insert total and both were
admitted. The escrow ``hold`` that follows *is* serialised — it is a
write — but by then the gate has already said yes twice.

The buy side has been guarded since #1503: :meth:`P2pService.buy` and
:meth:`~P2pService.express_buy` open with
:meth:`P2pRepo.lock_writer` precisely so that
``MAX_OPEN_TRADES_PER_BUYER`` is decided under the lock. The sell side
runs the same read-then-write shape against
``MAX_ACTIVE_ORDERS_PER_SELLER`` and did not.

That cap is not bookkeeping. ``P2pRepo.list_active`` sorts by price then
id and clamps to 50 rows, and every order in a currency carries the same
market price, so rows differentiate only on id: a seller who gets past
the cap owns the top of the book. The escrow is a ``hold`` and
``cancel_order`` refunds 100%, so the flood costs the attacker nothing —
which is the whole reason #1690 exists. A cap that a second concurrent
tap walks through is not a cap.

Why this file and not ``tests/integration/services/test_p2p_service``:
that suite builds its session from a bare ``create_async_engine``, which
never installs the ``db/engines.py`` listener the fix relies on. Here
the engines come from :func:`build_registry`, exactly as production
builds them — the same argument ``test_withdraw_quota_race.py`` makes
for #776.

Two guards, deliberately different in kind:

* the behavioural one races two real ``create_sell_order`` calls and
  counts rows;
* the structural one records the SQL and asserts the lock statement
  comes first, so the guard still bites if a future refactor moves the
  ceiling read back above it.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from sqlalchemy import event, func, select

from telegram_invite_bot.config.settings import (
    AppEnv,
    BotConfig,
    FeatureFlags,
    LoggingConfig,
    ObservabilityConfig,
    PathsConfig,
    Settings,
    WebhookConfig,
)
from telegram_invite_bot.db.engines import build_registry
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.db.models.p2p import P2pSellOrder
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.p2p_repo import P2pRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.p2p_service import (
    MAX_ACTIVE_ORDERS_PER_SELLER,
    CreateOrderOutcome,
    CreateOrderResult,
    P2pService,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.db import EngineRegistry

_SELLER = 7719
NOW = datetime(2026, 9, 11, 12, 0, 0)
_CURRENCY = "RUB"
#: Each order escrows this much, and the wallet holds far more than the
#: two racers together need — so a refusal is always the cap talking and
#: never the balance.
_AMOUNT = 10
_BALANCE = 10_000
#: One free slot, and exactly one. Both racers must want it.
_PRESEEDED = MAX_ACTIVE_ORDERS_PER_SELLER - 1


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    """The production engine set — listeners and pragmas included.

    ``AppEnv.DEV`` because ``install_safety`` raises on an unbounded
    ``UPDATE`` under ``prod``; ``lock_writer`` carries an explicit
    ``WHERE false`` and so is bounded either way, but the surrounding
    suite should not depend on that reading.
    """
    settings = Settings(
        app_env=AppEnv.DEV,
        bot=BotConfig(BOT_TOKEN=SecretStr("123:abc")),
        webhook=WebhookConfig(),
        paths=PathsConfig(
            DATABASE_DIR=tmp_path / "db",
            MESSAGE_STATS_DIR=tmp_path / "db",
            LOGS_DIR=tmp_path / "logs",
        ),
        logging=LoggingConfig(),
        observability=ObservabilityConfig(),
        features=FeatureFlags(),
    )
    reg = build_registry(settings)
    async with reg.engine(DBName.ECONOMY).begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    try:
        yield reg
    finally:
        await reg.dispose()


def _service(session: AsyncSession) -> P2pService:
    return P2pService(
        P2pRepo(session),
        EconomyRepo(session),
        TransactionsRepo(session),
        session,
    )


async def _seed(registry: EngineRegistry, now: datetime) -> None:
    """A funded wallet and a book that is one order short of the cap."""
    async with session_for(registry, DBName.ECONOMY) as session:
        session.add(EconomyUser(user_id=_SELLER, balance=_BALANCE, language="ru"))
        repo = P2pRepo(session)
        for _ in range(_PRESEEDED):
            await repo.create_order(
                user_id=_SELLER,
                amount_com=_AMOUNT,
                price_per_com=1.0,
                fiat_currency=_CURRENCY,
                payment_methods=None,
                min_amount=None,
                max_amount=None,
                now=now,
            )


async def _create(
    registry: EngineRegistry, barrier: asyncio.Barrier | None = None
) -> CreateOrderResult:
    """One sell order on its own session — i.e. its own connection.

    Production gives each update its own session (``middlewares/base``),
    so two taps really are two connections; sharing one here would let
    SQLAlchemy serialise the statements for us and the race could not
    reproduce even without the fix.

    ``barrier`` is what makes the race actually race. Opening the
    *second* connection costs far more than reusing the first (pool
    checkout, a new aiosqlite worker thread, the connect-time pragmas),
    and that asymmetry alone lets the first caller finish before the
    second has connected. So each caller warms its connection with a
    throwaway read — a ``SELECT``, which takes no writer lock and so
    cannot itself serialise the two — and only then waits at the
    barrier.
    """
    async with session_for(registry, DBName.ECONOMY) as session:
        service = _service(session)
        if barrier is not None:
            await session.execute(select(func.count()).select_from(P2pSellOrder))
            await barrier.wait()
        return await service.create_sell_order(
            seller_id=_SELLER, amount_com=_AMOUNT, currency=_CURRENCY, now=NOW
        )


async def _state(registry: EngineRegistry) -> tuple[int, int, int]:
    """``(live orders, wallet balance, escrow ledger rows)``."""
    async with session_for(registry, DBName.ECONOMY) as session:
        live = await P2pRepo(session).count_active_orders(_SELLER)
        balance = await session.execute(
            select(EconomyUser.balance).where(EconomyUser.user_id == _SELLER)
        )
        escrows = await session.execute(
            select(func.count()).select_from(Transaction).where(Transaction.type == "p2p_escrow")
        )
        return live, int(balance.scalar_one()), int(escrows.scalar_one())


async def test_two_concurrent_sell_orders_share_one_seller_slot(
    registry: EngineRegistry,
) -> None:
    """Two ``create_sell_order`` calls started together: one OK, one refused.

    Without ``lock_writer`` both coroutines issue their ceiling
    ``SELECT`` before either returns — the first ``await`` inside
    aiosqlite hands control to the other — so both count
    ``MAX_ACTIVE_ORDERS_PER_SELLER - 1`` live orders, both see a free
    slot and both insert. That is the bug, and it is what makes this
    assertion non-vacuous: the failing shape is two OKs and a seller
    holding one order more than the cap allows.
    """
    await _seed(registry, NOW)

    barrier = asyncio.Barrier(2)
    first, second = await asyncio.gather(_create(registry, barrier), _create(registry, barrier))

    outcomes = sorted(r.outcome.value for r in (first, second))
    assert outcomes == [CreateOrderOutcome.OK.value, CreateOrderOutcome.TOO_MANY_ORDERS.value], (
        f"both callers were admitted against the same pre-insert count: {outcomes}"
    )

    live, balance, escrows = await _state(registry)
    assert live == MAX_ACTIVE_ORDERS_PER_SELLER, (
        f"{live} live orders against a cap of {MAX_ACTIVE_ORDERS_PER_SELLER}"
    )
    # The refused caller must also have parked nothing (#1690).
    assert balance == _BALANCE - _AMOUNT
    assert escrows == 1


async def test_the_lock_is_taken_before_the_ceiling_count(
    registry: EngineRegistry,
) -> None:
    """Structural half: the critical section must *open* with a write.

    The timing test above can only fail when the interleave happens;
    this one fails whenever the ordering property is lost, which is the
    thing the fix actually establishes. It watches the same listener
    hook the fix relies on, so a read that migrates above
    ``lock_writer`` is caught even if it never loses a race in CI.
    """
    await _seed(registry, NOW)

    seen: list[str] = []

    @event.listens_for(registry.engine(DBName.ECONOMY).sync_engine, "before_cursor_execute")
    def _record(  # type: ignore[no-untyped-def]
        conn, cursor, statement, parameters, context, executemany
    ) -> None:
        head = statement.split(None, 1)
        if head:
            seen.append(head[0].upper())

    try:
        result = await _create(registry)
    finally:
        event.remove(registry.engine(DBName.ECONOMY).sync_engine, "before_cursor_execute", _record)

    assert result.outcome is CreateOrderOutcome.OK
    # ``BEGIN`` is emitted by the hook itself and PRAGMAs by the connect
    # listener; neither is a statement ``create_sell_order`` chose to run.
    interesting = [s for s in seen if s not in {"BEGIN", "PRAGMA", "COMMIT", "ROLLBACK"}]
    assert interesting, "no statements recorded — the listener never fired"
    assert interesting[0] == "UPDATE", (
        f"the critical section opened with {interesting[0]}, not the writer lock: {interesting}"
    )
