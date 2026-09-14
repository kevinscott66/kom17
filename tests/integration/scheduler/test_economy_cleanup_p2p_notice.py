"""#1768/#1771 — an expired P2P trade must reach the buyer who was waiting.

``P2pService.sweep`` cancels a ``pending`` trade whose TTL ran out and
returns the escrowed slice to the order (or, when the order is already
cancelled, to the seller's wallet). It has always returned a
``SweepReport`` naming every trade it retired — and the money sweeper
threw that report away. The buyer, who may well have sent the fiat and
simply not pressed «Я оплатил» yet, learned nothing at all: the trade
just silently stopped existing.

This file pins the fix-up pass:

* every trade the sweep retired earns its buyer a DM;
* the DMs happen AFTER the economy session has committed and closed —
  a session must never stay open across Telegram I/O;
* a per-buyer Telegram refusal (blocked bot) does not cost the other
  buyers, and the count the sweep reports is unaffected either way;
* the pass runs without a bot at all, and the refunds still happen;
* the wall-clock budget stops the fan-out and never the refunds;
* ``sweep_money`` reports BOTH money steps, which is what lets
  ``EconomyCleanupReport.p2p_trades_expired`` exist (#1771).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.db.models.p2p import P2pSellOrder, P2pTrade  # noqa: F401 — tables
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.p2p_repo import P2pRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.scheduler import economy_cleanup
from telegram_invite_bot.scheduler.economy_cleanup import EconomyCleanupSweeper
from telegram_invite_bot.services.p2p_service import (
    BuyOutcome,
    CreateOrderOutcome,
    P2pService,
)
from telegram_invite_bot.utils.time import db_now

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 6, 13, 12, 0, 0)
_SELLER = 111


class _FakeBot:
    """Captures DMs; optionally refuses selected recipients."""

    def __init__(
        self,
        fail_for: set[int] | None = None,
        probe: EngineRegistry | None = None,
    ) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail_for = fail_for or set()
        self._probe = probe
        self.statuses_seen: list[str] = []

    async def send_message(self, chat_id: int, text: str, **_kwargs: Any) -> None:
        if self._probe is not None:
            # The economy session must already be committed and closed by
            # the time we get here: a fresh connection has to see the
            # trade as ``cancelled_timeout``, not as the ``pending`` row
            # an open uncommitted transaction would still be sitting on.
            async with session_for(self._probe, DBName.ECONOMY) as session:
                rows = (await session.execute(select(P2pTrade.status))).scalars().all()
            self.statuses_seen.extend(rows)
        if chat_id in self.fail_for:
            msg = "Forbidden: bot was blocked by the user"
            raise RuntimeError(msg)
        self.sent.append((chat_id, text))


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    reg = EngineRegistry(
        engines={DBName.ECONOMY: engine},
        sessions={DBName.ECONOMY: sessionmaker},
    )
    try:
        yield reg
    finally:
        await engine.dispose()


async def _seed_pending_trades(
    registry: EngineRegistry, *, buyers: tuple[int, ...], minutes_old: int = 45
) -> None:
    """One sell order, one stale ``pending`` trade per buyer."""
    stale = db_now() - timedelta(minutes=minutes_old)
    async with session_for(registry, DBName.ECONOMY) as session:
        session.add(EconomyUser(user_id=_SELLER, balance=10_000, language="ru"))
        for buyer_id in buyers:
            session.add(EconomyUser(user_id=buyer_id, balance=1_000, language="ru"))
        await session.flush()
        svc = P2pService(
            P2pRepo(session),
            EconomyRepo(session),
            TransactionsRepo(session),
            session,
        )
        order = await svc.create_sell_order(
            seller_id=_SELLER, amount_com=1_000, currency="RUB", now=stale
        )
        assert order.outcome is CreateOrderOutcome.OK
        for buyer_id in buyers:
            bought = await svc.buy(
                buyer_id=buyer_id, order_id=order.order_id, amount_com=100, now=stale
            )
            assert bought.outcome is BuyOutcome.OK


async def test_every_expired_trade_reaches_its_buyer(registry: EngineRegistry) -> None:
    await _seed_pending_trades(registry, buyers=(222, 333))
    bot = _FakeBot()
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW, bot=bot)  # type: ignore[arg-type]

    report = await sweeper.sweep_money()

    assert report.p2p_trades_expired == 2
    assert [uid for uid, _ in bot.sent] == [222, 333]
    # Every buyer gets THEIR trade id and THEIR slice, not a shared
    # template — the id is the only handle they have on a support ticket.
    async with session_for(registry, DBName.ECONOMY) as session:
        rows = (await session.execute(select(P2pTrade.buyer_id, P2pTrade.id))).all()
    trade_ids = {buyer_id: trade_id for buyer_id, trade_id in rows}
    assert dict(bot.sent) == {
        buyer_id: t("h_p2p_trade_expired_buyer", "ru", trade_id=trade_ids[buyer_id], amount=100)
        for buyer_id in (222, 333)
    }


async def test_the_economy_session_is_closed_before_any_dm(registry: EngineRegistry) -> None:
    await _seed_pending_trades(registry, buyers=(222,))
    bot = _FakeBot(probe=registry)
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW, bot=bot)  # type: ignore[arg-type]

    assert (await sweeper.sweep_money()).p2p_trades_expired == 1

    assert bot.statuses_seen == ["cancelled_timeout"]


async def test_one_blocked_buyer_does_not_cost_the_others(registry: EngineRegistry) -> None:
    await _seed_pending_trades(registry, buyers=(222, 333))
    bot = _FakeBot(fail_for={222})
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW, bot=bot)  # type: ignore[arg-type]

    # The count is about coins, not DMs: both slices came back either way.
    assert (await sweeper.sweep_money()).p2p_trades_expired == 2
    assert [uid for uid, _ in bot.sent] == [333]


async def test_without_a_bot_the_slices_still_come_back(registry: EngineRegistry) -> None:
    await _seed_pending_trades(registry, buyers=(222,))
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW)

    assert (await sweeper.sweep_money()).p2p_trades_expired == 1

    async with session_for(registry, DBName.ECONOMY) as session:
        statuses = (await session.execute(select(P2pTrade.status))).scalars().all()
    assert list(statuses) == ["cancelled_timeout"]


async def test_a_fresh_trade_is_left_alone(registry: EngineRegistry) -> None:
    """The TTL is the only thing that retires a trade — not the pass."""
    await _seed_pending_trades(registry, buyers=(222,), minutes_old=1)
    bot = _FakeBot()
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW, bot=bot)  # type: ignore[arg-type]

    assert (await sweeper.sweep_money()).p2p_trades_expired == 0
    assert bot.sent == []


async def test_a_spent_budget_costs_dms_and_never_coins(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the budget: it may only ever cost words.

    ``_P2P_DM_BUDGET_SECONDS`` exists because this fan-out rides the
    SIXTY-second money tick, and a slow Telegram must not be what keeps
    the NEXT buyer's escrow held past its TTL. A budget of zero is the
    degenerate case of that: not one DM goes out, and both slices are
    still un-escrowed and still counted.
    """
    monkeypatch.setattr(economy_cleanup, "_P2P_DM_BUDGET_SECONDS", 0.0)
    await _seed_pending_trades(registry, buyers=(222, 333))
    bot = _FakeBot()
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW, bot=bot)  # type: ignore[arg-type]

    assert (await sweeper.sweep_money()).p2p_trades_expired == 2
    assert bot.sent == []

    async with session_for(registry, DBName.ECONOMY) as session:
        statuses = (await session.execute(select(P2pTrade.status))).scalars().all()
    assert sorted(statuses) == ["cancelled_timeout", "cancelled_timeout"]
