"""Advertiser request flow — /ad /ads /reklama (Cluster H4, L-61).

TRUTH-RULE NOTE (premise discrepancy, documented per directive)
---------------------------------------------------------------
The H4 backlog described /ads as an "owner announcements" broadcast
("send an announcement to all groups, dev-only, confirmation step,
rate-limited fan-out"). That premise is WRONG. Verified against legacy:

* ``bot.py:36854`` — ``@bot.message_handler(commands=['ad', 'ads',
  'reklama'])`` is open to EVERY user (only ``ensure_user_access``,
  no DEVELOPER_IDS gate). It is an inbound "advertise with us" funnel,
  not an outbound owner broadcast.
* ``bot.py:36861-36867`` — in groups it replies with a "DM the bot"
  pointer; the flow itself is private-only.
* ``bot.py:36533-36546`` — anti-abuse cooldown: max 1 request per
  ``ad_request_cooldown_hours`` (default 24, ``bot.py:2797``) per user;
  developers exempt. Tracked in-memory (``ad_request_last_time``,
  ``bot.py:3922``) — resets on restart, by design.
* ``bot.py:36792-36812`` — ``/ad`` sends an advertiser pitch form with
  live audience stats + an inline keyboard (refresh / FAQ / cancel),
  registers a next-step handler for the user's proposal text, and
  schedules form deletion after 600 s.
* ``bot.py:36891-36914`` — the user's next message is forwarded to
  ``ADMIN_CHAT_ID``, the cooldown timestamp is stamped AFTER the
  successful admin send, and the user gets a "request #N accepted"
  confirmation.

So the TRUE legacy rule is implemented here: a user-facing FSM funnel
(form → one free-text message → admin DM), not a broadcast. No money
moves; the only side effect is one admin-chat message. The implicit
"confirmation step" the backlog asked for exists naturally: nothing is
sent until the user types and submits their proposal text.

Scope reductions vs legacy (documented, not silent):

* Legacy ``get_ad_stats`` (``bot.py:36579-36688``) also rendered total
  bot users, Telegram-Premium count, language/city splits and message
  medians via raw cross-DB SQL. The strangler exposes only
  ``MessageStatsRepo.active_user_count`` (DAU/WAU/MAU) and the live
  Bot-API member count, so the form shows those four numbers. Adding
  users-DB aggregate repos for a marketing blurb was out of H4 scope.
* Legacy subtracted a ``main_chat_bots_count`` setting from the member
  count (``bot.py:36549-36559``); that runtime setting does not exist
  in the strangler config, so the raw ``get_chat_member_count`` value
  is shown (legacy default was "minus 1").
* Legacy had a 3-minute background thread auto-refreshing open forms
  (``bot.py:36689-36725``); the inline "refresh" button covers that
  interactively, no background task is spawned.
* The legacy menu entry-point callback ``ad_request_start``
  (``bot.py:16765``) is the main menu's, and is wired up there.

Parse mode: HTML. User-controlled fields are ``html.escape``-d before
interpolation into the admin notification (same contract as
``handlers/support.py:_format_admin_notification``).
"""

from __future__ import annotations

import contextlib
import html
import itertools
import time
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import Message as MessageType  # runtime — isinstance guard
from loguru import logger

from telegram_invite_bot.fsm.ads import AdsStates
from telegram_invite_bot.handlers.fsm_text import NOT_A_COMMAND, register_text_expected
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.message_stats import MessageStatsMiddleware
from telegram_invite_bot.scheduler.fsm_sweeper import STATE_ENTERED_AT_FIELD, utc_now_iso
from telegram_invite_bot.utils.aiogram import edit_card, require_from_user
from telegram_invite_bot.utils.render import clamp_utf16

log = logger.bind(component="handlers.ads")

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.types import CallbackQuery, Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.repositories.message_stats_repo import MessageStatsRepo


# ── Constants ────────────────────────────────────────────────────────────────

# Legacy default: max 1 request per 24 h per user (``bot.py:2797``
# ``"ad_request_cooldown_hours": 24``). Overridable through the
# ``ADS_REQUEST_COOLDOWN_HOURS`` setting; this constant stays the floor
# that setting falls back to, so a ``0`` in the environment means the
# legacy window rather than no cooldown at all.
_COOLDOWN_HOURS_DEFAULT = 24

# Legacy capped nothing on the proposal text; we truncate at 3000 so the
# admin DM (framing + escaped text) stays under Telegram's 4096 cap.
_REQUEST_MAX_LEN = 3000
# …but 3000 is code POINTS and Telegram measures UTF-16 units, so 2048
# emoji make a 4096-unit body that never reached ``_REQUEST_MAX_LEN``.
# The DM is the only delivery channel here (no ads table), so an
# over-length body means the request is refused every time it is
# retried. Clamp on Telegram's own ruler, leaving the framing — the
# fixed header plus a 64-char first_name and a 32-char @handle — room.
_ADMIN_BODY_MAX_UNITS = 3500

# Inline-keyboard callback tokens (legacy: ``ad_refresh_stats``
# ``bot.py:36815`` and ``ad_request_cancel`` ``bot.py:36839``).
_CB_REFRESH = "ads_refresh"
_CB_CANCEL = "ads_cancel"

# ── In-memory anti-abuse state (legacy parity: ``bot.py:3917-3922``) ─────────

# user_id -> unix-ts of last ACCEPTED request. In-memory on purpose:
# legacy ``ad_request_last_time`` was a plain dict that reset on restart;
# a marketing-funnel cooldown does not justify a DB table.
#
# Insertion order == stamp order: ``_stamp_cooldown`` pops before it
# writes, so re-stamping an existing user moves them to the end. That is
# what makes the "oldest is first" eviction below correct.
_last_request_at: dict[int, float] = {}

# Hard ceiling on the cooldown table. Pruning-on-stamp already drops every
# entry whose window has elapsed, so this only bites if that many distinct
# users hold a LIVE cooldown simultaneously — far above any plausible
# community, and it keeps the dict finite even if the cooldown is
# misconfigured to years.
_MAX_TRACKED_USERS = 50_000

# Monotone per-process request counter (legacy ``AdManager.counter``,
# ``bot.py:36506-36514`` — also in-memory, also restart-reset).
_request_seq = itertools.count(1)


def _cooldown_remaining_seconds(
    user_id: int,
    *,
    now: float,
    cooldown_hours: int,
    exempt: bool,
) -> int:
    """Seconds until the user may file the next request; 0 == allowed.

    Mirrors ``_can_create_ad_request`` (``bot.py:36533-36546``):
    developers are exempt, unknown users are allowed, otherwise the
    window is ``cooldown_hours`` from the last accepted request.
    """
    if exempt:
        return 0
    last_ts = _last_request_at.get(user_id)
    if last_ts is None:
        return 0
    remaining = int(cooldown_hours * 3600 - (now - last_ts))
    return max(0, remaining)


def _stamp_cooldown(user_id: int, *, now: float, cooldown_hours: int) -> None:
    """Record an accepted request (legacy ``bot.py:36904`` — stamped only
    AFTER the admin notification succeeded), and prune the table.

    An entry older than the cooldown window can never block a request
    again — :func:`_cooldown_remaining_seconds` returns 0 for it forever
    — so keeping it is pure leak. The dict used to grow by one float per
    advertiser for the lifetime of a process that is meant to run for
    months, and nothing ever removed a key.

    Pruning happens here rather than on the read path because a stamp is
    the *only* thing that can grow the table, and it happens at most once
    per user per window: the scan is O(live cooldowns) on an event that
    is already rate-limited to a trickle. Doing it on the read path
    instead would pay the scan on every ``/ad``, including the ones the
    cooldown rejects.
    """
    window = cooldown_hours * 3600
    for uid in [uid for uid, ts in _last_request_at.items() if now - ts >= window]:
        del _last_request_at[uid]
    # Pop-then-set so insertion order stays stamp order — otherwise a
    # re-stamped user keeps their original position and the eviction
    # below would drop a LIVE cooldown while a stale one survives.
    _last_request_at.pop(user_id, None)
    _last_request_at[user_id] = now
    while len(_last_request_at) > _MAX_TRACKED_USERS:
        del _last_request_at[next(iter(_last_request_at))]


def _cooldown_hours(settings: Settings) -> int:
    """Resolve the cooldown window from settings with the legacy default.

    ``BotConfig`` types the field ``int`` with ``ge=0``, so the only
    case left is zero — which the setting exists to shorten, not remove.
    """
    hours = settings.bot.ads_request_cooldown_hours
    return hours if hours > 0 else _COOLDOWN_HOURS_DEFAULT


# ── Stats gathering (scoped subset of legacy ``get_ad_stats``) ───────────────


async def _gather_stats(
    bot: Bot,
    message_stats_repo: MessageStatsRepo,
    *,
    main_chat_id: int,
    tz: ZoneInfo,
) -> tuple[int, int, int, int]:
    """Return ``(members, dau, wau, mau)`` for the main chat, best-effort.

    Legacy returned 0 on any failure (``bot.py:36549-36559`` member
    count; ``get_ad_stats`` wraps everything in try/except) — a stats
    hiccup must not block the funnel, so we do the same.
    """
    members = 0
    if main_chat_id != 0:
        try:
            members = await bot.get_chat_member_count(main_chat_id)
        except TelegramAPIError as exc:
            log.warning("ads form: member count failed: {e!r}", e=exc)

    dau = wau = mau = 0
    if main_chat_id != 0:
        today = datetime.now(tz).date()
        try:
            dau = await message_stats_repo.active_user_count(main_chat_id, days=1, today=today)
            wau = await message_stats_repo.active_user_count(main_chat_id, days=7, today=today)
            mau = await message_stats_repo.active_user_count(main_chat_id, days=30, today=today)
        except Exception as exc:  # noqa: BLE001 — stats are decorative here
            log.warning("ads form: activity stats failed: {e!r}", e=exc)

    return members, dau, wau, mau


def _form_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Refresh / cancel keyboard (legacy ``_ad_request_form_markup``,
    ``bot.py:36783-36789``; the legacy FAQ button pointed at the
    invite-FAQ callback owned by another router and is dropped here)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("h_ads_refresh_btn", lang), callback_data=_CB_REFRESH)],
            [InlineKeyboardButton(text=t("h_ads_cancel_btn", lang), callback_data=_CB_CANCEL)],
        ]
    )


def _format_admin_notification(
    *, req_id: int, uid: int, first_name: str, username: str, text: str
) -> str:
    """Admin-chat DM body (legacy ``_process_ad_request``,
    ``bot.py:36891-36903``). RU-only by the same convention as
    ``support._format_admin_notification`` — the admin chat is RU.
    All user-controlled fields are HTML-escaped.
    """
    handle = f"@{username}" if username else "—"
    return (
        "📢 <b>НОВАЯ РЕКЛАМНАЯ ЗАЯВКА</b>\n\n"
        f"📋 Номер: <code>#{req_id}</code>\n"
        f"👤 Пользователь: {html.escape(first_name or '—')} ({html.escape(handle)})\n"
        f"🆔 ID: <code>{uid}</code>\n\n"
        f"📝 Текст:\n{html.escape(clamp_utf16(text, _ADMIN_BODY_MAX_UNITS))}"
    )


# ── FSM sweeper callback ─────────────────────────────────────────────────────


async def on_expire_ads(bot: Bot, key: StorageKey, data: dict[str, object]) -> None:
    """Sweeper timeout for ``AdsStates.awaiting_text``.

    Legacy deleted the form message after 600 s (``schedule_deletion``,
    ``bot.py:36811``) and the next-step handler then answered "session
    expired". Here the sweeper clears the state; we DM the user so the
    silent expiry is visible. No money/ledger side effects exist in this
    flow, so expiry needs no compensation.

    NOTE: this is a service-side update path that bypasses the root
    LanguageMiddleware, hence ``lang`` comes from the FSM data stamp
    (same GAP-1 pattern as ``support.on_expire_support``).
    """
    lang_raw = data.get("lang")
    lang = lang_raw if isinstance(lang_raw, str) else "ru"
    with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(key.user_id, t("h_ads_timeout", lang))
    log.bind(uid=key.user_id).info("/ad session expired by sweeper")


# ── Handlers ─────────────────────────────────────────────────────────────────


async def handle_ad_command(
    message: Message,
    bot: Bot,
    state: FSMContext,
    message_stats_repo: MessageStatsRepo,
    settings: Settings,
    tz: ZoneInfo,
    lang: str,
) -> None:
    """``/ad`` (aliases ``/ads``, ``/reklama``) — open the advertiser form.

    Group chat → pointer reply (legacy ``bot.py:36861-36867``).
    Private → cooldown gate, then the stats form + FSM ``awaiting_text``.
    """
    user = require_from_user(message)

    if message.chat.type != ChatType.PRIVATE:
        await message.reply(t("h_ads_group_only", lang))
        return

    cooldown_h = _cooldown_hours(settings)
    remaining = _cooldown_remaining_seconds(
        user.id,
        now=time.time(),
        cooldown_hours=cooldown_h,
        exempt=settings.bot.is_developer(user.id),
    )
    if remaining > 0:
        await message.reply(
            t(
                "h_ads_cooldown",
                lang,
                hours=remaining // 3600,
                mins=(remaining % 3600) // 60,
                cooldown=cooldown_h,
            )
        )
        return

    prior = await state.get_state()
    if prior is not None and prior != AdsStates.awaiting_text.state:
        # In some OTHER flow (withdraw, support, …) — don't hijack it.
        await message.reply(t("h_ads_busy", lang))
        return

    members, dau, wau, mau = await _gather_stats(
        bot, message_stats_repo, main_chat_id=settings.bot.main_chat_id, tz=tz
    )
    req_id = next(_request_seq)

    await state.set_state(AdsStates.awaiting_text)
    # Stamp entered-at + lang + req_id: the sweeper reclaims orphaned
    # sessions after 600 s (legacy 10-min expiry) and renders the DM in
    # the user's language; req_id survives to the submit/refresh steps.
    await state.set_data(
        {STATE_ENTERED_AT_FIELD: utc_now_iso(), "lang": lang, "ads_req_id": req_id}
    )
    await message.reply(
        t("h_ads_form", lang, req_id=req_id, members=members, dau=dau, wau=wau, mau=mau),
        reply_markup=_form_keyboard(lang),
    )
    log.bind(uid=user.id, req_id=req_id).info("/ad form opened")


async def handle_ads_text(
    message: Message,
    state: FSMContext,
    bot: Bot,
    settings: Settings,
    lang: str,
) -> None:
    """Receive the proposal text and forward it to the admin chat.

    Order matters (legacy ``bot.py:36903-36905``): admin send FIRST,
    cooldown stamp ONLY on success — a failed delivery leaves the user
    free to retry immediately. State is cleared on both branches so the
    user is never stuck.
    """
    user = require_from_user(message)
    data = await state.get_data()
    req_id_raw = data.get("ads_req_id")
    req_id = req_id_raw if isinstance(req_id_raw, int) else 0
    text = (message.text or "").strip()[:_REQUEST_MAX_LEN]
    bound = log.bind(uid=user.id, req_id=req_id, text_len=len(text))

    admin_chat_id = settings.bot.admin_chat_id
    if admin_chat_id:
        try:
            await bot.send_message(
                admin_chat_id,
                _format_admin_notification(
                    req_id=req_id,
                    uid=user.id,
                    first_name=user.first_name or "",
                    username=user.username or "",
                    text=text,
                ),
            )
        except TelegramAPIError as exc:
            # Unlike support tickets (DB row is the source of truth,
            # admin DM best-effort), the admin DM IS the delivery here —
            # there is no ads table. So a failed send means a failed
            # request: tell the user, do NOT stamp the cooldown.
            bound.warning("ads admin notify failed: {e!r}", e=exc)
            await state.clear()
            await message.reply(t("h_ads_send_failed", lang))
            return
    else:
        # No admin chat configured: accepting silently would drop the
        # request into the void. Same refusal as the send-failure path.
        bound.warning("ads request dropped: no admin_chat_id configured")
        await state.clear()
        await message.reply(t("h_ads_send_failed", lang))
        return

    _stamp_cooldown(user.id, now=time.time(), cooldown_hours=_cooldown_hours(settings))
    await state.clear()
    await message.reply(t("h_ads_accepted", lang, req_id=req_id))
    bound.info("ads request forwarded to admin chat")


async def handle_ads_refresh(
    callback: CallbackQuery,
    state: FSMContext,
    bot: Bot,
    message_stats_repo: MessageStatsRepo,
    settings: Settings,
    tz: ZoneInfo,
    lang: str,
) -> None:
    """Inline "refresh stats" button (legacy ``callback_ad_refresh_stats``,
    ``bot.py:36815-36836``): re-render the form with live numbers.

    If the FSM session is gone (submitted / cancelled / swept) the
    button answers "session expired" — same contract as legacy's
    temp-data check (``bot.py:36820-36823``).
    """
    if await state.get_state() != AdsStates.awaiting_text.state:
        await callback.answer(t("h_ads_session_expired", lang))
        return

    data = await state.get_data()
    req_id_raw = data.get("ads_req_id")
    req_id = req_id_raw if isinstance(req_id_raw, int) else 0
    members, dau, wau, mau = await _gather_stats(
        bot, message_stats_repo, main_chat_id=settings.bot.main_chat_id, tz=tz
    )
    if isinstance(callback.message, MessageType):
        # "message is not modified" when numbers didn't change — the
        # toast below still confirms freshness (legacy answered the
        # callback with the member count either way).
        await edit_card(
            callback.message,
            t("h_ads_form", lang, req_id=req_id, members=members, dau=dau, wau=wau, mau=mau),
            reply_markup=_form_keyboard(lang),
        )
    await callback.answer(t("h_ads_refreshed", lang, members=members))


async def handle_ads_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    lang: str,
) -> None:
    """Inline "cancel" button (legacy ``callback_ad_request_cancel``,
    ``bot.py:36839-36851``): clear FSM, replace the form with a short
    cancelled notice. No cooldown stamp — cancelling is free.
    """
    if await state.get_state() == AdsStates.awaiting_text.state:
        await state.clear()
    body = t("h_ads_cancelled", lang)
    if isinstance(callback.message, MessageType):
        try:
            await callback.message.edit_text(body)
        except TelegramBadRequest:
            await callback.message.answer(body)
    await callback.answer()
    if callback.from_user is not None:
        log.bind(uid=callback.from_user.id).info("/ad form cancelled")


# ── Router factory ───────────────────────────────────────────────────────────


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Build the ads router.

    ``MessageStatsMiddleware`` is attached at router level, following
    the chatstats precedent (``chatstats.build_router``). It is attached
    to BOTH event types here, where chatstats attaches to ``message``
    only: the /ad command and the refresh callback both read DAU/WAU/MAU,
    so the callback needs the same repo.
    """
    router = Router(name="ads")
    stats_mw = MessageStatsMiddleware(registry)
    router.message.middleware(stats_mw)
    router.callback_query.middleware(stats_mw)

    tz = ZoneInfo(settings.stats.timezone)

    async def _handle_ad_command(
        message: Message,
        bot: Bot,
        state: FSMContext,
        message_stats_repo: MessageStatsRepo,
        lang: str,
    ) -> None:
        await handle_ad_command(message, bot, state, message_stats_repo, settings, tz, lang)

    # Not StateFilter(None)-gated: the command must also work while
    # ALREADY in awaiting_text (re-running /ad re-renders a fresh form —
    # legacy's next-step handler had the same effect of the newest form
    # winning). Other-flow states are rejected inside. /support carried
    # the router-level gate until #165 and was the only command in the
    # tree that refused to run purely because some state was set.
    router.message.register(
        _handle_ad_command,
        Command("ad", "ads", "reklama", ignore_case=True),
        F.from_user,
    )

    async def _handle_ads_text(
        message: Message,
        state: FSMContext,
        bot: Bot,
        lang: str,
    ) -> None:
        await handle_ads_text(message, state, bot, settings, lang)

    router.message.register(
        _handle_ads_text,
        StateFilter(AdsStates.awaiting_text),
        F.chat.type == ChatType.PRIVATE,
        F.from_user,
        F.text,
        # Legacy's next-step handler DID swallow commands — a known
        # annoyance, not a contract worth preserving.
        NOT_A_COMMAND,
    )
    register_text_expected(router, AdsStates.awaiting_text)

    async def _handle_ads_refresh(
        callback: CallbackQuery,
        state: FSMContext,
        bot: Bot,
        message_stats_repo: MessageStatsRepo,
        lang: str,
    ) -> None:
        await handle_ads_refresh(callback, state, bot, message_stats_repo, settings, tz, lang)

    router.callback_query.register(
        _handle_ads_refresh,
        F.data == _CB_REFRESH,
        F.from_user,
    )

    async def _handle_ads_cancel(
        callback: CallbackQuery,
        state: FSMContext,
        lang: str,
    ) -> None:
        await handle_ads_cancel(callback, state, lang)

    router.callback_query.register(
        _handle_ads_cancel,
        F.data == _CB_CANCEL,
        F.from_user,
    )

    return router
