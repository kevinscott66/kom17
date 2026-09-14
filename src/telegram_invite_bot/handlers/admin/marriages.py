"""``/admin_marriages`` — cross-chat marriages diagnostic.

The user-side ``/marriages`` command (already migrated in Stage 19)
shows the active marriage leaderboard *within a single chat*. That
view is the user's product surface — friends comparing XP scores in
their group. The operator question is different and inherently
cross-chat: *"how many marriages exist across the whole deployment,
which chats have the most, and is anything off?"*

The card folds two answers an operator would otherwise pull from
two queries:

1. Aggregate header — total active marriages (status NULL or
   ``active``, mirroring the legacy WHERE clause at bot.py:22991)
   plus the distinct count of chats that host at least one.
2. Top 5 chats by active-marriage count. A single chat carrying
   80% of the marriage population is a strong signal (community
   health hotspot, or a script-driven inflation pattern depending
   on how it grew) and worth surfacing.

Same posture as every other ``/admin_*``:

* Silent-drop for non-devs (existence is not a side-channel for
  enumerating dev IDs).
* Private-only at the router level — chat-level counts aren't PII
  on their own, but the per-chat ranking can identify which
  communities the operator's deployment serves. Operators
  themselves already know that; non-operators in a shared admin
  group shouldn't see it.
* No user-controlled string columns are surfaced (we render
  ``chat_id`` and counts only), so no HTML-escape is needed.

The "status NULL or 'active'" predicate is the load-bearing part:
legacy code at bot.py:22991 treats both as active, and a row written
by the legacy ``/marry_accept`` path leaves ``status`` NULL. If we
filtered on ``status == 'active'`` only, the card would understate
the count by however many rows the legacy writer hasn't backfilled —
typically the majority on a fresh deployment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import ColumnElement, func, or_, select

from telegram_invite_bot.db.models.users import Marriage
from telegram_invite_bot.db.names import DBName

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.admin.marriages")


_TOP_CHATS = 5

# (chat_id, count)
_ChatRow = tuple[int, int]


def _active_filter() -> ColumnElement[bool]:
    """The legacy 'is active' predicate.

    Defined in one place so a future change to the active-row
    convention (e.g. introducing a new status value) updates both
    the aggregate query and the per-chat group-by query at once —
    a drift between the two would surface as a total that doesn't
    match the sum of the per-chat counts.
    """
    return or_(Marriage.status.is_(None), Marriage.status == "active")


async def _gather(
    registry: EngineRegistry,
) -> tuple[int, int, list[_ChatRow]]:
    """Aggregate counts + top-chats GROUP BY.

    Two separate ``connect()`` blocks. The aggregate query is cheap
    enough to share a connection with the top-chats query, but the
    split matches the rest of the admin tree's "one signal per
    block, isolated failure modes" convention.

    Marriages live on the USERS engine (legacy ``users.db`` carries
    marriages + relationships alongside the user/group tables), not
    on ECONOMY.
    """
    engine = registry.engine(DBName.USERS)
    async with engine.connect() as conn:
        agg = (
            await conn.execute(
                select(
                    func.count(),
                    func.count(func.distinct(Marriage.chat_id)),
                ).where(_active_filter())
            )
        ).one()
        total_active = int(agg[0])
        chats_with_marriages = int(agg[1])

    top: list[_ChatRow] = []
    if total_active > 0:
        async with engine.connect() as conn:
            rows = await conn.execute(
                select(
                    Marriage.chat_id,
                    func.count().label("n"),
                )
                .where(_active_filter())
                .group_by(Marriage.chat_id)
                .order_by(func.count().desc(), Marriage.chat_id.asc())
                .limit(_TOP_CHATS)
            )
            top = [(int(r[0]), int(r[1])) for r in rows.all()]

    return total_active, chats_with_marriages, top


def _render(*, total_active: int, chats_with_marriages: int, top: list[_ChatRow]) -> str:
    lines = ["💍 <b>Marriages overview</b>", ""]
    lines.append(f"• active: <code>{total_active}</code>")
    lines.append(f"• distinct chats: <code>{chats_with_marriages}</code>")
    if top:
        lines.append("")
        lines.append(f"<b>Top {len(top)} chats by active count:</b>")
        for chat_id, n in top:
            lines.append(f"  • <code>{chat_id}</code> — <code>{n}</code> active")
    return "\n".join(lines)


async def handle_admin_marriages(
    message: Message, settings: Settings, registry: EngineRegistry
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_marriages; silently dropped"
        )
        return
    total_active, chats_with_marriages, top = await _gather(registry)
    text = _render(
        total_active=total_active,
        chats_with_marriages=chats_with_marriages,
        top=top,
    )
    await message.answer(text)
    log.bind(
        user_id=user.id,
        active=total_active,
        chats=chats_with_marriages,
    ).info("/admin_marriages rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    router = Router(name="admin.marriages")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_marriages(message, settings, registry)

    router.message.register(_entry, Command("admin_marriages", ignore_case=True))
    return router
