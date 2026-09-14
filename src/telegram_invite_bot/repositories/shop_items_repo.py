"""Read-only catalog access for ``economy.shop_items``.

Stage 16 surface: list the whole catalog ordered by id (legacy order).
``/buy`` (Stage 17) will add ``get_by_id`` and ``get_by_name_like``
plus a transactional ``purchase`` method that decrements stock under
the same session as the wallet debit.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from telegram_invite_bot.core.entities.shop import ACTIVATABLE_ITEM_TYPES, ShopItemEntity
from telegram_invite_bot.db.models.economy import ShopItem

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


def decode_item_data(raw: str | None) -> dict[str, Any]:
    """Decode a ``shop_items.data`` blob, tolerating anything.

    #192: the column is operator-writable free-form JSON — legacy
    ``/admin_shop`` puts whatever it likes there, and production rows
    carry unicode escapes. A malformed blob must degrade to "this
    item has no parameters" rather than break the whole catalog
    listing, so every decode failure collapses to ``{}``. A non-object
    top level (a bare list, a number) is treated the same way: the
    planner only ever asks for named keys.
    """
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def shop_item_to_entity(row: ShopItem) -> ShopItemEntity:
    # ``stock=None`` happens in old rows seeded before the column was
    # added. Legacy collapses to -1 ("infinite") via COALESCE; we do
    # the same so display logic doesn't have to special-case NULL.
    stock = -1 if row.stock is None else row.stock
    return ShopItemEntity(
        id=row.id,
        name=row.name,
        description=row.description or "",
        price=row.price,
        type=row.type,
        stock=stock,
        data=decode_item_data(row.data),
    )


class ShopItemsRepo:
    """``economy.shop_items`` reader."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(
        self, *, include_out_of_stock: bool = False, include_unusable: bool = False
    ) -> list[ShopItemEntity]:
        """Return catalog rows, ordered by ``price`` ASC (legacy order).

        ``ORDER BY price`` is the legacy convention (``bot.py:12445``,
        ``bot.py:12448``) so users see cheapest first — matters for
        discovery; ordering by ``id`` would surface admin-import order,
        which has no meaning to a player.

        By default sold-out rows (``stock == 0``) are hidden, matching
        ``ShopManager.get_all_items(include_out_of_stock=False)`` —
        legacy's ``/shop`` would otherwise show items a user cannot
        buy. ``stock == -1`` ("infinite") and any positive count stay
        visible. Pass ``include_out_of_stock=True`` for admin views
        (legacy /admin_shop, future stage).

        #2006 adds the second half of the same argument. A sold-out
        row is hidden because the user cannot buy it; a row whose
        ``type`` is not in :data:`ACTIVATABLE_ITEM_TYPES` is hidden
        because the user cannot *use* it — the effect planner has no
        branch for it, so the use flow refuses before consuming and the
        coins are gone for good. Pass ``include_unusable=True`` for the
        operator-facing views that need to see the whole catalog.

        No pagination here — the catalog is < 50 items in prod. When
        that ceiling lifts (or when we port the inline-keyboard
        SHOP_PAGE_SIZE flow from legacy) the handler will paginate;
        the repo stays a thin SELECT.
        """
        stmt = select(ShopItem).order_by(ShopItem.price)
        if not include_out_of_stock:
            # ``stock IS NULL`` collapses to -1 ("infinite") via
            # ``shop_item_to_entity``; we mirror the legacy WHERE clause exactly
            # so the same set of rows is visible.
            stmt = stmt.where((ShopItem.stock != 0) | (ShopItem.stock.is_(None)))
        if not include_unusable:
            stmt = stmt.where(ShopItem.type.in_(ACTIVATABLE_ITEM_TYPES))
        result = await self._session.execute(stmt)
        return [shop_item_to_entity(row) for row in result.scalars()]

    async def list_by_type(
        self,
        item_type: str,
        *,
        include_out_of_stock: bool = False,
        include_unusable: bool = False,
    ) -> list[ShopItemEntity]:
        """Return catalog rows of a single ``type``, ordered by ``price`` ASC.

        ``/vip_shop`` (and any future type-scoped surface like an
        "emoji shop") needs the catalog filtered to one ``shop_items.type``
        without re-implementing the price-ordering and out-of-stock
        semantics :meth:`list_all` already owns. Legacy renders the VIP
        plans by reading the whole catalog and filtering on ``type`` in
        Python (``bot.py`` VIP-shop render); pushing the filter into the
        WHERE clause keeps the handler thin and avoids pulling the full
        catalog over the session just to drop most of it.

        ``stock`` semantics mirror :meth:`list_all` exactly: sold-out
        rows (``stock == 0``) are hidden by default, ``stock IS NULL``
        collapses to -1 ("infinite") via ``shop_item_to_entity``, and the same
        ``(stock != 0) OR (stock IS NULL)`` predicate is reused so the
        two surfaces never diverge on what counts as "buyable". #2006's
        ``include_unusable`` is reused for the same reason: a caller
        that names one ``type`` is still asking what a user can buy,
        and an unusable type answered here but hidden in
        :meth:`list_all` would be exactly the divergence this paragraph
        exists to prevent.
        """
        stmt = select(ShopItem).where(ShopItem.type == item_type).order_by(ShopItem.price)
        if not include_out_of_stock:
            stmt = stmt.where((ShopItem.stock != 0) | (ShopItem.stock.is_(None)))
        if not include_unusable:
            stmt = stmt.where(ShopItem.type.in_(ACTIVATABLE_ITEM_TYPES))
        result = await self._session.execute(stmt)
        return [shop_item_to_entity(row) for row in result.scalars()]

    async def get(self, item_id: int) -> ShopItemEntity | None:
        """Single-row lookup by primary key — Stage 24 callback prep.

        ``PurchaseService._fetch_item`` already does an identical SELECT
        but inside its commit boundary; the inline-buy prompt callback
        needs the item BEFORE deciding whether to render a confirmation
        card (or answer with an out-of-stock / not-found toast), and
        re-using the service's private helper would couple the handler
        to the service's internals. A thin read on the repo keeps the
        purchase-vs-display surfaces independent — the service still
        re-reads inside its transaction, so a stale ``stock`` between
        prompt and confirm is caught by the rowcount guards, not by
        the handler trusting this read.

        Includes sold-out rows on purpose. The prompt handler decides
        how to surface ``stock == 0`` (a toast, not a confirmation
        card), and filtering at the repo would hide the distinction
        between "no such item" and "item exists but sold out" — the
        two cases get different user-visible copy. #2006's unusable
        rows are included for the identical reason: ``PurchaseService``
        refuses them with their own status, and a handler that could
        not tell them from a missing id would have to guess at the
        copy.
        """
        row = await self._session.get(ShopItem, item_id)
        return shop_item_to_entity(row) if row is not None else None
