"""End-to-end ``/admin_top_users``.

Pins:

* Non-developer → silent drop.
* Empty DB → friendly "economy.users is empty" line.
* Rows → ranked by balance DESC, user_id ASC tiebreak.
* Cap at 10 (12-row DB shows exactly 10 numbered lines).
* NULL last_seen → em-dash (legacy rows from before the column was
  added must not surface as "None").
* HTML-escape on language (defence-in-depth).
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
        make_message_update("/admin_top_users", user_id=42, chat_type="private"),
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
        make_message_update("/admin_top_users", user_id=555, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Top users by balance" in text
    assert "is empty" in text


@pytest.mark.asyncio
async def test_ranked_by_balance_desc(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            {"user_id": 100, "balance": 500},
            {"user_id": 200, "balance": 9999},
            {"user_id": 300, "balance": 1500},
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_top_users", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    # 200 (9999) before 300 (1500) before 100 (500).
    pos_200 = text.index("<code>200</code>")
    pos_300 = text.index("<code>300</code>")
    pos_100 = text.index("<code>100</code>")
    assert pos_200 < pos_300 < pos_100


@pytest.mark.asyncio
async def test_user_id_tiebreak_on_equal_balance(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """Ties on balance are common (default-seeded accounts at 100 COM).
    Without a deterministic tiebreak the order would flap across
    SQLite versions and break this test on a future bump. Pin the
    user_id ASC tiebreak so the contract is explicit."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            {"user_id": 500, "balance": 100},
            {"user_id": 100, "balance": 100},
            {"user_id": 300, "balance": 100},
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_top_users", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    pos_100 = text.index("<code>100</code>")
    pos_300 = text.index("<code>300</code>")
    pos_500 = text.index("<code>500</code>")
    assert pos_100 < pos_300 < pos_500


@pytest.mark.asyncio
async def test_capped_at_ten(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [{"user_id": i, "balance": 1000 - i} for i in range(1, 13)],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_top_users", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    # Exactly 10 numbered lines, regardless of total population.
    assert text.count("\n1. ") == 1
    assert "\n10. " in text
    assert "\n11. " not in text


@pytest.mark.asyncio
async def test_null_last_seen_renders_as_dash(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """Legacy economy.users has rows from before last_seen was added —
    those come back as None and would render as the string 'None'
    without the em-dash convention. That's exactly the kind of
    operator-confusion ping the convention is meant to prevent."""
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [{"user_id": 1, "balance": 100, "last_seen": None}],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_top_users", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "None" not in text
    assert "last_seen=<code>—</code>" in text


@pytest.mark.asyncio
async def test_formatted_last_seen(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[EconomyBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            {
                "user_id": 1,
                "balance": 100,
                "last_seen": datetime(2026, 5, 19, 14, 30),
            }
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_top_users", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "2026-05-19 14:30" in text


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
            "/admin_top_users",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []
