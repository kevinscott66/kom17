"""End-to-end ``/rules`` (Stage 22).

Pins:

* Group call with rules set → renders title + rules body.
* Group call with no row in ``group_settings`` → "rules empty"
  localised text.
* Group call with NULL/whitespace rules → same "empty" text (three
  on-disk shapes, one user-visible semantics).
* Private DM → the #123 refusal twin answers "group only"; the
  router-level group filter still keeps the renderer out.
* HTML in the persisted rules is escaped — operators can free-text
  type ``<`` into rules through legacy ``/setrules`` and the
  bot-wide HTML parse_mode must not choke on it.
* Localised header — ``en`` user gets the English title, ``ru`` user
  the Russian one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import GroupSettings, User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from tests.e2e.handlers.conftest import assert_chat_scope_refusal, make_message_update

if TYPE_CHECKING:
    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


_CHAT_ID = -100_555
_USER_ID = 4242


async def _seed(
    registry: EngineRegistry,
    *,
    rules: str | None,
    create_row: bool = True,
    user_lang: str = "ru",
) -> None:
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        # Pre-create the user so UserService.touch reads a stable language
        # row instead of upserting with the Telegram-supplied language_code.
        session.add(User(user_id=_USER_ID, first_name="R", language_code=user_lang))
        if create_row:
            session.add(GroupSettings(group_id=_CHAT_ID, rules=rules))
        await session.commit()


def _group_update(text: str, *, lang: str = "ru") -> Any:
    return make_message_update(
        text,
        user_id=_USER_ID,
        chat_id=_CHAT_ID,
        chat_type="supergroup",
        language_code=lang,
    )


@pytest.mark.asyncio
async def test_rules_renders_for_group(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed(registry, rules="Не флудить. Уважать друг друга.")
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _group_update("/rules"))

    assert result is not UNHANDLED
    body = sent[-1]["text"]
    assert "Правила группы" in body
    assert "Не флудить" in body


@pytest.mark.asyncio
async def test_rules_empty_when_no_row(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed(registry, rules=None, create_row=False)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _group_update("/rules"))
    assert sent[-1]["text"] == t("rules_empty", "ru")


@pytest.mark.asyncio
async def test_rules_empty_when_null(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed(registry, rules=None, create_row=True)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _group_update("/rules"))
    assert sent[-1]["text"] == t("rules_empty", "ru")


@pytest.mark.asyncio
async def test_rules_empty_when_whitespace(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed(registry, rules="   \n  \t  ", create_row=True)
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _group_update("/rules"))
    assert sent[-1]["text"] == t("rules_empty", "ru")


@pytest.mark.asyncio
async def test_rules_private_is_refused_not_rendered(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    """A DM ``/rules`` gets "group only", never a group's rules (#123).

    The router-level group filter MUST keep private DMs out of the
    worker: if that regresses, a DM would render the seeded rules
    text — which is why the assertion is an exact match on the refusal
    rather than "something was sent".
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed(registry, rules="ignored")
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(
        bot,
        make_message_update("/rules", user_id=_USER_ID, chat_type="private"),
    )
    assert result is not UNHANDLED
    assert_chat_scope_refusal(sent, scope="group", command="rules")


@pytest.mark.asyncio
async def test_rules_escapes_html(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed(registry, rules="<script>alert(1)</script> & more")
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _group_update("/rules"))
    body = sent[-1]["text"]
    # Raw < / > / & MUST be entity-escaped — otherwise Telegram either
    # rejects the message (HTML parse error) or worse, renders as markup.
    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "&amp; more" in body


@pytest.mark.asyncio
async def test_rules_uses_user_language(
    make_wired: WiredFactory,
    capture_outgoing: Any,
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    await _seed(registry, rules="Be kind.", user_lang="en")
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _group_update("/rules", lang="en"))
    body = sent[-1]["text"]
    assert t("rules_title", "en") in body
