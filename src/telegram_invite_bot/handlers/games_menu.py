"""``/games`` (+``/игры``, ``/kom_games``) — games menu card (L-15).

Port of legacy ``cmd_games`` (``bot.py:18002-18035``, registered as
``commands=['games', 'игры', 'kom_games']``): a discovery card that
lists the available game commands with one-line descriptions.

Accepted-port note (the WHY of the shape): the backlog audit verdict
(``docs/LOST_FEATURES_BACKLOG.md:64`` — "L-15 | games menu (command) |
Interactive games command menu (folded into /shop keyboard) | half")
records that legacy folded the interactive menu into a keyboard; the
accepted port for the slash-command surface is a plain text card —
no inline keyboard, no auto-delete. The legacy ``main_menu`` back
button and the group-only gating (legacy showed "only in group" in
private) are deliberately NOT carried over: the new pipeline answers
the card in private AND group, since the card is pure discovery text
and every listed command renders its own usage/chat-gate errors.

Listed surface (all already live in the new pipeline):

* ``/dice`` / ``/roll`` / ``/flip`` — ``handlers/games.py``
* ``/cpc`` — ``handlers/rps.py``
* ``/duel`` — ``handlers/duel.py``
* ``/roulette`` — ``handlers/roulette.py``

Static i18n text only — no DB, no services, no FSM. ``lang`` is the
effective bot language injected by the root ``LanguageMiddleware``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.i18n import t

if TYPE_CHECKING:
    from aiogram.types import Message

log = logger.bind(component="handlers.games_menu")


async def handle_games_menu(message: Message, lang: str) -> None:
    """Render the static games-discovery card (private + group)."""
    await message.reply(t("h_games_menu_card", lang))
    log.bind(
        uid=message.from_user.id if message.from_user else None,
        chat=message.chat.id,
    ).info("/games menu rendered")


def build_router() -> Router:
    """Factory — fresh ``Router`` per call so tests can re-wire dispatchers.

    No middleware: the handler is a static i18n leaf (same posture as
    ``/jokes`` / ``/faq2``). No chat-type filter — legacy answered the
    command in any chat, and the card is harmless discovery text.
    """
    router = Router(name="games_menu")
    router.message.register(
        handle_games_menu,
        Command("games", "игры", "kom_games", ignore_case=True),
    )
    return router
