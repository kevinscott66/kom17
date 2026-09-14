"""``/shop`` + ``/inventory`` + ``/buy`` — Stages 16-17 + 24.

* ``/shop`` (Stage 16) — list every catalog row with name, price,
  stock hint, and a ``/buy <id>`` hint per row. Also accepts
  ``/kom_shop`` — legacy registers ``commands=['shop', 'kom_shop']``
  and the group-welcome quick-links message points users at
  ``/kom_shop`` specifically.

  Stage 24 grew the rendering: each visible row now carries an inline
  "🛒 Купить" button alongside its ``/buy <id>`` text hint. The text
  hint is preserved for users on clients that hide inline keyboards
  (rare, but legacy users learnt the slash form). The catalog
  PAGINATES at :data:`_PAGE_SIZE`: body text and keyboard slice the
  same window, and the rest of the catalog is reached with the nav row
  rather than by falling back to the slash form. This description used
  to be the Stage 24 one — a hard cap on the keyboard with the body
  still listing every row — which is the arrangement the ``_PAGE_SIZE``
  comment below calls the worst of both worlds and says Stage 25 left
  behind. ``/buy <id>`` remains the universal fallback, not the
  overflow path.

* ``/inventory`` (Stage 16, alias ``/inv``) — last 30 unused, unexpired
  purchases for the caller, newest first.

* ``/buy <item_id>`` (Stage 17) — atomic shop purchase. Legacy has no
  slash command for this (it's callback-only via inline keyboards on
  /shop), but our /shop output advertises ``/buy <id>`` so the slash
  is the entry point users see.

Stage 24 ports the legacy ``shop_confirm_buy_*`` callback ladder to
three :class:`CallbackData` factories
(:class:`ShopBuyPrompt` / :class:`ShopBuyConfirm` / :class:`ShopBuyCancel`).
Three callback handlers in this module share the message router's
:class:`EconomyMiddleware`, so the prompt-confirm-execute sequence
all runs against fresh ``economy.db`` sessions and reaches the same
:class:`PurchaseService` the slash ``/buy`` already uses. The
post-purchase auto-apply effects (VIP / luck-gift / color_nick /
double_daily) STILL live in legacy — porting those is a per-effect
stage, not a callback-shape stage. A new-pipeline purchase produces
an inventory row identical to a legacy purchase, so the legacy
auto-apply continues to fire on the unused row at /inventory use
time without any cross-boundary state shared.

Buy-via-name fuzzy lookup is not in this stage — the slash form is
``/buy <numeric_id>`` only. Fuzzy lookup adds an ambiguity-resolution
turn that doesn't fit a single message exchange.

Private chats only (matches the legacy modular handler — group
``/shop`` in legacy spat 60 lines into the chat, which we won't
reproduce on purpose). Group ``/shop`` / ``/buy`` used to fall through
to legacy; since T-011 they are answered by the #123 refusal twin
(:func:`~handlers.chat_scope.with_chat_type_refusal`), which is the
router-level case this module is named as an example of.

Rate limit on /buy callbacks
---------------------------
Per-user rate limiting on the confirm step is deliberately NOT added
here, and a per-user throttle would be the wrong shape anyway: it would
only swap one user-visible copy for another (a "slow down" toast for a
"not enough coins" card), and would have to be tuned per-item-price
rather than per-user-second.

What IS enforced is one purchase per confirm card (#1761). This section
used to argue that :class:`PurchaseService`'s rowcount guards (the
``WHERE balance >= price`` and ``WHERE stock > 0`` clauses) already made
a rage-click safe. They don't: they refuse the second debit only for a
buyer who could not afford two anyway. A buyer with the coins got
charged twice for a card that offered one item. ``ShopBuyConfirm``
carries no FSM state and no nonce — the packed ``item_id`` alone is a
complete, replayable purchase order — so there is nothing for a lock to
consume, and serialising two taps just makes them buy twice in order.
``handle_shop_buy_confirm`` therefore claims the *card identity*
``(chat_id, message_id)`` in ``_spent_cards`` before the money moves;
the guards remain the second line of defence for the genuinely
concurrent case across two different cards.
"""

from __future__ import annotations

import contextlib
import html
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import Message as MessageType  # runtime — isinstance guard
from loguru import logger
from sqlalchemy.exc import SQLAlchemyError

from telegram_invite_bot.core.entities.shop import PurchaseStatus
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.fsm.custom_title import CustomTitleStates
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.handlers.custom_title import (
    PENDING_ENTRY_FIELD,
    handle_custom_title_text,
)
from telegram_invite_bot.handlers.fsm_text import NOT_A_COMMAND, register_text_expected
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders import (
    InventoryBack,
    InventoryInspect,
    InventoryPage,
    InventoryUse,
    ShopBuyCancel,
    ShopBuyConfirm,
    ShopBuyPrompt,
    ShopGroupMenu,
    ShopGroupPick,
    ShopPage,
)
from telegram_invite_bot.keyboards.builders.pagination import build_pagination_nav
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.repositories.bot_groups_repo import BotGroupsRepo
from telegram_invite_bot.services.group_donation_service import GroupDonationOutcome
from telegram_invite_bot.services.inventory_use_planner import (
    InventoryEffectKind,
    plan_effect_application,
)
from telegram_invite_bot.services.inventory_use_service import UseOutcome
from telegram_invite_bot.services.purchase_service import PurchaseService
from telegram_invite_bot.services.referral_commission_service import (
    purchase_commission_amount,
)
from telegram_invite_bot.utils.aiogram import (
    UndeliverableResultError,
    reply_or_send,
    require_from_user,
)
from telegram_invite_bot.utils.numbers import format_number, parse_int_token
from telegram_invite_bot.utils.telegram_admin import chat_creator_id
from telegram_invite_bot.utils.ttl_lru_cache import TTLLRUCache

log = logger.bind(component="handlers.shop")

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext
    from aiogram.types import CallbackQuery, Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.core.entities.shop import (
        InventoryDetail,
        InventoryEntry,
        PurchaseOutcome,
        ShopItemEntity,
    )
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.inventory_repo import InventoryRepo
    from telegram_invite_bot.repositories.shop_items_repo import ShopItemsRepo
    from telegram_invite_bot.services.group_donation_service import GroupDonationService
    from telegram_invite_bot.services.inventory_use_service import InventoryUseService
    from telegram_invite_bot.services.purchase_service import PurchaseService


# Page size for /shop. Telegram itself accepts up to 100 inline-keyboard
# rows per markup, but a 60-button column on a phone screen is
# user-hostile; legacy paginated at 8/page (bot.py SHOP_PAGE_SIZE) and
# Stage 24 mirrored the same first-page size as a hard cap without
# wiring the page-flip callbacks. Stage 25 turns the cap back into a
# proper page size: both the body text AND the keyboard now slice on
# the same window so the two surfaces stay in lockstep — paginating
# the keyboard while leaving the body listing every row would surface
# items the user can't see a button for, which is the worst of both
# worlds for discoverability. The full catalog is reachable across
# pages via the nav row; users past page 0 still have the ``/buy <id>``
# text form as the universal fallback.
_PAGE_SIZE = 8
# Stage 26: /inventory paginates at the same 8/page as /shop. Kept as a
# distinct constant rather than aliasing _PAGE_SIZE so a future tuning
# pass on one surface (e.g. inventory shrinking to 5/page because the
# detail buttons render larger labels than buy buttons) can move
# independently of the shop knob.
_INVENTORY_PAGE_SIZE = 8


def _page_count(total: int, page_size: int = _PAGE_SIZE) -> int:
    """How many pages a ``total``-item collection occupies at ``page_size``.

    An empty collection never reaches this helper (callers short-
    circuit with the empty-state message), but a 0 → 1 mapping here
    would be defensible too; we return 0 so a caller that fails to
    short-circuit fails loudly at the ``page < 0`` clamp downstream
    instead of silently rendering an empty page 1-of-1.

    Stage 26 grew the ``page_size`` parameter so /shop and /inventory
    can share the arithmetic without locking their page sizes together;
    the default keeps Stage 25 call sites untouched.
    """
    if total <= 0:
        return 0
    return (total + page_size - 1) // page_size


def _clamp_page(page: int, total: int, page_size: int = _PAGE_SIZE) -> int:
    """Snap ``page`` into ``[0, page_count - 1]`` for the given page size.

    Two failure modes this absorbs:

    * Stale callback against a shrunk collection: admin pulled enough
      rows (or the user used items between renders) so ``page`` now
      points past the last page. We snap to the new last page so the
      user lands on a populated body rather than an empty one.
    * Hand-crafted callback with a negative or absurdly large
      ``page`` field. The wire type is ``int`` (signed), so a
      ``shop_pg:-1`` / ``inv_pg:9999`` from a curious user must not
      crash the handler.

    Stage 26 grew the ``page_size`` parameter alongside
    :func:`_page_count` — see that docstring for the why.
    """
    last = _page_count(total, page_size) - 1
    if last < 0:
        return 0
    if page < 0:
        return 0
    if page > last:
        return last
    return page


def _format_stock_hint(stock: int, lang: str) -> str:
    if stock == 0:
        return t("h_shop_stock_none", lang)
    if stock > 0:
        return t("h_shop_stock_left", lang, stock=stock)
    # stock == -1 (or any negative) means "infinite" in legacy.
    return ""


async def _wallet_balance(economy_repo: EconomyRepo, user_id: int) -> int:
    """Current wallet balance for ``user_id``, or 0 for a never-seen user.

    The shop surfaces read the caller's balance from three sites (the
    list header, a page flip, the buy-prompt card); folding the
    ``get`` + None-to-zero coalesce here keeps them in lockstep.
    """
    wallet = await economy_repo.get(user_id)
    return wallet.balance if wallet is not None else 0


# ── RR-2 #14: "buy for a group" scope ───────────────────────────────
#
# Legacy showed at most 15 group buttons (bot.py:23802 ``groups[:15]``)
# and truncated to 40 chars on both branches (bot.py:23804 for a stored
# title, :23806 for the id fallback). Both numbers are kept: 15 rows is
# already a tall column on a phone, and a 40-char button label is the
# point past which Telegram starts eliding anyway.
_GROUP_CHOOSER_CAP = 15
_GROUP_TITLE_CAP = 40


# ── #1761: one confirm card is one purchase ─────────────────────────
#
# ``ShopBuyConfirm`` deliberately carries no FSM state and no nonce
# (see its docstring): the packed ``item_id`` alone is a complete
# purchase order, replayable off the card forever. That is the right
# shape for a stateless callback, but it means a double-tap on ✅ is
# two complete orders, and the rowcount guards the module docstring
# leans on ("``WHERE balance >= price``, ``WHERE stock > 0``") only
# refuse the second one for a buyer who could not afford it anyway.
# A buyer who can afford two gets charged twice for a card that
# offered one item.
#
# A ``KeyedLocks`` critical section — the fix ``handle_withdraw_confirm``
# uses for the same double-tap — is the wrong tool here, and this is
# the distinction worth remembering: a lock protects a read-then-consume
# span, and there is nothing to consume. Serialising two taps on a
# stateless payload just makes them buy twice in a defined order.
#
# What closes it is claiming the *card identity* instead. ``get`` and
# ``put`` on :class:`TTLLRUCache` are both synchronous with no ``await``
# between them, so the claim below is atomic under asyncio without any
# lock at all — which is why this needs no signature change and no
# ``bot`` parameter.
#
# TTL: half an hour is well past the point a user is still rage-tapping
# one card, and Telegram's own callback timeout expires the button long
# before then. Capacity bounds the table at a few thousand recent cards.
_SPENT_CARDS_TTL_SECONDS: Final[float] = 1800.0
_SPENT_CARDS_CAPACITY: Final[int] = 4096
_spent_cards: TTLLRUCache[tuple[int, int], bool] = TTLLRUCache(
    _SPENT_CARDS_TTL_SECONDS, _SPENT_CARDS_CAPACITY
)


def _reset_spent_cards_for_tests() -> None:
    """Drop every recorded card claim.

    The table is module-level (one process, one bot) so tests would
    otherwise leak a spent card from one case into the next — every
    callback case in the e2e suite lands on the same synthetic
    ``(chat_id, message_id)``. Named for its only caller rather than
    exported as a general ``clear()``: production has no reason to
    forget a claim early.
    """
    _spent_cards.clear()


@dataclass(frozen=True, slots=True)
class _GroupScope:
    """A group purchase target the caller has been *verified* to own."""

    group_id: int
    title: str
    """Raw (unescaped) display title — escape at render time."""


def _group_title(group_id: int, title: str | None, lang: str) -> str:
    """Display name for a group row, with legacy's id fallback.

    Legacy rendered an id-numbered name when ``bot_groups.title`` was
    NULL (bot.py:23806) — a group added before the bot started storing
    titles, or one whose title lookup failed. That line was already
    language-aware (``f"Group {gid}"`` for ``en``, ``f"Группа {gid}"``
    otherwise), so the i18n key used here is parity, not an upgrade.
    The id is not secret (the caller owns the group) and a numbered row
    is still clickable, which beats an empty button.

    One knowingly-dropped detail: legacy truncated the fallback to 40
    chars too. A rendered id tops out around 20, so the cap could never
    bite; leaving it off keeps the fallback a single expression.
    """
    cleaned = (title or "").strip()
    if not cleaned:
        return t("h_shop_group_fallback_name", lang, id=group_id)
    return cleaned[:_GROUP_TITLE_CAP]


async def _owned_groups(registry: EngineRegistry, user_id: int) -> list[tuple[int, str | None]]:
    """The caller's own groups, for the chooser.

    ``bot_groups`` lives in ``users.db`` while everything else in this
    handler rides the economy session, so this opens its own read-only
    session rather than widening :class:`EconomyMiddleware`.

    A users-DB failure degrades to "no groups" — the chooser is an
    *extra* step in front of the catalog, so losing it must cost the
    caller the group perk, never the shop itself. (Fail-OPEN is safe
    here precisely because an empty list grants nothing; the mirror-image
    lookup in :func:`_verify_group_scope` fails CLOSED.)
    """
    sessionmaker = registry.session(DBName.USERS)
    try:
        async with sessionmaker() as session:
            return await BotGroupsRepo(session).list_owned(user_id, limit=_GROUP_CHOOSER_CAP)
    except SQLAlchemyError:
        log.bind(uid=user_id).warning("shop group chooser: users-db lookup failed")
        return []


async def _verify_group_scope(
    registry: EngineRegistry, *, group_id: int, user_id: int, lang: str
) -> _GroupScope | None:
    """Re-verify a wire-supplied ``group_id`` against the caller's groups.

    THE security boundary of this feature. Legacy's ``shop_group_<gid>``
    callback (bot.py:24066-24078) took the id straight off the wire and
    stashed it — so a hand-crafted callback could route another group's
    rating points *and* pay that group's owner out of the attacker's
    purchase. Every consumer of ``group_id`` here (chooser click, buy
    prompt, buy confirm, page flip) calls this first, and the confirm
    step calls it again immediately before the money moves, so a
    revoked/transferred group between prompt and confirm can't slip
    through a stale card.

    ``None`` means "not yours / unknown" — :meth:`BotGroupsRepo.get_owned`
    deliberately conflates the two so the caller can't probe which
    groups exist. ``group_id == 0`` (the global sentinel) never reaches
    the repo; callers check for it before calling. A users-DB failure
    also returns ``None``: this is the authorization check, so an
    *unanswered* question must read as "no", never as "sure".
    """
    sessionmaker = registry.session(DBName.USERS)
    try:
        async with sessionmaker() as session:
            row = await BotGroupsRepo(session).get_owned(group_id, user_id)
    except SQLAlchemyError:
        log.bind(uid=user_id, group_id=group_id).warning("shop group scope: users-db check failed")
        return None
    if row is None:
        return None
    return _GroupScope(group_id=row[0], title=_group_title(row[0], row[1], lang))


def _format_group_chooser(lang: str, *, percent: int) -> str:
    """The "buy for whom?" card — the step legacy showed before the catalog.

    Legacy's header was one line plus a flat sentence (bot.py:23810);
    this spells out both halves of what the group actually gets (rating
    points AND an owner payout), because "часть суммы идёт в казну
    группы" undersold a perk Iris has no equivalent for.
    """
    return t("h_shop_group_chooser", lang, percent=percent)


def _build_group_chooser_keyboard(
    groups: list[tuple[int, str | None]], lang: str
) -> InlineKeyboardMarkup:
    """One row per owned group + the "no group" escape hatch.

    Button labels go through :func:`html.escape`? No — Telegram button
    text is plain text, never parsed as HTML, so a group titled
    ``<b>x</b>`` renders literally here. (The same title IS escaped
    where it lands in a *message* body — see :func:`_format_shop`.)
    """
    rows = [
        [
            InlineKeyboardButton(
                text=t("h_shop_group_btn", lang, name=_group_title(gid, title, lang)),
                callback_data=ShopGroupPick(group_id=gid).pack(),
            )
        ]
        for gid, title in groups[:_GROUP_CHOOSER_CAP]
    ]
    rows.append(
        [
            InlineKeyboardButton(
                text=t("h_shop_group_global_btn", lang),
                callback_data=ShopGroupPick(group_id=0).pack(),
            )
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_shop(
    items: list[ShopItemEntity],
    lang: str,
    page: int = 0,
    *,
    balance: int | None = None,
    scope: _GroupScope | None = None,
    group_percent: int = 0,
) -> str:
    """Render one page of the catalog as one Telegram message.

    ``items`` is the FULL catalog (price-ASC from the repo); the slice
    happens here so the page-count and the slice agree on the same
    bound. Item-supplied fields (``name``, ``description``) go through
    :func:`html.escape` — same risk class as the AI handler: a name
    like ``<b>Free coins</b>`` from an admin import would otherwise
    render bold on every user's screen.

    ``balance`` (#13): when supplied, a "your balance + top-up hint"
    line rides under the header so the user can size up a purchase
    without a separate ``/balance`` round-trip — the legacy shop header
    carried this (bot.py:23754).

    ``scope`` (#14): the group this catalog is scoped to. Restores the
    legacy header note "📍 Покупка для группы (часть суммы пойдёт в
    казну группы)" (bot.py:23747-23753), now naming the group and the
    exact percentage instead of a vague "часть суммы". The title is
    ``html.escape``d — it comes from ``bot_groups.title``, i.e. from
    whatever a group admin typed.
    """
    start = page * _PAGE_SIZE
    page_items = items[start : start + _PAGE_SIZE]
    header = t("h_shop_list_header", lang)
    if balance is not None:
        header += "\n" + t("h_shop_balance_line", lang, balance=format_number(balance))
    if scope is not None and group_percent > 0:
        header += "\n" + t(
            "h_shop_group_note",
            lang,
            name=html.escape(scope.title),
            percent=group_percent,
        )
    lines = [header + "\n"]
    for item in page_items:
        name = html.escape(item.name)
        desc = html.escape(item.description)
        stock_hint = _format_stock_hint(item.stock, lang)
        lines.append(f"• <b>{name}</b> — {item.price} 🪙{stock_hint}")
        if desc:
            lines.append(f"  <i>{desc}</i>")
        lines.append(t("h_shop_buy_line", lang, id=item.id))
        lines.append("")
    return "\n".join(lines).rstrip()


def _build_shop_keyboard(
    items: list[ShopItemEntity],
    lang: str,
    page: int = 0,
    *,
    group_id: int = 0,
    show_group_switch: bool = False,
) -> InlineKeyboardMarkup:
    """One column of "Buy" buttons for the requested page, plus a nav row.

    Out-of-stock rows (``stock == 0``) are already filtered upstream by
    :meth:`ShopItemsRepo.list_all`, but we still skip ``stock == 0`` in
    the keyboard pass defensively: an admin who toggles stock to 0
    between the repo read and the keyboard render would otherwise ship
    a button that can only ever produce an out-of-stock toast. Pinned
    on the conservative side because the cost is one branch.

    Nav row layout (Stage 25):

    * Page 0 of a multi-page catalog → ``[next »]`` only.
    * A middle page → ``[« prev] [N/M] [next »]``.
    * Last page of a multi-page catalog → ``[« prev] [N/M]``.
    * Single-page catalog (total ≤ PAGE_SIZE) → no nav row at all, so
      the small-catalog case looks identical to its Stage 24 form.

    The middle ``N/M`` indicator carries a no-op ``ShopPage(page=N-1)``
    payload so the handler can detect a same-page click and silently
    answer the callback instead of trying to ``edit_text`` with the
    identical body (which Telegram rejects with "message is not
    modified", a card that would otherwise poison the dispatcher).

    ``group_id`` (#14) is stamped into every buy button AND every nav
    button, so the chosen group survives a page flip. ``0`` = a global
    purchase, which is also the wire default — so a catalog rendered
    without a group is byte-identical to the pre-#14 keyboard apart from
    the trailing ``:0``. ``show_group_switch`` appends a "change group"
    row for users who have groups to switch between; users with none
    never see it.
    """
    label = t("h_shop_buy_btn", lang)
    total = len(items)
    pages = _page_count(total)
    start = page * _PAGE_SIZE
    page_items = items[start : start + _PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    for item in page_items:
        if item.stock == 0:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{label}: {item.name[:24]} ({item.price})",
                    callback_data=ShopBuyPrompt(item_id=item.id, group_id=group_id).pack(),
                )
            ]
        )
    if pages > 1:
        nav = build_pagination_nav(
            lang=lang,
            current_page=page,
            total_pages=pages,
            callback_factory=ShopPage,
            nav_keys=("h_shop_nav_prev", "h_shop_nav_indicator", "h_shop_nav_next"),
            extra={"group_id": group_id},
        )
        rows.append(nav)
    if show_group_switch:
        rows.append(
            [
                InlineKeyboardButton(
                    text=t("h_shop_group_switch_btn", lang),
                    callback_data=ShopGroupMenu().pack(),
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_inventory(entries: list[InventoryEntry], lang: str, page: int = 0) -> str:
    """Render one page of the inventory list.

    ``entries`` is the full ``list_for_user`` result (capped at 30 by
    the repo); the slice happens here so the page-count math and the
    body slice agree on the same bound — mirrors the /shop posture.
    Stage 26 grew the ``page`` parameter; Stage 16's bare call falls
    through to ``page=0`` so the legacy slash-render shape is intact
    when the catalog fits on one page.
    """
    start = page * _INVENTORY_PAGE_SIZE
    page_entries = entries[start : start + _INVENTORY_PAGE_SIZE]
    lines = [t("h_inventory_list_header", lang) + "\n"]
    for entry in page_entries:
        # ``purchase_date`` is naive LOCAL, like every other datetime on
        # this row (#1951) — render it raw, no conversion. It used to be
        # written in UTC, which put it three hours behind the ``expires``
        # printed on the very same line. Second precision matches legacy.
        date_str = entry.purchase_date.strftime("%Y-%m-%d %H:%M:%S")
        used_tag = t("h_inventory_used_tag", lang) if entry.used else ""
        name = html.escape(entry.item_name)
        # RR-2 #19: surface the expiry inline (None = permanent, no suffix).
        expires_tag = (
            t(
                "h_inventory_expires_inline",
                lang,
                when=entry.expires.strftime("%Y-%m-%d %H:%M"),
            )
            if entry.expires is not None
            else ""
        )
        lines.append(f"• {name}{used_tag} — {date_str}{expires_tag}")
    return "\n".join(lines)


def _build_inventory_keyboard(
    entries: list[InventoryEntry], lang: str, page: int = 0
) -> InlineKeyboardMarkup:
    """Per-row 🔍 inspect buttons + a /shop-shaped nav row.

    The keyboard mirrors :func:`_build_shop_keyboard`'s shape: one
    button per visible row carrying :class:`InventoryInspect`, then
    (if more than one page) a nav row with prev / indicator / next.
    The indicator carries the *current* page so a click on it is a
    detectable no-op — same pattern as Stage 25's shop nav.

    No authorization in the keyboard itself: the rendering happens on
    a per-user message reply, the wire payload is just the inventory
    PK, and the inspect handler re-checks ownership at the repo. See
    :class:`InventoryInspect`'s docstring for the design rationale.
    """
    total = len(entries)
    pages = _page_count(total, _INVENTORY_PAGE_SIZE)
    start = page * _INVENTORY_PAGE_SIZE
    page_entries = entries[start : start + _INVENTORY_PAGE_SIZE]
    rows: list[list[InlineKeyboardButton]] = []
    for entry in page_entries:
        rows.append(
            [
                InlineKeyboardButton(
                    text=t(
                        "h_inventory_inspect_btn",
                        lang,
                        # Truncate so a 60-char item name + 🔍 prefix
                        # doesn't blow past Telegram's button-label
                        # display budget (mirrors /shop's [:24] slice
                        # on the buy label).
                        name=entry.item_name[:32],
                    ),
                    callback_data=InventoryInspect(entry_id=entry.inventory_id).pack(),
                )
            ]
        )
    if pages > 1:
        nav = build_pagination_nav(
            lang=lang,
            current_page=page,
            total_pages=pages,
            callback_factory=InventoryPage,
            nav_keys=("h_inventory_nav_prev", "h_inventory_nav_indicator", "h_inventory_nav_next"),
        )
        rows.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _format_inventory_detail(detail: InventoryDetail, lang: str) -> str:
    """Render the single-entry inspect card.

    Fields surfaced beyond the list view: ``expires`` (if set), a
    ``used`` flag (only useful if the user got here via a stale list —
    the list filters used rows out), and the item description (often
    multi-line and too long for a list row). All admin-supplied strings
    go through :func:`html.escape` because parse_mode=HTML — same
    posture as :func:`_format_shop`.
    """
    extras: list[str] = []
    if detail.item_description:
        extras.append(
            t(
                "h_inventory_detail_description",
                lang,
                description=html.escape(detail.item_description),
            )
        )
    if detail.expires is not None:
        extras.append(
            t(
                "h_inventory_detail_expires",
                lang,
                expires=detail.expires.strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
    if detail.used:
        extras.append(t("h_inventory_detail_used", lang))
    return t(
        "h_inventory_detail",
        lang,
        name=html.escape(detail.item_name),
        purchased=detail.purchase_date.strftime("%Y-%m-%d %H:%M:%S"),
        extras="\n".join(extras),
    ).rstrip()


def _build_inventory_back_keyboard(
    lang: str, *, use_entry_id: int | None = None
) -> InlineKeyboardMarkup:
    """Keyboard under the inspect card — Back, optionally with a Use button.

    Stage 29 added the Use button. When the caller passes
    ``use_entry_id``, the keyboard renders ``🎁 Use`` next to ``🔙 Back``
    so the user can activate the item without leaving the inspect card.
    When ``use_entry_id`` is ``None`` (the planner returned UNKNOWN for
    this item, see :func:`handle_inventory_inspect`), only Back is
    rendered and the card body carries the legacy-hint copy so the user
    knows to fall back to the slash-command activation flow.

    Both buttons live on one row — they're a coupled pair (decide to
    use OR back away) and the side-by-side layout matches the Stage 24
    confirm/cancel pair under the buy prompt.
    """
    row: list[InlineKeyboardButton] = []
    if use_entry_id is not None:
        row.append(
            InlineKeyboardButton(
                text=t("h_inventory_use_btn", lang),
                callback_data=InventoryUse(entry_id=use_entry_id).pack(),
            )
        )
    row.append(
        InlineKeyboardButton(
            text=t("h_inventory_back_btn", lang),
            callback_data=InventoryBack().pack(),
        )
    )
    return InlineKeyboardMarkup(inline_keyboard=[row])


def _current_inventory_page_from_markup(message: object) -> int | None:
    """Sniff the displayed page out of an /inventory message's nav row.

    Same shape as :func:`_current_page_from_markup` for /shop: the
    indicator button packs ``InventoryPage(page=current)`` and renders
    with a ``/`` in its label (locale-independent — the indicator
    template carries the slash literally in both ru.yaml and en.yaml).
    Returns ``None`` if the markup is absent or doesn't contain an
    indicator-shaped button; callers fall through to the edit path,
    which ``_safe_edit`` absorbs the "message is not modified" reject
    on.
    """
    if not isinstance(message, MessageType):
        return None
    markup = message.reply_markup
    if markup is None:
        return None
    for row in markup.inline_keyboard:
        for btn in row:
            data = btn.callback_data
            if data is None or not data.startswith("inv_pg:"):
                continue
            if "/" not in btn.text:
                continue
            try:
                return int(data.split(":", 1)[1])
            except ValueError:
                return None
    return None


def _format_purchase_success(
    outcome: PurchaseOutcome,
    lang: str,
    *,
    balance_override: int | None = None,
    auto_applied: bool = False,
) -> str:
    """Render the post-debit confirmation line.

    Mirrors the structure of legacy ``buy_success`` (translations.py)
    — item name, price, remaining balance.

    ``auto_applied`` (#15): when the bought item's effect was applied
    on the spot (VIP / luck-coins / a timed booster), the "activate via
    /inventory" footer is dropped — the effect reveal rides under this
    block instead, and pointing the user at /inventory for an item that
    is already spent would only confuse. ``balance_override`` carries
    the post-payout balance for luck items (whose credit lands after the
    purchase debit, so ``outcome.new_balance`` is stale).
    """
    assert outcome.item is not None  # invariant of status==OK
    name = html.escape(outcome.item.name)
    balance = balance_override if balance_override is not None else outcome.new_balance
    lines = [
        t("h_purchase_success_bought", lang, name=name),
        t("h_purchase_success_price", lang, price=outcome.item.price),
    ]
    if balance is not None:
        lines.append(t("h_purchase_success_balance", lang, balance=balance))
    if outcome.new_stock is not None and outcome.new_stock >= 0:
        # Mirrors legacy "buy_stock_left": only mention finite stock,
        # never the -1 sentinel.
        lines.append(t("h_purchase_success_stock_left", lang, stock=outcome.new_stock))
    if not auto_applied:
        lines.append(t("h_purchase_success_inventory", lang))
    return "\n".join(lines)


async def _auto_apply_purchase(
    outcome: PurchaseOutcome,
    user_id: int,
    inventory_use_service: InventoryUseService,
    economy_repo: EconomyRepo,
    lang: str,
) -> tuple[str | None, int | None]:
    """Apply a freshly-bought consumable on the spot (#15).

    Returns ``(reveal, balance_override)``:

    * ``reveal`` — the per-kind effect blurb to append to the receipt
      when the item auto-applied (VIP / luck-coins / timed booster), or
      ``None`` when the item stays in the inventory for the manual flow
      (custom-title and unwarn need a second step; unclassified items
      refuse to act). A ``None`` keeps the legacy "go to /inventory"
      footer.
    * ``balance_override`` — the post-credit balance for a luck payout
      (whose credit lands after the purchase debit), else ``None``.

    Runs in the same middleware session as the purchase, so the consume
    + grant + coin credit commit together with the debit — there is no
    window where the coins are gone but the effect never landed.
    """
    if outcome.inventory_id is None:
        return None, None
    # Naive ``datetime.now()`` to match the rest of the inventory /
    # economy path — same convention the /inventory use handler uses so
    # the planner, the ``used_date`` write and the grant TTL agree.
    result = await inventory_use_service.use(
        user_id=user_id,
        entry_id=outcome.inventory_id,
        now=datetime.now(),  # noqa: DTZ005 — match legacy naive convention
    )
    if result.outcome is UseOutcome.BUSTER_ALREADY_ACTIVE:
        # #1903: the buy went through and the item is sitting unused in
        # ``/inventory`` — say that on the receipt rather than leaving a
        # silent gap where the effect line normally is. No
        # ``balance_override``: this branch credits nothing.
        return _format_buster_already_active(lang=lang, expires_at=result.buster_expires_at), None
    if result.outcome is not UseOutcome.SUCCESS or result.kind is None:
        return None, None
    reveal = _format_inventory_use_success(
        kind=result.kind,
        lang=lang,
        granted_till=result.granted_till,
        buster_expires_at=result.buster_expires_at,
        color_nick_expires_at=result.color_nick_expires_at,
        mute_protection_expires_at=result.mute_protection_expires_at,
        coin_payout_amount=result.coin_payout_amount,
        xp_boost_expires_at=result.xp_boost_expires_at,
        xp_boost_multiplier=result.xp_boost_multiplier,
    )
    # Luck payout credits coins after the purchase debit; re-read so the
    # receipt's balance line reflects the net. The read sees the pending
    # in-session credit (autoflush), and both commit together downstream.
    balance_override: int | None = None
    if result.kind is InventoryEffectKind.LUCK_COIN_PAYOUT:
        wallet = await economy_repo.get(user_id)
        balance_override = wallet.balance if wallet is not None else None
    return reveal, balance_override


async def handle_shop(
    message: Message,
    shop_items_repo: ShopItemsRepo,
    economy_repo: EconomyRepo,
    registry: EngineRegistry,
    group_percent: int,
    lang: str,
) -> None:
    """Render the group chooser, or page 0 of the catalog.

    The slash-command entry shape is unchanged from Stage 24 — calling
    ``/shop`` always lands page 0. Subsequent navigation is driven by
    :class:`ShopPage` callbacks against the same message via
    :func:`handle_shop_page`.

    #14 restores the legacy chooser-first step (bot.py:23777-23820): a
    caller who has groups is asked *who the purchase is for* before the
    catalog appears. Chooser-first rather than a quieter "change group"
    affordance on the catalog is deliberate — it's the only layout that
    guarantees the perk is discovered, which is the whole reason the
    regression mattered. Callers with no groups (or with group routing
    switched off) go straight to the catalog exactly as before, so the
    extra tap only exists for users it can pay off for.
    """
    items = await shop_items_repo.list_all()
    if not items:
        await message.reply(t("h_shop_empty", lang))
        return
    uid = message.from_user.id if message.from_user else 0
    groups = await _owned_groups(registry, uid) if group_percent > 0 and uid else []
    if groups:
        await message.reply(
            _format_group_chooser(lang, percent=group_percent),
            reply_markup=_build_group_chooser_keyboard(groups, lang),
        )
        log.bind(uid=uid, groups=len(groups)).info("/shop group chooser rendered")
        return
    balance = await _wallet_balance(economy_repo, uid)
    await message.reply(
        _format_shop(items, lang, page=0, balance=balance),
        reply_markup=_build_shop_keyboard(items, lang, page=0),
    )
    log.bind(
        uid=message.from_user.id if message.from_user else None,
        rows=len(items),
        pages=_page_count(len(items)),
    ).info("/shop rendered")


async def handle_buy(
    message: Message,
    command: CommandObject,
    purchase_service: PurchaseService,
    inventory_use_service: InventoryUseService,
    economy_repo: EconomyRepo,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Parse ``/buy <id>`` and run the atomic purchase.

    Three input failure modes (no arg, non-int arg, no user) are
    handled inline because they don't reach the service — the service
    assumes a valid ``user_id`` and ``item_id``.

    #2006: a type the effect planner cannot activate is refused by the
    service before any write, and rendered here as its own card. It is
    not an error the buyer can clear — no amount of coins or waiting
    makes that row usable — so the copy points at /support rather than
    back at /shop.

    Auto-apply (#15): legacy ``/buy`` activated a consumable on the spot
    and revealed the effect ("+N 🪙!", "VIP activated"). We restore that
    by running :meth:`InventoryUseService.use` on the just-created
    inventory row, in the SAME middleware session, so the purchase debit
    and the grant/credit commit together. Items that need a second step
    (custom title, unwarn) or are unclassified return a non-SUCCESS
    outcome and stay in the inventory for the manual /inventory flow.
    """
    tg_user = require_from_user(message)
    if not command.args:
        await message.reply(t("h_buy_usage", lang))
        return
    raw = command.args.strip().split()[0]
    # #1680: not a bare ``int()``. That parse takes any ASCII digit run
    # up to CPython's 4300-digit literal limit, so ``/buy <2**63>`` got
    # past the ``except ValueError`` as a perfectly valid Python int and
    # then died inside aiosqlite as ``OverflowError`` — not an
    # ``SQLAlchemyError``, caught nowhere on this path, one unhandled
    # traceback per message and freely repeatable by anyone. It also
    # accepted non-ASCII digits, so ``/buy \N{ARABIC-INDIC DIGIT THREE}``
    # bought item 3. :func:`parse_int_token` is the one gate that answers
    # both: an ASCII digit run this database can actually store.
    item_id = parse_int_token(raw)
    if item_id is None:
        await message.reply(t("h_buy_bad_id", lang))
        return

    outcome = await purchase_service.purchase(user_id=tg_user.id, item_id=item_id)
    if outcome.status is PurchaseStatus.OK:
        reveal = await _auto_apply_purchase(
            outcome, tg_user.id, inventory_use_service, economy_repo, lang
        )
        body = _format_purchase_success(
            outcome,
            lang,
            balance_override=reveal[1],
            auto_applied=reveal[0] is not None,
        )
        if reveal[0] is not None:
            body = f"{body}\n\n{reveal[0]}"
        if not await reply_or_send(message, body):
            log.bind(uid=tg_user.id, item_id=item_id).warning("/buy receipt undeliverable")
            # The debit and the auto-apply grant are already in this
            # session. Raising hands the update to ``middlewares.base``,
            # which rolls both back — a purchase that never happened
            # beats one the buyer was charged for and never saw.
            raise UndeliverableResultError("/buy receipt")
    elif outcome.status is PurchaseStatus.ITEM_NOT_FOUND:
        await message.reply(t("h_buy_not_found", lang))
    elif outcome.status is PurchaseStatus.NO_EFFECT:
        # #2006: refused before any write, so — unlike the branch below
        # — nothing was reserved and nothing has to be released. The
        # copy says the coins are untouched because the user cannot see
        # the transaction that did not happen.
        await message.reply(t("h_buy_no_effect", lang))
    elif outcome.status is PurchaseStatus.OUT_OF_STOCK:
        await message.reply(t("h_buy_out_of_stock", lang))
    elif outcome.status is PurchaseStatus.INSUFFICIENT_FUNDS:
        assert outcome.item is not None
        # #1968: this is the only status here that arrives with the
        # write transaction open. :meth:`PurchaseService.purchase` has
        # no affordability read — it goes straight to the guarded debit
        # — so ``INSUFFICIENT_FUNDS`` always means an UPDATE that
        # matched zero rows, and ``db/engines.py`` promotes the
        # connection to ``BEGIN IMMEDIATE`` on that statement anyway.
        # Nothing survives to be committed, but ``economy.db`` stays
        # locked over it for the whole round trip below unless we let
        # go, and SQLite's ``busy_timeout`` is 5s. ``ITEM_NOT_FOUND``
        # and the pre-check ``OUT_OF_STOCK`` never write; the stock-race
        # ``OUT_OF_STOCK`` rolls the session back itself
        # (``purchase_service.py``). None of the three needs this, and
        # the ``UndeliverableResultError`` policy on the success branch
        # above is untouched: there is no purchase here to undo.
        if checkpoint is not None:
            await checkpoint()
        await message.reply(t("h_buy_insufficient", lang, price=outcome.item.price))

    log.bind(
        uid=tg_user.id,
        item_id=item_id,
        status=outcome.status.value,
    ).info("/buy processed")


async def handle_inventory(message: Message, inventory_repo: InventoryRepo, lang: str) -> None:
    """Render page 0 of the caller's inventory.

    Stage 26 added pagination + per-row inspect buttons; the slash
    entry shape is unchanged — ``/inventory`` (or ``/inv``) always
    lands page 0. Subsequent navigation rides on
    :class:`InventoryPage` / :class:`InventoryInspect` /
    :class:`InventoryBack` callbacks against the same message.
    """
    tg_user = require_from_user(message)
    entries = await inventory_repo.list_for_user(tg_user.id)
    if not entries:
        await message.reply(t("h_inventory_empty", lang))
        return
    await message.reply(
        _format_inventory(entries, lang, page=0),
        reply_markup=_build_inventory_keyboard(entries, lang, page=0),
    )
    log.bind(
        uid=tg_user.id,
        rows=len(entries),
        pages=_page_count(len(entries), _INVENTORY_PAGE_SIZE),
    ).info("/inventory rendered")


# ── Stage 26: /inventory pagination + per-entry inspect callbacks ──────


async def handle_inventory_page(
    callback: CallbackQuery,
    callback_data: InventoryPage,
    inventory_repo: InventoryRepo,
    lang: str,
) -> None:
    """Page-flip click on /inventory's nav row.

    Re-reads the caller's inventory (cheap — same SELECT /inventory
    uses) and edits the message in place to the requested page. Three
    failure shapes mirror :func:`handle_shop_page`:

    * Empty inventory between renders (user used the last unused row
      via legacy) → edit to the empty-state line, drop the keyboard.
    * Out-of-range page (user used rows so the requested page is past
      the new last) → :func:`_clamp_page` snaps to the new last page.
    * Same-page indicator click → answer the callback silently, no
      edit, avoiding the "message is not modified" Telegram reject.

    **Authorization**: ``list_for_user`` is called with
    ``callback.from_user.id`` — there is no path by which user B's
    page-flip click renders user A's inventory. The wire payload
    carries the page number only, not the owning user_id; a forwarded
    /inventory message clicked by B renders B's inventory (likely
    empty), which is the right posture for a private-data surface.
    """
    assert callback.from_user is not None  # filter guarantees
    entries = await inventory_repo.list_for_user(callback.from_user.id)
    if not entries:
        await callback.answer()
        await _safe_edit(callback, t("h_inventory_empty", lang), markup=None)
        return

    target = _clamp_page(callback_data.page, len(entries), _INVENTORY_PAGE_SIZE)
    current_page = _current_inventory_page_from_markup(callback.message)
    if current_page is not None and current_page == target:
        await callback.answer()
        return

    body = _format_inventory(entries, lang, page=target)
    await callback.answer()
    await _safe_edit(
        callback,
        body,
        markup=_build_inventory_keyboard(entries, lang, page=target),
    )
    log.bind(
        uid=callback.from_user.id,
        requested_page=callback_data.page,
        rendered_page=target,
        pages=_page_count(len(entries), _INVENTORY_PAGE_SIZE),
    ).info("/inventory page flipped")


async def handle_inventory_inspect(
    callback: CallbackQuery,
    callback_data: InventoryInspect,
    inventory_repo: InventoryRepo,
    shop_items_repo: ShopItemsRepo,
    lang: str,
) -> None:
    """Per-row 🔍 click → render the single-entry detail card.

    **This is the most security-sensitive handler in the stage.**
    The wire payload carries only the inventory PK, not the owning
    user_id. Authorization rides entirely on the repo lookup being
    ``get_for_user(callback.from_user.id, entry_id)`` — a hand-
    crafted ``inv_ins:<other-user's entry>`` from user B reaches the
    repo with ``user_id == B.id``, the WHERE clause filters the row
    out, and we return the same "no longer available" toast that a
    deleted-entry lookup would produce. No edit happens in either
    case — leaving the inventory list visible so B can keep browsing
    their OWN inventory, and not leaking any information about
    whether the entry exists under a different owner.

    The defensive ``detail.user_id != callback.from_user.id`` assert
    is belt-and-braces: the repo's WHERE clause already enforces it,
    but a future refactor that adds an OR clause or drops the user_id
    filter would otherwise silently leak — the assert turns that into
    a crash rather than a privacy bug.
    """
    assert callback.from_user is not None  # filter guarantees
    detail = await inventory_repo.get_for_user(callback.from_user.id, callback_data.entry_id)
    if detail is None:
        # Covers four cases with one user-facing string: entry deleted,
        # entry never existed, entry belongs to a different user
        # (crafted callback), entry's owning user doesn't match the
        # caller (forwarded /inventory message). Same toast for all
        # four so no information leaks about the row's existence
        # under a different owner.
        await callback.answer(t("h_inventory_not_found_toast", lang), show_alert=False)
        return
    # Belt-and-braces — the repo already enforces this in SQL. If the
    # repo regresses, fail loud instead of silently leaking a card.
    assert detail.user_id == callback.from_user.id, (
        "InventoryRepo.get_for_user returned a row for a different user"
    )

    # Stage 29: decide whether to render the 🎁 Use button.
    #
    # Design call — BUTTON-HIDDEN (not always-shown):
    # We look up the catalog row and run the pure planner here so the
    # inspect card knows whether ``InventoryUseService.use`` would
    # accept this item. If the planner returns UNKNOWN we hide the Use
    # button entirely and append a one-line hint to the card body
    # (``h_inventory_detail_no_activation``). The hint was written when
    # an UNKNOWN item still had a legacy activation path; T-011 removed
    # it, so an UNKNOWN row is now unusable outright — the planner's own
    # docstring calls that terminal for a buyer who already paid. #2005
    # rewrote the copy accordingly: it used to promise the old flow and
    # send the buyer to /help, which cannot activate anything; it now
    # says the item cannot be activated here and points at /support,
    # the one destination staffed by someone who can act on it.
    # The alternative — always show Use, let the service return
    # UNKNOWN_EFFECT, render a per-click "not
    # supported yet" card — is one cheaper render at inspect time but
    # an extra dead-end click for every UNKNOWN item, on every inspect.
    # The button-hidden path is strictly better UX (no dead-end click)
    # and the diff is the same size (the lookup happens either at
    # inspect time or at use-click time; this only moves it earlier).
    # The Stage 29 handler still defends against UNKNOWN_EFFECT
    # surfacing from the service (race: admin deletes the catalog row
    # between inspect render and Use click) — see handle_inventory_use.
    #
    # The catalog lookup is a single PK SELECT on the already-open
    # session, so the cost is one extra round-trip per inspect render.
    # ``used`` and ``expired`` entries currently can't reach the
    # inspect card (list_for_user filters them), but if they ever do
    # we still want the Use button hidden — the planner check below
    # gates on item type only, but the service's outcome will reject
    # used/expired with the appropriate toast. We belt-and-braces hide
    # the button when ``detail.used`` so a stale list-render race
    # doesn't surface an unclickable Use button.
    item = await shop_items_repo.get(detail.item_id)
    show_use_button = (
        not detail.used
        and item is not None
        and plan_effect_application(
            item,
            now=datetime.now(),  # noqa: DTZ005 — match legacy naive convention
        ).kind
        is not InventoryEffectKind.UNKNOWN
    )

    body = _format_inventory_detail(detail, lang)
    if not show_use_button and not detail.used:
        # Tell the user this item has no activation path and where a
        # human can be reached. Not shown for ``used`` rows — they're
        # terminal for a different reason, and the hint would be noise.
        body = body + "\n\n" + t("h_inventory_detail_no_activation", lang)

    await callback.answer()
    await _safe_edit(
        callback,
        body,
        markup=_build_inventory_back_keyboard(
            lang,
            use_entry_id=detail.inventory_id if show_use_button else None,
        ),
    )
    log.bind(
        uid=callback.from_user.id,
        entry_id=callback_data.entry_id,
        use_button=show_use_button,
    ).info("/inventory entry inspected")


async def handle_inventory_back(
    callback: CallbackQuery,
    callback_data: InventoryBack,
    inventory_repo: InventoryRepo,
    lang: str,
) -> None:
    """🔙 Back click on the inspect card → re-render inventory page 0.

    ``callback_data`` is unused — see :class:`InventoryBack`'s
    docstring for why the wire payload carries no fields. Snapping to
    page 0 (rather than threading the source page through) is the
    deliberate simplification documented there.

    Authorization is identical to :func:`handle_inventory_page`: the
    list re-read is constrained to ``callback.from_user.id``, so a
    forwarded inspect card clicked by user B renders B's inventory
    (likely empty), not the original owner's.
    """
    del callback_data
    assert callback.from_user is not None  # filter guarantees
    entries = await inventory_repo.list_for_user(callback.from_user.id)
    await callback.answer()
    if not entries:
        # User cleared their inventory while inspecting (rare but
        # possible if legacy auto-apply ran on a used row between
        # renders). Drop the keyboard so the now-empty state can't
        # be "navigated".
        await _safe_edit(callback, t("h_inventory_empty", lang), markup=None)
        return
    await _safe_edit(
        callback,
        _format_inventory(entries, lang, page=0),
        markup=_build_inventory_keyboard(entries, lang, page=0),
    )
    log.bind(uid=callback.from_user.id).info("/inventory back from inspect")


# ── Stage 29: /inventory use callback over InventoryUseService ─────────


def _format_buster_already_active(*, lang: str, expires_at: datetime | None) -> str:
    """Copy for #1903's refuse-without-consume outcome.

    Two strings rather than one with an optional placeholder: an
    ``expires_at <= 0`` row (legacy's "never expires" slot, which this
    pipeline never writes) has no date to show, and rendering the epoch
    would be worse than saying nothing.
    """
    if expires_at is None:
        return t("h_inventory_use_buster_active_no_expiry", lang)
    return t(
        "h_inventory_use_buster_active",
        lang,
        expires_at=expires_at.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _format_inventory_use_success(
    *,
    kind: InventoryEffectKind,
    lang: str,
    granted_till: datetime | None,
    buster_expires_at: datetime | None,
    color_nick_expires_at: datetime | None,
    mute_protection_expires_at: datetime | None,
    coin_payout_amount: int | None,
    xp_boost_expires_at: datetime | None,
    xp_boost_multiplier: int | None,
) -> str:
    """Render the per-kind success card body.

    Branches over :class:`InventoryEffectKind` (not :class:`UseOutcome`)
    because the success branch alone has per-kind copy. New planner
    kinds added in a future stage fall through to a generic "effect
    applied" line — better than a missing branch silently rendering an
    empty body, and the per-kind copy can be filled in piecemeal as
    each kind ports.
    """
    if kind is InventoryEffectKind.VIP_GRANT:
        assert granted_till is not None  # service invariant on this kind
        return t(
            "h_inventory_use_success_vip",
            lang,
            granted_till=granted_till.strftime("%Y-%m-%d %H:%M:%S"),
        )
    if kind is InventoryEffectKind.DOUBLE_DAILY_BUSTER:
        assert buster_expires_at is not None  # service invariant on this kind
        return t(
            "h_inventory_use_success_double_daily",
            lang,
            expires_at=buster_expires_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
    if kind is InventoryEffectKind.COLOR_NICK:
        assert color_nick_expires_at is not None  # service invariant on this kind
        return t(
            "h_inventory_use_success_color_nick",
            lang,
            expires_at=color_nick_expires_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
    if kind is InventoryEffectKind.MUTE_PROTECTION:
        assert mute_protection_expires_at is not None  # service invariant
        return t(
            "h_inventory_use_success_mute_protection",
            lang,
            expires_at=mute_protection_expires_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
    if kind is InventoryEffectKind.LUCK_COIN_PAYOUT:
        assert coin_payout_amount is not None  # service invariant on this kind
        # ``amount`` is an int, so the {amount} substitution carries no
        # HTML-unsafe characters — no escape needed, consistent with the
        # other branches here passing pre-formatted strs.
        return t("h_use_luck_payout", lang, amount=coin_payout_amount)
    if kind is InventoryEffectKind.XP_BOOST:
        assert xp_boost_expires_at is not None  # service invariant on this kind
        assert xp_boost_multiplier is not None  # service invariant on this kind
        # Both substitutions are ints / a formatted timestamp — no
        # HTML-unsafe characters, consistent with the other branches.
        return t(
            "h_item_xp_boost_activated",
            lang,
            multiplier=xp_boost_multiplier,
            expires_at=xp_boost_expires_at.strftime("%Y-%m-%d %H:%M:%S"),
        )
    # Future kinds — generic copy until they get their own template.
    return t("h_inventory_use_success_generic", lang)


def _build_inventory_use_result_keyboard(lang: str) -> InlineKeyboardMarkup:
    """One Back-to-inventory-page-0 button under the result card.

    The keyboard intentionally does NOT carry a second Use button —
    success rows are consumed (the next click would surface
    ALREADY_USED), and the rejection cards are terminal for THIS
    entry. The Back button uses :class:`InventoryBack`, which Stage
    26's :func:`handle_inventory_back` already snaps to page 0.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_inventory_back_btn", lang),
                    callback_data=InventoryBack().pack(),
                )
            ]
        ]
    )


async def _apply_unwarn(
    *,
    registry: EngineRegistry,
    inventory_repo: InventoryRepo,
    user_id: int,
    entry_id: int,
    main_chat_id: int,
    now: datetime,
) -> bool:
    """Remove one active warning in the main chat, consuming the entry.

    Mirrors legacy ``apply_unwarn`` (``bot.py:13626``), which calls
    ``remove_warning(user_id, CHAT_ID)`` against the configured main
    chat. The warning lives in ``moderation.db``; the inventory entry in
    ``economy.db``. We:

    1. Open a moderation session and try to soft-delete the most-recent
       active warning for ``(user_id, main_chat_id)``.
    2. ONLY if one was removed, consume the inventory entry (economy
       session via ``inventory_repo``). Refuse-without-consume is the
       whole contract here: a user with no warning must not lose the
       item (no debit on refusal — see the L-21 atomicity requirement).
    3. ONLY if the consume won, commit the moderation session. The
       consume is the authority for the whole redemption (#1133), so
       the moderation transaction is held open across it and rolled
       back when it loses.

    Order matters: remove-then-consume means a crash between the two
    leaves a removed warning with an unconsumed item rather than a
    consumed item with no benefit. That is the right side to err on for
    a genuine crash, which no one can provoke on demand. It is NOT an
    acceptable steady state: the caller must make the consume durable
    before it does anything that can fail, or the tear stops being a
    crash window and becomes a repeatable way to spend one item on
    every warning (#1277). ``handle_inventory_use`` therefore calls
    ``checkpoint()`` the moment this returns True.

    Returns whether a warning was removed AND the entry consumed — the
    two are now inseparable.
    """
    sessionmaker = registry.session(DBName.MODERATION)
    async with sessionmaker() as session:
        from telegram_invite_bot.repositories.moderation_repo import ModerationRepo

        removed = await ModerationRepo(session).remove_last_warning(
            user_id=user_id,
            chat_id=main_chat_id,
            admin_id=user_id,  # self-service redemption; no separate admin
            reason="shop unwarn item",
        )
        if not removed:
            await session.rollback()
            return False
        # #1133: the consume is the single-shot guard for the whole
        # redemption, so it decides before the removal is made durable.
        # ``InventoryUseService.use`` hands unwarn to the handler
        # UNCONSUMED, so two concurrent taps both clear its ``used=0``
        # pre-check and both land here on separate moderation sessions,
        # each finding a DIFFERENT active warning to soft-delete. Only
        # one ``consume`` UPDATE can match ``used=0``; the loser must
        # put its warning back, or one 800-coin item lifts two
        # warnings. Committing the removal first — the previous shape —
        # made that compensation impossible.
        consumed = await inventory_repo.consume(user_id=user_id, inventory_id=entry_id, now=now)
        if not consumed:
            await session.rollback()
            return False
        # Moderation commits while the economy session still holds the
        # consume uncommitted: a later failure on the economy side
        # therefore errs toward the user (warning gone, item kept),
        # which is the tear direction this handler documents above.
        await session.commit()
    return True


async def handle_inventory_use(
    callback: CallbackQuery,
    callback_data: InventoryUse,
    inventory_use_service: InventoryUseService,
    inventory_repo: InventoryRepo,
    state: FSMContext,
    registry: EngineRegistry,
    settings: Settings,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """🎁 Use click → run :class:`InventoryUseService.use` → render outcome.

    Authorization rides entirely on the service: it composes
    :meth:`InventoryRepo.get_for_user` with ``user_id ==
    callback.from_user.id``, so a hand-crafted
    ``inv_use:<other-user's entry>`` reaches the service with the
    caller's id and surfaces NOT_FOUND — the same outcome a deleted-
    entry would produce, leaking nothing about whether the id belongs
    to someone. The inspect handler's belt-and-braces user_id assert
    isn't repeated here because the service IS the authoritative gate
    (the inspect-time assert exists only because the inspect handler
    rendered private-data fields before the service was wired).

    Outcome → card mapping is exhaustive over :class:`UseOutcome` so a
    future enum value triggers a mypy ``Never`` on the assertion
    rather than silently rendering an empty card. All terminal — the
    result keyboard carries only a Back button so the user can return
    to the paginated list. UNKNOWN_EFFECT is reachable in practice
    via a race (admin deletes the catalog row between inspect render
    and Use click, or two users hold inspect cards for the same row
    and the catalog row changes type underneath) even though the
    inspect handler tries to hide the button for UNKNOWN items —
    keeping the branch live means the handler degrades gracefully on
    races rather than silently consuming.
    """
    assert callback.from_user is not None  # filter guarantees
    # Naive ``datetime.now()`` to match the rest of the inventory /
    # economy code path (see :class:`InventoryRepo`'s docstring on
    # why legacy parity uses naive local time). The service threads
    # this same instant into the planner, the ``used_date`` write and
    # the grant TTL so the audit trail is internally consistent.
    now = datetime.now()  # noqa: DTZ005 — match legacy naive convention
    result = await inventory_use_service.use(
        user_id=callback.from_user.id,
        entry_id=callback_data.entry_id,
        now=now,
    )
    outcome = result.outcome

    # NEEDS_TITLE_INPUT: custom_title is two-step. Park the user in the
    # FSM awaiting-title state (stashing the pending entry id) and prompt
    # for the title text — the consume + grant happen in
    # ``handlers/custom_title.handle_custom_title_text`` when the next
    # message lands. We do NOT render the terminal result keyboard here:
    # the flow continues in the chat, not on this card.
    if outcome is UseOutcome.NEEDS_TITLE_INPUT:
        from telegram_invite_bot.scheduler.fsm_sweeper import (
            STATE_ENTERED_AT_FIELD,
            utc_now_iso,
        )

        await state.set_state(CustomTitleStates.awaiting_title)
        await state.set_data(
            {
                PENDING_ENTRY_FIELD: callback_data.entry_id,
                STATE_ENTERED_AT_FIELD: utc_now_iso(),
                "lang": lang,
            }
        )
        # #1955: the ack must not be able to swallow the prompt. The
        # FSM parking above is already durable — ``SQLiteStorage``
        # commits on its own connection, outside the update's
        # transaction — so a ``TelegramBadRequest`` here (an expired
        # query id, the one failure mode ``answer`` has) would leave
        # the user in ``awaiting_title`` having never been asked for a
        # title: the next thing they type, whatever it is, becomes
        # their group title and burns the item. Same suppress the
        # buy-confirm path already uses two screens down.
        with contextlib.suppress(TelegramBadRequest):
            await callback.answer()
        await _safe_edit(callback, t("h_item_custom_title_prompt", lang), markup=None)
        log.bind(uid=callback.from_user.id, entry_id=callback_data.entry_id).info(
            "custom_title FSM started from /inventory use"
        )
        return

    # NEEDS_MODERATION: unwarn lives in moderation.db. Remove one active
    # warning in the main chat; only consume the entry if a warning was
    # actually removed (refuse-without-consume on no-warning).
    if outcome is UseOutcome.NEEDS_MODERATION:
        removed = await _apply_unwarn(
            registry=registry,
            inventory_repo=inventory_repo,
            user_id=callback.from_user.id,
            entry_id=callback_data.entry_id,
            main_chat_id=settings.bot.main_chat_id,
            now=now,
        )
        body = t("h_item_unwarn_success", lang) if removed else t("h_item_unwarn_none", lang)
        if removed and checkpoint is not None:
            # #1277: unwarn is the one redemption whose two halves live
            # in different databases, and ``_apply_unwarn`` has just
            # made the moderation half DURABLE while the economy
            # ``consume`` still rides the middleware's post-handler
            # commit. Both calls below are Telegram round-trips, and
            # anything they raise — a ``TelegramRetryAfter`` from
            # re-clicking the button, a 5xx, a 403 from a user who
            # blocked the bot — reaches ``middlewares.base``, which
            # rolls the economy session back. The warning would stay
            # gone and the entry would stay unspent, so one 800-coin
            # item clears warnings for as long as the user can keep
            # provoking the failure. Committing here narrows it to the
            # width of one commit: the moderation half is already
            # durable (``_apply_unwarn`` commits its own session), so
            # what this call makes durable is the economy consume, and
            # everything after it is only a card.
            #
            # #1656: "narrows", not "closes". If THIS commit is the one
            # that loses — SQLITE_BUSY off the legacy bot writing the
            # same file with raw sqlite3 — the warning is gone and the
            # entry is unspent, which is the whole hole again. Nothing
            # available here can close it: the two halves are two
            # SQLite files and there is no 2PC. What #1493 added is
            # that such an outcome now names itself in the journal
            # instead of looking like a clean rollback.
            await checkpoint()
        # #1955: same reasoning as the custom_title branch above, one
        # commit later. The unwarn and the consume are both durable by
        # now — that is what the ``checkpoint`` on the line above is
        # for — so an ack that raises costs the user the only card that
        # would have told them their warning is gone.
        with contextlib.suppress(TelegramBadRequest):
            await callback.answer()
        # On refusal (no warning) the item is NOT consumed — keep the
        # entry inspectable by snapping back to the list rather than a
        # terminal card, so the user can still see / use it elsewhere.
        await _safe_edit(callback, body, markup=_build_inventory_use_result_keyboard(lang))
        log.bind(
            uid=callback.from_user.id,
            entry_id=callback_data.entry_id,
            removed=removed,
        ).info("/inventory unwarn item used")
        return

    if outcome is UseOutcome.SUCCESS:
        assert result.kind is not None  # service invariant on SUCCESS
        body = _format_inventory_use_success(
            kind=result.kind,
            lang=lang,
            granted_till=result.granted_till,
            buster_expires_at=result.buster_expires_at,
            color_nick_expires_at=result.color_nick_expires_at,
            mute_protection_expires_at=result.mute_protection_expires_at,
            coin_payout_amount=result.coin_payout_amount,
            xp_boost_expires_at=result.xp_boost_expires_at,
            xp_boost_multiplier=result.xp_boost_multiplier,
        )
    elif outcome is UseOutcome.NOT_FOUND:
        body = t("h_inventory_use_not_found", lang)
    elif outcome is UseOutcome.ALREADY_USED:
        body = t("h_inventory_use_already_used", lang)
    elif outcome is UseOutcome.EXPIRED:
        body = t("h_inventory_use_expired", lang)
    elif outcome is UseOutcome.UNKNOWN_EFFECT:
        body = t("h_inventory_use_unknown", lang)
    elif outcome is UseOutcome.BUSTER_ALREADY_ACTIVE:
        # #1903: the entry was NOT consumed, so the card has to say so —
        # the user is looking at an item that is still theirs.
        body = _format_buster_already_active(lang=lang, expires_at=result.buster_expires_at)
    else:  # pragma: no cover — exhausted above; mypy Never-check.
        return

    await callback.answer()
    await _safe_edit(
        callback,
        body,
        markup=_build_inventory_use_result_keyboard(lang),
    )
    log.bind(
        uid=callback.from_user.id,
        entry_id=callback_data.entry_id,
        outcome=outcome.value,
        kind=result.kind.value if result.kind is not None else None,
    ).info("/inventory entry used")


# ── Stage 24: inline-buy callback flow ──────────────────────────────


def _build_confirm_keyboard(item_id: int, lang: str, *, group_id: int = 0) -> InlineKeyboardMarkup:
    """Confirm / Cancel pair under the confirmation card.

    Both buttons live on the same row so they read as a coupled
    decision; a stacked layout would invite a misclick on a tall
    confirm button. Legacy ``shop_confirm_buy_*`` rendered the same
    side-by-side pair (bot.py inline keyboard builder).

    ``group_id`` rides on the Confirm button so the money step knows
    which group to credit — and re-verifies it before crediting anything
    (see :func:`_verify_group_scope`).
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("h_shop_buy_confirm_btn", lang),
                    callback_data=ShopBuyConfirm(item_id=item_id, group_id=group_id).pack(),
                ),
                InlineKeyboardButton(
                    text=t("h_shop_buy_cancel_btn", lang),
                    callback_data=ShopBuyCancel().pack(),
                ),
            ]
        ]
    )


def _current_page_from_markup(message: object) -> int | None:
    """Sniff the currently displayed page out of the indicator button.

    The /shop nav row's middle button is a :class:`ShopPage` packed
    with the page number being displayed; the prev/next buttons pack
    ``page ± 1``, so the indicator is the unique button whose page is
    NOT a neighbour of any other ShopPage button. In practice the
    indicator is also the only ShopPage button on its row that's NOT
    accompanied by directional text, but parsing button labels is
    fragile across locales — the page-arithmetic discriminator is
    locale-independent.

    Returns ``None`` if the markup is missing, doesn't contain any
    ShopPage buttons, or the row shape is unexpected (e.g. someone
    pointed a Stage-24 keyboard at a Stage-25 callback). Callers fall
    back to attempting the edit; ``_safe_edit`` absorbs the
    "message is not modified" error from a redundant edit.
    """
    if not isinstance(message, MessageType):
        return None
    markup = message.reply_markup
    if markup is None:
        return None
    # The indicator button is the unique nav button whose label
    # contains ``/`` (rendered by ``h_shop_nav_indicator`` as
    # ``{current}/{total}``); prev/next labels render arrows + the
    # word "prev"/"next" (or RU equivalents). Matching on ``/`` is
    # locale-independent because the indicator template carries the
    # slash literally in both ru.yaml and en.yaml.
    for row in markup.inline_keyboard:
        for btn in row:
            data = btn.callback_data
            if data is None or not data.startswith("shop_pg:"):
                continue
            if "/" not in btn.text:
                continue
            try:
                # ``shop_pg:<page>:<group_id>`` since #14 — take the page
                # segment only. Splitting on every ``:`` rather than
                # ``maxsplit=1`` is what keeps this correct now that the
                # payload carries a trailing field.
                return int(data.split(":")[1])
            except (IndexError, ValueError):
                return None
    return None


async def _safe_edit(
    callback: CallbackQuery,
    text: str,
    *,
    markup: InlineKeyboardMarkup | None,
) -> None:
    """Edit ``callback.message`` to ``text`` + ``markup``, absorbing the
    three predictable failure modes — but NOT the same way for all
    three, which is what this docstring used to get wrong:

    * ``TelegramBadRequest`` "message is not modified" — idempotent
      re-clicks on a button whose target text is unchanged shouldn't
      poison the dispatcher.
    * ``TelegramBadRequest`` "message to edit not found" — user
      deleted the message between render and click.

      Those two DO fall back to a fresh ``answer`` on the same message,
      so the user sees the new state.

    * ``InaccessibleMessage`` (>48h old in private; >24h in groups) or
      ``None`` (the card lives in an inline message) — there is no
      ``edit_text`` and, for the ``None`` case, no addressable chat
      either. The isinstance narrowing below returns SILENTLY: no
      fallback, no card, nothing the user sees. Whatever the caller
      committed before calling here still happened.

      This is the repo-wide posture, not a local slip — every other
      callback surface narrows on ``MessageType`` and bails the same
      way (``language.py:119``, ``support.py:776``, ``ads.py:444``,
      ``duel.py:685``, ``broadcast.py:327`` …). Turning the silence
      into a fresh ``bot.send_message`` is defensible — an inaccessible
      card is exactly the case where the user gets nothing — but it
      changes behaviour on every one of those surfaces at once, so it
      is an owner-level decision and is recorded rather than taken
      here. The docstring used to claim the fallback covered "any of
      these", which read as a guarantee the code never made.

    A rejection of that fallback is NOT swallowed. The suppress that
    used to sit here named "the user blocked the bot" as its reason —
    but that raises :class:`TelegramForbiddenError`, which a
    ``TelegramBadRequest`` guard never caught in the first place. All
    it actually hid was a malformed body: our own bug, on a card that
    ``handle_shop_buy_confirm`` uses to show a *completed purchase*.
    Letting it out means ``middlewares.base`` rolls the debit back
    instead of charging for a receipt nobody could read.
    """
    if not isinstance(callback.message, MessageType):
        return
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=markup)


async def handle_shop_buy_prompt(
    callback: CallbackQuery,
    callback_data: ShopBuyPrompt,
    shop_items_repo: ShopItemsRepo,
    economy_repo: EconomyRepo,
    registry: EngineRegistry,
    group_percent: int,
    lang: str,
) -> None:
    """First click — render the confirmation card.

    No mutation here. The card surfaces the *current* balance and item
    price so the user can decide without a separate /balance round
    trip; this read is cheap (two PK selects on the shared session) and
    is the same boundary legacy used (``shop_buy_<id>`` ladder rendered
    "Подтвердите покупку <name> за <price>?" with the wallet line).

    #14: a group-scoped click re-verifies ownership here (first of two
    checks — the second is at confirm, right before the money moves) and
    previews the exact number of rating points the group will earn, so
    the perk is a concrete promise on the card rather than a percentage
    the user has to do arithmetic on.
    """
    assert callback.from_user is not None  # filter guarantees
    item = await shop_items_repo.get(callback_data.item_id)
    if item is None:
        # Catalog row deleted between /shop render and click. Toast
        # without touching the message so the existing prompt stays
        # available for the user to click a *different* item.
        await callback.answer(t("h_shop_buy_not_found_toast", lang), show_alert=False)
        return
    if item.stock == 0:
        await callback.answer(t("h_shop_buy_out_of_stock_toast", lang), show_alert=False)
        return

    scope: _GroupScope | None = None
    if callback_data.group_id != 0 and group_percent > 0:
        scope = await _verify_group_scope(
            registry,
            group_id=callback_data.group_id,
            user_id=callback.from_user.id,
            lang=lang,
        )
        if scope is None:
            # Group left / ownership transferred / hand-crafted payload.
            # Toast and leave the catalog card intact so the user can
            # re-pick rather than losing their place.
            await callback.answer(t("h_shop_group_lost_toast", lang), show_alert=True)
            return

    balance = await _wallet_balance(economy_repo, callback.from_user.id)
    body = t(
        "h_shop_buy_prompt",
        lang,
        name=html.escape(item.name),
        price=item.price,
        balance=balance,
    )
    if scope is not None:
        body += "\n" + t(
            "h_shop_buy_prompt_group_line",
            lang,
            name=html.escape(scope.title),
            amount=purchase_commission_amount(item.price, group_percent),
        )
    # answer first to clear the spinner — if the edit below races a
    # transient Telegram hiccup the user still sees a responsive click.
    await callback.answer()
    await _safe_edit(
        callback,
        body,
        markup=_build_confirm_keyboard(
            item.id, lang, group_id=scope.group_id if scope is not None else 0
        ),
    )
    log.bind(
        uid=callback.from_user.id,
        item_id=item.id,
        balance=balance,
        group_id=scope.group_id if scope is not None else 0,
    ).info("/shop buy prompt rendered")


async def handle_shop_buy_confirm(
    callback: CallbackQuery,
    callback_data: ShopBuyConfirm,
    purchase_service: PurchaseService,
    group_donation_service: GroupDonationService,
    inventory_use_service: InventoryUseService,
    economy_repo: EconomyRepo,
    registry: EngineRegistry,
    group_percent: int,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Second click — run the atomic purchase.

    Outcome → card mapping is exhaustive over :class:`PurchaseStatus`
    (#2006's ``NO_EFFECT`` included)
    so a future enum value triggers a mypy ``Never`` on the assertion
    rather than silently rendering an empty card. The success and
    insufficient-funds branches drop the keyboard (terminal states);
    out-of-stock and not-found also drop it — a user who scrolls back
    and re-clicks would otherwise hit the same outcome and produce
    duplicate noise.

    #14 group routing, in this exact order:

    1. **re-verify** the wire-supplied ``group_id`` (a second check, not
       a duplicate one: ownership can change between prompt and confirm,
       and this is the call that actually moves money);
    2. **resolve the group's creator over the Telegram API** — BEFORE
       the purchase, so the round-trip never sits inside an open SQLite
       write transaction; SQLite serialises writers process-wide, so a
       slow API call there would stall every other writer. This is
       prophylaxis, not a legacy bug fix: legacy committed first and
       resolved after (``bot.py:10744`` then ``:10746``);
    3. run the purchase;
    4. route the group's slice on the SAME session, so the rating points
       and the owner payout commit with the debit or roll back with it.

    A failed ownership check aborts *before* the purchase: silently
    downgrading to a global buy would charge the user for something
    other than what the card promised.

    #192: this path auto-applies exactly as ``/buy`` does. It did not,
    and the two surfaces sell the SAME rows — so a 🎁 bought from the
    /shop card left its coins un-paid and its inventory row ``used=0``,
    while the identical ``/buy 3`` paid out. Which button the buyer
    happened to press is not a thing the catalog should price.
    """
    assert callback.from_user is not None  # filter guarantees
    scope: _GroupScope | None = None
    owner_id: int | None = None
    if callback_data.group_id != 0 and group_percent > 0:
        scope = await _verify_group_scope(
            registry,
            group_id=callback_data.group_id,
            user_id=callback.from_user.id,
            lang=lang,
        )
        if scope is None:
            await callback.answer(t("h_shop_group_lost_toast", lang), show_alert=True)
            return
        if callback.bot is not None:
            owner_id = await chat_creator_id(callback.bot, scope.group_id)

    # #1761: one confirm card is one purchase.
    #
    # The claim goes here, AFTER the group-ownership refusal above, so a
    # tap that is about to be refused does not spend the card — the
    # buyer fixes the group and taps the same card again. It is NOT what
    # keeps one person from burning another's card: the key is
    # ``(chat_id, message_id)``, this router renders cards in private
    # only (``build_router`` filters the message side to
    # ``ChatType.PRIVATE``), and a forwarded copy is a new message in a
    # new chat, so it claims its own key and leaves the original alone.
    # ``get`` and ``put`` are both synchronous with no ``await`` between
    # them, so this is atomic under asyncio without any lock.
    card: tuple[int, int] | None = None
    msg = callback.message
    if msg is not None:
        card = (msg.chat.id, msg.message_id)
        now = time.monotonic()
        if _spent_cards.get(card, now) is not None:
            # A second tap on a card that already bought. Answer so the
            # client stops spinning and leave the receipt as it stands.
            with contextlib.suppress(TelegramBadRequest):
                await callback.answer()
            return
        _spent_cards.put(card, True, now)

    try:
        outcome = await purchase_service.purchase(
            user_id=callback.from_user.id, item_id=callback_data.item_id
        )
        status = outcome.status
        if status is PurchaseStatus.OK:
            assert outcome.item is not None
            applied, balance_override = await _auto_apply_purchase(
                outcome, callback.from_user.id, inventory_use_service, economy_repo, lang
            )
            balance = balance_override if balance_override is not None else outcome.new_balance
            body = t(
                "h_shop_buy_success",
                lang,
                name=html.escape(outcome.item.name),
                price=outcome.item.price,
                balance=balance if balance is not None else 0,
            )
            if applied is not None:
                body = f"{body}\n\n{applied}"
            if scope is not None:
                reveal = await _route_group_slice(
                    group_donation_service,
                    scope=scope,
                    buyer_id=callback.from_user.id,
                    price=outcome.item.price,
                    owner_id=owner_id,
                    lang=lang,
                )
                if reveal:
                    body = f"{body}\n{reveal}"
        elif status is PurchaseStatus.INSUFFICIENT_FUNDS:
            assert outcome.item is not None
            body = t("h_shop_buy_insufficient", lang, price=outcome.item.price)
            # #1968: the guarded debit matched zero rows and promoted
            # the connection to ``BEGIN IMMEDIATE`` doing it. See the
            # matching branch in ``handle_buy``; the note below the
            # chain explains why the SUCCESS branch must not do this.
            if checkpoint is not None:
                await checkpoint()
        elif status is PurchaseStatus.OUT_OF_STOCK:
            # Re-fetch may have surfaced an item or not (legacy parity:
            # the outcome carries the item if pre-check fired, None if the
            # row vanished between fetch and stock-guard rollback). Render
            # a stable card either way.
            name = html.escape(outcome.item.name) if outcome.item is not None else "—"
            body = t("h_shop_buy_out_of_stock_card", lang, name=name)
        elif status is PurchaseStatus.ITEM_NOT_FOUND:
            body = t("h_shop_buy_not_found_card", lang)
        elif status is PurchaseStatus.NO_EFFECT:
            # #2006. Reachable from a keyboard rendered before the type
            # stopped being sellable — the listing filter cannot reach
            # back into chat history, which is why the service refuses
            # too. The item is always present here (the service only
            # returns this status after fetching the row), but the
            # ``is None`` arm mirrors the OUT_OF_STOCK branch above
            # rather than asserting: a card is cheaper than a traceback.
            name = html.escape(outcome.item.name) if outcome.item is not None else "—"
            body = t("h_shop_buy_no_effect_card", lang, name=name)
        else:  # pragma: no cover — exhausted above; mypy Never-check.
            return

        # No ``checkpoint()`` on the SUCCESS branch, deliberately, and
        # this is the one money handler where that is right:
        # ``handle_buy`` a few hundred lines up raises
        # ``UndeliverableResultError`` for exactly this case, on the
        # stated policy that "a purchase that never happened beats one
        # the buyer was charged for and never saw". Refunding an
        # undeliverable receipt REQUIRES holding the transaction open until
        # the receipt has landed, so committing early here would silently
        # give the two shop surfaces opposite answers for the same event.
        # The cost — economy.db's writer slot held across one edit — is
        # what that policy is worth paying.
        #
        # #1968: that argument is about a purchase that HAPPENED, so it
        # says nothing about the refusals. INSUFFICIENT_FUNDS releases
        # the lock in its own branch above — there is no sale to undo
        # there, only a zero-row UPDATE's lock to give back.
        #
        # The toast is the exception. "query is too old and response
        # timeout expired" is a routine ``TelegramBadRequest`` about the
        # age of a click, says nothing about whether the chat can receive
        # the card, and unsuppressed it was the only call here that could
        # cancel a sale: it raised BEFORE the receipt below was even
        # attempted, so the buyer got no card, no toast and no item, and
        # the log line at the bottom never ran either.
        with contextlib.suppress(TelegramBadRequest):
            await callback.answer()
        await _safe_edit(callback, body, markup=None)
        log.bind(
            uid=callback.from_user.id,
            item_id=callback_data.item_id,
            status=status.value,
        ).info("/shop buy confirm processed")
    except BaseException:
        # The purchase did not reach a rendered receipt, so release the
        # card: a transient DB or network failure should cost the buyer
        # a re-tap, not the item.
        if card is not None:
            _spent_cards.discard(card)
        raise


async def _route_group_slice(
    service: GroupDonationService,
    *,
    scope: _GroupScope,
    buyer_id: int,
    price: int,
    owner_id: int | None,
    lang: str,
) -> str:
    """Run the group routing and render what actually landed.

    Reports only what the service confirms: the rating-points line
    whenever the slice was written, and the owner-payout line ONLY when
    a credit really succeeded (``owner_credited``). A group whose creator
    can't be resolved — bot demoted, creator account deleted — still
    climbs the leaderboard, and the receipt says exactly that instead of
    promising a payout nobody received.

    Routing is best-effort with respect to the purchase: the item is
    already the user's, so an unexpected DB error here must surface as a
    missing bonus line, never as a failed buy. The exception is
    swallowed and logged. Swallowing is only half of that promise —
    :meth:`GroupDonationService.route` wraps its writes in a SAVEPOINT
    so a half-finished slice cannot ride the purchase commit out (or, on
    a failed flush, take the purchase down with it). (This used to cite
    the commission block in :meth:`PurchaseService.purchase` as the
    precedent; T-020/R10 deleted that block, and
    ``purchase_service.py:222-226`` is now the note explaining why it is
    gone.)
    """
    try:
        result = await service.route(
            group_id=scope.group_id,
            user_id=buyer_id,
            price=price,
            owner_id=owner_id,
        )
    except Exception:  # noqa: BLE001 — a bonus must never void a purchase
        log.bind(uid=buyer_id, group_id=scope.group_id).exception("group purchase routing failed")
        return ""
    if result.outcome not in (
        GroupDonationOutcome.ROUTED,
        GroupDonationOutcome.ROUTED_NO_OWNER,
    ):
        return ""
    name = html.escape(scope.title)
    lines = [t("h_shop_group_routed", lang, name=name, amount=result.amount)]
    if result.owner_credited and result.to_owner > 0:
        lines.append(t("h_shop_group_routed_owner", lang, amount=result.to_owner))
    return "\n".join(lines)


async def handle_shop_group_pick(
    callback: CallbackQuery,
    callback_data: ShopGroupPick,
    shop_items_repo: ShopItemsRepo,
    economy_repo: EconomyRepo,
    registry: EngineRegistry,
    group_percent: int,
    lang: str,
) -> None:
    """Chooser click — open the catalog scoped to the picked group.

    ``group_id == 0`` is the "just for me" row and skips verification
    entirely (there is nothing to authorize about buying for yourself).
    Any other id is verified before it reaches the catalog, so a
    hand-crafted ``shop_grp:<someone else's chat>`` dies here rather
    than rendering a card that promises to credit a group the caller
    doesn't own.
    """
    assert callback.from_user is not None  # filter guarantees
    scope: _GroupScope | None = None
    if callback_data.group_id != 0:
        scope = await _verify_group_scope(
            registry,
            group_id=callback_data.group_id,
            user_id=callback.from_user.id,
            lang=lang,
        )
        if scope is None:
            await callback.answer(t("h_shop_group_lost_toast", lang), show_alert=True)
            return

    items = await shop_items_repo.list_all()
    if not items:
        await callback.answer()
        await _safe_edit(callback, t("h_shop_empty", lang), markup=None)
        return
    balance = await _wallet_balance(economy_repo, callback.from_user.id)
    await callback.answer()
    await _safe_edit(
        callback,
        _format_shop(
            items,
            lang,
            page=0,
            balance=balance,
            scope=scope,
            group_percent=group_percent,
        ),
        markup=_build_shop_keyboard(
            items,
            lang,
            page=0,
            group_id=scope.group_id if scope is not None else 0,
            show_group_switch=True,
        ),
    )
    log.bind(
        uid=callback.from_user.id,
        group_id=scope.group_id if scope is not None else 0,
    ).info("/shop group picked")


async def handle_shop_group_menu(
    callback: CallbackQuery,
    callback_data: ShopGroupMenu,
    registry: EngineRegistry,
    group_percent: int,
    lang: str,
) -> None:
    """ "Change group" click — rebuild the chooser from the current list.

    Rebuilt rather than restored from the wire so a group added (or
    lost) since the card was rendered shows up correctly. A caller whose
    last group disappeared gets a toast and keeps the catalog they were
    on — dropping them onto an empty chooser would be a dead end.
    """
    del callback_data
    assert callback.from_user is not None  # filter guarantees
    groups = await _owned_groups(registry, callback.from_user.id)
    if not groups:
        await callback.answer(t("h_shop_group_none_toast", lang), show_alert=True)
        return
    await callback.answer()
    await _safe_edit(
        callback,
        _format_group_chooser(lang, percent=group_percent),
        markup=_build_group_chooser_keyboard(groups, lang),
    )
    log.bind(uid=callback.from_user.id, groups=len(groups)).info("/shop group chooser reopened")


async def handle_shop_buy_cancel(
    callback: CallbackQuery,
    callback_data: ShopBuyCancel,
    lang: str,
) -> None:
    """User backed out — strip the keyboard, show a brief cancel line.

    ``callback_data`` is unused but kept in the signature for symmetry
    with the prompt/confirm handlers (and so a future field on
    :class:`ShopBuyCancel` lands without re-wiring the registration).
    """
    del callback_data
    assert callback.from_user is not None  # filter guarantees
    await callback.answer()
    await _safe_edit(callback, t("h_shop_buy_cancelled", lang), markup=None)
    log.bind(uid=callback.from_user.id).info("/shop buy cancelled")


async def handle_shop_page(
    callback: CallbackQuery,
    callback_data: ShopPage,
    shop_items_repo: ShopItemsRepo,
    economy_repo: EconomyRepo,
    registry: EngineRegistry,
    group_percent: int,
    lang: str,
) -> None:
    """Page-flip click on /shop nav row.

    Re-reads the full catalog (cheap — same SELECT /shop uses) and
    edits the message in place to the requested page's body +
    keyboard. The read happens per click rather than caching the
    catalog in the wire payload because (a) the wire cap is 64 bytes
    and a catalog snapshot blows past that immediately, and (b) admin
    edits to stock/price between renders ought to surface on the next
    flip rather than being frozen at first-render time.

    Three failure shapes:

    * Catalog went empty between render and click → edit the message
      to the standard "shop is empty" line and drop the keyboard;
      a re-click is impossible without one. Answers the callback
      silently.
    * Catalog shrank so the requested page is past the new last page
      → :func:`_clamp_page` snaps to the new last page (so the user
      doesn't see an empty body just because an admin pulled rows).
    * Same-page click (the middle ``N/M`` indicator carries
      ``ShopPage(page=current)``) → answer the callback with no edit
      so Telegram doesn't reject the unchanged ``edit_text`` with
      "message is not modified" and poison the dispatcher.

    No chat-type filter — keyboards are only ever rendered in private
    (the message handler is private-gated) but a forwarded button on
    a stale message should still resolve through this handler rather
    than 404-ing into legacy. Same posture as the Stage 24 buy/confirm
    callbacks.
    """
    assert callback.from_user is not None  # filter guarantees
    items = await shop_items_repo.list_all()
    if not items:
        await callback.answer()
        await _safe_edit(callback, t("h_shop_empty", lang), markup=None)
        return

    # #14: the group scope rides on every nav button, and is re-verified
    # per flip — a group lost mid-browse must not keep advertising a
    # bonus that the confirm step would then refuse.
    scope: _GroupScope | None = None
    if callback_data.group_id != 0 and group_percent > 0:
        scope = await _verify_group_scope(
            registry,
            group_id=callback_data.group_id,
            user_id=callback.from_user.id,
            lang=lang,
        )
        if scope is None:
            await callback.answer(t("h_shop_group_lost_toast", lang), show_alert=True)
            return

    target = _clamp_page(callback_data.page, len(items))
    # Same-page detection: the indicator button packs the *current*
    # page, so a click on it produces ``callback_data.page ==
    # current_page``. We sniff the current page out of the existing
    # message's reply_markup (the indicator button's packed payload)
    # rather than caching it server-side. If the markup is absent
    # (forwarded message, very old client, test fixture without a
    # keyboard attached) we fall through to the edit path —
    # ``_safe_edit`` already swallows the "message is not modified"
    # TelegramBadRequest that would result, so the worst case is a
    # wasted edit attempt, not a dispatcher poison.
    current_page = _current_page_from_markup(callback.message)
    if current_page is not None and current_page == target:
        await callback.answer()
        return

    balance = await _wallet_balance(economy_repo, callback.from_user.id)
    body = _format_shop(
        items,
        lang,
        page=target,
        balance=balance,
        scope=scope,
        group_percent=group_percent,
    )
    # Whether to keep the "change group" row is derived from what the
    # caller owns *now*, not sniffed off the old card's markup: a group
    # added since the card was rendered should make the row appear, and
    # a verified scope already proves ownership without a second query.
    show_switch = scope is not None or (
        group_percent > 0 and bool(await _owned_groups(registry, callback.from_user.id))
    )
    await callback.answer()
    await _safe_edit(
        callback,
        body,
        markup=_build_shop_keyboard(
            items,
            lang,
            page=target,
            group_id=scope.group_id if scope is not None else 0,
            show_group_switch=show_switch,
        ),
    )
    log.bind(
        uid=callback.from_user.id,
        requested_page=callback_data.page,
        rendered_page=target,
        pages=_page_count(len(items)),
        group_id=scope.group_id if scope is not None else 0,
    ).info("/shop page flipped")


# T-020/R10 removed ``_notify_referrer_kickback`` from this module. A
# coin-paid shop buy no longer pays the inviter anything, so there is
# no kickback to announce. The identical DM still fires on the paths
# that DO pay one — the four real-money top-ups — from
# ``handlers/topup.py`` and ``webhook/payments.py``.


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Factory — fresh ``Router`` + middleware per call so tests can re-wire.

    Separate router from ``handlers/economy`` so future shop-specific
    middlewares (rate-limit on /buy, anti-flood on /shop spam) attach
    here without touching the /balance path. The middleware itself is
    shared (same ``economy.db`` session) — different routers, same
    EconomyMiddleware class, each opening its own session per update.

    Stage 24: the message-side router stays private-only via the
    router-level filter; the callback_query side gets ITS OWN
    EconomyMiddleware (a separate :class:`Observer` on the router) so
    a Confirm click opens a fresh ``economy.db`` session distinct from
    whatever message handler last fired. Without the parallel
    middleware attach the callback handlers would resolve their
    ``purchase_service`` parameter to ``None`` at injection time and
    crash inside the dispatcher — same shape as the ``/balance``
    callback would face if it ever grew one. The chat-type filter is
    NOT extended to the callback side: keyboards are only ever
    rendered in private (the message handler that produces them is
    private-gated), and a stray click on a forwarded button should
    still resolve through the new pipeline. Recorded as a deliberate
    #1608 exemption rather than an omission.
    """
    router = Router(name="shop")
    # Private-chat-only for the message side. Router-level filter
    # enforces this once for every message handler below.
    router.message.filter(F.chat.type == ChatType.PRIVATE)

    # T-020/R10: no referral percent is threaded in — a coin-paid /buy
    # mints nothing. The developer percent and recipient stay because
    # the RR-2 #14 group-donation split still uses them.
    def _economy_mw() -> EconomyMiddleware:
        return EconomyMiddleware(
            registry,
            developer_commission_percent=settings.economy.developer_commission_percent,
            developer_id=settings.bot.admin_chat_id,
            purchase_donation_to_group_percent=(
                settings.economy.purchase_donation_to_group_percent
            ),
        )

    router.message.middleware(_economy_mw())
    # Mirror the middleware on the callback observer so the three Stage 24
    # handlers below see ``shop_items_repo`` / ``economy_repo`` /
    # ``purchase_service`` in their handler ``data`` dict.
    router.callback_query.middleware(_economy_mw())
    # #14: ``registry`` (for the users-DB group lookups) and the group
    # percentage are NOT in handler data — the middleware only binds
    # economy-session objects — so the five group-aware handlers get them
    # through closures, the same pattern ``_use_entry`` below uses.
    group_percent = settings.economy.purchase_donation_to_group_percent

    async def _shop_entry(
        message: Message,
        shop_items_repo: ShopItemsRepo,
        economy_repo: EconomyRepo,
        lang: str,
    ) -> None:
        await handle_shop(message, shop_items_repo, economy_repo, registry, group_percent, lang)

    router.message.register(
        _shop_entry,
        Command(
            "shop",
            "магазин",
            "store",
            "kom_shop",
            ignore_case=True,
            magic=F.args.is_(None),
        ),
    )
    router.message.register(
        handle_inventory,
        Command("inventory", "inv", "инвентарь", ignore_case=True),
        # Channel posts have no user inventory; bail at routing time.
        F.from_user,
    )
    # ``/buy`` accepts an arg (``item_id``) — opposite of /shop which
    # requires NO arg (``magic=F.args.is_(None)``). Bare ``/buy`` falls
    # through to handle_buy itself which renders the usage hint.
    router.message.register(
        handle_buy,
        # ``kom_buy``: legacy registered it and the guide documents it —
        # see handlers/moderation.py for the same #116 omission.
        Command("buy", "купить", "kom_buy", ignore_case=True),
        F.from_user,
    )

    async def _buy_prompt_entry(
        callback: CallbackQuery,
        callback_data: ShopBuyPrompt,
        shop_items_repo: ShopItemsRepo,
        economy_repo: EconomyRepo,
        lang: str,
    ) -> None:
        await handle_shop_buy_prompt(
            callback,
            callback_data,
            shop_items_repo,
            economy_repo,
            registry,
            group_percent,
            lang,
        )

    async def _buy_confirm_entry(
        callback: CallbackQuery,
        callback_data: ShopBuyConfirm,
        purchase_service: PurchaseService,
        group_donation_service: GroupDonationService,
        inventory_use_service: InventoryUseService,
        economy_repo: EconomyRepo,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_shop_buy_confirm(
            callback,
            callback_data,
            purchase_service,
            group_donation_service,
            inventory_use_service,
            economy_repo,
            registry,
            group_percent,
            lang,
            checkpoint=checkpoint,
        )

    async def _page_entry(
        callback: CallbackQuery,
        callback_data: ShopPage,
        shop_items_repo: ShopItemsRepo,
        economy_repo: EconomyRepo,
        lang: str,
    ) -> None:
        await handle_shop_page(
            callback,
            callback_data,
            shop_items_repo,
            economy_repo,
            registry,
            group_percent,
            lang,
        )

    async def _group_pick_entry(
        callback: CallbackQuery,
        callback_data: ShopGroupPick,
        shop_items_repo: ShopItemsRepo,
        economy_repo: EconomyRepo,
        lang: str,
    ) -> None:
        await handle_shop_group_pick(
            callback,
            callback_data,
            shop_items_repo,
            economy_repo,
            registry,
            group_percent,
            lang,
        )

    async def _group_menu_entry(
        callback: CallbackQuery,
        callback_data: ShopGroupMenu,
        lang: str,
    ) -> None:
        await handle_shop_group_menu(callback, callback_data, registry, group_percent, lang)

    router.callback_query.register(
        _buy_prompt_entry,
        ShopBuyPrompt.filter(),
        F.from_user,
    )
    router.callback_query.register(
        _buy_confirm_entry,
        ShopBuyConfirm.filter(),
        F.from_user,
    )
    router.callback_query.register(
        handle_shop_buy_cancel,
        ShopBuyCancel.filter(),
        F.from_user,
    )
    router.callback_query.register(
        _page_entry,
        ShopPage.filter(),
        F.from_user,
    )
    # #14: the group chooser rows and the "change group" button. Both
    # re-verify ownership inside the handler — the wire ``group_id`` is
    # a claim, never an authorization.
    router.callback_query.register(
        _group_pick_entry,
        ShopGroupPick.filter(),
        F.from_user,
    )
    router.callback_query.register(
        _group_menu_entry,
        ShopGroupMenu.filter(),
        F.from_user,
    )
    # Stage 26: /inventory pagination + inspect callbacks share the
    # same callback-side EconomyMiddleware (registered above) so they
    # see ``inventory_repo`` injected from the same registry. All three
    # gate on ``F.from_user`` for the same reason the Stage 24 buy
    # callbacks do — channel-cast callbacks have no caller identity to
    # authorize against.
    router.callback_query.register(
        handle_inventory_page,
        InventoryPage.filter(),
        F.from_user,
    )
    router.callback_query.register(
        handle_inventory_inspect,
        InventoryInspect.filter(),
        F.from_user,
    )
    router.callback_query.register(
        handle_inventory_back,
        InventoryBack.filter(),
        F.from_user,
    )

    # Stage 29 + L-21: /inventory item activation. Shares the callback-
    # side EconomyMiddleware (registered above) so the handler sees
    # ``inventory_use_service`` / ``inventory_repo`` injected from the
    # same registry as the inspect / back / page callbacks. ``state`` is
    # auto-injected by aiogram (FSM storage); ``registry`` and
    # ``settings`` are NOT in handler data, so the closure below supplies
    # them (same pattern as handlers/nick.py's ``_entry``). ``F.from_user``
    # gate mirrors the other inventory callbacks.
    async def _use_entry(
        callback: CallbackQuery,
        callback_data: InventoryUse,
        inventory_use_service: InventoryUseService,
        inventory_repo: InventoryRepo,
        state: FSMContext,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_inventory_use(
            callback,
            callback_data,
            inventory_use_service,
            inventory_repo,
            state,
            registry,
            settings,
            lang,
            checkpoint=checkpoint,
        )

    router.callback_query.register(
        _use_entry,
        InventoryUse.filter(),
        F.from_user,
    )
    # L-21: the custom_title FSM title-input step. Registered on the
    # message side (private-only via the router filter) so it shares the
    # message-side EconomyMiddleware — the handler needs
    # ``privileges_repo`` + ``inventory_repo`` + ``shop_items_repo`` from
    # the same economy session to consume + grant atomically. Gated on
    # ``CustomTitleStates.awaiting_title`` so only a user mid-flow is
    # claimed; everyone else's plain messages fall through. ``F.text``
    # ignores non-text messages (a sticker/photo isn't a title) and
    # ``register_text_expected`` says so out loud — the user stays
    # in-state to try again, which is the reprompt posture, but now with
    # a reprompt instead of silence.
    router.message.register(
        handle_custom_title_text,
        StateFilter(CustomTitleStates.awaiting_title),
        F.text,
        NOT_A_COMMAND,
        F.from_user,
    )
    register_text_expected(router, CustomTitleStates.awaiting_title)
    return with_chat_type_refusal(router, scope="private")
