"""``/lang`` — private-chat language switch (Stage 26 + T-006 i18n).

Scope and parity:

* **Private only.** Legacy ``cmd_lang`` (bot.py:24625) also lets group
  *owners* run ``/lang`` to change *their own* preference inside that
  chat. Routing that branch needs ``is_chat_owner`` — a chat-admin
  cache populated by ``getChatAdministrators`` calls peppered through
  the legacy code, plus a fallback to ``chat.get_member`` for unknown
  chats. The new pipeline hasn't ported any of that, so group ``/lang``
  is not served at all; since #123 it gets the shared private-only
  refusal instead of the silence left behind when the legacy bridge
  was removed (T-011).
* Inline keyboard is RU / EN — same two options as legacy
  (bot.py:24639). Button labels via ``t("lang_ru", "ru")`` /
  ``t("lang_en", "en")`` ensure consistency across the i18n system.
* Callback ``lang_set_ru`` / ``lang_set_en`` writes
  ``user_settings.language`` via :class:`UserService.set_language`.
  Wire format matches legacy callback_data (bot.py:24747) so an
  in-flight legacy menu mid-migration can still answer if the user
  happens to be looking at one when the new code goes live — defence
  against the strangler-bridge dropping a stale-button click.
* Confirmation via ``t("lang_changed", lang)`` — no HTML escape needed
  as YAML templates are curated safe strings.
* Confirmation surface: ``message.edit_text`` of the original menu
  (matches legacy's "click button → menu becomes 'Language set'"). If
  the edit fails (rare — happens if the message is already gone),
  we fall back to a fresh ``answer`` so the user still sees the
  acknowledgement.

**T-006**: Migrated from ``_PROMPT_RU``/``_EN`` + ``_CONFIRM_RU``/``_EN``
constants to ``t()`` i18n lookups (lang_select, lang_changed keys).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import Message as MessageType  # runtime — isinstance guard
from loguru import logger

from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.language import invalidate_language_cache
from telegram_invite_bot.utils.aiogram import require_from_user
from telegram_invite_bot.utils.html import legacy_md_to_html

log = logger.bind(component="handlers.language")

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, Message

    from telegram_invite_bot.db import Checkpoint
    from telegram_invite_bot.services.user_service import UserService


def _build_keyboard() -> InlineKeyboardMarkup:
    """Two-button row. Same callback_data as legacy so a stray click on
    a legacy-rendered menu post-migration still routes correctly through
    the strangler bridge.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=t("lang_ru", "ru"), callback_data="lang_set_ru"),
                InlineKeyboardButton(text=t("lang_en", "en"), callback_data="lang_set_en"),
            ]
        ]
    )


async def handle_lang_command(
    message: Message,
    user_service: UserService,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Render the language picker. ``touch`` keeps ``last_seen`` fresh
    and populates ``language_override`` so the prompt copy matches the
    user's current effective language.
    """
    user = await user_service.touch(require_from_user(message))
    # #1983: end the bookkeeping transaction here rather than hold
    # ``users.db``'s single writer slot across the reply below. See
    # :class:`db.session.Checkpoint`.
    if checkpoint is not None:
        await checkpoint()
    prompt = legacy_md_to_html(t("lang_select", user.language))
    await message.answer(prompt, reply_markup=_build_keyboard())


async def handle_lang_callback(
    callback: CallbackQuery,
    user_service: UserService,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Persist the choice and edit the prompt into a confirmation.

    The callback payload is one of ``lang_set_ru`` / ``lang_set_en``
    (filter below pins those two literals); ``set_language`` clamps to
    ``ru`` for anything other than ``en``, so a future third option
    appearing without an updated handler would degrade to RU rather
    than write junk.
    """
    assert callback.from_user is not None and callback.data is not None  # filters guarantee
    # ``touch`` BEFORE the settings write — ``user_settings.user_id``
    # has a FK to ``users.user_id`` (db/models/user_settings.py), so a
    # cold callback (user clicks a legacy-rendered button without ever
    # having hit a new-pipeline message handler) would otherwise raise
    # IntegrityError. The message-side handler already touches via the
    # picker render, but the contract here is "callback fully owns its
    # row's existence" — we can't rely on the picker having rendered
    # through the new code path.
    await user_service.touch(callback.from_user)
    choice = "en" if callback.data == "lang_set_en" else "ru"
    stored = await user_service.set_language(callback.from_user.id, choice)
    # #1983: two Telegram calls follow — the toast and the edit, plus
    # a third on the fallback below — and ``users.db`` has been this
    # update's only writer since ``touch``. The preference above is
    # what the user asked for and must stand however the rendering
    # goes, which is the condition :class:`db.session.Checkpoint` asks
    # for. ``stored`` is a plain string, so nothing here can trigger a
    # post-commit refresh.
    if checkpoint is not None:
        await checkpoint()
    # Drop the LanguageMiddleware TTL cache for this user so the new
    # preference takes effect on the very next update rather than after
    # the cache window — without this, /lang would appear to "not work"
    # for up to the TTL on the user's next command.
    #
    # #2024: AFTER the commit above, never before it — the same rule
    # ``handlers/rank_admin.py`` and ``repositories/rank_repo.py`` state
    # for their caches. ``users.db`` runs at ``synchronous=FULL``, so
    # ``checkpoint()`` is a real fsync and a real suspension point, and
    # this user's other in-flight update gets picked up inside it. That
    # update misses the cache, opens its own connection — which under
    # WAL sees only committed rows, i.e. the OLD language — and stores
    # it. Invalidating first does not prevent that; it guarantees it,
    # by handing the racing reader a clean miss and a generation
    # snapshot that is already past the bump, so
    # :class:`~utils.cache_generation.CacheGeneration` waves the stale
    # fill through. Bumping here instead puts the snapshot BEFORE the
    # bump: the reader still serves what it read, and declines to speak
    # for the next TTL on the strength of it.
    #
    # Outside the ``if`` deliberately. In production a
    # ``BaseSessionMiddleware`` always supplies the checkpoint, so the
    # ``None`` branch is a test shape rather than a deployment state —
    # but the invalidation must not be something that stops happening
    # if that ever changes.
    invalidate_language_cache(callback.from_user.id)
    confirm = t("lang_changed", stored)
    # answer_callback is the small grey toast — keeps the click feeling
    # responsive even if the edit_text below races a network hiccup.
    await callback.answer(confirm[:50])
    # ``callback.message`` is ``Message | InaccessibleMessage | None``.
    # ``InaccessibleMessage`` (the 96h+ old post case) lacks ``edit_text``
    # — bail rather than crash; the toast above already acknowledged.
    if isinstance(callback.message, MessageType):
        try:
            await callback.message.edit_text(confirm)
        except TelegramBadRequest:
            # "message is not modified" or "message to edit not found"
            # — fall back to a fresh send so the user still gets the
            # confirmation rather than a silent click.
            await callback.message.answer(confirm)
    log.bind(
        uid=callback.from_user.id,
        lang=stored,
    ).info("/lang switched")


def build_router() -> Router:
    """Two handlers, one router. Message side is gated on private chat
    (``F.chat.type == PRIVATE``); group ``/lang`` used to fall through
    to legacy for the owner-check, and since T-011 removed legacy it is
    the #123 refusal twin
    (:func:`~handlers.chat_scope.with_chat_type_refusal`) that answers
    it. Callback side has no chat-type filter, and that outlived its
    original reason: legacy's in-group ``/lang`` menu also fired
    ``lang_set_*``, and those keyboards are still sitting in group
    scrollback even though the process that sent them is gone. A tap on
    one still arrives, and this handler owning it is what keeps the
    user_settings write working regardless of which surface rendered
    the menu.
    Recorded as a deliberate #1608 exemption for that reason.
    """
    router = Router(name="language")
    # Private-chat-only for message handlers — group calls fall through
    # to legacy. ``router.message.filter`` applies only to the message
    # event type; the callback_query registration below is intentionally
    # left unfiltered (see docstring above).
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    router.message.register(
        handle_lang_command,
        # Stage 30: ``/settings`` / ``/настройки`` are folded in here
        # because the legacy ``cmd_settings`` (bot.py:24771) renders the
        # *same* language picker with the *same* ``lang_set_ru`` /
        # ``lang_set_en`` callback_data — the inline keyboard's two
        # buttons are the entire body of the legacy settings card.
        # Routing both surfaces to one handler keeps the callback
        # ownership story single — anything else risked two new-pipeline
        # handlers competing for the lang_set_* clicks.
        Command(
            "lang",
            "language",
            "язык",
            "kom_lang",
            "settings",
            "настройки",
            ignore_case=True,
        ),
        F.from_user,
    )
    router.callback_query.register(
        handle_lang_callback,
        F.data.in_({"lang_set_ru", "lang_set_en"}),
        F.from_user,
    )
    return with_chat_type_refusal(router, scope="private")
