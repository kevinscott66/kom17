"""Engine registry creates all five DBs with correct pragmas + safety wiring.

These are real-filesystem integration tests: ``build_registry`` opens
aiosqlite engines against tmp paths and confirms pragmas were applied
through SQLAlchemy's connect event.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import text

from telegram_invite_bot.config.settings import AppEnv, Settings
from telegram_invite_bot.db import Checkpoint, build_registry
from telegram_invite_bot.db.engines import _WRITE_TXN_KEY
from telegram_invite_bot.db.names import ALL_DBS, DBName
from telegram_invite_bot.db.safety import UnboundedWriteError


@pytest.mark.asyncio
async def test_registry_creates_all_five_engines(
    make_settings: Callable[..., Settings],
) -> None:
    registry = build_registry(make_settings())
    try:
        assert set(registry.engines.keys()) == set(ALL_DBS)
        assert set(registry.sessions.keys()) == set(ALL_DBS)
    finally:
        await registry.dispose()


@pytest.mark.asyncio
async def test_pragmas_applied_on_connection(
    make_settings: Callable[..., Settings],
) -> None:
    registry = build_registry(make_settings())
    try:
        async with registry.engine(DBName.USERS).connect() as conn:
            journal = (await conn.execute(text("PRAGMA journal_mode"))).scalar_one()
            fk = (await conn.execute(text("PRAGMA foreign_keys"))).scalar_one()
            sync = (await conn.execute(text("PRAGMA synchronous"))).scalar_one()
        assert str(journal).lower() == "wal"
        assert fk == 1
        # synchronous=FULL maps to integer 2 in SQLite.
        assert sync == 2

        async with registry.engine(DBName.MODERATION).connect() as conn:
            sync = (await conn.execute(text("PRAGMA synchronous"))).scalar_one()
        # synchronous=NORMAL maps to 1.
        assert sync == 1
    finally:
        await registry.dispose()


@pytest.mark.asyncio
async def test_database_files_land_in_configured_dir(
    make_settings: Callable[..., Settings], tmp_path: Path
) -> None:
    registry = build_registry(make_settings())
    try:
        # Force a connection so the file materialises.
        async with registry.engine(DBName.USERS).connect() as conn:
            await conn.execute(text("SELECT 1"))
        expected = tmp_path / "db" / "users.db"
        assert expected.exists(), f"users.db not created at {expected}"
    finally:
        await registry.dispose()


@pytest.mark.asyncio
async def test_savepoint_write_is_undone_by_the_outer_rollback(
    make_settings: Callable[..., Settings],
) -> None:
    """A write inside ``begin_nested()`` must not outlive its transaction.

    pysqlite — and aiosqlite on top of it — never emits ``BEGIN`` by
    itself, and SQLite promotes a bare ``SAVEPOINT`` into a transaction
    of its own, so the matching ``RELEASE`` COMMITTED. Every service
    that wraps a money move in ``session.begin_nested()`` (transfer,
    payments, treasury, bonds) was therefore immune to the rollback
    ``middlewares.base`` performs when a handler raises: the coins had
    already landed, and the raise only reached the user as an error
    card. ``build_registry`` now drives BEGIN itself, which is what
    makes the "raise to undo" contract true for savepointed code.
    """
    registry = build_registry(make_settings())
    try:
        sessionmaker = registry.session(DBName.USERS)
        async with sessionmaker() as session:
            await session.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY, n INTEGER)"))
            await session.execute(text("INSERT INTO t (id, n) VALUES (1, 100)"))
            await session.commit()

        async with sessionmaker() as session:
            async with session.begin_nested():
                await session.execute(text("UPDATE t SET n = 300 WHERE id = 1"))
            await session.rollback()

        async with sessionmaker() as session:
            n = (await session.execute(text("SELECT n FROM t WHERE id = 1"))).scalar_one()
        assert n == 100, "the savepointed write survived a rollback of its transaction"
    finally:
        await registry.dispose()


@pytest.mark.asyncio
async def test_two_updates_writing_at_once_do_not_hit_a_locked_database(
    make_settings: Callable[..., Settings],
) -> None:
    """Two handlers racing the same row must both get through.

    Every handler has the same shape — read the state, decide, write —
    and the session middleware keeps one session open around all of it.
    Opening that session's transaction as DEFERRED pins the snapshot at
    the first read, and SQLite then refuses the later write outright
    with ``database is locked`` (SQLITE_BUSY_SNAPSHOT) the moment the
    other update commits: ``busy_timeout`` never gets a say. That is
    what turned two people tapping the same accept button into
    "⚠️ Произошла ошибка" instead of "уже обработано".

    The write transaction is IMMEDIATE for that reason, and it is taken
    lazily so read-only updates never queue behind a writer.
    """
    registry = build_registry(make_settings())
    try:
        sessionmaker = registry.session(DBName.USERS)
        async with sessionmaker() as session:
            await session.execute(text("CREATE TABLE r (id INTEGER PRIMARY KEY, n INTEGER)"))
            await session.execute(text("INSERT INTO r (id, n) VALUES (1, 0), (2, 0)"))
            await session.commit()

        async def read_then_write(row: int) -> None:
            async with sessionmaker() as session:
                await session.execute(text("SELECT n FROM r WHERE id = :i"), {"i": row})
                # The gap a real handler spends on its Telegram calls —
                # long enough for the other update to commit first.
                await asyncio.sleep(0.02)
                await session.execute(text("UPDATE r SET n = n + 1 WHERE id = :i"), {"i": row})
                await session.commit()

        await asyncio.gather(read_then_write(1), read_then_write(2))

        async with sessionmaker() as session:
            total = (await session.execute(text("SELECT SUM(n) FROM r"))).scalar_one()
        assert total == 2, "one of the two concurrent writes was lost"
    finally:
        await registry.dispose()


@pytest.mark.asyncio
async def test_checkpoint_lets_another_update_write_during_a_slow_call(
    make_settings: Callable[..., Settings],
) -> None:
    """A handler waiting on the network must not hold the write lock.

    The session lives for the whole update and the engine opens its
    transaction as ``BEGIN IMMEDIATE`` on the first write, so ``/ask``
    — which spends an AI quota slot and only then waits on DeepSeek —
    owned ``users.db`` for the entire model call. Every other update
    that wanted to write waited out ``busy_timeout`` (5 s) and then got
    ``database is locked``: one slow completion stalled the whole bot.

    :class:`Checkpoint` is what handlers call to end that transaction
    before the wait. Here the second writer runs *while the first
    session is still open* — with the checkpoint it goes straight
    through; without it, it blocks and raises.
    """
    registry = build_registry(make_settings())
    try:
        sessionmaker = registry.session(DBName.USERS)
        async with sessionmaker() as session:
            await session.execute(text("CREATE TABLE q (id INTEGER PRIMARY KEY, n INTEGER)"))
            await session.execute(text("INSERT INTO q (id, n) VALUES (1, 0), (2, 0)"))
            await session.commit()

        async with sessionmaker() as slow_update:
            # The quota slot / ``last_seen`` touch that precedes the call.
            await slow_update.execute(text("UPDATE q SET n = 1 WHERE id = 1"))
            checkpoint = Checkpoint()
            checkpoint.track(slow_update)
            await checkpoint()

            # ... and now the handler is off waiting on DeepSeek. Another
            # user's update arrives in the meantime.
            async with sessionmaker() as other_update:
                await other_update.execute(text("UPDATE q SET n = 2 WHERE id = 2"))
                await other_update.commit()

        async with sessionmaker() as session:
            rows = (await session.execute(text("SELECT id, n FROM q ORDER BY id"))).all()
        assert [tuple(row) for row in rows] == [(1, 1), (2, 2)]
    finally:
        await registry.dispose()


@pytest.mark.asyncio
async def test_checkpoint_skips_a_session_nobody_wrote_to(
    make_settings: Callable[..., Settings],
) -> None:
    """No transaction, no commit — the checkpoint is on the hot path of
    every AI/joke/quote call, and most updates only ever read.
    """
    registry = build_registry(make_settings())
    try:
        async with registry.session(DBName.USERS)() as session:
            checkpoint = Checkpoint()
            checkpoint.track(session)
            await checkpoint()
            assert not session.in_transaction()
    finally:
        await registry.dispose()


@pytest.mark.asyncio
async def test_safety_listener_blocks_prod_unbounded_delete(
    make_settings: Callable[..., Settings],
) -> None:
    registry = build_registry(make_settings(AppEnv.PROD))
    try:
        async with registry.engine(DBName.USERS).begin() as conn:
            await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))
        with pytest.raises(UnboundedWriteError):
            async with registry.engine(DBName.USERS).begin() as conn:
                await conn.execute(text("DELETE FROM t"))
    finally:
        await registry.dispose()


@pytest.mark.asyncio
async def test_the_safety_guard_runs_before_the_write_lock_is_taken(
    make_settings: Callable[..., Settings],
) -> None:
    """A rejected write must not have cost anything (#1654).

    Both the lazy-transaction promoter and the safety guard are
    ``before_cursor_execute`` listeners on the same engine, and
    SQLAlchemy calls them in REGISTRATION order. Registered the other
    way round, the promoter issues ``BEGIN IMMEDIATE`` first — so a
    statement the guard is about to refuse still takes the file's write
    lock, and holds it until the session unwinds. Every other writer on
    that DB waits behind a statement that never ran.

    ``_WRITE_TXN_KEY`` is the promoter's own flag, so "still False after
    the raise" is the same thing as "the promoter never got there".
    """
    registry = build_registry(make_settings(AppEnv.PROD))
    try:
        async with registry.engine(DBName.USERS).begin() as conn:
            await conn.execute(text("CREATE TABLE t (id INTEGER PRIMARY KEY)"))

        async with registry.engine(DBName.USERS).connect() as conn:
            sync_conn = conn.sync_connection
            assert sync_conn is not None
            assert sync_conn.info.get(_WRITE_TXN_KEY) is not True

            with pytest.raises(UnboundedWriteError):
                await conn.execute(text("DELETE FROM t"))

            assert sync_conn.info.get(_WRITE_TXN_KEY) is not True
    finally:
        await registry.dispose()
