"""``/achievements`` handler — A-07 of the strangler migration.

A READ-ONLY achievements card: the user always views their OWN earned
achievements (no ``/achievements @other`` lookup, no args) plus a footer
of economy stats (balance / games played / wins). Works in private AND
group chats with no admin gate — same access posture as legacy
``cmd_achievements``.

What is intentionally NOT ported (legacy side effects that have no place
in a viewer): the awarding logic, the ``notified`` flip, the auto-delete
and the message-deletion housekeeping. The card only reads.

Rendering: the achievement *definitions* (id → name / icon) live in
:mod:`telegram_invite_bot.core.achievements`, imported below as
``_name`` / ``_icon`` / ``_desc`` / ``_TOTAL`` — the legacy DB
``achievements`` table is usually empty and the code table is the
source of truth. Only EARNED achievements are listed (locked ones are
not rendered, just counted in the ``/14`` denominator). Legacy used
Markdown; this codebase sends ``parse_mode=HTML`` so the card renders
with ``<b>…</b>``. The names are looked up in that trusted table, and
an id missing from it falls back to the raw DB string
(``core/achievements.py:126-131``) — which is why every id production
can actually hold is defined there (#473).
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.core.achievements import TOTAL as _TOTAL
from telegram_invite_bot.core.achievements import description as _desc
from telegram_invite_bot.core.achievements import icon as _icon
from telegram_invite_bot.core.achievements import name as _name
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.utils.aiogram import require_from_user
from telegram_invite_bot.utils.numbers import format_number

log = logger.bind(component="handlers.achievements")

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.achievements_repo import (
        AchievementsRepo,
        EarnedAchievement,
    )
    from telegram_invite_bot.services.user_service import UserService


# The 14 achievement definitions + their unlock thresholds live in
# ``core.achievements`` (single source of truth, shared with the A-12
# awarding path). The card imports the localized ``name`` / ``icon`` /
# ``TOTAL`` from there (aliased ``_name`` / ``_icon`` / ``_TOTAL``).
#
# Legacy caps the rendered list at 10 rows (earned[:10]); the denominator
# still counts every earned row.
_MAX_ROWS = 10


def render_body(
    *,
    lang: str,
    earned: list[EarnedAchievement],
    balance: int,
    games_played: int,
    games_won: int,
) -> str:
    """Render the HTML achievements card body.

    Names come from the trusted ``core.achievements.DEFINITIONS``
    table — except when they do not. :func:`core.achievements.name`
    falls back to the raw ``achievement_id`` for an id it has never
    heard of (``core/achievements.py:126-131``), and that string comes
    off the legacy ``achievements`` table, not out of this codebase, so
    it is escaped below. Nothing else here needs it: the remaining
    interpolations are integers and ISO dates, and ``icon`` answers an
    unknown id with 🏆 rather than with the id itself.

    Shared with the ``/profile`` 🏆 panel (#2) so the two surfaces
    never drift.
    """
    lines = [
        t("h_achievements_title", lang),
        "",
        t("h_achievements_progress", lang, earned=len(earned), total=_TOTAL),
        "",
    ]
    if earned:
        lines.append(t("h_achievements_earned_header", lang))
        for row in earned[:_MAX_ROWS]:
            # Escaped because this is the one value on the card that
            # can be raw DB text — see the docstring.
            title = html.escape(_name(row.achievement_id, lang))
            lines.append(f"• {_icon(row.achievement_id)} <b>{title}</b>")
            # RR-1 #8: one-line description under each earned achievement.
            desc = _desc(row.achievement_id, lang)
            if desc:
                lines.append(f"  <i>{desc}</i>")
            if row.earned_date is not None:
                lines.append(f"  🗓️ {row.earned_date.strftime('%Y-%m-%d')}")
    else:
        lines.append(t("h_achievements_none", lang))
        lines.append(t("h_achievements_nudge", lang))
    lines.append("")
    lines.append(t("h_achievements_balance", lang, value=format_number(balance)))
    lines.append(t("h_achievements_games", lang, value=format_number(games_played)))
    lines.append(t("h_achievements_wins", lang, value=format_number(games_won)))
    return "\n".join(lines)


def build_router(registry: EngineRegistry) -> Router:
    """Factory — fresh ``Router`` + middleware per call so tests can re-wire.

    Mounts its own :class:`EconomyMiddleware` (for ``achievements_repo``)
    at the router level so non-achievement handlers don't pay for an
    extra ``economy.db`` session per update. ``user_service`` comes from
    the dispatcher-level :class:`SessionMiddleware` (same as ``/help`` /
    ``/profile``) and supplies the caller's language.
    """

    async def handle_achievements(
        message: Message,
        user_service: UserService,
        achievements_repo: AchievementsRepo,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        # ``touch`` bumps ``last_seen`` and resolves the language —
        # legacy ran the same update at the top of nearly every handler.
        # #1983: it used to say this was "exactly like /help", which had
        # stopped being true the moment /help took the checkpoint below
        # (#220) and this handler did not — the two card renders it
        # claimed to match then held ``users.db``'s writer slot across
        # two economy reads and a send. See :class:`db.session.Checkpoint`.
        user = await user_service.touch(require_from_user(message))
        if checkpoint is not None:
            await checkpoint()
        earned = await achievements_repo.earned_for_user(user.user_id)
        stats = await achievements_repo.stats_for_user(user.user_id)
        await message.answer(
            render_body(
                lang=user.language,
                earned=earned,
                balance=stats.balance,
                games_played=stats.games_played,
                games_won=stats.games_won,
            )
        )
        log.bind(uid=user.user_id, earned=len(earned)).info("/achievements rendered")

    router = Router(name="achievements")
    router.message.middleware(EconomyMiddleware(registry))
    # Chat-type-agnostic + no args: legacy answers in groups too, always
    # for the caller's own id. ``F.args.is_(None)`` keeps a future
    # ``/achievements @other`` (a lookup that hasn't migrated) falling
    # through rather than rendering the caller's own card under another
    # spelling — same posture as /balance and /profile.
    router.message.register(
        handle_achievements,
        Command(
            "achievements",
            "ach",
            ignore_case=True,
            magic=F.args.is_(None),
        ),
        F.from_user,
    )
    return router
