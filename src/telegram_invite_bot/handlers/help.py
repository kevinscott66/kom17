"""``/help`` handler — Stage 8 of the strangler migration.

Pivot rationale: the originally planned Stage 8 commands (``/daily``,
``/gift``) both have hard couplings the new pipeline can't yet honour
without divergence from legacy semantics:

* ``/daily`` reward curve mixes in VIP item effects
  (:func:`ItemEffects.get_daily_bonus_percent`,
  :func:`ItemEffects.has_double_daily`) — porting it before the shop /
  VIP layer would silently differ from the old reward.
* ``/gift`` (``transfer_coins``) applies a configurable tax with a VIP
  discount and runs through an anti-abuse rate-limiter. A new code path
  that skipped either would create an exploit corridor.

Both land once shop + VIP repos exist. In the meantime Stage 8 ships a
clean read-only command that exercises the existing user pipeline:
``/help`` (aliases ``/h``, ``/commands``, ``/kom_help``). It renders the
help card in every chat type and ignores trailing args, matching legacy
``cmd_help`` (bot.py:25202).

Scope:

* All chat types (legacy answers in groups too, ungated).
* Args ignored (legacy matches the command regardless of trailing
  args, so ``/help admin`` renders the same card).
* Reads ``users.db`` for the user's language preference via
  ``UserService.touch`` (same as ``/profile``) — also advances
  ``last_seen`` exactly the way legacy does at the top of every handler.
* Inline keyboard: legacy renders a Telegraph URL button when
  ``settings.json`` has ``telegraph_commands_url`` / ``..._en``
  populated. The new pipeline reads the same URLs from
  ``Settings.help`` (typed env vars ``TELEGRAPH_COMMANDS_URL`` /
  ``_EN``). When both are unset only that *button* drops out — the
  keyboard itself does not. Legacy had no keyboard-less branch at all
  (bot.py:25249-25257); see :func:`_build_keyboard` for what rides the
  card instead, and why a group gets a DM deep link rather than the
  main-menu rows.

RR-6 #62/#63 — the card body itself. Until this wave ``/help`` printed
four hand-picked bullets while sixty-five registered commands sat
undiscoverable. The body now comes from
:mod:`telegram_invite_bot.handlers.help_catalog`, which renders the full
categorised catalog and, for staff, the rank-annotated owner reference
legacy served from ``/owner_help`` — a name the new pipeline already
spends on the developer diagnostics index (``handlers/admin/help.py``).

Role resolution here is deliberately fail-CLOSED: :func:`is_user_admin`
returns ``None`` on an API error, and ``None`` must never be treated as
"is an admin" (bug class R-FIX-007), so the comparison is an explicit
``is True``. Worst case a real admin sees the plain-user card — a
cosmetic loss — instead of a member learning the moderation thresholds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.db import DBName
from telegram_invite_bot.handlers.chat_scope import _FALLBACK_USERNAME
from telegram_invite_bot.handlers.help_catalog import render_help_pages
from telegram_invite_bot.handlers.main_menu import main_menu_keyboard
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.rank_repo import RankRepo
from telegram_invite_bot.utils.aiogram import require_from_user
from telegram_invite_bot.utils.telegram_admin import is_user_admin

log = logger.bind(component="handlers.help")

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import HelpConfig, Settings
    from telegram_invite_bot.core.entities.user import User as UserEntity
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.services.user_service import UserService


async def _dm_username(bot: Bot) -> str:
    """Bot username for the group-to-DM deep link.

    ``Bot.me()`` — memoised on the instance — rather than ``get_me()``,
    matching ``handlers/chat_scope.py:134``. A help card must never 500,
    so an API failure degrades to the shipped fallback.
    """
    try:
        me = await bot.me()
    except Exception:  # noqa: BLE001 — a help card must never 500
        log.opt(exception=True).warning("help: bot.me() unavailable; using fallback username")
        return _FALLBACK_USERNAME
    return (me.username or "").strip() or _FALLBACK_USERNAME


async def _build_keyboard(
    message: Message,
    user: UserEntity,
    help_config: HelpConfig,
    bot: Bot,
) -> tuple[InlineKeyboardMarkup, bool]:
    """The card's keyboard, plus whether the Telegraph button is on it.

    Legacy never emitted a keyboard-less ``/help``. ``bot.py:25249-25257``
    opens an ``InlineKeyboardMarkup``, adds the Telegraph button *when a
    URL is configured*, appends every row of
    ``build_main_menu_keyboard`` — which takes ``(user_id, lang)`` and no
    chat argument (bot.py:16053), so legacy attached it in groups too —
    and only if both halves came back empty falls back to a lone
    ``🔙 back_to_menu``. Returning ``None`` on an unset URL was therefore
    the one shape legacy could not produce (#632).

    The navigation half cannot be copied 1:1 into a group: every
    :class:`~keyboards.builders.main_menu.MainMenu` callback is
    registered behind ``F.message.chat.type == ChatType.PRIVATE``
    (the router-level filter in ``main_menu.build_router``), so those
    buttons would be *dead* outside a DM — worse than absent. A group
    gets the DM deep link
    instead, the same escape hatch ``handlers/chat_scope.py:134-142``
    uses, and the place legacy's ``main_menu`` callback led anyway. That
    substitution is a deliberate divergence, not parity.

    The returned flag speaks about the *Telegraph* button alone, because
    that is the button the "press the button below" footer promises.
    """
    rows: list[list[InlineKeyboardButton]] = []
    url = help_config.guide_url(user.language)
    if url:
        rows.append([InlineKeyboardButton(text=t("help_btn_list", user.language), url=url)])
    if message.chat.type == ChatType.PRIVATE:
        rows.extend(main_menu_keyboard(user.language).inline_keyboard)
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text=t("go_to_dm_btn", user.language),
                    url=f"https://t.me/{await _dm_username(bot)}?start",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows), url is not None


async def _effective_ranks(registry: EngineRegistry) -> dict[str, int]:
    """DB min-rank overrides, ``{}`` on any failure.

    Only the *overrides* are fetched; the renderer falls back to the
    catalog default per key. ``{}`` is therefore a complete, correct
    answer whenever moderation.db is unavailable — the annotations
    degrade to the shipped defaults instead of the card failing.
    """
    try:
        sessionmaker = registry.session(DBName.MODERATION)
        async with sessionmaker() as session:
            return await RankRepo(session).command_overrides()
    except Exception:  # noqa: BLE001 — a help card must never 500
        log.opt(exception=True).warning("help: rank overrides unavailable; using defaults")
        return {}


async def _is_chat_admin(message: Message, bot: Bot, user_id: int) -> bool:
    """Confirmed chat-admin status only — ``None`` (API error) is False."""
    if message.chat.type not in GROUP_TYPES:
        return False
    return await is_user_admin(bot, message.chat.id, user_id) is True


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Factory — fresh ``Router`` per call so tests can re-wire dispatchers.

    Takes the full :class:`Settings` (not just :class:`HelpConfig`)
    because the owner view keys off ``settings.bot.is_developer``, and
    the registry because the rank annotations read moderation.db.
    """
    help_config = settings.help

    async def handle_help(
        message: Message,
        user_service: UserService,
        bot: Bot,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        # ``touch`` bumps ``last_seen`` — legacy does the same via
        # ``ensure_user_access`` at the top of /help. We don't need the
        # ``is_new`` flag here (it's a help screen, not a welcome).
        user = await user_service.touch(require_from_user(message))
        # #220: below this line come one ``getChatMember`` and then up to
        # three sequential ``sendMessage`` calls, one per help page. The
        # ``touch`` above is bookkeeping that stands either way, so end its
        # transaction here rather than hold ``users.db``'s single writer
        # slot across the fan-out. See :class:`db.session.Checkpoint`.
        if checkpoint is not None:
            await checkpoint()
        is_developer = settings.bot.is_developer(user.user_id)
        is_staff = is_developer or await _is_chat_admin(message, bot, user.user_id)
        ranks = await _effective_ranks(registry) if is_staff else None

        keyboard, has_button = await _build_keyboard(message, user, help_config, bot)
        pages = render_help_pages(
            user.language,
            is_staff=is_staff,
            is_developer=is_developer,
            ranks=ranks,
            has_button=has_button,
        )
        last = len(pages) - 1
        for index, page in enumerate(pages):
            # The Telegraph button rides the final page only — repeating
            # it under every chunk would read as three separate offers.
            await message.answer(
                page,
                reply_markup=keyboard if index == last else None,
                disable_web_page_preview=True,
            )
        log.bind(
            uid=user.user_id,
            lang=user.language,
            staff=is_staff,
            dev=is_developer,
            pages=len(pages),
        ).info("/help rendered")

    router = Router(name="help")
    # Chat-type-agnostic, matching legacy. The legacy ``cmd_help``
    # (bot.py:25202) renders the help card in groups too with NO
    # ``require_group_feature`` gate — only the slot-message tracking and
    # auto-delete extras are group-specific, and those are deferred, not
    # the help body. The keyboard is NOT among them: legacy's
    # ``build_main_menu_keyboard`` takes ``(user_id, lang)`` and no chat
    # argument (bot.py:16053), so it rode the card in every chat type —
    # see :func:`_build_keyboard`. After the legacy bridge was deleted a
    # router-level PRIVATE filter here turned group ``/help`` into a
    # silent dead-end, so it's removed.
    #
    # Args are intentionally NOT filtered: legacy matches
    # ``commands=['help', ...]`` regardless of trailing args, so
    # ``/help admin`` renders the same card. Dropping ``magic=F.args``
    # keeps that parity (an earlier ``F.args.is_(None)`` silently ate
    # argful invocations once legacy was gone).
    router.message.register(
        handle_help,
        Command(
            "help",
            "h",
            "commands",
            "kom_help",
            ignore_case=True,
        ),
        F.from_user,
    )
    return router
