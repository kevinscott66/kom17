"""``/admin_shop_prices`` — developer-only catalog dump.

Legacy ``/shop_prices`` (bot.py:34371) renders every row of
``economy.shop_items`` (including out-of-stock) as a price list for
operator audits — "did the last bulk-update overwrite a price?"
"is anything accidentally listed at 0?". Port lifts it onto
SQLAlchemy and the same dev-gating posture as the rest of the
``admin_*`` namespace.

Behaviour parity & deltas:

* Renamed ``/shop_prices`` → ``/admin_shop_prices`` to follow the
  new ``admin_*`` namespace convention. The short name was left with
  legacy, which answered it and incremented a migration counter
  surfaced by ``/admin_status``; T-011 removed both, so the old form
  now matches nothing and answers with silence. Same posture and same
  open question as ``/botstats`` — see ``handlers/admin/botstats.py``.
* Dev gating is the same silent-drop posture as ``/admin_botstats``:
  a non-dev gets no reply at all so the command name isn't a side
  channel for enumerating developer IDs. Chat-type gate runs AFTER
  the dev gate so the order doesn't leak existence.
* Legacy gated on ``admin_only`` (a broader role than "developer")
  but also rendered raw stock numbers and bulk-price snapshots —
  attacker-useful intel about which limited items are about to sell
  out. Narrowed to developers in the new path; a downgrade to
  per-group admin can be re-introduced later if a real workflow
  needs it. The silent-drop preserves enumeration defence for the
  intervening period.
* Display cap of 200 rows mirrors the legacy slice
  (``lines[:200]``). Catalog has historically been ~50 rows; the
  cap is defence-in-depth against a catastrophically over-seeded
  ``shop_items`` rendering a single message over Telegram's 4096
  byte cap.
* HTML rendering (``<b>``, ``<code>``) instead of legacy's Markdown.
  Bot-wide ``parse_mode=HTML`` makes backtick-Markdown render as
  literal text; new card matches the visual shape of
  ``/admin_botstats``.
* ``html.escape`` on ``name`` — admin handlers write the catalog
  via the legacy ``/shop_add`` flow with free-text input; an item
  named ``<b>spoof</b>`` would otherwise render as raw markup under
  the bot-wide HTML parse_mode and be visually indistinguishable
  from a real catalog header.
"""

from __future__ import annotations

import html as _html
from typing import TYPE_CHECKING

from aiogram import Router
from aiogram.filters import Command
from loguru import logger
from sqlalchemy import select

from telegram_invite_bot.core.entities.shop import ACTIVATABLE_ITEM_TYPES
from telegram_invite_bot.db.models.economy import ShopItem
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.i18n import t

if TYPE_CHECKING:
    from aiogram.types import Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import EngineRegistry


log = logger.bind(component="handlers.admin.shop_prices")

_DISPLAY_CAP = 200
_COIN = "🪙"


async def _fetch_items(registry: EngineRegistry) -> list[tuple[int, str, int, int | None, str]]:
    """``SELECT id, name, price, stock, type FROM shop_items ORDER BY id``.

    Returns tuples (no dataclass) — consumed once by the renderer.
    Ordering by ``id`` matches legacy's implicit insertion-order
    (sqlite returns rows in rowid order without an ORDER BY) and
    pins the output so a screenshot taken today still matches the
    same screenshot taken tomorrow on the same data.

    Deliberately not ``ShopItemsRepo`` — this is the operator's view
    and it must show the whole table, including the rows #2006 keeps
    out of ``/shop``. ``type`` is selected for exactly that: it is what
    decides whether a row is sellable, and without it the operator
    would have no way to tell a hidden item from a typo.
    """
    engine = registry.engine(DBName.ECONOMY)
    async with engine.connect() as conn:
        result = await conn.execute(
            select(
                ShopItem.id, ShopItem.name, ShopItem.price, ShopItem.stock, ShopItem.type
            ).order_by(ShopItem.id)
        )
        return [
            (int(item_id), name, int(price), None if stock is None else int(stock), item_type)
            for item_id, name, price, stock, item_type in result.all()
        ]


def _format_stock(stock: int | None) -> str:
    """Legacy uses ``"∞"`` for ``stock == -1`` (the prod sentinel for
    "infinite") and the raw integer otherwise. ``None`` is treated as
    infinite too: prod stock is NOT NULL DEFAULT -1 by the SQLAlchemy
    model, but mapped as ``int | None`` because the dump shows a few
    legacy NULLs from before the column was tightened.
    """
    if stock is None or stock == -1:
        return "∞"
    return str(stock)


def _render(items: list[tuple[int, str, int, int | None, str]], lang: str) -> str:
    if not items:
        return t("h_admin_shop_prices_empty", lang)
    lines: list[str] = [t("h_admin_shop_prices_title", lang), ""]
    for item_id, name, price, stock, item_type in items[:_DISPLAY_CAP]:
        safe_name = _html.escape(name)
        lines.append(f"#<code>{item_id}</code> — <b>{safe_name}</b>")
        lines.append(f"   💰 {price} {_COIN} | 📦 {_format_stock(stock)}")
        if item_type not in ACTIVATABLE_ITEM_TYPES:
            # #2006: this row is invisible in /shop and refuses to sell.
            # Without this line the operator sees a perfectly ordinary
            # catalog entry and no reason for the silence — the failure
            # mode the filter would otherwise introduce.
            lines.append(t("h_admin_shop_prices_unusable", lang, type=_html.escape(item_type)))
    return "\n".join(lines)


async def handle_admin_shop_prices(
    message: Message,
    settings: Settings,
    registry: EngineRegistry,
    *,
    lang: str,
) -> None:
    """Render the catalog iff dev + private chat. Gate order matches
    ``/admin_botstats`` — dev first, chat-type second — so a non-dev
    in any chat learns nothing about the command's existence.
    """
    user = message.from_user
    if user is None or not settings.bot.is_developer(user.id):
        log.bind(user_id=getattr(user, "id", None)).debug(
            "non-developer attempted /admin_shop_prices; silently dropped"
        )
        return
    if message.chat.type != "private":
        log.bind(user_id=user.id, chat_type=message.chat.type).debug(
            "/admin_shop_prices in non-private chat; silently dropped"
        )
        return

    items = await _fetch_items(registry)
    await message.answer(_render(items, lang))
    log.bind(user_id=user.id, rows=len(items), lang=lang).info("/admin_shop_prices rendered")


def build_router(settings: Settings, registry: EngineRegistry) -> Router:
    router = Router(name="admin.shop_prices")

    async def _entry(message: Message, lang: str) -> None:
        await handle_admin_shop_prices(message, settings, registry, lang=lang)

    router.message.register(_entry, Command("admin_shop_prices", ignore_case=True))
    return router
