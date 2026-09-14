"""``/groupstats`` — current group's donation aggregate + top donators.

Legacy ``/groupstats`` (bot.py:24987) has two modes:

* In a group chat — render the current group's name, XP/total
  donations, rating position, and a top-10 donator list with an
  inline "donate" / "open rating" keyboard.
* In a private DM — open the cross-group rating leaderboard
  (``send_rating_page``).

This router owns the **group-chat branch**. The private-DM branch
(the paginated cross-group rating leaderboard) is owned by
``handlers.rating`` (A-04), which registers the same command
spellings — :data:`GROUPSTATS_COMMANDS` — filtered to PRIVATE so the
two routes don't collide. Before A-04 the private branch fell through
to a deleted legacy bridge and silently dead-ended.

Behaviour parity & deltas:

* Group-only at the router level (same posture as ``/donaters``,
  ``/rules``). Private calls are claimed by ``handlers.rating``
  instead — see :data:`GROUPSTATS_COMMANDS`.
* Two reads against ``economy.db``: the group aggregate row and
  the per-user top-10 from ``group_top_donators``. Plus one read
  against ``users.db`` for display names. Same shape as
  ``/donaters``, plus the group header.
* Missing aggregate row (group never received a donation) → static
  ``no_donates_in_group`` localised refusal. Legacy renders the
  same line via ``stats`` returning falsy.
* Inline keyboard (#7): a "🏆 rating" button reusing the real
  cross-group leaderboard (``RatingNav`` — the same callback the
  ``/rating_groups`` card ships, so tapping opens page 1 in place) and
  a "💝 boost" button that pops a hint about how a group climbs the
  ranking (a cut of every ``/shop`` purchase feeds ``group_xp`` — see
  ``treasury_repo``; there is no explicit donate command in the new
  pipeline, so the button informs rather than dead-links a half-flow).
* HTML rendering — legacy used Markdown; bot-wide
  ``parse_mode=HTML`` makes backticks render as literal text.
* ``html.escape`` on group name + display names — both are
  operator-set free text and would otherwise render as markup.
* NO ``group_xp`` fallback to ``total_donations``. Legacy's
  ``g.get('group_xp', g['total_donations'])`` (bot.py:25005) looks
  like a fallback but never takes one: the key is always present
  (bot.py:10839), and both keys are the same ``COALESCE(group_xp, 0)``
  expression anyway (bot.py:10812). Pre-XP groups were repaired by a
  migration (bot.py:5342), not by the renderer (#479).
"""

from __future__ import annotations

import html as _html
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger
from sqlalchemy import select

from telegram_invite_bot.db.models.economy import GroupDonationsAggregate, GroupTopDonator
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.rating import RatingNav
from telegram_invite_bot.utils.aiogram import require_from_user
from telegram_invite_bot.utils.html import html_user_mention

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, Message

    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.services.user_service import UserService


class GroupStatsBoost(CallbackData, prefix="gsboost"):
    """ "💝 Boost" tap on a ``/groupstats`` card — pops a hint on how a
    group climbs the rating (no payload needed)."""


log = logger.bind(component="handlers.groupstats")

_TOP_LIMIT = 10
_COIN = "🪙"

# Legacy ``cmd_groupstats`` was dual-mode under one command set: in a
# group it rendered the local stats card; in a private DM it opened the
# cross-group rating leaderboard. This router owns the group branch; the
# rating handler imports this tuple to own the private branch under the
# same spellings (filtered to PRIVATE) so neither route swallows the
# other and the full legacy alias surface stays in one place.
GROUPSTATS_COMMANDS = (
    "groupstats",
    "статистика_группы",
    "group_treasury",
    "казна_группы",
    "kom_groupstats",
)


async def _fetch_aggregate(
    registry: EngineRegistry, chat_id: int
) -> tuple[str | None, int, int, int | None] | None:
    """``SELECT group_name, total_donations, group_xp, rating_position
    FROM groups_donations WHERE group_id=:chat``.

    Returns ``None`` when the row doesn't exist — that's the signal
    for "group hasn't received a donation yet" used by the renderer.
    "Row exists with zero donations" is a different state, and not a
    rare one: ``donations_ensure_group`` (bot.py:10574-10594) upserts
    a ``total_donations = 0`` row on ANY call and /chatstats calls it
    on every render (bot.py:41049). Only ``None`` reaches the refusal
    in :func:`handle_groupstats`; a zero row renders the full card
    with xp 0, which is deliberate — the group exists, it just has
    nothing yet.
    """
    engine = registry.engine(DBName.ECONOMY)
    async with engine.connect() as conn:
        result = await conn.execute(
            select(
                GroupDonationsAggregate.group_name,
                GroupDonationsAggregate.total_donations,
                GroupDonationsAggregate.group_xp,
                GroupDonationsAggregate.rating_position,
            ).where(GroupDonationsAggregate.group_id == chat_id)
        )
        row = result.first()
        if row is None:
            return None
        group_name, total_donations, group_xp, rating_position = row
        return (
            group_name,
            int(total_donations or 0),
            int(group_xp or 0),
            None if rating_position is None else int(rating_position),
        )


async def _fetch_top(
    registry: EngineRegistry, chat_id: int, limit: int = _TOP_LIMIT
) -> list[tuple[int, int]]:
    """Top-N (user_id, total) for this group, by total donated DESC."""
    engine = registry.engine(DBName.ECONOMY)
    async with engine.connect() as conn:
        result = await conn.execute(
            select(GroupTopDonator.user_id, GroupTopDonator.total_donated)
            .where(GroupTopDonator.group_id == chat_id)
            # ``user_id`` breaks ties: without it two donators on the same
            # total can swap places between calls, and at the LIMIT
            # boundary a different SET of rows comes back — a donator
            # appears and disappears with nothing having changed. The
            # siblings already do this (rating.py, message_stats_repo).
            .order_by(GroupTopDonator.total_donated.desc(), GroupTopDonator.user_id.asc())
            .limit(limit)
        )
        return [(int(uid), int(total)) for uid, total in result.all()]


async def _fetch_names(registry: EngineRegistry, user_ids: list[int]) -> dict[int, str]:
    """Bulk first_name lookup across users.db. Empty input → empty
    dict (skips the round-trip).
    """
    if not user_ids:
        return {}
    engine = registry.engine(DBName.USERS)
    async with engine.connect() as conn:
        result = await conn.execute(
            select(User.user_id, User.first_name).where(User.user_id.in_(user_ids))
        )
        return {int(uid): (name or "") for uid, name in result.all()}


def _render(
    lang: str,
    *,
    group_name: str | None,
    group_xp: int,
    rating_position: int | None,
    top: list[tuple[int, int]],
    names: dict[int, str],
) -> str:
    title = t("group_stats_title", lang)
    xp_label = t("group_xp_label", lang)
    xp_unit = t("group_xp_unit", lang)
    rating_label = t("rating_position_label", lang)
    top_title = t("top_donators_title", lang)
    default_name = t("group_default_name", lang)

    # ``group_xp`` only — see the module docstring. The group treasury
    # (``total_donations``, which the owner can withdraw from,
    # treasury_repo.py:96-99) is deliberately NOT a parameter here: it
    # used to be passed in and never read, and one careless f-string
    # would have re-opened the #479 leak. Substituting it for a zero XP
    # score also printed a number the rating below contradicts, because
    # ``DonationsRatingRepo.recalc_positions`` ranks on
    # ``COALESCE(group_xp, 0)``. Legacy printed 0 here (#479).
    xp_value = group_xp

    safe_group_name = _html.escape((group_name or "").strip() or default_name)
    # Falsy, not ``is not None``: legacy renders ``g['rating_position']
    # or '—'`` (bot.py:25006), so a legacy row holding 0 shows the dash.
    # ``DonationsRatingRepo.recalc_positions`` only ever writes NULL or
    # 1..N, so this differs from an ``is not None`` test on legacy rows
    # alone — matching /chatstats (inside ``chatstats.build_router``),
    # which already renders it this way (#485).
    rating_text = str(rating_position) if rating_position else "—"

    header = (
        f"📊 <b>{title}</b>\n\n"
        f"👥 {safe_group_name}\n"
        f"⚡ {xp_label}: <b>{xp_value}</b> {xp_unit}\n"
        f"🏆 {rating_label}: {rating_text}\n\n"
        f"🏅 <b>{top_title}:</b>\n"
    )
    if not top:
        return header.rstrip() + "\n"
    lines: list[str] = []
    for idx, (uid, total) in enumerate(top, start=1):
        # ``html_user_mention`` escapes the display string itself —
        # passing an already-escaped string would double-encode
        # ``<i>name</i>`` into ``&amp;lt;i&amp;gt;name&amp;lt;/i&amp;gt;``.
        display = (names.get(uid) or "").strip() or f"{default_name} {uid}"
        mention = html_user_mention(uid, display)
        lines.append(f"{idx}. {mention} — {total} {_COIN}")
    return header + "\n".join(lines)


def _keyboard(lang: str) -> InlineKeyboardMarkup:
    """The 🏆 rating + 💝 boost row under the /groupstats card (#7)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_groupstats_btn_rating", lang),
                    callback_data=RatingNav(page=1).pack(),
                ),
                InlineKeyboardButton(
                    text=t("h_groupstats_btn_boost", lang),
                    callback_data=GroupStatsBoost().pack(),
                ),
            ]
        ]
    )


async def handle_groupstats(
    message: Message,
    user_service: UserService,
    registry: EngineRegistry,
) -> None:
    user = await user_service.touch(require_from_user(message))
    chat_id = message.chat.id
    aggregate = await _fetch_aggregate(registry, chat_id)
    if aggregate is None:
        await message.reply(t("no_donates_in_group", user.language))
        log.bind(chat_id=chat_id, user_id=user.user_id).debug("/groupstats: no aggregate")
        return
    group_name, _treasury_total, group_xp, rating_position = aggregate
    top = await _fetch_top(registry, chat_id)
    names = await _fetch_names(registry, [uid for uid, _ in top])
    await message.reply(
        _render(
            user.language,
            group_name=group_name,
            group_xp=group_xp,
            rating_position=rating_position,
            top=top,
            names=names,
        ),
        reply_markup=_keyboard(user.language),
    )
    log.bind(chat_id=chat_id, user_id=user.user_id, rows=len(top)).info("/groupstats rendered")


def build_router(registry: EngineRegistry) -> Router:
    """Group-only at the router level — the private-DM branch (rating
    leaderboard) is owned by ``handlers.rating`` under the same command
    spellings. Bare-form only to keep room for a future
    ``/groupstats <chat_id>`` admin spelling without colliding with this
    self-group route.

    That split is also why no ``with_chat_type_refusal`` wrapper (#123)
    is applied here: these words already answer in a private chat, so a
    refusal twin would shadow the rating leaderboard instead of filling
    a silence.
    """
    router = Router(name="groupstats")
    router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

    async def _entry(message: Message, user_service: UserService) -> None:
        await handle_groupstats(message, user_service, registry)

    async def _boost_hint(callback: CallbackQuery, user_service: UserService) -> None:
        """💝 Boost tap → a localized alert on how the group climbs the
        rating. Read-only: no card edit, no owner-guard needed (the hint
        is public and identical for everyone)."""
        if callback.from_user is None:
            return
        user = await user_service.touch(callback.from_user)
        await callback.answer(t("h_groupstats_boost_hint", user.language), show_alert=True)

    router.message.register(
        _entry,
        Command(
            *GROUPSTATS_COMMANDS,
            ignore_case=True,
            magic=F.args.is_(None),
        ),
        F.from_user,
    )
    router.callback_query.register(_boost_hint, GroupStatsBoost.filter())
    return router
