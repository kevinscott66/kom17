"""``/admin_relations`` — cross-chat relationships diagnostic.

Sibling card to :mod:`admin.marriages`. Legacy stores the two
"pair" features in separate tables (``marriages`` and
``relationships``) with different XP curves and lifecycle
columns — but the operator question is the same shape:
*"how many active pairs across all chats, and which chats host
most of them?"*. We render the answer in the same layout so an
operator running both cards side-by-side can compare the two
features at a glance.

The load-bearing detail is the same as marriages: legacy treats
``status IS NULL`` AND ``status = 'active'`` as both meaning
active (every bot.py SELECT on relationships uses the
``status IS NULL OR status = 'active'`` predicate — see e.g.
bot.py:21920, 21963). A "cleaner" ``status = 'active'`` filter
would silently halve the count on real data, where the legacy
writer at bot.py:22036 leaves ``status`` NULL on insert. The
predicate is extracted into :func:`_active_filter` so the
aggregate query and the per-chat GROUP BY can't drift.

Posture matches every other ``/admin_*``:

* Silent-drop for non-devs (no enumeration of dev IDs).
* Private-only at the router level — per-chat ranking
  identifies which communities the deployment serves; that's
  fine for the operator but not in a shared admin group.
* No user-controlled string columns surfaced (chat_id + counts
  only), so no HTML-escape needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import ColumnElement, func, or_, select

from telegram_invite_bot.db.models.users import Relationship
from telegram_invite_bot.db.names import DBName

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.admin.relations")


_TOP_CHATS = 5

# (chat_id, count)
_ChatRow = tuple[int, int]


def _active_filter() -> ColumnElement[bool]:
    """Legacy active-row predicate (NULL or 'active').

    Single source-of-truth so the aggregate and per-chat queries
    can't drift on what counts as active — a drift would surface
    as a total that doesn't match the sum of the per-chat counts,
    exactly the regression hardest to spot in a numeric diagnostic.
    """
    return or_(Relationship.status.is_(None), Relationship.status == "active")


async def _gather(
    registry: EngineRegistry,
) -> tuple[int, int, list[_ChatRow]]:
    """Aggregate + top-chats GROUP BY.

    Same two-block layout as :mod:`admin.marriages` — one signal
    per ``connect()``, isolated failure modes. Relationships
    live on the USERS engine (legacy ``users.db`` carries
    marriages + relationships together).
    """
    engine = registry.engine(DBName.USERS)
    async with engine.connect() as conn:
        agg = (
            await conn.execute(
                select(
                    func.count(),
                    func.count(func.distinct(Relationship.chat_id)),
                ).where(_active_filter())
            )
        ).one()
        total_active = int(agg[0])
        chats_with_relations = int(agg[1])

    top: list[_ChatRow] = []
    if total_active > 0:
        async with engine.connect() as conn:
            rows = await conn.execute(
                select(
                    Relationship.chat_id,
                    func.count().label("n"),
                )
                .where(_active_filter())
                .group_by(Relationship.chat_id)
                .order_by(func.count().desc(), Relationship.chat_id.asc())
                .limit(_TOP_CHATS)
            )
            top = [(int(r[0]), int(r[1])) for r in rows.all()]

    return total_active, chats_with_relations, top


def _render(*, total_active: int, chats_with_relations: int, top: list[_ChatRow]) -> str:
    lines = ["💞 <b>Relationships overview</b>", ""]
    lines.append(f"• active: <code>{total_active}</code>")
    lines.append(f"• distinct chats: <code>{chats_with_relations}</code>")
    if top:
        lines.append("")
        lines.append(f"<b>Top {len(top)} chats by active count:</b>")
        for chat_id, n in top:
            lines.append(f"  • <code>{chat_id}</code> — <code>{n}</code> active")
    return "\n".join(lines)


async def handle_admin_relations(
    message: Message, settings: Settings, registry: EngineRegistry
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_relations; silently dropped"
        )
        return
    total_active, chats_with_relations, top = await _gather(registry)
    text = _render(
        total_active=total_active,
        chats_with_relations=chats_with_relations,
        top=top,
    )
    await message.answer(text)
    log.bind(
        user_id=user.id,
        active=total_active,
        chats=chats_with_relations,
    ).info("/admin_relations rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    router = Router(name="admin.relations")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_relations(message, settings, registry)

    router.message.register(_entry, Command("admin_relations", ignore_case=True))
    return router
