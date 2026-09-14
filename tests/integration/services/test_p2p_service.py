"""``P2pService`` integration — the P2P money core, end to end (#64).

Pins every money invariant from DESIGN_P2P.md §2.2 against a real
economy session (real SQLite, real guards):

* escrow-on-create debits the seller and writes the ``p2p_escrow``
  ledger row; the full ledger reconciles (escrow == refund + release)
  over a complete order lifecycle;
* the atomic fill guard stops the oversell race (second taker of the
  same escrow loses on rowcount);
* double-release: a second seller-confirm is rejected, the buyer is
  credited exactly once;
* cancel returns exactly the live ``remaining_com``, once;
* express buy fills cheapest-first, skips the buyer's own orders,
  truncates ``take_com`` like legacy (bot.py:19659);
* D2 expiry returns the slice to the order and reactivates a completed
  order; a slice whose order was cancelled refunds the seller instead;
* all three dispute outcomes (refund-buyer / confirm-seller /
  D1 return-seller) move the money to the right wallet with the right
  ledger type and the right stats side-effects;
* a failed (checked) credit rolls the in-flight transition back —
  the trade/order state is unchanged and no ledger row leaks.

Commits are issued explicitly after each service call to mirror
``EconomyMiddleware`` (commit on handler success), so the
``session.rollback()`` failure paths are exercised against durable
state exactly as in production.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.db.models.p2p import P2pSellerStats, P2pSellOrder, P2pTrade
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.p2p_repo import TRADE_DISPUTED, P2pRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.services.p2p_service import (
    MAX_ACTIVE_ORDERS_PER_SELLER,
    MAX_OPEN_TRADES_PER_BUYER,
    BuyOutcome,
    CancelOrderOutcome,
    ConfirmOutcome,
    CreateOrderOutcome,
    DisputeResolution,
    ExpressBuyOutcome,
    MarkPaidOutcome,
    OpenDisputeOutcome,
    P2pService,
    ResolveOutcome,
)
from telegram_invite_bot.utils.economy import _MAX_AMOUNT

_SELLER = 111
_BUYER = 222
_OTHER = 333
_ADMIN = 999

NOW = datetime(2026, 6, 11, 12, 0, 0)
# Past the default 30-minute pending TTL (D2).
LATER = NOW + timedelta(minutes=31)


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as s:
        yield s
    await engine.dispose()


def _service(session: AsyncSession) -> P2pService:
    return P2pService(
        P2pRepo(session),
        EconomyRepo(session),
        TransactionsRepo(session),
        session,
    )


async def _seed(session: AsyncSession, user_id: int, balance: int) -> None:
    repo = EconomyRepo(session)
    await repo.get_or_create(user_id, now=NOW)
    await repo.set_balance(user_id, balance)
    await session.commit()


async def _balance(session: AsyncSession, user_id: int) -> int:
    wallet = await EconomyRepo(session).get(user_id)
    assert wallet is not None
    return wallet.balance


async def _totals(session: AsyncSession, user_id: int) -> tuple[int, int]:
    """``(total_spent, total_earned)`` straight from the row (#1501)."""
    row = await session.get(EconomyUser, user_id)
    assert row is not None
    return int(row.total_spent), int(row.total_earned)


async def _ledger_sum(session: AsyncSession, type_: str) -> int:
    result = await session.execute(
        select(func.coalesce(func.sum(Transaction.amount), 0)).where(Transaction.type == type_)
    )
    return int(result.scalar_one())


async def _ledger_count(session: AsyncSession, type_: str) -> int:
    result = await session.execute(
        select(func.count()).select_from(Transaction).where(Transaction.type == type_)
    )
    return int(result.scalar_one())


async def _order(session: AsyncSession, order_id: int) -> P2pSellOrder:
    row = await session.get(P2pSellOrder, order_id)
    assert row is not None
    return row


async def _trade(session: AsyncSession, trade_id: int) -> P2pTrade:
    row = await session.get(P2pTrade, trade_id)
    assert row is not None
    return row


async def _create_order(session: AsyncSession, svc: P2pService, *, amount: int = 500) -> int:
    res = await svc.create_sell_order(seller_id=_SELLER, amount_com=amount, currency="RUB", now=NOW)
    assert res.outcome is CreateOrderOutcome.OK
    await session.commit()
    return res.order_id


async def _paid_trade(session: AsyncSession, svc: P2pService, *, amount: int = 200) -> int:
    """Seller order → buyer buys ``amount`` → buyer marks paid."""
    order_id = await _create_order(session, svc)
    buy = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=amount, now=NOW)
    assert buy.outcome is BuyOutcome.OK
    await session.commit()
    paid = await svc.mark_paid(buyer_id=_BUYER, trade_id=buy.trade_id, now=NOW)
    assert paid.outcome is MarkPaidOutcome.OK
    await session.commit()
    return buy.trade_id


# ---------------------------------------------------------------------------
# Order create — escrow-on-create
# ---------------------------------------------------------------------------


async def test_create_debits_seller_and_writes_escrow_ledger(
    session: AsyncSession,
) -> None:
    await _seed(session, _SELLER, 1000)
    svc = _service(session)
    res = await svc.create_sell_order(seller_id=_SELLER, amount_com=500, currency="RUB", now=NOW)
    await session.commit()

    assert res.outcome is CreateOrderOutcome.OK
    assert res.price_per_com == 1.0  # RUB market rate, bot.py:19185
    assert res.total_fiat == 500.0
    assert await _balance(session, _SELLER) == 500
    order = await _order(session, res.order_id)
    assert order.status == "active"
    assert order.amount_com == 500
    assert order.remaining_com == 500
    assert await _ledger_sum(session, "p2p_escrow") == 500


async def test_create_rejects_nonpositive_amount_without_side_effects(
    session: AsyncSession,
) -> None:
    await _seed(session, _SELLER, 1000)
    svc = _service(session)
    for bad in (0, -5):
        res = await svc.create_sell_order(
            seller_id=_SELLER, amount_com=bad, currency="RUB", now=NOW
        )
        assert res.outcome is CreateOrderOutcome.INVALID_AMOUNT
    assert await _balance(session, _SELLER) == 1000
    assert await _ledger_count(session, "p2p_escrow") == 0


async def test_create_rejects_unknown_currency(session: AsyncSession) -> None:
    await _seed(session, _SELLER, 1000)
    svc = _service(session)
    res = await svc.create_sell_order(seller_id=_SELLER, amount_com=100, currency="BTC", now=NOW)
    assert res.outcome is CreateOrderOutcome.INVALID_CURRENCY


async def test_create_rejects_amount_over_balance(session: AsyncSession) -> None:
    await _seed(session, _SELLER, 100)
    svc = _service(session)
    res = await svc.create_sell_order(seller_id=_SELLER, amount_com=101, currency="RUB", now=NOW)
    assert res.outcome is CreateOrderOutcome.INSUFFICIENT_FUNDS
    assert await _balance(session, _SELLER) == 100
    orders = await P2pRepo(session).my_orders(_SELLER)
    assert orders == []


async def test_create_refuses_orders_past_the_seller_cap_without_escrowing(
    session: AsyncSession,
) -> None:
    """#1690: the book-flooding gate answers BEFORE the escrow hold."""
    await _seed(session, _SELLER, 1000)
    svc = _service(session)
    for _ in range(MAX_ACTIVE_ORDERS_PER_SELLER):
        res = await svc.create_sell_order(seller_id=_SELLER, amount_com=10, currency="RUB", now=NOW)
        assert res.outcome is CreateOrderOutcome.OK
        await session.commit()
    held = await _balance(session, _SELLER)

    res = await svc.create_sell_order(seller_id=_SELLER, amount_com=10, currency="RUB", now=NOW)
    await session.commit()

    assert res.outcome is CreateOrderOutcome.TOO_MANY_ORDERS
    # Refused before the hold: no eleventh coin parked, no ledger row.
    assert await _balance(session, _SELLER) == held
    assert await _ledger_count(session, "p2p_escrow") == MAX_ACTIVE_ORDERS_PER_SELLER
    assert await P2pRepo(session).count_active_orders(_SELLER) == (MAX_ACTIVE_ORDERS_PER_SELLER)


async def test_cancelling_an_order_frees_a_seller_slot(session: AsyncSession) -> None:
    """The cap counts LIVE orders, so it cannot strand a real seller."""
    await _seed(session, _SELLER, 1000)
    svc = _service(session)
    first = None
    for _ in range(MAX_ACTIVE_ORDERS_PER_SELLER):
        res = await svc.create_sell_order(seller_id=_SELLER, amount_com=10, currency="RUB", now=NOW)
        assert res.outcome is CreateOrderOutcome.OK
        first = first if first is not None else res.order_id
        await session.commit()
    assert first is not None

    cancelled = await svc.cancel_order(seller_id=_SELLER, order_id=first, now=NOW)
    assert cancelled.outcome is CancelOrderOutcome.OK
    await session.commit()

    res = await svc.create_sell_order(seller_id=_SELLER, amount_com=10, currency="RUB", now=NOW)
    await session.commit()
    assert res.outcome is CreateOrderOutcome.OK


# ---------------------------------------------------------------------------
# Cancel — returns exactly the remaining escrow, once
# ---------------------------------------------------------------------------


async def test_cancel_returns_exactly_remaining(session: AsyncSession) -> None:
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)
    order_id = await _create_order(session, svc, amount=500)
    buy = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=200, now=NOW)
    assert buy.outcome is BuyOutcome.OK
    await session.commit()

    res = await svc.cancel_order(seller_id=_SELLER, order_id=order_id, now=NOW)
    await session.commit()

    assert res.outcome is CancelOrderOutcome.OK
    assert res.returned_com == 300  # 500 escrowed - 200 in the pending trade
    assert await _balance(session, _SELLER) == 500 + 300
    order = await _order(session, order_id)
    assert order.status == "cancelled"
    assert order.remaining_com == 0
    assert await _ledger_sum(session, "p2p_refund") == 300


async def test_cancel_twice_refunds_once(session: AsyncSession) -> None:
    await _seed(session, _SELLER, 1000)
    svc = _service(session)
    order_id = await _create_order(session, svc, amount=500)

    first = await svc.cancel_order(seller_id=_SELLER, order_id=order_id, now=NOW)
    await session.commit()
    second = await svc.cancel_order(seller_id=_SELLER, order_id=order_id, now=NOW)
    await session.commit()

    assert first.outcome is CancelOrderOutcome.OK
    assert second.outcome is CancelOrderOutcome.NOT_ACTIVE
    assert await _balance(session, _SELLER) == 1000
    assert await _ledger_sum(session, "p2p_refund") == 500


async def test_escrow_round_trip_leaves_the_lifetime_counters_alone(
    session: AsyncSession,
) -> None:
    """#1501 — an escrow is a park, not a spend.

    Create-then-cancel moves no money: the seller ends the round trip
    with the balance he started with. If the escrow leg used ``debit``
    and the refund leg used ``credit``, both lifetime counters behind
    the ``/balance`` card would still be ``amount`` higher afterwards,
    and the seller could repeat the pair as often as he liked. Legacy
    wrote the balance column and nothing else across the whole P2P
    region (bot.py:19486 and :19583 are the two statements this test
    covers), so the counters are the invariant, not an accident.

    The release leg is checked in the same test because it is the same
    mistake on the other side: the buyer receives coins that have been
    sitting in escrow since the order was created, which legacy hands
    over with a bare ``balance + amount`` (bot.py:20042).
    """
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)

    assert await _totals(session, _SELLER) == (0, 0)

    order_id = await _create_order(session, svc, amount=500)
    assert await _balance(session, _SELLER) == 500
    assert await _totals(session, _SELLER) == (0, 0)

    res = await svc.cancel_order(seller_id=_SELLER, order_id=order_id, now=NOW)
    await session.commit()
    assert res.outcome is CancelOrderOutcome.OK
    assert await _balance(session, _SELLER) == 1000
    assert await _totals(session, _SELLER) == (0, 0)

    trade_id = await _paid_trade(session, svc, amount=200)
    confirm = await svc.seller_confirm(seller_id=_SELLER, trade_id=trade_id, now=NOW)
    await session.commit()
    assert confirm.outcome is ConfirmOutcome.OK
    assert await _balance(session, _BUYER) == 300
    assert await _totals(session, _BUYER) == (0, 0)


async def test_cancel_foreign_order_rejected(session: AsyncSession) -> None:
    await _seed(session, _SELLER, 1000)
    svc = _service(session)
    order_id = await _create_order(session, svc)
    res = await svc.cancel_order(seller_id=_OTHER, order_id=order_id, now=NOW)
    assert res.outcome is CancelOrderOutcome.NOT_YOURS
    assert (await _order(session, order_id)).status == "active"


# ---------------------------------------------------------------------------
# Buy — gates + the oversell race
# ---------------------------------------------------------------------------


async def test_buy_gates_self_trade_bounds_and_inactive(
    session: AsyncSession,
) -> None:
    await _seed(session, _SELLER, 1000)
    svc = _service(session)
    order_id = await _create_order(session, svc, amount=500)

    self_buy = await svc.buy(buyer_id=_SELLER, order_id=order_id, amount_com=10, now=NOW)
    assert self_buy.outcome is BuyOutcome.SELF_TRADE  # bot.py:19905

    for bad in (0, -1, 501):
        res = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=bad, now=NOW)
        assert res.outcome is BuyOutcome.INVALID_AMOUNT  # bot.py:19902

    missing = await svc.buy(buyer_id=_BUYER, order_id=98765, amount_com=10, now=NOW)
    assert missing.outcome is BuyOutcome.ORDER_NOT_ACTIVE


async def test_buy_full_completes_order_and_carves_trade(
    session: AsyncSession,
) -> None:
    await _seed(session, _SELLER, 1000)
    svc = _service(session)
    order_id = await _create_order(session, svc, amount=500)

    res = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=500, now=NOW)
    await session.commit()

    assert res.outcome is BuyOutcome.OK
    assert res.remaining_com == 0
    assert res.total_fiat == 500.0
    order = await _order(session, order_id)
    assert order.status == "completed"
    assert order.remaining_com == 0
    trade = await _trade(session, res.trade_id)
    assert trade.status == "pending"
    assert trade.amount_com == 500
    assert trade.seller_id == _SELLER
    assert trade.buyer_id == _BUYER
    # No money moved on buy — the escrow merely changed pockets.
    assert await _balance(session, _SELLER) == 500


async def test_oversell_race_second_fill_guard_fails(session: AsyncSession) -> None:
    """Two buyers race the same escrow: the guard serialises them.

    Models the legacy fragility (#6, bot.py:19632-19680 read-then-write)
    at the exact contention point: both contenders have already passed
    the Python-side read (both believe ``remaining_com == 500``) and
    race the atomic fill UPDATE. The first wins; the second's guard
    matches no row (``remaining_com >= 500`` is now false) and MUST
    return False — no oversell, no negative escrow.
    """
    await _seed(session, _SELLER, 1000)
    svc = _service(session)
    order_id = await _create_order(session, svc, amount=500)
    repo = P2pRepo(session)

    assert await repo.fill(order_id, 500) is True
    assert await repo.fill(order_id, 500) is False  # the racing loser
    await session.rollback()

    # And end-to-end through the service: after a full buy the re-fetch
    # (or, if the read won the race, the guard) rejects the second buyer.
    first = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=500, now=NOW)
    await session.commit()
    second = await svc.buy(buyer_id=_OTHER, order_id=order_id, amount_com=500, now=NOW)
    assert first.outcome is BuyOutcome.OK
    assert second.outcome is BuyOutcome.ORDER_NOT_ACTIVE
    order = await _order(session, order_id)
    assert order.remaining_com == 0  # never negative


def _record_lock_order(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    """Note the order of ``lock_writer`` vs the ceiling count (#1503)."""
    lock = P2pRepo.lock_writer
    count = P2pRepo.count_open_as_buyer

    async def rec_lock(self: P2pRepo) -> None:
        calls.append("lock_writer")
        await lock(self)

    async def rec_count(self: P2pRepo, buyer_id: int) -> int:
        calls.append("count_open_as_buyer")
        return await count(self, buyer_id)

    monkeypatch.setattr(P2pRepo, "lock_writer", rec_lock)
    monkeypatch.setattr(P2pRepo, "count_open_as_buyer", rec_count)


async def test_buy_takes_the_writer_lock_before_the_ceiling_count(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1503 — the open-trade cap is decided under the writer lock.

    A structural guard, not a timing one. The race it closes needs the
    ``db/engines.py`` BEGIN-IMMEDIATE hook, which this suite's bare
    ``create_async_engine`` never installs (the same reason #776 has its
    own file under ``tests/regression/``); a behavioural test here would
    pass with or without the fix. What CAN be pinned here is the thing a
    refactor would break: that the write which takes the lock is issued
    before the reads that decide the outcome.
    """
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)
    order_id = await _create_order(session, svc, amount=500)

    calls: list[str] = []
    _record_lock_order(monkeypatch, calls)
    res = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=200, now=NOW)
    await session.commit()

    assert res.outcome is BuyOutcome.OK
    assert calls[0] == "lock_writer"
    assert "count_open_as_buyer" in calls


async def test_express_takes_the_writer_lock_before_sizing_the_loop(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1503 for express, where the count sizes the fill loop itself."""
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)
    await _create_order(session, svc, amount=500)

    calls: list[str] = []
    _record_lock_order(monkeypatch, calls)
    res = await svc.express_buy(buyer_id=_BUYER, currency="RUB", max_fiat=50.0, now=NOW)
    await session.commit()

    assert res.outcome is ExpressBuyOutcome.OK
    assert calls[0] == "lock_writer"
    assert "count_open_as_buyer" in calls


# ---------------------------------------------------------------------------
# Express buy — cheapest first, self-skip, legacy truncation
# ---------------------------------------------------------------------------


async def test_express_fills_cheapest_first_and_skips_self(
    session: AsyncSession,
) -> None:
    repo = P2pRepo(session)
    # Buyer's OWN order is the cheapest — must be skipped (bot.py:19652).
    own = await repo.create_order(
        user_id=_BUYER,
        amount_com=100,
        price_per_com=0.5,
        fiat_currency="RUB",
        payment_methods=None,
        min_amount=None,
        max_amount=None,
        now=NOW,
    )
    cheap = await repo.create_order(
        user_id=_SELLER,
        amount_com=100,
        price_per_com=1.0,
        fiat_currency="RUB",
        payment_methods=None,
        min_amount=None,
        max_amount=None,
        now=NOW,
    )
    pricey = await repo.create_order(
        user_id=_OTHER,
        amount_com=100,
        price_per_com=2.0,
        fiat_currency="RUB",
        payment_methods=None,
        min_amount=None,
        max_amount=None,
        now=NOW,
    )
    # Wrong currency — never touched.
    usd = await repo.create_order(
        user_id=_OTHER,
        amount_com=100,
        price_per_com=0.011,
        fiat_currency="USD",
        payment_methods=None,
        min_amount=None,
        max_amount=None,
        now=NOW,
    )
    await session.commit()
    svc = _service(session)

    res = await svc.express_buy(buyer_id=_BUYER, currency="RUB", max_fiat=150.0, now=NOW)
    await session.commit()

    assert res.outcome is ExpressBuyOutcome.OK
    # 150 RUB: cheap drains fully (100 COM @ 1.0 = 100 RUB), then 50 RUB
    # buys int(50 / 2.0) = 25 COM from pricey — legacy truncation,
    # bot.py:19659.
    assert [(f.order_id, f.amount_com) for f in res.fills] == [
        (cheap.id, 100),
        (pricey.id, 25),
    ]
    assert res.total_com == 125
    assert res.total_fiat == pytest.approx(150.0)
    assert (await _order(session, cheap.id)).status == "completed"
    pricey_row = await _order(session, pricey.id)
    assert pricey_row.status == "active"
    assert pricey_row.remaining_com == 75
    assert (await _order(session, own.id)).remaining_com == 100  # untouched
    assert (await _order(session, usd.id)).remaining_com == 100
    for fill in res.fills:
        assert (await _trade(session, fill.trade_id)).status == "pending"


async def test_express_no_fillable_orders(session: AsyncSession) -> None:
    repo = P2pRepo(session)
    await repo.create_order(
        user_id=_BUYER,
        amount_com=100,
        price_per_com=1.0,
        fiat_currency="RUB",
        payment_methods=None,
        min_amount=None,
        max_amount=None,
        now=NOW,
    )
    await session.commit()
    svc = _service(session)

    only_own = await svc.express_buy(buyer_id=_BUYER, currency="RUB", max_fiat=100.0, now=NOW)
    assert only_own.outcome is ExpressBuyOutcome.NO_ORDERS

    bad_amount = await svc.express_buy(buyer_id=_BUYER, currency="RUB", max_fiat=0.0, now=NOW)
    assert bad_amount.outcome is ExpressBuyOutcome.INVALID_AMOUNT

    bad_currency = await svc.express_buy(buyer_id=_BUYER, currency="BTC", max_fiat=100.0, now=NOW)
    assert bad_currency.outcome is ExpressBuyOutcome.INVALID_CURRENCY


@pytest.mark.parametrize(
    "budget",
    [float("nan"), float("inf"), float("-inf"), float("1e400")],
    ids=["nan", "inf", "-inf", "1e400"],
)
async def test_express_refuses_a_non_finite_budget(session: AsyncSession, budget: float) -> None:
    """NaN/Infinity are refused as a budget, not walked into the book.

    ``max_fiat <= 0`` is not a budget check on a float. NaN answers
    False to every comparison, so it passed the gate, made
    ``take_fiat = min(nan, …)`` NaN, sailed past the ``< 1e-6`` dust
    check and raised ``ValueError: cannot convert float NaN to integer``
    inside ``int(take_fiat / price)`` — mid-loop, after the lazy expiry
    sweep had already run. Infinity passed too and stopped bounding the
    fill: ``min(inf, fiat_available)`` is the seller's whole slice, so
    the "budget" drained the book (``"1e400"`` is the typo that gets a
    user there without typing the word).

    The seeded order exists on purpose — a refusal that only holds on
    an empty book is not a refusal.
    """
    repo = P2pRepo(session)
    await repo.create_order(
        user_id=_SELLER,
        amount_com=100,
        price_per_com=1.0,
        fiat_currency="RUB",
        payment_methods=None,
        min_amount=None,
        max_amount=None,
        now=NOW,
    )
    await session.commit()
    svc = _service(session)

    result = await svc.express_buy(buyer_id=_BUYER, currency="RUB", max_fiat=budget, now=NOW)
    assert result.outcome is ExpressBuyOutcome.INVALID_AMOUNT
    assert result.fills == ()
    # Nothing was reserved against the live order.
    order = (await repo.list_active(currency="RUB", limit=10))[0]
    assert int(order.remaining_com) == 100


# ---------------------------------------------------------------------------
# D4 — the per-buyer open-trade ceiling
# ---------------------------------------------------------------------------


async def _book(session: AsyncSession, count: int, *, amount: int = 100) -> list[int]:
    """``count`` identically-priced active orders from distinct sellers."""
    repo = P2pRepo(session)
    ids: list[int] = []
    for i in range(count):
        order = await repo.create_order(
            user_id=_SELLER + 1000 + i,
            amount_com=amount,
            price_per_com=1.0,
            fiat_currency="RUB",
            payment_methods=None,
            min_amount=None,
            max_amount=None,
            now=NOW,
        )
        ids.append(int(order.id))
    await session.commit()
    return ids


async def _fill_buyer_slots(
    session: AsyncSession, svc: P2pService, count: int, *, at: datetime = NOW
) -> list[int]:
    """Open ``count`` trades for ``_BUYER``, alternating pending/paid.

    Both statuses hold the seller's COM, so the ceiling must count both;
    alternating them is what makes that assertion non-vacuous.
    """
    trade_ids: list[int] = []
    for i, order_id in enumerate(await _book(session, count)):
        res = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=100, now=at)
        assert res.outcome is BuyOutcome.OK, res.outcome
        if i % 2 == 1:
            assert (
                await svc.mark_paid(buyer_id=_BUYER, trade_id=res.trade_id, now=at)
            ).outcome is MarkPaidOutcome.OK
        trade_ids.append(int(res.trade_id))
    await session.commit()
    return trade_ids


async def test_buy_refuses_past_the_open_trade_ceiling(session: AsyncSession) -> None:
    """A buyer at the D4 ceiling cannot open trade number ``MAX + 1``.

    Opening a trade is free for the buyer and escrows the seller's COM,
    so without this ceiling one account could hold the entire book.
    """
    svc = _service(session)
    await _fill_buyer_slots(session, svc, MAX_OPEN_TRADES_PER_BUYER)
    target = (await _book(session, 1))[0]

    res = await svc.buy(buyer_id=_BUYER, order_id=target, amount_com=100, now=NOW)
    await session.commit()

    assert res.outcome is BuyOutcome.TOO_MANY_OPEN
    # The refusal happens BEFORE the fill guard: the seller's escrow is
    # untouched and the order is still on the book for someone else.
    order = await _order(session, target)
    assert order.remaining_com == 100
    assert order.status == "active"
    assert await P2pRepo(session).count_open_as_buyer(_BUYER) == (MAX_OPEN_TRADES_PER_BUYER)


async def test_express_refuses_when_the_buyer_has_no_free_slot(
    session: AsyncSession,
) -> None:
    """Express with a full ledger takes nothing — not even the first order."""
    svc = _service(session)
    await _fill_buyer_slots(session, svc, MAX_OPEN_TRADES_PER_BUYER)
    fresh = await _book(session, 3)

    res = await svc.express_buy(buyer_id=_BUYER, currency="RUB", max_fiat=100_000.0, now=NOW)
    await session.commit()

    assert res.outcome is ExpressBuyOutcome.TOO_MANY_OPEN
    assert res.fills == ()
    for order_id in fresh:
        assert (await _order(session, order_id)).remaining_com == 100


async def test_express_stops_at_the_ceiling_instead_of_draining_the_book(
    session: AsyncSession,
) -> None:
    """With one slot left, express fills once and leaves the rest.

    This is the attack in miniature: the budget covers the whole book,
    and the only thing stopping the fill is the ceiling. A partial fill
    is still a success — the buyer is told what they got.
    """
    svc = _service(session)
    await _fill_buyer_slots(session, svc, MAX_OPEN_TRADES_PER_BUYER - 1)
    fresh = await _book(session, 4)

    res = await svc.express_buy(buyer_id=_BUYER, currency="RUB", max_fiat=100_000.0, now=NOW)
    await session.commit()

    assert res.outcome is ExpressBuyOutcome.OK
    assert len(res.fills) == 1
    assert res.fills[0].order_id == fresh[0]
    # Budget left over, book left over — the ceiling is what stopped it.
    for order_id in fresh[1:]:
        assert (await _order(session, order_id)).remaining_com == 100
    assert await P2pRepo(session).count_open_as_buyer(_BUYER) == (MAX_OPEN_TRADES_PER_BUYER)


async def test_a_stale_pending_frees_a_slot_instead_of_blocking_the_buyer(
    session: AsyncSession,
) -> None:
    """Trades past the D2 TTL must not count against the ceiling.

    They hold nothing — the sweep just hasn't run yet. Refusing a buyer
    over them would be a cap that punishes people for the scheduler's
    timing, so the check re-counts after a full expiry pass.
    """
    svc = _service(session)
    await _fill_buyer_slots(session, svc, MAX_OPEN_TRADES_PER_BUYER)
    target = (await _book(session, 1))[0]

    # Every pending trade is now stale; the paid ones never expire, so
    # this only works because pending is the majority at MAX == 3.
    res = await svc.buy(buyer_id=_BUYER, order_id=target, amount_com=100, now=LATER)
    await session.commit()

    assert res.outcome is BuyOutcome.OK
    assert (await _order(session, target)).remaining_com == 0


# ---------------------------------------------------------------------------
# Lifecycle: paid → confirmed (+ double-release)
# ---------------------------------------------------------------------------


async def test_mark_paid_guards(session: AsyncSession) -> None:
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)
    order_id = await _create_order(session, svc)
    buy = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=100, now=NOW)
    await session.commit()

    stranger = await svc.mark_paid(buyer_id=_OTHER, trade_id=buy.trade_id, now=NOW)
    assert stranger.outcome is MarkPaidOutcome.NOT_FOUND

    ok = await svc.mark_paid(buyer_id=_BUYER, trade_id=buy.trade_id, now=NOW)
    await session.commit()
    assert ok.outcome is MarkPaidOutcome.OK
    assert ok.seller_id == _SELLER
    assert (await _trade(session, buy.trade_id)).status == "paid"

    again = await svc.mark_paid(buyer_id=_BUYER, trade_id=buy.trade_id, now=NOW)
    assert again.outcome is MarkPaidOutcome.NOT_PENDING


async def test_confirm_releases_to_buyer_and_bumps_stats(
    session: AsyncSession,
) -> None:
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)
    trade_id = await _paid_trade(session, svc, amount=200)

    res = await svc.seller_confirm(seller_id=_SELLER, trade_id=trade_id, now=NOW)
    await session.commit()

    assert res.outcome is ConfirmOutcome.OK
    assert res.buyer_id == _BUYER
    assert res.amount_com == 200
    assert await _balance(session, _BUYER) == 300
    trade = await _trade(session, trade_id)
    assert trade.status == "confirmed"
    assert trade.confirmed_at is not None
    assert await _ledger_sum(session, "p2p_release") == 200
    stats = await session.get(P2pSellerStats, _SELLER)
    assert stats is not None
    assert stats.successful_trades == 1
    assert stats.total_sold_com == 200  # bot.py:20042-20047
    assert stats.dispute_count == 0


async def test_double_release_second_confirm_rejected(session: AsyncSession) -> None:
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)
    trade_id = await _paid_trade(session, svc, amount=200)

    first = await svc.seller_confirm(seller_id=_SELLER, trade_id=trade_id, now=NOW)
    await session.commit()
    second = await svc.seller_confirm(seller_id=_SELLER, trade_id=trade_id, now=NOW)
    await session.commit()

    assert first.outcome is ConfirmOutcome.OK
    assert second.outcome is ConfirmOutcome.NOT_PAID
    assert await _balance(session, _BUYER) == 300  # credited exactly once
    assert await _ledger_count(session, "p2p_release") == 1
    stats = await session.get(P2pSellerStats, _SELLER)
    assert stats is not None
    assert stats.successful_trades == 1


async def test_confirm_before_paid_and_by_stranger_rejected(
    session: AsyncSession,
) -> None:
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)
    order_id = await _create_order(session, svc)
    buy = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=100, now=NOW)
    await session.commit()

    early = await svc.seller_confirm(seller_id=_SELLER, trade_id=buy.trade_id, now=NOW)
    assert early.outcome is ConfirmOutcome.NOT_PAID  # bot.py:20039-20040

    stranger = await svc.seller_confirm(seller_id=_OTHER, trade_id=buy.trade_id, now=NOW)
    assert stranger.outcome is ConfirmOutcome.NOT_FOUND


async def test_credit_failure_rolls_back_confirm_transition(
    session: AsyncSession,
) -> None:
    """Checked-credit posture: the buyer's wallet sits at the balance cap,
    so ``credit`` returns ``None`` — the confirm MUST roll back: trade
    stays ``paid``, no release ledger row, no stats bump, escrow intact.
    """
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, _MAX_AMOUNT)  # any credit overflows the cap
    svc = _service(session)
    trade_id = await _paid_trade(session, svc, amount=200)

    res = await svc.seller_confirm(seller_id=_SELLER, trade_id=trade_id, now=NOW)
    await session.commit()

    assert res.outcome is ConfirmOutcome.CREDIT_FAILED
    assert (await _trade(session, trade_id)).status == "paid"  # rolled back
    assert await _balance(session, _BUYER) == _MAX_AMOUNT
    assert await _ledger_count(session, "p2p_release") == 0
    assert await session.get(P2pSellerStats, _SELLER) is None


# ---------------------------------------------------------------------------
# Disputes
# ---------------------------------------------------------------------------


async def test_open_dispute_gates(session: AsyncSession) -> None:
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)
    trade_id = await _paid_trade(session, svc)

    stranger = await svc.open_dispute(user_id=_OTHER, trade_id=trade_id)
    assert stranger.outcome is OpenDisputeOutcome.NOT_PARTICIPANT

    missing = await svc.open_dispute(user_id=_BUYER, trade_id=98765)
    assert missing.outcome is OpenDisputeOutcome.NOT_FOUND

    ok = await svc.open_dispute(user_id=_BUYER, trade_id=trade_id)
    await session.commit()
    assert ok.outcome is OpenDisputeOutcome.OK
    assert (await _trade(session, trade_id)).status == "disputed"

    again = await svc.open_dispute(user_id=_SELLER, trade_id=trade_id)
    assert again.outcome is OpenDisputeOutcome.NOT_DISPUTABLE


async def _disputed_trade(session: AsyncSession, svc: P2pService) -> int:
    trade_id = await _paid_trade(session, svc, amount=200)
    res = await svc.open_dispute(user_id=_BUYER, trade_id=trade_id)
    assert res.outcome is OpenDisputeOutcome.OK
    await session.commit()
    return trade_id


async def test_dispute_refund_buyer_money_correct(session: AsyncSession) -> None:
    """bot.py:20237-20247: buyer credited, seller dispute_count += 1."""
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)
    trade_id = await _disputed_trade(session, svc)

    res = await svc.resolve_dispute(
        admin_id=_ADMIN,
        trade_id=trade_id,
        resolution=DisputeResolution.REFUND_BUYER,
        now=NOW,
    )
    await session.commit()

    assert res.outcome is ResolveOutcome.OK
    assert await _balance(session, _BUYER) == 300
    assert await _balance(session, _SELLER) == 500  # escrow stays spent
    trade = await _trade(session, trade_id)
    assert trade.status == "dispute_refund_buyer"
    assert trade.resolved_by == _ADMIN
    assert trade.resolved_at is not None
    assert await _ledger_sum(session, "p2p_release") == 200
    stats = await session.get(P2pSellerStats, _SELLER)
    assert stats is not None
    assert stats.dispute_count == 1
    assert stats.successful_trades == 0


async def test_dispute_confirm_seller_money_correct(session: AsyncSession) -> None:
    """bot.py:20288-20296: buyer credited, seller gets success stats."""
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)
    trade_id = await _disputed_trade(session, svc)

    res = await svc.resolve_dispute(
        admin_id=_ADMIN,
        trade_id=trade_id,
        resolution=DisputeResolution.CONFIRM_SELLER,
        now=NOW,
    )
    await session.commit()

    assert res.outcome is ResolveOutcome.OK
    assert await _balance(session, _BUYER) == 300
    trade = await _trade(session, trade_id)
    assert trade.status == "confirmed"
    assert trade.confirmed_at is not None
    assert trade.resolved_by == _ADMIN
    assert await _ledger_sum(session, "p2p_release") == 200
    stats = await session.get(P2pSellerStats, _SELLER)
    assert stats is not None
    assert stats.successful_trades == 1
    assert stats.total_sold_com == 200
    assert stats.dispute_count == 0


async def test_dispute_return_seller_money_correct_d1(session: AsyncSession) -> None:
    """Deviation D1: SELLER credited (p2p_refund), no stats change."""
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)
    trade_id = await _disputed_trade(session, svc)

    res = await svc.resolve_dispute(
        admin_id=_ADMIN,
        trade_id=trade_id,
        resolution=DisputeResolution.RETURN_SELLER,
        now=NOW,
    )
    await session.commit()

    assert res.outcome is ResolveOutcome.OK
    assert await _balance(session, _BUYER) == 100  # buyer untouched
    assert await _balance(session, _SELLER) == 700  # 500 post-escrow + 200 back
    trade = await _trade(session, trade_id)
    assert trade.status == "dispute_returned_seller"
    assert trade.resolved_by == _ADMIN
    assert await _ledger_sum(session, "p2p_refund") == 200
    assert await _ledger_count(session, "p2p_release") == 0
    assert await session.get(P2pSellerStats, _SELLER) is None  # no stats change


async def test_resolve_twice_second_rejected(session: AsyncSession) -> None:
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)
    trade_id = await _disputed_trade(session, svc)

    first = await svc.resolve_dispute(
        admin_id=_ADMIN,
        trade_id=trade_id,
        resolution=DisputeResolution.REFUND_BUYER,
        now=NOW,
    )
    await session.commit()
    second = await svc.resolve_dispute(
        admin_id=_ADMIN,
        trade_id=trade_id,
        resolution=DisputeResolution.CONFIRM_SELLER,
        now=NOW,
    )
    await session.commit()

    assert first.outcome is ResolveOutcome.OK
    assert second.outcome is ResolveOutcome.NOT_DISPUTED
    assert await _balance(session, _BUYER) == 300  # paid exactly once


async def test_resolve_credit_failure_rolls_back_and_stays_disputed(
    session: AsyncSession,
) -> None:
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, _MAX_AMOUNT)
    svc = _service(session)
    trade_id = await _disputed_trade(session, svc)

    res = await svc.resolve_dispute(
        admin_id=_ADMIN,
        trade_id=trade_id,
        resolution=DisputeResolution.REFUND_BUYER,
        now=NOW,
    )
    await session.commit()

    assert res.outcome is ResolveOutcome.CREDIT_FAILED
    assert (await _trade(session, trade_id)).status == "disputed"  # retryable
    assert await _ledger_count(session, "p2p_release") == 0
    assert await session.get(P2pSellerStats, _SELLER) is None


# ---------------------------------------------------------------------------
# D2 expiry
# ---------------------------------------------------------------------------


async def test_expiry_returns_slice_and_reactivates_completed_order(
    session: AsyncSession,
) -> None:
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)
    order_id = await _create_order(session, svc, amount=500)
    buy = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=500, now=NOW)
    assert buy.outcome is BuyOutcome.OK
    await session.commit()
    assert (await _order(session, order_id)).status == "completed"

    report = await svc.sweep(LATER)
    await session.commit()

    assert report.count == 1
    expired = report.expired[0]
    assert expired.trade_id == buy.trade_id
    assert expired.returned_to_order is True
    assert (await _trade(session, buy.trade_id)).status == "cancelled_timeout"
    order = await _order(session, order_id)
    assert order.status == "active"  # completed → active again
    assert order.remaining_com == 500  # the full slice came back
    # No wallet movement: the COM went back into the order's escrow.
    assert await _balance(session, _SELLER) == 500
    assert await _ledger_count(session, "p2p_refund") == 0


async def test_expiry_skips_fresh_and_paid_trades(session: AsyncSession) -> None:
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)
    order_id = await _create_order(session, svc, amount=500)
    fresh = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=100, now=NOW)
    paid = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=100, now=NOW)
    await session.commit()
    assert (
        await svc.mark_paid(buyer_id=_BUYER, trade_id=paid.trade_id, now=NOW)
    ).outcome is MarkPaidOutcome.OK
    await session.commit()

    # Sweep at NOW: nothing is older than the TTL.
    report_now = await svc.sweep(NOW)
    assert report_now.count == 0

    # Sweep past the TTL: only the still-pending trade expires; the
    # ``paid`` one never auto-expires (D2 — dispute only).
    report_later = await svc.sweep(LATER)
    await session.commit()
    assert [e.trade_id for e in report_later.expired] == [fresh.trade_id]
    assert (await _trade(session, paid.trade_id)).status == "paid"


async def test_expiry_is_capped_per_pass(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1619: one D2 pass cancels at most ``_EXPIRY_SCAN_LIMIT`` trades.

    The constant is patched down rather than seeding hundreds of
    trades: what is under test is that the cap is APPLIED, not its
    value. The residue is deferred, never dropped — its escrowed COM
    comes back on the next pass, in ``id ASC`` order.
    """
    import telegram_invite_bot.services.p2p_service as mod

    monkeypatch.setattr(mod, "_EXPIRY_SCAN_LIMIT", 2)
    await _seed(session, _SELLER, 1000)
    for buyer in (_BUYER, _OTHER, _ADMIN):
        await _seed(session, buyer, 100)
    svc = _service(session)
    order_id = await _create_order(session, svc, amount=300)
    trades = []
    for buyer in (_BUYER, _OTHER, _ADMIN):
        buy = await svc.buy(buyer_id=buyer, order_id=order_id, amount_com=100, now=NOW)
        assert buy.outcome is BuyOutcome.OK
        trades.append(buy.trade_id)
    await session.commit()
    assert (await _order(session, order_id)).remaining_com == 0

    first = await svc.sweep(LATER)
    await session.commit()
    assert [e.trade_id for e in first.expired] == trades[:2]
    assert (await _order(session, order_id)).remaining_com == 200
    assert (await _trade(session, trades[2])).status == "pending"

    second = await svc.sweep(LATER)
    await session.commit()
    assert [e.trade_id for e in second.expired] == trades[2:]
    assert (await _order(session, order_id)).remaining_com == 300


async def test_expiry_lazy_on_buy_frees_the_slice(session: AsyncSession) -> None:
    """A buyer hitting a fully-pending-locked order after the TTL gets
    the freed escrow — the lazy on-access path of D2."""
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    await _seed(session, _OTHER, 100)
    svc = _service(session)
    order_id = await _create_order(session, svc, amount=500)
    stale = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=500, now=NOW)
    await session.commit()

    res = await svc.buy(buyer_id=_OTHER, order_id=order_id, amount_com=500, now=LATER)
    await session.commit()

    assert res.outcome is BuyOutcome.OK  # the expired slice was reusable
    assert (await _trade(session, stale.trade_id)).status == "cancelled_timeout"
    assert (await _trade(session, res.trade_id)).status == "pending"


async def test_expiry_on_cancelled_order_refunds_seller_wallet(
    session: AsyncSession,
) -> None:
    """If the order was cancelled while the trade hung pending, its
    escrow is gone — the expired slice goes back to the seller's wallet
    (checked credit + p2p_refund ledger row), never stranded."""
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)
    order_id = await _create_order(session, svc, amount=500)
    buy = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=200, now=NOW)
    await session.commit()
    cancel = await svc.cancel_order(seller_id=_SELLER, order_id=order_id, now=NOW)
    assert cancel.outcome is CancelOrderOutcome.OK
    assert cancel.returned_com == 300
    await session.commit()

    report = await svc.sweep(LATER)
    await session.commit()

    assert report.count == 1
    assert report.expired[0].returned_to_order is False
    assert (await _trade(session, buy.trade_id)).status == "cancelled_timeout"
    assert (await _order(session, order_id)).status == "cancelled"  # stays cancelled
    # Full escrow is home: 500 debited, 300 on cancel + 200 on expiry.
    assert await _balance(session, _SELLER) == 1000
    assert await _ledger_sum(session, "p2p_refund") == 500


async def test_a_failed_expiry_refund_does_not_discard_the_rest_of_the_pass(
    session: AsyncSession,
) -> None:
    """#209: one unrefundable slice must not sink the whole sweep.

    Two stale trades, both on cancelled orders (so both refund to a
    wallet rather than back into escrow). The FIRST seller's wallet is
    pegged at the ceiling, so ``EconomyRepo.credit`` returns ``None``.

    Before #209 that path called ``self._session.rollback()`` and
    returned ``[]``. The session is SHARED — the sweeper hands the same
    one to the PvP/inventory/game-plays steps, and the lazy on-access
    callers hand over the HANDLER's session — so the rollback threw
    away work that had nothing to do with P2P, and the second trade was
    never even looked at. ``pending_marker`` below is the stand-in for
    that unrelated work.
    """
    await _seed(session, _SELLER, 1000)
    await _seed(session, _OTHER, 1000)
    await _seed(session, _BUYER, 100)
    await _seed(session, _ADMIN, 0)
    svc = _service(session)

    # Order A (seller _SELLER) — its trade is the poison pill, and it is
    # created first so it leads the ``id ASC`` scan.
    order_a = await _create_order(session, svc, amount=500)
    trade_a = await svc.buy(buyer_id=_BUYER, order_id=order_a, amount_com=200, now=NOW)
    await session.commit()
    assert (
        await svc.cancel_order(seller_id=_SELLER, order_id=order_a, now=NOW)
    ).outcome is CancelOrderOutcome.OK
    await session.commit()

    # Order B (seller _OTHER) — the healthy one behind it.
    created_b = await svc.create_sell_order(
        seller_id=_OTHER, amount_com=500, currency="RUB", now=NOW
    )
    assert created_b.outcome is CreateOrderOutcome.OK
    await session.commit()
    trade_b = await svc.buy(buyer_id=_BUYER, order_id=created_b.order_id, amount_com=200, now=NOW)
    await session.commit()
    assert (
        await svc.cancel_order(seller_id=_OTHER, order_id=created_b.order_id, now=NOW)
    ).outcome is CancelOrderOutcome.OK
    await session.commit()

    # Peg seller A at the ceiling: ``credit`` guards ``balance + amount
    # <= _MAX_AMOUNT`` in the UPDATE's WHERE, so it returns ``None``.
    await _seed(session, _SELLER, _MAX_AMOUNT)

    # Unrelated work on the SHARED session, deliberately uncommitted.
    await EconomyRepo(session).set_balance(_ADMIN, 777)

    report = await svc.sweep(LATER)
    await session.commit()

    assert [e.trade_id for e in report.expired] == [trade_b.trade_id]
    # The poison pill is untouched and retryable next sweep — NOT
    # half-closed with its slice stranded.
    assert (await _trade(session, trade_a.trade_id)).status == "pending"
    assert await _balance(session, _SELLER) == _MAX_AMOUNT
    # The healthy trade behind it still got its refund.
    assert (await _trade(session, trade_b.trade_id)).status == "cancelled_timeout"
    assert await _balance(session, _OTHER) == 1000
    # And the caller's own work survived the failure.
    assert await _balance(session, _ADMIN) == 777


# ---------------------------------------------------------------------------
# Ledger reconciliation — the §2.2.1 invariant end to end
# ---------------------------------------------------------------------------


async def test_full_lifecycle_ledger_reconciles(session: AsyncSession) -> None:
    """escrow == release + refund over a mixed lifecycle, and total
    wallet value is conserved (COM only moved seller → buyer)."""
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, 100)
    svc = _service(session)

    order_id = await _create_order(session, svc, amount=500)  # escrow 500
    buy = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=200, now=NOW)
    await session.commit()
    assert (
        await svc.mark_paid(buyer_id=_BUYER, trade_id=buy.trade_id, now=NOW)
    ).outcome is MarkPaidOutcome.OK
    await session.commit()
    assert (
        await svc.seller_confirm(seller_id=_SELLER, trade_id=buy.trade_id, now=NOW)
    ).outcome is ConfirmOutcome.OK  # release 200
    await session.commit()
    assert (
        await svc.cancel_order(seller_id=_SELLER, order_id=order_id, now=NOW)
    ).outcome is CancelOrderOutcome.OK  # refund 300
    await session.commit()

    escrow = await _ledger_sum(session, "p2p_escrow")
    release = await _ledger_sum(session, "p2p_release")
    refund = await _ledger_sum(session, "p2p_refund")
    assert escrow == 500
    assert release == 200
    assert refund == 300
    assert escrow == release + refund  # the escrow fully accounted for

    seller_end = await _balance(session, _SELLER)
    buyer_end = await _balance(session, _BUYER)
    assert seller_end == 800  # 1000 - 500 + 300
    assert buyer_end == 300  # 100 + 200
    assert seller_end + buyer_end == 1000 + 100  # conservation


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_order_book_sorted_and_stats_default(session: AsyncSession) -> None:
    repo = P2pRepo(session)
    o_high = await repo.create_order(
        user_id=_SELLER,
        amount_com=10,
        price_per_com=2.0,
        fiat_currency="RUB",
        payment_methods=None,
        min_amount=None,
        max_amount=None,
        now=NOW,
    )
    o_low = await repo.create_order(
        user_id=_OTHER,
        amount_com=10,
        price_per_com=1.0,
        fiat_currency="RUB",
        payment_methods=None,
        min_amount=None,
        max_amount=None,
        now=NOW,
    )
    await session.commit()
    svc = _service(session)

    book = await svc.order_book(currency="RUB", now=NOW)
    assert [o.id for o in book] == [o_low.id, o_high.id]  # price ASC

    stats = await svc.seller_stats(_SELLER)
    assert (stats.successful_trades, stats.total_sold_com, stats.dispute_count) == (
        0,
        0,
        0,
    )
    # The transient default is NOT persisted.
    assert await session.get(P2pSellerStats, _SELLER) is None


# ---------------------------------------------------------------------------
# Per-trade bounds (``min_amount`` / ``max_amount``).
#
# Legacy stored them, printed them in the order book, and then let any
# amount through — the buyer read a rule the bot never applied. These pin
# the rule as enforced, in both units (the seller sets fiat, the buyer
# types COM), plus the clamp that keeps a partly-filled order sellable.
# ---------------------------------------------------------------------------


async def _bounded_order(
    session: AsyncSession,
    svc: P2pService,
    *,
    amount: int = 500,
    currency: str = "RUB",
    min_amount: int | None = None,
    max_amount: int | None = None,
) -> int:
    res = await svc.create_sell_order(
        seller_id=_SELLER,
        amount_com=amount,
        currency=currency,
        now=NOW,
        min_amount=min_amount,
        max_amount=max_amount,
    )
    assert res.outcome is CreateOrderOutcome.OK
    await session.commit()
    return res.order_id


async def test_buy_below_the_sellers_minimum_is_rejected(session: AsyncSession) -> None:
    await _seed(session, _SELLER, 1_000)
    svc = _service(session)
    order_id = await _bounded_order(session, svc, min_amount=100)  # RUB rate 1.0

    res = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=50, now=NOW)
    await session.commit()

    assert res.outcome is BuyOutcome.BELOW_MIN
    assert res.limit_fiat == 100.0
    assert res.limit_com == 100  # the amount the buyer should type instead
    # #695: the rejection carries the currency. The handler renders it as
    # ``cur=result.fiat_currency`` in ``handle_buy_amount``, so leaving
    # the dataclass default in place printed "минимум 100.00" with a
    # hole where the currency belongs.
    assert res.fiat_currency == "RUB"
    # Nothing moved: no trade, escrow untouched.
    assert (await _order(session, order_id)).remaining_com == 500
    assert (await session.execute(select(func.count()).select_from(P2pTrade))).scalar_one() == 0


async def test_buy_above_the_sellers_maximum_is_rejected(session: AsyncSession) -> None:
    await _seed(session, _SELLER, 1_000)
    svc = _service(session)
    order_id = await _bounded_order(session, svc, max_amount=200)

    res = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=300, now=NOW)
    await session.commit()

    assert res.outcome is BuyOutcome.ABOVE_MAX
    assert res.limit_fiat == 200.0
    assert res.limit_com == 200
    assert res.fiat_currency == "RUB"  # #695
    assert (await _order(session, order_id)).remaining_com == 500

    # Exactly the bound is allowed — the rejection is strict, not off-by-one.
    ok = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=200, now=NOW)
    await session.commit()
    assert ok.outcome is BuyOutcome.OK


async def test_minimum_is_clamped_to_what_the_order_has_left(
    session: AsyncSession,
) -> None:
    """A 400-COM floor must not strand the last 100 COM forever.

    Without the clamp the tail of a partly-filled order becomes
    unbuyable and the seller has to cancel to get the escrow back.
    """
    await _seed(session, _SELLER, 1_000)
    svc = _service(session)
    order_id = await _bounded_order(session, svc, min_amount=400)

    first = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=400, now=NOW)
    await session.commit()
    assert first.outcome is BuyOutcome.OK
    assert (await _order(session, order_id)).remaining_com == 100

    tail = await svc.buy(buyer_id=_OTHER, order_id=order_id, amount_com=100, now=NOW)
    await session.commit()
    assert tail.outcome is BuyOutcome.OK

    # Still a floor while the tail is bigger than the request.
    order_id2 = await _bounded_order(session, svc, amount=200, min_amount=150)
    partial = await svc.buy(buyer_id=_BUYER, order_id=order_id2, amount_com=100, now=NOW)
    assert partial.outcome is BuyOutcome.BELOW_MIN


async def test_bounds_round_to_a_buyable_com_amount(session: AsyncSession) -> None:
    """USD prices 0.011 per COM, so a 1-USD floor is 91 COM, not 90.9.

    The suggested number has to be one the next attempt accepts: ceil on
    the floor, floor on the ceiling.
    """
    await _seed(session, _SELLER, 10_000)
    svc = _service(session)
    order_id = await _bounded_order(
        session, svc, amount=5_000, currency="USD", min_amount=1, max_amount=10
    )

    low = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=90, now=NOW)
    assert low.outcome is BuyOutcome.BELOW_MIN
    assert low.limit_com == 91

    at_floor = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=low.limit_com, now=NOW)
    await session.commit()
    assert at_floor.outcome is BuyOutcome.OK

    high = await svc.buy(buyer_id=_BUYER, order_id=order_id, amount_com=1_000, now=NOW)
    assert high.outcome is BuyOutcome.ABOVE_MAX
    assert high.limit_com == 909  # floor(10 / 0.011)
    at_ceiling = await svc.buy(
        buyer_id=_BUYER, order_id=order_id, amount_com=high.limit_com, now=NOW
    )
    await session.commit()
    assert at_ceiling.outcome is BuyOutcome.OK


async def test_create_rejects_impossible_bounds_without_taking_escrow(
    session: AsyncSession,
) -> None:
    """An order nobody can fill must not exist — and must not hold coins."""
    await _seed(session, _SELLER, 1_000)
    svc = _service(session)

    for min_amount, max_amount in ((200, 100), (0, 100), (-5, None), (100, 0)):
        res = await svc.create_sell_order(
            seller_id=_SELLER,
            amount_com=100,
            currency="RUB",
            now=NOW,
            min_amount=min_amount,
            max_amount=max_amount,
        )
        assert res.outcome is CreateOrderOutcome.INVALID_LIMITS, (min_amount, max_amount)

    await session.commit()
    assert await _balance(session, _SELLER) == 1_000  # no escrow debited
    assert (await session.execute(select(func.count()).select_from(P2pSellOrder))).scalar_one() == 0


async def test_create_rejects_bounds_wider_than_the_database_can_store(
    session: AsyncSession,
) -> None:
    """A bound of 10^30 must come back INVALID_LIMITS, not blow up the insert.

    The pair check only asked whether the bounds were positive and in
    order, which 10^30 satisfies. So the amount reached
    :meth:`P2pRepo.create_order` — but by then the seller's escrow had
    already been debited, and sqlite3 refuses to bind an integer wider
    than 64 bits, so the ``OverflowError`` landed *between* the debit and
    the order row. The transaction rolled back, which saved the coins,
    but the seller saw a generic failure instead of "these limits are not
    valid" and had no way to tell which of the three fields was wrong.

    It is reachable by typing, not by crafting: the limits line accepts
    ``1e30`` and ``int(float(...))`` turns it into exactly this integer.
    """
    from telegram_invite_bot.utils.economy import _MAX_AMOUNT

    await _seed(session, _SELLER, 1_000)
    svc = _service(session)

    for min_amount, max_amount in ((10**30, None), (None, 10**30), (1, _MAX_AMOUNT + 1)):
        res = await svc.create_sell_order(
            seller_id=_SELLER,
            amount_com=100,
            currency="RUB",
            now=NOW,
            min_amount=min_amount,
            max_amount=max_amount,
        )
        assert res.outcome is CreateOrderOutcome.INVALID_LIMITS, (min_amount, max_amount)

    await session.commit()
    assert await _balance(session, _SELLER) == 1_000  # escrow never taken
    assert (await session.execute(select(func.count()).select_from(P2pSellOrder))).scalar_one() == 0


async def test_express_caps_each_fill_at_the_orders_maximum(
    session: AsyncSession,
) -> None:
    repo = P2pRepo(session)
    await repo.create_order(
        user_id=_SELLER,
        amount_com=500,
        price_per_com=1.0,
        fiat_currency="RUB",
        payment_methods=None,
        min_amount=None,
        max_amount=50,
        now=NOW,
    )
    await session.commit()
    svc = _service(session)

    res = await svc.express_buy(buyer_id=_BUYER, currency="RUB", max_fiat=300.0, now=NOW)
    await session.commit()

    assert res.outcome is ExpressBuyOutcome.OK
    assert res.total_com == 50  # budget was 300; the seller's ceiling won
    assert (await _order(session, res.fills[0].order_id)).remaining_com == 450


async def test_express_skips_an_order_the_budget_cannot_reach(
    session: AsyncSession,
) -> None:
    """Better to fill nothing than to fill below an advertised floor."""
    repo = P2pRepo(session)
    await repo.create_order(
        user_id=_SELLER,
        amount_com=500,
        price_per_com=1.0,
        fiat_currency="RUB",
        payment_methods=None,
        min_amount=100,
        max_amount=None,
        now=NOW,
    )
    await session.commit()
    svc = _service(session)

    res = await svc.express_buy(buyer_id=_BUYER, currency="RUB", max_fiat=50.0, now=NOW)
    await session.commit()

    assert res.outcome is ExpressBuyOutcome.NO_ORDERS
    assert (await _order(session, 1)).remaining_com == 500


async def test_active_order_count_is_not_capped_by_the_display_limit(
    session: AsyncSession,
) -> None:
    """#720: the menu counter must count in SQL, not filter a page.

    ``my_orders`` caps at 15 rows for display, newest first. A seller who
    opened one order and then churned fifteen newer ones therefore has
    the live order pushed off the page entirely — the old counter, which
    filtered that list in Python, printed «0 активных ордеров» while the
    order was on the book and selling. Legacy counted unbounded in SQL
    (bot.py:19231-19234) and never had the bug.
    """
    await _seed(session, _SELLER, 10_000)
    svc = _service(session)
    live = await _create_order(session, svc, amount=100)
    for _ in range(15):
        churned = await _create_order(session, svc, amount=100)
        await svc.cancel_order(seller_id=_SELLER, order_id=churned, now=NOW)
        await session.commit()

    page = await svc.my_orders(_SELLER)
    assert len(page) == 15
    assert live not in {o.id for o in page}, "the live order is off the display page"

    assert await svc.count_active_orders(_SELLER) == 1


async def test_a_dispute_does_not_free_a_buyer_slot(session: AsyncSession) -> None:
    """#1685: disputing a trade must NOT reset the D4 ceiling.

    D4 exists because opening a trade is free for the buyer and escrows
    the SELLER's COM, so one account could otherwise freeze the whole
    book at no cost. ``disputed`` holds that escrow exactly like
    ``pending`` and ``paid`` do — ``P2pRepo.fill`` already took the
    slice out of ``remaining_com``, ``expire_pending_guard`` will not
    touch a non-pending row, and only a developer running
    ``resolve_dispute`` ever gives it back.

    So if the ceiling ignores ``disputed``, the button that freezes a
    slice forever is also the button that refunds the attacker's slot:
    buy, dispute, repeat, one order frozen per tap and no bound at all.
    The ceiling must count every status that holds escrow.
    """
    svc = _service(session)
    trade_ids = await _fill_buyer_slots(session, svc, MAX_OPEN_TRADES_PER_BUYER)
    opened = await svc.open_dispute(user_id=_BUYER, trade_id=trade_ids[0])
    await session.commit()
    assert opened.outcome is OpenDisputeOutcome.OK

    target = (await _book(session, 1))[0]
    res = await svc.buy(buyer_id=_BUYER, order_id=target, amount_com=100, now=NOW)
    await session.commit()

    assert res.outcome is BuyOutcome.TOO_MANY_OPEN
    assert await P2pRepo(session).count_open_as_buyer(_BUYER) == MAX_OPEN_TRADES_PER_BUYER
    # And the order the attacker aimed at is untouched and still sellable.
    order = await _order(session, target)
    assert order.remaining_com == 100
    assert order.status == "active"


async def test_express_cannot_be_reloaded_by_disputing(session: AsyncSession) -> None:
    """The same hole, reached through the express path.

    ``express_buy`` sizes its loop from :meth:`_free_open_slots`, so a
    ceiling that forgets ``disputed`` hands express a fresh budget on
    every dispute tap — which is the cheaper version of the attack,
    since one call fills every free slot at once.
    """
    svc = _service(session)
    trade_ids = await _fill_buyer_slots(session, svc, MAX_OPEN_TRADES_PER_BUYER)
    for trade_id in trade_ids:
        opened = await svc.open_dispute(user_id=_BUYER, trade_id=trade_id)
        assert opened.outcome is OpenDisputeOutcome.OK
    await _book(session, 3)
    await session.commit()

    res = await svc.express_buy(buyer_id=_BUYER, currency="RUB", max_fiat=10_000.0, now=NOW)
    await session.commit()

    assert res.outcome is ExpressBuyOutcome.TOO_MANY_OPEN
    assert await P2pRepo(session).count_open_as_buyer(_BUYER) == MAX_OPEN_TRADES_PER_BUYER


async def test_list_disputed_surfaces_every_frozen_escrow(session: AsyncSession) -> None:
    """Every open dispute stays reachable after the delivery-time card.

    #1687. ``handle_dispute_open`` fires the admin card exactly once,
    best effort, and only when ``ADMIN_CHAT_ID`` is set — a blocked
    bot, a deleted message or an unset id left the escrow frozen with
    no query anywhere able to find it again. #1685 sharpened that: a
    buyer sitting on unresolved disputes can no longer buy either.
    """
    svc = _service(session)
    trade_ids = await _fill_buyer_slots(session, svc, MAX_OPEN_TRADES_PER_BUYER)
    for trade_id in trade_ids[:2]:
        opened = await svc.open_dispute(user_id=_BUYER, trade_id=trade_id)
        assert opened.outcome is OpenDisputeOutcome.OK
    await session.commit()

    listed = await svc.list_disputed()

    # Oldest first: the operator works a queue, and the escrow frozen
    # longest is the one to rule on next.
    assert [int(row.id) for row in listed] == trade_ids[:2]
    # The third trade is escrowed too, but nobody has asked for a
    # ruling on it — it must not dilute the queue.
    assert {row.status for row in listed} == {TRADE_DISPUTED}
    assert await svc.count_disputed() == 2


async def test_list_disputed_page_is_bounded_but_the_count_is_not(
    session: AsyncSession,
) -> None:
    """The page may truncate; the backlog figure must not.

    A console that shows a page and reports the page size as the total
    is the same silently-partial signal this ticket exists to close.
    """
    svc = _service(session)
    trade_ids = await _fill_buyer_slots(session, svc, MAX_OPEN_TRADES_PER_BUYER)
    for trade_id in trade_ids:
        opened = await svc.open_dispute(user_id=_BUYER, trade_id=trade_id)
        assert opened.outcome is OpenDisputeOutcome.OK
    await session.commit()

    assert [int(row.id) for row in await svc.list_disputed(limit=2)] == trade_ids[:2]
    assert await svc.count_disputed() == MAX_OPEN_TRADES_PER_BUYER


# ---------------------------------------------------------------------------
# #1985 — a failed payout must undo ITS OWN transition and nothing else.
#
# ``expire_pending`` already argues this in its docstring: the session
# these methods roll back is the caller's, shared with everything else
# that ran in the same update, so ``session.rollback()`` discards work
# that has nothing to do with the failure. #209 fixed the sweeper;
# these three kept the bug.
# ---------------------------------------------------------------------------


async def _other_work_pending(session: AsyncSession) -> None:
    """Somebody else's uncommitted write on the SAME session.

    ``set_balance`` stands in for whatever the rest of the update did
    before the p2p call — a message reward, a wallet seeded by
    ``get_or_create``, a ledger row from an earlier step. What matters
    is only that it is uncommitted and unrelated.
    """
    await EconomyRepo(session).set_balance(_OTHER, 777)


async def test_failed_cancel_refund_keeps_unrelated_work_on_the_session(
    session: AsyncSession,
) -> None:
    await _seed(session, _SELLER, 1000)
    await _seed(session, _OTHER, 50)
    svc = _service(session)
    order_id = await _create_order(session, svc, amount=500)
    # Push the seller to the cap so the refund credit is refused.
    await EconomyRepo(session).set_balance(_SELLER, _MAX_AMOUNT)
    await session.commit()

    await _other_work_pending(session)
    res = await svc.cancel_order(seller_id=_SELLER, order_id=order_id, now=NOW)
    await session.commit()
    session.expire_all()

    assert res.outcome is CancelOrderOutcome.CREDIT_FAILED
    # Its own transition is still undone — that half was never in doubt.
    assert (await _order(session, order_id)).status == "active"
    assert await _balance(session, _OTHER) == 777


async def test_failed_confirm_release_keeps_unrelated_work_on_the_session(
    session: AsyncSession,
) -> None:
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, _MAX_AMOUNT)
    await _seed(session, _OTHER, 50)
    svc = _service(session)
    trade_id = await _paid_trade(session, svc, amount=200)

    await _other_work_pending(session)
    res = await svc.seller_confirm(seller_id=_SELLER, trade_id=trade_id, now=NOW)
    await session.commit()
    session.expire_all()

    assert res.outcome is ConfirmOutcome.CREDIT_FAILED
    assert (await _trade(session, trade_id)).status == "paid"
    assert await _balance(session, _OTHER) == 777


async def test_failed_dispute_payout_keeps_unrelated_work_on_the_session(
    session: AsyncSession,
) -> None:
    await _seed(session, _SELLER, 1000)
    await _seed(session, _BUYER, _MAX_AMOUNT)
    await _seed(session, _OTHER, 50)
    svc = _service(session)
    trade_id = await _disputed_trade(session, svc)

    await _other_work_pending(session)
    res = await svc.resolve_dispute(
        admin_id=_ADMIN,
        trade_id=trade_id,
        resolution=DisputeResolution.REFUND_BUYER,
        now=NOW,
    )
    await session.commit()
    session.expire_all()

    assert res.outcome is ResolveOutcome.CREDIT_FAILED
    assert (await _trade(session, trade_id)).status == "disputed"
    assert await _balance(session, _OTHER) == 777
