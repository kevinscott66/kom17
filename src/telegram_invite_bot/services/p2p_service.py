"""P2P COM marketplace money core (#64, DESIGN_P2P.md §2.2-2.3).

Composes :class:`P2pRepo` + :class:`EconomyRepo` +
:class:`TransactionsRepo` over ONE shared ``economy`` session (the
CheckService composition pattern) so every flow is a single atomic
transaction committed by :class:`EconomyMiddleware` at request end.

The model (legacy parity, bot.py:19185-20308):

* **Pure COM escrow; fiat moves OUTSIDE the bot.** 0% commission.
* **Escrow-on-create:** the seller is debited when the sell order is
  created; the escrow IS ``p2p_sell_orders.remaining_com``. Cancel
  refunds ``remaining_com``.
* **Trade lifecycle:** pending → (buyer) paid → (seller confirms)
  confirmed = COM credited to the buyer + seller stats.
* **Disputes:** either party opens; developers resolve.

Hardening over legacy (display-neutral, §2.2):

1. Every escrow movement writes a **ledger row**: ``p2p_escrow`` debit
   on create; ``p2p_refund`` credit on cancel / seller-return /
   stranded-expiry; ``p2p_release`` credit to the buyer on confirm /
   dispute-release. /balance reconciliation then closes.
2. **Race-safe fills** via :meth:`P2pRepo.fill`'s rowcount guard — a
   failed guard aborts the trade (no oversell).
3. **Checked credits** everywhere: ``EconomyRepo.credit`` returning
   ``None`` (balance-cap overflow / vanished wallet) rolls the whole
   transition back + a loud log — the same posture as
   withdraw/transfer. The rollback is REQUIRED because the middleware
   commits on a normal return; returning CREDIT_FAILED without rolling
   back would commit the status flip with no payout. It is a SAVEPOINT
   rollback, not ``session.rollback()`` (#1985): the session belongs to
   the caller, and undoing a payout is no licence to undo whatever the
   rest of the update wrote to it. :meth:`expire_pending` has carried
   that argument since #209; the other three money paths caught up.
4. All transitions are status-guarded SQL, not read-then-write.

Deliberate deviations (§2.3, signed off):

* **D1** — third dispute outcome ``RETURN_SELLER``: credits the trade's
  COM back to the SELLER (ledger ``p2p_refund``), trade status
  ``dispute_returned_seller``, no stats change on either side.
* **D2** — pending-trade auto-expiry (default 30 min,
  ``P2P_PENDING_TTL_MINUTES``): ``pending`` older than the TTL →
  ``cancelled_timeout`` + the slice returns to the order's
  ``remaining_com`` (order reactivated if that fill completed it).
  Checked lazily on the buy paths + :meth:`sweep` for the
  EconomyCleanupSweeper's 60-second money pass. ``paid`` trades
  never auto-expire.
* **D3** — the dead always-5.0 ``rating`` is not ported; consumers
  render the real counters from :meth:`seller_stats`.
* **D4** — a ceiling on trades one BUYER may hold open at once
  (:data:`MAX_OPEN_TRADES_PER_BUYER`, ``pending`` + ``paid`` +
  ``disputed``). Opening a trade is free for the buyer but escrows
  the seller's COM, and neither ``paid`` nor ``disputed`` expires, so
  legacy let a single account freeze the whole book at no cost.
  Checked on both buy paths.
* **D5** — gate ORDER in :meth:`P2pService.buy`: the self-trade reject
  runs BEFORE the amount-bounds check, where legacy tested the bounds
  first (bot.py:19902-19903) and self-trade second (19905-19906). The
  only difference is which refusal a seller sees when they aim a bad
  amount at their own order, and only for an *oversized* one:
  ``amount_com <= 0`` never reaches the service, the handler answers
  ``h_p2p_amount_positive`` first
  (``handlers/p2p_trade.handle_buy_amount``). Both cards are in fact
  unreachable through the UI, but NOT — as this note claimed until
  #736 — because the buy button is withheld on one's own order. It is
  not withheld: :meth:`order_book` takes no viewer id and passes none
  to ``list_active``, and ``_render_order_list`` builds a row per order
  unconditionally (one ``_order_line`` per row), so a seller sees a
  live button on their own row. What makes ``SELF_TRADE`` unreachable
  is that both handlers reject the identity themselves before the
  service is called (``handlers/p2p_trade.handle_order_view`` and
  ``handle_buy_all``, both answering ``h_p2p_self_trade``) — the service
  outcome is the second line of defence, not the first. Kept in this
  order because the identity test is the cheaper and more fundamental
  one; recorded here rather than left as a silent divergence.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

from loguru import logger

from telegram_invite_bot.core.p2p import P2P_COM_RATES, market_rate
from telegram_invite_bot.repositories.p2p_repo import (
    ORDER_ACTIVE,
    TRADE_CONFIRMED,
    TRADE_DISPUTE_REFUND_BUYER,
    TRADE_DISPUTE_RETURNED_SELLER,
)
from telegram_invite_bot.utils.economy import _MAX_AMOUNT

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from telegram_invite_bot.db.models.p2p import P2pSellerStats, P2pSellOrder, P2pTrade
    from telegram_invite_bot.repositories.economy_repo import EconomyRepo
    from telegram_invite_bot.repositories.p2p_repo import P2pRepo
    from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo

log = logger.bind(component="services.p2p")

# Ledger ``type`` tags (§2.2.1) — distinct so /balance reconciliation and
# audits can net escrow holds against their refunds/releases.
ESCROW_TYPE = "p2p_escrow"
REFUND_TYPE = "p2p_refund"
RELEASE_TYPE = "p2p_release"

# D2 default — the fallback for the EconomyMiddleware construction
# sites that never see a P2P update, not the value P2P actually runs
# on. #703 claimed the middleware injected Settings here and it did
# not: of the 44 construction sites across 32 files, none passed the
# kwarg, so the handler path was pinned to 30 while
# EconomyCleanupSweeper (built in Application._start_background_tasks)
# honoured P2P_PENDING_TTL_MINUTES. The two agreed only because the
# variable is unset everywhere (commented out in .env.example); below
# 30 the sweeper would expire trades the handler still considered
# live, above 30 escrowed COM would go back to the order while the
# buyer was still paying.
#
# #1686 wired it: handlers/p2p.py and handlers/p2p_trade.py now pass
# ``p2p_pending_ttl_minutes`` on every EconomyMiddleware they hang, so
# both halves read the same setting. Pinned by
# tests/unit/handlers/test_p2p_ttl_wiring.py.
DEFAULT_PENDING_TTL_MINUTES = 30

# Deviation D4 — how many trades one buyer may hold open at once
# (``pending`` + ``paid`` + ``disputed``).
#
# Opening a trade costs the buyer nothing: no coins are debited, the
# fiat leg happens outside the bot, and the money only moves when the
# SELLER confirms. What it does cost is the seller's COM, which leaves
# the book into escrow the moment the trade opens. ``pending`` at least
# expires on the D2 TTL — ``paid`` never does (dispute only), and the
# buyer flips to ``paid`` by tapping a button nobody verifies.
#
# #1685: ``disputed`` counts too. It holds the escrow exactly like the
# other two and releases only when an admin rules, but the buyer
# reaches it unilaterally and for free. Leaving it out let the cap be
# reset at will — buy, dispute, repeat — which is the attack below,
# only slower to type.
#
# Without a cap, one ``/p2p express`` with a large enough budget opens a
# trade against EVERY order in the book, and a tap on each locks the
# whole marketplace indefinitely at zero cost to the attacker. Rate
# limiting does not help — one call is enough.
#
# Three is deliberately generous for a real buyer (who pays a seller,
# waits for the release and moves on) and useless as a lever.
MAX_OPEN_TRADES_PER_BUYER = 3

# #1690 — how many orders one seller may keep on the book at once.
#
# The book is a shared, finite surface: :meth:`P2pRepo.list_active`
# sorts by price then id and clamps ``limit`` to 50, and every order in
# a currency carries the SAME price (``core.p2p.P2P_COM_RATES`` is
# market-price-only), so rows cannot differentiate on price — they
# differentiate on id. A seller who posts sixty 1-COM orders therefore
# owns the whole express-buy window and every page a buyer pages
# through, and it costs nothing: the escrow is a ``hold``, and
# :meth:`cancel_order` returns 100% of the remaining COM.
#
# The cap is the size of the rate table: a real seller can stand in
# every currency book exactly once and never meet this gate. A seventh
# live order is by construction a duplicate listing in a book the
# seller is already in.
MAX_ACTIVE_ORDERS_PER_SELLER = len(P2P_COM_RATES)

# #1619: how many stale trades one D2 expiry pass may cancel.
#
# Bounds the work of a pass, not the marketplace: the full pass runs
# every 60 seconds, so this is 200 cancellations a minute against a
# production table measured in tens of rows. It matters because the
# same method is also the LAZY path — :meth:`buy`, :meth:`express_buy`
# and :meth:`order_book` all run it on a HANDLER's session, where an
# unbounded scan is paid for by a user waiting on a reply. A trade the
# cap leaves behind is cancelled by the next call a minute later; its
# escrowed COM is not lost, only late.
_EXPIRY_SCAN_LIMIT = 200


class CreateOrderOutcome(StrEnum):
    OK = "ok"
    INVALID_AMOUNT = "invalid_amount"
    """``amount_com <= 0`` (legacy bot.py:19328-19329)."""
    INVALID_CURRENCY = "invalid_currency"
    """Currency not in the 6-entry market-rate table."""
    INSUFFICIENT_FUNDS = "insufficient_funds"
    """Escrow debit failed (legacy bot.py:19331-19333 / 19479-19480)."""
    TOO_MANY_ORDERS = "too_many_orders"
    """The seller already holds ``MAX_ACTIVE_ORDERS_PER_SELLER`` live
    orders (#1690). Answered before the escrow hold, so a refused create
    never parks coins."""
    INVALID_LIMITS = "invalid_limits"
    """A per-trade bound that cannot be honoured: non-positive, above the
    ``_MAX_AMOUNT`` ceiling, or a max below the min. Rejected instead of
    stored — the book renders these bounds as a promise to the buyer, so
    an impossible pair would either lock the order or advertise a rule
    nobody can satisfy."""


class CancelOrderOutcome(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    NOT_YOURS = "not_yours"
    """Caller is not the order's seller (legacy bot.py:19577-19578)."""
    NOT_ACTIVE = "not_active"
    """Already cancelled/completed or drained (legacy bot.py:19580-19581)."""
    CREDIT_FAILED = "credit_failed"
    """Refund credit failed — the cancel was rolled back, order stays active."""


class BuyOutcome(StrEnum):
    OK = "ok"
    ORDER_NOT_ACTIVE = "order_not_active"
    """Order missing / not active on the re-fetch (legacy bot.py:19919-19926)."""
    SELF_TRADE = "self_trade"
    """Buyer is the seller (legacy bot.py:19905)."""
    INVALID_AMOUNT = "invalid_amount"
    """``amount <= 0`` or ``> remaining_com`` (legacy bot.py:19902)."""
    RACE_LOST = "race_lost"
    """The atomic fill guard failed — a concurrent buyer took the COM."""
    BELOW_MIN = "below_min"
    """Trade fiat under the order's ``min_amount`` (see
    :meth:`P2pService._limit_check`)."""
    ABOVE_MAX = "above_max"
    """Trade fiat over the order's ``max_amount``."""
    TOO_MANY_OPEN = "too_many_open"
    """Buyer already holds :data:`MAX_OPEN_TRADES_PER_BUYER` open trades
    (deviation D4). Only a terminal status frees a slot: disputing
    does not, because the escrow stays frozen either way (#1685)."""


class ExpressBuyOutcome(StrEnum):
    OK = "ok"
    INVALID_AMOUNT = "invalid_amount"
    """``max_fiat <= 0`` or non-finite (legacy bot.py:19692-19693)."""
    INVALID_CURRENCY = "invalid_currency"
    TOO_MANY_OPEN = "too_many_open"
    """No free slot under :data:`MAX_OPEN_TRADES_PER_BUYER` (deviation
    D4, counting ``disputed`` since #1685). Raised only when the buyer
    has NO slot at all; with one or two free the fill runs and simply
    stops at the ceiling."""
    NO_ORDERS = "no_orders"
    """Nothing fillable (legacy bot.py:19707-19708) — also covers the
    case where every candidate was the buyer's own order."""


class MarkPaidOutcome(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    """No such trade, or the caller is not its buyer (legacy queries
    ``WHERE id = ? AND buyer_id = ?``, bot.py:19979-19982)."""
    NOT_PENDING = "not_pending"
    """Already paid/confirmed/disputed/expired (legacy bot.py:19987-19988)."""


class ConfirmOutcome(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    """No such trade, or the caller is not its seller (bot.py:20031-20036)."""
    NOT_PAID = "not_paid"
    """Buyer hasn't marked paid — or this is a double-release attempt
    (the second confirm finds status != 'paid'; bot.py:20039-20040)."""
    CREDIT_FAILED = "credit_failed"
    """Buyer credit failed — the confirm was rolled back, trade stays paid."""


class OpenDisputeOutcome(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    NOT_PARTICIPANT = "not_participant"
    """Caller is neither seat of the trade (hardening — legacy let ANY
    callback sender dispute any trade id, bot.py:20187-20198)."""
    NOT_DISPUTABLE = "not_disputable"
    """Status is not pending|paid (already settled / already disputed)."""


class DisputeResolution(StrEnum):
    """The developer's decision for :meth:`P2pService.resolve_dispute`."""

    REFUND_BUYER = "refund_buyer"
    """Legacy bot.py:20216-20264: credit the BUYER, status
    ``dispute_refund_buyer``, seller ``dispute_count += 1``."""
    CONFIRM_SELLER = "confirm_seller"
    """Legacy bot.py:20267-20313: credit the BUYER, status
    ``confirmed``, seller success stats — the trade was legitimate."""
    RETURN_SELLER = "return_seller"
    """Deviation D1: credit the SELLER (ledger ``p2p_refund``), status
    ``dispute_returned_seller``, no stats change — the "buyer never
    paid fiat" case legacy could not resolve."""


class ResolveOutcome(StrEnum):
    OK = "ok"
    NOT_FOUND = "not_found"
    NOT_DISPUTED = "not_disputed"
    """Status guard failed (already resolved / never disputed) —
    legacy bot.py:20233-20235."""
    CREDIT_FAILED = "credit_failed"
    """Payout credit failed — the resolution was rolled back, trade
    stays disputed for a retry."""


@dataclass(frozen=True, slots=True)
class CreateOrderResult:
    outcome: CreateOrderOutcome
    order_id: int = 0
    price_per_com: float = 0.0
    """The fixed market rate the order was priced at."""
    total_fiat: float = 0.0
    """``amount_com * price_per_com`` — the legacy success-card estimate."""


@dataclass(frozen=True, slots=True)
class CancelOrderResult:
    outcome: CancelOrderOutcome
    returned_com: int = 0
    """Exactly the order's ``remaining_com`` at cancel time."""


@dataclass(frozen=True, slots=True)
class BuyResult:
    outcome: BuyOutcome
    trade_id: int = 0
    seller_id: int = 0
    amount_com: int = 0
    total_fiat: float = 0.0
    fiat_currency: str = ""
    remaining_com: int = 0
    """Order's remaining escrow AFTER this fill (0 == order completed)."""
    limit_fiat: float = 0.0
    """BELOW_MIN / ABOVE_MAX only: the bound that rejected the trade, in
    the order's fiat currency."""
    limit_com: int = 0
    """The same bound expressed in COM at the order's price — the buyer
    types COM, so the message has to speak COM."""


@dataclass(frozen=True, slots=True)
class ExpressFill:
    """One trade the express loop opened (legacy result tuple,
    bot.py:19632-19680, plus ``seller_id`` for the notification)."""

    trade_id: int
    order_id: int
    seller_id: int
    amount_com: int
    total_fiat: float


@dataclass(frozen=True, slots=True)
class ExpressBuyResult:
    outcome: ExpressBuyOutcome
    fills: tuple[ExpressFill, ...] = ()
    total_com: int = 0
    total_fiat: float = 0.0


@dataclass(frozen=True, slots=True)
class MarkPaidResult:
    outcome: MarkPaidOutcome
    seller_id: int = 0
    amount_com: int = 0
    total_fiat: float = 0.0
    fiat_currency: str = ""


@dataclass(frozen=True, slots=True)
class ConfirmResult:
    outcome: ConfirmOutcome
    buyer_id: int = 0
    amount_com: int = 0


@dataclass(frozen=True, slots=True)
class OpenDisputeResult:
    outcome: OpenDisputeOutcome
    seller_id: int = 0
    buyer_id: int = 0
    amount_com: int = 0
    total_fiat: float = 0.0
    fiat_currency: str = ""


@dataclass(frozen=True, slots=True)
class ResolveResult:
    outcome: ResolveOutcome
    resolution: DisputeResolution | None = None
    seller_id: int = 0
    buyer_id: int = 0
    amount_com: int = 0


@dataclass(frozen=True, slots=True)
class ExpiredTrade:
    """One trade :meth:`P2pService.expire_pending` cancelled (D2)."""

    trade_id: int
    order_id: int
    seller_id: int
    buyer_id: int
    amount_com: int
    returned_to_order: bool
    """True: slice went back to ``remaining_com``; False: the order was
    already cancelled, so the slice was refunded to the seller's wallet."""


@dataclass(frozen=True, slots=True)
class SweepReport:
    """Hourly-sweep result, for observability + tests."""

    expired: tuple[ExpiredTrade, ...] = field(default=())

    @property
    def count(self) -> int:
        return len(self.expired)


# Fiat comparisons run on ``com * price`` floats, so an exact ``<`` would
# reject a trade that lands one ULP under its own bound. A hundredth of a
# minor unit is below anything a currency can express.
_FIAT_EPS = 1e-6


def limits_are_sane(min_amount: int | None, max_amount: int | None) -> bool:
    """Reject bounds the book could not honour.

    Public because the sell interview checks the pair BEFORE handing it
    to :meth:`P2pService.create_sell_order`: the handler clears the FSM
    on its way into creation, so a service-side rejection would cost the
    seller the whole interview over one mistyped number. The service
    keeps its own check as the backstop for every other caller.

    A non-positive bound is meaningless (``0`` is how "unset" already
    renders), and ``max < min`` describes an order no amount can fill —
    stored, it would advertise a rule to buyers that rejects every one
    of them.

    The upper bound is the same idea, and closes a crash rather than a
    display bug. ``1e30`` typed into the limits line parses to a finite
    integer that is positive and correctly ordered — it passed both
    checks above and reached the order INSERT, where sqlite3 refuses to
    bind anything wider than 64 bits. The seller's escrow was debited by
    then, so the ``OverflowError`` landed mid-transaction: the rollback
    saved the coins, but the seller got a generic failure with no hint
    which field was wrong.

    :data:`_MAX_AMOUNT` is the ceiling rather than the driver's
    ``2**63`` because it is the documented cap on a wallet, so a bound
    above it cannot be filled by anyone and rejecting it costs nothing
    real — and one number stays *the* ceiling across the wallet and the
    book instead of two that drift apart.
    """
    if min_amount is not None and not 0 < min_amount <= _MAX_AMOUNT:
        return False
    if max_amount is not None and not 0 < max_amount <= _MAX_AMOUNT:
        return False
    return not (min_amount is not None and max_amount is not None and max_amount < min_amount)


def _effective_min_fiat(min_amount: int | None, order_fiat: float) -> float | None:
    """The min bound, clamped to what the order can still sell.

    A 1000-RUB minimum on an order with 300 RUB left would strand that
    remainder forever: no buyer could satisfy the bound, and the seller
    would have to cancel to get the escrow back. Clamping keeps the
    bound meaningful while the order is full and lets the tail be
    bought out.
    """
    if min_amount is None:
        return None
    return min(float(min_amount), order_fiat)


class P2pService:
    """Atomic P2P money flows against the shared economy session."""

    def __init__(
        self,
        p2p_repo: P2pRepo,
        economy_repo: EconomyRepo,
        transactions_repo: TransactionsRepo,
        session: AsyncSession,
        *,
        pending_ttl_minutes: int = DEFAULT_PENDING_TTL_MINUTES,
    ) -> None:
        self._p2p = p2p_repo
        self._economy = economy_repo
        self._ledger = transactions_repo
        self._session = session
        self._pending_ttl = timedelta(minutes=pending_ttl_minutes)

    # ------------------------------------------------------------------
    # Sell side
    # ------------------------------------------------------------------

    async def create_sell_order(
        self,
        *,
        seller_id: int,
        amount_com: int,
        currency: str,
        now: datetime,
        payment_methods: str | None = None,
        min_amount: int | None = None,
        max_amount: int | None = None,
    ) -> CreateOrderResult:
        """Escrow-on-create: hold the seller's coins, then insert the order.

        Order matters for the money invariant (mirrors check create):

        1. Validate the currency (market-price-only — the rate IS the
           price), ``amount_com > 0`` (legacy bot.py:19328-19329), the
           optional per-trade bounds and the seller's live-order count
           against ``MAX_ACTIVE_ORDERS_PER_SELLER`` (#1690), all before
           any coin moves. The cap is deliberately part of this step and
           not of the next one: a refused create must not park coins, or
           the refusal would itself be the escrow the flood was after.
           The count is taken under the writer lock; the three checks
           above it are pure Python and refuse without touching the
           database at all.
        2. Checked HOLD of exactly ``amount_com`` (legacy bot.py:19486
           wrote ``balance - amount`` UNguarded after a Python balance
           check at 19478-19479 — our guard closes that TOCTOU).
           ``None`` →
           INSUFFICIENT_FUNDS, no order row. :meth:`EconomyRepo.hold`
           rather than :meth:`~EconomyRepo.debit` (#1501): an escrow is
           a park, not a spend, and legacy wrote the balance column and
           nothing else across the whole P2P region — there is not one
           ``total_spent`` / ``total_earned`` write between bot.py:19000
           and 21200.
        3. Insert the ``active`` order with ``remaining_com = amount``.
        4. Ledger row ``type='p2p_escrow'`` (from=seller, to=None) —
           the hold leaves the wallet ecosystem into escrow.

        All three writes land in one transaction; a flush failure rolls
        the hold back with the rest.
        """
        rate = market_rate(currency)
        if rate is None:
            return CreateOrderResult(outcome=CreateOrderOutcome.INVALID_CURRENCY)
        if amount_com <= 0:
            return CreateOrderResult(outcome=CreateOrderOutcome.INVALID_AMOUNT)
        if not limits_are_sane(min_amount, max_amount):
            return CreateOrderResult(outcome=CreateOrderOutcome.INVALID_LIMITS)
        # #1503 for the sell side. The cap below is a ceiling decided by
        # a read, and a read in this project holds nothing: the engine
        # opens ``BEGIN IMMEDIATE`` only for a write-headed statement
        # (``db/engines.py``), so two taps both counted the same
        # pre-insert total and both were admitted. The escrow ``hold``
        # that follows IS serialised — it is a write — but by then the
        # gate has said yes twice, and the seller stands in the book one
        # order past a cap whose entire purpose is to stop exactly that
        # (#1690). :meth:`buy` and :meth:`express_buy` have opened with
        # this call since #1503; the sell side ran the same
        # read-then-write shape without it.
        #
        # The lock is taken here rather than at the top of the method so
        # that an invalid currency, amount or bound pair is still refused
        # without making the whole marketplace queue behind it.
        await self._p2p.lock_writer()
        live = await self._p2p.count_active_orders(seller_id)
        if live >= MAX_ACTIVE_ORDERS_PER_SELLER:
            return CreateOrderResult(outcome=CreateOrderOutcome.TOO_MANY_ORDERS)

        debited = await self._economy.hold(seller_id, amount_com)
        if debited is None:
            return CreateOrderResult(outcome=CreateOrderOutcome.INSUFFICIENT_FUNDS)

        order = await self._p2p.create_order(
            user_id=seller_id,
            amount_com=amount_com,
            price_per_com=rate,
            fiat_currency=currency.upper(),
            payment_methods=payment_methods,
            min_amount=min_amount,
            max_amount=max_amount,
            now=now,
        )
        await self._ledger.record(
            from_id=seller_id,
            to_id=None,
            amount=amount_com,
            reason=f"p2p order #{order.id} escrow",
            type=ESCROW_TYPE,
            date=now,
        )
        return CreateOrderResult(
            outcome=CreateOrderOutcome.OK,
            order_id=int(order.id),
            price_per_com=rate,
            total_fiat=amount_com * rate,
        )

    async def cancel_order(
        self, *, seller_id: int, order_id: int, now: datetime
    ) -> CancelOrderResult:
        """Cancel an active order, refunding exactly ``remaining_com``.

        The owner / active / remaining>0 gates mirror legacy
        bot.py:19573-19581; the cancel itself is the repo's one-statement
        guard so a double tap can't refund twice. The refund credit is
        CHECKED — on failure the whole cancel rolls back (order stays
        active, escrow intact) rather than burning the escrow.
        """
        order = await self._p2p.get_order(order_id)
        if order is None:
            return CancelOrderResult(outcome=CancelOrderOutcome.NOT_FOUND)
        if int(order.user_id) != seller_id:
            return CancelOrderResult(outcome=CancelOrderOutcome.NOT_YOURS)

        # #1985: the flip and its refund share one SAVEPOINT. The
        # guard must be inside it — undoing the refund while leaving
        # ``cancelled`` standing would strand the escrow — and the
        # savepoint must be what gets rolled back, not the session:
        # this is the handler's session, and everything else the update
        # has written to it is uncommitted too. See
        # :meth:`expire_pending`, where #209 already made this argument.
        async with self._session.begin_nested() as savepoint:
            remaining = await self._p2p.cancel_guard(order_id, seller_id)
            if remaining is None:
                return CancelOrderResult(outcome=CancelOrderOutcome.NOT_ACTIVE)

            # ``release`` rather than ``credit`` (#1501): handing back coins
            # the seller already owned is not income. ``credit`` here made a
            # create/cancel round trip — which moves no money at all — add
            # ``amount`` to BOTH lifetime counters on the ``/balance`` card,
            # repeatable for as long as the seller cares to tap.
            credited = await self._economy.release(seller_id, remaining)
            if credited is None:
                await savepoint.rollback()
                log.bind(order_id=order_id, seller_id=seller_id, amount=remaining).error(
                    "p2p cancel refund credit failed; cancel rolled back"
                )
                return CancelOrderResult(outcome=CancelOrderOutcome.CREDIT_FAILED)
            await self._ledger.record(
                from_id=None,
                to_id=seller_id,
                amount=remaining,
                reason=f"p2p order #{order_id} cancelled",
                type=REFUND_TYPE,
                date=now,
            )
        return CancelOrderResult(outcome=CancelOrderOutcome.OK, returned_com=remaining)

    # ------------------------------------------------------------------
    # Buy side
    # ------------------------------------------------------------------

    async def buy(
        self, *, buyer_id: int, order_id: int, amount_com: int, now: datetime
    ) -> BuyResult:
        """Open a ``pending`` trade for ``amount_com`` from one order.

        Lazy D2 expiry for THIS order runs first (a stale pending trade
        may return its slice, reactivating a "completed" order), then
        the gates, legacy's own run being bot.py:19891-19939: re-fetch +
        active check → self-trade reject → amount bounds → the seller's
        per-trade fiat bounds → the atomic fill guard (replacing legacy's
        unguarded read-then-write) → pending trade insert →
        complete-if-drained. Those middle two are swapped against legacy
        — see **D5** in the module docstring.

        The bounds gate is new: legacy stored ``min_amount`` /
        ``max_amount``, printed them in the order book, and then let any
        amount through — the buyer read a rule the bot never applied.

        No money moves here — the COM is already escrowed in the order;
        the fill only carves the slice into the trade.
        """
        # #1503: the writer lock comes FIRST, so the open-trade count
        # below cannot be taken concurrently by the buyer's own second
        # tap. See ``P2pRepo.lock_writer`` for why a SELECT-only prologue
        # holds nothing at all in this project.
        await self._p2p.lock_writer()
        await self.expire_pending(now=now, order_id=order_id)

        order = await self._p2p.get_order(order_id)
        if order is None or order.status != ORDER_ACTIVE or int(order.remaining_com) <= 0:
            return BuyResult(outcome=BuyOutcome.ORDER_NOT_ACTIVE)
        seller_id = int(order.user_id)
        if seller_id == buyer_id:
            return BuyResult(outcome=BuyOutcome.SELF_TRADE)
        remaining = int(order.remaining_com)
        if amount_com <= 0 or amount_com > remaining:
            return BuyResult(outcome=BuyOutcome.INVALID_AMOUNT)

        price = float(order.price_per_com)
        rejected = self._limit_check(
            amount_com=amount_com,
            remaining=remaining,
            price=price,
            min_amount=order.min_amount,
            max_amount=order.max_amount,
            fiat_currency=str(order.fiat_currency),
        )
        if rejected is not None:
            return rejected

        if not await self._has_open_slot(buyer_id, now=now):
            return BuyResult(outcome=BuyOutcome.TOO_MANY_OPEN)

        filled = await self._p2p.fill(order_id, amount_com)
        if not filled:
            return BuyResult(outcome=BuyOutcome.RACE_LOST)

        total_fiat = amount_com * price
        trade = await self._p2p.create_trade(
            order_id=order_id,
            seller_id=seller_id,
            buyer_id=buyer_id,
            amount_com=amount_com,
            price_per_com=price,
            total_fiat=total_fiat,
            fiat_currency=str(order.fiat_currency),
            now=now,
        )
        await self._p2p.complete_if_drained(order_id)
        return BuyResult(
            outcome=BuyOutcome.OK,
            trade_id=int(trade.id),
            seller_id=seller_id,
            amount_com=amount_com,
            total_fiat=total_fiat,
            fiat_currency=str(order.fiat_currency),
            remaining_com=remaining - amount_com,
        )

    async def _free_open_slots(self, buyer_id: int, *, now: datetime) -> int:
        """Trades this buyer may still open (deviation D4), never < 0.

        A buyer sitting at the ceiling is not refused on the first
        count: their own stale ``pending`` trades may be past the D2
        TTL and simply not swept yet, and a cap that refuses a buyer
        because of trades that no longer hold anything would be a bug
        wearing a rule's clothes. So the full expiry pass runs and the
        count is retaken — on the refusal path only, where the extra
        scan costs nothing anybody notices.

        Both counts are reads, and reads decide nothing on their own in
        SQLite. Callers must already hold the writer lock
        (``P2pRepo.lock_writer``, #1503) or the number returned here is
        a snapshot a concurrent buy is free to invalidate before the
        caller acts on it.
        """
        open_now = await self._p2p.count_open_as_buyer(buyer_id)
        if open_now >= MAX_OPEN_TRADES_PER_BUYER:
            await self.expire_pending(now=now)
            open_now = await self._p2p.count_open_as_buyer(buyer_id)
        return max(0, MAX_OPEN_TRADES_PER_BUYER - open_now)

    async def _has_open_slot(self, buyer_id: int, *, now: datetime) -> bool:
        return await self._free_open_slots(buyer_id, now=now) > 0

    def _limit_check(
        self,
        *,
        amount_com: int,
        remaining: int,
        price: float,
        min_amount: int | None,
        max_amount: int | None,
        fiat_currency: str,
    ) -> BuyResult | None:
        """``None`` when the slice fits the seller's bounds, else the
        rejection carrying that bound in both fiat and COM.

        COM matters because that is the unit the buyer types: telling
        them "минимум 500 RUB" without the COM equivalent makes them do
        the division themselves, and rounding the wrong way just earns a
        second rejection. ``ceil`` for the minimum and ``floor`` for the
        maximum keep the suggested number inside the bound.

        #695: ``fiat_currency`` is threaded in because the rejection is
        rendered with it — ``handlers/p2p_trade.handle_buy_amount``
        passes ``cur=result.fiat_currency`` into
        ``h_p2p_buy_below_min``. It used to fall through as the
        dataclass default ``""``, so the buyer read "минимум 500.00"
        with no currency at all while the "buy all" branch
        (``handlers/p2p_trade.handle_buy_all``), which reads the
        currency off the order instead, printed it correctly.
        """
        fiat = amount_com * price
        order_fiat = remaining * price
        min_fiat = _effective_min_fiat(min_amount, order_fiat)
        if min_fiat is not None and fiat < min_fiat - _FIAT_EPS:
            return BuyResult(
                outcome=BuyOutcome.BELOW_MIN,
                fiat_currency=fiat_currency,
                limit_fiat=min_fiat,
                limit_com=min(math.ceil(min_fiat / price), remaining),
            )
        if max_amount is not None and fiat > max_amount + _FIAT_EPS:
            return BuyResult(
                outcome=BuyOutcome.ABOVE_MAX,
                fiat_currency=fiat_currency,
                limit_fiat=float(max_amount),
                limit_com=math.floor(max_amount / price),
            )
        return None

    async def express_buy(
        self, *, buyer_id: int, currency: str, max_fiat: float, now: datetime
    ) -> ExpressBuyResult:
        """Auto-fill the cheapest orders for a fiat budget.

        The legacy loop verbatim (bot.py:19632-19680): active orders in
        ``currency`` cheapest-first; skip the buyer's own orders
        (19644); per order ``take_fiat = min(remaining_fiat,
        rem * price)``, skip dust (< 1e-6, 19649), ``take_com =
        min(rem, int(take_fiat / price))`` (truncating int — 19651),
        skip zero; open a pending trade for ``take_com``.

        Hardening: each order is taken through the atomic fill guard —
        a guard loss (concurrent buyer) SKIPS that order and the loop
        continues, where legacy oversold (fragility #6). Lazy D2 expiry
        for the whole currency book runs first so stale pendings free
        their slices into this fill.

        The seller's per-trade bounds are honoured here too: ``max``
        caps the slice, and an order whose ``min`` the remaining budget
        cannot reach is skipped rather than filled below its floor. An
        order carrying a contradictory legacy pair (``max < min``, which
        creation now rejects) is simply never filled — the seller can
        still cancel it and take the escrow back.
        """
        if market_rate(currency) is None:
            return ExpressBuyResult(outcome=ExpressBuyOutcome.INVALID_CURRENCY)
        if not math.isfinite(max_fiat) or max_fiat <= 0:
            # ``<= 0`` alone is not a budget check on a float: NaN
            # answers False to every comparison, so it survived to
            # ``int(take_fiat / price)`` and raised there, and Infinity
            # made ``min(remaining_fiat, …)`` a no-op — the budget
            # stopped bounding the fill and the loop drained the book.
            # A budget we cannot represent is a budget we refuse.
            return ExpressBuyResult(outcome=ExpressBuyOutcome.INVALID_AMOUNT)

        # #1503, as in :meth:`buy` — and it matters more here, because
        # ``slots`` does not merely admit the call, it sizes the loop.
        # Two concurrent express buys that both read three free slots
        # open six trades between them.
        await self._p2p.lock_writer()
        await self.expire_pending(now=now)

        slots = await self._free_open_slots(buyer_id, now=now)
        if slots <= 0:
            return ExpressBuyResult(outcome=ExpressBuyOutcome.TOO_MANY_OPEN)

        # The book is small (legacy read it whole); 50 is the repo's cap
        # and far beyond any realistic active-order count per currency.
        orders = await self._p2p.list_active(currency=currency.upper(), limit=50)
        fills: list[ExpressFill] = []
        total_com = 0
        total_fiat_spent = 0.0
        remaining_fiat = max_fiat
        for order in orders:
            if remaining_fiat <= 0:
                break
            if len(fills) >= slots:
                # The budget still has room, but the buyer does not.
                # Stopping here (rather than refusing the whole call)
                # keeps express useful: the fills already made are real
                # trades and the buyer is told what was taken.
                break
            seller_id = int(order.user_id)
            if seller_id == buyer_id:
                continue
            rem_com = int(order.remaining_com)
            price = float(order.price_per_com)
            fiat_available = rem_com * price
            take_fiat = min(remaining_fiat, fiat_available)
            if order.max_amount is not None:
                # Cap BEFORE the COM truncation so the ceiling applies to
                # the slice actually taken, not to the whole budget.
                take_fiat = min(take_fiat, float(order.max_amount))
            if take_fiat < 1e-6:
                continue
            take_com = min(rem_com, int(take_fiat / price))
            if take_com <= 0:
                continue
            min_fiat = _effective_min_fiat(order.min_amount, fiat_available)
            if min_fiat is not None and take_com * price < min_fiat - _FIAT_EPS:
                # The budget cannot reach this seller's floor. Skip and
                # keep walking the book — express is a best-effort fill,
                # and a cheaper order further down may still fit.
                continue
            if not await self._p2p.fill(int(order.id), take_com):
                # Lost the race to a concurrent buyer — skip, don't abort.
                continue
            actual_fiat = take_com * price
            trade = await self._p2p.create_trade(
                order_id=int(order.id),
                seller_id=seller_id,
                buyer_id=buyer_id,
                amount_com=take_com,
                price_per_com=price,
                total_fiat=actual_fiat,
                fiat_currency=str(order.fiat_currency),
                now=now,
            )
            await self._p2p.complete_if_drained(int(order.id))
            fills.append(
                ExpressFill(
                    trade_id=int(trade.id),
                    order_id=int(order.id),
                    seller_id=seller_id,
                    amount_com=take_com,
                    total_fiat=actual_fiat,
                )
            )
            total_com += take_com
            total_fiat_spent += actual_fiat
            remaining_fiat -= actual_fiat

        if not fills:
            return ExpressBuyResult(outcome=ExpressBuyOutcome.NO_ORDERS)
        return ExpressBuyResult(
            outcome=ExpressBuyOutcome.OK,
            fills=tuple(fills),
            total_com=total_com,
            total_fiat=total_fiat_spent,
        )

    # ------------------------------------------------------------------
    # Trade lifecycle
    # ------------------------------------------------------------------

    async def mark_paid(self, *, buyer_id: int, trade_id: int, now: datetime) -> MarkPaidResult:
        """Buyer's "Я оплатил": guarded ``pending → paid``.

        No money moves — purely the status stamp the seller waits on
        (legacy bot.py:19972-19991). A pending trade past the D2 TTL is
        NOT lazily expired here: the buyer asserting payment beats the
        sweep (the guarded transition makes the race safe either way).
        """
        trade = await self._p2p.get_trade(trade_id)
        if trade is None or int(trade.buyer_id) != buyer_id:
            return MarkPaidResult(outcome=MarkPaidOutcome.NOT_FOUND)
        moved = await self._p2p.mark_paid(trade_id, buyer_id, now)
        if not moved:
            return MarkPaidResult(outcome=MarkPaidOutcome.NOT_PENDING)
        return MarkPaidResult(
            outcome=MarkPaidOutcome.OK,
            seller_id=int(trade.seller_id),
            amount_com=int(trade.amount_com),
            total_fiat=float(trade.total_fiat),
            fiat_currency=str(trade.fiat_currency),
        )

    async def seller_confirm(
        self, *, seller_id: int, trade_id: int, now: datetime
    ) -> ConfirmResult:
        """Seller's "COM отправлены": release the escrow to the buyer.

        Order (the order matters):

        1. ``paid → confirmed`` guarded transition (the double-release
           guard — a second confirm finds ``status != 'paid'``).
        2. CHECKED credit of the buyer; ``None`` → rollback the
           transition + CREDIT_FAILED (trade stays ``paid``, escrow
           stays held — no silent burn). Legacy credited UNchecked at
           bot.py:20042.
        3. Ledger ``p2p_release`` to the buyer.
        4. Seller stats: ``successful_trades += 1``, ``total_sold_com
           += amount`` (legacy bot.py:20044-20047).
        """
        trade = await self._p2p.get_trade(trade_id)
        if trade is None or int(trade.seller_id) != seller_id:
            return ConfirmResult(outcome=ConfirmOutcome.NOT_FOUND)
        buyer_id = int(trade.buyer_id)
        amount_com = int(trade.amount_com)

        # #1985: one SAVEPOINT around steps 1-4 — see :meth:`cancel_order`.
        async with self._session.begin_nested() as savepoint:
            moved = await self._p2p.confirm_from_paid(trade_id, seller_id, now)
            if not moved:
                return ConfirmResult(outcome=ConfirmOutcome.NOT_PAID)

            # ``release``, not ``credit`` (#1501): these coins have been out
            # of circulation in escrow since the order was created, and
            # legacy's release is a bare ``balance + amount`` (bot.py:20042)
            # with no counter write anywhere in the region. The buyer paid
            # fiat off-platform, so nothing about this leg is visible to the
            # in-bot lifetime totals either way.
            credited = await self._economy.release(buyer_id, amount_com)
            if credited is None:
                await savepoint.rollback()
                log.bind(trade_id=trade_id, buyer_id=buyer_id, amount=amount_com).error(
                    "p2p release credit failed; confirm rolled back"
                )
                return ConfirmResult(outcome=ConfirmOutcome.CREDIT_FAILED)
            await self._ledger.record(
                from_id=None,
                to_id=buyer_id,
                amount=amount_com,
                reason=f"p2p trade #{trade_id} release",
                type=RELEASE_TYPE,
                date=now,
            )
            await self._p2p.record_successful_trade(seller_id, amount_com)
        return ConfirmResult(outcome=ConfirmOutcome.OK, buyer_id=buyer_id, amount_com=amount_com)

    async def open_dispute(self, *, user_id: int, trade_id: int) -> OpenDisputeResult:
        """Either party freezes the trade: ``pending|paid → disputed``.

        Hardening over legacy (which let any callback sender dispute
        any trade unconditionally, bot.py:20187-20198): the caller must
        sit on one of the trade's seats and the transition is
        status-guarded. No money moves — only developers resolve.
        """
        trade = await self._p2p.get_trade(trade_id)
        if trade is None:
            return OpenDisputeResult(outcome=OpenDisputeOutcome.NOT_FOUND)
        seller_id = int(trade.seller_id)
        buyer_id = int(trade.buyer_id)
        if user_id not in (seller_id, buyer_id):
            return OpenDisputeResult(outcome=OpenDisputeOutcome.NOT_PARTICIPANT)
        moved = await self._p2p.mark_disputed(trade_id)
        if not moved:
            return OpenDisputeResult(outcome=OpenDisputeOutcome.NOT_DISPUTABLE)
        return OpenDisputeResult(
            outcome=OpenDisputeOutcome.OK,
            seller_id=seller_id,
            buyer_id=buyer_id,
            amount_com=int(trade.amount_com),
            total_fiat=float(trade.total_fiat),
            fiat_currency=str(trade.fiat_currency),
        )

    async def resolve_dispute(
        self,
        *,
        admin_id: int,
        trade_id: int,
        resolution: DisputeResolution,
        now: datetime,
    ) -> ResolveResult:
        """Developer resolves a disputed trade — three outcomes.

        DEVELOPER AUTHORISATION IS THE CALLER'S JOB (the handler gates
        on developer ids exactly like legacy bot.py:20220/20271); the
        service trusts ``admin_id`` for the audit stamp only.

        All three share the shape: guarded ``disputed → <status>``
        transition first, then the CHECKED payout credit (rollback +
        CREDIT_FAILED on ``None``, leaving the trade disputed for a
        retry), then the ledger row (+ stats where applicable):

        * REFUND_BUYER (bot.py:20237-20247): buyer gets the COM
          (ledger ``p2p_release`` — it is the escrow leaving to the
          buyer), seller ``dispute_count += 1``.
        * CONFIRM_SELLER (bot.py:20288-20296): buyer gets the COM
          (``p2p_release``), seller gets the success stats — identical
          money to a normal confirm, the dispute was unfounded.
        * RETURN_SELLER (D1): SELLER gets the COM back (ledger
          ``p2p_refund``), status ``dispute_returned_seller``, no
          stats change on either side.
        """
        trade = await self._p2p.get_trade(trade_id)
        if trade is None:
            return ResolveResult(outcome=ResolveOutcome.NOT_FOUND)
        seller_id = int(trade.seller_id)
        buyer_id = int(trade.buyer_id)
        amount_com = int(trade.amount_com)

        if resolution is DisputeResolution.RETURN_SELLER:
            new_status = TRADE_DISPUTE_RETURNED_SELLER
            payee = seller_id
            ledger_type = REFUND_TYPE
            set_confirmed_at = False
        elif resolution is DisputeResolution.CONFIRM_SELLER:
            new_status = TRADE_CONFIRMED
            payee = buyer_id
            ledger_type = RELEASE_TYPE
            set_confirmed_at = True
        else:  # REFUND_BUYER
            new_status = TRADE_DISPUTE_REFUND_BUYER
            payee = buyer_id
            ledger_type = RELEASE_TYPE
            set_confirmed_at = False

        # #1985: one SAVEPOINT around the transition, the payout, the
        # ledger row and the stats — see :meth:`cancel_order`.
        async with self._session.begin_nested() as savepoint:
            moved = await self._p2p.resolve_disputed(
                trade_id,
                new_status=new_status,
                resolved_by=admin_id,
                now=now,
                set_confirmed_at=set_confirmed_at,
            )
            if not moved:
                return ResolveResult(outcome=ResolveOutcome.NOT_DISPUTED)

            # ``release`` for the same reason as the happy path (#1501):
            # whichever side wins, the coins come out of the escrow they
            # were already parked in. Legacy bot.py:20237 and :20288 are
            # both bare ``balance + amount``.
            credited = await self._economy.release(payee, amount_com)
            if credited is None:
                await savepoint.rollback()
                log.bind(
                    trade_id=trade_id,
                    payee=payee,
                    amount=amount_com,
                    resolution=str(resolution),
                ).error("p2p dispute payout credit failed; resolution rolled back")
                return ResolveResult(outcome=ResolveOutcome.CREDIT_FAILED)
            await self._ledger.record(
                from_id=None,
                to_id=payee,
                amount=amount_com,
                reason=f"p2p trade #{trade_id} dispute {resolution}",
                type=ledger_type,
                date=now,
            )

            if resolution is DisputeResolution.REFUND_BUYER:
                await self._p2p.record_dispute(seller_id)
            elif resolution is DisputeResolution.CONFIRM_SELLER:
                await self._p2p.record_successful_trade(seller_id, amount_com)

        return ResolveResult(
            outcome=ResolveOutcome.OK,
            resolution=resolution,
            seller_id=seller_id,
            buyer_id=buyer_id,
            amount_com=amount_com,
        )

    # ------------------------------------------------------------------
    # D2 expiry
    # ------------------------------------------------------------------

    async def expire_pending(
        self, *, now: datetime, order_id: int | None = None
    ) -> list[ExpiredTrade]:
        """Cancel ``pending`` trades older than the TTL, freeing escrow.

        Deviation D2. Per stale trade:

        1. Guarded ``pending → cancelled_timeout`` (the ``created_at <
           cutoff`` re-check rides the UPDATE, so a buyer's concurrent
           "Я оплатил" wins cleanly — see the repo guard).
        2. The slice returns to the order's ``remaining_com``; the
           order goes (back to) ``active`` — including the "this fill
           completed the order" case.
        3. If the order is ``cancelled`` (its escrow was already
           refunded), the slice can't rejoin it — it is refunded to
           the SELLER's wallet instead, with a CHECKED credit + ledger
           ``p2p_refund`` row, so no COM is ever stranded.

        #209: each trade is its own SAVEPOINT, and steps 1-3 live
        INSIDE it — the status flip must be undone with the refund or
        the slice is stranded. A failed credit rolls back that one
        trade and the pass moves on; the trade stays ``pending`` for the
        next sweep. This replaces a ``session.rollback()`` on the SHARED
        session, which was wrong twice over: the sweeper hands this
        service the same session as the PvP/inventory/game-plays steps,
        so one bad trade silently discarded THEIR work too, and the
        caller kept sweeping and reporting afterwards — a partial commit
        plus an over-stated report. The lazy on-access callers
        (:meth:`buy` / :meth:`express_buy` / :meth:`order_book`) are
        worse still: there the shared session is the HANDLER's.

        ``order_id`` narrows the scan for :meth:`buy` alone — it is
        the only caller that knows which order the request is about.
        :meth:`express_buy` walks the book across orders and
        :meth:`order_book` renders it, so both pass ``None``, as does
        the money sweep. ``paid`` trades never expire (dispute only).
        """
        cutoff = now - self._pending_ttl
        stale = await self._p2p.list_pending_older_than(
            cutoff, limit=_EXPIRY_SCAN_LIMIT, order_id=order_id
        )
        expired: list[ExpiredTrade] = []
        for trade in stale:
            trade_id = int(trade.id)
            t_order_id = int(trade.order_id)
            seller_id = int(trade.seller_id)
            amount = int(trade.amount_com)

            # #209: the guard lives INSIDE the savepoint on purpose. It
            # flips ``pending → cancelled_timeout`` before the slice is
            # returned, so a rollback that left the flip standing would
            # strand the COM — strictly worse than doing nothing.
            async with self._session.begin_nested() as savepoint:
                if not await self._p2p.expire_pending_guard(trade_id, cutoff):
                    continue  # buyer marked paid (or another sweep won)

                returned_to_order = await self._p2p.return_slice(t_order_id, amount)
                if not returned_to_order:
                    # Order cancelled (escrow already refunded) or vanished
                    # — the slice goes back to the seller's wallet directly.
                    # ``release`` (#1501): an expiry is a refund of the
                    # seller's own escrowed slice.
                    credited = await self._economy.release(seller_id, amount)
                    if credited is None:
                        await savepoint.rollback()
                        log.bind(trade_id=trade_id, seller_id=seller_id, amount=amount).error(
                            "p2p expiry refund credit failed; trade left pending"
                        )
                        continue
                    await self._ledger.record(
                        from_id=None,
                        to_id=seller_id,
                        amount=amount,
                        reason=f"p2p trade #{trade_id} expired (order closed)",
                        type=REFUND_TYPE,
                        date=now,
                    )
            expired.append(
                ExpiredTrade(
                    trade_id=trade_id,
                    order_id=t_order_id,
                    seller_id=seller_id,
                    buyer_id=int(trade.buyer_id),
                    amount_com=amount,
                    returned_to_order=returned_to_order,
                )
            )
        if expired:
            log.bind(count=len(expired)).info("p2p pending trades expired")
        return expired

    async def sweep(self, now: datetime) -> SweepReport:
        """Full expiry pass — the EconomyCleanupSweeper hook.

        #1620: this line said "hourly" until #268 split the sweeper's
        cadences and left the money half — this hook included — running
        once every 60 seconds. It is the docstring somebody reads while
        deciding whether a per-trade cost is affordable here, and the
        answer is 1440 passes a day, not 24.

        ``now`` must be NAIVE UTC (the convention every new-pipeline
        ``created_at`` write uses); the sweeper integration passes
        ``datetime.now(UTC).replace(tzinfo=None)``, NOT its naive-local
        inventory clock.
        """
        return SweepReport(expired=tuple(await self.expire_pending(now=now)))

    # ------------------------------------------------------------------
    # Reads for the UI clusters (P2/P3)
    # ------------------------------------------------------------------

    async def order_book(
        self,
        *,
        currency: str | None = None,
        limit: int = 10,
        offset: int = 0,
        now: datetime,
    ) -> list[P2pSellOrder]:
        """Active orders cheapest-first (+ lazy expiry so freed slices
        show). Thin pass-through kept on the service so handlers never
        touch the repo directly."""
        await self.expire_pending(now=now)
        return await self._p2p.list_active(
            currency=currency.upper() if currency else None,
            limit=limit,
            offset=offset,
        )

    async def get_order(self, order_id: int) -> P2pSellOrder | None:
        return await self._p2p.get_order(order_id)

    async def get_trade(self, trade_id: int) -> P2pTrade | None:
        return await self._p2p.get_trade(trade_id)

    async def my_orders(self, user_id: int, *, limit: int = 15) -> list[P2pSellOrder]:
        return await self._p2p.my_orders(user_id, limit=limit)

    async def count_active_orders(self, user_id: int) -> int:
        """Unbounded count of the seller's live orders (see the repo)."""
        return await self._p2p.count_active_orders(user_id)

    async def my_trades(self, user_id: int, *, limit: int = 20) -> list[P2pTrade]:
        return await self._p2p.my_trades(user_id, limit=limit)

    async def list_disputed(self, *, limit: int = 20) -> list[P2pTrade]:
        """The admin dispute queue, oldest first (#1687 — see the repo)."""
        return await self._p2p.list_disputed(limit=limit)

    async def count_disputed(self) -> int:
        """Backlog size behind :meth:`list_disputed`'s bounded page."""
        return await self._p2p.count_disputed()

    async def seller_stats(self, user_id: int) -> P2pSellerStats:
        """The D3 card counters (✅ сделок / ⚠️ споров / продано)."""
        return await self._p2p.get_stats(user_id)
