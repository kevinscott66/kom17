"""L-95 sweep additions: VIP expiry-notice DMs + expired-check deactivation.

Same throwaway-economy-DB harness as ``test_economy_cleanup.py``; here we
seed VIP grants around the notice window and expired/live checks, run
``sweep_once`` and assert:

* exactly the in-window, not-yet-notified grants are DM'd (legacy
  ``maybe_notify_vip_expiring``, bot.py:6957, moved onto the sweep);
* the once-per-grant mark is durable (second sweep DMs nobody) but
  re-arms when the grant is extended (``vip_till`` changes);
* a failed DM leaves the user eligible for the next pass;
* the fan-out is capped at ``vip_notice_budget`` per pass and the cap
  keeps the NEAREST deadlines, with the remainder carried to the next
  pass (#1617);
* without a bot the notice step degrades to a no-op while every reaping
  job still runs;
* expired-but-active checks flip ``is_active`` to 0 (legacy only did
  this lazily at claim time, bot.py:10110-10117) with NO refund;
* the fan-out also obeys a WALL-CLOCK budget, and one honoured flood
  wait ends the pass rather than being repeated per candidate (#1486);
* the DM is rendered in the ``/lang`` choice from ``users.db`` when one
  exists, falling back to the ``economy.users.language`` creation stamp
  otherwise — including when the registry carries no users database at
  all (#1511).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from telegram_invite_bot.db.engines import EngineRegistry
from telegram_invite_bot.db.models.base import EconomyBase, UsersBase
from telegram_invite_bot.db.models.economy import Check, EconomyUser
from telegram_invite_bot.db.models.user_settings import UserSetting
from telegram_invite_bot.db.models.users import User
from telegram_invite_bot.db.names import DBName
from telegram_invite_bot.db.session import session_for
from telegram_invite_bot.scheduler import economy_cleanup as ec_mod
from telegram_invite_bot.scheduler.economy_cleanup import EconomyCleanupSweeper

_NOW = datetime(2026, 6, 13, 12, 0, 0)
_NOW_TS = _NOW.timestamp()
_DB_NOW = datetime.now(UTC).replace(tzinfo=None)


class _FakeBot:
    """Captures DMs; optionally fails for selected user ids."""

    def __init__(self, fail_for: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail_for = fail_for or set()

    async def send_message(self, chat_id: int, text: str, **_: Any) -> None:
        if chat_id in self.fail_for:
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


@pytest.fixture
async def dual_registry(tmp_path: Path) -> AsyncIterator[EngineRegistry]:
    """Economy AND users, for the #1511 language-resolution path.

    The plain ``registry`` fixture above deliberately stays economy-only,
    so every other case in this file also exercises the degraded branch:
    ``EngineRegistry.session`` is a bare dict lookup, so asking it for
    ``users.db`` raises ``KeyError`` and the sweeper must fall back to
    the wallet column instead of dropping the notice.
    """
    econ = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'economy.db'}")
    users = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'users.db'}")
    async with econ.begin() as conn:
        await conn.run_sync(EconomyBase.metadata.create_all)
    async with users.begin() as conn:
        await conn.run_sync(UsersBase.metadata.create_all)
    reg = EngineRegistry(
        engines={DBName.ECONOMY: econ, DBName.USERS: users},
        sessions={
            DBName.ECONOMY: async_sessionmaker(econ, expire_on_commit=False),
            DBName.USERS: async_sessionmaker(users, expire_on_commit=False),
        },
    )
    try:
        yield reg
    finally:
        await econ.dispose()
        await users.dispose()


class _FakeMonotonic:
    """Stand-in for the ``time`` module inside the sweeper.

    The fan-out deadline reads ``time.monotonic()`` while the waits it is
    supposed to bound go through ``asyncio.sleep``, which these tests
    stub out — so without a fake clock no test time ever passes and the
    deadline could never be reached. Advancing this from the sleep stub
    keeps the two consistent and the assertions deterministic.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


def _user(user_id: int, vip_till: float | None, *, language: str = "ru") -> EconomyUser:
    return EconomyUser(user_id=user_id, balance=0, language=language, vip_till=vip_till)


def _check(code: str, *, expires_at: datetime | None, is_active: int = 1) -> Check:
    return Check(
        code=code,
        creator_id=1,
        type="fixed",
        total_amount=100,
        remaining_amount=100,
        fixed_amount=10,
        max_claims=0,
        claims_count=0,
        required_premium=0,
        required_subscription=0,
        expires_at=expires_at,
        created_at=_DB_NOW - timedelta(days=10),
        is_active=is_active,
    )


def _sweeper(
    registry: EngineRegistry,
    bot: _FakeBot | None,
    *,
    vip_notice_budget: int = ec_mod._VIP_NOTICE_BUDGET_PER_PASS,
    vip_fanout_budget_seconds: float = ec_mod._VIP_FANOUT_BUDGET_SECONDS,
) -> EconomyCleanupSweeper:
    return EconomyCleanupSweeper(
        registry,
        clock=lambda: _NOW,
        bot=bot,  # type: ignore[arg-type]  # duck-typed fake; only send_message is used
        vip_notice_budget=vip_notice_budget,
        vip_fanout_budget_seconds=vip_fanout_budget_seconds,
    )


async def test_vip_notice_targets_only_window(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stub the translator so the assertion pins the ARGUMENTS the sweep
    # passes (lang + hours) rather than the ``h_vip_expiring_soon`` copy —
    # the wording is free to change, the hours the user is told are not.
    import telegram_invite_bot.scheduler.economy_cleanup as mod

    monkeypatch.setattr(mod, "t", lambda key, lang=None, **kw: f"{key}|{lang}|{kw.get('hours')}")
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_user(1, _NOW_TS + 3600))  # 1h left → in window, notified
        s.add(_user(2, _NOW_TS + 2 * 86400, language="en"))  # 2d left → in window
        s.add(_user(3, _NOW_TS + 10 * 86400))  # 10d left → outside window
        s.add(_user(4, _NOW_TS - 60))  # already expired → no notice
        s.add(_user(5, None))  # never VIP

    bot = _FakeBot()
    report = await _sweeper(registry, bot).sweep_once()

    assert report.vip_notices_sent == 2
    assert sorted(uid for uid, _ in bot.sent) == [1, 2]
    # Hours-left math matches legacy (max(1, int(left/3600)), bot.py:6974)
    # and the DM renders in the user's STORED language, never
    # tg_user.language_code (no Telegram user exists in a sweep).
    by_uid = dict(bot.sent)
    assert by_uid[1] == "h_vip_expiring_soon|ru|1"
    assert by_uid[2] == "h_vip_expiring_soon|en|48"


async def test_vip_notice_once_per_grant_and_rearm(registry: EngineRegistry) -> None:
    till = _NOW_TS + 3600
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_user(1, till))

    bot = _FakeBot()
    sweeper = _sweeper(registry, bot)
    assert (await sweeper.sweep_once()).vip_notices_sent == 1
    # Second pass: durable mark suppresses a repeat DM.
    assert (await sweeper.sweep_once()).vip_notices_sent == 0
    assert len(bot.sent) == 1

    # Extension re-arms: a NEW deadline (still in window) notifies again.
    async with session_for(registry, DBName.ECONOMY) as s:
        user = (await s.execute(select(EconomyUser).where(EconomyUser.user_id == 1))).scalar_one()
        user.vip_till = _NOW_TS + 7200
    assert (await sweeper.sweep_once()).vip_notices_sent == 1
    assert len(bot.sent) == 2


async def test_vip_notice_failed_dm_retries_next_pass(registry: EngineRegistry) -> None:
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_user(7, _NOW_TS + 3600))

    failing = _FakeBot(fail_for={7})
    sweeper = _sweeper(registry, failing)
    report = await sweeper.sweep_once()
    assert report.vip_notices_sent == 0  # DM failed → NOT marked

    # Next pass with a working bot delivers (the mark never landed).
    working = _FakeBot()
    report = await _sweeper(registry, working).sweep_once()
    assert report.vip_notices_sent == 1
    assert working.sent[0][0] == 7


async def test_vip_notice_budget_caps_a_pass_nearest_deadlines_first(
    registry: EngineRegistry,
) -> None:
    """#1617: one pass sends at most ``vip_notice_budget`` DMs.

    The cap is only safe because the query orders by ``vip_till``
    ascending, so a truncated pass drops the grants a late notice
    harms least. Both halves are asserted here: the COUNT and WHICH.
    """
    async with session_for(registry, DBName.ECONOMY) as s:
        # ``user_id`` IS the rowid, so ids ascend while deadlines
        # DESCEND: a plain table scan would hand back the furthest
        # deadlines first. Seeded this way on purpose — with the ids
        # and the deadlines in the same order the assertions below
        # would hold even without the ``ORDER BY``, and guard nothing.
        for uid in range(1, 6):
            s.add(_user(uid, _NOW_TS + (6 - uid) * 3600))  # 5h..1h left

    bot = _FakeBot()
    sweeper = _sweeper(registry, bot, vip_notice_budget=2)

    assert (await sweeper.sweep_once()).vip_notices_sent == 2
    assert sorted(uid for uid, _ in bot.sent) == [4, 5]  # 1h and 2h left

    # Nothing is dropped, only deferred: the durable mark retires the
    # first two, so the next pass takes the next two.
    assert (await sweeper.sweep_once()).vip_notices_sent == 2
    assert sorted(uid for uid, _ in bot.sent) == [2, 3, 4, 5]
    assert (await sweeper.sweep_once()).vip_notices_sent == 1
    assert sorted(uid for uid, _ in bot.sent) == [1, 2, 3, 4, 5]
    assert (await sweeper.sweep_once()).vip_notices_sent == 0


async def test_vip_notice_budget_slot_is_held_by_a_failing_dm(
    registry: EngineRegistry,
) -> None:
    """#1617: the documented cost of the cap, pinned rather than hidden.

    A candidate whose DM keeps failing is never marked, so it keeps its
    slot at the head of the ``vip_till ASC`` scan and a later grant
    waits. Bounded — the row leaves the window for good once its
    ``vip_till`` goes by — but real, and the docstring on
    ``VipRepo.list_expiring_global`` says so.
    """
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_user(1, _NOW_TS + 3600))  # nearest deadline, DM always fails
        s.add(_user(2, _NOW_TS + 7200))

    bot = _FakeBot(fail_for={1})
    assert (await _sweeper(registry, bot, vip_notice_budget=1).sweep_once()).vip_notices_sent == 0
    assert bot.sent == []  # user 2 was never a candidate this pass


async def test_no_bot_skips_notice_but_reaps(registry: EngineRegistry) -> None:
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_user(1, _NOW_TS + 3600))
        s.add(_check("EXP1", expires_at=_DB_NOW - timedelta(hours=1)))

    report = await EconomyCleanupSweeper(registry, clock=lambda: _NOW).sweep_once()
    assert report.vip_notices_sent == 0
    assert report.checks_deactivated == 1  # reaping still runs without a bot


async def test_expired_checks_deactivated_no_refund(registry: EngineRegistry) -> None:
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_check("DEAD", expires_at=_DB_NOW - timedelta(hours=1)))  # expired → flipped
        s.add(_check("LIVE", expires_at=_DB_NOW + timedelta(days=1)))  # future → kept
        s.add(_check("OPEN", expires_at=None))  # no deadline → kept
        s.add(_check("GONE", expires_at=_DB_NOW - timedelta(days=2), is_active=0))  # already off

    report = await _sweeper(registry, _FakeBot()).sweep_once()
    assert report.checks_deactivated == 1

    async with session_for(registry, DBName.ECONOMY) as s:
        rows = {c.code: c for c in (await s.execute(select(Check))).scalars()}
    assert rows["DEAD"].is_active == 0
    # No refund by design: legacy never credits back an expired check's
    # remainder (bot.py:10110-10117 just flips is_active) — no credit()
    # call exists here at all, so no money-guard marker is needed.
    assert rows["DEAD"].remaining_amount == 100
    assert rows["LIVE"].is_active == 1
    assert rows["OPEN"].is_active == 1


async def test_flood_wait_is_slept_through_and_retried_once(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 429 must cost a pause, not the notice.

    Before the fix ``TelegramRetryAfter`` fell into the generic handler:
    the candidate was skipped, the very next send hit the same wait, and
    one flood dropped the whole batch for the hour.
    """
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_user(1, _NOW_TS + 3600))
        s.add(_user(2, _NOW_TS + 3600))

    slept: list[float] = []

    async def _record_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(ec_mod.asyncio, "sleep", _record_sleep)

    bot = _FakeBot()
    flooded = {1}
    original = bot.send_message

    async def _send_message(chat_id: int, text: str, **kw: Any) -> None:
        if chat_id in flooded:
            flooded.discard(chat_id)
            raise TelegramRetryAfter(
                method="sendMessage",  # type: ignore[arg-type]
                message="Too Many Requests",
                retry_after=7,
            )
        await original(chat_id, text, **kw)

    monkeypatch.setattr(bot, "send_message", _send_message)

    report = await _sweeper(registry, bot).sweep_once()
    assert report.vip_notices_sent == 2
    assert sorted(uid for uid, _ in bot.sent) == [1, 2]
    # The honoured wait, plus the inter-send pacing pause before user 2.
    assert 7 in slept
    assert ec_mod._SEND_PAUSE_SECONDS in slept  # noqa: SLF001


async def test_flood_wait_beyond_the_cap_is_not_slept_through(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An hourly sweep must not block on a multi-minute penalty."""
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_user(1, _NOW_TS + 3600))

    slept: list[float] = []

    async def _record_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(ec_mod.asyncio, "sleep", _record_sleep)

    bot = _FakeBot()

    async def _always_flooded(chat_id: int, text: str, **_: Any) -> None:
        raise TelegramRetryAfter(
            method="sendMessage",  # type: ignore[arg-type]
            message="Too Many Requests",
            retry_after=3600,
        )

    monkeypatch.setattr(bot, "send_message", _always_flooded)

    report = await _sweeper(registry, bot).sweep_once()
    # Retry bounced too → unmarked, so the next pass tries again.
    assert report.vip_notices_sent == 0
    assert slept == [ec_mod._MAX_RETRY_AFTER_SECONDS]  # noqa: SLF001

    working = _FakeBot()
    assert (await _sweeper(registry, working).sweep_once()).vip_notices_sent == 1


async def test_zero_fanout_budget_sends_nothing_and_carries_over(registry: EngineRegistry) -> None:
    """#1486: a spent wall-clock budget stops the fan-out, it does not lose it.

    Nothing is DMed and therefore nothing is marked, so the very next
    pass — with the normal budget — delivers the whole batch. That is
    what makes stopping early safe: the durable ``vip_notified_till``
    mark is written per DELIVERED notice, never per candidate examined.
    """
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_user(1, _NOW_TS + 3600))
        s.add(_user(2, _NOW_TS + 7200))

    starved = _FakeBot()
    report = await _sweeper(registry, starved, vip_fanout_budget_seconds=0.0).sweep_once()
    assert report.vip_notices_sent == 0
    assert starved.sent == []

    working = _FakeBot()
    assert (await _sweeper(registry, working).sweep_once()).vip_notices_sent == 2


async def test_one_honoured_flood_wait_ends_the_fan_out(
    registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1486: the wait is honoured once, then the deadline stops the pass.

    The wait itself is NOT declined — it is bounded by
    ``_MAX_RETRY_AFTER_SECONDS`` and honouring it is why that cap exists.
    But sleeping it necessarily blows the fan-out deadline, so the next
    candidate is never even attempted. Worst case is therefore budget +
    one cap, not one cap per candidate.
    """
    async with session_for(registry, DBName.ECONOMY) as s:
        s.add(_user(1, _NOW_TS + 3600))
        s.add(_user(2, _NOW_TS + 7200))

    clock = _FakeMonotonic()
    monkeypatch.setattr(ec_mod, "time", clock)

    slept: list[float] = []

    async def _record_sleep(delay: float) -> None:
        slept.append(delay)
        clock.now += delay

    # ``ec_mod.asyncio`` IS the asyncio module, so patching it here is
    # the same global patch the older cases make through the module
    # attribute — spelt directly because the indirect spelling trips
    # mypy's implicit-reexport check.
    monkeypatch.setattr(asyncio, "sleep", _record_sleep)

    bot = _FakeBot()
    attempts: list[int] = []
    deliver = bot.send_message

    async def _flood_the_first_attempt(chat_id: int, text: str, **kw: Any) -> None:
        attempts.append(chat_id)
        if len(attempts) == 1:
            raise TelegramRetryAfter(
                method="sendMessage",  # type: ignore[arg-type]
                message="Too Many Requests",
                retry_after=3600,
            )
        await deliver(chat_id, text, **kw)

    monkeypatch.setattr(bot, "send_message", _flood_the_first_attempt)

    report = await _sweeper(registry, bot).sweep_once()
    assert report.vip_notices_sent == 1
    # Two attempts, both for user 1: the flood, then the single retry.
    assert attempts == [1, 1]
    assert slept[0] == ec_mod._MAX_RETRY_AFTER_SECONDS  # noqa: SLF001
    assert [uid for uid, _ in bot.sent] == [1]

    # User 2 was never marked, so the next pass picks them up.
    working = _FakeBot()
    assert (await _sweeper(registry, working).sweep_once()).vip_notices_sent == 1
    assert [uid for uid, _ in working.sent] == [2]


async def test_vip_notice_follows_the_lang_choice_not_the_wallet_stamp(
    dual_registry: EngineRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1511: ``/lang`` wins over the wallet's creation-time stamp.

    ``economy.users.language`` is written once by
    ``EconomyRepo.get_or_create`` and never refreshed, while ``/lang``
    writes ``users.user_settings.language`` in a different database. User
    1 has both — the override must win. User 2 has no ``users.db`` rows
    at all, which is the last-resort leg: the wallet column.
    """

    def _stub_t(key: str, lang: str | None = None, **kw: Any) -> str:
        return f"{key}|{lang}|{kw.get('hours')}"

    monkeypatch.setattr(ec_mod, "t", _stub_t)
    async with session_for(dual_registry, DBName.ECONOMY) as s:
        s.add(_user(1, _NOW_TS + 3600, language="ru"))
        s.add(_user(2, _NOW_TS + 7200, language="ru"))
    async with session_for(dual_registry, DBName.USERS) as s:
        # Parent row first: user_settings.user_id carries an FK to users.
        s.add(User(user_id=1))
        await s.flush()
        s.add(UserSetting(user_id=1, language="en"))

    bot = _FakeBot()
    assert (await _sweeper(dual_registry, bot).sweep_once()).vip_notices_sent == 2
    assert bot.sent == [
        (1, "h_vip_expiring_soon|en|1"),
        (2, "h_vip_expiring_soon|ru|2"),
    ]
