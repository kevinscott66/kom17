"""``/donaters`` — top donators of the current group.

Legacy ``/donaters`` (bot.py:24928) reads
``economy.group_top_donators`` for the current chat, joins user names
from ``users.users`` for the mention, and renders a top-10 list. The
``/donate`` writer (legacy) maintains ``group_top_donators`` as a
denormalised counter so this read is one indexed SELECT instead of a
GROUP BY over the full ledger.

Port choices:

* Group-only (router filter). A private DM gets the shared #123
  group-only refusal. Same posture as ``/rules``.
* Two SELECTs across two engines: top-N from ``economy.db``, then
  display names from ``users.db`` keyed by the user IDs we just
  read. SQLite-cross-file JOINs aren't supported via SQLAlchemy
  without ATTACH (which we deliberately avoid — see ``db/engines.py``
  for the per-DB isolation rationale).
* Missing-name fallback: render ``h_donaters_default_name`` with the
  id (legacy fallback was a bare ``f"User {uid}"``). The point is that
  a missing ``users.users`` row never produces an empty mention or a
  crash.
* HTML mention via :func:`html_user_mention` — same primitive
  ``/relations`` uses, so a free-text display name with ``<`` in it
  doesn't break the bot-wide HTML parse_mode.
* No ``/donate`` write path here — that's the legacy writer's job
  until donations are fully ported (needs balance debit + group
  group_xp credit + ledger insert, three-way transactional shape).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import select

from telegram_invite_bot.db.models.economy import GroupTopDonator
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.html import html_user_mention

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.donaters")

_TOP_LIMIT = 10
# Legacy uses the static "монет" emoji; the new pipeline doesn't carry
# the COM_EMOJI constant yet, and rendering it requires the group's
# COIN config which isn't on the read path. Hardcoded coin emoji here
# matches the most-common legacy render (bot.py defines COM_EMOJI =
# "🪙" by default in modules where it's read).
_COIN = "🪙"


async def _fetch_top(
    registry: EngineRegistry, chat_id: int, limit: int = _TOP_LIMIT
) -> list[tuple[int, int]]:
    """SELECT user_id, total_donated FROM group_top_donators
    WHERE group_id=:chat ORDER BY total_donated DESC LIMIT :n.

    Returns the rows as ``(user_id, total)`` tuples — no dataclass
    because the result is consumed once, immediately, by the renderer.
    """
    engine = registry.engine(DBName.ECONOMY)
    async with engine.connect() as conn:
        result = await conn.execute(
            select(GroupTopDonator.user_id, GroupTopDonator.total_donated)
            .where(GroupTopDonator.group_id == chat_id)
            # ``user_id`` breaks ties: without it two donators on the same
            # total can swap places between calls, and at the LIMIT
            # boundary a different SET of rows comes back — a donator
            # appears and disappears with nothing having changed. The
            # siblings already do this (rating.py, message_stats_repo).
            .order_by(GroupTopDonator.total_donated.desc(), GroupTopDonator.user_id.asc())
            .limit(limit)
        )
        return [(int(uid), int(total)) for uid, total in result.all()]


async def _fetch_names(registry: EngineRegistry, user_ids: list[int]) -> dict[int, str]:
    """SELECT user_id, first_name FROM users.users WHERE user_id IN (...).

    Returns a dict so the renderer can look up by id with one
    ``dict.get`` call and a deterministic fallback for misses.
    An empty input list returns an empty dict without touching the
    database — saves a no-op round-trip when the top list is empty
    (caught one layer up, but defence-in-depth costs us one if).
    """
    if not user_ids:
        return {}
    engine = registry.engine(DBName.USERS)
    async with engine.connect() as conn:
        result = await conn.execute(
            select(User.user_id, User.first_name).where(User.user_id.in_(user_ids))
        )
        return {int(uid): (name or "") for uid, name in result.all()}


def _render(top: list[tuple[int, int]], names: dict[int, str], lang: str) -> str:
    lines = [t("h_donaters_title", lang), ""]
    for idx, (uid, total) in enumerate(top, start=1):
        display = (names.get(uid) or "").strip() or t("h_donaters_default_name", lang, uid=uid)
        mention = html_user_mention(uid, display)
        lines.append(t("h_donaters_row", lang, idx=idx, mention=mention, total=total, coin=_COIN))
    return "\n".join(lines)


async def handle_donaters(message: Message, registry: EngineRegistry, *, lang: str) -> None:
    top = await _fetch_top(registry, message.chat.id)
    if not top:
        await message.reply(t("h_donaters_empty", lang))
        log.bind(chat_id=message.chat.id).debug("/donaters: empty")
        return
    names = await _fetch_names(registry, [uid for uid, _ in top])
    await message.reply(_render(top, names, lang))
    log.bind(chat_id=message.chat.id, rows=len(top), lang=lang).info("/donaters rendered")


def build_router(registry: EngineRegistry) -> Router:
    """Factory — group-only at the router level; a private DM gets the
    shared #123 group-only refusal from ``with_chat_type_refusal``
    below, as the module docstring says. Bare-form only
    (``magic=F.args.is_(None)``) —
    legacy ``/donaters`` ignores trailing args, but pinning bare here
    means a future ``/donaters @user`` syntax can be added without
    routing collisions with the existing leaderboard command.
    """
    router = Router(name="donaters")
    router.message.filter(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))

    async def _entry(message: Message, lang: str) -> None:
        await handle_donaters(message, registry, lang=lang)

    router.message.register(
        _entry,
        Command(
            "donaters",
            "донатеры",
            "donors",
            "kom_donaters",
            ignore_case=True,
            magic=F.args.is_(None),
        ),
    )
    return with_chat_type_refusal(router, scope="group")
