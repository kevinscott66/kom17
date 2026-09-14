"""End-to-end ``/admin_donations``.

Pins:

* Non-developer → silent drop.
* Developer + empty DB → summary line with zeros, no top/recent blocks.
* Developer + rows → count, lifetime-sum, top-donors GROUP BY,
  newest-first recent sample.
* Top donors ranked by lifetime sum (not by donation count).
* Recent capped at 5 (7-row DB shows exactly 5 bullets).
* HTML-escape on free-text ``message``.
* ``message`` truncated past 30 chars.
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
from telegram_invite_bot.db.models.economy import Donation
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


async def _seed(registry: EngineRegistry, rows: list[dict[str, Any]]) -> None:
    engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(engine) as session:
        for r in rows:
            session.add(Donation(**r))
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
        make_message_update("/admin_donations", user_id=42, chat_type="private"),
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
        make_message_update("/admin_donations", user_id=555, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Donations overview" in text
    assert "count: <code>0</code>" in text
    assert "lifetime DLAB: <code>0</code>" in text
    # No top/recent blocks on empty DB — pinned to keep the empty card
    # under a single screen.
    assert "Top" not in text
    assert "Latest" not in text


@pytest.mark.asyncio
async def test_top_donors_ranked_by_lifetime(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """Top is sum-of-amount per user_id, not count-of-rows. Pin this:
    a donor with one big donation must outrank a donor with many small
    ones if the sum is larger. This is the cohort an operator first
    scrutinises during a fraud sweep."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            # User 100: one big donation, lifetime = 10000
            {
                "user_id": 100,
                "group_id": -1,
                "amount": 10000,
                "created_at": datetime(2026, 1, 1),
            },
            # User 200: five small donations, lifetime = 500
            *[
                {
                    "user_id": 200,
                    "group_id": -1,
                    "amount": 100,
                    "created_at": datetime(2026, 1, i + 2),
                }
                for i in range(5)
            ],
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_donations", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    # 100 (lifetime 10000) must rank above 200 (lifetime 500).
    pos_100 = text.find("<code>100</code> — <code>10000</code>")
    pos_200 = text.find("<code>200</code> — <code>500</code>")
    assert pos_100 != -1
    assert pos_200 != -1
    assert pos_100 < pos_200


@pytest.mark.asyncio
async def test_recent_capped_at_five_newest_first(
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
                "user_id": 1,
                "group_id": -1,
                "amount": 100,
                "message": f"d{i}",
                "created_at": datetime(2026, 1, i + 1),
            }
            for i in range(7)
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_donations", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    # 5 recent bullets despite 7 rows.
    recent_block = text.split("Latest")[1] if "Latest" in text else ""
    assert recent_block.count("  • <code>#") == 5
    # Newest (id=7, "d6") must appear before oldest visible (id=3, "d2").
    assert recent_block.index("d6") < recent_block.index("d2")


@pytest.mark.asyncio
async def test_message_escaped_and_truncated(
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
                "user_id": 1,
                "group_id": -1,
                "amount": 50,
                "message": "<script>alert(1)</script>" + "x" * 40,
                "created_at": datetime(2026, 1, 1),
            }
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_donations", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    # Raw HTML must NOT appear.
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    # Truncation marker appears (message was 65 chars, well over the
    # 30-char cap).
    assert "…" in text


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
            "/admin_donations",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []
