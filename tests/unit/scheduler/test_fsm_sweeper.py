"""Unit tests for :class:`FsmTimeoutSweeper` (Stage 35).

The matrix here pins the sweeper's pure-function core
(``sweep_once``) without touching the production handlers' on_expire
callbacks — those are exercised by the e2e tests in
``tests/e2e/handlers/test_rps.py``. The trade is intentional: this
file tests the SCHEDULER contract (deadline math, error handling,
storage-key iteration); the e2e file tests the wiring against the
real /cpc flow.

A frozen ``clock`` is injected at construction so every test pins
the wall clock at a known instant — no ``freezegun``, no
``time.sleep``, and the deadline branch (age < timeout vs >= timeout)
is exact rather than racy.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Collection
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiogram.exceptions import TelegramMigrateToChat, TelegramRetryAfter
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import SendMessage

from telegram_invite_bot.fsm.rps import RpsStates
from telegram_invite_bot.scheduler import (
    FsmTimeoutSweeper,
    SweepReport,
    TimeoutRule,
)
from telegram_invite_bot.scheduler import fsm_sweeper as fs_mod
from telegram_invite_bot.scheduler.fsm_sweeper import STATE_ENTERED_AT_FIELD, memory_storage_keys

_NOW = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)


def _frozen_clock() -> datetime:
    return _NOW


def _key(user_id: int) -> StorageKey:
    return StorageKey(bot_id=42, chat_id=user_id, user_id=user_id)


# The key every #126 test below seeds; hoisted so a guard closure can
# name it without threading it through the rule.
_KEY_100 = _key(100)


async def _seed(
    storage: MemoryStorage,
    *,
    user_id: int,
    state: str | None,
    entered_at: datetime,
    extra: dict[str, Any] | None = None,
) -> None:
    key = _key(user_id)
    await storage.set_state(key, state)
    data: dict[str, Any] = {STATE_ENTERED_AT_FIELD: entered_at.isoformat()}
    if extra:
        data.update(extra)
    await storage.set_data(key, data)


@pytest.fixture
def bot_stub() -> MagicMock:
    """Stand-in for :class:`Bot` — the sweeper passes it straight to
    on_expire callbacks and never calls anything on it directly, so a
    bare ``MagicMock`` is enough for the scheduler layer."""
    return MagicMock(name="bot")


async def test_sweep_fresh_state_not_expired(bot_stub: MagicMock) -> None:
    """A state entered 1s ago with a 60s budget stays put. Pins the
    'age < timeout' branch — without this, a too-eager sweeper would
    nuke healthy in-flight matches."""
    storage = MemoryStorage()
    on_expire_calls: list[StorageKey] = []

    async def on_expire(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        on_expire_calls.append(key)

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=1),
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=0, errors=0)
    assert on_expire_calls == []
    assert await storage.get_state(_key(100)) == RpsStates.awaiting_acceptance.state


async def test_sweep_stale_state_expires_clears_and_calls_callback(
    bot_stub: MagicMock,
) -> None:
    """A state entered well past the timeout is cleared AND the
    callback is invoked with the original data. Pins the post-callback
    clear ordering — callback sees data, then sweeper wipes FSM."""
    storage = MemoryStorage()
    captured: list[tuple[StorageKey, dict[str, Any]]] = []

    async def on_expire(_bot: Any, key: StorageKey, data: dict[str, Any]) -> None:
        captured.append((key, data))

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
        extra={"opponent_id": 200, "bet": 50},
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=1, errors=0)
    assert len(captured) == 1
    cb_key, cb_data = captured[0]
    assert cb_key == _key(100)
    # Callback sees the FULL data dict (incl. opponent_id/bet) — the
    # clear happens AFTER the callback returns.
    assert cb_data["opponent_id"] == 200
    assert cb_data["bet"] == 50
    # Post-callback state is empty.
    assert await storage.get_state(_key(100)) is None
    assert await storage.get_data(_key(100)) == {}


async def test_sweep_state_not_in_rules_is_ignored(bot_stub: MagicMock) -> None:
    """A state outside the rules table is left alone, even if its
    state_entered_at is ancient. Pins isolation: a future FSM flow
    that the sweeper doesn't know about doesn't get nuked by
    accident."""
    storage = MemoryStorage()

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        raise AssertionError("should not be called for unrelated state")

    await storage.set_state(_key(100), "SomeOtherStatesGroup:weird")
    await storage.set_data(
        _key(100), {STATE_ENTERED_AT_FIELD: (_NOW - timedelta(days=1)).isoformat()}
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    # Not in rules → not scanned (we only count rule-covered keys).
    assert report == SweepReport(scanned=0, expired=0, errors=0)
    assert await storage.get_state(_key(100)) == "SomeOtherStatesGroup:weird"


async def test_sweep_multiple_expired_all_processed(bot_stub: MagicMock) -> None:
    """Three stale sessions in one pass → all three expire, callback
    called three times, report counts them. Pins that the loop
    doesn't bail on the first expiry."""
    storage = MemoryStorage()
    call_count = 0

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        nonlocal call_count
        call_count += 1

    for uid in (100, 200, 300):
        await _seed(
            storage,
            user_id=uid,
            state=RpsStates.awaiting_acceptance.state,
            entered_at=_NOW - timedelta(seconds=200),
        )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=3, expired=3, errors=0)
    assert call_count == 3
    for uid in (100, 200, 300):
        assert await storage.get_state(_key(uid)) is None


async def test_sweep_callback_raises_state_left_intact(bot_stub: MagicMock) -> None:
    """If the on_expire callback raises, the FSM state is NOT cleared
    — the next sweep gets to retry. The exception is swallowed so the
    rest of the pass continues. Pins a critical defensive posture
    (one bad rule should not nuke unrelated sessions or kill the loop)."""
    storage = MemoryStorage()
    raised = False

    async def bad_on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        nonlocal raised
        raised = True
        raise RuntimeError("simulated callback failure")

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=200),
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={
            RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=bad_on_expire)
        },
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert raised is True
    assert report == SweepReport(scanned=1, expired=0, errors=1)
    # State NOT cleared — retry on next pass.
    assert await storage.get_state(_key(100)) == RpsStates.awaiting_acceptance.state


async def test_sweep_missing_entered_at_skipped(bot_stub: MagicMock) -> None:
    """A state in the rules table but with no state_entered_at stamp
    is left alone (logged WARNING). Pins handler-contract: forgetting
    to stamp doesn't punish the user with an instant expiry."""
    storage = MemoryStorage()

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        raise AssertionError("should not fire without entered_at")

    await storage.set_state(_key(100), RpsStates.awaiting_acceptance.state)
    await storage.set_data(_key(100), {"opponent_id": 200, "bet": 10})
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=0, errors=0)
    assert await storage.get_state(_key(100)) == RpsStates.awaiting_acceptance.state


async def test_sweep_two_rules_each_with_own_timeout(bot_stub: MagicMock) -> None:
    """Two rules with different budgets: a key in awaiting_moves with
    age=45s is fresh under a 60s budget, while a key in
    awaiting_acceptance with age=45s is fresh under 60s too — but
    bumping moves' budget to 30 expires the second. Pins that each
    rule's timeout is evaluated independently."""
    storage = MemoryStorage()
    expired_states: list[str] = []

    async def on_expire_a(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        expired_states.append(f"a:{key.user_id}")

    async def on_expire_m(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        expired_states.append(f"m:{key.user_id}")

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=45),
    )
    await _seed(
        storage,
        user_id=200,
        state=RpsStates.awaiting_moves.state,
        entered_at=_NOW - timedelta(seconds=45),
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={
            RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire_a),
            RpsStates.awaiting_moves: TimeoutRule(timeout_seconds=30, on_expire=on_expire_m),
        },
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=2, expired=1, errors=0)
    assert expired_states == ["m:200"]
    # awaiting_acceptance one is still alive.
    assert await storage.get_state(_key(100)) == RpsStates.awaiting_acceptance.state


async def test_sweep_exact_boundary_expires(bot_stub: MagicMock) -> None:
    """Age == timeout exactly is considered expired (>= boundary).
    Pins the comparison: ``age < timeout`` survives, anything else
    expires. Keeps the user from a "stuck for forever-minus-one-tick"
    edge case."""
    storage = MemoryStorage()
    fired = False

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        nonlocal fired
        fired = True

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=60),
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert fired is True
    assert report == SweepReport(scanned=1, expired=1, errors=0)


class FakeStorage:
    """A backend the sweeper cannot walk: no ``iter_keys``, not memory."""

    async def get_state(self, _key: StorageKey) -> str | None:
        return None

    async def get_data(self, _key: StorageKey) -> dict[str, Any]:
        return {}

    async def set_state(self, _key: StorageKey, _state: Any) -> None:
        return None

    async def set_data(self, _key: StorageKey, _data: Any) -> None:
        return None

    async def close(self) -> None:
        return None


async def _noop_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
    return None


_NOOP_RULES = {
    RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=_noop_expire)
}


def test_unknown_storage_is_refused_at_construction() -> None:
    """#1449: an unwalkable backend must fail at BOOT, not every 30s.

    The reader's own ``TypeError`` used to be the only answer, and it
    is raised by the FIRST statement of ``sweep_once`` — so it landed
    in ``run``'s defensive ``except Exception`` and was retried on the
    next tick, and every tick after that, indefinitely. The task
    stayed alive and the health check stayed green while not one
    registered state ever expired: held /duel and /cpc stakes never
    returned, every busy-gated flow cross-blocked, and a full
    traceback in the journal twice a minute as the only signal. A
    backend the sweeper cannot walk is a configuration error, so the
    honest answer is a startup failure — ``start_background`` builds
    the sweeper before it spawns the task, and this raise propagates
    out of it.
    """
    with pytest.raises(TypeError, match="iterate FakeStorage"):
        FsmTimeoutSweeper(
            FakeStorage(),  # type: ignore[arg-type]
            MagicMock(),
            rules=_NOOP_RULES,
            clock=_frozen_clock,
        )


async def test_reader_still_refuses_a_storage_swapped_in_afterwards() -> None:
    """The reader keeps its own raise, and the two must agree.

    ``__init__``'s boot check and ``_iter_storage_keys``'s dispatch
    both go through the same supported-backend predicate, so the
    reader's ``TypeError`` is unreachable through the constructor.
    It stays anyway: the day those two disagree is the day ``run``
    goes back to swallowing this raise every thirty seconds, and this
    test is what notices.
    """
    sweeper = FsmTimeoutSweeper(
        MemoryStorage(),
        MagicMock(),
        rules=_NOOP_RULES,
        clock=_frozen_clock,
    )
    sweeper._storage = FakeStorage()  # type: ignore[assignment]

    with pytest.raises(TypeError, match="iterate FakeStorage"):
        await sweeper.sweep_once()


# M-G-5: per-state rules with single ``timeout_seconds`` field.


async def test_per_state_rule_awaiting_acceptance_expires_at_own_budget(
    bot_stub: MagicMock,
) -> None:
    """M-G-5: each State registers its own TimeoutRule with its own
    ``timeout_seconds``. 70s > 60s budget → expires."""
    storage = MemoryStorage()
    on_expire_calls: list[StorageKey] = []

    async def on_expire(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        on_expire_calls.append(key)

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=70),
    )

    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=1, errors=0)
    assert len(on_expire_calls) == 1


async def test_per_state_rule_awaiting_moves_independent_budget(
    bot_stub: MagicMock,
) -> None:
    """M-G-5: a state with a 30s budget expires at 40s independently of
    any sibling state's own budget — no state-name literals branching
    inside the rule."""
    storage = MemoryStorage()
    on_expire_calls: list[StorageKey] = []

    async def on_expire(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        on_expire_calls.append(key)

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_moves.state,
        entered_at=_NOW - timedelta(seconds=40),
    )

    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={
            RpsStates.awaiting_moves: TimeoutRule(timeout_seconds=30, on_expire=on_expire),
        },
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=1, errors=0)
    assert len(on_expire_calls) == 1


def test_timeout_rule_rejects_non_positive_timeout() -> None:
    """M-G-5: defensive — a ``timeout_seconds=0`` rule would expire
    every state on the first sweep, which we'd rather catch at boot."""

    async def _noop(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        return None

    with pytest.raises(ValueError, match="positive integer"):
        TimeoutRule(timeout_seconds=0, on_expire=_noop)
    with pytest.raises(ValueError, match="positive integer"):
        TimeoutRule(timeout_seconds=-5, on_expire=_noop)


# ── T-012: SQLiteStorage iteration ───────────────────────────────────


async def test_sweep_works_with_sqlite_storage(bot_stub: MagicMock, tmp_path: Any) -> None:
    """Sweeper's storage-agnostic ``_iter_storage_keys`` MUST also
    work against :class:`SQLiteStorage` (T-012 backend).

    The duck-typed ``iter_keys`` hook is the integration contract;
    breaking it would silently downgrade the persistent backend to
    "never expires anything", which is the same failure shape as
    "no sweeper at all" — invisible until a user complains about
    being stuck.
    """
    from telegram_invite_bot.fsm.sqlite_storage import SQLiteStorage

    storage = SQLiteStorage(tmp_path / "sweep.db")
    captured: list[StorageKey] = []

    async def on_expire(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        captured.append(key)

    key = StorageKey(bot_id=42, chat_id=500, user_id=500)
    await storage.set_state(key, RpsStates.awaiting_acceptance.state)
    await storage.set_data(
        key,
        {STATE_ENTERED_AT_FIELD: (_NOW - timedelta(seconds=120)).isoformat()},
    )

    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
    )
    report = await sweeper.sweep_once()
    await storage.close()

    assert report.expired == 1
    assert captured == [key]


# ── #126: guard + compare-and-clear ──────────────────────────────────
#
# The scheduler half of "a click and a deadline arrive together". The
# handler half (the per-match lock those two share) lives in
# tests/unit/handlers/test_rps_silent_paths.py and the e2e /cpc file.


@contextlib.asynccontextmanager
async def _recording_guard(
    log: list[str],
    *,
    on_enter: Callable[[], Awaitable[None]] | None = None,
    on_exit: Callable[[], Awaitable[None]] | None = None,
) -> AsyncIterator[None]:
    log.append("enter")
    if on_enter is not None:
        await on_enter()
    try:
        yield
    finally:
        if on_exit is not None:
            await on_exit()
        log.append("exit")


async def test_guard_wraps_the_whole_expiry(bot_stub: MagicMock) -> None:
    """Callback and clear both happen strictly INSIDE the guard.

    That containment is the entire fix: a rule whose guard is the
    flow's own per-match lock only serialises against clicks if the
    lock is still held while the sweeper wipes the state. So the
    callback must see live data (its ``data`` argument describes a match
    that still exists) and the guard's exit must see an empty key —
    anything a click does after that point reads a dead match, whichever
    lock it ends up on.
    """
    storage = MemoryStorage()
    events: list[str] = []

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        events.append("on_expire")
        # The clear happens after us — the callback always sees live data.
        assert await storage.get_state(_KEY_100) is not None

    async def at_exit() -> None:
        events.append("cleared" if await storage.get_state(_KEY_100) is None else "still-live")

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={
            RpsStates.awaiting_acceptance: TimeoutRule(
                timeout_seconds=60,
                on_expire=on_expire,
                guard=lambda _bot, _key: _recording_guard(events, on_exit=at_exit),
            )
        },
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=1, errors=0, raced=0)
    assert events == ["enter", "on_expire", "cleared", "exit"]


async def test_state_name_changed_under_the_guard_is_left_alone(bot_stub: MagicMock) -> None:
    """The accept landed a millisecond before the deadline.

    The guard blocks until the accept releases it, so by the time the
    sweeper gets in, the state it scanned is gone. Expiring anyway
    would delete a match both players just started — they'd be left
    clicking move buttons that answer "match not found" forever.

    #280: the name says ``state_name`` on purpose. This covers only
    the half of the hazard where the *state* moved on; the other half
    — same state, refreshed deadline — is
    :func:`test_deadline_refreshed_under_the_guard_is_left_alone`
    below. Read as coverage for the whole class, this one would let
    the deadline half regress unnoticed.
    """
    storage = MemoryStorage()
    events: list[str] = []

    async def accept_lands() -> None:
        await storage.set_state(_KEY_100, RpsStates.awaiting_moves.state)

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        events.append("on_expire")

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
        extra={"opponent_id": 200},
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={
            RpsStates.awaiting_acceptance: TimeoutRule(
                timeout_seconds=60,
                on_expire=on_expire,
                guard=lambda _bot, _key: _recording_guard(events, on_enter=accept_lands),
            )
        },
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=0, errors=0, raced=1)
    assert events == ["enter", "exit"]
    # The accepted match survives intact — state AND data.
    assert await storage.get_state(_KEY_100) == RpsStates.awaiting_moves.state
    assert (await storage.get_data(_KEY_100))["opponent_id"] == 200


async def test_rules_without_a_guard_still_sweep(bot_stub: MagicMock) -> None:
    """The 15 flows that declare no guard must be untouched by #126 —
    the sweeper falls back to a no-op context manager."""
    storage = MemoryStorage()
    calls: list[StorageKey] = []

    async def on_expire(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        calls.append(key)

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=1, errors=0, raced=0)
    assert calls == [_KEY_100]


async def test_deadline_refreshed_under_the_guard_is_left_alone(bot_stub: MagicMock) -> None:
    """#258: the state NAME stayed put but the clock was reset.

    This is the shape ``duel.handle_duel_roll`` produces between
    best-of-N rounds — ``update_data`` restamps ``state_entered_at``
    without touching the state, so the sweeper's name comparison sees
    nothing wrong and, before the fix, expired a match whose deadline
    had just been pushed 300s into the future. The score and the round
    history went with it.

    Note the guard makes this *more* likely, not less: the sweeper
    decides to expire, then blocks on the very lock the roll handler
    holds across two Telegram round trips, then acts on the decision it
    took before waiting.
    """
    storage = MemoryStorage()
    events: list[str] = []

    async def round_two_starts() -> None:
        data = await storage.get_data(_KEY_100)
        await storage.set_data(
            _KEY_100,
            {**data, STATE_ENTERED_AT_FIELD: _NOW.isoformat(), "challenger_wins": 1},
        )

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        events.append("on_expire")

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
        extra={"opponent_id": 200},
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={
            RpsStates.awaiting_acceptance: TimeoutRule(
                timeout_seconds=60,
                on_expire=on_expire,
                guard=lambda _bot, _key: _recording_guard(events, on_enter=round_two_starts),
            )
        },
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=0, errors=0, raced=1)
    assert events == ["enter", "exit"]
    # The live match survives whole — state, running score and all.
    assert await storage.get_state(_KEY_100) == RpsStates.awaiting_acceptance.state
    survivor = await storage.get_data(_KEY_100)
    assert survivor["opponent_id"] == 200
    assert survivor["challenger_wins"] == 1


async def test_on_expire_receives_the_post_lock_snapshot(bot_stub: MagicMock) -> None:
    """#258: the callback renders from ``data``, so it must be fresh.

    A write that lands under the guard without moving the deadline is
    still a real expiry — but the dict the callback quotes has to be
    the one in storage now, not the copy read before the lock. Passing
    the stale copy makes ``_expire_duel`` announce a round-old score on
    the card it leaves behind.
    """
    storage = MemoryStorage()
    events: list[str] = []
    seen: list[dict[str, Any]] = []

    async def late_write() -> None:
        data = await storage.get_data(_KEY_100)
        await storage.set_data(_KEY_100, {**data, "challenger_wins": 2})

    async def on_expire(_bot: Any, _key: StorageKey, data: dict[str, Any]) -> None:
        seen.append(data)

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
        extra={"challenger_wins": 1},
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={
            RpsStates.awaiting_acceptance: TimeoutRule(
                timeout_seconds=60,
                on_expire=on_expire,
                guard=lambda _bot, _key: _recording_guard(events, on_enter=late_write),
            )
        },
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=1, errors=0, raced=0)
    assert len(seen) == 1
    assert seen[0]["challenger_wins"] == 2


async def test_answer_landing_during_on_expire_is_not_wiped(bot_stub: MagicMock) -> None:
    """#837: a guardless rule loses the race the guard was bought for.

    ``app.py`` wires a guard for four states (the two /cpc and the two
    /duel ones) and leaves it ``None`` for the other nineteen. For
    those, ``on_expire`` is a bare Telegram round trip with nothing
    serialising it: the user can answer the very prompt it just sent,
    and the clear that follows would delete the state that answer
    created. Before the fix the sweeper checked for that only BEFORE
    the callback, where it cannot have happened yet.

    The callback writes the new state here because a single-task test
    has no other way to pin the interleaving. No production callback
    does that — the write stands in for a handler running between the
    sweeper's two awaits.
    """
    storage = MemoryStorage()

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        # Stands in for the user answering while the DM is in flight.
        await storage.set_state(_KEY_100, RpsStates.awaiting_moves.state)
        await storage.set_data(
            _KEY_100,
            {STATE_ENTERED_AT_FIELD: _NOW.isoformat(), "challenger_move": "rock"},
        )

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={
            RpsStates.awaiting_acceptance: TimeoutRule(
                timeout_seconds=60,
                on_expire=on_expire,
            )
        },
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=0, errors=0, raced=1)
    # The answer survives: neither the state nor its payload was cleared.
    assert await storage.get_state(_KEY_100) == RpsStates.awaiting_moves.state
    assert (await storage.get_data(_KEY_100))["challenger_move"] == "rock"


async def test_deadline_refreshed_during_on_expire_is_not_wiped(bot_stub: MagicMock) -> None:
    """#837, the same-state half: the name held, the clock moved.

    A re-prompt restamps ``state_entered_at`` without changing the
    state — ``handlers/support.py`` does exactly that. Comparing only
    the name would clear a session whose deadline had just been pushed
    forward, so the post-callback check has to test the stamp too,
    exactly as the post-guard one does.
    """
    storage = MemoryStorage()

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        data = await storage.get_data(_KEY_100)
        await storage.set_data(
            _KEY_100,
            {**data, STATE_ENTERED_AT_FIELD: _NOW.isoformat(), "attempts": 2},
        )

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
        extra={"attempts": 1},
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={
            RpsStates.awaiting_acceptance: TimeoutRule(
                timeout_seconds=60,
                on_expire=on_expire,
            )
        },
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=0, errors=0, raced=1)
    assert await storage.get_state(_KEY_100) == RpsStates.awaiting_acceptance.state
    assert (await storage.get_data(_KEY_100))["attempts"] == 2


async def test_quiet_on_expire_still_clears_without_a_guard(bot_stub: MagicMock) -> None:
    """#837 must not turn every guardless expiry into a race.

    The post-callback re-read reads back exactly what the scan saw
    when nobody intervened, so the clear still happens and ``raced``
    stays 0. Without this the fix would silently disable expiry for
    the nineteen guardless states.
    """
    storage = MemoryStorage()
    calls: list[str] = []

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        calls.append("on_expire")

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={
            RpsStates.awaiting_acceptance: TimeoutRule(
                timeout_seconds=60,
                on_expire=on_expire,
            )
        },
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=1, errors=0, raced=0)
    assert calls == ["on_expire"]
    assert await storage.get_state(_KEY_100) is None
    assert await storage.get_data(_KEY_100) == {}


async def test_sweeper_uses_the_backend_one_shot_clear(
    bot_stub: MagicMock,
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#838: a backend that can empty a key in one statement gets used.

    ``set_state(None)`` + ``set_data({})`` is two commits against a
    disk-backed store, and a crash between them leaves a stateless row
    still holding its payload — which this very sweeper would then skip
    forever. The dispatch is duck-typed on ``clear`` for the same
    reason ``iter_keys`` is: ``BaseStorage`` promises neither.
    """
    from telegram_invite_bot.fsm.sqlite_storage import SQLiteStorage

    storage = SQLiteStorage(tmp_path / "clear.db")
    cleared: list[StorageKey] = []
    real_clear = storage.clear

    async def recording_clear(key: StorageKey) -> None:
        cleared.append(key)
        await real_clear(key)

    monkeypatch.setattr(storage, "clear", recording_clear)

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        return None

    key = StorageKey(bot_id=42, chat_id=501, user_id=501)
    await storage.set_state(key, RpsStates.awaiting_acceptance.state)
    await storage.set_data(
        key,
        {STATE_ENTERED_AT_FIELD: (_NOW - timedelta(seconds=120)).isoformat()},
    )

    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
    )
    report = await sweeper.sweep_once()
    # Read everything out, then close, then assert. A failing assert
    # between the two would otherwise leak the aiosqlite worker thread
    # and hang the whole session at exit, which hides the real failure.
    after_state = await storage.get_state(key)
    after_data = await storage.get_data(key)
    left_behind = await storage.iter_keys()
    await storage.close()

    assert report.expired == 1
    assert cleared == [key]
    assert after_state is None
    assert after_data == {}
    # The row is gone outright, not left behind empty.
    assert left_behind == []


async def test_sweeper_falls_back_when_the_backend_has_no_clear(bot_stub: MagicMock) -> None:
    """#838: the portable path stays the two-call sequence.

    The ``hasattr`` assertion is the point of the test. ``BaseStorage``
    defines no ``clear`` and :class:`MemoryStorage` inherits none, so
    the sweeper must fall back — and if a future aiogram adds one, this
    is the test that says so instead of the behaviour changing
    silently.
    """
    storage = MemoryStorage()
    assert not hasattr(storage, "clear")

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        return None

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
        extra={"opponent_id": 200},
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=1, errors=0, raced=0)
    assert await storage.get_state(_KEY_100) is None
    assert await storage.get_data(_KEY_100) == {}


async def test_sweep_failing_clear_does_not_end_the_pass(bot_stub: MagicMock) -> None:
    """#1515: a storage error on one key must not abandon the rest.

    Only ``on_expire`` used to sit inside a ``try``. The clear that
    follows it ran bare, so a single ``database is locked`` from
    ``SQLiteStorage`` threw out of the ``for`` loop and every key the
    pass had not reached yet was silently skipped — invisible, because
    ``run`` catches one level up and the task itself survives.
    """
    storage = MemoryStorage()
    expired_keys: list[StorageKey] = []

    async def on_expire(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        expired_keys.append(key)

    for uid in (100, 200):
        await _seed(
            storage,
            user_id=uid,
            state=RpsStates.awaiting_acceptance.state,
            entered_at=_NOW - timedelta(seconds=200),
        )

    # Patched only AFTER seeding, and only for the first key: the
    # portable clear path is ``set_state(None)`` then ``set_data({})``,
    # and ``sweep_once`` itself never writes state otherwise, so this
    # fails exactly the one call the ticket names.
    real_set_state = storage.set_state

    async def flaky_set_state(key: StorageKey, state: Any = None) -> None:
        if key == _KEY_100:
            raise RuntimeError("database is locked")
        await real_set_state(key, state)

    storage.set_state = flaky_set_state  # type: ignore[method-assign]

    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    # Both keys were reached: the failure is counted, not propagated.
    assert report == SweepReport(scanned=2, expired=1, errors=1, raced=0)
    assert expired_keys == [_KEY_100, _key(200)]
    # The failed key keeps its state, so the next pass retries it.
    assert storage.storage[_KEY_100].state == RpsStates.awaiting_acceptance.state
    # The key AFTER the failure was still processed — the whole point.
    assert memory_storage_keys(storage) == [_KEY_100]


def test_memory_storage_keys_drops_records_the_middleware_materialised() -> None:
    """#1516: the snapshot counts live sessions, not lifetime users.

    ``storage.storage`` is a ``defaultdict`` and aiogram's
    :class:`FSMContextMiddleware` calls ``get_state`` on every update,
    so an empty record accumulates for every user the bot has ever
    seen and is never removed. The predicate mirrors
    ``SQLiteStorage._DELETE_IF_EMPTY`` exactly, including the corner
    where a handler wrote data without a state.
    """
    storage = MemoryStorage()
    with_state = _key(100)
    data_only = _key(200)
    materialised = _key(300)

    storage.storage[with_state].state = RpsStates.awaiting_acceptance.state
    storage.storage[data_only].data = {"opponent_id": 999}
    # A bare read is all it takes to create the empty record.
    _ = storage.storage[materialised]

    assert materialised in storage.storage
    assert memory_storage_keys(storage) == [with_state, data_only]


async def test_expiry_removes_the_memory_record_outright(bot_stub: MagicMock) -> None:
    """#1516: clearing must not leave an empty record behind.

    ``SQLiteStorage`` deletes the row (#195), so leaving a
    ``state=None`` / ``{}`` record in memory made the two backends
    disagree about whether a finished flow still exists — and the
    sibling scan in ``scheduler.fsm_busy`` reads the same dict.
    """
    storage = MemoryStorage()

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        return None

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
        extra={"opponent_id": 200},
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=1, errors=0, raced=0)
    assert _KEY_100 not in storage.storage
    assert memory_storage_keys(storage) == []
    # The defaultdict still answers a later read, with an empty record.
    assert await storage.get_state(_KEY_100) is None


# -- #1488: the two readings of a missing stamp, made one ------------
#
# The scan treats "no state_entered_at" as "fresh session, leave
# alone". Both race re-checks used to treat it as "nothing defends
# this key, expire it". Eleven handlers set the state first and stamp
# it with a second write, so the contradiction had a real window.


async def test_a_stamp_that_vanished_under_the_guard_is_left_alone(bot_stub: MagicMock) -> None:
    """#1488: same state name, stamp gone — that is a NEW flow, not an old one.

    :meth:`FSMContext.clear` empties the data dict; a handler that
    re-enters the same state then sets the state and stamps it with a
    separate write. Between those two the record carries the state the
    sweeper scanned and no stamp at all. Before the fix the re-check
    read that as "no defence" and cleared a session one await old,
    handing ``on_expire`` an empty dict to render its card from.
    """
    storage = MemoryStorage()
    events: list[str] = []

    async def re_enters() -> None:
        await storage.set_data(_KEY_100, {})

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        events.append("on_expire")

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
        extra={"opponent_id": 200},
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={
            RpsStates.awaiting_acceptance: TimeoutRule(
                timeout_seconds=60,
                on_expire=on_expire,
                guard=lambda _bot, _key: _recording_guard(events, on_enter=re_enters),
            )
        },
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=0, errors=0, raced=1)
    assert events == ["enter", "exit"]
    assert await storage.get_state(_KEY_100) == RpsStates.awaiting_acceptance.state


async def test_a_stamp_that_vanished_during_on_expire_is_not_wiped(bot_stub: MagicMock) -> None:
    """#1488, one step later: the same window, now after the callback.

    Nineteen of the registered rules run without a guard, so nothing
    serialises the Telegram round trip ``on_expire`` makes. The user
    can answer the very prompt it sent, and the answer's first write
    lands here.
    """
    storage = MemoryStorage()
    events: list[str] = []

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        events.append("on_expire")
        await storage.set_data(_KEY_100, {})

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
        extra={"opponent_id": 200},
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=0, errors=0, raced=1)
    assert events == ["on_expire"]
    assert await storage.get_state(_KEY_100) == RpsStates.awaiting_acceptance.state


# -- #1489: the guard is taken under a deadline ----------------------


@contextlib.asynccontextmanager
async def _never_handed_over(log: list[str]) -> AsyncIterator[None]:
    """A guard somebody else holds and is not going to release."""
    log.append("waiting")
    await asyncio.sleep(3600)
    yield  # pragma: no cover — the acquire above never returns


async def test_a_guard_that_will_not_be_handed_over_is_skipped(bot_stub: MagicMock) -> None:
    """#1489: the sweeper gives up on the lock instead of waiting on it.

    Nothing has happened at that point — no callback, no clear — so the
    key is simply left for the next pass and counted in ``skipped``,
    which is neither an expiry nor an error.
    """
    storage = MemoryStorage()
    events: list[str] = []

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        events.append("on_expire")

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
        extra={"opponent_id": 200},
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={
            RpsStates.awaiting_acceptance: TimeoutRule(
                timeout_seconds=60,
                on_expire=on_expire,
                guard=lambda _bot, _key: _never_handed_over(events),
            )
        },
        guard_timeout_seconds=0.01,
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=1, expired=0, errors=0, raced=0, skipped=1)
    assert events == ["waiting"]
    assert await storage.get_state(_KEY_100) == RpsStates.awaiting_acceptance.state
    assert (await storage.get_data(_KEY_100))["opponent_id"] == 200


async def test_a_stuck_guard_does_not_hold_up_the_other_keys(bot_stub: MagicMock) -> None:
    """#1489: the loop is sequential, so one held lock used to stop it all.

    The second key here belongs to a DIFFERENT rule, which is the half
    that made the old behaviour indefensible: a stuck /cpc match froze
    the /duel deadlines, the /withdraw interviews and the /support
    sessions with it, silently and without raising.
    """
    storage = MemoryStorage()
    events: list[str] = []
    expired_keys: list[StorageKey] = []

    async def on_expire(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        expired_keys.append(key)

    await _seed(
        storage,
        user_id=100,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
    )
    await _seed(
        storage,
        user_id=101,
        state=RpsStates.awaiting_moves.state,
        entered_at=_NOW - timedelta(seconds=120),
    )
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={
            RpsStates.awaiting_acceptance: TimeoutRule(
                timeout_seconds=60,
                on_expire=on_expire,
                guard=lambda _bot, _key: _never_handed_over(events),
            ),
            RpsStates.awaiting_moves: TimeoutRule(timeout_seconds=30, on_expire=on_expire),
        },
        guard_timeout_seconds=0.01,
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert report == SweepReport(scanned=2, expired=1, errors=0, raced=0, skipped=1)
    assert expired_keys == [_key(101)]


# -- #1487: the period is the interval, not interval + pass ----------


class _FakeMonotonic:
    """Stands in for the ``time`` module inside the sweeper.

    Needed because the drift correction reads ``time.monotonic()``
    while the sleep it corrects is stubbed out — a real clock would
    report ~0 for both the sleep and the pass and prove nothing.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


async def test_run_charges_the_pass_against_the_interval(
    bot_stub: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1487: a four-second pass shortens the next sleep, not the period.

    The loop used to sleep the whole interval AFTER the pass, so the
    real period was ``interval + pass duration`` and every pass pushed
    the following one further out.
    """
    clock = _FakeMonotonic()
    slept: list[float] = []
    sweeper = FsmTimeoutSweeper(
        MemoryStorage(),
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=_noop)},
        interval_seconds=30.0,
        clock=_frozen_clock,
    )

    async def _slow_pass() -> SweepReport:
        clock.now += 4.0
        return SweepReport(scanned=0, expired=0, errors=0)

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.now += seconds
        if len(slept) >= 2:
            raise asyncio.CancelledError

    # ``fs_mod.asyncio`` is this very module object, so patching
    # ``asyncio`` directly is the same write; spelled this way because
    # the attribute form is not an explicit re-export (mypy, strict).
    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(fs_mod, "time", clock)
    monkeypatch.setattr(sweeper, "sweep_once", _slow_pass)

    with pytest.raises(asyncio.CancelledError):
        await sweeper.run()

    assert slept == [26.0, 26.0]


async def _noop(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
    """Callback for rules a test registers but never trips."""


class _BulkStorage(MemoryStorage):
    """A :class:`MemoryStorage` that also offers the #1451 bulk scan.

    Counts every call the sweeper makes so a test can pin WHICH reads
    happen, not merely that the answer came out right. The bulk method
    reaches through :class:`MemoryStorage` directly on purpose: it
    stands in for one SQL statement, so it must not inflate the
    per-key counters it exists to eliminate.
    """

    def __init__(self) -> None:
        super().__init__()
        self.iter_keys_calls = 0
        self.iter_records_calls = 0
        self.get_state_calls = 0
        self.get_data_calls = 0

    async def iter_keys(self) -> list[StorageKey]:
        self.iter_keys_calls += 1
        return memory_storage_keys(self)

    async def iter_records(
        self, states: Collection[str]
    ) -> list[tuple[StorageKey, str, dict[str, Any]]]:
        self.iter_records_calls += 1
        wanted = set(states)
        found: list[tuple[StorageKey, str, dict[str, Any]]] = []
        for key in memory_storage_keys(self):
            state = await MemoryStorage.get_state(self, key)
            if state is not None and state in wanted:
                found.append((key, state, await MemoryStorage.get_data(self, key)))
        return found

    async def get_state(self, key: StorageKey) -> str | None:
        self.get_state_calls += 1
        return await super().get_state(key)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        self.get_data_calls += 1
        return await super().get_data(key)


async def test_sweep_uses_the_bulk_scan_when_the_storage_offers_one(
    bot_stub: MagicMock,
) -> None:
    """#1451: candidate selection is one round trip, not ``1 + 2N``.

    The old shape was ``iter_keys()`` plus a ``get_state`` for every
    key in the store and a ``get_data`` for every key a rule covered.
    On ``FSM_BACKEND=sqlite`` all of those queue behind the single
    connection every aiogram update's own ``get_state`` uses, on every
    sweeper tick (``interval_seconds``, 30 s), and the count grows
    with rows the sweeper can never act on.

    The reads that remain are the two the sweeper is not allowed to
    lose: the re-read under the rule's guard (#258) and the re-read
    after ``on_expire`` returns (#837). Both must be FRESH at the
    moment of the decision, so both are counted here — four calls for
    the one key that expires, and nothing at all for the three that
    do not.
    """
    storage = _BulkStorage()
    await _seed(
        storage,
        user_id=1,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=90),
    )
    for uid in (2, 3, 4):
        await _seed(storage, user_id=uid, state="OtherStates:idle", entered_at=_NOW)
    expired: list[StorageKey] = []

    async def on_expire(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        expired.append(key)

    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
    )

    report = await sweeper.sweep_once()

    assert expired == [_key(1)]
    assert report.scanned == 1
    assert report.expired == 1
    assert storage.iter_records_calls == 1
    assert storage.iter_keys_calls == 0
    assert storage.get_state_calls == 2
    assert storage.get_data_calls == 2


# ── #1767: the sweeper's outbound fan-out is paced and bounded ───────
#
# ``sweep_once`` sends one Telegram message per expired key, back to
# back, on a 30-second cadence. Nothing between the keys yields to the
# rate limiter, and the ``on_expire`` error branch deliberately leaves
# the FSM record intact so the next pass retries — which is the right
# call for a transient DB error and exactly the wrong one for a 429,
# because a flood wait re-fires the same fan-out every 30 seconds and
# the bot never gets out from under it.
#
# ``TelegramRetryAfter`` is a SIBLING of ``TelegramBadRequest``, not a
# subclass, so the callbacks' own ``suppress(...)`` never covered it.


def _retry_after(seconds: int) -> TelegramRetryAfter:
    return TelegramRetryAfter(
        method=SendMessage(chat_id=1, text="x"),
        message=f"Too Many Requests: retry after {seconds}",
        retry_after=seconds,
    )


async def test_a_pass_expires_at_most_the_budget_and_defers_the_rest(
    bot_stub: MagicMock,
) -> None:
    """Five due keys, a budget of two: two expire, three wait.

    The deferred keys must be left completely untouched — no callback,
    no clear — so the next pass picks them up with their records still
    intact. A budget that half-expired a key would be worse than no
    budget at all.
    """
    storage = MemoryStorage()
    expired: list[StorageKey] = []

    async def on_expire(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        expired.append(key)

    for user_id in range(1, 6):
        await _seed(
            storage,
            user_id=user_id,
            state=RpsStates.awaiting_acceptance.state,
            entered_at=_NOW - timedelta(seconds=120),
        )

    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
        expire_budget=2,
        expire_pace_seconds=0.0,
    )

    first = await sweeper.sweep_once()

    assert first.expired == 2
    assert first.deferred == 3
    assert len(expired) == 2

    second = await sweeper.sweep_once()

    assert second.expired == 2
    assert second.deferred == 1
    assert len(expired) == 4

    third = await sweeper.sweep_once()

    assert third.expired == 1
    assert third.deferred == 0
    assert len(expired) == 5


async def test_expiries_are_paced_instead_of_bursting(bot_stub: MagicMock) -> None:
    """Three due keys at 50 ms apart cannot finish in under 100 ms.

    Only the GAPS are paced (two of them for three sends), so the
    assertion is against ``2 * pace`` and not ``3 * pace`` — a pass
    that paced after the last send would be paying for a wait nobody
    is waiting on.
    """
    storage = MemoryStorage()
    sent: list[StorageKey] = []

    async def on_expire(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        sent.append(key)

    for user_id in range(1, 4):
        await _seed(
            storage,
            user_id=user_id,
            state=RpsStates.awaiting_acceptance.state,
            entered_at=_NOW - timedelta(seconds=120),
        )

    pace = 0.05
    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
        expire_pace_seconds=pace,
    )

    started = time.monotonic()
    report = await sweeper.sweep_once()
    elapsed = time.monotonic() - started

    assert report.expired == 3
    assert len(sent) == 3
    assert elapsed >= 2 * pace, "the fan-out was not paced between keys"


async def test_a_flood_wait_aborts_the_pass_and_reports_the_backoff(
    bot_stub: MagicMock,
) -> None:
    """A 429 on the first key must stop the pass, not iterate into it.

    Continuing would spend the rest of the pass collecting 429s, and
    the untouched records would re-send the whole fan-out one interval
    later. The remaining keys are reported as ``deferred`` and the
    backoff Telegram asked for is handed to ``run`` in the report.
    """
    storage = MemoryStorage()
    attempts: list[StorageKey] = []

    async def on_expire(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        attempts.append(key)
        raise _retry_after(7)

    for user_id in range(1, 5):
        await _seed(
            storage,
            user_id=user_id,
            state=RpsStates.awaiting_acceptance.state,
            entered_at=_NOW - timedelta(seconds=120),
        )

    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
        expire_pace_seconds=0.0,
    )

    report = await sweeper.sweep_once()

    assert len(attempts) == 1, "the pass kept sending into a flood wait"
    assert report.expired == 0
    assert report.retry_after == 7.0
    assert report.deferred == 3
    # The record is left intact: the user still owes an expiry notice,
    # and the next pass — after the backoff — is the one that sends it.
    assert await storage.get_state(_key(1)) == RpsStates.awaiting_acceptance.state


# ── #1908: a callback that cannot succeed must not own the budget ────
#
# The budget is charged per ATTEMPT, which is the only correct place —
# a failed send costs Telegram the same request a successful one does.
# The consequence, before this block existed, was that a key whose
# ``on_expire`` always raised spent one of the pass's twenty slots on
# every pass forever, and twenty such keys ahead of the live ones in
# scan order meant nothing ever expired again while only ``deferred``
# looked wrong.
#
# ``TelegramMigrateToChat`` is the case that made it real: a SIBLING of
# ``TelegramBadRequest``, so the ``suppress(...)`` every registered
# callback wraps its send in does not cover it, and a group that
# migrates to a supergroup answers it forever.


def _migrate_to_chat(new_chat_id: int) -> TelegramMigrateToChat:
    return TelegramMigrateToChat(
        method=SendMessage(chat_id=1, text="x"),
        message="Bad Request: group chat was upgraded to a supergroup chat",
        migrate_to_chat_id=new_chat_id,
    )


async def test_an_undeliverable_notice_still_clears_the_seat(
    bot_stub: MagicMock,
) -> None:
    """A migrated chat answers the same way on every retry.

    Keeping the record would leave the player busy forever AND burn a
    budget slot every thirty seconds for as long as the process lives,
    so the sweeper clears it and reports the failure in ``errors``.
    """
    storage = MemoryStorage()

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        raise _migrate_to_chat(-1_001_234_567_890)

    await _seed(
        storage,
        user_id=1,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
    )

    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
        expire_pace_seconds=0.0,
    )

    report = await sweeper.sweep_once()

    assert report.expired == 1
    assert report.errors == 1
    assert report.poisoned == 0
    assert await storage.get_state(_key(1)) is None

    # And it is gone for good: the next pass has nothing left to do.
    # ``pruned=1`` is the assertion on the line above, showing its work:
    # ``get_state`` on a cleared key re-materialises the empty record
    # (that is #1940 in one line), and the next pass reclaims it.
    assert await sweeper.sweep_once() == SweepReport(scanned=0, expired=0, errors=0, pruned=1)


async def test_a_callback_that_always_raises_stops_taking_the_budget(
    bot_stub: MagicMock,
) -> None:
    """Three strikes and the sweeper stops attempting the key.

    The record is deliberately left intact — a transient failure is
    still the likelier reading, and #1515 keeps the data recoverable
    — but it is reported as ``poisoned`` and never attempted again, so
    it stops competing with keys the sweeper can serve.
    """
    storage = MemoryStorage()
    attempts: list[StorageKey] = []

    async def on_expire(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        attempts.append(key)
        raise RuntimeError("callback is broken")

    await _seed(
        storage,
        user_id=1,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
    )

    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
        expire_pace_seconds=0.0,
    )

    for _ in range(3):
        report = await sweeper.sweep_once()
        assert report.errors == 1
        assert report.poisoned == 0

    assert len(attempts) == 3

    fourth = await sweeper.sweep_once()

    assert fourth.poisoned == 1
    assert fourth.errors == 0
    assert len(attempts) == 3, "the sweeper kept calling a callback it had given up on"
    assert await storage.get_state(_key(1)) == RpsStates.awaiting_acceptance.state


async def test_a_poisoned_key_does_not_starve_the_ones_behind_it(
    bot_stub: MagicMock,
) -> None:
    """One broken key, a budget of one, and a healthy key behind it.

    This is the whole bug in miniature: while the broken key kept
    spending the pass's only slot, the healthy one was deferred every
    single pass and never expired at all.
    """
    storage = MemoryStorage()
    expired: list[StorageKey] = []

    async def on_expire(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        if key == _key(1):
            raise RuntimeError("callback is broken")
        expired.append(key)

    for user_id in (1, 2):
        await _seed(
            storage,
            user_id=user_id,
            state=RpsStates.awaiting_acceptance.state,
            entered_at=_NOW - timedelta(seconds=120),
        )

    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
        expire_budget=1,
        expire_pace_seconds=0.0,
    )

    for _ in range(3):
        report = await sweeper.sweep_once()
        assert report.expired == 0
        assert report.deferred == 1, "the healthy key was reached too early"

    fourth = await sweeper.sweep_once()

    assert fourth.poisoned == 1
    assert fourth.expired == 1
    assert fourth.deferred == 0
    assert expired == [_key(2)]


async def test_strikes_are_consecutive_not_cumulative(bot_stub: MagicMock) -> None:
    """A flaky callback that recovers must not be quarantined later.

    Two failures, one success, two more failures: five attempts on a
    ceiling of three, and the key is still being served — because the
    success in the middle dropped the strikes it had collected.
    """
    storage = MemoryStorage()
    attempts: list[StorageKey] = []
    outcomes = iter([False, False, True, False, False, True])

    async def on_expire(_bot: Any, key: StorageKey, _data: dict[str, Any]) -> None:
        attempts.append(key)
        if not next(outcomes):
            raise RuntimeError("flaky")

    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
        expire_pace_seconds=0.0,
    )

    for _ in range(6):
        # Re-seed after every pass that cleared the record, so each
        # pass has exactly one due key to work on.
        await _seed(
            storage,
            user_id=1,
            state=RpsStates.awaiting_acceptance.state,
            entered_at=_NOW - timedelta(seconds=120),
        )
        await sweeper.sweep_once()

    assert len(attempts) == 6, "a recovered key was quarantined on cumulative strikes"


async def test_the_strike_ledger_does_not_outlive_the_keys(
    bot_stub: MagicMock,
) -> None:
    """A key that leaves storage takes its strikes with it.

    The ledger is per-key bookkeeping in memory; without the prune it
    would grow one permanent entry per broken session for the life of
    the process.
    """
    storage = MemoryStorage()

    async def on_expire(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
        raise RuntimeError("callback is broken")

    await _seed(
        storage,
        user_id=1,
        state=RpsStates.awaiting_acceptance.state,
        entered_at=_NOW - timedelta(seconds=120),
    )

    sweeper = FsmTimeoutSweeper(
        storage,
        bot_stub,
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=60, on_expire=on_expire)},
        clock=_frozen_clock,
        expire_pace_seconds=0.0,
    )

    await sweeper.sweep_once()
    assert sweeper._expire_failures  # noqa: SLF001 — the ledger is the subject

    await storage.set_state(_key(1), None)
    await storage.set_data(_key(1), {})
    await sweeper.sweep_once()

    assert not sweeper._expire_failures, "the ledger kept an entry for a key that is gone"  # noqa: SLF001
