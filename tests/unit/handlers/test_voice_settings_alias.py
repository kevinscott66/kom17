"""Wiring test for the ``/voice_settings`` RU alias (Cluster T1, L-09).

Legacy registered BOTH tokens — ``command_aliases.py:596-598``,
``aliases: ['voice_settings', 'voice_settings_ru']`` — while the new
surface initially carried only the EN one. Pin the pair so the alias
can't silently drop again.
"""

from __future__ import annotations

from types import SimpleNamespace

from aiogram.filters import Command

from telegram_invite_bot.handlers.chat_scope import scoped_worker
from telegram_invite_bot.handlers.vip import build_router, handle_voice_settings


def test_voice_settings_has_legacy_ru_alias() -> None:
    # EconomyMiddleware only stores the registry at construction time
    # (sessions open lazily per update), so a dummy is enough here.
    #
    # ``scoped_worker``: since #123 ``build_router`` returns the wrapper
    # router that pairs the module with its chat-scope refusal twin, and
    # the wrapper itself registers nothing — the registrations this test
    # pins live on the module's own router underneath it.
    # #1957: the factory now binds the configured display zone for the
    # ``/vip`` card; this test is about command tokens, so a stub with
    # just the attribute the factory reads is enough.
    router = scoped_worker(
        build_router(object(), SimpleNamespace(timezone="UTC"))  # type: ignore[arg-type]
    )
    tokens: set[str] = set()
    for handler in router.message.handlers:
        if handler.callback is not handle_voice_settings:
            continue
        for filter_obj in handler.filters or []:
            if isinstance(filter_obj.callback, Command):
                tokens.update(c for c in filter_obj.callback.commands if isinstance(c, str))
    assert tokens == {"voice_settings", "voice_settings_ru"}
