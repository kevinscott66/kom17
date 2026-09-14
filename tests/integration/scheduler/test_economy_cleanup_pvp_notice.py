"""#1766 — an expired PvP challenge must lose its live «Accept» button.

``PvpService.sweep_expired`` refunds the creator's held stake and flips
the offer to ``expired``, but the group card it was published with is
never touched: the inline keyboard stays tappable forever. A tap on it
answers ``h_pvp_not_found``, so nothing is minted — but production is
sitting on thirty such cards, each one an invitation to play a game that
cannot be played, and the creator is never told their coins came back.

This file pins the fix-up pass:

* every offer the sweep retired gets its card edited, keyboard dropped;
* an offer that never got a card (``message_id IS NULL``) is skipped,
  not crashed on;
* the edits happen AFTER the economy session has committed and closed —
  a session must never stay open across Telegram I/O;
* a per-card Telegram refusal does not cost the other cards, and the
  refund count the sweep reports is unaffected either way.
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
from telegram_invite_bot.db.models.pvp import PvpEscrow, PvpOffer  # noqa: F401 — tables
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.scheduler.economy_cleanup import EconomyCleanupSweeper
from telegram_invite_bot.services.pvp_service import PvpService
from telegram_invite_bot.utils.time import db_now

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 6, 13, 12, 0, 0)


class _FakeBot:
    """Captures card edits; optionally refuses selected message ids."""

    def __init__(
        self,
        fail_for: set[int] | None = None,
        probe: EngineRegistry | None = None,
    ) -> None:
        self.edited: list[dict[str, Any]] = []
        self.fail_for = fail_for or set()
        self._probe = probe
        self.statuses_seen: list[str] = []

    async def edit_message_text(self, **kwargs: Any) -> None:
        if self._probe is not None:
            # The economy session must already be committed and closed
            # by the time we get here: a fresh connection has to see the
            # offer as ``expired``, not as the ``pending`` row an open
            # uncommitted transaction would still be sitting on.
            async with session_for(self._probe, DBName.ECONOMY) as session:
                rows = (await session.execute(select(PvpOffer.status))).scalars().all()
            self.statuses_seen.extend(rows)
        if kwargs.get("message_id") in self.fail_for:
            msg = "Bad Request: message to edit not found"
            raise RuntimeError(msg)
        self.edited.append(kwargs)


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


async def _seed_offer(
    registry: EngineRegistry,
    *,
    creator_id: int,
    chat_id: int,
    message_id: int | None,
    minutes_old: int = 30,
) -> int:
    """A stale pending offer with a real escrow hold behind it."""
    async with session_for(registry, DBName.ECONOMY) as session:
        session.add(EconomyUser(user_id=creator_id, balance=1_000, language="ru"))
        await session.flush()
        svc = PvpService(EconomyRepo(session), TransactionsRepo(session), session)
        created = await svc.create_offer(
            creator_id=creator_id,
            game="coin",
            bet=100,
            side="heads",
            chat_id=chat_id,
            now=db_now() - timedelta(minutes=minutes_old),
        )
        assert created.offer_id is not None
        if message_id is not None:
            await svc.set_offer_message(created.offer_id, chat_id, message_id)
        return created.offer_id


async def test_every_expired_offer_loses_its_keyboard(
    registry: EngineRegistry,
) -> None:
    await _seed_offer(registry, creator_id=1, chat_id=-100, message_id=11)
    await _seed_offer(registry, creator_id=2, chat_id=-200, message_id=22)
    bot = _FakeBot()
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW, bot=bot)  # type: ignore[arg-type]

    assert (await sweeper.sweep_money()).pvp_offers_expired == 2

    assert [(e["chat_id"], e["message_id"]) for e in bot.edited] == [(-100, 11), (-200, 22)]
    for edit in bot.edited:
        assert edit["reply_markup"] is None
        assert edit["text"] == t("h_pvp_expired_card", "ru")


async def test_an_offer_without_a_card_is_skipped_not_crashed_on(
    registry: EngineRegistry,
) -> None:
    await _seed_offer(registry, creator_id=1, chat_id=-100, message_id=None)
    await _seed_offer(registry, creator_id=2, chat_id=-200, message_id=22)
    bot = _FakeBot()
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW, bot=bot)  # type: ignore[arg-type]

    assert (await sweeper.sweep_money()).pvp_offers_expired == 2

    assert [e["message_id"] for e in bot.edited] == [22]


async def test_the_economy_session_is_closed_before_any_edit(
    registry: EngineRegistry,
) -> None:
    await _seed_offer(registry, creator_id=1, chat_id=-100, message_id=11)
    bot = _FakeBot(probe=registry)
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW, bot=bot)  # type: ignore[arg-type]

    assert (await sweeper.sweep_money()).pvp_offers_expired == 1

    assert bot.statuses_seen == ["expired"]


async def test_one_refused_card_does_not_cost_the_others(
    registry: EngineRegistry,
) -> None:
    await _seed_offer(registry, creator_id=1, chat_id=-100, message_id=11)
    await _seed_offer(registry, creator_id=2, chat_id=-200, message_id=22)
    bot = _FakeBot(fail_for={11})
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW, bot=bot)  # type: ignore[arg-type]

    # The refund count is about coins, not cards: both stakes came back.
    assert (await sweeper.sweep_money()).pvp_offers_expired == 2
    assert [e["message_id"] for e in bot.edited] == [22]


async def test_without_a_bot_the_refunds_still_run(
    registry: EngineRegistry,
) -> None:
    await _seed_offer(registry, creator_id=1, chat_id=-100, message_id=11)
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW)

    assert (await sweeper.sweep_money()).pvp_offers_expired == 1
