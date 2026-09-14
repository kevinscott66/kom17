"""``/rules`` — show group rules.

Legacy ``/rules`` (bot.py:32109) reads ``group_settings.rules`` for the
current chat and renders it under a localised header. Group-only — in
private DMs the command doesn't make sense (there are no per-chat
rules without a chat), and legacy already gates on
``require_group=True``.

Behaviour parity & deltas:

* Group chats only. The router filter rejects private DMs. They used
  to fall through to legacy, which rendered a "not in a group" guard
  message; T-011 removed legacy, so that branch would have been a
  silent drop, and #123 is what supplies the refusal now
  (:func:`~handlers.chat_scope.with_chat_type_refusal`, whose wording
  lives in ``handlers/group_only.py`` rather than being invented per
  command).
* Language sourced from :class:`UserService.touch` (the same
  ``users.language`` column legacy reads via
  ``get_user_language``). Touch keeps ``last_seen`` fresh as a side
  effect — matches the legacy decorator stack which calls
  ``register_user`` / ``update_user_info`` before every handler.
* Empty / NULL ``rules`` → localised "no rules set" string, same as
  legacy. Distinct from "table missing" (which would mean a deploy
  glitch and rightly surfaces as an exception from the error router).
* ``setrules`` (legacy bot.py:32125) — the admin write-side companion
  to ``/rules`` — IS ported here (L-46). It reuses the moderation
  router's group-admin gate (:func:`moderation._require_admin`, which
  already handles the dev-bypass, anonymous-admin, and Telegram-API
  fail-closed cases) so the auth posture matches /ban, /mute, … exactly
  rather than inventing a second admin check. Single-arg form
  (``/setrules <text>``) mirroring legacy ``cmd_setrules`` — the whole
  message tail after the command word becomes the rules blob; a 4000
  char cap matches legacy. The DB write is an upsert on the modelled
  ``(group_id, rules)`` columns only (the prod ``updated_at`` column is
  left to its schema default — the new pipeline never models it).
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from telegram_invite_bot.db.models.users import GroupSettings
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.handlers.moderation import _require_admin
from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.aiogram import command_args, require_from_user
from telegram_invite_bot.utils.render import (
    TELEGRAM_TEXT_LIMIT,
    clamp_utf16,
    parsed_length,
    utf16_length,
)

# Parity with legacy ``cmd_setrules`` (bot.py:32142) — reject blobs
# longer than this before touching the DB.
_RULES_MAX_LEN = 4000

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import Message
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.repositories.users_repo import UsersRepo
    from telegram_invite_bot.services.user_service import UserService


log = logger.bind(component="handlers.rules")


async def _fetch_rules(registry: EngineRegistry, chat_id: int) -> str | None:
    """SELECT rules FROM group_settings WHERE group_id=:chat_id.

    Returns ``None`` for missing-row OR explicit-NULL OR empty-string
    (after strip). Three distinct on-disk states, one user-visible
    semantics ("no rules") — collapsing them here keeps the handler
    branch-free. Same conflation legacy does at bot.py:32118.
    """
    engine = registry.engine(DBName.USERS)
    async with engine.connect() as conn:
        row = await conn.execute(
            select(GroupSettings.rules).where(GroupSettings.group_id == chat_id)
        )
        value = row.scalar_one_or_none()
    if value is None:
        return None
    text_value = value.strip()
    return text_value or None


async def _store_rules(session: AsyncSession, chat_id: int, rules_text: str) -> None:
    """Upsert ``group_settings.rules`` for ``chat_id`` on ``session``.

    INSERT ... ON CONFLICT(group_id) DO UPDATE — the same
    update-then-insert semantics legacy spells out by hand at
    bot.py:32147-32155, expressed as a single atomic SQLite upsert.
    Only the two modelled columns (``group_id``, ``rules``) are
    touched; prod's ``updated_at`` keeps its schema default on insert
    and its prior value on update (the new pipeline never models it —
    see :class:`GroupSettings`).

    The write runs on the SAME per-update ``users`` session the
    SessionMiddleware opened (reached via the injected ``UsersRepo``) —
    NOT a fresh ``engine.begin()`` connection. ``user_service.touch``
    already holds a write transaction on ``users.db`` for this update;
    a second connection would deadlock SQLite ("database is locked").
    Sharing the session also folds the rules write into the same
    commit/rollback boundary as the touch.
    """
    stmt = sqlite_insert(GroupSettings).values(group_id=chat_id, rules=rules_text)
    stmt = stmt.on_conflict_do_update(
        index_elements=[GroupSettings.group_id],
        set_={"rules": rules_text},
    )
    await session.execute(stmt)
    await session.flush()


async def handle_setrules(
    message: Message,
    command: CommandObject,
    bot: Bot,
    user_service: UserService,
    users_repo: UsersRepo,
    settings: Settings,
) -> None:
    """``/setrules <text>`` — admin-only write form behind ``/rules``.

    Parity with legacy ``cmd_setrules`` (bot.py:32125): group-only
    (enforced at the router filter), admin-gated, single-arg, 4000-char
    cap, upsert into ``group_settings``. The admin gate is
    :func:`moderation._require_admin` — it replies with its own
    localized refusal (and fail-closed retry-later on Telegram-API
    error) and returns False, so we just bail when it does.
    """
    user = await user_service.touch(require_from_user(message))
    lang = user.language

    # Auth FIRST — _require_admin emits its own refusal copy on failure.
    if not await _require_admin(message, bot, settings, lang):
        return

    rules_text = command_args(command).strip()
    if not rules_text:
        await message.answer(t("h_setrules_usage", lang))
        return
    if len(rules_text) > _RULES_MAX_LEN:
        await message.answer(t("h_setrules_too_long", lang, max_len=_RULES_MAX_LEN))
        return

    try:
        await _store_rules(users_repo._session, message.chat.id, rules_text)  # noqa: SLF001
    except Exception:  # noqa: BLE001 — surface a localized failure card, log the trace
        log.bind(uid=user.user_id, chat_id=message.chat.id).exception("/setrules: store failed")
        await message.answer(t("h_setrules_error", lang))
        return

    await message.answer(t("h_setrules_ok", lang))
    log.bind(uid=user.user_id, chat_id=message.chat.id, length=len(rules_text)).info(
        "/setrules stored"
    )


async def handle_rules(
    message: Message,
    user_service: UserService,
    registry: EngineRegistry,
) -> None:
    """Render the group rules card under a localised header."""
    user = await user_service.touch(require_from_user(message))
    rules_text = await _fetch_rules(registry, message.chat.id)
    if rules_text is None:
        await message.answer(t("rules_empty", user.language))
        log.bind(uid=user.user_id, chat_id=message.chat.id).debug("/rules: empty")
        return
    # html.escape on the persisted blob — operators have free-text-typed
    # rules into the DB through legacy ``/setrules``, and a literal ``<``
    # would otherwise break HTML parsing under the bot-wide HTML parse_mode.
    header = f"📜 <b>{t('rules_title', user.language)}</b>"
    # ``_RULES_MAX_LEN`` counts code points; Telegram counts UTF-16
    # units. A rules card with an emoji per line is ~4090 units at the
    # cap, and the header tips it over 4096 — the group then gets no
    # answer at all. Split rather than trim: nothing in a rules card is
    # safe to drop, and the header is a title, not content.
    if parsed_length(header) + 2 + utf16_length(rules_text) <= TELEGRAM_TEXT_LIMIT:
        await message.answer(f"{header}\n\n{html.escape(rules_text)}")
    else:
        await message.answer(header)
        # The clamp only matters if something wrote the column outside
        # ``/setrules`` — a blob that arrived as a Telegram message is
        # already under the ceiling on its own.
        await message.answer(html.escape(clamp_utf16(rules_text, TELEGRAM_TEXT_LIMIT)))
    log.bind(uid=user.user_id, chat_id=message.chat.id, length=len(rules_text)).info(
        "/rules rendered"
    )


def build_router(registry: EngineRegistry, settings: Settings | None = None) -> Router:
    """Group-only filter at router level — the rules-reading handler
    never sees a private DM, which keeps the dispatcher's UNHANDLED
    fall-through routing private invocations to legacy where the
    "use this in a group" message lives.

    ``settings`` carries the developer-id allowlist + anonymous-admin
    policy that :func:`moderation._require_admin` consults on the
    ``/setrules`` write path. It is optional so the existing
    ``build_rules_router(registry)`` call site in ``main_router`` needs
    no second argument to keep its behaviour; when
    omitted we fall back to the process-wide :func:`get_settings`
    singleton (the same object dishka injects in prod). Tests that need
    a bespoke dev-id pass an explicit ``Settings`` here.
    """
    router = Router(name="rules")
    router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

    async def _entry(message: Message, user_service: UserService) -> None:
        await handle_rules(message, user_service, registry)

    router.message.register(
        _entry,
        Command("rules", "правила", "kom_rules", ignore_case=True),
        F.from_user,
    )

    async def _setrules_entry(
        message: Message,
        command: CommandObject,
        bot: Bot,
        user_service: UserService,
        users_repo: UsersRepo,
    ) -> None:
        # Resolve settings LAZILY — at first ``/setrules`` invocation,
        # not at router-build time. Eager resolution would force
        # ``get_settings()`` (env-backed) to run inside every
        # ``build_router(registry)`` call, including test harnesses that
        # construct the router without a populated ``BOT_TOKEN`` env.
        if settings is not None:
            resolved_settings = settings
        else:
            from telegram_invite_bot.config.settings import get_settings

            resolved_settings = get_settings()
        await handle_setrules(message, command, bot, user_service, users_repo, resolved_settings)

    router.message.register(
        _setrules_entry,
        Command("setrules", "установить_правила", "kom_setrules", ignore_case=True),
        F.from_user,
    )
    return with_chat_type_refusal(router, scope="group")
