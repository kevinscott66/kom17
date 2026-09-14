"""Economy handlers — Stage 7 of the strangler migration.

Stage 7 ships only ``/balance`` (and its aliases) — a pure read that
also doubles as the first command to wire a second SQLite engine
(``economy.db``) through its own per-router middleware.

Transactional commands (``/daily``, ``/gift``, ``/buy``) land in
Stage 8 after we've matched legacy's exact reward curve and locking
semantics. ``/shop`` keeps living in legacy until its inline-keyboard
FSM has a proper aiogram equivalent.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.utils.aiogram import require_from_user
from telegram_invite_bot.utils.numbers import balance_tier_emoji, format_number
from telegram_invite_bot.utils.plural import plural
from telegram_invite_bot.utils.time import db_now

log = logger.bind(component="handlers.economy")

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.core.entities.wallet import Wallet
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.game_stats_repo import GameStatsRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo


def _format_balance(wallet: Wallet, lang: str | None = None) -> str:
    """HTML card via i18n (``h_balance_card``). Pure ints — nothing to escape.

    Numeric values run through :func:`format_number` for the thin-space
    thousands separator (``1 234 567`` instead of ``1234567``) — matches
    the legacy ``/balance`` rendering at ``bot.py:17897``. The header
    emoji adapts to balance magnitude via :func:`balance_tier_emoji`
    (💎/💰/🪙/👛) so power-users can see their tier at a glance, which
    matches the legacy ``format_balance_emoji`` ladder used in the
    profile card.

    The previous hardcoded "Всего заработано / Всего потрачено" labels
    were misleading: ``total_earned`` / ``total_spent`` are bumped ONLY
    by flows that pass :meth:`EconomyRepo.credit` / ``debit`` (daily,
    message rewards, game payouts/bets, transfers, shop, fines …) while
    several legacy write paths (e.g. legacy /daily's direct UPDATE,
    admin ``set_balance``) skipped them — live data shows
    earned ≪ balance. The new copy labels them as *tracked* operations
    ("учтённые операции") so the partial nature is honest.

    ``lang`` is optional so callers without an injected language
    (``main_menu``'s balance tap passes only the wallet) degrade to the
    wallet's stored language; the ``/balance`` handler passes the
    middleware-injected language.
    """
    effective = lang or wallet.language or "ru"
    tier = balance_tier_emoji(wallet.balance)
    return t(
        "h_balance_card",
        effective,
        tier=tier,
        balance=format_number(wallet.balance),
        earned=format_number(wallet.total_earned),
        spent=format_number(wallet.total_spent),
        streak=wallet.daily_streak,
    )


# RR-2/RR-3 #10/#25: the games whose per-game P&L the dashboard breaks out,
# paired with their localised display label key. Order = how legacy listed
# them. A game the user never played is skipped (plays == 0).
_BALANCE_GAMES: tuple[tuple[str, str, str], ...] = (
    ("roulette", "Рулетка", "Roulette"),
    ("dice", "Кости", "Dice"),
    ("flip", "Монетка", "Coin flip"),
    ("duel", "Дуэль", "Duel"),
    ("rps", "КНБ", "RPS"),
    ("pvp_coin", "PvP-монета", "PvP coin"),
    ("pvp_dice", "PvP-кости", "PvP dice"),
)


async def _format_dashboard_extras(
    wallet: Wallet,
    game_stats_repo: GameStatsRepo,
    transactions_repo: TransactionsRepo,
    lang: str,
) -> str:
    """RR-2/RR-3 #10/#25/#26/#27: the richer /balance sections legacy had —
    overall game record, per-game P&L breakdown, and the weekly cashflow —
    appended below the base wallet card."""
    parts: list[str] = []

    # Overall game record (#26).
    played = wallet.games_played
    won = wallet.games_won
    rate = round(won * 100 / played) if played else 0
    parts.append(t("h_balance_games", lang, played=played, won=won, rate=rate))

    # Per-game profit breakdown (#25) — only games actually played.
    rows: list[str] = []
    for key, ru_name, en_name in _BALANCE_GAMES:
        stats = await game_stats_repo.stats_for(wallet.user_id, game=key)
        if stats.total <= 0:
            continue
        label = ru_name if lang == "ru" else en_name
        rows.append(
            t(
                "h_balance_game_row",
                lang,
                game=label,
                plays=stats.total,
                noun=plural(stats.total, "h_plural_games", lang),
                profit=format_number(stats.total_profit),
            )
        )
    if rows:
        parts.append(t("h_balance_games_header", lang))
        parts.extend(rows)

    # Weekly cashflow (#27).
    since = db_now() - timedelta(days=7)
    win = await transactions_repo.window_stats(wallet.user_id, since=since)
    parts.append(
        t(
            "h_balance_week",
            lang,
            received=format_number(win.received),
            sent=format_number(win.sent),
            count=format_number(win.tx_count),
        )
    )
    return "\n".join(parts)


async def handle_balance(
    message: Message,
    economy_repo: EconomyRepo,
    game_stats_repo: GameStatsRepo,
    transactions_repo: TransactionsRepo,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    tg_user = require_from_user(message)
    wallet = await economy_repo.get_or_create(tg_user.id)
    # #1862: on a user's first ever ``/balance`` that call INSERTs the
    # wallet, and ``db/engines.py`` promotes the transaction to
    # ``BEGIN IMMEDIATE`` on that write — a lock then held across the
    # dashboard reads below and the send at the end. Everything after
    # this line is a SELECT or pure formatting, so end the write
    # transaction right here: the seeded wallet (welcome credit
    # included) is bookkeeping that must stand whether or not the card
    # reaches the user. Without it a blocked bot rolls the row back and
    # the next ``/balance`` seeds it again. Repeat calls find the row
    # present, take ``get_or_create``'s read-only fast path, and this
    # commits nothing.
    if checkpoint is not None:
        await checkpoint()
    card = _format_balance(wallet, lang)
    # The base card is shared with the main-menu balance tap; the richer
    # dashboard (game record + per-game P&L + weekly cashflow) is exclusive
    # to the /balance command, degrading to just the card on any read error.
    try:
        card += "\n" + await _format_dashboard_extras(
            wallet, game_stats_repo, transactions_repo, lang
        )
    except Exception:  # noqa: BLE001 — dashboard is best-effort enrichment
        log.opt(exception=True).warning("/balance dashboard extras failed")
    await message.answer(card)
    log.bind(
        uid=wallet.user_id,
        balance=wallet.balance,
    ).info("/balance rendered")


def build_router(registry: EngineRegistry) -> Router:
    """Factory — fresh ``Router`` + middleware per call so tests can re-wire.

    The middleware is attached at the router level (not the
    dispatcher) so non-economy handlers don't pay for an extra
    ``economy.db`` session per update.
    """
    router = Router(name="economy")
    router.message.middleware(EconomyMiddleware(registry))
    router.message.register(
        handle_balance,
        Command(
            "balance",
            "bal",
            "баланс",
            # The ``kom_`` twins exist so a chat running several of these
            # bots can address this one unambiguously. Legacy registered
            # both (bot.py); the catalog carried them forward but the
            # port did not, so the site advertised two names that only
            # ever answered with silence.
            "kom_balance",
            "kom_bal",
            ignore_case=True,
            magic=F.args.is_(None),
        ),
        F.from_user,
    )
    return router
