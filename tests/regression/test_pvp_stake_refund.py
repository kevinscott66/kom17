"""#267/#268/#263/#209 — a held PvP stake must come back promptly.

Prod offer #80 was created 2026-03-25 and was still ``pending`` — its
creator's 100 COM still in escrow — 87 days later. Four separate defects
kept it there, and this file pins all four:

* **#267** the port expired challenges on the P2P knob (30 min) instead
  of legacy's ``PVP_OFFER_TTL_SEC = 600`` (bot.py:3782), and dropped the
  lazy on-access expiry legacy ran at accept time (bot.py:14905), so the
  hourly sweeper was the ONLY thing that could ever release a stake;
* **#268** every money step shared one transaction with the hygiene
  steps, and the whole loop slept a full hour BEFORE its first pass — on
  a unit whose median lifetime between restarts is under half an hour,
  the refund step effectively never ran;
* **#263** the refund used a bare ``assert``. Prod runs without ``-O``,
  so one creator whose wallet had hit the balance ceiling poisoned the
  entire pass — permanently, since the scan is ordered by ``id ASC``;
* **#209** a failed refund called ``rollback()`` on the SHARED session,
  silently discarding the other steps' work and then over-reporting.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import EconomyUser, Transaction
from telegram_invite_bot.db.models.pvp import PvpEscrow, PvpOffer  # noqa: F401 — tables
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.repositories.economy_repo import EconomyRepo
from telegram_invite_bot.repositories.transactions_repo import TransactionsRepo
from telegram_invite_bot.scheduler import economy_cleanup as ec_mod
from telegram_invite_bot.scheduler.economy_cleanup import (
    EconomyCleanupReport,
    EconomyCleanupSweeper,
    MoneySweepReport,
)
from telegram_invite_bot.services.pvp_service import (
    DEFAULT_OFFER_TTL_MINUTES,
    PvpAcceptOutcome,
    PvpService,
)
from telegram_invite_bot.utils.economy import _MAX_AMOUNT
from telegram_invite_bot.utils.time import db_now

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session(tmp_path: Path) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


def _svc(session: AsyncSession, **kw: Any) -> PvpService:
    return PvpService(EconomyRepo(session), TransactionsRepo(session), session, **kw)


async def _seed(session: AsyncSession, user_id: int, balance: int = 1_000) -> None:
    session.add(EconomyUser(user_id=user_id, balance=balance, language="ru"))
    await session.commit()


async def _bal(session: AsyncSession, user_id: int) -> int:
    return int(
        (
            await session.execute(select(EconomyUser.balance).where(EconomyUser.user_id == user_id))
        ).scalar_one()
    )


async def _offer(session: AsyncSession, offer_id: int) -> PvpOffer:
    return (await session.execute(select(PvpOffer).where(PvpOffer.id == offer_id))).scalar_one()


async def _peg_at_the_ceiling(session: AsyncSession, user_id: int) -> None:
    """Make every future refund for this wallet fail.

    ``EconomyRepo.release`` (like ``credit``) guards
    ``balance + amount <= _MAX_AMOUNT`` in the UPDATE's WHERE, so a
    wallet already at the ceiling returns ``None`` rather than raising —
    exactly the shape the old ``assert`` turned into a crash.
    """
    await session.execute(
        update(EconomyUser).where(EconomyUser.user_id == user_id).values(balance=_MAX_AMOUNT)
    )
    await session.commit()


# --- #267: the TTL is legacy's ten minutes, not the P2P half-hour ----------


async def test_the_shipped_default_ttl_is_the_legacy_ten_minutes() -> None:
    assert DEFAULT_OFFER_TTL_MINUTES == 10


async def test_an_eleven_minute_old_offer_expires_on_the_default_ttl(
    session: AsyncSession,
) -> None:
    await _seed(session, 1, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1,
        game="coin",
        bet=100,
        side="heads",
        chat_id=-100,
        now=db_now() - timedelta(minutes=11),
    )
    assert created.offer_id is not None
    assert await _bal(session, 1) == 900  # held

    # No ttl_minutes override: this is what production passes.
    assert (await svc.sweep_expired(db_now())).count == 1
    assert await _bal(session, 1) == 1_000
    assert (await _offer(session, created.offer_id)).status == "expired"


async def test_a_nine_minute_old_offer_still_stands(session: AsyncSession) -> None:
    await _seed(session, 1, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1,
        game="coin",
        bet=100,
        side="heads",
        chat_id=-100,
        now=db_now() - timedelta(minutes=9),
    )
    assert created.offer_id is not None
    assert (await svc.sweep_expired(db_now())).count == 0
    assert (await _offer(session, created.offer_id)).status == "pending"


# --- #267: the lazy on-access expiry legacy ran at accept time -------------


async def test_accepting_a_stale_offer_expires_and_refunds_it(
    session: AsyncSession,
) -> None:
    """The hole that mattered: a dead challenge stayed CLICKABLE.

    Legacy called ``pvp_expire_offers()`` at the top of accept
    (bot.py:14905). Without it an offer whose deadline had passed was
    still acceptable right up until the next hourly sweep, so a stake
    could be matched against a challenge its creator had long written off.
    """
    await _seed(session, 1, 1_000)
    await _seed(session, 2, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1,
        game="dice",
        bet=100,
        side=None,
        chat_id=-100,
        now=db_now() - timedelta(minutes=11),
    )
    assert created.offer_id is not None

    res = await svc.accept_and_resolve(
        offer_id=created.offer_id, opponent_id=2, chat_id=-100, now=db_now()
    )
    assert res.outcome is PvpAcceptOutcome.NOT_FOUND
    assert (await _offer(session, created.offer_id)).status == "expired"
    assert await _bal(session, 1) == 1_000  # creator's stake released
    assert await _bal(session, 2) == 1_000  # opponent never escrowed
    # #2020: the retired offer rides back on the refusal. It is the only
    # carrier of the card's coordinates, and the sweeper that closes
    # those cards can never see an offer this path retired — ``expire``
    # runs exactly once per offer, and this call was it.
    assert res.expired is not None
    assert res.expired.offer_id == created.offer_id
    assert res.expired.bet == 100

    # ...and only on the call that did the retiring. A second tap moves
    # no coins and owns no edit.
    again = await svc.accept_and_resolve(
        offer_id=created.offer_id, opponent_id=2, chat_id=-100, now=db_now()
    )
    assert again.outcome is PvpAcceptOutcome.NOT_FOUND
    assert again.expired is None


async def test_accepting_a_fresh_offer_is_untouched_by_the_lazy_path(
    session: AsyncSession,
) -> None:
    await _seed(session, 1, 1_000)
    await _seed(session, 2, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1, game="dice", bet=100, side=None, chat_id=-100, now=db_now()
    )
    assert created.offer_id is not None
    res = await svc.accept_and_resolve(
        offer_id=created.offer_id, opponent_id=2, chat_id=-100, now=db_now()
    )
    assert res.outcome is PvpAcceptOutcome.SUCCESS


async def test_a_null_created_at_is_not_treated_as_stale(
    session: AsyncSession,
) -> None:
    """``created_at`` is nullable, and the SQL guards read NULL as false.

    If the lazy path called such an offer stale it would reject the
    accept while ``expire_guard`` refused to expire it — the stake would
    be frozen with nothing able to release it.
    """
    await _seed(session, 1, 1_000)
    await _seed(session, 2, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1, game="dice", bet=100, side=None, chat_id=-100, now=db_now()
    )
    assert created.offer_id is not None
    await session.execute(
        update(PvpOffer).where(PvpOffer.id == created.offer_id).values(created_at=None)
    )
    await session.commit()

    res = await svc.accept_and_resolve(
        offer_id=created.offer_id, opponent_id=2, chat_id=-100, now=db_now()
    )
    assert res.outcome is PvpAcceptOutcome.SUCCESS


# --- #267: finished_at must be the real time, not the cutoff --------------


async def test_finished_at_records_the_sweep_time_not_the_deadline(
    session: AsyncSession,
) -> None:
    """Prod offer #80 reads ``finished_at=09:18:51`` against a refund at
    ``09:48:51`` — the row was back-dated by the whole TTL because the
    cutoff was stamped instead of the clock. Legacy stamped the real
    sweep time (bot.py:14812).
    """
    await _seed(session, 1, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1,
        game="coin",
        bet=100,
        side="heads",
        chat_id=-100,
        now=db_now() - timedelta(minutes=30),
    )
    assert created.offer_id is not None

    now = db_now()
    cutoff = now - timedelta(minutes=10)
    assert await svc.expire(offer_id=created.offer_id, cutoff=cutoff, now=now) is not None

    finished_at = (await _offer(session, created.offer_id)).finished_at
    assert finished_at is not None
    assert abs((finished_at - now).total_seconds()) < 1
    assert finished_at > cutoff


# --- #263/#268: one poisoned offer must not take the pass down ------------


async def test_a_failing_refund_does_not_block_the_offers_behind_it(
    session: AsyncSession,
) -> None:
    """The scan is ordered by ``id ASC``, so a permanently-failing offer
    sits at the head of it forever. With the old bare ``assert`` (live in
    prod — the unit runs without ``-O``) every later offer's refund was
    collateral damage on every pass, for good.
    """
    await _seed(session, 1, 1_000)
    await _seed(session, 2, 1_000)
    svc = _svc(session)
    stale = db_now() - timedelta(minutes=30)
    poisoned = await svc.create_offer(
        creator_id=1, game="coin", bet=100, side="heads", chat_id=-100, now=stale
    )
    healthy = await svc.create_offer(
        creator_id=2, game="coin", bet=100, side="heads", chat_id=-100, now=stale
    )
    assert poisoned.offer_id is not None
    assert healthy.offer_id is not None
    assert poisoned.offer_id < healthy.offer_id  # the poison is FIRST in the scan
    await _peg_at_the_ceiling(session, 1)

    assert (await svc.sweep_expired(db_now())).count == 1

    # The healthy offer was refunded and closed...
    assert await _bal(session, 2) == 1_000
    assert (await _offer(session, healthy.offer_id)).status == "expired"
    # ...and the poisoned one was rolled back to pending, not half-expired.
    assert (await _offer(session, poisoned.offer_id)).status == "pending"
    assert await _bal(session, 1) == _MAX_AMOUNT


async def test_a_failed_expire_refund_leaves_no_half_closed_offer(
    session: AsyncSession,
) -> None:
    """``expire_guard`` flips ``pending → expired`` BEFORE the credit, so
    the two must be one savepoint. Without it a failed refund strands the
    stake as an ``expired`` offer with a ``held`` escrow — the coins
    simply vanish.
    """
    await _seed(session, 1, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1,
        game="coin",
        bet=100,
        side="heads",
        chat_id=-100,
        now=db_now() - timedelta(minutes=30),
    )
    assert created.offer_id is not None
    await _peg_at_the_ceiling(session, 1)

    now = db_now()
    retired = await svc.expire(
        offer_id=created.offer_id, cutoff=now - timedelta(minutes=10), now=now
    )
    assert retired is None
    assert (await _offer(session, created.offer_id)).status == "pending"


async def test_a_failed_cancel_refund_leaves_no_half_closed_offer(
    session: AsyncSession,
) -> None:
    """Same shape on the user-facing path: ``cancel_guard`` flips the
    status first, so a failed credit outside a savepoint would leave a
    cancelled offer whose stake is still held.
    """
    await _seed(session, 1, 1_000)
    svc = _svc(session)
    created = await svc.create_offer(
        creator_id=1, game="coin", bet=100, side="heads", chat_id=-100, now=db_now()
    )
    assert created.offer_id is not None
    await _peg_at_the_ceiling(session, 1)

    assert await svc.cancel(offer_id=created.offer_id, creator_id=1) is False
    assert (await _offer(session, created.offer_id)).status == "pending"


# --- #268: the sweeper's two cadences -------------------------------------


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cleanup.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    reg = EngineRegistry(
        engines={DBName.ECONOMY: engine},
        sessions={DBName.ECONOMY: async_sessionmaker(engine, expire_on_commit=False)},
    )
    try:
        yield reg
    finally:
        await engine.dispose()


async def test_the_money_half_runs_every_minute_not_every_hour(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop used to sleep a full hour before its FIRST pass, so on a
    unit that restarts every ~28 minutes the refund step never ran at all.

    #1393 put the hygiene half on the first tick for the same reason, so
    the expected shape is one hygiene pass right after startup and
    money-only ticks behind it — not hygiene on every tick.
    """
    slept: list[float] = []
    calls: list[str] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= 3:
            raise asyncio.CancelledError

    async def _money(_self: EconomyCleanupSweeper) -> MoneySweepReport:
        calls.append("money")
        return MoneySweepReport()

    async def _once(_self: EconomyCleanupSweeper) -> EconomyCleanupReport:
        calls.append("hygiene")
        return EconomyCleanupReport(inventory_deleted=0, game_plays_deleted=0)

    monkeypatch.setattr(ec_mod.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(EconomyCleanupSweeper, "sweep_money", _money)
    monkeypatch.setattr(EconomyCleanupSweeper, "sweep_once", _once)

    sweeper = EconomyCleanupSweeper(registry, interval_seconds=3600.0)
    with pytest.raises(asyncio.CancelledError):
        await sweeper.run()

    assert slept == [60.0, 60.0, 60.0]
    # The 3rd sleep cancels before its pass. Hygiene leads (#1393), then
    # the money-only ticks that fill the rest of the hour.
    assert calls == ["hygiene", "money"]


async def test_seeding_the_hygiene_counter_does_not_collapse_the_interval(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1393 must buy a startup pass, not turn every tick into one.

    With a 180 s interval and the 60 s money tick the shape is hygiene,
    money, money, hygiene — the seed is consumed by the first tick and
    the counter then measures a full interval like it always did.
    """
    slept: list[float] = []
    calls: list[str] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= 5:
            raise asyncio.CancelledError

    async def _money(_self: EconomyCleanupSweeper) -> MoneySweepReport:
        calls.append("money")
        return MoneySweepReport()

    async def _once(_self: EconomyCleanupSweeper) -> EconomyCleanupReport:
        calls.append("hygiene")
        return EconomyCleanupReport(inventory_deleted=0, game_plays_deleted=0)

    # ``ec_mod.asyncio`` above is this very module object, so patching it
    # here is the same write; spelled directly because the attribute form
    # is not an explicit re-export and mypy rejects it under strict.
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(EconomyCleanupSweeper, "sweep_money", _money)
    monkeypatch.setattr(EconomyCleanupSweeper, "sweep_once", _once)

    sweeper = EconomyCleanupSweeper(registry, interval_seconds=180.0)
    with pytest.raises(asyncio.CancelledError):
        await sweeper.run()

    assert calls == ["hygiene", "money", "money", "hygiene"]


class _FakeMonotonic:
    """Stands in for the ``time`` module inside the sweeper.

    Needed because #1487 charges the hygiene counter against
    ``time.monotonic()`` while the sleep it corrects is stubbed out —
    a real clock would report ~0 for both the sleep and the pass and
    prove nothing.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


async def test_a_pass_longer_than_its_tick_is_charged_to_the_hygiene_counter(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1487: the counter measures wall clock, not the nominal sleeps.

    ``since_hygiene += money_tick`` counted what the loop ASKED for.
    What an iteration actually spends is that sleep plus the pass that
    follows it, so the hygiene period was ``interval + sum of pass
    durations`` and drifted a little further out with every tick.

    Here one money pass costs 120 s against a 60 s tick, so the third
    slot of a 180 s interval is already gone by the time it comes
    round: the shape is hygiene, money, hygiene. The old arithmetic
    read the same three passes as hygiene, money, money and put the
    second hygiene pass a whole tick later than the interval promised.

    Passes shorter than the tick change nothing — ``max`` keeps the
    nominal number — which is why every other cadence pinned in this
    file still means what it meant.
    """
    clock = _FakeMonotonic()
    slept: list[float] = []
    calls: list[str] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) >= 4:
            raise asyncio.CancelledError

    async def _money(_self: EconomyCleanupSweeper) -> MoneySweepReport:
        calls.append("money")
        clock.now += 120.0
        return MoneySweepReport()

    async def _once(_self: EconomyCleanupSweeper) -> EconomyCleanupReport:
        calls.append("hygiene")
        return EconomyCleanupReport(inventory_deleted=0, game_plays_deleted=0)

    # ``asyncio`` here is the same module object the sweeper holds; see
    # the note in the test above for why it is spelled directly.
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(ec_mod, "time", clock)
    monkeypatch.setattr(EconomyCleanupSweeper, "sweep_money", _money)
    monkeypatch.setattr(EconomyCleanupSweeper, "sweep_alerts", _no_alerts)
    monkeypatch.setattr(EconomyCleanupSweeper, "sweep_once", _once)

    sweeper = EconomyCleanupSweeper(registry, interval_seconds=180.0)
    with pytest.raises(asyncio.CancelledError):
        await sweeper.run()

    assert calls == ["hygiene", "money", "hygiene"]


async def _no_alerts(_self: EconomyCleanupSweeper) -> int:
    """The alert scan is not part of the cadence under test."""
    return 0


async def test_a_short_interval_still_bounds_the_money_tick(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests configure tiny intervals; the tick must never exceed one."""
    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        raise asyncio.CancelledError

    monkeypatch.setattr(ec_mod.asyncio, "sleep", _fake_sleep)
    sweeper = EconomyCleanupSweeper(registry, interval_seconds=0.05)
    with pytest.raises(asyncio.CancelledError):
        await sweeper.run()
    assert slept == [0.05]


async def test_sweep_money_refunds_a_stale_offer_through_the_sweeper(
    registry: EngineRegistry,
) -> None:
    """End-to-end through the sweeper's own sessions, with the shipped
    TTL default — no override anywhere in the chain.
    """
    maker = registry.sessions[DBName.ECONOMY]
    async with maker() as s:
        s.add(EconomyUser(user_id=7, balance=1_000, language="ru"))
        await s.commit()
        created = await _svc(s).create_offer(
            creator_id=7,
            game="coin",
            bet=100,
            side="heads",
            chat_id=-100,
            now=db_now() - timedelta(minutes=11),
        )
        await s.commit()
    assert created.offer_id is not None

    sweeper = EconomyCleanupSweeper(registry, interval_seconds=3600.0)
    assert (await sweeper.sweep_money()).pvp_offers_expired == 1

    async with maker() as s:
        assert await _bal(s, 7) == 1_000
        assert (await _offer(s, created.offer_id)).status == "expired"


# --- #1707: the refund and its ledger row are one unit or neither ---------


class _LedgerThatRefusesRefunds(TransactionsRepo):
    """A ``transactions`` INSERT that fails the way a poisoned row does.

    Only the refund row refuses; the hold written at create time must
    still go through, or the offer under test would never exist.
    """

    async def record(
        self,
        *,
        amount: int,
        type: str,
        from_id: int | None = None,
        to_id: int | None = None,
        reason: str | None = None,
        date: datetime | None = None,
    ) -> None:
        if type == "pvp_refund":
            msg = "no such column: transactions.type"
            raise RuntimeError(msg)
        await super().record(
            amount=amount,
            type=type,
            from_id=from_id,
            to_id=to_id,
            reason=reason,
            date=date,
        )


async def _refund_rows(session: AsyncSession) -> int:
    stmt = select(Transaction).where(Transaction.type == "pvp_refund")
    return len((await session.execute(stmt)).scalars().all())


async def test_a_failing_refund_ledger_row_undoes_the_whole_expire(
    session: AsyncSession,
) -> None:
    """The hole: coins that appear from nowhere, once per poisoned offer.

    ``sweep_expired`` wraps each ``expire`` in ``except Exception``
    (#268) and the session it shares COMMITS on clean exit. While the
    ledger row was written AFTER the savepoint closed, a raising INSERT
    was logged, swallowed — and the released hold committed anyway,
    leaving a real credit with nothing in ``transactions`` to explain
    it. ``P2pService.expire_pending`` has always written its refund row
    inside the savepoint; this is the same shape.
    """
    await _seed(session, 1, 1_000)
    svc = PvpService(EconomyRepo(session), _LedgerThatRefusesRefunds(session), session)
    created = await svc.create_offer(
        creator_id=1,
        game="coin",
        bet=100,
        side="heads",
        chat_id=-100,
        now=db_now() - timedelta(minutes=11),
    )
    assert created.offer_id is not None
    assert await _bal(session, 1) == 900  # held

    # The prod path: the sweep eats the fault and commits the pass.
    assert (await svc.sweep_expired(db_now())).count == 0
    await session.commit()

    assert (await _offer(session, created.offer_id)).status == "pending"
    assert await _bal(session, 1) == 900  # NOT credited
    assert await _refund_rows(session) == 0


async def test_a_failing_cancel_ledger_row_undoes_the_whole_cancel(
    session: AsyncSession,
) -> None:
    """The same pairing on :meth:`PvpService.cancel`.

    Latent rather than live — every caller of ``cancel`` lets the
    exception reach a handler that rolls the session back — but the two
    paths are one refund idiom and drift between them is how #1707
    happened, so both are pinned.
    """
    await _seed(session, 1, 1_000)
    svc = PvpService(EconomyRepo(session), _LedgerThatRefusesRefunds(session), session)
    created = await svc.create_offer(
        creator_id=1, game="coin", bet=100, side="heads", chat_id=-100, now=db_now()
    )
    assert created.offer_id is not None

    with pytest.raises(RuntimeError, match="no such column"):
        await svc.cancel(offer_id=created.offer_id, creator_id=1)
    await session.commit()

    assert (await _offer(session, created.offer_id)).status == "pending"
    assert await _bal(session, 1) == 900  # still held, not double-counted
    assert await _refund_rows(session) == 0
