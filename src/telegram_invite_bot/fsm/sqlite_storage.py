"""Persistent aiogram FSM storage backed by aiosqlite (T-012 / plan Stage 15).

Why a custom storage instead of the bundled :class:`MemoryStorage`?

The /cpc flow (Stages 32-36) leaves an FSM session pinned in
``awaiting_acceptance`` / ``awaiting_moves`` for up to 60 seconds.
:class:`MemoryStorage` is a plain dict — a process restart (deploy,
crash, OOM) erases it. The user experience is: someone's match
silently vaporises, the challenger stays "stuck busy" until the
sweeper next runs (which is also gone, because the sweeper lives in
the same process). Persistence across restarts removes that whole
class of footgun.

Why hand-roll instead of a community storage package?

* Existing 3rd-party SQLite storages for aiogram 3 are mostly
  abandoned or pinned to 3.0/3.1; we're on 3.28. Maintaining a thin
  ~150-line module is cheaper than vendoring an external dep that we
  then have to patch as aiogram evolves.
* We already run aiosqlite for the business engines (``db/engines.py``);
  reusing the driver keeps the dependency footprint flat.
* The :class:`BaseStorage` surface area is tiny (5 async methods +
  close). A minimal implementation is genuinely small.

Storage shape
-------------
A single SQLite file (default ``database/fsm.db``) with one table::

    fsm_state(
        key   TEXT PRIMARY KEY,  -- serialised StorageKey
        state TEXT,              -- nullable: None means "no current state"
        data  TEXT NOT NULL      -- JSON dict; defaults to '{}'
    )

Why one row per key (vs split state/data columns into separate rows):
state and data are read and written together often enough that a join
would be pure overhead, and a single row lets one statement carry both
columns. Reads dominate the workload (every update routed through
aiogram triggers ``get_state`` even when no handler is interested).

This paragraph used to claim that the single row also made
:meth:`update_data` atomic — "an UPDATE…RETURNING, or two statements in
one transaction". It did not: :meth:`update_data` was not overridden at
all, so it ran aiogram's inherited read-modify-write
(``aiogram/fsm/storage/base.py:173-184``) and the row shape had nothing
to do with it. The claim was wrong in a way that hid a real defect for
the life of the module (#836); see :meth:`update_data` for what closes
the window now, and why a lock rather than SQL.

Why no FK to anything in users.db / economy.db: the FSM table is
ephemeral state, not a system of record. The /cpc handler's
authorisation already validates ``user_id`` against business data;
storing the same id as a free-form INTEGER here keeps the FSM file
independent and disposable (``rm database/fsm.db`` is a valid
recovery action — every pinned user just falls back to "no state",
which is the same as a cold start).

Concurrency
-----------
A single :class:`aiosqlite.Connection` is held for the lifetime of
the storage. SQLite serialises writes globally per file regardless
of how many connections open it, so spawning a connection per call
would only add open/close overhead. WAL + ``synchronous=NORMAL`` is
set on connect to match the rest of the DB stack
(``db/pragma.py``); without WAL a slow handler would block reads
mid-flow.

Row lifetime
------------
A row is created on the first write for a key and DELETED again the
moment it becomes empty on both columns — no state and no data (#195).
That form is indistinguishable from a missing row on every read, so
keeping it would only cost the sweeper one ``get_state`` per pass, per
finished flow, forever. See ``_DELETE_IF_EMPTY`` below.

Iteration for the sweeper
-------------------------
:meth:`iter_keys` is the integration point with
:class:`scheduler.fsm_sweeper.FsmTimeoutSweeper`. Aiogram's
:class:`BaseStorage` deliberately does NOT expose iteration (different
backends have different "list keys" cost: Redis SCAN, Memory dict,
SQLite SELECT…). The sweeper duck-types on the presence of this
method — see ``_iter_storage_keys`` in the sweeper for the dispatch.

Returning a materialised list (not an async iterator) so a sweep
pass takes one short-lived read lock and then releases it; a sweep
that held a cursor open across handler invocations would compete
with writes and block the writer queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from loguru import logger

from telegram_invite_bot.utils.keyed_locks import KeyedLocks

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

log = logger.bind(component="fsm.sqlite_storage")


# Field order is the row-format contract. Keep stable: changing it
# invalidates every existing key in any deployed ``database/fsm.db``.
# All six fields of :class:`StorageKey` participate so two keys that
# differ only by ``destiny`` or ``thread_id`` get distinct rows (which
# matches aiogram's hashing behaviour — :class:`StorageKey` is a
# frozen dataclass and its ``__hash__`` includes every field).
_KEY_FIELDS: tuple[str, ...] = (
    "bot_id",
    "chat_id",
    "user_id",
    "thread_id",
    "business_connection_id",
    "destiny",
)
# ``|`` is safe as a separator because every key field is either an
# integer or aiogram's own ``destiny`` string (default ``"default"``)
# — none of which contain ``|``. JSON would be more bulletproof but
# also ~3× larger per key, and FSM tables grow one row per active
# user.
_KEY_SEP = "|"

# The JSON encoding of an empty data dict, byte-for-byte as
# :meth:`SQLiteStorage.set_data` writes it. Also the column DEFAULT in
# the CREATE TABLE below, so a row that never had data set matches too.
_EMPTY_DATA = "{}"

# #195: a row with no state AND no data is observationally identical to
# a missing row — ``get_state`` returns ``None`` for both and
# ``get_data`` returns ``{}`` for both. Keeping it costs one extra
# SELECT per sweeper pass (``sweep_once`` reads every key's state once
# per ``FsmTimeoutSweeper(interval_seconds=...)`` tick, 30 s as wired
# in ``app.start_background``) and one extra row in every
# ``is_user_busy`` scan, for the lifetime of the file. Since
# aiogram's ``FSMContext.clear()`` is ``set_state(None)`` followed by
# ``set_data({})`` (``aiogram/fsm/context.py:42-44``), EVERY completed
# flow used to leave one behind permanently.
#
# The predicate is deliberately narrow: ``data`` must ALSO be empty.
# A row with a state of ``NULL`` but a non-empty payload is a handler
# that wrote data without a state, and those rows stay — see
# :meth:`iter_keys`.
_DELETE_IF_EMPTY = "DELETE FROM fsm_state WHERE key = ? AND state IS NULL AND data = ?"


def _encode_key(key: StorageKey) -> str:
    parts: list[str] = []
    for field in _KEY_FIELDS:
        value = getattr(key, field)
        # None for optional fields (thread_id, business_connection_id)
        # becomes the literal empty token; an empty token never
        # collides with a real value because Telegram chat/user IDs
        # are always non-zero integers and ``destiny`` defaults to a
        # non-empty string.
        parts.append("" if value is None else str(value))
    return _KEY_SEP.join(parts)


def _decode_data(raw: Any, key_repr: str) -> dict[str, Any]:
    """Parse one ``data`` column blob into a dict. Never raises.

    Shared by :meth:`SQLiteStorage.get_data` and
    :meth:`SQLiteStorage.iter_records` (#1451) so the single-key
    read and the bulk scan cannot drift apart about what a
    corrupt row means. ``key_repr`` is only ever logged.
    """
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        # Defensive: a corrupted blob (manual SQL fiddling, disk
        # corruption) returns an empty dict. We deliberately do
        # NOT raise -- a single bad row shouldn't break the bot
        # for every other user. Logged at WARNING so the operator
        # sees it.
        log.bind(key=key_repr).warning("corrupt FSM data blob; returning empty dict")
        return {}
    if not isinstance(parsed, dict):
        log.bind(key=key_repr, got=type(parsed).__name__).warning(
            "FSM data was not a JSON object; returning empty dict"
        )
        return {}
    return parsed


def _decode_key(encoded: str) -> StorageKey:
    parts = encoded.split(_KEY_SEP)
    if len(parts) != len(_KEY_FIELDS):
        msg = f"corrupt FSM key row in DB: {encoded!r}"
        raise ValueError(msg)
    bot_id, chat_id, user_id, thread_id, biz_conn, destiny = parts
    return StorageKey(
        bot_id=int(bot_id),
        chat_id=int(chat_id),
        user_id=int(user_id),
        thread_id=int(thread_id) if thread_id else None,
        business_connection_id=biz_conn if biz_conn else None,
        destiny=destiny,
    )


def _require_dict(data: Mapping[str, Any]) -> None:
    """Reject a non-dict payload the way aiogram's own storages do.

    Same error contract as :class:`MemoryStorage` —
    ``aiogram.fsm.storage.base.DataNotDictLikeError`` would match
    exactly, but it's a private class in the storage module.
    ``TypeError`` is the structural fallback aiogram tests against, so
    we stay portable.
    """
    if not isinstance(data, dict):
        msg = f"Data must be a dict, got {type(data).__name__}"
        raise TypeError(msg)


class SQLiteStorage(BaseStorage):
    """SQLite-backed FSM storage. See module docstring for design notes.

    ``path`` is the SQLite file (parent dir is created if missing).
    Pass ``":memory:"`` for tests — but note that an in-memory DB
    defeats the whole point (persistence) so production MUST point at
    a real file. A startup check rejects in-memory in non-test code by
    convention only (test fixtures use it explicitly).
    """

    def __init__(self, path: str | Path) -> None:
        # Resolve relative paths against the process CWD at construction
        # time (not at first use) so a later ``os.chdir`` in tests
        # doesn't make the file appear in a surprising location.
        self._path: str = str(path) if path == ":memory:" else str(Path(path).resolve())
        self._conn: aiosqlite.Connection | None = None
        # Serialises :meth:`_connect`. The open sequence has seven await
        # points between the "already open?" check and the publish, so
        # two coroutines reaching first use concurrently would each open
        # their own connection — and only the last one published would
        # ever be closed, leaking the other for the life of the process.
        # (aiogram dispatches updates concurrently, so "first use" is
        # genuinely racy right after a restart.)
        self._connect_lock = asyncio.Lock()
        # Serialises the WRITE path per encoded key, so that
        # :meth:`update_data`'s read-modify-write cannot interleave with
        # another coroutine's write to the same key. See
        # :meth:`update_data` for why a lock and not SQL. Keyed rather
        # than global because two different chats have nothing to
        # serialise against each other, and the FSM write path is on the
        # critical path of every stateful handler.
        self._key_locks: KeyedLocks[str] = KeyedLocks()

    async def _connect(self) -> aiosqlite.Connection:
        """Lazy connection open. Idempotent — safe to call repeatedly.

        Lazy because :class:`SQLiteStorage` is instantiated by the
        dishka provider at app boot; opening the file there would
        force a sync filesystem touch before the event loop has even
        started running real work. Deferring to first use also means
        unit tests that construct the storage but never call a method
        don't leave a file dangling.
        """
        if self._conn is not None:
            return self._conn
        async with self._connect_lock:
            return await self._open_locked()

    async def _open_locked(self) -> aiosqlite.Connection:
        """Open and publish the connection. Caller must hold the lock."""
        # Re-check: a coroutine that queued on the lock while another was
        # opening must reuse that connection, not open a second one.
        if self._conn is not None:
            return self._conn
        # ``parents=True`` matches the convenience of how the engine
        # registry treats ``database/`` — operators shouldn't have to
        # ``mkdir -p`` before first boot. ``exist_ok=True`` for the
        # idempotency.
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(self._path)
        try:
            await self._initialise(conn)
        except BaseException:
            # Never leave a half-initialised connection behind: it would
            # be unreachable (``self._conn`` is still ``None``) and so
            # never closed. The next caller retries from scratch.
            with contextlib.suppress(Exception):
                await conn.close()
            raise
        self._conn = conn
        return conn

    @staticmethod
    async def _initialise(conn: aiosqlite.Connection) -> None:
        """Apply the pragmas and ensure the schema exists."""
        # PRAGMAs mirror ``db/pragma.py`` so the FSM file behaves the
        # same as the business engines under concurrency. ``WAL`` is
        # the big win: readers don't block writers. ``foreign_keys=ON``
        # is irrelevant for this schema (no FKs) but kept for
        # consistency in case a future refactor adds one.
        #
        # ``busy_timeout`` is stated before ``journal_mode`` for the
        # reason spelled out in ``db/pragma.py``: the WAL switch takes a
        # brief exclusive lock on a not-yet-WAL file. aiosqlite already
        # installs a 5s timeout at ``connect()``, so this is belt and
        # braces, not a fix for a live failure.
        await conn.execute("PRAGMA busy_timeout=5000")
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS fsm_state (
                key   TEXT PRIMARY KEY,
                state TEXT,
                data  TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        # #195: one-shot purge of the rows that accumulated before the
        # write path learned to clean up after itself. Cheap (a full
        # scan of a table whose live size is tens of rows) and runs
        # once per process, on the connection's first use. Restricted
        # to the same narrow predicate as ``_DELETE_IF_EMPTY`` so it
        # can never take a row a handler still cares about.
        await conn.execute(
            "DELETE FROM fsm_state WHERE state IS NULL AND data = ?",
            (_EMPTY_DATA,),
        )
        await conn.commit()

    async def set_state(self, key: StorageKey, state: str | State | None = None) -> None:
        async with self._key_locks.acquire(_encode_key(key)):
            await self._set_state_locked(key, state)

    async def _set_state_locked(self, key: StorageKey, state: str | State | None) -> None:
        """Body of :meth:`set_state`. Caller must hold the key's lock.

        Split out because :class:`asyncio.Lock` is not reentrant, so a
        method that already holds the lock cannot call the public
        wrapper.
        """
        # Normalise aiogram's two state-passing conventions (raw string
        # name or the :class:`State` object) to the wire format we
        # store on disk (the dotted name string, or NULL for "no state").
        state_value: str | None = state.state if isinstance(state, State) else state
        conn = await self._connect()
        encoded = _encode_key(key)
        # INSERT-or-UPDATE pattern: a key may not exist yet (first
        # state set) and we don't want to read-then-write. ``ON
        # CONFLICT`` updates ONLY the ``state`` column so a prior
        # ``set_data`` call's payload survives a state transition,
        # mirroring :class:`MemoryStorage` semantics where state and
        # data live in separate fields of the same record.
        await conn.execute(
            """
            INSERT INTO fsm_state(key, state, data) VALUES (?, ?, '{}')
            ON CONFLICT(key) DO UPDATE SET state = excluded.state
            """,
            (encoded, state_value),
        )
        if state_value is None:
            # Clearing the state may have emptied the row completely.
            # Both statements land in one implicit transaction (aiosqlite
            # under LEGACY_TRANSACTION_CONTROL opens one on the first DML
            # and holds it until the ``commit`` below), so the pair is
            # atomic against any OTHER connection.
            #
            # It is NOT hidden from readers inside this process, and the
            # comment here used to say it was: every caller shares the one
            # connection (:attr:`_conn`), and SQLite lets a connection see
            # its own uncommitted rows. A concurrent :meth:`iter_keys`
            # between these two statements does list the intermediate key.
            # Harmless in practice — ``get_state`` and ``get_data`` return
            # the same values for the empty row and for a missing one, and
            # the sweeper skips such a key (``sweep_once`` ``continue``s
            # on a ``None`` state) — but worth stating correctly.
            await conn.execute(_DELETE_IF_EMPTY, (encoded, _EMPTY_DATA))
        await conn.commit()

    async def get_state(self, key: StorageKey) -> str | None:
        conn = await self._connect()
        async with conn.execute(
            "SELECT state FROM fsm_state WHERE key = ?",
            (_encode_key(key),),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return None
        # NULL state column == "row exists but no current state". Both
        # are returned as ``None`` to match :class:`MemoryStorage`,
        # which collapses "never seen" and "state cleared" into the
        # same return value.
        state: str | None = row[0]
        return state

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        # Type check BEFORE taking the lock: a caller passing the wrong
        # type is a programming error, and rejecting it should not queue
        # behind another coroutine's disk write.
        _require_dict(data)
        async with self._key_locks.acquire(_encode_key(key)):
            await self._set_data_locked(key, data)

    async def _set_data_locked(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        """Body of :meth:`set_data`. Caller must hold the key's lock.

        Assumes ``data`` has already passed :func:`_require_dict`.
        """
        conn = await self._connect()
        payload = json.dumps(dict(data), ensure_ascii=False, separators=(",", ":"))
        await conn.execute(
            """
            INSERT INTO fsm_state(key, state, data) VALUES (?, NULL, ?)
            ON CONFLICT(key) DO UPDATE SET data = excluded.data
            """,
            (_encode_key(key), payload),
        )
        if payload == _EMPTY_DATA:
            # The second half of ``FSMContext.clear()``. After
            # ``set_state(None)`` left the row stateful-but-dataful,
            # this is the call that makes it fully empty — and so the
            # one that actually removes it.
            await conn.execute(_DELETE_IF_EMPTY, (_encode_key(key), _EMPTY_DATA))
        await conn.commit()

    async def clear(self, key: StorageKey) -> None:
        """Drop the whole record for ``key`` in one statement.

        Deliberately NOT part of the :class:`BaseStorage` API — aiogram
        has no ``clear`` there. What callers reach for is
        :meth:`FSMContext.clear`, which is ``set_state(None)`` followed
        by ``set_data({})`` (``aiogram/fsm/context.py:42-44``). Against
        this backend that pair is two statements, two commits and two
        fsyncs for what is conceptually one delete, and it has a gap:
        a crash between the two leaves a row with no state and a live
        payload. :meth:`iter_keys` keeps returning that row, the
        sweeper skips it forever (it treats ``state is None`` as
        "nothing to do"), and the startup reclaim only takes rows that
        are empty on BOTH columns — so it leaks until the same key is
        reused.

        One DELETE has neither problem. Callers holding a concrete
        storage object dispatch to this with ``getattr``, the way
        ``scheduler/fsm_sweeper.py`` already dispatches
        :meth:`iter_keys`; everyone else keeps the two-call sequence,
        which stays correct and is what :class:`MemoryStorage` needs.
        """
        encoded = _encode_key(key)
        async with self._key_locks.acquire(encoded):
            conn = await self._connect()
            await conn.execute("DELETE FROM fsm_state WHERE key = ?", (encoded,))
            await conn.commit()

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        conn = await self._connect()
        async with conn.execute(
            "SELECT data FROM fsm_state WHERE key = ?",
            (_encode_key(key),),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            # Match :class:`MemoryStorage`: a never-seen key returns
            # an empty dict, not ``None``. Handlers depend on this
            # (``data.get("opponent_id")`` rather than
            # ``data is None and …``).
            return {}
        return _decode_data(row[0], str(key))

    async def update_data(self, key: StorageKey, data: Mapping[str, Any]) -> dict[str, Any]:
        """Merge ``data`` into the stored payload and return the result.

        Overridden because aiogram's inherited version
        (``aiogram/fsm/storage/base.py:173-184``) is a plain
        read-modify-write: ``get_data`` → ``dict.update`` →
        ``set_data``. On :class:`MemoryStorage` that is safe by
        accident — none of the three steps suspends, so the sequence is
        effectively atomic. Here every step is real disk I/O behind an
        ``await``, and aiogram dispatches updates concurrently with
        ``DisabledEventIsolation``, so two handlers touching the same
        key interleave and the later write silently discards the
        earlier one's keys (#836).

        Why a lock rather than doing the merge in SQL: SQLite's
        ``json_patch`` implements RFC 7396 merge-patch, which DELETES a
        key whose patch value is ``null``. ``dict.update`` keeps it with
        a ``None`` value, so ``"foo" in data`` would start answering
        differently after ``update_data(key, {"foo": None})``. Handlers
        rely on the dict semantics, so we keep them exactly and pay for
        it with a lock.

        A process-local lock is sufficient: ``_assert_single_worker``
        guarantees exactly one process touches the FSM database. If that
        ever stops being true, this needs a real SQL-level solution, not
        a bigger lock.
        """
        _require_dict(data)
        async with self._key_locks.acquire(_encode_key(key)):
            current = await self.get_data(key)
            current.update(data)
            await self._set_data_locked(key, current)
        return current.copy()

    async def close(self) -> None:
        # Called from :meth:`Application.close`. Safe to call
        # multiple times because we null out ``self._conn`` after
        # closing — a second close finds nothing to do.
        if self._conn is None:
            return
        try:
            await self._conn.close()
        finally:
            self._conn = None

    async def iter_keys(self) -> Sequence[StorageKey]:
        """Return every :class:`StorageKey` currently stored.

        Used by :class:`FsmTimeoutSweeper` to discover stale sessions
        — see the sweeper's ``_iter_storage_keys``. The signature is
        intentionally a materialised sequence (list) rather than an
        async iterator: at today's FSM volumes (low thousands of active
        sessions) an iterator would only complicate the caller without
        saving meaningful memory.

        The list IS a snapshot, and the sweeper crosses many awaits
        while walking it — ``sweep_once`` re-reads state and data per
        key, takes the rule's guard, and on expiry makes a Telegram
        round trip (``rule.on_expire``). Rows therefore appear and
        vanish underneath it. That is safe by DESIGN, not by construction: a
        key deleted mid-pass reads back a ``None`` state and is
        skipped, a key mutated mid-pass is caught by the re-read under
        the rule's guard, and a key created mid-pass is simply picked
        up by the next pass.

        Since #195 a row with NULL ``state`` AND empty ``data`` no
        longer exists to be returned: the write path deletes it, because
        it is indistinguishable from a missing row on every read.

        Rows with NULL ``state`` but a NON-empty ``data`` payload ARE
        still returned. They mean a handler wrote data without setting a
        state, which is a wiring bug worth keeping visible. Note that
        the sweeper does not currently report them — ``sweep_once``
        ``continue``s on ``state_name is None`` before any logging —
        so "visible" today means "visible to whoever opens the file",
        not "logged".
        """
        conn = await self._connect()
        async with conn.execute("SELECT key FROM fsm_state") as cursor:
            rows = await cursor.fetchall()
        keys: list[StorageKey] = []
        for (encoded,) in rows:
            try:
                keys.append(_decode_key(encoded))
            except ValueError:
                # Corrupt key — log + skip. Same posture as
                # :meth:`get_data` for bad blobs: don't let one bad
                # row poison the sweep pass.
                log.bind(encoded=encoded).warning("corrupt FSM key row; skipping")
        return keys

    async def iter_records(
        self, states: Collection[str]
    ) -> Sequence[tuple[StorageKey, str, dict[str, Any]]]:
        """Return ``(key, state, data)`` for every row whose state is in ``states``.

        #1451. The sweeper used to pick its candidates with
        :meth:`iter_keys` followed by a :meth:`get_state` per key
        and a :meth:`get_data` per key a rule covered: ``1 + N``
        queries, worst case ``1 + 2N``, on every
        :class:`~scheduler.fsm_sweeper.FsmTimeoutSweeper` tick
        (``interval_seconds``, 30 s). All of them queue behind
        the ONE connection this class holds for the life of the
        process -- the same connection every aiogram update's
        ``get_state`` goes through -- so the pass was paid for in
        user-facing latency, and it grew with the number of rows
        the sweeper could never act on rather than the number it
        could.

        One statement returns exactly the rows a rule covers. The
        state list is the sweeper's registered rules: small,
        fixed at construction, and never user input, so the
        placeholder count is bounded and the ``IN`` list carries
        no injection surface.

        This does NOT replace the per-key re-reads ``sweep_once``
        performs under the rule's guard and again after
        ``on_expire`` returns. Those are the #258 / #837
        protection and have to be FRESH at the moment of the
        decision; what goes away here is only the reads that
        decide whether a key is a candidate at all, which are
        pure filtering.

        Duck-typed like :meth:`iter_keys` -- the sweeper uses this
        when the storage has it and falls back to the key-by-key
        walk otherwise, so :class:`MemoryStorage`, where the same
        reads are dict lookups that cost nothing, needs no
        equivalent.

        Same snapshot semantics as :meth:`iter_keys`: rows appear
        and vanish under the caller while it walks the result,
        which is safe because the caller re-reads before it acts.
        """
        if not states:
            # ``... WHERE state IN ()`` is a syntax error, and a
            # sweeper with no registered rules is a legal (if
            # useless) configuration -- turning that into a crash
            # every pass would be a worse answer than "nothing to
            # scan".
            return []
        placeholders = ",".join("?" * len(states))
        conn = await self._connect()
        async with conn.execute(
            f"SELECT key, state, data FROM fsm_state WHERE state IN ({placeholders})",  # noqa: S608
            tuple(states),
        ) as cursor:
            rows = await cursor.fetchall()
        records: list[tuple[StorageKey, str, dict[str, Any]]] = []
        for encoded, state, raw in rows:
            try:
                key = _decode_key(encoded)
            except ValueError:
                # Same posture as :meth:`iter_keys`: one corrupt
                # row must not blind the sweeper to every other.
                log.bind(encoded=encoded).warning("corrupt FSM key row; skipping")
                continue
            records.append((key, state, _decode_data(raw, str(key))))
        return records


__all__ = ["SQLiteStorage"]
