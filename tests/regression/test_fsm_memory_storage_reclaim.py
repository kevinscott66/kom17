"""#1940: the in-memory FSM store must not grow with total audience.

``MemoryStorage.storage`` is a ``defaultdict``, and aiogram's
:class:`FSMContextMiddleware` calls ``get_state`` on EVERY update
(``aiogram/fsm/storage/memory.py`` + ``aiogram/fsm/middleware.py``). So
merely *reading* materialises a record, one per ``(chat_id, user_id)``
pair the bot has ever seen. Nothing removed them: ``MemoryStorage.close``
is a no-op, and :meth:`FsmTimeoutSweeper._clear_key` pops only the keys
it actually expires.

#1516 already made the leak CHEAP — :func:`memory_storage_keys` filters
the empties out of both scans, so the sweeper and the /cpc busy check no
longer pay a coroutine per lifetime user. It did not make the leak stop.
A process with a hundred thousand lifetime chatters still holds a
hundred thousand records that all mean "no session", and it holds them
until it restarts.

The pass now reclaims them, and this file pins the three things that
makes true: they go, the ones that are NOT empty stay, and the removal
is invisible to anybody who reads the key afterwards.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from telegram_invite_bot.fsm.rps import RpsStates
from telegram_invite_bot.scheduler.fsm_sweeper import (
    STATE_ENTERED_AT_FIELD,
    FsmTimeoutSweeper,
    TimeoutRule,
    memory_storage_keys,
    prune_empty_memory_records,
)

_NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)


def _key(user_id: int) -> StorageKey:
    return StorageKey(bot_id=42, chat_id=user_id, user_id=user_id)


async def _touch(storage: MemoryStorage, user_id: int) -> None:
    """What aiogram's middleware does to a passer-by: one read.

    This is the whole defect in one line — nobody wrote anything, and
    yet the dict is one record bigger, forever.
    """
    await storage.get_state(_key(user_id))


async def _seed_session(storage: MemoryStorage, user_id: int) -> None:
    """A real, live FSM session that must survive every reclaim."""
    key = _key(user_id)
    await storage.set_state(key, RpsStates.awaiting_acceptance.state)
    await storage.set_data(key, {STATE_ENTERED_AT_FIELD: _NOW.isoformat()})


async def _noop(_bot: Any, _key: StorageKey, _data: dict[str, Any]) -> None:
    """A rule the tests register but never trip."""


def _sweeper(storage: MemoryStorage) -> FsmTimeoutSweeper:
    return FsmTimeoutSweeper(
        storage,
        MagicMock(name="bot"),
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=600, on_expire=_noop)},
        clock=lambda: _NOW,
    )


# ── The function itself ──────────────────────────────────────────────────────


async def test_a_read_alone_materialises_a_record() -> None:
    """The premise. If this ever stops holding, the rest is pointless."""
    storage = MemoryStorage()

    await _touch(storage, 100)

    assert len(storage.storage) == 1
    # And the scan already agrees the record is not there.
    assert memory_storage_keys(storage) == []


async def test_the_empty_records_are_reclaimed() -> None:
    storage = MemoryStorage()
    for user_id in range(100, 200):
        await _touch(storage, user_id)

    dropped = prune_empty_memory_records(storage)

    assert dropped == 100
    assert len(storage.storage) == 0


async def test_a_live_session_is_never_reclaimed() -> None:
    storage = MemoryStorage()
    await _seed_session(storage, 100)
    await _touch(storage, 200)

    assert prune_empty_memory_records(storage) == 1
    assert list(storage.storage) == [_key(100)]


async def test_data_without_a_state_is_never_reclaimed() -> None:
    """The deliberate corner :func:`memory_storage_keys` keeps visible.

    A handler that wrote data but no state is a wiring bug, and the
    scan reports it rather than swallowing it. The reclaim must use the
    same predicate, or it would delete the evidence.
    """
    storage = MemoryStorage()
    await storage.set_data(_key(100), {"opponent_id": 200})

    assert prune_empty_memory_records(storage) == 0
    assert memory_storage_keys(storage) == [_key(100)]


async def test_the_predicate_is_exactly_the_scans() -> None:
    """Whatever survives the reclaim is whatever the scan can see."""
    storage = MemoryStorage()
    await _seed_session(storage, 100)
    await storage.set_data(_key(101), {"opponent_id": 200})
    for user_id in range(200, 210):
        await _touch(storage, user_id)
    visible = set(memory_storage_keys(storage))

    prune_empty_memory_records(storage)

    assert set(storage.storage) == visible


async def test_the_reclaim_is_invisible_to_a_later_reader() -> None:
    """Deleting a record that means "nothing" cannot change an answer.

    Every ``MemoryStorage`` accessor indexes the ``defaultdict`` afresh,
    so the read below recreates precisely the record the reclaim
    removed. This is the same argument ``_clear_key``'s existing pop
    rests on, pinned here so an aiogram upgrade that starts caching
    record objects fails loudly instead of corrupting a session.
    """
    storage = MemoryStorage()
    await _touch(storage, 100)
    prune_empty_memory_records(storage)

    assert await storage.get_state(_key(100)) is None
    assert await storage.get_data(_key(100)) == {}


# ── Through the sweeper ──────────────────────────────────────────────────────


async def test_the_sweep_reclaims_and_reports() -> None:
    storage = MemoryStorage()
    await _seed_session(storage, 100)
    for user_id in range(200, 205):
        await _touch(storage, user_id)

    report = await _sweeper(storage).sweep_once()

    assert report.pruned == 5
    assert report.scanned == 1
    assert report.expired == 0
    assert list(storage.storage) == [_key(100)]


async def test_growth_does_not_survive_across_passes() -> None:
    """The regression proper: audience grows, the store does not.

    Without the reclaim the assertion below reads 100, then 200, then
    300 — which is the leak, stated as a number.
    """
    storage = MemoryStorage()
    sweeper = _sweeper(storage)
    seen = 0

    for _round in range(3):
        for _ in range(100):
            seen += 1
            await _touch(storage, seen)
        await sweeper.sweep_once()
        assert len(storage.storage) == 0


async def test_an_expiring_session_still_expires_after_a_reclaim() -> None:
    """The reclaim runs BEFORE the scan; it must not eat the pass.

    A stale session is not empty, so it is not a reclaim candidate —
    but the ordering is what makes that true, and ordering is exactly
    what a later refactor gets wrong.
    """
    storage = MemoryStorage()
    key = _key(100)
    await storage.set_state(key, RpsStates.awaiting_acceptance.state)
    await storage.set_data(
        key, {STATE_ENTERED_AT_FIELD: (_NOW - timedelta(seconds=1200)).isoformat()}
    )
    for user_id in range(200, 203):
        await _touch(storage, user_id)
    expired_keys: list[StorageKey] = []

    async def on_expire(_bot: Any, k: StorageKey, _data: dict[str, Any]) -> None:
        expired_keys.append(k)

    sweeper = FsmTimeoutSweeper(
        storage,
        MagicMock(name="bot"),
        rules={
            RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=600, on_expire=on_expire)
        },
        clock=lambda: _NOW,
    )
    report = await sweeper.sweep_once()

    assert expired_keys == [key]
    assert report.expired == 1
    assert report.pruned == 3
    assert len(storage.storage) == 0


class _PersistentStorage(BaseStorage):
    """A backend that keeps its own records — no ``defaultdict``, no empties.

    Deliberately NOT a :class:`MemoryStorage` subclass: the point is to
    exercise the branch the reclaim must not take. It offers the
    duck-typed ``iter_keys`` the sweeper looks for first, which is the
    shape ``SQLiteStorage`` ships.
    """

    def __init__(self) -> None:
        self.records: dict[StorageKey, tuple[str | None, dict[str, Any]]] = {}

    async def iter_keys(self) -> list[StorageKey]:
        return list(self.records)

    async def set_state(self, key: StorageKey, state: Any = None) -> None:
        name = state.state if hasattr(state, "state") else state
        self.records[key] = (name, self.records.get(key, (None, {}))[1])

    async def get_state(self, key: StorageKey) -> str | None:
        return self.records.get(key, (None, {}))[0]

    async def set_data(self, key: StorageKey, data: Any) -> None:
        self.records[key] = (self.records.get(key, (None, {}))[0], dict(data))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return dict(self.records.get(key, (None, {}))[1])

    async def close(self) -> None:
        return None


async def test_a_persistent_backend_reports_zero() -> None:
    """``pruned`` must stay 0 where the concept does not apply.

    A backend that stores what it is told has no records meaning "no
    session" to reclaim, so the pass must neither walk its store for
    them nor report a number that suggests it did.
    """
    storage = _PersistentStorage()
    key = _key(100)
    await storage.set_state(key, RpsStates.awaiting_acceptance.state)
    await storage.set_data(key, {STATE_ENTERED_AT_FIELD: _NOW.isoformat()})

    sweeper = FsmTimeoutSweeper(
        storage,
        MagicMock(name="bot"),
        rules={RpsStates.awaiting_acceptance: TimeoutRule(timeout_seconds=600, on_expire=_noop)},
        clock=lambda: _NOW,
    )
    report = await sweeper.sweep_once()

    assert report.pruned == 0
    assert report.scanned == 1
    assert list(storage.records) == [key]
