"""/voice_settings — per-group voice-transcription settings menu (L-71/L-72).

Ports the legacy group-admin transcription menu (``bot.py:32266``
``cmd_voice_settings`` → ``_render_voice_settings_menu`` 27955-27984) and
its inline callbacks (``bot.py`` 27987-28184 toggle/target/language,
28187-28228 stats) from the telebot pipeline to aiogram.

Surface claimed (group-only — private invocations are NOT claimed here):

  /voice_settings  (alias ``voice_settings_ru``) — render the
  transcription settings menu.

Aliases mirror legacy EXACTLY (``bot.py:32266`` registered only
``voice_settings`` + ``voice_settings_ru``). No invented RU text alias:
legacy never had one here, and minting a new command token risks
shadowing unrelated group chatter — parity over guesswork.

Why group-only / no private collision
-------------------------------------
Legacy ``cmd_voice_settings`` returns early unless the chat is a
``group``/``supergroup`` and the caller is a group admin (bot.py:32271-32274).
The new pipeline already ships a SEPARATE private-chat stub for the same
token in ``handlers/vip.py`` (a static "configure it in the group" reply,
router-filtered to PRIVATE). These two never overlap: this router carries a
GROUP filter, vip's carries a PRIVATE filter. So a group ``/voice_settings``
lands here (the real menu) and a private one lands on the vip stub — exactly
the legacy split, with no shadowing.

L-72 stats — a CALLBACK, not a command
--------------------------------------
Legacy renders transcription stats as the ``📊 Статистика`` button inside
this menu (cb ``voice_stats_<group_id>``, bot.py:27982 → 28187), NOT as a
slash command. (The unrelated TTS ``/voice_stats`` in ``handlers/vip.py`` is
a different feature and is left untouched.) So stats here is the
:class:`VoiceSettingsStats` callback, re-rendered in place over the menu
message.

Admin gating
------------
The message entry reuses :func:`handlers.moderation._require_admin` +
:func:`_resolve_lang` exactly like ``handlers/clear.py`` (LIVE TG-admin,
developer-bypass, anonymous-admin handling). Every callback RE-asserts the
tapping user's live admin status via :func:`utils.telegram_admin.is_user_admin`
— callback taps always carry the REAL clicking user (Telegram does not
anonymise ``callback.from_user``), so a forged payload from a non-admin is
rejected with an alert and never mutates settings.

Storage
-------
Reads/writes go through the L-70 USERS-db repos via :func:`session_for`
(``VoiceSettingsRepo`` for the menu state, ``VoiceTranscriptionRepo`` for
the stats card). Each callback opens its own short USERS session — the
mutation + re-read commit together (``session_for`` commits on clean exit).
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.handlers.moderation import _require_admin, _resolve_lang
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.voice_settings import (
    TARGET_LOG_CHAT,
    VoiceSettingsBack,
    VoiceSettingsOpenLanguage,
    VoiceSettingsOpenTarget,
    VoiceSettingsPickLanguage,
    VoiceSettingsPickTarget,
    VoiceSettingsStats,
    VoiceSettingsToggle,
    VoiceSettingsToggleAutoDelete,
    VoiceSettingsToggleOnlyAdmins,
    build_language_markup,
    build_menu_markup,
    build_stats_markup,
    build_target_markup,
    is_known_language,
    is_known_target,
)
from telegram_invite_bot.middlewares.session import SessionMiddleware
from telegram_invite_bot.repositories.voice_settings_repo import VoiceSettingsRepo
from telegram_invite_bot.repositories.voice_transcription_repo import (
    VoiceTranscriptionRepo,
)
from telegram_invite_bot.utils.aiogram import edit_card
from telegram_invite_bot.utils.telegram_admin import is_user_admin

if TYPE_CHECKING:
    from aiogram.types import InlineKeyboardMarkup

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo
    from telegram_invite_bot.repositories.voice_settings_repo import VoiceSettings
    from telegram_invite_bot.repositories.voice_transcription_repo import (
        VoiceTranscriptionStats,
    )

log = logger.bind(component="handlers.voice_settings")

# Longest transcription preview shown on the stats card (legacy showed the
# full last text; we cap to keep the card compact and the HTML escape cheap).
_STATS_PREVIEW_LEN: int = 200


def _render_menu(vs: VoiceSettings, lang: str) -> str:
    """Main menu body (legacy ``_render_voice_settings_menu`` 27946-27957).

    Status / target / language / auto-delete / admins-only lines mirror
    legacy; the model and device lines are dropped, because the knobs
    behind them are (see :func:`keyboards.builders.voice_settings.
    build_menu_markup`). HTML parse mode, never literal ``**bold**``.

    The ``log_chat`` warning is part of the body rather than a callback
    alert: an alert is gone the moment it's dismissed, and this is a
    standing condition of the group's configuration, not an event.
    """
    status = t("h_vset_status_on" if vs.enabled else "h_vset_status_off", lang)
    target = (
        t(f"h_vset_target_{vs.target}", lang)
        if is_known_target(vs.target)
        else (t("h_vset_target_chat", lang))
    )
    # The column predates both menus and was last written by the legacy
    # process, so its contents are not ours to trust: a value carrying
    # ``<`` would break HTML parsing and take the whole card down, not just
    # the one line. Known codes render as their localised label (legacy
    # printed the bare code); anything else is escaped and shown as-is,
    # which is more useful to an admin than silently rewriting it to "ru".
    language = (
        t(f"h_vset_lang_{vs.language}", lang)
        if is_known_language(vs.language)
        else html.escape(vs.language)
    )
    warning = ""
    if vs.target == TARGET_LOG_CHAT and not vs.log_chat_id:
        warning = t("h_vset_log_chat_unset", lang)
    return t(
        "h_vset_menu",
        lang,
        status=status,
        target=target,
        language=language,
        auto_delete=_switch_word(vs.auto_delete, lang),
        only_admins=_switch_word(vs.only_admins, lang),
        warning=warning,
    )


def _switch_word(on: bool, lang: str) -> str:
    """``✅ yes`` / ``❌ no`` for the two switch lines on the card."""
    return t("h_vset_on" if on else "h_vset_off", lang)


def _render_stats(stats: VoiceTranscriptionStats, lang: str) -> str:
    """Stats card body (legacy ``bot.py:28217-28222``).

    Fields that are ``None`` are omitted gracefully. ``last_text`` is
    HTML-escaped — it is arbitrary transcribed user content shown inside an
    HTML-parse-mode message.
    """
    lines = [t("h_vset_stats_title", lang), ""]
    lines.append(t("h_vset_stats_total", lang, total=stats.total))
    if stats.avg_processing_ms is not None:
        lines.append(t("h_vset_stats_avg", lang, ms=stats.avg_processing_ms))
    if stats.last_created is not None:
        when = stats.last_created.strftime("%d.%m.%Y %H:%M")
        lines.append(t("h_vset_stats_last", lang, when=when))
    if stats.last_text:
        preview = stats.last_text[:_STATS_PREVIEW_LEN]
        if len(stats.last_text) > _STATS_PREVIEW_LEN:
            preview += "…"
        lines.append(t("h_vset_stats_preview", lang, text=html.escape(preview)))
    return "\n".join(lines)


def _menu_markup(vs: VoiceSettings, lang: str) -> InlineKeyboardMarkup:
    """Menu keyboard for the current settings.

    Three of the buttons render their own state, so every call site has
    to pass the whole view rather than just ``enabled`` — funnelling that
    through one helper keeps a future switch from being added to the card
    and forgotten on the keyboard.
    """
    return build_menu_markup(
        enabled=vs.enabled,
        auto_delete=vs.auto_delete,
        only_admins=vs.only_admins,
        lang=lang,
    )


async def _edit_menu(message: Message, vs: VoiceSettings, lang: str) -> None:
    """Re-render the main menu in place, tolerating an old panel.

    Via :func:`~telegram_invite_bot.utils.aiogram.edit_card` rather than
    a raw ``edit_text``. This panel lives in a group for weeks, and
    "message is not modified" is only one of the ordinary ways Telegram
    refuses to redraw it — the others (deleted card, past the edit
    window, a body that turned out to be a caption) used to reach the
    global error router, so a group admin tapping a toggle on yesterday's
    panel got "⚠️ Произошла ошибка" instead of the redrawn menu.

    The three submenu prompts below take the same route for the same
    reason; they are one tap deeper into the same card.
    """
    await edit_card(message, _render_menu(vs, lang), reply_markup=_menu_markup(vs, lang))


async def _callback_guard(
    callback: CallbackQuery, bot: Bot, settings: Settings, lang: str
) -> Message | None:
    """Shared callback preamble: validate context + re-gate the tapping user.

    Returns the group :class:`Message` to edit on success, or ``None`` (after
    answering the callback) when the tap must be dropped — an inaccessible
    message, a non-group chat, or a non-admin clicker.
    """
    message = callback.message
    if not isinstance(message, Message) or message.chat.type not in GROUP_TYPES:
        await callback.answer()
        return None
    if settings.bot.is_developer(callback.from_user.id):
        return message
    status = await is_user_admin(bot, message.chat.id, callback.from_user.id)
    if status is not True:
        # None (API error) or False (not admin) → refuse. Fail-closed.
        key = "h_mod_retry_later" if status is None else "h_mod_no_permission"
        await callback.answer(t(key, lang), show_alert=True)
        return None
    return message


async def handle_voice_settings(
    message: Message,
    bot: Bot,
    registry: EngineRegistry,
    user_settings_repo: UserSettingsRepo,
    settings: Settings,
) -> None:
    """``/voice_settings`` — render the transcription settings menu (group admin)."""
    lang = await _resolve_lang(message, user_settings_repo)
    if not await _require_admin(message, bot, settings, lang):
        return
    async with session_for(registry, DBName.USERS) as session:
        vs = await VoiceSettingsRepo(session).get(message.chat.id)
    await message.answer(
        _render_menu(vs, lang),
        reply_markup=_menu_markup(vs, lang),
    )
    log.bind(group_id=message.chat.id).info("/voice_settings menu rendered")


async def handle_toggle(
    callback: CallbackQuery,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """Flip the master on/off flag, persist, re-render the menu.

    The flip itself happens in SQL (:meth:`VoiceSettingsRepo.toggle_enabled`)
    rather than as a read-then-write here — see that method for why a
    handler-side flip loses one of two simultaneous taps (#740). The
    ``get`` below is for rendering only, and rides inside the write
    transaction the toggle just opened, so the menu cannot show a value
    the database never held.
    """
    message = await _callback_guard(callback, bot, settings, lang)
    if message is None:
        return
    async with session_for(registry, DBName.USERS) as session:
        repo = VoiceSettingsRepo(session)
        await repo.toggle_enabled(message.chat.id)
        vs = await repo.get(message.chat.id)
    await _edit_menu(message, vs, lang)
    await callback.answer()
    log.bind(group_id=message.chat.id, enabled=vs.enabled).info("voice transcription toggled")


async def handle_toggle_auto_delete(
    callback: CallbackQuery,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """Flip "delete the voice note once transcribed" (RR-6 #73).

    Atomic single-statement flip, like :func:`handle_toggle` (#740).
    """
    message = await _callback_guard(callback, bot, settings, lang)
    if message is None:
        return
    async with session_for(registry, DBName.USERS) as session:
        repo = VoiceSettingsRepo(session)
        await repo.toggle_auto_delete(message.chat.id)
        vs = await repo.get(message.chat.id)
    await _edit_menu(message, vs, lang)
    await callback.answer()
    log.bind(group_id=message.chat.id, auto_delete=vs.auto_delete).info("voice auto-delete toggled")


async def handle_toggle_only_admins(
    callback: CallbackQuery,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """Flip "transcribe admins' voice notes only" (RR-6 #73).

    Atomic single-statement flip, like :func:`handle_toggle` (#740).
    """
    message = await _callback_guard(callback, bot, settings, lang)
    if message is None:
        return
    async with session_for(registry, DBName.USERS) as session:
        repo = VoiceSettingsRepo(session)
        await repo.toggle_only_admins(message.chat.id)
        vs = await repo.get(message.chat.id)
    await _edit_menu(message, vs, lang)
    await callback.answer()
    log.bind(group_id=message.chat.id, only_admins=vs.only_admins).info("voice admins-only toggled")


async def handle_open_target(
    callback: CallbackQuery,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """Open the delivery-target submenu."""
    message = await _callback_guard(callback, bot, settings, lang)
    if message is None:
        return
    async with session_for(registry, DBName.USERS) as session:
        vs = await VoiceSettingsRepo(session).get(message.chat.id)
    await edit_card(
        message,
        t("h_vset_target_prompt", lang),
        reply_markup=build_target_markup(current=vs.target, lang=lang),
    )
    await callback.answer()


async def handle_pick_target(
    callback: CallbackQuery,
    callback_data: VoiceSettingsPickTarget,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """Persist the chosen delivery target, return to the main menu."""
    message = await _callback_guard(callback, bot, settings, lang)
    if message is None:
        return
    if not is_known_target(callback_data.target):
        # Forged / unknown wire value — never trusted. Drop silently.
        await callback.answer()
        return
    async with session_for(registry, DBName.USERS) as session:
        repo = VoiceSettingsRepo(session)
        await repo.set_target(message.chat.id, callback_data.target)
        vs = await repo.get(message.chat.id)
    await _edit_menu(message, vs, lang)
    await callback.answer()
    log.bind(group_id=message.chat.id, target=vs.target).info("voice transcription target set")


async def handle_open_language(
    callback: CallbackQuery,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """Open the recognition-language submenu."""
    message = await _callback_guard(callback, bot, settings, lang)
    if message is None:
        return
    async with session_for(registry, DBName.USERS) as session:
        vs = await VoiceSettingsRepo(session).get(message.chat.id)
    await edit_card(
        message,
        t("h_vset_language_prompt", lang),
        reply_markup=build_language_markup(current=vs.language, lang=lang),
    )
    await callback.answer()


async def handle_pick_language(
    callback: CallbackQuery,
    callback_data: VoiceSettingsPickLanguage,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """Persist the chosen recognition language, return to the main menu."""
    message = await _callback_guard(callback, bot, settings, lang)
    if message is None:
        return
    if not is_known_language(callback_data.language):
        await callback.answer()
        return
    async with session_for(registry, DBName.USERS) as session:
        repo = VoiceSettingsRepo(session)
        await repo.set_language(message.chat.id, callback_data.language)
        vs = await repo.get(message.chat.id)
    await _edit_menu(message, vs, lang)
    await callback.answer()
    log.bind(group_id=message.chat.id, language=vs.language).info(
        "voice transcription language set"
    )


async def handle_stats(
    callback: CallbackQuery,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """📊 Statistics — render the read-only transcription stats card (L-72)."""
    message = await _callback_guard(callback, bot, settings, lang)
    if message is None:
        return
    async with session_for(registry, DBName.USERS) as session:
        stats = await VoiceTranscriptionRepo(session).stats(message.chat.id)
    await edit_card(message, _render_stats(stats, lang), reply_markup=build_stats_markup(lang))
    await callback.answer()
    log.bind(group_id=message.chat.id, total=stats.total).info("voice transcription stats rendered")


async def handle_back(
    callback: CallbackQuery,
    bot: Bot,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
) -> None:
    """🔙 Back — re-render the main menu from a submenu / stats card."""
    message = await _callback_guard(callback, bot, settings, lang)
    if message is None:
        return
    async with session_for(registry, DBName.USERS) as session:
        vs = await VoiceSettingsRepo(session).get(message.chat.id)
    await _edit_menu(message, vs, lang)
    await callback.answer()


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Build the /voice_settings router (L-71/L-72).

    Middlewares:
    * :class:`SessionMiddleware` (message side) — injects
      ``user_settings_repo`` for ``_resolve_lang``, exactly like
      ``handlers/clear.py``. Callbacks read ``lang`` from the root
      :class:`LanguageMiddleware` (injected on both event types) and open
      their own USERS sessions via ``session_for``.

    Group-only message filter: private ``/voice_settings`` falls through to
    the vip-router private stub. Callbacks carry no chat-type filter at
    registration — the ``vset`` prefix is unique to buttons this router
    itself rendered in a group, and every handler re-checks the chat type +
    re-gates the tapping user anyway.
    """
    router = Router(name="voice_settings")
    router.message.middleware(SessionMiddleware(registry))

    group_filter = F.chat.type.in_(GROUP_TYPES)

    async def _entry(
        message: Message,
        bot: Bot,
        user_settings_repo: UserSettingsRepo,
    ) -> None:
        await handle_voice_settings(message, bot, registry, user_settings_repo, settings)

    router.message.register(
        _entry,
        Command(
            "voice_settings",
            "voice_settings_ru",
            ignore_case=True,
        ),
        F.from_user,
        group_filter,
    )

    async def _toggle(callback: CallbackQuery, bot: Bot, lang: str) -> None:
        await handle_toggle(callback, bot, registry, settings, lang)

    async def _toggle_auto_delete(callback: CallbackQuery, bot: Bot, lang: str) -> None:
        await handle_toggle_auto_delete(callback, bot, registry, settings, lang)

    async def _toggle_only_admins(callback: CallbackQuery, bot: Bot, lang: str) -> None:
        await handle_toggle_only_admins(callback, bot, registry, settings, lang)

    async def _open_target(callback: CallbackQuery, bot: Bot, lang: str) -> None:
        await handle_open_target(callback, bot, registry, settings, lang)

    async def _pick_target(
        callback: CallbackQuery,
        callback_data: VoiceSettingsPickTarget,
        bot: Bot,
        lang: str,
    ) -> None:
        await handle_pick_target(callback, callback_data, bot, registry, settings, lang)

    async def _open_language(callback: CallbackQuery, bot: Bot, lang: str) -> None:
        await handle_open_language(callback, bot, registry, settings, lang)

    async def _pick_language(
        callback: CallbackQuery,
        callback_data: VoiceSettingsPickLanguage,
        bot: Bot,
        lang: str,
    ) -> None:
        await handle_pick_language(callback, callback_data, bot, registry, settings, lang)

    async def _stats(callback: CallbackQuery, bot: Bot, lang: str) -> None:
        await handle_stats(callback, bot, registry, settings, lang)

    async def _back(callback: CallbackQuery, bot: Bot, lang: str) -> None:
        await handle_back(callback, bot, registry, settings, lang)

    router.callback_query.register(_toggle, VoiceSettingsToggle.filter())
    router.callback_query.register(_toggle_auto_delete, VoiceSettingsToggleAutoDelete.filter())
    router.callback_query.register(_toggle_only_admins, VoiceSettingsToggleOnlyAdmins.filter())
    router.callback_query.register(_open_target, VoiceSettingsOpenTarget.filter())
    router.callback_query.register(_pick_target, VoiceSettingsPickTarget.filter())
    router.callback_query.register(_open_language, VoiceSettingsOpenLanguage.filter())
    router.callback_query.register(_pick_language, VoiceSettingsPickLanguage.filter())
    router.callback_query.register(_stats, VoiceSettingsStats.filter())
    router.callback_query.register(_back, VoiceSettingsBack.filter())

    return router
