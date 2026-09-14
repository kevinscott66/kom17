"""End-to-end ``/admin_check_groups``.

Pins:

* Non-developer → silent drop (existence of the command isn't a
  side-channel for enumerating dev IDs).
* Developer in private → counts + bot_groups sample render.
* Counts come from the *current* DB state — empty DB shows zeros,
  populated DB shows totals.
* Sample is capped at 5 rows. A 7-row DB shows exactly 5 lines.
* Titles with HTML-special chars are escaped (operator-typed via
  Telegram; bot renders under HTML parse_mode).
* Group invocation → router-level private filter rejects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import BotGroup, GroupSettings
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


async def _seed_groups(
    registry: EngineRegistry,
    *,
    bot_groups: list[tuple[int, str | None, int]],
    group_settings_ids: list[int] | None = None,
) -> None:
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        for chat_id, title, added_by in bot_groups:
            session.add(
                BotGroup(
                    chat_id=chat_id,
                    chat_title=title,
                    added_by_user_id=added_by,
                )
            )
        for gid in group_settings_ids or ():
            session.add(GroupSettings(group_id=gid))
        await session.commit()


@pytest.mark.asyncio
async def test_silent_for_non_developer(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_check_groups", user_id=42, chat_type="private")
    )
    assert sent == []


@pytest.mark.asyncio
async def test_renders_empty_db(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=555),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_check_groups", user_id=555, chat_type="private")
    )
    assert len(sent) == 1
    text = sent[0]["text"]
    assert "Groups diagnostics" in text
    assert "<code>0</code>" in text  # both counts are zero
    # No sample block when empty.
    assert "bot_groups sample" not in text


@pytest.mark.asyncio
async def test_renders_with_data(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=777),
    )
    await _seed_groups(
        registry,
        bot_groups=[
            (-1001, "Alpha", 100),
            (-1002, "Beta", 200),
        ],
        group_settings_ids=[-1001, -1002, -1003],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_check_groups", user_id=777, chat_type="private")
    )
    text = sent[0]["text"]
    # Counts: 2 bot_groups, 3 group_settings.
    assert "bot_groups: <code>2</code>" in text
    assert "group_settings: <code>3</code>" in text
    # Sample lines for both rows.
    assert "Alpha" in text
    assert "Beta" in text
    assert "<code>-1001</code>" in text


@pytest.mark.asyncio
async def test_sample_capped_at_five(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """A 7-row DB must show exactly 5 sample lines — protects the
    operator card from growing unreadable on a large prod DB."""
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=111),
    )
    await _seed_groups(
        registry,
        bot_groups=[(-1000 - i, f"G{i}", 1) for i in range(7)],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_check_groups", user_id=111, chat_type="private")
    )
    text = sent[0]["text"]
    # Count is the truth (7), not the cap (5).
    assert "bot_groups: <code>7</code>" in text
    # Sample lines: count the leading "  • " markers in the rendered
    # sample block — caps the visible roster regardless of total.
    sample_lines = text.count("\n  • ")
    assert sample_lines == 5


@pytest.mark.asyncio
async def test_escapes_html_in_title(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    await _seed_groups(
        registry,
        bot_groups=[(-1001, "<script>alert(1)</script> & more", 1)],
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot, make_message_update("/admin_check_groups", user_id=42, chat_type="private")
    )
    text = sent[0]["text"]
    # Raw < / > / & must not appear in operator-controlled positions.
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert "&amp; more" in text


@pytest.mark.asyncio
async def test_group_invocation_falls_through(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Router-level private filter must keep this command from rendering
    DB internals in front of regular group members. Legacy short-
    circuits on ``chat.type != "private"`` too — same posture."""
    from aiogram.dispatcher.event.bases import UNHANDLED

    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=42),
    )
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_check_groups", user_id=42, chat_id=-100_555, chat_type="supergroup"
        ),
    )
    assert result is UNHANDLED
    assert sent == []
