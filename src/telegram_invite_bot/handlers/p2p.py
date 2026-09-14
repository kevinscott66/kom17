"""``/p2p`` — P2P marketplace entry, menu, and the sell-side flows (P2).

Cluster P2 of the P2P epic (#64, design ``docs/DESIGN_P2P.md``). This
module owns:

* the ``/p2p`` command (the only entry — legacy reached the market
  through the /withdraw menu, bot.py:19225, and the port did NOT carry
  that button over: ``handlers/withdraw.py`` and
  ``keyboards/builders/withdraw.py`` contain no ``p2p`` reference at
  all) and the main menu card
  (Продать / Купить / Экспресс / Мои сделки / Все ордера),
* the 4-step sell FSM: amount → currency (6 buttons) → market price
  (the ONLY price type, legacy bot.py:19381) → optional
  payment-methods/limits line — ending in
  :meth:`P2pService.create_sell_order` (escrow-on-create + ledger),
* «Мои ордера» with per-order cancel (bot.py:19527-19588) and the
  read-only «Мои сделки» list (bot.py:20068-20092).

The buy/trade lifecycle UI (order book, buy, express, trade cards,
disputes) is cluster P3 in ``handlers/p2p_trade.py`` — NOT here. Both
modules share the CallbackData factories in
``keyboards/builders/p2p.py``.

Money posture: the handler renders outcomes; every invariant (escrow
debit, atomic remaining_com guards, checked credits, ledger rows) lives
in :class:`P2pService` over the single economy session the
:class:`EconomyMiddleware` commits on handler success. ``CREDIT_FAILED``
paths are already rolled back by the service — the handler only shows
the apology.

Private-chat-only, like every ported economy surface — but a group
``/p2p`` is *answered*, not ignored: :func:`build_router` returns the
``with_chat_type_refusal`` wrapper, which replies with the localized
``h_private_only_command`` card and its deep-link button. Silence is
reserved for owner-rank commands whose existence isn't advertised, and
``p2p`` ranks 0 in the catalog (its ``core.ranks.COMMAND_ENTRIES``
row), well below ``chat_scope._SILENT_FROM_RANK``
(``handlers/chat_scope.py:87``); it is not in ``_TWO_SIDED_COMMANDS``
either. The only user-controlled string that reaches a card is the
payment-methods line, routed through :func:`html.escape`.

Copy is HTML (the bot default) with one exception the #708 audit found:
``p2p_limits_optional_hint`` is a ported legacy value and still carries
telebot-era Markdown. It is byte-locked by the i18n parity test, so it
is translated at render time via
:func:`utils.html.legacy_md_to_html` rather than edited in the YAML.
"""

from __future__ import annotations

import contextlib
import html
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.types import Message as MessageType
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger

from telegram_invite_bot.core.p2p import P2P_CURRENCIES, is_supported_currency, market_rate
from telegram_invite_bot.handlers.chat_scope import with_chat_type_refusal
from telegram_invite_bot.handlers.fsm_text import NOT_A_COMMAND, register_text_expected
from telegram_invite_bot.i18n import t
from telegram_invite_bot.keyboards.builders.main_menu import MainMenu
from telegram_invite_bot.keyboards.builders.p2p import (
    P2pBuyMenu,
    P2pCancelOrder,
    P2pExpressStart,
    P2pMenu,
    P2pMyOrders,
    P2pMyTrades,
    P2pOrderBook,
    P2pSellCurrency,
    P2pSellPriceMarket,
    P2pSellSkipLimits,
    P2pSellStart,
)
from telegram_invite_bot.middlewares.economy import EconomyMiddleware
from telegram_invite_bot.repositories.p2p_repo import ORDER_ACTIVE
from telegram_invite_bot.scheduler.fsm_sweeper import STATE_ENTERED_AT_FIELD, utc_now_iso
from telegram_invite_bot.services.p2p_service import (
    MAX_ACTIVE_ORDERS_PER_SELLER,
    CancelOrderOutcome,
    CreateOrderOutcome,
    limits_are_sane,
)
from telegram_invite_bot.utils.aiogram import NOT_MODIFIED
from telegram_invite_bot.utils.html import legacy_md_to_html
from telegram_invite_bot.utils.keyed_locks import KeyedLocks
from telegram_invite_bot.utils.numbers import format_number

if TYPE_CHECKING:
    from aiogram.fsm.context import FSMContext
    from aiogram.types import CallbackQuery, InaccessibleMessage, Message

    from telegram_invite_bot.config.settings import Settings
    from telegram_invite_bot.db import Checkpoint, EngineRegistry
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.services.p2p_service import P2pService

log = logger.bind(component="handlers.p2p")


class P2pSellStates(StatesGroup):
    """Sell-order FSM (legacy ``p2p_state`` dict steps, bot.py:19311+).

    Defined HERE (not in ``fsm/``) so this handler module wholly owns
    the interview — same pattern as ``CheckCreateStates``. NO money is
    escrowed until the final step calls ``create_sell_order``; an
    abandoned interview holds only the FSM busy-lock that the 10-minute
    sweeper rule (see :func:`on_expire_p2p_sell`) clears.

    * ``awaiting_amount`` — free-text COM amount (legacy step "amount").
    * ``awaiting_currency`` — 6-button currency pick (step "currency").
    * ``awaiting_price`` — the market-only price confirm (step "price").
    * ``awaiting_limits`` — optional ``methods; min; max`` line or the
      «Пропустить» button (step "limits_optional").
    """

    awaiting_amount = State()
    awaiting_currency = State()
    awaiting_price = State()
    awaiting_limits = State()


# FSM data keys.
_FD_LANG = "lang"
_FD_AMOUNT = "amount"
_FD_CURRENCY = "currency"

# Currency button emoji, verbatim from the legacy keyboard rows
# (bot.py:19338-19340). Keyed off P2P_CURRENCIES so a core change to the
# supported set surfaces here as a KeyError in tests, not a silent gap.
_CURRENCY_EMOJI = {
    "RUB": "🇷🇺",
    "USD": "🇺🇸",
    "UAH": "🇺🇦",
    "EUR": "🇪🇺",
    "USDT": "💵",
    "TON": "💎",
}

# Tokens that skip the optional limits step from the text side
# (legacy bot.py:19438: '/skip', 'skip', 'пропустить', '—').
_SKIP_TOKENS = frozenset({"/skip", "skip", "пропустить", "—"})

# One seller's read → clear → create section, serialised (#1502). Same
# registry and same shape as ``handlers/withdraw.py:107`` (#777) and
# ``handlers/rps.py:381``, which is the house pattern for a double tap on
# a card button. The state gate in :func:`handle_sell_skip_limits`
# resolves through an awaited ``FSMContext.get_state()``, so two taps
# both pass it before either reaches ``clear()`` — aiogram offers nothing
# else to lean on, the dispatcher being built without ``events_isolation``
# (the ``dispatcher`` provider in ``di/providers.py``). Without the lock
# both taps escrow, and
# the seller ends up with two orders for coins they parked once.
# ``KeyedLocks`` refcounts its slots, so the table cannot grow with every
# user who ever opened the sell interview.
_create_locks: KeyedLocks[tuple[int, int]] = KeyedLocks()


def _utcnow() -> datetime:
    """Naive UTC ``now`` — the codebase's stored-datetime convention."""
    return datetime.now(UTC).replace(tzinfo=None)


def _as_int(value: object) -> int:
    """Narrow an FSM-data value to ``int``; 0 on a missing/odd value.

    aiogram types FSM data as ``dict[str, Any]`` — every arithmetic
    read needs narrowing under mypy strict (same helper as checks.py).
    """
    return value if isinstance(value, int) else 0


async def _edit_or_answer(
    target: Message | InaccessibleMessage | None,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Edit the card in place; fall back to a fresh message.

    Mirrors the legacy try-edit-except-send dance every P2P screen used
    (bot.py:19254-19257), with one deliberate improvement. Legacy lumped
    :data:`~telegram_invite_bot.utils.aiogram.NOT_MODIFIED` in with the
    real failures, so re-tapping the button you are already on posted a
    second identical card — the exact incident ``handlers/rating.py``
    records for 12.08. Here that reject is a no-op: the screen already
    shows what the tap asked for. Everything else (card past the edit
    window, card deleted, bot blocked) still earns a fresh message. An
    inaccessible/absent message (inline-mode edge) renders nothing —
    the callback toast is the only feedback channel then.
    """
    if not isinstance(target, MessageType):
        return
    try:
        await target.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if NOT_MODIFIED in str(exc):
            return
        with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
            await target.answer(text, reply_markup=markup)
    except TelegramForbiddenError:
        with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
            await target.answer(text, reply_markup=markup)


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------


def _menu_keyboard(lang: str) -> InlineKeyboardMarkup:
    """The five legacy action rows (bot.py:19247-19251) plus an exit row.

    Legacy's exit was «🔙 Назад» → ``withdraw_menu_back``
    (bot.py:19252), which made sense there: the market was reached from
    the /withdraw menu (bot.py:19225). The port dropped that entry point
    — nothing in ``handlers/withdraw.py`` or
    ``keyboards/builders/withdraw.py`` mentions P2P — so pointing the
    row back at withdraw would send the user somewhere they never came
    from.

    #696: an earlier revision therefore dropped the row entirely and
    justified it with a withdraw-menu P2P button that does not exist.
    That left ``/p2p`` as the one menu in the bot with no way out: every
    other one carries ``back_to_menu`` → ``MainMenu(action="home")``
    (``handlers/rating.py:490``, ``keyboards/builders/mygroups.py:110``).
    This row is that same idiom, which is also where a /withdraw entry
    point would land a user if one is ever added back.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📢 " + t("p2p_create_sell_btn", lang), callback_data=P2pSellStart().pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🛒 " + t("p2p_buy_btn", lang), callback_data=P2pBuyMenu(page=0).pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⚡ " + t("p2p_express_buy_btn", lang), callback_data=P2pExpressStart().pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📋 " + t("p2p_my_trades_btn", lang), callback_data=P2pMyTrades().pack()
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📊 " + t("p2p_order_book_btn", lang),
            callback_data=P2pOrderBook(currency="", page=0).pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text=t("back_to_menu", lang), callback_data=MainMenu(action="home").pack()
        )
    )
    return builder.as_markup()


async def _menu_text(
    user_id: int, p2p_service: P2pService, economy_repo: EconomyRepo, lang: str
) -> str:
    """The menu card body (legacy bot.py:19237-19244, D3 applied).

    Legacy showed the dead always-5.0 ``rating`` here; per D3 the card
    renders the real counters («✅ сделок: N | ⚠️ споров: M») instead.
    ``get_or_create`` mirrors legacy's ``register_user`` on entry.

    The active-order count comes from SQL, not from filtering
    :meth:`my_orders`: that helper caps at 15 rows for display, so a
    seller whose fifteen newest rows were all cancelled read «0 активных
    ордеров» with live orders on the book (#720).
    """
    wallet = await economy_repo.get_or_create(user_id, now=_utcnow())
    active = await p2p_service.count_active_orders(user_id)
    stats = await p2p_service.seller_stats(user_id)
    return t(
        "h_p2p_menu",
        lang,
        balance=format_number(wallet.balance),
        active_orders=active,
        trades=stats.successful_trades,
        disputes=stats.dispute_count,
        total_sold=format_number(stats.total_sold_com),
    )


async def handle_p2p_command(
    message: Message,
    p2p_service: P2pService,
    economy_repo: EconomyRepo,
    lang: str,
) -> None:
    """``/p2p`` — post the marketplace menu (private chat only)."""
    user = message.from_user
    if user is None:
        return
    text = await _menu_text(user.id, p2p_service, economy_repo, lang)
    await message.answer(text, reply_markup=_menu_keyboard(lang))
    log.bind(uid=user.id).info("/p2p menu opened")


async def handle_menu(
    callback: CallbackQuery,
    p2p_service: P2pService,
    economy_repo: EconomyRepo,
    state: FSMContext,
    lang: str,
) -> None:
    """Back-to-menu button — edits the current card in place.

    Also used as the «Отмена» destination inside the sell FSM (legacy
    pointed cancel at ``withdraw_p2p_menu``, bot.py:19341), so any
    in-flight interview state is cleared here. Nothing is escrowed
    before the final create call, so clearing loses no money.
    """
    user = callback.from_user
    if await state.get_state() is not None:
        await state.clear()
    text = await _menu_text(user.id, p2p_service, economy_repo, lang)
    await _edit_or_answer(callback.message, text, _menu_keyboard(lang))
    await callback.answer()


# ---------------------------------------------------------------------------
# Sell FSM
# ---------------------------------------------------------------------------


async def handle_sell_start(
    callback: CallbackQuery,
    economy_repo: EconomyRepo,
    state: FSMContext,
    lang: str,
) -> None:
    """«Продать COM» — (re)enter the interview at the amount step.

    Legacy reset the whole ``p2p_state`` entry on this callback
    (bot.py:19295-19296) — pressing the button mid-flow restarts
    cleanly, and the in-step «Назад» buttons point here for exactly that
    reason.

    Deviation, deliberate (#721): legacy's prompt also printed a
    remaining-daily-withdrawal-limit line (bot.py:19300-19301) and
    ``h_p2p_sell_enter_amount`` has none. Nothing is lost — legacy never
    charged that limit through P2P either (its ``daily_avail`` at
    bot.py:19294 is assigned and never read), so the line was decoration
    over an unenforced rule. ``docs/DESIGN_P2P.md:96`` records the
    enforcement posture; this note records the missing text.
    """
    user = callback.from_user
    wallet = await economy_repo.get_or_create(user.id, now=_utcnow())
    await state.set_state(P2pSellStates.awaiting_amount)
    await state.set_data({_FD_LANG: lang, STATE_ENTERED_AT_FIELD: utc_now_iso()})
    await _edit_or_answer(
        callback.message,
        t("h_p2p_sell_enter_amount", lang, balance=format_number(wallet.balance)),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=t("p2p_back_btn", lang), callback_data=P2pMenu().pack())]
            ]
        ),
    )
    await callback.answer()
    log.bind(uid=user.id).info("p2p sell interview started")


def _currency_keyboard(lang: str) -> InlineKeyboardMarkup:
    """Six currency buttons, two per row (legacy bot.py:19338-19340)."""
    builder = InlineKeyboardBuilder()
    row: list[InlineKeyboardButton] = []
    for currency in P2P_CURRENCIES:
        row.append(
            InlineKeyboardButton(
                text=f"{_CURRENCY_EMOJI.get(currency, '')} {currency}".strip(),
                callback_data=P2pSellCurrency(currency=currency).pack(),
            )
        )
        if len(row) == 2:
            builder.row(*row)
            row = []
    if row:
        builder.row(*row)
    builder.row(InlineKeyboardButton(text=t("p2p_back_btn", lang), callback_data=P2pMenu().pack()))
    return builder.as_markup()


async def handle_sell_amount(
    message: Message,
    economy_repo: EconomyRepo,
    state: FSMContext,
    lang: str,
) -> None:
    """Amount step — legacy validations verbatim (bot.py:19311-19338):
    integer, > 0, ≤ balance. Stays on the step with a hint otherwise.
    """
    user = message.from_user
    if user is None:
        return
    raw = (message.text or "").strip()
    try:
        amount = int(raw)
    except ValueError:
        await message.reply(t("h_p2p_amount_not_number", lang))
        return
    if amount <= 0:
        await message.reply(t("h_p2p_amount_positive", lang))
        return
    wallet = await economy_repo.get_or_create(user.id, now=_utcnow())
    if amount > wallet.balance:
        await message.reply(t("h_p2p_insufficient", lang, balance=format_number(wallet.balance)))
        return
    await state.update_data({_FD_AMOUNT: amount, STATE_ENTERED_AT_FIELD: utc_now_iso()})
    await state.set_state(P2pSellStates.awaiting_currency)
    await message.reply(
        t("h_p2p_choose_currency", lang, amount=format_number(amount)),
        reply_markup=_currency_keyboard(lang),
    )


async def handle_sell_currency(
    callback: CallbackQuery,
    callback_data: P2pSellCurrency,
    state: FSMContext,
    lang: str,
) -> None:
    """Currency picked → the market-only price step (bot.py:19348-19376).

    A stale button (FSM no longer at the currency step) gets the legacy
    «Сессия истекла» toast rather than silently mutating a fresh flow.
    """
    if await state.get_state() != P2pSellStates.awaiting_currency.state:
        await callback.answer(t("h_p2p_session_expired", lang), show_alert=True)
        return
    currency = callback_data.currency
    rate = market_rate(currency)
    if rate is None or not is_supported_currency(currency):
        await callback.answer(t("h_p2p_bad_currency", lang), show_alert=True)
        return
    data = await state.get_data()
    amount = _as_int(data.get(_FD_AMOUNT))
    estimated = amount * rate
    await state.update_data({_FD_CURRENCY: currency, STATE_ENTERED_AT_FIELD: utc_now_iso()})
    await state.set_state(P2pSellStates.awaiting_price)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(
                        "h_p2p_price_market_btn",
                        lang,
                        estimated=f"{estimated:.2f}",
                        currency=currency,
                    ),
                    callback_data=P2pSellPriceMarket().pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("p2p_back_btn", lang), callback_data=P2pSellStart().pack()
                )
            ],
        ]
    )
    await _edit_or_answer(
        callback.message,
        t(
            "h_p2p_price_step",
            lang,
            amount=format_number(amount),
            currency=currency,
            estimated=f"{estimated:.2f}",
        ),
        keyboard,
    )
    await callback.answer()


async def handle_sell_price_market(
    callback: CallbackQuery,
    state: FSMContext,
    lang: str,
) -> None:
    """«По рынку» — the only price type → optional limits step
    (bot.py:19381-19410). The rate is NOT taken off the wire: the
    service re-derives it from ``P2P_COM_RATES`` at create time.
    """
    if await state.get_state() != P2pSellStates.awaiting_price.state:
        await callback.answer(t("h_p2p_session_expired", lang), show_alert=True)
        return
    data = await state.get_data()
    amount = _as_int(data.get(_FD_AMOUNT))
    currency = str(data.get(_FD_CURRENCY) or "")
    rate = market_rate(currency) or 0.0
    await state.update_data({STATE_ENTERED_AT_FIELD: utc_now_iso()})
    await state.set_state(P2pSellStates.awaiting_limits)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("p2p_skip_limits_btn", lang),
                    callback_data=P2pSellSkipLimits().pack(),
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("p2p_back_btn", lang), callback_data=P2pSellStart().pack()
                )
            ],
        ]
    )
    header = t(
        "h_p2p_limits_step",
        lang,
        amount=format_number(amount),
        price=f"{rate:.4f}",
        currency=currency,
    )
    await _edit_or_answer(
        callback.message,
        header + "\n\n" + legacy_md_to_html(t("p2p_limits_optional_hint", lang)),
        keyboard,
    )
    await callback.answer()


def _parse_limits(text: str) -> tuple[str | None, int | None, int | None]:
    """Parse the optional ``методы; мин; макс`` line (bot.py:19444-19460).

    Semicolon-separated; the methods string is capped at 500 chars like
    legacy; min/max accept ``int(float(x))`` and silently drop malformed
    numbers (legacy ``except ValueError: pass``).

    ``OverflowError`` is caught alongside ``ValueError`` because
    ``int(float(x))`` has two failure modes, not one: ``float("1e400")``
    is ``inf``, which survives the float parse and dies in ``int()``. A
    seller who typed one long number therefore crashed mid-interview
    instead of having the bound ignored like every other unreadable
    token. Infinity belongs in the same bucket as ``abc`` — a bound we
    cannot represent is a bound we do not store.

    A *finite* but absurd number (``1e30``) parses fine here and is
    rejected one layer up by :func:`limits_are_sane`, which can tell the
    seller their limits are invalid rather than silently unsetting them.
    """
    parts = [p.strip() for p in text.split(";") if p.strip()]
    payment_methods = parts[0][:500] if parts and parts[0] else None
    min_amount: int | None = None
    max_amount: int | None = None
    if len(parts) >= 2:
        with contextlib.suppress(ValueError, OverflowError):
            min_amount = int(float(parts[1]))
    if len(parts) >= 3:
        with contextlib.suppress(ValueError, OverflowError):
            max_amount = int(float(parts[2]))
    return payment_methods, min_amount, max_amount


def _created_card(
    lang: str,
    *,
    amount: int,
    currency: str,
    price_per_com: float,
    total_fiat: float,
    payment_methods: str | None,
    min_amount: int | None,
    max_amount: int | None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Success card + its keyboard (legacy bot.py:19496-19520).

    The payment-methods echo is user input — HTML-escaped (legacy
    interpolated it raw into Markdown, an injection we don't port).
    """
    details = ""
    if payment_methods:
        details += "\n" + t(
            "h_p2p_order_payment_line", lang, methods=html.escape(payment_methods[:200])
        )
    if min_amount is not None or max_amount is not None:
        details += "\n" + t(
            "h_p2p_order_limit_line",
            lang,
            min=min_amount or 0,
            max=max_amount if max_amount is not None else "∞",
            currency=currency,
        )
    text = (
        f"✅ <b>{t('p2p_order_created_title', lang)}</b>\n\n"
        + t(
            "h_p2p_order_created_body",
            lang,
            amount=format_number(amount),
            total=f"{total_fiat:.2f}",
            currency=currency,
            price=f"{price_per_com:.4f}",
        )
        + details
        + "\n\n"
        + t("p2p_order_created_footer", lang)
    )
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t("p2p_my_orders_btn", lang), callback_data=P2pMyOrders().pack()
                )
            ],
            [
                InlineKeyboardButton(
                    text=t("withdraw_back_to_p2p_btn", lang), callback_data=P2pMenu().pack()
                )
            ],
        ]
    )
    return text, keyboard


async def _create_and_render(
    *,
    bot_id: int,
    user_id: int,
    p2p_service: P2pService,
    state: FSMContext,
    lang: str,
    payment_methods: str | None,
    min_amount: int | None,
    max_amount: int | None,
    checkpoint: Checkpoint | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Run ``create_sell_order`` off the FSM data and build the reply.

    Single-shot interview: the FSM data is read once and cleared before
    the service call, under ``_create_locks`` so it can't be spent
    twice. The lock is the guard, not the state gates around it —
    :func:`handle_sell_skip_limits` re-reads the state and
    :func:`handle_sell_limits_text` sits behind
    ``StateFilter(P2pSellStates.awaiting_limits)``, but both resolve
    through an awaited read, so a double tap has both halves passing
    before either clears (#1502, the same check-then-act the withdraw
    confirm had in #777).

    Two earlier versions of this note were wrong about what absorbs the
    loser of that race. It is not the state gates (they are already
    passed), and it is not the emptied data reading as ``amount == 0 →
    INVALID_AMOUNT``: a read-back on a cleared FSM degrades the
    *currency* too, and ``market_rate("")`` is ``None``, so the service
    would answer ``INVALID_CURRENCY`` first and show
    ``h_p2p_bad_currency``. Hence the explicit consumed-data check
    inside the lock, which answers ``h_p2p_session_expired`` — the card
    a seller whose interview was just spent should see.

    #1867: the escrow is committed here, not by the caller. This is
    the only escrow-CREATING path in the bot that was still holding its
    locks across the render — ``create_sell_order`` writes a guarded
    ``hold`` on ``economy.db`` (which opens ``BEGIN IMMEDIATE`` even on
    the ``INSUFFICIENT_FUNDS`` 0-row match) plus the order row and the
    ``p2p_escrow`` ledger row, and both callers finish on an outgoing
    call that is NOT wrapped (``callback.answer()`` for the skip
    button, ``message.reply`` for the text path). Coins were never at
    risk — the hold and the order die together — but ``state.clear()``
    ran inside the lock above and the FSM lives on its own connection
    (``di/providers.py:85-106``), so it survived the rollback: the
    seller lost the whole interview and got a generic error card with
    no order to show for it. Siblings ``checks.py`` (:1343),
    :func:`~handlers.p2p_trade.handle_express_buy` and
    :func:`handle_cancel_order` all already commit here.

    The checkpoint sits after the service call rather than inside the
    OK branch on purpose: ``hold`` takes the writer lock on the refusal
    branch too, and a refusal has nothing to unwind.

    Returns ``(text, keyboard-or-None)`` for the caller to render on its
    own surface (edit for the skip button, reply for the text path).
    """
    async with _create_locks.acquire((bot_id, user_id)):
        # Read the bag INSIDE the lock: the tap that loses the race has
        # to see the *consumed* state, not the snapshot it took while
        # waiting at the door.
        data = await state.get_data()
        amount_raw = data.get(_FD_AMOUNT)
        if not isinstance(amount_raw, int):
            # Data gone: the sweeper reclaimed the interview, or the
            # other half of a double tap already spent it. Either way
            # there is nothing left to escrow.
            return t("h_p2p_session_expired", lang), None
        amount = amount_raw
        currency = str(data.get(_FD_CURRENCY) or "")
        await state.clear()

        result = await p2p_service.create_sell_order(
            seller_id=user_id,
            amount_com=amount,
            currency=currency,
            now=_utcnow(),
            payment_methods=payment_methods,
            min_amount=min_amount,
            max_amount=max_amount,
        )
    if checkpoint is not None:
        await checkpoint()
    if result.outcome is CreateOrderOutcome.INSUFFICIENT_FUNDS:
        return t("h_p2p_create_insufficient", lang), None
    if result.outcome is CreateOrderOutcome.INVALID_CURRENCY:
        return t("h_p2p_bad_currency", lang), None
    if result.outcome is CreateOrderOutcome.INVALID_AMOUNT:
        return t("h_p2p_session_expired", lang), None
    if result.outcome is CreateOrderOutcome.INVALID_LIMITS:
        # The text path screens the pair before getting here (it can keep
        # the interview alive); this is the backstop for anything else.
        return t("h_p2p_bad_limits", lang), None
    if result.outcome is CreateOrderOutcome.TOO_MANY_ORDERS:
        # #1690. Only reachable at the very end of the interview: the cap
        # counts LIVE orders, and one can be filled or cancelled while the
        # seller is still typing, so screening earlier would be a promise
        # the service could not keep.
        return t("h_p2p_too_many_orders", lang, limit=MAX_ACTIVE_ORDERS_PER_SELLER), None

    log.bind(uid=user_id, order_id=result.order_id, amount=amount, currency=currency).info(
        "p2p sell order created"
    )
    text, keyboard = _created_card(
        lang,
        amount=amount,
        currency=currency,
        price_per_com=result.price_per_com,
        total_fiat=result.total_fiat,
        payment_methods=payment_methods,
        min_amount=min_amount,
        max_amount=max_amount,
    )
    return text, keyboard


async def handle_sell_skip_limits(
    callback: CallbackQuery,
    bot: Bot,
    p2p_service: P2pService,
    state: FSMContext,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """«Пропустить» — create the order without payment/limits."""
    if await state.get_state() != P2pSellStates.awaiting_limits.state:
        await callback.answer(t("h_p2p_session_expired", lang), show_alert=True)
        return
    text, keyboard = await _create_and_render(
        bot_id=bot.id,
        user_id=callback.from_user.id,
        p2p_service=p2p_service,
        state=state,
        lang=lang,
        payment_methods=None,
        min_amount=None,
        max_amount=None,
        checkpoint=checkpoint,
    )
    await _edit_or_answer(callback.message, text, keyboard)
    await callback.answer()


async def handle_sell_limits_text(
    message: Message,
    bot: Bot,
    p2p_service: P2pService,
    state: FSMContext,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """Limits step text input — ``методы; мин; макс`` or a skip token."""
    user = message.from_user
    if user is None:
        return
    raw = (message.text or "").strip()
    if raw.lower() in _SKIP_TOKENS:
        payment_methods, min_amount, max_amount = None, None, None
    else:
        payment_methods, min_amount, max_amount = _parse_limits(raw)
        if not limits_are_sane(min_amount, max_amount):
            # Stay in the limits state so the seller retypes one line
            # instead of redoing the amount+currency interview. The
            # bounds are a promise to buyers now (the service enforces
            # them), so an impossible pair can't be stored and ignored.
            await message.reply(t("h_p2p_bad_limits", lang))
            return
    text, keyboard = await _create_and_render(
        bot_id=bot.id,
        user_id=user.id,
        p2p_service=p2p_service,
        state=state,
        lang=lang,
        payment_methods=payment_methods,
        min_amount=min_amount,
        max_amount=max_amount,
        checkpoint=checkpoint,
    )
    await message.reply(text, reply_markup=keyboard)


# ---------------------------------------------------------------------------
# My orders + cancel
# ---------------------------------------------------------------------------


async def _my_orders_view(
    user_id: int, p2p_service: P2pService, lang: str
) -> tuple[str, InlineKeyboardMarkup]:
    """Render the «Ваши ордера» list (legacy bot.py:19527-19554).

    15 newest orders, one line each; a cancel button per still-active
    order with escrow left. Status strings are shown raw, like legacy.
    """
    orders = await p2p_service.my_orders(user_id)
    builder = InlineKeyboardBuilder()
    if not orders:
        text = t("h_p2p_my_orders_empty", lang)
    else:
        lines = [t("h_p2p_my_orders_title", lang), ""]
        for order in orders:
            lines.append(
                t(
                    "h_p2p_order_line",
                    lang,
                    oid=order.id,
                    rem=format_number(order.remaining_com),
                    amt=format_number(order.amount_com),
                    price=f"{order.price_per_com:.4f}",
                    currency=order.fiat_currency,
                    status=order.status,
                )
            )
            if order.status == ORDER_ACTIVE and order.remaining_com > 0:
                builder.row(
                    InlineKeyboardButton(
                        text=t("h_p2p_cancel_order_btn", lang, oid=order.id),
                        callback_data=P2pCancelOrder(order_id=order.id).pack(),
                    )
                )
        text = "\n".join(lines)
    builder.row(InlineKeyboardButton(text=t("p2p_back_btn", lang), callback_data=P2pMenu().pack()))
    return text, builder.as_markup()


async def handle_my_orders(
    callback: CallbackQuery,
    p2p_service: P2pService,
    lang: str,
) -> None:
    """«📋 Мои ордера» — list + cancel buttons, edited in place."""
    text, keyboard = await _my_orders_view(callback.from_user.id, p2p_service, lang)
    await _edit_or_answer(callback.message, text, keyboard)
    await callback.answer()


async def handle_cancel_order(
    callback: CallbackQuery,
    callback_data: P2pCancelOrder,
    p2p_service: P2pService,
    lang: str,
    checkpoint: Checkpoint | None = None,
) -> None:
    """«❌ Отменить #N» — refund ``remaining_com`` (bot.py:19558-19588).

    Outcome → legacy toast mapping; on OK the list re-renders so the
    cancelled order's button disappears (legacy re-called
    ``p2p_my_orders``). NOT_YOURS can only fire on a forged payload —
    the buttons we render are always the presser's own orders.

    #694: the refund is committed before the toast and the re-render.
    The session middleware otherwise commits only after the handler
    returns (``middlewares/base.py:157-158``), which would hold
    ``economy.db``'s single writer slot across two Telegram calls and
    push every concurrent writer into its 5 s ``busy_timeout``
    (``db/pragma.py:63``). Nothing after the checkpoint writes, so there
    is no work left to unwind — see :class:`db.session.Checkpoint`.
    """
    user = callback.from_user
    result = await p2p_service.cancel_order(
        seller_id=user.id, order_id=callback_data.order_id, now=_utcnow()
    )
    if checkpoint is not None:
        await checkpoint()
    if result.outcome is CancelOrderOutcome.NOT_FOUND:
        await callback.answer(t("order_not_found", lang), show_alert=True)
        return
    if result.outcome is CancelOrderOutcome.NOT_YOURS:
        await callback.answer(t("h_p2p_not_your_order", lang), show_alert=True)
        return
    if result.outcome is CancelOrderOutcome.NOT_ACTIVE:
        await callback.answer(t("order_not_active", lang), show_alert=True)
        return
    if result.outcome is CancelOrderOutcome.CREDIT_FAILED:
        # Service already rolled back — escrow stays on the order.
        await callback.answer(t("h_p2p_credit_failed", lang), show_alert=True)
        return

    await callback.answer(
        t(
            "order_cancelled_refund",
            lang,
            order_id=callback_data.order_id,
            remaining_com=format_number(result.returned_com),
        ),
        show_alert=True,
    )
    log.bind(uid=user.id, order_id=callback_data.order_id, returned=result.returned_com).info(
        "p2p order cancelled"
    )
    text, keyboard = await _my_orders_view(user.id, p2p_service, lang)
    await _edit_or_answer(callback.message, text, keyboard)


# ---------------------------------------------------------------------------
# My trades (read-only)
# ---------------------------------------------------------------------------


async def handle_my_trades(
    callback: CallbackQuery,
    p2p_service: P2pService,
    lang: str,
) -> None:
    """«Мои сделки» — 20 newest trades, both seats (bot.py:20068-20092).

    Read-only rows here; the actionable trade cards (pay/confirm/
    dispute buttons) are P3's surface. Status strings render raw, like
    legacy.
    """
    user = callback.from_user
    trades = await p2p_service.my_trades(user.id)
    if not trades:
        text = t("h_p2p_my_trades_empty", lang)
    else:
        lines = [t("h_p2p_my_trades_title", lang), ""]
        for trade in trades:
            role_key = "h_p2p_role_seller" if trade.seller_id == user.id else "h_p2p_role_buyer"
            lines.append(
                t(
                    "h_p2p_trade_line",
                    lang,
                    tid=trade.id,
                    role=t(role_key, lang),
                    amount=format_number(trade.amount_com),
                    fiat=f"{trade.total_fiat:.2f}",
                    currency=trade.fiat_currency,
                    status=trade.status,
                )
            )
        text = "\n".join(lines)
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("p2p_back_btn", lang), callback_data=P2pMenu().pack())]
        ]
    )
    await _edit_or_answer(callback.message, text, keyboard)
    await callback.answer()


# ---------------------------------------------------------------------------
# FSM sweeper hook (10-minute interview timeout, like /support)
# ---------------------------------------------------------------------------


async def on_expire_p2p_sell(bot: Bot, key: object, data: dict[str, object]) -> None:
    """Sweeper timeout callback for an abandoned sell interview.

    No escrow exists before the final create call, so expiry only
    releases the FSM busy-lock; we DM the user so the silent lapse is
    visible. The sweeper clears the FSM after this returns.
    """
    user_id = getattr(key, "user_id", None)
    if not isinstance(user_id, int):
        return
    lang_raw = data.get(_FD_LANG)
    lang = lang_raw if isinstance(lang_raw, str) else "ru"
    with contextlib.suppress(TelegramBadRequest, TelegramForbiddenError):
        await bot.send_message(user_id, t("h_p2p_sell_expired", lang))
    log.bind(uid=user_id).info("p2p sell interview expired by sweeper")


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(registry: EngineRegistry, settings: Settings) -> Router:
    """Fresh ``Router`` + per-event ``EconomyMiddleware``, like checks.py.

    ``settings`` supplies the D2 pending-trade TTL (#1686), which the
    ``main_router`` include passes in.

    Private-chat-only on both event types: the inline cards can't be
    driven from a group, and a group ``/p2p`` gets the localized refusal
    card from the ``with_chat_type_refusal`` wrapper this factory
    returns rather than falling through (see the module docstring). The
    ``p2p_service`` parameter on every handler is injected by
    ``EconomyMiddleware._bind`` on the same session the middleware
    commits.
    """
    router = Router(name="p2p")
    router.message.filter(F.chat.type == ChatType.PRIVATE)
    # #1686: the D2 TTL must reach P2pService's lazy expiry, not just
    # the sweeper. Without it the two disagree the moment the setting
    # moves off its 30-minute default.
    ttl = settings.economy.p2p_pending_ttl_minutes
    router.message.middleware(EconomyMiddleware(registry, p2p_pending_ttl_minutes=ttl))
    router.callback_query.middleware(EconomyMiddleware(registry, p2p_pending_ttl_minutes=ttl))
    router.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)

    router.message.register(
        handle_p2p_command, Command("p2p", "п2п", ignore_case=True), F.from_user
    )
    router.callback_query.register(handle_menu, P2pMenu.filter(), F.from_user)
    router.callback_query.register(handle_sell_start, P2pSellStart.filter(), F.from_user)
    router.message.register(
        handle_sell_amount,
        StateFilter(P2pSellStates.awaiting_amount),
        F.text,
        NOT_A_COMMAND,
        F.from_user,
    )
    # The currency / price / skip callbacks deliberately carry NO
    # StateFilter: a stale button press must answer the legacy
    # «Сессия истекла» toast (the in-handler state check), not fall
    # through unhandled and leave the spinner hanging.
    router.callback_query.register(handle_sell_currency, P2pSellCurrency.filter(), F.from_user)
    router.callback_query.register(
        handle_sell_price_market, P2pSellPriceMarket.filter(), F.from_user
    )
    router.callback_query.register(handle_sell_skip_limits, P2pSellSkipLimits.filter(), F.from_user)
    router.message.register(
        handle_sell_limits_text,
        StateFilter(P2pSellStates.awaiting_limits),
        F.text,
        NOT_A_COMMAND,
        F.from_user,
    )
    # Both text steps: an attachment sent instead of the number is
    # answered rather than dropped.
    register_text_expected(router, P2pSellStates.awaiting_amount, P2pSellStates.awaiting_limits)
    router.callback_query.register(handle_my_orders, P2pMyOrders.filter(), F.from_user)
    router.callback_query.register(handle_cancel_order, P2pCancelOrder.filter(), F.from_user)
    router.callback_query.register(handle_my_trades, P2pMyTrades.filter(), F.from_user)
    return with_chat_type_refusal(router, scope="private")
