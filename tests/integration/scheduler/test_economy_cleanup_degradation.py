"""Degradation posture of the economy cleanup sweeper (#1512-#1514, #1519).

Every test here pins a failure that used to propagate further than it
should have. The sweeper is a background unit with a very short median
incarnation, so "one bad row costs the whole pass" and "one bad row
costs the whole hour" are the same bug wearing different clocks: work
that is skipped in silence is never retried before the next restart
throws the counter away again.

The four fixes under test are deliberately narrow — each one widens or
moves a guard, none of them changes what the sweeper does when nothing
fails. The tests are written so they fail LOUDLY if a guard is dropped:
the discriminator in each case is a call count or a return value, never
a log line.
"""

from __future__ import annotations

import asyncio
import inspect
import pathlib
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import pytest
from aiogram import Bot
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.handlers import checks as checks_module
from telegram_invite_bot.repositories.vip_repo import VipExpiryCandidate
from telegram_invite_bot.scheduler import economy_cleanup
from telegram_invite_bot.scheduler.economy_cleanup import (
    EconomyCleanupReport,
    EconomyCleanupSweeper,
    MoneySweepReport,
    _HygieneResult,
)
from telegram_invite_bot.services import withdraw_service
from telegram_invite_bot.services.p2p_service import ExpiredTrade
from telegram_invite_bot.services.p2p_service import SweepReport as P2pSweepReport
from telegram_invite_bot.services.pvp_service import ExpiredOffer, PvpSweepReport

_NOW = datetime(2026, 6, 10, 12, 0, 0)


@pytest.fixture
async def registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    reg = EngineRegistry(
        engines={DBName.ECONOMY: engine},
        sessions={DBName.ECONOMY: sessionmaker},
    )
    try:
        yield reg
    finally:
        await engine.dispose()


class _FakeBot:
    """Captures DMs; never fails."""

    def __init__(self) -> None:
        self.sent: list[int] = []

    async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
        del text
        self.sent.append(chat_id)


# ---------------------------------------------------------------------------
# #1512 — the hygiene slot is charged after the pass, not before it
# ---------------------------------------------------------------------------


async def test_a_failed_hygiene_pass_retries_on_the_very_next_tick(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising ``sweep_once`` must not consume the hygiene window.

    The reset used to run BEFORE the call, so one transient failure
    burned the whole interval and pushed the retry a full window out.
    Since the median incarnation of this unit is shorter than the
    production interval, that retry usually never happened at all.

    The discriminator is ``sweep_money``: under the fixed ordering the
    counter is never decremented on a failing pass, so every following
    tick is a hygiene tick and the money branch is never reached. Under
    the old ordering the intervening money-only ticks would run.
    """
    monkeypatch.setattr(economy_cleanup, "_MONEY_TICK_SECONDS", 0.01)
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW, interval_seconds=0.05)

    calls = {"hygiene": 0, "money": 0, "alerts": 0}

    async def _sweep_once() -> EconomyCleanupReport:
        calls["hygiene"] += 1
        # Third failure ends the loop; CancelledError is re-raised by
        # both guards rather than swallowed as a pass failure.
        if calls["hygiene"] >= 3:
            raise asyncio.CancelledError
        raise RuntimeError("database is locked")

    async def _sweep_money() -> MoneySweepReport:
        calls["money"] += 1
        return MoneySweepReport()

    async def _sweep_alerts() -> int:
        calls["alerts"] += 1
        return 0

    monkeypatch.setattr(sweeper, "sweep_once", _sweep_once)
    monkeypatch.setattr(sweeper, "sweep_money", _sweep_money)
    monkeypatch.setattr(sweeper, "sweep_alerts", _sweep_alerts)

    with pytest.raises(asyncio.CancelledError):
        await sweeper.run()

    assert calls["hygiene"] == 3
    assert calls["money"] == 0
    assert calls["alerts"] == 0


async def test_a_successful_hygiene_pass_still_yields_the_money_ticks(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The counterpart: a pass that SUCCEEDS must give the slot back.

    Without this test the #1512 fix could be taken further into never
    charging the slot at all, which would turn every tick into a
    hygiene tick — an hourly job running once a minute, against the
    money DB, forever.

    The sleep is stubbed out because this is the one test in the file
    whose discriminator is an exact tick COUNT. The loop charges the
    counter in wall clock (``max(money_tick, measured span)``, #1487),
    so with a real ``asyncio.sleep(0.01)`` the answer depends on how
    much over 10 ms the event loop actually returns: four ticks reach
    the 50 ms interval on an idle machine and three reach it under the
    load of a full-suite run, which is exactly how this test failed
    intermittently. A stubbed sleep measures ~0, the nominal tick
    becomes the only truthful number — the case that ``max`` is there
    for — and the cadence is arithmetic instead of a race.
    """

    async def _instant_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(economy_cleanup.asyncio, "sleep", _instant_sleep)
    monkeypatch.setattr(economy_cleanup, "_MONEY_TICK_SECONDS", 0.01)
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW, interval_seconds=0.05)

    calls = {"hygiene": 0, "money": 0}

    async def _sweep_once() -> EconomyCleanupReport:
        calls["hygiene"] += 1
        if calls["hygiene"] >= 2:
            raise asyncio.CancelledError
        return EconomyCleanupReport(inventory_deleted=0, game_plays_deleted=0)

    async def _sweep_money() -> MoneySweepReport:
        calls["money"] += 1
        return MoneySweepReport()

    async def _sweep_alerts() -> int:
        return 0

    monkeypatch.setattr(sweeper, "sweep_once", _sweep_once)
    monkeypatch.setattr(sweeper, "sweep_money", _sweep_money)
    monkeypatch.setattr(sweeper, "sweep_alerts", _sweep_alerts)

    with pytest.raises(asyncio.CancelledError):
        await sweeper.run()

    # One hygiene tick, then four money-only ticks, then the second
    # hygiene tick that cancels: the slot was charged exactly once,
    # and a full interval was measured from zero afterwards.
    assert calls["hygiene"] == 2
    assert calls["money"] == 4


# ---------------------------------------------------------------------------
# #1513 — the two money steps degrade independently
# ---------------------------------------------------------------------------


class _BoomP2p:
    """Stands in for ``P2pService`` with a poisoned trade table."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def sweep(self, _now: datetime) -> P2pSweepReport:
        raise RuntimeError("no such column: p2p_trades.status")


class _BoomPvp:
    """Stands in for ``PvpService`` with a poisoned offer table."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def sweep_expired(self, _now: datetime) -> PvpSweepReport:
        raise RuntimeError("no such column: pvp_offers.status")


async def test_a_failing_p2p_step_does_not_stop_the_pvp_refunds(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 1/3 down must leave step 2/3 running.

    Both steps refund real coins, and they already had separate
    transactions — but a raised exception still escaped the method, so
    one poisoned P2P row froze every held PvP stake too.
    """
    swept: list[str] = []

    class _FakePvp:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def sweep_expired(self, _now: datetime) -> PvpSweepReport:
            swept.append("pvp")
            return PvpSweepReport(
                expired=tuple(
                    ExpiredOffer(
                        offer_id=i,
                        creator_id=i,
                        bet=100,
                        chat_id=-100,
                        message_id=None,
                    )
                    for i in range(7)
                )
            )

    monkeypatch.setattr(economy_cleanup, "P2pService", _BoomP2p)
    monkeypatch.setattr(economy_cleanup, "PvpService", _FakePvp)
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW)

    assert (await sweeper.sweep_money()).pvp_offers_expired == 7
    assert swept == ["pvp"]


async def test_a_failing_pvp_step_reports_zero_instead_of_raising(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Step 2/3 down must not abort the pass either.

    ``sweep_money`` feeds ``pvp_offers_expired`` on the report, so the
    degraded value has to be a real count — zero — rather than an
    exception that would take the stale-withdrawal alert down with it.
    """
    swept: list[str] = []

    class _FakeP2p:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def sweep(self, _now: datetime) -> P2pSweepReport:
            swept.append("p2p")
            return P2pSweepReport()

    monkeypatch.setattr(economy_cleanup, "P2pService", _FakeP2p)
    monkeypatch.setattr(economy_cleanup, "PvpService", _BoomPvp)
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW)

    assert (await sweeper.sweep_money()).pvp_offers_expired == 0
    assert swept == ["p2p"]


async def test_both_money_steps_down_still_returns_a_count(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pathological case: neither step survives, the pass still does."""
    monkeypatch.setattr(economy_cleanup, "P2pService", _BoomP2p)
    monkeypatch.setattr(economy_cleanup, "PvpService", _BoomPvp)
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW)

    assert (await sweeper.sweep_money()).pvp_offers_expired == 0


# ---------------------------------------------------------------------------
# #2012 — a step's report is only true once its transaction committed
# ---------------------------------------------------------------------------


class _CommitFailsP2p:
    """Sweeps a trade, then poisons the commit that would persist it.

    Stands in for the ordinary way this happens in production: SQLite
    takes the writes into the transaction without complaint and refuses
    at ``COMMIT`` — a locked database, a full disk, a disk I/O error.
    Everything the service returned is real right up to that moment and
    worthless immediately after.
    """

    def __init__(self, *args: object, **_kwargs: object) -> None:
        # 4th positional, matching the real ``P2pService`` signature.
        self._session = cast("Any", args[3])

    async def sweep(self, _now: datetime) -> P2pSweepReport:
        async def boom() -> None:
            raise RuntimeError("database is locked")

        self._session.commit = boom
        return P2pSweepReport(
            expired=(
                ExpiredTrade(
                    trade_id=1,
                    order_id=1,
                    seller_id=11,
                    buyer_id=22,
                    amount_com=500,
                    returned_to_order=True,
                ),
            )
        )


async def test_a_rolled_back_p2p_step_neither_dms_nor_counts(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refund that did not commit did not happen.

    The step's report used to be bound INSIDE the ``async with``, i.e.
    before ``session_for`` had committed anything. A commit that then
    failed was caught by the step's own ``except`` — correctly, that is
    what #1513 built — but the populated report survived the failure in
    a variable the fan-out below reads. The buyer got «ваши COM
    возвращены» for coins that were rolled back, and
    ``MoneySweepReport`` counted an expiry that never landed.

    The discriminator is deliberately the DM and the count, not a log
    line: this is a lie told to a user about money, and it has to fail
    here on the same terms the user would experience it.
    """

    class _FakePvp:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def sweep_expired(self, _now: datetime) -> PvpSweepReport:
            return PvpSweepReport()

    monkeypatch.setattr(economy_cleanup, "P2pService", _CommitFailsP2p)
    monkeypatch.setattr(economy_cleanup, "PvpService", _FakePvp)
    bot = _FakeBot()
    sweeper = EconomyCleanupSweeper(
        registry, clock=lambda: _NOW, bot=cast("Bot", cast("object", bot))
    )

    report = await sweeper.sweep_money()

    assert bot.sent == [], (
        "the sweeper told a buyer their escrow came back after the"
        f" transaction that would have returned it was rolled back: {bot.sent}"
    )
    assert report.p2p_trades_expired == 0, (
        "the pass reported an expiry whose transaction did not commit"
    )


# ---------------------------------------------------------------------------
# #1514 — a failed notice mark costs one repeat DM, not the rest of the batch
# ---------------------------------------------------------------------------


async def test_a_failed_vip_mark_does_not_abort_the_rest_of_the_fan_out(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unwritable mark must not silence the remaining candidates.

    The mark used to sit outside every guard in the loop, so a locked
    economy.db on the first candidate left the rest un-DMed while the
    ones before it stayed marked — the worst of both halves. The
    documented trade is "at most one repeat DM next pass", and that is
    exactly what a failure here now costs.
    """
    marked: list[int] = []

    class _FlakyVipRepo:
        def __init__(self, _session: object) -> None:
            pass

        async def mark_expiry_notified(self, *, user_id: int, vip_till: float) -> None:
            del vip_till
            marked.append(user_id)
            if user_id == 1:
                raise RuntimeError("database is locked")

    monkeypatch.setattr(economy_cleanup, "VipRepo", _FlakyVipRepo)
    monkeypatch.setattr(economy_cleanup, "_SEND_PAUSE_SECONDS", 0.0)
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW)

    candidates = [
        VipExpiryCandidate(user_id=uid, vip_till=_NOW.timestamp() + 3600, language="ru")
        for uid in (1, 2, 3)
    ]
    bot = _FakeBot()
    sent = await sweeper._notify_vip_expiring(cast("Bot", bot), candidates, _NOW)

    # Every candidate was DMed and every mark was attempted; the DM
    # count is what the report shows, so a failed mark must not shrink
    # it either.
    assert bot.sent == [1, 2, 3]
    assert marked == [1, 2, 3]
    assert sent == 3


# ---------------------------------------------------------------------------
# #1519 — the two timestamp-frame anchors name symbols that exist
# ---------------------------------------------------------------------------


def test_the_timestamp_frame_anchors_name_real_symbols() -> None:
    """Both anchors in the sweeper pointed at things that do not exist.

    ``WithdrawService._now_iso`` was never a method, and the checks
    anchor cited a call site instead of the helper it names. An anchor
    that is wrong about WHAT it points at is worse than a missing one,
    because it invites the reader to reason from a frame the code does
    not actually use.
    """
    source = pathlib.Path(economy_cleanup.__file__).read_text(encoding="utf-8")

    assert "WithdrawService._now_iso" not in source
    assert "withdraw_service._now_iso" in source
    assert inspect.isfunction(withdraw_service._now_iso)

    checks_lines = pathlib.Path(checks_module.__file__).read_text(encoding="utf-8").splitlines()
    lineno = next(
        index for index, line in enumerate(checks_lines, 1) if line.startswith("def _utcnow")
    )
    assert f"handlers/checks.py:{lineno}" in source


# ---------------------------------------------------------------------------
# #1706 — a failing hygiene step must not take the rest of the pass with it
# ---------------------------------------------------------------------------


async def test_a_failing_hygiene_step_still_runs_the_stale_withdrawal_alarm(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alarm sat AFTER the hygiene session, so a fault hid it.

    #1706. ``sweep_alerts`` is the only push signal that says a
    hand-worked payout queue has stopped being worked, and it lived
    downstream of a session that reaps inventory rows. A durable
    hygiene fault — a broken index, one poisoned row — therefore
    silenced it for as long as the fault lasted, which for a signal
    about a queue nobody is watching is exactly the wrong direction to
    fail in.

    The discriminator is the alert count and the returned report: an
    unguarded hygiene step raises out of ``sweep_once`` before either
    exists.
    """
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW)
    alerts = {"n": 0}

    async def _boom(_now: datetime) -> _HygieneResult:
        msg = "no such index: ix_inventory_expires"
        raise RuntimeError(msg)

    async def _sweep_money() -> MoneySweepReport:
        return MoneySweepReport()

    async def _sweep_alerts() -> int:
        alerts["n"] += 1
        return 2

    monkeypatch.setattr(sweeper, "_sweep_hygiene", _boom)
    monkeypatch.setattr(sweeper, "sweep_money", _sweep_money)
    monkeypatch.setattr(sweeper, "sweep_alerts", _sweep_alerts)

    report = await sweeper.sweep_once()

    assert alerts["n"] == 1
    assert report.stale_withdrawals_alerted == 2
    # The hygiene third reports nothing rather than lying about it.
    assert report.inventory_deleted == 0
    assert report.privileges_deleted == 0
    assert report.game_plays_deleted == 0
    assert report.checks_deactivated == 0
    assert report.vip_notices_sent == 0


async def test_a_failing_money_step_still_ends_the_pass(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of #1706: only HYGIENE gets the new guard.

    #268 split the pass into three transactions precisely because the
    money steps refund real coins, and #1512 then arranged for a failed
    pass to retry on the next money tick instead of an hour later. Both
    of those still depend on a money fault propagating out of
    ``sweep_once``. If the new guard ever widens to cover the whole
    pass, a refund that cannot be written would be swallowed AND would
    consume the hygiene slot, and this test is what says so.
    """
    sweeper = EconomyCleanupSweeper(registry, clock=lambda: _NOW)

    async def _sweep_money() -> MoneySweepReport:
        msg = "database is locked"
        raise RuntimeError(msg)

    monkeypatch.setattr(sweeper, "sweep_money", _sweep_money)

    with pytest.raises(RuntimeError, match="database is locked"):
        await sweeper.sweep_once()
