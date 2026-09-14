"""Unit matrix for the /cpc + /duel busy scan (``scheduler.fsm_busy``).

The module had ZERO direct coverage until #1517 widened it from
"opponent seat only" to "either seat". The first test here carries the
half of the rename guard the module itself cannot: since #1450 the
module READS ``_BUSY_STATES`` off the real ``State`` objects, so a
rename in ``fsm/rps.py`` or ``fsm/duel.py`` raises ``AttributeError``
at import instead of silently ceasing to match. That derivation then
follows any rename without comment — including one that orphans the
state strings already written into the FSM store — so the four
literals live down here, where changing them is a deliberate act.

The rest pins the two seats separately, because they are reached by
different code paths and the opponent arm alone is what #1517 found
insufficient: it costs a data read, so the challenger arm is checked
first and a test has to prove it actually fires.

The two public predicates differ ONLY in that arm, and the difference
is load-bearing rather than cosmetic. ``is_user_busy`` counts both
seats and belongs to the accept path; ``is_user_an_opponent`` ignores
matches the user started, because R-FIX-008 pins one independent /cpc
session per chat for the same challenger and the creation path would
otherwise revoke it. A test below fails if the two ever collapse into
one another.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from telegram_invite_bot.scheduler.fsm_busy import (
    _BUSY_STATES,
    _iter_keys,
    is_user_an_opponent,
    is_user_busy,
)
from telegram_invite_bot.scheduler.fsm_sweeper import memory_storage_keys

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from aiogram.fsm.storage.base import StateType


_BOT_ID = 42

ALICE = 100
BOB = 200
CAROL = 300
DAVE = 400


def _key(user_id: int, *, chat_id: int | None = None) -> StorageKey:
    """Challenger-owned FSM key. /cpc keys on the private chat (so
    ``chat_id == user_id``); /duel keys on the group, hence the
    override."""
    return StorageKey(
        bot_id=_BOT_ID,
        chat_id=user_id if chat_id is None else chat_id,
        user_id=user_id,
    )


async def _seed_match(
    storage: MemoryStorage,
    *,
    challenger_id: int,
    opponent_id: int,
    state: str,
    chat_id: int | None = None,
) -> StorageKey:
    key = _key(challenger_id, chat_id=chat_id)
    await storage.set_state(key, state)
    await storage.set_data(key, {"opponent_id": opponent_id, "bet": 100})
    return key


class _UnknownStorage(BaseStorage):
    """A backend the scan has no way to enumerate — neither
    ``iter_keys()`` nor ``MemoryStorage`` internals."""

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        return None

    async def get_state(self, key: StorageKey) -> str | None:
        return None

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        return None

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return {}

    async def close(self) -> None:
        return None


# ── The set itself ───────────────────────────────────────────────────


def test_busy_states_are_the_real_state_names() -> None:
    """``_BUSY_STATES`` must equal the four real state names.

    #1450 inverted this pin. The module used to hold the literals and
    this test derived them from the classes, so a rename failed here
    — but only after the same rename had already disabled the guard
    at import. The derivation now lives in the module, where it fails
    loudly, and the literals live here, where they still catch what a
    derivation never can: a set quietly widened or narrowed, and a
    rename that silently orphans every state string already sitting in
    the FSM store.
    """
    assert {
        "RpsStates:awaiting_acceptance",
        "RpsStates:awaiting_moves",
        "DuelStates:awaiting_acceptance",
        "DuelStates:awaiting_rolls",
    } == _BUSY_STATES


# ── Both seats ───────────────────────────────────────────────────────


async def test_challenger_seat_reads_as_busy() -> None:
    """#1517's accept-path hole: the CHALLENGER of a live match used
    to read as free, because the scan only looked at ``opponent_id``."""
    storage = MemoryStorage()
    await _seed_match(
        storage,
        challenger_id=BOB,
        opponent_id=CAROL,
        state="RpsStates:awaiting_moves",
    )

    assert await is_user_busy(storage, user_id=BOB) is True


async def test_opponent_seat_reads_as_busy() -> None:
    storage = MemoryStorage()
    await _seed_match(
        storage,
        challenger_id=ALICE,
        opponent_id=BOB,
        state="RpsStates:awaiting_acceptance",
    )

    assert await is_user_busy(storage, user_id=BOB) is True


async def test_duel_seats_count_too() -> None:
    """A duel keys on the GROUP chat, so the busy key does not look
    like its user at all — the scan must still match both seats."""
    storage = MemoryStorage()
    await _seed_match(
        storage,
        challenger_id=ALICE,
        opponent_id=BOB,
        state="DuelStates:awaiting_rolls",
        chat_id=-1001,
    )

    assert await is_user_busy(storage, user_id=ALICE) is True
    assert await is_user_busy(storage, user_id=BOB) is True


async def test_uninvolved_user_reads_as_free() -> None:
    storage = MemoryStorage()
    await _seed_match(
        storage,
        challenger_id=ALICE,
        opponent_id=BOB,
        state="RpsStates:awaiting_moves",
    )

    assert await is_user_busy(storage, user_id=CAROL) is False


async def test_state_outside_the_set_is_ignored() -> None:
    """A finished or unrelated flow must not read as a match."""
    storage = MemoryStorage()
    await _seed_match(
        storage,
        challenger_id=ALICE,
        opponent_id=BOB,
        state="P2PStates:awaiting_amount",
    )

    assert await is_user_busy(storage, user_id=ALICE) is False
    assert await is_user_busy(storage, user_id=BOB) is False


# ── exclude_key ──────────────────────────────────────────────────────


async def test_exclude_key_skips_the_match_being_resolved() -> None:
    """The accept path scans from INSIDE the match it is flipping;
    without the exclusion every accept would reject itself."""
    storage = MemoryStorage()
    key = await _seed_match(
        storage,
        challenger_id=ALICE,
        opponent_id=BOB,
        state="RpsStates:awaiting_acceptance",
    )

    assert await is_user_busy(storage, user_id=BOB, exclude_key=key) is False
    assert await is_user_busy(storage, user_id=ALICE, exclude_key=key) is False


async def test_exclude_key_hides_only_that_one_match() -> None:
    storage = MemoryStorage()
    key = await _seed_match(
        storage,
        challenger_id=ALICE,
        opponent_id=BOB,
        state="RpsStates:awaiting_acceptance",
    )
    await _seed_match(
        storage,
        challenger_id=CAROL,
        opponent_id=BOB,
        state="DuelStates:awaiting_acceptance",
        chat_id=-1002,
    )

    assert await is_user_busy(storage, user_id=BOB, exclude_key=key) is True


# ── Opponent seat only (creation path) ───────────────────────────────


async def test_opponent_predicate_matches_the_opponent_seat() -> None:
    """#1517's creation-path hole: the invited player's own key is
    empty, so ``prior is not None`` let them open a second match."""
    storage = MemoryStorage()
    await _seed_match(
        storage,
        challenger_id=ALICE,
        opponent_id=BOB,
        state="RpsStates:awaiting_acceptance",
    )

    assert await is_user_an_opponent(storage, user_id=BOB) is True


async def test_opponent_predicate_ignores_the_challenger_seat() -> None:
    """R-FIX-008: one independent /cpc session per chat.

    Bob challenging from another chat must still be able to open a
    match here — the same-chat case is what the handler's own
    ``prior`` read covers. This is the single behavioural difference
    between the two predicates, so it is asserted against both.
    """
    storage = MemoryStorage()
    await _seed_match(
        storage,
        challenger_id=BOB,
        opponent_id=CAROL,
        state="RpsStates:awaiting_moves",
    )

    assert await is_user_an_opponent(storage, user_id=BOB) is False
    assert await is_user_busy(storage, user_id=BOB) is True


async def test_opponent_predicate_honours_exclude_key() -> None:
    storage = MemoryStorage()
    key = await _seed_match(
        storage,
        challenger_id=ALICE,
        opponent_id=BOB,
        state="DuelStates:awaiting_acceptance",
        chat_id=-1001,
    )

    assert await is_user_an_opponent(storage, user_id=BOB, exclude_key=key) is False


# ── Backends ─────────────────────────────────────────────────────────


async def test_unknown_backend_degrades_to_no_op() -> None:
    """Losing the guard beats crashing the accept path — the sweeper
    would not run on such a backend either."""
    assert await is_user_busy(_UnknownStorage(), user_id=BOB) is False


async def test_memory_scan_skips_records_the_middleware_materialised() -> None:
    """#1516: ``MemoryStorage.storage`` is a ``defaultdict`` and
    aiogram's ``FSMContextMiddleware`` calls ``get_state`` on every
    update, so a plain READ inserts an empty record that then shows up
    in every scan, forever. The snapshot must drop those."""
    storage = MemoryStorage()
    live = await _seed_match(
        storage,
        challenger_id=ALICE,
        opponent_id=BOB,
        state="RpsStates:awaiting_acceptance",
    )
    # Exactly what the middleware does for a passer-by.
    assert await storage.get_state(_key(DAVE)) is None
    assert _key(DAVE) in storage.storage

    assert await _iter_keys(storage) == [live]


async def test_data_without_state_is_still_reported() -> None:
    """The deliberate corner both backends keep visible: a handler that
    wrote data without a state is a wiring bug, not a finished flow, so
    the key stays in the snapshot (mirrors ``SQLiteStorage.iter_keys``).
    It is not a MATCH, though — no state means no busy."""
    storage = MemoryStorage()
    key = _key(DAVE)
    await storage.set_data(key, {"opponent_id": BOB})

    assert await _iter_keys(storage) == [key]
    assert await is_user_busy(storage, user_id=BOB) is False


class _BulkStorage(MemoryStorage):
    """A :class:`MemoryStorage` that also offers the #1451 bulk scan.

    Counts every call so a test can pin WHICH reads the scan makes,
    not merely that it answered correctly. ``iter_records`` reaches
    through :class:`MemoryStorage` directly because it stands in for
    one SQL statement and must not inflate the per-key counters it
    exists to remove.
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


async def test_busy_scan_uses_the_bulk_read_when_the_backend_offers_one() -> None:
    """#1491: one accept click costs one query, not ``1 + N + M``.

    This scan runs on the HOT path — every /cpc and /duel accept — and
    under ``FSM_BACKEND=sqlite`` its ``get_state``-per-key walk queued
    behind the single connection every routed update also uses. The
    states it wants are exactly ``_BUSY_STATES``, a fixed set known at
    import, so the backend can answer in one statement.

    Correctness is unchanged and pinned by every other test in this
    file; what is pinned HERE is that the per-key reads are gone, and
    that a backend without the bulk method still gets the old walk
    (``test_memory_scan_skips_records_the_middleware_materialised``
    and the rest all run on a plain :class:`MemoryStorage`).
    """
    storage = _BulkStorage()
    await _seed_match(
        storage,
        challenger_id=ALICE,
        opponent_id=BOB,
        state="RpsStates:awaiting_acceptance",
    )
    for uid in (CAROL, DAVE):
        await storage.set_state(_key(uid), "SomeOtherStates:idle")

    assert await is_user_busy(storage, user_id=BOB) is True

    assert storage.iter_records_calls == 1
    assert storage.iter_keys_calls == 0
    assert storage.get_state_calls == 0
    assert storage.get_data_calls == 0


async def test_bulk_read_still_honours_exclude_key() -> None:
    """The fast path must not quietly drop the exclusion.

    ``exclude_key`` is what stops an accept handler flagging the very
    match it is resolving. It is applied by the caller either way, but
    the two paths reach the loop differently, so the fast one gets its
    own pin rather than an argument that it obviously still works.
    """
    storage = _BulkStorage()
    key = await _seed_match(
        storage,
        challenger_id=ALICE,
        opponent_id=BOB,
        state="RpsStates:awaiting_acceptance",
    )

    assert await is_user_busy(storage, user_id=BOB, exclude_key=key) is False
    assert storage.iter_records_calls == 1
