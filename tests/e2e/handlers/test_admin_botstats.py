"""End-to-end ``/admin_botstats``.

Pins three contracts:

* Non-developers get **no** reply (silent drop — same enumeration-
  defence posture as ``/admin_status``).
* A developer running it in a **non-private** chat also gets silence,
  because rendering counts in a public group would leak operational
  numbers to anyone watching the chat — the screenshot-forwarding
  threat the silent gate is meant to close.
* A developer in a private chat sees both numbers, sourced from the
  real ``users`` and ``bot_groups`` tables (not hardcoded), so a
  schema mismatch with prod surfaces immediately.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import BotConfig
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import BotGroup, User
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


async def _seed(registry: EngineRegistry, *, users: int, groups: int, left_groups: int = 0) -> None:
    """Insert ``users`` rows into ``users`` and ``groups`` rows into
    ``bot_groups``. IDs are deterministic so the test reads exactly
    the rows it wrote — no risk of double-counting fixtures from
    another test that landed in the same in-memory engine (the
    factory builds a fresh tmp_path per test, but the seed shape
    documents the contract anyway).

    ``left_groups`` adds rows the bot is no longer in (``is_active=0``,
    #111). They exist on purpose: the row survives a removal so the
    original owner keeps their payout attribution, which means the
    counter has to exclude them explicitly.
    """
    users_engine = registry.engine(DBName.USERS)
    async with AsyncSession(users_engine) as session:
        for i in range(users):
            session.add(User(user_id=10_000 + i, first_name=f"u{i}"))
        for i in range(groups + left_groups):
            session.add(
                BotGroup(
                    chat_id=-100_000 - i,
                    added_by_user_id=10_000,
                    added_at=datetime(2024, 1, 1),
                    chat_title=f"g{i}",
                    is_active=0 if i >= groups else 1,
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_admin_botstats_silent_for_non_developer(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, _registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=999),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/admin_botstats", user_id=42))
    assert sent == []


@pytest.mark.asyncio
async def test_admin_botstats_silent_in_group_for_developer(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Dev in a group chat — still silent. The point of the silent-drop
    is that a screenshot of the chat must not leak that any admin
    command exists at all; rendering "wrong chat type, try DM" would
    be a tell.
    """
    bot, dispatcher, _registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=555),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(
        bot,
        make_message_update(
            "/admin_botstats", user_id=555, chat_id=-100123, chat_type="supergroup"
        ),
    )
    assert sent == []


@pytest.mark.asyncio
async def test_admin_botstats_renders_for_developer(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=777),
    )
    await _seed(registry, users=7, groups=3)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, make_message_update("/admin_botstats", user_id=777))

    assert len(sent) == 1
    text = sent[0]["text"]
    # Numbers come from the seeded rows, not from a hardcoded template.
    assert "<code>7</code>" in text
    assert "<code>3</code>" in text
    assert "Статистика бота" in text


@pytest.mark.asyncio
async def test_admin_botstats_renders_zero_when_empty(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Fresh schema, no rows — the card still renders (with zeros).
    Important: the COUNT query must not error on an empty table or
    the operator's first call after a fresh deploy would look like
    a bug.
    """
    bot, dispatcher, _registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=888),
    )
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, make_message_update("/admin_botstats", user_id=888))
    assert len(sent) == 1
    text = sent[0]["text"]
    assert "<code>0</code>" in text


@pytest.mark.asyncio
async def test_admin_botstats_counts_only_groups_the_bot_is_still_in(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """Groups the bot was removed from must not inflate the counter.

    Their rows stay in ``bot_groups`` deliberately (#111) so a kick and
    re-add cannot reassign the group's payout owner. The consequence is
    that every read has to filter on ``is_active`` — and this counter is
    the one the operator uses to judge growth, so a stale number here is
    a decision made on fiction.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=333),
    )
    await _seed(registry, users=2, groups=1, left_groups=4)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(bot, make_message_update("/admin_botstats", user_id=333))

    text = sent[0]["text"]
    assert "<code>1</code>" in text
    # 1 active + 4 left. Anything but 1 means the filter is gone.
    assert "<code>5</code>" not in text, text


@pytest.mark.asyncio
async def test_admin_botstats_answers_in_the_developers_language(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """The counters card was a Russian f-string. Developers are users
    too and the roster is not guaranteed Russian-speaking; the numbers
    are asserted alongside the copy so a template that translates but
    drops the substitutions still fails.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc"), DEVELOPER_ID_1=222),
    )
    await _seed(registry, users=4, groups=2)
    sent = capture_outgoing(bot)

    await dispatcher.feed_update(
        bot, make_message_update("/admin_botstats", user_id=222, language_code="en")
    )

    text = sent[0]["text"]
    assert "Bot statistics" in text
    assert "<code>4</code>" in text
    assert "<code>2</code>" in text
    assert not any("Ѐ" <= ch <= "ӿ" for ch in text), text
