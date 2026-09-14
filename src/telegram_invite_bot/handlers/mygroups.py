"""``/mygroups`` — multi-group admin panel, read-side (L-58).

Legacy ``cmd_mygroups`` / ``/мои_группы`` (bot.py:24826) opens
``send_mygroups_panel`` (bot.py:24791): the list of groups the caller
added the bot to, one button per group, plus a back-to-menu row. The
buttons were ``t.me`` URL jumps (public groups only) and the actual
admin tree lived behind a separate raw-callback maze that hasn't
migrated.

This port keeps the same scope ("groups where the caller is the
bot-registered admin/owner" — ``bot_groups.added_by_user_id``, the
attribution legacy writes on every ``my_chat_member`` update) and
upgrades the read-side surface:

* **List** — paginated (:data:`PAGE_SIZE` per page, legacy hard-capped
  at 10 buttons total), each row a typed callback button instead of a
  URL, so private groups are tappable too.
* **Card** — tapping a group renders a read-only summary: title/id,
  donations XP + rating position (``economy.groups_donations``),
  moderation one-liner (automod/antiflood via
  :class:`GroupModConfigRepo` — synthesised defaults when the group
  has no persisted row), and 7-day message/active-member counters
  (``message_stats.message_counts``). No write actions this batch —
  the legacy panel's transfer-ownership/leave buttons stay in legacy
  until the write-side migrates.

Authorisation: every callback re-checks
``added_by_user_id == callback.from_user.id`` against ``users.db``, so
forged or stale callback payloads can never open another user's card.

Behaviour parity notes:

* Private-chat-only at the router level — a group invocation falls
  through to UNHANDLED (→ bridge → legacy's ``only_private`` line),
  same as before this batch.
* Result capped at :data:`_MAX_GROUPS` rows. Pagination makes the old
  50-line readability cap moot; the cap now only bounds the per-update
  query cost for a pathological "added the bot to 1000 groups" user.
* Titles HTML-escaped — operator-typed via Telegram; bot-wide
  parse_mode is HTML.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger
from sqlalchemy import select

from telegram_invite_bot.db.models.economy import GroupDonationsAggregate
from telegram_invite_bot.db.models.users import BotGroup
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.mygroups import (
    PAGE_SIZE,
    MyGroupsCard,
    MyGroupsPage,
    build_card_markup,
    build_list_markup,
    total_pages,
)
from telegram_invite_bot.repositories.group_mod_config_repo import GroupModConfigRepo
from telegram_invite_bot.repositories.message_stats_repo import MessageStatsRepo
from telegram_invite_bot.utils.aiogram import edit_card

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, InlineKeyboardMarkup

    from telegram_invite_bot.config.settings import StatsConfig
    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.mygroups")


# Upper bound on rows fetched per update. Pagination keeps any page
# readable; this only bounds the query for pathological attributions.
_MAX_GROUPS = 200

# Activity window for the card's message/active counters — matches the
# ``/chatstats`` "week" window.
_ACTIVITY_DAYS = 7

_NAME_TRUNC = 40


async def _gather(registry: EngineRegistry, *, user_id: int) -> list[tuple[int, str | None]]:
    """Active ``bot_groups`` rows where ``added_by_user_id == user_id``.

    Groups the bot has been removed from are excluded: the card offers
    per-group actions the bot could no longer carry out there.

    One ``connect()``, one SELECT — read-only, not hot-path, so no
    transaction scoping needed. Order: by chat_id ascending so the same
    user gets a stable list (and stable page boundaries) across calls
    (the table has no clean ``added_at`` index and not every row has it
    populated).
    """
    engine = registry.engine(DBName.USERS)
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(BotGroup.chat_id, BotGroup.chat_title)
            .where(BotGroup.added_by_user_id == user_id)
            .where(BotGroup.is_active.is_distinct_from(0))
            .order_by(BotGroup.chat_id)
            .limit(_MAX_GROUPS)
        )
        return [(int(r[0]), r[1]) for r in rows.all()]


async def _fetch_owned_group(
    registry: EngineRegistry, *, user_id: int, group_id: int
) -> tuple[int, str | None] | None:
    """The (chat_id, title) row IF the caller is its registered admin.

    ``None`` both when the group is unknown and when it belongs to a
    different user — the two cases are deliberately indistinguishable
    to the caller so the card callback can't be used to probe which
    chat ids the bot knows about.
    """
    engine = registry.engine(DBName.USERS)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(BotGroup.chat_id, BotGroup.chat_title)
                .where(BotGroup.chat_id == group_id)
                .where(BotGroup.added_by_user_id == user_id)
                .where(BotGroup.is_active.is_distinct_from(0))
            )
        ).first()
    if row is None:
        return None
    return (int(row[0]), row[1])


@dataclass(frozen=True, slots=True)
class _Donations:
    total_donations: int
    group_xp: int
    rating_position: int | None


async def _fetch_donations(registry: EngineRegistry, group_id: int) -> _Donations | None:
    """The group's ``groups_donations`` aggregate, or ``None`` when the
    group never received a donation (no row).
    """
    engine = registry.engine(DBName.ECONOMY)
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    GroupDonationsAggregate.total_donations,
                    GroupDonationsAggregate.group_xp,
                    GroupDonationsAggregate.rating_position,
                ).where(GroupDonationsAggregate.group_id == group_id)
            )
        ).first()
    if row is None:
        return None
    total, xp, position = row
    return _Donations(
        total_donations=int(total or 0),
        group_xp=int(xp or 0),
        rating_position=int(position) if position else None,
    )


async def _fetch_mod_flags(registry: EngineRegistry, group_id: int) -> tuple[bool, bool]:
    """(automod_enabled, antiflood_enabled) — defaults view when unset.

    Read-only short session; :meth:`GroupModConfigRepo.get_or_default`
    is side-effect-free (the defaults view is never persisted).
    """
    sessionmaker = registry.session(DBName.MODERATION)
    async with sessionmaker() as session:
        view = await GroupModConfigRepo(session).get_or_default(group_id)
    return view.automod_enabled, view.antiflood_enabled


async def _fetch_activity(
    registry: EngineRegistry, group_id: int, *, tz: ZoneInfo
) -> tuple[int, int]:
    """(messages, active_users) over the last :data:`_ACTIVITY_DAYS` days.

    ``tz`` is :pyattr:`StatsConfig.timezone`, threaded from the router
    exactly as ``/chatstats`` does it. #1953: this used to be a bare
    ``date.today()`` under a comment claiming "same trade as /chatstats"
    — which was the one thing it was not. Every other reader of
    ``message_counts`` (``chatstats``, ``ads``, ``stats``, ``top``,
    ``profile``) threads the configured zone, and ``MessageStatsRepo``
    deliberately refuses to default it so the policy decision lands in
    the handler. Answering with the host clock made this card the only
    surface whose window shifts when ``STATS_TIMEZONE`` is not the host
    zone, and it shifts in the losing direction: ``_date_in_window`` is
    bounded at both ends, so a day-behind ``today`` silently drops the
    newest day instead of over-reporting.
    """
    today = datetime.now(tz).date()
    sessionmaker = registry.session(DBName.MESSAGE_STATS)
    async with sessionmaker() as session:
        repo = MessageStatsRepo(session)
        messages = await repo.chat_total_for_days(group_id, days=_ACTIVITY_DAYS, today=today)
        active = await repo.active_user_count(group_id, days=_ACTIVITY_DAYS, today=today)
    return messages, active


def _page_slice(
    rows: list[tuple[int, str | None]], page: int
) -> tuple[list[tuple[int, str | None]], int]:
    """Clamp ``page`` into range and return (visible rows, clamped page)."""
    pages = total_pages(len(rows))
    page = min(max(1, page), pages)
    start = (page - 1) * PAGE_SIZE
    return rows[start : start + PAGE_SIZE], page


def _render_list(
    lang: str, *, rows: list[tuple[int, str | None]], page: int
) -> tuple[str, InlineKeyboardMarkup]:
    """List page text + keyboard. ``rows`` is the FULL result set."""
    visible, page = _page_slice(rows, page)
    lines = [
        t("h_mygroups_title", lang),
        "",
        t("h_mygroups_total", lang, count=len(rows)),
        "",
    ]
    for chat_id, title in visible:
        safe_title = html.escape((title or "—")[:_NAME_TRUNC])
        lines.append(f"• <code>{chat_id}</code> — {safe_title}")
    lines.extend(["", t("h_mygroups_pick_hint", lang)])
    markup = build_list_markup(lang, rows=visible, page=page, total=len(rows))
    return "\n".join(lines), markup


def _render_card(
    lang: str,
    *,
    chat_id: int,
    title: str | None,
    donations: _Donations | None,
    automod: bool,
    antiflood: bool,
    messages: int,
    active: int,
    page: int,
) -> tuple[str, InlineKeyboardMarkup]:
    """One group's read-only summary card."""
    safe_title = html.escape(title or "—")
    lines = [t("h_mygroups_card_title", lang, title=safe_title, chat_id=chat_id), ""]
    if donations is None:
        lines.append(t("h_mygroups_card_no_donations", lang))
    else:
        lines.append(
            t(
                "h_mygroups_card_xp",
                lang,
                xp=donations.group_xp,
                donations=donations.total_donations,
            )
        )
        if donations.rating_position is not None:
            lines.append(t("h_mygroups_card_rating_pos", lang, position=donations.rating_position))
    on = t("h_mygroups_on", lang)
    off = t("h_mygroups_off", lang)
    lines.append(
        t(
            "h_mygroups_card_mod",
            lang,
            automod=on if automod else off,
            antiflood=on if antiflood else off,
        )
    )
    lines.append(t("h_mygroups_card_activity", lang, messages=messages, active=active))
    return "\n".join(lines), build_card_markup(lang, page=page)


async def handle_mygroups(message: Message, registry: EngineRegistry, lang: str) -> None:
    user = message.from_user
    if user is None:
        # Anonymous/sender_chat-as-author has no user_id — there's
        # nothing to scope the query to. Legacy implicitly skips this
        # branch too (it reads ``message.from_user.id`` without a
        # guard and the safe_handler swallows the AttributeError).
        return
    rows = await _gather(registry, user_id=user.id)
    if not rows:
        await message.answer(t("h_mygroups_empty", lang))
        log.bind(user_id=user.id).info("/mygroups rendered (empty)")
        return
    text, markup = _render_list(lang, rows=rows, page=1)
    await message.answer(text, reply_markup=markup)
    log.bind(user_id=user.id, count=len(rows)).info("/mygroups rendered")


async def handle_mygroups_page(
    callback: CallbackQuery,
    callback_data: MyGroupsPage,
    registry: EngineRegistry,
    lang: str,
) -> None:
    """Edit the list message to ``callback_data.page`` (re-scoped to the
    tapping user — the result never reflects anyone else's groups).
    """
    rows = await _gather(registry, user_id=callback.from_user.id)
    if not rows:
        # All attributions vanished between taps (bot kicked everywhere).
        await callback.answer(t("h_mygroups_not_found", lang), show_alert=True)
        return
    text, markup = _render_list(lang, rows=rows, page=callback_data.page)
    message = callback.message
    if isinstance(message, Message):
        # Page number rides in the callback data, so re-tapping the page
        # you are already on renders the same bytes — a Telegram reject,
        # not a bug. ``edit_card`` absorbs it (and the too-old card).
        await edit_card(message, text, reply_markup=markup)
    await callback.answer()


async def handle_mygroups_card(
    callback: CallbackQuery,
    callback_data: MyGroupsCard,
    registry: EngineRegistry,
    lang: str,
    tz: ZoneInfo,
) -> None:
    """Edit the list message to one group's summary card.

    Authorisation first: the group must be attributed to the tapping
    user in ``bot_groups``; unknown and not-yours both alert with the
    same "not found" so the callback can't probe the bot's group set.
    """
    owned = await _fetch_owned_group(
        registry, user_id=callback.from_user.id, group_id=callback_data.group_id
    )
    if owned is None:
        await callback.answer(t("h_mygroups_not_found", lang), show_alert=True)
        return
    chat_id, title = owned
    donations = await _fetch_donations(registry, chat_id)
    automod, antiflood = await _fetch_mod_flags(registry, chat_id)
    messages, active = await _fetch_activity(registry, chat_id, tz=tz)
    text, markup = _render_card(
        lang,
        chat_id=chat_id,
        title=title,
        donations=donations,
        automod=automod,
        antiflood=antiflood,
        messages=messages,
        active=active,
        page=callback_data.page,
    )
    message = callback.message
    if isinstance(message, Message):
        # Same reason as the list handler: re-opening the card already
        # on screen must be a no-op, not an error toast.
        await edit_card(message, text, reply_markup=markup)
    await callback.answer()
    log.bind(user_id=callback.from_user.id, group_id=chat_id).info("/mygroups card rendered")


def build_router(registry: EngineRegistry, stats_config: StatsConfig) -> Router:
    """Private-only at the router level — legacy returns a one-liner
    "only in DM" hint for group invocations; falling through to
    UNHANDLED (and via the bridge to legacy's one-liner) keeps the
    user-visible behaviour identical.

    The callback side carries the same filter since #1608. The old
    reasoning still holds — a panel this router itself sent is always
    private, and every payload is re-scoped to ``callback.from_user.id``
    anyway — but it left the scope resting entirely on those inner
    gates, which is the class this ticket exists to close.
    """
    router = Router(name="mygroups")
    # #1953: captured here rather than read per-tap, mirroring
    # ``chatstats.build_router`` — one place decides what "today" means
    # for every reader of ``message_counts``.
    tz = ZoneInfo(stats_config.timezone)
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)
    # #1608: same scope on the callback side. No legacy surface emits
    # the ``mygrp``/``mygrc`` prefixes (checked against bot.py), so a
    # click from a group cannot be a real flow. ``F`` is not imported
    # here, and the lambda spelling matches the message filter above
    # as well as ``admin/panel.py`` and ``admin/withdrawals.py``.
    router.callback_query.filter(
        lambda c: c.message is not None and c.message.chat.type == ChatType.PRIVATE
    )

    async def _entry(message: Message, lang: str) -> None:
        await handle_mygroups(message, registry, lang)

    async def _page(callback: CallbackQuery, callback_data: MyGroupsPage, lang: str) -> None:
        await handle_mygroups_page(callback, callback_data, registry, lang)

    async def _card(callback: CallbackQuery, callback_data: MyGroupsCard, lang: str) -> None:
        await handle_mygroups_card(callback, callback_data, registry, lang, tz)

    # ``admin``: legacy ``cmd_admin`` (bot.py:25457) answered a
    # non-developer in a DM with exactly this list of manageable
    # groups, and the command catalog still describes the word that
    # way. It was pointing at the developer panel here; that panel
    # keeps ``/admin_panel``.
    router.message.register(_entry, Command("mygroups", "мои_группы", "admin", ignore_case=True))
    router.callback_query.register(_page, MyGroupsPage.filter())
    router.callback_query.register(_card, MyGroupsCard.filter())
    return with_chat_type_refusal(router, scope="private")
