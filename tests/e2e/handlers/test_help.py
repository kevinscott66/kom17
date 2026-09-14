"""End-to-end ``/help`` flow: dispatcher → SessionMiddleware → DB.

Stage 8 pilot — mirrors the ``/profile`` fixture pattern. Language-
aware, chat-type-agnostic and arg-tolerant, matching legacy
``cmd_help`` (which answers in groups and ignores trailing args).

Migrated to the shared ``make_wired`` factory at Stage 25. The
bespoke ``_capture`` stays because the tests inspect
``SendMessage.reply_markup`` (inline keyboard), which the shared
``capture_outgoing`` helper deliberately doesn't record.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Chat, Message, Update
from aiogram.types import User as TelegramUser
from pydantic import SecretStr
from sqlalchemy import update as sql_update

from telegram_invite_bot.config.settings import BotConfig, HelpConfig
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers import help as help_module
from telegram_invite_bot.handlers.help_catalog import render_help_pages
from tests.e2e.handlers.conftest import make_message_update

# Telegram's supported inline tags for message text.
_SUPPORTED_TAGS = {
    variant for tag in ("b", "i", "u", "s", "code", "pre") for variant in (tag, f"/{tag}")
}

if TYPE_CHECKING:
    from aiogram import Bot

    from tests.e2e.handlers.conftest import WiredFactory


def _update(
    text: str,
    *,
    chat_type: str = "private",
    user_id: int = 3131,
    language_code: str = "ru",
) -> Update:
    """File-local defaults: user 3131 named ``Helga`` (ru). Delegates to
    the shared builder.
    """
    return make_message_update(
        text,
        chat_type=chat_type,
        user_id=user_id,
        first_name="Helga",
        language_code=language_code,
    )


def _page_count(lang: str = "ru", *, has_button: bool = False) -> int:
    """Messages one plain-user ``/help`` produces right now.

    The card stopped fitting a single message when the 74 uncatalogued
    commands joined the catalog (#114), and ``handle_help`` answers with
    one message per page. Derived from the renderer rather than pinned
    to a number so these tests keep asserting *shape* — button on the
    last page, body across all of them — instead of re-breaking every
    time the catalog grows a row.
    """
    return len(render_help_pages(lang, has_button=has_button))


def _capture(bot: Bot, monkeypatch: pytest.MonkeyPatch, sink: list[dict[str, Any]]) -> None:
    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        if type(method).__name__ == "SendMessage":
            sink.append({"text": method.text, "reply_markup": method.reply_markup})
            return Message(
                message_id=1,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
                text=method.text,
            )
        if type(method).__name__ == "GetMe":
            # The group card's DM deep link needs a username (#632).
            return TelegramUser(id=0, is_bot=True, first_name="bot", username="kom17bot")
        raise AssertionError(f"unexpected Telegram call: {type(method).__name__}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


async def test_help_renders_ru_card_for_ru_user(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    result = await dispatcher.feed_update(bot, _update("/help"))
    assert result is not UNHANDLED
    assert len(sent) == _page_count("ru")
    body = "\n".join(message["text"] for message in sent)
    assert "Что я умею" in body
    # Category headings + a command from each end of the catalog: the
    # card is the *whole* user surface now, not four hand-picked lines.
    assert "Базовые" in body
    assert "Экономика" in body
    assert "Ком и общение" in body
    assert "• /start —" in body
    assert "• /balance —" in body
    assert "• /rp_commands —" in body
    # A plain user must not learn the moderation surface from /help.
    assert "/ban —" not in body


async def test_help_renders_en_card_for_en_user(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update("/help", language_code="en"))
    body = "\n".join(message["text"] for message in sent)
    assert "What I can do" in body
    assert "Basics" in body
    assert "• /start —" in body


async def test_help_aliases_match(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    for alias in ("/h", "/commands", "/kom_help"):
        result = await dispatcher.feed_update(bot, _update(alias))
        assert result is not UNHANDLED, alias
    assert len(sent) == 3 * _page_count("ru")


async def test_help_renders_in_group(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Group ``/help`` renders the card — parity with legacy
    ``cmd_help`` (bot.py:25202), which answers in groups with NO
    ``require_group_feature`` gate.

    REGRESSION PIN: after the legacy telebot bridge was deleted, a
    router-level PRIVATE filter turned group ``/help`` into a silent
    dead-end. Only the cosmetic group extras (slot-message tracking,
    auto-delete, main-menu keyboard) are deferred — the help body
    itself must still render in groups.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    result = await dispatcher.feed_update(bot, _update("/help", chat_type="group"))
    assert result is not UNHANDLED
    assert "Что я умею" in sent[0]["text"]


async def test_help_drops_only_the_telegraph_button_when_url_unset(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No telegraph URL → no *guide* button, but the menu still rides.

    REGRESSION PIN (#632): this test used to assert every page came back
    with ``reply_markup is None``, i.e. it pinned the one keyboard shape
    legacy could never emit — ``bot.py:25249-25257`` always appended the
    main-menu rows and fell back to a lone button when even those were
    empty. An unset URL drops the Telegraph row only.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update("/help"))
    assert all(message["reply_markup"] is None for message in sent[:-1])
    rows = sent[-1]["reply_markup"].inline_keyboard
    buttons = [button for row in rows for button in row]
    assert buttons, "the last page must still carry the navigation keyboard"
    assert all(button.url is None for button in buttons), "no guide URL was configured"
    assert any((button.callback_data or "").startswith("menu:") for button in buttons)
    # No guide button → the "press the button below" footer must NOT
    # appear, otherwise the copy promises a button that never rendered.
    assert "кнопке ниже" not in "\n".join(message["text"] for message in sent)


async def test_help_in_group_offers_the_dm_link_not_dead_menu_buttons(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Group card carries a DM deep link instead of the menu rows.

    Every :class:`MainMenu` callback is registered behind
    ``F.message.chat.type == ChatType.PRIVATE`` (the router-level
    filter in ``main_menu.build_router``), so attaching those buttons to
    a group card would render them dead. The deep link is the deliberate
    substitute — see ``help._build_keyboard``.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update("/help", chat_type="group"))
    rows = sent[-1]["reply_markup"].inline_keyboard
    buttons = [button for row in rows for button in row]
    assert not any((button.callback_data or "").startswith("menu:") for button in buttons)
    assert [button.url for button in buttons] == ["https://t.me/kom17bot?start"]


async def test_help_renders_ru_button_when_url_set(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        help_config=HelpConfig(
            TELEGRAPH_COMMANDS_URL="https://telegra.ph/ru-guide",
            TELEGRAPH_COMMANDS_URL_EN="https://telegra.ph/en-guide",
        ),
    )
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update("/help"))
    assert len(sent) == _page_count("ru", has_button=True)
    # The Telegraph offer rides the FINAL page only — repeated under
    # every chunk it reads as three separate offers, and attached to the
    # first it arrives before the list it is offering to replace.
    assert all(message["reply_markup"] is None for message in sent[:-1])
    markup = sent[-1]["reply_markup"]
    assert markup is not None
    rows = markup.inline_keyboard
    # The guide leads; the menu rows follow it (#632).
    assert len(rows[0]) == 1
    assert rows[0][0].url == "https://telegra.ph/ru-guide"
    assert "список" in rows[0][0].text.lower()
    assert any(
        (button.callback_data or "").startswith("menu:") for row in rows[1:] for button in row
    )
    # Button present → the footer prompt pointing at it should render.
    assert "кнопке ниже" in "\n".join(message["text"] for message in sent)


async def test_help_renders_en_button_when_url_set(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        help_config=HelpConfig(
            TELEGRAPH_COMMANDS_URL="https://telegra.ph/ru-guide",
            TELEGRAPH_COMMANDS_URL_EN="https://telegra.ph/en-guide",
        ),
    )
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update("/help", language_code="en"))
    markup = sent[-1]["reply_markup"]
    assert markup is not None
    assert markup.inline_keyboard[0][0].url == "https://telegra.ph/en-guide"


async def test_help_body_carries_only_supported_html(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dispatcher sends with ``parse_mode=HTML``, and the card is now
    assembled from ~70 yaml values. One raw ``<`` anywhere in that pile
    (legacy's ``<город>`` placeholder was exactly this shape) makes
    Telegram reject the whole send with ``Unsupported start tag``.

    Assert every ``<…>`` in the rendered body is a tag Telegram actually
    supports, in both locales.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update("/help"))
    await dispatcher.feed_update(bot, _update("/help", language_code="en", user_id=3132))

    assert len(sent) == _page_count("ru") + _page_count("en")
    for message in sent:
        tags = {m.group(1).split(" ", 1)[0] for m in re.finditer(r"<([^>]*)>", message["text"])}
        assert tags <= _SUPPORTED_TAGS, sorted(tags - _SUPPORTED_TAGS)


async def test_help_developer_sees_admin_reference(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RR-6 #63: the rank-annotated owner reference legacy served from
    ``/owner_help`` lives inside role-aware ``/help`` — ``/owner_help``
    is already the developer *diagnostics* index in this pipeline.
    """
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=BotConfig(BOT_TOKEN=SecretStr("123:abc"), DEVELOPER_ID_1=3131),
    )
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update("/help"))
    body = "\n".join(message["text"] for message in sent)
    assert "Модерация" in body
    assert "Админ" in body
    assert "• /ban —" in body
    assert "• /cmdcfg —" in body
    # Effective-rank annotations (legacy's ``[от N⭐]``).
    assert "⭐" in body


async def test_help_plain_user_gets_no_admin_reference(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same wiring, different user id — the developer view must be keyed
    on identity, not on the command being reachable."""
    bot, dispatcher, _ = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=BotConfig(BOT_TOKEN=SecretStr("123:abc"), DEVELOPER_ID_1=999999),
    )
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    await dispatcher.feed_update(bot, _update("/help"))
    body = "\n".join(message["text"] for message in sent)
    assert "• /ban —" not in body
    assert "• /cmdcfg —" not in body
    assert "⭐" not in body


async def test_help_with_args_renders(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/help something`` renders the same card — legacy ``cmd_help``
    matches ``commands=['help', ...]`` regardless of trailing args, so
    ``/help admin`` is just ``/help``.

    REGRESSION PIN: an earlier ``magic=F.args.is_(None)`` dropped argful
    invocations into the (now-deleted) legacy bridge, silently eating
    ``/help admin``. The Command filter must match args.
    """
    bot, dispatcher, _ = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)

    result = await dispatcher.feed_update(bot, _update("/help admin"))
    assert result is not UNHANDLED
    assert "Что я умею" in sent[0]["text"]


async def test_help_does_not_hold_the_write_lock_across_the_render(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/help`` asks Telegram who the caller is, then answers page by
    page; users.db must stay writable throughout.

    ``user_service.touch`` opens the update's write transaction, and
    ``BEGIN IMMEDIATE`` means one writer per DB until the middleware
    commits — which, without a checkpoint, is after one
    ``getChatMember`` and up to three ``sendMessage`` calls. Everyone
    else's update would spend ``busy_timeout`` waiting and then fail
    with ``database is locked``. The probe runs inside the admin
    lookup, exactly where a real request would be in flight.
    """
    bot, dispatcher, registry = await make_wired(schemas=[UsersBase], session_middleware=True)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent)
    other_updates_could_write: list[bool] = []

    async def is_admin_while_probing(_message: Any, _bot: Any, _uid: int) -> bool:
        async with registry.session(DBName.USERS)() as other:
            await other.execute(
                sql_update(User).where(User.user_id == 3131).values(messages_count=1)
            )
            await other.commit()
        other_updates_could_write.append(True)
        return False

    monkeypatch.setattr(help_module, "_is_chat_admin", is_admin_while_probing)

    result = await dispatcher.feed_update(bot, _update("/help"))

    assert result is not UNHANDLED
    assert other_updates_could_write == [True]
    assert "Что я умею" in sent[0]["text"]
