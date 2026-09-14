"""End-to-end ``/admin_marriages``.

Pins:

* Non-developer → silent drop.
* Empty DB → header with zeros, no top-chats block.
* Active count includes BOTH ``status IS NULL`` AND ``status = 'active'``
  rows — the load-bearing legacy convention (bot.py:22990). A future
  refactor that "cleans up" the NULL branch would silently halve the
  count on prod, where the majority of rows are still NULL.
* ``status = 'divorced'`` rows excluded.
* Distinct-chat count is COUNT(DISTINCT chat_id), not COUNT — two
  marriages in one chat contribute 2 to active, 1 to distinct.
* Top chats ordered by count DESC, ``chat_id`` ASC on ties (deterministic
  output for snapshot-style operator comparison across runs).
* Top capped at 5 (a 7-chat seed shows exactly 5).
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
from telegram_invite_bot.db.models.users import Marriage
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


async def _seed(registry: EngineRegistry, rows: list[dict[str, Any]]) -> None:
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        for r in rows:
            session.add(Marriage(**r))
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
        make_message_update("/admin_marriages", user_id=42, chat_type="private"),
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
        make_message_update("/admin_marriages", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "Marriages overview" in text
    assert "active: <code>0</code>" in text
    assert "distinct chats: <code>0</code>" in text
    assert "Top" not in text  # no top-chats block on empty


@pytest.mark.asyncio
async def test_null_status_counts_as_active(
    make_wired: WiredFactory, capture_outgoing: Any
) -> None:
    """Load-bearing: legacy ``/marry_accept`` leaves ``status`` NULL,
    and bot.py:22990's WHERE clause treats NULL as active. If the new
    card filters on ``status == 'active'`` only, the count silently
    halves on real data."""
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
        make_message_update("/admin_marriages", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "active: <code>2</code>" in text


@pytest.mark.asyncio
async def test_divorced_rows_excluded(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed(
        registry,
        [
            _row(chat_id=-100, user1_id=1, user2_id=2, status="active"),
            _row(chat_id=-100, user1_id=3, user2_id=4, status="divorced"),
            _row(chat_id=-100, user1_id=5, user2_id=6, status="ended"),
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_marriages", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    assert "active: <code>1</code>" in text


@pytest.mark.asyncio
async def test_distinct_chats_correct(make_wired: WiredFactory, capture_outgoing: Any) -> None:
    """COUNT(DISTINCT chat_id), not COUNT — two pairs in one chat must
    contribute 2 to active but only 1 to distinct."""
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
        make_message_update("/admin_marriages", user_id=42, chat_type="private"),
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
            # chat -100: 3 pairs (winner)
            _row(chat_id=-100, user1_id=1, user2_id=2),
            _row(chat_id=-100, user1_id=3, user2_id=4),
            _row(chat_id=-100, user1_id=5, user2_id=6),
            # chat -300: 1 pair, tied with -200 on count
            _row(chat_id=-300, user1_id=7, user2_id=8),
            # chat -200: 1 pair, tied — must come BEFORE -300 (lower chat_id)
            _row(chat_id=-200, user1_id=9, user2_id=10),
        ],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update("/admin_marriages", user_id=42, chat_type="private"),
    )
    text = sent[0]["text"]
    i100 = text.index("<code>-100</code>")
    i300 = text.index("<code>-300</code>")
    i200 = text.index("<code>-200</code>")
    # Winner first.
    assert i100 < i300
    assert i100 < i200
    # Tiebreak: -300 < -200 numerically, so -300 comes first under ASC.
    assert i300 < i200


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
        make_message_update("/admin_marriages", user_id=42, chat_type="private"),
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
            "/admin_marriages",
            user_id=42,
            chat_id=-100_555,
            chat_type="supergroup",
        ),
    )
    assert result is UNHANDLED
    assert sent == []
