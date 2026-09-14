"""/clear — bulk message cleanup (L-47).

Ports legacy ``cmd_clear`` (bot.py:32165) from the telebot pipeline to the
new aiogram pipeline. Admin-gated, group-only.

Commands claimed (group-only — a private chat gets the #123 refusal):

  /clear      (aliases purge, очистить, очистка) — bulk-delete recent messages.

Forms
-----
* ``/clear N``     — delete the last ``N`` messages (default 100, capped at
  :data:`MAX_CLEAR`). Deletes the contiguous ``message_id`` range ending at
  the ``/clear`` command message itself.
* ``/clear`` (by reply) — delete the contiguous ``message_id`` range from
  the replied-to message up to the command, **regardless of who wrote each
  message**. ``/clear N`` by reply caps the scan window at ``N``.

  That is a DELIBERATE divergence from legacy, not parity (#341). Legacy
  filtered by author inside SQL — ``clear_messages(chat_id, user_id=...)``
  selected only that user's rows from the ``user_history`` table
  (bot.py:9439-9448, called from bot.py:32196). We have no per-message
  history table (see "Why message-id arithmetic" below) and Telegram
  exposes no "messages by user in range" API, so the reply can only
  *anchor* the window — everything inside it goes. An admin clearing one
  user's spam burst also deletes whatever anyone else posted inside that
  burst. What bounds the damage is the window itself
  (``max(reply_id, cmd_id - N)``, so the range never reaches back past the
  replied-to message) and the owner-only gate below. ``target_id`` survives
  in the log line only, as the operator's record of what was aimed at.

  Not ported deliberately: legacy's query filtered on ``user_id`` but not
  on ``chat_id``, so it fed this chat ``delete_message`` the ids of that
  user's messages in *other* chats. Reproducing that would be a bug.

Why message-id arithmetic
-------------------------
The legacy handler read a ``user_history`` table that recorded every
group message's ``message_id``. The new pipeline persists no such
per-message history — deliberately, it is a lot of rows — so we
reconstruct the deletion set from Telegram's monotonic, per-chat
``message_id`` sequence: the messages immediately preceding the ``/clear``
command. That range is a guess: it knowingly contains gaps, service
messages and posts the bot may not delete, so "delete the last N, skip
what we can't" is the intended semantics.

Constraints (mirrors Telegram Bot API):
* Messages older than 48h cannot be deleted by a bot → those calls fail and
  are counted as skipped, reported back to the admin.
* Deletion is one ``deleteMessage`` per id — see :func:`_delete_ids` for
  why the bulk ``deleteMessages`` cannot be used here.

Authorisation (CRITICAL) — TWO gates, not one
---------------------------------------------
1. :func:`handlers.moderation._require_admin` — LIVE Telegram admin
   status plus the developer bypass and the anonymous-admin /
   foreign-``sender_chat`` handling, exactly as every other moderation
   command.
2. :func:`_require_chat_owner` — the legacy ``clear_command_only_owner``
   gate (bot.py:32175-32180), which the earlier port dropped on the
   false premise that "there is NO rank/role model in the new pipeline"
   (#339). The premise was wrong twice over: a rank model does exist
   (:mod:`services.rank_service`), and legacy's gate did not need one —
   it accepted the *chat creator* (``get_chat_creator_id``) or a
   developer independently of the stored group role. Both of those we
   can answer, so the gate is ported rather than waived. It defaults to
   ON because legacy's column defaulted to ON (bot.py:5732
   ``clear_command_only_owner INTEGER DEFAULT 1``, and every read site
   used ``gs.get(..., True)`` — bot.py:7792, 7822, 32175).

Deliberate narrowing vs. the legacy gate: legacy also let a user whose
stored *group role* was ``owner`` clear even when they were not the
Telegram creator. We have no per-group role store, so that arm is
dropped — it can only ever refuse someone legacy allowed, never allow
someone legacy refused, which is the safe direction for a command that
bulk-deletes up to :data:`MAX_CLEAR` messages.

Anonymous admins are refused here even though gate 1 lets them through:
Telegram deliberately hides which admin acted, so "is the actor the
creator?" is unanswerable, and legacy refused them for the same reason
(its role lookup ran against ``GroupAnonymousBot``'s id and missed).
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message
from loguru import logger

from telegram_invite_bot.core.chat_types import GROUP_TYPES
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.handlers.moderation import _require_admin, _resolve_lang
from telegram_invite_bot.i18n import t
from telegram_invite_bot.middlewares.session import SessionMiddleware
from telegram_invite_bot.utils.aiogram import command_body
from telegram_invite_bot.utils.numbers import is_int_token
from telegram_invite_bot.utils.telegram_admin import chat_creator_id

if TYPE_CHECKING:
    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.repositories.user_settings_repo import UserSettingsRepo

log = logger.bind(component="handlers.clear")


# Hard cap on how many messages a single /clear may attempt to delete.
# Legacy clamped to 500 server-side; we cap lower (100) because the new
# implementation reconstructs the id range without a history table and a
# wider blast radius (deleting service messages, other users' posts the
# admin can't see in context) is riskier. It also bounds a single
# ``/clear`` to 100 Telegram calls (see :func:`_delete_ids`).
MAX_CLEAR: int = 100

DEFAULT_CLEAR: int = 100


def _parse_count(token: str | None, *, default: int) -> int:
    """Parse a ``/clear N`` count token; clamp to ``[1, MAX_CLEAR]``.

    Returns ``default`` (already clamped) when the token is missing or
    non-numeric.
    """
    if token is None or not is_int_token(token):
        return min(max(default, 1), MAX_CLEAR)
    return min(max(int(token), 1), MAX_CLEAR)


async def _delete_ids(bot: Bot, chat_id: int, ids: list[int]) -> int:
    """Delete ``ids`` one at a time; return how many really went away.

    One ``deleteMessage`` per id, exactly as legacy did
    (``bot.py:9473-9477``), because the bulk ``deleteMessages`` reports
    nothing per id — the Bot API contract is "if some of the specified
    messages can't be found, they are skipped; returns True on success".
    A batch that removed 12 of 100 answers ``True`` all the same, and the
    ids here are *reconstructed* arithmetically rather than read out of a
    history table, so a wide miss is the normal case, not the edge one.
    Adding the batch size on that ``True`` (#253, what this did until
    now) is what turned "удалено 12" into "удалено 100".

    Keeping the bulk call would mean dropping the count from the
    confirmation altogether; the count is the more useful half, so it
    wins. The cost is bounded: :data:`MAX_CLEAR` caps one ``/clear`` at
    100 sequential calls — legacy ran the same loop with a cap of 500.

    Per-id failures (older than 48h, never existed, no rights) are
    swallowed and simply not counted.
    """
    deleted = 0
    for msg_id in ids:
        try:
            await bot.delete_message(chat_id, msg_id)
        except Exception:  # noqa: BLE001, PERF203, S112 — skip undeletable id
            continue
        deleted += 1
    return deleted


async def _require_chat_owner(
    message: Message,
    bot: Bot,
    settings: Settings,
    lang: str,
) -> bool:
    """Port of the legacy ``clear_command_only_owner`` gate (#339).

    Mirrors bot.py:32175-32180: a developer passes, otherwise the actor
    must be the chat creator. Legacy's third arm — a stored group role
    of ``owner`` — has no counterpart here and is dropped; see the
    module docstring for why that narrowing is the safe direction.

    Fail-closed on an unknown creator: :func:`utils.telegram_admin
    .chat_creator_id` answers ``None`` both for an API error and for a
    chat with no creator in its admin list, and neither is evidence the
    caller owns the chat. Legacy compared against ``None`` and fell into
    the same refusal.
    """
    tg_user = message.from_user
    if tg_user is None:  # pragma: no cover — the router filters on F.from_user
        return False
    if settings.bot.is_developer(tg_user.id):
        return True

    creator_id = await chat_creator_id(bot, message.chat.id)
    if creator_id is not None and creator_id == tg_user.id:
        return True

    await message.reply(t("h_clear_owner_only", lang))
    log.bind(chat_id=message.chat.id, from_id=tg_user.id, creator_id=creator_id).info(
        "/clear refused: caller is not the group owner",
    )
    return False


async def handle_clear(
    message: Message,
    bot: Bot,
    user_settings_repo: UserSettingsRepo,
    settings: Settings,
) -> None:
    """Bulk-delete recent messages (last N, or recent ones from a replied-to user)."""
    lang = await _resolve_lang(message, user_settings_repo)
    chat_id = message.chat.id

    if not await _require_admin(message, bot, settings, lang):
        return

    # #339: admin is necessary but not sufficient — /clear is owner-only.
    if not await _require_chat_owner(message, bot, settings, lang):
        return

    parts = command_body(message).split()
    count_token = parts[1] if len(parts) > 1 else None
    count = _parse_count(count_token, default=DEFAULT_CLEAR)

    cmd_id = message.message_id
    reply = message.reply_to_message

    if reply is not None and reply.from_user is not None:
        # Reply form: delete recent messages from the replied-to user,
        # scanning the id window [reply_id, cmd_id). ``count`` caps the
        # window so ``/clear 10`` (by reply) never scans more than 10 ids.
        target_id = reply.from_user.id
        window_start = max(reply.message_id, cmd_id - count)
        candidate_ids = list(range(window_start, cmd_id))
        # We cannot know each historical message's author from the id alone
        # (Telegram exposes no "messages by user in range" API), but the
        # reply anchors the window to the target's own message and the
        # contiguous burst that followed it — matching the legacy intent of
        # "clean up this user's recent spam". Always include the anchor.
        if reply.message_id not in candidate_ids:
            candidate_ids.insert(0, reply.message_id)
        ids = candidate_ids
        log.bind(chat_id=chat_id, target=target_id, n=len(ids)).info("/clear by reply")
    else:
        # Count form: delete the contiguous range of the last ``count``
        # message ids immediately preceding the command.
        ids = list(range(max(1, cmd_id - count), cmd_id))
        log.bind(chat_id=chat_id, n=len(ids)).info("/clear N")

    requested = len(ids)
    deleted = await _delete_ids(bot, chat_id, ids)
    skipped = requested - deleted

    # Also delete the /clear command message itself (best-effort, not
    # counted — it's the admin's own command, not "content").
    with contextlib.suppress(Exception):
        await bot.delete_message(chat_id, cmd_id)

    # Confirm back to the admin. The confirmation is itself a fresh message
    # (the command message is gone), so reply() would fail — send instead.
    if skipped > 0:
        text = t("h_clear_success_partial", lang, count=deleted, skipped=skipped)
    else:
        text = t("h_clear_success", lang, count=deleted)
    try:
        await bot.send_message(chat_id, text)
    except Exception as exc:  # noqa: BLE001
        log.debug("clear confirmation send failed: {exc!r}", exc=exc)


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Build the /clear router.

    Middlewares:
    * :class:`SessionMiddleware` — injects ``user_settings_repo`` for
      persisted-language resolution (mirrors moderation.py wiring).

    Group-only filter: private-chat invocations get the #123 refusal twin.
    """
    router = Router(name="clear")
    router.message.middleware(SessionMiddleware(registry))

    group_filter = F.chat.type.in_(GROUP_TYPES)

    async def _clear(
        message: Message,
        bot: Bot,
        user_settings_repo: UserSettingsRepo,
    ) -> None:
        await handle_clear(message, bot, user_settings_repo, settings)

    router.message.register(
        _clear,
        Command("clear", "purge", "очистить", "очистка", ignore_case=True),
        F.from_user,
        group_filter,
    )

    return with_chat_type_refusal(router, scope="group")
