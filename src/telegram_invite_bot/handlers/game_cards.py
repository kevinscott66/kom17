"""Shared rendering + posting for the game result cards.

Every capped game (`/roulette`, `/roll <bet>`, `/flip <bet>`) closes its
result card the same way, so the blocks live here instead of being
copy-pasted per handler:

* :func:`render_achievements` — the "🏆 Новые достижения!" list (A-12).
  Was duplicated verbatim in ``handlers.roulette`` and ``handlers.games``.
* :func:`render_allowance` — how many plays the anti-abuse windows still
  allow (RR-3 #34).

Both return ``""`` when there is nothing to say, and both are pure: they
take already-computed values and produce text, no I/O.

:func:`post_game_card` is the one impure member — it sends the finished
card and, when the operator opted into the TTL sweep, schedules its
removal (RR-3 #33).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.config.settings import GamesConfig, get_settings
from telegram_invite_bot.core.achievements import icon as _ach_icon
from telegram_invite_bot.core.achievements import name as _ach_name
from telegram_invite_bot.core.chat_types import GROUP_TYPE_NAMES
from telegram_invite_bot.i18n import t
from telegram_invite_bot.services.game_limit_service import GameAbuse
from telegram_invite_bot.services.roulette_service import (
    COOLDOWN_SEC,
    MAX_PER_DAY,
    MAX_PER_HOUR,
)

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.services.game_limit_service import GameAbuseCheck

log = logger.bind(component="handlers.game_cards")

# The cooldown is a round 180s today; rendering it in minutes keeps the
# line readable. ``max(1, …)`` so a future sub-minute cooldown never
# advertises "next play in 0 min".
_COOLDOWN_MIN = max(1, round(COOLDOWN_SEC / 60))


def render_achievements(lang: str, awarded: list[str]) -> str:
    """The "🏆 Новые достижения!" block, or ``""`` when none (A-12).

    Names/icons come from the trusted ``core.achievements`` table (no
    user-controlled text), so nothing needs escaping here.
    """
    if not awarded:
        return ""
    lines = ["", t("h_roulette_new_achievements", lang)]
    lines += [f"• {_ach_icon(aid)} {_ach_name(aid, lang)}" for aid in awarded]
    return "\n".join(lines)


def render_abuse_refusal(lang: str, check: GameAbuseCheck) -> str:
    """The localised refusal for a play the caps blocked (L-25).

    One renderer for all three caps so every game surface refuses in
    the same words. The ``h_roulette_*`` keys are reused verbatim
    because the legacy copy was already game-agnostic ("Слишком
    часто…", "Лимит игр в час…"); a per-game key family would be
    three times the strings for identical sentences.

    Cooldown is tested first, matching the order
    :meth:`GameLimitService.check` evaluates the caps in — a cooldown
    block short-circuits before the window counts even run, so it is
    the only reason that can arrive without them.
    """
    if check.reason is GameAbuse.COOLDOWN:
        return t("h_roulette_cooldown", lang, wait_sec=check.wait_sec)
    if check.reason is GameAbuse.MAX_PER_HOUR:
        return t("h_roulette_max_per_hour", lang, max_per_hour=MAX_PER_HOUR)
    return t("h_roulette_max_per_day", lang, max_per_day=MAX_PER_DAY)


def render_allowance(lang: str, check: GameAbuseCheck) -> str:
    """The "how many plays you have left" footer (RR-3 #34).

    Legacy only ever mentioned the caps when one of them BLOCKED you
    (bot.py:21104) — the number a player actually wants is the one left
    *before* that happens, on the card they are already reading.

    The counts come from the PRE-play ``check``, so the play that just
    settled is subtracted here as well: the caller stamps it only after
    the check has run. Whichever window runs out first gets its own
    honest line — advertising "next play in 3 min" when the hourly cap
    is spent would be a lie the cooldown cannot deliver on.

    Returns ``""`` when the counts are unavailable (a cooldown block
    short-circuits before them), which also keeps every non-success card
    free of a footer it has no business carrying.
    """
    if check.plays_in_hour is None or check.plays_in_day is None:
        return ""
    left_hour = max(MAX_PER_HOUR - check.plays_in_hour - 1, 0)
    left_day = max(MAX_PER_DAY - check.plays_in_day - 1, 0)
    if left_day <= 0:
        return "\n\n" + t("h_game_allowance_day_done", lang)
    if left_hour <= 0:
        return "\n\n" + t("h_game_allowance_hour_done", lang)
    return "\n\n" + t(
        "h_game_allowance",
        lang,
        left_hour=left_hour,
        left_day=left_day,
        cooldown_min=_COOLDOWN_MIN,
    )


# Strong refs to the in-flight deletion tasks. ``asyncio`` only holds a
# weak reference to a running task, so without this the GC may collect a
# sleeping sweep mid-flight and the card would survive its TTL.
_pending_deletions: set[asyncio.Task[None]] = set()


async def _delete_after(message: Message, delay: float) -> None:
    """Sleep ``delay`` seconds, then drop ``message``. Best-effort."""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception as exc:  # noqa: BLE001 — hygiene, never a user-facing error
        # Deletion fails for ordinary reasons (no delete rights, the
        # message is already gone, >48h old). None of them are worth
        # surfacing, and all of them are worth seeing in a debug log.
        log.debug("game card sweep failed: {exc!r}", exc=exc)


def _sweep_config() -> GamesConfig:
    """The games knobs, or their defaults when settings are unavailable.

    ``get_settings`` validates the WHOLE app config (BOT_TOKEN included),
    so reading it from inside a handler turns any config problem into a
    crashed game — a card that was already sent, a wallet that was already
    settled, and a user who sees an error. The sweep is cosmetic; it opts
    itself out rather than take the game down with it.
    """
    try:
        settings: Settings = get_settings()
    except Exception as exc:  # noqa: BLE001 — cosmetics never break a game
        log.debug("games config unavailable; sweep disabled: {exc!r}", exc=exc)
        return GamesConfig()
    return settings.games


def schedule_card_sweep(card: Message, *, chat_type: str) -> None:
    """Queue ``card`` for TTL removal when the operator opted in (RR-3 #33).

    No-op unless ``GAMES_AUTO_DELETE`` is set, and no-op outside groups —
    see :class:`~telegram_invite_bot.config.settings.GamesConfig` for why
    the default inverts legacy's.

    Fire-and-forget by design: the sweep must never delay the reply, and
    its failure must never turn a settled game into an error.
    """
    games = _sweep_config()
    if not games.auto_delete or chat_type not in GROUP_TYPE_NAMES:
        return
    task = asyncio.create_task(_delete_after(card, games.ttl_seconds))
    _pending_deletions.add(task)
    task.add_done_callback(_pending_deletions.discard)


async def post_game_card(message: Message, body: str) -> Message:
    """Reply with a finished game card, sweep-scheduled per RR-3 #33.

    ``message`` is the user's command; the card goes into the same chat,
    whose type decides whether the sweep applies.
    """
    sent = await message.answer(body)
    schedule_card_sweep(sent, chat_type=message.chat.type)
    return sent
