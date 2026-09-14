"""L-60 e2e: a seeded group alias word dispatches its target through the FULL router.

Unit tests cover the middleware in isolation; this is the dispatch-level
guard (alias word -> GroupAliasMiddleware rewrite -> Command filter -> reply).
NOTE: in PROD this additionally requires the bot to be a group ADMIN (or
privacy mode off) — Telegram does not deliver plain group text to a
plain-member bot at all (live-debugged 2026-06-11).
"""

from __future__ import annotations

import pytest

from telegram_invite_bot.db.models.base import EconomyBase, ModerationBase, UsersBase
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.group_aliases_repo import GroupAliasRepo

pytestmark = pytest.mark.asyncio

GROUP_ID = -100777
USER_ID = 4242


async def test_alias_word_dispatches_target(make_wired, capture_outgoing):
    from tests.e2e.handlers.conftest import make_message_update

    bot, dispatcher, registry = await make_wired(
        schemas=[ModerationBase, UsersBase, EconomyBase], session_middleware=True
    )
    sink = capture_outgoing(bot)
    async with registry.session(DBName.MODERATION)() as session:
        await GroupAliasRepo(session).upsert(
            group_id=GROUP_ID, word="тестпинг", target_command="ping", added_by=USER_ID
        )
        await session.commit()
    upd = make_message_update("тестпинг", chat_id=GROUP_ID, chat_type="supergroup", user_id=USER_ID)
    res = await dispatcher.feed_update(bot, upd)
    texts = [e.get("text", "")[:70] for e in sink]
    print("DISPATCH:", res)
    print("OUTGOING:", texts)
    assert texts, f"alias word produced NO outgoing message; dispatch result={res!r}"
