"""#169: the hourly sweep tells the owner when a payout queue stops moving.

Payouts are manual (``/admin_withdrawals`` — the operator pays out
externally, then presses ✅), so a ``pending`` row is work assigned to a
human. Production carried one for five months with no signal anywhere.
These pin the push half of the fix:

* only requests older than the threshold are reported, and the alert
  goes to ``admin_chat_id`` — nobody else;
* each request is reported ONCE: a second pass over the same requests
  is silent, which is what keeps a repeating job from training the owner
  to mute it — but a backlog deeper than the sample keeps advancing
  through its tail rather than falling silent (#208);
* silence survives a RESTART (#1518): the ledger is
  ``withdrawal_requests.alerted_at``, not a set on the instance, so a
  deploy no longer re-sends the whole backlog on the next minute tick;
* a request handed back to the queue by ``release_processing`` earns a
  fresh alert — that is the one path back into ``pending``, and it
  clears the stamp;
* a failed DM does NOT mark the ids, so the next pass retries;
* no ``admin_chat_id`` (or no bot) → the step is skipped entirely while
  every reaping job still runs;
* ``payment_details`` never appears in the alert body.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import EconomyBase
from telegram_invite_bot.db.models.economy import WithdrawalRequest
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.repositories.withdrawals_repo import WithdrawalsRepo
from telegram_invite_bot.scheduler.economy_cleanup import EconomyCleanupSweeper
from telegram_invite_bot.utils.time import db_now

_OWNER = 123456789
_LOCAL_NOW = datetime(2026, 6, 13, 12, 0, 0)


class _FakeBot:
    """Captures DMs; optionally raises on every send."""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
        if self.fail:
            msg = "Forbidden: bot was blocked by the user"
            raise RuntimeError(msg)
        self.sent.append((chat_id, text))


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


def _iso(delta: timedelta) -> str:
    """``created_at`` text ``delta`` in the past, in the naive-UTC frame
    the write side stamps (NOT the sweeper's naive-local clock)."""
    return (db_now() - delta).isoformat(sep=" ", timespec="seconds")


def _request(
    user_id: int,
    *,
    age: timedelta,
    status: str = "pending",
    amount_com: int = 100,
    payment_details: str | None = None,
) -> WithdrawalRequest:
    return WithdrawalRequest(
        user_id=user_id,
        amount_com=amount_com,
        amount_crypto=0.0,
        currency="RUB",
        payment_method="card_rub",
        payment_details=payment_details,
        status=status,
        created_at=_iso(age),
    )


def _sweeper(
    registry: EngineRegistry, bot: _FakeBot | None, *, admin_chat_id: int = _OWNER
) -> EconomyCleanupSweeper:
    return EconomyCleanupSweeper(
        registry,
        clock=lambda: _LOCAL_NOW,
        bot=bot,  # type: ignore[arg-type]  # duck-typed fake; only send_message is used
        admin_chat_id=admin_chat_id,
    )


async def test_reports_only_overdue_requests_and_only_to_the_owner(
    registry: EngineRegistry,
) -> None:
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_request(1, age=timedelta(days=5)))  # overdue
        s.add(_request(2, age=timedelta(hours=2)))  # fresh
        s.add(_request(3, age=timedelta(days=9), status="completed"))  # not pending
    bot = _FakeBot()

    report = await _sweeper(registry, bot).sweep_once()

    assert report.stale_withdrawals_alerted == 1
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == _OWNER
    assert "#1" in text
    # The fresh row and the completed one must not be advertised as work.
    assert "#2" not in text
    assert "#3" not in text
    assert "/admin_withdrawals" in text


async def test_each_request_is_reported_once(registry: EngineRegistry) -> None:
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_request(1, age=timedelta(days=5)))
    bot = _FakeBot()
    sweeper = _sweeper(registry, bot)

    first = await sweeper.sweep_once()
    second = await sweeper.sweep_once()

    assert first.stale_withdrawals_alerted == 1
    # An hourly job that re-alarms every hour gets muted; silence on the
    # second pass IS the feature.
    assert second.stale_withdrawals_alerted == 0
    assert len(bot.sent) == 1


async def test_a_newly_overdue_request_is_reported_on_a_later_pass(
    registry: EngineRegistry,
) -> None:
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_request(1, age=timedelta(days=5)))
        s.add(_request(2, age=timedelta(hours=2)))
    bot = _FakeBot()
    sweeper = _sweeper(registry, bot)
    await sweeper.sweep_once()

    # Request #2 crosses the threshold between passes.
    async with session_for(registry, DBName.ECONOMY) as s:
        await s.execute(
            update(WithdrawalRequest)
            .where(WithdrawalRequest.user_id == 2)
            .values(created_at=_iso(timedelta(days=2)))
        )

    report = await sweeper.sweep_once()

    assert report.stale_withdrawals_alerted == 1
    assert len(bot.sent) == 2
    assert "#2" in bot.sent[1][1]
    # Already-reported #1 is not repeated in the body.
    assert "#1" not in bot.sent[1][1]


async def test_silence_survives_a_restart(registry: EngineRegistry) -> None:
    """#1518: the ledger is a column, so a deploy does not re-alarm.

    The alert rides the 60-second money tick since #281 and the unit
    carries ``Restart=always``, so "reported ids" held on the instance
    were emptied far more often than the queue drained. A second
    sweeper over the same database is exactly what a restart looks like
    from the data's side.
    """
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_request(1, age=timedelta(days=5)))
        s.add(_request(2, age=timedelta(days=4)))
    first_bot = _FakeBot()

    assert (await _sweeper(registry, first_bot).sweep_once()).stale_withdrawals_alerted == 2
    assert len(first_bot.sent) == 1

    restarted_bot = _FakeBot()
    report = await _sweeper(registry, restarted_bot).sweep_once()

    assert report.stale_withdrawals_alerted == 0
    assert restarted_bot.sent == []


async def test_a_released_request_earns_a_fresh_alert(registry: EngineRegistry) -> None:
    """``release_processing`` is the one path back into ``pending``.

    The provider definitively refused the payout, so the request is
    actionable again — for a NEW reason. A stamp left over from the DM
    that named it before the attempt would suppress the alert forever,
    which is the durable-ledger version of exactly the silence #169
    exists to prevent.
    """
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_request(1, age=timedelta(days=5)))
    bot = _FakeBot()
    sweeper = _sweeper(registry, bot)
    await sweeper.sweep_once()
    assert (await sweeper.sweep_once()).stale_withdrawals_alerted == 0

    async with session_for(registry, DBName.ECONOMY) as s:
        repo = WithdrawalsRepo(s)
        assert await repo.claim_processing(1, processed_by=_OWNER)
        assert await repo.release_processing(1)

    report = await sweeper.sweep_once()

    assert report.stale_withdrawals_alerted == 1
    assert len(bot.sent) == 2
    assert "#1" in bot.sent[1][1]


async def test_a_terminal_request_keeps_its_stamp(registry: EngineRegistry) -> None:
    """Completed and rejected rows leave the ``pending`` slice the alert
    reads, so clearing their stamp would buy nothing — and a hand-edited
    flip back to ``pending`` is not a path the code takes."""
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_request(1, age=timedelta(days=5)))
    bot = _FakeBot()
    sweeper = _sweeper(registry, bot)
    await sweeper.sweep_once()

    async with session_for(registry, DBName.ECONOMY) as s:
        await s.execute(
            update(WithdrawalRequest)
            .where(WithdrawalRequest.user_id == 1)
            .values(status="completed")
        )

    assert (await sweeper.sweep_once()).stale_withdrawals_alerted == 0
    assert len(bot.sent) == 1


async def test_failed_dm_is_retried_next_pass(registry: EngineRegistry) -> None:
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_request(1, age=timedelta(days=5)))
    failing = _FakeBot(fail=True)
    sweeper = _sweeper(registry, failing)

    report = await sweeper.sweep_once()

    assert report.stale_withdrawals_alerted == 0
    # Nothing was marked, so a working bot on the next pass still reports.
    working = _FakeBot()
    sweeper._bot = working  # type: ignore[assignment]  # noqa: SLF001 — swap the transport mid-test
    assert (await sweeper.sweep_once()).stale_withdrawals_alerted == 1


async def test_alert_never_carries_payment_details(registry: EngineRegistry) -> None:
    """A push alert can be forwarded or screenshotted; the card behind the
    private-only router is where payout details belong."""
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_request(1, age=timedelta(days=5), payment_details="4111111111111111"))
    bot = _FakeBot()

    await _sweeper(registry, bot).sweep_once()

    assert "4111111111111111" not in bot.sent[0][1]


@pytest.mark.parametrize("admin_chat_id", [0, -1])
async def test_no_recipient_skips_the_step(registry: EngineRegistry, admin_chat_id: int) -> None:
    """``0`` is the settings default when ADMIN_CHAT_ID is unset. Only
    ``0`` disables; a negative id is a legitimate group chat."""
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_request(1, age=timedelta(days=5)))
    bot = _FakeBot()

    report = await _sweeper(registry, bot, admin_chat_id=admin_chat_id).sweep_once()

    if admin_chat_id == 0:
        assert report.stale_withdrawals_alerted == 0
        assert bot.sent == []
    else:
        assert report.stale_withdrawals_alerted == 1
        assert bot.sent[0][0] == -1


async def test_no_bot_degrades_to_a_noop(registry: EngineRegistry) -> None:
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_request(1, age=timedelta(days=5)))

    report = await _sweeper(registry, None).sweep_once()

    assert report.stale_withdrawals_alerted == 0


async def test_deep_backlog_names_the_oldest_and_totals_the_rest(
    registry: EngineRegistry,
) -> None:
    async with session_for(registry, DBName.ECONOMY) as s:
        for i in range(1, 9):
            s.add(_request(i, age=timedelta(days=10 - i)))
    bot = _FakeBot()
    sweeper = _sweeper(registry, bot)

    report = await sweeper.sweep_once()

    # Only the sample is named; the header total tells the truth about
    # the queue, and the second count line stops the 5-bullet list from
    # reading as "that is all of them".
    assert report.stale_withdrawals_alerted == 5
    text = bot.sent[0][1]
    assert "Просрочено (дольше 24 ч): <b>8</b>" in text
    assert "Новые в этой сводке: <b>5</b>" in text
    assert "#5" in text
    assert "#6" not in text

    # #208 — the half that used to be missing. NOTHING is resolved
    # between the passes: the owner has not touched the queue, which is
    # exactly the state the alarm exists to escalate. The next pass must
    # walk past the stuck head and name the tail. The old code pruned the
    # ledger against the five-row SAMPLE, so those five ids could never
    # leave it, the sample never advanced, and #6-#8 were unreportable
    # forever — silence that reads identically to a clean queue.
    second = await sweeper.sweep_once()

    assert second.stale_withdrawals_alerted == 3
    tail = bot.sent[1][1]
    assert "Просрочено (дольше 24 ч): <b>8</b>" in tail
    assert "Новые в этой сводке: <b>3</b>" in tail
    for named in ("#6", "#7", "#8"):
        assert named in tail
    # The head is not repeated — advancing must not mean re-alarming.
    assert "#1" not in tail

    # And once the whole queue has been named, it goes quiet again.
    assert (await sweeper.sweep_once()).stale_withdrawals_alerted == 0
    assert len(bot.sent) == 2


async def test_single_overdue_request_has_no_redundant_count_line(
    registry: EngineRegistry,
) -> None:
    """The common case is one request going overdue; the header total
    already says ``1``, so a second ``new: 1`` line is pure noise."""
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_request(1, age=timedelta(days=5)))
    bot = _FakeBot()

    await _sweeper(registry, bot).sweep_once()

    assert "Новые в этой сводке" not in bot.sent[0][1]


async def test_alert_runs_on_the_money_tick_not_the_hygiene_interval(
    registry: EngineRegistry,
) -> None:
    """#281: the scan must not be gated behind an hour of uptime.

    ``run`` sleeps a money tick, then either does a full hygiene pass or
    the money-only branch. The alert has to fire on BOTH — otherwise its
    firing depends on nobody having deployed in the last hour, and the
    journal says the hygiene pass has logged work three times in nine
    days. Calling :meth:`sweep_alerts` directly is the honest test of
    that: it is the method both branches call.
    """
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_request(1, age=timedelta(days=5)))
    bot = _FakeBot()
    sweeper = _sweeper(registry, bot)

    assert await sweeper.sweep_alerts() == 1
    assert len(bot.sent) == 1
    assert "#1" in bot.sent[0][1]
    # Same ledger either way: a hygiene pass right after must not repeat
    # what the money tick already reported.
    assert (await sweeper.sweep_once()).stale_withdrawals_alerted == 0
    assert len(bot.sent) == 1
