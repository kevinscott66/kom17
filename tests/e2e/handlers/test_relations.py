"""End-to-end ``/marriages`` + ``/relations`` flows (Stage 19).

Read-only group leaderboards. The piece worth proving end-to-end is:

* Dispatcher routes both commands (and every legacy alias) to the new
  handler in groups, and answers a private invocation with the shared
  "this one lives in groups" refusal (#122).
* Empty chat → "пока нет" friendly message, not silence.
* Populated chat → both pair mentions are HTML-escaped, both bond
  level / category labels mirror legacy phrasing.
* The handler reads the chat's own pairs, not someone else's chat.

Migrated to the shared ``make_wired`` / ``capture_outgoing`` fixtures
at Stage 25.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Update

from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import Marriage, Relationship, User
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from collections.abc import Callable

    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


def _update(text: str, *, chat_id: int = -100123, chat_type: str = "supergroup") -> Update:
    """File-local defaults: supergroup ``-100123``, user 7 named ``Eve``.
    Delegates to the shared builder.
    """
    return make_message_update(
        text,
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=7,
        first_name="Eve",
    )


async def test_marriages_empty(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)

    result = await dispatcher.feed_update(bot, _update("/marriages"))
    assert result is not UNHANDLED
    assert "пока нет браков" in sent[0]["text"].lower()


async def test_marriages_renders_couples_in_xp_order(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        session.add_all(
            [
                User(user_id=1, first_name="Alice"),
                User(user_id=2, first_name="Bob"),
                User(user_id=3, first_name="Carol"),
                User(user_id=4, first_name="Dan"),
                Marriage(
                    chat_id=-100123,
                    user1_id=1,
                    user2_id=2,
                    created_at=datetime.now() - timedelta(days=10),
                    experience=50,
                    status="active",
                ),
                Marriage(
                    chat_id=-100123,
                    user1_id=3,
                    user2_id=4,
                    created_at=datetime.now() - timedelta(days=400),
                    experience=5000,
                    status="active",
                ),
            ]
        )
        await session.commit()

    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/marriages"))
    body = sent[0]["text"]
    # Highest XP first.
    assert body.find("Carol") < body.find("Alice")
    # Both mentions use the tg://user?id= deep link.
    assert 'href="tg://user?id=3"' in body
    assert 'href="tg://user?id=1"' in body
    # Veteran/Newlyweds buckets render the legacy category label.
    assert "Ветераны" in body  # 400-day-old couple
    assert "Молодожёны" in body  # 10-day-old couple


async def test_marriages_escapes_html_in_name(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A user with first_name='<b>Pwn</b>' must not produce bold text
    in everyone else's leaderboard. Same risk class as the shop
    rendering — admin/import-side data can carry HTML.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        session.add_all(
            [
                User(user_id=1, first_name="<b>Pwn</b>"),
                User(user_id=2, first_name="Safe"),
                Marriage(
                    chat_id=-100123,
                    user1_id=1,
                    user2_id=2,
                    created_at=datetime.now() - timedelta(days=5),
                    experience=10,
                    status="active",
                ),
            ]
        )
        await session.commit()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/marriages"))
    body = sent[0]["text"]
    assert "&lt;b&gt;Pwn&lt;/b&gt;" in body
    # The raw HTML tag must NOT appear inside the mention anchor text.
    assert '<a href="tg://user?id=1"><b>Pwn</b></a>' not in body


async def test_marriages_falls_back_to_default_name_when_user_missing(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """A participant who never hit /start has no users row; the
    mention must still render with the legacy fallback name. Same
    contract legacy ``get_user_mention`` enforces at bot.py:15852.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        session.add_all(
            [
                User(user_id=1, first_name="Registered"),
                Marriage(
                    chat_id=-100123,
                    user1_id=1,
                    user2_id=999,
                    created_at=datetime.now() - timedelta(days=3),
                    experience=1,
                    status="active",
                ),
            ]
        )
        await session.commit()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/marriages"))
    body = sent[0]["text"]
    assert 'href="tg://user?id=999"' in body
    assert "Пользователь" in body


@pytest.mark.parametrize("alias", ["/marriages", "/браки", "/пары"])
async def test_marriages_aliases(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    alias: str,
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase])
    capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update(alias))
    assert result is not UNHANDLED


async def test_marriages_with_args_still_renders(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Legacy ``cmd_marriages`` (bot.py:22982) matches the command
    regardless of trailing args and ignores them. A ``magic=F.args.is_(None)``
    filter here turned ``/marriages foo`` into a silent dead-end after the
    bridge was deleted; the filter is removed so args are tolerated.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/marriages foo"))
    assert result is not UNHANDLED
    assert "пока нет браков" in sent[0]["text"].lower()


@pytest.mark.parametrize("alias", ["/marriages", "/браки", "/пары"])
async def test_marriages_in_private_is_refused_out_loud(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    alias: str,
) -> None:
    """Legacy refuses ``/marriages`` outside groups
    (``require_group=True``). This used to assert ``UNHANDLED`` on the
    theory that private chats "fall through to legacy, which renders
    the refusal text" — true only while the telebot process ran beside
    this one. It doesn't, so the fallthrough was silence and the
    refusal was never spoken (#122). Every alias gets it, naming the
    one the user typed.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update(alias, chat_type="private", chat_id=7))
    assert result is not UNHANDLED
    assert "только в группе" in sent[0]["text"]
    assert alias in sent[0]["text"]


# --- /relations ------------------------------------------------------------


async def test_relations_empty(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/relations"))
    assert "пока нет пар" in sent[0]["text"].lower()


async def test_relations_renders_with_level(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        session.add_all(
            [
                User(user_id=1, first_name="Anya"),
                User(user_id=2, first_name="Boris"),
                Relationship(
                    chat_id=-100123,
                    user1_id=1,
                    user2_id=2,
                    created_at=datetime(2024, 5, 1),
                    experience=1500,  # → level 2
                    status="active",
                ),
            ]
        )
        await session.commit()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update("/relations"))
    body = sent[0]["text"]
    assert "ур. 2" in body
    assert "Anya" in body
    assert "Boris" in body
    assert "2024-05-01" in body


@pytest.mark.parametrize("alias", ["/relations", "/отношения_список", "/отны"])
async def test_relations_aliases(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    alias: str,
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase])
    capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update(alias))
    assert result is not UNHANDLED


async def test_relations_with_args_still_renders(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    """Legacy ``cmd_relations`` (bot.py:23480) ignores trailing args.
    Dropping the ``magic=F.args.is_(None)`` filter keeps ``/relations foo``
    from becoming a silent dead-end post-bridge-deletion.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update("/relations foo"))
    assert result is not UNHANDLED
    assert "пока нет пар" in sent[0]["text"].lower()


@pytest.mark.parametrize("alias", ["/relations", "/отношения_список", "/отны"])
async def test_relations_in_private_is_refused_out_loud(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
    alias: str,
) -> None:
    """Same as the ``/marriages`` twin above (#122)."""
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)
    result = await dispatcher.feed_update(bot, _update(alias, chat_type="private", chat_id=7))
    assert result is not UNHANDLED
    assert "только в группе" in sent[0]["text"]
    assert alias in sent[0]["text"]


# --- EN convergence (I18N-4b) ---------------------------------------------

_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _update_en(text: str) -> Update:
    """Supergroup update from an English-locale user (language_code='en')."""
    return make_message_update(
        text,
        chat_id=-100123,
        chat_type="supergroup",
        user_id=7,
        first_name="Eve",
        language_code="en",
    )


async def test_marriages_empty_en_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase])
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update_en("/marriages"))
    assert not _CYRILLIC.search(sent[0]["text"])
    assert "No marriages" in sent[0]["text"]


async def test_marriages_populated_en_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        session.add_all(
            [
                User(user_id=1, first_name="Alice"),
                User(user_id=2, first_name="Bob"),
                Marriage(
                    chat_id=-100123,
                    user1_id=1,
                    user2_id=2,
                    created_at=datetime.now() - timedelta(days=400),
                    experience=5000,
                    status="active",
                ),
                # Participant with no users row → localized fallback name.
                Marriage(
                    chat_id=-100123,
                    user1_id=3,
                    user2_id=999,
                    created_at=datetime.now() - timedelta(days=5),
                    experience=10,
                    status="active",
                ),
            ]
        )
        await session.commit()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update_en("/marriages"))
    body = sent[0]["text"]
    assert not _CYRILLIC.search(body), body
    assert "Marriages in this chat" in body
    assert "Veterans" in body  # 400-day couple category
    assert "User" in body  # localized default name (no Cyrillic "Пользователь")


async def test_relations_populated_en_no_cyrillic(
    make_wired: WiredFactory,
    capture_outgoing: Callable[[Bot], list[dict[str, Any]]],
) -> None:
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase])
    sessionmaker = registry.session(DBName.USERS)
    async with sessionmaker() as session:
        session.add_all(
            [
                User(user_id=1, first_name="Anya"),
                User(user_id=2, first_name="Boris"),
                Relationship(
                    chat_id=-100123,
                    user1_id=1,
                    user2_id=2,
                    created_at=datetime(2024, 5, 1),
                    experience=1500,
                    status="active",
                ),
            ]
        )
        await session.commit()
    sent = capture_outgoing(bot)
    await dispatcher.feed_update(bot, _update_en("/relations"))
    body = sent[0]["text"]
    assert not _CYRILLIC.search(body), body
    assert "Relationships in this chat" in body
    assert "lvl 2" in body
