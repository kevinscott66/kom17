"""Regression guard: the payout lease does not serialise two approvers.

``WithdrawService.approve`` takes a ``processing`` lease and COMMITS it
before the Crypto Pay transfer (T-020 R13). That lease is what makes a
leased row untouchable by ``reject`` and ``approve_manual`` — both guard
on ``pending`` — and it is easy to read the method's own docstring as
saying the lease excludes a second *approve* too. It does not, and it is
not meant to: :meth:`WithdrawalsRepo.claim_processing` matches
``processing`` as well as ``pending`` on purpose, so a process that dies
mid-transfer cannot strand the request forever.

The consequence is worth pinning where someone will trip over it. Two
admins tapping «Одобрить» at the same moment BOTH reach
``client.transfer``. Exactly one USDT payment still happens — but only
because the provider dedupes on ``spend_id = wd_<id>``. Nothing local
stops the second call. Drop the ``spend_id``, or move to a provider
without idempotency keys, and this is a double payout with no guard
underneath it.

Two guards, deliberately different in kind:

* the behavioural one races two real ``approve`` calls and counts the
  provider calls;
* the structural one re-claims a leased row directly, so the re-entrancy
  is pinned even when the race happens not to interleave.

Scope note, same as ``test_withdraw_unconfirmed_payout``: ``approve`` is
the auto-payout path and is not wired to the admin panel yet (payout
doctrine v1 is manual). The guard is written now precisely because the
wiring is what would make this reachable.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

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
from telegram_invite_bot.repositories.withdrawals_repo import (
    PROCESSING_STATUS,
    WithdrawalsRepo,
)
from telegram_invite_bot.services.economy_service import EconomyService
from telegram_invite_bot.services.payments.crypto_client import TransferResult
from telegram_invite_bot.services.withdraw_service import (
    ApproveOutcome,
    ApproveResult,
    CreateOutcome,
    WithdrawService,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.db import EngineRegistry

_USER = 6170
_ADMIN_A = 11
_ADMIN_B = 22
_COINS_PER_USDT = 900.0
_MIN = 4500
_MAX = 90000
_BALANCE = 100_000
_AMOUNT = 4500
#: How long the second approver gets to reach ``transfer`` before the
#: first one stops waiting for it. Generous: the failure this bounds is
#: "it never arrives", not "it was slow".
_PAY_GRACE_SECONDS = 10.0


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    """The production engine set — listeners and pragmas included."""
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


class _GateClient:
    """Transfer stub that parks until BOTH approvers are inside it.

    The parking is the assertion. If the lease ever did exclude a second
    approver, only one caller would arrive here, the barrier would time
    out, and ``calls`` would hold a single entry — which is what the test
    below reports.
    """

    def __init__(self, both_paying: asyncio.Barrier) -> None:
        self._both_paying = both_paying
        self.calls: list[str] = []

    async def transfer(
        self,
        *,
        user_id: int,
        asset: str,
        amount: str,
        spend_id: str,
        comment: str | None = None,
    ) -> TransferResult:
        self.calls.append(spend_id)
        with suppress(TimeoutError, asyncio.BrokenBarrierError):
            async with asyncio.timeout(_PAY_GRACE_SECONDS):
                await self._both_paying.wait()
        return TransferResult(transfer_id=7, spend_id=spend_id, asset=asset, amount=amount)


def _service(session: AsyncSession) -> WithdrawService:
    """A service with the lifetime gates off — this file is about payout.

    ``transactions`` is left unset on purpose: ``check_lifetime_gate``
    returns immediately without a ledger, so nothing but the lease is
    between the two approvers.
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
        daily_limit_coins=1_000_000,
        monthly_limit_coins=10_000_000,
    )


async def _escrowed(registry: EngineRegistry) -> int:
    """Seed a funded user and put one request into escrow."""
    async with session_for(registry, DBName.ECONOMY) as session:
        session.add(EconomyUser(user_id=_USER, balance=_BALANCE, language="ru"))
    async with session_for(registry, DBName.ECONOMY) as session:
        created = await _service(session).create(user_id=_USER, amount_com=_AMOUNT)
    assert created.outcome is CreateOutcome.OK
    assert created.request_id is not None
    return created.request_id


async def _approve(
    registry: EngineRegistry,
    *,
    request_id: int,
    admin_id: int,
    client: _GateClient,
    start: asyncio.Barrier,
) -> ApproveResult:
    """One approval on its own session — i.e. its own connection.

    The warm-up read before the barrier is the same trick
    ``test_withdraw_quota_race`` needs: opening the second connection
    costs far more than reusing the first, and that asymmetry alone can
    let the first caller finish before the second has connected.
    """
    async with session_for(registry, DBName.ECONOMY) as session:
        await session.execute(select(func.count()).select_from(WithdrawalRequest))
        await start.wait()
        return await _service(session).approve(
            request_id=request_id,
            admin_id=admin_id,
            client=client,  # type: ignore[arg-type]
        )


async def _row(registry: EngineRegistry, request_id: int) -> tuple[str, int]:
    """``(request status, wallet balance)`` after the dust settles."""
    async with session_for(registry, DBName.ECONOMY) as session:
        status = await session.execute(
            select(WithdrawalRequest.status).where(WithdrawalRequest.id == request_id)
        )
        balance = await session.execute(
            select(EconomyUser.balance).where(EconomyUser.user_id == _USER)
        )
        return str(status.scalar_one()), int(balance.scalar_one())


async def test_two_concurrent_approvals_both_reach_the_provider(
    registry: EngineRegistry,
) -> None:
    """Both admins pay. ``spend_id`` is the only reason it costs once."""
    request_id = await _escrowed(registry)
    client = _GateClient(asyncio.Barrier(2))
    start = asyncio.Barrier(2)

    first, second = await asyncio.gather(
        _approve(registry, request_id=request_id, admin_id=_ADMIN_A, client=client, start=start),
        _approve(registry, request_id=request_id, admin_id=_ADMIN_B, client=client, start=start),
    )

    assert client.calls == [f"wd_{request_id}"] * 2, (
        "expected both approvers inside client.transfer — the lease is "
        f"re-entrant by design, so neither is excluded: {client.calls}"
    )

    # One writer flips the row; the other is told it was already done.
    # That is ``claim_terminal``, and it protects the ROW — not the money
    # that has already left twice over the wire above.
    outcomes = sorted(r.outcome.value for r in (first, second))
    assert outcomes == [ApproveOutcome.ALREADY_PROCESSED.value, ApproveOutcome.COMPLETED.value]

    status, balance = await _row(registry, request_id)
    assert status == "completed"
    # Escrow was debited once at create; approval moves no on-platform coins.
    assert balance == _BALANCE - _AMOUNT


async def test_a_leased_request_can_be_leased_again(registry: EngineRegistry) -> None:
    """Structural half: re-entrancy is a decision, not an accident.

    The race above only fails when the interleave happens. This one fails
    the moment ``claim_processing`` stops matching ``processing`` — which
    is the shape a well-meaning "make approve exclusive" change takes.
    Anyone making it has to confront the cost it buys back: a request
    whose payer died mid-transfer becomes permanently un-drivable.
    """
    request_id = await _escrowed(registry)
    async with session_for(registry, DBName.ECONOMY) as session:
        repo = WithdrawalsRepo(session)
        assert await repo.claim_processing(request_id, processed_by=_ADMIN_A)
        assert await repo.claim_processing(request_id, processed_by=_ADMIN_B), (
            "a leased row must stay re-claimable, or a crash mid-payout strands it"
        )

    status, _ = await _row(registry, request_id)
    assert status == PROCESSING_STATUS
