"""End-to-end ``/admin_relations``.

Sibling test-suite to :mod:`test_admin_marriages`. Pins are
intentionally identical in shape so a future drift between the
two cards (e.g. one filters NULL-status, the other doesn't) is
caught by side-by-side test diffs rather than only by an operator
noticing weird numbers on prod.

Pins:

* Non-developer → silent drop.
* Empty DB → header with zeros, no top-chats block.
* Active count includes BOTH ``status IS NULL`` AND
  ``status = 'active'`` rows (matches bot.py SELECTs on
  relationships, e.g. bot.py:21920/21963).
* ``status = 'ended'`` rows excluded.
* Distinct-chat count is COUNT(DISTINCT chat_id).
* Top chats ordered by count DESC, ``chat_id`` ASC tiebreak.
* Top capped at 5.
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import Relationship
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


async def _seed(registry: EngineRegistry, rows: list[dict[str, Any]]) -> None:
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        for r in rows:
            session.add(Relationship(**r))
        await session.commit()


def _row(
    *,
    chat_id: int,
    user1_id: int,
    user2_id: int,
    status: str | None = "active",
) -> dict[str, Any]:
    return {
        "chat_id": chat_id,
        "user1_id": user1_id,
        "user2_id": user2_id,
        "created_at": datetime(2026, 1, 1),
        "experience": 0,
        "status": status,
    }


@pytest.mark.asyncio
async def test_silent_for_non_developer(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_relations", user_id=42, chat_type="private"),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_empty(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_relations", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Relationships overview" in text
    assert "active: <code>0</code>" in text
    assert "distinct chats: <code>0</code>" in text
    assert "Top" not in text


@pytest.mark.asyncio
async def test_null_status_counts_as_active(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """Load-bearing parity with marriages: legacy writer at
    bot.py:22036 leaves ``status`` NULL on insert, and every SELECT
    on relationships treats NULL as active. A "cleaner" filter
    would silently halve the count on prod."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            _row(chat_id=-100, user1_id=1, user2_id=2, status=None),
            _row(chat_id=-100, user1_id=3, user2_id=4, status="active"),
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_relations", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "active: <code>2</code>" in text


@pytest.mark.asyncio
async def test_ended_rows_excluded(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            _row(chat_id=-100, user1_id=1, user2_id=2, status="active"),
            _row(chat_id=-100, user1_id=3, user2_id=4, status="ended"),
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_relations", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "active: <code>1</code>" in text


@pytest.mark.asyncio
async def test_distinct_chats_correct(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            _row(chat_id=-100, user1_id=1, user2_id=2),
            _row(chat_id=-100, user1_id=3, user2_id=4),
            _row(chat_id=-200, user1_id=5, user2_id=6),
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_relations", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "active: <code>3</code>" in text
    assert "distinct chats: <code>2</code>" in text


@pytest.mark.asyncio
async def test_top_chats_ordered_by_count_desc_with_chat_id_tiebreak(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            _row(chat_id=-100, user1_id=1, user2_id=2),
            _row(chat_id=-100, user1_id=3, user2_id=4),
            _row(chat_id=-100, user1_id=5, user2_id=6),
            _row(chat_id=-300, user1_id=7, user2_id=8),
            _row(chat_id=-200, user1_id=9, user2_id=10),
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_relations", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    i100 = text.index("<code>-100</code>")
    i300 = text.index("<code>-300</code>")
    i200 = text.index("<code>-200</code>")
    assert i100 < i300 < i200


@pytest.mark.asyncio
async def test_top_capped_at_five(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [_row(chat_id=-(100 + i), user1_id=i * 2, user2_id=i * 2 + 1) for i in range(7)],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_relations", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert text.count("\n  • <code>") == 5
    assert "Top 5 chats" in text


@pytest.mark.asyncio
async def test_group_invocation_falls_through(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_relations",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []
