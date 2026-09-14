"""Value objects for shop + inventory display.

Domain entities (not ORM rows) so handlers can render them without
holding a live session. ``InventoryEntry`` is denormalised — it
carries the item name from ``shop_items`` so the handler doesn't
need a second query per row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Mapping


#: The ``shop_items.type`` values this bot can actually activate.
#:
#: #2006. Every other value falls to the trailing UNKNOWN branch of
#: :func:`~telegram_invite_bot.services.inventory_use_planner.plan_effect_application`,
#: and UNKNOWN is terminal for the *buyer*, not just for the planner:
#: the use service refuses BEFORE consuming, so a row bought under an
#: unhandled type sits in the inventory forever and nothing in the bot
#: can refund it. Production sells exactly one such SKU today —
#: ``legend``, seeded at 2000 coins by legacy ``init_default_items``
#: (bot.py:12411), dispatched by legacy (bot.py:13808) and never
#: ported. #2005 made the inventory card admit this to the buyer after
#: the fact; the point of this set is that there is no "after".
#:
#: The set is type-level on purpose. A ``luck`` row whose own ``data``
#: declares no usable payout span also classifies UNKNOWN, and the
#: planner documents that such a row is unsellable; catching it needs
#: a per-row plan rather than a type check, and the catalog-side
#: validation that would place it is an owner decision tracked
#: separately. Type-level is what removes the SKU prod actually sells.
#:
#: It lives in the entity module rather than beside the planner
#: because the two surfaces that must agree on it are a repository and
#: a service, and repositories may not import services. The planner
#: stays the authority all the same:
#: :mod:`tests.regression.test_shop_sells_only_what_it_can_activate`
#: reads its ``item_type == "..."`` branches out of the AST and fails
#: if this set and those branches ever drift apart.
ACTIVATABLE_ITEM_TYPES: Final[frozenset[str]] = frozenset(
    {
        "color_nick",
        "custom_title",
        "double_daily",
        "luck",
        "mute_protection",
        "unwarn",
        "vip",
        "xp_boost",
    }
)


@dataclass(frozen=True, slots=True)
class ShopItemEntity:
    id: int
    name: str
    description: str
    price: int
    type: str
    # ``-1`` means infinite (legacy convention). ``0`` is genuinely
    # out-of-stock; anything > 0 is the remaining count.
    stock: int
    # #192: the ``shop_items.data`` JSON blob, decoded. Legacy reads
    # every per-item parameter from here — a luck item's coin range, a
    # VIP row's term, an xp_boost's multiplier. Dropping it from the
    # entity is what forced the effect planner to key off item NAMES
    # instead, and the production catalog's names are not the ones the
    # planner was written against, so three paid-for SKUs silently did
    # nothing. Carrying the blob lets the planner read the operator's
    # own parameters rather than guess from a string.
    #
    # Default-empty so the ~40 construction sites that predate #192
    # (and every test fixture) keep working unchanged: an absent blob
    # is exactly what a row with ``data IS NULL`` means.
    #
    # NOTE: a dict field makes this dataclass unhashable despite
    # ``frozen=True``. Nothing hashes shop items — they are rendered
    # and pattern-matched, never used as dict keys or set members.
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InventoryEntry:
    """One purchased item, joined with its catalog name for display."""

    inventory_id: int
    item_name: str
    purchase_date: datetime
    used: bool
    # RR-2 #19: expiry timestamp (None = permanent), surfaced inline on
    # each /inventory row so the user sees what's about to lapse.
    expires: datetime | None = None


@dataclass(frozen=True, slots=True)
class InventoryDetail:
    """Single-entry inspect view for the Stage 26 inline-keyboard flow.

    Carries the same fields the list view shows plus ``expires`` and
    ``item_description`` so the detail card can surface info the list
    omits (long descriptions / expiry timestamps don't fit in a
    paginated list row). ``item_id`` is denormalised onto the entity
    so Stage 28's ``InventoryUseService`` can route the entry's
    catalog row to the effect planner without a second JOIN — the
    detail SELECT already touches ``shop_items`` for the name, so
    surfacing the id is a free byte on the wire. ``user_id`` is
    denormalised onto the entity
    so the handler can assert ``detail.user_id == callback.from_user.id``
    defensively even though the repo already constrains the SELECT to
    the requesting user — two checks for a private-data read are
    cheaper than the post-mortem for the first one being wrong.
    """

    inventory_id: int
    user_id: int
    item_id: int
    item_name: str
    item_description: str
    purchase_date: datetime
    used: bool
    expires: datetime | None


class PurchaseStatus(StrEnum):
    """Why a :class:`PurchaseOutcome` is what it is.

    String-valued so logger-bind/JSON-ish renderings stay readable.
    Each value maps to one user-facing message branch in the handler;
    adding a new branch means adding a new value here (rather than
    teaching the handler to pattern-match on a free-form string).
    """

    OK = "ok"
    ITEM_NOT_FOUND = "item_not_found"
    OUT_OF_STOCK = "out_of_stock"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    # #2006: the item exists and is affordable, but its ``type`` is not
    # in :data:`ACTIVATABLE_ITEM_TYPES`, so buying it would charge for
    # something that can never be used. Refused before any write —
    # unlike the three above it is a property of the catalog row, not
    # of the buyer, so retrying or topping up cannot change it.
    NO_EFFECT = "no_effect"


@dataclass(frozen=True, slots=True)
class PurchaseOutcome:
    """Result of a /buy attempt — success or a categorised failure.

    ``new_balance`` and ``new_stock`` are populated on success so the
    handler can render the post-purchase state without re-reading the
    DB (it'd see the same numbers anyway, the session has committed,
    but the round-trip is pointless). ``-1`` for ``new_stock`` keeps
    the "infinite" sentinel from :class:`ShopItemEntity`.
    """

    status: PurchaseStatus
    item: ShopItemEntity | None = None
    new_balance: int | None = None
    new_stock: int | None = None
    inventory_id: int | None = None
