"""``/admin_botstats`` — developer-only "how many users / groups" snapshot.

Legacy ``/botstats`` (bot.py:25680) is two ``SELECT COUNT(*)`` queries
on ``users.users`` and ``users.bot_groups`` rendered as a 4-line card.
It's the second-most-asked admin question after ``/admin_status``
("the bot is alive — but is anybody actually using it?"). Port lifts
it onto SQLAlchemy and the same dev-gating posture as
``handlers/admin/status.py``.

Behaviour parity & deltas:

* Renamed ``/botstats`` → ``/admin_botstats`` to follow the new
  ``admin_*`` namespace convention (matches ``/admin_status``). The
  short name was left with legacy, which answered it and incremented a
  migration counter surfaced by ``/admin_status``. T-011 removed both,
  so ``/botstats`` now matches nothing at all: it is an owner-tier word
  the tree does not register, which :mod:`handlers.unknown_form` is
  deliberately blind to, so the operator gets silence. Unlike
  ``/deploy`` (#2003) no alias was added — that rename was undone
  because the module exists to prevent exactly that silence, whereas
  this one is a deliberate namespace move and re-adding the short name
  is an owner call. We rename ONCE, not in every admin port, because
  the muscle-memory tax of "guess the new prefix" is paid upfront
  rather than per-command.
* Dev gating is the same silent-drop posture as ``/admin_status``: a
  non-dev gets no reply at all (not "доступ запрещён") so the command
  isn't a side channel for enumerating developer IDs. See
  ``handlers/admin/status.py`` docstring for the rationale in full.
* Legacy gated additionally on ``message.chat.type == 'private'``.
  Kept verbatim — a developer running ``/admin_botstats`` in a public
  group would leak the user/group counts to the whole chat, which is
  the exact screenshot-forwarding leak that ``/admin_status`` already
  redacts against. Non-private invocations are dropped silently,
  same shape as the dev gate.
* HTML rendering (``<b>``, ``<code>``) instead of legacy's Markdown.
  The bot-wide ``parse_mode=HTML`` makes backtick-Markdown render as
  literal text; the new card matches the visual shape of
  ``/admin_status``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import func, select

from telegram_invite_bot.db.models.users import BotGroup, User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.admin.botstats")


async def _count_users_and_groups(registry: EngineRegistry) -> tuple[int, int]:
    """Two ``SELECT COUNT(*)`` queries against ``users.db``.

    Legacy does the same two reads against ``users.users`` and
    ``users.bot_groups`` via raw ``sqlite3``. We route through
    SQLAlchemy ``func.count`` so the query goes through the same
    engine-level PRAGMA tuning (WAL, busy_timeout) as the rest of the
    new pipeline — no ad-hoc connection lifecycle inside the handler.

    Returned as a tuple instead of a dict because there's never going
    to be a third dimension here (the legacy card has exactly two
    numbers and any "richer stats" command would be a separate admin
    handler with its own template).
    """
    engine = registry.engine(DBName.USERS)
    async with engine.connect() as conn:
        users_row = await conn.execute(select(func.count()).select_from(User))
        # Groups the bot has been removed from are excluded (#111) — the
        # card is read as "how far does the bot reach", and a chat it was
        # kicked out of last spring is not reach.
        groups_row = await conn.execute(
            select(func.count()).select_from(BotGroup).where(BotGroup.is_active.is_distinct_from(0))
        )
        return int(users_row.scalar_one()), int(groups_row.scalar_one())


def _render(users: int, groups: int, lang: str) -> str:
    return t("h_admin_botstats_card", lang, users=users, groups=groups)


async def handle_admin_botstats(
    message: Message,
    settings: Settings,
    registry: EngineRegistry,
    *,
    lang: str,
) -> None:
    """Render the bot-stats card iff dev + private chat.

    The two guards are evaluated in this order on purpose: the dev
    gate runs FIRST so a non-dev typing ``/admin_botstats`` in a group
    can't even learn that "private only" is a constraint (which would
    confirm the command exists). Reversing the order would turn the
    chat-type rejection into the same enumeration side-channel the
    silent dev-gate is designed to close.
    """
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_botstats; silently dropped"
        )
        return
    if message.chat.type != "private":
        log.bind(user_id=user.id, chat_type=message.chat.type).debug(
            "/admin_botstats in non-private chat; silently dropped"
        )
        return

    users, groups = await _count_users_and_groups(registry)
    await message.answer(_render(users, groups, lang))
    log.bind(user_id=user.id, users=users, groups=groups, lang=lang).info(
        "/admin_botstats rendered"
    )


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    router = Router(name="admin.botstats")

    async def _entry(message: Message, lang: str) -> None:
        await handle_admin_botstats(message, settings, registry, lang=lang)

    router.message.register(_entry, Command("admin_botstats", ignore_case=True))
    return router
