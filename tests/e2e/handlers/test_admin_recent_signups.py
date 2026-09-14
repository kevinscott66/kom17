"""End-to-end ``/admin_recent_signups``.

Pins:

* Non-developer → silent drop.
* Empty DB or all-NULL ``registered`` → friendly fallback line.
* Rows → newest-first by ``registered`` DESC, full seconds in
  timestamp.
* NULL ``registered`` rows excluded from the result (the card is
  useless for them and they'd otherwise pollute the head/tail
  depending on SQLite version).
* Cap at 10 (12-row DB shows exactly 10 bullets).
* NULL ``referred_by`` → em-dash.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


async def _seed(registry: EngineRegistry, rows: list[dict[str, Any]]) -> None:
    engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(engine) as session:
        for r in rows:
            session.add(EconomyUser(**r))
        await session.commit()


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_recent_signups", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_empty(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=555),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_recent_signups", user_id=555, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Recent signups" in text
    assert "No rows with a registered timestamp" in text


@pytest.mark.asyncio
async def test_null_registered_rows_excluded(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """Legacy economy.users has rows from before the column existed.
    Those NULL-registered rows must NOT appear in the card — they'd
    push the meaningful rows off the bottom and confuse the operator
    (whose interpretation depends on having a timestamp to compare)."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            {"user_id": 100, "registered": None},
            {
                "user_id": 200,
                "registered": datetime(2026, 5, 19, 14, 30, 5),
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_recent_signups", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "<code>200</code>" in text
    assert "<code>100</code>" not in text


@pytest.mark.asyncio
async def test_newest_first_with_full_seconds(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 100,
                "registered": datetime(2026, 5, 19, 14, 30, 0),
            },
            {
                "user_id": 200,
                "registered": datetime(2026, 5, 19, 14, 30, 5),
            },
            {
                "user_id": 300,
                "registered": datetime(2026, 5, 19, 14, 30, 10),
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_recent_signups", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    # Newest (300, 14:30:10) before middle (200) before oldest (100).
    assert text.index("<code>300</code>") < text.index("<code>200</code>")
    assert text.index("<code>200</code>") < text.index("<code>100</code>")
    # Full seconds visible — the whole point of this card is spotting
    # sub-minute signup clusters during raids.
    assert "14:30:10" in text
    assert "14:30:05" in text
    assert "14:30:00" in text


@pytest.mark.asyncio
async def test_capped_at_ten(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            {
                "user_id": i,
                "registered": datetime(2026, 5, 19, 14, 0, i),
            }
            for i in range(1, 13)
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_recent_signups", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert text.count("\n  • <code>") == 10


@pytest.mark.asyncio
async def test_null_referred_by_renders_as_dash(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 100,
                "registered": datetime(2026, 1, 1),
                "referred_by": None,
            },
            {
                "user_id": 200,
                "registered": datetime(2026, 1, 2),
                "referred_by": 999,
            },
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_recent_signups", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    # None must not surface as the string "None".
    assert "None" not in text
    # User 200 has a real referrer.
    assert "ref=<i><code>999</code></i>" in text
    # User 100 has no referrer — rendered as em-dash.
    assert "ref=<i>—</i>" in text


@pytest.mark.asyncio
async def test_group_invocation_falls_through(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, _ = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_recent_signups",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []
