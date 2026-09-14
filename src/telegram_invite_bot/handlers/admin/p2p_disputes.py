"""``/admin_p2p_disputes`` — the P2P dispute work queue (#1687).

A P2P dispute freezes real money. ``mark_disputed`` moves a trade out
of ``pending``/``paid`` while the seller's COM stays in escrow, and
only an admin ruling (``resolve_dispute``) releases it — the D2 expiry
guard explicitly wants ``pending`` and will not touch a disputed row.
The only surface that ever showed a disputed trade was the card
``handle_dispute_open`` pushes to ``ADMIN_CHAT_ID`` at the moment the
dispute opens, and that card is fire-and-forget:

* it is not sent at all when ``ADMIN_CHAT_ID`` is unset
  (``config/settings.py`` defaults it to ``0``);
* ``_notify`` swallows every ``TelegramAPIError`` — by design, a
  blocked bot must not fail the money path — so a failed send is a log
  line and nothing more;
* a delivered card can still be deleted, or simply scroll away in a
  busy admin chat.

Any of those left the escrow frozen with no query in the tree able to
find it again. #1685 sharpened the cost: ``count_open_as_buyer`` now
counts ``disputed``, so a buyer holding unresolved disputes is also
locked out of the marketplace until someone rules.

This console closes that: it lists the queue oldest-first and re-issues
the SAME three resolution buttons the delivery-time card carried. The
markup is imported from :mod:`telegram_invite_bot.handlers.p2p_trade`
rather than rebuilt here precisely so the two surfaces cannot drift —
a button set that differed between them would resolve disputes
differently depending on which message the operator happened to tap.
The callbacks themselves are still handled (and developer-gated) in
that module; nothing about the resolution path is duplicated.

Same posture as every other ``/admin_*`` handler: silent drop for
non-developers, private-only at the router level (the cards carry both
parties' user IDs), and HTML-escaping on the one free-text field a
seller controls, ``fiat_currency``.

The header reports the untruncated backlog beside the page size. A
console that printed its page size as the total would be the same
silently-partial signal this module exists to remove.
"""

from __future__ import annotations

import html
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers.p2p_trade import _admin_resolve_markup
from telegram_invite_bot.i18n import t
from telegram_invite_bot.repositories.p2p_repo import P2pRepo
from telegram_invite_bot.utils.economy import format_age
from telegram_invite_bot.utils.numbers import format_amount_compact
from telegram_invite_bot.utils.time import db_now

if TYPE_CHECKING:
    from datetime import datetime

    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry
    from telegram_invite_bot.db.models.p2p import P2pTrade

log = logger.bind(component="handlers.admin.p2p_disputes")

# One Telegram message per disputed trade, so the page bound is a
# message-count bound too. Ten is generous for a backlog that should
# normally be empty and short of the flood limits if it is not.
PAGE_SIZE = 10


def _render_row(trade: P2pTrade, lang: str, *, now: datetime) -> str:
    """One re-issued resolution card.

    ``created_at`` is nullable and there is no ``disputed_at`` column,
    so the age shown is the TRADE's age, labelled as such. A row with
    no timestamp renders ``?`` rather than an invented duration — the
    same posture ``/admin_withdrawals`` takes.
    """
    created = trade.created_at
    age = format_age(now - created) if created is not None else "?"
    return t(
        "h_admin_p2p_dispute_row",
        lang,
        trade_id=int(trade.id),
        seller_id=int(trade.seller_id),
        buyer_id=int(trade.buyer_id),
        amount=format_amount_compact(float(trade.amount_com)),
        fiat=f"{trade.total_fiat:.2f}",
        currency=html.escape(trade.fiat_currency),
        age=age,
    )


async def _gather(registry: EngineRegistry) -> tuple[list[P2pTrade], int]:
    """``(page, backlog)`` on one read-only session — no commit."""
    engine = registry.engine(DBName.ECONOMY)
    async with AsyncSession(engine) as session:
        repo = P2pRepo(session)
        page = await repo.list_disputed(limit=PAGE_SIZE)
        total = await repo.count_disputed()
    return page, total


async def handle_admin_p2p_disputes(
    message: Message, settings: Settings, registry: EngineRegistry, lang: str
) -> None:
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_p2p_disputes; silently dropped"
        )
        return
    page, total = await _gather(registry)
    if not page:
        await message.answer(t("h_admin_p2p_disputes_empty", lang))
        log.bind(user_id=user.id).info("/admin_p2p_disputes rendered (empty)")
        return

    await message.answer(t("h_admin_p2p_disputes_header", lang, total=total, shown=len(page)))
    now = db_now()
    for trade in page:
        await message.answer(
            _render_row(trade, lang, now=now),
            reply_markup=_admin_resolve_markup(int(trade.id), lang),
        )
    log.bind(user_id=user.id, total=total, shown=len(page)).info("/admin_p2p_disputes rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    """Private-only: the cards carry both parties' Telegram user IDs."""
    router = Router(name="admin.p2p_disputes")
    router.message.filter(lambda m: m.chat.type == ChatType.PRIVATE)

    async def _entry(message: Message, lang: str) -> None:
        await handle_admin_p2p_disputes(message, settings, registry, lang)

    router.message.register(_entry, Command("admin_p2p_disputes", ignore_case=True))
    return router
