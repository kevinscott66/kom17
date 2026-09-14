"""``/duel_stats`` — caller's personal duel record.

Legacy ``/duel_stats`` (bot.py:21498) is a single SELECT-aggregate
over ``economy.games`` filtered to ``game='duel'`` for the calling
user, rendered as a stat card with the legacy depth: total duels,
wins, losses, win-rate %, net profit, average bet, best win and
worst loss. The aggregate query lives in
:class:`~telegram_invite_bot.repositories.game_stats_repo.GameStatsRepo`
(read-only — the write side of ``games`` belongs to ``EconomyRepo``).

Behaviour parity & deltas (cluster F2 / L-20 finish-the-port):

* Any chat type. Legacy gates on ``ensure_user_access`` (role) and a
  per-group ``games`` feature flag — neither modelled in the new
  pipeline yet, same gap ``/ping``/``/botcheck`` accept (see
  ``handlers/heartbeat.py``).
* Empty record short-circuits to a static line — legacy keeps the
  stat-card hidden when ``total_duels == 0`` to avoid showing seven
  zeros to a user who's never duelled. Same UX here.
* Win-rate uses integer math (``wins * 100 // total``) instead of
  legacy's float-then-``:.1f``. The card never claimed sub-percent
  precision; an integer percent reads cleaner in the monospace card.
* Localised via the root ``LanguageMiddleware`` — the handler renders
  through ``t(key, lang)`` with the injected effective language
  instead of the hardcoded-Russian card the first port shipped.
* HTML rendering (``<b>``, ``<code>``) instead of legacy's Markdown.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.game_stats_repo import GameStatsRepo
from telegram_invite_bot.utils.aiogram import require_from_user

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.repositories.game_stats_repo import GameStatsRow


log = logger.bind(component="handlers.duel_stats")


def _render(stats: GameStatsRow, lang: str) -> str:
    if stats.total == 0:
        return t("h_duel_stats_empty", lang)
    win_rate = (stats.wins * 100) // stats.total
    # Legacy renders ``max_loss`` as its absolute value (``abs(...)``)
    # next to a 💔 emoji that already conveys the sign — "lost 500 🪙"
    # reads better than "−500 🪙".
    return t(
        "h_duel_stats_card",
        lang,
        total=stats.total,
        wins=stats.wins,
        losses=stats.losses,
        win_rate=win_rate,
        total_profit=stats.total_profit,
        avg_bet=stats.avg_bet,
        max_win=stats.max_win,
        max_loss=abs(stats.max_loss),
    )


async def handle_duel_stats(message: Message, registry: EngineRegistry, lang: str) -> None:
    from_user = require_from_user(message)
    sessionmaker = registry.session(DBName.ECONOMY)
    async with sessionmaker() as session:
        stats = await GameStatsRepo(session).stats_for(from_user.id, game="duel")
    await message.answer(_render(stats, lang))
    log.bind(uid=from_user.id, total=stats.total).info("/duel_stats rendered")


def build_router(registry: EngineRegistry) -> Router:
    """Bare-form only (``magic`` filter) so a future ``/duel_stats @x``
    syntax for inspecting another user can be added without colliding
    with this self-stats route.
    """
    router = Router(name="duel_stats")

    async def _entry(message: Message, lang: str) -> None:
        await handle_duel_stats(message, registry, lang)

    router.message.register(
        _entry,
        Command("duel_stats", ignore_case=True, magic=F.args.is_(None)),
        F.from_user,
    )
    return router
