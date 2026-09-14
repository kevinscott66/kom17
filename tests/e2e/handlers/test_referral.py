"""End-to-end ``/referral`` (Stage 28).

Pins:

* Renders a t.me deep-link of the form ``?start=ref_<user_id>``
  where ``<user_id>`` is the caller's id (not a hardcoded value).
* Commission percent from settings appears verbatim in the body.
* ``get_me`` failure falls back to ``YourBot`` placeholder rather
  than crashing the handler — the whole point of the link is to
  drive growth, and a transient API hiccup must not block it.
* Russian and English variants render the correct title/body.
* All three command aliases route to the handler.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import Chat, Message
from aiogram.types import User as TelegramUser
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.config.settings import BotConfig, EconomyConfig
from telegram_invite_bot.db.models.base import UsersBase
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from tests.e2e.handlers.conftest import make_message_update

if TYPE_CHECKING:
    from aiogram import Bot

    from telegram_invite_bot.db import EngineRegistry
    from tests.e2e.handlers.conftest import WiredFactory


_USER_ID = 4242


def _capture(
    bot: Bot,
    monkeypatch: pytest.MonkeyPatch,
    sink: list[dict[str, Any]],
    *,
    username: str = "MyTestBot",
    get_me_raises: bool = False,
) -> None:
    """Patch the session so ``GetMe`` returns a controllable username
    (or raises) and ``SendMessage`` captures text. The handler calls
    ``bot.get_me()`` exactly once per /referral; tests don't currently
    count, but the patched path keeps the network surface deterministic.
    """

    async def fake_make_request(_bot: Any, method: Any, timeout: Any = None) -> Any:  # noqa: ASYNC109
        name = type(method).__name__
        if name == "GetMe":
            if get_me_raises:
                raise RuntimeError("simulated API outage")
            return TelegramUser(id=0, is_bot=True, first_name="bot", username=username)
        if name == "SendMessage":
            sink.append({"text": method.text})
            return Message(
                message_id=2,
                date=datetime(2024, 1, 1),
                chat=Chat(id=method.chat_id, type="private"),
                from_user=TelegramUser(id=0, is_bot=True, first_name="bot"),
                text=method.text,
            )
        raise AssertionError(f"unexpected Telegram call: {name}")

    monkeypatch.setattr(bot.session, "make_request", fake_make_request)


async def _seed_user(registry: EngineRegistry, *, lang: str = "ru") -> None:
    engine = registry.engine(DBName.USERS)
    async with AsyncSession(engine) as session:
        session.add(User(user_id=_USER_ID, first_name="Me", language_code=lang))
        await session.commit()


@pytest.mark.asyncio
async def test_referral_renders_link_and_percent(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc")),
        economy_config=EconomyConfig(REFERRAL_COMMISSION_PERCENT=15),
    )
    await _seed_user(registry, lang="ru")
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, username="MyTestBot")

    result = await dispatcher.feed_update(bot, make_message_update("/referral", user_id=_USER_ID))
    assert result is not UNHANDLED
    body = sent[-1]["text"]
    # Link uses the patched bot username and the caller's user_id.
    assert f"https://t.me/MyTestBot?start=ref_{_USER_ID}" in body
    # Commission percent from settings appears verbatim.
    assert "15%" in body
    # RU labels.
    assert "реферальная ссылка" in body.lower()


@pytest.mark.asyncio
async def test_referral_renders_english_for_en_user(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc")),
    )
    await _seed_user(registry, lang="en")
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, username="MyTestBot")
    # Telegram-supplied ``language_code=en`` on the incoming update is
    # what UserService.touch persists; the seeded row's language_code is
    # overwritten by the upsert-from-telegram path.
    await dispatcher.feed_update(
        bot, make_message_update("/referral", user_id=_USER_ID, language_code="en")
    )
    body = sent[-1]["text"]
    assert "Your referral link" in body
    assert "Share it with friends" in body


@pytest.mark.asyncio
async def test_referral_fallback_username_on_get_me_failure(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """API outage during /referral must not block the reply — link
    falls back to ``YourBot``. Tests the exact same fallback legacy
    uses so an existing card surviving the cutover doesn't change
    shape.
    """
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc")),
    )
    await _seed_user(registry)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, get_me_raises=True)
    await dispatcher.feed_update(bot, make_message_update("/referral", user_id=_USER_ID))
    body = sent[-1]["text"]
    assert "https://t.me/YourBot?start=ref_" in body


@pytest.mark.asyncio
@pytest.mark.parametrize("cmd", ["/referral", "/реферал", "/реферальная_ссылка"])
async def test_referral_aliases_route(
    make_wired: WiredFactory,
    monkeypatch: pytest.MonkeyPatch,
    cmd: str,
) -> None:
    bot, dispatcher, registry = await make_wired(
        schemas=[UsersBase],
        session_middleware=True,
        bot_config=BotConfig(BOT_TOKEN=SecretStr("1:abc")),
    )
    await _seed_user(registry)
    sent: list[dict[str, Any]] = []
    _capture(bot, monkeypatch, sent, username="X")
    result = await dispatcher.feed_update(bot, make_message_update(cmd, user_id=_USER_ID))
    assert result is not UNHANDLED
    assert sent, f"alias {cmd} did not produce a reply"
