"""Unit tests for the L-70 ``private`` delivery target (#1538).

``_route_to_target`` DMs the speaker when a group is configured with
``target="private"``. The speaker comes from ``message.from_user``,
which aiogram types as optional and which really is ``None`` for a
channel post forwarded into the linked discussion group.

The guard for that case used to be a bare ``assert`` sitting INSIDE the
``try`` that catches "DM blocked". Under ``python -O`` the assert is
compiled away, ``speaker.id`` raises ``AttributeError``, and the same
``except Exception`` swallows it — the right message reached the user,
but by accident and only because the fallback happened to be broad.
These tests pin the explicit guard, and the source pin below keeps an
``assert`` from creeping back into the delivery path.
"""

from __future__ import annotations

import inspect
from typing import Any, cast

from aiogram import Bot
from aiogram.types import Message

from telegram_invite_bot.handlers.voice_transcribe import _route_to_target
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.voice_settings_repo import VoiceSettings

_PRIVATE = VoiceSettings(
    enabled=True,
    target="private",
    language="ru",
    log_chat_id=None,
    auto_delete=False,
    only_admins=False,
)


class _Chat:
    def __init__(self, chat_id: int) -> None:
        self.id = chat_id
        self.type = "supergroup"


class _User:
    def __init__(self, user_id: int) -> None:
        self.id = user_id


class _Message:
    """Minimal stand-in for the aiogram ``Message`` fields used here."""

    def __init__(self, from_user: _User | None) -> None:
        self.from_user = from_user
        self.chat = _Chat(-100123)
        self.replies: list[str] = []

    async def reply(self, text: str, **_kwargs: Any) -> None:
        self.replies.append(text)


class _Bot:
    """Records DMs; ``fail`` makes every send raise, as a blocked DM does."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self._fail = fail

    async def send_message(self, chat_id: int, text: str, **_kwargs: Any) -> None:
        if self._fail:
            raise RuntimeError("Forbidden: bot can't initiate conversation")
        self.sent.append((chat_id, text))


async def _route(bot: _Bot, message: _Message, body: str) -> None:
    """Call the handler with the stubs cast to the aiogram types.

    ``_route_to_target`` touches only the few attributes the stubs
    above define; constructing real ``Bot``/``Message`` objects would
    drag in a token and a full update payload for no added coverage.
    """
    await _route_to_target(cast("Bot", bot), cast("Message", message), _PRIVATE, body, "ru")


async def test_private_target_dms_the_speaker() -> None:
    bot = _Bot()
    message = _Message(_User(777))
    await _route(bot, message, "<blockquote>hi</blockquote>")
    assert [chat_id for chat_id, _ in bot.sent] == [777]
    assert bot.sent[0][1].endswith("<blockquote>hi</blockquote>")
    assert message.replies == []


async def test_private_target_without_a_sender_falls_back_to_the_group() -> None:
    """#1538: no ``from_user`` must not reach ``speaker.id`` at all."""
    bot = _Bot()
    message = _Message(None)
    await _route(bot, message, "body")
    assert bot.sent == []
    assert message.replies == [t("h_vtr_private_blocked", "ru")]


async def test_private_target_falls_back_when_the_dm_is_blocked() -> None:
    bot = _Bot(fail=True)
    message = _Message(_User(777))
    await _route(bot, message, "body")
    assert bot.sent == []
    assert message.replies == [t("h_vtr_private_blocked", "ru")]


def test_delivery_path_carries_no_assert() -> None:
    """``python -O`` erases asserts; this path must not depend on one.

    Production runs the service without ``-O`` today (the systemd unit
    passes no flag), so the old code behaved correctly by luck. Pinning
    the source keeps the guard explicit if that ever changes.
    """
    source = inspect.getsource(_route_to_target)
    assert "assert " not in source
