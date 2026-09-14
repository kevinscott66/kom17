"""``/marriages`` + ``/relations`` group leaderboards — Stage 19.

Two read-only commands that render the chat's bond tables:

* ``/marriages`` (+ ``/браки``, ``/пары``) — every active marriage in
  the chat with category, level name, date, and "how long" duration.
  Legacy: bot.py:22982.
* ``/relations`` (+ ``/отношения_список``, ``/отны``) — every active
  relationship with relationship-level and date. Legacy: bot.py:23480.

Group-only by filter. ``ensure_user_access(require_group=True)`` is the
only access gate legacy applies for these commands — there's no
feature flag, no role check. We mirror that with a plain
``F.chat.type`` predicate; group-feature gating lands with the
bet-variant stage that also re-introduces the broader games-policy
surface area (see ``handlers/games.py`` Stage 18 docstring for why
we defer policy gates from read-only ports).

The write-side bond flow now lives in :mod:`~handlers.marriage`:
``/marry`` + ``/marry_accept`` / ``/marry_decline``, ``/divorce``,
``/breakup``, and ``/relationship`` propose + ``rel_accept_`` /
``rel_decline_`` callbacks (A-02). These two read-only leaderboards stay
here because they're pure SELECTs with no proposal state.

The rest of the bond surface is ported too — it just lives in other
modules, not here:

* ``/marriage`` (single-user status card, with activity + history inline
  buttons) — :mod:`~handlers.marriage`. Legacy: bot.py:22661.
* The relationship *activity* subsystem —
  :mod:`~handlers.couple_activities` (``cpl_menu`` / ``cpl_hist``, legacy
  ``rel_activity_menu_*`` / ``rel_history_*``, bot.py:23275) and
  :mod:`~handlers.rp` (the RP verbs).
* ``/marry_top_on`` / ``/marry_top_off``, ``/marry_extend``,
  ``/marry_auto_divorce``, ``/marry_other`` — :mod:`~handlers.marriage`
  (L-03..L-07). Legacy: bot.py:22726, :22744, :22762, :22794, :22830.

HTML rendering: ``first_name`` joined from ``users.users`` is
user-supplied. ``html.escape`` is mandatory — a name like
``<b>Free coins</b>`` would otherwise render bold on every viewer's
screen. Same risk class as the shop catalog and the AI handler.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.handlers.group_only import handle_group_only
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.session import SessionMiddleware
from telegram_invite_bot.utils.bonds import (
    format_db_date,
    format_duration,
    marriage_category,
    marriage_level_name,
    marriage_xp_to_level,
    relationship_xp_to_level,
)
from telegram_invite_bot.utils.names import mention

log = logger.bind(component="handlers.relations")

# Ceiling on both boards. Neither repo query had a LIMIT and a marriage
# row costs ~205 visible characters at Telegram's 64-character name
# limit (two names, category, level, date, duration), so a chat with
# twenty couples walked the reply past the 4096 ceiling and Telegram
# rejected it outright — the user saw nothing at all, not even a short
# board. Truncating rather than paginating: both queries order by
# experience DESC, so what survives is the top of the leaderboard, which
# is what the command is for. Same number as
# :data:`~handlers.marriage._REL_LIST_MAX`, for the same reason.
_BOARD_MAX: int = 15

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aiogram.types import Message

    from telegram_invite_bot.core.entities.bonds import (
        MarriagePair,
        RelationshipPair,
    )
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.repositories.bonds_repo import (
        MarriagesRepo,
        RelationshipsRepo,
    )


def _format_marriages(pairs: list[MarriagePair], lang: str, hidden: int = 0) -> str:
    """Render the marriages leaderboard.

    Line shape mirrors legacy at bot.py:23004 — index, both mentions,
    category, level name, date, duration. HTML semantics replace the
    legacy Markdown italics so the new pipeline can run with
    ``parse_mode=HTML`` end-to-end.

    ``hidden`` is how many pairs the cap dropped; a non-zero value gets
    its own closing line so a truncated board doesn't read as the whole
    chat's roster.
    """
    lines = [t("h_marriages_header", lang) + "\n"]
    for idx, pair in enumerate(pairs, start=1):
        mention1 = mention(pair.user1_id, pair.user1_name, lang)
        mention2 = mention(pair.user2_id, pair.user2_name, lang)
        level = marriage_xp_to_level(pair.experience)
        level_name = marriage_level_name(level, lang)
        category = marriage_category(pair.created_at, pair.extra_days, lang=lang)
        date_str = format_db_date(pair.created_at)
        duration = format_duration(pair.created_at, lang=lang)
        lines.append(
            f"{idx}. {mention1} 💒 {mention2} — {category}, {level_name} ({date_str}, {duration})"
        )
    if hidden > 0:
        lines.append(t("h_rel_status_more", lang, count=hidden))
    return "\n".join(lines)


def _format_relations(pairs: list[RelationshipPair], lang: str, hidden: int = 0) -> str:
    lines = [t("h_relations_header", lang) + "\n"]
    for idx, pair in enumerate(pairs, start=1):
        mention1 = mention(pair.user1_id, pair.user1_name, lang)
        mention2 = mention(pair.user2_id, pair.user2_name, lang)
        level = relationship_xp_to_level(pair.experience)
        date_str = format_db_date(pair.created_at)
        level_label = t("h_relations_level_abbr", lang)
        lines.append(f"{idx}. {mention1} 💕 {mention2} — {level_label} {level} ({date_str})")
    if hidden > 0:
        lines.append(t("h_rel_status_more", lang, count=hidden))
    return "\n".join(lines)


async def _hidden_count(shown: int, total_fn: Callable[[], Awaitable[int]]) -> int:
    """How many rows the cap dropped, without a needless COUNT.

    A board that came back short of the cap is provably complete, so the
    extra query only runs for the chats that can actually overflow.
    """
    if shown < _BOARD_MAX:
        return 0
    return max(0, await total_fn() - shown)


async def handle_marriages(message: Message, marriages_repo: MarriagesRepo, lang: str) -> None:
    pairs = await marriages_repo.list_active(message.chat.id, limit=_BOARD_MAX)
    if not pairs:
        await message.reply(t("h_marriages_empty", lang))
        return
    hidden = await _hidden_count(len(pairs), lambda: marriages_repo.count_active(message.chat.id))
    await message.reply(_format_marriages(pairs, lang, hidden))
    log.bind(
        chat_id=message.chat.id,
        rows=len(pairs),
        hidden=hidden,
    ).info("/marriages rendered")


async def handle_relations(
    message: Message, relationships_repo: RelationshipsRepo, lang: str
) -> None:
    pairs = await relationships_repo.list_active(message.chat.id, limit=_BOARD_MAX)
    if not pairs:
        await message.reply(t("h_relations_empty", lang))
        return
    hidden = await _hidden_count(
        len(pairs), lambda: relationships_repo.count_active(message.chat.id)
    )
    await message.reply(_format_relations(pairs, lang, hidden))
    log.bind(
        chat_id=message.chat.id,
        rows=len(pairs),
        hidden=hidden,
    ).info("/relations rendered")


def build_router(registry: EngineRegistry) -> Router:
    """Factory — fresh ``Router`` + middleware per call so tests can re-wire.

    Attaches its own :class:`SessionMiddleware` instance: the bond
    repos read from ``users.db``, same as :mod:`~handlers.start`, but
    we don't want to depend on which routers have already mounted
    their own session middleware — each handler that touches the
    users DB pulls a fresh per-update transaction here.

    Group-only: legacy refuses both commands outside groups
    (``ensure_user_access(require_group=True)``). We pin that at the
    filter level — and, since #122, pair each with a private-chat twin
    that delivers the refusal. The original note here said private use
    "falls through to legacy, where it'll get the same «только в
    группе»"; legacy no longer runs, so the fallthrough was a silent
    drop and the promised refusal was never spoken.
    """
    router = Router(name="relations")
    router.message.middleware(SessionMiddleware(registry))
    router.message.register(
        handle_marriages,
        Command(
            "marriages",
            "браки",
            "пары",
            "couples",
            ignore_case=True,
        ),
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    )
    router.message.register(
        handle_group_only,
        Command(
            "marriages",
            "браки",
            "пары",
            "couples",
            ignore_case=True,
        ),
        F.chat.type == ChatType.PRIVATE,
    )
    router.message.register(
        handle_relations,
        Command(
            "relations",
            "rels",
            "отношения_список",
            "отны",
            ignore_case=True,
        ),
        F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    )
    router.message.register(
        handle_group_only,
        Command(
            "relations",
            "rels",
            "отношения_список",
            "отны",
            ignore_case=True,
        ),
        F.chat.type == ChatType.PRIVATE,
    )
    return router
