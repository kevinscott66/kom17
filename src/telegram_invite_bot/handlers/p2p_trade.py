"""P2P buy-side + trade lifecycle UI (#64 P3, DESIGN_P2P.md §2.4-2.5).

Ports the legacy buy half of the P2P COM marketplace over the P1 money
core (:class:`P2pService`, injected by :class:`EconomyMiddleware`):

* order book + buy menu with the currency filter row and paging,
  price ASC (legacy bot.py:19750-19853 buy menu, 20095-20143 book);
* order detail → free-text buy amount OR "Купить всё"
  (bot.py:19805-19939), creating the trade via ``service.buy`` —
  trade card to the buyer (``p2p_trade_important`` instructions +
  "Я оплатил" / "Открыть спор" buttons) and a best-effort seller
  notification with the "COM отправлены" confirm button;
* express buy: currency → fiat budget → ``service.express_buy``
  fills cheapest-first; result summary + per-trade cards + per-fill
  seller notifications (bot.py:19591-19747);
* buyer "Я оплатил" (``mark_paid``, bot.py:19972-20021) + seller
  "COM отправлены" (``seller_confirm``, bot.py:20024-20065);
* dispute open (either party, bot.py:20187-20213): the OTHER party is
  notified and the admin chat (``settings.bot.admin_chat_id``) gets a
  card with THREE resolve buttons — refund_buyer / confirm_seller /
  return_seller (D1 adds the third, legacy bot.py:20220/20271 had two);
* admin resolve — developer-gated via ``settings.bot.is_developer``
  exactly like legacy's ``DEVELOPER_IDS`` check (the service trusts
  ``admin_id`` for the audit stamp only);
* seller-stats popup (bot.py:20159-20184) — D3: real counters
  ("✅ сделок / ⚠️ споров / 💰 продано"), no fake 5.0 rating.

Cross-party notifications are rendered in the ACTING user's language,
matching the ``/check`` creator notification (``handlers/checks.py:584``,
which DMs the creator in the *claimer's* language). That is an
internal-consistency choice, not legacy parity and not a hard limit:

* legacy sent no other-party notification at all — its dispute handler
  (bot.py:20187-20213) answers the opener's toast, flips the status and
  posts the admin card, nothing more, so nothing there constrains the
  language of a message this port *adds*;
* the recipient's language IS reachable. ``users_repo`` and
  ``user_settings_repo`` ride the dispatcher-level ``SessionMiddleware``
  on ``callback_query`` (mounted in ``AppProvider.dispatcher``,
  ``di/providers.py``), so
  ``language_for_user`` (``middlewares/language.py:109-140``) works here
  exactly as ``handlers/support.py:648-653`` uses it for its third-party
  DM. This module simply doesn't inject those repos yet.

The admin dispute card is a genuine divergence: legacy resolved its
button labels from ``get_user_language(DEVELOPER_IDS[0])``
(bot.py:20200) and hardcoded the body in Russian, while this port
renders both in the *opener's* language (and ``handle_admin_resolve``
DMs both parties in the *admin's*). Cosmetic only — the buttons carry
``DisputeResolution`` wire values in their ``callback_data`` and the
resolve handler dispatches off those, never off the label, so a card in
an unexpected language can be mis-read but cannot route to the wrong
outcome.

Private-chat-only on the message side; the callback side carries no
chat-type filter because the dispute-resolution card lives in the admin
chat (often a group) and its buttons must keep working there. Every
user-facing callback only ever exists on cards this module sent to
private chats, and the admin buttons re-gate on ``is_developer``.
"""

from __future__ import annotations

import contextlib
import html
import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
)
from aiogram.filters import StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import Message as MessageType
from loguru import logger

from telegram_invite_bot.core.p2p import P2P_CURRENCIES, is_supported_currency
from telegram_invite_bot.handlers.fsm_text import NOT_A_COMMAND, register_text_expected
from telegram_invite_bot.i18n import t

# CallbackData factories — OWNED by keyboards/builders/p2p.py (landed with
# the P2 cluster); P3 only consumes them. NOTE: P2pBuyMenu carries no
# ``currency`` field, so the buy menu's filter row routes into the
# (functionally identical) order-book view, which does carry one.
from telegram_invite_bot.keyboards.builders.p2p import (
    P2pBuyAll,
    P2pBuyMenu,
    P2pDisputeOpen,
    P2pDisputeResolve,
    P2pExpressCurrency,
    P2pExpressStart,
    P2pMenu,
    P2pMyTrades,
    P2pOrderBook,
    P2pOrderView,
    P2pSellerStats,
    P2pTradeConfirm,
    P2pTradePaid,
)
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.repositories.p2p_repo import ORDER_ACTIVE
from telegram_invite_bot.scheduler.fsm_sweeper import (
    STATE_ENTERED_AT_FIELD,
    utc_now_iso,
)
from telegram_invite_bot.services.p2p_service import (
    MAX_OPEN_TRADES_PER_BUYER,
    BuyOutcome,
    ConfirmOutcome,
    DisputeResolution,
    ExpressBuyOutcome,
    MarkPaidOutcome,
    OpenDisputeOutcome,
    ResolveOutcome,
)
from telegram_invite_bot.utils.aiogram import NOT_MODIFIED, require_from_user
from telegram_invite_bot.utils.html import legacy_md_to_html, plain_text
from telegram_invite_bot.utils.numbers import format_amount_compact, page_offset

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext
    from aiogram.types import CallbackQuery, Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.db.models.p2p import P2pSellOrder
    from telegram_invite_bot.services.p2p_service import P2pService

__all__ = [
    "P2pBuyStates",
    "build_router",
    "handle_admin_resolve",
    "handle_buy_all",
    "handle_buy_amount",
    "handle_buy_menu",
    "handle_dispute_open",
    "handle_express_currency",
    "handle_express_fiat",
    "handle_express_start",
    "handle_order_book",
    "handle_order_view",
    "handle_seller_confirm",
    "handle_seller_stats",
    "handle_trade_paid",
    "on_expire_p2p_buy",
]

log = logger.bind(component="handlers.p2p_trade")

# Legacy buy menu listed 10 orders per screen
# (bot.py:19783 and 19793, both ``[:10]``);
# we keep 10 as the page size and add real paging on top (the book's
# 25-row dump becomes page 1..N of the same list).
PAGE_SIZE = 10

# Currency → flag/emoji for the filter row (legacy bot.py:20146-20156).
_CURRENCY_EMOJI: dict[str, str] = {
    "RUB": "🇷🇺",
    "USD": "🇺🇸",
    "UAH": "🇺🇦",
    "EUR": "🇪🇺",
    "USDT": "💵",
    "TON": "💎",
}

# FSM data keys.
_FD_LANG = "lang"
_FD_ORDER_ID = "order_id"
_FD_CURRENCY = "express_currency"


class P2pBuyStates(StatesGroup):
    """Free-text inputs of the buy side (amount / express fiat budget).

    Defined HERE (not in ``fsm/``) like ``CheckCreateStates`` — this
    module wholly owns both interviews. NO money is held by either
    state: the escrow slice only moves when ``service.buy`` /
    ``service.express_buy`` runs, so an abandoned input costs nothing
    and the sweeper callback below only has to DM + clear.
    """

    awaiting_buy_amount = State()
    awaiting_express_fiat = State()


_OWN_BUY_STATES: frozenset[str] = frozenset(
    {
        P2pBuyStates.awaiting_buy_amount.state or "",
        P2pBuyStates.awaiting_express_fiat.state or "",
    }
)


async def _foreign_flow(state: FSMContext) -> bool:
    """True when an interview this module does NOT own holds the FSM.

    Every other module that opens an interview gates on a bare
    ``get_state() is not None`` (``checks.handle_check_create_start``,
    ``withdraw.handle_withdraw_start``,
    ``transfer_rights.handle_transfer_rights``). The buy side could not:
    it re-enters its OWN states constantly — tapping a second order card
    off the book, or picking another currency after the first — and a
    bare gate would refuse ordinary browsing. So the test is membership
    instead: only a state belonging to some other flow is a conflict.

    Without any gate at all, ``set_data`` (which replaces, not merges)
    silently destroyed an in-flight ``/check_create`` or
    ``/transfer_rights`` interview the moment the user tapped an order
    card (#732). Nothing is escrowed by either buy state, so refusing
    the tap costs the user nothing but a second press.
    """
    current = await state.get_state()
    return current is not None and current not in _OWN_BUY_STATES


async def on_expire_p2p_buy(bot: Bot, key: object, data: dict[str, object]) -> None:
    """FSM-sweeper timeout callback for both buy-side inputs.

    Registered in ``app.py`` with a 10-minute window, like the /support
    sweeper next to it. No escrow is held by
    these states, so expiry is purely a UX nudge.
    """
    user_id = getattr(key, "user_id", None)
    if not isinstance(user_id, int):
        return
    lang_raw = data.get(_FD_LANG)
    lang = lang_raw if isinstance(lang_raw, str) else "ru"
    with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(user_id, t("h_p2p_buy_expired", lang))
    log.bind(uid=user_id).info("p2p buy input expired by sweeper")


def _utcnow() -> datetime:
    """Naive UTC ``now`` — the codebase's stored-datetime convention."""
    return datetime.now(UTC).replace(tzinfo=None)


def _fiat(value: float) -> str:
    """Fiat amounts render with 2 decimals (legacy ``{x:.2f}``)."""
    return f"{value:.2f}"


def _price(value: float) -> str:
    """Per-COM price renders with 4 decimals (legacy ``{price:.4f}``)."""
    return f"{value:.4f}"


def _com(value: int | float) -> str:
    """COM amounts use the legacy ``format_com_amount`` compact form."""
    return format_amount_compact(float(value))


async def _edit_or_answer(
    target: object, text: str, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Best-effort edit of a callback's card; falls back to a new message.

    Mirrors legacy's edit-then-send try/except (bot.py:19963-19965,
    :20140-20142) except for one reject we refuse to inherit:
    :data:`~telegram_invite_bot.utils.aiogram.NOT_MODIFIED` means the
    card already says what the tap asked for, so posting a fresh copy
    just spams the chat. It is reachable — re-tapping the currency flag
    you are already filtered on in ``handle_order_book`` redraws an
    identical card — and it is the same defect ``handlers/rating.py``
    records from prod on 12.08 (#722). Every other reject (card past the
    edit window, card deleted, bot blocked) still earns a new message.
    An inaccessible message (None / not a ``Message``) is silently
    skipped — the callback toast is the only feedback channel then.
    """
    if not isinstance(target, MessageType):
        return
    try:
        await target.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if NOT_MODIFIED in str(exc):
            return
        with contextlib.suppress(TelegramAPIError):
            await target.answer(text, reply_markup=markup)
    except TelegramForbiddenError:
        with contextlib.suppress(TelegramAPIError):
            await target.answer(text, reply_markup=markup)


async def _notify(
    bot: Bot, user_id: int, text: str, *, markup: InlineKeyboardMarkup | None = None
) -> None:
    """Best-effort DM — a blocked bot must never fail the money path."""
    try:
        await bot.send_message(user_id, text, reply_markup=markup)
    except TelegramAPIError as exc:
        log.warning("p2p notify failed for {uid}: {e!r}", uid=user_id, e=exc)


# ---------------------------------------------------------------------------
# Order list (buy menu + order book) — legacy bot.py:19750-19853 / 20095-20137.
# ---------------------------------------------------------------------------


def _filter_row(lang: str) -> list[InlineKeyboardButton]:
    """Currency filter row (legacy ``_p2p_filter_currency_row``).

    Six flag buttons + "Все"; every press resets to page 0. Always
    targets the order-book view: :class:`P2pBuyMenu` carries no
    ``currency`` field, and both screens render the same buy buttons,
    so the filter landing in the book view is display-equivalent.
    """
    row = [
        InlineKeyboardButton(
            text=_CURRENCY_EMOJI[cur],
            callback_data=P2pOrderBook(currency=cur, page=0).pack(),
        )
        for cur in P2P_CURRENCIES
    ]
    row.append(
        InlineKeyboardButton(
            text=t("p2p_filter_all_btn", lang),
            callback_data=P2pOrderBook(currency="", page=0).pack(),
        )
    )
    return row


def _order_line(order: P2pSellOrder, lang: str) -> str:
    """One book row: ``#id — rem по price CUR/COM [| pay] [| min–max CUR]``.

    ``payment_methods`` is seller-typed free text — escaped (HTML parse
    mode) and truncated to 40 chars like the legacy buy menu.
    """
    line = t(
        "h_p2p_book_line",
        lang,
        oid=order.id,
        rem=_com(order.remaining_com),
        price=_price(order.price_per_com),
        cur=order.fiat_currency,
    )
    if order.payment_methods:
        line += f" | {html.escape(order.payment_methods[:40])}"
    if order.min_amount is not None or order.max_amount is not None:
        line += f" | {order.min_amount or 0}–{order.max_amount or '∞'} {order.fiat_currency}"
    return line


async def _render_order_list(
    view: str,
    currency: str,
    page: int,
    p2p_service: P2pService,
    lang: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """Shared renderer for the buy menu (``view="buy"``) and the book.

    Active orders, price ASC (the service's fixed ordering), PAGE_SIZE
    per page; ``limit+1`` over-fetch detects whether a next page exists.
    """
    page = max(page, 0)

    async def _fetch(target: int) -> list[P2pSellOrder]:
        return await p2p_service.order_book(
            currency=currency or None,
            limit=PAGE_SIZE + 1,
            # #1984: the page number is bounded by what SQLite can
            # bind, the product it becomes was not.
            offset=page_offset(target, PAGE_SIZE),
            now=_utcnow(),
        )

    orders = await _fetch(page)
    if not orders and page > 0:
        # The card is older than the book: the orders this page used to
        # hold were filled or cancelled. Showing "no orders" here would
        # read as an empty market when the first page may well be full,
        # so snap back to the top the way the rating board does.
        page = 0
        orders = await _fetch(0)
    has_next = len(orders) > PAGE_SIZE
    orders = orders[:PAGE_SIZE]
    title_key = "p2p_buy_menu_title" if view == "buy" else "p2p_order_book_title"

    def _page_cb(target_page: int) -> str:
        if view == "buy":
            return P2pBuyMenu(page=target_page).pack()
        return P2pOrderBook(currency=currency, page=target_page).pack()

    rows: list[list[InlineKeyboardButton]] = []
    if not orders:
        text = t("p2p_no_orders", lang) + (f" ({currency})" if currency else "")
    else:
        header = "📊 <b>" + t(title_key, lang) + "</b>"
        if currency:
            header += f" — {currency}"
        text = header + "\n\n" + "\n".join(_order_line(o, lang) for o in orders)
        rows.extend(
            [
                InlineKeyboardButton(
                    text=t("p2p_buy_order_btn", lang, oid=o.id, rem=_com(o.remaining_com)),
                    callback_data=P2pOrderView(order_id=o.id).pack(),
                )
            ]
            for o in orders
        )

    paging: list[InlineKeyboardButton] = []
    if page > 0:
        paging.append(InlineKeyboardButton(text="⬅️", callback_data=_page_cb(page - 1)))
    if has_next:
        paging.append(InlineKeyboardButton(text="➡️", callback_data=_page_cb(page + 1)))
    if paging:
        rows.append(paging)
    rows.append(_filter_row(lang))
    rows.append(
        [InlineKeyboardButton(text=t("p2p_back_btn", lang), callback_data=P2pMenu().pack())]
    )
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def handle_buy_menu(
    callback: CallbackQuery,
    callback_data: P2pBuyMenu,
    p2p_service: P2pService,
    lang: str,
) -> None:
    """Buy menu — active orders + paging (bot.py:19750). Always
    unfiltered: the factory carries no currency, the filter row routes
    into the order-book view."""
    text, markup = await _render_order_list("buy", "", callback_data.page, p2p_service, lang)
    await _edit_or_answer(callback.message, text, markup)
    await callback.answer()


async def handle_order_book(
    callback: CallbackQuery,
    callback_data: P2pOrderBook,
    p2p_service: P2pService,
    lang: str,
) -> None:
    """ "Все ордера" book view — same list, book title (bot.py:20095).

    #1959: ``currency`` arrives off the wire, and ``CallbackData``
    validates a field's *type*, not its value. The factory documents it
    as ``""`` or one of ``P2P_CURRENCIES``; anything else is a stale
    card or a hand-rolled client, and it used to be interpolated into an
    HTML message unescaped while the neighbouring ``_order_line``
    escaped its free text. Bail the way ``handle_express_currency``
    does. Upper-casing is part of the same repair: ``create_order``
    stores ``fiat_currency`` upper-cased, so a lower-cased filter
    matched nothing.
    """
    currency = callback_data.currency.upper()
    if currency and not is_supported_currency(currency):
        await callback.answer()
        return
    text, markup = await _render_order_list("book", currency, callback_data.page, p2p_service, lang)
    await _edit_or_answer(callback.message, text, markup)
    await callback.answer()


# ---------------------------------------------------------------------------
# Order detail → buy amount / buy all — legacy bot.py:19805-19939.
# ---------------------------------------------------------------------------


async def handle_order_view(
    callback: CallbackQuery,
    callback_data: P2pOrderView,
    p2p_service: P2pService,
    state: FSMContext,
    lang: str,
) -> None:
    """Order detail card + the free-text buy-amount prompt.

    D3: the seller block shows the REAL counters from ``seller_stats``
    ("✅ сделок / ⚠️ споров"), not legacy's dead 5.0 rating. Self-trade
    is rejected up front (bot.py:19905) — the service re-asserts it.

    Gated by :func:`_foreign_flow` because the ``set_data`` below
    replaces whatever another interview had stored (#732).
    """
    user = callback.from_user
    if await _foreign_flow(state):
        await callback.answer(t("h_p2p_buy_busy", lang), show_alert=True)
        return
    order = await p2p_service.get_order(callback_data.order_id)
    if order is None or order.status != ORDER_ACTIVE:
        await callback.answer(t("h_p2p_order_not_active", lang), show_alert=True)
        return
    if order.user_id == user.id:
        await callback.answer(t("h_p2p_self_trade", lang), show_alert=True)
        return
    stats = await p2p_service.seller_stats(order.user_id)

    await state.set_state(P2pBuyStates.awaiting_buy_amount)
    await state.set_data(
        {
            _FD_LANG: lang,
            _FD_ORDER_ID: order.id,
            STATE_ENTERED_AT_FIELD: utc_now_iso(),
        }
    )
    text = t(
        "h_p2p_order_detail",
        lang,
        order_id=order.id,
        seller_id=order.user_id,
        trades=stats.successful_trades,
        disputes=stats.dispute_count,
        rem=_com(order.remaining_com),
        price=_price(order.price_per_com),
        cur=order.fiat_currency,
        amount=_com(order.amount_com),
    )
    # The bounds are enforced on the way in (``P2pService.buy``), so the
    # card that asks for an amount has to state them — the book line is
    # two taps back by the time the buyer is typing. Both units are
    # shown because the prompt asks for COM and the seller set fiat.
    limit_hint = _limit_hint(order, lang)
    if limit_hint:
        text += "\n\n" + limit_hint
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("p2p_buy_all_btn", lang),
                    callback_data=P2pBuyAll(order_id=order.id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("h_p2p_stats_btn", lang),
                    callback_data=P2pSellerStats(seller_id=order.user_id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("p2p_back_btn", lang),
                    callback_data=P2pBuyMenu(page=0).pack(),
                )
            ],
        ]
    )
    await _edit_or_answer(callback.message, text, markup)
    await callback.answer()


def _limit_hint(order: P2pSellOrder, lang: str) -> str:
    """The order's per-trade bounds in fiat AND COM, or ``""`` if unset.

    ``ceil`` on the floor and ``floor`` on the ceiling so both suggested
    COM numbers are inside the bound the service checks; the minimum is
    clamped to what is left, matching the service's own clamp (an order
    with less remaining than its floor can still be bought out).
    """
    if order.min_amount is None and order.max_amount is None:
        return ""
    price = float(order.price_per_com)
    remaining = int(order.remaining_com)
    if price <= 0:  # defensive: a zero price would divide by zero below
        return ""
    if order.min_amount is not None:
        min_fiat = min(float(order.min_amount), remaining * price)
        min_com: object = min(math.ceil(min_fiat / price), remaining)
    else:
        min_fiat, min_com = 0.0, 0
    max_com: object = math.floor(order.max_amount / price) if order.max_amount is not None else "∞"
    return t(
        "h_p2p_order_limit_hint",
        lang,
        min=order.min_amount or 0,
        max=order.max_amount if order.max_amount is not None else "∞",
        cur=order.fiat_currency,
        min_com=min_com,
        max_com=max_com,
    )


def _trade_card_markup(trade_id: int, lang: str) -> InlineKeyboardMarkup:
    """Buyer trade card buttons: Я оплатил / Мои сделки / Открыть спор."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("p2p_i_paid_btn", lang),
                    callback_data=P2pTradePaid(trade_id=trade_id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text="📋 " + t("p2p_my_trades_btn", lang),
                    callback_data=P2pMyTrades().pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("p2p_open_dispute_btn", lang),
                    callback_data=P2pDisputeOpen(trade_id=trade_id).pack(),
                )
            ],
        ]
    )


def _seller_confirm_markup(trade_id: int, lang: str) -> InlineKeyboardMarkup:
    """Seller notification buttons: ✅ Подтвердить / Открыть спор.

    #1697: the dispute row is new. This message is only ever sent once
    the buyer has marked the trade paid, which is precisely the state
    where a seller-raised dispute is the honest move — the money did
    not arrive, or arrived short — and until now the seller had no
    button for it at all. :meth:`P2pService.open_dispute` accepts
    either participant, so the button works; what was missing was any
    surface that offered it.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("p2p_confirm_com_sent_btn", lang),
                    callback_data=P2pTradeConfirm(trade_id=trade_id).pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("p2p_open_dispute_btn", lang),
                    callback_data=P2pDisputeOpen(trade_id=trade_id).pack(),
                )
            ],
        ]
    )


def _paid_card_markup(trade_id: int, lang: str) -> InlineKeyboardMarkup:
    """Buyer card AFTER "Я оплатил": 📋 Мои сделки / Открыть спор.

    #1697: the replacement markup used to carry only "Мои сделки", so
    the buyer's own normal action deleted the only emergency exit they
    had — at the exact moment it starts to matter, because from here on
    the trade moves only if the seller confirms, and a seller who never
    confirms is a seller the buyer can do nothing about except escalate.

    Two accidents hid how sharp that was: if ``edit_text`` failed for
    any reason other than NOT_MODIFIED, :func:`_edit_or_answer` sent a
    NEW message and the original card kept its dispute button; and if
    ``callback.message`` was unavailable it returned without editing at
    all. Neither is a design; both made the loss intermittent.

    This is not a substitute for an automatic exit from ``paid``
    (#1695) — when both sides go quiet, no button helps. It restores
    the manual one, which makes the automatic one a backstop rather
    than the only route.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 " + t("p2p_my_trades_btn", lang),
                    callback_data=P2pMyTrades().pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("p2p_open_dispute_btn", lang),
                    callback_data=P2pDisputeOpen(trade_id=trade_id).pack(),
                )
            ],
        ]
    )


def _trade_card_text(
    trade_id: int, amount_com: int, total_fiat: float, currency: str, lang: str
) -> str:
    """Buyer trade card: header + the legacy ``p2p_trade_important`` steps."""
    return (
        t(
            "h_p2p_trade_created",
            lang,
            trade_id=trade_id,
            amount=_com(amount_com),
            fiat=_fiat(total_fiat),
            currency=currency,
        )
        + "\n\n"
        + legacy_md_to_html(t("p2p_trade_important", lang))
    )


async def _notify_seller_new_trade(
    bot: Bot,
    *,
    seller_id: int,
    buyer_id: int,
    trade_id: int,
    amount_com: int,
    total_fiat: float,
    currency: str,
    lang: str,
) -> None:
    """Seller-side "Новая заявка" notify (bot.py:19941-19945).

    #692: no confirm button here. An earlier revision attached
    :func:`_seller_confirm_markup` to this message, citing
    ``bot.py:19946`` for it — but 19946 is the ``except Exception:`` of
    the very ``send_message`` above, and legacy's notify carries no
    ``reply_markup`` at all. Legacy sent the button as a SEPARATE
    message, and only once the buyer had pressed "Я оплатил"
    (bot.py:20001-20008); :func:`handle_trade_paid` does the same.

    Attaching it here bought nothing and cost twice: the trade is still
    ``pending``, so :meth:`P2pService.seller_confirm`, over
    ``P2pRepo.confirm_from_paid``, refuses the press with
    ``NOT_PAID`` — the seller whose money has in
    fact arrived taps a live button and is told no — and once the buyer
    does mark the trade paid, a second message with the same button
    lands in the same chat.
    """
    await _notify(
        bot,
        seller_id,
        t(
            "h_p2p_seller_new_trade",
            lang,
            trade_id=trade_id,
            buyer_id=buyer_id,
            amount=_com(amount_com),
            fiat=_fiat(total_fiat),
            currency=currency,
        ),
    )


async def handle_buy_amount(
    message: Message,
    state: FSMContext,
    p2p_service: P2pService,
    bot: Bot,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Free-text COM amount → ``service.buy`` (legacy bot.py:19886-19939).

    Stays in the state on a bad/oversized number (legacy let the user
    retype); clears on every terminal outcome. RACE_LOST is the new
    atomic-guard outcome (legacy oversold instead).

    #1868: the checkpoint sits after the service call, not inside the
    OK branch. :meth:`P2pService.buy` opens with
    ``p2p_repo.lock_writer`` (``repositories/p2p_repo.py:175-205``, an
    ``UPDATE ... WHERE false`` whose only job is ``BEGIN IMMEDIATE``)
    unconditionally, before any check, so all six refusals below hold
    the single writer slot across their Telegram round-trip exactly as
    firmly as a fill does, and a refusal has nothing to unwind. The OK
    branch needs it for its own reason: the card reply is not wrapped,
    and the ``state.clear()`` above it runs on the FSM's own connection
    (``di/providers.py:85-106``), so a rollback there takes the trade
    row and the ``remaining_com`` decrement with it while the cleared
    interview survives — the buyer ends up with no card, no trade and
    no step to retype into. :func:`handle_express_fiat` has committed
    here since #694.
    """
    user = require_from_user(message)
    data = await state.get_data()
    order_id_raw = data.get(_FD_ORDER_ID)
    if not isinstance(order_id_raw, int):
        await state.clear()
        await message.reply(t("h_p2p_order_not_active", lang))
        return
    try:
        amount = int((message.text or "").strip())
    except ValueError:
        await message.reply(t("h_p2p_enter_number", lang))
        return
    if amount <= 0:
        await message.reply(t("h_p2p_amount_positive", lang))
        return

    result = await p2p_service.buy(
        buyer_id=user.id, order_id=order_id_raw, amount_com=amount, now=_utcnow()
    )
    if checkpoint is not None:
        await checkpoint()
    if result.outcome is BuyOutcome.INVALID_AMOUNT:
        order = await p2p_service.get_order(order_id_raw)
        rem = order.remaining_com if order is not None else 0
        await message.reply(t("h_p2p_buy_invalid_amount", lang, rem=_com(rem)))
        return
    if result.outcome in (BuyOutcome.BELOW_MIN, BuyOutcome.ABOVE_MAX):
        # Stay in the state: this is a retypeable mistake, same as an
        # oversized amount above, and the message says which way to go.
        key = (
            "h_p2p_buy_below_min"
            if result.outcome is BuyOutcome.BELOW_MIN
            else "h_p2p_buy_above_max"
        )
        await message.reply(
            t(
                key,
                lang,
                fiat=f"{result.limit_fiat:.2f}",
                cur=result.fiat_currency,
                com=_com(result.limit_com),
            )
        )
        return
    if result.outcome is BuyOutcome.ORDER_NOT_ACTIVE:
        await state.clear()
        await message.reply(t("h_p2p_order_not_active", lang))
        return
    if result.outcome is BuyOutcome.SELF_TRADE:
        await state.clear()
        await message.reply(t("h_p2p_self_trade", lang))
        return
    if result.outcome is BuyOutcome.RACE_LOST:
        await state.clear()
        await message.reply(t("h_p2p_race_lost", lang))
        return
    if result.outcome is BuyOutcome.TOO_MANY_OPEN:
        # Not a retypeable mistake — no amount gets past the ceiling —
        # so the flow ends and the buyer is sent to settle what they
        # already hold.
        await state.clear()
        await message.reply(t("h_p2p_too_many_open", lang, max=MAX_OPEN_TRADES_PER_BUYER))
        return

    await state.clear()
    await message.reply(
        _trade_card_text(
            result.trade_id, result.amount_com, result.total_fiat, result.fiat_currency, lang
        ),
        reply_markup=_trade_card_markup(result.trade_id, lang),
    )
    await _notify_seller_new_trade(
        bot,
        seller_id=result.seller_id,
        buyer_id=user.id,
        trade_id=result.trade_id,
        amount_com=result.amount_com,
        total_fiat=result.total_fiat,
        currency=result.fiat_currency,
        lang=lang,
    )
    log.bind(uid=user.id, trade=result.trade_id, amount=result.amount_com).info("p2p buy created")


async def handle_buy_all(
    callback: CallbackQuery,
    callback_data: P2pBuyAll,
    p2p_service: P2pService,
    state: FSMContext,
    bot: Bot,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """ "Купить всё" — buy the order's current ``remaining_com``
    (bot.py:19855-19857).

    Reads the live remainder and passes it as the amount; the service's
    atomic fill guard turns a concurrent shrink into RACE_LOST instead
    of an oversell.

    #1868: the same checkpoint as :func:`handle_buy_amount`, for the
    same lock reason and a sharper one on the OK branch —
    :func:`_edit_or_answer` has already drawn the trade card citing
    ``result.trade_id`` by the time the unwrapped ``callback.answer``
    below runs. A rollback there leaves the buyer holding a card for a
    trade that no longer exists: "Я оплатил" answers "сделка не
    найдена", and the seller was never told anything either
    (:func:`_notify` swallows its own failures, so it cannot be what
    raises).
    """
    user = callback.from_user
    order = await p2p_service.get_order(callback_data.order_id)
    if order is None or order.status != ORDER_ACTIVE:
        await callback.answer(t("h_p2p_order_not_active", lang), show_alert=True)
        return
    if order.user_id == user.id:
        await callback.answer(t("h_p2p_self_trade", lang), show_alert=True)
        return

    result = await p2p_service.buy(
        buyer_id=user.id,
        order_id=order.id,
        amount_com=order.remaining_com,
        now=_utcnow(),
    )
    if checkpoint is not None:
        await checkpoint()
    if result.outcome is BuyOutcome.ABOVE_MAX:
        # "Buy all" is the one path that can walk into the seller's
        # ceiling: the remainder is simply bigger than one trade may be.
        # BELOW_MIN cannot happen here — the service clamps the floor to
        # what the order has left, and this takes exactly that.
        #
        # ``plain_text``: the key is shared with the typed-amount branch
        # above, which replies as an HTML message. An alert has no
        # parse_mode, so without the strip the user reads a literal
        # ``<b>500.00 RUB</b>``.
        await callback.answer(
            plain_text(
                t(
                    "h_p2p_buy_above_max",
                    lang,
                    fiat=f"{result.limit_fiat:.2f}",
                    cur=order.fiat_currency,
                    com=_com(result.limit_com),
                )
            ),
            show_alert=True,
        )
        return
    if result.outcome is BuyOutcome.TOO_MANY_OPEN:
        await callback.answer(
            plain_text(t("h_p2p_too_many_open", lang, max=MAX_OPEN_TRADES_PER_BUYER)),
            show_alert=True,
        )
        return
    if result.outcome is not BuyOutcome.OK:
        key = (
            "h_p2p_race_lost"
            if result.outcome is BuyOutcome.RACE_LOST
            else "h_p2p_order_not_active"
        )
        await callback.answer(t(key, lang), show_alert=True)
        return

    await state.clear()
    await _edit_or_answer(
        callback.message,
        _trade_card_text(
            result.trade_id, result.amount_com, result.total_fiat, result.fiat_currency, lang
        ),
        _trade_card_markup(result.trade_id, lang),
    )
    await callback.answer(t("h_p2p_trade_created_toast", lang))
    await _notify_seller_new_trade(
        bot,
        seller_id=result.seller_id,
        buyer_id=user.id,
        trade_id=result.trade_id,
        amount_com=result.amount_com,
        total_fiat=result.total_fiat,
        currency=result.fiat_currency,
        lang=lang,
    )
    log.bind(uid=user.id, trade=result.trade_id, amount=result.amount_com).info(
        "p2p buy-all created"
    )


# ---------------------------------------------------------------------------
# Express buy — legacy bot.py:19591-19747.
# ---------------------------------------------------------------------------


async def handle_express_start(
    callback: CallbackQuery,
    lang: str,
) -> None:
    """Express entry: hint + currency keyboard (bot.py:19591-19612)."""
    text = (
        "⚡ <b>"
        + t("h_p2p_express_title", lang)
        + "</b>\n\n"
        + t("p2p_express_buy_hint", lang)
        + "\n\n"
        + t("p2p_express_select_currency", lang)
    )
    rows = [
        [
            InlineKeyboardButton(
                text=f"{_CURRENCY_EMOJI[cur]} {cur}",
                callback_data=P2pExpressCurrency(currency=cur).pack(),
            )
            for cur in P2P_CURRENCIES[i : i + 2]
        ]
        for i in range(0, len(P2P_CURRENCIES), 2)
    ]
    rows.append(
        [InlineKeyboardButton(text=t("p2p_back_btn", lang), callback_data=P2pMenu().pack())]
    )
    await _edit_or_answer(callback.message, text, InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


async def handle_express_currency(
    callback: CallbackQuery,
    callback_data: P2pExpressCurrency,
    state: FSMContext,
    lang: str,
) -> None:
    """Currency picked → prompt for the fiat budget (bot.py:19614-19629)."""
    currency = callback_data.currency.upper()
    if not is_supported_currency(currency):
        await callback.answer()
        return
    if await _foreign_flow(state):
        await callback.answer(t("h_p2p_buy_busy", lang), show_alert=True)
        return
    await state.set_state(P2pBuyStates.awaiting_express_fiat)
    await state.set_data(
        {
            _FD_LANG: lang,
            _FD_CURRENCY: currency,
            STATE_ENTERED_AT_FIELD: utc_now_iso(),
        }
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("p2p_back_btn", lang), callback_data=P2pMenu().pack())]
        ]
    )
    await _edit_or_answer(
        callback.message,
        legacy_md_to_html(t("p2p_express_enter_fiat", lang, currency=currency)),
        markup,
    )
    await callback.answer()


async def handle_express_fiat(
    message: Message,
    state: FSMContext,
    p2p_service: P2pService,
    bot: Bot,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Fiat budget → ``service.express_buy`` (bot.py:19683-19747).

    On OK: result summary card, then one card per fill with the
    paid/dispute buttons, then one best-effort seller notification per
    fill (each fill can have a different seller).

    #694: this is the worst Telegram fan-out in the module — the summary
    card, then two ``send_message`` per fill, up to
    ``MAX_OPEN_TRADES_PER_BUYER`` fills. The middleware only commits
    after the handler returns (``middlewares/base.py:157-158``), so
    without the checkpoint the express purchase holds ``economy.db``'s
    single writer slot across all of it and every other writer burns its
    5 s ``busy_timeout`` (``db/pragma.py:63``) before failing with
    "database is locked". The money is already decided when
    ``express_buy`` returns, so the transaction ends here — see
    :class:`db.session.Checkpoint` and the twins inside
    ``chatstats.build_router`` (``handle_chatstats`` and
    ``handle_topactive``).
    """
    user = require_from_user(message)
    data = await state.get_data()
    currency_raw = data.get(_FD_CURRENCY)
    currency = currency_raw if isinstance(currency_raw, str) else "RUB"
    try:
        max_fiat = float((message.text or "").strip().replace(",", "."))
    except ValueError:
        await message.reply(t("h_p2p_express_amount_invalid", lang))
        return
    if not math.isfinite(max_fiat):
        # ``float()`` happily takes "nan", "inf" and "1e400", and none of
        # them is a budget. NaN compares False against every bound below,
        # so it walked into the fill loop and died inside
        # ``int(nan / price)`` — the FSM step raised, the state was
        # already cleared, and the buyer was left with no answer at all.
        # Infinity read as "take the whole book". Both are refused here,
        # where the text is still text, and again at the money boundary
        # in ``express_buy`` — the service is what decides money.
        await message.reply(t("h_p2p_express_amount_invalid", lang))
        return
    if max_fiat <= 0:
        await message.reply(t("h_p2p_amount_positive", lang))
        return

    await state.clear()
    result = await p2p_service.express_buy(
        buyer_id=user.id, currency=currency, max_fiat=max_fiat, now=_utcnow()
    )
    if checkpoint is not None:
        await checkpoint()
    if result.outcome is ExpressBuyOutcome.NO_ORDERS:
        await message.reply(legacy_md_to_html(t("p2p_express_no_orders", lang, currency=currency)))
        return
    if result.outcome is ExpressBuyOutcome.TOO_MANY_OPEN:
        # Distinct from the invalid-amount fallback below: the budget
        # was fine, the buyer's open-trade ledger was not, and telling
        # them "wrong amount" would send them retyping numbers forever.
        await message.reply(t("h_p2p_too_many_open", lang, max=MAX_OPEN_TRADES_PER_BUYER))
        return
    if result.outcome is not ExpressBuyOutcome.OK:
        await message.reply(t("h_p2p_express_amount_invalid", lang))
        return

    summary = (
        "⚡ <b>"
        + t("h_p2p_express_done", lang)
        + "</b>\n\n"
        + legacy_md_to_html(
            t(
                "p2p_express_result",
                lang,
                count=len(result.fills),
                total_com=_com(result.total_com),
                total_fiat=_fiat(result.total_fiat),
                currency=currency,
            )
        )
        + "\n\n"
        + t("p2p_express_result_hint", lang)
    )
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="📋 " + t("p2p_my_trades_btn", lang),
                    callback_data=P2pMyTrades().pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("withdraw_back_to_p2p_btn", lang),
                    callback_data=P2pMenu().pack(),
                )
            ],
        ]
    )
    await message.reply(summary, reply_markup=markup)

    # Per-trade buyer cards (bot.py:19720-19733) + per-fill seller
    # notifications (bot.py:19741-19745; ``ExpressFill.seller_id`` exists
    # exactly for this). #692: the seller notify carries no markup here
    # either — legacy's does not (19744 is ``parse_mode=``, not a
    # keyboard), and an express fill is as ``pending`` as any other.
    for fill in result.fills:
        await _notify(
            bot,
            user.id,
            legacy_md_to_html(
                t(
                    "p2p_express_trade_line",
                    lang,
                    trade_id=fill.trade_id,
                    amount=_com(fill.amount_com),
                    fiat=_fiat(fill.total_fiat),
                    currency=currency,
                )
                + "\n\n"
                + t("p2p_trade_important", lang)
            ),
            markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=t("p2p_i_paid_btn", lang),
                            callback_data=P2pTradePaid(trade_id=fill.trade_id).pack(),
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=t("p2p_open_dispute_btn", lang),
                            callback_data=P2pDisputeOpen(trade_id=fill.trade_id).pack(),
                        )
                    ],
                ]
            ),
        )
        await _notify(
            bot,
            fill.seller_id,
            legacy_md_to_html(
                t(
                    "p2p_express_seller_notify",
                    lang,
                    trade_id=fill.trade_id,
                    amount=_com(fill.amount_com),
                    currency=currency,
                )
            ),
        )
    log.bind(uid=user.id, fills=len(result.fills), com=result.total_com).info("p2p express buy")


# ---------------------------------------------------------------------------
# Trade lifecycle: paid → confirm — legacy bot.py:19972-20065.
# ---------------------------------------------------------------------------


async def handle_trade_paid(
    callback: CallbackQuery,
    callback_data: P2pTradePaid,
    p2p_service: P2pService,
    bot: Bot,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Buyer "Я оплатил" → ``mark_paid`` + seller notify (bot.py:19972).

    #1864: the transition is committed before the seller is told about
    it. ``P2pRepo.mark_paid`` is a guarded ``UPDATE``, so it opens
    ``BEGIN IMMEDIATE`` on ``economy.db`` even when it matches no row,
    and the session middleware otherwise commits only after the handler
    returns (``middlewares/base.py:157-158``). The file name matters to
    the argument. This used to name a P2P-only database, which does not
    exist: every P2P table subclasses ``EconomyBase``, so the writer
    slot held here is the one every balance write in the bot queues
    behind, not a quiet corner of its own. The siblings at
    ``handlers/p2p.py:897`` and ``handlers/p2p_trade.py:1053`` had it
    right. Two things went wrong
    without the checkpoint. The writer slot was held across as many as
    three Telegram calls; and the last of them —
    ``callback.answer(...)`` — is the one outgoing call here that is
    NOT wrapped, so an aged-out callback query raised, the middleware
    rolled the transition back, and the trade returned to ``pending``
    *after* the seller had already been DM'd "the buyer has paid". The
    buyer was told nothing: ``handlers/errors.py`` classes that reject
    as benign. Siblings :func:`handle_seller_confirm` and
    :func:`handle_admin_resolve` were already doing this.
    """
    user = callback.from_user
    result = await p2p_service.mark_paid(
        buyer_id=user.id, trade_id=callback_data.trade_id, now=_utcnow()
    )
    if checkpoint is not None:
        await checkpoint()
    if result.outcome is MarkPaidOutcome.NOT_FOUND:
        await callback.answer(t("h_p2p_trade_not_found", lang), show_alert=True)
        return
    if result.outcome is MarkPaidOutcome.NOT_PENDING:
        await callback.answer(t("h_p2p_trade_already_processed", lang), show_alert=True)
        return

    await _notify(
        bot,
        result.seller_id,
        t(
            "h_p2p_paid_seller_notify",
            lang,
            trade_id=callback_data.trade_id,
            amount=_com(result.amount_com),
            fiat=_fiat(result.total_fiat),
            currency=result.fiat_currency,
        ),
        markup=_seller_confirm_markup(callback_data.trade_id, lang),
    )
    await _edit_or_answer(
        callback.message,
        t("h_p2p_paid_done", lang, trade_id=callback_data.trade_id),
        _paid_card_markup(callback_data.trade_id, lang),
    )
    await callback.answer(t("h_p2p_paid_toast", lang), show_alert=True)
    log.bind(uid=user.id, trade=callback_data.trade_id).info("p2p marked paid")


async def handle_seller_confirm(
    callback: CallbackQuery,
    callback_data: P2pTradeConfirm,
    p2p_service: P2pService,
    bot: Bot,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Seller "COM отправлены" → ``seller_confirm`` release (bot.py:20024).

    #694: the release is committed before the buyer notification and the
    card edit, so the escrow payout does not hold ``economy.db``'s
    writer slot across two Telegram round-trips.
    """
    user = callback.from_user
    result = await p2p_service.seller_confirm(
        seller_id=user.id, trade_id=callback_data.trade_id, now=_utcnow()
    )
    if checkpoint is not None:
        await checkpoint()
    if result.outcome is ConfirmOutcome.NOT_FOUND:
        await callback.answer(t("h_p2p_trade_not_found", lang), show_alert=True)
        return
    if result.outcome is ConfirmOutcome.NOT_PAID:
        await callback.answer(t("h_p2p_not_paid_yet", lang), show_alert=True)
        return
    if result.outcome is ConfirmOutcome.CREDIT_FAILED:
        await callback.answer(t("h_p2p_credit_failed", lang), show_alert=True)
        return

    await _notify(
        bot,
        result.buyer_id,
        t(
            "h_p2p_confirm_done_buyer",
            lang,
            trade_id=callback_data.trade_id,
            amount=_com(result.amount_com),
        ),
    )
    await _edit_or_answer(
        callback.message,
        t("h_p2p_confirm_done_seller", lang, trade_id=callback_data.trade_id),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="📋 " + t("p2p_my_trades_btn", lang),
                        callback_data=P2pMyTrades().pack(),
                    )
                ]
            ]
        ),
    )
    await callback.answer(t("h_p2p_confirm_toast", lang), show_alert=True)
    log.bind(uid=user.id, trade=callback_data.trade_id, amount=result.amount_com).info(
        "p2p seller confirmed"
    )


# ---------------------------------------------------------------------------
# Disputes — legacy bot.py:20187-20315 + D1's third outcome.
# ---------------------------------------------------------------------------

# Resolution wire value → (admin-card label key, buyer notify key,
# seller notify key, resolved-card label key).
_RESOLUTIONS: dict[str, tuple[str, str, str, str]] = {
    DisputeResolution.REFUND_BUYER.value: (
        "admin_p2p_refund_btn",
        "h_p2p_resolve_refund_buyer",
        "h_p2p_resolve_refund_seller",
        "h_p2p_res_refund_buyer",
    ),
    DisputeResolution.CONFIRM_SELLER.value: (
        "admin_p2p_confirm_seller_btn",
        "h_p2p_resolve_confirm_buyer",
        "h_p2p_resolve_confirm_seller",
        "h_p2p_res_confirm_seller",
    ),
    DisputeResolution.RETURN_SELLER.value: (
        "h_p2p_admin_return_seller_btn",
        "h_p2p_resolve_return_buyer",
        "h_p2p_resolve_return_seller",
        "h_p2p_res_return_seller",
    ),
}


def _admin_resolve_markup(trade_id: int, lang: str) -> InlineKeyboardMarkup:
    """THREE resolve buttons (D1 adds return_seller to legacy's two)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(label_key, lang),
                    callback_data=P2pDisputeResolve(
                        trade_id=trade_id, resolution=resolution
                    ).pack(),
                )
            ]
            for resolution, (label_key, _, _, _) in _RESOLUTIONS.items()
        ]
    )


async def handle_dispute_open(
    callback: CallbackQuery,
    callback_data: P2pDisputeOpen,
    p2p_service: P2pService,
    bot: Bot,
    settings: Settings,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Open a dispute (bot.py:20187-20213) — either participant.

    Unlike legacy (which flipped the status with NO existence/participant
    check), the service validates both; the handler renders the outcome.
    OK → alert toast to the opener, notify the OTHER party, send the
    admin chat the resolution card with all three buttons.

    #1864: ``mark_disputed`` is a guarded ``UPDATE`` and the toast is
    the FIRST outgoing call — and the only one here that is not
    wrapped. An aged-out callback query therefore rolled the dispute
    flag back, and because the two ``_notify`` calls come after it,
    neither the counterparty nor the admin desk ever heard about a
    dispute the opener was shown as opened. Commit first.
    """
    user = callback.from_user
    result = await p2p_service.open_dispute(user_id=user.id, trade_id=callback_data.trade_id)
    if checkpoint is not None:
        await checkpoint()
    if result.outcome is OpenDisputeOutcome.NOT_FOUND:
        await callback.answer(t("h_p2p_trade_not_found", lang), show_alert=True)
        return
    if result.outcome is OpenDisputeOutcome.NOT_PARTICIPANT:
        await callback.answer(t("h_p2p_dispute_not_participant", lang), show_alert=True)
        return
    if result.outcome is OpenDisputeOutcome.NOT_DISPUTABLE:
        await callback.answer(t("h_p2p_dispute_not_disputable", lang), show_alert=True)
        return

    await callback.answer(t("h_p2p_dispute_opened_toast", lang), show_alert=True)
    other = result.seller_id if user.id == result.buyer_id else result.buyer_id
    await _notify(bot, other, t("h_p2p_dispute_other_party", lang, trade_id=callback_data.trade_id))

    admin_chat = settings.bot.admin_chat_id
    if admin_chat:
        opener = html.escape(user.full_name or str(user.id))
        await _notify(
            bot,
            admin_chat,
            t(
                "h_p2p_dispute_admin_card",
                lang,
                trade_id=callback_data.trade_id,
                opener=opener,
                opener_id=user.id,
                seller_id=result.seller_id,
                buyer_id=result.buyer_id,
                amount=_com(result.amount_com),
                fiat=_fiat(result.total_fiat),
                currency=result.fiat_currency,
            ),
            markup=_admin_resolve_markup(callback_data.trade_id, lang),
        )
    else:
        log.warning(
            "p2p dispute {tid} opened but ADMIN_CHAT_ID is unset — no admin card",
            tid=callback_data.trade_id,
        )
    log.bind(uid=user.id, trade=callback_data.trade_id).info("p2p dispute opened")


async def handle_admin_resolve(
    callback: CallbackQuery,
    callback_data: P2pDisputeResolve,
    p2p_service: P2pService,
    bot: Bot,
    settings: Settings,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Admin resolve — DEVELOPER-GATED (legacy bot.py:20220/20271).

    The service trusts ``admin_id`` for the audit stamp only, so the
    gate lives HERE: a non-developer press is answered silently (legacy
    posture) and nothing moves. All three outcomes credit/refund inside
    the service; the handler notifies both parties and stamps the card.
    """
    user = callback.from_user
    if not settings.bot.is_developer(user.id):
        await callback.answer()
        return
    keys = _RESOLUTIONS.get(callback_data.resolution)
    if keys is None:
        await callback.answer()
        return
    _, buyer_key, seller_key, label_key = keys

    result = await p2p_service.resolve_dispute(
        admin_id=user.id,
        trade_id=callback_data.trade_id,
        resolution=DisputeResolution(callback_data.resolution),
        now=_utcnow(),
    )
    # #694: three Telegram calls follow (both parties plus the card), so
    # the resolution is committed first rather than held open across them.
    if checkpoint is not None:
        await checkpoint()
    if result.outcome in (ResolveOutcome.NOT_FOUND, ResolveOutcome.NOT_DISPUTED):
        await callback.answer(t("h_p2p_not_disputed", lang), show_alert=True)
        return
    if result.outcome is ResolveOutcome.CREDIT_FAILED:
        await callback.answer(t("h_p2p_credit_failed", lang), show_alert=True)
        return

    await _notify(bot, result.buyer_id, t(buyer_key, lang, trade_id=callback_data.trade_id))
    await _notify(bot, result.seller_id, t(seller_key, lang, trade_id=callback_data.trade_id))
    await _edit_or_answer(
        callback.message,
        t(
            "h_p2p_resolve_done",
            lang,
            trade_id=callback_data.trade_id,
            resolution=t(label_key, lang),
        ),
    )
    await callback.answer(t(label_key, lang), show_alert=True)
    log.bind(admin=user.id, trade=callback_data.trade_id, resolution=callback_data.resolution).info(
        "p2p dispute resolved"
    )


async def handle_seller_stats(
    callback: CallbackQuery,
    callback_data: P2pSellerStats,
    p2p_service: P2pService,
    lang: str,
) -> None:
    """Seller-stats popup (bot.py:20159-20184) — D3: real counters, no rating."""
    stats = await p2p_service.seller_stats(callback_data.seller_id)
    await callback.answer(
        t(
            "h_p2p_seller_stats_popup",
            lang,
            seller_id=callback_data.seller_id,
            trades=stats.successful_trades,
            disputes=stats.dispute_count,
            sold=_com(stats.total_sold_com),
        ),
        show_alert=True,
    )


# ---------------------------------------------------------------------------
# Router wiring.
# ---------------------------------------------------------------------------


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Factory — fresh ``Router`` + ``EconomyMiddleware`` per call.

    Mirrors ``handlers/checks.py``: EconomyMiddleware on BOTH event
    types (``EconomyMiddleware._bind`` injects ``p2p_service``),
    private-only on the message side. The callback side deliberately has
    NO chat-type filter — the dispute card's resolve buttons live in the
    admin chat (usually a group); every money transition is re-guarded
    by the service and the resolve handler re-gates on ``is_developer``.
    Recorded as a deliberate #1608 exemption: a private-only callback
    filter here would break dispute resolution outright.
    """
    router = Router(name="p2p_trade")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    # #1686: the D2 TTL must reach P2pService's lazy expiry, not just
    # the sweeper. Without it the two disagree the moment the setting
    # moves off its 30-minute default.
    ttl = settings.economy.p2p_pending_ttl_minutes
    router.message.middleware(EconomyMiddleware(registry, p2p_pending_ttl_minutes=ttl))
    router.callback_query.middleware(EconomyMiddleware(registry, p2p_pending_ttl_minutes=ttl))

    router.callback_query.register(handle_buy_menu, P2pBuyMenu.filter(), F.from_user)
    router.callback_query.register(handle_order_book, P2pOrderBook.filter(), F.from_user)
    router.callback_query.register(handle_order_view, P2pOrderView.filter(), F.from_user)
    router.callback_query.register(handle_buy_all, P2pBuyAll.filter(), F.from_user)
    router.callback_query.register(handle_express_start, P2pExpressStart.filter(), F.from_user)
    router.callback_query.register(
        handle_express_currency, P2pExpressCurrency.filter(), F.from_user
    )
    router.message.register(
        handle_buy_amount,
        StateFilter(P2pBuyStates.awaiting_buy_amount),
        F.text,
        NOT_A_COMMAND,
        F.from_user,
    )
    router.message.register(
        handle_express_fiat,
        StateFilter(P2pBuyStates.awaiting_express_fiat),
        F.text,
        NOT_A_COMMAND,
        F.from_user,
    )
    # A screenshot of the payment is the single most likely thing a buyer
    # sends at these steps, and it used to vanish without a word.
    register_text_expected(
        router, P2pBuyStates.awaiting_buy_amount, P2pBuyStates.awaiting_express_fiat
    )
    router.callback_query.register(handle_trade_paid, P2pTradePaid.filter(), F.from_user)
    router.callback_query.register(handle_seller_confirm, P2pTradeConfirm.filter(), F.from_user)
    router.callback_query.register(handle_seller_stats, P2pSellerStats.filter(), F.from_user)

    async def _handle_dispute_open(
        callback: CallbackQuery,
        callback_data: P2pDisputeOpen,
        p2p_service: P2pService,
        bot: Bot,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_dispute_open(
            callback, callback_data, p2p_service, bot, settings, lang, checkpoint
        )

    router.callback_query.register(_handle_dispute_open, P2pDisputeOpen.filter(), F.from_user)

    async def _handle_admin_resolve(
        callback: CallbackQuery,
        callback_data: P2pDisputeResolve,
        p2p_service: P2pService,
        bot: Bot,
        lang: str,
        checkpoint: Checkpoint | None = None,
    ) -> None:
        await handle_admin_resolve(
            callback, callback_data, p2p_service, bot, settings, lang, checkpoint
        )

    router.callback_query.register(_handle_admin_resolve, P2pDisputeResolve.filter(), F.from_user)
    return router
