"""``/admin_recent_signups`` — newest economy.users rows.

Companion to ``/admin_top_users``: that card ranks by balance to spot
the whales; this one ranks by ``registered`` DESC to spot signup
spikes. A row of fresh ``registered`` timestamps clustered inside a
few minutes is the classic shape of a raid attempt, a referral-farm
abuse pattern, or a misconfigured group that suddenly funneled
hundreds of users at the bot. Without this card the operator has to
SSH to the host and query SQLite by hand to see the pattern; with it,
``/admin_recent_signups`` answers the question in one prefix.

The card surfaces three columns per row:

* ``user_id`` — the entity to investigate. Operators copy this into
  ``/admin_top_users`` follow-ups (or whatever per-user tool lands
  next) when a row looks suspicious.
* ``registered`` — the timestamp. Clustering on this column is the
  whole point of the card; ``%Y-%m-%d %H:%M:%S`` (full seconds, not
  truncated to minutes like /admin_top_users) so an operator can
  spot sub-minute bursts.
* ``referred_by`` — the inviter user_id, or em-dash if direct. Raid
  signups commonly share a referrer; surfacing this column lets the
  operator see the pattern without a second query.

Same posture as every other ``/admin_*``:

* Silent-drop for non-devs.
* Private-only at the router level — registration timestamps tied to
  user_ids are PII; rendering in a group exposes when individual
  users joined.
* No HTML-escape needed on the rendered columns — all three are
  int/datetime, no user-controlled string fields are surfaced.

10 rows, same cap as the other tail cards. NULL ``registered`` rows
(legacy data from before the column was added) are excluded from the
query so the card always shows rows where the signal is meaningful.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import select

from telegram_invite_bot.db.models.economy import EconomyUser
from telegram_invite_bot.db.names import DBName

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.admin.recent_signups")


_LIMIT = 10

# (user_id, registered, referred_by)
_Row = tuple[int, datetime, int | None]


async def _gather(registry: EngineRegistry) -> list[_Row]:
    """Latest 10 rows where ``registered IS NOT NULL``.

    The NOT-NULL filter is deliberate: legacy economy.users rows from
    before the ``registered`` column existed have NULL here, and they
    would either pollute the head of the list (if NULLs sort last and
    we sort DESC, SQLite actually sorts NULLs LAST by default on DESC
    — but the behaviour varies across versions) or push the
    interesting rows down. The card's only useful answer is "what
    are the most recent signups WITH a real timestamp"; rows without
    a timestamp can't contribute to spike detection anyway, so
    excluding them at the SQL level is the right call.
    """
    engine = registry.engine(DBName.ECONOMY)
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(
                EconomyUser.user_id,
                EconomyUser.registered,
                EconomyUser.referred_by,
            )
            .where(EconomyUser.registered.is_not(None))
            .order_by(EconomyUser.registered.desc())
            .limit(_LIMIT)
        )
        return [(int(r[0]), r[1], (int(r[2]) if r[2] is not None else None)) for r in rows.all()]


def _render(rows: list[_Row]) -> str:
    if not rows:
        return "🆕 <b>Recent signups</b>\n\n<i>No rows with a registered timestamp.</i>"
    lines = ["🆕 <b>Recent signups</b>", ""]
    lines.append(f"<i>Latest {len(rows)} by registered DESC:</i>")
    lines.append("")
    for uid, registered, referred_by in rows:
        # Full seconds — the whole point of this card is spotting
        # sub-minute signup clusters during raids. Truncating to
        # minutes (as /admin_top_users does) would hide exactly the
        # pattern this card exists to expose.
        registered_str = registered.strftime("%Y-%m-%d %H:%M:%S")
        ref_str = f"<code>{referred_by}</code>" if referred_by else "—"
        lines.append(
            f"  • <code>{uid}</code> at <code>{registered_str}</code> ← ref=<i>{ref_str}</i>"
        )
    return "\n".join(lines)


async def handle_admin_recent_signups(
    message: Message, settings: Settings, registry: EngineRegistry
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_recent_signups; silently dropped"
        )
        return
    rows = await _gather(registry)
    await message.answer(_render(rows))
    log.bind(user_id=user.id, count=len(rows)).info("/admin_recent_signups rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    router = Router(name="admin.recent_signups")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_recent_signups(message, settings, registry)

    router.message.register(_entry, Command("admin_recent_signups", ignore_case=True))
    return router
