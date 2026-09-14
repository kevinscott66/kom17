"""``/admin_top_users`` — top economy users by balance.

A new operator diagnostic, no exact legacy counterpart. The legacy
admin tree has a "user list" view buried inside the FSM panel that
shows pagination + click-through to per-user actions, but during an
incident the operator wants the answer immediately: *"who are the
top-balance accounts right now, when were they last seen, and is
anything obviously suspicious?"*

This card is the read-only answer. It joins three signals an operator
naturally correlates by eye:

* **balance** — current COM holdings, the primary "is this account
  worth scrutinising" sort key.
* **last_seen** — recency. A 10-million-COM account that last logged
  in two years ago is far less interesting than the same account
  that pinged the bot an hour ago.
* **language** — quick cohort hint; useful when triaging reports from
  a specific community (the RU vs EN split correlates with which
  group cluster a user belongs to).

10 rows by ``balance`` DESC. Rationale matches every other admin tail
card: enough to spot a pattern, small enough to scan without scroll.

Same posture as every other ``/admin_*``:

* Silent-drop for non-devs.
* Private-only at the router level — balances are sensitive
  per-user data. Rendering a top-10 leaderboard in a group exposes
  who has the most COM, which is exactly the surface that attracts
  social-engineering attempts on those users.
* HTML-escape on ``language`` — pure defence-in-depth: the column is
  populated by code paths today, but the schema allows arbitrary
  TEXT and a future admin tool could let an operator type into it.

NULL ``last_seen`` renders as em-dash — matches the convention from
``/admin_transactions``. The legacy economy.users table has rows from
before ``last_seen`` was added; without the em-dash convention those
would show as ``"None"`` and trigger confused operator pings.
"""

from __future__ import annotations

import html
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


log = logger.bind(component="handlers.admin.top_users")


_LIMIT = 10

# (user_id, balance, language, last_seen)
_Row = tuple[int, int, str, datetime | None]


async def _gather(registry: EngineRegistry) -> list[_Row]:
    """Top-balance users via ``balance DESC`` + ``user_id ASC`` tiebreak.

    The secondary sort matters: ties on balance are common in the
    legacy data (lots of accounts sitting at the default 100 COM
    that the legacy register_user path seeds). Without a deterministic
    tiebreak the test pinning order would flap across SQLite versions.
    ``user_id ASC`` is the legacy implicit order and the cheapest
    column to break on (primary-key index).
    """
    engine = registry.engine(DBName.ECONOMY)
    async with engine.connect() as conn:
        rows = await conn.execute(
            select(
                EconomyUser.user_id,
                EconomyUser.balance,
                EconomyUser.language,
                EconomyUser.last_seen,
            )
            .order_by(
                EconomyUser.balance.desc(),
                EconomyUser.user_id.asc(),
            )
            .limit(_LIMIT)
        )
        return [(int(r[0]), int(r[1]), r[2], r[3]) for r in rows.all()]


def _render(rows: list[_Row]) -> str:
    if not rows:
        return "👥 <b>Top users by balance</b>\n\n<i>economy.users is empty.</i>"
    lines = ["👥 <b>Top users by balance</b>", ""]
    lines.append(f"<i>Top {len(rows)} by DLAB balance:</i>")
    lines.append("")
    for rank, (uid, balance, lang, last_seen) in enumerate(rows, 1):
        last_seen_str = last_seen.strftime("%Y-%m-%d %H:%M") if last_seen else "—"
        lang_str = html.escape(lang) if lang else "—"
        lines.append(
            f"{rank}. <code>{uid}</code> — "
            f"<code>{balance}</code> DLAB "
            f"[<i>{lang_str}</i>] "
            f"last_seen=<code>{last_seen_str}</code>"
        )
    return "\n".join(lines)


async def handle_admin_top_users(
    message: Message, settings: Settings, registry: EngineRegistry
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_top_users; silently dropped"
        )
        return
    rows = await _gather(registry)
    await message.answer(_render(rows))
    log.bind(user_id=user.id, count=len(rows)).info("/admin_top_users rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    """Private-only — see module docstring on the per-user-balance
    privacy concern."""
    router = Router(name="admin.top_users")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_top_users(message, settings, registry)

    router.message.register(_entry, Command("admin_top_users", ignore_case=True))
    return router
