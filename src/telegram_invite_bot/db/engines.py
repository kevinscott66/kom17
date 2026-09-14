"""Async engine + sessionmaker registry for the five SQLite databases.

One :class:`AsyncEngine` per file, wired with pragma + safety listeners.
The registry is constructed once (app-scoped via dishka) and disposed on
shutdown — never per-request.

URL format follows SQLAlchemy's aiosqlite dialect:
``sqlite+aiosqlite:///<relative-or-absolute-path>``.

Connection pooling: SQLAlchemy's aiosqlite dialect gives a file-backed
URL an ``AsyncAdaptedQueuePool`` and a memory URL a ``StaticPool``.
``NullPool`` is not the default in either case, and it is not what
``check_same_thread=False`` selects — the dialect injects that flag
unconditionally via ``create_connect_args``, with no bearing on the pool
class. The ``pool_size=5, max_overflow=10`` we pass is byte-for-byte
what ``QueuePool`` would have picked on its own, so those numbers are
documentation rather than tuning. They stay spelled out because the
write-transaction bookkeeping below stores its flag in
``Connection.info`` (:data:`_WRITE_TXN_KEY`, reset on ``checkout``),
which only makes sense on a pooled connection: moving to ``NullPool``
would be a behaviour change, not a knob.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from telegram_invite_bot.config.settings import PathsConfig, Settings
from telegram_invite_bot.db.names import ALL_DBS, DBName
from telegram_invite_bot.db.pragma import apply_pragmas
from telegram_invite_bot.db.safety import install as install_safety

log = logger.bind(component="db.engines")

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

# Key under which the "we already opened a write transaction on this
# connection" flag lives (``Connection.info`` — per pooled connection).
_WRITE_TXN_KEY = "_sqlite_write_txn"

# Leading keywords that must NOT open a write transaction. Everything
# else does, deliberately: the fail-safe direction here is "one more
# transaction than needed", never "a write that quietly autocommits"
# (that is exactly the SAVEPOINT bug this listener exists to fix).
# ``VACUUM``/``ANALYZE``/``ATTACH`` are refused inside a transaction and
# the codebase never issues them; they are listed so a future caller
# doesn't get a puzzling failure. ``ROLLBACK`` covers ``ROLLBACK TO
# SAVEPOINT``, which can only appear with a transaction already open.
_NON_WRITE_HEADS = frozenset(
    {
        "select",
        "pragma",
        "explain",
        "vacuum",
        "analyze",
        "attach",
        "detach",
        "begin",
        "commit",
        "rollback",
        "release",
    }
)


def resolve_db_path(paths: PathsConfig, db: DBName) -> Path:
    """Path on disk for a given DB.

    ``message_stats`` historically lives in its own dir (legacy migration
    artefact — see ``bot/config.py``). The other four sit under
    ``DATABASE_DIR``.
    """
    if db is DBName.MESSAGE_STATS:
        return paths.resolved_message_stats_dir() / "message_stats.db"
    return paths.database_dir / f"{db.value}.db"


def build_url(path: Path) -> str:
    # aiosqlite needs an explicit ``///`` for relative paths; ``Path``
    # rendering loses the leading slash on POSIX otherwise.
    return f"sqlite+aiosqlite:///{path}"


@dataclass(frozen=True, slots=True)
class EngineRegistry:
    """Frozen view onto five engines + sessionmakers, keyed by :class:`DBName`."""

    engines: dict[DBName, AsyncEngine]
    sessions: dict[DBName, async_sessionmaker[AsyncSession]]

    def engine(self, db: DBName) -> AsyncEngine:
        return self.engines[db]

    def session(self, db: DBName) -> async_sessionmaker[AsyncSession]:
        return self.sessions[db]

    async def dispose(self) -> None:
        for db, engine in self.engines.items():
            log.bind(db=db.value).debug("disposing engine")
            await engine.dispose()


def build_registry(settings: Settings) -> EngineRegistry:
    """Construct all five engines, attach listeners, return the registry.

    Idempotent only at the dishka-container level — calling twice in the
    same process creates duplicate engines/file-handles. Tests must
    ``await registry.dispose()`` in their teardown.
    """
    safety_listener = install_safety(settings.app_env)
    engines: dict[DBName, AsyncEngine] = {}
    sessions: dict[DBName, async_sessionmaker[AsyncSession]] = {}

    for db in ALL_DBS:
        path = resolve_db_path(settings.paths, db)
        # Parent dir must exist; ``mkdir(parents=True, exist_ok=True)`` is
        # safe even when the .db file is already there.
        path.parent.mkdir(parents=True, exist_ok=True)

        engine = create_async_engine(
            build_url(path),
            # NOTE: ``echo`` is wired off; loguru intercept handler in
            # ``config.logging`` forwards sqlalchemy.engine logs already.
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=False,  # aiosqlite + local file: no stale conns
            future=True,
        )

        # ``event.listens_for(engine.sync_engine, ...)`` wires into the
        # underlying DBAPI; aiosqlite proxies these calls correctly.
        @event.listens_for(engine.sync_engine, "connect")
        def _on_connect(dbapi_connection, _record, _db: DBName = db) -> None:  # type: ignore[no-untyped-def]
            # Take BEGIN away from the driver (see ``_on_begin``). Done
            # before the pragmas on purpose: ``journal_mode=WAL`` is
            # refused inside an open transaction, and autocommit is
            # where the driver leaves us until the first BEGIN.
            dbapi_connection.isolation_level = None
            apply_pragmas(dbapi_connection, _db)

        # The physical-transaction flag lives on the pooled connection's
        # ``info`` dict, so every listener below sees the same state.
        @event.listens_for(engine.sync_engine, "begin")
        @event.listens_for(engine.sync_engine, "commit")
        @event.listens_for(engine.sync_engine, "rollback")
        def _clear_write_txn(conn) -> None:  # type: ignore[no-untyped-def]
            conn.info[_WRITE_TXN_KEY] = False

        @event.listens_for(engine.sync_engine, "checkout")
        def _clear_write_txn_on_checkout(_dbapi_conn, record, _proxy) -> None:  # type: ignore[no-untyped-def]
            # Belt and braces: the pool resets a connection by calling
            # ``rollback()`` on the driver directly, which does not fire
            # the event above.
            record.info[_WRITE_TXN_KEY] = False

        # #1654: the guard goes on FIRST, and the order is the whole
        # point. SQLAlchemy calls same-event listeners in registration
        # order, so with the promoter in front a statement the guard is
        # about to refuse has already taken the file's write lock via
        # ``BEGIN IMMEDIATE`` below — and holds it until the session
        # unwinds, with every other writer on that DB queued behind a
        # statement that never ran. Registered here, a refusal costs
        # nothing. Pinned by
        # ``test_the_safety_guard_runs_before_the_write_lock_is_taken``.
        event.listens_for(engine.sync_engine, "before_cursor_execute")(safety_listener)

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def _promote_to_write_txn(  # type: ignore[no-untyped-def]
            conn, cursor, statement, parameters, context, executemany
        ) -> None:
            # pysqlite (and aiosqlite over it) does not emit BEGIN for
            # plain statements — and SQLite treats a bare ``SAVEPOINT``
            # as ``BEGIN DEFERRED``, so the matching ``RELEASE`` COMMITS.
            # Every write inside a ``session.begin_nested()`` block was
            # therefore durable the moment the savepoint released, and
            # the outer rollback in ``middlewares.base`` silently undid
            # nothing: a /send whose handler raised after the transfer
            # still moved the coins.
            #
            # So we open the transaction ourselves — but only when a
            # write is actually on its way, and as IMMEDIATE. Both parts
            # matter:
            #
            # * lazily, because a session lives for the whole update and
            #   most updates only read; starting every one of them as a
            #   writer would serialise the bot behind whichever handler
            #   is waiting on the Telegram API.
            # * IMMEDIATE, because a DEFERRED transaction that has
            #   already read takes its snapshot then, and SQLite refuses
            #   its later write outright ("database is locked",
            #   SQLITE_BUSY_SNAPSHOT — ``busy_timeout`` does not apply)
            #   as soon as anyone else committed in between. Two people
            #   racing the same button got "⚠️ Произошла ошибка"
            #   instead of "уже обработано". IMMEDIATE takes the write
            #   lock up front, so the loser waits out ``busy_timeout``
            #   and then proceeds.
            if conn.info.get(_WRITE_TXN_KEY):
                return
            head = statement.split(None, 1)
            if not head or head[0].lower() in _NON_WRITE_HEADS:
                return
            cursor.execute("BEGIN IMMEDIATE")
            conn.info[_WRITE_TXN_KEY] = True

        engines[db] = engine
        sessions[db] = async_sessionmaker(engine, expire_on_commit=False)
        log.bind(db=db.value, path=str(path)).info("engine ready")

    return EngineRegistry(engines=engines, sessions=sessions)
