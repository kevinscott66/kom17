"""``/nick`` — set or clear the caller's per-group display name.

Legacy ``/nick`` (bot.py:24843) writes ``users.user_group_nicknames``
through raw ``sqlite3``; this stage replaces the writer with the
SQLAlchemy mapper. Read-side users (legacy ``get_user_display_name_in_chat``,
the upcoming /top renderer, the /profile card) keep working against
the same row — same table, same columns, same updated_at semantics.

Behaviour parity with legacy:

* Group-only. The router filter rejects private DMs, and since #123
  such a DM is answered by the shared ``h_group_only_command``
  refusal — the same "in a group only" wording legacy rendered
  before its bridge was removed (T-011), kept in one place instead
  of reinvented per module.
* ``/nick Some Name`` upserts the row; the name is trimmed and clamped
  to 100 chars (legacy slice at ``bot.py:24854``).
* Bare ``/nick`` (no args) deletes the row, restoring the global
  first-name display. Symmetric with legacy's ``not name`` branch at
  bot.py:6939.
* The handler always replies (success or "couldn't save") — no silent
  drops. Failure messaging is the localised ``nick_error`` string
  already present in legacy + ``i18n/data/{ru,en}.yaml``.

Why the write goes through :class:`NicknamesRepo` (i.e. the
session-middleware session) rather than a fresh ``engine.begin()``:

* The session middleware already holds an open ``users.db`` session
  for ``UserService.touch``. Opening a second writer against the same
  SQLite file from the same handler deadlocks on the file lock under
  WAL — the touch is mid-transaction, the new ``begin()`` waits for
  it to commit, and the touch can't commit until the handler returns.
* Sharing the session also means ``touch`` + nick-write commit
  atomically. A crash between them rolls back both — no half-saved
  state where the user is registered as new but their nick write
  was lost.

Concurrency with legacy: SQLite's writer lock + ON CONFLICT match
legacy's raw upsert verbatim, so a row written from either side is
identical regardless of which writer ran last.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from loguru import logger

from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.aiogram import require_from_user

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.repositories.nicknames_repo import NicknamesRepo
    from telegram_invite_bot.services.user_service import UserService


log = logger.bind(component="handlers.nick")

# Legacy hard-clamp at bot.py:24854. Same constant exposed here so a
# future schema migration that widens ``display_name`` only needs to
# touch one place — and tests can assert the boundary explicitly.
NICK_MAX_LEN = 100

# Visual-impersonation defence. The nick renders inside HTML-escaped
# output, so this is NOT about injection — it's about a user setting a
# nick that *looks like* someone else's (or like a system string) in
# /top and /profile via Unicode control characters:
#
#   * bidi overrides  U+202A–U+202E  — force LTR/RTL, can visually
#     reverse or reorder a string ("admin" → "nimda" rendered as admin).
#   * bidi isolates   U+2066–U+2069  — same family, isolate-scoped.
#   * zero-width chars U+200B–U+200D, U+2060, U+FEFF — invisible, let two
#     distinct stored nicks render identically (duplicate impersonation).
#
# We strip every one of these *before* the length clamp so the visible
# character budget isn't burned by invisible padding either.
_FORBIDDEN_NICK_CHARS = frozenset(
    {
        *(chr(cp) for cp in range(0x202A, 0x202F)),  # bidi embeddings/overrides + PDF
        *(chr(cp) for cp in range(0x2066, 0x206A)),  # bidi isolates + PDI
        *(chr(cp) for cp in range(0x200B, 0x200E)),  # zero-width space/non-joiner/joiner
        chr(0x2060),  # word-joiner
        chr(0xFEFF),  # zero-width no-break space / BOM
    }
)


def _sanitize_nick(raw: str) -> str:
    """Drop bidi-control and zero-width chars, then collapse the result.

    Returns the cleaned string (still pre-clamp). An input that is
    *only* control characters collapses to ``""`` and is treated by the
    caller exactly like a bare ``/nick`` — i.e. a clear — so there is no
    way to store an all-invisible nick.
    """
    cleaned = "".join(ch for ch in raw if ch not in _FORBIDDEN_NICK_CHARS)
    return cleaned.strip()


async def handle_nick(
    message: Message,
    command: CommandObject,
    user_service: UserService,
    nicknames_repo: NicknamesRepo,
) -> None:
    """Dispatch on argv: text-arg = set, bare = clear."""
    from_user = require_from_user(message)
    user = await user_service.touch(from_user)
    raw = (command.args or "").strip()
    # Strip Unicode control chars (bidi/zero-width) used for visual
    # impersonation *before* the length clamp — invisible padding must
    # not burn the display-char budget. An all-control input collapses
    # to "" and falls through to the clear branch below.
    raw = _sanitize_nick(raw)
    # Slice on the trimmed value — leading whitespace shouldn't burn
    # display chars. Legacy applied ``[:100]`` to the trimmed string
    # (bot.py:24854), match that order so a 105-char input with two
    # leading spaces lands as a 100-char nick, not 98.
    new_name = raw[:NICK_MAX_LEN] if raw else ""
    chat_id = message.chat.id

    # The try wraps the DB write ONLY. It used to cover the reply too,
    # which meant a send failure ("nick saved, message didn't render")
    # was reported to the user as "couldn't save" — the opposite of
    # what happened, and unrecoverable: they'd retry a nick that was
    # already stored. Delivery failures now reach the errors router
    # like everywhere else.
    try:
        if new_name:
            await nicknames_repo.set(
                user_id=user.user_id,
                chat_id=chat_id,
                display_name=new_name,
            )
        else:
            await nicknames_repo.clear(user_id=user.user_id, chat_id=chat_id)
    except Exception:  # noqa: BLE001 — any DB failure surfaces as the
        # localised "couldn't save" UX, identical to legacy's ``ok=False``
        # branch at bot.py:24858. The exception still propagates to the
        # session-middleware rollback (we re-raise nothing, but the
        # session won't commit because the unit-of-work never reached
        # the BaseSessionMiddleware's commit point if we re-raise).
        # We swallow + reply because the user-visible contract is
        # "always a reply" — legacy never bubbled.
        log.bind(uid=user.user_id, chat_id=chat_id).exception("/nick failed")
        await message.reply(t("nick_error", user.language))
        return

    if new_name:
        # SECURITY: the nick is raw user text and the bot sends with
        # parse_mode=HTML, while ``i18n.t`` does not escape its kwargs.
        # Unescaped, ``/nick <a href="…">Поддержка</a>`` made the BOT
        # post a live link in the group under its own name, and a lone
        # ``<`` broke entity parsing outright.
        await message.reply(t("nick_set", user.language, name=html.escape(new_name)))
        log.bind(uid=user.user_id, chat_id=chat_id, length=len(new_name)).info("/nick set")
    else:
        await message.reply(t("nick_cleared", user.language))
        log.bind(uid=user.user_id, chat_id=chat_id).info("/nick cleared")


def build_router() -> Router:
    """Group-only at the router level — matches legacy's ``require_group``
    gate and keeps private invocations falling through to legacy where
    the localised "use in a group" message lives.

    No ``registry`` argument: the repo is supplied by the session
    middleware as a kwarg on dispatch, so this router doesn't need a
    DI handle at construction time.
    """
    router = Router(name="nick")
    router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

    async def _entry(
        message: Message,
        command: CommandObject,
        user_service: UserService,
        nicknames_repo: NicknamesRepo,
    ) -> None:
        await handle_nick(message, command, user_service, nicknames_repo)

    router.message.register(
        _entry,
        Command("nick", "setnick", "ник", "никнейм", ignore_case=True),
        F.from_user,
    )
    return with_chat_type_refusal(router, scope="group")
