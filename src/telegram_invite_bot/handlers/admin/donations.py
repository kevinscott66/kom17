"""``/admin_donations`` — operator's read-only donations dashboard.

The companion card to Stage 42's ``/admin_transactions``: where that
one shows the raw coin-movement ledger, this one focuses on the
``donations`` table — the append-only record of every ``/donate`` event
the legacy writer creates. Operators want this view first when a
group reports "the donation rating looks wrong" or when fraud-shaped
patterns surface (one user dumping the same amount across many
groups in rapid succession).

The card folds three things an operator would otherwise pull from
three SQL queries:

1. A summary line: total donation count + lifetime fiat-equivalent
   (well, raw COM sum — fiat conversion lives behind currency_manager
   which hasn't migrated, and a per-row sum across COM is the more
   useful operator-facing number anyway since legacy /donaters totals
   are computed in COM too).
2. Top 5 donor user_ids by lifetime COM contributed — cheap GROUP BY
   that surfaces the whales, which is the first cohort to scrutinise
   when something looks off.
3. Latest 5 donation rows newest-first — the "what happened in the
   last few minutes" view that matches the rest of the admin tree's
   tail-style cards.

Three slices in one card is the right granularity for the use case;
splitting into three commands would force the operator to retype
prefixes during an incident.

Same posture as every other ``/admin_*``:

* Silent-drop for non-devs.
* Private-only — donations expose user_id ↔ group_id pairs, and the
  free-text ``message`` column is user-typed via the legacy /donate
  form. Rendering in a group would leak both.
* HTML-escape on ``message`` is load-bearing (user-controlled).

Cap rationale: 5 + 5 keeps the card under ~1.5kB even with long
messages, well clear of Telegram's 4kB hard limit.
"""

from __future__ import annotations

import html
from datetime import datetime
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import func, select

from telegram_invite_bot.db.models.economy import Donation
from telegram_invite_bot.db.names import DBName

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.admin.donations")


_TOP_DONORS = 5
_RECENT_SAMPLE = 5
_MESSAGE_TRUNCATE = 30

# (user_id, lifetime_com)
_DonorRow = tuple[int, int]
# (id, user_id, group_id, amount, message, created_at)
_RecentRow = tuple[int, int, int, int, str | None, datetime | None]


async def _gather(
    registry: EngineRegistry,
) -> tuple[int, int, list[_DonorRow], list[_RecentRow]]:
    """Aggregate + top-donors + recent-sample in three separate
    ``connect()`` blocks.

    Three blocks rather than one — same reasoning as ``/admin_check_groups``
    and ``/admin_withdrawals``: a diagnostic isn't hot-path, failure of
    one slice shouldn't poison the others. If the recent-sample SELECT
    ever trips a corruption error, the operator still sees the summary
    line and top-donors — strictly more useful than a hard fail.
    """
    engine = registry.engine(DBName.ECONOMY)
    async with engine.connect() as conn:
        agg = (
            await conn.execute(
                select(
                    func.count(),
                    func.coalesce(func.sum(Donation.amount), 0),
                )
            )
        ).one()
        total_count = int(agg[0])
        # ``SUM`` on Integer in SQLite comes back as int, but
        # COALESCE with the 0 literal sometimes upgrades to int64 —
        # the explicit int() keeps the type stable.
        total_amount = int(agg[1] or 0)

    top: list[_DonorRow] = []
    if total_count > 0:
        async with engine.connect() as conn:
            rows = await conn.execute(
                select(
                    Donation.user_id,
                    func.sum(Donation.amount).label("lifetime"),
                )
                .group_by(Donation.user_id)
                .order_by(func.sum(Donation.amount).desc())
                .limit(_TOP_DONORS)
            )
            top = [(int(r[0]), int(r[1])) for r in rows.all()]

    recent: list[_RecentRow] = []
    if total_count > 0:
        async with engine.connect() as conn:
            rows = await conn.execute(
                select(
                    Donation.id,
                    Donation.user_id,
                    Donation.group_id,
                    Donation.amount,
                    Donation.message,
                    Donation.created_at,
                )
                .order_by(Donation.id.desc())
                .limit(_RECENT_SAMPLE)
            )
            recent = [(int(r[0]), int(r[1]), int(r[2]), int(r[3]), r[4], r[5]) for r in rows.all()]

    return total_count, total_amount, top, recent


def _truncate(s: str | None) -> str:
    """Truncate ``message`` to keep the recent-sample bullet readable.

    Same shape as ``/admin_withdrawals._truncate``: cap at N then add
    a horizontal-ellipsis. Operators familiar with that card see the
    same convention here without needing a separate mental model.
    """
    if not s:
        return ""
    if len(s) > _MESSAGE_TRUNCATE:
        return s[:_MESSAGE_TRUNCATE] + "…"
    return s


def _render(
    *,
    total_count: int,
    total_amount: int,
    top: list[_DonorRow],
    recent: list[_RecentRow],
) -> str:
    lines = ["💝 <b>Donations overview</b>", ""]
    lines.append(f"• count: <code>{total_count}</code>")
    lines.append(f"• lifetime DLAB: <code>{total_amount}</code>")
    if top:
        lines.append("")
        lines.append(f"<b>Top {len(top)} donors (lifetime DLAB):</b>")
        for uid, lifetime in top:
            lines.append(f"  • <code>{uid}</code> — <code>{lifetime}</code> DLAB")
    if recent:
        lines.append("")
        lines.append(f"<b>Latest {len(recent)} (newest first):</b>")
        for rid, uid, gid, amount, message, created in recent:
            msg_render = _truncate(message)
            msg_part = f" | {html.escape(msg_render)}" if msg_render else ""
            created_str = created.strftime("%Y-%m-%d %H:%M") if created else "—"
            lines.append(
                f"  • <code>#{rid}</code> uid=<code>{uid}</code> → "
                f"gid=<code>{gid}</code> <code>{amount}</code> DLAB"
                f"{msg_part} ({created_str})"
            )
    return "\n".join(lines)


async def handle_admin_donations(
    message: Message, settings: Settings, registry: EngineRegistry
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_donations; silently dropped"
        )
        return
    total_count, total_amount, top, recent = await _gather(registry)
    text = _render(
        total_count=total_count,
        total_amount=total_amount,
        top=top,
        recent=recent,
    )
    await message.answer(text)
    log.bind(
        user_id=user.id,
        count=total_count,
        total_amount=total_amount,
    ).info("/admin_donations rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    """Private-only at the router level — see module docstring on the
    user_id ↔ group_id privacy concern."""
    router = Router(name="admin.donations")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message) -> None:
        await handle_admin_donations(message, settings, registry)

    router.message.register(_entry, Command("admin_donations", ignore_case=True))
    return router
