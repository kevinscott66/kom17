"""End-to-end ``/admin_p2p_disputes`` (#1687).

The delivery-time admin card is fired once, best effort, from
``handle_dispute_open`` — and only when ``ADMIN_CHAT_ID`` is set. A
blocked bot, a deleted message or an unset id therefore stranded a
disputed trade: its escrow stays frozen, ``expire_pending_guard``
cannot reap it (that guard wants ``pending``), and until this console
existed nothing in the tree could list it again.

Pins:

* Non-developer → silent drop (existence must not enumerate dev IDs).
* Developer + no disputes → the empty card, nothing else.
* Developer + disputes → a header carrying the backlog figure, then
  one re-issued resolution card per trade, oldest first, each with the
  same three buttons the delivery-time card offered.
* Trades in every other status — including the escrow-holding
  ``pending``/``paid`` pair — stay out of the queue.
* Group invocation → router-level private filter rejects (UNHANDLED).
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.p2p import P2pTrade
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.time import db_now
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory

_SELLER = 700_001
_BUYER = 700_002


async def _seed(registry: EngineRegistry, rows: list[dict[str, Any]]) -> None:
    engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(engine) as session:
        for row in rows:
            session.add(P2pTrade(**row))
        await session.commit()


def _trade(trade_id: int, status: str, *, age_hours: int = 1) -> dict[str, Any]:
    return {
        "id": trade_id,
        "order_id": trade_id * 10,
        "seller_id": _SELLER,
        "buyer_id": _BUYER,
        "amount_com": 100,
        "price_per_com": 1.5,
        "total_fiat": 150.0,
        "fiat_currency": "RUB",
        "status": status,
        "created_at": db_now() - timedelta(hours=age_hours),
    }


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    await _seed(registry, [_trade(101, "disputed")])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_p2p_disputes", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_empty(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_p2p_disputes", user_id=42, chat_type="private"),
    )
    rendered = [entry["text"] for entry in sent]
    assert rendered == [t("h_admin_p2p_disputes_empty", "ru")]
    # ``t`` returns the key itself when the key is missing, so the line
    # above would pass against an empty catalog. This is the half that
    # actually pins the copy.
    assert "h_admin_p2p_disputes_empty" not in rendered[0]


@pytest.mark.asyncio
async def test_reissues_one_resolution_card_per_dispute(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """The whole point: the buttons come back, oldest dispute first."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            _trade(101, "disputed", age_hours=5),
            _trade(102, "disputed", age_hours=2),
            _trade(103, "pending"),
            _trade(104, "paid"),
            _trade(105, "dispute_refund_buyer"),
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_p2p_disputes", user_id=42, chat_type="private"),
    )

    # Header + one card per disputed trade, in that order.
    assert len(sent) == 3
    cards = sent[1:]
    assert [card["text"].count("#101") for card in cards] == [1, 0]
    assert [card["text"].count("#102") for card in cards] == [0, 1]
    for card in cards:
        assert str(_SELLER) in card["text"]
        assert str(_BUYER) in card["text"]
        markup = card["markup"]
        assert markup is not None, "a card with no buttons is the bug, not the fix"
        assert len(markup.inline_keyboard) == 3

    # Nothing that is not disputed leaks into the queue.
    joined = "".join(entry["text"] for entry in sent)
    for trade_id in (103, 104, 105):
        assert f"#{trade_id}" not in joined


@pytest.mark.asyncio
async def test_group_invocation_falls_through(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(registry, [_trade(101, "disputed")])
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_p2p_disputes",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []
