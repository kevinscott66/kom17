"""Unit tests for :class:`SQLiteStorage` (T-012).

The point of this storage is **survival across process restart** —
something :class:`MemoryStorage` does not provide. The tests here are
shaped around that contract:

* the storage round-trips state + data across a close/reopen cycle,
* an empty key returns the same defaults as MemoryStorage so handler
  code that switched backends doesn't have to learn new sentinels,
* the sweeper's iteration hook (``iter_keys``) reports exactly the
  set of keys we wrote.

Anything that's purely the BaseStorage contract (set→get round-trip
inside one process) is exercised by aiogram's own tests against
MemoryStorage and isn't worth duplicating here — the value of this
file is the *persistence* assertions.

``update_data`` is the exception. It used to be inherited, and the
sentence above used to name it as one of the things aiogram covers for
us. It does cover it — against :class:`MemoryStorage`, where no step
of the read-modify-write suspends. That is precisely the property this
backend does not have, which is how #836 stayed open. The override and
its concurrency tests live here.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import StorageKey

from telegram_invite_bot.fsm.sqlite_storage import SQLiteStorage


class _DemoStates(StatesGroup):
    awaiting_input = State()


def _key(user_id: int = 111, chat_id: int = 222, *, bot_id: int = 9000) -> StorageKey:
    """Helper: build a :class:`StorageKey` with sensible defaults.

    Tests usually only care about ``user_id`` + ``chat_id`` — the rest
    (``destiny``, optional ``thread_id`` / ``business_connection_id``)
    are aiogram-internal scoping fields, so we let them keep their
    defaults unless a test specifically exercises them.
    """
    return StorageKey(bot_id=bot_id, chat_id=chat_id, user_id=user_id, destiny="default")


@pytest.mark.asyncio
async def test_state_and_data_roundtrip(tmp_path: Path) -> None:
    """Sanity: set + get inside one process.

    Not a persistence test (that's the next one) — this just confirms
    the SQL plumbing isn't broken. If this fails the persistence test
    is meaningless because we're not even storing anything correctly.
    """
    storage = SQLiteStorage(tmp_path / "fsm.db")
    key = _key()
    await storage.set_state(key, _DemoStates.awaiting_input)
    await storage.set_data(key, {"foo": 1, "bar": "baz"})

    assert await storage.get_state(key) == _DemoStates.awaiting_input.state
    assert await storage.get_data(key) == {"foo": 1, "bar": "baz"}
    await storage.close()


@pytest.mark.asyncio
async def test_state_survives_storage_reopen(tmp_path: Path) -> None:
    """THE persistence test — the entire reason T-012 exists.

    A /cpc challenger pinned in ``awaiting_acceptance`` must still be
    pinned after a process restart. We simulate the restart by
    closing one :class:`SQLiteStorage` and opening a fresh one against
    the same file: any FSM state that survives this cycle survives a
    real deploy.
    """
    db_file = tmp_path / "persist.db"
    key = _key()

    first_state = "RpsStates:awaiting_acceptance"
    first = SQLiteStorage(db_file)
    await first.set_state(key, first_state)
    await first.set_data(key, {"opponent_id": 42, "bet": 100})
    await first.close()

    second = SQLiteStorage(db_file)
    assert await second.get_state(key) == first_state
    assert await second.get_data(key) == {"opponent_id": 42, "bet": 100}
    await second.close()


@pytest.mark.asyncio
async def test_unknown_key_returns_memorystorage_compatible_defaults(tmp_path: Path) -> None:
    """Handler code reads ``await state.get_data()`` expecting a dict.

    :class:`MemoryStorage` returns ``{}`` for a never-seen key (not
    ``None``); we MUST match that or ``data.get("opponent_id")`` would
    raise ``AttributeError`` on the SQLite backend. Same story for
    ``get_state`` returning ``None``.
    """
    storage = SQLiteStorage(tmp_path / "empty.db")
    key = _key(user_id=99999)  # never written
    assert await storage.get_state(key) is None
    assert await storage.get_data(key) == {}
    await storage.close()


@pytest.mark.asyncio
async def test_set_state_preserves_existing_data(tmp_path: Path) -> None:
    """State transitions must not blow away the payload.

    Aiogram's /cpc handler does ``state.set_state(awaiting_moves)``
    AFTER it has written ``{opponent_id, bet}``. If our INSERT…ON
    CONFLICT clobbered the data column on the state-only path, the
    move-resolution branch would suddenly see an empty dict and the
    match would corrupt. Locked here.
    """
    storage = SQLiteStorage(tmp_path / "preserve.db")
    key = _key()
    await storage.set_data(key, {"opponent_id": 7, "bet": 50})
    await storage.set_state(key, "RpsStates:awaiting_moves")
    assert await storage.get_data(key) == {"opponent_id": 7, "bet": 50}
    assert await storage.get_state(key) == "RpsStates:awaiting_moves"
    await storage.close()


@pytest.mark.asyncio
async def test_set_data_preserves_existing_state(tmp_path: Path) -> None:
    """Symmetric to the previous test, for the data-after-state path."""
    storage = SQLiteStorage(tmp_path / "preserve2.db")
    key = _key()
    await storage.set_state(key, "RpsStates:awaiting_acceptance")
    await storage.set_data(key, {"opponent_id": 8})
    assert await storage.get_state(key) == "RpsStates:awaiting_acceptance"
    assert await storage.get_data(key) == {"opponent_id": 8}
    await storage.close()


@pytest.mark.asyncio
async def test_iter_keys_returns_every_written_key(tmp_path: Path) -> None:
    """Contract relied on by :class:`FsmTimeoutSweeper`.

    The sweeper iterates every key in storage to find ones with
    state-name matching a configured rule. If ``iter_keys`` skipped
    keys with no state set (or any other "interesting" subset), the
    sweep would silently miss expirable sessions.
    """
    storage = SQLiteStorage(tmp_path / "iter.db")
    keys = [_key(user_id=u) for u in (10, 20, 30)]
    for k in keys:
        await storage.set_state(k, f"S{k.user_id}")
    found = list(await storage.iter_keys())
    assert sorted(k.user_id for k in found) == [10, 20, 30]
    await storage.close()


@pytest.mark.asyncio
async def test_close_is_idempotent(tmp_path: Path) -> None:
    """Application shutdown calls close twice in rare paths (the
    suppress(Exception) wrapper in app.py + a possible explicit
    shutdown hook). Double close MUST NOT raise — the second call
    just sees no open connection.
    """
    storage = SQLiteStorage(tmp_path / "close.db")
    await storage.set_state(_key(), "x")
    await storage.close()
    await storage.close()  # second call: no-op, no exception


@pytest.mark.asyncio
async def test_set_data_rejects_non_dict(tmp_path: Path) -> None:
    """Match :class:`MemoryStorage`'s contract: only dicts allowed.

    A handler that accidentally hands us a list / tuple / pydantic
    model would silently round-trip *something* if we used a permissive
    serializer; rejecting at write time keeps the schema honest.
    """
    storage = SQLiteStorage(tmp_path / "reject.db")
    with pytest.raises(TypeError, match="Data must be a dict"):
        await storage.set_data(_key(), [("not", "a", "dict")])  # type: ignore[arg-type]
    await storage.close()


@pytest.mark.asyncio
async def test_concurrent_first_use_opens_exactly_one_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two coroutines racing on first use must share one connection (#196).

    ``_connect`` has seven await points between the "already open?"
    check and the publish. Without a lock both racers see ``None``,
    both open a file handle, and whichever publishes first is
    overwritten — leaking a connection that ``close()`` can never
    reach, on every restart under load.
    """
    storage = SQLiteStorage(tmp_path / "race.db")
    opened: list[object] = []
    real_connect = aiosqlite.connect

    def counting_connect(*args: Any, **kwargs: Any) -> Any:
        opened.append(object())
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(aiosqlite, "connect", counting_connect)

    first, second = await asyncio.gather(storage._connect(), storage._connect())

    assert first is second
    assert len(opened) == 1
    await storage.close()


@pytest.mark.asyncio
async def test_failed_initialisation_does_not_leak_the_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pragma/DDL failure must close the half-open handle (#196).

    ``self._conn`` is still ``None`` at that point, so nothing else can
    ever reach the connection — if we don't close it here it lives
    until the process exits.
    """
    storage = SQLiteStorage(tmp_path / "broken.db")
    closed: list[bool] = []
    real_connect = aiosqlite.connect

    async def failing_initialise(conn: object) -> None:
        raise RuntimeError("pragma exploded")

    def tracking_connect(*args: Any, **kwargs: Any) -> Any:
        connection = real_connect(*args, **kwargs)

        async def wrapper() -> Any:
            opened: Any = await connection
            real_close = opened.close

            async def close_and_record() -> None:
                closed.append(True)
                await real_close()

            opened.close = close_and_record
            return opened

        return wrapper()

    monkeypatch.setattr(aiosqlite, "connect", tracking_connect)
    monkeypatch.setattr(SQLiteStorage, "_initialise", staticmethod(failing_initialise))

    with pytest.raises(RuntimeError, match="pragma exploded"):
        await storage._connect()

    assert closed == [True]
    # Nothing was published, so a later caller starts clean.
    assert storage._conn is None


@pytest.mark.asyncio
async def test_close_checkpoints_and_removes_the_wal_sidecar(tmp_path: Path) -> None:
    """``close()`` must leave no ``-wal`` behind (#279).

    We run the FSM in WAL mode, so every write lands in ``fsm.db-wal``
    first and only migrates into ``fsm.db`` at a checkpoint. SQLite
    checkpoints and unlinks the sidecar when the *last* connection to
    the file closes — which is exactly the call that webhook mode was
    never making: uvicorn re-raises the captured SIGTERM as
    ``serve()`` unwinds, so the runner's ``finally`` never ran and the
    process died with the connection open. The sidecar survived every
    restart, and the writes in it were only recovered because the next
    startup happened to open the same file.
    """
    path = tmp_path / "fsm.db"
    storage = SQLiteStorage(path)
    key = StorageKey(bot_id=1, chat_id=2, user_id=3)
    await storage.set_state(key, "Some:state")

    wal = path.with_name(path.name + "-wal")
    assert wal.exists(), "precondition: WAL mode should have created the sidecar"

    await storage.close()

    assert not wal.exists()
    # And the state really is in the main file, not lost with the WAL.
    reopened = SQLiteStorage(path)
    try:
        assert await reopened.get_state(key) == "Some:state"
    finally:
        await reopened.close()


async def _row_count(path: Path) -> int:
    """Row count read on a fresh connection.

    Deliberately NOT via ``iter_keys``: the point of these tests is
    that the row is gone from the *table*, and asserting through the
    same object that decides what to return would pass just as well
    against a filtering ``SELECT``.
    """
    async with (
        aiosqlite.connect(path) as conn,
        conn.execute("SELECT COUNT(*) FROM fsm_state") as cursor,
    ):
        row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.asyncio
async def test_clear_deletes_the_row_instead_of_leaving_an_empty_one(tmp_path: Path) -> None:
    """#195: a finished flow must not leave a row behind forever.

    ``FSMContext.clear()`` is ``set_state(None)`` then ``set_data({})``
    (``aiogram/fsm/context.py:42-44``). Before #195 that left a row
    with NULL state and ``'{}'`` data, which reads exactly like a
    missing row but still costs the sweeper one ``get_state`` round
    trip on every pass, forever. Prod's ``fsm.db`` had accumulated
    such rows since cutover.
    """
    path = tmp_path / "clear.db"
    storage = SQLiteStorage(path)
    key = _key()
    try:
        await storage.set_state(key, _DemoStates.awaiting_input)
        await storage.set_data(key, {"amount": 100})
        assert await _row_count(path) == 1

        # What FSMContext.clear() does, in order.
        await storage.set_state(key, None)
        # Still holding data at this point, so the row must survive.
        assert await _row_count(path) == 1
        await storage.set_data(key, {})

        assert await _row_count(path) == 0
        assert list(await storage.iter_keys()) == []
        # And the reads still answer the MemoryStorage defaults.
        assert await storage.get_state(key) is None
        assert await storage.get_data(key) == {}
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_dropping_the_state_of_a_dataless_row_deletes_it(tmp_path: Path) -> None:
    """#195: the cleanup lives on BOTH write methods, not just one.

    A flow that only ever set a state — no ``update_data`` — is empty
    the moment the state goes, so ``set_state(None)`` is the call that
    has to remove it. ``set_data`` never runs on that path and cannot
    cover for it.
    """
    path = tmp_path / "stateonly.db"
    storage = SQLiteStorage(path)
    key = _key()
    try:
        await storage.set_state(key, _DemoStates.awaiting_input)
        assert await _row_count(path) == 1

        await storage.set_state(key, None)

        assert await _row_count(path) == 0
        assert list(await storage.iter_keys()) == []
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_state_cleared_but_data_kept_leaves_the_row_alone(tmp_path: Path) -> None:
    """#195's predicate is two-conjunct on purpose.

    A row with no state but a real payload means a handler wrote data
    without setting a state. That is a wiring bug, and deleting it
    would silently destroy the evidence *and* the payload. Only rows
    that are empty on BOTH columns go.
    """
    path = tmp_path / "half.db"
    storage = SQLiteStorage(path)
    key = _key()
    try:
        await storage.set_data(key, {"opponent_id": 8})
        await storage.set_state(key, None)

        assert await _row_count(path) == 1
        assert [k.user_id for k in await storage.iter_keys()] == [key.user_id]
        assert await storage.get_data(key) == {"opponent_id": 8}
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_first_open_purges_empty_rows_left_by_older_revisions(tmp_path: Path) -> None:
    """#195: the fix has to clean up, not just stop making the mess.

    Every deployment already carries an ``fsm.db`` full of rows written
    before the write path learned to delete them, so the one-shot purge
    in ``_initialise`` is what actually resolves the ticket for prod.

    The "old" rows are produced by writing through the real storage and
    then blanking the columns with raw SQL — hand-writing the encoded
    key strings here would pin the private key format into a test that
    is not about the key format.
    """
    path = tmp_path / "legacy.db"
    keys = [_key(user_id=u) for u in (1, 2, 3, 4)]
    seed = SQLiteStorage(path)
    try:
        for k in keys:
            await seed.set_state(k, "DemoStates:awaiting_input")
        await seed.set_data(keys[3], {"opponent_id": 8})
    finally:
        await seed.close()

    async with aiosqlite.connect(path) as conn:
        # users 1 and 2: fully empty — exactly what a pre-#195
        # ``clear()`` left behind. User 3 keeps its state, user 4 keeps
        # its data; both must survive.
        await conn.execute(
            "UPDATE fsm_state SET state = NULL, data = '{}' "
            "WHERE key LIKE '%|1|%' OR key LIKE '%|2|%'"
        )
        await conn.execute("UPDATE fsm_state SET state = NULL WHERE key LIKE '%|4|%'")
        await conn.commit()
    assert await _row_count(path) == 4, "precondition: all four rows are still there"

    storage = SQLiteStorage(path)
    try:
        assert sorted(k.user_id for k in await storage.iter_keys()) == [3, 4]
        assert await storage.get_data(keys[3]) == {"opponent_id": 8}
    finally:
        await storage.close()
    assert await _row_count(path) == 2


@pytest.mark.asyncio
async def test_update_data_merges_and_returns_the_full_payload(tmp_path: Path) -> None:
    """The plain BaseStorage contract, re-asserted for the override.

    Cheap insurance: the override reimplements a method aiogram used to
    supply, so the boring behaviour has to be pinned here or nothing
    pins it at all.
    """
    storage = SQLiteStorage(tmp_path / "fsm.db")
    key = _key()
    try:
        await storage.set_data(key, {"a": 1})
        returned = await storage.update_data(key, {"b": 2})
        assert returned == {"a": 1, "b": 2}
        assert await storage.get_data(key) == {"a": 1, "b": 2}

        # The return value must be a copy: a caller mutating it must not
        # reach back into anything the storage holds.
        returned["c"] = 3
        assert await storage.get_data(key) == {"a": 1, "b": 2}
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_update_data_keeps_none_valued_keys(tmp_path: Path) -> None:
    """``dict.update`` semantics, not RFC 7396 merge-patch semantics.

    This is the test that forbids "just use SQLite's ``json_patch``"
    as a future simplification of :meth:`update_data`. ``json_patch``
    would DELETE ``opponent_id`` here; ``dict.update`` keeps it with a
    ``None`` value, and handlers distinguish the two
    (``"opponent_id" in data`` vs ``data.get("opponent_id")``).
    """
    storage = SQLiteStorage(tmp_path / "fsm.db")
    key = _key()
    try:
        await storage.set_data(key, {"opponent_id": 7, "stake": 100})
        await storage.update_data(key, {"opponent_id": None})
        stored = await storage.get_data(key)
        assert stored == {"opponent_id": None, "stake": 100}
        assert "opponent_id" in stored
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_update_data_rejects_non_dict(tmp_path: Path) -> None:
    """Same error contract as :meth:`set_data`, and raised before any I/O."""
    storage = SQLiteStorage(tmp_path / "fsm.db")
    key = _key()
    try:
        with pytest.raises(TypeError, match="must be a dict"):
            await storage.update_data(key, [("a", 1)])  # type: ignore[arg-type]
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_update_data_preserves_the_current_state(tmp_path: Path) -> None:
    """A data merge must not disturb the state column."""
    storage = SQLiteStorage(tmp_path / "fsm.db")
    key = _key()
    try:
        await storage.set_state(key, _DemoStates.awaiting_input)
        await storage.update_data(key, {"stake": 50})
        assert await storage.get_state(key) == "_DemoStates:awaiting_input"
        assert await storage.get_data(key) == {"stake": 50}
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_concurrent_update_data_does_not_lose_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#836: the regression that motivated overriding ``update_data``.

    aiogram's inherited implementation is ``get_data`` →
    ``dict.update`` → ``set_data``. Every one of those awaits real disk
    I/O on this backend, and aiogram dispatches updates concurrently
    (``DisabledEventIsolation``), so two handlers merging into the same
    key interleave and the later ``set_data`` overwrites the earlier
    one's keys wholesale.

    The interleaving is forced rather than raced, so the test cannot
    flake in either direction:

    * the first ``get_data`` parks on an event *after* reading, so the
      reader is guaranteed to be holding a stale snapshot;
    * the second task only starts once that park is reached.

    Without the per-key lock the second writer lands first and the
    first writer's blind overwrite drops ``"b"``. With it, the second
    task cannot even reach its ``get_data`` until the first update has
    committed — the park then simply times out, which is why the wait
    is bounded and its expiry suppressed.
    """
    storage = SQLiteStorage(tmp_path / "fsm.db")
    key = _key()
    first_read_done = asyncio.Event()
    second_write_done = asyncio.Event()
    real_get_data = storage.get_data
    calls = 0

    async def instrumented_get_data(k: StorageKey) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        result = await real_get_data(k)
        if calls == 1:
            first_read_done.set()
            # Bounded: under the fixed code nobody ever sets this, and
            # waiting forever would turn a pass into a hang.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(second_write_done.wait(), timeout=0.5)
        return result

    async def second_writer() -> None:
        await first_read_done.wait()
        await storage.update_data(key, {"b": 2})
        second_write_done.set()

    try:
        await storage.set_data(key, {})
        assert calls == 0, "precondition: set_data must not route through get_data"
        monkeypatch.setattr(storage, "get_data", instrumented_get_data)
        await asyncio.gather(
            storage.update_data(key, {"a": 1}),
            second_writer(),
        )
        # Read back through the unpatched bound method: re-assigning it
        # onto the instance would only add a second typing waiver.
        assert await real_get_data(key) == {"a": 1, "b": 2}
    finally:
        await storage.close()


@pytest.mark.asyncio
async def test_write_path_does_not_leak_lock_slots(tmp_path: Path) -> None:
    """The per-key lock registry must not grow without bound.

    ``KeyedLocks`` reference-counts and drops a slot once the last
    waiter leaves, but that only holds if every write path exits its
    context manager — including the one that raises. A leak here would
    be a slow memory bloat proportional to the number of distinct
    (bot, chat, user) triples the bot has ever served, which on this
    deployment is unbounded.
    """
    storage = SQLiteStorage(tmp_path / "fsm.db")
    keys = [_key(user_id=u) for u in range(20)]
    try:
        await asyncio.gather(*(storage.update_data(k, {"n": k.user_id}) for k in keys))
        await asyncio.gather(*(storage.set_state(k, _DemoStates.awaiting_input) for k in keys))
        await asyncio.gather(*(storage.set_data(k, {"n": 1}) for k in keys))
        assert len(storage._key_locks) == 0  # noqa: SLF001

        with pytest.raises(TypeError):
            await storage.update_data(keys[0], "nope")  # type: ignore[arg-type]
        assert len(storage._key_locks) == 0  # noqa: SLF001
    finally:
        await storage.close()


async def test_clear_removes_the_row_in_one_shot(tmp_path: Path) -> None:
    """#838: ``clear`` deletes the record rather than emptying it.

    The two-call sequence ``FSMContext.clear`` performs
    (``aiogram/fsm/context.py:42-44``) reaches the same observable
    state, but through an intermediate row and two commits. This
    asserts the end state AND that nothing is left for ``iter_keys``
    to hand the sweeper on the next pass.
    """
    storage = SQLiteStorage(tmp_path / "clear.db")
    key = _key(1)
    await storage.set_state(key, _DemoStates.awaiting_input)
    await storage.update_data(key, {"opponent_id": 200})

    await storage.clear(key)

    assert await storage.get_state(key) is None
    assert await storage.get_data(key) == {}
    assert await storage.iter_keys() == []
    await storage.close()


async def test_clear_on_an_unknown_key_is_a_no_op(tmp_path: Path) -> None:
    """Clearing a key that was never set must not raise or create a row.

    The sweeper reaches ``clear`` after its own re-read said the key
    was still there, but a handler finishing in between is exactly the
    race the re-read cannot close, so the call has to tolerate a row
    that vanished under it.
    """
    storage = SQLiteStorage(tmp_path / "clear_missing.db")

    await storage.clear(_key(999))

    assert await storage.iter_keys() == []
    assert len(storage._key_locks) == 0  # noqa: SLF001
    await storage.close()


@pytest.mark.asyncio
async def test_iter_records_returns_only_the_requested_states(tmp_path: Path) -> None:
    """#1451: the bulk scan filters in SQL, not in the caller.

    The sweeper only ever acts on keys whose state one of its rules
    covers. Reading every row's state to throw most of them away is
    the whole cost this method exists to remove, so the filter has to
    happen in the statement.
    """
    storage = SQLiteStorage(tmp_path / "records.db")
    await storage.set_state(_key(user_id=10), "WANTED")
    await storage.set_state(_key(user_id=20), "ALSO_WANTED")
    await storage.set_state(_key(user_id=30), "IGNORED")

    found = await storage.iter_records(["WANTED", "ALSO_WANTED"])

    assert sorted((k.user_id, state) for k, state, _ in found) == [
        (10, "WANTED"),
        (20, "ALSO_WANTED"),
    ]
    await storage.close()


@pytest.mark.asyncio
async def test_iter_records_carries_the_data_payload(tmp_path: Path) -> None:
    """The payload comes back with the row, not in a second query.

    ``sweep_once`` needs ``state_entered_at`` out of ``data`` to decide
    whether a key is even a candidate. If the bulk scan returned only
    keys and states, the per-key ``get_data`` would survive and half
    the round trips with it.
    """
    storage = SQLiteStorage(tmp_path / "records_data.db")
    key = _key(user_id=10)
    await storage.set_state(key, "WANTED")
    await storage.set_data(key, {"opponent_id": 77})

    ((got_key, got_state, got_data),) = await storage.iter_records(["WANTED"])

    assert got_key == key
    assert got_state == "WANTED"
    assert got_data == {"opponent_id": 77}
    await storage.close()


@pytest.mark.asyncio
async def test_iter_records_with_no_states_returns_nothing(tmp_path: Path) -> None:
    """An empty state list must not reach SQLite.

    ``... WHERE state IN ()`` is a syntax error, and a sweeper with no
    registered rules is a legal (if useless) configuration — refusing
    it here would turn an empty rule set into a crash every pass.
    """
    storage = SQLiteStorage(tmp_path / "records_empty.db")
    await storage.set_state(_key(user_id=10), "WANTED")

    assert await storage.iter_records([]) == []
    await storage.close()


@pytest.mark.asyncio
async def test_iter_records_survives_a_corrupt_data_blob(tmp_path: Path) -> None:
    """One unparsable row must not blind the sweeper to every other.

    Same posture as :meth:`get_data`, and for the same reason: the
    sweeper is the thing that returns held stakes, so a single bad
    blob taking out the whole pass is far worse than that one key
    reading as dataless.
    """
    path = tmp_path / "records_corrupt.db"
    storage = SQLiteStorage(path)
    await storage.set_state(_key(user_id=10), "WANTED")
    await storage.set_state(_key(user_id=20), "WANTED")
    await storage.close()

    async with aiosqlite.connect(path) as conn:
        await conn.execute("UPDATE fsm_state SET data = ? WHERE key LIKE ?", ("not json", "%|10|%"))
        await conn.commit()

    storage = SQLiteStorage(path)
    found = {k.user_id: data for k, _state, data in await storage.iter_records(["WANTED"])}

    assert found == {10: {}, 20: {}}
    await storage.close()
