"""``/timezone`` — private-chat timezone preference (Stage 27).

Three call shapes, mirroring legacy ``cmd_timezone`` (bot.py:16888):

1. **No argument** → show the user's stored tz with current local time,
   or a help blurb if they have none set.
2. **Argument in {``reset``, ``сброс``, ``clear``, ``удалить``}** →
   clear the stored tz. Wire format is the empty string (see
   :class:`UserSettingsRepo.set_timezone` for the parity rationale).
3. **Any other argument** → treat as IANA tz name; validate via
   :func:`format_local_time` (a successful ``ZoneInfo`` resolution and
   ``strftime`` is both necessary and sufficient — what the user will
   see is what we just rendered, so a name that parses but can't format
   is invalid by construction). On success persist; on failure echo an
   error.

Scope: all chat types, matching legacy ``cmd_timezone`` (which doesn't
gate on chat type and answers in groups too). The timezone is a per-user
setting keyed off the message author, so a group invocation simply sets
*that user's* zone — the weather/forecast renderers read it back from the
author regardless of where it was set. (An earlier private-only filter
relied on the strangler bridge passing group ``/timezone`` to legacy;
once the bridge was deleted that turned group calls into a silent
dead-end, so the filter was removed.)

Rendering note: legacy used ``parse_mode=Markdown``. The new bot is
HTML-only (default), so this handler renders HTML and escapes user
input. Output text is intentionally close to legacy copy but not
identical — we drop the ``t(lang, 'timezone_*')`` lookups for now and
inline RU strings; the EN copy will return when an i18n module lands
(Stage 14 of the plan).
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.filters import Command
from loguru import logger

from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.aiogram import command_args, require_from_user
from telegram_invite_bot.utils.time import format_local_time

log = logger.bind(component="handlers.timezone")

if TYPE_CHECKING:
    from aiogram.filters import CommandObject
    from aiogram.types import Message

    from telegram_invite_bot.db import Checkpoint
    from telegram_invite_bot.services.user_service import UserService


# Tokens that mean "clear my stored timezone". Match legacy exactly so a
# user's muscle memory keeps working post-migration. Lower-cased on
# comparison; the user-facing keywords stay as-is.
_RESET_TOKENS = frozenset({"reset", "сброс", "clear", "удалить"})


async def handle_timezone(
    message: Message,
    command: CommandObject,
    user_service: UserService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    tg_user = require_from_user(message)
    help_text = t("h_timezone_help", lang)
    # ``touch`` keeps users.last_seen fresh AND ensures the FK target
    # for any subsequent ``user_settings`` UPSERT exists. Mirrors the
    # /lang handler's cold-callback fix (Stage 26 audit HIGH).
    await user_service.touch(tg_user)
    # #1983: end the bookkeeping transaction here rather than hold
    # ``users.db``'s single writer slot across the reply below. See
    # :class:`db.session.Checkpoint`.
    if checkpoint is not None:
        await checkpoint()
    user_id = tg_user.id
    bound = log.bind(uid=user_id)

    arg = command_args(command)

    # ── Case 1: no argument → show current setting ────────────────────
    if not arg:
        stored = await user_service.get_timezone(user_id)
        if stored is None:
            await message.answer(t("h_timezone_unset", lang, help=help_text))
            return
        # ``lang`` picks the weekday vocabulary — without it an English
        # user's otherwise-English card ended on a Cyrillic weekday.
        loc_time, loc_date, weekday = format_local_time(stored, lang)
        safe_tz = html.escape(stored)
        if loc_time:
            await message.answer(
                t(
                    "h_timezone_current",
                    lang,
                    tz=safe_tz,
                    time=loc_time,
                    date=loc_date,
                    weekday=weekday,
                )
            )
        else:
            # Stored tz no longer resolves (e.g. tzdata removed a zone).
            # Surface that rather than silently showing only the name.
            await message.answer(t("h_timezone_current_unresolved", lang, tz=safe_tz))
        return

    # ── Case 2: reset token ───────────────────────────────────────────
    if arg.lower() in _RESET_TOKENS:
        await user_service.set_timezone(user_id, None)
        # The clear is what the user asked for; the confirmation below
        # must not be able to hold ``users.db`` while it is delivered.
        if checkpoint is not None:
            await checkpoint()
        await message.answer(t("h_timezone_reset_done", lang))
        bound.info("timezone cleared")
        return

    # ── Case 3: validate and persist ──────────────────────────────────
    # Validate via the same formatter the renderer uses — if it can't
    # produce a time string here, it can't render it on subsequent
    # /timezone calls either. Empty string indicates ZoneInfo failed.
    loc_time, _, _ = format_local_time(arg)
    if not loc_time:
        safe_arg = html.escape(arg)
        await message.answer(t("h_timezone_unrecognized", lang, tz=safe_arg, help=help_text))
        return
    await user_service.set_timezone(user_id, arg)
    # Same as the reset branch: the stored zone stands regardless of
    # how the confirmation goes out.
    if checkpoint is not None:
        await checkpoint()
    safe_arg = html.escape(arg)
    await message.answer(t("h_timezone_set_done", lang, tz=safe_arg))
    bound.info("timezone set: {tz!r}", tz=arg)


def build_router() -> Router:
    """Aliases mirror legacy registration (bot.py:16888).

    Chat-type-agnostic, matching legacy ``cmd_timezone``, which has NO
    ``require_group_feature`` gate and answers in groups too. A timezone
    is a per-user setting keyed off the message author, so it works the
    same from a group as from a DM. After the legacy bridge was deleted,
    a router-level PRIVATE filter here turned group ``/timezone`` into a
    silent dead-end, so it's removed.
    """
    router = Router(name="timezone")
    router.message.register(
        handle_timezone,
        Command("timezone", "часовой_пояс", "tz", "kom_timezone", ignore_case=True),
        F.from_user,
    )
    return router
