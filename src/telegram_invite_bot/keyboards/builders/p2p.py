"""P2P marketplace CallbackData factories (epic #64, design §2.4).

EVERY P2P callback wire format lives here — the sell-side set consumed
by ``handlers/p2p.py`` (P2 cluster) AND the buy/trade/dispute set the
``handlers/p2p_trade.py`` module (P3 cluster) imports. Centralising the
factories in one module keeps the 64-byte callback budget auditable in
one place and prevents the two handler modules from inventing
colliding prefixes.

Prefix map (all distinct from every other prefix in the codebase; the
legacy literal callbacks ``p2p_*`` never reach the new dispatcher —
the new pipeline only sees updates for routers it owns):

==================  ====================================================
``p2p_menu``        open / back-to the P2P menu (edit in place)
``p2p_sell``        start the sell-order FSM
``p2p_cur``         sell FSM currency pick (6 buttons)
``p2p_price``       sell FSM "market price" confirm (market-only, D-none)
``p2p_skip``        sell FSM skip the optional limits step
``p2p_myord``       my sell orders list
``p2p_cancel``      cancel one of my active sell orders
``p2p_mytr``        my trades list (read-only, both seats)
``p2p_buymenu``     buy-side order list («Купить COM», one button per order)
``p2p_book``        order book page (currency filter + offset paging)
``p2p_order``       order detail card + the buy-amount prompt
``p2p_buyall``      buy the order's full ``remaining_com``
``p2p_expr``        express-buy entry (currency pick next)
``p2p_excur``       express-buy currency pick
``p2p_paid``        buyer's "Я оплатил" on a pending trade
``p2p_conf``        seller's "COM отправлены" on a paid trade
``p2p_disp``        open a dispute on a trade (either seat)
``p2p_resolve``     ADMIN dispute resolution (3 outcomes, D1 included)
``p2p_stats``       seller-stats popup (D3 counters, no fake rating)
==================  ====================================================

No money authority rides on any payload: every handler re-runs the
service-level gates (ownership, status guards, balance checks) so a
hand-crafted callback routes through the exact same SQL guards as a
fresh button press. The widest payload (``p2p_resolve`` with a 64-bit
trade id + the longest resolution literal) stays well under Telegram's
64-byte callback_data cap.
"""

from __future__ import annotations

from aiogram.filters.callback_data import CallbackData

from telegram_invite_bot.core.callback_fields import DbInt

# ---------------------------------------------------------------------------
# Menu + sell side (P2 cluster — handlers/p2p.py)
# ---------------------------------------------------------------------------


class P2pMenu(CallbackData, prefix="p2p_menu"):
    """Open (or return to) the P2P main menu, editing the card in place.

    Carries nothing: the menu re-derives balance / active-order count /
    seller stats for ``callback.from_user`` on every render, so a stale
    button always shows fresh numbers.
    """


class P2pSellStart(CallbackData, prefix="p2p_sell"):
    """«Продать COM» — enter the 4-step sell FSM (amount first)."""


class P2pSellCurrency(CallbackData, prefix="p2p_cur"):
    """Sell FSM step 2 — one of the six legacy currencies.

    ``currency`` is re-validated against ``P2P_CURRENCIES`` by the
    handler (and again by the service at create time), so a forged
    payload cannot smuggle an unsupported currency into an order.
    """

    currency: str


class P2pSellPriceMarket(CallbackData, prefix="p2p_price"):
    """Sell FSM step 3 — «По рынку (...)», the ONLY price option.

    Deliberately carries NO price: legacy serialised the rate into the
    callback (``p2p_price_market_0_011``) and parsed it back, trusting
    the client wire. The new service derives the market rate from
    ``P2P_COM_RATES`` itself, so the payload would be dead weight at
    best and a spoof vector at worst.
    """


class P2pSellSkipLimits(CallbackData, prefix="p2p_skip"):
    """Sell FSM step 4 — «Пропустить»: create with no payment/limits."""


class P2pMyOrders(CallbackData, prefix="p2p_myord"):
    """«📋 Мои ордера» — my sell-orders list with cancel buttons."""


class P2pCancelOrder(CallbackData, prefix="p2p_cancel"):
    """«❌ Отменить #N» — cancel my active order, refund remaining_com.

    Ownership is NOT trusted from the payload: the service re-checks
    ``seller_id == order.user_id`` (NOT_YOURS) and the active status in
    an atomic guard, so replaying someone else's button is inert.
    """

    order_id: DbInt


class P2pMyTrades(CallbackData, prefix="p2p_mytr"):
    """«Мои сделки» — read-only trade list (both seats)."""


# ---------------------------------------------------------------------------
# Buy side + trade lifecycle (P3 cluster — handlers/p2p_trade.py)
# ---------------------------------------------------------------------------


class P2pBuyMenu(CallbackData, prefix="p2p_buymenu"):
    """«Купить COM» — the buy-side order list (buy buttons per order).

    Distinct from :class:`P2pOrderBook` because legacy kept two screens:
    ``p2p_buy_menu`` (orders with «Купить #N» buttons, bot.py:19750+)
    and ``p2p_order_book`` (the read-style book with a currency filter,
    bot.py:20097+). P3 implements both; sharing one factory would
    forever weld the two screens together on the wire.
    """

    page: DbInt = 0


class P2pOrderBook(CallbackData, prefix="p2p_book"):
    """«Все ордера» — one order-book page.

    ``currency`` is ``""`` for the unfiltered view (CallbackData cannot
    carry ``None`` losslessly), else one of ``P2P_CURRENCIES``.
    ``page`` is the zero-based page index; the handler multiplies by
    its page size for the service's ``offset``.
    """

    currency: str = ""
    page: DbInt = 0


class P2pOrderView(CallbackData, prefix="p2p_order"):
    """Order detail card (price, limits, payment methods, seller stats)."""

    order_id: DbInt


class P2pBuyAll(CallbackData, prefix="p2p_buyall"):
    """«💰 Купить всё» — fill the order's whole remaining_com.

    The handler reads the CURRENT ``remaining_com`` and passes it to
    ``P2pService.buy``; the atomic fill guard makes a concurrent
    shrink lose the race loudly (RACE_LOST) instead of overselling.
    """

    order_id: DbInt


class P2pExpressStart(CallbackData, prefix="p2p_expr"):
    """«Экспресс-покупка» — pick a currency, then enter a fiat budget."""


class P2pExpressCurrency(CallbackData, prefix="p2p_excur"):
    """Express-buy currency pick (same six currencies as sell)."""

    currency: str


class P2pTradePaid(CallbackData, prefix="p2p_paid"):
    """Buyer's «✅ Я оплатил» on a pending trade.

    The service's ``mark_paid`` re-checks both the buyer seat and the
    ``pending`` status in SQL — a foreign or repeated press is inert.
    """

    trade_id: DbInt


class P2pTradeConfirm(CallbackData, prefix="p2p_conf"):
    """Seller's «✅ Подтвердить (COM отправлены)» on a paid trade.

    Releases escrow to the buyer. ``seller_confirm`` status-guards the
    transition (``paid`` only) so a double press cannot double-credit.
    """

    trade_id: DbInt


class P2pDisputeOpen(CallbackData, prefix="p2p_disp"):
    """«❓ Открыть спор» — either participant; admins get the card."""

    trade_id: DbInt


class P2pDisputeResolve(CallbackData, prefix="p2p_resolve"):
    """ADMIN-ONLY dispute resolution button (3 outcomes, incl. D1).

    ``resolution`` is one of the :class:`DisputeResolution` values
    (``refund_buyer`` / ``confirm_seller`` / ``return_seller``). The
    HANDLER must gate on developer ids (legacy bot.py:20220/20271)
    BEFORE calling the service — the service trusts ``admin_id`` only
    as an audit stamp, by documented contract.
    """

    trade_id: DbInt
    resolution: str


class P2pSellerStats(CallbackData, prefix="p2p_stats"):
    """Seller-reputation popup: «✅ сделок: N | ⚠️ споров: M» (D3)."""

    seller_id: DbInt
