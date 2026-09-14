"""``/mydonates`` — caller's own donation history + lifetime total.

Legacy ``/mydonates`` (bot.py:24963) is the user-side companion to
``/donaters``: private-DM only, shows the last N donations the
caller made across all groups plus a lifetime sum. The new pipeline
serves it with two ``economy.db`` reads (LEFT JOIN donations →
groups_donations for the group name) + i18n.

Behaviour parity & deltas:

* Private-DM only, enforced by a router-level filter. A group call
  used to return UNHANDLED on the theory that legacy's
  ``mydonates_private_only`` refusal would render it; the telebot
  bridge went away with T-011, so that was plain silence until #123
  gave the module a shared private-only refusal twin.
* Lifetime total via ``SELECT SUM(amount)`` (one query) rather than
  summing the in-memory page. Legacy does the same separately —
  paginating the history would otherwise misreport the total for
  users who donated more times than the page limit.
* Display cap of 25 rows mirrors legacy (bot.py:24976's
  ``get_my_donations(user_id, 25)``). The cap is rendering-only;
  the SUM still spans the user's entire ledger.
* Date format ``YYYY-MM-DD HH:MM``: legacy slices first 16 chars off
  the ISO string. We render through :func:`datetime.isoformat` and
  slice the same way so the visible shape is identical even when
  prod stores microseconds.
* ``html.escape`` on group names — operators set group_name from
  the chat title which is attacker-controllable. A title like
  ``<b>x</b>`` would otherwise render as raw markup under the
  bot-wide HTML parse_mode.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import func, select

from telegram_invite_bot.db.models.economy import Donation, GroupDonationsAggregate
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.i18n import t
from telegram_invite_bot.utils.aiogram import require_from_user
from telegram_invite_bot.utils.render import paginate_lines

if TYPE_CHECKING:
    from datetime import datetime

    from aiogram.types import Message

    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.services.user_service import UserService


log = logger.bind(component="handlers.mydonates")

_HISTORY_LIMIT = 25
_COIN = "🪙"


async def _fetch_total(registry: EngineRegistry, user_id: int) -> int:
    """``SELECT COALESCE(SUM(amount), 0) FROM donations WHERE user_id=?``.

    COALESCE so a user with zero donations gets ``0`` instead of
    ``None`` — the renderer would otherwise format ``None 🪙`` for the
    "lifetime total" line, which is the first visible number on the
    card and the one users glance at to decide whether to keep
    donating.
    """
    engine = registry.engine(DBName.ECONOMY)
    async with engine.connect() as conn:
        result = await conn.execute(
            select(func.coalesce(func.sum(Donation.amount), 0)).where(Donation.user_id == user_id)
        )
        return int(result.scalar_one())


async def _fetch_history(
    registry: EngineRegistry, user_id: int, limit: int = _HISTORY_LIMIT
) -> list[tuple[int, int, datetime | None, str | None]]:
    """LEFT JOIN to ``groups_donations`` for the human-readable name.

    LEFT (not INNER) so a donation to a group that has since been
    deleted from ``groups_donations`` still shows up — the
    ``group_id`` fallback ("Group <id>") is rendered downstream so
    the user's own history is never silently truncated by orphan
    rows on the join side.
    """
    engine = registry.engine(DBName.ECONOMY)
    async with engine.connect() as conn:
        result = await conn.execute(
            select(
                Donation.group_id,
                Donation.amount,
                Donation.created_at,
                GroupDonationsAggregate.group_name,
            )
            .outerjoin(
                GroupDonationsAggregate,
                GroupDonationsAggregate.group_id == Donation.group_id,
            )
            .where(Donation.user_id == user_id)
            .order_by(Donation.created_at.desc().nullslast())
            .limit(limit)
        )
        return [
            (int(group_id), int(amount), created_at, group_name)
            for group_id, amount, created_at, group_name in result.all()
        ]


def _format_date(value: datetime | None) -> str:
    """ISO ``YYYY-MM-DD HH:MM`` with no timezone suffix.

    Legacy renders ``created_at[:16].replace("T", " ")``. We match
    the slice exactly so screenshots of the new card line up with
    operator memory of legacy output. Missing ``created_at`` (rare
    but possible on legacy rows) renders as ``"—"`` — never blank.
    """
    if value is None:
        return "—"
    return value.isoformat(sep=" ", timespec="minutes")[:16]


def _render(
    lang: str,
    *,
    total: int,
    rows: list[tuple[int, int, datetime | None, str | None]],
) -> list[str]:
    """Render the card, split across messages when it cannot fit one.

    ``_HISTORY_LIMIT`` caps the row COUNT at 25, but nothing caps a
    row's width: the group name is the chat title, and Telegram allows
    128 characters of it — emoji included, which cost two UTF-16 units
    each against the 4096 ceiling. Twenty-five donations to groups with
    long names is 4095 characters of card, one under the limit, and a
    seven-digit amount or a single astral emoji takes it over. Past the
    ceiling ``answer`` comes back a 400 and the user, who ran this in
    their own DM, simply gets no reply.
    """
    import html as _html

    title = t("mydonates_title", lang)
    total_label = t("mydonates_total", lang)
    header = f"💸 <b>{title}</b>\n\n{total_label}: <b>{total}</b> {_COIN}\n"
    if not rows:
        return [f"{header}\n{t('mydonates_history_empty', lang)}"]
    lines: list[str] = []
    for group_id, amount, created_at, group_name in rows:
        name = _html.escape((group_name or "").strip() or f"Group {group_id}")
        lines.append(f"• {name} — {amount} {_COIN} ({_format_date(created_at)})")
    return paginate_lines(
        header,
        lines,
        more_line=lambda left: t("h_mydonates_history_more", lang, count=left),
    )


async def handle_mydonates(
    message: Message,
    user_service: UserService,
    registry: EngineRegistry,
    checkpoint: Checkpoint | None = None,
) -> None:
    user = await user_service.touch(require_from_user(message))
    # #220: the render below is paginated — one ``sendMessage`` per page,
    # sequentially. The ``touch`` above is bookkeeping that stands either
    # way, so end its transaction before the fan-out instead of holding
    # ``users.db``'s single writer slot through it. See
    # :class:`db.session.Checkpoint`.
    if checkpoint is not None:
        await checkpoint()
    total = await _fetch_total(registry, user.user_id)
    history = await _fetch_history(registry, user.user_id)
    for page in _render(user.language, total=total, rows=history):
        await message.answer(page)
    log.bind(uid=user.user_id, total=total, rows=len(history)).info("/mydonates rendered")


def build_router(registry: EngineRegistry) -> Router:
    """Private-DM filter at the router level; a group call gets the
    shared #123 private-only refusal from ``with_chat_type_refusal``
    below. (An older note here promised a fall-through to legacy —
    there is no legacy left to fall through to.)
    """
    router = Router(name="mydonates")
    router.message.filter(F.chat.type == ChatType.PRIVATE)

    async def _entry(
        message: Message,
        user_service: UserService,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_mydonates(message, user_service, registry, checkpoint)

    router.message.register(
        _entry,
        Command(
            "mydonates",
            "donates",
            "мои_донаты",
            ignore_case=True,
            magic=F.args.is_(None),
        ),
        F.from_user,
    )
    return with_chat_type_refusal(router, scope="private")
