"""Regression guard: two concurrent withdrawals cannot share one quota.

``WithdrawService.create`` reads the rolling daily/monthly usage and then
debits against the headroom that read granted. Between those two steps
nothing used to hold the writer lock, because this project opens the
SQLite transaction *lazily* and only for writes::

    db/engines.py:207   head[0].lower() in _NON_WRITE_HEADS  →  return
    db/engines.py:56    _NON_WRITE_HEADS = {"select", ...}

A ``SELECT``-only prologue therefore ran outside any transaction at all.
Grouping the statements in one SQLAlchemy session did not help: autobegin
groups the *unit of work*, not the database's view of it. So two taps a
few milliseconds apart both read ``daily_used = 0``, both saw the whole
cap as headroom, and both escrowed — the user withdrew twice their daily
limit and the ledger showed nothing wrong.

#776 makes the first statement of the critical section a write
(:meth:`WithdrawalsRepo.lock_writer`, an ``UPDATE … WHERE false``) so the
hook above promotes the connection to ``BEGIN IMMEDIATE`` and the second
caller waits out ``PRAGMA busy_timeout`` (5 000 ms, ``db/pragma.py:63``)
instead of racing.

Why this file and not ``tests/integration/services/test_withdraw_service``:
that suite builds its session from a bare ``create_async_engine``, which
never installs the ``db/engines.py`` listener. The fix is *entirely* that
listener's behaviour, so a test written against a bare engine would pass
with or without it. Here the engines come from :func:`build_registry`,
exactly as production builds them.

Two guards, deliberately different in kind:

* the behavioural one races two real ``create`` calls and counts rows;
* the structural one records the SQL and asserts the lock statement comes
  first, so the guard still bites if a future refactor reorders the reads
  in a way the timing test happens not to catch.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
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
from telegram_invite_bot.db.models.economy import EconomyUser, WithdrawalRequest
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.repositories.withdrawals_repo import WithdrawalsRepo
from telegram_invite_bot.services.economy_service import EconomyService
from telegram_invite_bot.services.withdraw_service import (
    CreateOutcome,
    CreateResult,
    WithdrawService,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.db import EngineRegistry

_USER = 5150
_COINS_PER_USDT = 900.0
_MIN = 4500
_MAX = 90000
#: Funded well past two withdrawals, so the wallet is never what refuses
#: the second one — the quota has to be.
_BALANCE = 100_000
_AMOUNT = 4500
#: Room for exactly one ``_AMOUNT``, with change left over. The leftover
#: matters: it makes the refusal's ``remaining`` a number the assertion
#: can pin (``_DAILY - _AMOUNT``) rather than a bare zero that a
#: clamped-to-zero bug would also produce.
_DAILY = 5000


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


def _service(session: AsyncSession) -> WithdrawService:
    """A service with only the rolling quota armed.

    ``transactions`` is left unset on purpose: that turns off the two
    lifetime gates (``check_lifetime_gate`` returns ``None`` immediately
    when it has no ledger), so the only thing standing between the two
    racers is the daily cap this file is about.
    """
    economy = EconomyService(EconomyRepo(session), TransactionsRepo(session))
    return WithdrawService(
        session=session,
        economy=economy,
        withdrawals=WithdrawalsRepo(session),
        coins_per_usdt=_COINS_PER_USDT,
        min_coins=_MIN,
        max_coins=_MAX,
        asset="USDT",
        daily_limit_coins=_DAILY,
        monthly_limit_coins=10_000_000,
    )


async def _seed(registry: EngineRegistry) -> None:
    async with session_for(registry, DBName.ECONOMY) as session:
        session.add(EconomyUser(user_id=_USER, balance=_BALANCE, language="ru"))


async def _create(registry: EngineRegistry, barrier: asyncio.Barrier | None = None) -> CreateResult:
    """One withdrawal on its own session — i.e. its own connection.

    Production gives each update its own session (``middlewares/base``),
    so two taps really are two connections; sharing one here would let
    SQLAlchemy serialise the statements for us and the race could not
    reproduce even without the fix.

    ``barrier`` is what makes the race actually race. Opening the *second*
    connection is far more expensive than reusing the first (pool
    checkout, a new aiosqlite worker thread, the connect-time pragmas),
    and that asymmetry alone was enough to let the first caller finish
    before the second had connected — the test then passed against the
    unfixed service, which is worse than no test. So each caller warms its
    connection with a throwaway read and only then waits at the barrier;
    both enter ``create`` from the same starting line.
    """
    async with session_for(registry, DBName.ECONOMY) as session:
        service = _service(session)
        if barrier is not None:
            # Any statement will do — this one is a SELECT precisely
            # because a SELECT does *not* take the writer lock, so the
            # warm-up cannot itself serialise the two callers.
            await session.execute(select(func.count()).select_from(WithdrawalRequest))
            await barrier.wait()
        return await service.create(user_id=_USER, amount_com=_AMOUNT)


async def _rows(registry: EngineRegistry) -> tuple[int, int]:
    """``(request count, wallet balance)`` after the dust settles."""
    async with session_for(registry, DBName.ECONOMY) as session:
        count = await session.execute(
            select(func.count())
            .select_from(WithdrawalRequest)
            .where(WithdrawalRequest.user_id == _USER)
        )
        balance = await session.execute(
            select(EconomyUser.balance).where(EconomyUser.user_id == _USER)
        )
        return int(count.scalar_one()), int(balance.scalar_one())


async def test_two_concurrent_withdrawals_share_one_daily_cap(
    registry: EngineRegistry,
) -> None:
    """Two ``create`` calls started together: one OK, one refused.

    Without ``lock_writer`` both coroutines issue their quota ``SELECT``
    before either returns — the first ``await`` inside aiosqlite hands
    control to the other — so both read ``daily_used = 0`` and both
    escrow. That is the bug, and it is what makes this assertion
    non-vacuous: the failing shape is two OKs and 9 000 coins gone.
    """
    await _seed(registry)

    barrier = asyncio.Barrier(2)
    first, second = await asyncio.gather(_create(registry, barrier), _create(registry, barrier))

    outcomes = sorted(r.outcome.value for r in (first, second))
    assert outcomes == [CreateOutcome.DAILY_QUOTA_EXCEEDED.value, CreateOutcome.OK.value], (
        f"both callers were admitted against the same pre-debit total: {outcomes}"
    )

    refused = first if first.outcome is not CreateOutcome.OK else second
    assert refused.remaining == _DAILY - _AMOUNT, (
        "the refusal reported headroom that does not account for the winner"
    )

    count, balance = await _rows(registry)
    assert count == 1, f"{count} requests escrowed against a one-request cap"
    assert balance == _BALANCE - _AMOUNT


async def test_the_lock_is_taken_before_the_first_gate_read(
    registry: EngineRegistry,
) -> None:
    """Structural half: the critical section must *open* with a write.

    The timing test above can only fail when the interleave happens; this
    one fails whenever the ordering property is lost, which is the thing
    the fix actually establishes. It watches the same listener hook the
    fix relies on, so a read that migrates above ``lock_writer`` is caught
    even if it never loses a race in CI.
    """
    await _seed(registry)

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

    assert result.outcome is CreateOutcome.OK
    # ``BEGIN`` is emitted by the hook itself and PRAGMAs by the connect
    # listener; neither is a statement ``create`` chose to run.
    interesting = [s for s in seen if s not in {"BEGIN", "PRAGMA", "COMMIT", "ROLLBACK"}]
    assert interesting, "no statements recorded — the listener never fired"
    assert interesting[0] == "UPDATE", (
        f"the critical section opened with {interesting[0]}, not the writer lock: {interesting}"
    )
