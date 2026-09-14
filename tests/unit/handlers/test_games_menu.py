"""Unit tests for the ``/games`` menu card (Cluster T1, backlog L-15).

Pins the accepted-port contract (``docs/LOST_FEATURES_BACKLOG.md:64``):
a static i18n text card answered in private AND group (legacy
``cmd_games``, ``bot.py:18002``, group-gated the body; the port does
not), registered under the legacy alias set ``games`` / ``игры`` /
``kom_games``.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiogram.filters import Command

from telegram_invite_bot.handlers import games_menu as games_menu_mod
from telegram_invite_bot.handlers.games_menu import build_router, handle_games_menu


class FakeMessage:
    def __init__(self, *, chat_type: str = "private") -> None:
        self.from_user = SimpleNamespace(id=100)
        self.chat = SimpleNamespace(id=100 if chat_type == "private" else -100, type=chat_type)
        self.replies: list[str] = []

    async def reply(self, text: str) -> None:
        self.replies.append(text)


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["private", "group", "supergroup"])
async def test_renders_card_in_private_and_group(
    monkeypatch: pytest.MonkeyPatch, chat_type: str
) -> None:
    calls: list[tuple[str, str]] = []

    def fake_t(key: str, lang: str, **kwargs: Any) -> str:
        calls.append((key, lang))
        return key

    monkeypatch.setattr(games_menu_mod, "t", fake_t)
    msg: Any = FakeMessage(chat_type=chat_type)
    await handle_games_menu(msg, "ru")
    assert msg.replies == ["h_games_menu_card"]
    assert calls == [("h_games_menu_card", "ru")]


def test_router_registers_legacy_alias_set() -> None:
    router = build_router()
    tokens: set[str] = set()
    for handler in router.message.handlers:
        for filter_obj in handler.filters or []:
            if isinstance(filter_obj.callback, Command):
                tokens.update(c for c in filter_obj.callback.commands if isinstance(c, str))
    # Legacy registration: commands=['games', 'игры', 'kom_games']
    # (``bot.py:18002``).
    assert tokens == {"games", "игры", "kom_games"}
